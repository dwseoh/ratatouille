# Cut list

What we cut or pushed back from v1, why, and what it would take to bring it back. Only pick something
up from here **after a full guacamole run works**. Newest at the top of each section.

## Decided

| What | Status | Why | To bring it back |
|---|---|---|---|
| **Model bake-off** | Cut. Use **Claude Haiku 5.5** for everything | Fast and cheap enough for short spoken answers. `CLAUDE_MODEL` in `.env` can switch to Sonnet/Opus if answers are too weak. | A 30 min timing script (old B0). |
| **Gemini / MLH "Best Use of Gemini API" track** | Cut | We benchmarked it (B0): the fastest Gemini model misread the image and the bigger one hit capacity errors. Latency is the goal, so we use Claude Opus 5.5 fast mode and don't chase a second track. | Add `google-genai`, port the benchmark's Gemini runner, and use it for one non-critical piece (e.g. the watcher) with a Claude fallback. ~2 h. |
| **Live recipe swap, runtime image generation, LLM-chosen video search** | Cut | Swapping needs the agent prompt, YOLO classes and intent router rebuilt while running; generated images add seconds and can look wrong; the model can invent video URLs. The importer (docs/roles/C-plan.md phase 7) runs ahead of time instead. | Rebuild + prewarm the agent, re-set YOLO classes, reload router keywords. ~half a day. |
| **Knife-near-hand warning** (and any other safety warnings) | Cut (code removed from `watcher.py` too) | It would fire constantly while cutting (your hand is always near the knife). Knife safety goes in the step `tips` + check-ins instead. | MediaPipe Hands + knife box + "in the blade's path" rule. ~half a day + tuning. |
| **Background watcher** (vision checks steps every few seconds, push-back like "the pit's still in", event log) | Stretch. Replaced by **check-ins** (task B3a) | It's the biggest source of complexity: a second model, a loop and timing rules. Check-ins give the "talks on its own" feel by reusing the agent. | Task B3 in task-specs.md. Needs the event log (C10) + `Steps.propose_done("vision")`. ~half a day. |
| **Priority speech queue** (safety interrupts, drop stale comments, queue order) | Later | Check-ins only fire when it's quiet, so nothing competes yet. | Callers already use `say(text, priority)`. Add the rules inside `say()`. ~1 h, no one else's code changes. |
| **Offline mode** (demo with WiFi off) | Cut | The camera needs the hotspot anyway. We keep "API slow → speak the step's `fallback` line" + a backup video. | Local STT/TTS + canned lines. Not worth it. |
| **Timers** (`set_timer` tool) | **Built** (`coach/timers.py`, see docs/agent-notes.md) | Was cut because guacamole has nothing to time; imported recipes and "give me 5 minutes" do. | n/a |
| **QNX RTOS** | Not doing (unless there's a QNX prize) | Doesn't help latency (our delays are network/cloud). Needs a Raspberry Pi, a box redesign and a new toolchain. | Pi + QNX replaces the S3 as the controller with the same UDP protocol. ~1 day. |
| **Video model like V-JEPA ("jev")** | Not doing | Too heavy for 48 h. YOLO + Claude cover what we need. | n/a |

## Proposed (not agreed yet)

| What | Proposal | Why | To bring it back |
|---|---|---|---|
| **Laser dot finder + closed-loop aiming** | Calibrate by **clicking the dot on the dashboard image**, aim from the calibration only (open loop). Dot finder = stretch. | Biggest piece of the hardware critical path. Calibrating once is enough because the camera and laser are fixed in the same box. | P4 dot finder + P6 closed loop in task-specs.md. ~1 day. |
| **Blink check** (laser off for a frame to tell the dot from red food) | Cut | Only needed for the dot finder. | Part of P4. ~1-2 h. |
| **"Hey chef" wake phrase** | Stretch. BOOT button starts/resets for v1. | The button is simpler and can't false-trigger. | Fuzzy match in interim transcripts (V5). ~1-2 h. |
| **Detailed latency telemetry** | 4 timestamps: speech end, first token, first audio, laser moved | Enough for the dashboard + pitch. | Full C9 record. ~1 h. |

## Future ideas: accessibility (after v1, see docs/roles/C.md section 4b for what's in v1)

| Idea | Who it helps | Owner | How |
|---|---|---|---|
| **Button as a universal fallback**: short press = repeat the step, long press = reset | Speech that STT struggles with (dysarthria, aphasia, strong accents) | A (firmware: short/long press messages) + B (session) | S3 sends `B1` / `B2` instead of `B`. Small protocol change. |
| **Specific praise after each step** ("nice, those halves look perfect") | IDD, TBI, anyone building confidence | C | Prompt rule after `propose_done` → advance. ~15 min. |
| **Safety story for the pitch**: laser clamped below horizontal, 30 s auto-off, knife tips in steps, not a replacement for supervision | Everyone, especially dementia/IDD | All | Mostly already built; write it into the pitch. |
| **Laser blinks or circles on the target** so a still dot is easy to find | Low vision, visual neglect, attention difficulties | A | Blink: toggle `link.laser` at ~3 Hz while `on_target`. Circle: add a ~2° circular offset to pan/tilt over ~0.5 s. Make sure the S3's 0.5 s watchdog and the dot finder (if used) cope with blinking. |
| **Laser guides cutting/placement** (traces where to cut, marks where to put the knife) | Shaky hands, dyspraxia, spatial difficulties | A + C | Needs closed-loop aiming (dot finder) and object-relative positions ("a line through the middle of the avocado"). Agent tool like `trace(name, "halve")`. Hardest one; demo-able only with good calibration. Keep the beam off faces (it's already clamped below horizontal). |
