"""Laser dot finder, blink check, DotTracker, closed loop end to end, and calibrate --auto, on synthetic frames."""
import asyncio
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from coach.aim import Aimer, Calibration
from coach.calibrate import MIN_SAVE_POINTS, auto_sweep, fit_and_save, make_grid
from coach.contracts import LAT_ON_TARGET, Det, FakeLink, Recipe, State, TurnLatency
from coach.dot import LAG_S, DotTracker, blink_check
from coach.vision import find_red_dot

RECIPE = Recipe.load(Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json")


# ── synthetic scenes ─────────────────────────────────────────────────────────

def background(rng, h=480, w=640):
    """Smooth random colour blobs (40..170 per channel) + sensor noise: a busy counter, nothing blown out."""
    low = rng.integers(0, 256, (h // 32 + 1, w // 32 + 1, 3)).astype(np.float32)
    img = cv2.resize(low, (w, h), interpolation=cv2.INTER_CUBIC)
    img = 40 + (img - img.min()) / (np.ptp(img) + 1e-6) * 130
    img += rng.normal(0, 6, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def add_dot(img, x, y, core=1.5, halo=4.5, red=420.0, white=320.0):
    """Laser dot as added light: a red Gaussian halo + a narrower white core (the sensor blows out in the middle)."""
    out = img.copy()
    r = int(4 * halo) + 2
    h, w = img.shape[:2]
    x0, x1, y0, y1 = max(int(x) - r, 0), min(int(x) + r + 1, w), max(int(y) - r, 0), min(int(y) + r + 1, h)
    if x0 >= x1 or y0 >= y1:
        return out                                  # off-screen
    yy, xx = np.mgrid[y0:y1, x0:x1]
    d2 = (xx - x) ** 2 + (yy - y) ** 2
    c = white * np.exp(-d2 / (2 * core ** 2))
    win = out[y0:y1, x0:x1].astype(np.float32)
    win[..., 2] += red * np.exp(-d2 / (2 * halo ** 2)) + c
    win[..., 1] += c
    win[..., 0] += c
    out[y0:y1, x0:x1] = np.clip(win, 0, 255).astype(np.uint8)
    return out


def add_tomato(img, x, y, r=60.0, highlight=False, rng=None):
    """Red disk, shaded (brightest top-left, R up to ~232), optionally with a small white specular highlight."""
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    inside = (xx - x) ** 2 + (yy - y) ** 2 <= r * r
    shade = np.clip(1 - np.hypot(xx - (x - r / 3), yy - (y - r / 3)) / (1.6 * r), 0, 1)
    out = img.astype(np.float32)
    out[..., 0][inside] = (25 + 20 * shade)[inside]
    out[..., 1][inside] = (30 + 25 * shade)[inside]
    out[..., 2][inside] = (170 + 62 * shade)[inside]
    if rng is not None:
        out[inside] += rng.normal(0, 5, (int(inside.sum()), 3))
    if highlight:
        out[(xx - (x - r / 3)) ** 2 / 16 + (yy - (y - r / 3)) ** 2 / 9 <= 1] = 252
    return np.clip(out, 0, 255).astype(np.uint8)


def highlight_xy(x, y, r=60.0):
    return x - r / 3, y - r / 3


def near(p, q, tol):
    return p is not None and np.hypot(p[0] - q[0], p[1] - q[1]) <= tol


# ── find_red_dot ─────────────────────────────────────────────────────────────

def test_finds_dot_on_textured_background():
    rng = np.random.default_rng(7)
    for _ in range(40):
        x, y = rng.uniform(15, 625), rng.uniform(15, 465)
        core = rng.uniform(0.9, 2.0)
        img = add_dot(background(rng), x, y, core=core, halo=core * rng.uniform(2.5, 3.5),
                      red=rng.uniform(350, 500), white=rng.uniform(280, 400))
        p = find_red_dot(img)
        assert near(p, (x, y), 3), (x, y, p)


def test_finds_all_red_dot():
    """No white core (dimmer laser / exposure): a saturated red spot much brighter than around it."""
    rng = np.random.default_rng(8)
    for _ in range(10):
        x, y = rng.uniform(15, 625), rng.uniform(15, 465)
        assert near(find_red_dot(add_dot(background(rng), x, y, halo=2.5, white=0)), (x, y), 3)


def test_nothing_on_plain_background():
    rng = np.random.default_rng(9)
    for _ in range(10):
        assert find_red_dot(background(rng)) is None
    assert find_red_dot(None) is None


def test_tomato_is_not_a_dot():
    rng = np.random.default_rng(10)
    for _ in range(20):
        img = add_tomato(background(rng), rng.uniform(100, 540), rng.uniform(100, 380), r=rng.uniform(30, 90), rng=rng)
        assert find_red_dot(img) is None


def test_big_bright_blob_is_not_a_dot():
    """A lamp / window / white board: hot but far too big."""
    img = background(np.random.default_rng(11))
    cv2.circle(img, (300, 200), 44, (60, 60, 220), 8)      # even with a red rim right around it
    cv2.circle(img, (300, 200), 40, (255, 255, 255), -1)
    assert find_red_dot(img) is None


def test_white_highlight_without_red_is_not_a_dot():
    """Small specular glints on a steel bowl: blown out but no red halo."""
    img = background(np.random.default_rng(12))
    cv2.circle(img, (200, 300), 60, (150, 150, 145), -1)
    for x, y in [(180, 280), (220, 320), (200, 260)]:
        cv2.circle(img, (x, y), 3, (255, 255, 255), -1)
    assert find_red_dot(img) is None


def test_dot_beside_and_on_tomato():
    rng = np.random.default_rng(13)
    for x, y in [(450, 120), (320, 260), (300, 230)]:          # beside, on the tomato, on the tomato
        img = add_dot(add_tomato(background(rng), 320, 240, rng=rng), x, y)
        assert near(find_red_dot(img), (x, y), 3)


def test_prefers_blob_near_last():
    img = add_dot(add_dot(background(np.random.default_rng(14)), 100, 100), 500, 380)
    assert near(find_red_dot(img, last=(110, 95)), (100, 100), 3)
    assert near(find_red_dot(img, last=(480, 400)), (500, 380), 3)


def test_large_frame_is_downscaled():
    rng = np.random.default_rng(15)
    img = add_dot(background(rng, 1080, 1920), 1500.0, 300.0, core=3, halo=9)
    assert near(find_red_dot(img), (1500, 300), 3)


def test_fast_enough():
    rng = np.random.default_rng(16)
    img = add_dot(add_tomato(background(rng), 320, 240, highlight=True, rng=rng), 100, 100)
    find_red_dot(img)
    times = []
    for _ in range(30):
        t0 = time.perf_counter()
        find_red_dot(img, last=(100, 100))
        times.append(time.perf_counter() - t0)
    assert np.median(times) < 0.005


# ── blink check ──────────────────────────────────────────────────────────────

@pytest.fixture
def scene():
    rng = np.random.default_rng(20)
    return add_tomato(background(rng), 320, 240, highlight=True, rng=rng)


def test_blink_confirms_real_dot(scene):
    on, off = add_dot(scene, 150, 380), scene
    assert near(blink_check(on, off), (150, 380), 3)
    assert near(blink_check(on, off, near=(140, 370)), (150, 380), 3)


def test_blink_confirms_dot_on_tomato(scene):
    on = add_dot(scene, 330, 250)
    assert near(blink_check(on, scene, near=(330, 250)), (330, 250), 3)


def test_blink_rejects_tomato_highlight(scene):
    hx, hy = highlight_xy(320, 240)
    assert near(find_red_dot(scene), (hx, hy), 3)        # the finder alone is fooled ...
    assert blink_check(scene, scene) is None             # ... the blink isn't: it doesn't go away
    on = add_dot(scene, 150, 380)                        # real dot elsewhere, but we asked about the highlight
    assert blink_check(on, scene, near=(hx, hy)) is None


def test_blink_rejects_when_laser_did_not_go_off(scene):
    on = add_dot(scene, 150, 380)
    assert blink_check(on, on) is None
    assert blink_check(on, add_dot(scene, 152, 381)) is None    # moved 2 px: still there
    assert blink_check(None, on) is None and blink_check(on, None) is None


def test_blink_rejects_when_no_dot(scene):
    bg = background(np.random.default_rng(21))
    assert blink_check(bg, bg) is None


# ── simulated rig: pan/tilt -> pixel camera model, rendered frames ───────────

class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def cal_map(x, y):
    """What the calibration believes: pan = 30 + x/8, tilt = 90 - y/8."""
    return 30 + x / 8, 90 - y / 8


def calib_from(fn):
    px = [(x, y) for x in np.linspace(20, 620, 5) for y in np.linspace(20, 460, 5)]
    return Calibration().fit(px, [fn(*p) for p in px])


def true_cam(pan, tilt):
    """Where the dot really lands: a bit off from the calibration (scale + offset + slight curve)."""
    x = 8.4 * (pan - 30) + 18 + 0.02 * (pan - 70) ** 2
    y = 7.8 * (90 - tilt) - 14
    return x, y


class SimFrames:
    """FrameSource that renders the scene + the laser dot where `cam` says the servos point it (no lag)."""

    def __init__(self, link, clock, bg, cam=true_cam, **dot):
        self.link, self.clock, self.bg, self.cam, self.dot = link, clock, bg, cam, dot
        self.connected = True
        self._cache = None

    def latest(self):
        key = (self.link.pan, self.link.tilt, bool(self.link.laser))
        if self._cache is None or self._cache[0] != key:
            f = self.bg
            if self.link.laser:
                f = add_dot(self.bg, *self.cam(self.link.pan, self.link.tilt), **self.dot)
            self._cache = (key, f)
        return self._cache[1], self.clock()

    def recent(self, n):
        return [self.latest()[0]]


def run(clock, n, *tickers, dt=1 / 30):
    for _ in range(n):
        clock.advance(dt)
        for t in tickers:
            t.tick()


@pytest.fixture
def sim():
    rng = np.random.default_rng(30)
    bg = add_tomato(background(rng), 450, 330, r=50, highlight=True, rng=rng)
    link, clock = FakeLink(), Clock()
    st = State(RECIPE, dets=[])
    frames = SimFrames(link, clock, bg)
    return link, clock, st, frames


# ── DotTracker ───────────────────────────────────────────────────────────────

def test_tracker_publishes_dot_only_after_blink(sim):
    link, clock, st, frames = sim
    tr = DotTracker(frames, link, st, clock=clock, want_laser=lambda: True)
    link.pan, link.tilt, link.laser = 60.0, 70.0, True
    truth = true_cam(60.0, 70.0)
    blinked = False
    for _ in range(60):
        run(clock, 1, tr)
        blinked |= not link.laser
        if st.dot is not None:
            break
        assert st.dot is None
    assert blinked and tr.blinks == 1 and tr.confirmed == 1
    assert link.laser and near(st.dot, truth, 3)
    run(clock, int(LAG_S * 30) + 1, tr)                    # frames show the laser on again
    assert near(st.dot, truth, 3)
    # small moves are tracked without another blink
    for k in range(5):
        link.pan += 1.0
        run(clock, 3, tr)
        assert link.laser and near(st.dot, true_cam(link.pan, link.tilt), 3)
    assert tr.blinks == 1
    # a big jump: untrusted until a new blink confirms it
    link.pan, link.tilt = 40.0, 50.0
    run(clock, 1, tr)
    assert st.dot is None
    run(clock, 40, tr)
    assert tr.blinks == 2 and near(st.dot, true_cam(40.0, 50.0), 3)


def test_tracker_rejects_tomato_highlight(sim):
    link, clock, st, frames = sim
    frames.cam = lambda pan, tilt: (-100.0, -100.0)        # laser points off-screen; only the highlight is visible
    tr = DotTracker(frames, link, st, clock=clock, want_laser=lambda: True)
    link.laser = True
    run(clock, 90, tr)                                     # 3 s: several blinks, none confirmed
    assert tr.blinks >= 2 and tr.confirmed == 0 and st.dot is None and link.laser


def test_tracker_laser_off_means_no_dot(sim):
    link, clock, st, frames = sim
    tr = DotTracker(frames, link, st, clock=clock)
    link.laser = True
    st.target = "lime"
    run(clock, 40, tr)
    assert st.dot is not None
    link.laser = False
    run(clock, 1, tr)
    assert st.dot is None
    run(clock, 30, tr)
    assert st.dot is None and not link.laser and tr.blinks == 1


def test_tracker_blink_respects_laser_off(sim):
    """laser_off() during a blink (State.target cleared): the tracker must not switch the laser back on."""
    link, clock, st, frames = sim
    tr = DotTracker(frames, link, st, clock=clock)
    st.target, link.laser = "lime", True
    for _ in range(40):
        run(clock, 1, tr)
        if not link.laser:
            break
    assert not link.laser and tr.blinks == 1               # mid-blink
    st.target = None                                       # Aimer.laser_off()
    run(clock, 30, tr)
    assert not link.laser and st.dot is None


# ── closed loop end to end: rendered frames -> DotTracker -> Aimer ───────────

def lime_rig(sim, closed_loop):
    link, clock, st, frames = sim
    st.dets = [Det("lime", 0.9, (240, 140, 300, 190))]     # centre (270, 165)
    st.turns.append(TurnLatency(1))
    aimer = Aimer(link, calib_from(cal_map), st, clock=clock, closed_loop=closed_loop)
    tracker = DotTracker(frames, link, st, clock=clock)
    return aimer, tracker, link, clock, st


@pytest.mark.parametrize("closed_loop", [False, True])
def test_closed_loop_end_to_end(sim, closed_loop):
    aimer, tracker, link, clock, st = lime_rig(sim, closed_loop)
    target = st.det("lime").center
    assert aimer.point_at("lime") == "ok"
    t0 = clock()
    on_at = None
    for _ in range(120):                                   # 4 s
        run(clock, 1, tracker, aimer)
        if aimer.on_target and on_at is None:
            on_at = clock() - t0
    dot = true_cam(link.pan, link.tilt)                    # where the dot really is now
    err = np.hypot(dot[0] - target[0], dot[1] - target[1])
    assert aimer.on_target and st.on_target and link.laser
    if closed_loop:
        assert err <= Aimer.CLOSE_PX and tracker.confirmed >= 1
        assert on_at < 2.0
        assert LAT_ON_TARGET in st.turns[-1].marks
        assert aimer.trim != (0.0, 0.0)
    else:
        assert err > 20                                    # open loop: the calibration is off for this camera
        assert aimer.trim == (0.0, 0.0) and LAT_ON_TARGET not in st.turns[-1].marks


# ── calibrate --auto ─────────────────────────────────────────────────────────

def true_angles(x, y):
    """Inverse of true_cam's linear part, used to check the fitted calibration."""
    return 30 + (x - 18) / 8.4, 90 - (y + 14) / 7.8


def sweep(frames, link, clock, grid):
    async def fake_sleep(dt):
        clock.advance(dt)
        await asyncio.sleep(0)
    lines = []
    sess = asyncio.run(auto_sweep(link, frames, grid, sleep=fake_sleep, log=lines.append))
    return sess, lines


def test_auto_calibration_sweep(sim, tmp_path):
    link, clock, st, frames = sim
    frames.cam = lambda pan, tilt: (8.4 * (pan - 30) + 18, 7.8 * (90 - tilt) - 14)
    grid = make_grid((45.0, 95.0), (35.0, 85.0), 4)
    sess, lines = sweep(frames, link, clock, grid)
    assert sess.done and sess.n_points == 16 and len(lines) == 16
    pixels, angles = sess.points()
    for (x, y), (pan, tilt) in zip(pixels, angles):
        assert near((x, y), frames.cam(pan, tilt), 3)
    calib, report, worst = fit_and_save(pixels, angles, tmp_path / "c.json")
    assert worst < 0.5 and len(report) == 16
    for p in [(150, 150), (320, 240), (500, 330)]:
        assert np.allclose(calib.angles_for(p), true_angles(*p), atol=0.5)


def test_auto_calibration_skips_points_without_dot(sim, tmp_path):
    link, clock, st, frames = sim
    cam = lambda pan, tilt: (8.4 * (pan - 30) + 18, 7.8 * (90 - tilt) - 14)
    frames.cam = lambda pan, tilt: (-50.0, -50.0) if pan > 90 else cam(pan, tilt)   # right column off-screen
    grid = make_grid((45.0, 95.0), (35.0, 85.0), 4)
    sess, lines = sweep(frames, link, clock, grid)
    assert sess.n_points == 12 and sum("skipped" in ln for ln in lines) == 4
    assert sess.can_save()
    fit_and_save(*sess.points(), tmp_path / "c.json")

    frames.cam = lambda pan, tilt: (-50.0, -50.0) if pan > 70 else cam(pan, tilt)   # only 8 points visible
    sess, _ = sweep(frames, link, clock, grid)
    assert sess.n_points == 8 < MIN_SAVE_POINTS and not sess.can_save()
    with pytest.raises(ValueError):
        fit_and_save(*sess.points(), tmp_path / "d.json")
