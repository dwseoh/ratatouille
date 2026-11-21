# Architecture

## Topology

The laptop is a star hub. Everything connects to it, nothing connects to anything else.

| Link | Transport | Direction | Notes |
|---|---|---|---|
| CAM → laptop | MJPEG over HTTP, `http://coach-cam.local:81/stream` | one way | Grabber thread keeps only the newest frame. Reconnects with backoff. |
| laptop ↔ S3 | UDP, port 4210 | both | Laptop sends a control packet at 30 Hz, which doubles as a heartbeat. S3 replies with status every 200 ms. See [protocol.md](protocol.md). |
| mic → laptop | Bluetooth (or a 2.4 GHz lav dongle) | in | OS audio input device |
| laptop → speaker | Bluetooth A2DP (or wired) | out | OS audio output device |
| laptop → cloud | HTTPS (Claude API, STT, TTS) | both | One long-lived client per service, connection reused |

All devices join the **same phone hotspot**, in 2.4 GHz / "maximize compatibility" mode.
The ESPs advertise mDNS names (`coach-cam.local`, `coach-ctl.local`), so the laptop doesn't need
to know IPs. If mDNS misbehaves, put IPs in `.env`.

### Staying connected

- **Camera:** if `read()` fails or no frame arrives for 2 s, reopen the stream (1 s, 2 s, 4 s backoff).
  The dashboard shows "camera lost" and the voice loop keeps working without vision.
- **S3:** the laptop streams control packets nonstop. If the S3 hears nothing for 500 ms it turns the
  laser **off** and holds the servos (fail safe). If the laptop hears no status for 1 s, it marks
  the controller offline and keeps resending. UDP needs no reconnect: as soon as packets flow again it works.
- **Cloud:** one `AsyncAnthropic` client for the app's lifetime (keep-alive connection). The SDK
  retries 429/5xx. Pre-warm the prompt cache at startup.
- **Bluetooth:** paired at the OS level, and the app just opens the default input and output devices.
  Two separate devices (mic plus speaker) avoid the "headset mode" (HFP) quality drop that happens
  when one BT device does both mic and speaker.
- **Microcontroller to microcontroller:** none. If that's ever needed, ESP-NOW (direct
  ESP-to-ESP radio) is the fallback, but the laptop relaying is simpler.

## Loop 1: Pointing (20-30 Hz, local)

```
latest frame ─▶ YOLO-World (target box) ─┐
            └─▶ red-dot finder (HSV) ────┴─▶ aimer ─▶ UDP "C pan tilt laser seq"
```

- YOLO-World (`yolov8s-worldv2`, open vocabulary) with `set_classes(recipe.objects)`.
  On the M2, use `device="mps"`, 640 px input. Export to CoreML if it runs too slowly.
- Dot finder: threshold on very bright pixels with a red hue, take the largest blob near the last
  dot position. Blink the laser to confirm it's the dot and not a red tomato.
- Aimer: calibration gives the first aim (pixel → angles), then a P-controller on the
  remaining error until it's within ~10 px. Clamp tilt so the laser can never point up at faces.

## Loop 2: Voice (on speech): latency is the core goal

```
mic ─▶ VAD + streaming STT ─▶ (point-first keyword match ─▶ loop 1 immediately)
                          └─▶ Claude (streamed) ─▶ sentence splitter ─▶ streaming TTS ─▶ speaker
                                    └─ tool calls: point_at / next_step / set_timer / show_text
```

### Latency budget (end of speech → first audible word)

| Stage | Naive | Target | How |
|---|---|---|---|
| Detect end of speech | 800 ms | **250-400 ms** | STT endpointing at ~300 ms of silence; start early on a confident partial transcript |
| Final transcript | 300 ms | **~100 ms** | Streaming STT: the transcript is already built while you talk |
| Claude first token | 1.5-3 s | **0.5-1 s** | Short prompt, cached prefix, 2-3 small frames, low effort, short answers; measure models |
| TTS first audio | 500 ms | **~150 ms** | Streaming TTS, starting on the first sentence or clause |
| Audio output | 200 ms (BT) | 30-200 ms | Wired is fastest; BT A2DP adds 100-200 ms |
| **Total** | ~3-5 s | **~1-1.6 s** | |

What makes it *feel* instant even inside that budget:
1. **Point first.** A keyword match on the partial transcript ("lime") moves the laser before Claude answers.
2. **Instant acknowledgement.** A short earcon or "mm-hm" plays the moment speech ends, and the display shows "thinking…".
3. **Barge-in.** If the user starts talking, stop TTS right away.
4. **Tools don't cost a second round trip.** Claude speaks, then calls tools. The tools run locally and their
   `tool_result`s are attached to the *next* user turn instead of making a second request.

## Loop 3: Watcher (every 3-5 s)

Sends Claude 3-4 timestamped frames plus the event log, and gets back new events
("avocado halved") and whether the current step looks finished. It only speaks up on its own for
real problems (knife near fingers, burning).

## Displays

- **TFT on the S3** (1.54", 240×240, SPI; 5 lines of 20 chars): connection state, current step, target, last latency, short verbose
  status lines sent by the laptop (`T` command).
- **Laptop dashboard** (FastAPI): live feed with YOLO boxes and dot, step, event log,
  transcript, per-stage latency numbers. This is for the judges, who can't see what your chest sees.

## Team split

- **A:** firmware, servos, laser, YOLO, dot, calibration, aimer (loop 1).
- **B:** voice pipeline, Claude agent, recipe, watcher, dashboard (loops 2 and 3).
- Contract between the two halves: `vision.Detections` and `aim.Aimer.set_target(name)`.
