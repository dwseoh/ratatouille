"""Run the YOLO detector on a webcam or video and show boxes + fps. Prints per-object hit rates on exit.

    cd laptop && python -m tools.try_yolo 0                   # webcam
    cd laptop && python -m tools.try_yolo counter.mp4         # recorded test video
    cd laptop && python -m tools.try_yolo 0 --save counter.mp4   # record while testing (re-test offline later)
    cd laptop && python -m tools.try_yolo 0 --imgsz 480

Done when (task-specs P3): >= 10 fps, and avocado / lime / bowl visible in >= 80% of frames.
Keys: q = quit.
"""
import argparse
import time
from collections import Counter
from pathlib import Path

import cv2

from coach.config import CONFIG
from coach.contracts import Recipe
from coach.vision import Detector, Smoother


def draw(frame, dets, fps):
    for d in dets:
        x1, y1, x2, y2 = map(int, d.box)
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 220, 0), 2)
        cv2.putText(frame, f"{d.name} {d.conf:.2f}", (x1, max(12, y1 - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 0), 1)
    cv2.putText(frame, f"{fps:.1f} fps", (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("source", help="webcam index (0) or video path")
    p.add_argument("--imgsz", type=int, default=CONFIG.yolo_imgsz)
    p.add_argument("--device", default=CONFIG.yolo_device)
    p.add_argument("--save", help="record the raw camera video to this .mp4")
    a = p.parse_args()

    recipe = Recipe.load(Path(__file__).resolve().parent.parent / CONFIG.recipe_path)
    det = Detector(CONFIG.yolo_weights, a.device, recipe.objects, imgsz=a.imgsz)
    smooth = Smoother()
    cap = cv2.VideoCapture(int(a.source) if a.source.isdigit() else a.source)
    writer, frames, hits, fps = None, 0, Counter(), 0.0
    print(f"classes: {det.classes}\nimgsz {a.imgsz} on {a.device}. q to quit.")
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
            dets = smooth.update(det.detect(frame))
            dt = time.perf_counter() - t0
            fps = 1 / dt if not fps else 0.9 * fps + 0.1 / dt
            frames += 1
            hits.update({d.name for d in dets})
            draw(frame, dets, fps)
            cv2.imshow("try_yolo", frame)
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
