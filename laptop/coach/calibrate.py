"""Click calibration: python -m coach.calibrate   (run from laptop/)

The servos visit a 5x5 grid of (pan, tilt). For each one, click the laser dot in the window.
Keys: s = skip point (dot off-screen), r = redo last point, t = test mode (click -> laser goes there),
      v = verify (the laser visits random spots, you click where the dot really is: prints the error in deg and cm),
      a = add the verify points to the calibration and refit, q = quit.
Calibrate at the demo counter distance and posture. Redo only if the camera or pan-tilt moved relative to each other.

Every grid point (and every aim at runtime) is approached the same way (pan from below, tilt from above), so the
SG90's backlash settles the same way each time. A click is only taken once ControlLink says the servos stopped, the settle time passed and
a camera frame newer than that is on screen. A click near the dot snaps to the dot's centre (dot finder).
Both models are fitted (physical "plane" + v1 polynomial, see docs/aiming.md); the one with the lower
leave-one-out error is used, and both are printed. calibration.json records the frame size and CAM_ROTATE.

--auto (stretch): same grid, no window and no clicks. At each point the dot finder + a blink check
(laser off for a moment, the dot must disappear) locates the dot; points where it isn't found are skipped.
Same save gate and error report, then an automatic verify pass (--verify N, 0 = off).

--check [N]: no window, no clicks: the laser visits N (default 8) random spots of the saved calibration and the dot
finder + blink check measures where the dot really lands. Prints mean/max error in degrees and cm.

--eval: print what's in calibration.json (models, fit and leave-one-out errors, geometry, how far the two models
disagree over the reachable area, frame/rotation vs the current .env). No hardware needed.

The same steps run from a browser: python -m coach.calib_web (see calib_web.py). Both use the helpers below.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import time
from pathlib import Path
from typing import Awaitable, Callable

import numpy as np

from .aim import APPROACH_DIR, Calibration, norm_rotate
from .aim_model import DEG
from .contracts import ControlLinkAPI, FrameSource
from .dot import LAG_S, blink_dot

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "calibration.json"   # laptop/calibration.json
MIN_SAVE_POINTS = 9
WARN_DEG = 2.0
DEFAULT_N = 5           # grid size per axis
SETTLE_S = 0.6          # after the servos stop: SG90 settle + camera latency (~7 fps ESP32 stream) before a click
AUTO_SETTLE_S = 0.6     # --auto: same, before looking for the dot
AUTO_TRIES = 2          # --auto: blinks per grid point before skipping it
APPROACH_DEG = 3.0      # visit every point from 3 deg away in aim.APPROACH_DIR (same as the Aimer): cancels backlash
VIA_SETTLE_S = 0.08     # pause at the approach point before the final move
SNAP_FRAC = 0.04        # a click within this fraction of the frame width of a found dot snaps to it
VERIFY_N = 8            # verify points
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


def _limits() -> tuple[tuple[float, float], tuple[float, float]]:
    from .config import CONFIG
    return (CONFIG.pan_min, CONFIG.tilt_min), (CONFIG.pan_max, CONFIG.tilt_max)


class Mover:
    """Moves the servos like the Aimer does: first to APPROACH_DEG short of the goal (pan below it, tilt above it:
    aim.APPROACH_DIR), then to it, so gear backlash settles the same way for every point. settled() is True once
    ControlLink stopped ramping (`moving`) and `settle_s` passed. A link without `moving` (FakeLink) goes straight
    to the goal."""

    def __init__(self, link: ControlLinkAPI, approach_deg: float = APPROACH_DEG,
                 lo: tuple[float, float] | None = None, clock: Callable[[], float] = time.monotonic,
                 hi: tuple[float, float] | None = None, dirs: tuple[float, float] = APPROACH_DIR):
        self.link, self.approach, self.clock, self.dirs = link, approach_deg, clock, dirs
        lim_lo, lim_hi = _limits()
        self.lo = lo if lo is not None else lim_lo
        self.hi = hi if hi is not None else lim_hi
        self.goal: tuple[float, float] | None = None
        self.via: tuple[float, float] | None = None
        self.arrived_at: float | None = None        # clock() when the servos stopped at the goal
        self.arrived_wall = 0.0                     # time.time() of the same moment (frame timestamps use it)
        self._via_at: float | None = None

    def go(self, goal) -> None:
        self.goal = (float(goal[0]), float(goal[1]))
        self.arrived_at, self._via_at = None, None
        if self.approach > 0 and hasattr(self.link, "moving"):
            self.via = tuple(min(max(g - d * self.approach, lo), hi)
                             for g, d, lo, hi in zip(self.goal, self.dirs, self.lo, self.hi))
            self.link.pan, self.link.tilt = self.via
        else:
            self.via = None
            self.link.pan, self.link.tilt = self.goal

    def update(self) -> None:
        if self.goal is None:
            return
        if getattr(self.link, "moving", False):     # ControlLink still ramping
            self.arrived_at = self._via_at = None
            return
        now = self.clock()
        if self.via is not None:
            if self._via_at is None:
                self._via_at = now
            elif now - self._via_at >= VIA_SETTLE_S:
                self.via = None
                self.link.pan, self.link.tilt = self.goal
            return
        if self.arrived_at is None:
            self.arrived_at, self.arrived_wall = now, time.time()

    def settled(self, settle_s: float) -> bool:
        self.update()
        return self.via is None and self.arrived_at is not None and self.clock() - self.arrived_at >= settle_s

    async def wait(self, settle_s: float, sleep: Callable[[float], Awaitable] = asyncio.sleep,
                   timeout_s: float = 10.0, step_s: float = 0.02) -> bool:
        """go() then this: ramp to the approach point, final move, ramp, settle. Timed by `sleep` alone (so a
        test's fake sleep drives it). False if ControlLink still says moving after timeout_s."""
        waited = 0.0
        while True:
            if getattr(self.link, "moving", False):
                if waited > timeout_s:
                    return False
                await sleep(step_s)
                waited += step_s
                continue
            if self.via is not None:
                await sleep(VIA_SETTLE_S)
                waited += VIA_SETTLE_S
                self.via = None
                self.link.pan, self.link.tilt = self.goal
                continue
            break
        self.arrived_at, self.arrived_wall = self.clock(), time.time()
        await sleep(settle_s)
        return True


def snap_to_dot(frame, x: float, y: float, radius: float | None = None) -> tuple[float, float, bool]:
    """A click near the laser dot -> the dot's centre (sub-pixel, from the dot finder). (x, y, snapped)."""
    if frame is None:
        return float(x), float(y), False
    from .vision import find_red_dot
    r = radius if radius is not None else max(8.0, SNAP_FRAC * frame.shape[1])
    p = find_red_dot(frame, (x, y))
    if p is not None and math.hypot(p[0] - x, p[1] - y) <= r:
        return float(p[0]), float(p[1]), True
    return float(x), float(y), False


def frame_fresh(frames: FrameSource, mover: Mover, settle_s: float) -> bool:
    """The newest frame was received after the servos stopped + settle_s (so it shows the dot where it is now)."""
    if mover.arrived_at is None:
        return False
    _, ts = frames.latest()
    return ts >= mover.arrived_wall + settle_s


class CalibSession:
    """Point-collection state, no GUI. grid index i walks forward; each point is clicked, skipped or redone."""

    def __init__(self, grid: list[tuple[float, float]]):
        self.grid = grid
        self.i = 0
        self.results: list[tuple[float, float] | None] = []   # per visited grid point: clicked pixel or None (skipped)
        self.extra: list[tuple[tuple[float, float], tuple[float, float]]] = []   # (pixel, angles) from verify

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
        """(pixels, angles) of the clicked points (+ any verify points added)."""
        pairs = [(px, self.grid[k]) for k, px in enumerate(self.results) if px is not None] + list(self.extra)
        return [p for p, _ in pairs], [a for _, a in pairs]

    @property
    def n_points(self) -> int:
        return sum(px is not None for px in self.results) + len(self.extra)

    def can_save(self) -> bool:
        return self.n_points >= MIN_SAVE_POINTS


def session_from(calib: Calibration | None) -> CalibSession:
    """A finished session holding a saved calibration's points, so verify points can be added and refitted."""
    pts = (calib.points or {}) if calib is not None else {}
    sess = CalibSession([])
    sess.extra = [(tuple(p), tuple(a)) for p, a in zip(pts.get("px", []), pts.get("angles", []))]
    return sess


def fit_and_save(pixels, angles, path=DEFAULT_OUT, warn_deg: float = WARN_DEG, frame=None, rotate=None,
                 model: str = "auto") -> tuple[Calibration, list[str], float]:
    """Fit (both models, robust to a bad click, pick by leave-one-out), save. Returns (calib, report lines, worst).

    Refuses (ValueError) with fewer than MIN_SAVE_POINTS points. frame = (w, h) of the camera frames clicked on,
    rotate = CAM_ROTATE they were turned with (None = CAM_ROTATE from .env).
    """
    if len(pixels) < MIN_SAVE_POINTS:
        raise ValueError(f"only {len(pixels)} points, need at least {MIN_SAVE_POINTS} to save")
    from .config import CONFIG
    rotate = CONFIG.cam_rotate if rotate is None else rotate
    calib = Calibration().fit(pixels, angles, model=model, frame=frame, rotate=rotate, robust=True,
                              baseline_m=CONFIG.laser_above_cm / 100, hfov_deg=CONFIG.cam_hfov_deg)
    lines, worst = calib.report(pixels, angles, warn_deg)
    calib.save(path)
    return calib, lines, worst


def frame_size(frames: FrameSource | None) -> tuple[int, int] | None:
    if frames is None:
        return None
    f, _ = frames.latest()
    return None if f is None else (int(f.shape[1]), int(f.shape[0]))


def finish(sess: CalibSession, out: Path, log: Callable[[str], None] = print, frame=None) -> Calibration | None:
    """Fit + save the session's points and log the report. None (and nothing saved) if there are too few points."""
    pixels, angles = sess.points()
    try:
        calib, lines, worst = fit_and_save(pixels, angles, out, frame=frame)
    except ValueError as e:
        log(f"NOT SAVED: {e}")
        return None
    for ln in lines:
        log(ln)
    for ln in calib.summary():
        log(ln)
    log(f"saved {out} ({len(pixels)} points, worst error {worst:.2f} deg)")
    if worst > WARN_DEG:
        log(f"WARNING: some points are off by more than {WARN_DEG} deg. Redo them (r) or rerun.")
    return calib


def aim_at_pixel(calib: Calibration, link: ControlLinkAPI, px: tuple[float, float],
                 mover: Mover | None = None) -> tuple[float, float]:
    """Test mode: point the laser at pixel px with calib (approach from below with a Mover). Returns the angles."""
    p, t = calib.angles_for(px)
    if not (math.isfinite(p) and math.isfinite(t)):
        return p, t
    goal = (min(max(p, TEST_PAN[0]), TEST_PAN[1]), t)
    if mover is not None:
        mover.go(goal)
    else:
        link.pan, link.tilt = goal
    return p, t


# ── verify ───────────────────────────────────────────────────────────────────

class VerifySession:
    """Accuracy check: the laser is sent to n random reachable spots; for each, the dot is found (dot finder) or
    clicked. Error = how far the dot landed from where it was sent: degrees of servo angle and cm on the counter.

    The measured points are also good calibration points (pixel of the dot, angles sent): add_to() puts them in a
    CalibSession so a refit uses them.
    """

    def __init__(self, calib: Calibration, n: int = VERIFY_N, frame: tuple[int, int] | None = None,
                 limits: tuple[float, float, float, float] | None = None, rng=None, margin_px: float = 10.0):
        self.calib = calib
        rng = rng if rng is not None else np.random.default_rng()
        if limits is None:
            from .config import CONFIG
            limits = (CONFIG.pan_min, CONFIG.pan_max, CONFIG.tilt_min, CONFIG.tilt_max)
        pts = np.asarray(calib.points["px"], float) if calib.points else None
        w, h = frame or calib.frame or ((pts.max(axis=0) * 1.1).astype(int) if pts is not None else (320, 240))
        # where the calibration has data: the clicked area, grown a bit (verifying far outside it says little)
        if pts is not None:
            lo, hi = pts.min(axis=0), pts.max(axis=0)
            pad = 0.1 * (hi - lo)
            lo, hi = np.maximum(lo - pad, margin_px), np.minimum(hi + pad, (w - margin_px, h - margin_px))
        else:
            lo, hi = np.array([margin_px, margin_px]), np.array([w - margin_px, h - margin_px])
        self.targets: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for _ in range(4000):
            if len(self.targets) >= n:
                break
            px = rng.uniform(lo, hi)
            a = calib.angles_for(px)
            if (np.all(np.isfinite(a)) and limits[0] <= a[0] <= limits[1] and limits[2] <= a[1] <= limits[3]):
                self.targets.append(((float(px[0]), float(px[1])), (float(a[0]), float(a[1]))))
        self.i = 0
        self.dots: list[tuple[float, float] | None] = []

    @property
    def done(self) -> bool:
        return self.i >= len(self.targets)

    def current(self):
        return None if self.done else self.targets[self.i]

    def record(self, dot) -> None:
        if not self.done:
            self.dots.append(None if dot is None else (float(dot[0]), float(dot[1])))
            self.i += 1

    def skip(self) -> None:
        self.record(None)

    def errors(self) -> list[tuple[float, float, float]]:
        """Per measured point: (pixel error, degrees, cm on the counter)."""
        out = []
        for (px, ang), dot in zip(self.targets, self.dots):
            if dot is None:
                continue
            a = self.calib.angles_many([px, dot])
            deg = float(np.hypot(*(a[0] - a[1])))
            cm = float("nan")
            if self.calib.plane is not None:
                pts = self.calib.plane.points([px, dot])
                cm = float(np.linalg.norm(pts[0] - pts[1])) * 100
            if not math.isfinite(cm):
                cm = deg * DEG * self.calib.distance_m(px) * 100
            out.append((float(math.hypot(px[0] - dot[0], px[1] - dot[1])), deg, cm))
        return out

    def summary(self) -> tuple[list[str], dict]:
        lines, errs = [], self.errors()
        it = iter(errs)
        for (px, ang), dot in zip(self.targets, self.dots):
            head = f"verify ({px[0]:5.0f},{px[1]:5.0f}) pan {ang[0]:5.1f} tilt {ang[1]:5.1f}: "
            if dot is None:
                lines.append(head + "no dot")
                continue
            e_px, deg, cm = next(it)
            lines.append(head + f"dot at ({dot[0]:5.0f},{dot[1]:5.0f}) off {e_px:4.1f} px = {deg:4.2f} deg "
                                f"~ {cm:4.1f} cm")
        stats = {"n": len(errs)}
        if errs:
            e = np.asarray(errs)
            stats.update(mean_deg=float(e[:, 1].mean()), max_deg=float(e[:, 1].max()),
                         mean_cm=float(e[:, 2].mean()), max_cm=float(e[:, 2].max()))
            lines.append(f"VERIFY: {len(errs)} points, mean {stats['mean_deg']:.2f} deg (~{stats['mean_cm']:.1f} cm), "
                         f"max {stats['max_deg']:.2f} deg (~{stats['max_cm']:.1f} cm)")
        else:
            lines.append("VERIFY: no points measured")
        return lines, stats

    def add_to(self, sess: CalibSession) -> int:
        """Add the measured points (dot pixel, angles sent) to a calibration session. Returns how many."""
        n = 0
        for (px, ang), dot in zip(self.targets, self.dots):
            if dot is not None:
                sess.extra.append((dot, ang))
                n += 1
        return n


async def auto_verify(link: ControlLinkAPI, frames: FrameSource, vs: VerifySession,
                      settle_s: float = AUTO_SETTLE_S, lag_s: float = LAG_S, tries: int = AUTO_TRIES,
                      sleep: Callable[[float], Awaitable] = asyncio.sleep, log: Callable[[str], None] = print,
                      mover: Mover | None = None) -> VerifySession:
    """Verify with the dot finder: visit each target, blink, record the confirmed dot (or skip)."""
    mover = mover or Mover(link)
    link.laser = True
    while not vs.done:
        px, ang = vs.current()
        mover.go(ang)
        await mover.wait(max(settle_s, lag_s), sleep)
        p = None
        for k in range(tries):
            if k:
                await sleep(lag_s)
            p = await blink_dot(link, frames, near=px, lag_s=lag_s, sleep=sleep)
            if p is not None:
                break
        vs.record(p)
        log(f"verify {vs.i}/{len(vs.targets)}: " + ("no dot" if p is None else f"dot at ({p[0]:.0f},{p[1]:.0f}), "
                                                    f"sent to ({px[0]:.0f},{px[1]:.0f})"))
    for ln in vs.summary()[0]:
        log(ln)
    return vs


def draw_points(frame, sess: CalibSession | None = None, verify: VerifySession | None = None):
    """Copy of frame with the clicked points as green dots (and the current verify target as a cross)."""
    import cv2
    img = frame.copy()
    if sess is not None:
        for x, y in sess.points()[0]:
            cv2.circle(img, (int(x), int(y)), 4, (0, 255, 0), -1)
    if verify is not None and not verify.done:
        (x, y), _ = verify.current()
        cv2.drawMarker(img, (int(x), int(y)), (255, 255, 0), cv2.MARKER_CROSS, 14, 1)
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
                     log: Callable[[str], None] = print, sess: CalibSession | None = None,
                     mover: Mover | None = None) -> CalibSession:
    """--auto: visit the grid and locate the dot with find_red_dot + a blink check instead of a click.

    Points with no confirmed dot after `tries` blinks are skipped. Leaves the laser on; the caller turns it off.
    Pass `sess` (a fresh CalibSession over `grid`) to watch progress while it runs.
    """
    sess = sess if sess is not None else CalibSession(grid)
    mover = mover or Mover(link)
    link.laser = True
    while not sess.done:
        pan, tilt = sess.current()
        mover.go((pan, tilt))           # from below, then wait for ControlLink's ramp + settle
        await mover.wait(max(settle_s, lag_s), sleep)
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
# --eval: look at a saved calibration.json (no hardware)
# ═══════════════════════════════════════════════════════════════════════════════

def evaluate(path: Path, log: Callable[[str], None] = print, cam_rotate=None,
             limits: tuple[float, float, float, float] | None = None) -> Calibration | None:
    """Print what a calibration file holds and how much to trust it. Returns it (None if missing)."""
    from .config import CONFIG
    if not Path(path).exists():
        log(f"no {path}: run python -m coach.calibrate")
        return None
    calib = Calibration.load(path, model="auto")
    rot = norm_rotate(CONFIG.cam_rotate if cam_rotate is None else cam_rotate)
    limits = limits or (CONFIG.pan_min, CONFIG.pan_max, CONFIG.tilt_min, CONFIG.tilt_max)
    log(f"{path}")
    for ln in calib.summary():
        log(ln)
    if calib.rotate is None or calib.frame is None:
        log("old file: no frame size / CAM_ROTATE recorded, so a resolution or rotation change can't be detected. "
            "Recalibrate to record them.")
    elif calib.rotate != rot:
        log(f"PROBLEM: calibrated with CAM_ROTATE={calib.rotate}, .env now says {rot}: the Aimer will refuse it.")
    if not calib.points:
        log("no raw points stored (old file): only the polynomial is available. Recalibrate for the physical model.")
        return calib
    px, ang = np.asarray(calib.points["px"], float), np.asarray(calib.points["angles"], float)
    cmp = Calibration().fit(px, ang, model="auto", frame=calib.frame, rotate=calib.rotate, robust=True,
                            baseline_m=CONFIG.laser_above_cm / 100, hfov_deg=CONFIG.cam_hfov_deg)
    log("per point (model in use):")
    for ln in calib.report(px, ang, WARN_DEG)[0]:
        log("  " + ln)
    a_lo, a_hi = ang.min(axis=0), ang.max(axis=0)
    log(f"grid covers pan {a_lo[0]:.0f}-{a_hi[0]:.0f}, tilt {a_lo[1]:.0f}-{a_hi[1]:.0f}; limits allow pan "
        f"{limits[0]:.0f}-{limits[1]:.0f}, tilt {limits[2]:.0f}-{limits[3]:.0f}")
    if cmp.plane is not None and calib.frame is not None:
        w, h = calib.frame
        xs, ys = np.meshgrid(np.linspace(0, w - 1, 33), np.linspace(0, h - 1, 25))
        grid = np.c_[xs.ravel(), ys.ravel()]
        a_plane = cmp.plane.predict(grid)
        a_poly = cmp.poly.predict(grid)
        reach = (np.all(np.isfinite(a_plane), axis=1) & (a_plane[:, 0] >= limits[0]) & (a_plane[:, 0] <= limits[1])
                 & (a_plane[:, 1] >= limits[2]) & (a_plane[:, 1] <= limits[3]))
        if reach.any():
            r = grid[reach]
            d = np.hypot(*(a_plane[reach] - a_poly[reach]).T)
            inside = ((a_plane[reach] >= a_lo) & (a_plane[reach] <= a_hi)).all(axis=1)
            log(f"reachable part of the picture: x {r[:, 0].min():.0f}-{r[:, 0].max():.0f}, "
                f"y {r[:, 1].min():.0f}-{r[:, 1].max():.0f} ({100 * reach.mean():.0f}% of the frame)")
            log(f"plane vs poly over it: mean {d.mean():.2f} deg, max {d.max():.2f} deg "
                f"(inside the grid max {d[inside].max() if inside.any() else 0:.2f}, outside it max "
                f"{d[~inside].max() if (~inside).any() else 0:.2f})")
        else:
            log("no part of the picture is within the servo limits according to the plane model: check PAN_*/TILT_*")
    return calib


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
    calib = finish(sess, out, frame=frame_size(frames))
    if calib is not None and args.verify:
        vs = VerifySession(calib, args.verify, frame=frame_size(frames))
        await auto_verify(link, frames, vs, settle_s=args.settle_ms / 1000)
    link.laser = False


async def _check_main(args, link, frames, out: Path) -> None:
    if not out.exists():
        print(f"no {out}: calibrate first")
        return
    if not await wait_for_frame(frames):
        print("no camera frames, check CAM_URL")
        return
    calib = Calibration.load(out)
    fc = calib.frame_check(frame_size(frames), _cam_rotate())
    if not fc.ok or fc.scale != 1.0:
        print(fc.msg)
        if not fc.ok:
            return
    vs = VerifySession(calib, args.check, frame=calib.frame)
    print(f"checking {out} ({calib.model} model) at {len(vs.targets)} random spots with the dot finder, keep the "
          f"counter clear of people")
    if fc.scale != 1.0:     # measured dots are in the camera's pixels now: compare in calibration pixels
        vs.targets = [(tuple(fc.to_runtime(px)), ang) for px, ang in vs.targets]
        vs.calib = _RescaledCalib(calib, fc)
    await auto_verify(link, frames, vs, settle_s=args.settle_ms / 1000)


def _cam_rotate():
    from .config import CONFIG
    return CONFIG.cam_rotate


class _RescaledCalib:
    """A Calibration seen through a FrameCheck (runtime pixels in, same answers), for --check at another size."""

    def __init__(self, calib: Calibration, fc):
        self._c, self._fc = calib, fc
        self.plane = None               # cm on the counter falls back to deg * distance

    def angles_many(self, pxs, height: float = 0.0):
        return self._c.angles_many(self._fc.to_calib(pxs), height)

    def distance_m(self, px):
        return self._c.distance_m(tuple(self._fc.to_calib(px)))


async def _main(args) -> None:
    import cv2

    from .config import CONFIG
    from .control import ControlLink
    from .frames import FrameGrabber

    cam = args.cam or CONFIG.cam_url
    frames = FrameGrabber(int(cam) if str(cam).isdigit() else cam).start()
    link = await ControlLink(args.ctl or CONFIG.ctl_host, CONFIG.ctl_port).start()
    out = Path(args.out)
    if args.check:
        try:
            await _check_main(args, link, frames, out)
        finally:
            link.laser = False
            await asyncio.sleep(0.1)
            frames.stop()
        return
    if args.auto:
        try:
            await _auto_main(args, link, frames, out)
        finally:
            link.laser = False
            await asyncio.sleep(0.1)    # one more packet with the laser off
            frames.stop()
        return
    settle_s = args.settle_ms / 1000
    sess = CalibSession(grid_from_args(args))
    calib = Calibration.load(out) if out.exists() else None
    mover = Mover(link)
    mode = "grid"           # grid | test | verify
    verify: VerifySession | None = None
    saved = False
    clicks: list[tuple[int, int, float]] = []
    last_goal = None

    def on_mouse(event, x, y, *_):
        if event == cv2.EVENT_LBUTTONDOWN:
            clicks.append((x, y, time.monotonic()))

    win = "calibrate (s skip, r redo, t test, v verify, q quit)"
    cv2.namedWindow(win)
    cv2.setMouseCallback(win, on_mouse)
    link.laser = True
    try:
        while True:
            if mode == "grid":
                goal = sess.current()
                if goal is not None and goal != last_goal:
                    mover.go(goal)
                    last_goal = goal
            elif mode == "verify":
                goal = verify.current()[1] if verify and not verify.done else None
                if goal is not None and goal != last_goal:
                    mover.go(goal)
                    last_goal = goal
            settled = mover.settled(settle_s) and frame_fresh(frames, mover, settle_s)

            frame, _ = frames.latest()
            if frame is not None:
                img = draw_points(frame, sess, verify if mode == "verify" else None)
                if mode == "test":
                    msg = "TEST: click a spot" if calib else "TEST: no calibration yet"
                elif mode == "verify":
                    msg = (f"VERIFY {verify.i + 1}/{len(verify.targets)}: "
                           f"{'click the dot (s: none)' if settled else 'settling...'}" if not verify.done
                           else "verify done: a = add these points + refit, t = test, q = quit")
                elif goal is None:
                    msg = "grid done. t = test, v = verify, q = quit"
                else:
                    msg = (f"{sess.i + 1}/{len(sess.grid)} pan {goal[0]:.0f} tilt {goal[1]:.0f} "
                           f"{'click the dot' if settled else 'settling...'}  [{sess.n_points} pts]")
                cv2.putText(img, msg, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)
                cv2.imshow(win, img)

            while clicks:
                x, y, t_click = clicks.pop(0)
                if mode == "test":
                    if calib is not None:
                        p, t = aim_at_pixel(calib, link, (x, y), mover)
                        print(f"test: ({x},{y}) -> pan {p:.1f} tilt {t:.1f}")
                    continue
                if not settled or t_click < mover.arrived_at + settle_s:    # made before this point settled
                    print("still settling: click again once the message says so")
                    continue
                x, y, snapped = snap_to_dot(frame, x, y)
                tag = " (snapped to the dot)" if snapped else ""
                if mode == "verify" and not verify.done:
                    verify.record((x, y))
                    print(f"verify {verify.i}/{len(verify.targets)}: dot at ({x:.1f},{y:.1f}){tag}")
                    if verify.done:
                        for ln in verify.summary()[0]:
                            print(ln)
                elif mode == "grid" and goal is not None:
                    sess.click(x, y)
                    print(f"point {sess.i}/{len(sess.grid)}: ({x:.1f},{y:.1f}) -> {goal}{tag}")
                else:
                    continue
                clicks.clear()      # one click per point: a double click must not land on the next point too
                break

            if mode == "grid" and sess.done and not saved:
                calib = finish(sess, out, frame=frame_size(frames)) or calib
                saved = True

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                if not saved and sess.n_points:
                    calib = finish(sess, out, frame=frame_size(frames)) or calib
                break
            elif key == ord("s"):
                if mode == "grid":
                    sess.skip()
                elif mode == "verify" and verify and not verify.done:
                    verify.skip()
            elif key == ord("r") and mode == "grid":
                sess.redo()
                saved, last_goal = False, None
            elif key == ord("t"):
                mode = "grid" if mode == "test" else "test"
                last_goal = None
                print("test mode", "on" if mode == "test" else "off")
            elif key == ord("v") and calib is not None:
                verify = VerifySession(calib, args.verify or VERIFY_N, frame=frame_size(frames))
                mode, last_goal = "verify", None
                print(f"verify: {len(verify.targets)} random spots. Click the dot each time (s if not visible).")
            elif key == ord("a") and verify is not None and verify.done:
                if sess.n_points == 0:          # verifying a saved calibration: refit its points + these
                    sess = session_from(calib)
                    saved = True
                n = verify.add_to(sess)
                print(f"added {n} verify points, refitting")
                calib = finish(sess, out, frame=frame_size(frames)) or calib
                verify, mode, last_goal = None, "grid", None
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
    ap.add_argument("-n", type=int, default=DEFAULT_N, help=f"grid size per axis (n x n points, default {DEFAULT_N})")
    ap.add_argument("--settle-ms", type=float, default=None,
                    help=f"wait after the servos stop (default {SETTLE_S * 1000:.0f})")
    ap.add_argument("--out", default=str(DEFAULT_OUT))


def grid_from_args(args) -> list[tuple[float, float]]:
    return make_grid((args.pan_min, args.pan_max), (args.tilt_min, args.tilt_max), args.n)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_grid_args(ap)
    ap.add_argument("--auto", action="store_true", help="find the dot automatically (dot finder + blink check)")
    ap.add_argument("--verify", type=int, default=None,
                    help=f"verify points ('v' in the window; after --auto, default {VERIFY_N}, 0 = skip)")
    ap.add_argument("--eval", action="store_true", help="print what calibration.json holds and how good it is")
    ap.add_argument("--check", type=int, nargs="?", const=VERIFY_N, default=0, metavar="N",
                    help=f"measure the saved calibration at N random spots with the dot finder (default {VERIFY_N})")
    ap.add_argument("--cam", default=None, help="camera URL (default CAM_URL from laptop/.env)")
    ap.add_argument("--ctl", default=None, help="S3 IP or host (default CTL_HOST from laptop/.env)")
    args = ap.parse_args(argv)
    if args.eval:
        evaluate(Path(args.out))
        return
    if args.settle_ms is None:
        args.settle_ms = (AUTO_SETTLE_S if args.auto else SETTLE_S) * 1000
    if args.verify is None:
        args.verify = VERIFY_N if args.auto else 0
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
