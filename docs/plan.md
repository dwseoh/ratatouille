# Software plan

How we build it, in what order, and how we know each piece works. The design itself is in
[architecture.md](architecture.md). This file is the **implementation plan**.

## Goals

1. **Low latency is the headline.** It should feel conversational: about 1-1.5 s from end of speech
   to the first word, and the laser moving in under 0.3 s when you name an object.
2. **The laser points reliably** at the object being talked about. The dot lands within about 2 cm at arm's length.
3. **It coaches one recipe (guacamole) end to end** without touching the laptop.
4. **The demo is legible to judges:** a dashboard shows what it sees, hears, and decides, with latency numbers.

**Non-goals for v1:** multiple recipes, recipe learning ("record grandma"), heat or temperature sensing,
the ESPs talking to each other, running without a laptop.

## High-level components

| # | Component | Lives in | Owner | Input → Output |
|---|---|---|---|---|
| 1 | Camera firmware | `firmware/camera` | A | sensor → MJPEG stream |
| 2 | Control firmware | `firmware/control_s3` | A | UDP `C`/`T` → servo angles, laser, display, `S` status |
| 3 | Frame grabber | `coach/frames.py` | A | MJPEG → latest frame + 0.5 s history |
| 4 | Control link | `coach/control.py` | A | pan/tilt/laser state → 30 Hz UDP; status in |
| 5 | Perception | `coach/vision.py` | A | frame → `[Det]`, dot (x, y) |
| 6 | Aimer + calibration | `coach/aim.py` | A | target name + dets + dot → pan/tilt |
| 7 | Voice in (VAD + STT) | `coach/voice.py` | B | mic → partial / final transcripts, speech-start |
| 8 | Voice out (TTS) | `coach/voice.py` | B | sentences → audio, barge-in stop |
| 9 | Agent | `coach/agent.py` | B | transcript + frames + state → streamed sentences + tool calls |
| 10 | Recipe and state | `coach/main.py`, `recipes/` | B | step machine, event log |
| 11 | Watcher | `coach/main.py` (to split out) | B | frame history → events, "step done?" |
| 12 | Dashboard | `coach/dashboard.py` | B | state + annotated frames → web page |
| 13 | Latency telemetry | everywhere | both | timestamps at each stage → dashboard + log |

### Contracts between the two halves (agree on these Friday at 8 pm, then don't change them)

- `Aimer.set_target(name: str | None)`: B's agent calls it, A's loop does the rest.
- `State.dets: list[Det]`, `State.dot`: written by A, read by the dashboard and watcher.
- `ControlLink.show_text(line, text)`: anyone can write display lines (max 20 chars). Line map: 0 = links/status,
  1 = IP, 2 = current step, 3 = latency/"thinking…", 4 = agent message.
- `latency` dict keys: `endpoint_ms`, `stt_ms`, `ttft_ms`, `tts_first_audio_ms`, `point_ms`.

## Phases

### Phase 0: before the hackathon (Tue-Thu)

- [ ] Parts ordered by Wed (see hardware.md)
- [ ] API keys work: Claude, plus the chosen STT and TTS candidates
- [ ] `pip install -r requirements.txt` on the M2. YOLO-World runs on a webcam image with `device="mps"`, and we **write down the fps**.
- [ ] Arduino IDE / PlatformIO set up with esp32 board package 3.x, Adafruit ST7789 + GFX (servos use LEDC directly, no servo library)
- [ ] Flash the CameraWebServer example and see the stream in a browser
- [ ] **Latency bake-off script** (`tools/bench_llm.py`): same prompt + 3 frames, 10 runs each, recording time to first token for
      `claude-opus-5-5` at low effort, Opus 5.5 with fast mode, and `claude-haiku-5-5`. We pick the model from these numbers.
- [ ] Try 2 STT and 2 TTS options for end-of-speech → transcript and text → first audio

Done when: every risky dependency has been run once on the real laptop.

### Phase 1: Fri night: "everything talks"

| Task | Owner | Done when |
|---|---|---|
| Camera streams over the hotspot, `FrameGrabber` shows it in an OpenCV window | A | ≥15 fps, reconnects after a camera power cycle |
| S3 firmware flashed. `nc -u` sends `C 90 60 1 1` and the servo and laser respond | A | Watchdog: laser off 0.5 s after packets stop |
| `ControlLink` sweeps the servos from Python and reads status | A | Status shows RSSI and seq |
| Mic → STT → printed transcript, streaming partials | B | Partials appear while talking |
| `Agent.turn` with a typed question + webcam frames → streamed sentences → `say` | B | Prints TTFT |
| Hardware mounted on a cardboard mock-up | both | Worn for 5 minutes without anything falling off |

### Phase 2: Sat morning: the two halves work independently

| Task | Owner | Done when |
|---|---|---|
| YOLO-World on the stream with the recipe classes, boxes drawn | A | Finds avocado, lime, bowl on our counter ≥80% of frames |
| Red-dot detector + laser blink check | A | Dot found in ≥90% of frames, no false dot on a tomato |
| Calibration sweep (5×5) → `calibration.json` → feed-forward aim | A | First aim within ~5 cm |
| Closed-loop correction | A | Within 10 px in under 1 s, holds while walking slowly |
| Full voice loop: speech → Claude → streamed TTS, barge-in | B | Measured end-of-speech → first audio ≤1.6 s |
| Point-first keyword matcher → `set_target` | B | Laser starts moving <300 ms after saying "lime" |
| Recipe step machine + `next_step` / `show_text` tools | B | Can talk through all 7 steps |

### Phase 3: Sat afternoon/evening: integrate

- [ ] `main.py` runs all loops together. Profile it so nothing blocks the event loop (YOLO in a thread).
- [ ] Agent `point_at` drives the real laser
- [ ] Watcher loop: event log + "is this step done?" on "done"
- [ ] Latency telemetry wired through to display line 3
- [ ] **First full guacamole run**, recorded on a phone. List what broke.
- [ ] Fix the top 3 problems. Cut anything that isn't working by 10 pm.

### Phase 4: Sun morning: demo polish

- [ ] Dashboard: annotated feed, step, transcript, event log, latency bars
- [ ] Prompt tuning: shorter replies, it points more, it only speaks up unprompted through check-ins
- [ ] Pre-warm cache at startup, canned fallback lines if the API is slow or down
- [ ] 5 full rehearsals with the 2-minute pitch script
- [ ] **Record a backup demo video** (in case the venue WiFi dies)

## Latency instrumentation (build it in from day 1)

Each stage stamps `time.monotonic()` into a per-turn dict, and the dashboard shows the breakdown:

```
speech_end → transcript_final → request_sent → first_token → first_sentence → tts_first_audio
name_heard (partial) → set_target → dot_on_target
```

Without these numbers we can't tell what to optimise, and judges love seeing them.

## Decisions still open

| Decision | Options | Decide by | How |
|---|---|---|---|
| Main voice model | Opus 5.5 low effort / Opus 5.5 fast mode / Haiku 5.5 / split fast-decide + talk | Thu | bake-off numbers |
| STT | Deepgram streaming / local whisper on M2 | Thu | measured endpoint latency |
| TTS | ElevenLabs Flash / Deepgram Aura / Cartesia / macOS `say` | Thu | measured first-audio latency, voice quality |
| Audio hardware | BT mic + BT speaker / lav 2.4 GHz + wired earbuds | Fri | measured round trip |
| "Fast decision model" (the one the user mentioned) | TBD, need the name or link | Thu | |
| Display | ~~0.96" / 1.3" OLED~~ → 1.54" ST7789 TFT (decided) | done | Check it has a header-pin PCB |

## Risks and fallbacks

| Risk | Fallback |
|---|---|
| Venue WiFi or captive portal | Phone hotspot (planned anyway) |
| YOLO misses an object | Lower the confidence, add synonyms, or fixed counter positions per ingredient |
| Laser dot not detectable in bright light | Dim the demo area, a whiter counter mat, blink detection |
| API slow or down | Canned per-step lines + keyword pointing still work (no offline mode: the camera needs the hotspot anyway) |
| Board brownouts | NiMH pack for the servos (schematic) |
| Calibration drifts when the box shifts | Closed loop corrects it, re-run the sweep before the demo |

## Demo script (draft)

1. "I've never made guac." The coach greets the user and lists the ingredients while the **laser touches each one**.
2. Halve the avocado. Ask "which knife?" and the laser points at it.
3. Say "done". It checks the frames, pushes back once ("the pit's still in"), then moves on.
4. Interrupt it mid-sentence (barge-in) to show it's conversational.
5. Show the dashboard latency numbers to close.
