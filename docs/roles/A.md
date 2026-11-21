# Role A: hardware + firmware + pointing (dw127)

> **Scope note (pending dw127's OK):** v1 uses **click-to-calibrate + open-loop aiming**. The laser dot finder
> (P4) and closed-loop correction (P6 closed loop) move to stretch. See [cut-list.md](../cut-list.md).
> If you reject this, P4 and P6 in [task-specs.md](../task-specs.md) come back.

## Goal

When anyone calls `aimer.point_at("lime")`, the laser lands on the lime within ~5 cm in under 0.5 s.
The box is worn on the chest, runs from the power bank, and the laser can never point above horizontal.

Your part is the critical path. B and C can fake you, but the demo can't. Push small, working pieces early.

## Files you own

| File | What |
|---|---|
| `firmware/control_s3/control_s3.ino` | S3: UDP → servos, laser, TFT, status, watchdog, BOOT button |
| `firmware/camera/LaserPointerCam.ino` | Cam board: MJPEG stream over the hotspot |
| `firmware/servo_test/servo_test.ino` | Bench test (already works) |
| `laptop/coach/control.py` | `ControlLink` (UDP link to the S3) |
| `laptop/coach/frames.py` | `FrameGrabber` (camera frames) |
| `laptop/coach/aim.py` | `Calibration` + `Aimer` |
| `laptop/coach/calibrate.py` | Click calibration tool (new) |
| `tools/fake_s3.py` | Fake S3 for everyone (new) |
| `hardware/enclosure/*` | Box, lid, laser holder |
| `vision.py` `find_red_dot` | Stretch only (B owns the rest of `vision.py`) |

## Interfaces (exact names, from `laptop/coach/contracts.py`)

**You provide:**
- `ControlLinkAPI`: `pan`, `tilt`, `laser`, `online`, `show_text(line, text)`, `reset()`, `on_button(cb)`
- `FrameSource`: `connected`, `latest() -> (frame | None, t)`, `recent(n) -> list[frame]`
- `AimerAPI`: `target`, `on_target`, `point_at(name) -> "ok" | "not_visible" | "unknown_object"`, `laser_off()`
- `State` fields only you write: `target`, `on_target`, `dot` (stays `None` in v1)

**You consume:**
- `State.dets` (B's YOLO). Until it exists, use `fake_dets()`.
- `State.recipe.objects` (to know what's a valid name).
- `LASER_IDLE_OFF_S = 30`, TFT line constants (`TFT_STATUS` ... `TFT_AGENT`).

**Who calls you:** B's intent router calls `point_at` and `laser_off`. C's agent tools call them too. B's session
reset calls `laser_off`. B listens on `on_button` for the BOOT press. Anyone may call `show_text`.

---

## Build plan (checkpoints, in order)

### 1. Power + servos on the bench (Fri night)

Status: bank → USB-C breakout (VBUS + GND only) → 5 V rail works. `servo_test` moves the GPIO4 servo.

Steps:
1. **Solder the breakout header.** Loose pins caused random power cuts.
2. **2nd servo (GPIO5) not moving: swap test.** Swap the two servo plugs.
   - Problem follows the servo → bad servo, use a spare.
   - Problem stays on GPIO5 → check the jumper, the pin, and that the servo's brown wire shares GND with the ESP.
3. **Laser from Q1.** Laser + to the 5 V rail, laser − to Q1 collector, Q1 emitter to GND, GPIO6 → 1 k → Q1 base,
   10 k from base to GND (laser off at boot). From a GPIO directly it's faint (3.3 V).
4. Feed the ESPs through their 5V pin from the rail. **Never draw power from an ESP 5V pin** for the servos or laser.

Test: flash `servo_test`, unplug USB, power from the bank. After 10 s it sweeps both servos with the laser on.

**Done when:** both servos sweep from the bank for 5 min, laser is bright, no ESP resets.

### 2. Fake S3 + control link (Fri night, push early: B and C use the fake)

`tools/fake_s3.py` behaviour:
- Listens on UDP `0.0.0.0:4210`.
- Prints each packet on one line, but only prints `C` packets when the values change (30 Hz is spammy).
- After the first `C` packet, sends `S <pan> <tilt> <laser> -50 5100 <last_seq> <uptime_ms>` every 200 ms
  back to the sender's address.
- Pressing Enter sends `B` (BOOT button).
- Applies the same watchdog rule: no `C` for 500 ms → prints `laser OFF (watchdog)`.

`ControlLink` details (`control.py`, mostly written):
- Sends `C pan tilt laser seq` at 30 Hz nonstop. It's also the heartbeat.
- Resolves `CTL_HOST` (mDNS `coach-ctl.local`) once at start. If `online` has been false for 5 s, re-resolve.
  If mDNS fails, use the IP shown on TFT line 1 (`CTL_HOST=172.20.10.5` in `.env`).
- Parses `S` into `status`. `online` = an `S` arrived in the last 1 s.
- Parses `B` and calls every callback registered with `on_button(cb)`.
- `reset()` sends `R`. `show_text(line, text)` sends `T line text[:20]`.

Test (fake S3 in one terminal, sweep in another):
```bash
python tools/fake_s3.py
```
```python
# tools/sweep.py
import asyncio
from coach.control import ControlLink
async def main():
    link = await ControlLink("127.0.0.1", 4210).start()
    link.on_button(lambda: print("BUTTON"))
    link.show_text(2, "sweep test")
    for pan in list(range(60, 121, 5)) + list(range(120, 59, -5)):
        link.pan, link.laser = pan, True
        await asyncio.sleep(0.1)
    print("online:", link.online, link.status)
asyncio.run(main())
```
Run with `cd laptop && python ../tools/sweep.py`.

**Done when:** the fake S3 prints the sweep, the sweep prints `online: True`, and Enter in the fake prints `BUTTON`.

### 3. Frame grabber (Fri night, push early: B's YOLO and C's agent need it)

`frames.py` is mostly written. Add:
- `recent(n)`: the last `n` frames from `history` (about 0.5 s apart), oldest first.
- `CAM_URL` can be a webcam index (`0`), a video file, or the ESP stream URL. Turn `"0"` into the int `0`.
- Keep only the newest frame (no backlog). Reconnect with backoff. Set `connected`.

Test:
```bash
cd laptop && python -c "
from coach.frames import FrameGrabber; import time
g = FrameGrabber(0).start(); time.sleep(3)
f, t = g.latest(); print(f.shape if f is not None else None, len(g.recent(3)))"
```
Then kill the stream (unplug the webcam or stop the ESP) and plug it back in.

**Done when:** it prints a frame shape and 3 frames, and recovers in under 3 s after the source comes back.

### 4. Cam board stream (Fri night)

- Start from the CameraWebServer example (`firmware/camera/`). Join the phone hotspot (2.4 GHz /
  "Maximize Compatibility" on iPhone). mDNS name `coach-cam`.
- Stream on `:81/stream`. VGA or QVGA, JPEG quality ~12.
- Set flip/mirror so the image is upright as mounted.
- Reconnect WiFi if it drops.

Test: open `http://coach-cam.local:81/stream` in a browser on the laptop (on the hotspot). Then
`CAM_URL=http://coach-cam.local:81/stream` with the checkpoint 3 test.

**Done when:** ≥15 fps in the browser, and it comes back by itself after a power cycle.

### 5. Real S3 control firmware (Fri night → Sat AM)

`control_s3.ino` is written but untested. Steps:
1. Copy `secrets.example.h` to `secrets.h`, fill in the hotspot. Flash (board "ESP32S3 Dev Module", PSRAM OPI).
2. Test by hand:
   ```bash
   nc -u coach-ctl.local 4210
   C 90 60 1 1
   T 2 hello
   R
   ```
   Servos move, laser on, "hello" on line 2, then reset. Stop typing: laser off after 0.5 s (watchdog).
3. **Add the BOOT button `B` message.** GPIO0 is the BOOT button. After boot it's a normal input:
   ```cpp
   pinMode(0, INPUT_PULLUP);
   // in loop():
   static uint32_t lastPress = 0; static bool wasDown = false;
   bool down = digitalRead(0) == LOW;
   if (down && !wasDown && now - lastPress > 500 && hubPort) {   // edge + 500 ms debounce
     lastPress = now;
     udp.beginPacket(hubIp, hubPort); udp.print("B"); udp.endPacket();
   }
   wasDown = down;
   ```
   Add `B` to `docs/protocol.md` (S3 → laptop table).
4. Run the checkpoint 2 sweep with `CTL_HOST=coach-ctl.local`.

**Done when:** the Python sweep moves the real servos, `online` is true, and pressing BOOT prints `BUTTON`.

### 6. Box build + mounting (Sat AM → Sat PM)

1. Print laser holder v4 (`hardware/enclosure/laser_mount.scad`). Check the snap fit and that the head sits in the hole.
   If the head is off-centre the wrong way, flip `head_dx` to −1.2.
2. Print the shell and lid. Mount the carrier perfboard on the 4 posts. Both ESPs plug into female headers.
3. Wire the 5 V rail: bank → breakout → rail → both ESPs' 5V pins, servos, laser (via Q1). One shared GND.
4. Mount the pan-tilt on top, holder on its platform. Leave slack in the servo and laser wires.
5. Strap through the lid loops, plus the lanyard.

**Done when:** worn on the chest for 10 min with servos sweeping, nothing loose, no resets.

### 7. Tilt limits (Sat PM, needs the box mounted)

With `servo_test`, `u` / `d` until the beam is exactly horizontal. That's `TILT_MAX` (or `TILT_MIN`, depending on
which way is up for your mounting). Find the lowest angle before the beam or holder hits anything. Put both in
`control_s3.ino` and reflash.

**Done when:** `C 90 0 1 1` and `C 90 180 1 1` both leave the beam at or below horizontal.

### 8. Click calibration (Sat PM)

`laptop/coach/calibrate.py`, run as `cd laptop && python -m coach.calibrate`.

Design (cv2 window, simplest; the dashboard version is a stretch):
1. Start `FrameGrabber` and `ControlLink`. Laser on.
2. For each angle pair in a **4x4 grid** (e.g. pan 60..120, tilt over your safe range, 16 points):
   - Move the servos there, wait 400 ms.
   - Show the live frame in a cv2 window. **You click the laser dot.** `s` skips a point (dot off-screen),
     `r` redoes the last one, `q` quits.
   - Store `(pixel_x, pixel_y) → (pan, tilt)`.
3. Fit `Calibration().fit(pixels, angles)`. It's a 2nd order polynomial with 6 terms, so it needs **at least
   6 points**. Use 16 to be safe, and refuse to save with fewer than 9.
4. Print the fit error: for each point, predicted angles vs real angles. Warn if any error is over 2°.
5. Save `calibration.json` in `laptop/`.

When to redo it: only when the camera or the pan-tilt moves relative to the other (rebuilt, bumped, holder reseated).
Leaning or walking doesn't matter: they move together. The laser sits a few cm from the lens, so calibrate at the
**demo counter distance** (parallax error grows away from it).

Test: after saving, click anywhere in the frame and the tool points the laser there (add a "test mode" key `t`).

**Done when:** clicking any spot puts the dot within ~5 cm of it.

### 9. Open-loop Aimer (Sat PM)

`aim.py` `Aimer`, implementing `AimerAPI`. A small asyncio loop at 20-30 Hz:

- **`point_at(name)`:**
  - Not in `state.recipe.objects` → `"unknown_object"`.
  - Not in `state.visible()` → `laser_off()`, return `"not_visible"`.
  - Otherwise: `target = state.target = name`, laser on, reset the idle timer, return `"ok"` at once.
- **Each tick:**
  - `det = state.det(target)`. If found, `goal = calib.angles_for(det.center)`.
  - **Smoothing:** keep the last 3 target centres, use their mean. This stops jitter from YOLO boxes.
  - **Rate limit:** move at most ~4° per tick. Big jumps still take under 0.3 s at 30 Hz.
  - **Deadband:** don't move if the change is under 0.5°. It kills servo buzz.
  - Write `link.pan`, `link.tilt`. Clamp pan to 10..170. The S3 clamps tilt as well.
  - `on_target = state.on_target = (the servos have reached goal)`.
  - **Follow moving targets:** the goal updates every tick, so if the bowl moves, the laser follows.
  - **Lost target:** not seen for 1 s → hold position. Not seen for 3 s → `laser_off()`.
  - **Idle off:** no new `point_at` for `LASER_IDLE_OFF_S` (30 s) → `laser_off()`.
- **`laser_off()`:** `link.laser = False`, `target = state.target = None`, `on_target = False`.
- No calibration file → `point_at` returns `"ok"` but the tick does nothing, and a warning goes on TFT line 0.

Test (no YOLO needed):
```bash
cd laptop && python -c "
import asyncio
from coach.contracts import *
from coach.aim import Aimer, Calibration
from coach.control import ControlLink
async def main():
    r = Recipe.load('recipes/guacamole.json'); st = State(r, dets=fake_dets())
    link = await ControlLink('coach-ctl.local', 4210).start()
    a = Aimer(link, Calibration.load('calibration.json'), st); asyncio.create_task(a.run())
    for n in ['avocado', 'lime', 'salt']: print(n, a.point_at(n)); await asyncio.sleep(2)
asyncio.run(main())"
```
(`salt` isn't in `fake_dets()`, so it should print `not_visible`.)

**Done when:** with real YOLO dets, `point_at("lime")` lands on the lime in under 0.5 s, follows it when you slide
it 20 cm, and turns off after 30 s.

### 10. Integration (Sat eve, with B and C)

- Replace `FakeAimer` with the real `Aimer` in `main.py`. B's intent router and C's agent tools both call it.
- Put the latency on TFT line 3 (C sends it). Check the TFT shows the step on line 2.
- Profile: the aimer tick must not block the event loop (no `time.sleep`, no heavy work).

**Done when:** a spoken "where's the lime" moves the laser onto the real lime, and the full guacamole run works.

---

## Time plan

| When | You | Handoff (who needs what from you) |
|---|---|---|
| Fri night | K0 with the team. Checkpoints 1-5: solder, servo swap, Q1 laser, fake S3 + ControlLink, FrameGrabber, cam stream, S3 firmware + `B` | **B + C need `fake_s3.py` + `FrameGrabber` pushed tonight.** B tests YOLO on `FrameGrabber(0)`. |
| Sat AM | Checkpoint 5 finished, checkpoint 6 (print + build box) | **B needs `on_button` working by Sat noon** (session reset). |
| Sat PM | Checkpoints 7-9: tilt limits, click calibration, open-loop Aimer | **B + C need the real `point_at` / `laser_off` by Sat 6 pm.** Until then they use `FakeAimer`. You need **B's real `State.dets`** by Sat 10 am (use `fake_dets()` until then). |
| Sat eve | Checkpoint 10: integration with everyone | First full run, recorded. |
| Sun AM | Aim tuning under venue light. Redo calibration at the demo counter. Stretch if all good. | |
| Sun PM | Rehearsals + backup video (everyone) | |

## Pitfalls

- **Power:** feed the ESPs through the 5V pin from the bank rail. Never power servos or the laser *from* an ESP pin.
  Servos stall when they hit something, which means current spikes and ESP resets. Add a 1000 µF cap across the
  servo rail if it resets.
- **Grounds:** bank GND, servo GND, laser GND and both ESP GNDs all tied together. A missing ground = servos twitch or do nothing.
- **Bank auto-off:** a bank can switch itself off at low current. That's only a bench problem; with both ESPs running it stays on.
- **mDNS on a phone hotspot is flaky.** Show the IP on TFT line 1 and put it in `.env` as a fallback. Same for the cam.
- **Hotspot:** 2.4 GHz / "Maximize Compatibility" on, mobile data on. The ESPs can't join 5 GHz.
- **Servo jitter:** deadband + smoothing in the Aimer. Don't send angles that change by 0.1° every tick.
- **Tilt clamp:** the clamp lives in the firmware, so the laptop can't override it. Set it before any demo with people around.
- **Laser safety:** laser off at boot (10 k pulldown on Q1's base), 0.5 s watchdog, tilt clamp, keep faces out of the way
  while testing. Never point it at the judges.
- **Flashing:** unplug J1 (bank power) before flashing over USB.
- **Wires:** leave slack so the pan-tilt can swing. Hot-glue or solder anything that's on a breadboard by Sat night.

## Stretch (only after a full run works)

1. **Laser dot finder** (P4): `find_red_dot` in `vision.py`, writes `State.dot`. Needed for everything below.
2. **Closed-loop correction** (P6): nudge until the dot is within 10 px of the target. Sets `on_target` for real
   and lets telemetry record `on_target` latency.
3. **Blink check:** laser off for 1 frame to tell the dot from red food.
4. **Automatic calibration sweep** using the dot finder instead of clicking.
5. **Calibration in the dashboard** (click in the browser instead of a cv2 window).
