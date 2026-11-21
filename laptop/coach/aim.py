"""Aiming: calibrated first guess (pixel -> angles) + closed-loop correction on the seen dot."""
import json
from pathlib import Path

import numpy as np


class Calibration:
    """Fit angles = f(pixel) with a 2nd-order polynomial from a grid sweep.

    Sweep: for pan in 5 values x tilt in 5 values, move servos, wait ~300 ms, record dot pixel.
    TODO: write the sweep routine (coach.main --calibrate).
    """

    def __init__(self, coef=None):
        self.coef = coef  # shape (6, 2)

    @staticmethod
    def _features(px):
        x, y = np.asarray(px, dtype=float).T
        return np.stack([np.ones_like(x), x, y, x * y, x * x, y * y], axis=-1)

    def fit(self, pixels, angles):
        self.coef, *_ = np.linalg.lstsq(self._features(pixels), np.asarray(angles, float), rcond=None)
        return self

    def angles_for(self, px):
        return (self._features([px]) @ self.coef)[0]  # (pan, tilt)

    def save(self, path):
        Path(path).write_text(json.dumps(self.coef.tolist()))

    @classmethod
    def load(cls, path):
        return cls(np.array(json.loads(Path(path).read_text())))


class Aimer:
    def __init__(self, link, calib: Calibration | None, gain: float = 0.04, close_px: int = 10):
        self.link, self.calib, self.gain, self.close_px = link, calib, gain, close_px
        self.target_name: str | None = None
        self.on_target = False

    def set_target(self, name: str | None):
        """Called by the agent (point_at) or the point-first keyword matcher."""
        self.target_name = name
        self.on_target = False
        self.link.laser = name is not None

    def step(self, target_px, dot_px):
        """One control tick. target_px/dot_px may be None when not visible."""
        if self.target_name is None or target_px is None:
            return
        if dot_px is None:
            if self.calib is not None:  # feed-forward jump from calibration
                self.link.pan, self.link.tilt = map(float, self.calib.angles_for(target_px))
            return
        ex, ey = target_px[0] - dot_px[0], target_px[1] - dot_px[1]
        self.on_target = (ex * ex + ey * ey) ** 0.5 < self.close_px
        if not self.on_target:  # TODO: signs depend on mounting; flip if it runs away
            self.link.pan -= self.gain * ex
            self.link.tilt += self.gain * ey
