"""Draw boxes on recorded frames (tools/capture_frames.py) to fine-tune YOLO-World, then export a YOLO dataset.

    cd laptop && python -m tools.label_boxes                    # label the training scenes (names ending in _a)
    python -m tools.label_boxes --scenes avo_a lime_cut_a       # only these scenes
    python -m tools.label_boxes --export                        # write the dataset for tools/train_yolo.py

Only the objects listed as "present" when the scene was recorded are asked for. Boxes from the previous frame
of the same scene are carried over, so if nothing moved just press Enter. Progress is saved after every frame
to boxes.json (next to labels.json): quit any time and run it again to continue.

Mouse:  drag = new box for the current object     right-click inside a box = delete it
Keys:   Enter / n = accept, next frame            b = back one frame
        Tab = next object   1-9 = pick object     u = undo last box     c = clear all boxes
        s = skip frame (not used for training)    q = save and quit

Every listed object should have a box on every frame (two avocado halves = two avocado boxes). A frame where a
"present" object has no box is flagged in yellow; accept it anyway only if the object really isn't visible.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
from pathlib import Path

import cv2
import numpy as np

from coach.config import CONFIG
from coach.contracts import Recipe
from coach.vision import HAND

FRAMES = Path(__file__).resolve().parents[2] / "tools" / "eval_frames"
DATASET = Path(__file__).resolve().parents[1] / "datasets" / "guac"
SCALE = 3                                    # 320x240 frames shown at 960x720
COLORS = [(60, 200, 60), (0, 230, 200), (60, 60, 230), (230, 160, 0), (200, 0, 200), (0, 140, 255),
          (255, 255, 0), (120, 120, 255), (255, 120, 120), (180, 180, 180), (0, 255, 0)]


def scene_of(name: str) -> str:
    return re.sub(r"_\d{3}\.\w+$", "", name)


def class_list() -> list[str]:
    recipe = Recipe.load(Path(__file__).resolve().parent.parent / CONFIG.recipe_path)
    return list(dict.fromkeys([*recipe.objects, HAND]))     # same order as vision.Detector


class Labeller:
    def __init__(self, frames_dir: Path, scenes: list[str] | None):
        self.dir = frames_dir
        self.labels = json.loads((frames_dir / "labels.json").read_text())["frames"]
        self.box_path = frames_dir / "boxes.json"
        self.boxes: dict = json.loads(self.box_path.read_text()) if self.box_path.exists() else {}
        want = (lambda s: s in scenes) if scenes else (lambda s: s.endswith("_a"))
        self.todo = [n for n in self.labels if want(scene_of(n)) and n.lower().endswith(".jpg")]
        if not self.todo:
            raise SystemExit("no frames to label (record scenes ending in _a, or pass --scenes)")
        self.classes = class_list()
        self.cur: list[dict] = []           # boxes on the frame being edited: {"cls": name, "box": [x1,y1,x2,y2]}
        self.obj = 0
        self.drag = None

    def save(self):
        self.box_path.write_text(json.dumps(self.boxes, indent=1) + "\n")

    def present(self, name):
        return self.labels[name].get("present", [])

    def start_frame(self, i):
        name = self.todo[i]
        if name in self.boxes:
            self.cur = [dict(b) for b in self.boxes[name].get("boxes", [])]
        else:                                # carry over from the previous frame of the same scene
            prev = next((self.todo[j] for j in range(i - 1, -1, -1)
                         if scene_of(self.todo[j]) == scene_of(name) and self.todo[j] in self.boxes), None)
            keep = set(self.present(name))
            self.cur = [dict(b) for b in self.boxes.get(prev, {}).get("boxes", []) if b["cls"] in keep]
        self.obj = 0

    def on_mouse(self, ev, x, y, flags, _):
        x, y = x / SCALE, y / SCALE
        objs = self.present(self.todo[self.i])
        if ev == cv2.EVENT_LBUTTONDOWN and objs:
            self.drag = (x, y, x, y)
        elif ev == cv2.EVENT_MOUSEMOVE and self.drag:
            self.drag = (*self.drag[:2], x, y)
        elif ev == cv2.EVENT_LBUTTONUP and self.drag:
            x1, y1, x2, y2 = self.drag
            self.drag = None
            if abs(x2 - x1) > 3 and abs(y2 - y1) > 3:
                self.cur.append({"cls": objs[self.obj],
                                 "box": [round(min(x1, x2), 1), round(min(y1, y2), 1),
                                         round(max(x1, x2), 1), round(max(y1, y2), 1)]})
        elif ev == cv2.EVENT_RBUTTONDOWN:
            for k in range(len(self.cur) - 1, -1, -1):
                bx = self.cur[k]["box"]
                if bx[0] <= x <= bx[2] and bx[1] <= y <= bx[3]:
                    del self.cur[k]
                    break

    def render(self):
        name = self.todo[self.i]
        im = cv2.imread(str(self.dir / name))
        view = cv2.resize(im, (im.shape[1] * SCALE, im.shape[0] * SCALE), interpolation=cv2.INTER_NEAREST)
        objs = self.present(name)
        for b in self.cur:
            c = COLORS[self.classes.index(b["cls"]) % len(COLORS)]
            x1, y1, x2, y2 = (int(v * SCALE) for v in b["box"])
            cv2.rectangle(view, (x1, y1), (x2, y2), c, 2)
            cv2.putText(view, b["cls"], (x1 + 4, y1 + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4)
            cv2.putText(view, b["cls"], (x1 + 4, y1 + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.7, c, 2)
        if self.drag:
            x1, y1, x2, y2 = (int(v * SCALE) for v in self.drag)
            cv2.rectangle(view, (x1, y1), (x2, y2), (255, 255, 255), 1)
        missing = [o for o in objs if not any(b["cls"] == o for b in self.cur)]
        head = np.full((70, view.shape[1], 3), 30, np.uint8)
        done = sum(n in self.boxes for n in self.todo)
        cv2.putText(head, f"{name}   [{self.i + 1}/{len(self.todo)}, {done} done]", (10, 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (230, 230, 230), 1)
        if objs:
            tabs = "   ".join(f"{k + 1}:{o}" + ("*" if k == self.obj else "") for k, o in enumerate(objs))
            cv2.putText(head, f"draw: {objs[self.obj]}     ({tabs})", (10, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.65,
                        (0, 220, 255) if missing else (120, 230, 120), 2)
        else:
            cv2.putText(head, "nothing listed as present: just press Enter", (10, 55),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (120, 230, 120), 2)
        if missing:
            cv2.putText(head, "no box yet: " + ", ".join(missing), (view.shape[1] - 420, 25),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 220, 255), 2)
        return np.vstack([head, view])

    def run(self):
        self.i = next((k for k, n in enumerate(self.todo) if n not in self.boxes), 0)
        self.start_frame(self.i)
        cv2.namedWindow("label_boxes")
        cv2.setMouseCallback("label_boxes", self.on_mouse)
        while True:
            cv2.imshow("label_boxes", self.render())
            k = cv2.waitKey(20) & 0xFF
            if k == 255:
                continue
            objs = self.present(self.todo[self.i])
            if k in (13, 10, ord("n"), ord("s")):
                self.boxes[self.todo[self.i]] = {"boxes": self.cur, "skip": k == ord("s")}
                self.save()
                if self.i + 1 >= len(self.todo):
                    print("all frames done")
                    break
                self.i += 1
                self.start_frame(self.i)
            elif k == ord("b") and self.i > 0:
                self.boxes[self.todo[self.i]] = {"boxes": self.cur, "skip": False}
                self.i -= 1
                self.start_frame(self.i)
            elif k == 9 and objs:                                  # Tab
                self.obj = (self.obj + 1) % len(objs)
            elif ord("1") <= k <= ord("9") and k - ord("1") < len(objs):
                self.obj = k - ord("1")
            elif k == ord("u") and self.cur:
                self.cur.pop()
            elif k == ord("c"):
                self.cur = []
            elif k == ord("q"):
                break
        self.save()
        cv2.destroyAllWindows()
        print(f"saved {self.box_path} ({sum(n in self.boxes for n in self.todo)}/{len(self.todo)} frames)")


def export(frames_dir: Path, out: Path, val_frac: float, seed: int = 0):
    """YOLO dataset: images/{train,val}, labels/{train,val}, data.yaml. Val = a random slice of each scene."""
    boxes = json.loads((frames_dir / "boxes.json").read_text())
    classes = class_list()
    if out.exists():
        shutil.rmtree(out)
    by_scene: dict[str, list[str]] = {}
    for name, rec in boxes.items():
        if not rec.get("skip"):
            by_scene.setdefault(scene_of(name), []).append(name)
    rng = random.Random(seed)
    counts = {"train": 0, "val": 0}
    per_class = {c: 0 for c in classes}
    for scene, names in by_scene.items():
        names = sorted(names)
        val = set(rng.sample(names, max(1, round(len(names) * val_frac)))) if len(names) > 3 else set()
        for name in names:
            split = "val" if name in val else "train"
            im = cv2.imread(str(frames_dir / name))
            h, w = im.shape[:2]
            (out / "images" / split).mkdir(parents=True, exist_ok=True)
            (out / "labels" / split).mkdir(parents=True, exist_ok=True)
            shutil.copy(frames_dir / name, out / "images" / split / name)
            lines = []
            for b in boxes[name]["boxes"]:
                x1, y1, x2, y2 = b["box"]
                lines.append(f"{classes.index(b['cls'])} {(x1 + x2) / 2 / w:.6f} {(y1 + y2) / 2 / h:.6f} "
                             f"{(x2 - x1) / w:.6f} {(y2 - y1) / h:.6f}")
                per_class[b["cls"]] += 1
            (out / "labels" / split / (Path(name).stem + ".txt")).write_text("\n".join(lines) + ("\n" if lines else ""))
            counts[split] += 1
    names_yaml = "\n".join(f"  {i}: {c}" for i, c in enumerate(classes))
    (out / "data.yaml").write_text(f"path: {out}\ntrain: images/train\nval: images/val\nnames:\n{names_yaml}\n")
    print(f"dataset -> {out}: {counts['train']} train / {counts['val']} val images")
    print("boxes per object: " + ", ".join(f"{c} {n}" for c, n in per_class.items() if n))
    if not counts["val"]:
        print("WARNING: no val images (need > 3 frames per scene)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--frames", type=Path, default=FRAMES)
    ap.add_argument("--scenes", nargs="*", help="scenes to label (default: every scene ending in _a)")
    ap.add_argument("--export", action="store_true", help="write the YOLO dataset instead of labelling")
    ap.add_argument("--out", type=Path, default=DATASET)
    ap.add_argument("--val", type=float, default=0.15, help="fraction of each scene held out for training's val")
    a = ap.parse_args()
    if a.export:
        export(a.frames, a.out, a.val)
    else:
        Labeller(a.frames, a.scenes).run()


if __name__ == "__main__":
    main()
