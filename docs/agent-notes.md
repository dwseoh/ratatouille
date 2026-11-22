# Agent notes: why Remy felt dumb, and what changed

Scope: `laptop/coach/agent.py` (the Claude voice agent), `context.py` (what it knows each turn), `timers.py`,
pause / resume in `session.py`, `intents.py`, `voice.py`, `main.py`. Model unchanged: `claude-opus-5-5`, effort
`low`, fast mode (a valid, current setup); backup `claude-haiku-5-5`.

## What was making it dumb

| # | Problem | Effect the cook noticed |
|---|---|---|
| 1 | **No way back.** The only step tool was `propose_done` (forward). `Steps` had no jump. Worse: "previous step" looks like "next step" to the embedding router, which could *advance*. | "Go back to the onion step" got words but no action, or skipped ahead. |
| 2 | **Forgetful.** History = current step + 6 turns, and nothing else survived. | "I don't have cilantro" or "make it for 6" was forgotten two steps later. |
| 3 | **Recipe as raw JSON, no amounts.** A 0-based list the model had to count to name a step, plus file paths and canned lines. No servings, quantities or substitutes anywhere. | Wrong step numbers; "how much salt / for 6 people / no lime?" was a guess. |
| 4 | **Thin state note.** `State.note()` = step, visible names, laser target, last 5 event texts with no ages. Positions were left/right/near/far only. | No "next to the bowl", no "is the laser on it", no next step, no "how long have I been on this", no stale-vs-fresh events. |
| 5 | **System prompt gaps.** It never said it was Remy (the fallback greeting did), never described the laser/display, never said tool results arrive with the *next* message, nothing on amounts, swaps or what to do if the cook gets hurt. | Inconsistent self-description; occasional "let me check" with nothing to check. |
| 6 | **Only the system prompt was cached.** Images are stripped from old turns (good), but with no breakpoint in `messages` every turn re-read the whole conversation uncached. | TTFT grew with the conversation. |
| 7 | **Few tools, out of sync.** 5 tools; `TOOL_NAMES` listed 4. No timers, no pause, no way to show several things. | "Set a timer", "hang on, someone's at the door" did nothing useful. |
| 8 | **Timer and pause phrases were STOP.** "Stop the timer" / "pause" were 1-3 word stop phrases: audio cut, agent skipped. | The request vanished silently. |
| 9 | **A step going backwards = restart.** The agent wiped its history whenever the step number went down. | Would have broken any "go back" support. |
| 10 | Small: "Enjoy your guacamole." hard-coded for every recipe. | Wrong dish after a recipe import. |

A likely cause of the known "Opus answers with tool calls only ~1 in 4" problem (the `_backstop` exists for it):
on Opus 5.5, text written before a tool call that runs longer than a sentence or two can come back as a
progress-update `thinking` block, which is empty under the default `display: "omitted"`. Replies here are 1-2
short sentences, so it is rare, and the backstop covers it. If it still happens, measure
`thinking: {"type": "adaptive", "display": "updates"}` (beta `thinking-display-updates-2026-08-18`); not enabled
blind because it can't be tested without a key.

## What changed

### Context per turn (`context.state_note`, last block of the newest user message)

```
[step 3/7 (mash): Mash the avocado, leaving it a bit chunky. | on this step 4 min 10 s | next: step 4 (dice onion)
 | visible: bowl (centre, far), fork (right, near, in the bowl), lime (left, far, left of the bowl)
 | needed now, not in view: knife | laser: fork (on target) | timers: onions 4:12 left
 | servings: 4 (the recipe is for 2; amounts now: 4 avocado, 1/2 onion, ...) | cook notes: no cilantro
 | recent: vision 40 s ago: avocado halved; user 12 s ago: said done -> advance
 | cook said earlier: "I don't have cilantro" (6 min ago) | cook may be stuck: 4 min on this step
 | you last said: "..." | last turn: point_at(lime) -> not_visible]
User: ...
```

- Positions are the cook's (chest camera): left/centre/right, near/far, plus in/on/next to the nearest landmark
  (bowl, board, pan ...), from the YOLO boxes. Landmarks only relate to each other by nesting ("bowl on the board").
- "needed now, not in view" = the step's `point` objects the detector doesn't see (only when frames came this turn).
- Time on step uses `State.cook_clock()`, which stands still while paused.
- "cook said earlier" = up to 3 utterances that were trimmed out of the history.
- "cook may be stuck" = 4+ min on the step (pauses excluded) or 3+ questions on it; never while paused.
- Paused: "session: PAUSED 2 min ago ..." with how to behave.

The recipe goes into the **cached system prompt** as numbered text (`context.render_recipe`): ingredients with
amounts, notes and substitutes, the objects and their other names, then "Step N (short): goal / How / Tips / Done
when". `guacamole.json` gained `servings` + `ingredients` (optional fields; older recipe files still load).

### Tools (13; `TOOL_NAMES` in contracts.py matches)

Existing: `point_at`, `laser_off`, `propose_done`, `show_text`, `show_image`. New:

| Tool | Does | Result (deferred to the next turn) |
|---|---|---|
| `point_sequence(names)` | laser tours several visible objects, 2 s each, stays on the last; stops if paused or someone points elsewhere | `ok: showing avocado, knife; not visible: salt` |
| `go_to_step(step)` | back or forward (1-based); laser off; reopens a finished recipe; history kept | `ok: now on step 2 (remove pits): ...` |
| `set_timer(seconds, label)` / `cancel_timer(label)` | `Timers`; "" cancels all; same label restarts | `ok: started onions timer, 5 minutes` |
| `remember(fact)` | session memory (max 8, deduped) shown in every note as "cook notes" | `ok: noted` |
| `set_servings(servings)` | scales the ingredient amounts; the note lists them | `ok: amounts for 4: 4 avocado, 1/2 onion, ...` |
| `pause_session()` / `resume_session()` | `Session.pause_now()` / `resume_now()`: no audio cut, so the model's own "sure, I'll wait" plays | `ok: paused; ...` |

Every schema: all properties required, `additionalProperties: false`, validated locally before running (arrays,
integers vs bools included), because `eager_input_streaming` means the API doesn't. `strict: true` is *not* sent:
it couldn't be verified here without a key, and a 400 would silence the coach on every turn. The schemas are
strict-compatible, so it's a one-line change after a real-API check.

Deliberately **not** added: `what_i_see` and `get_step_details`. Tool results arrive one turn later by design (no
second round trip, see architecture.md); a lookup tool would need one (~1 s more before the answer). What they
would return is already in the prompt: fresh detections + positions in the note, the frames themselves, the whole
recipe in the system prompt. Substitutions are answered from the recipe and recorded with `remember`.

### Pause / resume

| Trigger | Pause | Resume |
|---|---|---|
| Voice | "pause", "give me a sec/minute", "hold on a minute", "I need a break", "be right back" (router PAUSE, final transcript; the partial already cuts the audio). Said over the coach, it cuts it like a stop word and then pauses. | "I'm back", "resume", "continue", "let's keep going", "carry on", "where were we", "hey Remy" |
| Agent | `pause_session` (paraphrases: "I need to get the door") | `resume_session` (only reachable in a paused safety turn) |
| BOOT (`--button`) | press while active | press while paused (runs the welcome-back turn) |
| Typing mode | same phrases | same phrases, or `/button` (= a BOOT press) |

While paused: step kept, laser off, check-ins and the watcher silent (they only run while `active`), TFT line 0
"paused: say I'm back", dashboard pill "PAUSED m:ss", the intent router does nothing but RESUME / START_OVER.
Ordinary speech is ignored; "start over" and safety ("help", "I cut myself", "something's burning") still get
through (safety is answered while staying paused). Timers keep running and still announce: the cook asked for them.
Resume plays the start chime and runs one agent turn: "[session resumed after 3 minutes: welcome the cook back ...]
User: I'm back, where was I?". "Hold on a second" / "one sec" / "wait" stay STOP (a moment, not a break).

### Prompt caching

`tools` -> `system` (instructions + recipe, breakpoint 1, prewarmed) -> history (breakpoint 2 on the last block of
the previous reply) -> the new user message (pending tool results, frames, state note). Everything before
breakpoint 2 is byte-identical to the previous request (history is only ever trimmed from the front; images are
stripped before a turn is stored, which is why the marker sits on the reply and not on the image-bearing message),
so each turn reads the earlier conversation from the cache. The stored history never carries a marker (2 of the 4
allowed). Typing mode prints `cache read / write / uncached` after every turn to check it.

## Latency impact

- No new round trips: every tool still runs mid-stream and answers on the next turn.
- Uncached per-turn input grows from ~55 to ~110 tokens (the richer note): negligible prefill.
- The cached prefix grew from ~2.5K to ~4.1K tokens (system ~2.9K incl. recipe, tools ~1.2K; rough chars/4). Prewarmed at
  startup and read from cache on every turn; only a cold request (>5 min idle) pays the bigger prefill once.
- History is now read from the cache: TTFT no longer grows with the conversation.
- Building the note is pure Python (<1 ms). Pause / resume / timers are local (the pause line is a canned TTS line;
  resume costs one normal turn).

## Knobs to measure next (need a key)

- `cache_read_input_tokens` on turn 2+ should cover everything but the newest turn (typing mode prints it).
- A system-prompt line "Answer directly without deliberating." can cut Opus 5.5's thinking (TTFT) at low effort;
  check that answers stay good before keeping it.
- `display: "updates"` for the rare tools-only reply (above). `strict: true` on the tools.

## Try it

```bash
cd laptop
# typing mode, real agent (needs ANTHROPIC_API_KEY in laptop/.env); no mic, S3, YOLO or camera
python -m coach.main --type --no-yolo --no-s3 --cam black
#   where's the lime / show me what I need for this step / how much onion for 6 people?
#   I don't have cilantro / set a timer for 1 minute for the onions / go back a step / skip to the tomato
#   pause  (ignored: how chunky?)  I'm back, where was I?   /button (BOOT press)   help I cut myself (while paused)
python -m coach.main --type --no-yolo --no-s3 --cam black --fake-agent     # no key: echo agent, pause/timers/intents
# voice (needs DEEPGRAM_API_KEY too)
python -m coach.main --no-yolo --no-s3 --cam 0               # "hey Remy" ... "give me a sec" ... "I'm back"
python -m coach.main --button                                # full hardware; BOOT = start / pause / resume
```
