"""
laser_tracker.py  -  host-side vision + control for the ESP32 laser pointer
--------------------------------------------------------------------------
pip install opencv-python numpy

Run:  python3 laser_tracker.py                 (finds coach-cam.local)
      python3 laser_tracker.py --ip 192.168.x.x  (if mDNS fails)

Controls (video window focused):
  left-click   set the target point (where the laser should go)
  right-click  clear target
  l            toggle laser on/off
  w/a/s/d      nudge tilt/pan by 2 deg (use for calibration)
  k            print current pan/tilt (write these down at image centre!)
  m            show/hide the red-detection mask (white = counted as laser)
  r            reset: clear target and re-centre servos
  q            quit

How it works
  * The camera is fixed to the harness and sees the counter.
  * Each frame we find (1) the TARGET pixel and (2) the LASER DOT pixel.
  * If the dot is visible -> closed-loop: nudge pan/tilt so dot -> target.
    Because both are measured in the SAME frame, body sway cancels out,
    which is why no IMU is needed.
  * If the dot is not visible -> open-loop jump using the pinhole model
    (pixel -> angle), then closed-loop takes over once the dot appears.
"""
import argparse
import math
import socket
import sys
import threading
import time
import urllib.request

import cv2
import numpy as np

# ---------------- calibration constants (tune these) ----------------
HFOV_DEG = 60.0         # OV2640 stock lens is roughly 60-66 deg horizontal
PAN_CENTER = 90.0       # servo angle that puts the dot at the image centre
TILT_CENTER = 90.0
PAN_SIGN = -1.0         # flip if the laser moves the wrong way
TILT_SIGN = 1.0
KP = 0.5                # fraction of the angular error corrected per frame
MAX_STEP_DEG = 3.0      # per-frame correction limit
DEADBAND_PX = 4
GATE_FRAC = 0.25        # only trust a red blob within 25% of image width of the prediction
MAX_TRIM_DEG = 12.0     # closed loop may only correct the model by this much (anti-windup)
PAN_LIM = (0.0, 180.0)
TILT_LIM = (30.0, 150.0)
# --------------------------------------------------------------------


class MJPEGStream:
    """Reads the ESP32 MJPEG stream directly (no FFmpeg) in a background thread.
    Splits JPEGs on the FFD8/FFD9 markers and always keeps only the newest frame,
    so a slow loop never builds up lag. Prints the real reason if it can't connect."""

    def __init__(self, url, timeout=5):
        self.url, self.timeout = url, timeout
        self.frame, self.frame_id = None, 0
        self.lock = threading.Lock()
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while self.running:
            try:
                print(f"connecting to {self.url} ...")
                with urllib.request.urlopen(self.url, timeout=self.timeout) as r:
                    print("connected, receiving frames")
                    buf = b""
                    while self.running:
                        chunk = r.read(4096)
                        if not chunk:
                            raise ConnectionError("ESP32 closed the stream")
                        buf += chunk
                        start = buf.find(b"\xff\xd8")
                        end = buf.find(b"\xff\xd9", start + 2)
                        if start != -1 and end != -1:
                            jpg, buf = buf[start:end + 2], buf[end + 2:]
                            img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                            if img is not None:
                                with self.lock:
                                    self.frame, self.frame_id = img, self.frame_id + 1
                        elif len(buf) > 1_000_000:      # garbage guard
                            buf = b""
            except Exception as e:
                print(f"stream error: {e!r} - retrying in 1 s")
                time.sleep(1)

    def read(self):
        with self.lock:
            return self.frame_id, self.frame

    def stop(self):
        self.running = False


def laser_mask(frame):
    f = frame.astype(np.int16)
    b, g, r = f[..., 0], f[..., 1], f[..., 2]
    redness = r - np.maximum(g, b)
    # Laser dots are very bright; their core often clips to white with a red halo.
    mask = ((redness > 60) & (r > 180)) | ((r > 250) & (g > 230) & (b > 230) & (redness > 5))
    mask = cv2.morphologyEx(mask.astype(np.uint8) * 255, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    return mask, redness, r


def find_laser_candidates(frame):
    """All small, bright, red blobs: [(x, y, score), ...]"""
    mask, redness, r = laser_mask(frame)
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask)
    out = []
    for i in range(1, n):
        if 2 <= stats[i, cv2.CC_STAT_AREA] <= 400:   # reject big red objects (tomatoes!)
            cx, cy = int(cents[i][0]), int(cents[i][1])
            out.append((cx, cy, float(redness[cy, cx]) + float(r[cy, cx])))
    return out


def find_laser_dot(frame, predicted=None, gate_px=None):
    """Pick the laser dot. With a prediction, only accept a blob near where the
    servos SHOULD be pointing -- a red lamp or LED elsewhere in view is ignored."""
    cands = find_laser_candidates(frame)
    if predicted is not None and gate_px:
        near = [c for c in cands if math.hypot(c[0] - predicted[0], c[1] - predicted[1]) <= gate_px]
        if not near:
            return None
        c = min(near, key=lambda c: math.hypot(c[0] - predicted[0], c[1] - predicted[1]))
        return (c[0], c[1])
    if not cands:
        return None
    c = max(cands, key=lambda c: c[2])
    return (c[0], c[1])


def focal_px(w):
    return (w / 2) / math.tan(math.radians(HFOV_DEG / 2))


def pixel_to_angle(u, v, w, h):
    """Pinhole model: pixel -> (pan, tilt) offset in degrees from the optical axis."""
    f = focal_px(w)
    return math.degrees(math.atan((u - w / 2) / f)), math.degrees(math.atan((v - h / 2) / f))


def angle_to_pixel(pan, tilt, w, h):
    """Inverse model: where the dot SHOULD appear for these servo angles."""
    f = focal_px(w)
    ap = clamp((pan - PAN_CENTER) / PAN_SIGN, -80, 80)
    at = clamp((tilt - TILT_CENTER) / TILT_SIGN, -80, 80)
    return (int(w / 2 + f * math.tan(math.radians(ap))), int(h / 2 + f * math.tan(math.radians(at))))


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="coach-cam.local",
                    help="ESP32 host name or IP (default: coach-cam.local via mDNS)")
    ap.add_argument("--port", type=int, default=4210)
    args = ap.parse_args()
    # Accept "coach-cam.local", "192.168.2.96", "http://coach-cam.local:81/stream" -> bare host
    args.ip = args.ip.split("://")[-1].split("/")[0].split(":")[0]
    # Resolve mDNS once: UDP sends then go straight to the IP (no lookup per frame)
    try:
        args.ip = socket.gethostbyname(args.ip)
    except socket.gaierror:
        sys.exit(f"Can't find {args.ip}. Same hotspot? Otherwise pass --ip <IP from Serial Monitor>.")
    print(f"Stream: http://{args.ip}:81/stream   UDP: {args.ip}:{args.port}")

    stream = MJPEGStream(f"http://{args.ip}:81/stream")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    last_id = -1

    state = {"target": None}
    pan, tilt, laser = PAN_CENTER, TILT_CENTER, False
    trim_p = trim_t = 0.0
    show_mask = False

    def on_mouse(evt, x, y, *_):
        if evt == cv2.EVENT_LBUTTONDOWN:
            state["target"] = (x, y)
            state["jump"] = True
        elif evt == cv2.EVENT_RBUTTONDOWN:
            state["target"] = None

    cv2.namedWindow("cook-assist")
    cv2.setMouseCallback("cook-assist", on_mouse)

    while True:
        fid, frame = stream.read()
        if frame is None or fid == last_id:          # no new frame yet
            if cv2.waitKey(5) & 0xFF == ord("q"):
                break
            continue
        last_id = fid
        h, w = frame.shape[:2]
        tgt = state["target"]
        predicted = angle_to_pixel(pan, tilt, w, h)
        dot = find_laser_dot(frame, predicted, GATE_FRAC * w) if laser else None

        if tgt is not None:
            if state.pop("jump", False):
                trim_p = trim_t = 0.0                  # new target: start from the model
            # open-loop base = where the model says to point
            dp, dt = pixel_to_angle(*tgt, w, h)
            base_p = PAN_CENTER + PAN_SIGN * dp
            base_t = TILT_CENTER + TILT_SIGN * dt
            if dot is not None:                        # closed loop trims the model
                ex, ey = tgt[0] - dot[0], tgt[1] - dot[1]
                if abs(ex) > DEADBAND_PX or abs(ey) > DEADBAND_PX:
                    cp, ct = pixel_to_angle(w / 2 + ex, h / 2 + ey, w, h)
                    trim_p += PAN_SIGN * clamp(KP * cp, -MAX_STEP_DEG, MAX_STEP_DEG)
                    trim_t += TILT_SIGN * clamp(KP * ct, -MAX_STEP_DEG, MAX_STEP_DEG)
                    trim_p = clamp(trim_p, -MAX_TRIM_DEG, MAX_TRIM_DEG)
                    trim_t = clamp(trim_t, -MAX_TRIM_DEG, MAX_TRIM_DEG)
            pan = clamp(base_p + trim_p, *PAN_LIM)
            tilt = clamp(base_t + trim_t, *TILT_LIM)
        else:
            pan, tilt = clamp(pan, *PAN_LIM), clamp(tilt, *TILT_LIM)

        sock.sendto(f"{pan:.1f},{tilt:.1f},{int(laser)}".encode(), (args.ip, args.port))

        # ---- overlay ----
        # yellow ring = where the model expects the dot (search gate)
        if laser:
            cv2.circle(frame, predicted, int(GATE_FRAC * w), (0, 255, 255), 1)
        if tgt:
            cv2.drawMarker(frame, tgt, (0, 255, 0), cv2.MARKER_CROSS, 20, 2)
        if dot:
            cv2.circle(frame, dot, 8, (0, 0, 255), 2)
        mode = "LOCKED" if (tgt and dot) else ("open-loop" if tgt else "manual")
        cv2.putText(frame, f"pan {pan:.1f} tilt {tilt:.1f} laser {'ON' if laser else 'off'} {mode}",
                    (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        cv2.imshow("cook-assist", frame)
        if show_mask:
            cv2.imshow("laser-mask", laser_mask(frame)[0])

        k = cv2.waitKey(1) & 0xFF
        if k == ord("q"):
            break
        elif k == ord("l"):
            laser = not laser
        elif k == ord("a"):
            pan -= 2
        elif k == ord("d"):
            pan += 2
        elif k == ord("w"):
            tilt -= 2
        elif k == ord("s"):
            tilt += 2
        elif k == ord("k"):
            print(f"pan={pan:.1f} tilt={tilt:.1f}")
        elif k == ord("m"):
            show_mask = not show_mask
            if not show_mask:
                cv2.destroyWindow("laser-mask")
        elif k == ord("r"):                      # reset: clear target, re-centre
            state["target"] = None
            pan, tilt, trim_p, trim_t = PAN_CENTER, TILT_CENTER, 0.0, 0.0

    sock.sendto(f"{pan:.1f},{tilt:.1f},0".encode(), (args.ip, args.port))  # laser off
    stream.stop()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
