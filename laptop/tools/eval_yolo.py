"""Score the YOLO-World detector on labelled frames: per-object hit rate, false positives, mix-ups, and
suggested per-class thresholds (vision.CLASS_CONF). No ingredients needed: label whatever you have.

    cd laptop && python -m tools.eval_yolo ../tools/test_frames/labels.json
    python -m tools.eval_yolo ../tools/test_frames/labels.json --width 320    # as the ESP cam sees it (QVGA)
    python -m tools.eval_yolo my_clips/labels.json --sweep                   # threshold table per class

labels.json (paths relative to it):
    {"frames": {
        "counter.jpg":      {"present": ["bowl", "knife", "hand"], "ignore": ["fork"]},
        "bowl_knife.mp4":   {"present": ["bowl", "knife"], "every": 5}
    }}
  present: objects visible in the frame. Any other class detected = false positive.
  ignore:  ambiguous ones (half out of frame, mashed, a spoon): neither a hit nor a false positive.
  Videos get one label set for the whole clip (sampled every `every` frames), so record clips where the
  objects in view don't change. `python -m tools.try_yolo <cam url> --save clip.mp4` records one.

Per-frame scores, no 2-of-3 smoothing (the live app smooths, which only helps). Target:
avocado / lime / bowl found in >= 80% of the frames they're in.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import defaultdict
from pathlib import Path

import cv2

from coach.config import CONFIG
from coach.contracts import Recipe
from coach.vision import DEFAULT_CONF, Detector

RAW_FLOOR = 0.03          # collect every box above this, then apply thresholds offline
SWEEP = [0.05, 0.08, 0.1, 0.12, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5]
TARGET_HIT = 0.8


def load_samples(labels_path: Path, width: int | None, only: list[str] | None = None):
    """Yield (name, frame, present set, ignore set). `only`: scene names (avo_whole -> avo_whole_NNN.jpg)."""
    spec = json.loads(labels_path.read_text())["frames"]
    for rel, lab in spec.items():
        if only and not any(re.fullmatch(rf"{re.escape(s)}(_\d{{3}})?\.\w+", rel) for s in only):
            continue
        path = labels_path.parent / rel
        present, ignore = set(lab.get("present", [])), set(lab.get("ignore", []))
        if path.suffix.lower() in (".mp4", ".mov", ".avi", ".mkv"):
            cap, i, every = cv2.VideoCapture(str(path)), 0, int(lab.get("every", 5))
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if i % every == 0:
                    yield f"{rel}#{i}", resize(frame, width), present, ignore
                i += 1
            cap.release()
        else:
            frame = cv2.imread(str(path))
            if frame is None:
                raise SystemExit(f"can't read {path}")
            yield rel, resize(frame, width), present, ignore


def resize(frame, width):
    if not width or frame.shape[1] == width:
        return frame
    h = round(frame.shape[0] * width / frame.shape[1])
    return cv2.resize(frame, (width, h), interpolation=cv2.INTER_AREA)


def score(rows, classes, thr):
    """rows: [(name, maxconf{cls: conf}, present, ignore)] -> per-class stats at thresholds thr{cls: t}."""
    out = {}
    for c in classes:
        pos = [r for r in rows if c in r[2]]
        neg = [r for r in rows if c not in r[2] and c not in r[3]]
        hits = sum(r[1].get(c, 0) >= thr[c] for r in pos)
        fps = [r[0] for r in neg if r[1].get(c, 0) >= thr[c]]
        out[c] = {"pos": len(pos), "hit": hits, "neg": len(neg), "fp": len(fps), "fp_frames": fps}
    return out


def pct(a, b):
    return f"{100 * a / b:5.0f}%" if b else "    -"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("labels", type=Path)
    ap.add_argument("--width", type=int, help="resize frames to this width first (320 = ESP cam QVGA)")
    ap.add_argument("--imgsz", type=int, default=CONFIG.yolo_imgsz)
    ap.add_argument("--device", default=CONFIG.yolo_device)
    ap.add_argument("--sweep", action="store_true", help="hit / false-positive rate per threshold, per class")
    ap.add_argument("--fp-max", type=float, default=0.05, help="false-positive rate allowed when suggesting")
    ap.add_argument("--json", type=Path, help="also write the raw per-frame scores here")
    ap.add_argument("--only", nargs="*", help="score only these scenes (e.g. avo_whole lime_whole)")
    ap.add_argument("--weights", default=CONFIG.yolo_weights, help="e.g. yolov8m-worldv2.pt (downloads once)")
    ap.add_argument("--prompts", type=Path, help='JSON {"avocado": ["avocado", "halved avocado"], ...}')
    a = ap.parse_args()

    recipe = Recipe.load(Path(__file__).resolve().parent.parent / CONFIG.recipe_path)
    prompts = json.loads(a.prompts.read_text()) if a.prompts else None
    det = Detector(a.weights, a.device, recipe.objects, imgsz=a.imgsz, prompts=prompts)
    classes = det.classes
    rows, mixups, ms = [], defaultdict(lambda: defaultdict(int)), []
    for name, frame, present, ignore in load_samples(a.labels, a.width, a.only):
        unknown = (present | ignore) - set(classes)
        if unknown:
            raise SystemExit(f"{name}: not YOLO classes: {sorted(unknown)} (classes: {classes})")
        det.detect_raw(frame, RAW_FLOOR) if not ms else None      # warm-up on the first frame
        t0 = time.perf_counter()
        raw = det.detect_raw(frame, RAW_FLOOR)
        ms.append(1000 * (time.perf_counter() - t0))
        best: dict[str, float] = {}
        for d in raw:
            best[d.name] = max(best.get(d.name, 0.0), d.conf)
        rows.append((name, best, present, ignore))
        # mix-ups: a present object missed while an absent one fired in the same frame
        seen = {c for c, v in best.items() if v >= det.threshold(c)}
        for miss in present - seen:
            for wrong in seen - present - ignore:
                mixups[miss][wrong] += 1

    shape = f"{frame.shape[1]}x{frame.shape[0]}"
    print(f"\n{a.weights}{' + prompts' if prompts else ''} | {len(rows)} frames at {shape} | imgsz {a.imgsz} on {a.device} | "
          f"detector {sum(ms) / len(ms):.0f} ms/frame (~{1000 * len(ms) / sum(ms):.0f} fps)\n")

    cur = {c: det.threshold(c) for c in classes}
    res = score(rows, classes, cur)
    print(f"{'object':14} {'thr':>5} {'found':>12} {'false pos':>12}   false-positive frames")
    for c in classes:
        r = res[c]
        if not r["pos"] and not r["fp"]:
            continue
        print(f"{c:14} {cur[c]:5.2f} {pct(r['hit'], r['pos'])} ({r['hit']}/{r['pos']:<2}) "
              f"{pct(r['fp'], r['neg'])} ({r['fp']}/{r['neg']:<2})   {', '.join(r['fp_frames'][:4])}")

    if mixups:
        print("\nmix-ups (present but missed -> what fired instead, frames):")
        for miss, wrongs in mixups.items():
            print(f"  {miss:14} -> " + ", ".join(f"{w} x{n}" for w, n in sorted(wrongs.items(), key=lambda x: -x[1])))

    # suggestion: per class, the lowest threshold whose false-positive rate is <= fp_max (max hits under it)
    print(f"\nsuggested thresholds (lowest with false positives <= {a.fp_max:.0%}; current default {DEFAULT_CONF}):")
    for c in classes:
        pos = [r for r in rows if c in r[2]]
        neg = [r for r in rows if c not in r[2] and c not in r[3]]
        if not pos and not neg:
            continue
        ok = [t for t in SWEEP if not neg or sum(r[1].get(c, 0) >= t for r in neg) / len(neg) <= a.fp_max]
        if not ok:
            print(f"  {c:14} none <= {SWEEP[-1]} keeps false positives down")
            continue
        t = ok[0]
        hit = sum(r[1].get(c, 0) >= t for r in pos)
        flag = "" if not pos or hit / len(pos) >= TARGET_HIT else f"   < {TARGET_HIT:.0%} found"
        note = " (no negatives: can't check false positives)" if not neg else ""
        print(f"  {c:14} {t:4.2f}  found {pct(hit, len(pos))}{flag}{note}")

    if a.sweep:
        print("\nsweep: found% / false-pos% per threshold")
        print(f"{'object':14} " + " ".join(f"{t:>9.2f}" for t in SWEEP))
        for c in classes:
            r = [score(rows, [c], {c: t})[c] for t in SWEEP]
            if not r[0]["pos"] and not r[0]["neg"]:
                continue
            cells = [f"{pct(x['hit'], x['pos']).strip():>4}/{pct(x['fp'], x['neg']).strip():<4}" for x in r]
            print(f"{c:14} " + " ".join(f"{x:>9}" for x in cells))

    if a.json:
        a.json.write_text(json.dumps([{"frame": n, "best": b, "present": sorted(p), "ignore": sorted(i)}
                                      for n, b, p, i in rows], indent=1))


if __name__ == "__main__":
    main()
