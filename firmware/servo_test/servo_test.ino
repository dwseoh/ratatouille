// Bench + power test for the pan-tilt + laser on the ESP32-S3 (no WiFi). Shows its state on the TFT.
// Use it to: check the servos move smoothly, centre them before fitting the arms,
// and find the tilt angle where the beam reaches horizontal.
//
// Libraries: Adafruit ST7735 and ST7789 (+ Adafruit GFX). Works with no TFT wired (draws to nothing).
// Board: "ESP32S3 Dev Module" (esp32 board package 3.x). No servo library needed: the servos are
// driven straight from the chip's LEDC PWM (ESP32Servo breaks on the newest esp32 package).
// Plug the USB cable into the port marked "UART"/"COM" (if you use the "USB" port instead,
// set Tools > USB CDC On Boot: Enabled). Serial Monitor: 115200 baud, line ending "Newline".
//
// Wiring (same pins as control_s3.ino):
//   pan servo signal (orange)  -> GPIO 4
//   tilt servo signal (orange) -> GPIO 5
//   laser S                    -> GPIO 6   (bench test only; the real build switches it with Q1)
//   laser -                    -> GND
//   servo red (+)  -> bank 5 V (breakout VBUS). NOT the ESP's 5VIN pin: it can't supply current.
//   servo brown (-) -> bank GND, AND bank GND -> ESP GND (grounds must be shared)
//   optional: 1000 uF across the servo 5 V and GND (stripe to GND) if the ESP resets when they move
//   TFT (optional): VCC -> ESP 3V3 (not 5 V), GND, SCL -> 12, SDA -> 11, RES -> 8, DC -> 9, CS -> 10, BLK -> 7
//
// Screen: mode, angles, laser, uptime, sweeps done, and WHY the ESP last reset. During a power test,
// "reset: BROWNOUT" or an uptime that keeps going back to 0 means the 5 V rail dipped.
//
// Commands (type in the Serial Monitor, press Enter):
//   c        centre both servos at 90
//   p 120    pan to 120 deg        t 70    tilt to 70 deg
//   u / d    tilt up / down by 1 deg (to creep up to horizontal)
//   s        slow sweep test of both servos, then back to centre
//   l        laser on/off
//   b        laser blink on/off (500 ms on / 500 ms off)
//   r        repeat: sweep over and over (1 s pause between) until you press r again
//   ?        print the current angles
// The laser starts OFF. Point the pan-tilt at a wall and keep faces out of the way.
//
// NO-LAPTOP MODE: if nothing is typed within 3 s of power-up (AUTO_START_MS), it sweeps forever on its own,
// with the laser BLINKING (AUTO_LASER). Point it at a wall.
// Use it to run the ESP from the power bank too (bank 5 V -> ESP 5V pin, USB unplugged):
// the ESP's own current keeps the bank from switching itself off.

#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include "esp_system.h"

// Minimal servo driver on the ESP32's LEDC PWM (esp32 board package 3.x API).
struct LedcServo {
  int pin = -1;
  void attach(int p) { pin = p; ledcAttach(pin, 50, 14); }   // 50 Hz, 14-bit duty (0..16383 over 20 ms)
  void write(int deg) {                                      // SG90: 500 us = 0 deg, 2400 us = 180 deg
    uint32_t us = 500 + (uint32_t)deg * 1900 / 180;
    ledcWrite(pin, us * 16383 / 20000);
  }
};

const int PIN_PAN = 4, PIN_TILT = 5, PIN_LASER = 6;
const int PIN_TFT_BL = 7, PIN_TFT_RST = 8, PIN_TFT_DC = 9, PIN_TFT_CS = 10, PIN_TFT_MOSI = 11, PIN_TFT_SCK = 12;
const uint32_t SCREEN_MS = 250;          // redraw at most this often (drawing takes a few ms)
const int PAN_MIN = 10, PAN_MAX = 170;
const int TILT_MIN = 0, TILT_MAX = 180;  // full range here, to find the limits. control_s3.ino clamps it.
const bool AUTO_LASER = true;            // laser on during the no-laptop auto sweep
const int STEP_MS = 12;                  // slew speed: 1 deg every 12 ms (smooth, gentle on the power)
const uint32_t AUTO_START_MS = 3000;     // no-laptop mode starts this long after boot if nothing is typed
const uint32_t BLINK_MS = 500;           // laser blink period half (on 500 ms, off 500 ms) in auto mode / 'b'

LedcServo pan, tilt;
void slew(LedcServo &s, int &cur, int target);  // explicit prototype (keeps the Arduino IDE from guessing one above the struct)
int panDeg = 90, tiltDeg = 90;
bool laserOn = false;
bool repeatSweep = false;  // 'r' command
bool blinkLaser = false;   // auto mode or 'b' command: laser toggles every BLINK_MS while running
uint32_t lastBlink = 0;
uint32_t sweeps = 0;
const char *resetWhy = "?";

Adafruit_ST7789 tft(&SPI, PIN_TFT_CS, PIN_TFT_DC, PIN_TFT_RST);
const uint16_t LINE_COLOR[5] = {ST77XX_GREEN, ST77XX_WHITE, ST77XX_CYAN, ST77XX_YELLOW, ST77XX_WHITE};
char shown[5][21];                       // what each line shows now (only changed lines are redrawn)
uint32_t lastScreen = 0;
bool gotCmd = false;  // stays false until something is typed -> auto-sweep mode

void setLaser(bool on) { laserOn = on; digitalWrite(PIN_LASER, on ? HIGH : LOW); }

const char *resetReason() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:  return "power on";
    case ESP_RST_BROWNOUT: return "BROWNOUT!";
    case ESP_RST_PANIC:    return "crash";
    case ESP_RST_INT_WDT: case ESP_RST_TASK_WDT: case ESP_RST_WDT: return "watchdog";
    case ESP_RST_SW:       return "software";
    case ESP_RST_EXT:      return "reset pin";
    default:               return "other";
  }
}

// Draw one 20-char line, padded so old text is overwritten without clearing the screen (no flicker).
void drawLine(int i, const char *text) {
  char padded[21];
  snprintf(padded, sizeof(padded), "%-20s", text);
  if (strcmp(padded, shown[i]) == 0) return;
  strcpy(shown[i], padded);
  tft.setTextColor(LINE_COLOR[i], ST77XX_BLACK);
  tft.setCursor(0, 8 + i * 46);
  tft.print(padded);
}

void screenTick() {
  uint32_t now = millis();
  if (now - lastScreen < SCREEN_MS) return;
  lastScreen = now;
  char buf[24];
  drawLine(0, (!gotCmd && now > AUTO_START_MS) ? "AUTO sweep" : (gotCmd ? "serial control" : "auto in 3 s..."));
  snprintf(buf, sizeof(buf), "pan %3d tilt %3d", panDeg, tiltDeg);     drawLine(1, buf);
  drawLine(2, blinkLaser ? "laser BLINK" : (laserOn ? "laser ON" : "laser off"));
  snprintf(buf, sizeof(buf), "up %lus sw %lu", (unsigned long)(now / 1000), (unsigned long)sweeps);
  drawLine(3, buf);
  snprintf(buf, sizeof(buf), "reset: %s", resetWhy);                    drawLine(4, buf);
}

// Called every servo step and while idling: the laser blinks and the screen updates even during a sweep.
void tick() {
  if (blinkLaser) {
    uint32_t now = millis();
    if (now - lastBlink >= BLINK_MS) { lastBlink = now; setLaser(!laserOn); }
  }
  screenTick();
}

// delay() that keeps the laser blinking.
void waitMs(uint32_t ms) {
  uint32_t t0 = millis();
  while (millis() - t0 < ms) { tick(); delay(5); }
}

int clampi(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }

// Move one servo gradually from its current angle to the target.
void slew(LedcServo &s, int &cur, int target) {
  while (cur != target) {
    cur += (target > cur) ? 1 : -1;
    s.write(cur);
    tick();
    delay(STEP_MS);
  }
}

// Move both servos together (both arrive at the same time).
void slewBoth(int panT, int tiltT) {
  while (panDeg != panT || tiltDeg != tiltT) {
    if (panDeg != panT) panDeg += (panT > panDeg) ? 1 : -1;
    if (tiltDeg != tiltT) tiltDeg += (tiltT > tiltDeg) ? 1 : -1;
    pan.write(panDeg);
    tilt.write(tiltDeg);
    tick();
    delay(STEP_MS);
  }
}

void report() {
  Serial.printf("pan %d  tilt %d  laser %s\n", panDeg, tiltDeg, laserOn ? "ON" : "off");
}

void sweep() {
  Serial.println("sweep: pan 10 -> 170 -> 90, tilt 60 -> 120 -> 90, then both together");
  slew(pan, panDeg, PAN_MIN);
  slew(pan, panDeg, PAN_MAX);
  slew(pan, panDeg, 90);
  slew(tilt, tiltDeg, 60);
  slew(tilt, tiltDeg, 120);
  slew(tilt, tiltDeg, 90);
  slewBoth(60, 70);    // both at once: a diagonal box
  slewBoth(120, 70);
  slewBoth(120, 110);
  slewBoth(60, 110);
  slewBoth(90, 90);
  sweeps++;
  Serial.println("sweep done. Smooth? No buzzing/jitter? No ESP reset?");
}

void setup() {
  pinMode(PIN_LASER, OUTPUT);
  digitalWrite(PIN_LASER, LOW);  // laser off at boot
  Serial.begin(115200);
  resetWhy = resetReason();
  pinMode(PIN_TFT_BL, OUTPUT); digitalWrite(PIN_TFT_BL, HIGH);
  SPI.begin(PIN_TFT_SCK, -1, PIN_TFT_MOSI, PIN_TFT_CS);
  tft.init(240, 240);  // module with no CS pin: tft.init(240, 240, SPI_MODE3)
  tft.setSPISpeed(40000000);
  tft.fillScreen(ST77XX_BLACK);
  tft.setTextSize(2);
  tft.setTextWrap(false);
  screenTick();
  delay(500);
  pan.attach(PIN_PAN);
  tilt.attach(PIN_TILT);
  pan.write(panDeg);
  tilt.write(tiltDeg);
  Serial.printf("\nlast reset: %s\n", resetWhy);
  Serial.println("servo_test ready. Both servos at 90. Commands: c, p <deg>, t <deg>, u, d, s, r, l, b, ?");
}

void loop() {
  if (!Serial.available()) {
    if (!gotCmd && millis() > AUTO_START_MS) {  // no-laptop mode
      if (AUTO_LASER && !blinkLaser) { blinkLaser = true; Serial.println("auto mode: sweep + laser blink"); }
      sweep(); waitMs(1000);
    } else if (repeatSweep) { sweep(); waitMs(1000); }
    else tick();
    return;
  }
  if (!gotCmd && blinkLaser) { blinkLaser = false; setLaser(false); }  // typing stops auto mode
  gotCmd = true;
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.length() == 0) return;
  char cmd = line.charAt(0);
  int arg = line.substring(1).toInt();

  switch (cmd) {
    case 'c': slew(pan, panDeg, 90); slew(tilt, tiltDeg, 90); break;
    case 'p': slew(pan, panDeg, clampi(arg, PAN_MIN, PAN_MAX)); break;
    case 't': slew(tilt, tiltDeg, clampi(arg, TILT_MIN, TILT_MAX)); break;
    case 'u': slew(tilt, tiltDeg, clampi(tiltDeg + 1, TILT_MIN, TILT_MAX)); break;
    case 'd': slew(tilt, tiltDeg, clampi(tiltDeg - 1, TILT_MIN, TILT_MAX)); break;
    case 's': sweep(); break;
    case 'r': repeatSweep = !repeatSweep; Serial.println(repeatSweep ? "repeat ON (r to stop)" : "repeat off"); break;
    case 'l': blinkLaser = false; setLaser(!laserOn); break;
    case 'b': blinkLaser = !blinkLaser; if (!blinkLaser) setLaser(false);
              Serial.println(blinkLaser ? "blink ON (b to stop)" : "blink off"); break;
    case '?': break;
    default: Serial.println("commands: c, p <deg>, t <deg>, u, d, s, r, l, b, ?"); return;
  }
  report();
}
