# Design notes

The limits we designed around, the decisions we made, what we cut and why, and the gotchas we hit.

## 1. Priorities (in order)

1. **Low latency is the headline.** About 1-1.5 s from the end of speech to the first word heard, and the laser
   moving within 0.3 s of naming an object. When a choice trades smartness for speed, we measured it.
2. **The laser points reliably,** within about 2 cm at arm's length.
3. **It coaches a whole recipe** without anyone touching the laptop.
4. **Others can follow it:** a dashboard shows what it sees, hears and decides, with latency numbers.
5. **Wearable and safe:** light, stable on the chest, and the laser can never reach eyes.

## 2. Constraints by area

### Power

| Fact | Consequence |
|---|---|
| Camera board ~0.3 A, S3 ~0.15 A, 2 servos ~0.5 A moving (spikes up to ~0.65 A each), laser 0.03 A, TFT backlight 0.06 A: **~1.1 A typical, ~2 A peaks.** | The supply needs 2.5-3 A at 5 V. |
| Main supply: a 20000 mAh, 22.5 W power bank. | Its plain-5 V **USB-A port** → USB-A-to-C cable → a USB-C breakout **with 5.1 kΩ on CC1/CC2** on the carrier board. Never a USB-PD trigger set to 9/12 V. |
| Servos dip the rail when they start moving (ESP resets, jitter, camera glitches). | 470-1000 µF (≥10 V) right at the servo plugs, stripe to GND. The laptop also ramps servo moves (max 2° per packet). Never drive servos into their end stops. |
| A board on USB while the carrier also feeds 5 V can backfeed. | Unplug the power input before flashing over USB. |
| All parts must share one ground. | Common GND everywhere. |

### Boards

| Fact | Consequence |
|---|---|
| **Camera:** Freenove ESP32-WROVER CAM. Streaming uses most of its CPU and WiFi, and most pins go to the camera and PSRAM. | It only streams. Servos and laser live on the S3. |
| **Controller:** ESP32-S3 N8R2 DevKitC (8 MB flash, 2 MB quad PSRAM: Arduino "QSPI PSRAM" or Disabled, not OPI). | Pins: 4 pan, 5 tilt, 6 laser, 7-12 TFT, 1 optional battery divider. |
| ESP logic is 3.3 V. | SG90 signal inputs and the NPN laser switch accept 3.3 V; no level shifters. |
| The two boards never talk to each other. | The laptop is the hub. Either board can crash without taking down the other. |

### Camera

| Fact | Consequence |
|---|---|
| OV3660, stock ~66° lens. Bad in dim light; steam fogs the lens. | Bring a lamp. Flat clear window, not a dome (a dome blurs). |
| On this board 20 MHz XCLK + 2 frame buffers + `CAMERA_GRAB_LATEST` gave corrupted, banded frames. The Freenove example settings (10 MHz, 1 buffer, `GRAB_WHEN_EMPTY`) stream cleanly at ~11 fps QVGA. | Higher resolutions help YOLO a lot (a lime is ~20-25 px wide at QVGA, ~40-50 px at VGA), so we test VGA/HVGA against the frame rate. Calibrate at the resolution the firmware streams. |
| The lens sits ~34 mm up from the USB end, on a fragile 24-pin ribbon. | Boards stand upright, USB down; `CAM_ROTATE` turns the frames upright in software. |
| Only one client can watch the stream at a time. | Close browser tabs on the stream before running the coach. |

### Pan-tilt and laser

| Fact | Consequence |
|---|---|
| Mini pan-tilt with 2 × SG90: ~1° resolution (~1 cm at 50 cm), plastic gears, a little wobble. | Accurate enough for a lime. MG90S (metal gears) are drop-in spares. |
| Only the laser moves; the camera is fixed to the box. | Servo vibration never reaches the video. |
| **The camera and pan-tilt must stay rigid relative to each other.** The calibration maps camera pixels to servo angles. | Both are fixed to the same piece of the box. Body motion is fine because they move together. |
| **Parallax:** the laser fires from a different point than the camera sees, so calibration is exact only at the calibration depth. With the laser ~8-10 cm from the lens, a counter 10 cm off the calibration depth gives ~2 cm error. | Calibrate at the demo counter distance. The optional closed loop (`--closed-loop`) corrects the rest by finding the real dot. |
| KY-008 650 nm laser, ≤5 mW, ~30-40 mA. A GPIO can't drive it. | NPN low-side switch (S8050 / S9014 / 2N2222): 1 kΩ base resistor, **10 kΩ base pulldown** so the laser stays off during boot. |
| **Eye safety.** The box sits under the wearer's chin, with other people around. | Tilt limits keep the beam below horizontal (`TILT_MIN/MAX` in `laptop/.env`, applied by the aimer; set them after mounting). The S3 turns the laser off 0.5 s after packets stop and boots with it off. The laptop turns it off after 30 s idle. |
| Red-dot detection struggles in bright light and with red food (tomato). | The dot finder blinks the laser and checks the dot disappears. |

### Display, network, audio, compute

| Fact | Consequence |
|---|---|
| 1.54" 240 × 240 ST7789 TFT (SPI), 5 lines × 20 characters. | Shows link status, IP, step, latency and the last message. Mounted on top, since the wearer looks down. |
| ESP32s only do 2.4 GHz WiFi and can't join captive-portal networks. | Everything joins a phone hotspot (iPhone: "Maximize Compatibility"). The hotspot's mobile data carries the cloud calls. |
| Bodies absorb 2.4 GHz and metal blocks it. | Antennas point up or out, away from the body and the power bank. |
| One Bluetooth device doing both mic and speaker drops to headset quality (HFP), and Bluetooth output adds 100-200 ms. | A separate mic and speaker, or a 2.4 GHz lav mic. An echo gate stops Remy hearing itself through a speaker. |
| YOLO-World runs on the Mac GPU (`mps`). Hosted detection APIs add 100-300 ms per frame. | Run YOLO locally, in its own thread. |
| Claude latency depends on the model, the input size (images!) and the network. | 2-3 small frames, prompt caching for the system prompt and recipe, everything streamed, short answers. |

### Latency budget (end of speech → first word heard)

| Stage | Target | How |
|---|---|---|
| End-of-speech detection | 250-400 ms | VAD + smart-turn |
| Final transcript | ~100 ms | streaming STT |
| Claude first sentence | ~1 s | Opus 5.5 fast mode, low effort, cached prompt, small frames |
| TTS first audio | ~150 ms | streaming TTS, sentence by sentence |
| Audio out | 30-200 ms | wired fastest, Bluetooth slowest |

Perceived-speed tricks: **point first** (the router moves the laser before Claude answers), barge-in, and tool
results that ride along with the next turn instead of costing a second round trip.

### Enclosure

| Fact | Consequence |
|---|---|
| OpenSCAD design (`hardware/enclosure/`), ~89 × 89 × 38 mm, with a recessed lid on the chest side held by 4 countersunk screws, strap loops and lanyard eyelets. | The front holding the camera stays one solid piece, and the seam faces away from splashes. |
| Both boards stand on one carrier perfboard screwed to 4 posts on the front wall. USB slots and the power notch are in the floor; the TFT is on top; servo and laser wires go through the left wall. | Every size is a variable, so a misfit means editing one number and reprinting. |
| Printed holes come out 0.2-0.4 mm small. | Drill or file holes to size, or use self-tapping screws one size down. Heat-set inserts last longer. |
| A swinging box ruins the aim and the video. | ~50 mm strap through the lid loops plus a lanyard; the power bank stays off the box. |

## 3. Decisions (and what we rejected)

| Decision | Why | Rejected |
|---|---|---|
| Laser on a pan-tilt | Cheap, doable in a weekend, visible across the room | Pico projector (brightness, weight, cost), galvo (fragile, ±15 V) |
| Servos | Absolute position, one pin each, light | Steppers (homing, drivers, weight) |
| Two ESPs, laptop as hub | Streaming doesn't jitter the servos, servo spikes don't reset the camera | One ESP doing everything (our first prototype) |
| Camera fixed to the box, not on the pan-tilt | Steady video | |
| No IMU, no depth sensor | Camera and laser move together; calibration (plus the optional closed loop) handles distance | MPU6050, VL53L5CX |
| YOLO-World locally + Claude for reasoning | Fast "where" on every frame, smart "what" per turn; open vocabulary means a new recipe needs no training | Hosted detection |
| Voice-first step control ("done" moves on) | A false automatic advance would be confusing | Fully automatic step detection |
| Claude Opus 5.5 fast mode, Haiku 5.5 as backup | Fastest first sentence in our benchmark (~1.2 s vs ~2 s) | Haiku-only, standard Opus, Gemini (misread the image or hit capacity errors) |
| Wake phrase "hey Remy" | No buttons to find with messy hands | The S3's BOOT button |
| Power bank over USB-A | Steady 5 V, no extra parts, safe on the body | LiPo + UBEC, NiMH packs |
| Demo recipe: guacamole | No heat, visible changes at each step, fast | |

## 4. What we cut

| What | Why | To bring it back |
|---|---|---|
| Live recipe swap, runtime image generation, LLM-chosen videos | Swapping needs the agent, detector and router rebuilt while running; generated images add seconds; the model can invent URLs. The recipe importer runs ahead of time instead. | Rebuild + prewarm the agent and reload the detector classes (~half a day) |
| Knife-near-hand warning | It would fire constantly while cutting. Knife safety is in the step tips and check-ins instead. | Hand tracking + a "blade path" rule (~half a day + tuning) |
| Offline mode | The camera needs the hotspot anyway. A slow API falls back to the step's canned line. | Local STT/TTS + canned lines |
| Timers | Guacamole has nothing to time. | A `set_timer` tool + a spoken reminder (~1-2 h) |
| Priority speech queue | Check-ins only fire when it's quiet, so nothing competes. | Rules inside `say()` (~1 h) |

## 5. Future ideas

| Idea | Who it helps | How |
|---|---|---|
| Laser guides cutting and placement (trace where to cut) | Shaky hands, dyspraxia, spatial difficulties | Closed-loop aiming + object-relative positions ("a line through the middle of the avocado") |
| Blinking or circling dot on the target | Low vision, attention difficulties | Toggle the laser at ~3 Hz, or add a small circular offset |
| Specific praise after each step | Confidence building | A prompt rule after a step completes |
| A physical button as a fallback (short press = repeat, long press = reset) | Speech that speech-to-text struggles with | S3 sends short/long press messages |
| Testing with occupational therapists and the people we designed for | Everyone | Before any claim beyond a demo |

## 6. Gotcha checklist (before wiring)

- [ ] All grounds connected
- [ ] Servo capacitor at the servo plugs, stripe to GND
- [ ] Never a 9/12 V PD trigger: bank USB-A → A-to-C cable → USB-C breakout with 5.1 kΩ CC resistors
- [ ] Carrier power unplugged before USB flashing
- [ ] Laser off at boot (10 kΩ pulldown), tilt limits set so the beam stays below horizontal
- [ ] Never power the laser or servos from an ESP's 5V pin while it runs on USB (often input-only, collapses under load)
- [ ] Servos centred before mounting; never driven into end stops
- [ ] Hotspot on 2.4 GHz with mobile data on
- [ ] Power bank charged; backup demo video recorded
