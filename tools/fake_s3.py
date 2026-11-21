#!/usr/bin/env python3
"""Fake ESP32-S3 controller for testing without hardware. Protocol: docs/protocol.md.

    python tools/fake_s3.py [port]      # default 4210

Prints each packet (C only when pan/tilt/laser change), sends S every 200 ms to the last C sender,
Enter sends B (BOOT button), and prints `laser OFF (watchdog)` after 500 ms without a C.
Stdlib only.
"""
import socket
import sys
import threading
import time

WATCHDOG_S = 0.5
STATUS_EVERY_S = 0.2


def out(msg: str):
    print(msg, flush=True)


def main(port: int = 4210):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.02)
    t0 = time.monotonic()
    pan, tilt, laser = 90.0, 60.0, False
    last_seq, last_c, last_status = 0, 0.0, 0.0
    hub = None              # address of the last C packet (where S and B go)
    shown = None            # last printed (pan, tilt, laser)
    watchdog_fired = True   # nothing to time out until the first C

    def stdin_loop():       # Enter -> B
        for _ in sys.stdin:
            if hub:
                sock.sendto(b"B", hub)
                out("-> B (button)")
            else:
                out("no hub yet (waiting for a C packet)")

    threading.Thread(target=stdin_loop, daemon=True).start()
    out(f"fake S3 listening on udp 0.0.0.0:{port}  (Enter = BOOT button)")

    while True:
        try:
            data, addr = sock.recvfrom(256)
        except socket.timeout:
            data = None
        now = time.monotonic()
        if data:
            msg = data.decode(errors="ignore").rstrip("\r\n")
            parts = msg.split()
            kind = parts[0] if parts else ""
            if kind == "C" and len(parts) == 5:
                try:
                    pan, tilt, laser, last_seq = float(parts[1]), float(parts[2]), parts[3] != "0", int(parts[4])
                except ValueError:
                    out(f"bad C: {msg!r}")
                    continue
                hub, last_c, watchdog_fired = addr, now, False
                if (pan, tilt, laser) != shown:
                    shown = (pan, tilt, laser)
                    out(f"C pan={pan:.1f} tilt={tilt:.1f} laser={int(laser)} seq={last_seq}")
            elif kind == "T":
                line, _, text = msg[2:].partition(" ")
                out(f"T line {line}: {text[:20]!r}")
            elif kind == "R":
                pan, tilt, laser = 90.0, 60.0, False
                out("R reset: laser off, servos centred")
            else:
                out(f"? {msg!r} from {addr[0]}:{addr[1]}")

        if not watchdog_fired and now - last_c > WATCHDOG_S:
            watchdog_fired, laser, shown = True, False, None
            out("laser OFF (watchdog)")

        if hub and now - last_status >= STATUS_EVERY_S:
            last_status = now
            uptime = int((now - t0) * 1000)
            sock.sendto(f"S {pan:.1f} {tilt:.1f} {int(laser)} -50 5100 {last_seq} {uptime}".encode(), hub)


if __name__ == "__main__":
    try:
        main(int(sys.argv[1]) if len(sys.argv) > 1 else 4210)
    except KeyboardInterrupt:
        pass
