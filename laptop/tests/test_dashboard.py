"""Dashboard tests: /state is valid and complete, / serves the page, /stream yields MJPEG. No camera, no hardware."""
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest
from fastapi.testclient import TestClient

from coach import dashboard
from coach.contracts import FakeFrames, Recipe, State, TurnLatency, fake_dets

RECIPE = Recipe.load(Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json")


def fake_state():
    st = State(RECIPE, dets=fake_dets())
    st.session, st.step, st.transcript, st.speaking = "active", 2, "how chunky should it be", True
    st.target, st.on_target = "fork", True
    for i in range(25):
        st.log("user" if i % 2 else "vision", f"event {i}")
    t = TurnLatency(1)
    t.marks.update(speech_end=10.0, first_token=10.64, first_audio=10.91, point_called=10.18)
    st.turns.append(t)
    return st


class Link:
    online = True
    status = NS(vbat_mv=5120)


class Agent:
    recent_sentences = ["Press down with the fork.", "Leave it a bit chunky."]


def test_snapshot_has_everything_the_page_needs():
    snap = dashboard.snapshot(fake_state(), FakeFrames(), Link(), Agent())
    assert snap["step"] == {"n": 3, "total": 7, "short": RECIPE.steps[2].short, "goal": RECIPE.steps[2].goal}
    assert snap["transcript"] == "how chunky should it be" and snap["speaking"] is True
    assert snap["laser"] == {"target": "fork", "on_target": True}
    assert "avocado" in snap["visible"] and len(snap["events"]) == dashboard.EVENTS_SHOWN
    assert snap["events"][-1]["text"] == "event 24"
    assert snap["latency"] == {"first_token": 640, "first_audio": 910, "point_called": 180}
    assert snap["links"] == {"camera": True, "s3": True, "vbat_mv": 5120}
    assert snap["captions"] == ["Press down with the fork.", "Leave it a bit chunky."]


def test_snapshot_copes_with_missing_parts():
    snap = dashboard.snapshot(State(RECIPE))                # no frames, link, agent, turns
    assert snap["latency"] == {} and snap["captions"] == [] and snap["links"]["camera"] is False
    assert snap["step"]["n"] == 1 and snap["session"] == "idle"


def test_routes():
    client = TestClient(dashboard.build_app(fake_state(), FakeFrames(), Link(), Agent()))
    r = client.get("/state")
    assert r.status_code == 200 and r.json()["step"]["short"] == RECIPE.steps[2].short
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "/state" in page.text and "/stream" in page.text and "Remy says" in page.text


def test_page_captions_are_large_and_high_contrast():
    css = dashboard.PAGE
    assert "font-size:34px" in css and "--accent:#ffd447" in css          # >= 32 px, bright on dark


def test_stream_is_mjpeg_with_boxes():
    st = fake_state()
    app = dashboard.build_app(st, FakeFrames(), Link(), Agent())
    client = TestClient(app)
    with client.stream("GET", "/stream") as r:
        assert r.headers["content-type"].startswith("multipart/x-mixed-replace; boundary=frame")
        chunk = b""
        for part in r.iter_bytes():
            chunk += part
            if b"\xff\xd9" in chunk:                                          # one whole JPEG
                break
    assert chunk.startswith(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8")


def test_stream_without_a_camera_shows_a_placeholder():
    app = dashboard.build_app(State(RECIPE), None, None, None)
    client = TestClient(app)
    with client.stream("GET", "/stream") as r:
        first = next(r.iter_bytes())
    assert first.startswith(b"--frame")


def test_annotate_draws_boxes_and_target():
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    out = dashboard.annotate(frame, fake_dets(("lime",)), (100, 100), "lime")
    assert out.shape == frame.shape and out.sum() > 0 and frame.sum() == 0   # drew on a copy


def test_serve_starts_answers_and_stops_cleanly_when_cancelled():
    import socket
    import urllib.request

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    async def go():
        app = dashboard.build_app(fake_state(), FakeFrames(), Link(), Agent())
        task = asyncio.create_task(dashboard.serve(app, port))
        await asyncio.sleep(0.8)
        body = await asyncio.to_thread(lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/state", timeout=3).read())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return body

    assert b'"step"' in asyncio.run(go())
