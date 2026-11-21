# C's QA script: how to test everything by hand

Do these in order. Each test says what to run, what you should see, and what a failure usually means.
Everything here runs from the repo root unless it says `cd laptop`. "Typed" tests need no mic, speaker or hardware.

**Cost:** a typed session costs a few cents (Opus 5.5 fast mode is ~$0.04 per turn). The full live script is ~$1.

## Test status (updated 2026-11-21)

Tested means Chloe ran it and it worked; the exact numbers were not written down, so re-run if you need them for the pitch.

| Area | Status | Notes |
|---|---|---|
| Unit tests | **Tested** | 346 passed on 2026-11-21 (offline, after pulling Hanjing's changes) |
| Live agent checks (`tools/live_agent_check.py`) | **Tested** | real key, about $1 |
| Live watcher checks (`tools/live_watcher_check.py`) | **Tested** | real key |
| Typed run through all 7 steps (`python -m coach.main --type`) | **Tested** | |
| Voice mode with the microphone | **Tested** | check-ins and watcher behaviour inside voice mode were not recorded separately |
| Phone dashboard (section 4) | **NOT tested** | needs phone on the same WiFi; also check the display options on a phone |
| Fallback drills (section 6) | **NOT tested** | slow API, backup model, bad key, no key, camera down, refusal |
| Anything on real hardware | **NOT tested** | real ESP32 camera stream, S3 (servos, laser, TFT), calibration, laser landing on a real object, latency on the real device |

## 0. Setup (once)

```bash
cd laptop && ../.venv/bin/pip install -r requirements.txt      # pipecat, rapidfuzz, sentence-transformers...
brew install portaudio                                         # needed for the microphone
```

- `laptop/.env` needs `ANTHROPIC_API_KEY` (and `DEEPGRAM_API_KEY` for voice). Copy `laptop/.env.example` for the rest.
- **Renamed setting:** the effort level is `COACH_EFFORT` (it used to be `CLAUDE_EFFORT`, which Claude Code sets in its
  own shell and which silently beat `.env`). If your `.env` still has `CLAUDE_EFFORT=...`, rename it; the old name is
  ignored (the default is `low` anyway).
- The first run downloads a small language model for the intent router (~80 MB). Run the unit tests once with WiFi
  on so it is cached.

## 1. The automated tests (5 min)

| What | Command | You should see |
|---|---|---|
| Unit tests | `cd laptop && ../.venv/bin/python -m pytest -q` | `346 passed` on 2026-11-21 (a few more if tests were added) |
| Live agent checks | `.venv/bin/python tools/live_agent_check.py` | `28/28 checks passed` (costs ~$1) |
| Live watcher checks | `.venv/bin/python tools/live_watcher_check.py` | 4 × PASS |

If the live script shows one FAIL: run that scenario again alone, e.g. `.venv/bin/python tools/live_agent_check.py plain_language`.
A single failure on a model-behaviour check (a sentence a bit too long) is usually the model, not a bug. A FAIL on
"cache" or "history" checks is a real bug: tell me.

## 2. The typed coach, no app (5 min)

```bash
cd laptop && ../.venv/bin/python -m coach.agent_cli --visible avocado knife bowl lime
```

Type these one at a time (empty line quits). The camera is black in this harness, so it may say "my camera looks dark": that is expected.

| Type | You should see |
|---|---|
| `where do I start?` | 1-2 short sentences about step 1, says where the avocado/knife are ("on your left"), `[laser] -> avocado` |
| `where's the lime?` | `[laser] -> lime` and a sentence saying where it is |
| `where's the salt?` | says it can't see the salt, **no** laser move |
| `say it simpler` | confirms in one sentence; from now on every reply is short, one action per sentence, no numbers |
| `back to normal` | confirms; normal wording again |
| `my camera is covered, just ignore the camera` | one short acknowledgement, then it **never mentions the camera again** (check 3-4 more replies) |
| `ok you can use the camera again` | it uses the camera again |
| `I'm done` (when the avocado is clearly not cut) | it **moves on**, no argument: your word wins |

Under every reply you get `ttft` (time to first word) and the cache line. **Pass:** `ttft` about 0.5-1.5 s, and from
the second turn `cache: read` is a big number (about 3000), not 0.

## 3. The whole app, typed (10 min). This is the main test

```bash
cd laptop && ../.venv/bin/python -m coach.main --type --no-s3 --no-yolo --cam black
```

Wait about 3 seconds. A boxed **TYPING MODE** banner appears, followed by a `you>` prompt. **That `you>` line is where you
type**: click the terminal, type, press Enter. (Log lines or `[TFT ...]` lines may print after it and push it up
the screen: ignore them, the app is still waiting for you, just keep typing.) It must be run in a normal
interactive terminal (Terminal.app, iTerm, or the app's Terminal panel), not piped or run in the background.

Open **http://localhost:8000** in a browser (the dashboard). Then type:

| Type | You should see |
|---|---|
| `where's the lime` | laser line in the terminal; dashboard "Laser: lime"; the coach's sentence shows big in "Remy says" |
| `ok I'm done` | `[intent] done -> advance`; dashboard step changes to 2/7; terminal `[TFT 2] Step 2: remove pits` |
| `where was I?` | "You're on step 2, ..." plus the next action |
| `talk less` | "Got it, I'll talk less"; check-ins become gentle (45 s) |
| wait 20 s without typing (default level) | the coach speaks up once on its own (a check-in) |
| (empty line) | exits cleanly, **no** red traceback |

**7-step run:** type `ok I'm done` six times (wait ~4 s between each: two "done"s within 3 s count as one) and check the
dashboard reaches 7/7 "cilantro, mix". The seventh "done" says the recipe is finished and stays on step 7.
Flags: `--no-checkins` (silence the coach), `--no-dashboard`, `--cam 0` (real webcam frames).

**Pass:** no crashes, step shown on the dashboard matches the terminal, the event log lists each step change.

By default **your "I'm done" always moves on**, whatever the camera shows (`PUSHBACK_DONE=0`). The only camera nudge is
the vision "ask" ("looks mashed, ready for the next step?"), once per step, and it needs the watcher, which only runs in
**voice mode**, so you can't trigger it by typing. Set `PUSHBACK_DONE=1` to let the camera push back once on a "done".
The automated test `test_full_seven_step_run_with_pushback_and_a_vision_ask` covers both, and section 8 tests the ask live.

## 4. The dashboard on its own (5 min)

```bash
cd laptop && ../.venv/bin/python -m tools.try_dashboard
```

Open http://localhost:8000. Fake data changes every few seconds. Check each panel:
live video with boxes (the target box is green, the red circle is the laser dot), step n/7, the big yellow caption
(last coach sentence), "You said", the speaking dot, event log (newest on top), 4 latency bars, camera / S3 /
battery pills, "live" in the header.

- **Display options (top right of the page):** **Light mode** switches dark/light; **Dyslexia font** shows *all* text in
  OpenDyslexic. Click each: the button gets an orange border when on, and your choice is remembered in that browser
  (reload to check). To send someone a ready-made view, add it to the address: `http://<address>:8000/?theme=light&font=dyslexic`
  (a link choice is for that visit only). The font loads from the internet (jsdelivr); with no internet it falls back to
  Comic Sans, another dyslexia-friendly font. For a fully offline demo, tell me and I'll bundle the font files.
- **Phone test:**
  1. Put the phone on the **same WiFi** as the laptop (not mobile data, not a guest network: those often block it).
  2. Start the dashboard (`python -m tools.try_dashboard`, or the app from section 3). The log prints
     `dashboard: http://localhost:8000 (phone on the same WiFi: http://192.168.x.x:8000)`. If you don't see it,
     run `ipconfig getifaddr en0` in another terminal for the laptop's address.
  3. On the phone, open that `http://<address>:8000` in the browser. macOS may ask "Allow Python to accept
     incoming network connections": click **Allow** (or the phone can't connect).
  4. You should see the same page, in **one column** (video on top, then step, captions, log). Captions must be easy to read.
     No video but the numbers update? The phone is connecting; wait a few seconds for the stream.
  If it doesn't load: check the phone and laptop are on the same network name, try Safari/Chrome, and that
  nothing else uses port 8000.
- **Disconnect test:** stop the script (Ctrl-C). The page header turns red "disconnected" within a second.

## 5. Check-ins (the coach talks on its own) (5 min)

In the section-3 app, with `CHECKINS=normal` (default):
1. Say nothing for 20 s: one short comment, or nothing (it may answer `SKIP` when there's nothing to say).
2. Then 20 s more: another. Never two in a row inside 20 s.
3. Type `ok I'm done`: after the reply, about a second of quiet, the coach introduces the new step and points at things.
4. `CHECKINS=off` in `.env` (or say `talk less` twice): total silence unless you speak.

**Fail signs:** talking while you type a question (check-ins must never interrupt), or repeating the same intro.

## 6. Fallback drills: nothing should ever leave the user in silence (10 min)

Each of these should print **one sensible canned line** (the step's instruction), not an error or silence.

| Drill | How | Expect |
|---|---|---|
| Opus too slow, backup works | `FIRST_TOKEN_TIMEOUT_S=0.01 ../.venv/bin/python -m coach.main --type --no-s3 --no-yolo --no-dashboard --cam black`, type `where do I start?` | a **normal answer** (from Haiku, about 1 s after the timeout), not a canned line. The log says `retrying once on claude-haiku-5-5` and the event log shows `agent: answered by claude-haiku-5-5` |
| Everything too slow | the same command with `BACKUP_MODEL=` (empty) in front | the step's canned line ("Cut the avocados in half lengthwise around the pit."). A second failure on the same step says "Sorry, give me a second." |
| Bad key | same command with `ANTHROPIC_API_KEY=invalid` in front | the step's canned line (a bad key fails on the backup too) |
| No key at all | comment out the `ANTHROPIC_API_KEY` line in `laptop/.env`, run the app, then put it back | a red "No Anthropic credentials" line at start, then canned lines |
| Camera down | `... -m coach.main --type --no-s3 --no-yolo --no-dashboard --cam http://127.0.0.1:9/x` | terminal says `camera DOWN`; the coach says once that its camera is offline and carries on from the recipe |
| Refusal | covered by `live_agent_check.py` scenario 13 | canned line |

On the dashboard each fallback shows as a red `system` event ("agent fallback: ...").

## 7. Accessibility features (5 min)

| Feature | Try | Pass |
|---|---|---|
| Plain language | `say it simpler`, then `how do I cut the avocado?` | sentences under ~12 words, one action each, no digits |
| Where am I | `wait, where was I?` | step number + name + next action |
| Spatial wording | `where's the lime?` (with `--cam 0` and a real lime in view) | "on your left / right / in the middle" matches reality |
| Never rush | read any reply | no "quickly", "hurry" |
| Talk less / more | `talk less`, `talk more` | acknowledges; check-in rate changes |
| Captions | look at the dashboard while it talks | last 3 sentences in large text, newest in yellow |

Start in plain mode: `PLAIN_LANGUAGE=1` in `.env`.

## 8. Voice (needs mic + speaker + Deepgram key) (15 min)

```bash
cd laptop && ../.venv/bin/python -m coach.main --no-s3 --no-yolo --cam 0
```

(`--no-s3` = no hardware; drop it and add `--ctl <ip>` with the S3 powered. Drop `--no-yolo` once the YOLO model is installed.)

| Say | Pass |
|---|---|
| "hey Remy" | a chime, then the greeting + step 1 |
| "where's the lime?" | laser moves **before** the coach finishes answering; the answer says where it is |
| "I'm done" | step advances, the coach introduces the next step |
| interrupt the coach mid-sentence | it stops talking immediately and answers what you just said |
| stay silent 20 s | one short comment, never over you |
| watch the **dashboard captions** while it talks | **one sentence at a time**; the next only appears when the voice has finished the previous one; if you interrupt, the unspoken ones vanish |
| cover the camera, say "ignore the camera" | it stops mentioning the camera; the dashboard pill says "camera ignored"; no "ready for the next step?" from the watcher |
| "start over" | chime, back to idle |
| halve an avocado in view of the camera | within ~5 s the event log shows "avocado halved"; later the coach asks "ready for the next step?" and you say "yes" and it advances |

Watch the dashboard latency bars: first word should be about 1-1.5 s after you stop talking.

**What I could not test (no mic/hardware here):** the real voice pipeline, the S3 board and laser, YOLO on a real
camera, the check-in/watcher timing over real speech, and the speaker. Test these first when you have the kit.
After the first real-voice run, tune the system prompt (`SYSTEM` in `laptop/coach/agent.py`) and re-run section 1.

## 9. Before every demo

1. Check `.env`: `COACH_EFFORT=low`, `CLAUDE_MODEL=claude-opus-5-5`, `CLAUDE_FAST=1`, `FIRST_TOKEN_TIMEOUT_S=5`, `BACKUP_MODEL=claude-haiku-5-5` (retries there once if Opus is slow; both models are warmed at startup).
2. Start the app 1-2 minutes early: it warms the cache (`prewarm ok: ... cache_read=...` in the log). The cache
   lasts 5 minutes, so re-warm by restarting if the coach sat idle longer.
3. Open the dashboard on the judges' screen; check camera ✓ and S3 ✓.
4. Do one typed turn first ("where do I start?") to check the API answers fast.
5. Have the backup video ready (offline mode was cut, so the video is the safety net).
