"""Aiming accuracy on a simulated box (coach.aim_sim.Rig: camera, counter, pan-tilt 12 cm above the lens, SG90s with
offset/gain errors, nonlinearity, backlash and noise). The physical plane model vs the v1 polynomial, model choice,
calibration.json v2, frame-size / CAM_ROTATE checks, the Aimer end to end, Mover, click snapping, verify, --eval."""
import json
from pathlib import Path

import numpy as np
import pytest

from coach.aim import OUT_OF_REACH, Aimer, Calibration
from coach.aim_model import PolyModel, fit_plane
from coach.aim_sim import Rig, aim_error
from coach.calibrate import (
    CalibSession,
    Mover,
    VerifySession,
    evaluate,
    fit_and_save,
    make_grid,
    snap_to_dot,
)
from coach.contracts import TFT_STATUS, Det, FakeLink, Recipe, State

RECIPE = Recipe.load(Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json")
PAN, TILT = (72.0, 128.0), (0.0, 20.0)          # the jog range of the real box
LIMITS = (*PAN, *TILT)
LO, HI = (65.0, 0.0), (135.0, 25.0)              # PAN_*/TILT_* in .env: the jog range plus a few degrees
GRID = make_grid((75.0, 125.0), (3.0, 17.0), 5)


def quiet(r: Rig) -> Rig:
    for s in (r.pan, r.tilt):
        s.play_deg = s.noise_deg = 0.0
    return r


def ideal() -> Rig:
    r = quiet(Rig(k1=0.0, k2=0.0, pp=(0.0, 0.0), yaw=0.0, roll=0.0, tilt_off=0.0, beam_off=0.0, cone=0.0, skew=0.0))
    r.pan.nl_deg = r.tilt.nl_deg = 0.0
    return r


def reachable_pixels(rig: Rig, n=300, seed=1):
    pts = rig.targets(PAN, TILT, n=n, rng=np.random.default_rng(seed))
    return pts, rig.project(pts), np.array([rig.ideal_cmd(p) for p in pts])


@pytest.fixture(scope="module")
def real_cal():
    """One realistic calibration: 5x5 grid approached from below, clicks snapped (0.4 px), CAM_ROTATE 180."""
    rig = Rig()
    px, ang = rig.calibrate(GRID, np.random.default_rng(0), click_px=0.4, via_deg=3.0, lo=LO, hi=HI)
    calib = Calibration().fit(px, ang, model="auto", frame=rig.frame, rotate="180", robust=True)
    return rig, px, ang, calib


# ── the model ────────────────────────────────────────────────────────────────

def test_plane_model_is_exact_on_an_ideal_rig():
    rig = ideal()
    px, ang = rig.calibrate(GRID, np.random.default_rng(0), click_px=0.0, via_deg=3.0, lo=LO, hi=HI)
    plane, fit_rms = fit_plane(px, ang, rig.frame)
    pts, tp, ideal_cmd = reachable_pixels(rig, 150)
    err = np.hypot(*(plane.predict(tp) - ideal_cmd).T)
    assert fit_rms < 0.05 and err.max() < 0.15
    assert plane.params["pan_gain"] < 0 < plane.params["tilt_gain"]   # bigger pan = left, bigger tilt = up


def test_plane_beats_the_polynomial_especially_outside_the_grid():
    rig = quiet(Rig())                               # all the model mismatch, no noise
    px, ang = rig.calibrate(GRID, np.random.default_rng(0), click_px=0.0, via_deg=3.0)
    plane, _ = fit_plane(px, ang, rig.frame)
    poly = PolyModel().fit(px, ang)
    pts, tp, ideal_cmd = reachable_pixels(rig)
    inside = (ideal_cmd[:, 0] >= 75) & (ideal_cmd[:, 0] <= 125) & (ideal_cmd[:, 1] >= 3) & (ideal_cmd[:, 1] <= 17)
    e_plane = np.hypot(*(plane.predict(tp) - ideal_cmd).T)
    e_poly = np.hypot(*(poly.predict(tp) - ideal_cmd).T)
    assert e_plane.mean() < 0.4 * e_poly.mean()
    assert e_plane[~inside].max() < 0.5 * e_poly[~inside].max()
    assert e_plane.max() < 0.3                       # degrees, anywhere the laser can reach

    # a smaller grid (more of the picture is extrapolation): the polynomial's worst case doubles, the plane's doesn't
    px2, ang2 = rig.calibrate(make_grid((85.0, 115.0), (6.0, 14.0), 5), np.random.default_rng(0), via_deg=3.0)
    e_plane2 = np.hypot(*(fit_plane(px2, ang2, rig.frame)[0].predict(tp) - ideal_cmd).T)
    e_poly2 = np.hypot(*(PolyModel().fit(px2, ang2).predict(tp) - ideal_cmd).T)
    assert e_plane2.max() < 0.7 * e_poly2.max() and e_plane2.mean() < e_poly2.mean()


def test_new_pipeline_lands_closer_than_v1_through_the_servos():
    """End to end through the simulated servos (backlash, noise): v1 calibration habits vs the new ones."""
    rig = Rig()
    pts = rig.targets(PAN, TILT, n=200, rng=np.random.default_rng(1))
    tp = rig.project(pts)
    # v1: 4x4 serpentine grid, eyeballed clicks, polynomial, servos arrive from wherever they were
    px, ang = rig.calibrate(make_grid((75.0, 125.0), (3.0, 17.0), 4), np.random.default_rng(0), click_px=1.5)
    v1 = Calibration().fit(px, ang)
    deg_v1, cm_v1 = aim_error(rig, pts, np.clip(v1.angles_many(tp), (72, 0), (128, 20)), via_deg=0.0)
    # new: 5x5 approached from below, snapped clicks, robust auto fit, approach from below when aiming
    px, ang = rig.calibrate(GRID, np.random.default_rng(0), click_px=0.4, via_deg=3.0, lo=LO, hi=HI)
    new = Calibration().fit(px, ang, model="auto", frame=rig.frame, robust=True)
    deg_new, cm_new = aim_error(rig, pts, np.clip(new.angles_many(tp), (72, 0), (128, 20)), via_deg=3.0,
                                lo=LO, hi=HI)
    assert new.model == "plane"
    assert deg_new.mean() < 0.5 * deg_v1.mean() and deg_new.max() < 0.6 * deg_v1.max()
    assert cm_new.mean() < 0.5                       # cm on the counter


def test_auto_choice_by_cross_validation(real_cal):
    rig, px, ang, calib = real_cal
    assert calib.model == "plane"
    assert calib.cv["plane"]["loo"] < calib.cv["poly"]["loo"]
    assert calib.frame == (320, 240) and calib.rotate == "180"
    lines = calib.summary()
    assert "model: plane" in lines[0] and any("<- used" in ln and "plane" in ln for ln in lines)
    # a mapping that really is quadratic: the polynomial wins
    q = [(x, y) for x in np.linspace(20, 300, 5) for y in np.linspace(20, 220, 5)]
    qa = [(120 - 0.09 * x + 2e-5 * x * x, 95 - 0.1 * y + 3e-5 * y * y) for x, y in q]
    assert Calibration().fit(q, qa, model="auto", frame=(320, 240)).model == "poly"


def test_bad_click_is_flagged_and_counted_less():
    rig = ideal()
    px, ang = rig.calibrate(GRID, np.random.default_rng(0), click_px=0.3, via_deg=3.0)
    px[12] += (25.0, -10.0)                          # one misclick
    calib = Calibration().fit(px, ang, model="auto", frame=rig.frame, robust=True)
    lines, worst = calib.report(px, ang)
    assert worst > 2 and "bad click" in lines[12] and sum("bad click" in ln for ln in lines) == 1
    pts, tp, ideal_cmd = reachable_pixels(rig, 100)
    assert np.hypot(*(calib.angles_many(tp) - ideal_cmd).T).max() < 0.4


# ── calibration.json ─────────────────────────────────────────────────────────

def test_json_v2_roundtrip_and_old_files(real_cal, tmp_path):
    rig, px, ang, calib = real_cal
    f = tmp_path / "calibration.json"
    calib.save(f)
    d = json.loads(f.read_text())
    assert d["terms"] == ["1", "x", "y", "x2", "xy", "y2"] and np.asarray(d["coef"]).shape == (6, 2)   # v1 readers
    assert d["model"] == "plane" and d["frame"] == [320, 240] and d["rotate"] == "180" and len(d["points"]["px"]) == 25
    back = Calibration.load(f)
    assert back.model == "plane" and back.angles_for((150, 60)) == pytest.approx(calib.angles_for((150, 60)))
    assert Calibration.load(f, model="poly").angles_for((150, 60)) == pytest.approx(calib.poly.predict([(150, 60)])[0])
    # v1 file and the even older bare list
    (tmp_path / "v1.json").write_text(json.dumps({"terms": d["terms"], "coef": d["coef"]}))
    (tmp_path / "v0.json").write_text(json.dumps(d["coef"]))
    for name in ("v1.json", "v0.json"):
        old = Calibration.load(tmp_path / name)
        assert old.model == "poly" and old.frame is None and old.rotate is None
        assert old.angles_for((150, 60)) == pytest.approx(calib.poly.predict([(150, 60)])[0])
        assert old.frame_check((640, 480), "180").ok                   # nothing recorded: can't check


def test_frame_check_rescales_to_the_same_physical_point(real_cal):
    rig, px, ang, calib = real_cal
    big = Rig().scale(640)                           # same optics, camera streaming 640x480 now
    pts = rig.targets(PAN, TILT, n=50, rng=np.random.default_rng(4))
    fc = calib.frame_check((640, 480), "180")
    assert fc.ok and fc.scale == 0.5 and "640x480" in fc.msg and "SCALED" in fc.tft
    a_small = calib.angles_many(rig.project(pts))
    a_big = calib.angles_many(fc.to_calib(big.project(pts)))
    assert np.abs(a_big - a_small).max() < 0.05
    assert not calib.frame_check((320, 240), "0").ok                   # rotation changed: refuse
    assert calib.frame_check((320, 240), "180") .msg == ""             # exact match
    hvga = calib.frame_check((480, 320), "180")
    assert hvga.ok and "different shape" in hvga.msg


class SpyLink(FakeLink):
    def __init__(self):
        super().__init__()
        self.texts = []

    def show_text(self, line, text):
        self.texts.append((line, text))


def obj_box(rig: Rig, centre, r):
    """YOLO box of a ball of radius r centred at a 3D point."""
    u = np.random.default_rng(3).normal(size=(400, 3))
    u /= np.linalg.norm(u, axis=1, keepdims=True)
    p = rig.project(centre + r * u)
    return (*p.min(0), *p.max(0))


def run_aimer(aimer, n=90):
    t = [1000.0]
    aimer.clock = lambda: t[0]
    for _ in range(n):
        t[0] += 1 / 30
        aimer.tick()
        if aimer.on_target:
            break


def test_aimer_rescales_a_bigger_frame_and_refuses_a_rotation(real_cal):
    rig, px, ang, calib = real_cal
    big = Rig().scale(640)
    centre = rig.targets(PAN, TILT, n=1, rng=np.random.default_rng(7))[0] - rig.n * 0.025
    st = State(RECIPE, dets=[Det("lime", 0.9, obj_box(big, centre, 0.025))])
    link = SpyLink()
    aimer = Aimer(link, calib, st, limits=LIMITS, frame_size=lambda: (640, 480), cam_rotate="180")
    assert aimer.point_at("lime") == "ok" and link.laser
    run_aimer(aimer)
    small_box = obj_box(rig, centre, 0.025)
    expect = calib.angles_for(((small_box[0] + small_box[2]) / 2, small_box[1] + 0.6 * (small_box[3] - small_box[1])))
    expect = np.clip(expect, (LIMITS[0], LIMITS[2]), (LIMITS[1], LIMITS[3]))   # within REACH_TOL: clamped
    assert aimer.on_target and aimer.goal == pytest.approx(expect, abs=0.1)
    assert (TFT_STATUS, "CALIB 320x240 SCALED") in link.texts

    link2 = SpyLink()
    aimer2 = Aimer(link2, calib, st, limits=LIMITS, frame_size=lambda: (320, 240), cam_rotate="0")
    assert aimer2.point_at("lime") == "ok" and not link2.laser       # calibration doesn't match: laser stays off
    run_aimer(aimer2, 30)
    assert not link2.laser and not aimer2.on_target and (link2.pan, link2.tilt) == (90.0, 60.0)
    assert any(line == TFT_STATUS and "ROTATE" in text for line, text in link2.texts)


def test_aimer_lands_on_fruit_and_reports_unreachable_ones(real_cal):
    rig, px, ang, calib = real_cal
    rng = np.random.default_rng(11)
    errs = []
    for k in range(25):
        p = rig.targets(PAN, TILT, n=1, rng=rng)[0]
        centre = p - rig.n * 0.025                       # a lime resting on the counter
        c = rig.ideal_cmd(centre)
        if not (PAN[0] + 1 <= c[0] <= PAN[1] - 1 and TILT[0] + 1 <= c[1] <= TILT[1] - 1):
            continue
        st = State(RECIPE, dets=[Det("lime", 0.9, obj_box(rig, centre, 0.025))])
        aimer = Aimer(SpyLink(), calib, st, limits=LIMITS, frame_size=lambda: (320, 240), cam_rotate="180")
        assert aimer.point_at("lime") == "ok"
        run_aimer(aimer)
        assert aimer.on_target
        deg, _ = aim_error(rig, [centre], [aimer.goal], via_deg=3.0, lo=LO, hi=HI)
        errs.append(deg[0])
    assert len(errs) >= 15 and np.mean(errs) < 0.35 and np.max(errs) < 0.8
    # a lime at the bottom of the picture: below the tilt end stop
    bottom = rig.on_counter([(160.0, 220.0)])[0] - rig.n * 0.025
    st = State(RECIPE, dets=[Det("lime", 0.9, obj_box(rig, bottom, 0.025))])
    link = SpyLink()
    aimer = Aimer(link, calib, st, limits=LIMITS, frame_size=lambda: (320, 240), cam_rotate="180")
    assert aimer.point_at("lime") == OUT_OF_REACH and not link.laser and aimer.target is None


# ── calibration tool helpers ─────────────────────────────────────────────────

class RampLink(FakeLink):
    """FakeLink + ControlLink's 2 deg/packet ramp and `moving` flag. pump() = one 30 Hz packet."""

    def __init__(self, pan=110.0, tilt=20.0):
        super().__init__()
        self.pan, self.tilt = pan, tilt
        self.out = [pan, tilt]
        self.trail = [tuple(self.out)]

    @property
    def moving(self):
        return abs(self.out[0] - self.pan) > 0.05 or abs(self.out[1] - self.tilt) > 0.05

    def pump(self):
        for k, goal in enumerate((self.pan, self.tilt)):
            d = goal - self.out[k]
            self.out[k] = goal if abs(d) <= 2 else self.out[k] + (2 if d > 0 else -2)
        self.trail.append(tuple(self.out))


def test_mover_approaches_one_way_and_waits_for_the_ramp():
    link, t = RampLink(110.0, 5.0), [0.0]
    m = Mover(link, lo=LO, hi=HI, clock=lambda: t[0])
    m.go((100.0, 10.0))
    assert (link.pan, link.tilt) == (97.0, 13.0)         # 3 deg short of the goal first: pan below, tilt above
    settled_at = None
    for i in range(200):
        link.pump()
        t[0] += 1 / 30
        if m.settled(0.5):
            settled_at = i
            break
    assert settled_at is not None and tuple(link.out) == (100.0, 10.0)
    moves = [(b[0] - a[0], b[1] - a[1]) for a, b in zip(link.trail, link.trail[1:]) if a != b]
    assert moves[-1][0] >= 0 and moves[-1][1] <= 0         # the final move: pan up, tilt down (APPROACH_DIR)
    assert t[0] - m.arrived_at >= 0.5


def test_click_snaps_to_the_dot():
    import cv2
    img = np.full((240, 320, 3), 70, np.uint8)
    cv2.circle(img, (100, 80), 5, (0, 0, 255), -1)     # red halo
    cv2.circle(img, (100, 80), 2, (255, 255, 255), -1)  # blown-out core
    x, y, snapped = snap_to_dot(img, 105.0, 84.0)
    assert snapped and abs(x - 100) < 1 and abs(y - 80) < 1
    assert snap_to_dot(img, 200.0, 200.0) == (200.0, 200.0, False)    # nothing near: keep the click
    assert snap_to_dot(None, 5.0, 6.0) == (5.0, 6.0, False)


def test_verify_session_measures_the_error(real_cal):
    rig, px, ang, calib = real_cal
    vs = VerifySession(calib, 8, frame=rig.frame, limits=LIMITS, rng=np.random.default_rng(5))
    assert len(vs.targets) == 8
    rng = np.random.default_rng(6)
    while not vs.done:
        target_px, cmd = vs.current()
        assert LIMITS[0] <= cmd[0] <= LIMITS[1] and LIMITS[2] <= cmd[1] <= LIMITS[3]
        rig.approach(cmd, 3.0, rng, lo=LO, hi=HI)
        vs.record(rig.project(rig.hit(*rig.beam(rig.pan.pos, rig.tilt.pos)))[0])
    lines, stats = vs.summary()
    assert stats["n"] == 8 and stats["mean_deg"] < 0.4 and stats["max_cm"] < 2.0
    assert lines[-1].startswith("VERIFY: 8 points, mean")
    sess = CalibSession(GRID)
    assert vs.add_to(sess) == 8 and sess.n_points == 8


def test_eval_reports_models_and_old_files(real_cal, tmp_path):
    rig, px, ang, calib = real_cal
    f = tmp_path / "calibration.json"
    fit_and_save(px, ang, f, frame=rig.frame, rotate="180")
    out = []
    evaluate(f, log=out.append, cam_rotate="180", limits=LIMITS)
    text = "\n".join(out)
    assert "model: plane" in text and "leave-one-out" in text and "plane vs poly" in text and "PROBLEM" not in text
    out.clear()
    evaluate(f, log=out.append, cam_rotate="0", limits=LIMITS)
    assert any("PROBLEM" in ln for ln in out)
    (tmp_path / "old.json").write_text(json.dumps({"terms": ["1", "x", "y", "x2", "xy", "y2"],
                                                   "coef": calib.poly.coef.tolist()}))
    out.clear()
    evaluate(tmp_path / "old.json", log=out.append, cam_rotate="180", limits=LIMITS)
    assert any("old file" in ln for ln in out)
    out.clear()
    assert evaluate(tmp_path / "missing.json", log=out.append) is None and "no " in out[0]


# ── the cv2 calibration window, driven by a fake window + camera ────────────

def test_click_calibration_window_grid_verify_and_add(tmp_path, monkeypatch):
    import time

    import cv2

    import coach.calibrate as calibrate
    import coach.control as control
    import coach.frames as frames_mod

    def cam(pan, tilt):
        return (pan - 30) * 8, (90 - tilt) * 8

    link = FakeLink()

    class Link:
        async def start(self):
            return link

    class Frames:
        def __init__(self, *a, **k):
            pass

        def start(self):
            return self

        def stop(self):
            pass

        def latest(self):
            img = np.full((480, 640, 3), 60, np.uint8)
            if link.laser:
                x, y = (int(round(v)) for v in cam(link.pan, link.tilt))
                cv2.circle(img, (x, y), 5, (0, 0, 255), -1)
                cv2.circle(img, (x, y), 2, (255, 255, 255), -1)
            return img, time.time()

    calls, mouse = [0], {}

    def wait_key(_ms):
        calls[0] += 1
        x, y = cam(link.pan, link.tilt)
        for _ in range(2):                  # a double click, a bit off the dot: one point, snapped to the dot
            mouse["cb"](cv2.EVENT_LBUTTONDOWN, int(x) + 2, int(y) - 1, 0, None)
        return {30: ord("v"), 60: ord("a"), 62: ord("q")}.get(calls[0], 255)

    monkeypatch.setattr(control, "ControlLink", lambda *a, **k: Link())
    monkeypatch.setattr(frames_mod, "FrameGrabber", Frames)
    monkeypatch.setattr(cv2, "namedWindow", lambda *a, **k: None)
    monkeypatch.setattr(cv2, "setMouseCallback", lambda win, cb: mouse.update(cb=cb))
    monkeypatch.setattr(cv2, "imshow", lambda *a: None)
    monkeypatch.setattr(cv2, "waitKey", wait_key)
    monkeypatch.setattr(cv2, "destroyAllWindows", lambda: None)
    out = tmp_path / "calibration.json"
    calibrate.main(["-n", "3", "--settle-ms", "0", "--out", str(out), "--pan-min", "40", "--pan-max", "100",
                    "--tilt-min", "32", "--tilt-max", "70"])           # every dot inside the 640x480 picture
    d = json.loads(out.read_text())
    assert d["frame"] == [640, 480] and len(d["points"]["px"]) == 9 + calibrate.VERIFY_N
    assert not link.laser
    back = Calibration.load(out)
    assert back.angles_for(cam(70, 50)) == pytest.approx((70, 50), abs=0.05)   # snapped clicks: exact


def test_closed_loop_carry_cancels_most_of_a_posture_change():
    """Calibrated with the counter 35 cm away, used at 42 cm (wearer stepped back): the first target is found with
    the dot, and the trim it needed brings the next target most of the way before any dot is seen."""
    cal_rig = quiet(Rig())
    px, ang = cal_rig.calibrate(GRID, np.random.default_rng(0), click_px=0.0, via_deg=3.0, lo=LO, hi=HI)
    calib = Calibration().fit(px, ang, model="plane", frame=cal_rig.frame)
    use = quiet(Rig(h=0.42))
    pts = use.targets(PAN, TILT, n=40, rng=np.random.default_rng(9))
    pix = use.project(pts)
    inner = [p for p in pix if 60 < p[0] < 260 and 20 < p[1] < 90]
    a, b = inner[0], inner[-1]
    box = lambda p: (p[0] - 6, p[1] - 6, p[0] + 6, p[1] + 6)
    st = State(RECIPE, dets=[Det("lime", 0.9, box(a)), Det("avocado", 0.9, box(b))])
    aimer = Aimer(SpyLink(), calib, st, closed_loop=True, limits=LIMITS, aim_y=0.5, frame_size=lambda: (320, 240))
    t = [0.0]
    aimer.clock = lambda: t[0]
    aimer.point_at("lime")
    for _ in range(300):
        t[0] += 1 / 30
        st.dot = tuple(use.dot_px((aimer.link.pan, aimer.link.tilt), move=False))
        aimer.tick()
        if aimer.on_target:
            break
    assert aimer.on_target and aimer.carry != (0.0, 0.0)

    def miss(trim):
        g = np.add(calib.angles_for(b), trim)
        return float(np.hypot(*(use.dot_px(g, move=False) - b)))
    assert miss(aimer.carry) < 0.4 * miss((0.0, 0.0)) and miss((0.0, 0.0)) > 5


def test_aimer_with_controllink_ramp_ends_every_move_the_same_way():
    """Against ControlLink's 2 deg/packet ramp (`moving`), retargeting mid-move: the servo output's last move is
    still pan-up / tilt-down, and on_target waits for the ramp + settle."""
    link = RampLink(90.0, 10.0)
    st = State(RECIPE, dets=[Det("lime", 0.9, (300, 40, 320, 60))])
    calib = Calibration().fit([(x, y) for x in (0, 160, 320, 480, 640) for y in (0, 120, 240, 360, 480)],
                              [(130 - x / 8, 25 - y / 24) for x in (0, 160, 320, 480, 640)
                               for y in (0, 120, 240, 360, 480)])
    t = [0.0]
    aimer = Aimer(link, calib, st, limits=(60, 140, 0, 30), aim_y=0.5, clock=lambda: t[0], frame_size=lambda: None)
    aimer.point_at("lime")
    for k in range(200):
        if k == 6:                                       # the lime is re-detected elsewhere while the servos move
            st.dets = [Det("lime", 0.9, (500, 300, 520, 320))]
        t[0] += 1 / 30
        aimer.tick()
        link.pump()
        if aimer.on_target:
            break
    assert aimer.on_target and not link.moving
    assert tuple(link.out) == pytest.approx(aimer.goal)
    moves = [(b[0] - a[0], b[1] - a[1]) for a, b in zip(link.trail, link.trail[1:]) if a != b]
    last_pan = next(d[0] for d in reversed(moves) if d[0] != 0)
    last_tilt = next(d[1] for d in reversed(moves) if d[1] != 0)
    assert last_pan > 0 and last_tilt < 0
