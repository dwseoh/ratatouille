"""Calibration + Aimer + calibrate.py logic. No hardware: FakeLink, fake_dets, injected clock."""
import asyncio
from pathlib import Path

import numpy as np
import pytest

from coach.aim import Aimer, Calibration
from coach.calibrate import CalibSession, fit_and_save, make_grid
from coach.contracts import LASER_IDLE_OFF_S, TFT_STATUS, AimerAPI, Det, FakeLink, Recipe, State, fake_dets
from coach.contracts import LAT_ON_TARGET, TurnLatency

RECIPE = Recipe.load(Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json")


def true_map(x, y):
    """A made-up but realistic pixel -> angle mapping (slight curvature + cross term)."""
    pan = 120 - 0.09 * x + 2e-5 * x * x + 1e-6 * x * y
    tilt = 95 - 0.1 * y + 3e-5 * y * y - 2e-6 * x * x
    return pan, tilt


def grid_pixels(n=5):
    return [(x, y) for x in np.linspace(20, 620, n) for y in np.linspace(20, 460, n)]


# ── Calibration ──────────────────────────────────────────────────────────────

def test_fit_recovers_known_mapping():
    px = grid_pixels()
    calib = Calibration().fit(px, [true_map(*p) for p in px])
    for p in [(100, 100), (320, 240), (555, 401), (37, 450)]:
        assert np.allclose(calib.angles_for(p), true_map(*p), atol=1e-6)
    lines, worst = calib.report(px, [true_map(*p) for p in px])
    assert len(lines) == len(px) and worst < 1e-6 and "WARN" not in "".join(lines)


def test_fit_with_noise_small_error_and_warning():
    rng = np.random.default_rng(0)
    px = grid_pixels(4)
    angles = [np.add(true_map(*p), rng.normal(0, 0.1, 2)) for p in px]
    calib = Calibration().fit(px, angles)
    assert np.allclose(calib.angles_for((320, 240)), true_map(320, 240), atol=0.5)
    angles[3] = angles[3] + np.array([15.0, 0.0])   # one bad click
    calib.fit(px, angles)
    lines, worst = calib.report(px, angles)
    assert worst > 2 and any("WARN" in ln for ln in lines)


def test_fit_needs_six_points():
    px = grid_pixels(2)[:4] + [(300, 300)]
    with pytest.raises(ValueError):
        Calibration().fit(px, [true_map(*p) for p in px])


def test_save_load_roundtrip(tmp_path):
    px = grid_pixels()
    calib = Calibration().fit(px, [true_map(*p) for p in px])
    f = tmp_path / "calibration.json"
    calib.save(f)
    back = Calibration.load(f)
    assert np.allclose(back.coef, calib.coef)
    assert back.angles_for((200, 300)) == pytest.approx(calib.angles_for((200, 300)))


# ── Aimer ────────────────────────────────────────────────────────────────────

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class SpyLink(FakeLink):
    def __init__(self):
        super().__init__()
        self.texts = []

    def show_text(self, line, text):
        self.texts.append((line, text))


def linear_calib():
    """pan = 30 + x/8 (x 0..640 -> 30..110), tilt = 90 - y/8."""
    px = grid_pixels()
    return Calibration().fit(px, [(30 + x / 8, 90 - y / 8) for x, y in px])


def box_at(name, cx, cy):
    return Det(name, 0.9, (cx - 10, cy - 10, cx + 10, cy + 10))


LIMITS = (10.0, 170.0, 20.0, 100.0)     # explicit limits so the clamp tests don't depend on laptop/.env


def test_limits_default_to_config_full_range():
    a = Aimer(SpyLink(), None, State(RECIPE, dets=fake_dets()))
    from coach.config import CONFIG
    assert (a.PAN_MIN, a.PAN_MAX, a.TILT_MIN, a.TILT_MAX) == (CONFIG.pan_min, CONFIG.pan_max, CONFIG.tilt_min, CONFIG.tilt_max)


@pytest.fixture
def rig():
    link, clock = SpyLink(), Clock()
    st = State(RECIPE, dets=fake_dets())
    aimer = Aimer(link, linear_calib(), st, clock=clock, limits=LIMITS, aim_y=0.5)   # aim at the box centre
    return aimer, link, st, clock


def run_ticks(aimer, clock, n, dt=1 / 30):
    for _ in range(n):
        clock.advance(dt)
        aimer.tick()


def test_is_aimer_api(rig):
    assert isinstance(rig[0], AimerAPI)


def test_point_at_results(rig):
    aimer, link, st, _ = rig
    assert aimer.point_at("spaceship") == "unknown_object"
    assert aimer.point_at("lime") == "ok"
    assert link.laser and aimer.target == st.target == "lime" and not aimer.on_target
    assert aimer.point_at("salt") == "not_visible"          # recipe object, not in fake_dets
    assert not link.laser and aimer.target is None and st.target is None and not st.on_target


def test_reaches_goal_with_rate_limit(rig):
    aimer, link, st, clock = rig
    det = st.det("lime")
    goal = aimer.calib.angles_for(det.center)
    assert aimer.point_at("lime") == "ok"
    prev = (link.pan, link.tilt)
    for _ in range(60):
        run_ticks(aimer, clock, 1)
        assert abs(link.pan - prev[0]) <= 4.0 + 1e-9 and abs(link.tilt - prev[1]) <= 4.0 + 1e-9
        prev = (link.pan, link.tilt)
        if aimer.on_target:
            break
    assert aimer.on_target and st.on_target
    assert link.pan == pytest.approx(goal[0], abs=0.5) and link.tilt == pytest.approx(goal[1], abs=0.5)


def test_deadband(rig):
    aimer, link, st, clock = rig
    aimer.point_at("lime")
    run_ticks(aimer, clock, 60)
    assert aimer.on_target
    pan, tilt = link.pan, link.tilt
    lime = st.det("lime")
    cx, cy = lime.center
    st.dets = [d for d in st.dets if d.name != "lime"] + [box_at("lime", cx + 2, cy + 2)]  # 0.25 deg move
    run_ticks(aimer, clock, 10)
    assert (link.pan, link.tilt) == (pan, tilt) and aimer.on_target


def test_smoothing_median_of_new_detections_and_jumps(rig):
    aimer, link, st, clock = rig
    aimer.point_at("lime")
    others = [d for d in st.dets if d.name != "lime"]
    for cx in (300, 304, 302, 314):                         # YOLO jitter, then one box 12 px off
        st.dets = others + [box_at("lime", cx, 240)]
        run_ticks(aimer, clock, 3)                          # the same box for 3 ticks counts once
    assert aimer.goal[0] == pytest.approx(30 + 304 / 8)     # median of the last 3 boxes (304, 302, 314)
    st.dets = others + [box_at("lime", 600, 240)]           # the lime moved: start over, don't average across
    run_ticks(aimer, clock, 1)
    assert aimer.goal[0] == pytest.approx(30 + 600 / 8)


def test_sticks_to_the_tracked_instance(rig):
    aimer, link, st, clock = rig
    others = [d for d in st.dets if d.name != "lime"]
    st.dets = others + [Det("lime", 0.9, (290, 230, 310, 250))]
    aimer.point_at("lime")
    run_ticks(aimer, clock, 2)
    # a second lime shows up with a higher confidence: keep pointing at the first one
    st.dets = others + [Det("lime", 0.8, (292, 230, 312, 250)), Det("lime", 0.95, (490, 230, 510, 250))]
    run_ticks(aimer, clock, 60)
    assert aimer.goal[0] == pytest.approx(30 + 301 / 8, abs=0.2) and aimer.on_target


def test_out_of_reach_is_reported_and_laser_stays_off(rig):
    aimer, link, st, clock = rig
    aimer.calib = Calibration().fit(grid_pixels(), [(200.0, 60.0)] * 25)   # everything maps to pan 200 (limit 170)
    assert aimer.point_at("lime") == "out_of_reach"
    assert not link.laser and aimer.target is None and not aimer.on_target and not st.on_target
    run_ticks(aimer, clock, 30)
    assert (link.pan, link.tilt) == (90.0, 60.0)


def test_bad_fit_aiming_up_never_commanded(rig):
    aimer, link, st, clock = rig
    aimer.calib = Calibration().fit(grid_pixels(), [(90.0, 150.0)] * 25)   # a bad fit: tilt 150 (aims up)
    assert aimer.point_at("lime") == "out_of_reach"
    run_ticks(aimer, clock, 60)
    assert link.tilt <= aimer.TILT_MAX and not link.laser and not aimer.on_target


def test_just_past_a_limit_is_clamped_and_still_on_target(rig):
    aimer, link, st, clock = rig
    aimer.calib = Calibration().fit(grid_pixels(), [(170.6, 60.0)] * 25)   # within REACH_TOL_DEG of PAN_MAX
    assert aimer.point_at("lime") == "ok"
    run_ticks(aimer, clock, 60)
    assert link.pan == pytest.approx(170.0) and aimer.on_target and link.laser


def test_half_reachable_box_gets_the_dot_on_its_reachable_part(rig):
    aimer, link, st, clock = rig
    others = [d for d in st.dets if d.name != "lime"]
    st.dets = others + [Det("lime", 0.9, (300, 540, 340, 640))]    # tilt = 90 - y/8: 22.5 .. 10 (limit 20)
    assert aimer.point_at("lime") == "ok"
    run_ticks(aimer, clock, 60)
    assert aimer.on_target and link.laser and link.tilt >= aimer.TILT_MIN - 1e-9
    assert 540 < aimer.aim_px[1] <= (90 - aimer.TILT_MIN + aimer.REACH_TOL_DEG) * 8   # in the box, reachable part


def test_target_moving_out_of_reach_and_back(rig):
    aimer, link, st, clock = rig
    others = [d for d in st.dets if d.name != "lime"]
    aimer.point_at("lime")
    run_ticks(aimer, clock, 60)
    assert aimer.on_target and link.laser
    st.dets = others + [box_at("lime", 320, 700)]          # tilt 2.5: below TILT_MIN 20
    run_ticks(aimer, clock, 2)
    assert not link.laser and not aimer.on_target and aimer.target == "lime" and not aimer.reachable
    st.dets = others + [box_at("lime", 320, 300)]
    run_ticks(aimer, clock, 60)
    assert link.laser and aimer.on_target and aimer.reachable


def test_final_approach_is_always_from_below(rig):
    aimer, link, st, clock = rig
    others = [d for d in st.dets if d.name != "lime"]
    for x, y in ((500, 100), (200, 300), (400, 200), (100, 50)):
        st.dets = others + [box_at("lime", x, y)]
        aimer.point_at("lime")
        trail = [(link.pan, link.tilt)]
        for _ in range(80):
            run_ticks(aimer, clock, 1)
            trail.append((link.pan, link.tilt))
            if aimer.on_target:
                break
        assert aimer.on_target
        moves = [(b[0] - a[0], b[1] - a[1]) for a, b in zip(trail, trail[1:]) if a != b]
        assert moves and all(d >= 0 for d in moves[-1])   # the last move goes up on every axis that moved
        assert (link.pan, link.tilt) == pytest.approx(aimer.goal)


def test_follows_moving_target(rig):
    aimer, link, st, clock = rig
    aimer.point_at("lime")
    run_ticks(aimer, clock, 60)
    others = [d for d in st.dets if d.name != "lime"]
    st.dets = others + [box_at("lime", 600, 400)]
    run_ticks(aimer, clock, 1)
    assert not aimer.on_target
    run_ticks(aimer, clock, 60)
    assert aimer.on_target
    assert link.pan == pytest.approx(30 + 600 / 8, abs=0.5) and link.tilt == pytest.approx(90 - 400 / 8, abs=0.5)


def test_lost_target_holds_then_turns_off(rig):
    aimer, link, st, clock = rig
    aimer.point_at("lime")
    run_ticks(aimer, clock, 3)                  # partway to the goal
    st.dets = [d for d in st.dets if d.name != "lime"]
    run_ticks(aimer, clock, 1, dt=0.5)          # 0.5 s lost: still moving towards the last goal
    clock.advance(0.6)                          # 1.1 s lost
    aimer.tick()
    pan, tilt = link.pan, link.tilt
    run_ticks(aimer, clock, 40, dt=0.04)        # 2.7 s lost: holding
    assert (link.pan, link.tilt) == (pan, tilt) and link.laser and aimer.target == "lime"
    assert not aimer.on_target
    run_ticks(aimer, clock, 10, dt=0.04)        # > 3 s lost
    assert not link.laser and aimer.target is None and st.target is None


def test_idle_laser_off(rig):
    aimer, link, st, clock = rig
    aimer.point_at("lime")
    run_ticks(aimer, clock, 5, dt=(LASER_IDLE_OFF_S - 1) / 5)
    assert link.laser and aimer.target == "lime"
    assert aimer.point_at("avocado") == "ok"     # a new point_at resets the idle timer
    run_ticks(aimer, clock, 5, dt=(LASER_IDLE_OFF_S - 1) / 5)
    assert link.laser
    run_ticks(aimer, clock, 2, dt=1.0)
    assert not link.laser and aimer.target is None and st.target is None


def test_no_calibration(rig):
    _, link, st, clock = rig
    aimer = Aimer(link, None, st, clock=clock)
    pan, tilt = link.pan, link.tilt
    assert aimer.point_at("lime") == "ok"
    assert any(line == TFT_STATUS for line, _ in link.texts)
    run_ticks(aimer, clock, 30)
    assert (link.pan, link.tilt) == (pan, tilt) and not aimer.on_target
    assert not link.laser            # never shine an uncalibrated laser


@pytest.mark.timeout(5)
def test_run_loop_does_not_block():
    async def main():
        link = SpyLink()
        st = State(RECIPE, dets=fake_dets())
        aimer = Aimer(link, linear_calib(), st)
        task = asyncio.create_task(aimer.run())
        aimer.point_at("lime")
        ticks = 0
        for _ in range(50):                       # the loop stays responsive while the aimer runs
            await asyncio.sleep(0.01)
            ticks += 1
            if aimer.on_target:
                break
        task.cancel()
        return aimer.on_target, ticks
    on_target, ticks = asyncio.run(main())
    assert on_target and ticks < 50


# ── calibrate.py logic ───────────────────────────────────────────────────────

def test_make_grid():
    g = make_grid((60, 120), (30, 90), 4)
    assert len(g) == 16 and len(set(g)) == 16
    assert {p for p, _ in g} == {60, 80, 100, 120} and {t for _, t in g} == {30, 50, 70, 90}
    assert g[3] == (120, 30) and g[4] == (120, 50)   # serpentine


def test_session_click_skip_redo():
    s = CalibSession(make_grid(n=4))
    s.click(10, 20)
    s.skip()
    s.click(30, 40)
    s.redo()                                   # forget (30, 40), back to point 2
    assert s.i == 2 and s.current() == s.grid[2] and s.n_points == 1
    s.click(50, 60)
    px, ang = s.points()
    assert px == [(10, 20), (50, 60)] and ang == [s.grid[0], s.grid[2]]
    s.redo(); s.redo(); s.redo(); s.redo()     # can't go before the start
    assert s.i == 0 and s.results == []


def test_session_done_and_save_gate(tmp_path):
    s = CalibSession(make_grid(n=4))
    for k in range(16):
        pan, tilt = s.current()
        if k < 8:
            s.click(pan * 8 - 240, (90 - tilt) * 8)   # inverse of linear_calib
        else:
            s.skip()
    assert s.done and s.current() is None and not s.can_save()
    with pytest.raises(ValueError):
        fit_and_save(*s.points(), tmp_path / "c.json")
    assert not (tmp_path / "c.json").exists()

    s = CalibSession(make_grid(n=4))
    while not s.done:
        pan, tilt = s.current()
        s.click(pan * 8 - 240, (90 - tilt) * 8)
    assert s.can_save()
    calib, lines, worst = fit_and_save(*s.points(), tmp_path / "c.json")
    assert len(lines) == 16 and worst < 1e-6
    assert Calibration.load(tmp_path / "c.json").angles_for((320, 240)) == pytest.approx((70, 60))


# ── closed loop (stretch, Aimer(closed_loop=True)) ───────────────────────────

def cam_px(pan, tilt):
    """Where the dot really lands: linear_calib's inverse with a scale + offset error, so open loop misses."""
    return 8.5 * (pan - 30) + 20, 7.6 * (90 - tilt) - 15


def cl_ticks(aimer, clock, n, cam=cam_px, dt=1 / 30):
    """Ticks with a perfect, instant dot finder: State.dot = where the dot really is."""
    for _ in range(n):
        clock.advance(dt)
        aimer.state.dot = cam(aimer.link.pan, aimer.link.tilt) if aimer.link.laser else None
        aimer.tick()


def dot_err(aimer, name="lime", cam=cam_px):
    tx, ty = aimer.state.det(name).center
    x, y = cam(aimer.link.pan, aimer.link.tilt)
    return float(np.hypot(x - tx, y - ty))


@pytest.fixture
def cl_rig():
    link, clock = SpyLink(), Clock()
    st = State(RECIPE, dets=fake_dets())
    return Aimer(link, linear_calib(), st, clock=clock, closed_loop=True, limits=LIMITS, aim_y=0.5), link, st, clock


def test_closed_loop_off_by_default(rig):
    aimer, link, st, clock = rig
    assert aimer.closed_loop is False
    aimer.point_at("lime")
    cl_ticks(aimer, clock, 60, cam=lambda p, t: (0.0, 0.0))    # a dot far off changes nothing in open loop
    assert aimer.on_target and aimer.trim == (0.0, 0.0)
    assert (link.pan, link.tilt) == pytest.approx(aimer.calib.angles_for(st.det("lime").center), abs=0.5)


def test_closed_loop_converges(rig, cl_rig):
    ol, _, _, ol_clock = rig
    ol.point_at("lime")
    cl_ticks(ol, ol_clock, 60)
    assert ol.on_target and dot_err(ol) > 30                  # open loop says on target, but it's ~44 px off

    aimer, link, st, clock = cl_rig
    st.turns.append(TurnLatency(1))
    aimer.point_at("lime")
    t0 = clock()
    while not aimer.on_target and clock() - t0 < 3:
        cl_ticks(aimer, clock, 1)
        assert not aimer.on_target or dot_err(aimer) <= Aimer.CLOSE_PX   # never "on target" while really off
    assert aimer.on_target and st.on_target and dot_err(aimer) <= Aimer.CLOSE_PX
    assert clock() - t0 < 2.0
    assert LAT_ON_TARGET in st.turns[-1].marks
    pan, tilt = link.pan, link.tilt
    cl_ticks(aimer, clock, 60)                                  # stays put, no hunting
    assert (link.pan, link.tilt) == (pan, tilt) and aimer.on_target


def test_closed_loop_follows_moving_target(cl_rig):
    aimer, link, st, clock = cl_rig
    aimer.point_at("lime")
    cl_ticks(aimer, clock, 60)
    assert aimer.on_target
    st.dets = [d for d in st.dets if d.name != "lime"] + [box_at("lime", 450, 300)]
    cl_ticks(aimer, clock, 90)
    assert aimer.on_target and dot_err(aimer) <= Aimer.CLOSE_PX


def test_closed_loop_flips_a_mirrored_axis(cl_rig):
    aimer, link, st, clock = cl_rig
    mirrored = lambda pan, tilt: (560 - 8 * (pan - 30), 8 * (90 - tilt) - 6)   # x runs the other way
    aimer.point_at("lime")
    cl_ticks(aimer, clock, 120, cam=mirrored)
    assert aimer._sign == [-1.0, 1.0]
    assert aimer.on_target and dot_err(aimer, cam=mirrored) <= Aimer.CLOSE_PX


def test_closed_loop_trim_limited_and_pan_clamped(cl_rig):
    aimer, link, st, clock = cl_rig
    px = grid_pixels()
    aimer.calib = Calibration().fit(px, [(131.25 + x / 8, 90 - y / 8) for x, y in px])   # lime -> pan 165
    aimer.point_at("lime")
    prev = (link.pan, link.tilt)
    for _ in range(150):
        cl_ticks(aimer, clock, 1, cam=lambda p, t: (0.0, 0.0))    # a bad detection stuck in the corner
        assert abs(link.pan - prev[0]) <= 4.0 + 1e-9 and abs(link.tilt - prev[1]) <= 4.0 + 1e-9
        assert aimer.PAN_MIN <= link.pan <= aimer.PAN_MAX
        prev = (link.pan, link.tilt)
    assert aimer.trim == pytest.approx((Aimer.CL_MAX_TRIM_DEG, -Aimer.CL_MAX_TRIM_DEG))
    assert link.pan == pytest.approx(170.0) and not aimer.on_target


def test_closed_loop_without_dot_falls_back_to_open_loop(cl_rig):
    aimer, link, st, clock = cl_rig
    aimer.point_at("lime")
    run_ticks(aimer, clock, 20)                 # servos there, no dot seen yet: still looking
    assert not aimer.on_target and st.dot is None
    run_ticks(aimer, clock, 40)                 # no dot for > CL_DOT_TIMEOUT_S: trust the calibration
    assert aimer.on_target and aimer.trim == (0.0, 0.0)
    assert (link.pan, link.tilt) == pytest.approx(aimer.calib.angles_for(st.det("lime").center), abs=0.5)


def test_closed_loop_trim_resets_on_new_target_and_laser_off(cl_rig):
    aimer, link, st, clock = cl_rig
    aimer.point_at("lime")
    cl_ticks(aimer, clock, 60)
    trim = aimer.trim
    assert aimer.on_target and trim != (0.0, 0.0)
    aimer.point_at("lime")                      # asked again: same object, keep what we learned
    assert aimer.trim == trim
    aimer.point_at("avocado")
    assert aimer.trim == (0.0, 0.0)
    cl_ticks(aimer, clock, 60)
    assert aimer.on_target and dot_err(aimer, "avocado") <= Aimer.CLOSE_PX
    aimer.laser_off()
    assert aimer.trim == (0.0, 0.0) and not aimer.on_target
