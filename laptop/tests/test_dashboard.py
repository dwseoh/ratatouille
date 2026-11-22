"""Dashboard tests: /state is valid and complete, the built pages are served, /stream yields MJPEG. No camera, no hardware."""
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest
from fastapi.testclient import TestClient

from coach import dashboard
from coach.contracts import Det, FakeFrames, Recipe, State, TurnLatency, fake_dets

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
    step = RECIPE.steps[2]
    assert snap["step"] == {"n": 3, "total": 7, "short": step.short, "goal": step.goal, "how": list(step.how),
                            "tips": list(step.tips), "image_url": "/recipe-image/2", "image_step": 3,
                            "image_real": False, "image_note": ""} and snap["recipe"] == RECIPE.name
    assert snap["transcript"] == "how chunky should it be" and snap["speaking"] is True
    assert snap["laser"] == {"target": "fork", "on_target": True}
    assert "avocado" in snap["visible"] and len(snap["events"]) == dashboard.EVENTS_SHOWN
    assert snap["events"][-1]["text"] == "event 24"
    assert snap["latency"] == {"first_token": 640, "first_audio": 910, "point_called": 180}
    assert snap["links"] == {"camera": True, "camera_ignored": False, "s3": True, "vbat_mv": 5120}
    assert snap["caption"] == "Leave it a bit chunky."          # typing mode: just the latest sentence, one at a time


def test_snapshot_copes_with_missing_parts():
    snap = dashboard.snapshot(State(RECIPE))                # no frames, link, agent, turns
    assert snap["latency"] == {} and snap["caption"] == "" and snap["links"]["camera"] is False
    assert snap["step"]["n"] == 1 and snap["session"] == "idle"


def test_routes():
    client = TestClient(dashboard.build_app(fake_state(), FakeFrames(), Link(), Agent()))
    r = client.get("/state")
    assert r.status_code == 200 and r.json()["step"]["short"] == RECIPE.steps[2].short
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert '<div id="root">' in page.text and "Ratatouille" in page.text


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


def test_lan_ip_is_an_address_or_none():
    ip = dashboard.lan_ip()
    assert ip is None or (ip.count(".") == 3 and all(p.isdigit() and 0 <= int(p) <= 255 for p in ip.split(".")))
    assert ip is None or not ip.startswith("127.")


def test_caption_follows_the_voice_timeline_when_there_is_one():
    from coach.captions import CaptionTimeline
    clock = {"t": 0.0}
    cap = CaptionTimeline(lambda: clock["t"])
    for text, secs in (("First sentence.", 2.0), ("Second sentence.", 2.0)):
        cap.queue(text)
    for secs in (2.0, 2.0):
        cap.audio_started(); cap.audio_chunk(secs); cap.audio_stopped()
    cap.playback_started()
    st = fake_state()
    assert dashboard.snapshot(st, None, None, Agent(), cap)["caption"] == "First sentence."
    clock["t"] = 2.5
    assert dashboard.snapshot(st, None, None, Agent(), cap)["caption"] == "Second sentence."   # not the agent's latest sentence


def test_snapshot_says_when_the_camera_is_ignored():
    class A(Agent):
        camera_ignored = True
    assert dashboard.snapshot(fake_state(), FakeFrames(), Link(), A())["links"]["camera_ignored"] is True
    assert dashboard.snapshot(fake_state(), FakeFrames(), Link(), Agent())["links"]["camera_ignored"] is False


# ───────────── visuals: step photo, checklist, phone page, QR ─────────────

def _photo_root(tmp_path, step=None):
    d = tmp_path / "images" / "guacamole"
    d.mkdir(parents=True)
    (d / ("hero.jpg" if step is None else f"step-{step}.jpg")).write_bytes(b"\xff\xd8\xff\xe0JFIFfake")
    return tmp_path


def test_an_image_is_always_up_step_photo_then_dish_photo_then_a_card(tmp_path):
    st = fake_state()
    snap = dashboard.snapshot(st, recipes_root=tmp_path)["step"]
    assert snap["image_url"] == "/recipe-image/2" and snap["image_real"] is False         # nothing at all: a card
    _photo_root(tmp_path)                                                       # hero.jpg only
    snap = dashboard.snapshot(st, recipes_root=tmp_path)["step"]
    assert snap["image_url"].startswith("/recipe-image/2?v=") and snap["image_real"] is True   # dish photo
    _photo_root_step = tmp_path / "images" / "guacamole" / "step-3.jpg"
    _photo_root_step.write_bytes(b"\xff\xd8\xff\xe0JFIFstep")
    assert dashboard.snapshot(st, recipes_root=tmp_path)["step"]["image_url"].startswith("/recipe-image/2?v=")


def test_recipe_image_route_serves_the_photo_dish_photo_or_a_card(tmp_path):
    st = fake_state()
    client = TestClient(dashboard.build_app(st, FakeFrames(), Link(), Agent(), recipes_root=tmp_path))
    r = client.get("/recipe-image/2")
    assert r.status_code == 200 and r.headers["content-type"] == "image/svg+xml" and b"Step 3" in r.content
    _photo_root(tmp_path, step=3)
    r = client.get("/recipe-image/2")
    assert r.status_code == 200 and r.content.startswith(b"\xff\xd8") and r.headers["cache-control"] == "no-cache"
    assert client.get("/recipe-image/99").status_code == 404
    assert client.get("/recipe-image/..%2F..%2Fsecret").status_code in (404, 422)          # no client-chosen paths


def test_a_shown_image_overrides_the_current_step_until_the_step_changes(tmp_path):
    st = fake_state()
    st.shown_image = (0, st.step)
    snap = dashboard.snapshot(st, recipes_root=tmp_path)["step"]
    assert snap["image_url"].startswith("/recipe-image/0") and snap["image_step"] == 1
    assert snap["image_note"] == f"Showing step 1: {RECIPE.steps[0].short}"
    st.step += 1                                                                # the cook moved on: back to following them
    snap = dashboard.snapshot(st, recipes_root=tmp_path)["step"]
    assert snap["image_step"] == st.step + 1 and snap["image_note"] == ""


def test_cook_page_and_info_and_qr(tmp_path):
    client = TestClient(dashboard.build_app(fake_state(), FakeFrames(), Link(), Agent(), recipes_root=tmp_path))
    page = client.get("/cook")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert '<div id="root">' in page.text and "step by step" in page.text and "viewport" in page.text
    info = client.get("/info").json()
    assert info["cook_url"].endswith("/cook") and info["cook_url"].startswith("http://")
    qr = client.get("/cook-qr.svg")
    if info["qr"]:
        assert qr.status_code == 200 and qr.headers["content-type"] == "image/svg+xml" and b"<svg" in qr.content
    else:
        assert qr.status_code == 404


# ───────────── the built frontend (laptop/frontend -> coach/static) ─────────────

import re  # noqa: E402

# Every field the frontend reads from /state (see AppState in laptop/frontend/src/api.ts). Rename one on either side
# and the test below fails, instead of the page silently going blank.
FRONTEND_FIELDS = {
    "": ["session", "recipe", "transcript", "caption", "speaking", "user_talking", "events", "latency", "laser",
         "visible", "links", "step"],
    "step": ["n", "total", "short", "goal", "how", "tips", "image_url", "image_note"],
    "laser": ["target", "on_target"],
    "links": ["camera", "camera_ignored", "s3", "vbat_mv"],
}


def test_state_has_every_field_the_frontend_reads_and_the_frontend_still_names_them():
    snap = dashboard.snapshot(fake_state(), FakeFrames(), Link(), Agent())
    api_ts = Path(__file__).resolve().parent.parent / "frontend" / "src" / "api.ts"
    source = api_ts.read_text() if api_ts.exists() else None
    for parent, names in FRONTEND_FIELDS.items():
        block = snap[parent] if parent else snap
        for name in names:
            assert name in block, f"/state is missing {parent + '.' if parent else ''}{name}"
            if source is not None:
                assert re.search(rf"\b{name}\b", source), f"frontend/src/api.ts no longer mentions {name}"


def _page_links(html: str) -> list[str]:
    """Local files a built page loads (scripts, styles, icons, preloads), e.g. /assets/main-abc.js."""
    return re.findall(r'(?:src|href)="(/[^"]+)"', html)


def test_the_built_pages_load_only_files_that_exist_and_the_server_serves_them():
    client = TestClient(dashboard.build_app(fake_state(), FakeFrames(), Link(), Agent()))
    for route, name in (("/", "index.html"), ("/cook", "cook.html")):
        html = client.get(route).text
        assert html == (dashboard.STATIC_DIR / name).read_text()
        links = _page_links(html)
        assert any(link.startswith("/assets/") and link.endswith(".js") for link in links), f"{name} loads no script"
        for link in links:
            assert (dashboard.STATIC_DIR / link.lstrip("/")).is_file(), f"{name} references {link}, not in coach/static/"
            assert client.get(link).status_code == 200, f"{link} is not served"
    for icon in ("/favicon-32-light.png", "/favicon-32-dark.png", "/favicon-180.png", "/menu.svg"):   # used by the code
        assert client.get(icon).status_code == 200, icon


def test_assets_never_shadow_the_api_and_unknown_or_escaping_paths_are_404():
    client = TestClient(dashboard.build_app(fake_state(), FakeFrames(), Link(), Agent()))
    assert client.get("/state").headers["content-type"].startswith("application/json")
    assert client.get("/nope.js").status_code == 404
    for evil in ("/../dashboard.py", "/%2e%2e/dashboard.py", "/assets/../../dashboard.py", "/..%2fdashboard.py"):
        r = client.get(evil)
        assert r.status_code in (404, 400) and b"build_app" not in r.content, evil


def test_a_missing_build_says_how_to_build_and_the_api_still_works(tmp_path):
    client = TestClient(dashboard.build_app(fake_state(), FakeFrames(), Link(), Agent(), static_dir=tmp_path / "nope"))
    for route in ("/", "/cook"):
        r = client.get(route)
        assert r.status_code == 503 and "npm run build" in r.text
    assert client.get("/state").status_code == 200


def test_the_laser_target_is_much_more_visible_than_the_other_boxes():
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    dets = [Det("lime", 0.9, (100, 100, 200, 200)), Det("bowl", 0.9, (300, 100, 400, 200))]
    plain = dashboard.annotate(frame, dets, None, None)
    aimed = dashboard.annotate(frame, dets, None, "lime")
    def lit(img, box):                                          # non-black pixels around a box (incl. label above)
        x1, y1, x2, y2 = box
        return int((img[max(y1 - 40, 0):y2 + 10, x1 - 10:x2 + 10].sum(axis=2) > 0).sum())
    assert lit(aimed, (100, 100, 200, 200)) > 3 * lit(plain, (100, 100, 200, 200))             # thick box + big label
    assert (aimed[100:200, 100:200, 1] == 200).any() and not (aimed[100:200, 100:200, 2] == 200).any()   # green, not red
    assert lit(aimed, (300, 100, 400, 200)) == lit(plain, (300, 100, 400, 200))                # others unchanged
