"""Detector-side tests (no model): smoothing and the worker thread writing State.dets on the loop."""
import asyncio
import time
from pathlib import Path

from coach.contracts import Det, FakeFrames, Recipe, State
from coach.vision import DetectorThread, Smoother

RECIPE = Recipe.load(Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json")


def det(name, x=0.0, conf=0.9):
    return Det(name, conf, (x, 0.0, x + 10, 10.0))


def names(dets):
    return sorted(d.name for d in dets)


def test_two_of_three_smoothing():
    s = Smoother()
    assert names(s.update([det("lime")])) == []                  # seen once: not yet
    assert names(s.update([det("lime")])) == ["lime"]            # 2 of 2
    assert names(s.update([])) == ["lime"]                       # 2 of 3: one miss is tolerated
    assert names(s.update([])) == []                             # 1 of 3: gone
    assert names(s.update([det("knife")])) == []                 # single false hit never shows


def test_smoother_reports_newest_boxes():
    s = Smoother()
    s.update([det("lime", x=0)])
    out = s.update([det("lime", x=50), det("lime", x=200)])      # two limes in the newest frame
    assert sorted(d.box[0] for d in out) == [50, 200]
    out = s.update([])
    assert sorted(d.box[0] for d in out) == [50, 200]            # missed frame: keep last seen boxes


class FakeDetector:
    def __init__(self):
        self.calls = 0

    def detect(self, frame):
        self.calls += 1
        return [det("avocado"), det("hand", conf=0.5)]


def test_thread_writes_state_on_loop_and_clears_when_camera_stops():
    async def go():
        state = State(RECIPE)
        frames = FakeFrames()
        fake = FakeDetector()
        th = DetectorThread(fake, frames, state, asyncio.get_running_loop()).start()
        try:
            await asyncio.sleep(0.2)
            assert names(state.dets) == ["avocado", "hand"]
            assert th.fps > 0

            frames.latest = lambda: (None, 0.0)                  # camera disconnected
            await asyncio.sleep(1.3)
            assert state.dets == []
        finally:
            th.stop()
    asyncio.run(go())
