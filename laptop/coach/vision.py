"""Perception: YOLO-World object boxes + red laser dot. Runs every frame in a worker thread."""
import cv2
import numpy as np

from .contracts import Det  # shared detection type (name, conf, box, center)


class Detector:
    def __init__(self, weights: str, device: str, classes: list[str]):
        from ultralytics import YOLOWorld  # heavy import, keep lazy
        self.model = YOLOWorld(weights)
        self.model.set_classes(classes)
        self.device = device

    def detect(self, frame, conf: float = 0.15) -> list[Det]:
        r = self.model.predict(frame, device=self.device, imgsz=640, conf=conf, verbose=False)[0]
        names = r.names
        return [Det(names[int(c)], float(p), tuple(map(float, b)))
                for b, c, p in zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist(), r.boxes.conf.tolist())]


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
