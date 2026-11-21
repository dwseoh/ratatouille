# Chloe (C): implementation plan

Written 2026-11-21 after the B5 commit (`1aa9db4`). Decisions: **knife/safety warnings are cut** (code removed from
`watcher.py`), **offline mode is cut**, **Opus 5.5 fast** is the default model (B0 result). Each task says what to
build, how to know it works, and what it needs from someone else. Do them in order unless a blocker says otherwise.

Legend: S ≈ under 1 h, M ≈ 1-3 h.

## Phase 0: housekeeping (S)

| # | Task | Done when |
|---|---|---|
| 0.1 | Commit the cut-warnings/offline doc + watcher changes, `git pull --rebase origin main`, push | `git log origin/main` shows it |
| 0.2 | `pip install -r laptop/requirements.txt` into the root `.venv` (adds pipecat) | `pytest` collects all 5 test files with no import errors |
| 0.3 | Post in team chat: B0 table + Opus 5.5 fast; model decision made on latency; `Agent.busy` added; default model changed in `config.py`; knife warning code removed | teammates replied or reacted |
| 0.4 | Ask Hanjing: who calls `propose_done("user")`, who sets latency marks, who owns `last_activity`, a `SPEECH_RATE` setting. Ask dw127: when the real Aimer is ready, do they want a `/calibrate` page | answers written into `docs/task-specs.md` handoffs |

## Phase 1: check-ins, `checkin.py` (B3a, M)

Build `laptop/coach/checkin.py` as in C.md section 3.
1. `checkin_loop(state, agent, speech, frames, steps)`; wake every 1 s.
2. Fire only if all hold: `state.session == "active"`, not `state.speaking`, not `state.user_talking`, not `agent.busy`.
3. Two triggers: step intro (set by `steps.on_change`, needs 1 s of quiet) and 20 s of silence. Keep your own `last_checkin`; require 20 s since both it and `state.last_activity`. Never write `last_activity`.
4. Send through `agent.turn(text, frames.recent(3), on_sentence)` with `Priority.PROACTIVE`.
5. Settle the skip word. Code uses `SKIP`, `C.md` says `-`. Pick `SKIP` and fix `C.md` section 1.3/1.5/6.
6. `tests/test_checkin.py` with fake clock/state: fires once per trigger, never when speaking/talking/busy/idle, resets on step change, SKIP says nothing.

Done when: a webcam run via `agent_cli --webcam 0` plus the loop speaks up at least once per step and never over you.
Needs: nothing (FakeSpeech/FakeFrames).

## Phase 2: dashboard, `dashboard.py` (S2, M)

Build against a fake `State` first (`fake_dets()`, made-up turns), so it needs nobody.
1. FastAPI app, uvicorn in the same loop (`asyncio.create_task(server.serve())`), `host="0.0.0.0"`, port from config.
2. `GET /state`: the JSON shape in C.md section 5 (`state.turns[-1].ms()`, last ~20 events, links).
3. `GET /stream`: MJPEG via `StreamingResponse`, `annotate()` per frame, `asyncio.to_thread(cv2.imencode)`, at most 10 fps.
4. `GET /`: one HTML page, polls `/state` every 250 ms. Panels: live video, step n/7 + short + goal, live transcript, speaking indicator, event log, latency bars (first token, first audio, laser), link status.
5. Captions panel (also the accessibility item): current step and last coach sentence in text of at least 32 px, high contrast. Keep the last 3 coach sentences (wrap `on_sentence`).
6. Tests: `/state` returns valid JSON from a fake State; `/` returns 200.

Done when: a judge can tell what's going on without anyone explaining it. Open it on a phone on the hotspot.
Needs: real `state.dets` (Hanjing) and `state.turns` (Hanjing) for real numbers; until then fake.

## Phase 3: wire `main.py` (S3, M, with everyone, Sat evening)

1. Replace the old `State` and handlers with `contracts.State`, `Steps`, `Session`, `Agent`, watcher, check-ins, dashboard.
2. Order from task-specs: voice ↔ agent, then tools ↔ real aimer, then watcher on real frames, then everything.
3. Decide with Jamie whether the watcher is in the demo or stays a stretch (it asks "ready for the next step?" but adds timing risk).
4. Missing `ANTHROPIC_API_KEY`: print a clear error and run on canned lines.

Done when: a full guacamole run, start to finish, recorded on video.
Needs: Hanjing's pipeline, dw127's Aimer (about Sat 6 pm).

## Phase 4: finish B5 (S)

Already done: 3 s first-token timeout, canned line once per step then "Sorry, give me a second.", refusals, errors, camera-offline note, missing-key credentials handling.
Left:
1. Camera offline: confirm voice and the typed harness still work end to end with `frames=[]` (live check 10 in `tools/live_agent_check.py`).
2. Fallback drills (below) all pass.

## Phase 5: accessibility, C.md section 4b (M, Sun AM, only after Phase 1 and a full run)

1. Plain-language mode: `PLAIN_LANGUAGE` setting + "say it simpler" / "too fast"; `[mode: plain]` line; prompt addendum.
2. "Where am I?": prompt rule + `last_action` string in the turn text.
3. Spatial wording: `positions(dets, w, h)` in `agent.py`; "visible: lime (left, near)".
4. Never rush: `CHECKINS=off|gentle|normal` (45 s / 20 s); "talk less" / "talk more"; no time-pressure words in the prompt.
5. Captions panel (done in Phase 2).
Tests: add to `test_agent.py`; try them in `agent_cli`.

## Phase 6: testing checklist

| Test | How | Pass |
|---|---|---|
| Unit suite | `cd laptop && ../.venv/bin/python -m pytest -q` | all green |
| Typed harness | `python -m coach.agent_cli`, real key: "where do I start?", "where's the lime?", "where's the salt?", "ok I'm done" | 1-2 short sentences that say where things are, the coach speaks first and then calls `point_at` (in voice mode the laser already moves before the coach answers, because the intent router points from your partial speech), recipe never read out |
| Cache | second turn prints `cache_read_input_tokens` | large, not 0 |
| Latency | note ttft per turn | about 1.5 s first sentence with fast mode |
| Fallback drills | `FIRST_TOKEN_TIMEOUT_S=0.01`; unset key; no frames; forced refusal | each speaks one sensible line, `TurnStats.error` set, dashboard event shown |
| Live checks | `tools/live_agent_check.py`, `tools/live_watcher_check.py` | all PASS |
| 7-step run | typed (`python -m coach.main --type`): six "ok I'm done" with ~4 s between them, step ends at 7 and the 7th says finished. The push-back and vision "ask" need the watcher, which only runs in voice mode: covered by `test_full_seven_step_run_with_pushback_and_a_vision_ask`, and live by the last row of C-qa.md section 8 | step ends at 7 |
| Check-ins | silence 20 s during a step | speaks once, never over the user |
| Watcher | run alongside a voice turn | never speaks over the user |

## Phase 7: improvements and demo prep (Sun)

- Tune the system prompt after the first real-voice run; keep it stable afterwards (it is the cached part).
- `prewarm()` again before each demo (cache lasts 5 min).
- 5 rehearsals with real food: after each, fix only the top 3 problems.
- Record a backup video (offline mode is cut, so the video is the safety net).
- Pick one persona for the pitch and show 2-3 accessibility features on them.

## Phase 8: stretch (only after a full run works)

- "Why" mode (explain the reason behind a step).
- Latency chart over the whole run on the dashboard.
- Second recipe.
- Specific praise after each step.

## Doc clean-up

- Done: `docs/roles/C.md` now uses `SKIP`, `agent_cli.py` and notes the Opus-fast default (sections 1.1/1.3/1.5/6).
