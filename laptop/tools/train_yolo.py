"""Fine-tune YOLO-World on our own labelled frames (tools/label_boxes.py --export).

    cd laptop && python -m tools.train_yolo                         # yolov8s-worldv2 -> yolo-world-guac.pt
    python -m tools.train_yolo --weights yolov8m-worldv2.pt --epochs 80
    python -m tools.train_yolo --extra datasets/lvis --out yolo-world-guac-web.pt   # + LVIS web photos
    python -m tools.eval_yolo ../tools/eval_frames/labels.json --only <the _b scenes> --weights yolo-world-guac.pt

Why: the pretrained models (YOLO-World s/m/l, YOLO26-Objects365) found ~0% of the avocados and limes in the
ESP cam's 320x240 frames. A model fine-tuned on this camera, light and food is the standard fix.
The class list is the recipe objects + "hand" (same order as vision.Detector), so the app loads the result with
YOLO_WEIGHTS=yolo-world-guac.pt in laptop/.env and nothing else changes. Don't pass --prompts to the
fine-tuned model: it was trained on the plain class names.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from coach.config import CONFIG

LAPTOP = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, default=LAPTOP / "datasets" / "guac" / "data.yaml")
    ap.add_argument("--weights", default="yolov8s-worldv2.pt", help="pretrained YOLO-World to start from")
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--imgsz", type=int, default=320, help="the ESP cam streams 320x240")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--device", default=CONFIG.yolo_device)
    ap.add_argument("--out", default="yolo-world-guac.pt")
    ap.add_argument("--extra", nargs="*", type=Path, default=[],
                    help="more YOLO datasets to TRAIN on (e.g. datasets/lvis from tools/fetch_lvis.py). "
                         "Validation (which picks the best epoch) stays on our own camera frames only.")
    ap.add_argument("--repeat-own", type=int, default=4,
                    help="with --extra: list our own training frames this many times so web photos don't drown them")
    a = ap.parse_args()
    if not a.data.exists():
        raise SystemExit(f"{a.data} not found: run  python -m tools.label_boxes --export  first")
    if a.extra:
        base = a.data.parent
        train = [str(base / "images" / "train")] * max(1, a.repeat_own)
        for e in a.extra:
            e = e if e.is_absolute() else LAPTOP / e
            if not (e / "images" / "train").exists():
                raise SystemExit(f"{e}/images/train not found (run python -m tools.fetch_lvis first)")
            train.append(str(e / "images" / "train"))
        names = a.data.read_text().split("names:", 1)[1]
        combined = base.parent / f"{base.name}_plus.yaml"
        combined.write_text("train:\n" + "".join(f"  - {t}\n" for t in train)
                            + f"val: {base / 'images' / 'val'}\nnames:{names}")
        print(f"training on {len(train)} datasets, validating on {base.name} only -> {combined}")
        a.data = combined

    from ultralytics import YOLOWorld   # heavy import
    model = YOLOWorld(str(LAPTOP / a.weights) if (LAPTOP / a.weights).exists() else a.weights)
    model.train(data=str(a.data), epochs=a.epochs, imgsz=a.imgsz, batch=a.batch, device=a.device,
                project=str(LAPTOP / "runs"), name=Path(a.out).stem, exist_ok=True, plots=True,
                patience=20,           # stop early if val stops improving
                fliplr=0.5, hsv_h=0.01, hsv_s=0.4, hsv_v=0.3, degrees=10, scale=0.3,   # light augmentation
                mosaic=1.0, close_mosaic=10, verbose=False)
    best = Path(model.trainer.best)
    shutil.copy(best, LAPTOP / a.out)
    print(f"\nbest weights: {best}\ncopied to   : {LAPTOP / a.out}\n"
          f"try it live : python -m tools.try_yolo http://172.20.10.8:81/stream --weights {a.out}\n"
          f"use in app  : YOLO_WEIGHTS={a.out} in laptop/.env")


if __name__ == "__main__":
    main()
