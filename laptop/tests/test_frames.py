"""FrameGrabber tests with synthetic sources (no webcam / ESP needed)."""
import http.server
import os
import socket
import threading
import time

import cv2
import numpy as np
import pytest

from coach.contracts import FrameSource
from coach.frames import FrameGrabber, parse_source

W, H = 160, 120


def make_video(path, n=30, fps=20):
    """Writes n frames; frame i is filled with gray value i*8 so order is checkable."""
    tmp = str(path) + ".tmp.avi"
    vw = cv2.VideoWriter(tmp, cv2.VideoWriter_fourcc(*"MJPG"), fps, (W, H))
    assert vw.isOpened()
    for i in range(n):
        vw.write(np.full((H, W, 3), (i * 8) % 256, np.uint8))
    vw.release()
    os.replace(tmp, path)  # appear atomically


def wait_for(cond, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "clip.avi"
    make_video(p)
    return str(p)


def test_parse_source():
    assert parse_source("0") == 0 and isinstance(parse_source("0"), int)
    assert parse_source(" 1 ") == 1
    assert parse_source(0) == 0
    assert parse_source("http://coach-cam.local:81/stream") == "http://coach-cam.local:81/stream"
    assert parse_source("clip.avi") == "clip.avi"
    assert FrameGrabber("0").url == 0


def test_latest_and_recent(video):
    g = FrameGrabber(video).start()
    try:
        assert isinstance(g, FrameSource)
        assert wait_for(lambda: g.connected, 3)
        f, t = g.latest()
        assert f.shape == (H, W, 3) and f.dtype == np.uint8
        assert abs(time.time() - t) < 1.0
        assert wait_for(lambda: len(g.history) >= 3, 3)
        r = g.recent(3)
        assert len(r) == 3
        ts = [t for t, _ in list(g.history)[-3:]]
        assert ts == sorted(ts) and all(b - a >= 0.45 for a, b in zip(ts, ts[1:]))
        assert g.recent(0) == [] and len(g.recent(100)) == len(g.history) <= 8
    finally:
        g.stop()
    assert not g.connected


def test_latest_none_before_start(video):
    g = FrameGrabber(video)
    assert g.latest() == (None, 0.0)
    assert g.recent(3) == []
    assert not g.connected


def test_missing_then_appears(tmp_path):
    p = tmp_path / "later.avi"
    g = FrameGrabber(str(p)).start()
    try:
        time.sleep(1.0)
        assert not g.connected and g.latest() == (None, 0.0)
        assert g._thread.is_alive()
        make_video(p)
        t0 = time.time()
        assert wait_for(lambda: g.connected, 3)
        assert time.time() - t0 < 3
    finally:
        g.stop()


def test_source_ends_then_recovers(tmp_path):
    p = tmp_path / "short.avi"
    make_video(p, n=10, fps=20)  # 0.5 s clip
    g = FrameGrabber(str(p)).start()
    try:
        assert wait_for(lambda: g.connected, 3)
        os.remove(p)                       # source goes away
        assert wait_for(lambda: not g.connected, 3)
        make_video(p, n=10, fps=20)        # comes back
        t0 = time.time()
        assert wait_for(lambda: g.connected, 3)
        assert time.time() - t0 < 3
    finally:
        g.stop()


# ── tiny MJPEG server, like the ESP32 CameraWebServer ─────────────────────────

class _Mjpeg(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=frame")
        self.end_headers()
        i = 0
        try:
            while not self.server.halt.is_set():
                ok, jpg = cv2.imencode(".jpg", np.full((H, W, 3), i % 256, np.uint8))
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(jpg)).encode() + b"\r\n\r\n" + jpg.tobytes() + b"\r\n")
                i += 1
                time.sleep(0.03)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _serve(port):
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Mjpeg)
    srv.daemon_threads = True
    srv.halt = threading.Event()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _kill(srv):
    srv.halt.set()
    srv.shutdown()
    srv.server_close()


def test_mjpeg_url_drop_and_recover():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = _serve(port)
    g = FrameGrabber(f"http://127.0.0.1:{port}/stream").start()
    try:
        assert wait_for(lambda: g.connected, 5)
        f, _ = g.latest()
        assert f.shape == (H, W, 3)
        _kill(srv)
        assert wait_for(lambda: not g.connected, 4)
        srv = _serve(port)
        t0 = time.time()
        assert wait_for(lambda: g.connected, 3)
        assert time.time() - t0 < 3
    finally:
        g.stop()
        _kill(srv)


def test_rotate_180_and_flip(tmp_path):
    import cv2
    import numpy as np
    from coach.frames import FrameGrabber, frame_transform

    img = np.zeros((48, 64, 3), np.uint8); img[2, 3] = (255, 255, 255)       # one white pixel near top-left
    assert frame_transform("0") is None and frame_transform(None) is None
    assert frame_transform("180")(img)[45, 60].tolist() == [255, 255, 255]   # -> near bottom-right
    assert frame_transform("vflip")(img)[45, 3].tolist() == [255, 255, 255]
    path = str(tmp_path / "v.avi")
    w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 20, (64, 48))
    for _ in range(20):
        f = np.zeros((48, 64, 3), np.uint8); f[:10, :] = 255                  # bright band on top
        w.write(f)
    w.release()
    g = FrameGrabber(path, rotate="180").start()
    try:
        t0 = time.time()
        while g.latest()[0] is None and time.time() - t0 < 5:
            time.sleep(0.05)
        f, _ = g.latest()
        assert f is not None and f[-3:, :].mean() > 200 and f[:3, :].mean() < 50   # band moved to the bottom
    finally:
        g.stop()


def test_last_frame_size_follows_the_newest_frame():
    import numpy as np

    from coach import frames as fr
    fr._note_size(np.zeros((240, 320, 3), np.uint8))
    assert fr.last_frame_size() == (320, 240)
    fr._note_size(np.zeros((480, 640, 3), np.uint8))
    assert fr.last_frame_size() == (640, 480)
    fr._last_size = None
