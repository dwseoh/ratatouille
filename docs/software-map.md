# Software spec (high level)

What has to be built, how the pieces fit, and what "done" means for each. Use this to plan and
split work. Details (protocols, latency budget, phases) live in [architecture.md](architecture.md),
[protocol.md](protocol.md) and [plan.md](plan.md), and can grow later.

**Headline requirement: low latency.** About 1-1.5 s from end of speech to the first spoken word,
and the laser moving in under 0.3 s after you name an object.

## How it fits together

The laptop is the hub. The two ESPs and the audio devices only talk to the laptop, never to each other.

```
                 ┌──────────────────────────── LAPTOP ─────────────────────────────┐
 CAM ESP ─MJPEG─▶│ Frame grabber ─▶ YOLO-World ─┐                                   │
 (camera)        │               └─▶ Dot finder ─┴─▶ Aimer ─▶ Control link ─UDP 30Hz─┼─▶ S3 ESP
                 │                                     ▲                           │   (servos, laser,
                 │                                     │ set_target(name)          │    TFT display)
                 │                                     │                           │
 Mic ───────────▶│ VAD + streaming STT ─▶ keyword match ┘  (fast path, no LLM)     │
                 │          └──────────▶ LLM agent ─▶ TTS + speaker queue ─────────┼─▶ Speaker
                 │                         │  tools: point_at, next_step, show_text│
                 │ Watcher (every 3-5 s) ─▶│                                       │
                 │                    Recipe + step state machine                  │
                 │                    Latency telemetry ─▶ Dashboard (web) ─────────┼─▶ Judges' screen
                 └─────────────────────────────────────────────────────────────────┘
```

Three loops run side by side:

- **Loop 1, pointing (20-30 Hz, all local):** camera frame → find target and laser dot → move the servos.
- **Loop 2, voice (on speech):** mic → transcript → LLM → speech. A keyword match on the partial
  transcript can move the laser before the LLM answers.
- **Loop 3, watcher (every 3-5 s):** a few recent frames → LLM → events ("avocado halved") and
  "is this step done?".

## Deliverables

Owner **A** = firmware + pointing. Owner **B** = voice + brain. Each part already has a skeleton file.

### Firmware (2 boards, owner A)

| # | Deliverable | File | Done when |
|---|---|---|---|
| 1 | **Cam stream:** MJPEG over the phone hotspot | `firmware/camera/LaserPointerCam.ino` | ≥15 fps in a browser over the hotspot |
| 2 | **S3 control:** UDP packets → servos, laser, TFT text lines. Watchdog turns the laser off if packets stop. The "show logs/state on the display" feature lives here: the laptop sends `T` lines, the S3 just draws them. | `firmware/control_s3/control_s3.ino` | `C 90 60 1 1` over UDP moves the servos and turns the laser on. Laser off 0.5 s after packets stop. |

### Loop 1: pointing (owner A)

| # | Deliverable | File | Done when |
|---|---|---|---|
| 3 | **Frame grabber:** keeps only the newest frame, reconnects on its own | `laptop/coach/frames.py` | Survives a camera power cycle |
| 4 | **Object detection:** YOLO-World with the recipe's object names | `laptop/coach/vision.py` | Finds avocado, lime, bowl in ≥80% of frames |
| 5 | **Laser dot finder:** finds the red dot; blinks the laser to tell it from a tomato | `laptop/coach/vision.py` | Dot found in ≥90% of frames, no false dot on red food |
| 6 | **Calibration + aimer:** calibration maps pixel → pan/tilt; the aimer nudges until the dot is on the target. This is the "move the laser" logic, separate from YOLO. | `laptop/coach/aim.py` | First aim within ~5 cm, settles within 10 px in under 1 s |
| 7 | **Control link:** laptop → S3 UDP at 30 Hz (also the heartbeat), reads S3 status. Covers "config to connect to the ESPs". | `laptop/coach/control.py`, `config.py`, `.env` | Sweeps the servos from Python, shows S3 status |

### Loop 2: voice (owner B)

| # | Deliverable | File | Done when |
|---|---|---|---|
| 8 | **Mic → VAD + streaming STT:** VAD knows when you start and stop talking; streaming STT has the transcript ready as you finish | `laptop/coach/voice.py` | Partial transcripts appear while talking |
| 9 | **Point-first keyword match:** "lime" in the partial transcript → `set_target("lime")` immediately | `laptop/coach/main.py` | Laser starts moving <300 ms after saying "lime" |
| 10 | **LLM agent:** streams sentences, calls tools (`point_at`, `next_step`, `show_text`, `set_timer`) | `laptop/coach/agent.py` | First token measured and shown; tools drive the real laser and step |
| 11 | **TTS + speaker queue + barge-in:** speaks the first sentence as soon as it exists, stops instantly when you talk | `laptop/coach/voice.py` | End of speech → first audio ≤1.6 s; talking over it cuts it off |

### Loop 3 and the brain (owner B)

| # | Deliverable | File | Done when |
|---|---|---|---|
| 12 | **Watcher:** every 3-5 s sends a few frames to the LLM, gets back events and "step done?" | `laptop/coach/main.py` | Event log fills during a run; "done" is checked against the frames |
| 13 | **Recipe data + step state machine:** seeded with guacamole for v1 | `laptop/recipes/guacamole.json`, `laptop/coach/main.py` | Can talk through all 7 steps |
| 14 | **Dashboard (web):** live feed with boxes and dot, current step, transcript, event log, latency bars | `laptop/coach/dashboard.py` | Judges can follow a full run on the screen |
| 15 | **Latency telemetry:** timestamps at every stage, shown on the dashboard and the TFT | everywhere | Per-stage numbers for every turn |
| 16 | **Fallbacks:** canned per-step lines if the API is slow or down; keyword pointing works offline | `laptop/coach/agent.py`, `main.py` | Demo still runs with the network off |

### Interfaces between A and B (agree once, then don't change)

- `Aimer.set_target(name | None)`: B calls it, A's loop does the pointing.
- `State.dets`, `State.dot`: written by A, read by the watcher and the dashboard.
- `ControlLink.show_text(line, text)`: anyone can write a TFT line (max 20 chars).
  Lines: 0 = links/status, 1 = IP, 2 = current step, 3 = latency/"thinking…", 4 = agent message.
- Latency keys: `endpoint_ms`, `stt_ms`, `ttft_ms`, `tts_first_audio_ms`, `point_ms`.

## Design rules

### Who changes the recipe step

Voice and vision both feed in, but **only the step state machine changes the step**. The LLM
*proposes* (through `next_step`) and code checks it.

- **You say "done"** → the watcher checks the last frames → advance, or push back once ("the pit's still in").
- **Vision alone sees a step finished** → it *asks* ("looks mashed, ready for the lime?") and waits for a yes.
- **Safety** (knife near fingers, burning) is the only thing that speaks up without being asked.

### Turn-taking: one speaker

One speech channel. Priority: **safety > reply to you > proactive comment**.

- **You talking always wins.** It cuts TTS mid-word (barge-in) and cancels any LLM request still running.
- **Talking while it's thinking:** don't queue two answers. Cancel, merge the transcripts, send one request.
- **Proactive comments** play only when you're quiet and nothing else is queued. Drop them if
  they're more than ~3 s old.

### Latency

- Point first (keyword match on the partial transcript), before the LLM answers.
- A short "mm-hm" sound the moment you stop talking; the TFT shows "thinking…".
- Stream everything (STT, LLM, TTS) and speak the first sentence before the rest exists.
- Small requests: 2-3 small frames and a cached prompt prefix, pre-warmed at startup.
- Tools run locally; their results ride along with the next turn, with no second LLM round trip.
- Measure every stage (deliverable 15) before optimising anything.

## Not in v1

Multiple recipes, recipe learning, heat/temperature sensing, the ESPs talking to each other,
running without a laptop.
