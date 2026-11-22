# ratatouille

A chest-worn AI cooking coach. A camera watches your hands, you talk to it, and it talks back
and **points at things with a laser** ("grab the lime", plus a red dot on the lime).

Hackathon build, Fri 2026-11-20 to Sun 2026-11-22, team of 2. **Core goal: low latency.**

## How it fits together

```
 ESP32 WROVER CAM ──MJPEG/WiFi──▶                       ◀──BT──  mic
                                  LAPTOP (Python hub)     ──BT──▶  speaker / earbuds
 ESP32-S3 (servos, laser, TFT)  ◀──UDP/WiFi──▶           ──HTTPS─▶ Claude API, STT, TTS
```

The laptop is the hub. The two ESP boards never talk to each other directly.

Three loops run at different speeds. Design: [docs/architecture.md](docs/architecture.md). **Implementation plan (phases, owners, done-when): [docs/plan.md](docs/plan.md).** **What has to be built (16 deliverables, how they fit): [docs/software-map.md](docs/software-map.md).**

| Loop | Rate | Uses AI API? | Does |
|---|---|---|---|
| Pointing | 20-30 Hz | no | YOLO finds the target, OpenCV finds the dot, the servos nudge the dot onto the target |
| Voice | on speech | yes | speech-to-text, Claude, then text-to-speech, all streamed |
| Watcher | every 3-5 s | yes | keeps an event log and checks that steps are done |

## Layout

```
docs/            architecture, protocol, hardware, latency budget
firmware/
  camera/        Freenove WROVER CAM: stock CameraWebServer + notes
  control_s3/    ESP32-S3: UDP commands -> servos, laser, TFT display
laptop/
  coach/         Python hub (asyncio); coach/static/ = the built web pages
  frontend/      source of the dashboard and phone pages (React + Vite); see its README
  recipes/       recipe JSON (steps, objects to detect)
```

## Status

Sketch / scaffolding. Every `TODO` marks unbuilt work. Short version (full plan in docs/plan.md):

- [ ] Fri night: camera stream visible on the laptop; servos move from a laptop UDP command; Claude hello world
- [ ] Sat AM: YOLO + red dot + calibration (A); voice loop end to end (B)
- [ ] Sat PM: integrate; full guacamole run
- [ ] Sun AM: polish, dashboard, 5 rehearsals, record a backup demo video

## Running (planned)

```bash
brew install portaudio        # C library pyaudio compiles against (Pipecat local audio); pip can't install it
cd laptop
python3.12 -m venv .venv && source .venv/bin/activate   # 3.12: Pipecat + deps aren't verified on 3.14
pip install -r requirements.txt
cp .env.example .env   # fill in keys and IPs
python -m coach.main
```
