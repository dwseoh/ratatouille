"""Click calibration: python -m coach.calibrate   (run from laptop/)

The servos visit a 4x4 grid of (pan, tilt). For each one, click the laser dot in the window.
Keys: s = skip point (dot off-screen), r = redo last point, t = test mode (click -> laser goes there), q = quit.
Calibrate at the demo counter distance. Redo only if the camera or pan-tilt moved relative to each other.
"""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from .aim import Calibration

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "calibration.json"   # laptop/calibration.json
MIN_SAVE_POINTS = 9
WARN_DEG = 2.0
SETTLE_S = 0.4


def make_grid(pan=(60.0, 120.0), tilt=(30.0, 90.0), n: int = 4) -> list[tuple[float, float]]:
    """n x n (pan, tilt) points, serpentine order (each row reverses) so the servos travel less."""
    def lin(lo, hi):
        return [lo + (hi - lo) * i / (n - 1) for i in range(n)] if n > 1 else [lo]
    out = []
    for row, t in enumerate(lin(*tilt)):
        pans = lin(*pan)
        out += [(float(p), float(t)) for p in (pans if row % 2 == 0 else pans[::-1])]
    return out


class CalibSession:
    """Point-collection state, no GUI. grid index i walks forward; each point is clicked, skipped or redone."""

    def __init__(self, grid: list[tuple[float, float]]):
        self.grid = grid
        self.i = 0
        self.results: list[tuple[float, float] | None] = []   # per visited grid point: clicked pixel or None (skipped)

    @property
    def done(self) -> bool:
        return self.i >= len(self.grid)

    def current(self) -> tuple[float, float] | None:
        return None if self.done else self.grid[self.i]

    def click(self, x: float, y: float) -> None:
        if not self.done:
            self.results.append((float(x), float(y)))
            self.i += 1

    def skip(self) -> None:
        if not self.done:
            self.results.append(None)
            self.i += 1

    def redo(self) -> None:
        """Go back to the previous grid point and forget its click."""
        if self.i > 0:
            self.i -= 1
            self.results.pop()

    def points(self) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """(pixels, angles) of the clicked points."""
        pairs = [(px, self.grid[k]) for k, px in enumerate(self.results) if px is not None]
        return [p for p, _ in pairs], [a for _, a in pairs]

    @property
    def n_points(self) -> int:
        return sum(px is not None for px in self.results)

    def can_save(self) -> bool:
        return self.n_points >= MIN_SAVE_POINTS


def fit_and_save(pixels, angles, path=DEFAULT_OUT, warn_deg: float = WARN_DEG) -> tuple[Calibration, list[str], float]:
    """Fit, print the per-point error report, save. Refuses (ValueError) with fewer than MIN_SAVE_POINTS points."""
    if len(pixels) < MIN_SAVE_POINTS:
        raise ValueError(f"only {len(pixels)} points, need at least {MIN_SAVE_POINTS} to save")
    calib = Calibration().fit(pixels, angles)
    lines, worst = calib.report(pixels, angles, warn_deg)
    calib.save(path)
    return calib, lines, worst


# ═══════════════════════════════════════════════════════════════════════════════
# GUI (cv2 window). Needs a display, the camera and the S3 (or tools/fake_s3.py).
# ═══════════════════════════════════════════════════════════════════════════════

def _finish(sess: CalibSession, out: Path) -> Calibration | None:
    pixels, angles = sess.points()
    try:
        calib, lines, worst = fit_and_save(pixels, angles, out)
    except ValueError as e:
        print(f"NOT SAVED: {e}")
        return None
    print("\n".join(lines))
    print(f"saved {out} ({len(pixels)} points, worst error {worst:.2f} deg)")
    if worst > WARN_DEG:
        print(f"WARNING: some points are off by more than {WARN_DEG} deg. Redo them (r) or rerun.")
    return calib


async def _main(args) -> None:
    import cv2

    from .config import CONFIG
    from .control import ControlLink
    from .frames import FrameGrabber

    cam = CONFIG.cam_url
    frames = FrameGrabber(int(cam) if str(cam).isdigit() else cam).start()
    link = await ControlLink(CONFIG.ctl_host, CONFIG.ctl_port).start()
    out = Path(args.out)
    sess = CalibSession(make_grid((args.pan_min, args.pan_max), (args.tilt_min, args.tilt_max), args.n))
    calib = Calibration.load(out) if out.exists() else None
    test_mode, saved = False, False
    clicks: list[tuple[int, int]] = []
    moved_at = 0.0
    last_goal = None

    def on_mouse(event, x, y, *_):
        if event == cv2.EVENT_LBUTTONDOWN:
            clicks.append((x, y))

    win = "calibrate (s skip, r redo, t test, q quit)"
    cv2.namedWindow(win)
    cv2.setMouseCallback(win, on_mouse)
    link.laser = True
    try:
        while True:
            goal = sess.current()
            if not test_mode and goal is not None and goal != last_goal:
                link.pan, link.tilt = goal
                moved_at, last_goal = time.monotonic(), goal
            settled = time.monotonic() - moved_at >= args.settle_ms / 1000

            frame, _ = frames.latest()
            if frame is not None:
                img = frame.copy()
                px, _ = sess.points()
                for x, y in px:
                    cv2.circle(img, (int(x), int(y)), 4, (0, 255, 0), -1)
                if test_mode:
                    msg = "TEST: click a spot" if calib else "TEST: no calibration yet"
                elif goal is None:
                    msg = "grid done. t = test, q = quit"
                else:
                    msg = (f"{sess.i + 1}/{len(sess.grid)} pan {goal[0]:.0f} tilt {goal[1]:.0f} "
                           f"{'click the dot' if settled else 'settling...'}  [{sess.n_points} pts]")
                cv2.putText(img, msg, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
                cv2.imshow(win, img)

            while clicks:
                x, y = clicks.pop(0)
                if test_mode:
                    if calib is not None:
                        p, t = calib.angles_for((x, y))
                        link.pan, link.tilt = min(max(p, 10.0), 170.0), t
                        print(f"test: ({x},{y}) -> pan {p:.1f} tilt {t:.1f}")
                elif goal is not None and settled:
                    sess.click(x, y)
                    print(f"point {sess.i}/{len(sess.grid)}: ({x},{y}) -> {goal}")

            if sess.done and not saved and not test_mode:
                calib = _finish(sess, out) or calib
                saved = True

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                if not saved and sess.n_points:
                    calib = _finish(sess, out) or calib
                break
            elif key == ord("s") and not test_mode:
                sess.skip()
            elif key == ord("r") and not test_mode:
                sess.redo()
                saved, last_goal = False, None
            elif key == ord("t"):
                test_mode = not test_mode
                last_goal = None
                print("test mode", "on" if test_mode else "off")
            await asyncio.sleep(0.01)   # let ControlLink's 30 Hz pump run
    finally:
        link.laser = False
        await asyncio.sleep(0.1)        # one more packet with the laser off
        cv2.destroyAllWindows()
        frames.stop()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pan-min", type=float, default=60.0)
    ap.add_argument("--pan-max", type=float, default=120.0)
    ap.add_argument("--tilt-min", type=float, default=30.0, help="set to your safe range (see control_s3.ino)")
    ap.add_argument("--tilt-max", type=float, default=90.0)
    ap.add_argument("-n", type=int, default=4, help="grid size per axis (n x n points)")
    ap.add_argument("--settle-ms", type=float, default=SETTLE_S * 1000)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    asyncio.run(_main(ap.parse_args(argv)))


if __name__ == "__main__":
    main()
