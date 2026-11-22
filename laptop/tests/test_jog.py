from coach.jog import Jog


def test_keys_move_clamp_and_quit():
    j = Jog()
    j.key("d"); j.key("w")
    assert (j.pan, j.tilt) == (92, 62)
    from coach.jog import TILT_LIM
    for _ in range(40):
        j.key("W")
    assert j.tilt == TILT_LIM[1]             # clamped to TILT_MAX (laptop/.env, default 180)
    j.key("c")
    assert (j.pan, j.tilt) == (90, 60)
    j.key("l")
    assert j.laser is False
    assert j.key("q") is False


def test_suggestion_from_marks():
    j = Jog()
    assert j.suggestion() is None
    for p, t in ((70, 60), (110, 60), (90, 40), (90, 80)):
        j.pan, j.tilt = p, t
        j.key("m")
    assert j.suggestion() == "python3 -m coach.calibrate --pan-min 73 --pan-max 107 --tilt-min 43 --tilt-max 77"


def test_predicted_dot_overlay():
    from coach.aim import Calibration
    from coach.jog import predicted_dot
    px = [(x, y) for x in (20, 100, 180, 260, 300) for y in (20, 60, 100, 140, 220)]
    calib = Calibration().fit(px, [(120 - x / 4, 40 - y / 8) for x, y in px], frame=(320, 240), rotate="180")
    assert predicted_dot(calib, 95.0, 30.0, (240, 320, 3), "180") == (100, 80)
    assert predicted_dot(calib, 95.0, 30.0, (480, 640, 3), "180") == (200, 160)     # camera at 640x480: rescaled
    assert predicted_dot(calib, 95.0, 30.0, (240, 320, 3), "0") is None             # rotation changed: no guess
    assert predicted_dot(calib, 10.0, 30.0, (240, 320, 3), "180") is None           # off-screen
    assert predicted_dot(None, 95.0, 30.0, (240, 320, 3), "180") is None
