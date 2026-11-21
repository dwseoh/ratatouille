# Dev plan: tasks, dependencies, contracts, order

The working plan for building the software. What each part *is* lives in
[software-map.md](software-map.md) (deliverables #1-16). This file covers **who does what, in what
order, and what has to be agreed first** so A and B can work at the same time without blocking each other.

**Detailed per-task specs, updated contracts and the cross-dependency table: [task-specs.md](task-specs.md).**
Where the two disagree, task-specs.md wins (it has tonight's decisions).

- **A** = firmware + pointing (loop 1). **B** = voice + brain (loops 2 and 3).
- **HW?** = needs real hardware. Everything marked "no" can be built and tested on a laptop with mocks.
- Size: S = under 2 h, M = half a day, L = most of a day.

## Decisions so far (2026-11-20)

- **3 people, 48 h.** Split (rebalanced, A carries the hardware critical path):
  - **A:** F1-F3, hardware build, P2 control link, P4 dot finder, P5-P6 calibration + aimer
  - **B:** V1-V5 voice (Pipecat), V4 keyword pointing, **P3 YOLO**, S1 telemetry
  - **C:** B0 bake-off, B1 agent, B2 step machine, B3 watcher, B5 fallbacks, **S2 dashboard**
  - Names TBD. S3 integration and S4 rehearsals: everyone.
- **Laptop:** MacBook M5 (YOLO on `mps`).
- **Git:** branch per task, PR, approve and merge immediately, rebase often. Parallel coding agents each get their own git worktree.
- **Audio:** Bluetooth speaker + separate Bluetooth mic (not a headset, to avoid the HFP call-quality mode). Echo is the main risk
  with always-on listening: gate or raise the barge-in threshold while TTS plays, keep the speaker pointed away from the mic,
  try macOS echo cancellation. Test in phase 0.
- **Watcher speed-ups:** safety (knife near hand) is a local YOLO box-distance check, no LLM. The LLM watcher only runs when
  something changes (YOLO object set changes, or the user says "done"), with small frames and a cached prompt.
- **Start / reset:** wake phrase "hey chef" matched in the STT stream (no separate wake-word engine). The S3's BOOT button
  (GPIO0) is reused after boot as a session reset: add `B` (button) to the S3 → laptop messages in protocol.md (C1 change).
- **API keys:** Anthropic (agent + watcher) and Deepgram (streaming STT + Aura TTS). Credits via YC student deals:
  Anthropic $500 + Tier 4 limits, Deepgram $15K. Backups only: OpenAI $1K (speech APIs), Vapi $100 (hosted voice
  agent, switch to it if Pipecat isn't talking by Saturday morning). Keys go in `.env` (gitignored), never in git.
- **Use existing services where possible.** Candidate: **Pipecat** for the voice loop (VAD, streaming STT, LLM, TTS, barge-in), which would replace most of V1-V3. Try it first in phase 0.
- **Listening:** always on (no push-to-talk).
- **TTS:** simple is fine (macOS `say` or Deepgram Aura).
- **Laser:** the LLM decides. Tools `point_at(name)` + `laser_off()`; it stays on until changed, with a safety auto-off after ~30 s idle.
- **Watcher:** cheaper/faster model (Haiku 5.5).
- **Talks like a person** once started (see Start / reset above).
- **Not visible:** if the target isn't in view, say "I don't see it" and turn the laser off.
- **Timers:** cut from v1 (B4 dropped).
- **No separate "fast decision model":** pointing skips the LLM (keyword match), and the talking model streams.

## 1. Task list

### Firmware (A)

| ID | Task | Deliv. | Depends on | HW? | Size | Done when |
|---|---|---|---|---|---|---|
| F1 | Cam firmware: MJPEG stream over the hotspot, mDNS `coach-cam.local` | #1 | cam board | yes | S | ≥15 fps in a browser |
| F2 | S3 control firmware: test UDP `C`/`T`/`R`, status `S`, watchdog, TFT lines (code exists, untested) | #2 | S3, C1 | yes | M | `nc -u` moves servos; laser off 0.5 s after packets stop |
| F3 | Measure tilt limits (horizontal, lowest safe), put them in `TILT_MIN/MAX` | #2 | pan-tilt + holder built | yes | S | Beam can't go above horizontal |

### Pointing (A)

| ID | Task | Deliv. | Depends on | HW? | Size | Done when |
|---|---|---|---|---|---|---|
| P1 | Frame grabber: newest frame only, 0.5 s history, reconnect | #3 | C4 | no (webcam / video file / phone IP-cam app) | S | Survives the source going away and coming back |
| P2 | Control link (30 Hz UDP + status) **+ fake S3** (`tools/fake_s3.py`: listens on 4210, prints, replies `S`) | #7 | C1 | no | S | Python sweep shows up in the fake S3, then the real one |
| P3 | YOLO-World on the recipe objects, in a worker thread, measure fps on the M2 | #4 | C2, C8 | no (webcam + real avocado/lime) | M | ≥80% of frames find avocado, lime, bowl |
| P4 | Laser dot finder + blink check | #5 | P1, laser | partly (laser + webcam works) | M | Dot in ≥90% of frames, no false dot on a tomato |
| P5 | Calibration sweep (5×5 grid → `calibration.json`) | #6 | P1, P2, P4, F1, F2, mounted box | yes | M | First aim within ~5 cm |
| P6 | Aimer closed loop (P-control, sign check, settle) | #6 | P5, C3 | yes | M | Within 10 px in under 1 s |

### Voice (B)

| ID | Task | Deliv. | Depends on | HW? | Size | Done when |
|---|---|---|---|---|---|---|
| V1 | Mic → VAD + streaming STT (pick provider by measuring) | #8 | C5 | no (laptop mic) | M | Partials while talking; final ≤400 ms after you stop |
| V2 | Streaming TTS + speaker that can be cut off mid-word | #11 | C5 | no | M | First audio ≤150 ms after the first sentence; `stop()` silences instantly |
| V3 | Turn-taking manager: priority queue, barge-in cancels TTS **and** the running LLM request, merge overlapping user speech, drop stale proactive lines | #11 | V1, V2 (or mocks), C5, C6 | no | M | Unit tests for each rule in software-map "Turn-taking" |
| V4 | Point-first keyword match: synonyms/plurals, debounce | #9 | V1 partials, C3 (mock Aimer) | no | S | `set_target` <300 ms after the word |
| V5 | Earcon + "thinking…" on speech end | #11 | V2, C12 | no | S | Plays within 100 ms of end of speech |

### Brain (B)

| ID | Task | Deliv. | Depends on | HW? | Size | Done when |
|---|---|---|---|---|---|---|
| B0 | Model bake-off: TTFT for Opus 5.5 low effort / fast mode / Haiku 5.5, same prompt + 3 frames | n/a | API key | no | S | Table of numbers, model picked |
| B1 | Agent: streaming, sentence splitter, tools, deferred tool results, prewarm, history trim per step | #10 | C6, B0 | no (typed text + webcam frames) | M | Typed question → spoken-length sentences + real tool calls; TTFT printed |
| B2 | Recipe + step state machine (single owner of `step`), "done" verification flow | #13 | C7, C8 | no | M | Unit test: talk through all 7 steps, incl. push-back and vision-asks-first |
| B3 | Watcher: every 3-5 s, frames + event log → events + "step done?" | #12 | P1, B1 client, B2, C10 | no (webcam) | M | Event log fills during a webcam run |
| B4 | Timer tool (`set_timer`): countdown, spoken + TFT reminder via V3 | #10 | V3, C12 | no | S | Timer fires through the speech queue, not over the user |
| B5 | Fallbacks: canned per-step lines, offline keyword pointing | #16 | B1, B2, V4 | no | S | Demo runs with WiFi off (after startup) |

### Shared

| ID | Task | Deliv. | Depends on | HW? | Size | Done when |
|---|---|---|---|---|---|---|
| S1 | Latency telemetry: per-turn record with stage timestamps | #15 | C9 | no | S | Every turn logs all keys |
| S2 | Dashboard (FastAPI): feed + boxes + dot, step, transcript, event log, latency bars | #14 | C2, C9, C10 | no (mock State) | M | Judges can follow a run |
| S3 | `main.py` integration: all loops in one asyncio app, YOLO off the event loop, config | all | most of the above | yes for full run | M | Full guacamole run, nothing blocks |
| S4 | Rehearsals + backup demo video | n/a | S3 | yes | M | 5 clean runs, video recorded |

## 2. Shared contracts (agree these first, then don't change them)

Write these as Python stubs (types + docstrings) on day one, then each person codes against them.
If something has to change, both people agree first.

| ID | Contract | Shape | Owner |
|---|---|---|---|
| C1 | **UDP protocol** laptop ↔ S3 | `C pan tilt laser seq`, `T line text`, `R`; `S ...` status. See [protocol.md](protocol.md). Already fixed. | A |
| C2 | **`State`** (shared blackboard) | `recipe`, `step`, `events`, `dets: list[Det]`, `dot: (x, y) \| None`, `target: str \| None`, `transcript: str`, `latency: dict`, `speaking: bool`. **One writer per field**: A writes `dets`/`dot`/`target`, the step machine writes `step`, V3 writes `speaking`/`transcript`. | both |
| C3 | **Aimer API** | `set_target(name \| None)`, `.target_name`, `.on_target: bool`. B calls it, A implements it. | A |
| C4 | **Frames** | `FrameGrabber.latest() -> (frame_bgr, t)`, `.history -> [(t, frame)]` (last ~4 s, every 0.5 s) | A |
| C5 | **Speech API** | `Transcriber(on_speech_start, on_partial(text), on_final(text))`. `Speaker.say(text, priority, created_at)`, `.stop()`, `.speaking`. Priorities: `SAFETY > REPLY > PROACTIVE`. | B |
| C6 | **Agent + tools** | `await agent.turn(user_text, frames, state_note, on_sentence) -> stats`. Tools: `point_at(name)`, `next_step()`, `set_timer(seconds, label)`, `show_text(text)`. Tool handlers return a short string. A turn can be cancelled (barge-in). | B |
| C7 | **Step machine** | `current() -> step`, `propose_done(source: "user"\|"vision"\|"llm") -> "advance"\|"push_back"\|"ask"`, `confirm()`, `advance()`. Only it changes `State.step`. | B |
| C8 | **Recipe JSON** | `{name, objects[], steps[{short, say, done_when}]}` (see `recipes/guacamole.json`). `objects` = YOLO classes = `point_at` names = keyword list. | B |
| C9 | **Latency record** | per turn: `speech_end, transcript_final, request_sent, first_token, first_sentence, tts_first_audio` (+ pointing: `name_heard, set_target, dot_on_target`). Derived keys: `endpoint_ms, stt_ms, ttft_ms, tts_first_audio_ms, point_ms`. | both |
| C10 | **Event log** | `(t, source, text)`, source = `vision`/`user`/`agent`/`system` | B |
| C11 | **Config** | `.env`: `ANTHROPIC_API_KEY, CLAUDE_MODEL, CLAUDE_EFFORT, CAM_URL, CTL_HOST, YOLO_DEVICE` + STT/TTS keys. Read only in `config.py`. | both |
| C12 | **TFT line map** | 0 = links/status, 1 = IP, 2 = step, 3 = latency/"thinking…", 4 = agent message (≤20 chars) | both |

## 3. Dependencies (what blocks what)

```
C1 ─▶ P2 (+fake S3) ─▶ F2 (real S3) ─┐
C4 ─▶ P1 ─▶ P4 (dot) ────────────────┼─▶ P5 calibration ─▶ P6 aimer ─┐
F1 (cam board) ──────────────────────┘        ▲ mounted box + F3     │
C2/C8 ─▶ P3 (YOLO) ──────────────────────────────────────────────────┤
                                                                     ├─▶ S3 integration ─▶ S4 rehearse
C5 ─▶ V1 (STT) ─┬─▶ V4 (keyword) ──▶ Aimer.set_target (C3) ──────────┤
C5 ─▶ V2 (TTS) ─┴─▶ V3 (turn-taking) ─▶ B4 (timer), V5 ──────────────┤
B0 ─▶ B1 (agent) ─┬─────────────────────────────────────────────────┤
C7/C8 ─▶ B2 (steps) ─┴─▶ B3 (watcher, needs P1) ─▶ B5 (fallbacks) ───┤
C9 ─▶ S1 (telemetry) ─▶ S2 (dashboard) ──────────────────────────────┘
```

**Critical path:** cam board + S3 working (F1, F2) → dot finder (P4) → calibration (P5) → aimer (P6).
That chain needs the most hardware and the most tuning, so A should spend the most time on it.
Everything on B's side can run on mocks until integration.

## 4. Mocks that let both people work at the same time

| Real thing | Mock | Used by |
|---|---|---|
| S3 board | `tools/fake_s3.py` (prints packets, sends `S`) | P2, B1 tool tests, S2 |
| Camera board | laptop webcam / phone IP-cam app / a recorded video file as `CAM_URL` | P1, P3, P4, B3, S2 |
| Aimer | `FakeAimer.set_target` that just logs | V4, B1 |
| STT | typed text in the terminal, sent to `on_final` | B1, B2, V3 |
| TTS | `print` + macOS `say` | B1, V3 |
| Detections | a fixed `dets` list | S2, B3 |

## 5. Order of work and what runs in parallel

Contracts first (both, ~1 h): write the C2-C10 stubs together and commit them. After that, A and B run in parallel.

| Phase | A (firmware + pointing) | B (voice + brain) | Sync point at the end |
|---|---|---|---|
| **0. Now, no hardware** | P2 + fake S3, P1 on the webcam, P3 YOLO on the webcam (measure fps) | B0 bake-off, B1 agent with typed input, B2 step machine + tests, V1 and V2 provider try-outs | Contracts committed. Model + STT/TTS picked from numbers. |
| **1. Boards work** | F2 on the real S3 via P2, F1 cam stream, P4 dot finder with laser + webcam | V3 turn-taking, V4 keyword match, V5 earcon, S1 telemetry | `python -m coach` sweeps the real servos; voice loop talks end to end with `say` |
| **2. Mounted** | F3 tilt limits, P5 calibration, P6 aimer | B3 watcher on the webcam, B4 timer, S2 dashboard on mock State | Laser lands on a named object; dashboard shows live state |
| **3. Integrate** | S3 together: real `point_at`, YOLO off the event loop, profile | S3 together: tools drive the real laser, latency on TFT line 3 | **First full guacamole run**, recorded. Fix top 3 problems. |
| **4. Polish** | Aim tuning under venue light | B5 fallbacks, prompt tuning, prewarm | S4: 5 rehearsals, backup video |

**Rules for working in parallel**

- Each task lives in its own file(s) (see software-map). Two people don't edit the same file at the same time. `main.py` gets wired at phase 3 by both, together.
- Small commits, push often, pull before starting a task.
- Anything that touches a contract (C1-C12) gets a heads-up message first.
- When blocked on hardware, switch to the mock, never wait.

## 6. Hardware status (so the software side knows what's real)

- Working: bank 5.2 V, breakout, ESP powered from the bank through its 5V pin, servo sweep (LEDC, `servo_test.ino`), laser from 5 V and GPIO.
- To do: 2nd servo not moving yet (GPIO5, debugging), breakout header to solder, laser holder v4 to print, tilt limits (F3), cam board stream (F1), `control_s3.ino` over UDP (F2).
