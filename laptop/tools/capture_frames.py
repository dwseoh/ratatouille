"""Record a labelled scene from the camera for tools/eval_yolo.py. No ingredients needed: label what's there.

    cd laptop && python -m tools.capture_frames bowl_board --present bowl "cutting board"
    python -m tools.capture_frames bare_counter --nothing               # nothing: every detection = false alarm
    python -m tools.capture_frames knife_hand --present knife hand --ignore "cutting board" --seconds 15
    python -m tools.capture_frames green_things --cam 0                  # laptop webcam instead of the ESP cam

Saves one frame every --every seconds (default 0.5, so frames aren't near-copies) for --seconds (default 10),
at the camera's real resolution, into ../tools/eval_frames/<scene>_NNN.jpg, and adds them to
../tools/eval_frames/labels.json. Then:

    python -m tools.eval_yolo ../tools/eval_frames/labels.json

Rules for good labels: keep the same objects in view for the whole scene (move them around, change the
angle, put a hand in, but don't add or remove any); "present" = clearly visible; "ignore" = half out of
frame or ambiguous. Shoot from chest height looking down at the counter, like the demo.
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import time
from urllib.parse import urlsplit, urlunsplit
from pathlib import Path

import cv2

from coach.config import CONFIG
from coach.contracts import Recipe
from coach.frames import FrameGrabber
from coach.vision import HAND

OUT = Path(__file__).resolve().parents[2] / "tools" / "eval_frames"


def ipv4_url(url):
    """http://coach-cam.local:81/stream -> http://172.20.10.8:81/stream. macOS takes ~5 s to resolve a .local
    name (it waits for an IPv6 answer the ESP32 never sends), longer than OpenCV waits to open the stream.
    Asking for IPv4 only is instant. Webcam indexes and plain IPs pass through unchanged."""
    if not isinstance(url, str) or "://" not in url:
        return url
    u = urlsplit(url)
    if not u.hostname or not u.hostname.endswith(".local"):
        return url
    ip = socket.getaddrinfo(u.hostname, u.port or 80, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    return urlunsplit(u._replace(netloc=u.netloc.replace(u.hostname, ip)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scene", help="short name, used for the file names (e.g. bowl_board)")
    ap.add_argument("--present", nargs="*", default=[], help="objects clearly in view the whole time")
    ap.add_argument("--ignore", nargs="*", default=[], help="ambiguous objects: neither hit nor false alarm")
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--every", type=float, default=0.5)
    ap.add_argument("--cam", default=CONFIG.cam_url, help="stream URL, or 0 for the laptop webcam")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--countdown", type=float, default=3, help="seconds to get in position first")
    ap.add_argument("--nothing", action="store_true", help="scene with no objects on purpose (false-alarm test)")
    ap.add_argument("--replace", action="store_true", help="re-record: delete this scene's old frames + labels")
    a = ap.parse_args()

    recipe = Recipe.load(Path(__file__).resolve().parent.parent / CONFIG.recipe_path)
    known = set(recipe.objects) | {HAND}
    if not a.present and not a.nothing:
        raise SystemExit("no --present objects given. List what's in view, e.g.\n"
                         f'  python -m tools.capture_frames {a.scene} --present avocado "cutting board"\n'
                         "or add --nothing if the scene is meant to be empty.")
    bad = (set(a.present) | set(a.ignore)) - known
    if bad:
        raise SystemExit(f"unknown objects {sorted(bad)}. Use: {', '.join(sorted(known))}")

    a.out.mkdir(parents=True, exist_ok=True)
    labels_path = a.out / "labels.json"
    labels = json.loads(labels_path.read_text()) if labels_path.exists() else {
        "_doc": "Made by laptop/tools/capture_frames.py. See tools/eval_yolo.py for the format.", "frames": {}}
    mine = re.compile(rf"{re.escape(a.scene)}_\d{{3}}\.jpg")       # exact scene: avo_whole_000.jpg, not avo_whole_2_000
    old = [k for k in labels["frames"] if mine.fullmatch(k)]
    if old and not a.replace:
        raise SystemExit(f"scene {a.scene!r} already exists ({len(old)} frames). Re-record it with --replace, "
                         f"or pick another name")

    cam = int(a.cam) if str(a.cam).isdigit() else ipv4_url(a.cam)
    grabber = FrameGrabber(cam).start()
    try:
        t_wait = time.monotonic() + 10
        while grabber.latest()[0] is None:
            if time.monotonic() > t_wait:
                raise SystemExit(f"no frames from {a.cam} (camera on? browser tab closed? one client at a time)")
            time.sleep(0.05)
        if old:                                   # camera works: now it's safe to drop the old take
            for k in old:
                (a.out / k).unlink(missing_ok=True)
                del labels["frames"][k]
            labels_path.write_text(json.dumps(labels, indent=1) + "\n")
            print(f"replaced: deleted the old {len(old)} frames of {a.scene}")
        print(f"scene {a.scene}: present={a.present or 'nothing'} ignore={a.ignore or '-'}")
        for i in range(int(a.countdown), 0, -1):
            print(f"  starting in {i}...", flush=True)
            time.sleep(1)
        saved, last_t, t_end = [], None, time.monotonic() + a.seconds
        while time.monotonic() < t_end:
            frame, t = grabber.latest()
            if frame is not None and t != last_t:
                last_t = t
                name = f"{a.scene}_{len(saved):03d}.jpg"
                cv2.imwrite(str(a.out / name), frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
                saved.append(name)
                print(f"  {name}  {frame.shape[1]}x{frame.shape[0]}", flush=True)
            time.sleep(a.every)
    finally:
        grabber.stop()

    for name in saved:
        labels["frames"][name] = {"present": a.present, "ignore": a.ignore}
    labels_path.write_text(json.dumps(labels, indent=1) + "\n")
    print(f"\n{len(saved)} frames -> {a.out}\nscore: python -m tools.eval_yolo {labels_path}")


if __name__ == "__main__":
    main()
