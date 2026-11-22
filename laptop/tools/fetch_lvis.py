"""Download labelled web photos of our recipe objects from LVIS (no account needed) as a YOLO dataset, to train
together with our own camera frames (tools/label_boxes.py --export, then tools/train_yolo.py --extra).

    cd laptop && python -m tools.fetch_lvis                       # -> laptop/datasets/lvis (gitignored)
    python -m tools.fetch_lvis --max-per-class 150 --split val    # smaller / quicker

LVIS (lvisdataset.org): 1200+ categories boxed on COCO photos. Labels CC BY 4.0; the photos are COCO's Flickr
images (each under its own Flickr licence): fine for training, don't commit or redistribute them.
LVIS has avocado (whole, halves, slices), lime (whole, halves, wedges), tomato, chopping_board, knife, bowl,
fork, onion, saltshaker. No cilantro; "hand" isn't a category (our class just gets no web boxes).
LVIS is "federated": a photo is only checked for some categories. A photo is kept only if every one of our
classes is either boxed in it, or verified absent / not flagged as incompletely labelled, so the model isn't
taught that an unlabelled avocado is background.
Photos are downscaled to --max-side (default 640) to keep the download small.
"""
from __future__ import annotations

import argparse
import io
import json
import random
import urllib.request
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

from tools.label_boxes import class_list

LAPTOP = Path(__file__).resolve().parents[1]
URL = "https://dl.fbaipublicfiles.com/LVIS/lvis_v1_{split}.json.zip"
# our class -> LVIS category name(s)
LVIS_NAMES = {"avocado": ["avocado"], "lime": ["lime"], "tomato": ["tomato"], "onion": ["onion"],
              "knife": ["knife"], "bowl": ["bowl"], "cutting board": ["chopping_board"], "fork": ["fork"],
              "salt": ["saltshaker"]}
PRIORITY = ["avocado", "lime", "tomato"]        # always take every usable photo of these


def load_split(split: str, cache: Path) -> dict:
    path = cache / f"lvis_v1_{split}.json"
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        print(f"downloading LVIS {split} annotations (train ~350 MB, val ~65 MB)...", flush=True)
        data = urllib.request.urlopen(URL.format(split=split), timeout=600).read()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extract(f"lvis_v1_{split}.json", cache)
    return json.loads(path.read_text())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", nargs="*", default=["train", "val"], choices=["train", "val"])
    ap.add_argument("--max-per-class", type=int, default=300, help="cap for the common classes (bowl, knife...)")
    ap.add_argument("--max-side", type=int, default=640)
    ap.add_argument("--out", type=Path, default=LAPTOP / "datasets" / "lvis")
    ap.add_argument("--cache", type=Path, default=LAPTOP / "datasets" / "_lvis_cache")
    ap.add_argument("--val-frac", type=float, default=0.1)
    a = ap.parse_args()

    ours = class_list()
    picked: dict[int, dict] = {}                 # image id -> {"url", "boxes": [(cls, x, y, w, h)], "w", "h"}
    for split in a.split:
        d = load_split(split, a.cache)
        lvis_id = {c["name"]: c["id"] for c in d["categories"]}
        to_ours = {lvis_id[n]: o for o, ns in LVIS_NAMES.items() for n in ns}
        our_ids = set(to_ours)
        imgs = {i["id"]: i for i in d["images"]}
        anns = defaultdict(list)
        for an in d["annotations"]:
            if an["category_id"] in our_ids:
                anns[an["image_id"]].append(an)
        usable = []
        for iid, al in anns.items():
            im = imgs[iid]
            boxed = {an["category_id"] for an in al}
            # every one of our classes must be boxed or not flagged as incompletely labelled
            if set(im.get("not_exhaustive_category_ids", [])) & our_ids:
                continue
            usable.append((iid, Counter(to_ours[c] for c in boxed)))
        random.Random(0).shuffle(usable)
        count = Counter()
        for iid, cls in usable:
            if iid in picked:
                continue
            if any(c in PRIORITY for c in cls) or any(count[c] < a.max_per_class for c in cls):
                im = imgs[iid]
                picked[iid] = {"url": im["coco_url"], "w": im["width"], "h": im["height"],
                               "boxes": [(to_ours[an["category_id"]], *an["bbox"]) for an in anns[iid]]}
                count.update(cls)
        print(f"{split}: {len(usable)} usable photos, picked so far {len(picked)}")

    per_class = Counter(b[0] for p in picked.values() for b in p["boxes"])
    print("boxes per object: " + ", ".join(f"{c} {per_class[c]}" for c in ours if per_class[c]))
    ids = sorted(picked)
    random.Random(1).shuffle(ids)
    n_val = max(1, int(len(ids) * a.val_frac))
    for split in ("train", "val"):
        (a.out / "images" / split).mkdir(parents=True, exist_ok=True)
        (a.out / "labels" / split).mkdir(parents=True, exist_ok=True)

    def fetch(k_iid):
        k, iid = k_iid
        split = "val" if k < n_val else "train"
        p = picked[iid]
        dst = a.out / "images" / split / f"lvis_{iid}.jpg"
        if not dst.exists():
            raw = urllib.request.urlopen(p["url"], timeout=60).read()
            im = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if im is None:
                return 0
            s = min(1.0, a.max_side / max(im.shape[:2]))
            if s < 1.0:
                im = cv2.resize(im, (round(im.shape[1] * s), round(im.shape[0] * s)), interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(dst), im, [cv2.IMWRITE_JPEG_QUALITY, 90])
        w, h = p["w"], p["h"]           # boxes are in original pixels; YOLO labels are normalised, so no rescale
        lines = [f"{ours.index(c)} {(x + bw / 2) / w:.6f} {(y + bh / 2) / h:.6f} {bw / w:.6f} {bh / h:.6f}"
                 for c, x, y, bw, bh in p["boxes"]]
        (a.out / "labels" / split / f"lvis_{iid}.txt").write_text("\n".join(lines) + "\n")
        return 1

    print(f"downloading {len(ids)} photos from COCO...", flush=True)
    done = 0
    with ThreadPoolExecutor(8) as pool:
        for k, ok in enumerate(pool.map(fetch, enumerate(ids))):
            done += ok
            if (k + 1) % 200 == 0:
                print(f"  {k + 1}/{len(ids)}", flush=True)
    names_yaml = "\n".join(f"  {i}: {c}" for i, c in enumerate(ours))
    (a.out / "data.yaml").write_text(f"path: {a.out}\ntrain: images/train\nval: images/val\nnames:\n{names_yaml}\n")
    print(f"done: {done} photos -> {a.out} ({len(ids) - n_val} train / {n_val} val)")


if __name__ == "__main__":
    main()
