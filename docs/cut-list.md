# Cut list

What we cut or pushed back from v1, why, and what it would take to bring it back. Only pick something
up from here **after a full guacamole run works**. Newest at the top of each section.

## Decided

| What | Status | Why | To bring it back |
|---|---|---|---|
| **Model bake-off** | Cut. Use **Claude Haiku 5.5** for everything | Fast and cheap enough for short spoken answers. `CLAUDE_MODEL` in `.env` can switch to Sonnet/Opus if answers are too weak. | A 30 min timing script (old B0). |
| **Knife-near-hand warning** | Cut | It would fire constantly while cutting (your hand is always near the knife). Knife safety goes in the step `tips` + check-ins instead. | MediaPipe Hands + knife box + "in the blade's path" rule. ~half a day + tuning. |
| **Background watcher** (vision checks steps every few seconds, push-back like "the pit's still in", event log) | Stretch. Replaced by **check-ins** (task B3a) | It's the biggest source of complexity: a second model, a loop and timing rules. Check-ins give the "talks on its own" feel by reusing the agent. | Task B3 in task-specs.md. Needs the event log (C10) + `Steps.propose_done("vision")`. ~half a day. |
| **Priority speech queue** (safety interrupts, drop stale comments, queue order) | Later | Check-ins only fire when it's quiet, so nothing competes yet. | Callers already use `say(text, priority)`. Add the rules inside `say()`. ~1 h, no one else's code changes. |
| **Offline mode** (demo with WiFi off) | Cut | The camera needs the hotspot anyway. We keep "API slow → speak the step's `fallback` line" + a backup video. | Local STT/TTS + canned lines. Not worth it. |
| **Timers** (`set_timer` tool) | Cut | Guacamole has nothing to time. | Tool + countdown + spoken reminder through `say()`. ~1-2 h. |
| **QNX RTOS** | Not doing (unless there's a QNX prize) | Doesn't help latency (our delays are network/cloud). Needs a Raspberry Pi, a box redesign and a new toolchain. | Pi + QNX replaces the S3 as the controller with the same UDP protocol. ~1 day. |
| **Video model like V-JEPA ("jev")** | Not doing | Too heavy for 48 h. YOLO + Claude cover what we need. | n/a |

## Proposed (not agreed yet)

| What | Proposal | Why | To bring it back |
|---|---|---|---|
| **Laser dot finder + closed-loop aiming** | Calibrate by **clicking the dot on the dashboard image**, aim from the calibration only (open loop). Dot finder = stretch. | Biggest piece of the hardware critical path. Calibrating once is enough because the camera and laser are fixed in the same box. | P4 dot finder + P6 closed loop in task-specs.md. ~1 day. |
| **Blink check** (laser off for a frame to tell the dot from red food) | Cut | Only needed for the dot finder. | Part of P4. ~1-2 h. |
| **"Hey chef" wake phrase** | Stretch. BOOT button starts/resets for v1. | The button is simpler and can't false-trigger. | Fuzzy match in interim transcripts (V5). ~1-2 h. |
| **Detailed latency telemetry** | 4 timestamps: speech end, first token, first audio, laser moved | Enough for the dashboard + pitch. | Full C9 record. ~1 h. |
