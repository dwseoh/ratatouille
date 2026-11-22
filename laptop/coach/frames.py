"""Camera grabber: always holds the NEWEST frame (never a backlog) and reconnects on failure.

Satisfies `contracts.FrameSource`. A background thread reads the source as fast as it delivers, so
`latest()` never returns a stale buffered frame and never blocks the asyncio loop.

Source can be a webcam index (0 or "0"), a video file path (played at its own fps, looped), or an
http MJPEG URL (the ESP32 cam, e.g. http://coach-cam.local:81/stream).
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque

import cv2

STALE_S = 2.0          # no new frame for this long -> connected is False (stream hung)
BACKOFF_MIN_S = 0.25
BACKOFF_MAX_S = 2.0    # keeps recovery under 3 s after the source comes back
NET_TIMEOUT_MS = 2000  # open/read timeout for network streams (FFmpeg backend)


_XF = {"90": lambda f: cv2.rotate(f, cv2.ROTATE_90_CLOCKWISE), "180": lambda f: cv2.rotate(f, cv2.ROTATE_180),
       "270": lambda f: cv2.rotate(f, cv2.ROTATE_90_COUNTERCLOCKWISE),
       "vflip": lambda f: cv2.flip(f, 0), "hflip": lambda f: cv2.flip(f, 1)}


def frame_transform(spec):
    """CAM_ROTATE value -> function applied to each frame, or None for "0" / unknown values."""
    return _XF.get(str(spec).strip().lower()) if spec is not None else None


def parse_source(src: int | str) -> int | str:
    """"0" -> 0 (webcam index); anything else is passed to cv2 as-is (file path or URL)."""
    if isinstance(src, str) and src.strip().isdigit():
        return int(src.strip())
    return src


class FrameGrabber:
    def __init__(self, url: int | str, history_every_s: float = 0.5, history_len: int = 8, rotate=None):
        self.url = parse_source(url)
        if rotate is None:                      # default: CAM_ROTATE from laptop/.env
            from .config import CONFIG
            rotate = CONFIG.cam_rotate
        self._xf = frame_transform(rotate)
        self._lock = threading.Lock()
        self._frame = None
        self._ts = 0.0
        self.history: deque = deque(maxlen=history_len)  # (ts, frame) samples for the LLM, ~4 s
        self._history_every = history_every_s
        self._connected = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── FrameSource ────────────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._connected and time.time() - self._ts < STALE_S

    def latest(self):
        """Newest frame + its time.time(), (None, 0.0) if none yet."""
        with self._lock:
            return self._frame, self._ts

    def recent(self, n: int) -> list:
        """Last n history frames (~0.5 s apart), oldest first. Fewer if history is short."""
        if n <= 0:
            return []
        with self._lock:
            return [f for _, f in list(self.history)[-n:]]

    # ── lifecycle ──────────────────────────────────────────────────────────────
    def start(self) -> "FrameGrabber":
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="frames")
            self._thread.start()
        return self

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._connected = False

    # ── reader thread ──────────────────────────────────────────────────────────
    def _open(self):
        src = self.url
        if isinstance(src, str) and "://" in src:
            try:
                return cv2.VideoCapture(src, cv2.CAP_FFMPEG, [
                    cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, NET_TIMEOUT_MS,
                    cv2.CAP_PROP_READ_TIMEOUT_MSEC, NET_TIMEOUT_MS])
            except (cv2.error, TypeError, AttributeError):  # older OpenCV without params
                return cv2.VideoCapture(src)
        return cv2.VideoCapture(src)

    def _run(self):
        backoff = BACKOFF_MIN_S
        while not self._stop.is_set():
            is_file = isinstance(self.url, str) and os.path.isfile(self.url)
            cap = self._open()
            if not cap.isOpened():
                cap.release()
                self._connected = False
                self._stop.wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
                continue
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            # Files would otherwise be read in a burst; play them at their own rate like a live cam.
            period = 0.0
            if is_file:
                fps = cap.get(cv2.CAP_PROP_FPS)
                period = 1.0 / fps if 0 < fps < 240 else 1 / 30
            last_hist = 0.0
            got_any = False
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    break  # stream dropped / file ended -> reconnect
                if self._xf is not None:
                    frame = self._xf(frame)
                now = time.time()
                with self._lock:
                    self._frame, self._ts = frame, now
                    if now - last_hist >= self._history_every:
                        self.history.append((now, frame))
                        last_hist = now
                self._connected = True
                got_any = True
                if period:
                    self._stop.wait(period)
            cap.release()
            self._connected = False
            if got_any:
                backoff = BACKOFF_MIN_S
            else:  # opened but no frames: back off like a failed open
                self._stop.wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
