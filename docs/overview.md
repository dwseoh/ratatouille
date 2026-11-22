# How everything pieces together (v1)

The big picture for all three of us. Interfaces are in `laptop/coach/contracts.py`, task details are in
[task-specs.md](task-specs.md), and what we cut is in [cut-list.md](cut-list.md).

- **A = dw127:** hardware + aiming
- **B = Hanjing:** voice + YOLO + intent router + telemetry
- **C = Chloe:** agent + steps + check-ins + dashboard

## 1. The picture

```
   ON THE CHEST (A)                       DEMO LAPTOP: MacBook M5, ONE Python process            CLOUD
 ┌───────────────────┐  phone hotspot  ┌──────────────────────────────────────────────────────┐
 │ CAM ESP32         │──MJPEG video───▶│ FrameSource (A, thread) ──▶ YOLO-World detector (B,   │
 │  camera           │                 │      │                       thread) ──▶ State.dets  │
 ├───────────────────┤                 │      │                                                │
 │ S3 ESP32          │◀─UDP C/T/R──────│ ControlLinkAPI (A, 30 Hz) ◀── AimerAPI (A, loop)       │
 │  pan/tilt servos, │──UDP S / B─────▶│      │ on_button ──▶ SessionAPI (B)       ▲ point_at  │
 │  laser, TFT       │                 │                                         │            │
 └───────────────────┘                 │ Pipecat pipeline (B, asyncio task):     │            │
                                       │  mic ─▶ Silero VAD ─▶ Deepgram STT ─────┼──────────┼──▶ Deepgram
 Bluetooth/USB mic ───────────────────▶│        smart-turn      │ interim text   │          │    STT + TTS
 Bluetooth speaker ◀───────────────────│                        ├─▶ IntentRouterAPI (B) ──┘   │
                                       │                        │   (local, <50 ms)          │
                                       │                        └─▶ final text ─▶ AgentAPI (C)─┼──▶ Claude
                                       │                                 │ sentences          │   Haiku 5.5
                                       │  Deepgram TTS ◀── SpeechAPI (B) ◀┘  tools: point_at,  │
                                       │                                     laser_off,       │
                                       │  Check-in loop (C) ──▶ AgentAPI     propose_done,    │
                                       │  StepsAPI (C) owns State.step       show_text        │
                                       │  Telemetry (B) ──▶ State.turns                       │
                                       │  FastAPI dashboard (C) ── reads State ──▶ browser    │
 Judges' screen ◀──────────────────────│  http://localhost:8000                               │
                                       └──────────────────────────────────────────────────────┘
```

Rules that make it fast:

- **Local decisions run in parallel with the agent, never before it.** Silero VAD, smart-turn,
  YOLO-World and the intent router are all on the laptop. They act at once (move the laser, stop talking,
  advance the step) while Claude is still thinking.
- **Claude only does the talking.** It streams sentences, and each one goes to TTS as soon as it exists.
- **The ESPs only talk to the laptop**, never to each other or the cloud.

## 2. What runs where

| Component | File | Runs as | Owner |
|---|---|---|---|
| Frame grabber | `laptop/coach/frames.py` | thread, keeps only the newest frame | A |
| Control link (UDP to the S3) | `laptop/coach/control.py` | asyncio task, sends `C` at 30 Hz | A |
| Aimer (open loop from calibration) | `laptop/coach/aim.py` | asyncio task, ~20 Hz | A |
| Click calibration | `laptop/coach/aim.py` + a dashboard page | run once, saves `calibration.json` | A (+C for the page) |
| Dot finder + closed loop | `laptop/coach/vision.py` `find_red_dot` | stretch, pending dw127's OK | A |
| YOLO-World detector | `laptop/coach/vision.py` `Detector` | thread, writes `State.dets` via `call_soon_threadsafe` | B |
| Voice pipeline (VAD, STT, TTS, barge-in) | `laptop/coach/voice.py` | Pipecat pipeline, asyncio task | B |
| Intent router (embeddings + keywords) | `laptop/coach/router.py` (new) | called on each interim transcript, <50 ms | B |
| Session (idle / active) | `laptop/coach/voice.py` or `session.py` | callbacks | B |
| Telemetry | `laptop/coach/telemetry.py` (new) | `TurnLatency.mark()` calls | B |
| Agent | `laptop/coach/agent.py` | coroutine per turn | C |
| Steps | `laptop/coach/steps.py` (new) | plain object, the only writer of `State.step` | C |
| Check-ins | `laptop/coach/checkin.py` (new) | asyncio task, wakes every ~1 s | C |
| Dashboard | `laptop/coach/dashboard.py` | FastAPI (uvicorn) in the same process | C |
| Wiring it all up | `laptop/coach/main.py` | entry point | everyone, at integration |
| Cam firmware | `firmware/camera/` | on the cam ESP32 | A |
| S3 firmware | `firmware/control_s3/control_s3.ino` | on the S3 | A |

The laptop is both the "backend" and the "frontend". The dashboard page is served by the same Python
process, so it can read `State` directly. The pages are built from `laptop/frontend` into `laptop/coach/static/`
(committed), so there is nothing else to install or deploy.

## 3. One turn: "where's the lime?"

Rough timeline. **All times are estimates** until telemetry measures them.

| t (ms, est.) | What happens | Who |
|---|---|---|
| 0 | User starts talking. Silero VAD fires. If the coach was speaking: `SpeechAPI.stop()` + `AgentAPI.cancel()` (barge-in). `State.user_talking = True`. | B |
| ~300 | Deepgram sends an interim transcript: "where's the li…" | B |
| ~450 | Interim says "where's the lime". `IntentRouterAPI.classify()` returns `POINT lime` in <50 ms. | B |
| ~500 | `AimerAPI.point_at("lime")` returns `"ok"` at once. The aimer jumps the servos using the calibration. Telemetry marks `point_called`. | B → A |
| ~650 | **Laser is on the lime** (servos take ~100-200 ms to swing). The user hasn't even finished the sentence. | A |
| ~800 | User stops talking. VAD marks `speech_end`. | B |
| ~800-1100 | Smart-turn decides the sentence is finished (shorter wait when it clearly sounds done, up to ~0.8-1 s of silence when unsure). | B |
| ~1150 | Deepgram final transcript. Pipecat hands it to `AgentAPI.turn(text, frames.recent(3), on_sentence)`. | B → C |
| ~1150 | Agent sends: cached system prompt + recipe, 2-3 small frames, `State.note()` (step, visible objects, laser target), user text. | C |
| ~1600-1900 | First token from Haiku. Telemetry marks `first_token`. | C |
| ~1800-2100 | First full sentence ("There it is, the lime's right by the bowl.") goes to `on_sentence` → `SpeechAPI.say(text, Priority.REPLY)`. | C → B |
| ~2000-2300 | Deepgram TTS first audio plays. Telemetry marks `first_audio`. | B |
| after | Any tool calls (`point_at`, `show_text`…) run locally the moment their input is complete. Their results ride along with the next turn. | C |

What the user feels: the laser moves *while they're still talking*, and the voice answers about 1-1.5 s
after they stop. The biggest single wait is "are they done talking?", which is why smart-turn is part of V1.

If the router matches nothing (`Intent.OTHER`), nothing happens early. The agent handles it alone.
If the lime isn't visible, `point_at` returns `"not_visible"` and turns the laser off. The agent already knows
from `State.note()` that the lime isn't visible, so it says "I don't see the lime" instead of pretending.

Other router intents act the same way, in parallel:

| Intent | Instant action |
|---|---|
| `POINT` | `AimerAPI.point_at(obj)` |
| `DONE` | `StepsAPI.propose_done("user")` → step advances, TFT line 2 updates |
| `STOP` | `SpeechAPI.stop()` + `AgentAPI.cancel()` |
| `REPEAT` | agent turn (it repeats the step in its own words) |
| `START_OVER` | `SessionAPI.reset()` |

## 4. A check-in (the coach speaks up by itself)

The check-in loop (C) wakes about once a second and checks three things: `State.session == "active"`,
`not State.user_talking`, and `not State.speaking`, with no turn running.

1. **Step start:** when `StepsAPI.on_change` fires, the coach explains the new step and points at the
   first object in `Step.point`.
2. **Silence:** if `State.last_activity` is ~20 s old, it runs one normal `AgentAPI.turn()` with fresh frames and
   the hidden text `"[check-in: comment briefly only if something is worth saying, otherwise reply with nothing]"`.
3. The agent answers ("Nice, that's mashed enough. Ready for the lime?") or produces no sentences at all.
   Sentences go out with `Priority.PROACTIVE`.

If the user starts talking in the middle, the normal barge-in stops it. There's no priority queue yet: the
"only when quiet" check above is the whole rule.

## 5. Session flow

```
 idle ──BOOT press──▶ active: greeting + step 1 explained + laser points at step 1's objects
                        │
                        ├─ user talks / check-ins / "done" → StepsAPI advances → next step explained
                        │
                        └─ last step done → "finished" → coach wraps up → idle
 any time: BOOT press or "start over" → SessionAPI.reset():
           Speech.stop, Agent.cancel, Aimer.laser_off, Steps.reset, history cleared → idle
```

The recipe is picked on the laptop (command line or dashboard; only guacamole for now). While idle, the coach
ignores speech. "Hey chef" as a wake phrase is a stretch goal.

The recipe holds **facts, not a script** (`goal`, `how`, `tips`, `point`, `done_when`, `fallback`, plus
`aliases`). The agent explains steps in its own words. Only `fallback` is ever read out word for word, and
only when the API fails.

## 6. Startup and failures

`main.py` starts things in this order:

1. Load `.env` (`config.py`) and the recipe (`Recipe.load`). Create `State`.
2. Start the frame grabber thread (camera may not be up yet, that's fine).
3. Start the control link. Show "starting" on TFT line 0 and the IP on line 1.
4. Load `calibration.json` if it exists. Start the aimer loop.
5. Start the YOLO thread (model load takes a few seconds).
6. `AgentAPI.prewarm()` so the first turn hits a warm prompt cache.
7. Start the Pipecat pipeline, the check-in loop and the dashboard.
8. TFT line 0: "ready, press BOOT". Session is idle.

| Failure | What the user sees | How |
|---|---|---|
| Camera down | Voice still works. The coach says once "I can't see right now". Laser stays off. | `FrameSource.connected` is false, the agent sends no frames, `point_at` returns `not_visible` |
| S3 offline | Everything works except the laser and TFT. Dashboard shows the link as red. | `ControlLinkAPI.online` is false. The S3's own watchdog turns the laser off after 0.5 s with no packets. |
| Claude slow (no first token in ~3 s) or erroring | The coach speaks the step's `fallback` line. Keyword pointing still works. | Agent sets `TurnStats.error` and calls `on_sentence(step.fallback)` |
| Deepgram down | No voice. Backup is the recorded demo video. | n/a in v1 (offline mode was cut) |
| No calibration file | Laser can't aim. Run click calibration first. | Aimer refuses `point_at` with a log line |

## 7. State: one writer per field

| Field | Writer | Main readers |
|---|---|---|
| `recipe` | loaded once at startup | everyone |
| `step` | `StepsAPI` (C) | agent, dashboard, TFT |
| `dets` | YOLO detector (B) | aimer, router, agent note, dashboard |
| `dot` | dot finder (A), stays `None` in open-loop v1 | aimer, dashboard |
| `target`, `on_target` | `AimerAPI` (A) | agent note, dashboard, telemetry |
| `transcript` | voice (B) | dashboard |
| `speaking`, `user_talking` | voice (B) | check-ins, dashboard |
| `session` | `SessionAPI` (B) | everyone |
| `events` | anyone, through `State.log()` only | agent note, dashboard |
| `turns` | telemetry (B) | dashboard, TFT line 3 |
| `last_activity` | voice (B) | check-ins |

Only touch `State` from the asyncio loop. Threads hand results back with `loop.call_soon_threadsafe`.

## 8. Repo map

```
ratatouille/
├── docs/                      plans: overview (this), task-specs, dev-plan, cut-list, protocol, constraints
├── firmware/
│   ├── camera/                A   cam ESP32: MJPEG stream over the hotspot
│   ├── control_s3/            A   S3: UDP → servos, laser, TFT, BOOT button
│   └── servo_test/            A   bench test sketch
├── hardware/enclosure/        A   OpenSCAD: box, lid, laser holder
├── laptop/
│   ├── frontend/              C   source of the dashboard + phone pages (React/Vite); `npm run build` writes coach/static/
│   ├── recipes/guacamole.json C   recipe (facts, aliases)
│   ├── requirements.txt       all (append only, say so when adding something heavy)
│   └── coach/
│       ├── contracts.py       all  shared interfaces + fakes (frozen)
│       ├── config.py          all  reads .env
│       ├── main.py            all  wiring, at integration
│       ├── frames.py          A
│       ├── control.py         A
│       ├── aim.py             A
│       ├── vision.py          A (find_red_dot) + B (Detector)
│       ├── voice.py           B
│       ├── router.py          B   (new)
│       ├── telemetry.py       B   (new)
│       ├── agent.py           C
│       ├── steps.py           C   (new)
│       ├── checkin.py         C   (new)
│       ├── dashboard.py       C   serves the pages and /state, /stream
│       └── static/            C   built pages from laptop/frontend (committed)
└── tools/fake_s3.py           A   pretend S3 on UDP 4210 (new)
```

## 9. Running each piece alone

All fakes are in `contracts.py`. Smoke test: `cd laptop && python -m coach.contracts`.

| You're building | Run it with | Fakes / stand-ins |
|---|---|---|
| A: control link | `python tools/fake_s3.py` in one terminal, a sweep script in another | `fake_s3.py` instead of the board; press Enter there to send a `B` |
| A: frames | `CAM_URL=0` (laptop webcam) or a video file | webcam |
| A: aimer | `FakeLink` + `fake_dets()` in a `State` | no servos needed to test the maths |
| B: voice pipeline | Pipecat with `FakeAgent` (echo bot) in the agent's place | `FakeAgent`, `FakeAimer` |
| B: YOLO | webcam + real avocado/lime on the table | `FakeFrames(0)` |
| B: intent router | unit tests on a list of phrases | `KeywordRouter` is the baseline to beat |
| C: agent | typed text in a terminal, webcam frames | `FakeSpeech(say_aloud=True)`, `FakeAimer`, `FakeFrames(0)`, `fake_dets()` |
| C: steps | unit test walking all 7 steps | `State` with the real recipe |
| C: check-ins | fake clock or a short silence timeout | `FakeAgent`, `FakeSpeech` |
| C: dashboard | a `State` filled with `fake_dets()`, fake turns, fake events | everything fake |

Hand-offs where fakes get swapped for the real thing:
**Sat ~10 am:** B's pipeline + C's agent. **Sat ~6 pm:** A's real aimer behind `point_at`.
