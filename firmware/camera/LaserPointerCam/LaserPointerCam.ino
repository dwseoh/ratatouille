/*
  LaserPointerCam.ino  -  Freenove ESP32-WROVER CAM firmware: camera streaming ONLY
  -------------------------------------------------------------------------------
  Streams MJPEG to the laptop:  http://coach-cam.local:81/stream  (or http://<ip>:81/stream)
  That's all this board does. The servos and the laser are on the ESP32-S3 (firmware/control_s3,
  docs/hardware.md). YOLO, the dot finder and aiming run on the laptop.

  Serial (115200): boot progress, the stream line every 2 s while a client is connected
    [stream] 12.0 fps | capture 5 ms | send 70 ms | 8 KB/frame | 96 KB/s | RSSI -45 dBm
  and a heap/PSRAM line every 5 s (should stay flat). "cam_hal: FB-OVF" = a frame was dropped because
  every buffer was busy; harmless unless the fps in the stream line drops.

  ONE client at a time: while a browser tab shows the stream, the laptop app can't get it.

  Arduino IDE settings (same as Freenove Chapter 6):
    Board: "ESP32 Wrover Module"   PSRAM: Enabled
    Partition scheme: "Huge APP (3MB No OTA/1MB SPIFFS)"
  Wi-Fi: copy secrets.example.h to secrets.h (gitignored) and fill in the phone hotspot.
*/

#include "esp_camera.h"
#include "esp_http_server.h"
#include <WiFi.h>
#include <ESPmDNS.h>

#include "secrets.h"   // WIFI_SSID, WIFI_PASS
const char *MDNS_NAME = "coach-cam";   // -> http://coach-cam.local:81/stream

// ---------- camera settings ----------
// This board's sensor is an OV3660 (docs/constraints.md 4.3).
// Settings = Freenove's Sketch_06.1_CameraWebServer, which streams cleanly on this board. With 20 MHz /
// 2 buffers / GRAB_LATEST the old sketch got corrupted, banded JPEGs and 3-4 s per frame.
// Change ONE thing at a time from here, and keep a setting only if the [stream] line stays healthy.
#define XCLK_HZ      10000000
#define FRAME_SIZE   FRAMESIZE_VGA   // 640x480: works with these settings (10 MHz, 1 buffer). Fallback: FRAMESIZE_QVGA
#define FB_COUNT     1
#define GRAB_MODE    CAMERA_GRAB_WHEN_EMPTY
#define JPEG_QUALITY 12               // lower number = better image, bigger frames

// Image look (OV3660 sensor registers; no effect on frame rate). Ranges: contrast/brightness/sharpness -3..3,
// saturation -4..4. The defaults (0) look washed out on this module. Find good values live with Freenove's
// CameraWebServer sliders, copy them here, then DON'T change them after recording YOLO training frames
// (a different look = different images = retrain).
#define CONTRAST     2
#define SATURATION   2
#define BRIGHTNESS   0
#define SHARPNESS    1

// Camera pins: CAMERA_MODEL_WROVER_KIT (Freenove ESP32-WROVER CAM)
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

httpd_handle_t streamHttpd = NULL;

// ---------- camera ----------
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
  c.xclk_freq_hz = XCLK_HZ;
  c.pixel_format = PIXFORMAT_JPEG;
  c.frame_size   = FRAME_SIZE;
  c.jpeg_quality = JPEG_QUALITY;
  if (psramFound()) {
    c.fb_count    = FB_COUNT;
    c.fb_location = CAMERA_FB_IN_PSRAM;
    c.grab_mode   = GRAB_MODE;
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
  if (s->id.PID == OV3660_PID) {        // orientation as Freenove's sketch for this sensor
    s->set_hmirror(s, 1);
    s->set_vflip(s, 0);
  } else {
    s->set_hmirror(s, 1);
    s->set_vflip(s, 1);
  }
  s->set_contrast(s, CONTRAST);
  s->set_saturation(s, SATURATION);
  s->set_brightness(s, BRIGHTNESS);
  s->set_sharpness(s, SHARPNESS);
  Serial.printf("[boot] look: contrast %d saturation %d brightness %d sharpness %d\n",
                CONTRAST, SATURATION, BRIGHTNESS, SHARPNESS);
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
    delay(1);                 // yield so the Wi-Fi task gets CPU time
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

void setup() {
  Serial.begin(115200);
  delay(500);                             // let the serial monitor catch the first lines
  Serial.printf("\n[boot] camera board starting (PSRAM %s, %u bytes)\n",
                psramFound() ? "found" : "NOT found", (unsigned)ESP.getPsramSize());

  if (!cameraInit()) {
    Serial.println("Camera init failed - check PSRAM enabled / board selection");
    while (true) delay(1000);
  }
  Serial.println("[boot] camera OK");

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
  Serial.printf("\nStream: http://%s:81/stream (IP fallback)\n", WiFi.localIP().toString().c_str());

  startStreamServer();
}

void loop() {
  // Leak check: these numbers should stay flat over minutes of streaming.
  static unsigned long lastMem = 0;
  if (millis() - lastMem > 5000) {
    lastMem = millis();
    Serial.printf("heap free %u  min %u | psram free %u\n",
                  ESP.getFreeHeap(), ESP.getMinFreeHeap(), ESP.getFreePsram());
  }
  delay(10);                              // nothing else to do: don't spin the CPU
}
