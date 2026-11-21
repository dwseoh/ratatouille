// ESP32-S3 controller: UDP commands -> pan/tilt servos, laser, TFT. Protocol in docs/protocol.md.
// Libraries: Adafruit ST7735 and ST7789, Adafruit GFX, ESPmDNS (core). Needs the esp32 board package 3.x.
// Servos run on the chip's LEDC PWM directly (ESP32Servo fails to build on the newest esp32 package).
// Board: "ESP32S3 Dev Module". Our board is N8R2: PSRAM "QSPI PSRAM" (or Disabled), Flash Size 8MB.
// ("OPI PSRAM" on an R2 board prints "octal_psram: PSRAM chip is not connected".)
#include <WiFi.h>
#include <WiFiUdp.h>
#include <ESPmDNS.h>
#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include "secrets.h"  // WIFI_SSID, WIFI_PASS (copy secrets.example.h)

const int PIN_PAN = 4, PIN_TILT = 5, PIN_LASER = 6, PIN_VBAT = 1;
const int PIN_BOOT = 0;  // BOOT button, a normal input after boot. Press -> "B" to the hub.
// 1.54" 240x240 ST7789 TFT on SPI. The module's "SCL"/"SDA" pins are SPI clock/data.
const int PIN_TFT_BL = 7, PIN_TFT_RST = 8, PIN_TFT_DC = 9, PIN_TFT_CS = 10, PIN_TFT_MOSI = 11, PIN_TFT_SCK = 12;
const int LINE_CHARS = 20;  // text size 2 = 12 px per char, 240 px wide
const uint16_t UDP_PORT = 4210;
const uint32_t WATCHDOG_MS = 500;       // no control packet -> laser off
const uint32_t STATUS_EVERY_MS = 200;
const float PAN_MIN = 10, PAN_MAX = 170;
const float TILT_MIN = 20, TILT_MAX = 100;  // TODO: set after mounting so the beam never goes above horizontal

// Minimal servo driver on LEDC (esp32 3.x API). Microseconds give sub-degree steps.
struct LedcServo {
  int pin = -1;
  void attach(int p) { pin = p; ledcAttach(pin, 50, 14); }   // 50 Hz, 14-bit duty over 20 ms
  void write(float deg) {                                    // SG90: 500 us = 0 deg, 2400 us = 180 deg
    uint32_t us = 500 + (uint32_t)(deg * 1900.0f / 180.0f);
    ledcWrite(pin, us * 16383 / 20000);
  }
};
LedcServo pan, tilt;
WiFiUDP udp;
Adafruit_ST7789 tft(&SPI, PIN_TFT_CS, PIN_TFT_DC, PIN_TFT_RST);

float panDeg = 90, tiltDeg = 60;
bool laserOn = false;
uint32_t lastCmdMs = 0, lastStatusMs = 0, lastSeq = 0;
IPAddress hubIp; uint16_t hubPort = 0;
char lines[5][22] = {"coach-ctl", "", "", "", ""};
bool lineDirty[5] = {true, true, true, true, true};
const uint16_t LINE_COLOR[5] = {ST77XX_GREEN, ST77XX_WHITE, ST77XX_CYAN, ST77XX_YELLOW, ST77XX_WHITE};

static float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

void applyOutputs() {
  pan.write(clampf(panDeg, PAN_MIN, PAN_MAX));
  tilt.write(clampf(tiltDeg, TILT_MIN, TILT_MAX));
  digitalWrite(PIN_LASER, laserOn ? HIGH : LOW);
}

void safeState() { laserOn = false; applyOutputs(); }

void handlePacket(char *msg) {
  switch (msg[0]) {
    case 'C': {  // C pan tilt laser seq
      float p, t; int l; unsigned long s;
      if (sscanf(msg + 1, "%f %f %d %lu", &p, &t, &l, &s) == 4) {
        panDeg = p; tiltDeg = t; laserOn = l != 0; lastSeq = s;
        lastCmdMs = millis(); hubIp = udp.remoteIP(); hubPort = udp.remotePort();
        applyOutputs();
      }
      break;
    }
    case 'T': {  // T line text
      int line = msg[2] - '0';
      if (line >= 0 && line < 5 && strlen(msg) > 4) {
        strncpy(lines[line], msg + 4, LINE_CHARS); lines[line][LINE_CHARS] = 0; lineDirty[line] = true;
      }
      break;
    }
    case 'R': panDeg = 90; tiltDeg = 60; safeState(); break;
  }
}

// Redraw only the lines that changed. Text drawn with a background colour, padded to the full
// width, so the old text is overwritten without clearing the screen (no flicker).
void drawLines() {
  char padded[LINE_CHARS + 1];
  for (int i = 0; i < 5; i++) {
    if (!lineDirty[i]) continue;
    snprintf(padded, sizeof(padded), "%-*s", LINE_CHARS, lines[i]);
    tft.setTextColor(LINE_COLOR[i], ST77XX_BLACK);
    tft.setCursor(0, 8 + i * 46);
    tft.print(padded);
    lineDirty[i] = false;
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_LASER, OUTPUT); digitalWrite(PIN_LASER, LOW);  // laser off before anything else
  pinMode(PIN_BOOT, INPUT_PULLUP);
  pan.attach(PIN_PAN); tilt.attach(PIN_TILT);
  applyOutputs();

  pinMode(PIN_TFT_BL, OUTPUT); digitalWrite(PIN_TFT_BL, HIGH);
  SPI.begin(PIN_TFT_SCK, -1, PIN_TFT_MOSI, PIN_TFT_CS);
  tft.init(240, 240);  // module with no CS pin: tft.init(240, 240, SPI_MODE3)
  tft.setSPISpeed(40000000);
  tft.fillScreen(ST77XX_BLACK);
  tft.setTextSize(2);
  tft.setTextWrap(false);
  snprintf(lines[1], sizeof(lines[1]), "wifi: connecting"); drawLines();

  WiFi.mode(WIFI_STA); WiFi.setSleep(false);  // no modem sleep = lower UDP latency
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) delay(200);
  MDNS.begin("coach-ctl");
  udp.begin(UDP_PORT);
  snprintf(lines[1], sizeof(lines[1]), "%s", WiFi.localIP().toString().c_str()); lineDirty[1] = true;
}

void loop() {
  char buf[128];
  int n;
  while ((n = udp.parsePacket()) > 0) {
    int len = udp.read(buf, sizeof(buf) - 1); buf[len > 0 ? len : 0] = 0;
    handlePacket(buf);
  }
  uint32_t now = millis();
  if (laserOn && now - lastCmdMs > WATCHDOG_MS) safeState();  // fail safe
  if (WiFi.status() != WL_CONNECTED) { safeState(); WiFi.reconnect(); delay(100); }

  static uint32_t lastPress = 0; static bool wasDown = false;
  bool down = digitalRead(PIN_BOOT) == LOW;
  if (down && !wasDown && now - lastPress > 500 && hubPort) {  // edge + 500 ms debounce
    lastPress = now;
    udp.beginPacket(hubIp, hubPort); udp.print("B"); udp.endPacket();
  }
  wasDown = down;

  if (hubPort && now - lastStatusMs > STATUS_EVERY_MS) {
    lastStatusMs = now;
    int vbat = analogReadMilliVolts(PIN_VBAT) * 2;  // 100k/100k divider; 0 if not fitted
    udp.beginPacket(hubIp, hubPort);
    udp.printf("S %.1f %.1f %d %d %d %lu %lu", panDeg, tiltDeg, laserOn, WiFi.RSSI(), vbat, lastSeq, now);
    udp.endPacket();
  }
  drawLines();  // only touches changed lines; one line is ~1 ms at 40 MHz SPI
}
