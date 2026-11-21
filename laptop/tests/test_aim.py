"""Calibration + Aimer + calibrate.py logic. No hardware: FakeLink, fake_dets, injected clock."""
import asyncio
from pathlib import Path

import numpy as np
import pytest

from coach.aim import Aimer, Calibration
from coach.calibrate import CalibSession, fit_and_save, make_grid
from coach.contracts import LASER_IDLE_OFF_S, TFT_STATUS, AimerAPI, Det, FakeLink, Recipe, State, fake_dets

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


@pytest.fixture
def rig():
    link, clock = SpyLink(), Clock()
    st = State(RECIPE, dets=fake_dets())
    aimer = Aimer(link, linear_calib(), st, clock=clock)
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


def test_smoothing_mean_of_last_three(rig):
    aimer, link, st, clock = rig
    aimer.point_at("lime")
    others = [d for d in st.dets if d.name != "lime"]
    for cx in (100, 200, 600):
        st.dets = others + [box_at("lime", cx, 240)]
        run_ticks(aimer, clock, 1)
    assert aimer.goal[0] == pytest.approx(30 + 300 / 8)     # mean x = 300


def test_clamp_pan(rig):
    aimer, link, st, clock = rig
    aimer.calib = Calibration().fit(grid_pixels(), [(200.0, 60.0)] * 25)   # everything maps to pan 200
    aimer.point_at("lime")
    run_ticks(aimer, clock, 60)
    assert link.pan == pytest.approx(170.0) and aimer.on_target


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
