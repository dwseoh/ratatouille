"""Jog the laser with the keyboard while watching the camera: python -m coach.jog   (run from laptop/)

Use it before calibrating, to find the pan/tilt range where the laser dot is inside the camera picture.
A window shows the camera (turned by CAM_ROTATE) with the current angles. Steer the dot to each edge of the
picture and press m to mark it. When you quit, it prints the calibrate command for the marked range.

Keys (click the window first):
    a / d   pan  -2 / +2 deg        A / D   pan  -10 / +10
    s / w   tilt -2 / +2 deg        S / W   tilt -10 / +10
    l       laser on / off          c       centre (pan 90, tilt 60)
    m       mark this spot          q       quit (laser off)

Uses CAM_URL / CTL_HOST / CAM_ROTATE from laptop/.env, or --cam / --ctl.
Angles are limited by PAN_MIN/PAN_MAX/TILT_MIN/TILT_MAX in laptop/.env (default 0-180). Keep faces out of the beam.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field

def _limits():
    from .config import CONFIG
    return (CONFIG.pan_min, CONFIG.pan_max), (CONFIG.tilt_min, CONFIG.tilt_max)


PAN_LIM, TILT_LIM = _limits()     # PAN_* / TILT_* from laptop/.env, default 0-180 (the firmware's full range)
KEYS = {"a": (-2, 0), "d": (2, 0), "A": (-10, 0), "D": (10, 0),
        "s": (0, -2), "w": (0, 2), "S": (0, -10), "W": (0, 10)}


@dataclass
class Jog:
    pan: float = 90.0
    tilt: float = 60.0
    laser: bool = True
    marks: list[tuple[float, float]] = field(default_factory=list)

    def key(self, ch: str) -> bool:
        """Apply one key. Returns False when it's time to quit."""
        if ch in KEYS:
            dp, dt = KEYS[ch]
            self.pan = min(max(self.pan + dp, PAN_LIM[0]), PAN_LIM[1])
            self.tilt = min(max(self.tilt + dt, TILT_LIM[0]), TILT_LIM[1])
        elif ch == "l":
            self.laser = not self.laser
        elif ch == "c":
            self.pan, self.tilt = 90.0, 60.0
        elif ch == "m":
            self.marks.append((self.pan, self.tilt))
        elif ch == "q":
            return False
        return True

    def suggestion(self, margin: float = 3.0) -> str | None:
        """calibrate command for the marked range, a few degrees inside the edges."""
        if len(self.marks) < 2:
            return None
        ps, ts = [p for p, _ in self.marks], [t for _, t in self.marks]
        p0, p1, t0, t1 = min(ps) + margin, max(ps) - margin, min(ts) + margin, max(ts) - margin
        if p1 - p0 < 4 or t1 - t0 < 4:
            return None
        return (f"python3 -m coach.calibrate --pan-min {p0:.0f} --pan-max {p1:.0f} "
                f"--tilt-min {t0:.0f} --tilt-max {t1:.0f}")


async def _main(args) -> None:
    import cv2

    from .config import CONFIG
    from .control import ControlLink
    from .frames import FrameGrabber

    cam = args.cam or CONFIG.cam_url
    frames = FrameGrabber(int(cam) if str(cam).isdigit() else cam).start()
    link = await ControlLink(args.ctl or CONFIG.ctl_host, CONFIG.ctl_port).start()
    jog = Jog()
    win = "jog (a/d pan, w/s tilt, l laser, m mark, q quit)"
    cv2.namedWindow(win)
    print(__doc__.split("Keys")[1].split("Uses")[0])
    try:
        running = True
        while running:
            link.pan, link.tilt, link.laser = jog.pan, jog.tilt, jog.laser
            frame, _ = frames.latest()
            if frame is not None:
                img = frame.copy()
                msg = (f"pan {jog.pan:.0f}  tilt {jog.tilt:.0f}  laser {'ON' if jog.laser else 'off'}  "
                       f"S3 {'online' if link.online else 'OFFLINE'}  marks {len(jog.marks)}")
                cv2.putText(img, msg, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
                cv2.imshow(win, img)
            else:
                blank = __import__("numpy").zeros((240, 640, 3), "uint8")
                cv2.putText(blank, "no camera frames yet: check CAM_URL", (8, 40), cv2.FONT_HERSHEY_SIMPLEX,
                            0.6, (0, 0, 255), 2)
                cv2.imshow(win, blank)
            k = cv2.waitKey(1) & 0xFF
            if k != 255:
                before = len(jog.marks)
                running = jog.key(chr(k))
                if len(jog.marks) > before:
                    print(f"mark {len(jog.marks)}: pan {jog.pan:.0f} tilt {jog.tilt:.0f}")
            await asyncio.sleep(0.02)
    finally:
        link.laser = False
        await asyncio.sleep(0.15)       # a couple more packets with the laser off
        link.close()
        frames.stop()
        cv2.destroyAllWindows()
    if jog.marks:
        print("marked:", ", ".join(f"({p:.0f}, {t:.0f})" for p, t in jog.marks))
    cmd = jog.suggestion()
    print(f"\ncalibrate inside the marked range:\n  {cmd}" if cmd else
          "\nmark at least the left/right and top/bottom edges of the picture to get a calibrate command")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cam", default=None, help="camera URL (default CAM_URL from laptop/.env)")
    ap.add_argument("--ctl", default=None, help="S3 IP or host (default CTL_HOST from laptop/.env)")
    asyncio.run(_main(ap.parse_args(argv)))


if __name__ == "__main__":
    main()
