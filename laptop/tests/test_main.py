"""main.py integration: the real Aimer + fakes for everything that needs hardware, keys or a mic."""
import asyncio
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
    return app_main.parse_args(["--type", "--no-yolo", "--no-s3", "--fake-agent", "--cam", "black", *extra])


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
def test_detect_loop_writes_dets_without_blocking():
    class Frames:
        def __init__(self):
            self.n = 0

        def latest(self):
            self.n += 1
            return np.zeros((48, 64, 3), np.uint8), time.time() + self.n   # always a "new" frame

    class SlowDetector:
        calls = 0

        def detect(self, frame):
            SlowDetector.calls += 1
            time.sleep(0.05)                          # blocking, like YOLO: must run off the loop
            return [Det("lime", 0.9, (10, 10, 20, 20))]

    async def go():
        from coach.contracts import Recipe
        st = State(Recipe.load(app_main.LAPTOP / "recipes" / "guacamole.json"))
        task = asyncio.create_task(app_main.detect_loop(Frames(), st, SlowDetector()))
        ticks = 0
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.5:           # the loop keeps ticking while YOLO "runs"
            await asyncio.sleep(0.01)
            ticks += 1
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return st, ticks

    st, ticks = asyncio.run(go())
    assert st.visible() == {"lime"}
    assert SlowDetector.calls >= 3
    assert ticks > 20


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
