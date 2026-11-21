// Bench test for the pan-tilt + laser on the ESP32-S3 (no WiFi, no display).
// Use it to: check the servos move smoothly, centre them before fitting the arms,
// and find the tilt angle where the beam reaches horizontal.
//
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
bool gotCmd = false;  // stays false until something is typed -> auto-sweep mode

void setLaser(bool on) { laserOn = on; digitalWrite(PIN_LASER, on ? HIGH : LOW); }

// Called every servo step and while idling, so the laser blinks even during a sweep.
void laserTick() {
  if (!blinkLaser) return;
  uint32_t now = millis();
  if (now - lastBlink >= BLINK_MS) { lastBlink = now; setLaser(!laserOn); }
}

// delay() that keeps the laser blinking.
void waitMs(uint32_t ms) {
  uint32_t t0 = millis();
  while (millis() - t0 < ms) { laserTick(); delay(5); }
}

int clampi(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }

// Move one servo gradually from its current angle to the target.
void slew(LedcServo &s, int &cur, int target) {
  while (cur != target) {
    cur += (target > cur) ? 1 : -1;
    s.write(cur);
    laserTick();
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
    laserTick();
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
  Serial.println("sweep done. Smooth? No buzzing/jitter? No ESP reset?");
}

void setup() {
  pinMode(PIN_LASER, OUTPUT);
  digitalWrite(PIN_LASER, LOW);  // laser off at boot
  Serial.begin(115200);
  delay(500);
  pan.attach(PIN_PAN);
  tilt.attach(PIN_TILT);
  pan.write(panDeg);
  tilt.write(tiltDeg);
  Serial.println("\nservo_test ready. Both servos at 90. Commands: c, p <deg>, t <deg>, u, d, s, r, l, b, ?");
}

void loop() {
  if (!Serial.available()) {
    if (!gotCmd && millis() > AUTO_START_MS) {  // no-laptop mode
      if (AUTO_LASER && !blinkLaser) { blinkLaser = true; Serial.println("auto mode: sweep + laser blink"); }
      sweep(); waitMs(1000);
    } else if (repeatSweep) { sweep(); waitMs(1000); }
    else laserTick();
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
