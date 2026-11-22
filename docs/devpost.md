# Devpost submission: Ratatouille (Remy)

Paste-ready. Items in **[FILL]** need a real number or detail from the team before submitting.

---

## Elevator pitch

Remy is a chest-worn cooking coach that hears you, sees your counter, and points a laser at what to grab next, so you can cook without ever reading a recipe.

(Joke option for the video/thumbnail only: "A device for idiot sandwiches.")

---

## About the project

### Inspiration

A recipe asks you to do five things at once: read, remember where you are, convert quantities, find the right thing on the counter, and use your hands. Your hands are messy, so scrolling a phone mid-chop is awkward. Most people drop at least one of those five, and for some people a written recipe is simply a wall.

We were thinking especially about people for whom that wall is real: dyslexia and other language-processing differences, dyscalculia, dyspraxia, autism, intellectual and developmental disabilities, people recovering from a stroke or brain injury (often with one usable hand), early memory loss, ADHD, low vision, and hearing loss. Cooking is also a classic rehab task, which is why occupational therapists use kitchens to rebuild independence.

The name comes from the movie. Remy is the rat under Linguini's hat who steers him through a kitchen. We wanted that: a coach that rides along, sees what you see, and **shows you instead of telling you**. The core moment is simple. Remy says "grab the lime", and a red dot lands on the lime.

We designed with these needs in mind from the first night. We have not tested with these communities yet, and we make no medical claims.

### What it does

You clip the box to your chest and say "Hey Remy, let's make guacamole."

- **Talks like a person.** You speak, it answers in one or two short sentences, and it looks at the camera picture before it replies. Ask "is this ripe enough?" or "how small do I chop the onion?" You can interrupt it mid-word and it stops and listens.
- **Points with a laser.** Object detection finds the ingredients and tools on the counter. Remy says "the lime, on your left" and a servo-aimed red laser lands on it. It turns off on "stop", when a step is done, and after 30 s idle.
- **Tracks the recipe.** It knows which of the 7 guacamole steps you're on and only moves on when you say "done". If the camera clearly disagrees (the pit is still in), it can push back once. It never advances by itself.
- **Watches and checks in.** A second, cheaper model glances at the camera every few seconds and keeps a running diary. If you've been quiet for ~20 s, or a new step just started, Remy may speak up with a nudge or a tip, or decide to say nothing. It never talks over you.
- **Accessibility built in.** "Say it simpler" gives short plain sentences, one action each, no numbers. "Where was I?" gives the current step, the last action, and what's next. Directions use spatial language plus the laser. "Talk less" turns the check-ins down or off. Remy never uses time-pressure words and never rushes you.
- **Dashboard for everyone else.** A web page (works on a phone) shows what the coach sees with boxes around detected objects, the step and its goal, a picture of what the step should look like, and Remy's words in big captions. It has high-contrast light and dark modes and an OpenDyslexic font option. It's also how judges can see what a chest camera sees.
- **Never goes silent.** If the AI is slow, down, or errors, it retries once on a faster model, then speaks the step's pre-written line. If the camera drops, it keeps coaching by voice.
- **Safe by design.** The laser is clamped below horizontal (never aimed at eye level), and the firmware turns it off if the laptop goes quiet for 0.5 s.

Recipes are data, not code: goal, how, tips, what to point at, what "done" looks like. A recipe importer turns a web page into Remy's format, and detection is open-vocabulary, so a new recipe needs no retraining.

### How we built it

Three people, three parts, one night of planning. We wrote shared interface "contracts" with fake versions of every part, so hardware, voice/vision, and the brain were built in parallel and plugged together at the end.

**Hardware (the box).**
- A 3D-printed enclosure designed in OpenSCAD (~89 × 89 × 38 mm) with a chest-side lid, strap loops, USB slots, and a TFT window.
- An ESP32 WROVER camera streams MJPEG over WiFi. An ESP32-S3 drives two SG90 servos (pan/tilt), switches a 650 nm laser (≤5 mW) through an NPN transistor, and runs a 1.54" TFT.
- Both boards share one perfboard carrier so the camera and laser stay fixed relative to each other. The laser sits right above the lens to cut parallax, so one calibration stays valid.
- The laptop talks to the S3 over UDP at 30 Hz. The firmware watchdog kills the laser if packets stop. Power is a USB power bank with decoupling capacitors at the servos. The schematic is generated in KiCad from a Python script.

**Seeing and pointing.**
- YOLO-World (open-vocabulary, no custom training) runs on the laptop GPU at roughly 50 fps at 640 px on an M2, with per-class thresholds and frame smoothing so boxes don't flicker.
- Calibration sweeps the laser over a 4×4 grid, finds the red dot in the camera image, and fits a 2nd-order polynomial from pixel to pan/tilt. The aimer smooths the target, limits speed, and holds briefly when an object is lost. Servo moves are ramped, max 2° per packet.

**Voice.**
- A Pipecat pipeline: mic → Silero VAD → Deepgram streaming speech-to-text → smart turn detection → agent → Deepgram Aura text-to-speech.
- A local intent router (keywords, then fuzzy matching, then MiniLM embeddings) handles "done", "repeat", "stop", "start over", and "point" in milliseconds without waiting for the LLM.
- Deepgram keyterm boosting uses the recipe's own words, so "cilantro" isn't heard as "silent row". An echo gate stops Remy from hearing itself through the speaker.

**The brain.**
- Claude Opus 5.5 (low effort, fast mode) is the coach. We chose it by benchmarking time to the first spoken sentence: about 1.2 s median, versus about 2 s for Haiku 5.5 and standard Opus. We also benchmarked Gemini.
- Each turn sends what you said, the latest camera frames, the current step, and recent events. Claude streams back short sentences, each spoken the moment it ends, and calls tools mid-stream: `point_at`, `laser_off`, `propose_done`, `show_text`, `show_image`.
- Claude Haiku 5.5 is the backup if the first token is slow, and it also runs the background watcher.

**Dashboard.** A FastAPI server on the laptop streams live MJPEG with boxes and a `/state` JSON. The front end is React + TypeScript (Vite), designed by hand in Figma with hand-drawn assets in Procreate.

**Engineering.** One Python asyncio app runs the frame grabber, YOLO thread, voice pipeline, aimer loop, check-in loop, and dashboard. We built a fake S3, a hardware check script, a typed-input mode, an LLM benchmark script, and per-turn latency telemetry. The repo has hundreds of unit tests ( **[FILL: final count]** passing). We built with help from Claude Code.

### Challenges we ran into

- **Our first hardware project.** We had little experience, and most "bugs" turned out to be power.
- **Power.** Loose breakout pins caused random power cuts until we soldered them. Servos starting up dipped the 5 V rail and reset the board, which we fixed with capacitors and by ramping servo moves.
- **Driving a laser from an ESP.** The pin can't power it directly. We learned PNP vs NPN the hard way after grabbing the wrong transistor.
- **3D printing.** The first enclosure didn't fit and printed holes came out smaller than designed. **[FILL: what we changed]**
- **WiFi on the S3** kept dropping off the hotspot because it retried every loop. We moved to a 5 s retry and logged the reason.
- **Making a laser hit a lime.** The camera and laser see from different spots, so we built calibration and smoothing so the dot lands on target without jitter.
- **Latency.** Feeling like a person means about one second to answer. We streamed everything (STT, Claude sentences, mid-stream tool calls, TTS on the first sentence) and picked the model by measurement.
- **When to talk.** Deciding when an AI should speak up, and when to stay quiet, was harder than deciding what to say.
- **Echo and mics.** With a speaker, Remy heard itself and interrupted itself, so we added an echo gate. Wired mics are short and Bluetooth adds latency.
- **Scope.** We cut a lot to finish: a knife-near-hand warning that fired constantly while chopping, timers, offline mode, and a video model.
- **Coordination.** Three people across hardware, voice, and the brain in 36 hours.

### Accomplishments that we're proud of

- **Accessibility from the start, not bolted on.** Plain-language mode, "where was I?", spatial directions, adjustable check-ins, encouraging feedback, high contrast in light and dark, a dyslexia-friendly font, and big captions, spread across a laser, camera, mic, speaker, and dashboard.
- **A working wearable from a first hardware project:** two microcontrollers, servos, a laser, a screen, and a custom enclosure, built with limited resources.
- **It points.** A real laser landing on a real ingredient from a spoken request.
- **It feels like a person:** about 1 to 1.5 s responses, interruptible, short answers.
- **It never goes silent,** and it's safe by design: laser below horizontal, watchdog and idle auto-off, never auto-advances.
- **A product we could use ourselves** to follow a recipe without reading it. **[FILL once true]**
- **Scalable:** recipes are data, imported from the web, and detection is open-vocabulary.
- **A designed interface:** hand-designed UI/UX in Figma and hand-drawn assets in Procreate.
- **Engineering quality:** shared contracts with fakes, hundreds of tests, and hardware check scripts.

### What we learned

- **Hardware is hard, and power is everything.** Test each part alone on the bench before combining, and measure real parts before printing.
- **Latency is a design problem,** not just an engineering one. Streaming, short answers, and cheap local decisions mattered more than raw model speed.
- **Agree on interfaces early.** Contracts and fakes let three people build in parallel.
- **Show, don't tell.** A dot on the right object replaces whole sentences, and that is also the accessibility win.
- **Designing for accessibility makes it better for everyone:** captions, plain language, and hands-free help everyone.
- **Knowing when an AI should stay quiet** matters as much as what it says.
- **Teamwork.** Each of us took ownership of our part without being asked and can explain the others.

### What's next for Ratatouille

- Test with occupational therapists and the communities we designed for.
- A PCB instead of perfboard, and a smaller all-in-one wearable with built-in mic, speaker, and longer battery.
- More recipes: import from any site, harder techniques, multi-dish timing.
- Laser-guided cutting (trace where to slice, for shaky hands or dyspraxia) and blinking or circling the target for low vision.
- A button as a universal fallback (short press repeats, long press resets) for people speech recognition struggles with.
- A caregiver view, multiple languages, per-user profiles, AAC/symbol-board integration, haptic "look here", timers, and the knife-near-hand warning.

---

## Built with

python, c++, arduino, esp32, esp32-s3, claude, anthropic, deepgram, pipecat, silero-vad, sentence-transformers, yolo-world, ultralytics, opencv, pytorch, fastapi, react, typescript, vite, figma, procreate, openscad, kicad, 3d-printing

(25 tags. Optional swaps if you'd rather: rapidfuzz, udp.)

## "Try it out" links

- https://github.com/dwseoh/ratatouille (**make it public before judging**)
- Demo video: **[FILL]**

## Additional info

**Prize:** Best Hardware Prototype by PCBWay. Also tick any Anthropic/Claude, accessibility, or Deepgram prizes the Devpost list offers.

**Schools:** University of Waterloo, Western University

**Which AI tools did you use?** Anthropic, Gemini

**Did you implement a generative AI model or API? How and why?**

Claude Opus 5.5 (fast mode) is Remy's brain. Each turn it gets the user's words, the latest camera frames, and the current recipe step, and it streams back short spoken sentences plus tool calls (move the laser, mark a step done, show a picture). We chose it on measured time to the first spoken sentence (about 1.2 s median) because a coach has to feel like a person. Claude Haiku 5.5 is the fallback when Opus is slow and also runs a background watcher that reads camera frames to track progress. Deepgram Aura generates Remy's voice, and YOLO-World finds objects by name without training. We also benchmarked Gemini for latency but it is not in the final build.

**Technology feedback (pick one per teammate):**
- *Deepgram:* keyterm boosting fixed ingredient names that were otherwise misheard ("cilantro" became "silent row"), and streaming kept latency low.
- *Pipecat:* barge-in and turn detection worked out of the box, which saved us hours.
- *Claude fast mode:* cut the time to the first spoken sentence from about 2 s to about 1.2 s, which is what made Remy feel conversational.
- *YOLO-World:* open-vocabulary detection meant no training, so a new recipe just lists new objects.
- *ESP32:* **[FILL]**

---

## Checks before you submit

1. **Hack Western dates:** your notes say Nov 21-22, 2026. The repo README says Fri Nov 20 to Sun Nov 22 and "team of 2". The writeup above says three people, matching your notes. Confirm.
2. **Watcher** is enabled by default in the repo (`--no-watcher` disables it), so it's safe to claim if it's on in your demo run.
3. **Closed-loop correction** (camera nudging the dot onto the target) and **encouraging praise** are not claimed above beyond "encouraging feedback". Add them only if the demo build shows them.
4. **Gemini** wording ("benchmarked, not in final build") assumes that's still true.
5. **Not claimed anywhere:** medical treatment. The wording is "designed with X in mind; next step is testing with OTs."
