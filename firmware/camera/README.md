# Camera firmware (Freenove ESP32 WROVER CAM)

Start from Freenove's tutorial copy of the **CameraWebServer** example (File → Examples → ESP32 →
Camera → CameraWebServer). Changes:

1. Select the camera model: `#define CAMERA_MODEL_WROVER_KIT`.
2. Put the hotspot SSID and password in.
3. Lower latency and raise frame rate in `setup()` after `esp_camera_init`:
   ```cpp
   sensor_t *s = esp_camera_sensor_get();
   s->set_framesize(s, FRAMESIZE_VGA);   // 640x480
   s->set_quality(s, 12);                // lower number = better quality, bigger frames
   ```
   and in `camera_config_t`: `config.fb_count = 2; config.grab_mode = CAMERA_GRAB_LATEST;`
4. `WiFi.setSleep(false);` after connecting.
5. mDNS so the laptop finds it by name: `#include <ESPmDNS.h>` then `MDNS.begin("coach-cam");`.

The stream is at `http://coach-cam.local:81/stream`. Expect ~15-25 fps at VGA.

TODO: measure glass-to-laptop latency (film a stopwatch). Target under 150 ms.
