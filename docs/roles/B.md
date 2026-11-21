# B: voice + YOLO (Hanjing)

The build plan for B. It's based on [task-specs.md](../task-specs.md) (V1-V5, P3, S1) and
[contracts.py](../../laptop/coach/contracts.py). If this file and contracts.py disagree, contracts.py wins.

## Goal

You build the coach's **ears, mouth and eyes**:

- **Ears:** hear the user, know when they start and stop talking, and turn speech into text fast.
- **Mouth:** speak the agent's sentences as soon as each one exists, and stop instantly when the user talks.
- **Eyes:** find the recipe objects in the camera image (YOLO), so the laser and the agent know what's where.
- **Reflexes:** a fast local intent router. "Where's the lime" moves the laser before Claude has even answered.

The headline requirement is latency. The target is about 1 s from end of speech to the first spoken word,
and the laser moving under 0.3 s after you name an object.

## Files you own

| File | What |
|---|---|
| `laptop/coach/voice.py` | Pipecat pipeline, our agent processor, `Speech` (SpeechAPI), echo gate |
| `laptop/coach/intents.py` (new) | `IntentRouter` (IntentRouterAPI): keywords + fuzzy + embeddings |
| `laptop/coach/session.py` (new) | `Session` (SessionAPI): BOOT button start/reset |
| `laptop/coach/vision.py` | `Detector` class only. `find_red_dot` in the same file is dw127's. Pull before editing. |
| `laptop/coach/telemetry.py` (new) | Turn ids, the 4 latency marks, `logs/latency.jsonl`, TFT line 3 |
| `tools/try_voice.py`, `tools/try_yolo.py` (new) | Small scripts to test your parts alone |

`requirements.txt` is append only. Post in the chat when you add Pipecat, rapidfuzz, sentence-transformers.

## Interfaces

**You provide** (exact shapes in contracts.py):

- `SpeechAPI`: `await say(text, priority)`, `stop()`, `speaking`. Chloe's agent and check-ins call it.
- `IntentRouterAPI`: `classify(text) -> IntentResult(intent, obj, score)`.
- `SessionAPI`: `start()`, `reset()`.
- `State` fields you are the only writer of: `dets`, `transcript`, `speaking`, `user_talking`, `session`,
  `turns`, `last_activity`.
- Latency marks: `speech_end`, `first_audio`, `point_called` (plus Chloe marks `first_token`).

**You call:**

- `AgentAPI` (Chloe): `await agent.turn(text, frames, on_sentence)`, `agent.cancel()`, `agent.prewarm()`.
  Until Chloe's is ready, use `FakeAgent` (echo bot).
- `AimerAPI` (dw127): `point_at(name) -> "ok" | "not_visible" | "unknown_object"`, `laser_off()`.
  Until ready, use `FakeAimer`.
- `StepsAPI` (Chloe): `propose_done("user")` for the DONE intent, `reset()`. Until ready, `FakeSteps`.
- `ControlLinkAPI` (dw127): `on_button(cb)` for the BOOT button, `show_text(TFT_LATENCY, ...)`. Until ready, `FakeLink`.
- `FrameSource` (dw127): `latest()` for YOLO, `recent(3)` for the agent's frames. Until ready, `FakeFrames(0)` (webcam).

## Pipecat: read this first

Pipecat class and module names change between versions. Every Pipecat name below is an **example**
(`e.g.`). Before writing code:

1. Pin one version in `requirements.txt` (`pipecat-ai[deepgram,silero,local]==X.Y.Z`; check the exact
   extras names for that version).
2. Open that version's examples folder on GitHub and find a **local audio** example (mic + speaker on the
   laptop, no WebRTC). Copy it as your starting point.
3. Check each class name against that version's source, not this doc.

What we need from Pipecat, whatever the names are:

- A local audio transport (mic in, speaker out). e.g. `LocalAudioTransport` with `audio_in_enabled`,
  `audio_out_enabled`, input/output device index, and a VAD analyzer.
- Silero VAD (e.g. `SileroVADAnalyzer`). It tells us when the user starts and stops talking.
- Smart turn (end-of-turn model, if your version has it). It decides "finished sentence" vs "just a pause".
- Deepgram STT (e.g. `DeepgramSTTService`), streaming, with interim results on.
- Deepgram TTS (e.g. `DeepgramTTSService`) with an Aura voice.
- Interruptions: when the user starts talking while the bot speaks, Pipecat sends an interruption frame
  downstream and cuts the audio. We also cancel the agent on it.
- Frames we listen for (names vary): user started/stopped speaking, interim transcription,
  final transcription, bot started/stopped speaking, interruption.

## Checkpoints (in order)

Each checkpoint should run and be pushed before the next. Small PRs.

### 1. Echo bot pipeline (Fri night, ~2 h)

Prove the full mic → STT → our processor → TTS → speaker loop, with `FakeAgent` in the middle.

- `pip install` the pinned Pipecat. Put `DEEPGRAM_API_KEY` in `laptop/.env`.
- Pipeline (e.g.): `transport.input() → stt → AgentProcessor(FakeAgent) → tts → transport.output()`.
- Laptop mic and speaker for now. Bluetooth comes in checkpoint 4.
- **Test:** `cd laptop && python -m tools.try_voice` (or `python tools/try_voice.py`), then say "hello".
- **Done when:** it answers "You said: hello" out loud, and talking over it stops it mid-sentence.

### 2. Our agent processor + barge-in (Fri night, ~1-2 h)

The processor is the seam between Pipecat and Chloe's plain-Python agent. See the sketch below.

- Final transcript → `agent.turn(text, frames.recent(3), on_sentence)` as a background task.
- Each sentence → a TTS text frame pushed downstream.
- User starts talking (or interruption frame) → `agent.cancel()` + cancel the task.
- If a new final transcript arrives while a turn is still running: cancel it, join the two texts, run one new turn.
- Set `State.transcript`, `State.user_talking`, `State.speaking`, `State.last_activity` from the frames.
- **Done when:** a long echo reply is cut off by talking, and no second reply comes out after the cut.

### 3. Endpointing + smart turn (Fri night / Sat AM, ~1 h)

- Start with ~0.8-1.0 s of silence before treating speech as finished. People pause mid-sentence while cooking.
- Turn on smart turn if your version has it. With it you can lower the silence wait to ~0.3 s when the
  sentence clearly sounds finished.
- Deepgram: interim results on, keyterm boosting with every name in `recipe.names()` (objects + aliases),
  so "cilantro" isn't heard as "silent row".
- **Test:** say "ok I've cut the onion... uh... what's next" with a 1 s pause in the middle.
- **Done when:** that comes out as ONE turn, and a normal short question still gets answered fast.

### 4. Bluetooth audio + echo gate (Sat AM, ~1 h)

- Pair the BT speaker and the separate mic in macOS settings. Never use a speaker's built-in mic: it
  switches macOS to "call mode" (HFP) and both directions sound terrible.
- List devices: `python -m sounddevice`. Pick input and output **by name substring** in code
  (`MIC_NAME`, `SPEAKER_NAME` in `.env`), then map to the index Pipecat wants. Never "default": macOS changes
  the default whenever something connects.
- Echo gate: while `speaking` (and 0.3 s after), ignore user speech, except a stop word
  ("stop", "wait", "hold on") which calls `Speech.stop()`. If your Pipecat version has a setting for
  "don't allow interruptions" or a minimum interruption length, try that first.
- **Done when:** a 2 min conversation at demo volume with zero self-interruptions, and "stop" still works.

### 5. Speech API for others (Sat AM, ~1 h)

- `Speech.say(text, priority)`: push a TTS text frame into the running pipeline from outside it
  (e.g. `task.queue_frames([TTSSpeakFrame(text)])`, check the name in your version).
- v1 has no priority queue. Accept the `priority` argument and ignore it. The rule "PROACTIVE only when quiet"
  is checked by the caller (Chloe's check-ins) using `State.user_talking` and `State.speaking`.
- `stop()`: an interruption (cut audio, clear queued text) plus `agent.cancel()`.
- **Done when:** a test script can call `await speech.say("check-in test", Priority.PROACTIVE)` while the
  pipeline runs and hear it.

### 6. Intent router (Sat AM, ~2-3 h), see design below

- Start from `KeywordRouter` in contracts.py. Add fuzzy matching, then the embedding fallback.
- Run it on **interim** transcripts. Act once per intent per utterance (debounce).
- Wire the actions: POINT → `aimer.point_at(obj)` + mark `point_called`; STOP → `speech.stop()`;
  START_OVER → `session.reset()`; DONE → `steps.propose_done("user")`; REPEAT and OTHER → nothing (the agent answers).
- It runs **in parallel** with the agent turn, never in front of it.
- **Done when:** "where's the green one" points at the lime in under 300 ms after the word, and
  `tests/test_intents.py` passes.

### 7. Session: BOOT button (Sat AM, ~1 h)

- idle → BOOT press → `start()`: `State.session = "active"`, then one agent turn with
  `"[session start: greet briefly and explain step 1]"`.
- BOOT press while active, or the START_OVER intent → `reset()`: `speech.stop()`, `agent.cancel()`,
  `aimer.laser_off()`, `steps.reset()`, `State.session = "idle"`.
- While idle, transcripts are ignored (no wake phrase in v1).
- Test with `FakeLink.press_button()`, then with `tools/fake_s3.py` (press Enter), then the real S3.
- **Done when:** press → greeting + step 1; press again → laser off and silence.

### 8. YOLO detector (Fri night fps test, tuning Sat AM), see YOLO section below

### 9. Telemetry (Sat PM, ~1 h)

- A new `TurnLatency` per user turn, appended to `State.turns`. Marks: `speech_end` (VAD stopped),
  `first_token` (Chloe), `first_audio` (first TTS audio out), `point_called` (router or agent tool).
- Write one JSON line per turn to `logs/latency.jsonl`. Show `first_audio` ms on TFT line 3.
- **Done when:** every turn logs all marks that happened, and Chloe's dashboard can draw them.

## Processor sketch (pseudo-code)

Names are examples. Match them to your pinned Pipecat version.

```python
class AgentProcessor(FrameProcessor):          # e.g. pipecat.processors.frame_processor
    def __init__(self, agent, frames, router, state, telemetry):
        super().__init__()
        self.agent, self.frames, self.router, self.state, self.tel = agent, frames, router, state, telemetry
        self.task = None          # the running agent turn
        self.pending = ""         # text of a turn that got cancelled by more speech

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStartedSpeakingFrame):          # VAD: user talking
            self.state.user_talking = True
            await self.cancel_turn()                             # barge-in
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self.state.user_talking = False
            self.tel.new_turn().mark("speech_end")
        elif isinstance(frame, InterimTranscriptionFrame):
            self.state.transcript = frame.text
            self.router_act(frame.text)                          # fast path: laser, stop, done
        elif isinstance(frame, TranscriptionFrame):              # final transcript
            if self.state.session != "active":
                return                                           # idle: ignore speech
            text = (self.pending + " " + frame.text).strip()
            self.pending = ""
            self.task = asyncio.create_task(self.run_turn(text))
            return                                               # don't pass the text on
        elif isinstance(frame, StartInterruptionFrame):          # Pipecat cut the audio
            await self.cancel_turn()
        await self.push_frame(frame, direction)

    async def run_turn(self, text):
        async def on_sentence(s):
            await self.push_frame(TTSSpeakFrame(s))              # or TextFrame, check your version
        try:
            await self.agent.turn(text, self.frames.recent(3), on_sentence)
        except asyncio.CancelledError:
            self.pending = text                                  # user kept talking: merge next time
            raise

    async def cancel_turn(self):
        if self.task and not self.task.done():
            self.agent.cancel()
            self.task.cancel()
```

Notes:

- `agent.turn` is Chloe's. You never touch Claude directly.
- Mark `first_audio` when the first audio frame for a turn goes out (e.g. on bot started speaking).
- Whether `pending` should be merged is a judgment call. If merging causes weird replies, drop it.

## Intent router design

Order: cheapest first. Stop at the first confident answer.

1. **Exact keywords + aliases** (0 ms). `recipe.names()` gives every spoken form → object
   ("green one" → lime). Longest phrase first.
2. **Fuzzy match** with `rapidfuzz` (~1 ms) for speech-to-text mistakes: "lyme", "avocadoes",
   "silantro". Accept a word if its similarity is ≥ 85.
3. **Embedding fallback** with sentence-transformers `all-MiniLM-L6-v2` (~80 MB, CPU is fine, ~5-20 ms).
   Embed ~10 example phrases per intent once at startup. For new text, take the cosine similarity to every
   example and pick the best intent. Accept it only if the score is ≥ 0.6 and beats the runner-up by ≥ 0.05.
   Otherwise return OTHER. Tune both numbers with the test file.

For POINT, the object still comes from step 1 or 2 (the embedding decides "this is a where-is question",
the word match decides which object). If no object is found, it's not POINT.

Example phrases (put them in `intents.py` and grow them as you test):

| Intent | Examples |
|---|---|
| POINT | where's the lime, which one is the cilantro, show me the knife, point at the bowl, where did I put the salt, I can't find the onion, which knife |
| DONE | ok I'm done, finished, what's next, next step, I did that, that's done, ok what now, all cut, I've mashed it |
| REPEAT | say that again, repeat that, what did you say, sorry what, come again, one more time |
| STOP | stop, wait, hold on, shh, quiet, pause, one sec |
| START_OVER | start over, restart, let's begin again, reset |
| OTHER | how chunky should it be, is this ripe, how much salt, why lime, does this look right |

Rules:

- Interim transcripts change as the user talks. Act only once per intent per utterance.
- POINT also needs the object to be in `State.dets`. If not, do nothing; the agent will say "I don't see it".
- Keep STOP strict (exact words only). A false STOP kills a good answer.
- `tests/test_intents.py`: ~40 phrases with the expected intent. Run it after every threshold change.

## YOLO details

- `Detector` in `vision.py` (draft exists): YOLO-World, `set_classes(recipe.objects)`, `device="mps"`
  (the M5's GPU).
- Run in a **worker thread**, always on `frames.latest()` (never a backlog), as fast as it goes.
- Hand results to the asyncio loop with `loop.call_soon_threadsafe(apply, dets)`. Never write
  `State.dets` from the thread itself.
- **Smoothing:** an object counts as visible if it was seen in 2 of the last 3 frames. Use the newest box.
- Per-class confidence thresholds in a dict (start at 0.15, raise the ones that false-trigger).
- Try `imgsz` 480 vs 640 and pick by fps vs hits. Write the fps down in the chat.
- Test on real avocados, limes, onions on a real counter, seen from chest height. Record a 1 min video so
  you can re-test offline without cooking again.
- **Test:** `python tools/try_yolo.py 0` (webcam) or `... path/to/video.mp4`, showing boxes and fps.
- **Done when:** ≥ 10 fps, and avocado / lime / bowl found in ≥ 80% of frames.
- dw127's aimer reads `State.det(name).center`, so keep box coordinates in the camera frame's pixels
  (don't resize them away).

## Time plan and handoffs

| When | You | Handoff |
|---|---|---|
| Fri night | K0 with the team. Checkpoints 1-3. YOLO fps test. | Post YOLO fps + "echo bot talks" in the chat |
| **Sat 10 am** | Plug Chloe's real `Agent` into the processor (replace FakeAgent) | **Voice ↔ agent talking end to end, with Chloe.** If Pipecat isn't talking by now, switch to **Vapi** (hosted voice agent) and tell the team. |
| Sat AM | Checkpoints 4-7, YOLO tuning | `State.dets` live for dw127's aimer and Chloe's state note |
| Sat PM | Telemetry, help dw127 test pointing from voice | **6 pm: spoken word → laser on a real avocado** |
| Sat eve | Integration in `main.py` with everyone | First full run recorded |
| Sun AM | Echo + latency tuning in the venue | |

## Pitfalls

- **Echo.** The always-on mic hears the coach. Without the echo gate it interrupts itself or answers itself.
  Point the speaker away from the mic, keep the mic clipped on the cook.
- **Bluetooth call mode.** A device that is both mic and speaker drops to call quality. Use separate devices.
- **Default device switching.** macOS changes the default input/output when anything connects. Pick by name.
- **Endpointing too eager.** Short silence waits cut people off mid-thought. Start at 0.8-1.0 s, lower only with smart turn.
- **Two answers.** If a new final transcript arrives while a turn runs, cancel the old one first.
- **Deepgram keys.** The key goes in `laptop/.env` only, never in git. Check usage in the Deepgram console
  if requests start failing. Both STT and TTS use the same key.
- **YOLO on the event loop.** Calling `detect()` on the asyncio loop freezes voice. Thread only.
- **Pipecat versions.** Examples from the internet may be for another version. Trust the pinned version's source.

## Stretch (only after a full run works)

- "Hey chef" wake phrase: fuzzy match in interim transcripts ("hey chef", "hey jeff", "a chef").
- Real priority rules inside `say()` (SAFETY interrupts, PROACTIVE dropped if older than 3 s).
- Earcon: a short "mm-hm" sound the moment the user stops talking, plus "thinking…" on the TFT.
- Full latency record (STT final, LLM sent, first sentence) and per-stage bars on the dashboard.
- Lower the silence wait further once smart turn is tuned.
