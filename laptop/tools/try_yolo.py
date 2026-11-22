"""Run the YOLO detector on a webcam, video or the ESP cam stream: boxes, fps, and live scores. Prints per-object
hit rates on exit.

    cd laptop && python -m tools.try_yolo http://172.20.10.8:81/stream          # the ESP cam
    python -m tools.try_yolo 0                                                   # laptop webcam
    python -m tools.try_yolo counter.mp4                                         # recorded test video
    python -m tools.try_yolo <src> --save counter.mp4                            # record while testing
    python -m tools.try_yolo <src> --weights yolov8l-worldv2.pt --prompts tools/yolo_prompts.json

Setup tuning (lens, light, distance, angle): the bars at the bottom show each watched object's BEST score in
the frame, even below its threshold (the tick). Change one thing, watch whether the bar grows, keep what helps.
--watch picks the objects (default: avocado lime tomato). The scores are also printed once a second.

Done when (task-specs P3): >= 10 fps, and avocado / lime / bowl visible in >= 80% of frames.
Keys: q = quit.
"""
import argparse
import json
import time
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

from coach.config import CONFIG
from coach.contracts import Recipe
from coach.vision import Detector, Smoother
from tools.capture_frames import ipv4_url

SHOW_W = 640            # display width (the ESP cam's 320x240 is tiny on screen)


def draw(frame, dets, fps, scale=1.0):
    for d in dets:
        x1, y1, x2, y2 = (int(v * scale) for v in d.box)
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 220, 0), 2)
        cv2.putText(frame, f"{d.name} {d.conf:.2f}", (x1, max(12, y1 - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 0), 1)
    cv2.putText(frame, f"{fps:.1f} fps", (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)


def score_panel(width, watch, best, det):
    """One bar per watched object: best raw score (0..0.6 full width) and its threshold tick."""
    row, full = 26, 0.6
    panel = np.full((row * len(watch) + 6, width, 3), 30, np.uint8)
    x0, bw = 120, width - 180
    for i, name in enumerate(watch):
        y = 6 + i * row
        s, thr = best.get(name, 0.0), det.threshold(name)
        color = (0, 200, 0) if s >= thr else (0, 140, 255)
        cv2.putText(panel, name, (8, y + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (230, 230, 230), 1)
        cv2.rectangle(panel, (x0, y + 4), (x0 + bw, y + 18), (70, 70, 70), -1)
        cv2.rectangle(panel, (x0, y + 4), (x0 + int(bw * min(s, full) / full), y + 18), color, -1)
        tx = x0 + int(bw * thr / full)
        cv2.line(panel, (tx, y + 1), (tx, y + 21), (255, 255, 255), 2)
        cv2.putText(panel, f"{s:.2f}", (x0 + bw + 8, y + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (230, 230, 230), 1)
    return panel


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("source", help="webcam index (0), video path, or stream URL")
    p.add_argument("--imgsz", type=int, default=CONFIG.yolo_imgsz)
    p.add_argument("--device", default=CONFIG.yolo_device)
    p.add_argument("--weights", default=CONFIG.yolo_weights)
    p.add_argument("--prompts", type=Path, help='JSON {"avocado": ["avocado", "halved avocado"], ...}')
    p.add_argument("--watch", nargs="*", default=["avocado", "lime", "tomato"], help="objects to show score bars for")
    p.add_argument("--save", help="record the raw camera video to this .mp4")
    a = p.parse_args()

    recipe = Recipe.load(Path(__file__).resolve().parent.parent / CONFIG.recipe_path)
    prompts = json.loads(a.prompts.read_text()) if a.prompts else None
    det = Detector(a.weights, a.device, recipe.objects, imgsz=a.imgsz, prompts=prompts)
    smooth = Smoother()
    cap = cv2.VideoCapture(int(a.source) if a.source.isdigit() else ipv4_url(a.source))
    writer, frames, hits, fps, last_print = None, 0, Counter(), 0.0, 0.0
    print(f"{a.weights}{' + prompts' if prompts else ''} | classes: {det.classes}\n"
          f"imgsz {a.imgsz} on {a.device}. q to quit.")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if a.save and writer is None:
                h, w = frame.shape[:2]
                writer = cv2.VideoWriter(a.save, cv2.VideoWriter_fourcc(*"mp4v"), 15, (w, h))
            if writer:
                writer.write(frame)
            t0 = time.perf_counter()
            raw = det.detect_raw(frame, 0.01)
            dt = time.perf_counter() - t0
            dets = smooth.update([d for d in raw if d.conf >= det.threshold(d.name)])
            best: dict[str, float] = {}
            for d in raw:
                best[d.name] = max(best.get(d.name, 0.0), d.conf)
            fps = 1 / dt if not fps else 0.9 * fps + 0.1 / dt
            frames += 1
            hits.update({d.name for d in dets})

            scale = SHOW_W / frame.shape[1]
            view = cv2.resize(frame, (SHOW_W, round(frame.shape[0] * scale)))
            draw(view, dets, fps, scale)
            cv2.imshow("try_yolo", np.vstack([view, score_panel(SHOW_W, a.watch, best, det)]))
            if time.monotonic() - last_print >= 1.0:
                last_print = time.monotonic()
                print("  " + "  ".join(f"{n} {best.get(n, 0.0):.2f}" for n in a.watch), flush=True)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        cap.release()
        if writer:
            writer.release()
        cv2.destroyAllWindows()
    print(f"\n{frames} frames, ~{fps:.1f} fps (detector only)")
    for name in det.classes:
        print(f"  {name:14s} {100 * hits[name] / max(frames, 1):5.1f}% of frames")


if __name__ == "__main__":
    main()
