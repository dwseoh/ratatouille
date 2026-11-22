"""Click calibration: python -m coach.calibrate   (run from laptop/)

The servos visit a 4x4 grid of (pan, tilt). For each one, click the laser dot in the window.
Keys: s = skip point (dot off-screen), r = redo last point, t = test mode (click -> laser goes there), q = quit.
Calibrate at the demo counter distance. Redo only if the camera or pan-tilt moved relative to each other.

--auto (stretch): same grid, no window and no clicks. At each point the dot finder + a blink check
(laser off for a moment, the dot must disappear) locates the dot; points where it isn't found are skipped.
Same save gate and error report. Then check it with the click tool's test mode (t).

The same steps run from a browser: python -m coach.calib_web (see calib_web.py). Both use the helpers below.
"""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import Awaitable, Callable

from .aim import Calibration
from .contracts import ControlLinkAPI, FrameSource
from .dot import LAG_S, blink_dot

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "calibration.json"   # laptop/calibration.json
MIN_SAVE_POINTS = 9
WARN_DEG = 2.0
SETTLE_S = 0.4
AUTO_SETTLE_S = 0.6     # --auto: servo move + camera lag before looking for the dot
AUTO_TRIES = 2          # --auto: blinks per grid point before skipping it
TEST_PAN = (10.0, 170.0)    # test mode clamps pan to this


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


def finish(sess: CalibSession, out: Path, log: Callable[[str], None] = print) -> Calibration | None:
    """Fit + save the session's points and log the report. None (and nothing saved) if there are too few points."""
    pixels, angles = sess.points()
    try:
        calib, lines, worst = fit_and_save(pixels, angles, out)
    except ValueError as e:
        log(f"NOT SAVED: {e}")
        return None
    for ln in lines:
        log(ln)
    log(f"saved {out} ({len(pixels)} points, worst error {worst:.2f} deg)")
    if worst > WARN_DEG:
        log(f"WARNING: some points are off by more than {WARN_DEG} deg. Redo them (r) or rerun.")
    return calib


def aim_at_pixel(calib: Calibration, link: ControlLinkAPI, px: tuple[float, float]) -> tuple[float, float]:
    """Test mode: point the laser at pixel px with calib. Returns the (unclamped) angles."""
    p, t = calib.angles_for(px)
    link.pan, link.tilt = min(max(p, TEST_PAN[0]), TEST_PAN[1]), t
    return p, t


def draw_points(frame, sess: CalibSession):
    """Copy of frame with the clicked points as green dots."""
    import cv2
    img = frame.copy()
    for x, y in sess.points()[0]:
        cv2.circle(img, (int(x), int(y)), 4, (0, 255, 0), -1)
    return img


async def wait_for_frame(frames: FrameSource, timeout_s: float = 10.0) -> bool:
    """True once the camera has delivered a frame, False after timeout_s."""
    for _ in range(max(1, int(timeout_s / 0.1))):
        if frames.latest()[0] is not None:
            return True
        await asyncio.sleep(0.1)
    return False


async def auto_sweep(link: ControlLinkAPI, frames: FrameSource, grid: list[tuple[float, float]],
                     settle_s: float = AUTO_SETTLE_S, tries: int = AUTO_TRIES, lag_s: float = LAG_S,
                     sleep: Callable[[float], Awaitable] = asyncio.sleep,
                     log: Callable[[str], None] = print, sess: CalibSession | None = None) -> CalibSession:
    """--auto: visit the grid and locate the dot with find_red_dot + a blink check instead of a click.

    Points with no confirmed dot after `tries` blinks are skipped. Leaves the laser on; the caller turns it off.
    Pass `sess` (a fresh CalibSession over `grid`) to watch progress while it runs.
    """
    sess = sess if sess is not None else CalibSession(grid)
    link.laser = True
    while not sess.done:
        pan, tilt = sess.current()
        link.pan, link.tilt = pan, tilt
        while getattr(link, "moving", False):   # ControlLink ramps the servos; wait until they arrive
            await sleep(0.05)
        await sleep(max(settle_s, lag_s))
        p = None
        for k in range(tries):
            if k:
                await sleep(lag_s)      # the laser just came back on: wait until the frames show it
            p = await blink_dot(link, frames, lag_s=lag_s, sleep=sleep)
            if p is not None:
                break
        n = f"point {sess.i + 1}/{len(grid)} pan {pan:.0f} tilt {tilt:.0f}"
        if p is None:
            sess.skip()
            log(f"{n}: no dot, skipped")
        else:
            sess.click(*p)
            log(f"{n}: dot at ({p[0]:.0f},{p[1]:.0f})")
    return sess


# ═══════════════════════════════════════════════════════════════════════════════
# GUI (cv2 window). Needs a display, the camera and the S3 (or tools/fake_s3.py).
# ═══════════════════════════════════════════════════════════════════════════════

async def _auto_main(args, link, frames, out: Path) -> None:
    if not await wait_for_frame(frames):
        print("no camera frames, check CAM_URL")
        return
    grid = grid_from_args(args)
    print(f"auto calibration: {len(grid)} points, keep the counter clear of people")
    sess = await auto_sweep(link, frames, grid, settle_s=args.settle_ms / 1000)
    link.laser = False
    finish(sess, out)


async def _main(args) -> None:
    import cv2

    from .config import CONFIG
    from .control import ControlLink
    from .frames import FrameGrabber

    cam = args.cam or CONFIG.cam_url
    frames = FrameGrabber(int(cam) if str(cam).isdigit() else cam).start()
    link = await ControlLink(args.ctl or CONFIG.ctl_host, CONFIG.ctl_port).start()
    out = Path(args.out)
    if args.auto:
        try:
            await _auto_main(args, link, frames, out)
        finally:
            link.laser = False
            await asyncio.sleep(0.1)    # one more packet with the laser off
            frames.stop()
        return
    sess = CalibSession(grid_from_args(args))
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
            if getattr(link, "moving", False):      # servos still ramping: settle time starts on arrival
                moved_at = time.monotonic()
            settled = time.monotonic() - moved_at >= args.settle_ms / 1000

            frame, _ = frames.latest()
            if frame is not None:
                img = draw_points(frame, sess)
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
                        p, t = aim_at_pixel(calib, link, (x, y))
                        print(f"test: ({x},{y}) -> pan {p:.1f} tilt {t:.1f}")
                elif goal is not None and settled:
                    sess.click(x, y)
                    print(f"point {sess.i}/{len(sess.grid)}: ({x},{y}) -> {goal}")

            if sess.done and not saved and not test_mode:
                calib = finish(sess, out) or calib
                saved = True

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                if not saved and sess.n_points:
                    calib = finish(sess, out) or calib
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


def add_grid_args(ap: argparse.ArgumentParser) -> None:
    """Grid, settle and output options, shared with calib_web."""
    ap.add_argument("--pan-min", type=float, default=60.0)
    ap.add_argument("--pan-max", type=float, default=120.0)
    ap.add_argument("--tilt-min", type=float, default=30.0, help="set to your safe range (see control_s3.ino)")
    ap.add_argument("--tilt-max", type=float, default=90.0)
    ap.add_argument("-n", type=int, default=4, help="grid size per axis (n x n points)")
    ap.add_argument("--settle-ms", type=float, default=None,
                    help=f"wait after each move (default {SETTLE_S * 1000:.0f}, {AUTO_SETTLE_S * 1000:.0f} with --auto)")
    ap.add_argument("--out", default=str(DEFAULT_OUT))


def grid_from_args(args) -> list[tuple[float, float]]:
    return make_grid((args.pan_min, args.pan_max), (args.tilt_min, args.tilt_max), args.n)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_grid_args(ap)
    ap.add_argument("--auto", action="store_true", help="find the dot automatically (dot finder + blink check)")
    ap.add_argument("--cam", default=None, help="camera URL (default CAM_URL from laptop/.env)")
    ap.add_argument("--ctl", default=None, help="S3 IP or host (default CTL_HOST from laptop/.env)")
    args = ap.parse_args(argv)
    if args.settle_ms is None:
        args.settle_ms = (AUTO_SETTLE_S if args.auto else SETTLE_S) * 1000
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
