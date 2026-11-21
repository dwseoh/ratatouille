"""Aiming (v1 = open loop): click calibration (pixel -> servo angles) + the Aimer loop that implements AimerAPI.

Closed-loop correction on the seen laser dot is stretch (see docs/cut-list.md): Aimer(..., closed_loop=True)
plus a dot.DotTracker writing State.dot. Off by default; with it off nothing here reads State.dot.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Callable

import numpy as np

from .contracts import LASER_IDLE_OFF_S, LAT_ON_TARGET, TFT_STATUS, ControlLinkAPI, PointResult, State

log = logging.getLogger(__name__)

TERMS = ("1", "x", "y", "x2", "xy", "y2")
MIN_FIT_POINTS = len(TERMS)


class Calibration:
    """angles = f(pixel), a 2nd-order 2D polynomial per output: pan, tilt = c . [1, x, y, x², xy, y²]."""

    def __init__(self, coef=None):
        self.coef = None if coef is None else np.asarray(coef, dtype=float)  # shape (6, 2): column 0 pan, 1 tilt

    @staticmethod
    def _features(px) -> np.ndarray:
        x, y = np.asarray(px, dtype=float).reshape(-1, 2).T
        return np.stack([np.ones_like(x), x, y, x * x, x * y, y * y], axis=-1)

    def fit(self, pixels, angles) -> "Calibration":
        pixels, angles = np.asarray(pixels, float).reshape(-1, 2), np.asarray(angles, float).reshape(-1, 2)
        if len(pixels) != len(angles):
            raise ValueError(f"{len(pixels)} pixels but {len(angles)} angles")
        if len(pixels) < MIN_FIT_POINTS:
            raise ValueError(f"need at least {MIN_FIT_POINTS} points to fit, got {len(pixels)}")
        self.coef, *_ = np.linalg.lstsq(self._features(pixels), angles, rcond=None)
        return self

    def angles_for(self, px) -> tuple[float, float]:
        if self.coef is None:
            raise RuntimeError("Calibration not fitted")
        pan, tilt = (self._features(px) @ self.coef)[0]
        return float(pan), float(tilt)

    def errors(self, pixels, angles) -> list[tuple[float, float]]:
        """Per point (pan error, tilt error) in degrees = predicted - real."""
        pred = self._features(pixels) @ self.coef
        return [(float(dp), float(dt)) for dp, dt in pred - np.asarray(angles, float).reshape(-1, 2)]

    def report(self, pixels, angles, warn_deg: float = 2.0) -> tuple[list[str], float]:
        """Human-readable per-point fit error lines + the worst error (deg). Lines over warn_deg end in '<-- WARN'."""
        lines, worst = [], 0.0
        for (x, y), (pan, tilt), (dp, dt) in zip(np.asarray(pixels, float).reshape(-1, 2),
                                                  np.asarray(angles, float).reshape(-1, 2),
                                                  self.errors(pixels, angles)):
            err = max(abs(dp), abs(dt))
            worst = max(worst, err)
            flag = "  <-- WARN" if err > warn_deg else ""
            lines.append(f"px ({x:6.1f},{y:6.1f}) real ({pan:6.1f},{tilt:6.1f}) "
                         f"pred ({pan + dp:6.1f},{tilt + dt:6.1f}) err ({dp:+5.2f},{dt:+5.2f}){flag}")
        return lines, worst

    def save(self, path) -> None:
        Path(path).write_text(json.dumps({"terms": list(TERMS), "coef": self.coef.tolist()}, indent=1))

    @classmethod
    def load(cls, path) -> "Calibration":
        d = json.loads(Path(path).read_text())
        return cls(d["coef"] if isinstance(d, dict) else d)


class Aimer:
    """Implements AimerAPI. Call point_at()/laser_off() from the asyncio loop; run() is the control loop."""

    PAN_MIN, PAN_MAX = 10.0, 170.0
    MAX_STEP_DEG = 4.0      # rate limit per tick
    DEADBAND_DEG = 0.5      # don't send changes smaller than this (servo buzz)
    SMOOTH_N = 3            # mean of the last N target centres
    LOST_HOLD_S = 1.0       # target unseen this long -> hold position
    LOST_OFF_S = 3.0        # target unseen this long -> laser off

    # Closed loop (stretch, only with closed_loop=True; needs a DotTracker writing State.dot)
    CLOSE_PX = 10.0         # dot within this many px of the target centre = on target
    CL_GAIN = 0.7           # fraction of the pixel error corrected per nudge
    CL_MAX_NUDGE_DEG = 3.0  # per nudge, per axis
    CL_MAX_TRIM_DEG = 10.0  # total correction on top of the calibration, per axis (a bad dot can't drag it far)
    CL_WAIT_S = 0.35        # servos still this long before the dot is trusted (servo settle + camera lag)
    CL_DOT_TIMEOUT_S = 1.2  # no dot this long after the move -> fall back to the open-loop on_target
    SIGN_GROW_PX = 3.0      # an axis error grew by more than this after a nudge -> flip that axis (once per target)

    def __init__(self, link: ControlLinkAPI, calib: Calibration | None, state: State,
                 hz: float = 30.0, clock: Callable[[], float] = time.monotonic, closed_loop: bool = False):
        self.link, self.calib, self.state, self.hz, self.clock = link, calib, state, hz, clock
        self.closed_loop = closed_loop
        self.target: str | None = None
        self.on_target = False
        self._centers: deque[tuple[float, float]] = deque(maxlen=self.SMOOTH_N)
        self._last_point = self._last_seen = 0.0
        self.goal: tuple[float, float] | None = None   # last commanded goal (pan, tilt), for debugging/dashboard
        self._reset_closed_loop()
        self._moved_at = 0.0    # last time the servos were commanded to move (or nudged)
        self._turn = None       # the TurnLatency that was current at point_at (gets LAT_ON_TARGET)

    # ── AimerAPI ──────────────────────────────────────────────────────────────
    def point_at(self, name: str) -> PointResult:
        if name not in self.state.recipe.objects:
            return "unknown_object"
        if name not in self.state.visible():
            self.laser_off()
            return "not_visible"
        if name != self.target:
            self._centers.clear()
            self.goal = None
            self._reset_closed_loop()
        self.target = self.state.target = name
        self.on_target = self.state.on_target = False
        self.link.laser = True
        self._last_point = self._last_seen = self.clock()
        self._verdict, self._turn = None, (self.state.turns[-1] if self.state.turns else None)
        if self.calib is None:
            self._warn_no_calib()
        return "ok"

    def laser_off(self) -> None:
        self.link.laser = False
        self.target = self.state.target = None
        self.on_target = self.state.on_target = False
        self._centers.clear()
        self.goal = None
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
        now = self.clock()
        if now - self._last_point >= LASER_IDLE_OFF_S:
            log.info("aimer: idle %.0fs, laser off", LASER_IDLE_OFF_S)
            self.laser_off()
            return
        det = self.state.det(self.target)
        if det is not None:
            self._last_seen = now
            self._centers.append(det.center)
        else:
            lost = now - self._last_seen
            if lost >= self.LOST_OFF_S:
                log.info("aimer: %s lost for %.1fs, laser off", self.target, lost)
                self.laser_off()
                return
            if lost >= self.LOST_HOLD_S:     # hold where we are
                self.on_target = self.state.on_target = False
                return
        if not self._centers:
            return
        cx, cy = np.mean(np.asarray(self._centers), axis=0)
        gp, gt = self.calib.angles_for((cx, cy))
        if self.closed_loop:
            gp, gt = gp + self.trim[0], gt + self.trim[1]
        gp = min(max(gp, self.PAN_MIN), self.PAN_MAX)
        self.goal = (gp, gt)
        pan, tilt = self._step(self.link.pan, gp), self._step(self.link.tilt, gt)
        if (pan, tilt) != (self.link.pan, self.link.tilt):
            self._moved_at, self._verdict = now, None
        self.link.pan, self.link.tilt = pan, tilt
        reached = abs(gp - self.link.pan) < self.DEADBAND_DEG and abs(gt - self.link.tilt) < self.DEADBAND_DEG
        if self.closed_loop and reached:
            reached = self._correct(now, (float(cx), float(cy)))
        self.on_target = self.state.on_target = reached

    def _step(self, cur: float, goal: float) -> float:
        d = goal - cur
        if abs(d) < self.DEADBAND_DEG:
            return cur
        return float(cur + max(-self.MAX_STEP_DEG, min(self.MAX_STEP_DEG, d)))

    # ── closed loop (stretch) ─────────────────────────────────────────────────
    def _reset_closed_loop(self) -> None:
        self.trim = (0.0, 0.0)  # correction added to the calibration's goal (pan, tilt), degrees, learned per target
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
        if np.hypot(*err) <= self.CLOSE_PX:
            if self._turn is not None:
                self._turn.mark(LAT_ON_TARGET)       # once per point_at
                self._turn = None
            self._verdict, self._last_err = True, None
            return True
        self._check_sign(target, err)
        # The calibration says how far the dot is from where we want it, in degrees: angles(aim) - angles(dot).
        aim = (dot[0] + self._sign[0] * err[0], dot[1] + self._sign[1] * err[1])
        d = np.subtract(self.calib.angles_for(aim), self.calib.angles_for(dot)) * self.CL_GAIN
        d = np.clip(d, -self.CL_MAX_NUDGE_DEG, self.CL_MAX_NUDGE_DEG)
        self.trim = tuple(float(v) for v in np.clip(np.add(self.trim, d), -self.CL_MAX_TRIM_DEG, self.CL_MAX_TRIM_DEG))
        self._moved_at, self._last_err, self._verdict = now, (target, err), False
        log.debug("aimer: dot %s target %s err %s -> trim %s", dot, target, err, self.trim)
        return False

    def _check_sign(self, target, err) -> None:
        """If an axis error grew after the last nudge (and the target didn't move), that axis is mirrored: flip it."""
        if self._last_err is None:
            return
        last_target, last = self._last_err
        if np.hypot(target[0] - last_target[0], target[1] - last_target[1]) > self.SIGN_GROW_PX:
            return
        for k in (0, 1):
            grew = abs(last[k]) > self.CLOSE_PX / 2 and abs(err[k]) > abs(last[k]) + self.SIGN_GROW_PX
            if grew and not self._flipped[k]:
                self._sign[k], self._flipped[k] = -self._sign[k], True
                log.warning("aimer: closed-loop error grew on axis %s, flipping its sign", "xy"[k])

    def _warn_no_calib(self) -> None:
        log.warning("aimer: no calibration.json, laser won't move (run python -m coach.calibrate)")
        self.link.show_text(TFT_STATUS, "NO CALIB: aim off")
