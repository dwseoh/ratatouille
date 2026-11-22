"""main.py integration: the real Aimer + fakes for everything that needs hardware, keys or a mic."""
import asyncio
import re
import time

import numpy as np
import pytest

from coach import main as app_main
from coach.aim import Aimer, Calibration
from coach.contracts import (
    LAT_FIRST_AUDIO,
    LAT_SPEECH_END,
    TFT_LATENCY,
    Det,
    FakeAgent,
    FakeFrames,
    FakeLink,
    FakeSteps,
    State,
    TurnLatency,
)


class RecLink(FakeLink):
    """FakeLink that remembers everything instead of printing."""

    def __init__(self):
        super().__init__()
        self.texts: list[tuple[int, str]] = []
        self.resets = 0
        self.laser_seen = False

    def show_text(self, line, text):
        self.texts.append((line, text[:20]))

    def reset(self):
        super().reset()
        self.resets += 1


def linear_calib() -> Calibration:
    """pan = 60 + x/640*60, tilt = 30 + y/480*60: a plausible mapping for a 640x480 frame."""
    pix, ang = [], []
    for x in np.linspace(0, 640, 5):
        for y in np.linspace(0, 480, 5):
            pix.append((x, y))
            ang.append((60 + x / 640 * 60, 30 + y / 480 * 60))
    return Calibration().fit(pix, ang)


def args(*extra):
    return app_main.parse_args(["--type", "--no-yolo", "--no-s3", "--fake-agent", "--cam", "black",
                                "--no-dashboard", "--no-checkins", *extra])


@pytest.mark.timeout(15)
def test_type_mode_point_done_and_shutdown():
    link = RecLink()

    async def go():
        app = await app_main.App.create(args(), frames=FakeFrames(), link=link, calib=linear_calib(),
                                        agent=FakeAgent())
        assert isinstance(app.aimer, Aimer)
        seen = {}

        orig_turn = app.agent.turn

        async def spy_turn(text, frames, on_sentence):
            await asyncio.sleep(0.4)                  # let the 30 Hz aimer move the servos
            seen.setdefault(text, (app.aimer.target, link.laser, link.pan, link.tilt, app.state.step))
            return await orig_turn(text, frames, on_sentence)

        app.agent.turn = spy_turn
        await app.run(lines=["where's the lime", "ok I'm done"])
        return app, seen

    app, seen = asyncio.run(go())
    target, laser, pan, tilt, _ = seen["where's the lime"]
    assert target == "lime" and laser is True
    # moved away from the start position (90, 60) toward the lime's calibrated angles
    lime = app.state.det("lime").center
    gp, gt = linear_calib().angles_for(lime)
    assert abs(pan - 90) > 3 and abs(pan - gp) < abs(90 - gp)
    assert seen["ok I'm done"][4] == 1                # FakeSteps advanced to step 2
    # shutdown: laser off, S3 reset sent, step shown on the TFT
    assert link.laser is False and app.aimer.target is None and link.resets >= 1
    assert any(t.startswith("Step 2") for _, t in link.texts)


@pytest.mark.timeout(10)
def test_unknown_and_invisible_objects():
    link = RecLink()

    async def go():
        app = await app_main.App.create(args(), frames=FakeFrames(), link=link, calib=linear_calib(),
                                        agent=FakeAgent())
        app.state.dets = []                           # nothing visible
        assert app.aimer.point_at("lime") == "not_visible"
        assert app.aimer.point_at("spaceship") == "unknown_object"
        await app.shutdown()

    asyncio.run(go())
    assert link.laser is False


@pytest.mark.timeout(10)
def test_yolo_runs_in_detector_thread_with_smoothing(monkeypatch):
    """Without --no-yolo the app runs B's DetectorThread (2-of-3 smoothing, imgsz from cfg), the loop stays
    responsive while YOLO blocks, and shutdown stops the thread."""
    import coach.vision as vision

    class Frames(FakeFrames):
        def __init__(self):
            super().__init__()
            self.n = 0

        def latest(self):
            self.n += 1
            return np.zeros((48, 64, 3), np.uint8), time.time() + self.n   # always a "new" frame

    class SlowDetector:
        calls, imgsz = 0, None

        def __init__(self, weights, device, classes, *, imgsz=640, conf=None):
            SlowDetector.imgsz = imgsz

        def detect(self, frame):
            SlowDetector.calls += 1
            time.sleep(0.05)                          # blocking, like YOLO: must run off the loop
            # the lime every frame; a one-frame "bowl" false hit that smoothing must drop
            return [Det("lime", 0.9, (10, 10, 20, 20))] + ([Det("bowl", 0.9, (0, 0, 5, 5))] if SlowDetector.calls == 1 else [])

    monkeypatch.setattr(vision, "Detector", SlowDetector)

    async def go():
        a = app_main.parse_args(["--type", "--no-s3", "--fake-agent", "--cam", "black"])
        app = await app_main.App.create(a, frames=Frames(), link=RecLink(), calib=linear_calib(), agent=FakeAgent())
        app._start_detection()
        ticks, t0 = 0, time.monotonic()
        while time.monotonic() - t0 < 0.5:           # the loop keeps ticking while YOLO "runs"
            await asyncio.sleep(0.01)
            ticks += 1
        visible = app.state.visible()
        th = app.detector
        await app.shutdown()
        return visible, ticks, th

    visible, ticks, th = asyncio.run(go())
    assert isinstance(th, vision.DetectorThread)
    assert visible == {"lime"}                        # bowl seen once: not visible
    assert SlowDetector.calls >= 3 and SlowDetector.imgsz == app_main.cfg.yolo_imgsz
    assert ticks > 20
    assert not th._thread.is_alive()


@pytest.mark.timeout(10)
def test_tft_latency_line():
    link = RecLink()

    async def go():
        from coach.contracts import Recipe
        st = State(Recipe.load(app_main.LAPTOP / "recipes" / "guacamole.json"))
        turn = TurnLatency(1)
        turn.marks[LAT_SPEECH_END] = 10.0
        turn.marks[LAT_FIRST_AUDIO] = 10.85
        st.turns.append(turn)
        task = asyncio.create_task(app_main.tft_latency_loop(st, link))
        await asyncio.sleep(0.7)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert (TFT_LATENCY, "reply 850 ms") in link.texts


def test_make_steps_falls_back_to_fake():
    from coach.contracts import Recipe
    st = State(Recipe.load(app_main.LAPTOP / "recipes" / "guacamole.json"))
    steps = app_main.make_steps(st)
    assert hasattr(steps, "propose_done") and hasattr(steps, "on_change")


def test_missing_calibration_is_none(tmp_path):
    assert app_main.load_calibration(tmp_path / "nope.json") is None


def test_parse_args_defaults():
    a = app_main.parse_args([])
    assert not a.type and not a.no_yolo and not a.no_s3 and a.cam is None and not a.button


@pytest.mark.timeout(10)
def test_closed_loop_flag_starts_dot_tracker():
    link = RecLink()

    async def go():
        app = await app_main.App.create(args("--closed-loop"), frames=FakeFrames(), link=link,
                                        calib=linear_calib(), agent=FakeAgent())
        assert app.aimer.closed_loop
        await app.run(lines=["where's the lime"])
        return app

    app = asyncio.run(go())
    assert link.laser is False


def test_closed_loop_needs_calibration(monkeypatch):
    monkeypatch.setattr(app_main, "load_calibration", lambda path=None: None)   # no calibration.json

    async def go():
        return await app_main.App.create(args("--closed-loop"), frames=FakeFrames(), link=RecLink(),
                                         agent=FakeAgent())

    assert asyncio.run(go()).aimer.closed_loop is False



@pytest.mark.timeout(15)
def test_type_mode_runs_checkins_and_serves_the_dashboard(monkeypatch):
    """With a real (stub) agent the check-in loop starts, typing counts as activity, the dashboard answers."""
    import dataclasses
    import socket
    import urllib.request

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setattr(app_main, "cfg", dataclasses.replace(app_main.cfg, dashboard_port=port))

    class StubAgent(FakeAgent):
        pass

    async def go():
        a = app_main.parse_args(["--type", "--no-yolo", "--no-s3", "--cam", "black"])
        app = await app_main.App.create(a, frames=FakeFrames(), link=RecLink(), calib=linear_calib(), agent=StubAgent())
        app.speech = None
        from coach.contracts import FakeSpeech
        app.speech = FakeSpeech()
        app._start_dashboard()
        app._start_checkins()
        assert app.checkins is not None and app.checkins.level == app_main.cfg.checkins
        await asyncio.sleep(0.6)
        body = await asyncio.to_thread(lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/state", timeout=3).read())
        # typing resets the quiet timer
        before = app.state.last_activity
        await app_main.type_loop(app, ["where is the lime"])
        assert app.state.last_activity > before and app.state.transcript == "where is the lime"
        await app.shutdown()
        return body

    body = asyncio.run(go())
    assert b'"step"' in body and b'"session"' in body


def test_new_flags_parse():
    a = app_main.parse_args(["--no-checkins", "--no-dashboard"])
    assert a.no_checkins and a.no_dashboard


@pytest.mark.timeout(10)
def test_type_mode_sends_no_frames_when_the_camera_is_down():
    sent = []
    dead = FakeFrames()
    dead.connected = False

    async def go():
        app = await app_main.App.create(args(), frames=dead, link=RecLink(), calib=linear_calib(),
                                        agent=FakeAgent())
        orig = app.agent.turn

        async def spy(text, frames, on_sentence):
            sent.append(len(frames))
            return await orig(text, frames, on_sentence)
        app.agent.turn = spy
        await app.run(lines=["hello there"])

    asyncio.run(go())
    assert sent == [0]


@pytest.mark.timeout(20)
def test_full_seven_step_run_with_pushback_and_a_vision_ask():
    """Type through the whole recipe: a push-back on step 3, a vision 'ask' + confirm on step 5, finish at 7."""
    from coach.steps import StepVerdict

    link = RecLink()

    async def go():
        app = await app_main.App.create(args(), frames=FakeFrames(), link=link, calib=linear_calib(),
                                        agent=FakeAgent())
        from coach.contracts import FakeSpeech
        app.speech = FakeSpeech()
        clock = {"t": 0.0}

        def tick():
            clock["t"] += 10.0                         # every call is a separate utterance (> the 3 s guard)
            return clock["t"]
        app.steps._clock = tick
        app.steps.pushback = True                       # the optional camera veto (PUSHBACK_DONE=1)
        app.steps.verdict_provider = lambda step: StepVerdict(False, 0.9, "still big chunks") if step == 2 else None
        n = len(app.state.recipe.steps)
        log = []
        for i in range(n):
            assert app.state.step == i
            if i == 2:
                await app_main.type_loop(app, ["ok I'm done"])
                assert app.state.step == 2, "first 'done' on step 3 must be pushed back"
                log.append("push_back")
            if i == 4:
                assert app.steps.propose_done("vision", "looks done") == "ask" and app.state.step == 4
                assert app.steps.confirm() == "advance"
                log.append("ask+confirm")
                continue
            await app_main.type_loop(app, ["ok I'm done"])
        return app, log

    app, log = asyncio.run(go())
    assert log == ["push_back", "ask+confirm"]
    assert app.steps.finished and app.state.step == len(app.state.recipe.steps) - 1
    shown = [t for _, t in link.texts if t.startswith("Step ")]
    assert shown[0].startswith("Step 2") and len(shown) == len(app.state.recipe.steps) - 1
    assert any("watcher disagrees" in e.text for e in app.state.events)
    assert any("asking the user" in e.text for e in app.state.events)


# ───────────── pause / resume, timers, the new tools in typing mode ─────────────

def _spy_texts(app):
    texts = []
    orig = app.agent.turn

    async def spy(text, frames, on_sentence):
        texts.append(text)
        return await orig(text, frames, on_sentence)
    app.agent.turn = spy
    return texts


@pytest.mark.timeout(15)
def test_type_mode_pause_ignores_chatter_and_resumes_with_a_welcome_back():
    link = RecLink()

    async def go():
        app = await app_main.App.create(args(), frames=FakeFrames(), link=link, calib=linear_calib(), agent=FakeAgent())
        texts = _spy_texts(app)
        await app.run(lines=["where's the lime", "pause", "how chunky should it be", "I'm back, where was I?"])
        return app, texts

    app, texts = asyncio.run(go())
    assert texts[0] == "where's the lime" and len(texts) == 2          # the chatter while paused never reached it
    assert texts[1].startswith("[session resumed after") and texts[1].endswith("User: I'm back, where was I?")
    assert app.state.session == "active"
    assert (0, "paused: say I'm back") in link.texts and (0, "cooking") in link.texts
    events = [e.text for e in app.state.events]
    assert any(e.startswith("session paused") for e in events) and any(e.startswith("session resumed") for e in events)


@pytest.mark.timeout(15)
def test_type_mode_paused_safety_question_and_start_over():
    async def go():
        app = await app_main.App.create(args(), frames=FakeFrames(), link=RecLink(), calib=linear_calib(),
                                        agent=FakeAgent())
        texts = _spy_texts(app)
        app.steps.confirm()                                            # on step 2
        await app.run(lines=["give me a sec", "where's the lime", "ow I burned my hand", "start over"])
        return app, texts

    app, texts = asyncio.run(go())
    assert texts == ["ow I burned my hand"]                           # answered while paused; no pointing
    assert app.state.step == 0 and app.state.session == "active"      # "start over" works while paused


@pytest.mark.timeout(15)
def test_button_starts_pauses_and_resumes():
    async def go():
        app = await app_main.App.create(args(), frames=FakeFrames(), link=RecLink(), calib=linear_calib(),
                                        agent=FakeAgent())
        calls = []

        class Spy:
            async def start(self):
                calls.append("start")

            async def pause(self, why):
                calls.append("pause")

            async def resume(self, why, greet=False):
                calls.append(("resume", greet))
        app.button_pressed()                                           # no session yet: nothing
        app.session = Spy()
        for s in ("idle", "active", "paused"):
            app.state.session = s
            app.button_pressed()
        await asyncio.sleep(0.05)
        return calls

    assert asyncio.run(go()) == ["start", "pause", ("resume", True)]


@pytest.mark.timeout(15)
def test_type_mode_button_command_toggles_pause():
    async def go():
        app = await app_main.App.create(args(), frames=FakeFrames(), link=RecLink(), calib=linear_calib(),
                                        agent=FakeAgent())
        texts = _spy_texts(app)
        await app.run(lines=["/button", "how chunky should it be", "/button"])
        return app, texts

    app, texts = asyncio.run(go())
    assert app.state.session == "active"
    assert texts and texts[0].startswith("[session resumed after")    # the button's welcome back (a background turn)


@pytest.mark.timeout(15)
def test_timers_are_announced_through_the_speech_queue(capsys):
    async def go():
        app = await app_main.App.create(args(), frames=FakeFrames(), link=RecLink(), calib=linear_calib(),
                                        agent=FakeAgent())
        app.speech = app_main.TypedSpeech(app)
        app._start_timers()
        app.timers.set(1, "tea")
        await asyncio.sleep(1.6)
        await app.shutdown()
        return app

    app = asyncio.run(go())
    assert "[coach:reply] Time's up for the tea." in capsys.readouterr().out
    assert not app.state.timers and any(e.text == "timer done: tea" for e in app.state.events)


@pytest.mark.timeout(20)
def test_real_agent_tools_reach_timers_and_the_session_in_type_mode():
    """The real Agent (scripted client): set_timer and pause_session reach main's Timers and Session."""
    from test_agent import FakeClient, FakeStream, text, tool

    async def go():
        a = app_main.parse_args(["--type", "--no-yolo", "--no-s3", "--cam", "black", "--no-dashboard", "--no-checkins"])
        app = await app_main.App.create(a, frames=FakeFrames(), link=RecLink(), calib=linear_calib())
        app.agent.client = FakeClient(
            FakeStream([text("Timer set for one minute. "), tool("t1", "set_timer", {"seconds": 60, "label": "onions"})],
                       "tool_use"),
            FakeStream([text("Sure, I'll wait for you. "), tool("t2", "pause_session", {})], "tool_use"),
            FakeStream([text("Welcome back, you're on step one. ")]))
        await app.run(lines=["set a one minute timer for the onions", "I have to take a call", "I'm back"])
        return app

    app = asyncio.run(go())
    reqs = app.agent.client.requests
    assert len(reqs) == 3 and app.state.session == "active"
    assert re.search(r"timers: onions (1:00|0:5\d) left", reqs[1]["messages"][-1]["content"][-1]["text"])
    last = reqs[2]["messages"][-1]["content"]
    assert last[0] == {"type": "tool_result", "tool_use_id": "t2",
                       "content": "ok: paused; you stay quiet until they say they're back"}
    assert "[session resumed after" in last[-1]["text"] and "User: I'm back" in last[-1]["text"]
