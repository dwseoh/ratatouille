"""Camera grabber: always holds the NEWEST frame (never a backlog) and reconnects on failure."""
import threading
import time
from collections import deque

import cv2


class FrameGrabber:
    def __init__(self, url: str, history_every_s: float = 0.5, history_len: int = 8):
        self.url = url
        self._lock = threading.Lock()
        self._frame = None
        self._ts = 0.0
        self.history: deque = deque(maxlen=history_len)  # (ts, frame) samples for the LLM
        self._history_every = history_every_s
        self.connected = False
        self._stop = False

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="frames").start()
        return self

    def latest(self):
        with self._lock:
            return self._frame, self._ts

    def _run(self):
        backoff = 1.0
        while not self._stop:
            cap = cv2.VideoCapture(self.url)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not cap.isOpened():
                self.connected = False
                time.sleep(backoff); backoff = min(backoff * 2, 8)
                continue
            backoff, last_hist = 1.0, 0.0
            while not self._stop:
                ok, frame = cap.read()
                if not ok:
                    break  # stream dropped -> reconnect
                now = time.time()
                with self._lock:
                    self._frame, self._ts = frame, now
                self.connected = True
                if now - last_hist >= self._history_every:
                    self.history.append((now, frame)); last_hist = now
            cap.release()
            self.connected = False

    def stop(self):
        self._stop = True
