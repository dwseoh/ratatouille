"""Entry point: one asyncio app with every loop (S3 integration).

    cd laptop && python -m coach.main                        # full run: ESP cam, S3, YOLO, voice, agent
    python -m coach.main --cam 0                             # laptop webcam instead of the ESP stream
    python -m coach.main --ctl 127.0.0.1                     # fake S3 (run `python ../tools/fake_s3.py` first)
    python -m coach.main --no-s3                             # in-process FakeLink, no UDP at all
    python -m coach.main --no-yolo                           # fixed fake detections instead of YOLO
    python -m coach.main --type                              # type instead of talking (no mic/Pipecat/Deepgram)
                                                             #   ("pause", "I'm back", "/button" = a BOOT press)
    python -m coach.main --fake-agent                        # echo agent, no Anthropic key needed
    python -m coach.main --closed-loop                       # stretch: dot finder + closed-loop correction
    python -m coach.main --button                            # BOOT button: start / pause / resume
    python -m coach.main --no-checkins --no-dashboard        # leave out the check-ins / the dashboard server

Who does what at runtime:
    FrameGrabber (thread)     -> frames for YOLO, the agent and calibration
    DetectorThread (thread)   -> YOLO on the newest frame, 2-of-3 smoothing -> State.dets
    Aimer.run    (30 Hz)      -> State.dets + calibration -> link.pan/tilt/laser
    ControlLink  (30 Hz)      -> UDP to the S3
    voice        (Pipecat)    -> intents (fast point/done/stop) + agent turns -> TTS
    Watcher                   -> event log, "looks done, next step?" asks (voice mode only)
    CheckIns                  -> the coach speaks up on its own: step intros + a comment after a quiet spell
    Timers                    -> the agent's set_timer: "time's up" through the speech queue when it's quiet
    Dashboard                 -> http://<laptop>:8000: live feed, step, captions, event log, latency (judges)
    Session                   -> "hey Remy" starts, "pause" / "I'm back" pause and resume, "start over" resets
Nothing here blocks the event loop: YOLO and frames each run in their own thread.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from pathlib import Path

from .aim import Aimer, Calibration
from .captions import CaptionTimeline
from .config import CONFIG as cfg
from .telemetry import LatencyLog
from .timers import Timers
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
    """The real step machine (it shows the step on TFT line 2 itself), else FakeSteps (always advances)."""
    try:
        from .steps import Steps
        return Steps(state, link, pushback=cfg.pushback_done)
    except (ImportError, TypeError) as e:
        log.info("using FakeSteps (%s)", e.__class__.__name__)
        return FakeSteps(state)


def make_agent(state: State, aimer, steps, link, fake: bool, *, timers=None, session=None):
    """`session`: the Session or a callable returning it (it only exists once speech does)."""
    if fake:
        return FakeAgent()
    from .agent import Agent, make_handlers   # needs the anthropic package + ANTHROPIC_API_KEY
    return Agent(cfg, state, make_handlers(state, aimer, steps, link, timers=timers, session=session))


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


class TypedSpeech(FakeSpeech):
    """--type: prints each sentence (or speaks it with --say), and runs the agent turns the app starts on its own
    (check-ins, the welcome back after a BOOT-button resume) in the background, like the voice pipeline does.
    Typing while one runs cancels it first (barge-in)."""

    def __init__(self, app: "App", say_aloud: bool = False):
        super().__init__(say_aloud=say_aloud)
        self._app, self._task = app, None

    async def chime(self, kind: str) -> None:
        print(f"  [chime: {kind}]", flush=True)

    async def interrupt(self) -> None:
        self.stop()
        await self.cancel_turn()

    async def cancel_turn(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            self._app.agent.cancel()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def agent_turn(self, text: str) -> None:
        await self.cancel_turn()
        frames = self._app.frames
        recent = frames.recent(cfg.frames_per_turn) if getattr(frames, "connected", True) else []
        self._task = asyncio.create_task(self._app.agent.turn(text, recent, self.say))


def _status(state: State) -> str:
    """One line after each typed turn: what the agent's state note is built from."""
    from .timers import describe
    bits = [state.note()]
    if state.session != "active":
        bits.append(f"session: {state.session}")
    if state.timers:
        bits.append(f"timers: {describe(state)}")
    if state.cook_notes:
        bits.append(f"cook notes: {'; '.join(state.cook_notes)}")
    if state.servings:
        bits.append(f"servings: {state.servings}")
    return " | ".join(bits)


async def type_loop(app: "App", lines=None) -> None:
    """--type: keyboard instead of the voice pipeline. Same fast intents, then an agent turn.
    `lines` (a list) is for tests; otherwise reads stdin until an empty line / Ctrl-D."""
    from .contracts import Intent
    from .intents import IntentRouter, allowed_while_paused   # the same router as voice mode (strict STOP, fuzzy names)
    router = IntentRouter(app.state.recipe, use_embeddings=not lines)   # tests (lines=...) skip the model
    it = iter(lines) if lines is not None else None
    if app.state.session == "idle":
        app.state.session = "active"        # typing mode needs no wake phrase (a pause survives a re-entry)
    session = app.session                   # None when a test drives type_loop on its own: no pause then
    if it is None:
        await asyncio.sleep(1.5)      # let the startup log lines finish, so the prompt below is the LAST thing shown
        print("\n" + "=" * 64 + "\n  TYPING MODE: click this terminal, type a line, press Enter.\n"
              "  Log lines may appear in between: ignore them and keep typing.\n"
              "  Try: where's the lime / set a timer for 1 minute / go back a step / pause / I'm back\n"
              "  /button = a BOOT press (start, pause, resume). Empty line (just Enter) or Ctrl-D quits.\n"
              + "=" * 64, flush=True)
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
        if text == "/button":
            app.button_pressed()
            await asyncio.sleep(0.05)
            print(f"  [button] session: {app.state.session}")
            continue
        app.state.transcript, app.state.last_activity = text, time.monotonic()   # typing = speaking, for check-ins
        if hasattr(app.speech, "cancel_turn"):
            await app.speech.cancel_turn()  # typing over a check-in = barge-in
        agent_text = text
        if app.state.session == "paused":   # only "I'm back" / "hey Remy", "start over" and safety get through
            if session is not None and await session.heard(text):
                agent_text = session.first_turn(text)
                print("  [session] resumed")
            elif not allowed_while_paused(text):
                print("  [paused] ignored. Type \"I'm back\" (or \"resume\") to carry on.")
                continue
        r = router.classify(text)
        if app.state.session == "paused" and r.intent is not Intent.START_OVER:
            r = router.classify("")         # paused safety question: no pointing, no step changes
        if r.intent is Intent.PAUSE:
            if session is not None:
                await session.pause(f"typed {text!r}")
            else:
                app.aimer.laser_off()
            print(f"  [intent] pause -> session: {app.state.session}")
            continue
        if r.intent.value == "point" and r.obj:
            print(f"  [intent] point_at({r.obj}) -> {app.aimer.point_at(r.obj)}")
        elif r.intent.value == "done":
            verdict = app.steps.propose_done('user', evidence=text)
            if verdict in ("advance", "finished"):
                app.aimer.laser_off()            # the target belonged to the step that just ended
            print(f"  [intent] done -> {verdict}")
        elif r.intent.value == "start_over":
            if session is not None:
                session.resume_now("start over")    # typing mode stays active: just un-pause
            app.aimer.laser_off(); app.steps.reset(); app.link.reset()
            print("  [intent] start over")
            continue
        elif r.intent.value == "stop":
            app.aimer.laser_off()
            app.speech.stop()
            continue
        # camera down: no frames at all (the agent then tells the user once), never stale ones
        frames = app.frames.recent(cfg.frames_per_turn) if getattr(app.frames, "connected", True) else []
        stats = await app.agent.turn(agent_text, frames, app.speech.say)
        app.state.last_activity = time.monotonic()
        ttft = f"{stats.ttft_s:.2f}s" if stats.ttft_s is not None else "n/a"
        u = getattr(app.agent, "last_usage", None)       # prompt cache check: "read" should cover all but the new turn
        cache = (f" | cache read {getattr(u, 'cache_read_input_tokens', 0)} / write "
                 f"{getattr(u, 'cache_creation_input_tokens', 0)} / uncached {getattr(u, 'input_tokens', 0)}") if u else ""
        print(f"  ttft {ttft} | tools: {', '.join(stats.tools) or 'none'}{cache} | {_status(app.state)}")


# ───────────────────────── the app ─────────────────────────

class App:
    """Everything wired together. Build with `await App.create(args)`, run with `await app.run()`."""

    def __init__(self, args, state, frames, link, calib, aimer, steps, agent):
        self.args, self.state, self.frames, self.link, self.calib = args, state, frames, link, calib
        self.aimer, self.steps, self.agent = aimer, steps, agent
        self.speech = None
        self.session = None
        self.checkins = None
        self.detector = None          # vision.DetectorThread when YOLO is on
        self.latency_log = LatencyLog(state, meta={"model": cfg.model, "fast": cfg.fast})   # logs/latency.jsonl
        self.captions = CaptionTimeline()       # voice mode: the dashboard caption follows the voice, one sentence at a time
        self.timers = Timers(state, link)       # the agent's set_timer; announced once speech exists (_start_timers)
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
        app = cls(args, state, frames, link, calib, aimer, steps, agent)
        if agent is None:                       # the pause tool needs the Session, which is built later
            app.agent = make_agent(state, aimer, steps, link, args.fake_agent, timers=app.timers,
                                   session=lambda: app.session)
        return app

    def _spawn(self, coro, name: str) -> None:
        self._tasks.append(asyncio.create_task(coro, name=name))

    def _start_detection(self) -> None:
        if self.args.no_yolo:
            self.state.dets = fake_dets(tuple(self.state.recipe.objects[:8]))
            log.info("fake detections: %s", ", ".join(sorted(self.state.visible())))
            return
        from .vision import Detector, DetectorThread   # ultralytics, heavy
        detector = Detector(cfg.yolo_weights, cfg.yolo_device, self.state.recipe.objects, imgsz=cfg.yolo_imgsz)
        self.detector = DetectorThread(detector, self.frames, self.state, asyncio.get_running_loop()).start()

    def _start_watcher(self) -> None:
        """The watcher: event log and "step done?" asks. Needs the voice pipeline."""
        from .watcher import Watcher
        w = Watcher(cfg, self.state, self.frames, self.speech, self.steps,
                    on_ask=self.speech.agent_turn, busy=lambda: self.agent.busy,
                    ignored=lambda: getattr(self.agent, "camera_ignored", False))
        if hasattr(self.steps, "verdict_provider"):
            self.steps.verdict_provider = w.latest_verdict     # lets Steps push back on a premature "done"
        self._spawn(w.run(), "watcher")

    def _start_checkins(self) -> None:
        """Check-ins: the coach speaks up on its own. Voice: through the pipeline (barge-in cancels it)."""
        if self.args.no_checkins or self.args.fake_agent:
            return
        from .checkin import CheckIns
        self.checkins = CheckIns(self.state, self.agent, self.frames, self.speech.say, self.steps,
                                 ask=getattr(self.speech, "agent_turn", None), level=cfg.checkins)
        if hasattr(self.agent, "checkins"):
            self.agent.checkins = self.checkins            # lets "talk less" / "talk more" change its level
        self._spawn(self.checkins.run(), "checkins")

    def _start_timers(self) -> None:
        """Timers announce through the speech queue, only when nobody is talking and no agent turn runs."""
        st = self.state
        quiet = lambda: not st.user_talking and not st.speaking and not getattr(self.agent, "busy", False)  # noqa: E731
        self._spawn(self.timers.run(self.speech.say, quiet), "timers")

    def _start_dashboard(self) -> None:
        """The dashboard on 0.0.0.0:DASHBOARD_PORT. Never takes the app down (missing package, port in use)."""
        if self.args.no_dashboard:
            return
        try:
            from . import dashboard
            captions = None if self.args.type else self.captions     # typing mode has no TTS timeline: latest sentence
            web = dashboard.build_app(self.state, self.frames, self.link, self.agent, captions)
        except ImportError as e:
            log.warning("no dashboard (%s): pip install fastapi uvicorn", e)
            return

        async def serve():
            try:
                await dashboard.serve(web, cfg.dashboard_port)
            except (OSError, SystemExit):          # uvicorn exits the process when the port is taken
                log.error("dashboard could not start on port %d (already in use?)", cfg.dashboard_port)
        self._spawn(serve(), "dashboard")

    def button_pressed(self) -> None:
        """BOOT (--button), or "/button" in typing mode: start when idle, pause when active, resume when paused.
        ("Start over" by voice still resets.)"""
        if self.session is None:
            return
        s = self.state.session
        coro = (self.session.start() if s == "idle" else self.session.pause("BOOT button") if s == "active"
                else self.session.resume("BOOT button", greet=True))
        asyncio.get_running_loop().create_task(coro)

    def _wire_button(self) -> None:
        """--button: the BOOT button starts, pauses and resumes (backup for the wake phrase and "pause")."""
        self.link.on_button(self.button_pressed)

    async def run(self, lines=None) -> None:
        self._spawn(self.aimer.run(), "aimer")
        self._spawn(tft_latency_loop(self.state, self.link), "tft-latency")
        self._spawn(self.latency_log.run(), "latency-log")
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
                from .session import Session
                self.speech = TypedSpeech(self, say_aloud=self.args.say)
                self.session = Session(self.state, self.speech, self.agent, self.aimer, self.steps, self.link)
                self.link.show_text(TFT_STATUS, "typing mode")
                self._start_checkins()
                self._start_timers()
                if self.args.button:
                    self._wire_button()
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
        worker, self.speech = build_pipeline(cfg, self.agent, self.frames, self.state, actions=actions,
                                             captions=self.captions)
        self.session = Session(self.state, self.speech, self.agent, self.aimer, self.steps, self.link)
        actions.session = self.session
        if not self.args.no_watcher and not self.args.fake_agent:
            self._start_watcher()
        self._start_checkins()
        self._start_timers()
        if self.args.button:
            self._wire_button()
        print('\n>>> say "hey Remy" to start, "pause" / "I\'m back" to pause and resume, "start over" to reset\n',
              flush=True)
        runner = WorkerRunner()
        await runner.add_workers(worker)
        await runner.run()

    async def shutdown(self) -> None:
        """Laser off and servos centred before exiting, whatever happened."""
        self.aimer.laser_off()
        self.link.reset()
        self.latency_log.flush()
        if self.detector is not None:
            self.detector.stop()
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
    ap.add_argument("--button", action="store_true", help="BOOT button starts / pauses / resumes the session")
    ap.add_argument("--no-watcher", action="store_true", help="don't run the watcher")
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
