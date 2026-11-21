# Role A progress

✅ done · 🔄 in progress / waiting · ⏳ not started · (?) to confirm

## Checkpoints

| # | Item | Done before Sat | Done overnight (Fri → Sat) | Status |
|---|---|---|---|---|
| 1 | Power + servos + laser on the bench | Servos and laser work, 3 s auto-sweep with laser blink | — | ✅ (header soldering not confirmed) |
| 2 | `tools/fake_s3.py` + `ControlLink` | Written, tested, pushed | — | ✅ |
| 3 | `FrameGrabber` | Written, tested, pushed | — | ✅ |
| 4 | Camera stream | — | Old `LaserPointerCam.ino` compiles | 🔄 waiting on B's camera firmware |
| 5 | Real S3 firmware | `secrets.h` filled in, BOOT `B` message added | Compiles clean (esp32 core 3.3.13). `tools/check_hw.py` written | 🔄 flash it, then `check_hw.py --button 15` |
| — | 5-min power test, everything on the rail | Reported working (?) | — | ✅ (?) |
| 6 | Box build + mounting | (?) | — | ⏳ (?) |
| 7 | Tilt limits | — | — | ⏳ needs the box |
| 8 | Click calibration | `calibrate.py` written | — | 🔄 code ✅, needs camera + box to run |
| 9 | Open-loop Aimer | Written, tested with fakes | Laser stays off without `calibration.json` | 🔄 code ✅, needs calibration + real YOLO |
| 10 | Integration | — | `main.py` rewired (real Aimer/link/frames + B's voice/YOLO/session + C's agent), tested end to end against the fake S3 | 🔄 code ✅, full team run pending |

## Stretch goals

| # | Item | Status |
|---|---|---|
| 1 | Laser dot finder | ✅ overnight (synthetic tests only) |
| 2 | Closed-loop correction | ✅ overnight (`main.py --closed-loop`) |
| 3 | Blink check | ✅ overnight |
| 4 | Automatic calibration | ✅ overnight (`calibrate --auto`) |
| 5 | Calibration in the dashboard | ✅ `python -m coach.calib_web` (own page, fakes only; `mount()` hook for C's dashboard) |

## Hand-offs to teammates

| What | Due | Status |
|---|---|---|
| `fake_s3.py` + `FrameGrabber` to B and C | Fri night | ✅ on `main` |
| `on_button` to B | Sat noon | ✅ code done. Optional (`--button`): B's session uses the "hey Remy" wake phrase |
| `point_at` / `laser_off` to B and C | Sat 6 pm | 🔄 code done, needs calibration |

## Next steps, in order

1. ~~Push `role-a-software` into `main`~~ ✅ done.
2. (Handed off; `secrets.h` filled in for both boards, gitignored.) Flash `control_s3.ino`, then `cd laptop && python ../tools/check_hw.py --button 15`.
3. Flash B's camera firmware (`LaserPointerCam`, board "ESP32 Wrover Module", Huge APP, upload 115200; close the IDE Serial Monitor first) and check fps with `check_hw.py --skip-s3`. The OV3660 tops out at ~12 fps at HVGA 480x320, so expect less than the 15 fps target.
4. Finish the box (print, perfboard, mount), then set the tilt limits.
5. `python -m coach.calibrate` at counter distance.
6. ~~`python -m coach.main --type --no-yolo`~~ ✅ against the fake S3 with a synthetic calibration: every target within 0.5°. "stop"/"done" now turn the laser off; tilt is clamped on the laptop too. Repeat on the real S3.
7. Real YOLO, then a full voice run with the team.
