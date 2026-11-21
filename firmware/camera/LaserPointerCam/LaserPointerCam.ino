/*
  LaserPointerCam.ino  -  Freenove ESP32-WROVER (CAM) firmware
  -------------------------------------------------------------
  Job of this board:
    1. Stream camera video (MJPEG)  ->  http://<ip>:81/stream
    2. Receive pan/tilt/laser commands over UDP port 4210
       Format (ASCII):  "<pan_deg>,<tilt_deg>,<laser 0|1>\n"   e.g. "92.5,80.0,1"
    3. Drive the two servos (slew-limited) and the laser enable pin.
    4. Fail-safe: laser turns OFF if no command arrives for 1 s.

  The heavy vision work (finding the target and the laser dot) runs on a
  laptop with host/laser_tracker.py. The ESP32 alone does not have the
  compute for real object detection.

  Arduino IDE settings (same as Freenove Chapter 6):
    Board: "ESP32 Wrover Module"   PSRAM: Enabled
    Partition scheme: "Huge APP (3MB No OTA/1MB SPIFFS)"
  Works with arduino-esp32 core 2.x and 3.x. No extra libraries needed.
*/

#include "esp_camera.h"
#include "esp_http_server.h"
#include <WiFi.h>
#include <WiFiUdp.h>
#include <ESPmDNS.h>

// ---------------- USER SETTINGS ----------------
// Phone/laptop hotspot (2.4 GHz)
#include "secrets.h"   // WIFI_SSID, WIFI_PASS (copy secrets.example.h; gitignored)
const char *MDNS_NAME = "coach-cam";   // -> http://coach-cam.local:81/stream

// Pins NOT used by the camera (see Freenove "Cam Pin" table).
// Avoid 0, 2, 12, 15 (strapping pins) and 16/17 (PSRAM).
#define PAN_PIN    32
#define TILT_PIN   33
#define LASER_PIN  13      // drives a transistor/MOSFET, NOT the laser directly

#define PAN_MIN   0.0f     // mechanical limits of YOUR gimbal (degrees)
#define PAN_MAX   180.0f
#define TILT_MIN  30.0f    // keep the laser from pointing up toward faces
#define TILT_MAX  150.0f
#define SERVO_MIN_US 500   // check your servo datasheet (SG90 ~500-2400)
#define SERVO_MAX_US 2400
#define MAX_SLEW_DEG_PER_TICK 1.5f   // smoothing; tick = 10 ms
#define CMD_TIMEOUT_MS 1000
#define UDP_PORT 4210
// ------------------------------------------------

// Camera model: CAMERA_MODEL_WROVER_KIT (Freenove ESP32-WROVER CAM)
#define CAMERA_MODEL_WROVER_KIT
#define PWDN_GPIO_NUM  -1
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM  21
#define SIOD_GPIO_NUM  26
#define SIOC_GPIO_NUM  27
#define Y9_GPIO_NUM    35
#define Y8_GPIO_NUM    34
#define Y7_GPIO_NUM    39
#define Y6_GPIO_NUM    36
#define Y5_GPIO_NUM    19
#define Y4_GPIO_NUM    18
#define Y3_GPIO_NUM     5
#define Y2_GPIO_NUM     4
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM  23
#define PCLK_GPIO_NUM  22

// Camera XCLK uses LEDC channel 0 / timer 0.
// Servos use channels 2 and 3 (timer 1) so they never fight the camera clock.
#define PAN_CH   2
#define TILT_CH  3
#define PWM_FREQ 50
#define PWM_RES  16

WiFiUDP udp;
httpd_handle_t streamHttpd = NULL;

volatile float targetPan = 90, targetTilt = 90;
float curPan = 90, curTilt = 90;
volatile bool laserReq = false;
unsigned long lastCmdMs = 0;

// ---------- servo helpers ----------
static uint32_t angleToDuty(float deg) {
  float us = SERVO_MIN_US + (deg / 180.0f) * (SERVO_MAX_US - SERVO_MIN_US);
  return (uint32_t)(us * ((1 << PWM_RES) - 1) / (1000000.0f / PWM_FREQ));
}

static void servoAttach(int pin, int ch) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttachChannel(pin, PWM_FREQ, PWM_RES, ch);
#else
  ledcSetup(ch, PWM_FREQ, PWM_RES);
  ledcAttachPin(pin, ch);
#endif
}

static void servoWrite(int pin, int ch, float deg) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(pin, angleToDuty(deg));
#else
  ledcWrite(ch, angleToDuty(deg));
#endif
}

static float approach(float cur, float tgt, float step) {
  if (tgt > cur + step) return cur + step;
  if (tgt < cur - step) return cur - step;
  return tgt;
}

// ---------- camera ----------
// This board's sensor is an OV3660 (the stock CameraWebServer page says so), not the OV2640 the docs assume.
// Measured 2026-11-21 with the stock example on this board, phone hotspot, laptop pulling :81/stream:
//   XCLK 10 MHz, any size  -> camera fails (no frames).   <- what stalled this sketch before
//   XCLK 20 MHz: QVGA 320x240 ~11 fps | CIF 400x296 ~10.5 fps | HVGA 480x320 ~12 fps | VGA 640x480 fails
// So: 20 MHz, HVGA. Calibrate the aim at this size (calibration is in pixels).
#define XCLK_HZ      20000000
#define FRAME_SIZE   FRAMESIZE_HVGA   // 480x320. Fallback: FRAMESIZE_QVGA
#define FB_COUNT     2                // as the stock example (PSRAM + GRAB_LATEST)
// JPEG quality: lower number = better image, bigger frames. ~8 KB/frame at HVGA q10-12.
#define JPEG_QUALITY 12

static bool cameraInit() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer   = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM;  c.pin_d1 = Y3_GPIO_NUM;
  c.pin_d2 = Y4_GPIO_NUM;  c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM;  c.pin_d5 = Y7_GPIO_NUM;
  c.pin_d6 = Y8_GPIO_NUM;  c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM;   c.pin_pclk  = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM; c.pin_href  = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM; c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM;   c.pin_reset = RESET_GPIO_NUM;
  // NOT 10 MHz: the OV3660 delivers no frames at 10 MHz (that was the stall). FB-OVF lines are harmless.
  c.xclk_freq_hz = XCLK_HZ;
  c.pixel_format = PIXFORMAT_JPEG;
  c.frame_size   = FRAME_SIZE;
  c.jpeg_quality = JPEG_QUALITY;
  if (psramFound()) {
    // "cam_hal: FB-OVF" = the sensor finished a frame while every buffer was busy (one being sent over
    // Wi-Fi), so that frame was dropped. Harmless with GRAB_LATEST; judge by the [stream] fps line.
    c.fb_count    = FB_COUNT;
    c.fb_location = CAMERA_FB_IN_PSRAM;
    c.grab_mode   = CAMERA_GRAB_LATEST; // drop stale frames, lowest latency
  } else {
    Serial.println("WARNING: no PSRAM - using 1 frame buffer in DRAM");
    c.fb_count    = 1;
    c.fb_location = CAMERA_FB_IN_DRAM;
    c.grab_mode   = CAMERA_GRAB_WHEN_EMPTY;
  }

  if (esp_camera_init(&c) != ESP_OK) return false;
  sensor_t *s = esp_camera_sensor_get();
  Serial.printf("[boot] sensor PID 0x%04x (%s)\n", s->id.PID,
                s->id.PID == OV3660_PID ? "OV3660" : s->id.PID == OV2640_PID ? "OV2640" : "other");
  s->set_vflip(s, 1);   // matches Freenove example orientation
  if (s->id.PID == OV3660_PID) {        // same tweaks as the stock CameraWebServer
    s->set_brightness(s, 1);
    s->set_saturation(s, -2);
  }
  s->set_framesize(s, FRAME_SIZE);
  s->set_quality(s, JPEG_QUALITY);
  // Tip: if the laser dot washes out, lower exposure a bit:
  // s->set_ae_level(s, -2);
  return true;
}

// ---------- MJPEG stream ----------
#define BOUNDARY "frame"
static esp_err_t streamHandler(httpd_req_t *req) {
  httpd_resp_set_type(req, "multipart/x-mixed-replace;boundary=" BOUNDARY);
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  Serial.println("[stream] client connected");
  char hdr[64];
  int misses = 0;
  uint32_t frames = 0;
  // Stats, printed every 2 s: where does each frame's time go? (capture vs Wi-Fi send)
  uint32_t statN = 0, statGetMs = 0, statSendMs = 0, statBytes = 0, statT0 = millis();
  while (true) {
    uint32_t t0 = millis();
    camera_fb_t *fb = esp_camera_fb_get();
    uint32_t t1 = millis();
    if (!fb) {
      // fb_get already waits ~4 s internally, so a few misses = camera stalled.
      // Bail out instead of spinning forever and locking the server.
      if (++misses >= 3) { Serial.println("[stream] camera stopped delivering frames"); break; }
      continue;
    }
    misses = 0;
    size_t hl = snprintf(hdr, sizeof(hdr),
                         "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
                         (unsigned)fb->len);
    esp_err_t r = httpd_resp_send_chunk(req, "\r\n--" BOUNDARY "\r\n", strlen("\r\n--" BOUNDARY "\r\n"));
    if (r == ESP_OK) r = httpd_resp_send_chunk(req, hdr, hl);
    if (r == ESP_OK) r = httpd_resp_send_chunk(req, (const char *)fb->buf, fb->len);
    size_t len = fb->len;
    esp_camera_fb_return(fb);              // ALWAYS returned -> no buffer leak
    if (r != ESP_OK) break;   // client disconnected (or send timed out)
    frames++;
    statN++; statGetMs += t1 - t0; statSendMs += millis() - t1; statBytes += len;
    if (millis() - statT0 >= 2000) {
      float secs = (millis() - statT0) / 1000.0f;
      Serial.printf("[stream] %.1f fps | capture %lu ms | send %lu ms | %lu KB/frame | %.0f KB/s | RSSI %d dBm\n",
                    statN / secs, (unsigned long)(statGetMs / statN), (unsigned long)(statSendMs / statN),
                    (unsigned long)(statBytes / statN / 1024), statBytes / 1024.0f / secs, WiFi.RSSI());
      statN = statGetMs = statSendMs = statBytes = 0; statT0 = millis();
    }
    delay(1);                 // yield so Wi-Fi/UDP tasks get CPU time
  }
  Serial.printf("[stream] client gone after %u frames\n", (unsigned)frames);
  return ESP_OK;
}

static void startStreamServer() {
  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  cfg.server_port = 81;
  cfg.ctrl_port = 32769;
  // The server has ONE worker task: while a stream runs, nobody else is served.
  // Short send timeout => a dead/abandoned client errors out in ~2 s and frees
  // the server, instead of blocking it and making new clients time out.
  cfg.send_wait_timeout = 2;
  cfg.recv_wait_timeout = 2;
  cfg.max_open_sockets  = 3;
  cfg.lru_purge_enable  = true;   // drop the oldest idle socket when full
  cfg.stack_size        = 8192;
  httpd_uri_t uri = { "/stream", HTTP_GET, streamHandler, NULL };
  if (httpd_start(&streamHttpd, &cfg) == ESP_OK)
    httpd_register_uri_handler(streamHttpd, &uri);
}

// ---------- UDP commands ----------
static void pollUdp() {
  int n = udp.parsePacket();
  if (n <= 0) return;
  char buf[64];
  int len = udp.read(buf, sizeof(buf) - 1);
  if (len <= 0) return;
  buf[len] = 0;
  float p, t; int l;
  if (sscanf(buf, "%f,%f,%d", &p, &t, &l) == 3) {
    targetPan  = constrain(p, PAN_MIN, PAN_MAX);
    targetTilt = constrain(t, TILT_MIN, TILT_MAX);
    laserReq   = (l != 0);
    lastCmdMs  = millis();
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);                             // let the serial monitor catch the first lines
  Serial.printf("\n[boot] camera board starting (PSRAM %s, %u bytes)\n",
                psramFound() ? "found" : "NOT found", (unsigned)ESP.getPsramSize());
  pinMode(LASER_PIN, OUTPUT);
  digitalWrite(LASER_PIN, LOW);           // laser OFF at boot

  if (!cameraInit()) {
    Serial.println("Camera init failed - check PSRAM enabled / board selection");
    while (true) delay(1000);
  }
  Serial.println("[boot] camera OK");

  servoAttach(PAN_PIN, PAN_CH);
  servoAttach(TILT_PIN, TILT_CH);
  servoWrite(PAN_PIN, PAN_CH, curPan);
  servoWrite(TILT_PIN, TILT_CH, curTilt);
  Serial.println("[boot] servos OK");

  Serial.printf("[boot] joining WiFi \"%s\" (dots = still trying)\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) { delay(300); Serial.print("."); }
  WiFi.setSleep(false);                   // after connecting: lower, steadier latency

  if (MDNS.begin(MDNS_NAME)) {
    MDNS.addService("http", "tcp", 81);
    Serial.printf("\nmDNS: http://%s.local:81/stream", MDNS_NAME);
  } else {
    Serial.println("\nmDNS failed - use the IP below");
  }
  Serial.printf("\nStream: http://%s:81/stream (IP fallback)   UDP cmd port: %d\n",
                WiFi.localIP().toString().c_str(), UDP_PORT);

  startStreamServer();
  udp.begin(UDP_PORT);
}

void loop() {
  pollUdp();

  static unsigned long lastTick = 0;
  if (millis() - lastTick >= 10) {        // 100 Hz servo update
    lastTick = millis();
    curPan  = approach(curPan,  targetPan,  MAX_SLEW_DEG_PER_TICK);
    curTilt = approach(curTilt, targetTilt, MAX_SLEW_DEG_PER_TICK);
    servoWrite(PAN_PIN, PAN_CH, curPan);
    servoWrite(TILT_PIN, TILT_CH, curTilt);
  }

  // Leak check: these numbers should stay flat over minutes of streaming.
  static unsigned long lastMem = 0;
  if (millis() - lastMem > 5000) {
    lastMem = millis();
    Serial.printf("heap free %u  min %u | psram free %u\n",
                  ESP.getFreeHeap(), ESP.getMinFreeHeap(), ESP.getFreePsram());
  }

  bool alive = (millis() - lastCmdMs) < CMD_TIMEOUT_MS;
  digitalWrite(LASER_PIN, (laserReq && alive) ? HIGH : LOW);
}
