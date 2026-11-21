"""Perception: YOLO-World object boxes + red laser dot. Runs every frame in a worker thread."""
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Det:
    name: str
    conf: float
    box: tuple  # x1, y1, x2, y2 (px)

    @property
    def center(self):
        x1, y1, x2, y2 = self.box
        return ((x1 + x2) / 2, (y1 + y2) / 2)


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


def find_red_dot(frame, last=None):
    """Return (x, y) of the laser dot, or None. TODO: tune thresholds under venue lighting."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    # laser dot = very bright, red-ish (often blown out to near-white with a red halo)
    red = cv2.inRange(hsv, (0, 70, 200), (10, 255, 255)) | cv2.inRange(hsv, (165, 70, 200), (180, 255, 255))
    bright = cv2.inRange(hsv, (0, 0, 245), (180, 60, 255))
    mask = cv2.dilate(red, None, iterations=2) & bright | red
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask)
    if n <= 1:
        return None
    cands = [(i, stats[i, cv2.CC_STAT_AREA]) for i in range(1, n) if 2 <= stats[i, cv2.CC_STAT_AREA] <= 400]
    if not cands:
        return None
    if last is not None:  # prefer the blob nearest to where the dot was
        i = min(cands, key=lambda c: np.hypot(*(cents[c[0]] - np.array(last))))[0]
    else:
        i = max(cands, key=lambda c: c[1])[0]
    return tuple(cents[i])
