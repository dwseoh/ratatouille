# Architecture

How the pieces fit together. The shared interfaces are in `laptop/coach/contracts.py`. Each one has a fake, so
every part can run without the others.

## 1. The picture

```
   ON THE CHEST                          LAPTOP: ONE Python process (asyncio)                  CLOUD
 ┌───────────────────┐  phone hotspot  ┌──────────────────────────────────────────────────────┐
 │ CAM ESP32         │──MJPEG video───▶│ FrameGrabber (thread) ──▶ YOLO-World detector         │
 │  camera           │                 │      │                    (thread) ──▶ State.dets    │
 ├───────────────────┤                 │      │                                                │
 │ S3 ESP32          │◀─UDP C/T/R──────│ ControlLink (30 Hz) ◀── Aimer (calibration, 30 Hz)    │
 │  pan/tilt servos, │──UDP S─────────▶│                                     ▲ point_at       │
 │  laser, TFT       │                 │                                     │                │
 └───────────────────┘                 │ Pipecat pipeline:                   │                │
                                       │  mic ─▶ Silero VAD ─▶ Deepgram STT ─┼───────────────┼──▶ Deepgram
 mic ─────────────────────────────────▶│        smart-turn      │ interim    │               │    STT + TTS
 speaker ◀─────────────────────────────│                        ├─▶ intent router (local) ─┘   │
                                       │                        │   (<50 ms)                  │
                                       │                        └─▶ final text ─▶ Agent ──────┼──▶ Claude
                                       │                                 │ sentences          │   Opus 5.5
                                       │  Deepgram TTS ◀── Speech ◀──────┘  tools: point_at,   │
                                       │                                     laser_off,       │
                                       │  Check-ins ──▶ Agent                propose_done,    │
                                       │  Watcher (Haiku) ──▶ event log      show_text,       │
                                       │  Steps: the only writer of State.step   show_image   │
                                       │  Telemetry ──▶ State.turns                           │
                                       │  FastAPI dashboard ── reads State ──▶ browser/phone  │
                                       └──────────────────────────────────────────────────────┘
```

Rules that make it fast:

- **Local decisions run alongside the agent, never before it.** Silero VAD, smart-turn, YOLO-World and the
  intent router all run on the laptop. They act at once (move the laser, stop talking, advance the step) while
  Claude is still thinking.
- **Claude only does the talking.** It streams sentences, and each one goes to TTS as soon as it's complete.
- **The ESPs only talk to the laptop**, never to each other or to the cloud.

## 2. What runs where

| Component | File | Runs as |
|---|---|---|
| Frame grabber | `coach/frames.py` | thread, keeps only the newest frame; `CAM_ROTATE` turns frames to match how the board is mounted |
| YOLO-World detector | `coach/vision.py` | thread, per-class thresholds + 2-of-3 smoothing, writes `State.dets` |
| Control link (UDP to the S3) | `coach/control.py` | asyncio task, sends `C` at 30 Hz, ramps servo moves (max 2° per packet) |
| Aimer | `coach/aim.py` | asyncio task: target box → calibration → pan/tilt. Laser off after 30 s idle |
| Dot finder + closed loop | `coach/vision.py`, `coach/dot.py` | optional (`--closed-loop`): finds the real dot (blink check) and corrects the aim |
| Calibration | `coach/calibrate.py`, `coach/calib_web.py`, `coach/jog.py` | run once; saves `calibration.json` |
| Voice pipeline | `coach/voice.py`, `coach/echo.py` | Pipecat: VAD, STT, TTS, barge-in, echo gate |
| Intent router | `coach/intents.py` | keywords → fuzzy match → MiniLM embeddings, on each interim transcript |
| Session | `coach/session.py` | "hey Remy" starts, "start over" resets |
| Agent | `coach/agent.py` | one streamed Claude turn per utterance, tools run mid-stream |
| Steps | `coach/steps.py` | the step machine, the only writer of `State.step` |
| Check-ins | `coach/checkin.py` | wakes every ~1 s; speaks only when it's quiet |
| Watcher | `coach/watcher.py` | a cheaper vision model (Haiku) every few seconds: event log + "looks done, next step?" |
| Telemetry | `coach/telemetry.py` | latency marks per turn |
| Dashboard | `coach/dashboard.py`, `coach/captions.py` | FastAPI in the same process; pages built from `laptop/frontend` into `coach/static/` |
| Recipe tools | `coach/recipe_import.py`, `coach/recipe_images.py` | import a recipe from a web page, PDF or photo; step pictures |
| Wiring | `coach/main.py` | entry point |
| Camera firmware | `firmware/camera/` | MJPEG stream on the camera ESP32 |
| S3 firmware | `firmware/control_s3/` | UDP → servos, laser, TFT |

The laptop is both the backend and the frontend. The dashboard is served by the same Python process, so it reads
`State` directly, and there is nothing else to install or deploy.

## 3. One turn: "where's the lime?"

Rough timeline (estimates; the dashboard shows the measured numbers per turn).

| t (ms) | What happens |
|---|---|
| 0 | User starts talking. Silero VAD fires. If the coach was speaking, it stops and the agent turn is cancelled (barge-in). |
| ~300 | Deepgram sends an interim transcript: "where's the li…" |
| ~450 | Interim says "where's the lime". The intent router returns `POINT lime` in under 50 ms. |
| ~500 | `point_at("lime")` returns at once. The aimer swings the servos using the calibration. |
| ~650 | **The laser is on the lime** before the user has finished the sentence. |
| ~800 | User stops talking. Smart-turn decides the sentence is finished (shorter when it clearly sounds done). |
| ~1150 | Final transcript goes to the agent with the latest frames and a state note (step, visible objects, laser target). |
| ~1150 + ~1200 | First full sentence from Claude ("There it is, the lime's right by the bowl."), spoken immediately. |
| after | Tool calls (`point_at`, `show_text`…) run locally the moment their input is complete. Their results ride along with the next turn instead of costing a second request. |

The user sees the laser move *while they're still talking*, and hears the answer about a second after they stop.
We picked Claude Opus 5.5 in fast mode by benchmarking time to the first spoken sentence (about 1.2 s, against
about 2 s for the alternatives).

If the router matches nothing, nothing happens early and the agent handles it alone. If the lime isn't visible,
`point_at` returns `"not_visible"` and the agent, which already knows from the state note, says "I don't see the
lime" instead of pretending.

Other router intents act the same way:

| Intent | Instant action |
|---|---|
| `POINT` | `point_at(obj)` |
| `DONE` | `propose_done("user")` → the step advances, the TFT updates |
| `STOP` | stop speaking + cancel the agent turn |
| `REPEAT` | an agent turn (it repeats the step in its own words) |
| `START_OVER` | reset the session |

## 4. Check-ins and the watcher

The **check-in loop** wakes about once a second and only acts when the session is active, nobody is talking and no
turn is running:

1. **Step start:** when the step changes, the coach explains the new step and points at its first object.
2. **Silence:** after ~20 s of quiet (45 s in "talk less" mode), it runs a normal agent turn with fresh frames and
   a hidden note asking for a brief comment only if something is worth saying. Often the answer is nothing.

The **watcher** sends a cheaper model 3-4 recent frames every few seconds. It logs what changed ("avocado halved")
to the event log, and when a step looks done it asks once, "looks mashed, ready for the next step?". It never
advances a step by itself: the user's "done" always wins.

If the user starts talking in the middle, barge-in stops it.

## 5. Session flow

```
 idle ──"hey Remy"──▶ active: greeting + step 1 explained + laser points at step 1's objects
                        │
                        ├─ user talks / check-ins / "done" → steps advance → next step explained
                        │
                        └─ last step done → the coach wraps up → idle
 any time: "start over" → stop speech, cancel the agent, laser off, steps reset, history cleared → idle
```

The recipe is picked on the laptop (`RECIPE=recipes/<name>.json`). It holds **facts, not a script**: `goal`,
`how`, `tips`, `point`, `done_when`, `fallback`, plus `aliases` for each object. The agent explains steps in its
own words. Only `fallback` is ever read out word for word, and only when the API fails.

## 6. Connections and failures

All devices join one **phone hotspot** on 2.4 GHz (ESP32s can't use 5 GHz or captive portals). The boards
advertise mDNS names (`coach-cam.local`, `coach-ctl.local`); if mDNS misbehaves, put IPs in `.env`.

| Link | Transport | Notes |
|---|---|---|
| Camera → laptop | MJPEG over HTTP, `http://coach-cam.local:81/stream` | One client at a time. Reconnects with backoff. |
| Laptop ↔ S3 | UDP 4210 | 30 Hz control packet doubles as a heartbeat; status back every 200 ms. See [protocol.md](protocol.md). |
| Mic / speaker | OS audio devices | Separate mic and speaker avoid Bluetooth headset mode (HFP). |
| Laptop → cloud | HTTPS | One long-lived client per service; the prompt cache is warmed at startup. |

| Failure | What the user gets |
|---|---|
| Camera down | Voice still works. The coach says once that it can't see, and the laser stays off. |
| S3 offline | Everything works except the laser and TFT; the dashboard shows the link as red. The S3's own watchdog turns the laser off 0.5 s after packets stop. |
| Claude slow or erroring | It retries once on Haiku, then speaks the step's `fallback` line. Keyword pointing still works. |
| Deepgram down | No voice. The backup is a recorded demo video. |
| No calibration file | The laser can't aim; run the calibration first. |

## 7. State: one writer per field

| Field | Writer | Main readers |
|---|---|---|
| `recipe` | loaded once at startup | everyone |
| `step` | Steps | agent, dashboard, TFT |
| `dets` | YOLO detector | aimer, router, agent note, dashboard |
| `dot` | dot finder (closed loop only) | aimer, dashboard |
| `target`, `on_target` | Aimer | agent note, dashboard, telemetry |
| `transcript`, `speaking`, `user_talking`, `last_activity` | voice | check-ins, dashboard |
| `session` | Session | everyone |
| `events` | anyone, through `State.log()` | agent note, dashboard |
| `turns` | telemetry | dashboard, TFT |

Only touch `State` from the asyncio loop. Threads hand results back with `loop.call_soon_threadsafe`.

## 8. Running each piece alone

Every interface has a fake in `contracts.py` (smoke test: `cd laptop && python -m coach.contracts`).

| Piece | Run it with |
|---|---|
| S3 link | `python tools/fake_s3.py` in one terminal, `python -m coach.main --ctl 127.0.0.1` in another |
| Frames / YOLO | `--cam 0` (laptop webcam) or a video file; `python -m tools.try_yolo 0` shows live boxes and scores |
| Aimer | `FakeLink` + `fake_dets()`: no servos needed to test the maths |
| Voice | `python -m tools.try_voice` (echo agent) or `python -m coach.main --fake-agent` |
| Agent | `python -m coach.agent_cli` (typed, no app) |
| Whole app, typed | `python -m coach.main --type --no-s3 --no-yolo --cam black` |
| Dashboard | `python -m tools.try_dashboard` (fake data) |
