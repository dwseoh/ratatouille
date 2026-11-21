"""Perception: YOLO-World object boxes (B) + red laser dot (A).

Detector side (B, P3):
- `Detector`: YOLO-World with the recipe objects + "hand" as classes, per-class confidence thresholds.
- `Smoother`: an object counts as visible if seen in 2 of the last 3 frames; reports its newest boxes.
- `DetectorThread`: runs the detector in a worker thread on `frames.latest()` only (never a backlog),
  as fast as it goes, and hands results to the asyncio loop with call_soon_threadsafe. It is the only
  writer of State.dets. Boxes stay in camera-frame pixels (the aimer uses `State.det(name).center`).
Measured on an M2 (mps): ~50 fps at imgsz 640, ~70 fps at 480. Try it: python -m tools.try_yolo 0
"""
import threading
import time
from collections import deque

import cv2
import numpy as np
from loguru import logger

from .contracts import Det  # shared detection type (name, conf, box, center)

HAND = "hand"                       # extra YOLO class for the safety check, not a recipe object
DEFAULT_CONF = 0.15
CLASS_CONF = {HAND: 0.25}           # raise the classes that false-trigger; tune on the real counter
STALE_S = 1.0                       # no new camera frame for this long -> State.dets = []


class Detector:
    def __init__(self, weights: str, device: str, classes: list[str], *, imgsz: int = 640,
                 conf: dict[str, float] | None = None):
        from ultralytics import YOLOWorld  # heavy import, keep lazy
        self.classes = list(dict.fromkeys([*classes, HAND]))
        self.model = YOLOWorld(weights)
        self.model.set_classes(self.classes)
        self.device, self.imgsz = device, imgsz
        self.conf = {**CLASS_CONF, **(conf or {})}
        self._floor = min(DEFAULT_CONF, *self.conf.values())   # model-level cut, per-class after

    def threshold(self, name: str) -> float:
        return self.conf.get(name, DEFAULT_CONF)

    def detect(self, frame) -> list[Det]:
        r = self.model.predict(frame, device=self.device, imgsz=self.imgsz, conf=self._floor, verbose=False)[0]
        names = r.names
        dets = [Det(names[int(c)], float(p), tuple(map(float, b)))
                for b, c, p in zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist(), r.boxes.conf.tolist())]
        return [d for d in dets if d.conf >= self.threshold(d.name)]


class Smoother:
    """Visible = seen in >= k of the last n frames. Returns the boxes from the newest frame it was seen in,
    so one missed frame doesn't make the laser drop its target, and one false hit doesn't show up."""

    def __init__(self, n: int = 3, k: int = 2):
        self.k = k
        self._hist: deque[dict[str, list[Det]]] = deque(maxlen=n)

    def update(self, dets: list[Det]) -> list[Det]:
        frame: dict[str, list[Det]] = {}
        for d in dets:
            frame.setdefault(d.name, []).append(d)
        self._hist.append(frame)
        out: list[Det] = []
        for name in {n for f in self._hist for n in f}:
            seen = [f[name] for f in self._hist if name in f]
            if len(seen) >= self.k:
                out.extend(seen[-1])
        return out

    def clear(self) -> None:
        self._hist.clear()


class DetectorThread:
    """Runs a Detector on the newest frame in a thread; the only writer of State.dets (on the loop)."""

    def __init__(self, detector, frames, state, loop, smoother: Smoother | None = None):
        self.detector, self.frames, self.state, self.loop = detector, frames, state, loop
        self.smoother = smoother or Smoother()
        self.fps = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "DetectorThread":
        self._thread = threading.Thread(target=self._run, name="yolo", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _apply(self, dets: list[Det]) -> None:          # runs on the asyncio loop
        self.state.dets = dets

    def _publish(self, dets: list[Det]) -> None:
        try:
            self.loop.call_soon_threadsafe(self._apply, dets)
        except RuntimeError:                            # loop closed during shutdown
            self._stop.set()

    def _run(self) -> None:
        last_t, last_new = None, time.monotonic()
        while not self._stop.is_set():
            frame, t = self.frames.latest()
            if frame is None or t == last_t:
                if self.state.dets and time.monotonic() - last_new > STALE_S:
                    self.smoother.clear()
                    self._publish([])                   # camera gone: don't aim at old boxes
                time.sleep(0.005)
                continue
            last_t, last_new = t, time.monotonic()
            t0 = time.perf_counter()
            try:
                dets = self.detector.detect(frame)
            except Exception:
                logger.exception("YOLO detect failed")
                time.sleep(0.5)
                continue
            self._publish(self.smoother.update(dets))
            dt = time.perf_counter() - t0
            self.fps = 1 / dt if not self.fps else 0.9 * self.fps + 0.1 / dt


# ── Laser dot (A, stretch). Everything below is dw127's; the blink check + State.dot writer is in dot.py. ──
# A laser dot = a small blown-out spot (the sensor saturates) that is either near-white with a red halo
# around it, or saturated red and much brighter than what's around it. Thresholds are for a 640-wide frame;
# tune under venue light.
DOT_HOT_V = 235         # "hot" pixel: brightest channel (HSV V) at least this
DOT_MIN_AREA = 2        # hot blob size limits (px). Bigger = lamp, window, white board, bright tomato
DOT_MAX_AREA = 600
DOT_RING_PX = 3         # halo = the ring this wide just outside the hot blob
DOT_WHITE = 200         # white-core rule: some core pixels have ALL channels >= this (blown out) ...
DOT_RED_FRAC = 0.3      # ... and at least this fraction of the ring is red
DOT_RED_EXCESS = 60     # red-core rule: median R - max(G, B) over the core >= this ...
DOT_PEAK = 50           # ... and the core is this much brighter (V) than the ring
DOT_RED_HUE = 10        # red = hue <= this or >= 180 - this (OpenCV hue 0..179) ...
DOT_RED_S, DOT_RED_V = 70, 90   # ... with at least this saturation and brightness,
DOT_RING_EXCESS = 30    # or R - max(G, B) >= this (red glow on a coloured surface shifts the hue)
DOT_MAX_CANDS = 40      # hot blobs examined per frame (keeps a sparkly scene fast)
DOT_MAX_W = 1280        # wider frames are downscaled first
_RING_K = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * DOT_RING_PX + 1,) * 2)


def _red(hsv, bgr):
    """Boolean mask of red-ish pixels: two HSV hue bands (red wraps around hue 0), or R clearly dominant."""
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    band = ((h <= DOT_RED_HUE) | (h >= 180 - DOT_RED_HUE)) & (s >= DOT_RED_S) & (v >= DOT_RED_V)
    c = bgr.astype(np.int16)
    return band | (c[..., 2] - np.maximum(c[..., 0], c[..., 1]) >= DOT_RING_EXCESS)


def _dot_score(frame, hsv, labels, i, x, y, w, h):
    """How dot-like hot blob i is: ring red fraction (> 0 = accepted), or None if it's not a laser dot."""
    pad = DOT_RING_PX + 1
    H, W = labels.shape
    x0, y0, x1, y1 = max(x - pad, 0), max(y - pad, 0), min(x + w + pad, W), min(y + h + pad, H)
    lab, bgr, hsv_w = labels[y0:y1, x0:x1], frame[y0:y1, x0:x1], hsv[y0:y1, x0:x1]
    core = lab == i
    ring = cv2.dilate(core.view(np.uint8), _RING_K).view(bool) & (lab == 0)   # just outside, not hot
    if not ring.any():
        return None
    red_frac = float(_red(hsv_w[ring], bgr[ring]).mean())
    c = bgr[core].astype(np.int16)  # (n, 3) BGR
    if (c.min(axis=1) >= DOT_WHITE).any() and red_frac >= DOT_RED_FRAC:
        return red_frac             # white core + red halo
    excess = np.median(c[:, 2] - np.maximum(c[:, 0], c[:, 1]))
    peak = float(hsv_w[..., 2][core].mean()) - float(hsv_w[..., 2][ring].mean())
    if excess >= DOT_RED_EXCESS and peak >= DOT_PEAK:
        return max(red_frac, 0.01)  # saturated red spot, much brighter than its surroundings
    return None


def find_red_dot(frame, last=None):
    """Return (x, y) of the laser dot in a BGR frame, or None. `last` = previous dot: prefer the blob nearest it.

    Gates: hot (V >= DOT_HOT_V), small (DOT_MIN_AREA..DOT_MAX_AREA px), and either a blown-out white core with a
    red halo, or a saturated red core much brighter than its ring. A red tomato is big and not blown out, so it
    fails. A white specular highlight ON red food can still pass: that's what the blink check (dot.py) is for.
    ~1 ms at 640x480.
    """
    if frame is None:
        return None
    scale = 1.0
    if frame.shape[1] > DOT_MAX_W:
        scale = DOT_MAX_W / frame.shape[1]
        frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    hot = cv2.threshold(hsv[..., 2], DOT_HOT_V - 1, 255, cv2.THRESH_BINARY)[1]
    n, labels, stats, cents = cv2.connectedComponentsWithStats(hot, connectivity=8)
    idx = [i for i in range(1, n) if DOT_MIN_AREA <= stats[i, cv2.CC_STAT_AREA] <= DOT_MAX_AREA]
    if not idx:
        return None
    near = None if last is None else np.asarray(last, float) * scale
    if near is not None:
        idx.sort(key=lambda i: np.hypot(*(cents[i] - near)))
    elif len(idx) > DOT_MAX_CANDS:
        idx.sort(key=lambda i: -stats[i, cv2.CC_STAT_AREA])
    best, best_key = None, None
    for i in idx[:DOT_MAX_CANDS]:
        x, y, w, h = stats[i, :4]
        score = _dot_score(frame, hsv, labels, i, int(x), int(y), int(w), int(h))
        if score is None:
            continue
        if near is not None:        # sorted by distance: the first accepted one is the nearest
            best = i
            break
        if best_key is None or score > best_key:
            best, best_key = i, score
    if best is None:
        return None
    cx, cy = cents[best]
    return float(cx / scale), float(cy / scale)
