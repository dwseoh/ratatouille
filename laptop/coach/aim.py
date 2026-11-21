"""Aiming (v1 = open loop): click calibration (pixel -> servo angles) + the Aimer loop that implements AimerAPI.

Closed-loop correction on the seen laser dot is stretch (see docs/cut-list.md).
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

from .contracts import LASER_IDLE_OFF_S, TFT_STATUS, ControlLinkAPI, PointResult, State

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

    def __init__(self, link: ControlLinkAPI, calib: Calibration | None, state: State,
                 hz: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.link, self.calib, self.state, self.hz, self.clock = link, calib, state, hz, clock
        self.target: str | None = None
        self.on_target = False
        self._centers: deque[tuple[float, float]] = deque(maxlen=self.SMOOTH_N)
        self._last_point = self._last_seen = 0.0
        self.goal: tuple[float, float] | None = None   # last commanded goal (pan, tilt), for debugging/dashboard

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
        self.target = self.state.target = name
        self.on_target = self.state.on_target = False
        self.link.laser = True
        self._last_point = self._last_seen = self.clock()
        if self.calib is None:
            self._warn_no_calib()
        return "ok"

    def laser_off(self) -> None:
        self.link.laser = False
        self.target = self.state.target = None
        self.on_target = self.state.on_target = False
        self._centers.clear()
        self.goal = None

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
        gp = min(max(gp, self.PAN_MIN), self.PAN_MAX)
        self.goal = (gp, gt)
        self.link.pan = self._step(self.link.pan, gp)
        self.link.tilt = self._step(self.link.tilt, gt)
        reached = abs(gp - self.link.pan) < self.DEADBAND_DEG and abs(gt - self.link.tilt) < self.DEADBAND_DEG
        self.on_target = self.state.on_target = reached

    def _step(self, cur: float, goal: float) -> float:
        d = goal - cur
        if abs(d) < self.DEADBAND_DEG:
            return cur
        return float(cur + max(-self.MAX_STEP_DEG, min(self.MAX_STEP_DEG, d)))

    def _warn_no_calib(self) -> None:
        log.warning("aimer: no calibration.json, laser won't move (run python -m coach.calibrate)")
        self.link.show_text(TFT_STATUS, "NO CALIB: aim off")
