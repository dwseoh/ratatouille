"""Pixel -> servo-angle models for the laser. aim.Calibration picks one; docs/aiming.md has the error budget.

PolyModel   the original v1 model: pan, tilt = 2nd-order polynomial in the pixel (x, y), 6 terms per axis, linear
            least squares. Exact only if the real mapping is quadratic. Fine inside the clicked grid, drifts outside
            it (it has no idea why the mapping curves).

PlaneModel  physical: a pinhole camera (focal length, principal point, one radial-distortion term) looks at a flat
            counter; the laser's pan-tilt pivot sits at an offset L from the lens (~12 cm above it). For a pixel:
                camera ray -> hits the counter plane -> 3D point -> direction from the laser pivot
                -> pan-tilt inverse kinematics (pan about the box's vertical axis, tilt, a small beam cone angle)
                -> servo command = offset + gain * mechanical angle, per servo.
            For points on the calibrated plane this is exact anywhere in the picture (pixel -> plane is a
            homography; the rest is geometry), so it extrapolates where the polynomial can't. It also knows each
            point's distance, so errors can be reported in cm, and it can aim at a point ABOVE the counter
            (the middle of a fruit) instead of the counter behind it.

            Fitted by Levenberg-Marquardt (numpy, no scipy) with weak priors on the physical parameters: one plane
            of data can't pin down every parameter (scale, focal length vs distance trade off), so the priors pick
            the physically plausible solution. The predictions on the calibrated plane don't depend on that choice.

Box frame (metres): origin at the camera lens, X right, Y DOWN, Z forward (same handedness as the camera frame).
Y is the pan axis. The laser pivot is at (lx, ly, lz); 12 cm above the lens is ly = -0.12.
"""
from __future__ import annotations

import math

import numpy as np

DEG = math.pi / 180.0

# ═══════════════════════════════════════════════════════════════════════════════
# Polynomial (v1)
# ═══════════════════════════════════════════════════════════════════════════════

POLY_TERMS = ("1", "x", "y", "x2", "xy", "y2")


def poly_features(px) -> np.ndarray:
    x, y = np.asarray(px, dtype=float).reshape(-1, 2).T
    return np.stack([np.ones_like(x), x, y, x * x, x * y, y * y], axis=-1)


class PolyModel:
    kind = "poly"
    n_params = len(POLY_TERMS)

    def __init__(self, coef=None):
        self.coef = None if coef is None else np.asarray(coef, dtype=float)    # (6, 2): column 0 pan, 1 tilt

    def fit(self, pixels, angles, weights=None) -> "PolyModel":
        pixels, angles = np.asarray(pixels, float).reshape(-1, 2), np.asarray(angles, float).reshape(-1, 2)
        if len(pixels) < self.n_params:
            raise ValueError(f"need at least {self.n_params} points to fit, got {len(pixels)}")
        sw = np.ones(len(pixels)) if weights is None else np.sqrt(np.asarray(weights, float))
        self.coef, *_ = np.linalg.lstsq(poly_features(pixels) * sw[:, None], angles * sw[:, None], rcond=None)
        return self

    def jacobian(self, px) -> np.ndarray:
        """d(pan, tilt)/d(x, y) at one pixel, (2, 2)."""
        x, y = float(px[0]), float(px[1])
        dx = np.array([0, 1, 0, 2 * x, y, 0.0]) @ self.coef
        dy = np.array([0, 0, 1, 0, x, 2 * y]) @ self.coef
        return np.stack([dx, dy], axis=1)

    def invert(self, angles, guess) -> np.ndarray:
        """Pixel whose prediction is `angles` (Newton from `guess`). NaN if it doesn't converge."""
        p, a = np.asarray(guess, float).copy(), np.asarray(angles, float)
        for _ in range(30):
            e = self.predict(p)[0] - a
            if np.max(np.abs(e)) < 1e-6:
                return p
            try:
                p = p - np.linalg.solve(self.jacobian(p), e)
            except np.linalg.LinAlgError:
                break
        return p if np.max(np.abs(self.predict(p)[0] - a)) < 1e-3 else np.full(2, np.nan)

    def predict(self, pixels) -> np.ndarray:
        return poly_features(pixels) @ self.coef


# ═══════════════════════════════════════════════════════════════════════════════
# Physical model: camera + counter plane + pan-tilt
# ═══════════════════════════════════════════════════════════════════════════════

# Parameter order. Angles in degrees, lengths in metres, f/cx/cy in pixels of the calibration frame.
PLANE_PARAMS = ("f", "cx", "cy", "k1",            # camera: focal, principal point, radial (undistortion) term
                "pitch", "roll",                  # camera orientation in the box: pitched down, rolled
                "lean_fwd", "lean_side", "h",     # counter plane: normal tilt vs the pan axis, distance from the lens
                "lx", "ly", "lz",                 # laser pivot relative to the lens (box frame)
                "pan0", "pan_gain", "tilt0", "tilt_gain",   # servo command = offset + gain * mechanical angle
                "cone")                           # beam not quite square to the tilt axis
LOG_PARAMS = {"f", "h"}                           # positive: fitted (and given priors) in log space

HFOV_DEG = 64.0           # OV3660 stock lens, horizontal (spec says ~66 diagonal-ish; only the prior's centre)
BASELINE_M = 0.12         # laser pivot above the lens (measured on the box)
SIGMA_DATA_DEG = 0.5      # expected calibration noise per point (click + servo slop), weights data vs priors
MISS_PENALTY = 200.0      # fit residual for a point whose camera ray doesn't reach the plane


def _rx(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], float)


def _rz(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], float)


class PlaneModel:
    """pixel -> (pan, tilt) through camera ray, counter plane and pan-tilt kinematics. See the module docstring."""

    kind = "plane"
    n_params = len(PLANE_PARAMS)

    def __init__(self, params: dict | None = None):
        self.params: dict[str, float] = {}
        if params is not None:
            self.set(params)

    def set(self, params: dict) -> "PlaneModel":
        p = {k: float(params[k]) for k in PLANE_PARAMS}
        self.params = p
        # camera -> box: pitch the optical axis down, after rolling about it. Y is down, so down-forward = +Y.
        self._R = _rx(-p["pitch"] * DEG) @ _rz(p["roll"] * DEG)
        self._n = _rx(p["lean_fwd"] * DEG) @ _rz(p["lean_side"] * DEG) @ np.array([0.0, 1.0, 0.0])  # plane normal, down
        self._L = np.array([p["lx"], p["ly"], p["lz"]])
        return self

    def as_vector(self) -> np.ndarray:
        return np.array([self.params[k] for k in PLANE_PARAMS])

    @classmethod
    def from_vector(cls, v) -> "PlaneModel":
        return cls(dict(zip(PLANE_PARAMS, map(float, v))))

    # ── geometry ─────────────────────────────────────────────────────────────
    def rays(self, pixels) -> np.ndarray:
        """Camera rays (box frame, not unit length) through pixels."""
        p = self.params
        px = np.asarray(pixels, float).reshape(-1, 2)
        xn, yn = (px[:, 0] - p["cx"]) / p["f"], (px[:, 1] - p["cy"]) / p["f"]
        s = 1.0 + p["k1"] * (xn * xn + yn * yn)
        rc = np.stack([xn * s, yn * s, np.ones_like(xn)], axis=-1)
        return rc @ self._R.T

    def points(self, pixels, height: float | np.ndarray = 0.0) -> np.ndarray:
        """Where each pixel's ray meets the counter plane raised by `height` metres (box frame).

        Rays that never come down to the plane (above the horizon) give NaN.
        """
        r = self.rays(pixels)
        denom = r @ self._n
        t = (self.params["h"] - np.asarray(height, float)) / np.where(denom > 1e-3, denom, np.nan)
        return r * t[:, None]

    def angles_for_points(self, pts) -> np.ndarray:
        """Servo commands (pan, tilt) that send the beam from the laser pivot through 3D points (box frame)."""
        p = self.params
        d = np.asarray(pts, float).reshape(-1, 3) - self._L
        d = d / np.linalg.norm(d, axis=1, keepdims=True)
        g = p["cone"] * DEG
        cg, sg = math.cos(g), math.sin(g)
        beta = np.arcsin(np.clip(-d[:, 1] / cg, -1.0, 1.0))
        alpha = np.arctan2(d[:, 0], d[:, 2]) - np.arctan2(sg, cg * np.cos(beta))
        return np.stack([p["pan0"] + p["pan_gain"] * alpha / DEG, p["tilt0"] + p["tilt_gain"] * beta / DEG], axis=-1)

    def predict(self, pixels, height: float | np.ndarray = 0.0) -> np.ndarray:
        return self.angles_for_points(self.points(pixels, height))

    def beams(self, angles) -> np.ndarray:
        """Unit beam directions (box frame) for servo commands."""
        p = self.params
        a = np.asarray(angles, float).reshape(-1, 2)
        alpha = (a[:, 0] - p["pan0"]) / p["pan_gain"] * DEG
        beta = (a[:, 1] - p["tilt0"]) / p["tilt_gain"] * DEG
        g = p["cone"] * DEG
        u = np.stack([np.full_like(beta, math.sin(g)), -math.cos(g) * np.sin(beta), math.cos(g) * np.cos(beta)], -1)
        ca, sa = np.cos(alpha), np.sin(alpha)
        return np.stack([ca * u[:, 0] + sa * u[:, 2], u[:, 1], -sa * u[:, 0] + ca * u[:, 2]], axis=-1)

    def landing_points(self, angles) -> tuple[np.ndarray, np.ndarray]:
        """Where the beam meets the counter plane for servo commands: (points, hits_plane mask)."""
        d = self.beams(angles)
        denom = d @ self._n
        ok = denom > 1e-3
        t = (self.params["h"] - self._L @ self._n) / np.where(ok, denom, 1e-3)
        return self._L + d * t[:, None], ok & (t > 0)

    def project(self, pts) -> np.ndarray:
        """Box-frame points -> pixels (distortion inverted by fixed-point iteration)."""
        p = self.params
        c = np.asarray(pts, float).reshape(-1, 3) @ self._R       # box -> camera (R is orthonormal)
        z = np.where(c[:, 2] > 1e-6, c[:, 2], 1e-6)
        xu, yu = c[:, 0] / z, c[:, 1] / z
        xd, yd = xu.copy(), yu.copy()
        for _ in range(20):
            s = 1.0 + p["k1"] * (xd * xd + yd * yd)
            xd, yd = xu / s, yu / s
        return np.stack([p["cx"] + p["f"] * xd, p["cy"] + p["f"] * yd], axis=-1)

    def pixels_for(self, angles) -> np.ndarray:
        """Where the dot shows up in the picture for servo commands (on the calibrated plane)."""
        pts, _ = self.landing_points(angles)
        return self.project(pts)

    def distance(self, pixels, height: float = 0.0) -> np.ndarray:
        """Laser pivot -> point distance (m) for pixels, for converting degrees to cm."""
        return np.linalg.norm(self.points(pixels, height) - self._L, axis=1)

    def summary(self) -> str:
        p = self.params
        cam_up = -p["ly"] * 100
        return (f"camera: f {p['f']:.0f}px ({2 * math.atan(p['cx'] / p['f']) / DEG:.0f} deg wide), pitched "
                f"{p['pitch']:.0f} deg down, k1 {p['k1']:+.2f} | counter {p['h'] * 100:.0f} cm from the lens | "
                f"laser {cam_up:.0f} cm above, {p['lx'] * 100:+.1f} cm right, {p['lz'] * 100:+.1f} cm fwd | "
                f"pan = {p['pan0']:.1f} {p['pan_gain']:+.2f}*a, tilt = {p['tilt0']:.1f} {p['tilt_gain']:+.2f}*b, "
                f"cone {p['cone']:+.1f}")


# ── fitting ──────────────────────────────────────────────────────────────────

def _lm(fun, q0: np.ndarray, iters: int = 150, tol: float = 1e-9) -> tuple[np.ndarray, float]:
    """Levenberg-Marquardt on a residual function of a (roughly unit-scaled) parameter vector."""
    q = np.asarray(q0, float).copy()
    r = fun(q)
    cost = float(r @ r)
    lam, eps = 1e-3, 1e-6
    eye = np.eye(len(q))
    for _ in range(iters):
        J = np.empty((len(r), len(q)))
        for j in range(len(q)):
            dq = q.copy()
            dq[j] += eps
            J[:, j] = (fun(dq) - r) / eps
        A, g = J.T @ J, J.T @ r
        improved = False
        while lam < 1e10:
            try:
                step = np.linalg.solve(A + lam * (np.diag(np.diag(A)) + 1e-9 * eye), -g)
            except np.linalg.LinAlgError:
                lam *= 10
                continue
            r2 = fun(q + step)
            c2 = float(r2 @ r2)
            if np.isfinite(c2) and c2 < cost:
                q, done = q + step, cost - c2 < tol * (1.0 + cost)
                r, cost, lam, improved = r2, c2, max(lam / 3.0, 1e-12), True
                break
            lam *= 4.0
        if not improved or done:
            break
    return q, cost


class _Priors:
    """Gaussian priors per parameter (mean, sigma) in the fit space (log for f and h). Also the unit scaling."""

    def __init__(self, means: dict, sigmas: dict):
        self.mu = np.array([math.log(means[k]) if k in LOG_PARAMS else means[k] for k in PLANE_PARAMS])
        self.sd = np.array([sigmas[k] for k in PLANE_PARAMS], float)

    def to_params(self, q) -> np.ndarray:
        v = self.mu + self.sd * q
        for i, k in enumerate(PLANE_PARAMS):
            if k in LOG_PARAMS:
                v[i] = math.exp(min(v[i], 50.0))
        return v

    def to_q(self, v) -> np.ndarray:
        w = np.array([math.log(x) if k in LOG_PARAMS else x for k, x in zip(PLANE_PARAMS, v)])
        return (w - self.mu) / self.sd


def _priors(pixels, angles, frame, baseline: float, hfov_deg: float, roll: float, pan_sign: float,
            tilt_sign: float) -> _Priors:
    px = np.asarray(pixels, float)
    if frame is not None:
        w, h = float(frame[0]), float(frame[1])
        cx, cy, c_sd = w / 2, h / 2, 0.04 * w
        f0, f_sd = (w / 2) / math.tan(hfov_deg * DEG / 2), 0.3
    else:   # no frame size: centre of the clicks, focal from how far the dot moved per degree of pan
        lo, hi = px.min(axis=0), px.max(axis=0)
        cx, cy = (lo + hi) / 2
        c_sd = max(hi[0] - lo[0], 1.0)
        span = np.ptp(np.asarray(angles, float)[:, 0]) * DEG
        f0, f_sd = (max(hi[0] - lo[0], 1.0) / max(span, 1e-3)), 0.6
    means = dict(f=f0, cx=cx, cy=cy, k1=0.0, pitch=40.0, roll=roll, lean_fwd=0.0, lean_side=0.0, h=0.45,
                 lx=0.0, ly=-baseline, lz=0.0, pan0=90.0, pan_gain=pan_sign, tilt0=60.0, tilt_gain=tilt_sign,
                 cone=0.0)
    sigmas = dict(f=f_sd, cx=c_sd, cy=c_sd, k1=0.1, pitch=20.0, roll=10.0, lean_fwd=20.0, lean_side=10.0, h=0.7,
                  lx=0.03, ly=0.012, lz=0.03, pan0=90.0, pan_gain=0.12, tilt0=90.0, tilt_gain=0.12, cone=3.0)
    return _Priors(means, sigmas)


def _signs(pixels, angles) -> tuple[float, float]:
    """Servo directions from the data: does the dot move right with more pan, up with more tilt?"""
    px, a = np.asarray(pixels, float), np.asarray(angles, float)
    A = np.c_[a, np.ones(len(a))]
    coef, *_ = np.linalg.lstsq(A, px, rcond=None)         # pixel ~ pan, tilt (linear)
    dx_dpan, dy_dtilt = coef[0, 0], coef[1, 1]
    return (1.0 if dx_dpan >= 0 else -1.0), (1.0 if dy_dtilt <= 0 else -1.0)


def fit_plane(pixels, angles, frame=None, baseline: float = BASELINE_M, hfov_deg: float = HFOV_DEG,
              start: PlaneModel | None = None, sigma_deg: float = SIGMA_DATA_DEG,
              weights=None) -> tuple[PlaneModel, float]:
    """Fit a PlaneModel to calibration points. Returns (model, rms angle error over the points, deg).

    `start`: a previous fit to start from (one LM run, used by leave-one-out and reweighting); otherwise a few
    starts over the camera pitch and roll (upright or upside down); the lowest cost (data + priors) wins.
    `weights`: per point, 0..1 (robust reweighting of bad clicks).
    """
    px, ang = np.asarray(pixels, float).reshape(-1, 2), np.asarray(angles, float).reshape(-1, 2)
    if len(px) < 6:
        raise ValueError(f"need at least 6 points to fit, got {len(px)}")
    sw = np.ones(len(px)) if weights is None else np.sqrt(np.asarray(weights, float))
    sw2 = np.repeat(sw, 2)
    ps, ts = _signs(px, ang)
    best = None
    rolls = [(0.0, ps, ts), (180.0, -ps, -ts)]
    if start is not None:
        sp = start.params
        rolls = [(sp["roll"], math.copysign(1.0, sp["pan_gain"]), math.copysign(1.0, sp["tilt_gain"]))]
    for roll, sgn_p, sgn_t in rolls:
        pri = _priors(px, ang, frame, baseline, hfov_deg, roll, sgn_p, sgn_t)

        def resid(q, pri=pri):
            m = PlaneModel.from_vector(pri.to_params(q))
            d = (m.predict(px) - ang).ravel() / sigma_deg
            d = np.where(np.isfinite(d), d, MISS_PENALTY) * sw2   # a dot whose ray misses the plane: wrong geometry
            return np.concatenate([d, q])          # q = (param - prior mean) / sigma: the prior residuals

        if start is not None:
            inits = [start.as_vector()]
        else:
            inits = []
            for pitch in (20.0, 40.0, 60.0):
                v = pri.to_params(np.zeros(len(PLANE_PARAMS)))
                v[PLANE_PARAMS.index("pitch")] = pitch
                m = PlaneModel.from_vector(v)
                # servo offsets: whatever makes the mean error zero for this geometry
                with np.errstate(invalid="ignore"):
                    off = np.nanmean(ang - m.predict(px), axis=0)
                if not np.all(np.isfinite(off)):
                    continue
                v[PLANE_PARAMS.index("pan0")] += off[0]
                v[PLANE_PARAMS.index("tilt0")] += off[1]
                inits.append(v)
        for v0 in inits:
            q, cost = _lm(resid, pri.to_q(v0))
            m = PlaneModel.from_vector(pri.to_params(q))
            data = rms(m.predict(px) - ang)
            if np.isfinite(data) and np.isfinite(cost) and (best is None or cost < best[2]):
                best = (m, data, cost)
    if best is None or not np.isfinite(best[1]):
        raise ValueError("plane model fit failed")
    return best[0], best[1]


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-validation
# ═══════════════════════════════════════════════════════════════════════════════

def loo_errors(kind: str, pixels, angles, frame=None, baseline: float = BASELINE_M,
               full: PlaneModel | None = None, weights=None, hfov_deg: float = HFOV_DEG) -> np.ndarray:
    """Leave-one-out: refit without each point, predict it. (N, 2) errors (pred - real), degrees."""
    px, ang = np.asarray(pixels, float).reshape(-1, 2), np.asarray(angles, float).reshape(-1, 2)
    w = np.ones(len(px)) if weights is None else np.asarray(weights, float)
    out = np.full_like(ang, np.nan)
    for i in range(len(px)):
        keep = np.arange(len(px)) != i
        try:
            if kind == "poly":
                m = PolyModel().fit(px[keep], ang[keep], w[keep])
            else:
                m, _ = fit_plane(px[keep], ang[keep], frame, baseline, hfov_deg, start=full, weights=w[keep])
            out[i] = m.predict(px[i:i + 1])[0] - ang[i]
        except (ValueError, np.linalg.LinAlgError):
            pass
    return out


def robust_weights(err, floor_deg: float = 0.25, k: float = 2.5) -> np.ndarray:
    """Huber weights per point from 2D errors: 1 up to k robust sigmas, then k*sigma/|err| (a bad click counts less)."""
    r = np.linalg.norm(np.asarray(err, float).reshape(-1, 2), axis=1)
    r = np.where(np.isfinite(r), r, 1e6)
    s = max(1.4826 * float(np.median(r)), floor_deg)
    return np.minimum(1.0, k * s / np.maximum(r, 1e-12))


def loo_score(err, cap_deg: float = 3.0) -> float:
    """RMS of leave-one-out errors with each point capped (one bad click shouldn't decide the model)."""
    r = np.linalg.norm(np.asarray(err, float).reshape(-1, 2), axis=1)
    r = np.where(np.isfinite(r), np.minimum(r, cap_deg), cap_deg)
    return float(np.sqrt(np.mean(r * r))) if len(r) else float("inf")


def rms(err) -> float:
    """RMS of 2D errors; any non-finite row (a point the model can't map) makes it inf."""
    e = np.asarray(err, float).reshape(-1, 2)
    if not len(e) or not np.all(np.isfinite(e)):
        return float("inf")
    return float(np.sqrt(np.mean(np.sum(e * e, axis=1))))
