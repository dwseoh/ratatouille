"""Synthetic laser rig for aiming simulations and tests (no hardware): camera + counter + pan-tilt + SG90 servos.

The truth here is deliberately richer than coach.aim_model.PlaneModel, so a fit can't be exact by construction:
two-term lens distortion, principal point offset, camera yaw, tilt axis offset from the pan axis, beam offset from the
tilt axis, tilt axis skew, a beam cone angle, servo gain/offset errors, a smooth servo nonlinearity, backlash (play)
and positioning noise. Defaults follow the real box (docs/aiming.md): OV3660 at 320x240 (~62 deg wide) pitched ~45 deg
down, counter ~35 cm below the lens, laser pivot 12 cm above the lens, pan 72..128 / tilt 0..20 reaching the top half
of the picture, "bigger pan = dot moves left", "bigger tilt = dot moves up".

    rig = Rig()                                    # or Rig(h=0.42, lean_fwd=5) for a wearer standing differently
    px, ang = rig.calibrate(make_grid(...))         # simulated click calibration
    rig.land((pan, tilt))                           # where the dot lands (3D) for a command, through the servos

Box frame as in aim_model: metres, origin at the lens, X right, Y down, Z forward, Y = pan axis.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

DEG = math.pi / 180.0


def _rot(axis, a) -> np.ndarray:
    """Rotation matrix about a unit axis (Rodrigues)."""
    x, y, z = np.asarray(axis, float) / np.linalg.norm(axis)
    c, s, C = math.cos(a), math.sin(a), 1 - math.cos(a)
    return np.array([[c + x * x * C, x * y * C - z * s, x * z * C + y * s],
                     [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
                     [z * x * C - y * s, z * y * C + x * s, c + z * z * C]])


@dataclass
class Servo:
    """SG90-ish: mechanical angle = (cmd - zero) / gain + smooth nonlinearity, with backlash and noise."""
    zero: float = 100.0         # command at mechanical 0
    gain: float = -1.04         # command degrees per mechanical degree (sign = direction)
    nl_deg: float = 0.6         # amplitude of a smooth nonlinearity over the working range
    play_deg: float = 1.2       # backlash + deadband: output stays within +-play/2 of the static curve
    noise_deg: float = 0.15     # positioning repeatability (1 sigma)
    state: float | None = None  # play operator state (None = not moved yet)
    pos: float = 0.0            # actual mechanical angle after the last move (state + noise)

    def static(self, cmd) -> np.ndarray:
        cmd = np.asarray(cmd, float)
        return (cmd - self.zero) / self.gain + self.nl_deg * np.sin(math.pi * (cmd - 90.0) / 60.0)

    def move(self, cmd: float, rng: np.random.Generator | None = None) -> float:
        """Command the servo, return its mechanical angle. Play operator: the output only follows once the
        command has pulled it more than play/2 away (so the final angle depends on the approach direction)."""
        target = float(self.static(cmd))
        half = self.play_deg / 2
        if self.state is None:
            self.state = target
        elif target - self.state > half:
            self.state = target - half
        elif self.state - target > half:
            self.state = target + half
        noise = rng.normal(0, self.noise_deg) if (rng is not None and self.noise_deg) else 0.0
        self.pos = self.state + noise
        return self.pos


@dataclass
class Rig:
    w: int = 320
    h_px: int = 240
    hfov: float = 62.0              # deg
    pp: tuple[float, float] = (5.0, -4.0)      # principal point offset from the frame centre, px at 320 wide
    k1: float = -0.10               # forward radial distortion x_d = x_u (1 + k1 r^2 + k2 r^4)
    k2: float = 0.02
    pitch: float = 45.0             # camera pitched down in the box, deg
    roll: float = 1.5
    yaw: float = 2.0
    h: float = 0.35                 # counter distance from the lens along its normal, m
    lean_fwd: float = 3.0           # counter normal vs the pan axis, deg (wearer leaning)
    lean_side: float = 1.0
    laser: tuple[float, float, float] = (0.005, -0.12, 0.01)   # pan-tilt pivot relative to the lens, m
    tilt_off: float = 0.012         # tilt axis this far in front of the pan axis, m
    beam_off: float = 0.008         # beam this far above the tilt axis, m
    cone: float = 1.0               # beam vs tilt axis, deg off square
    skew: float = 0.8               # tilt axis vs pan axis, deg off square
    pan: Servo = field(default_factory=lambda: Servo(zero=100.0, gain=-1.04, nl_deg=0.6, play_deg=1.2))
    tilt: Servo = field(default_factory=lambda: Servo(zero=50.0, gain=0.95, nl_deg=0.5, play_deg=1.5))

    def __post_init__(self):
        self.scale(self.w)

    def scale(self, w: int) -> "Rig":
        """Same optics at another resolution (4:3 assumed when going through here)."""
        s = w / 320.0
        self.w, self.h_px = int(w), int(round(w * 3 / 4))
        self.f = (self.w / 2) / math.tan(self.hfov * DEG / 2)
        self.c = np.array([self.w / 2 + self.pp[0] * s, self.h_px / 2 + self.pp[1] * s])
        return self

    @property
    def frame(self) -> tuple[int, int]:
        return self.w, self.h_px

    # ── frames ───────────────────────────────────────────────────────────────
    @property
    def R(self) -> np.ndarray:      # camera -> box
        return _rot((0, 1, 0), self.yaw * DEG) @ _rot((1, 0, 0), -self.pitch * DEG) @ _rot((0, 0, 1), self.roll * DEG)

    @property
    def n(self) -> np.ndarray:      # counter normal, pointing down (away from the lens)
        return _rot((1, 0, 0), self.lean_fwd * DEG) @ _rot((0, 0, 1), self.lean_side * DEG) @ np.array([0, 1.0, 0])

    # ── camera ───────────────────────────────────────────────────────────────
    def project(self, pts) -> np.ndarray:
        c = np.asarray(pts, float).reshape(-1, 3) @ self.R
        xu, yu = c[:, 0] / c[:, 2], c[:, 1] / c[:, 2]
        r2 = xu * xu + yu * yu
        s = 1 + self.k1 * r2 + self.k2 * r2 * r2
        return np.stack([self.c[0] + self.f * xu * s, self.c[1] + self.f * yu * s], axis=-1)

    def ray(self, px) -> np.ndarray:
        """Unit camera rays (box frame) for pixels (inverts the distortion numerically)."""
        px = np.asarray(px, float).reshape(-1, 2)
        xd, yd = (px[:, 0] - self.c[0]) / self.f, (px[:, 1] - self.c[1]) / self.f
        xu, yu = xd.copy(), yd.copy()
        for _ in range(30):
            r2 = xu * xu + yu * yu
            s = 1 + self.k1 * r2 + self.k2 * r2 * r2
            xu, yu = xd / s, yd / s
        r = np.stack([xu, yu, np.ones_like(xu)], -1) @ self.R.T
        return r / np.linalg.norm(r, axis=1, keepdims=True)

    def on_counter(self, px, height: float = 0.0) -> np.ndarray:
        """3D point where each pixel's ray meets the counter (raised by height)."""
        r = self.ray(px)
        return r * ((self.h - height) / (r @ self.n))[:, None]

    def in_frame(self, px, margin: float = 0.0) -> np.ndarray:
        px = np.asarray(px, float).reshape(-1, 2)
        return ((px[:, 0] >= margin) & (px[:, 0] <= self.w - 1 - margin) &
                (px[:, 1] >= margin) & (px[:, 1] <= self.h_px - 1 - margin))

    # ── laser ────────────────────────────────────────────────────────────────
    def beam(self, alpha_deg: float, beta_deg: float) -> tuple[np.ndarray, np.ndarray]:
        """Beam origin + unit direction (box frame) for MECHANICAL angles (alpha right, beta up)."""
        axis = _rot((0, 0, 1), self.skew * DEG) @ np.array([1.0, 0, 0])
        Rt = _rot(axis, beta_deg * DEG)
        T = np.array([0, 0, self.tilt_off])
        o = T + Rt @ np.array([0, -self.beam_off, 0])
        d = Rt @ np.array([math.sin(self.cone * DEG), 0, math.cos(self.cone * DEG)])
        Rp = _rot((0, 1, 0), alpha_deg * DEG)
        return np.asarray(self.laser) + Rp @ o, Rp @ d

    def hit(self, o, d, height: float = 0.0) -> np.ndarray:
        t = (self.h - height - o @ self.n) / (d @ self.n)
        return o + t * d

    def mech(self, cmd, rng=None, move: bool = True) -> tuple[float, float]:
        """Mechanical angles for a command. move=True goes through the servos (backlash, noise)."""
        if move:
            return self.pan.move(cmd[0], rng), self.tilt.move(cmd[1], rng)
        return float(self.pan.static(cmd[0])), float(self.tilt.static(cmd[1]))

    def land(self, cmd, rng=None, move: bool = True, height: float = 0.0) -> np.ndarray:
        """Where the dot lands on the counter (raised by height) for a pan/tilt command."""
        return self.hit(*self.beam(*self.mech(cmd, rng, move)), height)

    def dot_px(self, cmd, rng=None, move: bool = True) -> np.ndarray:
        return self.project(self.land(cmd, rng, move))[0]

    def approach(self, cmd, via_deg: float = 3.0, rng=None, lo=(0.0, 0.0)) -> tuple[float, float]:
        """Move like the Aimer/calibration do: first to cmd - via_deg on each axis, then up to cmd."""
        if via_deg:
            self.mech((max(cmd[0] - via_deg, lo[0]), max(cmd[1] - via_deg, lo[1])), rng)
        return self.mech(cmd, rng)

    def ideal_cmd(self, pt, guess=(100.0, 10.0)) -> np.ndarray:
        """Command whose static (no backlash) beam passes through a 3D point, by Newton iteration."""
        c = np.array(guess, float)
        for _ in range(30):
            def err(cc):
                o, d = self.beam(*self.mech(cc, move=False))
                v = pt - o
                v = v / np.linalg.norm(v)
                # angular error in (horizontal, vertical) as seen along the beam
                return np.array([math.atan2(v[0], v[2]) - math.atan2(d[0], d[2]),
                                 math.asin(-v[1]) - math.asin(-d[1])]) / DEG
            e = err(c)
            if np.max(np.abs(e)) < 1e-7:
                break
            J = np.empty((2, 2))
            for j in range(2):
                dc = c.copy()
                dc[j] += 1e-4
                J[:, j] = (err(dc) - e) / 1e-4
            c = c - np.linalg.solve(J, e)
        return c

    def reset_servos(self, cmd=(90.0, 60.0)) -> None:
        """Power-on: the servos snap to a command (the S3 starts at pan 90, tilt 60)."""
        self.pan.state, self.tilt.state = None, None
        self.mech(cmd)

    # ── calibration ──────────────────────────────────────────────────────────
    def calibrate(self, grid, rng=None, click_px: float = 1.2, via_deg: float = 0.0, lo=(0.0, 0.0),
                  start=(90.0, 60.0)) -> tuple[np.ndarray, np.ndarray]:
        """Simulated click calibration over grid (list of (pan, tilt)), visited in order. Points whose dot is
        off-screen are skipped. via_deg > 0 approaches every point from below (as the new calibrate does)."""
        rng = rng if rng is not None else np.random.default_rng(0)
        self.reset_servos(start)
        pix, ang = [], []
        for cmd in grid:
            self.approach(cmd, via_deg, rng, lo) if via_deg else self.mech(cmd, rng)
            p = self.project(self.hit(*self.beam(self.pan.pos, self.tilt.pos)))[0]
            if not self.in_frame(p, 2):
                continue
            pix.append(p + rng.normal(0, click_px, 2))
            ang.append(cmd)
        return np.asarray(pix), np.asarray(ang, float)

    def targets(self, pan=(72.0, 128.0), tilt=(0.0, 20.0), n: int = 400, rng=None) -> np.ndarray:
        """Random counter points the laser can reach (uniform over the command range), as (n, 3)."""
        rng = rng if rng is not None else np.random.default_rng(1)
        pts = []
        while len(pts) < n:
            c = (rng.uniform(*pan), rng.uniform(*tilt))
            p = self.land(c, move=False)
            if self.in_frame(self.project(p), 2):
                pts.append(p)
        return np.asarray(pts)


def aim_error(rig: Rig, pts, cmds, rng=None, via_deg: float = 3.0, lo=(0.0, 0.0), height: float = 0.0,
              start=None) -> tuple[np.ndarray, np.ndarray]:
    """Send each command through the servos and measure where the dot lands vs the target point.

    Returns (angular error at the laser in deg, distance on the counter in cm) per target. Targets are visited in
    a random order from wherever the previous one left the servos, so backlash shows up unless via_deg > 0.
    """
    rng = rng if rng is not None else np.random.default_rng(2)
    if start is not None:
        rig.reset_servos(start)
    deg, cm = [], []
    for p, c in zip(np.asarray(pts, float), np.asarray(cmds, float)):
        rig.approach(c, via_deg, rng, lo) if via_deg else rig.mech(c, rng)
        o, d = rig.beam(rig.pan.pos, rig.tilt.pos)
        landed = rig.hit(o, d, height)
        v = p - o
        cosang = float(np.clip(v @ d / np.linalg.norm(v), -1, 1))
        deg.append(math.acos(cosang) / DEG)
        cm.append(float(np.linalg.norm(landed - p)) * 100)
    return np.asarray(deg), np.asarray(cm)
