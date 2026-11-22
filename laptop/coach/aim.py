"""Aiming: the calibration (pixel -> servo angles) + the Aimer loop that implements AimerAPI.

Calibration holds two models fitted from the same click points (see aim_model.py and docs/aiming.md):
  - "plane": camera ray -> counter plane -> pan-tilt geometry with the laser's 12 cm offset. Exact on the counter
    anywhere in the picture, extrapolates, and can aim at the middle of a fruit instead of the counter behind it.
  - "poly":  the v1 2nd-order polynomial. Always fitted too (old code reads it from calibration.json).
The one with the lower leave-one-out error is used. calibration.json also records the frame size and CAM_ROTATE it
was made with, so a resolution change is rescaled and a rotation change is refused instead of aiming wrong.

Closed-loop correction on the seen laser dot is stretch (see docs/cut-list.md): Aimer(..., closed_loop=True)
plus a dot.DotTracker writing State.dot. Off by default; with it off nothing here reads State.dot.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from .aim_model import (
    BASELINE_M,
    DEG,
    HFOV_DEG,
    POLY_TERMS,
    PlaneModel,
    PolyModel,
    fit_plane,
    loo_errors,
    loo_score,
    rms,
    robust_weights,
)
from .contracts import LASER_IDLE_OFF_S, LAT_ON_TARGET, TFT_STATUS, ControlLinkAPI, Det, PointResult, State

log = logging.getLogger(__name__)

TERMS = POLY_TERMS
MIN_FIT_POINTS = len(TERMS)
FORMAT_VERSION = 2
NOMINAL_DIST_M = 0.6        # for degrees -> cm when there's no plane model
OUT_OF_REACH = "out_of_reach"   # point_at result: visible, but outside the servo limits (laser off)
# Direction of the final move on each axis (pan, tilt): +1 = the last move increases the angle. Every aim and every
# calibration point ends with a move this way, so the SG90's backlash/deadband settles on the same side each time.
# Tilt ends downwards because tilt 0 is the tilt servo's end stop on this box (no room to come up from below it).
APPROACH_DIR = (1.0, -1.0)
MODELS = ("poly", "plane")


def norm_rotate(spec) -> str:
    """CAM_ROTATE as stored/compared: '0', '90', '180', '270', 'vflip', 'hflip'."""
    s = str(spec).strip().lower() if spec is not None else "0"
    return s if s in ("90", "180", "270", "vflip", "hflip") else "0"


@dataclass
class FrameCheck:
    """How runtime pixels relate to the calibration's pixels. ok=False: don't aim (rotation changed)."""
    ok: bool = True
    scale: float = 1.0                                  # runtime px -> calibration px, about the frame centres
    run_c: tuple[float, float] = (0.0, 0.0)             # runtime frame centre
    cal_c: tuple[float, float] = (0.0, 0.0)             # calibration frame centre
    msg: str = ""                                       # log line ("" = exact match)
    tft: str = ""                                       # 20-char status line

    def to_calib(self, px) -> np.ndarray:
        p = np.asarray(px, float)
        if self.scale == 1.0 and self.run_c == self.cal_c:
            return p
        return (p - np.asarray(self.run_c)) * self.scale + np.asarray(self.cal_c)

    def to_runtime(self, px) -> np.ndarray:
        p = np.asarray(px, float)
        if self.scale == 1.0 and self.run_c == self.cal_c:
            return p
        return (p - np.asarray(self.cal_c)) / self.scale + np.asarray(self.run_c)


class Calibration:
    """angles = f(pixel). `model` says which of the fitted models angles_for() uses ("plane" or "poly").

    Old API kept: Calibration(coef), .coef (the polynomial), fit(pixels, angles) (polynomial only, as before),
    angles_for, errors, report, save, load (reads v1 files: {"terms", "coef"} or a bare coef list).
    """

    def __init__(self, coef=None, *, model: str = "poly", plane: PlaneModel | dict | None = None,
                 frame: tuple[int, int] | None = None, rotate: str | None = None, points: dict | None = None,
                 cv: dict | None = None):
        self.poly = PolyModel(coef)
        self.plane = PlaneModel(plane) if isinstance(plane, dict) else plane
        self.model = model if model in MODELS else "poly"
        if self.model == "plane" and self.plane is None:
            self.model = "poly"
        self.frame = tuple(int(v) for v in frame) if frame is not None else None     # (w, h) at calibration
        self.rotate = norm_rotate(rotate) if rotate is not None else None
        self.points = points or None        # {"px": [...], "angles": [...], "weights": [...]}: the raw data
        self.cv = cv or {}                  # {"poly": {"fit": deg, "loo": deg}, "plane": {...}}

    # ── compat ──────────────────────────────────────────────────────────────
    @property
    def coef(self):
        return self.poly.coef

    @coef.setter
    def coef(self, value):
        self.poly.coef = None if value is None else np.asarray(value, float)

    @property
    def fitted(self) -> bool:
        return self.poly.coef is not None or self.plane is not None

    def _active(self):
        if self.model == "plane" and self.plane is not None:
            return self.plane
        if self.poly.coef is None:
            raise RuntimeError("Calibration not fitted")
        return self.poly

    # ── fitting ─────────────────────────────────────────────────────────────
    def fit(self, pixels, angles, model: str = "poly", frame=None, rotate=None, robust: bool = False,
            baseline_m: float | None = None, hfov_deg: float | None = None, loo: bool = True) -> "Calibration":
        """Fit from calibration points. model: "poly" (v1 behaviour), "plane", or "auto" (both, pick the one with
        the lower leave-one-out error). robust: down-weight points that disagree with the rest (bad clicks).
        frame (w, h) and rotate (CAM_ROTATE) are stored so a later resolution/rotation change is detected."""
        pixels, angles = np.asarray(pixels, float).reshape(-1, 2), np.asarray(angles, float).reshape(-1, 2)
        if len(pixels) != len(angles):
            raise ValueError(f"{len(pixels)} pixels but {len(angles)} angles")
        if len(pixels) < MIN_FIT_POINTS:
            raise ValueError(f"need at least {MIN_FIT_POINTS} points to fit, got {len(pixels)}")
        if model not in ("poly", "plane", "auto"):
            raise ValueError(f"unknown model {model!r}")
        if frame is not None:
            self.frame = (int(frame[0]), int(frame[1]))
        if rotate is not None:
            self.rotate = norm_rotate(rotate)
        baseline = BASELINE_M if baseline_m is None else baseline_m
        hfov = HFOV_DEG if hfov_deg is None else hfov_deg

        w = np.ones(len(pixels))
        for _ in range(3 if robust else 1):
            self.poly.fit(pixels, angles, w)
            if not robust:
                break
            w = robust_weights(self.poly.predict(pixels) - angles)
        self.model, self.plane, self.cv = "poly", None, {}
        poly_w = w
        self.cv["poly"] = _fit_stats(self.poly.predict(pixels) - angles, poly_w)
        if model in ("plane", "auto"):
            try:
                w = np.ones(len(pixels))
                plane, _ = fit_plane(pixels, angles, self.frame, baseline, hfov, weights=w)
                for _ in range(2 if robust else 0):
                    w = robust_weights(plane.predict(pixels) - angles)
                    plane, _ = fit_plane(pixels, angles, self.frame, baseline, hfov, start=plane, weights=w)
                self.plane = plane
                self.cv["plane"] = _fit_stats(plane.predict(pixels) - angles, w)
            except (ValueError, np.linalg.LinAlgError, FloatingPointError) as e:
                log.warning("plane model fit failed (%s): using the polynomial", e)
        if self.plane is not None and (model == "plane" or (model == "auto" and loo)):
            self.model = "plane"
        if loo and len(pixels) > MIN_FIT_POINTS:
            self.cv["poly"]["loo"] = loo_score(loo_errors("poly", pixels, angles, weights=poly_w))
            if self.plane is not None:
                self.cv["plane"]["loo"] = loo_score(loo_errors("plane", pixels, angles, self.frame, baseline,
                                                               full=self.plane, weights=w, hfov_deg=hfov))
                if model == "auto":
                    self.model = "plane" if self.cv["plane"]["loo"] < self.cv["poly"]["loo"] else "poly"
        elif model == "auto":
            self.model = "poly"
        weights = w if self.model == "plane" else poly_w
        self.points = {"px": pixels.tolist(), "angles": angles.tolist(), "weights": np.round(weights, 3).tolist()}
        return self

    # ── using it ────────────────────────────────────────────────────────────
    def angles_for(self, px, height: float = 0.0) -> tuple[float, float]:
        """(pan, tilt) for a pixel (calibration frame). height: aim this many metres above the counter (plane
        model only; the polynomial ignores it). NaN if the pixel's ray never reaches the counter."""
        pan, tilt = self.angles_many([px], height)[0]
        return float(pan), float(tilt)

    def angles_many(self, pxs, height: float = 0.0) -> np.ndarray:
        m = self._active()
        if m is self.plane:
            return m.predict(pxs, height)
        return m.predict(pxs)

    def pixel_for(self, angles) -> tuple[float, float]:
        """Where the dot should show up for servo angles (inverse of angles_for). NaN if unknown."""
        m = self._active()
        if m is self.plane:
            x, y = m.pixels_for([angles])[0]
        else:
            guess = np.mean(np.asarray(self.points["px"]), axis=0) if self.points else np.array([320.0, 240.0])
            x, y = m.invert(angles, guess)
        return float(x), float(y)

    def distance_m(self, px) -> float:
        """Laser -> counter distance at a pixel (plane model), else a nominal 60 cm."""
        if self.plane is not None:
            d = float(self.plane.distance([px])[0])
            if math.isfinite(d) and 0.05 < d < 5:
                return d
        return NOMINAL_DIST_M

    def deg_to_cm(self, deg: float, px) -> float:
        return float(deg) * DEG * self.distance_m(px) * 100

    def errors(self, pixels, angles) -> list[tuple[float, float]]:
        """Per point (pan error, tilt error) in degrees = predicted - real."""
        pred = self.angles_many(pixels)
        return [(float(dp), float(dt)) for dp, dt in pred - np.asarray(angles, float).reshape(-1, 2)]

    def report(self, pixels, angles, warn_deg: float = 2.0) -> tuple[list[str], float]:
        """Human-readable per-point fit error lines + the worst error (deg). Lines over warn_deg end in '<-- WARN'."""
        lines, worst = [], 0.0
        weights = (self.points or {}).get("weights")
        pts = np.asarray(pixels, float).reshape(-1, 2)
        if weights is None or len(weights) != len(pts):
            weights = [1.0] * len(pts)
        for (x, y), (pan, tilt), (dp, dt), wt in zip(pts, np.asarray(angles, float).reshape(-1, 2),
                                                      self.errors(pixels, angles), weights):
            err = max(abs(dp), abs(dt))
            worst = max(worst, err) if math.isfinite(err) else float("inf")
            flag = "  <-- WARN" if not err <= warn_deg else ""
            if wt < 0.5:
                flag += " (bad click? counted less)"
            cm = self.deg_to_cm(math.hypot(dp, dt), (x, y)) if math.isfinite(err) else float("nan")
            lines.append(f"px ({x:6.1f},{y:6.1f}) real ({pan:6.1f},{tilt:6.1f}) "
                         f"pred ({pan + dp:6.1f},{tilt + dt:6.1f}) err ({dp:+5.2f},{dt:+5.2f}) {cm:4.1f} cm{flag}")
        return lines, worst

    def summary(self) -> list[str]:
        """Which model, how well each fits (in-sample and leave-one-out), and the plane model's geometry."""
        out = []
        frame = f"{self.frame[0]}x{self.frame[1]}" if self.frame else "unknown size"
        rot = self.rotate if self.rotate is not None else "unknown"
        n = len(self.points["px"]) if self.points else 0
        out.append(f"model: {self.model} | frame {frame}, CAM_ROTATE {rot} | {n} points")
        for k in MODELS:
            cv = self.cv.get(k)
            if not cv:
                continue
            loo = cv.get("loo")
            loo_s = f", leave-one-out {loo:.2f} deg (~{self.deg_to_cm(loo, self._centre()):.1f} cm)" if loo is not None else ""
            used = "  <- used" if k == self.model else ""
            bad = f", {cv['bad']} bad click(s) counted less" if cv.get("bad") else ""
            out.append(f"  {k:5s}: fit {cv['fit']:.2f} deg{loo_s}{bad}{used}")
        if self.plane is not None:
            out.append("  " + self.plane.summary())
        return out

    def _centre(self):
        if self.points:
            return tuple(np.mean(np.asarray(self.points["px"], float), axis=0))
        if self.frame:
            return (self.frame[0] / 2, self.frame[1] / 2)
        return (160.0, 120.0)

    # ── frame checks ────────────────────────────────────────────────────────
    def frame_check(self, size: tuple[int, int] | None, rotate=None) -> FrameCheck:
        """Compare the runtime frame (w, h) and CAM_ROTATE with what the calibration was made with."""
        rot = norm_rotate(rotate)
        if self.rotate is not None and rotate is not None and rot != self.rotate:
            msg = (f"calibrated with CAM_ROTATE={self.rotate} but the camera now uses CAM_ROTATE={rot}: "
                   f"pixel positions don't match, laser off. Recalibrate (python -m coach.calibrate)")
            return FrameCheck(ok=False, msg=msg, tft="CALIB ROTATE: recal")
        if self.frame is None or size is None:
            return FrameCheck()
        w, h = int(size[0]), int(size[1])
        cw, ch = self.frame
        if (w, h) == (cw, ch):
            return FrameCheck()
        s = cw / w      # the OV3660 keeps its full width at every frame size (4:3, 3:2 crop the height)
        same_shape = abs(w / h - cw / ch) < 0.01
        msg = (f"camera frames are {w}x{h} but the calibration was made at {cw}x{ch}: rescaling pixel "
               f"positions" + ("" if same_shape else " (different shape: approximate, assumes a centred crop)") +
               ". Recalibrate at this resolution for best accuracy")
        return FrameCheck(scale=s, run_c=(w / 2, h / 2), cal_c=(cw / 2, ch / 2), msg=msg,
                          tft=f"CALIB {cw}x{ch} SCALED"[:20])

    # ── files ───────────────────────────────────────────────────────────────
    def to_json(self) -> dict:
        d = {"version": FORMAT_VERSION, "terms": list(TERMS),
             "coef": None if self.poly.coef is None else self.poly.coef.tolist(), "model": self.model,
             "plane": self.plane.params if self.plane is not None else None,
             "frame": list(self.frame) if self.frame else None, "rotate": self.rotate,
             "cv": self.cv, "points": self.points, "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        return d

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_json(), indent=1))

    @classmethod
    def from_json(cls, d, model: str | None = None) -> "Calibration":
        if not isinstance(d, dict):         # v0: a bare coefficient list
            return cls(d)
        c = cls(d.get("coef"), model=d.get("model", "poly"), plane=d.get("plane"), frame=d.get("frame"),
                rotate=d.get("rotate"), points=d.get("points"), cv=d.get("cv"))
        if model in MODELS:
            if model == "plane" and c.plane is None:
                log.warning("CALIB_MODEL=plane but the calibration has no plane model: using the polynomial")
            else:
                c.model = model
        return c

    @classmethod
    def load(cls, path, model: str | None = None) -> "Calibration":
        """Read calibration.json (any version). model: force "poly"/"plane"; None = CALIB_MODEL from .env, if set."""
        if model is None:
            from .config import CONFIG
            model = getattr(CONFIG, "calib_model", "auto")
        return cls.from_json(json.loads(Path(path).read_text()), model)


def _fit_stats(err, weights) -> dict:
    """In-sample RMS error (deg) over the points that count (weight >= 0.5) + how many were down-weighted."""
    w = np.asarray(weights, float)
    good = w >= 0.5
    return {"fit": rms(np.asarray(err)[good]) if good.any() else rms(err), "bad": int((~good).sum())}


def default_frame_size() -> tuple[int, int] | None:
    """Size of the newest camera frame in this process (frames.FrameGrabber records it)."""
    from .frames import last_frame_size
    return last_frame_size()


class Aimer:
    """Implements AimerAPI. Call point_at()/laser_off() from the asyncio loop; run() is the control loop.

    point_at() returns "ok", "not_visible", "unknown_object", or "out_of_reach" (visible, but outside the servo
    limits: the laser stays off). Each tick: follow the target's detection (median of its last few boxes, sticking
    to the same instance), aim AIM_Y of the way down its box, step the servos there with the final move on each
    axis in APPROACH_DIR (so servo backlash always settles the same way, as in calibration), and only claim
    on_target once the servos have arrived at a goal that wasn't clamped.
    """

    # Servo limits: per instance, from PAN_MIN / PAN_MAX / TILT_MIN / TILT_MAX in laptop/.env (default 0-180,
    # the firmware's full range). Set TILT_* once the level angle is known: it stops a bad fit aiming up.
    PAN_MIN, PAN_MAX, TILT_MIN, TILT_MAX = 0.0, 180.0, 0.0, 180.0
    MAX_STEP_DEG = 4.0      # rate limit per tick
    DEADBAND_DEG = 0.5      # don't send changes smaller than this (servo buzz)
    HOLD_DEG = 1.0          # on target with a full smoothing window (open loop): ignore smaller goal changes
    SMOOTH_N = 5            # median of the last N detections (new boxes only; ~0.7 s of YOLO at 7 fps)
    JUMP_FRAC = 0.75        # a box this far (x its size) from the smoothed one: the object moved / another instance
    JUMP_MIN_PX = 12.0
    REACH_TOL_DEG = 1.0     # goal outside the limits by more than this = out of reach
    APPROACH_DEG = 3.0      # final approach length on each axis, in APPROACH_DIR (cancels backlash). 0 = off
    APPROACH_DIR = APPROACH_DIR
    SETTLE_S = 0.15         # link reports it stopped moving -> this long before on_target (ControlLink only)
    LOST_HOLD_S = 1.0       # target unseen this long -> hold position
    LOST_OFF_S = 3.0        # target unseen this long -> laser off

    # Closed loop (stretch, only with closed_loop=True; needs a DotTracker writing State.dot)
    CLOSE_PX = 10.0         # dot within this many px of the target = on target (at 640 wide; scaled to the frame)
    CL_GAIN = 0.7           # fraction of the pixel error corrected per nudge
    CL_MAX_NUDGE_DEG = 3.0  # per nudge, per axis
    CL_MAX_TRIM_DEG = 10.0  # total correction on top of the calibration, per axis (a bad dot can't drag it far)
    CL_WAIT_S = 0.35        # servos still this long before the dot is trusted (servo settle + camera lag)
    CL_DOT_TIMEOUT_S = 1.2  # no dot this long after the move -> fall back to the open-loop on_target
    SIGN_GROW_PX = 3.0      # an axis error grew by more than this after a nudge -> flip that axis (once per target)
    CL_CARRY = True         # a new target starts from the last confirmed trim: a posture change (counter nearer or
                            # further than at calibration) shifts every target about the same way

    def __init__(self, link: ControlLinkAPI, calib: Calibration | None, state: State,
                 hz: float = 30.0, clock: Callable[[], float] = time.monotonic, closed_loop: bool = False,
                 limits: tuple[float, float, float, float] | None = None, aim_y: float | None = None,
                 approach_deg: float | None = None,
                 frame_size: Callable[[], tuple[int, int] | None] | None = None, cam_rotate: str | None = None):
        self.link, self.calib, self.state, self.hz, self.clock = link, calib, state, hz, clock
        from .config import CONFIG
        if limits is None:
            limits = (CONFIG.pan_min, CONFIG.pan_max, CONFIG.tilt_min, CONFIG.tilt_max)
        self.PAN_MIN, self.PAN_MAX, self.TILT_MIN, self.TILT_MAX = map(float, limits)
        self.aim_y = float(CONFIG.aim_y if aim_y is None else aim_y)
        if approach_deg is not None:
            self.APPROACH_DEG = float(approach_deg)
        self.frame_size = frame_size if frame_size is not None else default_frame_size
        self.cam_rotate = CONFIG.cam_rotate if cam_rotate is None else cam_rotate
        self.closed_loop = closed_loop
        self.target: str | None = None
        self.on_target = False
        self.reachable = True   # False while the target is visible but outside the servo limits (laser off)
        self.aim_px: tuple[float, float] | None = None  # where we aim, runtime pixels (dashboard / debugging)
        self._boxes: deque[tuple[float, float, float, float]] = deque(maxlen=self.SMOOTH_N)
        self._last_box = None
        self._last_point = self._last_seen = 0.0
        self.goal: tuple[float, float] | None = None   # last commanded goal (pan, tilt), for debugging/dashboard
        self._final: tuple[float, float] | None = None  # goal being driven to (after hold/deadband)
        self._via: tuple[float, float] | None = None    # approach point below the goal, visited first
        self._arrived_at: float | None = None
        self.carry = (0.0, 0.0)  # closed loop: trim of the last target the dot confirmed (session knowledge)
        self._reset_closed_loop()
        self._moved_at = 0.0    # last time the servos were commanded to move (or nudged)
        self._turn = None       # the TurnLatency that was current at point_at (gets LAT_ON_TARGET)
        self._fsize = None      # frame size the frame check was made for
        self._fc = FrameCheck()
        self._fc_calib = None
        self._fc_logged = ""

    # ── AimerAPI ──────────────────────────────────────────────────────────────
    def point_at(self, name: str) -> PointResult:
        if name not in self.state.recipe.objects:
            return "unknown_object"
        if name not in self.state.visible():
            self.laser_off()
            return "not_visible"
        if name != self.target:
            self._reset_track()
        usable = self._usable()
        if usable:
            det = self._pick(name)
            if det is not None and self._aim(det.box) is None:
                lim = f"pan {self.PAN_MIN:.0f}-{self.PAN_MAX:.0f}, tilt {self.TILT_MIN:.0f}-{self.TILT_MAX:.0f}"
                log.warning("aimer: %s at px (%.0f, %.0f) is out of the laser's reach (%s): laser off",
                            name, *det.center, lim)
                self.laser_off()
                return OUT_OF_REACH     # type: ignore[return-value]
        self.target = self.state.target = name
        self.on_target = self.state.on_target = False
        self.reachable = True
        # No (usable) calibration -> the laser would land somewhere random: keep it off (still "ok").
        self.link.laser = usable
        self._last_point = self._last_seen = self.clock()
        self._verdict, self._turn = None, (self.state.turns[-1] if self.state.turns else None)
        if self.calib is None:
            self._warn_no_calib()
        return "ok"

    def laser_off(self) -> None:
        self.link.laser = False
        self.target = self.state.target = None
        self.on_target = self.state.on_target = False
        self.reachable = True
        self._reset_track()

    def _reset_track(self) -> None:
        self._boxes.clear()
        self._last_box = None
        self.goal = self._final = self._via = self.aim_px = None
        self._arrived_at = None
        self._reset_closed_loop()

    # ── loop ──────────────────────────────────────────────────────────────────
    async def run(self) -> None:
        if self.calib is None:
            self._warn_no_calib()
        period = 1.0 / self.hz
        while True:
            try:
                self.tick()
            except Exception:       # never let one bad tick kill the loop
                log.exception("aimer tick failed")
            await asyncio.sleep(period)

    def tick(self) -> None:
        if self.target is None or self.calib is None:
            return
        if not self._usable():
            if self.link.laser:
                self.link.laser = False
            self.on_target = self.state.on_target = False
            return
        now = self.clock()
        if now - self._last_point >= LASER_IDLE_OFF_S:
            log.info("aimer: idle %.0fs, laser off", LASER_IDLE_OFF_S)
            self.laser_off()
            return
        det = self._pick(self.target)
        if det is not None:
            self._last_seen = now
            self._add_box(det.box)
        else:
            lost = now - self._last_seen
            if lost >= self.LOST_OFF_S:
                log.info("aimer: %s lost for %.1fs, laser off", self.target, lost)
                self.laser_off()
                return
            if lost >= self.LOST_HOLD_S:     # hold where we are
                self.on_target = self.state.on_target = False
                return
        if not self._boxes:
            return
        box = tuple(np.median(np.asarray(self._boxes), axis=0))
        aim = self._aim(box)
        if aim is None:
            if self.reachable:
                log.warning("aimer: %s moved out of the laser's reach: laser off until it's back in reach",
                            self.target)
            self.reachable = False
            self.link.laser = False
            self.on_target = self.state.on_target = False
            return
        if not self.reachable:
            log.info("aimer: %s back in reach, laser on", self.target)
            self.reachable = True
            self.link.laser = True
        (gp, gt), self.aim_px, look_px = aim
        if self.closed_loop:
            gp, gt = gp + self.trim[0], gt + self.trim[1]
        gp = min(max(gp, self.PAN_MIN), self.PAN_MAX)
        gt = min(max(gt, self.TILT_MIN), self.TILT_MAX)
        self.goal = (gp, gt)
        reached = self._drive((gp, gt), now)
        if self.closed_loop and reached:
            reached = self._correct(now, look_px)
        self.on_target = self.state.on_target = reached

    # ── target tracking ─────────────────────────────────────────────────────
    def _pick(self, name: str) -> Det | None:
        """The detection to follow: the one nearest the box we're tracking (if close), else the most confident."""
        hits = [d for d in self.state.dets if d.name == name]
        if not hits:
            return None
        if self._boxes:
            ref = np.median(np.asarray(self._boxes), axis=0)
            near = min(hits, key=lambda d: _box_dist(d.box, ref))
            if _box_dist(near.box, ref) <= self._jump_px(ref):
                return near
        return max(hits, key=lambda d: d.conf)

    def _jump_px(self, box) -> float:
        return max(self.JUMP_MIN_PX, self.JUMP_FRAC * max(box[2] - box[0], box[3] - box[1]))

    def _add_box(self, box) -> None:
        box = tuple(float(v) for v in box)
        if box == self._last_box:           # same detection as last tick (YOLO runs slower than the aimer)
            return
        self._last_box = box
        if self._boxes:
            ref = np.median(np.asarray(self._boxes), axis=0)
            if _box_dist(box, ref) > self._jump_px(ref):
                self._boxes.clear()         # moved or a different instance: don't average across it
        self._boxes.append(box)

    def _aim(self, box):
        """(goal angles, aim pixel, look pixel) for a box, or None if no part of it is within reach.

        Aims at the box's horizontal centre, aim_y of the way down. If that's outside the servo limits, the
        reachable point of the box nearest to it (an object half in reach gets the dot on its reachable half).
        The look pixel is where the dot should be SEEN (closed loop): the box centre, because aim_y > 0.5 only
        makes up for the open-loop parallax (the ray through the centre meets the counter behind the fruit).
        """
        x1, y1, x2, y2 = (float(v) for v in box)
        w, h = x2 - x1, y2 - y1
        aim = np.array([(x1 + x2) / 2, y1 + self.aim_y * h])
        xs = np.linspace(x1 + 0.2 * w, x2 - 0.2 * w, 5)
        ys = np.linspace(y1 + 0.15 * h, y2 - 0.15 * h, 5)
        grid = np.array([(x, y) for y in ys for x in xs])
        grid = grid[np.argsort(np.hypot(*(grid - aim).T))]
        cands = np.vstack([aim, grid])
        ang = self.calib.angles_many(self._fc.to_calib(cands))
        tol = self.REACH_TOL_DEG
        ok = (np.all(np.isfinite(ang), axis=1) &
              (ang[:, 0] >= self.PAN_MIN - tol) & (ang[:, 0] <= self.PAN_MAX + tol) &
              (ang[:, 1] >= self.TILT_MIN - tol) & (ang[:, 1] <= self.TILT_MAX + tol))
        if not ok.any():
            return None
        i = int(np.argmax(ok))
        aim_px = (float(cands[i, 0]), float(cands[i, 1]))
        look_px = (float(aim[0]), y1 + 0.5 * h) if i == 0 else aim_px
        return (float(ang[i, 0]), float(ang[i, 1])), aim_px, look_px

    # ── servo drive ─────────────────────────────────────────────────────────
    def _drive(self, goal: tuple[float, float], now: float) -> bool:
        """Step towards goal (via the approach point if the move would end the wrong way). True once arrived and
        still."""
        settled_track = self.on_target and not self.closed_loop and len(self._boxes) >= self.SMOOTH_N
        hold = self.HOLD_DEG if settled_track else self.DEADBAND_DEG
        if self._final is None or max(abs(goal[0] - self._final[0]), abs(goal[1] - self._final[1])) >= hold:
            self._final = goal
            self._arrived_at = None
            cur = (self.link.pan, self.link.tilt)
            via = (self._via_axis(goal[0], cur[0], self.PAN_MIN, self.PAN_MAX, self.APPROACH_DIR[0]),
                   self._via_axis(goal[1], cur[1], self.TILT_MIN, self.TILT_MAX, self.APPROACH_DIR[1]))
            self._via = via if via != goal else None
        target = self._via if self._via is not None else self._final
        pan, tilt = self._step(self.link.pan, target[0]), self._step(self.link.tilt, target[1])
        moving = bool(getattr(self.link, "moving", False))
        if (pan, tilt) != (self.link.pan, self.link.tilt) or moving:
            self._moved_at, self._verdict, self._arrived_at = now, None, None
        self.link.pan, self.link.tilt = pan, tilt
        at = (abs(target[0] - self.link.pan) < self.DEADBAND_DEG and abs(target[1] - self.link.tilt) < self.DEADBAND_DEG)
        if not at or moving:
            return False
        if self._via is not None:
            self._via = None                # at the approach point: next tick goes up to the goal
            return False
        if self._arrived_at is None:
            self._arrived_at = now
        settle = self.SETTLE_S if hasattr(self.link, "moving") else 0.0
        return now - self._arrived_at >= settle

    def _via_axis(self, goal: float, cur: float, lo: float, hi: float, direction: float) -> float:
        """Approach point on one axis: goal itself if the move already ends in `direction`, else overshoot."""
        if self.APPROACH_DEG <= 0 or direction * (goal - cur) > -self.DEADBAND_DEG:
            return goal                     # the move already ends the right way (or there's no move)
        return min(max(goal - direction * self.APPROACH_DEG, lo), hi)

    def _step(self, cur: float, goal: float) -> float:
        d = goal - cur
        if abs(d) < self.DEADBAND_DEG:
            return cur
        return float(cur + max(-self.MAX_STEP_DEG, min(self.MAX_STEP_DEG, d)))

    # ── calibration vs camera ───────────────────────────────────────────────
    def _usable(self) -> bool:
        """Calibration present and it matches the camera (re-checked whenever the frame size changes)."""
        if self.calib is None:
            return False
        try:
            size = self.frame_size() if self.frame_size is not None else None
        except Exception:
            size = None
        if size != self._fsize or self._fc_logged == "" or self.calib is not self._fc_calib:
            self._fsize, self._fc_calib = size, self.calib
            self._fc = self.calib.frame_check(size, self.cam_rotate)
            if self._fc.msg and self._fc.msg != self._fc_logged:
                (log.error if not self._fc.ok else log.warning)("aimer: %s", self._fc.msg)
                if self._fc.tft:
                    self.link.show_text(TFT_STATUS, self._fc.tft)
            self._fc_logged = self._fc.msg or "-"
        return self._fc.ok

    # ── closed loop (stretch) ─────────────────────────────────────────────────
    def _reset_closed_loop(self) -> None:
        # correction added to the calibration's goal (pan, tilt), degrees, learned per target (starts from carry)
        self.trim = self.carry if self.CL_CARRY else (0.0, 0.0)
        self._sign, self._flipped = [1.0, 1.0], [False, False]
        self._last_err = None   # (target px, pixel error) at the last nudge, for the sign check
        self._verdict = None    # dot-measured on_target since the last move; None = not measured yet

    def _correct(self, now: float, target: tuple[float, float]) -> bool:
        """Servos are at the goal: compare the seen dot with the target and nudge `trim`. Returns on_target.

        No dot seen since the move -> False for CL_DOT_TIMEOUT_S (a blink takes ~0.5 s), then the open-loop
        answer (True). The dot seen and then lost -> keep what it said.
        """
        if now - self._moved_at < self.CL_WAIT_S:   # frames don't show the latest move yet
            return bool(self._verdict)
        dot = self.state.dot
        if dot is None:
            return now - self._moved_at >= self.CL_DOT_TIMEOUT_S if self._verdict is None else self._verdict
        err = (target[0] - dot[0], target[1] - dot[1])
        if np.hypot(*err) <= self._close_px():
            if self._turn is not None:
                self._turn.mark(LAT_ON_TARGET)       # once per point_at
                self._turn = None
            self._verdict, self._last_err = True, None
            self.carry = self.trim
            return True
        self._check_sign(target, err)
        # The calibration says how far the dot is from where we want it, in degrees: angles(aim) - angles(dot).
        aim = (dot[0] + self._sign[0] * err[0], dot[1] + self._sign[1] * err[1])
        d = np.subtract(*self.calib.angles_many(self._fc.to_calib([aim, dot]))) * self.CL_GAIN
        if not np.all(np.isfinite(d)):
            return False
        d = np.clip(d, -self.CL_MAX_NUDGE_DEG, self.CL_MAX_NUDGE_DEG)
        self.trim = tuple(float(v) for v in np.clip(np.add(self.trim, d), -self.CL_MAX_TRIM_DEG, self.CL_MAX_TRIM_DEG))
        self._moved_at, self._last_err, self._verdict = now, (target, err), False
        log.debug("aimer: dot %s target %s err %s -> trim %s", dot, target, err, self.trim)
        return False

    def _close_px(self) -> float:
        """CLOSE_PX for this camera: it's meant for 640-wide frames (~1 deg); at 320x240 that's 5 px."""
        if self._fsize:
            return max(4.0, self.CLOSE_PX * self._fsize[0] / 640.0)
        return self.CLOSE_PX

    def _check_sign(self, target, err) -> None:
        """If an axis error grew after the last nudge (and the target didn't move), that axis is mirrored: flip it."""
        if self._last_err is None:
            return
        last_target, last = self._last_err
        if np.hypot(target[0] - last_target[0], target[1] - last_target[1]) > self.SIGN_GROW_PX:
            return
        for k in (0, 1):
            grew = abs(last[k]) > self._close_px() / 2 and abs(err[k]) > abs(last[k]) + self.SIGN_GROW_PX
            if grew and not self._flipped[k]:
                self._sign[k], self._flipped[k] = -self._sign[k], True
                log.warning("aimer: closed-loop error grew on axis %s, flipping its sign", "xy"[k])

    def _warn_no_calib(self) -> None:
        log.warning("aimer: no calibration.json, laser won't move (run python -m coach.calibrate)")
        self.link.show_text(TFT_STATUS, "NO CALIB: aim off")


def _box_dist(a, b) -> float:
    """Distance between box centres, px."""
    return float(math.hypot((a[0] + a[2] - b[0] - b[2]) / 2, (a[1] + a[3] - b[1] - b[3]) / 2))
