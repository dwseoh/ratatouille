# Aiming: where the error comes from and how the laser gets onto the lime

Goal: after one calibration, `point_at("lime")` puts the dot on the lime wherever it is in the picture, as long as the
laser can physically reach it, and says so plainly when it can't.

Code: `laptop/coach/aim.py` (Calibration + Aimer), `aim_model.py` (the two pixel -> angle models), `calibrate.py` /
`calib_web.py` (calibration, verify, `--eval`), `aim_sim.py` (simulated box used by the tests and the numbers below),
`tools/aim_sim.py` (prints the error budget).

## TL;DR

| What | v1 | Now |
|---|---|---|
| Typical miss on a lime, everything together (simulated box) | 1.5 deg mean, 3.6 deg worst = 2.4 cm / 5.9 cm on the counter | 0.46 deg mean, 1.25 deg worst = 0.7 cm / 2.9 cm |
| Camera resolution changed after calibrating (320x240 -> 640x480) | ~16 deg, 20 cm off, silently | rescaled automatically, warned in the log + TFT |
| `CAM_ROTATE` changed after calibrating | ~24 deg, 28 cm off, silently | refused: laser off, "CALIB ROTATE: recal" on the TFT |
| Object in the bottom part of the picture (below the tilt end stop) | dot parked at the edge of the reach, 6-25 cm away, `on_target` True | `point_at` returns `out_of_reach`, laser off, logged |
| Pixel -> angle model | 2nd-order polynomial (0.25 deg mean / 0.5 worst model error, 1.0 worst when extrapolating) | physical camera + counter + pan-tilt model (0.06 / 0.27, 0.57), chosen by cross-validation |
| Wearer stands 5-10 cm closer/further than at calibration | 1-1.8 deg, 2-3.6 cm | same: physics (12 cm laser offset). Only the dot finder (closed loop) fixes it |

To recalibrate and check it on the real box, see [Recalibrate and verify](#recalibrate-and-verify).

## How a pixel becomes a servo angle

```
YOLO box ──> aim point: box centre x, AIM_Y (0.6) of the way down
         ──> runtime pixel -> calibration pixel (rescaled if the camera's frame size changed)
         ──> model: camera ray -> hits the counter plane -> 3D point -> direction from the laser pivot
                    -> pan-tilt inverse kinematics -> servo command = offset + gain * angle        ("plane")
             or     2nd-order polynomial in (x, y)                                                  ("poly")
         ──> reach check (servo limits; else the reachable part of the box; else out_of_reach)
         ──> Aimer: 4 deg/tick, final move pan-up / tilt-down, ControlLink ramps 2 deg/packet
```

**Plane model** (`aim_model.PlaneModel`, 17 parameters): pinhole camera (focal length, principal point, one radial
distortion term), camera pitch/roll inside the box, the counter plane (distance + tilt vs the pan axis), the laser
pivot's offset from the lens (prior: 12 cm above), beam cone angle, and per servo an offset and a gain. For points on
the counter this is exact anywhere in the picture: pixel -> counter plane is a homography and the rest is geometry,
so it extrapolates to the corners and margins the click grid didn't cover. One plane of data can't pin down every
parameter (overall scale, focal length vs distance trade off), so the fit (numpy Levenberg-Marquardt, no scipy) has
weak priors from the box: laser ~12 cm above the lens (`LASER_ABOVE_CM`), ~64 deg wide lens (`CAM_HFOV_DEG`),
camera pitched ~40 deg down. The priors pick the physically sensible solution; the predictions on the calibrated
counter don't depend on them. Starts: camera upright and upside down, three pitches; lowest cost wins.

**Choosing the model:** `calibrate` fits both, robustly (a click that disagrees with the rest is counted less and
flagged "bad click?"), and computes each one's leave-one-out error (refit without a point, predict it; corner points
make this an extrapolation test). The lower one is used and both are printed and stored. `CALIB_MODEL=poly` or
`plane` in `laptop/.env` overrides it at runtime.

## Error sources, ranked (simulated box)

Numbers from `cd laptop && python ../tools/aim_sim.py` (~15 s). The simulated box (`coach/aim_sim.py`) is the
real one as far as we know it: OV3660 at 320x240, ~62 deg wide, pitched 45 deg down, counter 35 cm below the lens,
laser pivot 12 cm above the lens, pan 72-128 / tilt 0-20 reach (bigger pan = left, bigger tilt = up), objects
58-86 cm from the laser (median 69). The truth has more in it than the plane model (two-term lens distortion, a
5 px principal point offset, camera yaw, the tilt axis 1.2 cm in front of the pan axis, the beam 8 mm off the tilt
axis, 0.8 deg axis skew, 1 deg beam cone, servo gain errors of 4-5 %, +-0.6 deg servo nonlinearity, 1.2/1.5 deg
backlash, 0.15 deg servo noise). Calibration: 5x5 grid over pan 75-125 / tilt 3-17; targets: random counter points
anywhere the laser can reach. "cm" = distance on the counter between where the dot lands and where it should
(counter seen at a slant, so cm per degree grows towards the top of the picture). 1 px at 320 wide = 0.22 deg.

| # | Source | Size (mean / worst) | What changed |
|---|---|---|---|
| 1 | **Pixel coordinates from a different frame size** (calibrated at 320x240, streaming 640x480) | 16 / 26 deg, 20 / 36 cm | `calibration.json` stores the frame size; the Aimer compares it with the newest frame and rescales about the centre (exact: 0.03 deg). Log + TFT "CALIB 320x240 SCALED". A 3:2 mode (480x320) is rescaled as a centred crop and flagged approximate |
| 2 | **`CAM_ROTATE` changed after calibrating** | 24 / 43 deg | stored in `calibration.json`; a mismatch is refused (laser off, TFT "CALIB ROTATE: recal"), because a physically re-mounted camera and a changed setting can't be told apart |
| 3 | **Target below the reach** (tilt 0 is the servo's end stop; only the top ~45 % of the picture height is reachable on the simulated box, the real box measured "top half") | object at y=160/200/230: dot lands 14/21/25 cm away, v1 said `on_target` | reach check on the aim point and then the rest of the box: half-reachable boxes get the dot on their reachable part; nothing reachable -> `point_at` returns `"out_of_reach"`, laser off, logged; a tracked target that moves out of reach turns the laser off until it's back. Clamped goals never count as `on_target` (1 deg tolerance) |
| 4 | **Parallax: wearer's posture differs from calibration** (laser 12 cm above the lens) | counter 5 cm closer: 1.3 / 1.4 deg, 2.0 / 2.6 cm; 5 cm further: 1.0 / 1.1 deg, 1.9 / 2.8 cm; 10 cm further: 1.8 / 2.0 deg, 3.6 / 5.3 cm; leaning 6 deg: 0.6 / 0.7 deg | no software fix without depth. Calibrate in the demo posture; `--closed-loop` (dot finder) corrects it per target |
| 5 | **Fruit height: box centre is not where the beam should go** (laser above the camera: the box centre's ray hits the counter behind the fruit, so the beam passes above its middle) | lime (r 2.5 cm): 0.62 / 0.71 deg, 1.0 cm; avocado (r 4 cm): 1.1 / 1.2 deg, 1.8 cm | `AIM_Y=0.6` (aim 60 % down the box): lime 0.13 / 0.25 deg, avocado 0.26 / 0.45. 0.5 = old box centre |
| 6 | **SG90 backlash/deadband with mixed approach directions** (serpentine grid, aims from wherever) | 0.70-0.83 / 1.8-2.1 deg, 1.1-1.3 / 3.9-4.9 cm | every calibration point and every aim ends with pan moving up and tilt moving down from 3 deg away (`APPROACH_DIR`): 0.04 / 0.7 deg (the worst case is next to a limit where there's no room to come from) |
| 7 | **YOLO box jitter** (2 px per box) | one box: 0.50 / 1.4 deg | median of the last 5 new boxes of the same instance, restart on a jump: 0.28 / 0.8 deg. Once on target, goal changes under 1 deg are ignored (no hunting). v1 averaged the last 3 *ticks* (mostly the same box), and mixed two limes when the best-confidence one switched |
| 8 | **Polynomial model error** (no noise) | 0.25 / 0.51 deg; margins outside the grid 0.32 / 0.51; smaller grid 1.0 worst; 4x4 with corners skipped 0.68 worst | plane model: 0.06 / 0.27; margins 0.09 / 0.27; smaller grid 0.57 worst; corners skipped 0.46 worst |
| 9 | **Click error** (eyeballing a 3-5 px dot at 7 fps) | 1.5 px: +0.15-0.3 deg on the result (0.18 / 0.54 plane) | a click within 4 % of the frame width of the dot snaps to the dot finder's centre (0.4 px: 0.05 / 0.14) |
| 10 | **A bad click** (one 25 px off, or clicked on a stale frame) | plain least squares: 0.28-0.46 / 0.6-0.9 deg everywhere | robust fit: 0.04 / 0.11, the point is flagged in the report |
| 11 | **Clicking before the servos settle / on an old frame** (7 fps = 143 ms per frame, plus WiFi/MJPEG latency, plus ControlLink's 60 deg/s ramp) | that point is off by up to the distance moved (degrees) | clicks only count once ControlLink's `moving` is False, 0.6 s passed (was 0.4) and a frame received after that is on screen; one click per point (a double click used to record the same pixel for two grid points) |
| 12 | Servo repeatability | 0.15 deg -> 0.18 / 0.52 deg | none possible (MG90S metal gears are a bit better) |
| 13 | Servo nonlinearity (+-0.6 deg) | poly 0.30 / 0.60, plane 0.05 / 0.16 | absorbed by the fit |
| 14 | Lens distortion, pan-tilt axis offsets, beam cone | plane 0.03-0.04 / 0.09-0.15 | modelled / small |

All together, lime on the counter, 2 px YOLO jitter:

| Setup | deg mean / worst | cm mean / worst |
|---|---|---|
| v1: poly, 4x4 serpentine, eyeballed clicks, box centre, raw boxes | 1.53 / 3.56 | 2.4 / 5.9 |
| new: plane, 5x5 one-way approach, snapped clicks, robust fit, AIM_Y 0.6, median of 5 | 0.46 / 1.25 | 0.7 / 2.9 |
| new, but the wearer stands 5 cm further back than at calibration | 1.00 / 2.01 | 1.8 / 4.2 |

On the real box v1 was probably hit by several of the big ones at once: points only reachable in the top half
(objects lower down got a dot parked at the edge of the reach, reported as on target), a resolution or rotation
change after calibrating (nothing checked it), backlash from the serpentine grid, and posture.

## What the Aimer does now

- `point_at(name)`: `"unknown_object"`, `"not_visible"` as before; `"out_of_reach"` (new) when the object is visible
  but no part of its box is within `PAN_*`/`TILT_*` (laser off, warning logged with the pixel and the limits).
  Callers treat it like any non-"ok" result; `contracts.PointResult` could list it (one-word change, not made here
  because contracts.py is shared).
- Aim point: box centre x, `AIM_Y` (default 0.6) of the way down. Set `AIM_Y=0.5` for the box centre.
- Tracking: follows the detection nearest the box it's tracking (not the most confident one), median of the last 5
  new boxes, restarts when the box jumps by more than 0.75 box sizes.
- Moves: 4 deg/tick rate limit, 0.5 deg deadband; if a move would end the wrong way for backlash, it goes 3 deg past
  first (pan below, tilt above) and comes back; `on_target` only when there, ControlLink's ramp finished
  (`moving` False) plus 0.15 s, and the goal wasn't clamped.
- Calibration vs camera: on every frame size change it re-checks `calibration.json`'s frame size and CAM_ROTATE
  (the newest frame size comes from `frames.last_frame_size()`, so `main.py` didn't change). Rescale or refuse as in
  the table. Old files without that info still work, with a note in `--eval`.
- Closed loop (`--closed-loop`, stretch: dot finder + blink check) works with either model. Changes: the dot is
  driven to the box centre (AIM_Y only makes up for open-loop parallax); "on target" = within `CLOSE_PX` scaled to
  the frame width (10 px at 640 wide, 5 px at 320, ~1 deg; it was 10 px = 2.2 deg at 320x240); and a new target
  starts from the trim the last confirmed target needed (`CL_CARRY`), because a posture change shifts every
  target about the same way (simulated: counter 7 cm further than at calibration, the next target's miss drops
  by more than 60 % before any dot is seen).

## calibration.json (v2)

```
{"version": 2, "terms": [...], "coef": [[...] x6],      # the polynomial, always there: v1 code still reads it
 "model": "plane", "plane": {"f": ..., "pitch": ..., "ly": -0.12, "pan0": ..., ...},
 "frame": [320, 240], "rotate": "180",
 "cv": {"poly": {"fit": 0.31, "loo": 0.45, "bad": 0}, "plane": {"fit": 0.21, "loo": 0.27, "bad": 0}},
 "points": {"px": [...], "angles": [...], "weights": [...]}, "created": "..."}
```

v1 files (`{"terms", "coef"}` or a bare list) load as the polynomial with no frame/rotation info.

## Recalibrate and verify

From `laptop/`, on the box at the demo counter, standing the way you'll stand during the demo:

1. `laptop/.env`: the real `CAM_URL`, `CTL_HOST`, `CAM_ROTATE=180`, and limits a few degrees outside the jog range,
   e.g. `PAN_MIN=65 PAN_MAX=135 TILT_MIN=0 TILT_MAX=25` (the tilt approach needs ~3 deg of room above the top
   calibration row). Set the camera resolution you'll demo with **before** calibrating.
2. `python3 -m coach.jog`: find the reachable range (mark the corners), as in bringup step 4.
3. Calibrate, 5x5 by default, a few degrees inside the jog range:
   ```
   python3 -m coach.calibrate --pan-min 75 --pan-max 125 --tilt-min 3 --tilt-max 17
   ```
   Click the dot when the window says "click the dot" (clicks before that are ignored; a click near the dot snaps
   to it). `s` skip, `r` redo. At the end it prints each point's error (deg and cm) and both models' fit and
   leave-one-out errors, and saves `laptop/calibration.json`.
   The dot must land on the counter (or a flat board on it) for every point, not on a back wall.
4. Verify: press `v` in the same window. The laser visits 8 random reachable spots (cross on screen = where it was
   sent); click the dot each time (`s` if it's not visible). It prints the error per spot and
   `VERIFY: 8 points, mean X deg (~Y cm), max ...`. Press `a` to add those points to the calibration and refit.
   With the dot finder instead of clicks: `python3 -m coach.calibrate --auto` (sweep + 8-point auto verify), or the
   browser page's **Auto verify** (`python3 -m coach.calib_web`, http://127.0.0.1:8001).
5. Check the file any time, no hardware: `python3 -m coach.calibrate --eval`.
6. Spot-check live: `python3 -m coach.jog` now draws a cyan circle where the calibration expects the dot for the
   current angles. Jog around the whole reachable area; the dot should stay inside the circle.

What good looks like:

| Number | Good | Look again |
|---|---|---|
| per-point error in the report | < 0.5 deg | any "WARN" / "bad click?": redo that point (`r`) |
| leave-one-out (used model) | < 0.6 deg | > 1 deg: clicks inconsistent, servo slipping, or the dot hit something off the counter |
| verify mean / max | < 0.6 / < 1.5 deg (~1 / ~2.5 cm) | max > 2 deg: run `--eval`, look at where it's worst |
| plane geometry line | laser 10-14 cm above, camera pitched 25-60 deg down, counter 25-70 cm from the lens, gains 0.85-1.15 | far off: the fit found a mirror solution or the data is bad; `CALIB_MODEL=poly` is the fallback |

Redo the calibration when the camera or pan-tilt moved relative to each other, when `CAM_ROTATE` changes, or when
the tilt horn is re-seated. A resolution change alone is handled, but calibrating at the new resolution is better.

## What's left

- **Posture parallax (largest remaining error, 1-2 deg).** The 12 cm laser offset makes the mapping depend on how far
  the counter is. Without depth the only fix is seeing the dot: `--closed-loop` (dot finder + blink check +
  per-target trim carried to the next target) exists but is untested on the real box under venue light. A further
  step with the plane model: triangulate the counter distance from each confirmed dot (laser beam vs camera ray,
  12 cm apart) and move the plane for the whole session.
- **Reach.** Only the top part of the picture is reachable with the tilt servo at its end stop. Re-seat the tilt horn
  1-2 teeth down (bringup Troubleshooting) to cover more; `out_of_reach` now says when it isn't.
- **Hardware unknowns** the simulation assumes: real SG90 backlash (1-1.5 deg assumed), YOLO-World box jitter on a
  real lime (2 px assumed), camera latency (0.6 s settle assumed enough). Verify on the box measures the sum.
- `AIM_Y=0.6` is tuned for a laser above the lens and fruit-sized objects; flat objects (a cutting board) don't care.
