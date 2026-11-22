# Role C: brain + dashboard (Chloe)

You build the part that decides what the coach says and does. Voice (B, Hanjing) hands you the user's words and
gives you a way to speak. The aimer (A, dw127) gives you a way to point the laser. You own the Claude agent,
the recipe step tracker, the check-ins that make the coach talk on its own, the fallbacks, and the dashboard
the judges watch.

Read first: `laptop/coach/contracts.py` (the interfaces), `docs/task-specs.md` (B1, B2, B3a, B5, S2),
`docs/overview.md` (the big picture), `docs/cut-list.md` (what we're not building).

## What you own

| File | What |
|---|---|
| `laptop/coach/agent.py` | `Agent` (implements `AgentAPI`): Claude calls, streaming, tools, fallbacks |
| `laptop/coach/steps.py` | `Steps` (implements `StepsAPI`): the only code that changes `State.step` (new) |
| `laptop/coach/checkin.py` | Check-in loop: the coach speaks up on its own (new) |
| `laptop/coach/dashboard.py` | FastAPI dashboard: `/`, `/cook`, `/state`, `/stream`, and the built pages in `coach/static/` |
| `laptop/frontend/` | Source of the two web pages (React + TypeScript). `npm run build` there writes `laptop/coach/static/`, which is committed. See its README |
| `laptop/coach/agent_cli.py` | Typed-input test harness, no voice needed (`python -m coach.agent_cli`) |
| `laptop/recipes/guacamole.json` | The recipe (already in the new facts format) |
| `laptop/tests/test_steps.py` | Step machine tests (new) |

## Interfaces (exact names, from `contracts.py`)

**You provide:**
- `AgentAPI`: `async turn(user_text, frames, on_sentence) -> TurnStats`, `cancel()`, `async prewarm()`
- `StepsAPI`: `propose_done(source, evidence="") -> Verdict`, `confirm()`, `reset()`, `on_change(cb)`
- `State.step` (only `Steps` writes it). `Recipe.load()` is already written in contracts.py.

**You call:**
- `AimerAPI.point_at(name) -> "ok" | "not_visible" | "unknown_object"`, `laser_off()` (A). Use `FakeAimer` until Sat 6 pm.
- `SpeechAPI.say(text, priority)` (B). Use `FakeSpeech` until B's pipeline works.
- `ControlLinkAPI.show_text(line, text)` (A). Use `FakeLink`. Your lines: `TFT_STEP` (2), `TFT_LATENCY` (3), `TFT_AGENT` (4).
- `FrameSource.recent(n)` (A). Use `FakeFrames()` (black) or `FakeFrames(0)` (webcam).
- `State.note()`, `State.visible()`, `State.events`, `State.speaking`, `State.user_talking`, `State.last_activity`.

**Who calls you:** B's Pipecat pipeline calls `agent.turn()` when the user finishes talking, and `agent.cancel()`
when the user talks over the coach. B's intent router calls `steps.propose_done("user")` when it hears "done".
B's session code calls `steps.reset()` and `agent.cancel()` on a BOOT button reset.

## 1. The agent (`agent.py`)

The draft works but needs these changes. Do them in this order.

### 1.1 Model and request settings

- **Model update:** the default is now Opus 5.5 with fast mode (`CLAUDE_MODEL`, `CLAUDE_FAST` in `config.py`). The Haiku
  notes below still apply if you switch back to Haiku 5.5.
- Model from config: `cfg.model`, which is now `claude-haiku-5-5` (`.env.example` already says so; change the
  default in `config.py` too, and tell the team since `config.py` is shared).
- Effort `low`, and turn thinking **off** for speed: `thinking={"type": "disabled"}` plus
  `output_config={"effort": "low"}`. Haiku 5.5 thinks by default; "disabled" is allowed at effort `high` or
  below. Check the first-token time with and without it once; keep whichever is faster.
- Use the normal client, not the beta one: `self.client.messages.stream(...)`.
- **Remove `betas=["server-side-fallback-2026-07-01"]` and `fallbacks="default"`.** Haiku 5.5 has no
  server-side fallback, and a `fallbacks` value returns a 400 on it. Our fallback is the canned line (1.8).
- Don't send `temperature`, `top_p`, `top_k`, or an assistant prefill. Haiku 5.5 rejects all of them with a 400.
- `max_tokens` around 400. Replies are 1-2 sentences plus maybe a tool call. 4096 is fine too, but a small cap
  stops a runaway answer.

### 1.2 Tools

Exactly the 4 in `TOOL_NAMES`. Drop `next_step` and `set_timer` from the draft.

| Tool | Input | Handler |
|---|---|---|
| `point_at` | `{"name": one of recipe.objects}` (use an `enum` in the schema) | `aimer.point_at(name)` |
| `laser_off` | `{}` | `aimer.laser_off()` |
| `propose_done` | `{"evidence": str}` (what the user said or what the frames show) | `steps.propose_done("llm", evidence)` |
| `show_text` | `{"text": str}` (20 chars max) | `link.show_text(TFT_AGENT, text)` |

- Keep `"eager_input_streaming": True` on each tool so inputs arrive as they are generated. That means the
  server no longer checks the input against the schema, so **check it yourself** before running the tool:
  `name` must be in `recipe.objects`, `text` must be a string. If it isn't, return an error `tool_result`.
- **Run tools as soon as their block is finished, not at the end of the message.** The draft waits for
  `get_final_message()`. That delays the laser by the whole sentence. Instead, watch for the
  `content_block_stop` event of a `tool_use` block and run it then. (Collect each block's `input_json`
  fragments by `event.index`, or read the finished block from the event if your SDK version gives it to you.
  Print one event to check what you get.)
- If the stream raises `ValueError` (tool input JSON it couldn't parse at all), don't crash: skip the tool and
  continue. API errors are a different exception type; those go to the fallback (1.8).
- Constructor: `Agent(cfg, state, aimer, steps, link)`. Build the handler dict inside, so callers don't have to.

### 1.3 System prompt

The recipe is facts (`goal`, `how`, `tips`, `point`, `done_when`), not a script. The prompt tells the model to
coach in its own words. Starting point:

```
You are a friendly cooking coach. You talk to the user through a speaker while they cook with their hands.
You see what their chest camera sees: each message has 2-3 recent frames.
Your words are spoken aloud, so:
- Reply in 1-2 short sentences, under about 25 words. No lists, no markdown, no emoji.
- Explain one action at a time in your own words, using the step's goal, how and tips. Never read the recipe out.
- Follow the user's lead. Answer what they ask. Mention a safety tip when it matters (knife work).
Laser pointer:
- If you are about to mention an object that is in the "visible" list, call point_at FIRST, then speak.
- If the user asks about an object that is NOT in the visible list, say you don't see it and ask them to put it
  in view. Call laser_off.
- Call laser_off when you're done showing something.
Steps:
- The app tracks the current step (it's in the note at the top of each message).
- Call propose_done when the user says they finished, or the frames clearly show the step's done_when.
Check-ins:
- A message starting with "[check-in" is not from the user. Comment briefly only if something is worth saying
  (progress, a mistake, the next action). Otherwise reply with just `SKIP` and nothing else (the agent drops it).
RECIPE:
<the recipe JSON>
```

Tune it with `agent_cli` (section 6) and `tools/live_agent_check.py`. Keep this text stable during a run, because it's the cached part.

### 1.4 Every turn's message

`turn()` takes `(user_text, frames, on_sentence)`. It reads the state note itself (`self.state.note()`), so the
draft's `state_note` argument goes away. The user message content, in order:

1. Any deferred `tool_result` blocks from the last turn (they must come first, see 1.6).
2. 2-3 frames as JPEG, at most 512 px on the long side (`jpeg_block` in the draft). Check-ins: 384 px is enough.
3. Text: `[step 3/7 (mash): ... | visible: avocado, bowl, fork | laser: off | recent: ...]\nUser: <text>`.

The visible list is what lets the model say "I don't see the lime" **before** it calls a tool. That matters,
because tool results only reach the model on the next turn.

### 1.5 Sentence splitter

Feed `on_sentence` the moment a sentence exists, so TTS starts early.
- Split on `.`, `!`, `?` followed by a space. Merge pieces under ~4 words into the next one ("Okay." + the next sentence).
- If 120 characters go by without an ending, split at the last comma.
- Strip markdown characters (`*`, `#`, leading `-`) before speaking.
- Flush whatever is left at the end of the stream.
- Never speak `SKIP` (the check-in "nothing to say" answer): the agent drops it.
- Mark `LAT_FIRST_TOKEN` on the first text token, if a `TurnLatency` is passed in (agree with B how it's shared:
  simplest is `self.state.turns[-1].mark(...)`).

### 1.6 Tool results go with the next turn

The draft already does this: tools run locally, and their `tool_result` blocks are sent at the start of the next
user message, so there's never a second Claude round trip in the same turn. Rules that will bite you:
- Every `tool_use` in an assistant message needs a matching `tool_result` at the start of the next user message,
  or the API returns a 400.
- If a check-in turn produced nothing (`-`) and no tools, **don't add it to the history at all** (drop both the
  `[check-in]` message and the `-` reply). Otherwise the history fills with empty chatter.

### 1.7 cancel()

B calls `cancel()` when the user starts talking over the coach.
- Run the stream inside an `asyncio.Task` you keep a handle to. `cancel()` cancels that task. That closes the
  HTTP stream, so no more `on_sentence` calls happen.
- After a cancel, fix the history: keep the user message, and add an assistant message with only the text that
  was already spoken (if any). Drop any half-finished `tool_use` blocks. If nothing was spoken, drop the user
  message too.
- A new `turn()` must wait for a cancelled one to finish cleaning up. Use one `asyncio.Lock` around the turn body.
  Expose `self.busy` (True while a turn runs). The check-in loop reads it. (`busy` isn't in the contract yet;
  it's an additive change, so just tell the team.)

### 1.8 Fallback when Claude is slow or down

- If no first token arrives within **3 s**, give up on that request: cancel it and speak the current step's
  `fallback` line (`self.state.current_step.fallback`). `asyncio.timeout(3.0)` around the wait for the first
  event, then `.reschedule(None)` once text arrives, works on Python 3.11+.
- **Retry first:** on that timeout the agent retries ONCE on `BACKUP_MODEL` (Haiku 5.5, own `BACKUP_TIMEOUT_S`, default 3 s) with
  the same history; only if that is also silent does it speak the canned line. Both models' caches are warmed at startup.
- Same for API errors (connection error, rate limit, 5xx, 529 overloaded) and `stop_reason == "refusal"`.
- Only speak the fallback once per step. After that, say "Sorry, give me a second." instead of repeating it.
- Put the error in `TurnStats.error` and `State.log("system", ...)`, so the dashboard shows it.
- Set a client timeout (e.g. `anthropic.AsyncAnthropic(timeout=15)`), so a dead connection doesn't hang forever.

### 1.9 History trimming

Images are the main cost. Long histories make every request slower.
- Keep the last ~6 turns. When the step changes, you can start fresh with a one-line summary of the previous
  steps.
- Only the newest user message keeps its images. In older messages, replace image blocks with the text
  `"[frames omitted]"`.
- Editing old messages means that part of the request can't be served from the cache. That's fine: the big stable
  part (tools + system prompt + recipe) is cached separately. With thinking off there are no thinking blocks,
  so editing history is safe. (If you ever turn thinking on, editing history breaks the replayed thinking blocks.
  Start a fresh conversation instead.)

### 1.10 Prompt caching + prewarm

- The draft already puts `cache_control: {"type": "ephemeral"}` on the system block. Tools come before the system
  prompt in the request, so that one marker caches tools + system + recipe together.
- The minimum cacheable size on Haiku 5.5 is 512 tokens. Ours is well above that.
- `prewarm()`: at startup, send one request with `max_tokens=0`, the **same** model, tools, system, thinking and
  effort as real turns, a tiny user message, and `stream` **off** (`max_tokens=0` with streaming is rejected).
  It writes the cache, so the first real turn is fast.
- The cache lasts 5 minutes. During a demo, turns and check-ins keep it warm on their own. If the coach sits idle
  for longer (between rehearsals), call `prewarm()` again before starting.
- Check it works: print `usage.cache_read_input_tokens` on the second real turn. It should be large, not 0.

## 2. The step machine (`steps.py`)

The only code that changes `State.step`. Constructor: `Steps(state, link)`.

**v1 rules:**

| Call | Result |
|---|---|
| `propose_done("user" or "llm")` | Advance: `"advance"`. On the last step: `"finished"` (step stays at the last one). |
| `propose_done("vision")` | `"ask"`. Remembers it's waiting. The agent asks "looks mashed, ready for the lime?" |
| `confirm()` | If waiting after an `"ask"`: advance. Otherwise do nothing and return `"push_back"`. |
| `reset()` | Step 0, clear the waiting flag, call the `on_change` callbacks with 0. |

`"push_back"` isn't used by `propose_done` in v1 (no vision verification). It's there for the stretch watcher.

**Double-advance guard (important):** B's router calls `propose_done("user")` when it hears "I'm done", and the
LLM may also call `propose_done` for the same sentence. Ignore any `propose_done` within **4 s** of the last
step change: return `"advance"` without changing the step. Tell Hanjing: the router must run before
`agent.turn()` is called, so the state note already shows the new step.

**On every change:**
- `link.show_text(TFT_STEP, f"Step {n+1}: {short}")` (cut to 20 chars)
- `state.log("system", f"step {n+1}: {short}")`
- call every `on_change(cb)` callback. The check-in loop uses one to introduce the new step.

**Tests (`laptop/tests/test_steps.py`, pytest, no API calls):**
- walk all 7 steps with `propose_done("user")`, and the 7th call returns `"finished"`
- `propose_done("vision")` returns `"ask"` and doesn't move. `confirm()` then advances.
- `confirm()` without an ask doesn't move
- the 4 s guard: two calls in a row advance once (inject a fake clock: `Steps(state, link, clock=...)`)
- `reset()` goes to 0 and fires `on_change(0)`
- `on_change` gets the right step number, and `FakeLink` gets the TFT text

## 3. Check-ins (`checkin.py`)

This is what makes the coach talk on its own. It's one normal `agent.turn()` with fresh frames, no second model.

```python
async def checkin_loop(state, agent, speech, frames, steps):
    pending_intro = True                       # step 1 gets introduced when the session starts
    def on_step(n):
        nonlocal pending_intro; pending_intro = True
    steps.on_change(on_step)
    while True:
        await asyncio.sleep(1.0)
        if state.session != "active" or state.speaking or state.user_talking or agent.busy:
            continue
        quiet_for = time.monotonic() - state.last_activity
        if pending_intro and quiet_for > 1.0:
            text = f"[check-in: step {state.step + 1} just started. Introduce it in your own words and point at what they need.]"
            pending_intro = False
        elif quiet_for > 20.0:
            text = "[check-in: comment briefly only if something is worth saying, otherwise reply with just SKIP]"
        else:
            continue
        await agent.turn(text, frames.recent(3), lambda s: speech.say(s, Priority.PROACTIVE))
        state.last_activity = time.monotonic()  # ask B first: last_activity is theirs. Or keep your own timer.
```

- Conditions, all of them: session active, coach not speaking, user not talking, no turn running.
- If the user starts talking during a check-in, B's barge-in cancels it like any turn.
- `last_activity` is written by B (voice). Don't write it from here; keep a separate `last_checkin` time instead,
  and require 20 s since both.
- The step intro uses the recipe's `point` list through the prompt ("point at what they need").
- **Done when:** on a webcam run, it speaks up at least once per step unprompted and never talks over you.

## 4. Fallbacks (B5)

Mostly covered in 1.8. Also:
- No camera (`frames.connected` False): send the turn without images, and add "camera offline" to the note so
  the model says so once.
- If the API key is missing at startup, print a clear error and run with `FakeAgent`-style canned lines.
- Offline mode is cut (see cut-list.md). So is the knife-near-hand warning: knife safety lives in the step `tips`.

## 4b. Accessibility features (v1)

Who this is for: people for whom a written recipe is the barrier (dyslexia, language processing disorders,
dyscalculia, dyspraxia, autism, IDD, TBI/stroke, early dementia). Most of this is prompt + settings, so it's cheap.
Do these after check-ins work (Sat late AM / Sun AM). Settings go in `config.py` / `.env`.

1. **Plain-language mode ("say it simpler" / "slower").**
   - Setting `PLAIN_LANGUAGE=1`, or the user says "say it simpler" / "too fast" (the agent switches for the rest
     of the session; keep a `self.plain` flag and add one line to the turn text: `[mode: plain]`).
   - Prompt addendum for plain mode: sentences under ~12 words, one action per sentence, no idioms, no numbers.
     Use concrete amounts instead ("about half the lime", "a pinch", "until it looks like this") and point at it.
   - Slower speech is B's side (TTS speed or a short pause between sentences). Agree with Hanjing on a
     `SPEECH_RATE` setting; the agent just sends shorter sentences.
   - Test: in `agent_cli` (or `tools/live_agent_check.py`), "say it simpler" then "how do I cut the avocado" gives short, number-free sentences.
2. **"Where am I?" / "what was I doing?"**
   - Prompt rule: always answer with the current step number + `short` + the last thing they did, then the next action.
   - Keep a `last_action` string (the last sentence the coach said about the task) and put it in the turn text,
     because `State.note()` only has the step and events.
   - Test: mid-recipe, "wait, where was I?" gives "You're on step 3, mashing. Next, ...".
3. **Spatial language with the laser.**
   - Build `positions(dets, w, h)` in `agent.py`: each visible object gets left/centre/right (x thirds) and
     near/far (bottom half of the frame = near). Send it instead of the bare visible list:
     `visible: lime (left, near), bowl (centre, far)`.
   - Prompt rule: when you point at something, also say where it is ("the lime, on your left").
   - Helps low vision, aphasia, and anyone who can't spot the dot.
4. **Never rush.**
   - Steps only advance when the user says done or confirms an "ask" (already the v1 rule, keep it).
   - Check-in level setting `CHECKINS=off|gentle|normal` (gentle = 45 s quiet, normal = 20 s). The user can say
     "talk less" / "talk more" to change it. Some autistic users find constant comments overwhelming.
   - Never use time pressure in the prompt ("quickly", "hurry").
5. **Captions on the dashboard.**
   - The current step (name, goal, how-to list), the coach's current sentence next to Remy's image and the user's last
     words next to theirs, as live captions. Pulls `State.transcript` + the agent's sentences (keep the last 3).
   - Light/dark mode and a dyslexia font (OpenDyslexic) are in the page's menu.
   - The TFT already shows the step. Together these cover hearing loss and auditory processing.

Pitch: pick ONE persona for the demo (e.g. relearning to cook after a stroke, one good hand, long instructions
are hard; or someone with dyslexia who can't follow written recipes) and show 2-3 of these features on them.
Say "designed with X in mind", not medical claims. Future accessibility ideas are in `docs/cut-list.md`.

## 5. Dashboard (`dashboard.py`)

One FastAPI app in the same process, on the same asyncio loop:

```python
server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=cfg.dashboard_port, log_level="warning"))
asyncio.create_task(server.serve())
```

`host="0.0.0.0"` lets a judge's phone on the same hotspot open it too.

**Routes:**

| Route | What |
|---|---|
| `GET /` | The dashboard page (built from `laptop/frontend`, served from `coach/static/`). JS polls `/state` every 250 ms. |
| `GET /cook` | The phone page: one step at a time, big text (same build). `/info` gives its address, `/cook-qr.svg` a QR code for it. |
| `GET /state` | JSON (below) |
| `GET /stream` | MJPEG: `StreamingResponse` with `media_type="multipart/x-mixed-replace; boundary=frame"`, yielding `b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"` at ~10 fps. Each frame goes through `annotate(frame, state.dets, state.dot, state.target)` (draft exists). |

**`/state` JSON:**
```json
{
  "session": "active",
  "step": {"n": 3, "total": 7, "short": "mash", "goal": "..."},
  "transcript": "how chunky should it be",
  "speaking": true,
  "laser": {"target": "fork", "on_target": true},
  "visible": ["avocado", "bowl", "fork"],
  "events": [{"t": 1732160000.1, "source": "user", "text": "..."}],
  "latency": {"first_token": 640, "first_audio": 910, "point_called": 180},
  "links": {"camera": true, "s3": true, "vbat_mv": 5120}
}
```
`latency` is `state.turns[-1].ms()`. `events` is the last ~20.

**Panels:** live video with boxes + laser target, step n/7 + `short` + `goal` + how-to list, step picture, live
transcript and the coach's current sentence (both with talking animations), event log, latency bars for the last turn
(first token / first audio / laser), link status (camera, S3, battery), laser target and what the camera sees.
Which `/state` fields the page reads is listed in `laptop/frontend/README.md`.

**Pitfalls:** `cv2.imencode` blocks for a few ms. Run it in `asyncio.to_thread` so the event loop (voice!) never
stalls. Don't encode faster than 10 fps. Build the page with a fake `State` (`fake_dets()`, made-up turns) first;
it doesn't need anyone else's code.

**Calibration in the browser:** A's v1 calibration uses a cv2 window (`python -m coach.calibrate`), so the
dashboard doesn't need it. Stretch: a `/calibrate` page where clicking the video sends
`POST /calibrate/click {"x", "y"}` in **frame** pixels (convert from the displayed size using the image's
`naturalWidth`/`naturalHeight`), and A's calibrator registers a callback for it. Only if dw127 asks for it.

## 6. Typed-input harness (`agent_cli.py`)

Your main tool for the first day. No voice, no hardware, real API key.

```
cd laptop && python -m coach.agent_cli                    # black frames, every recipe object "visible"
python -m coach.agent_cli --webcam 0 --say                # real webcam frames, speak replies with macOS `say`
python -m coach.agent_cli --visible avocado knife bowl    # choose what the fake detector "sees"
```

It prints each spoken sentence, the tool calls, `ttft`, the cache line (`cache: read N / write N`) and the state
note. Things to try: "where do I start?", "where's the lime?" (visible), "where's the salt?" (not visible),
"how chunky should it be?", "say it simpler", "talk less". Watch: first-token time, whether it names where things
are, whether it ever reads the recipe out, and that the cache read is large on the second turn.
For the whole app without voice use `python -m coach.main --type` (adds the intents router, check-ins, the dashboard).
`python -m tools.try_dashboard` shows the dashboard with fake data. The scripted checks are `tools/live_agent_check.py`
(28 live checks) and `tools/live_watcher_check.py`. `docs/roles/C-qa.md` is the manual test script.

## 7. Plan and handoffs

| When | Do | Handoff |
|---|---|---|
| Fri night | K0 with the team. Agent changes 1.1-1.6, `agent_cli.py`, `steps.py` + tests. | Post first-token numbers for Haiku in the channel. |
| Sat 9-10 am | Finish `cancel()` (1.7) and the fallback (1.8). | **Sat 10 am: `Agent.turn()` works with typed input. Wire it into Hanjing's pipeline together.** |
| Sat late AM | Check-ins (`checkin.py`). Prompt tuning with real voice. | Agree with Hanjing: who calls `propose_done("user")`, `last_activity`, latency marks. |
| Sat PM | Dashboard v1: `/stream`, `/state`, `/` with a fake State. History trimming (1.9). | Swap `FakeAimer` for dw127's real Aimer when it's ready (~6 pm). |
| Sat eve | Integration with everyone (`main.py`). First full run. | |
| Sun AM | Dashboard polish (latency bars, event log, captions panel). Accessibility features 4b (1-5). Prompt tuning from the run. Prewarm before demos. | **Dashboard done Sun AM.** |
| Sun PM | Rehearsals + backup video (everyone). | |

## 8. Pitfalls

- Haiku 5.5 400s: `fallbacks`, `temperature`/`top_p`/`top_k`, assistant prefill, `budget_tokens`. Thinking
  `disabled` only works at effort `high` or below.
- `max_tokens=0` prewarm: no streaming, and the settings must match real turns, or it warms a cache nobody uses.
- A `tool_use` without a matching `tool_result` at the start of the next user message: 400. Watch this after
  cancels and dropped check-ins.
- Tool handlers run on the event loop. Keep them instant (they are: `point_at` just sets a target).
- Don't block the loop anywhere: no `time.sleep`, no sync HTTP, JPEG encoding in a thread.
- The speech-to-text will hand you typos ("lyme", "silantro"). Claude handles those fine; don't over-engineer.
- Two `propose_done` for one "done": the 4 s guard.
- Run everything from `laptop/` (`python -m coach....`). `cfg.recipe_path` is relative.
- Never commit `.env`.

## 9. Stretch (only after a full run works)

- Watcher (task B3): vision checks whether a step is done, with push-back ("the pit's still in").
- Priority rules inside `say()` (that's B's code, but you'd send SAFETY lines).
- Dashboard calibration page for dw127 (section 5).
- A "why" mode: when asked, explain the reason behind a step (ripe avocado, why lime stops browning).
- A latency chart over the whole run on the dashboard (good for the pitch).
- A second recipe, to show it's general.
