"""Live check of the watcher against the REAL API with the photos in tools/test_frames. Costs a few cents.

    .venv/bin/python tools/live_watcher_check.py

For each (photo, recipe step) pair, runs one watcher check and prints what it logged, its verdict and its
latency, then PASS/FAIL against the expected "is the step done?" answer. Safety needs no API: covered by tests.
"""
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "laptop"))

import cv2  # noqa: E402

from coach.config import CONFIG as cfg  # noqa: E402
from coach.contracts import Det, FakeSteps, Recipe, State  # noqa: E402
from coach.watcher import Watcher  # noqa: E402

RECIPE = Recipe.load(ROOT / "laptop" / cfg.recipe_path)
PHOTOS = {p.stem: cv2.imread(str(p)) for p in (ROOT / "tools" / "test_frames").glob("*.jpg")}
NEEDED = ("avocado_hold", "avocado_halves", "mash_bowl")
if missing := [n for n in NEEDED if n not in PHOTOS]:
    sys.exit(f"Missing tools/test_frames/{', '.join(n + '.jpg' for n in missing)}. See tools/test_frames/README.md.")

# (photo, step index, expected step_done)
CASES = [
    ("avocado_hold", 0, False),      # whole avocado in hand: "halve avocados" is not done
    ("avocado_halves", 0, True),     # halves visible, cut faces up: done
    ("avocado_halves", 2, False),    # halves are not "mashed"
    ("mash_bowl", 2, True),          # mashed texture in a bowl: "mash" is done
]


class Frames:
    connected = True

    def __init__(self, img):
        self.img = img

    def latest(self):
        return self.img, 0.0

    def recent(self, n):
        return [self.img] * n


class Speech:
    async def say(self, *a, **k):
        pass


async def main():
    ok_all = True
    print(f"watcher model: {cfg.watcher_model}\n")
    for photo, step, expect in CASES:
        st = State(RECIPE, dets=[Det("avocado", 0.9, (0, 0, 10, 10))])
        st.step, st.session = step, "active"
        w = Watcher(cfg, st, Frames(PHOTOS[photo]), Speech(), FakeSteps(st))
        t0 = time.monotonic()
        await w._check("live", step)
        dt = time.monotonic() - t0
        v = w._verdict[1] if w._verdict else None
        events = [e.text for e in st.events if e.source == "vision"]
        ok = v is not None and v.done == expect and v.confidence >= 0.6
        ok_all &= ok
        print(f"{'PASS' if ok else 'FAIL'}  {photo} @ step {step + 1} ({RECIPE.steps[step].short}): "
              f"done={v.done if v else None} conf={v.confidence if v else None} {dt:.1f}s")
        print(f"        note: {v.note if v else None}\n        events: {events}")
    sys.exit(0 if ok_all else 1)


asyncio.run(main())
