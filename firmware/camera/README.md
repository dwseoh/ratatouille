# Camera firmware (Freenove ESP32 WROVER CAM, OV3660 sensor)

`LaserPointerCam/LaserPointerCam.ino` streams MJPEG to the laptop. That's the board's only job: the servos and the
laser are on the ESP32-S3 (`firmware/control_s3`).

- Stream: `http://coach-cam.local:81/stream` (or `http://<ip>:81/stream`, printed on Serial at boot).
- Camera settings are Freenove's `Sketch_06.1_CameraWebServer` ones, which stream cleanly with this sensor:
  VGA 640x480 (fallback QVGA 320x240), 10 MHz XCLK, JPEG quality 12, 1 frame buffer, `CAMERA_GRAB_WHEN_EMPTY`, WiFi sleep off.
  20 MHz + 2 buffers + `CAMERA_GRAB_LATEST` gave corrupted, banded frames on this board.
  Change one setting at a time (they're `#define`s at the top) and keep it only if the stream stays clean.
- Serial (115200): `[boot] sensor PID 0x3660 (OV3660)`, the stream address, a `[stream] fps | capture | send |
  KB/frame` line every 2 s while a client is connected, and a heap/PSRAM line every 5 s.
- One client at a time: close any browser tab on the stream before running the coach.

Setup: copy `LaserPointerCam/secrets.example.h` to `secrets.h` (gitignored) and fill in the phone hotspot.
Arduino IDE: board "ESP32 Wrover Module", PSRAM Enabled, partition "Huge APP (3MB No OTA/1MB SPIFFS)".
On an iPhone hotspot a Mac may get no IPv4 address (IPv6-only); give it a manual IPv4 (e.g. 172.20.10.14,
mask 255.255.255.240, router 172.20.10.1) or it can't reach the camera.

TODO: measure glass-to-laptop latency (film a stopwatch). Target under 150 ms.
