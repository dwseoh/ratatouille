# Constraints and design drivers

Everything we're designing around, in one place, so neither of us loses context. It covers the hard
limits (physics, parts, venue), the decisions already made and why, and what is still unknown.
When something changes, update this file in the same commit.

Related docs: [architecture.md](architecture.md) (how it works), [plan.md](plan.md) (who builds what,
when), [hardware.md](hardware.md) (parts and pins), [protocol.md](protocol.md) (UDP messages).

---

## 1. The project in one paragraph

A chest-worn box with a camera watches you cook and coaches you by voice. A laser on a pan-tilt
mount **points at things** ("grab the lime" → a red dot lands on the lime). The laptop is the brain:
local YOLO and OpenCV find the objects and the dot, Claude decides what to say and what to point at, and
speech runs over Bluetooth. Demo recipe: guacamole.

## 2. Event and team constraints

| Constraint | Value | What it means for us |
|---|---|---|
| Event | Fri 2026-11-20 to Sun 2026-11-22, about 48 h | One recipe, one polished demo. No feature that can't be finished by Sat 10 pm. |
| Team | 2 people | Split by contracts (see plan.md): A = firmware, aim, vision. B = voice, agent, dashboard. |
| Goal | Top 3 overall, no specific track | It needs a "whoa" moment visible from across the room (the laser landing on the lime) plus a tight, rehearsed demo. |
| Shipping | Parts ordered by Tue/Wed | Anything that arrives late is a risk. Prefer parts we already have. |
| Budget | Student budget | We dropped the GoPro harness ($) for a strap. Buy spares only of cheap, fragile parts. |
| Venue | Unknown WiFi, lighting, tables, tools | Bring our own hotspot, tools, lamp and soldering iron. Don't count on the hardware table. |

## 3. Design priorities (in order)

1. **Low latency is the headline.** About 1-1.5 s from end of speech to the first audible word, and the
   laser moving within 0.3 s of naming an object. When a choice trades smartness for speed, measure it.
2. **The laser points reliably,** landing within about 2 cm at arm's length.
3. **It coaches one recipe end to end** without touching the laptop.
4. **Judges can follow it:** a dashboard shows what it sees, hears and decides, with latency numbers.
5. **Wearable and safe:** light, stable on the chest, and the laser can never reach eyes.

## 4. Hard constraints by area

### 4.1 Power

| Fact | Consequence |
|---|---|
| Draw: camera board about 0.3 A, S3 about 0.15 A, 2 servos about 0.5 A moving (up to about 0.65 A each in spikes or stalls), laser 0.03 A, TFT backlight 0.06 A. **About 1.1 A typical, about 2 A peaks.** | The supply needs at least 2.5-3 A at 5 V. The 10 W (2 A) bank is too tight and is only a backup. |
| **Main supply:** 20000 mAh, 22.5 W bank. USB-A gives about 5 V/4.5 A, USB-C 5 V/3 A. About 12 Ah usable at 5 V, so roughly 10 h. | One bank powers everything. It sits in a pocket (350-400 g), with one USB cable up to the box. |
| The bank's 9/12 V modes only switch on when a device asks for them. | Use the bank's **USB-A port** (plain 5 V) → USB-A-to-C cable → a **USB-C breakout with 5.1 kΩ on CC1/CC2** on the carrier (J1, detachable). Fallback with no parts: cut a USB-A cable and solder red/black to the rail. **Never use a USB-PD trigger board set to 9/12 V.** It fries everything. |
| A USB-C breakout gets 0 V from a bank unless CC1 and CC2 each have a 5.1 kΩ resistor to GND. | Buy the breakout with both resistors fitted. From the A port it works regardless; the resistors make a C-to-C cable work too. |
| Power banks hold 5 V, then cut out suddenly when empty. | Watch the bank's LEDs. Charge it fully before judging. Develop on laptop USB power to save the bank. Check whether it supports pass-through charging. |
| Friend's NiMH packs: 4.8 V nominal, about 5.6 V full, about 4.4 V under load. Hitec 2-pin plug. | Good for **servos only**. Too low and unsteady for the ESPs' 5 V pins (they need about 4.5 V+). Backup only, via JP1/J6 in the KiCad schematic. Check polarity with a meter. Don't cut the plug. |
| Running the ESPs from NiMH would need a 3.3 V LDO (MCP1826S-3302) into each 3V3 pin. AMS1117 needs about 1.2 V headroom and won't work. | That's the "NiMH + LDO" schematic. It's a fallback, not the plan. |
| Servos dip the rail when they start moving, which causes ESP resets, jitter and camera glitches. | **C1 1000 µF ≥10 V physically at the servo plugs** (stripe = GND), plus an optional 100 nF. Never drive servos into their end stops (stall current). |
| A USB cable plugged into a board while J1 also feeds 5 V can backfeed. | **Unplug J1 before flashing over USB.** Never fit JP1 with the NiMH plugged into J6. |
| All parts must share one ground. | Common GND everywhere. This is the #1 beginner bug. |

### 4.2 Microcontrollers

| Fact | Consequence |
|---|---|
| **Camera board:** Freenove ESP32-WROVER CAM, about 57 × 28 mm (about 64 mm including the antenna overhang), USB-C at one end, PCB antenna at the other. | Stands upright, USB end down, antenna up. |
| Most camera-board pins are used by the camera and PSRAM. Free: about 13, 14, 32, 33 (avoid strapping pins 0, 2, 12, 15 and PSRAM 16/17). Streaming uses most of the CPU and WiFi. | Our plan keeps servos and laser **off** this board (see §6.1 for the prototype that does the opposite). |
| **Control board:** ESP32-S3 **N8R2** DevKitC clone (8 MB flash, 2 MB quad PSRAM: Arduino PSRAM setting "QSPI PSRAM" or Disabled, not OPI), dual USB-C, about 64 × 28 mm, headers soldered, 2 × 22 pins at 2.54 mm. | Flash via the COM/UART USB-C port. |
| On R8 (octal PSRAM) S3 boards GPIO 35-37 are taken; ours is R2, but we don't use them anyway. GPIO 0, 3, 45 and 46 are strapping pins. | Used pins: 4 pan, 5 tilt, 6 laser, 7 TFT backlight, 8 TFT reset, 9 TFT DC, 10 TFT CS, 11 TFT MOSI, 12 TFT SCK, and 1 for the optional battery divider. |
| ESP logic is 3.3 V. | SG90 signal inputs and the 2N2222 base accept 3.3 V, so no level shifters are needed. |
| The two ESPs never talk to each other. | The laptop is the star hub. ESP-NOW is the fallback only if that's ever needed. |

### 4.3 Camera

| Fact | Consequence |
|---|---|
| **OV3660**, 3 MP, stock lens (field of view about 66°; confirm during calibration). Bad in dim light. Steam fogs the lens. | Bring a lamp. Keep the lens away from the stove. Flat clear window, **not a dome** (a dome blurs). |
| QVGA 320×240, 10 MHz XCLK, JPEG quality 12, `fb_count 1`, `CAMERA_GRAB_WHEN_EMPTY`, WiFi sleep off (Freenove's CameraWebServer settings). On this board 20 MHz + `fb_count 2` + `CAMERA_GRAB_LATEST` gave corrupted, banded frames. Stock example measured about 11 fps at QVGA and 12 fps at HVGA 480×320; VGA failed. | Below the 15 fps hoped for, above the 10 fps YOLO needs. The laptop keeps only the newest frame. If it streams under ~5 fps, the closed loop gets sluggish. Only **one client** can watch the stream at a time. |
| The lens sits on top of the WROVER module, **about 34 mm up from the USB end**, about 3 mm toward the 5V pin row. It's mounted on a 21 mm, 24-pin, 0.5 mm-pitch ribbon held on with tape. | The lens is mid-board, not at the top. **Downward tilt (30-45°)** comes from peeling the camera up and angling it on a small wedge. The ribbon is fragile: no sharp folds. |
| Chest height to counter is about 40-60 cm. | That's the calibration distance. A lime is about 20-25 px wide at QVGA (40-50 px at VGA). Calibrate at the resolution the firmware streams. |
| Wide-angle (120-160°) camera modules exist for this 24-pin connector. | Rejected: fisheye shrinks objects (worse YOLO), distorts the aim math and shrinks the dot. Optional spare: a stock-angle OV3660 with a **75 mm ribbon** (spare for a torn ribbon, and it lets the lens sit next to the pan-tilt at any angle). |

### 4.4 Pan-tilt and laser

| Fact | Consequence |
|---|---|
| Assembled mini pan-tilt with 2 × SG90, about 70 × 38 × 36 mm, about 38 g. SG90: 4.8-6 V, about 1° resolution (about 1 cm at 50 cm), plastic gears. | Accurate enough for a 5 cm lime. Buy 2 × MG90S as spares (metal gears, same size and plug). Centre the servos (pan 90°) before mounting anything. |
| SG90s buzz or jitter while holding, and the pan joint wobbles a little. | Use hardware PWM. Optionally stop the pulses once the dot is on target. The camera loop absorbs the wobble. |
| The camera is **not** on the pan-tilt. Only the laser moves. | Servo vibration never reaches the video. |
| **The camera and pan-tilt must be rigid relative to each other.** The calibration maps camera pixels to servo angles. | Both are fixed to the same solid piece of the box (front shell + top). Body motion is fine because they move together. If they shift relative to each other, the aim is off by that angle. |
| **Parallax:** the laser fires from a different point than the camera sees. The calibration is exact only at the calibration depth. Offset 5 cm → about 1 cm error. 15 cm → about 3 cm. | Keep the laser within about 5 cm of the lens. Earlier estimate of 5-6 cm only counted lens to box top. Counting the laser's height on the pan-tilt (~3.5-4.5 cm, unmeasured) it is **about 8-10 cm** (lens ~5 cm below the top, directly under the pan-tilt). That's ~2 cm error if the counter is 10 cm off the calibration depth: OK for a lime-sized target, and the closed loop corrects it. To halve it: the 75 mm-ribbon spare camera lets the lens sit right under the top wall. Worst case without correction (calibrated at 50 cm, target at 35 or 70 cm): 2.7-3.6 cm, stretched ~1.5x on a counter seen at 40-45° → 4-5 cm. Fixes in software: (1) counter-plane depth (camera height + tilt give each pixel row's distance, so parallax can be computed out); (2) closed loop on the detected dot. Servo slop (~1-2°) and body motion are about as big. |
| KY-008 650 nm, ≤5 mW, about 19 × 15 mm with a 6 mm head. GPIO pins can't drive it. | 2N2222 low-side switch: 1 kΩ base resistor, **10 kΩ base pulldown** (laser stays off during boot). Check the transistor's E-B-C pinout. |
| **Eye safety.** The box sits under the wearer's chin, and other people are around. | Firmware clamps tilt so the laser **never points above horizontal**, plus a **physical stop**. Watchdog: laser off 500 ms after packets stop. Laser off at boot and on WiFi loss. Point at the counter only. |
| Red dot detection fails in bright light and gets confused by red food (tomato). | Blink check (toggle the laser, confirm the dot disappears). Dim the demo area or use a white counter mat. |

### 4.5 Display

| Fact | Consequence |
|---|---|
| **1.54" 240 × 240 ST7789 IPS TFT, SPI**, 3.3 V, backlight about 60 mA. (It replaced the planned 0.96" I2C OLED.) | Uses 6 S3 pins (see §4.2). Firmware uses Adafruit_ST7789. The module's pins labelled "SCL/SDA" are actually SPI clock/data. |
| Confirmed from photos (2026-11-18): "1.54TFT-SPI-ST7789 Ver 1.1" on a PCB with **8 empty 2.54 mm pads** (GND VCC SCL SDA RST DC CS BL), 4 corner mounting holes and an onboard regulator. The 0.7 mm ribbon is already soldered to the PCB. | **Solder an 8-pin header** (or wires) to the pads. Mount it with its 4 corner holes. Board size and hole spacing still need measuring. |
| Contract: 5 lines × 20 characters (line 0 links/status, 1 IP, 2 step, 3 latency/"thinking…", 4 agent message). | `ControlLink.show_text(line, text)` truncates to 20. |
| On the chest, the wearer looks *down* at the box. | Mount the TFT on the **top** of the box facing up (current enclosure draft). |

### 4.6 Networking and radio

| Fact | Consequence |
|---|---|
| ESP32s only do **2.4 GHz** WiFi and can't join eduroam-style or captive-portal networks. | Use a **phone hotspot** for everything. iPhone: turn on "Maximize Compatibility". Android: set the band to 2.4 GHz. |
| The laptop reaches Claude (and cloud STT/TTS) through the same hotspot. | The hotspot needs mobile data. Venue WiFi is only a bonus. |
| Range is 10-20 m indoors, but smooth low-latency video needs about 5 m with line of sight. | Laptop and phone sit on the demo table. The S3 reports RSSI; anything better than about −70 dBm is fine. |
| Bodies absorb 2.4 GHz, and metal blocks it. | Antenna ends point up or out, at least 1 cm from the body. No foil, metal screws or the power bank near antennas. Plastic is fine. |
| Laptop ↔ S3 uses UDP 4210, a 30 Hz control packet that doubles as a heartbeat. The S3 sends status every 200 ms. The camera streams MJPEG on `:81/stream`. mDNS names: `coach-cam.local`, `coach-ctl.local`. | No reconnect logic is needed for UDP. The camera reconnects with backoff. If mDNS fails, put IPs in `.env`. |

### 4.7 Audio

| Fact | Consequence |
|---|---|
| The BT mic and BT speaker pair with the **laptop**, not the ESPs. | They're not in the schematic. The bare speaker component is skipped for v1 (it would need a MAX98357A plus audio streaming to an ESP). |
| One BT device doing both mic and speaker drops into low-quality headset mode (HFP). | Keep the mic and speaker as **two separate devices**. |
| BT output adds 100-200 ms. | The lowest-latency option is the friend's 2.4 GHz lav mic plus wired earbuds. Decide by measuring (plan.md). |
| A speaker near the mic means the coach can hear itself. | Use earbuds, or echo handling, or mute the mic while it talks (barge-in needs care). |
| The "TikTok" lav mics usually come with a USB-C receiver dongle, not Bluetooth. | Check the port on the M2 and bring an adapter. |

### 4.8 Laptop and compute

| Fact | Consequence |
|---|---|
| M2 MacBook. YOLO-World runs on the GPU (`device="mps"`), estimated at 15-25 fps. | **Measure in Phase 0.** Export to CoreML if it's too slow. Run YOLO in a thread so it never blocks the event loop. |
| Hosted YOLO APIs add 100-300 ms per frame plus rate limits. | Run YOLO locally. |
| Claude runs in the cloud. Its latency depends on the model, the size of the input (images!) and the network. | 2-3 frames at about 512 px. Prompt caching for the system prompt and recipe. Stream everything. |

### 4.9 Latency budget (end of speech → first word heard)

| Stage | Target | How |
|---|---|---|
| End-of-speech detection | 250-400 ms | STT endpointing at about 300 ms of silence |
| Final transcript | about 100 ms | streaming STT |
| Claude time to first token | 0.5-1 s | `claude-opus-5-5`, effort low, cached prompt, small frames, short answers. Bake-off against fast mode and `claude-haiku-5-5`. |
| TTS first audio | about 150 ms | streaming TTS, sentence by sentence |
| BT output | 100-200 ms | or wired |
| **Total** | **about 1-1.6 s** | |

Perceived-speed tricks: **point first** (a keyword match moves the laser before Claude answers), an
instant earcon plus "thinking…" on the display, barge-in, and tool results that don't need a second
round trip.

### 4.10 Enclosure and mechanical

| Fact | Consequence |
|---|---|
| Draft (`hardware/enclosure/enclosure.scad`): about 89 × 89 × 38 mm outside, open at the back, with a recessed lid held by 4 countersunk M3 screws. The lid has strap loops plus two eyelets near the top corners for a 2-clip lanyard (both, for double security). | The **lid goes on the back** (chest side). The front holding the camera stays one solid piece, the seam is hidden, the strap loops go there, and the seam faces away from splashes. |
| Layout: the pan-tilt sits on top above the lens. Both boards stand upright, USB ends down, on one **carrier perfboard** screwed to 4 posts on the front wall (diamond: 2 on the centre column, 2 beside the boards; 7 × 9 cm perfboard cut to ~82 × 70). Screws are conductive: use holes with no copper trace, or nylon M3 screws / washers. | Both USB slots and the power-cable notch are in the **floor**. The TFT is on top. Vents are on the sides. |
| All part dimensions are guesses from listings. | The design is deliberately oversized (+4 mm), with slots instead of exact holes and every size a variable. **Measure with calipers, then print a test front wall** before the full box. |
| Breadboard jumpers pop out when worn. | Move to a solderable breadboard or perfboard once it works (Sat night), or hot-glue everything. Use screws or velcro for the boards, not only glue (so a dead board can be swapped at 3 am). |
| A swinging box ruins the aim and the video. | ~50 mm strap through the lid loops, plus a lanyard. Keep it thin, light and close to the chest. The power bank stays off the box. |
| Printing | PLA is fine (PETG near heat). Walls about 2.4 mm. Leave a 0.2-0.3 mm gap so the lid fits. Heat-set inserts last longer than self-tapping screws. |
| The pan-tilt needs a clear swing. | Hitting the box makes the servo stall, which causes a current spike. Leave slack in the servo and laser wires. |

## 5. Decisions made (and what we rejected)

| Decision | Why | Rejected |
|---|---|---|
| Laser on a pan-tilt as the "wow" | Cheap, doable in 48 h, visible across the room | Pico projector (brightness, weight, cost, needs a video source). Galvo (fragile, ±15 V). |
| Servos | Absolute position, 1 pin each, light | Steppers (homing, drivers, heavy, constant current) |
| No IMU | Camera + laser move together. The closed loop (see the dot, nudge it) corrects body motion and calibration error. | MPU6050 (an optional extra later) |
| No ToF sensor (VL53L5CX) | The closed loop already handles distance | |
| Two ESPs, laptop as hub | Isolation: streaming doesn't jitter the servos, and servo spikes don't reset the camera. If one crashes, the other keeps working. | One ESP doing everything (see §6.1) |
| Power bank (USB-A) powers everything | Steady 5 V, no extra parts, safe on the body | 2S LiPo + UBEC, NiMH for everything, 10 W bank |
| Voice-first step control ("done" → AI checks the frames) | False auto-advances would wreck the demo | Fully automatic step detection |
| YOLO-World locally + Claude for reasoning | Fast "where" (every frame) vs smart "what" (per turn) | Hosted YOLO |
| Camera fixed to the box, not on the pan-tilt | The video stays still | |
| Strap + lanyard, printed or project box | Cost | GoPro chest harness |
| 1.54" ST7789 TFT | It's what we have. Colour and bigger is nicer. | 0.96" SSD1306 OLED |
| Exposed pan-tilt on top, dome maybe later | Easy to debug | Dome first |
| Demo recipe: guacamole | No heat, visible state changes, fast | |

## 6. Open questions and unknowns

### 6.1 ⚠️ Two different architectures in the repo right now

Hanjing's prototype (`firmware/camera/LaserPointerCam.ino` + `laser_tracker.py`, commit feb332f)
drives the **servos and laser from the camera board** (GPIO 32 pan, 33 tilt, 13 laser). Its UDP format on
port 4210 is `pan,tilt,laser\n`, it has a 1 s timeout, tilt 30-150°, slew limiting, and a pinhole-model aim
(HFOV 60°) plus a closed-loop trim.

Our plan (docs, schematic, enclosure, `control_s3.ino`, `laptop/coach/`) uses the **S3** for the servos and laser,
with the UDP format `C pan tilt laser seq`, a 500 ms watchdog and tilt 20-100°.

**Needs a decision from both of you.** Recommendation: keep the two-board plan, because everything else
assumes it and it isolates servo noise from the camera. Port the prototype's good ideas (slew limiting, pinhole
open-loop jump + clamped trim) into `control_s3.ino` and `laptop/coach/aim.py`. The prototype is still a
great single-board bench test until the S3 wiring exists.

### 6.2 Things to verify when parts arrive

- [x] TFT: PCB with 8 × 2.54 mm pads (confirmed). Solder a header. Measure the board and mounting holes.
- [ ] Power bank label: confirm 5 V ≥3 A on the port we use. Pass-through charging?
- [ ] Real dimensions (calipers): both boards, lens position and height, pan-tilt base and holes, TFT, USB plug bodies. Then update `enclosure.scad`.
- [ ] 2N2222 pinout (E-B-C?) and KY-008 pin labels.
- [ ] Camera field of view: measure the width seen at 50 cm (listings say 60-66°; HFOV = 2·atan(w/2 ÷ 50)). Find the aim angle with a phone at chest height (expect 35-45° down). Fix the lens with blu-tack, then hot glue.
- [ ] Pan-tilt base: holes → M2/M3 bolts + nuts through the top slots. No holes → zip ties + hot glue. Never tape alone.
- [ ] Servo direction and the safe tilt range after mounting. Set `TILT_MIN/MAX` so the beam never goes above horizontal.
- [ ] YOLO fps on the M2, camera fps over the hotspot, RSSI at 5 m.
- [ ] The lav mic's receiver type (USB-C?) and BT round-trip latency.
- [ ] KiCad schematic opens cleanly (generated by script, not yet opened in KiCad). OpenSCAD file renders (not yet rendered).

### 6.3 Decisions still open (details in plan.md)

- Main voice model: Opus 5.5 low effort / Opus fast mode / Haiku 5.5 / split fast-decide + talk (bake-off numbers)
- The "fast decision model" (we still need its name or link)
- STT: Deepgram streaming vs local whisper. TTS: ElevenLabs Flash / Deepgram Aura / Cartesia / macOS `say`.
- Audio hardware: BT mic + BT speaker vs 2.4 GHz lav + wired earbuds
- How to get the camera tilt: a wedge under the camera module vs an angled box mount
- One board vs two (§6.1)

## 7. Assumptions (unverified, correct these when you can)

The designs above rest on these. If one is wrong, the linked file needs a change.

**Dimensions (from listings or photos, not measured)**
- S3 board 64 × 28 mm. Cam board 57 × 28 mm plus about 6.5 mm antenna overhang. Lens about 34 mm from the USB end and about 10 mm tall above the PCB. → `enclosure.scad`
- Pan-tilt about 70 × 38 × 36 mm. Buyer photo: square base plate, 4 corner holes, 2 horn screw heads sticking out in the middle. **Hole spacing unknown**; the top uses 4 diagonal slots that fit square patterns ~21-30 mm, plus a 14 mm hub clearance. → `pt_r`, `pt_hub_d`
- Camera ribbon: thin flat cable, bend it gently once (no crease), keep the ZIF latch closed. Spare camera is the backup.
- Female headers 8.5 mm tall. USB-C plug bodies about 12 × 7 mm. → `post_len`, USB slots
- TFT board size and mounting-hole spacing: unknown. Drawn as 31 × 35 in `assembly.py`; it sits ~4.5 mm from the pan-tilt plate. → `disp_x`
- Servo + laser wires enter through a hole in the LEFT side wall near the top (a top hole would sit under the TFT). → `wire_hole`

**Things we assumed you have or can get**
- An FDM 3D printer with a bed ≥ 100 × 100 mm, printing PLA with about 0.2-0.3 mm tolerance (see the one-print plan below)
- M3 screws (self-tapping, or with heat-set inserts), perfboard, female headers, a ~50 mm strap, a lanyard
- Soldering iron, multimeter, hand drill or pin vise, calipers or a ruler, hot glue
- An iPhone or Android hotspot with mobile data

**Electrical**
- The bank's USB-A port gives ≥3 A at 5 V (from generic 22.5 W specs, not this unit's label)
- 2N2222 in TO-92 is E-B-C. KY-008 "S" is laser +, "-" is laser −, the middle pin is unused.
- The TFT runs from 3.3 V (it has an onboard regulator, so 5 V may also work; untested)

**How it's worn and used**
- Chest-mounted, about 40-60 cm from the counter. The wearer looks down at the top of the box.
- Power bank in a pocket, about 1 m of cable to the box
- Laptop and phone on the demo table within about 5 m

### If we only get one 3D print

1. **Cardboard first (free, unlimited tries):** tape the real parts to a cardboard box the same size,
   wear it, check the camera view, the pan-tilt swing and the cable routes. Then measure and update the variables.
2. Print **once** with measured numbers. The design is deliberately forgiving (slots, oversized gaps).
3. A print that's slightly off is fixable: PLA cuts with a hobby knife, holes widen with a drill bit or a hot
   soldering-iron tip (ventilate), and anything loose gets foam tape or hot glue.
4. Non-printed fallbacks:
   - an ABS project box (Hammond 1591 series), drilled by hand
   - a clear plastic food container (easy to cut, and judges can see the electronics)
   - foam board or cardboard with hot glue
   - a laser-cut box (makercase.com) if a makerspace has a cutter
5. The one thing that must stay rigid in any version: the camera and the pan-tilt on the same stiff piece
   (plywood, acrylic or a thick plastic plate).

## 8. Gotcha checklist (read before wiring)

- [ ] All grounds connected
- [ ] C1 at the servo plugs, stripe to GND
- [ ] Never a 9/12 V PD trigger. Bank's USB-A port → A-to-C cable → USB-C breakout (5.1 kΩ CC) = plain 5 V.
- [ ] J1 unplugged before USB flashing. Never JP1 + J6 together.
- [ ] Laser off at boot (10 k pulldown), tilt clamped, physical stop fitted
- [ ] No GPIO 35-37 on the S3
- [ ] Never power the laser/servos from an ESP's VIN/5V pin while it runs on USB: on many boards it's input-only (diode), reads ~4.8 V open and collapses under 30 mA. Bench-test from the bank + breakout.
- [ ] Servos centred before mounting. Don't drive them into end stops.
- [ ] Hotspot on 2.4 GHz / Maximize Compatibility, with mobile data on
- [ ] Bank fully charged before judging. Backup demo video recorded.
