"""Where a step's "what it should look like" photo lives.

Photos are taken at the first rehearsal and dropped in `laptop/recipes/images/<recipe>/`:

    images/guacamole/step-1.jpg  ... step-7.jpg     one per step (1-based)
    images/guacamole/hero.jpg                       fallback for steps without their own picture

A picture is ALWAYS available: the step's own photo, else the finished-dish `hero`, else a plain placeholder card
(`placeholder_svg`), so the dashboard never shows an empty hole.

A step can also name a file explicitly (`"image": "images/guacamole/mash.jpg"` in the recipe JSON, relative to
`recipes/`). Nothing is required.
Only files inside `recipes/` are ever served, however the JSON is written.
"""
from __future__ import annotations

import re
from pathlib import Path

RECIPES_DIR = Path(__file__).resolve().parent.parent / "recipes"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def slug(name: str) -> str:
    """"Guacamole" -> "guacamole", "Mom's Pad Thai!" -> "mom-s-pad-thai"."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "recipe"


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _first_existing(folder: Path, stem: str) -> Path | None:
    for ext in IMAGE_EXTS:
        p = folder / f"{stem}{ext}"
        if p.is_file():
            return p
    return None


def step_image_path(recipe, n: int, root: Path = RECIPES_DIR) -> Path | None:
    """The photo to show for step index `n` (0-based), or None."""
    if not 0 <= n < len(recipe.steps):
        return None
    candidates: list[Path | None] = []
    named = recipe.steps[n].image
    if named:
        candidates.append(root / named)
    folder = root / "images" / slug(recipe.name)
    candidates.append(_first_existing(folder, f"step-{n + 1}"))
    candidates.append(_first_existing(folder, "hero"))
    for p in candidates:
        if p is not None and p.is_file() and p.suffix.lower() in IMAGE_EXTS and _inside(p, root):
            return p
    return None


def placeholder_svg(recipe, n: int) -> str:
    """A calm card for when no photo exists at all: recipe name and the step. Served as an <img>, so it can't run code."""
    from html import escape
    n = min(max(n, 0), len(recipe.steps) - 1)
    return ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 640 360'>"
            "<rect width='640' height='360' fill='#2b2b2b'/>"
            f"<text x='320' y='160' fill='#e6e6e6' font-family='sans-serif' font-size='34' text-anchor='middle'>{escape(recipe.name)}</text>"
            f"<text x='320' y='212' fill='#a8a8a8' font-family='sans-serif' font-size='26' text-anchor='middle'>"
            f"Step {n + 1}: {escape(recipe.steps[n].short)}</text></svg>")
