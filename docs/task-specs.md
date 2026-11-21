# Task specs

A detailed spec for every task in [dev-plan.md](dev-plan.md): what to build, where it lives, what it
needs from other people, what it hands to them, and how to know it's done. Read your own tasks plus
the **contracts** section, because that's the part other people code against.

- **A** = hardware + pointing (dw127?). **B** = voice + YOLO + telemetry (**Hanjing**). **C** = brain + dashboard (**Chloe**).
- Times are in the event's local time. Fri = tonight, Sat, Sun.
- "Blocks" lists who is waiting on the task. If you're late on one of those, say so in the chat early.
- If you're blocked, switch to the mock listed and never wait.

---

## 0. Before anyone codes (everyone, ~1 h, Fri night)

### K0. Setup + contract stubs

**Goal:** everyone can run the code, and the shared interfaces exist as real Python files.

1. Add teammates as collaborators on GitHub, then clone the repo.
2. `cd laptop && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY` and `DEEPGRAM_API_KEY`. **Never commit `.env`.**
4. Write `laptop/coach/contracts.py`: the dataclasses and abstract classes from section 1 below, with
   docstrings and no logic. Write a fake of each one in the same file (`FakeAimer`, `FakeSpeech`,
   `FakeLink`) so anyone can import them.
5. Commit and push. **Contracts are frozen after this.** To change one, post in the chat and get a 👍 from
   the people it affects.

**Done when:** `python -c "from coach.contracts import *"` works on all 3 laptops.

---

## 1. Contracts (the shared interfaces)

These replace the C1-C12 table in dev-plan.md where they differ (the changes come from tonight's decisions).

> **`laptop/coach/contracts.py` is now the source of truth.** It has every interface below as real Python, plus a Fake
> of each one. Where this prose and contracts.py disagree, **contracts.py wins**. Smoke test: `cd laptop && python -m coach.contracts`.

### C1. UDP protocol (A). Port 4210, see protocol.md

- **Laptop → S3:** `C pan tilt laser seq` at 30 Hz, `T line text`, `R`.
- **S3 → laptop:** `S pan tilt laser rssi vbat_mv last_seq uptime_ms` every 200 ms.
- **New:** `B` is sent once each time the BOOT button is pressed (debounced, at most 1 per 500 ms).
  The laptop treats it as "reset session".

### C2. `State`, the shared blackboard. Every field has exactly one writer

| Field | Type | Writer | Readers |
|---|---|---|---|
| `dets` | `list[Det]` (name, conf, box) | P3 YOLO (B) | aimer, watcher, dashboard, agent |
| `dot` | `(x, y) \| None` | P4 dot finder (A) | aimer, dashboard |
| `target` | `str \| None` | Aimer (A) | dashboard, agent |
| `on_target` | `bool` | Aimer (A) | telemetry, dashboard |
| `step` | `int` | step machine (C) | everyone |
| `events` | `list[Event]` | `State.log()` (anyone can append, nobody edits) | watcher, agent, dashboard |
| `transcript` | `str` (current user utterance) | voice (B) | dashboard |
| `speaking` | `bool` | voice (B) | watcher (doesn't interrupt), dashboard |
| `session` | `"idle" \| "active"` | voice (B), set by the wake phrase / B button | everyone |
| `latency` | `list[TurnLatency]` | telemetry (B) | dashboard |

`State` is a plain object on one asyncio loop. YOLO runs in a thread and hands results back with
`loop.call_soon_threadsafe`. Never write to `State` from a thread.

### C3. Aimer API (A builds it, B and C call it)

```python
class Aimer:
    def point_at(self, name: str) -> str:    # "ok" | "not_visible" | "unknown_object"
    def laser_off(self) -> None:
    target: str | None                       # what it's pointing at
    on_target: bool                          # dot within 10 px of the target
```
- `point_at` returns at once (it doesn't wait for the servos). If `name` isn't in `State.dets`
  it returns `"not_visible"` and turns the laser off.
- The laser turns off by itself after **30 s** with no new `point_at`, and when the session resets.

### C4. Frames (A)

`FrameGrabber.latest() -> (frame_bgr, t)`, `.history -> list[(t, frame)]`: about one frame every 0.5 s,
covering the last 4 s. `.connected: bool`. `CAM_URL` can be the ESP, a webcam index (`0`) or a video file.

### C5. Speech API (B builds it, C calls it)

```python
class Speech:
    async def say(self, text: str, priority: Priority, created_at: float) -> None
    def stop(self) -> None                   # cut current audio + clear the queue
    speaking: bool
# Priority: SAFETY > REPLY > PROACTIVE
```
- SAFETY interrupts anything. REPLY waits for the current sentence to finish. PROACTIVE only plays
  when the user is quiet and nothing is queued, and is dropped if it's older than 3 s.
- Voice calls C's brain with: `on_user_turn(text: str, t_speech_end: float)`.

### C6. Agent (C)

```python
class Agent:
    async def turn(self, user_text: str, frames: list, on_sentence: Callable[[str], Awaitable]) -> TurnStats
    def cancel(self) -> None                 # barge-in: abort the stream, discard unsent sentences
```
- **Tools:** `point_at(name)`, `laser_off()`, `propose_done()`, `show_text(text)`.
  (`next_step` became `propose_done`. `set_timer` is cut.)
- The agent never talks to Pipecat. Voice hands it text and gets sentences back.

### C7. Step machine (C)

```python
class Steps:
    current() -> dict                        # the recipe step
    propose_done(source: "user"|"vision"|"llm", evidence: str = "") -> "advance"|"push_back"|"ask"
    confirm() -> None                        # user said yes after an "ask"
    reset() -> None
```
It's the only code that changes `State.step`. Every change also sends `T 2 Step n: short` to the TFT.

### C8. Recipe JSON (C)

`{name, objects[], steps[{short, say, done_when}]}`. `objects` are the YOLO classes, the
`point_at` names and the keyword list, all in one place. YOLO also gets the class `hand`
(for the safety check), which isn't a recipe object.

### C9. Latency record (B)

`TurnLatency` timestamps (`time.monotonic()`): `speech_end, stt_final, llm_sent, llm_first_token,
first_sentence, tts_first_audio`, plus for pointing `name_heard, point_called, on_target`.
Derived: `stt_ms, ttft_ms, tts_ms, total_ms, point_ms`. Anyone with a timestamp calls
`telemetry.mark(turn_id, key)`.

### C10. Event log (anyone appends)

`Event(t, source, text)` where `source` is `vision` / `user` / `agent` / `system` / `safety`.

### C11. Config (everyone)

Only `config.py` reads `.env`. Keys: `ANTHROPIC_API_KEY, DEEPGRAM_API_KEY, CLAUDE_MODEL,
WATCHER_MODEL, CAM_URL, CTL_HOST, YOLO_DEVICE, RECIPE`.

### C12. TFT lines (anyone, through `ControlLink.show_text(line, text)`, 20 chars max)

0 = links/status, 1 = IP, 2 = step, 3 = latency / "thinking…", 4 = agent message.

### C13. Session (B owns it)

`idle` → "hey chef" heard in the transcript → `active` (coach greets you and says step 1).
A `B` button press, or "start over", resets: `Steps.reset()`, `Aimer.laser_off()`,
`Speech.stop()`, clears history, goes back to `idle`. While idle, only the wake phrase is listened for.

---

## 2. Task specs

### A: hardware + pointing

#### H1. Hardware build (A, Fri night → Sat afternoon)
- **Tonight:** solder the USB-C breakout header. Fix the 2nd servo: swap the two servo plugs to
  find out whether the servo or the pin is bad. Print laser holder v4.
- **Sat AM:** print the box, mount the carrier board, wire the 5 V rail (bank → breakout → rail → both ESPs'
  5V pins, servos, laser via Q1).
- **Sat PM:** mount the pan-tilt on the box, strap and lanyard, then cable management.
- **Blocks:** F3, P5, P6, and every full run.
- **Done when:** worn on the chest for 10 min with the servos sweeping, no resets, nothing loose.

#### F1. Camera firmware (A, Fri night, S)
- **File:** `firmware/camera/`. Start from the CameraWebServer example. Connect to the hotspot, set mDNS `coach-cam`,
  stream at `:81/stream`, QVGA or VGA, JPEG quality ~12.
- **Edge cases:** reconnect to WiFi if the link drops, and make sure the lens orientation matches how it's mounted
  (flip/mirror settings).
- **Blocks:** P4 on real frames, P5, the watcher on real frames. Until then, everyone uses a webcam.
- **Done when:** ≥15 fps in a laptop browser over the hotspot, and it comes back by itself after a power cycle.

#### F2. S3 control firmware (A, Fri night → Sat AM, M)
- **File:** `firmware/control_s3/control_s3.ino` (already written, untested).
- Test with `nc -u coach-ctl.local 4210`, then `C 90 60 1 1`. Check the watchdog (laser off 0.5 s after
  packets stop), the `T` lines on the TFT, and the `S` status coming back.
- **Add:** the `B` message on a BOOT (GPIO0) press, debounced.
- **Blocks:** P2 with the real board, P5. **Mock until then:** `tools/fake_s3.py`.
- **Done when:** the Python sweep (P2) moves the real servos, and pressing BOOT prints `B` on the laptop.

#### F3. Tilt limits (A, Sat PM, S)
- Mount everything, then use `servo_test` `u`/`d` to find the tilt where the beam is horizontal and the lowest
  angle before it hits anything. Put those in `TILT_MIN/TILT_MAX` in `control_s3.ino`.
- **Done when:** commanding tilt 0 and 180 can't point the beam above horizontal.

#### P2. Control link + fake S3 (A, Fri night, S)
- **Files:** `laptop/coach/control.py` (mostly written), `tools/fake_s3.py` (new).
- `fake_s3.py`: listens on UDP 4210, prints each packet on one line, replies `S` every 200 ms,
  and sends `B` when you press Enter.
- `ControlLink`: sends at 30 Hz, parses `S` into `status`, `online` is true if `S` was seen in the last 1 s, and
  `on_button(cb)` fires on `B`. Resolve mDNS once, and re-resolve if offline for 5 s.
- **Blocks:** the dashboard link status (C), the B-button reset (B), P5.
- **Done when:** a 10-line sweep script moves the fake S3, then the real one, with no code change other than `CTL_HOST`.

#### P1. Frame grabber (A, Fri night, S)
- **File:** `laptop/coach/frames.py` (mostly written). Only keep the newest frame (no buffering), keep the history ring,
  reconnect with backoff, and set `connected`.
- **Test:** unplug the webcam or kill the stream, then plug it back in, and it recovers in under 3 s.
- **Blocks:** P3, P4, the watcher, the dashboard. This is the first thing A should push.

#### P4. Laser dot finder (A, Sat AM, M)
- **File:** `laptop/coach/vision.py` `find_red_dot` (draft exists).
- Threshold for very bright + red, take the blob closest to the last dot, and size-filter it.
- **Blink check:** when unsure, turn the laser off for 1 frame. If the blob disappears, it's the dot.
  Use it to check the dot after a big jump, not every frame.
- Writes `State.dot`, or `None` if not found.
- **Test set:** record 30 s of video (laser on a tomato, a red bowl, under bright light) and score it offline.
- **Blocks:** P5, P6. **Done when:** the dot is found in ≥90% of frames, with no false dot on the tomato.

#### P5. Calibration sweep (A, Sat PM, M)
- **File:** `laptop/coach/aim.py` + `python -m coach.calibrate`.
- Sweep 5×5 pan/tilt angles over the counter, wait 300 ms each, record the dot pixel, fit
  `Calibration` (the polynomial is already written), and save `calibration.json`.
- **Needs:** the box mounted (camera and pan-tilt fixed to each other), F1, F2, P4.
- **Done when:** `point_at` on any visible object lands within ~5 cm on the first jump.

#### P6. Aimer (A, Sat PM, M)
- **File:** `laptop/coach/aim.py` `Aimer`, implementing C3.
- Loop at 20-30 Hz: target box centre (from `State.dets`) and dot. No dot → jump using the calibration. Dot seen →
  P-control on the pixel error, clamped to ≤3° per tick. Check the sign once (if the error grows, flip it).
- `point_at` checks visibility and returns `not_visible`. Plus the 30 s auto-off and `laser_off`.
- When the target moves (the user moves the bowl), keep following it.
- **Mock for others before it's real:** `FakeAimer` in contracts.py.
- **Done when:** the dot is within 10 px in under 1 s, and an object moved 20 cm is reacquired.

### B: voice + YOLO + telemetry

#### V1. Pipecat voice pipeline (B, Fri night, M). **Most important task tonight**
- **File:** `laptop/coach/voice.py` (replace the skeleton).
- Pin the Pipecat version in `requirements.txt`. Pipeline: local audio in → Silero VAD →
  Deepgram streaming STT → **our own processor** (calls C's `on_user_turn` / `Agent.turn`) →
  Deepgram Aura TTS → local audio out. Check the class names against the pinned Pipecat version's
  examples (search their repo for a local-audio example and start from it).
- The LLM step is **our processor, not Pipecat's built-in LLM service**. It gets the final transcript,
  calls `agent.turn(text, frames, on_sentence)`, and turns each sentence into a TTS frame. That keeps
  C's code plain Python, testable with typed input.
- **Barge-in:** when Pipecat detects the user speaking, call `agent.cancel()` + `Speech.stop()`.
- **Turn end:** use Pipecat's **smart-turn** model as part of V1 (not optional any more): it decides whether
  the sentence sounds finished, so the silence wait can drop to ~0.3 s for complete sentences. Keep a
  ~0.8-1.0 s silence fallback for when smart-turn isn't sure.
- **Mock for C until done:** `FakeSpeech` (print + macOS `say`) and typed input.
- **Blocks:** everything spoken. **Deadline: Sat 10 am.** If it isn't talking end to end by then, switch to Vapi.
- **Done when:** you say "hey, what's step one", it answers out loud, and talking over it stops it.

#### V2. Speech API: priorities + proactive (B, Sat AM, S)
- **File:** `voice.py`, implementing C5 on top of V1: push a TTS frame from outside the pipeline (for the watcher and check-in
  lines), the priority rules, dropping PROACTIVE lines older than 3 s, and `speaking`.
- **Blocks:** C's watcher and check-ins speaking up.
- **Done when:** unit tests show SAFETY cuts in, PROACTIVE waits for silence, and stale lines get dropped.

#### V3. Echo + Bluetooth audio (B, Sat AM, S)
- Pair the BT speaker + BT mic on the Mac and choose them as input and output. Check the mic isn't the speaker's
  built-in mic (that switches to low-quality call mode).
- While the coach is talking (+0.3 s after), ignore the mic, except a "stop" / "wait" keyword which
  calls `Speech.stop()`.
- **Done when:** a 2 min conversation with the speaker at demo volume and zero self-interruptions.

#### V4. Point-first keyword match (B, Sat AM, S)
- **File:** `voice.py` `keyword_target` (draft exists). Run it on **interim** transcripts, using the recipe objects + plurals
  + a small synonyms list (`"cilantro": ["coriander"]`, `"board": "cutting board"`).
- Only call `aimer.point_at` if the object is in `State.dets` and isn't already the target.
  Debounce: once per object per utterance.
- Record `name_heard` and `point_called` for telemetry.
- **Done when:** saying "where's the lime" moves the laser <300 ms after the word.

#### V4b. Intent router (B, Sat AM, S-M)
- **File:** `laptop/coach/intents.py` (new), implementing `IntentRouterAPI` from contracts.py. Start from
  `KeywordRouter` in contracts.py (keywords + recipe aliases) and build up:
  1. **Exact keywords + recipe `aliases`** (0 ms), then **`rapidfuzz`** for speech-to-text misspellings ("lyme", "silantro").
  2. **Local embedding model** for paraphrases: `sentence-transformers` `all-MiniLM-L6-v2` (~80 MB, CPU or `mps`).
     Write ~10 example phrases per `Intent` (POINT, DONE, REPEAT, STOP, START_OVER), embed them once at startup,
     and pick the closest intent by cosine similarity. Below a threshold (~0.5, tune it), return `OTHER`.
     For POINT, still find the object name with step 1.
- Runs on **interim** transcripts, **in parallel** with the agent, never in front of it. Actions: POINT → `aimer.point_at`,
  DONE → `steps.propose_done("user")`, STOP → `speech.stop()`, START_OVER → `session.reset()`.
- **Done when:** <50 ms per call on the M5, and a list of ~30 test phrases classifies right
  ("I'm done with that" → DONE, "where's the green one" → POINT lime, "how chunky?" → OTHER).

#### V5. Wake phrase + session reset (B, Sat AM, S)
- Implement C13. Fuzzy-match "hey chef" in interim transcripts ("hey chef", "hey jeff", "a chef"…).
- Hook `ControlLink.on_button` → reset. "Start over" → reset.
- **Done when:** idle → "hey chef" → greeting + step 1; BOOT press → laser off, back to idle.

#### P3. YOLO-World detector (B, Fri night → Sat AM, M)
- **File:** `laptop/coach/vision.py` `Detector` (draft exists). Classes = recipe objects + `hand`.
- Run in a worker thread on the latest frame only, as fast as it goes. Write `State.dets` via
  `call_soon_threadsafe`. Measure fps on the M5 with `mps`, and try `imgsz` 480 vs 640.
- Per-class confidence thresholds. Smoothing: an object counts as "visible" if it was seen in 2 of the last 3 frames.
- Bring real avocados, limes and onions, and test on a real counter.
- **Blocks:** A's aimer (target boxes), C's watcher triggers, the dashboard.
  **Mock until then:** a fixed `dets` list.
- **Done when:** ≥10 fps, and avocado / lime / bowl found in ≥80% of frames.

#### S1. Latency telemetry (B, Sat PM, S)
- **File:** `laptop/coach/telemetry.py` (new), implementing C9. Each turn gets an id. Write a JSON line per turn to `logs/latency.jsonl`.
  Put `ttft` / `total` on TFT line 3.
- **Done when:** every turn has all the keys, and the dashboard can draw them.

### C: brain + dashboard

#### B0. Model bake-off (C, Fri night, S)
- **File:** `tools/bench_llm.py`. The same system prompt + recipe + 3 frames at 512 px + one question, 10 runs each:
  Opus 5.5 at low effort, Opus 5.5 fast mode, Haiku 5.5. Record the time to the first token and to the first
  sentence, and the cost. Use prompt caching on the system prompt + recipe.
- **Done when:** a table of numbers is pasted in the chat and `CLAUDE_MODEL` is chosen.

#### B1. Agent (C, Fri night → Sat AM, M)
- **File:** `laptop/coach/agent.py` (draft exists), implementing C6.
- Streaming Messages API with the cached system prompt + recipe. Each turn sends: user text, 2-3 frames, and a short
  **state note** (current step, visible objects from `State.dets`, laser target, last 5 events).
  Including the visible objects lets the model say "I don't see the lime" *before* it calls a tool.
- Sentence splitter: cut on `. ! ?`, minimum ~4 words, and send each sentence to `on_sentence` right away.
- Tools run locally as soon as their input is complete. Their results are added to the next turn's
  context (no second round trip), except when a tool returns `not_visible`, which the next turn sees.
- `cancel()` aborts the HTTP stream. History is trimmed to the current step + the last 6 turns.
- `prewarm()` sends a tiny request at startup, so the first real turn hits a warm cache.
- **Mock:** typed text in a terminal, `FakeSpeech`, `FakeAimer`, webcam frames.
- **Blocks:** V1 integration (Sat AM). **Done when:** typed "where do I start?" gives 1-2 spoken-length
  sentences + a `point_at("avocado")` call, with the first-token time printed.

#### B2. Step machine (C, Fri night, M)
- **File:** `laptop/coach/steps.py` (new), implementing C7.
- **User says done** → check with the watcher's latest verdict for this step (or ask the watcher now, ≤2 s).
  If it says not done, push back **once** with the reason. If the user insists, advance.
- **Vision says done** → return `"ask"`. The agent asks "looks mashed, ready for the lime?", and `confirm()` follows a yes.
- **LLM proposes** (the `propose_done` tool) → same as user.
- **Tests:** `tests/test_steps.py` talks through all 7 steps, including push-back and vision-asks-first.

#### B3a. Check-ins (C, Sat AM, S). **Replaces the watcher below for v1 (decided 2026-11-21)**
- **File:** `laptop/coach/checkin.py` (new). It reuses `Agent.turn`: no second model, no event log, no background vision loop.
- **Triggers:** (1) a new step starts → the coach explains it and points (`point` from the recipe);
  (2) ~20 s of silence during a step → one turn with fresh frames and the hidden user text
  `"[check-in: comment briefly only if something is worth saying, otherwise reply with nothing]"`.
- Only fires when the mic is quiet, the coach isn't speaking, and no turn is running. An empty reply = say nothing.
- Speaks through `Speech.say(text, PROACTIVE)`. The priority rule for now is just "only when quiet", which already holds.
- **Done when:** during a webcam run it speaks up at least once per step without being asked, and never talks over the user.

#### B3. Watcher (stretch, only after a full run works)
- **File:** `laptop/coach/watcher.py` (new).
- **Trigger:** only when something changes: the set of visible objects changed, the user said "done", or 10 s
  passed during a step. Never while `State.speaking`.
- Haiku 5.5 with 3-4 frames from `history` (≤384 px), the current step's `done_when`, and the event log.
  Structured output: `{events: [str], step_done: bool, confidence, note}`.
- New events are appended to the log. `step_done` with high confidence → `Steps.propose_done("vision")`.
- ~~Safety, local knife-near-hand warning~~: **cut** (fires constantly while cutting; knife safety is in the step `tips`).
- **Done when:** a webcam run of halving an avocado logs "avocado halved" within ~5 s of it happening.

#### B5. Fallbacks (C, Sun AM, S)
- An API timeout (first token > 3 s) or error → speak the step's canned `say` line, and keyword pointing still works.
- No camera → the agent says so once, and voice still works.
- **Done when:** with the API slowed or failing, all 7 steps still get a spoken canned line. (Offline mode / WiFi-off demo is cut, see cut-list.md.)

#### S2. Dashboard (C, Sat PM → Sun AM, M)
- **File:** `laptop/coach/dashboard.py` (draft exists). FastAPI in the same process:
  `GET /stream` (MJPEG with boxes + dot + target), `GET /state` (JSON), `GET /` (one HTML page polling every 250 ms).
- Panels: live feed, step n/7 + `short`, live transcript, event log, latency bars for the last turn
  (stt / ttft / tts / point), link status (cam, S3, battery).
- **Mock:** a fake State filled with fixed data until the others are real.
- **Done when:** a judge can tell what's going on without anyone explaining it.

### Everyone

#### S3. Integration (all, Sat evening, M)
- **File:** `laptop/coach/main.py`: one asyncio app, YOLO and frames in threads, voice in Pipecat's task,
  the aimer loop, watcher, dashboard.
- **Order:** voice ↔ agent (Sat AM, B+C) → agent tools ↔ real aimer (Sat PM, A+C) → watcher on real
  frames → everything together.
- **Done when:** a full guacamole run, start to finish, recorded on video.

#### S4. Rehearsals (all, Sun)
- 5 full runs with the real food. Note the top 3 problems after each run and fix only those.
- Record a backup demo video. Practise a 2-minute pitch.

---

## 3. Cross-dependencies: who needs what from whom

| Needs | From | What exactly | By | Mock until then |
|---|---|---|---|---|
| everyone | all | `contracts.py` committed | Fri, first hour | none |
| B (P3), C (watcher), C (dashboard) | A | P1 frame grabber pushed | Fri night | `cv2.VideoCapture(0)` directly |
| A (aimer), C (watcher, dashboard) | B | P3 writing `State.dets` | Sat 10 am | fixed `dets` list |
| B (V1 integration) | C | `Agent.turn` / `cancel` working with typed input | Sat 10 am | echo bot: repeats the user |
| C (agent speaks) | B | V1 talking end to end | Sat 10 am | `FakeSpeech` (`say`) |
| C (watcher proactive) | B | V2 `say(text, priority)` | Sat noon | `FakeSpeech` |
| B (V4), C (tools) | A | Aimer `point_at` / `laser_off` real | Sat 6 pm | `FakeAimer` |
| A (P5, P6) | A | H1 box mounted, F1, F2 | Sat 2 pm | webcam + loose pan-tilt for P4 |
| B (V5 reset) | A | F2 sends `B`, P2 `on_button` | Sat noon | `fake_s3.py` Enter key |
| C (dashboard latency) | B | S1 telemetry | Sat 6 pm | fake numbers |
| C (B2 "done" check) | C | B3 watcher verdict | Sat PM | B2 always says "advance" |

### Where people overlap (talk before editing these)

- `vision.py`: P3 `Detector` is B's, `find_red_dot` is A's. Same file, different functions. Fine, but pull first.
- `main.py`: nobody edits it before S3, except to add their own loop in a separate PR.
- `requirements.txt`: append only. Say so in the chat when you add something heavy (Pipecat).

## 4. Timeline at a glance

| When | A | B | C | Sync |
|---|---|---|---|---|
| Fri night | K0, P1, P2 + fake S3, F1, F2 test, solder + 2nd servo, print holder | K0, **V1 Pipecat**, P3 fps test | K0, B0 bake-off, B1 typed, B2 + tests | 11 pm: contracts in, model picked |
| Sat AM | Print + build box (H1), P4 dot finder | V2, V3, V4, V5, P3 tuning | B1 ↔ V1 wired together, B3 watcher start | **10 am: voice talks to the agent** (else Vapi) |
| Sat PM | Mount, F3, P5 calibration, P6 aimer | S1 telemetry, help A test aiming | B3a check-ins, S2 dashboard | **6 pm: spoken word → laser on a real avocado** |
| Sat eve | S3 integration (all) | | | First full run recorded |
| Sun AM | Aim tuning under venue light | Echo / latency tuning | B5 fallbacks, prompt tuning | |
| Sun PM | S4 rehearsals + backup video (all) | | | Demo |
