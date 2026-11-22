# Bring-up: from a cold box to a laser that lands on the lime

Run this every time the box is set up somewhere new (new table, new venue, new hotspot), or after anything on
the box was moved. Commands run from `laptop/` on the laptop that runs the app. Takes ~20 minutes.

Two rules the whole time:
- **Never plug USB into an ESP while the power bank is connected.** Unplug one first.
- **Keep faces out of the beam** until step 5 has set the limits.

## 0. Network (the thing that broke most often)

Everything (laptop, S3, camera board) must be on the **same 2.4 GHz phone hotspot**.

| Check | How |
|---|---|
| iPhone hotspot visible to the ESPs | Settings → Personal Hotspot: **Maximize Compatibility on**, keep that screen **open**, Auto-Lock **Never** |
| Hotspot name has no curly apostrophe | Rename the phone (Settings → General → About → Name) to something plain like `JamiesiPhone17` |
| Laptop really on the hotspot | `ifconfig \| grep "inet "` shows `172.20.10.x`. If it shows `192.0.0.2` only: Wi-Fi → Details → TCP/IP → **Using DHCP**, Forget Network, rejoin with **Other Network…** (name + password typed in), turn off VPN and "Limit IP address tracking" |
| ESP credentials | `firmware/control_s3/secrets.h` (S3) and the camera firmware's settings must match the hotspot. Change hotspot = reflash both |

## 1. Power on and find the IPs

1. Bank in (USB out of both ESPs).
2. **S3 IP:** TFT line 1, or the S3's Serial Monitor: `coach-ctl: wifi OK, IP 172.20.10.x`.
3. **Camera IP:** the camera board's Serial Monitor at boot (`Stream: http://172.20.10.y:81/stream`), or the address that shows video in a browser.

## 2. `laptop/.env`

```
CTL_HOST=172.20.10.x                    # S3
CAM_URL=http://172.20.10.y:81/stream    # camera
CAM_ROTATE=180                          # picture was upside down as mounted (vflip if it reads mirrored)
PAN_MIN=65                              # safety limits, see step 5
PAN_MAX=135
TILT_MIN=0
TILT_MAX=25
```

Real IPs only, no `x`/`y` left in. Restart any running tool after editing (Ctrl-C, run again).
Get the code first: `git checkout main && git pull origin main`.

## 3. Hardware check

```bash
python3 ../tools/check_hw.py --button 15
```

Pass: S3 online, echoes pan, receives C packets, BOOT button (press it when asked), camera frames.
The "≥ 15 fps" line fails at ~7 fps with the OV3660; that's expected and fine for v1.

## 4. Find where the laser is visible (`jog`)

```bash
python3 -m coach.jog
```

Click the window. `a`/`d` pan, `w`/`s` tilt (Shift = 10° steps), `l` laser, `c` centre, `m` mark, `q` quit.
Steer the dot to the **top-left, top-right, bottom-right, bottom-left** of the picture and press `m` at each.
`q` prints a `calibrate` command for that area.

Last measured (Sun): pan 72-128 (bigger = dot goes **left**), tilt 0-20 (bigger = dot goes **up**). Tilt 0 is
the servo's end stop, so the bottom half of the picture is out of reach. To reach lower, re-seat the tilt horn
1-2 teeth down (see Troubleshooting), then redo this step.

## 5. Safety limits

Set `PAN_MIN/PAN_MAX/TILT_MIN/TILT_MAX` in `laptop/.env` to the `jog` range plus a few degrees, so the
laser can never leave the camera's view (the counter) and point at people. The S3 firmware allows the full
0-180, so these `.env` values are the limit that counts. No reflash needed to change them.

## 6. Calibrate

Set the camera resolution and `CAM_ROTATE` you'll demo with **first** (the calibration records both). Stand the way
you'll stand during the demo, same counter distance: the laser sits 12 cm above the lens, so every 5 cm the counter
is nearer or further than at calibration costs ~1 deg (~2 cm). Use the range from step 4, a few degrees inside it,
and keep `TILT_MAX` in `.env` at least 3 deg above `--tilt-max` (each point is approached from 3 deg above in tilt):

```bash
python3 -m coach.calibrate --pan-min 75 --pan-max 125 --tilt-min 3 --tilt-max 17
```

- 5x5 grid (25 points, `-n 4` for the old 16). Wait for **"click the dot"**, then click the dot: clicks before that
  are ignored, and a click near the dot snaps to its centre. `s` skip (dot off-screen), `r` redo, `q` quit.
- The dot must land on the counter (or a flat board on it), not on a back wall.
- At the end it prints each point's error in deg and cm (redo anything marked `WARN` / `bad click?`), both models'
  errors (`plane` = physical model, `poly` = old polynomial; the lower leave-one-out one is used) and saves
  `laptop/calibration.json`. **Good: points under ~0.5 deg, leave-one-out under ~0.6 deg.**
- Press `v` to **verify**: the laser visits 8 random spots (cross = where it was sent), click the dot each time.
  It prints `VERIFY: mean ... deg (~cm), max ...`. **Good: mean under ~0.6 deg, max under ~1.5 deg.** `a` adds those
  points to the calibration and refits.
- `t` test mode: click spots on the picture, the laser goes there. `q` to finish.
- Re-check later without clicking (dot finder): `python3 -m coach.calibrate --check`.
- Check the saved file any time (no hardware): `python3 -m coach.calibrate --eval`.
- `python3 -m coach.jog` now shows a cyan circle where the calibration expects the dot: jog around, the dot should
  stay in it everywhere.
- Browser version, same result (plus **Verify** / **Auto verify** / **Add verify points**):
  `python3 -m coach.calib_web`, then open http://127.0.0.1:8001.

Redo calibration whenever the camera or pan-tilt moves, `CAM_ROTATE` changes (the laser refuses to aim until you
do: TFT "CALIB ROTATE: recal"), or the horn is re-seated. A resolution change is rescaled automatically (TFT
"CALIB 320x240 SCALED"), but recalibrating at the new resolution is better. Details: [aiming.md](aiming.md).

## 7. Aiming tests

```bash
python3 -m coach.main --type --no-yolo --fake-agent    # fake detections: "where's the lime" moves the laser
python3 -m coach.main --type --fake-agent              # real YOLO: put a real lime inside the reachable area
python3 -m coach.main                                  # full voice run: "hey Remy"
```

Put the ingredients **inside the reachable area** (tape it off on the counter). Objects outside it can't be
pointed at: `point_at` answers `out_of_reach` and the laser stays off (logged with the pixel and the limits).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| S3 reboots / WiFi reconnects in a loop while servos move | Brownout: both servos jumping at once | Fixed in `ControlLink` (moves ramp at 60°/s). Still happens: caps at the servo headers (stripe to GND), bank's high-current port, S3's 5V off the rail when it's on USB |
| `E (208) octal_psram: PSRAM chip is not connected` | Board is **N8R2** (quad PSRAM) | Arduino: PSRAM **Disabled**, Flash Mode **QIO 80MHz**, Flash Size **8MB**. Harmless if it still prints |
| Serial Monitor shows nothing from the sketch | Native USB port (`cu.usbmodem*`) | Arduino: **USB CDC On Boot: Enabled** |
| S3 never joins WiFi (dots forever) | Hotspot asleep / 5 GHz / name mismatch | Firmware prints why every 15 s plus visible networks. See step 0 |
| `sta is connecting, return error` spam | Old firmware reconnect bug | Flash the current `control_s3.ino` |
| `check_hw`: S3 FAIL, `ping` times out | Laptop not really on the hotspot | Step 0, "Laptop really on the hotspot" |
| `Failed to resolve hostname 172.20.10.y` | Placeholder left in `.env` | Put the real IP |
| `Host is down` for the camera | Wrong camera IP / camera off the hotspot | Check its Serial Monitor; open the URL in a browser |
| Picture upside down | Camera mounted flipped | `CAM_ROTATE=180` (the raw browser stream stays flipped) |
| Laser dot not in the picture during calibration | Grid outside the visible area | Step 4 (`jog`), then calibrate inside that range |
| Servo moves by hand, not on command | No servo power, loose plug, or stripped gears | Bank connected? Plug seated? Clicking = replace servo (MG90S) |
| Laser only reaches the top half | Tilt servo at its end stop | Re-seat the tilt horn: in `jog` go to the lowest tilt, laser off (`l`), unscrew the horn's centre screw, pull the arm off, put it back 1-2 teeth further **down**, screw in. Then redo steps 4-6 |

## Firmware (rarely needed)

All aiming logic is on the laptop. Reflash only for: a new hotspot (`secrets.h`), a hard tilt limit in
`control_s3.ino`, or `tft.setRotation(2)` if the display reads upside down. Arduino settings for the S3:
board **ESP32S3 Dev Module**, PSRAM **Disabled**, Flash Mode **QIO 80MHz**, Flash Size **8MB**,
USB CDC On Boot **Enabled**, Upload Speed 921600. Unplug the bank before flashing.
