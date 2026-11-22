# Laptop ↔ ESP32-S3 protocol (UDP, port 4210)

ASCII, one command per datagram, fields separated by spaces. Easy to debug with `nc -u`.

## Laptop → S3

| Command | Example | Meaning |
|---|---|---|
| `C <pan> <tilt> <laser> <seq>` | `C 92.5 61.0 1 1834` | Control and heartbeat. Angles in degrees (float), laser 0/1. Sent at 30 Hz nonstop. |
| `T <line> <text>` | `T 2 Step 3: mash it` | Put text on display line 0-4 (truncated to 20 chars). |
| `R` | `R` | Reset to safe state (laser off, servos centred). |

## S3 → laptop (sent to the address of the last `C` packet)

| Message | Example | Meaning |
|---|---|---|
| `S <pan> <tilt> <laser> <rssi> <vbat_mv> <last_seq> <uptime_ms>` | `S 92.5 61.0 1 -58 5120 1834 90233` | Status every 200 ms |
| `B` | `B` | BOOT button (GPIO0) pressed. Edge-triggered, 500 ms debounce. Not used by the coach (it starts with "hey Remy"). |

## Safety rules (enforced on the S3, not the laptop)

- No `C` packet for **500 ms** → laser off, servos hold position.
- Servos accept the full 0-180° range. The laptop applies the real limits (`PAN_*` / `TILT_*` in `laptop/.env`)
  so they change without a reflash; set `TILT_*` so the beam stays below horizontal.
- Laser starts off on boot.

## Why UDP

Lowest latency, no connection state. A lost packet doesn't matter because a fresh one arrives 33 ms later.
