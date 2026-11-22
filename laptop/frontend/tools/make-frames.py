"""Resize the talking-animation frames, title and tab icons from laptop/imgs/ (read-only) into src/assets/ and public/.
Run from laptop/frontend/:  python3 tools/make-frames.py   (needs Pillow)"""
from pathlib import Path
from PIL import Image

SRC = Path(__file__).resolve().parents[2] / "imgs"  # laptop/imgs
DST = Path(__file__).resolve().parents[1] / "src" / "assets"
SIZE = 512  # displayed at 130 px; 512 stays sharp on 3x screens

for name in ("remy_talking", "user_talking"):
    for i in range(1, 5):
        im = Image.open(SRC / f"{name}-{i}.png").convert("RGBA").resize((SIZE, SIZE), Image.LANCZOS)
        out = DST / f"{name.replace('_', '-')}-{i}.png"
        im.save(out, optimize=True)
        print(out.name, out.stat().st_size // 1024, "KB")

# Title: laptop/imgs/title-{light,dark}.png (2048x600) -> src/assets/ at 1024x300 (shown 352 px wide; 2x is plenty).
# The crop offsets in index.css assume the drawing sits at x 258..1847, y 104..447 of the 2048x600 canvas.
for mode in ("light", "dark"):
    im = Image.open(SRC / f"title-{mode}.png").convert("RGBA").resize((1024, 300), Image.LANCZOS)
    out = DST / f"title-{mode}.png"
    im.save(out, optimize=True)
    print(out.name, out.stat().st_size // 1024, "KB")

# Tab icons: laptop/imgs/icon-{light,dark}.png -> public/favicon-32-{light,dark}.png (+ favicon-180.png from light)
PUBLIC = Path(__file__).resolve().parents[1] / "public"
for mode in ("light", "dark"):
    icon = Image.open(SRC / f"icon-{mode}.png").convert("RGBA")
    out = PUBLIC / f"favicon-32-{mode}.png"
    icon.resize((32, 32), Image.LANCZOS).save(out, optimize=True)
    print(out.name, out.stat().st_size // 1024, "KB")
    if mode == "light":
        out = PUBLIC / "favicon-180.png"
        icon.resize((180, 180), Image.LANCZOS).save(out, optimize=True)
        print(out.name, out.stat().st_size // 1024, "KB")
