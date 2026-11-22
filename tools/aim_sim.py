"""Aiming error budget by simulation: each error source on its own, then all together, polynomial vs plane model.

    cd laptop && python ../tools/aim_sim.py            # ~1 min, prints the tables used in docs/aiming.md
    python ../tools/aim_sim.py --quick                 # fewer targets / repeats

Synthetic truth: coach/aim_sim.py's Rig (the real box: OV3660 at 320x240 pitched ~45 deg down, counter ~35 cm
below the lens, laser pivot 12 cm above it, SG90s with gain/offset errors, nonlinearity and backlash). Each row
calibrates on a simulated 5x5 click grid over pan 75-125 / tilt 3-17 (inside the jog range 72-128 / 0-20), then
aims at random counter points anywhere the laser can reach and measures where the dot really lands.
Errors: degrees at the laser and cm on the counter (where the dot lands vs where it should), mean / max.
"""
from __future__ import annotations

import argparse
import copy
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "laptop"))
from coach.aim_model import PolyModel, fit_plane, robust_weights  # noqa: E402
from coach.aim_sim import Rig, aim_error  # noqa: E402
from coach.calibrate import make_grid  # noqa: E402

PAN, TILT = (72.0, 128.0), (0.0, 20.0)          # what the laser can reach (jog)
GRID = make_grid((75.0, 125.0), (3.0, 17.0), 5)
LO, HI = (65.0, 0.0), (135.0, 25.0)            # PAN_*/TILT_* safety limits in .env (bringup.md)
MED5 = 1.2533 / math.sqrt(5)                    # std of the median of 5 noisy boxes, relative to one box


def ideal(**kw) -> Rig:
    """A rig with every error source off: the baseline each row adds one source to."""
    r = Rig(k1=0.0, k2=0.0, pp=(0.0, 0.0), yaw=0.0, roll=0.0, tilt_off=0.0, beam_off=0.0, cone=0.0, skew=0.0)
    for s in (r.pan, r.tilt):
        s.nl_deg, s.play_deg, s.noise_deg = 0.0, 0.0, 0.0
    for k, v in kw.items():
        setattr(r, k, v)
    r.scale(r.w)
    return r


def real(**kw) -> Rig:
    r = Rig()
    for k, v in kw.items():
        setattr(r, k, v)
    r.scale(r.w)
    return r


def fit_models(px, ang, frame, robust=False):
    w = np.ones(len(px))
    poly = PolyModel().fit(px, ang)
    if robust:
        for _ in range(2):
            w = robust_weights(poly.predict(px) - ang)
            poly = PolyModel().fit(px, ang, w)
    plane, _ = fit_plane(px, ang, frame)
    if robust:
        for _ in range(2):
            w = robust_weights(plane.predict(px) - ang)
            plane, _ = fit_plane(px, ang, frame, start=plane, weights=w)
    return {"poly": poly, "plane": plane}


def run(cal: Rig, use: Rig | None = None, grid=GRID, click_px=0.0, cal_via=3.0, run_via=3.0, n=300, seed=0,
        height=0.0, yolo_px=0.0, sphere=None, aim_y=0.5, misclick=None, robust=True, region="all",
        px_map=None) -> dict:
    """Calibrate on `cal`, aim on `use` (default: the same rig). Returns {model: (deg array, cm array)}."""
    rng = np.random.default_rng(seed)
    use = use if use is not None else cal
    px, ang = cal.calibrate(grid, rng, click_px=click_px, via_deg=cal_via, lo=LO, hi=HI)
    if misclick is not None:
        px = px.copy()
        px[len(px) // 2] += misclick
    models = fit_models(px, ang, cal.frame, robust=robust)
    pts = use.targets(PAN, TILT, n=n, rng=np.random.default_rng(seed + 1))
    if region != "all":
        cmd = np.array([use.ideal_cmd(p) for p in pts])
        inside = ((cmd[:, 0] >= 75) & (cmd[:, 0] <= 125) & (cmd[:, 1] >= 3) & (cmd[:, 1] <= 17))
        pts = pts[inside if region == "inside" else ~inside]
    if sphere is not None or height:    # targets above the counter: keep the ones the laser can reach
        lift = sphere if sphere is not None else height
        cmd = np.array([use.ideal_cmd(p - use.n * lift) for p in pts])
        pts = pts[(cmd[:, 0] >= PAN[0]) & (cmd[:, 0] <= PAN[1]) & (cmd[:, 1] >= TILT[0]) & (cmd[:, 1] <= TILT[1])]
    if sphere is not None:          # fruit of radius `sphere` resting on the counter: aim at its YOLO box
        centres = pts - use.n * sphere
        u = np.random.default_rng(3).normal(size=(600, 3))
        u /= np.linalg.norm(u, axis=1, keepdims=True)
        boxes = np.array([np.r_[b.min(0), b.max(0)] for b in (use.project(c + sphere * u) for c in centres)])
        tp = np.c_[(boxes[:, 0] + boxes[:, 2]) / 2, boxes[:, 1] + aim_y * (boxes[:, 3] - boxes[:, 1])]
        targets, target_h = centres, sphere
    else:
        targets, target_h = pts - use.n * height, height
        tp = use.project(targets)
    if yolo_px:
        tp = tp + np.random.default_rng(seed + 2).normal(0, yolo_px, tp.shape)
    if px_map is not None:
        tp = px_map(tp)
    out = {}
    for name, m in models.items():
        cmds = m.predict(tp)
        cmds = np.c_[np.clip(cmds[:, 0], *PAN), np.clip(cmds[:, 1], *TILT)]
        ok = np.all(np.isfinite(cmds), axis=1)
        use_r = copy.deepcopy(use)
        deg, cm = aim_error(use_r, targets[ok], cmds[ok], rng=np.random.default_rng(seed + 5), via_deg=run_via,
                            lo=LO, hi=HI, height=target_h)
        out[name] = (deg, cm)
    return out


def fmt(res: dict) -> str:
    cells = []
    for name in ("poly", "plane"):
        deg, cm = res[name]
        cells.append(f"{deg.mean():5.2f} /{deg.max():5.2f}  {cm.mean():4.1f} /{cm.max():5.1f}")
    return " | ".join(cells)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args(argv)
    n = 120 if args.quick else 300
    print(__doc__.split("\n\n")[1].strip())
    print()
    head = f"{'scenario':58s} | {'poly: deg mean/max  cm mean/max':31s} | plane: deg mean/max  cm mean/max"
    print(head)
    print("-" * len(head))

    def row(label, **kw):
        print(f"{label:58s} | {fmt(run(n=n, **kw))}", flush=True)

    print("# one source at a time (everything else ideal)")
    row("ideal rig (pinhole, perfect servos)", cal=ideal())
    row("lens distortion + principal point offset", cal=ideal(k1=-0.10, k2=0.02, pp=(5.0, -4.0)))
    row("pan-tilt geometry: axis offsets, skew, beam cone, cam yaw", cal=ideal(tilt_off=0.012, beam_off=0.008,
                                                                             skew=0.8, cone=1.0, yaw=2.0))
    r = ideal()
    r.pan.nl_deg, r.tilt.nl_deg = 0.6, 0.5
    row("servo nonlinearity (+-0.6 deg)", cal=r)
    row("model mismatch: all of the above (no noise)", cal=_quiet(real()))
    row("  same, targets inside the grid only", cal=_quiet(real()), region="inside")
    row("  same, targets in the margins outside the grid", cal=_quiet(real()), region="outside")
    row("  same, smaller grid 85-115 / 6-14 (more extrapolation)", cal=_quiet(real()),
        grid=make_grid((85.0, 115.0), (6.0, 14.0), 5))
    row("  same, 4x4 grid with its 4 corners skipped", cal=_quiet(real()),
        grid=[g for g in make_grid((75.0, 125.0), (3.0, 17.0), 4) if not (g[0] in (75, 125) and g[1] in (3, 17))])
    row("click noise 1.5 px (eyeballed click at 320x240)", cal=ideal(), click_px=1.5)
    row("click noise 0.4 px (snapped to the dot finder)", cal=ideal(), click_px=0.4)
    row("one misclick 25 px, plain least squares", cal=ideal(), misclick=(25.0, -10.0), robust=False)
    row("one misclick 25 px, robust fit (bad click counted less)", cal=ideal(), misclick=(25.0, -10.0))
    rb = ideal()
    rb.pan.play_deg, rb.tilt.play_deg = 1.2, 1.5
    row("SG90 backlash 1.2/1.5 deg, v1 (serpentine grid, any approach)", cal=copy.deepcopy(rb), cal_via=0, run_via=0)
    row("SG90 backlash, same approach direction (calibration + aiming)", cal=copy.deepcopy(rb))
    rn = ideal()
    rn.pan.noise_deg = rn.tilt.noise_deg = 0.15
    row("servo repeatability 0.15 deg", cal=rn)
    row("YOLO box jitter 2 px (one detection)", cal=ideal(), yolo_px=2.0)
    row("YOLO box jitter 2 px, median of 5 detections (Aimer)", cal=ideal(), yolo_px=MED5 * 2.0)
    print("# where the target is vs the calibrated counter plane (parallax, 12 cm laser offset)")
    row("wearer 5 cm closer to the counter than at calibration", cal=ideal(), use=ideal(h=0.30))
    row("wearer 5 cm further", cal=ideal(), use=ideal(h=0.40))
    row("wearer 10 cm further", cal=ideal(), use=ideal(h=0.45))
    row("wearer leaning 6 deg further forward", cal=ideal(), use=ideal(lean_fwd=6.0))
    row("lime (r 2.5 cm) aimed at its box centre (AIM_Y 0.5)", cal=ideal(), sphere=0.025, aim_y=0.5)
    row("lime aimed 0.6 down its box (AIM_Y 0.6, default)", cal=ideal(), sphere=0.025, aim_y=0.6)
    row("avocado (r 4 cm), AIM_Y 0.5", cal=ideal(), sphere=0.04, aim_y=0.5)
    row("avocado, AIM_Y 0.6", cal=ideal(), sphere=0.04, aim_y=0.6)
    print("# pixel coordinates that don't match the calibration")
    c = ideal()
    row("calibrated 320x240, camera now 640x480, NOT rescaled", cal=c, use=ideal().scale(640),
        px_map=lambda p: p)
    row("calibrated 320x240, camera now 640x480, rescaled (Aimer)", cal=c, use=ideal().scale(640),
        px_map=lambda p: p * 0.5)
    row("CAM_ROTATE changed 0 -> 180 after calibrating, not caught", cal=c,
        px_map=lambda p: np.c_[319 - p[:, 0], 239 - p[:, 1]])
    print("# everything together")
    row("v1: poly, 4x4 serpentine, 1.5 px clicks, centre aim, lime", cal=real(),
        grid=make_grid((75.0, 125.0), (3.0, 17.0), 4), click_px=1.5, cal_via=0, run_via=0, sphere=0.025,
        aim_y=0.5, yolo_px=2.0, robust=False)
    row("new: 5x5 one-way approach, snapped clicks, robust, AIM_Y 0.6", cal=real(), click_px=0.4, sphere=0.025,
        aim_y=0.6, yolo_px=MED5 * 2.0)
    row("new, and the wearer stands 5 cm further than at calibration", cal=real(), use=real(h=0.40),
        click_px=0.4, sphere=0.025, aim_y=0.6, yolo_px=MED5 * 2.0)
    print()
    print("Reachable: tilt 0 is the tilt servo's end stop. Share of the 320x240 picture the laser can reach:",
          f"{100 * _reach_share(real()):.0f}% (the top of it). Everything below is out_of_reach (laser off).")


def _quiet(r: Rig) -> Rig:
    for s in (r.pan, r.tilt):
        s.play_deg, s.noise_deg = 0.0, 0.0
    return r


def _reach_share(r: Rig) -> float:
    xs, ys = np.meshgrid(np.linspace(0, r.w - 1, 40), np.linspace(0, r.h_px - 1, 30))
    px = np.c_[xs.ravel(), ys.ravel()]
    pts = r.on_counter(px)
    ok = 0
    for p in pts:
        c = r.ideal_cmd(p)
        ok += PAN[0] <= c[0] <= PAN[1] and TILT[0] <= c[1] <= TILT[1]
    return ok / len(pts)


if __name__ == "__main__":
    main()
