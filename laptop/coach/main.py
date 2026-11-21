"""Entry point: one asyncio app with every loop (S3 integration).

    cd laptop && python -m coach.main                        # full run: ESP cam, S3, YOLO, voice, agent
    python -m coach.main --cam 0                             # laptop webcam instead of the ESP stream
    python -m coach.main --ctl 127.0.0.1                     # fake S3 (run `python ../tools/fake_s3.py` first)
    python -m coach.main --no-s3                             # in-process FakeLink, no UDP at all
    python -m coach.main --no-yolo                           # fixed fake detections instead of YOLO
    python -m coach.main --type                              # type instead of talking (no mic/Pipecat/Deepgram)
    python -m coach.main --fake-agent                        # echo agent, no Anthropic key needed
    python -m coach.main --closed-loop                       # stretch: dot finder + closed-loop correction
    python -m coach.main --no-checkins --no-dashboard        # leave out C's check-ins / the dashboard server

Who does what at runtime:
    FrameGrabber (A, thread)  -> frames for YOLO, the agent and calibration
    detect_loop  (B's Detector in a worker thread) -> State.dets
    Aimer.run    (A, 30 Hz)   -> State.dets + calibration -> link.pan/tilt/laser
    ControlLink  (A, 30 Hz)   -> UDP to the S3
    voice        (B, Pipecat) -> intents (fast point/done/stop) + agent turns (C) -> TTS
    Watcher      (C, loop 3)  -> event log, "looks done, next step?" asks (voice mode only)
    CheckIns     (C)          -> the coach speaks up on its own: step intros + a comment after a quiet spell
    Dashboard    (C)          -> http://<laptop>:8000: live feed, step, captions, event log, latency (judges)
    Session      (B)          -> "hey Remy" starts, "start over" resets
Nothing here blocks the event loop: YOLO runs in an executor, frames in their own thread.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .aim import Aimer, Calibration
from .config import CONFIG as cfg
from .contracts import (
    LAT_FIRST_AUDIO,
    TFT_LATENCY,
    TFT_STATUS,
    TFT_STEP,
    FakeAgent,
    FakeFrames,
    FakeLink,
    FakeSpeech,
    FakeSteps,
    KeywordRouter,
    Recipe,
    State,
    fake_dets,
)

log = logging.getLogger("coach.main")
LAPTOP = Path(__file__).resolve().parent.parent
CALIB_PATH = LAPTOP / "calibration.json"


# ───────────────────────── builders (each one swappable for a fake) ─────────────────────────

def load_calibration(path: Path = CALIB_PATH) -> Calibration | None:
    if not path.exists():
        log.warning("no %s: laser won't aim (run: python -m coach.calibrate)", path.name)
        return None
    return Calibration.load(path)


def make_steps(state: State, link=None):
    """C's step machine (it shows the step on TFT line 2 itself), else FakeSteps (always advances)."""
    try:
        from .steps import Steps
        return Steps(state, link)
    except (ImportError, TypeError) as e:
        log.info("using FakeSteps (%s)", e.__class__.__name__)
        return FakeSteps(state)


def make_agent(state: State, aimer, steps, link, fake: bool):
    if fake:
        return FakeAgent()
    from .agent import Agent, make_handlers   # needs the anthropic package + ANTHROPIC_API_KEY
    return Agent(cfg, state, make_handlers(state, aimer, steps, link))


async def make_link(args):
    if args.no_s3:
        return FakeLink()
    from .control import ControlLink
    return await ControlLink(args.ctl or cfg.ctl_host, cfg.ctl_port, cfg.control_hz).start()


def make_frames(args):
    if args.cam == "black":
        return FakeFrames()
    from .frames import FrameGrabber
    return FrameGrabber(args.cam if args.cam is not None else cfg.cam_url).start()


# ───────────────────────── loops ─────────────────────────

async def detect_loop(frames, state: State, detector) -> None:
    """B's YOLO, one frame at a time in a worker thread. Only writes State.dets (from the loop)."""
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="yolo")
    last_t, fails = 0.0, 0
    while True:
        frame, t = frames.latest()
        if frame is None or t == last_t:
            await asyncio.sleep(0.01)
            continue
        last_t = t
        try:
            state.dets = await loop.run_in_executor(pool, detector.detect, frame)
            fails = 0
        except Exception:
            fails += 1
            if fails in (1, 10, 100):
                log.exception("detector failed (%d in a row)", fails)
            await asyncio.sleep(0.2)


async def tft_latency_loop(state: State, link) -> None:
    """TFT line 3: speech end -> first audio of the last finished turn."""
    shown = None
    while True:
        await asyncio.sleep(0.5)
        if not state.turns:
            continue
        turn = state.turns[-1]
        ms = turn.ms().get(LAT_FIRST_AUDIO)
        if ms is not None and (turn.turn_id, ms) != shown:
            shown = (turn.turn_id, ms)
            link.show_text(TFT_LATENCY, f"reply {ms} ms")


async def health_loop(frames, link, calib) -> None:
    """Log (and show on TFT line 0) when the camera or the S3 drops out or comes back."""
    was = None
    while True:
        await asyncio.sleep(1.0)
        now = (bool(getattr(frames, "connected", True)), bool(link.online))
        if now != was:
            cam, s3 = now
            log.info("camera %s | S3 %s", "ok" if cam else "DOWN", "ok" if s3 else "DOWN")
            if not cam:
                link.show_text(TFT_STATUS, "camera down")
            elif calib is None:
                link.show_text(TFT_STATUS, "NO CALIB: aim off")
            was = now


async def type_loop(app: "App", lines=None) -> None:
    """--type: keyboard instead of the voice pipeline. Same fast intents, then an agent turn.
    `lines` (a list) is for tests; otherwise reads stdin until an empty line / Ctrl-D."""
    router = KeywordRouter(app.state.recipe)
    it = iter(lines) if lines is not None else None
    app.state.session = "active"
    while True:
        if it is not None:
            text = next(it, "")
        else:
            try:
                text = (await asyncio.to_thread(input, "\nyou> ")).strip()
            except EOFError:
                text = ""
        if not text:
            return
        app.state.transcript, app.state.last_activity = text, time.monotonic()   # typing = speaking, for check-ins
        r = router.classify(text)
        if r.intent.value == "point" and r.obj:
            print(f"  [intent] point_at({r.obj}) -> {app.aimer.point_at(r.obj)}")
        elif r.intent.value == "done":
            print(f"  [intent] done -> {app.steps.propose_done('user', evidence=text)}")
        elif r.intent.value == "start_over":
            app.aimer.laser_off(); app.steps.reset(); app.link.reset()
            print("  [intent] start over")
            continue
        elif r.intent.value == "stop":
            app.speech.stop()
            continue
        # camera down: no frames at all (the agent then tells the user once), never stale ones
        frames = app.frames.recent(cfg.frames_per_turn) if getattr(app.frames, "connected", True) else []
        stats = await app.agent.turn(text, frames, app.speech.say)
        app.state.last_activity = time.monotonic()
        ttft = f"{stats.ttft_s:.2f}s" if stats.ttft_s is not None else "n/a"
        print(f"  ttft {ttft} | tools: {', '.join(stats.tools) or 'none'} | {app.state.note()}")


# ───────────────────────── the app ─────────────────────────

class App:
    """Everything wired together. Build with `await App.create(args)`, run with `await app.run()`."""

    def __init__(self, args, state, frames, link, calib, aimer, steps, agent):
        self.args, self.state, self.frames, self.link, self.calib = args, state, frames, link, calib
        self.aimer, self.steps, self.agent = aimer, steps, agent
        self.speech = None
        self.session = None
        self.checkins = None
        self._tasks: list[asyncio.Task] = []

    @classmethod
    async def create(cls, args, *, frames=None, link=None, calib=None, agent=None) -> "App":
        recipe = Recipe.load(LAPTOP / cfg.recipe_path)
        state = State(recipe)
        frames = frames if frames is not None else make_frames(args)
        link = link if link is not None else await make_link(args)
        calib = calib if calib is not None else load_calibration()
        closed = bool(getattr(args, "closed_loop", False)) and calib is not None
        aimer = Aimer(link, calib, state, hz=cfg.control_hz, closed_loop=closed)
        steps = make_steps(state, link)
        if isinstance(steps, FakeSteps):
            steps.on_change(lambda n: link.show_text(TFT_STEP, f"Step {n + 1}: {recipe.steps[n].short}"))
        agent = agent if agent is not None else make_agent(state, aimer, steps, link, args.fake_agent)
        return cls(args, state, frames, link, calib, aimer, steps, agent)

    def _spawn(self, coro, name: str) -> None:
        self._tasks.append(asyncio.create_task(coro, name=name))

    def _start_detection(self) -> None:
        if self.args.no_yolo:
            self.state.dets = fake_dets(tuple(self.state.recipe.objects[:8]))
            log.info("fake detections: %s", ", ".join(sorted(self.state.visible())))
            return
        from .vision import Detector   # ultralytics, heavy
        detector = Detector(cfg.yolo_weights, cfg.yolo_device, self.state.recipe.objects)
        self._spawn(detect_loop(self.frames, self.state, detector), "yolo")

    def _start_watcher(self) -> None:
        """C's watcher (loop 3): event log and "step done?" asks. Needs the voice pipeline."""
        from .watcher import Watcher
        w = Watcher(cfg, self.state, self.frames, self.speech, self.steps,
                    on_ask=self.speech.agent_turn, busy=lambda: self.agent.busy)
        if hasattr(self.steps, "verdict_provider"):
            self.steps.verdict_provider = w.latest_verdict     # lets Steps push back on a premature "done"
        self._spawn(w.run(), "watcher")

    def _start_checkins(self) -> None:
        """C's check-ins: the coach speaks up on its own. Voice: through the pipeline (barge-in cancels it)."""
        if self.args.no_checkins or self.args.fake_agent:
            return
        from .checkin import CheckIns
        self.checkins = CheckIns(self.state, self.agent, self.frames, self.speech.say, self.steps,
                                 ask=getattr(self.speech, "agent_turn", None), level=cfg.checkins)
        if hasattr(self.agent, "checkins"):
            self.agent.checkins = self.checkins            # lets "talk less" / "talk more" change its level
        self._spawn(self.checkins.run(), "checkins")

    def _start_dashboard(self) -> None:
        """C's dashboard on 0.0.0.0:DASHBOARD_PORT. Never takes the app down (missing package, port in use)."""
        if self.args.no_dashboard:
            return
        try:
            from . import dashboard
            web = dashboard.build_app(self.state, self.frames, self.link, self.agent)
        except ImportError as e:
            log.warning("no dashboard (%s): pip install fastapi uvicorn", e)
            return

        async def serve():
            try:
                await dashboard.serve(web, cfg.dashboard_port)
            except (OSError, SystemExit):          # uvicorn exits the process when the port is taken
                log.error("dashboard could not start on port %d (already in use?)", cfg.dashboard_port)
        self._spawn(serve(), "dashboard")

    def _wire_button(self) -> None:
        """--button: BOOT press starts the session when idle, resets it when active (backup for the wake phrase)."""
        def pressed():
            if self.session is None:
                return
            coro = self.session.start() if self.state.session == "idle" else self.session.reset()
            asyncio.get_running_loop().create_task(coro)
        self.link.on_button(pressed)

    async def run(self, lines=None) -> None:
        self._spawn(self.aimer.run(), "aimer")
        self._spawn(tft_latency_loop(self.state, self.link), "tft-latency")
        self._spawn(health_loop(self.frames, self.link, self.calib), "health")
        self._start_detection()
        self._start_dashboard()
        if self.aimer.closed_loop:
            from .dot import DotTracker   # stretch: finds the real dot (with blink checks), writes State.dot
            self._spawn(DotTracker(self.frames, self.link, self.state, hz=cfg.control_hz).run(), "dot")
            log.info("closed-loop aiming ON (dot tracker running)")
        try:
            await self.agent.prewarm()
            if self.args.type:
                self.speech = FakeSpeech(say_aloud=self.args.say)
                self.link.show_text(TFT_STATUS, "typing mode")
                self._start_checkins()
                await type_loop(self, lines)
            else:
                await self._run_voice()
        finally:
            await self.shutdown()

    async def _run_voice(self) -> None:
        from pipecat.workers.runner import WorkerRunner

        from .intents import IntentActions, IntentRouter
        from .session import Session
        from .voice import build_pipeline

        actions = IntentActions(IntentRouter(self.state.recipe), self.state, self.aimer, self.steps)
        worker, self.speech = build_pipeline(cfg, self.agent, self.frames, self.state, actions=actions)
        self.session = Session(self.state, self.speech, self.agent, self.aimer, self.steps, self.link)
        actions.session = self.session
        if not self.args.no_watcher and not self.args.fake_agent:
            self._start_watcher()
        self._start_checkins()
        if self.args.button:
            self._wire_button()
        print('\n>>> say "hey Remy" to start, "start over" to reset\n', flush=True)
        runner = WorkerRunner()
        await runner.add_workers(worker)
        await runner.run()

    async def shutdown(self) -> None:
        """Laser off and servos centred before exiting, whatever happened."""
        self.aimer.laser_off()
        self.link.reset()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await asyncio.sleep(0.1)        # let the last R / C packets go out
        for obj, meth in ((self.frames, "stop"), (self.link, "close")):
            fn = getattr(obj, meth, None)
            if fn is not None:
                try:
                    fn()
                except Exception:
                    log.exception("%s.%s failed", type(obj).__name__, meth)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="python -m coach.main", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cam", default=None, help="CAM_URL override: 0 = webcam, a file, a URL, or 'black'")
    ap.add_argument("--ctl", default=None, help="S3 host override (127.0.0.1 for tools/fake_s3.py)")
    ap.add_argument("--no-s3", action="store_true", help="in-process FakeLink instead of UDP")
    ap.add_argument("--no-yolo", action="store_true", help="fixed fake detections of the recipe objects")
    ap.add_argument("--type", action="store_true", help="type instead of talking")
    ap.add_argument("--say", action="store_true", help="with --type: speak replies with macOS `say`")
    ap.add_argument("--fake-agent", action="store_true", help="echo agent, no API key needed")
    ap.add_argument("--button", action="store_true", help="BOOT button starts/resets the session")
    ap.add_argument("--no-watcher", action="store_true", help="don't run C's watcher (loop 3)")
    ap.add_argument("--no-checkins", action="store_true", help="the coach never speaks unprompted")
    ap.add_argument("--no-dashboard", action="store_true", help="don't start the dashboard web server")
    ap.add_argument("--closed-loop", action="store_true",
                    help="stretch: find the real laser dot and correct the aim (needs calibration.json)")
    return ap.parse_args(argv)


async def amain(argv=None) -> None:
    args = parse_args(argv)
    app = await App.create(args)
    await app.run()


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(amain(argv))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
