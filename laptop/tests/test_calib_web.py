"""calib_web: the browser calibration's HTTP endpoints and calibration.json, against FakeLink + FakeFrames."""
import asyncio
import json
import time

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import coach.calibrate as calibrate
from coach.aim import Calibration
from coach.calib_web import CalibWeb, create_app, mount
from coach.calibrate import MIN_SAVE_POINTS
from coach.contracts import FakeFrames, FakeLink


def cam(pan, tilt):
    """Where the dot lands (same linear map as test_aim's linear_calib)."""
    return pan * 8 - 240, (90 - tilt) * 8


@pytest.fixture
def rig(tmp_path):
    link, frames, out = FakeLink(), FakeFrames(), tmp_path / "calibration.json"
    app, cw = create_app(link, frames, out=out, settle_s=0.0, auto_settle_s=0.0, lag_s=0.0)
    with TestClient(app) as c:
        yield c, cw, link, out


def click_current(c, link):
    x, y = cam(link.pan, link.tilt)
    return c.post("/click", json={"x": x, "y": y})


def test_page_state_and_frame(rig):
    c, cw, link, out = rig
    r = c.get("/")
    assert r.status_code == 200 and "Laser calibration" in r.text and 'BASE = ""' in r.text
    s = c.get("/state").json()
    assert s["mode"] == "idle" and s["n_points"] == 0 and not s["has_calib"] and s["out"] == str(out)
    r = c.get("/frame.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"


def test_no_frame_is_503(tmp_path):
    class NoFrames(FakeFrames):
        def latest(self):
            return None, 0.0
    app, _ = create_app(FakeLink(), NoFrames(), out=tmp_path / "c.json")
    with TestClient(app) as c:
        assert c.get("/frame.jpg").status_code == 503


def test_click_run_saves_calibration_json(rig):
    c, cw, link, out = rig
    assert c.get("/state").json()["grid"]["n"] == 5                        # 5x5 by default
    s = c.post("/start", json={"n": 4}).json()
    assert s["mode"] == "click" and s["total"] == 16 and link.laser
    assert (link.pan, link.tilt) == tuple(s["goal"])
    click_current(c, link)
    c.post("/skip")                                   # point 2 off-screen
    click_current(c, link)
    s = c.post("/redo").json()                        # forget point 3, servos go back to it
    assert s["index"] == 2 and s["n_points"] == 1 and (link.pan, link.tilt) == tuple(s["goal"])
    while not s["done"]:
        s = click_current(c, link).json()
    assert s["saved"] and s["n_points"] == 15 and out.exists()
    assert any("saved" in ln for ln in s["log"]) and any("err (" in ln for ln in s["log"])
    d = json.loads(out.read_text())
    assert d["terms"] == ["1", "x", "y", "x2", "xy", "y2"] and np.asarray(d["coef"]).shape == (6, 2)
    assert Calibration.load(out).angles_for((320, 240)) == pytest.approx((70, 60))
    assert c.post("/click", json={"x": 1, "y": 1}).status_code == 409       # grid done


def test_save_gate_and_errors(rig):
    c, cw, link, out = rig
    assert c.post("/click", json={"x": 1, "y": 2}).status_code == 409      # not started
    assert c.post("/save").status_code == 409
    assert c.post("/test").status_code == 409                               # no calibration yet
    c.post("/start", json={"n": 3})
    for _ in range(MIN_SAVE_POINTS - 1):
        click_current(c, link)
    r = c.post("/save")
    assert r.status_code == 409 and "NOT SAVED" in r.json()["detail"] and not out.exists()
    assert c.post("/start", json={"n": 1}).status_code == 409
    cw.settle_s = 60.0                                                       # servos still moving
    c.post("/start", json={"n": 4})
    r = click_current(c, link)
    assert r.status_code == 409 and "settling" in r.json()["detail"]


def test_save_early_then_test_mode(rig):
    c, cw, link, out = rig
    c.post("/start", json={"n": 4})
    for _ in range(10):
        click_current(c, link)
    assert c.post("/save").json()["saved"] and out.exists()
    s = c.post("/test").json()
    assert s["mode"] == "test" and link.laser
    c.post("/click", json={"x": 320, "y": 240})                              # laser goes there
    assert (link.pan, link.tilt) == pytest.approx((70, 60), abs=0.01)
    assert c.post("/test").json()["mode"] == "click"
    s = c.post("/stop").json()
    assert s["mode"] == "idle" and not link.laser


def test_existing_calibration_is_loaded(tmp_path):
    out = tmp_path / "calibration.json"
    pts = [cam(p, t) for p, t in calibrate.make_grid()]
    calibrate.fit_and_save(pts, calibrate.make_grid(), out)
    app, cw = create_app(FakeLink(), FakeFrames(), out=out)
    with TestClient(app) as c:
        assert c.get("/state").json()["has_calib"]
        assert c.post("/test").json()["mode"] == "test"


def test_auto_run(rig, monkeypatch):
    c, cw, link, out = rig

    async def fake_blink(link, frames, near=None, lag_s=0.0, sleep=asyncio.sleep):
        await sleep(0)
        return None if link.pan > 110 else cam(link.pan, link.tilt)          # right column off-screen
    monkeypatch.setattr(calibrate, "blink_dot", fake_blink)
    assert c.post("/auto", json={"n": 4}).json()["mode"] == "auto"
    for _ in range(200):
        s = c.get("/state").json()
        if s["mode"] != "auto":
            break
        time.sleep(0.01)
    assert s["mode"] == "click" and s["saved"] and s["n_points"] == 12 and not link.laser
    assert sum("skipped" in ln for ln in s["log"]) == 4
    assert Calibration.load(out).angles_for((320, 240)) == pytest.approx((70, 60))


def test_stop_cancels_auto(rig, monkeypatch):
    c, cw, link, out = rig

    async def slow_blink(*a, **k):
        await asyncio.sleep(10)
    monkeypatch.setattr(calibrate, "blink_dot", slow_blink)
    c.post("/auto", json={})
    s = c.post("/stop").json()
    assert s["mode"] == "idle" and not link.laser and not out.exists()


def test_mount_under_prefix(tmp_path):
    app = FastAPI()
    cw = mount(app, FakeLink(), FakeFrames(), prefix="/calib/", out=tmp_path / "c.json", settle_s=0.0)
    assert isinstance(cw, CalibWeb)
    with TestClient(app) as c:
        assert 'BASE = "/calib"' in c.get("/calib/").text
        assert c.post("/calib/start").json()["mode"] == "click"
        assert c.get("/calib/frame.jpg").status_code == 200


def test_watchdog_turns_laser_off(tmp_path):
    t = [0.0]
    link = FakeLink()
    cw = CalibWeb(link, FakeFrames(), out=tmp_path / "c.json", settle_s=0.0, clock=lambda: t[0])
    cw.start()
    assert link.laser

    async def run():
        task = asyncio.create_task(cw.watchdog(timeout_s=3.0))
        await asyncio.sleep(0.6)
        assert link.laser                       # page still there
        t[0] = 10.0
        await asyncio.sleep(0.6)
        task.cancel()
    asyncio.run(run())
    assert not link.laser and cw.mode == "idle"


def test_cli_args_still_parse():
    import argparse
    ap = argparse.ArgumentParser()
    calibrate.add_grid_args(ap)
    args = ap.parse_args(["-n", "3", "--pan-min", "50"])
    assert calibrate.grid_from_args(args) == calibrate.make_grid((50, 120), (30, 90), 3)


def test_requests_keep_the_page_alive(tmp_path):
    t = [0.0]
    app, cw = create_app(FakeLink(), FakeFrames(), out=tmp_path / "c.json", clock=lambda: t[0])
    with TestClient(app) as c:
        t[0] = 100.0
        c.post("/start")                        # a POST counts as the page being open, not just /state
        assert cw.seen_at == 100.0


def test_verify_measures_then_adds_points(rig, monkeypatch):
    c, cw, link, out = rig
    assert c.post("/verify").status_code == 409                             # nothing to verify yet
    s = c.post("/start", json={"n": 4}).json()
    while not s["done"]:
        s = click_current(c, link).json()
    assert s["saved"] and json.loads(out.read_text())["frame"] == [640, 480]   # FakeFrames' size recorded
    s = c.post("/verify").json()
    assert s["mode"] == "verify" and link.laser and s["verify"]["total"] == calibrate.VERIFY_N
    assert tuple(s["goal"]) == (link.pan, link.tilt)
    while s["mode"] == "verify":
        x, y = cam(link.pan, link.tilt)
        s = c.post("/click", json={"x": x + 3, "y": y}).json()             # the dot lands 3 px right
    v = s["verify"]
    assert v["done"] and v["stats"]["n"] == calibrate.VERIFY_N and v["stats"]["mean_deg"] == pytest.approx(3 / 8, abs=0.01)
    assert any(ln.startswith("VERIFY:") for ln in s["log"]) and not link.laser
    s = c.post("/verify_add").json()
    assert s["saved"] and s["n_points"] == 16 + calibrate.VERIFY_N
    assert len(json.loads(out.read_text())["points"]["px"]) == 16 + calibrate.VERIFY_N


def test_auto_verify(rig, monkeypatch):
    c, cw, link, out = rig
    c.post("/start", json={"n": 4})
    s = None
    for _ in range(16):
        s = click_current(c, link).json()
    assert s["saved"]

    async def fake_blink(link, frames, near=None, lag_s=0.0, sleep=asyncio.sleep):
        await sleep(0)
        return cam(link.pan, link.tilt)
    monkeypatch.setattr(calibrate, "blink_dot", fake_blink)
    assert c.post("/verify_auto").json()["mode"] == "auto_verify"
    for _ in range(200):
        s = c.get("/state").json()
        if s["mode"] != "auto_verify":
            break
        time.sleep(0.01)
    assert s["verify"]["done"] and s["verify"]["stats"]["max_deg"] < 0.01 and not link.laser
