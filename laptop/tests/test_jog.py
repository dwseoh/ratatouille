from coach.jog import Jog


def test_keys_move_clamp_and_quit():
    j = Jog()
    j.key("d"); j.key("w")
    assert (j.pan, j.tilt) == (92, 62)
    for _ in range(20):
        j.key("W")
    assert j.tilt == 100                     # firmware clamp
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
