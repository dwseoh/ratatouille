"""Recipe importer (Phase 7b): a link, PDF, text file or photo of a recipe -> a recipe JSON in our format.

An offline command-line tool (`python -m tools.import_recipe <link|file>`), never on the live path.

    1. read      links: the page's schema.org JSON-LD recipe data (title, ingredients, steps, image), no AI needed;
                 falls back to the page text. PDFs and photos go to Claude as a document / image.
    2. convert   Claude (structured output, quality over speed) -> our format. `objects` are the physical things a
                 camera can see ("salt", "bowl"), not "1 tsp kosher salt, divided".
    3. validate  strict checks BEFORE anything is saved; one retry that tells Claude what was wrong.
    4. save      recipes/<slug>.json (+ the hero image downloaded to recipes/images/<slug>/, never hot-linked).

Caveats: scraped photos can be copyrighted (we keep the source link in the JSON; local demo only), and YOLO-World is
untested on arbitrary ingredients, so say "imports a recipe", not "works with any counter".
"""
from __future__ import annotations

import base64
import html as htmllib
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from .recipe_images import slug as make_slug

IMPORT_MODEL = "claude-opus-5-5"          # quality over speed: this runs once, ahead of time
USER_AGENT = "Mozilla/5.0 (compatible; RemyRecipeImporter/1.0)"
MAX_PAGE_BYTES = 3_000_000
MAX_IMAGE_BYTES = 5_000_000
MAX_TEXT_CHARS = 24_000
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}

# validation limits
MIN_OBJECTS, MAX_OBJECTS = 3, 16
MIN_STEPS, MAX_STEPS = 2, 14
MAX_SHORT = 15
MAX_FALLBACK_WORDS = 22
_MEASURE = re.compile(r"\d|\b(tsp|tbsp|teaspoons?|tablespoons?|cups?|grams?|kg|oz|ounces?|lbs?|pounds?|ml|litres?|liters?|"
                      r"divided|to taste|optional|chopped|diced|minced|sliced|plus more|for serving)\b|[,;()]", re.I)

SYSTEM = """You convert a recipe into the JSON format used by a hands-free cooking coach that has a camera.

Rules
- objects: the PHYSICAL THINGS a camera can see on the counter: ingredients as whole items ("avocado", "red onion",
  "lime") and tools ("knife", "bowl", "cutting board", "fork"). Lowercase, singular, 1-3 words, no quantities and no
  preparation notes: "1 tsp kosher salt, divided" becomes "salt". At most 12 objects, only things a person would point at.
- aliases: other ways people say an object ("lime": ["limes", "the green one"]). Only for objects.
- steps: keep the author's order and do not invent ingredients or steps. Usually 4 to 10 steps. For each step:
    short      15 characters or fewer, e.g. "dice onion"
    goal       one sentence: what this step achieves
    how        2 to 5 short actions in order; keep quantities here ("add a pinch of salt")
    tips       technique and safety tips (claw grip, keep fingers curled away from the knife); may be empty
    point      objects (from `objects`) the laser should show when the step starts, in order
    done_when  what the camera should see when the step is finished, concrete and visual
    fallback   ONE short imperative sentence (at most 15 words) the coach can say if its AI is unavailable
    source_steps  the numbers of the source's numbered instructions this step covers (e.g. [3, 4]); [] if unnumbered
- Plain English a nervous beginner understands. If the source is not a recipe, return an empty steps array."""

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "objects": {"type": "array", "items": {"type": "string"}},
        "aliases": {"type": "array", "items": {
            "type": "object",
            "properties": {"object": {"type": "string"}, "also_called": {"type": "array", "items": {"type": "string"}}},
            "required": ["object", "also_called"], "additionalProperties": False}},
        "steps": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "short": {"type": "string"}, "goal": {"type": "string"},
                "how": {"type": "array", "items": {"type": "string"}},
                "tips": {"type": "array", "items": {"type": "string"}},
                "point": {"type": "array", "items": {"type": "string"}},
                "done_when": {"type": "string"}, "fallback": {"type": "string"},
                "source_steps": {"type": "array", "items": {"type": "integer"}}},
            "required": ["short", "goal", "how", "tips", "point", "done_when", "fallback", "source_steps"],
            "additionalProperties": False}},
    },
    "required": ["name", "objects", "aliases", "steps"],
    "additionalProperties": False,
}


class ImportErr(Exception):
    """The recipe could not be read or converted (the message says why, in plain words)."""


@dataclass
class Source:
    kind: str                                   # "web" | "pdf" | "image" | "text"
    title: str = ""
    text: str = ""                              # recipe text for Claude (web / text files)
    blocks: list[dict] = field(default_factory=list)       # Claude content blocks (pdf / image)
    link: str = ""                              # where it came from (kept in the JSON)
    hero_url: str = ""                          # picture of the finished dish to download (links only)
    step_images: list[str] = field(default_factory=list)   # picture url (or "") per numbered instruction, aligned with the text
    step_map: list[list[int]] = field(default_factory=list)  # per recipe step: the instruction numbers it covers (set by convert)


# ───────────────────────── reading ─────────────────────────

def is_url(arg: str) -> bool:
    return bool(re.match(r"^[a-z][a-z0-9+.-]*://", arg, re.I))


def _check_http(url: str) -> None:
    if urllib.parse.urlparse(url).scheme.lower() not in ("http", "https"):
        raise ImportErr(f"only http(s) links are supported, not {url!r}")


def fetch_url(url: str, max_bytes: int = MAX_PAGE_BYTES) -> tuple[str, bytes, str]:
    """GET a page or file. Returns (final url, body, content-type). Only http(s); size-capped."""
    _check_http(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read(max_bytes + 1)
            ctype = r.headers.get("Content-Type", "")
            final = r.geturl()
    except Exception as e:
        raise ImportErr(f"could not fetch {url}: {e}") from e
    if len(body) > max_bytes:
        raise ImportErr(f"{url} is larger than {max_bytes // 1000} KB")
    return final, body, ctype


def _decode(body: bytes, ctype: str) -> str:
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    for enc in ([m.group(1)] if m else []) + ["utf-8", "latin-1"]:
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def _types(node) -> list[str]:
    t = node.get("@type", []) if isinstance(node, dict) else []
    return [t] if isinstance(t, str) else [x for x in t if isinstance(x, str)]


def extract_jsonld_recipe(page: str) -> dict | None:
    """The schema.org Recipe object embedded in the page (most recipe sites have one), or None."""
    found: list[dict] = []

    def walk(node):
        if isinstance(node, list):
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            if "Recipe" in _types(node):
                found.append(node)
            for k in ("@graph", "mainEntity", "mainEntityOfPage", "itemListElement"):
                if k in node:
                    walk(node[k])

    for m in re.finditer(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", page, re.S | re.I):
        try:
            walk(json.loads(htmllib.unescape(m.group(1)).strip()))
        except ValueError:
            continue
    return found[0] if found else None


def _image_of(node) -> str:
    img = node.get("image") if isinstance(node, dict) else None
    if isinstance(img, list):
        img = img[0] if img else ""
    if isinstance(img, dict):
        img = img.get("url", "")
    return img if isinstance(img, str) else ""


def _flatten_steps(instr) -> list[tuple[str, str]]:
    """The recipe's instructions as (text, picture url or "") pairs, in order. Section headings count as a line."""
    out: list[tuple[str, str]] = []
    if isinstance(instr, str):
        out += [(ln.strip(), "") for ln in re.split(r"[\r\n]+", htmllib.unescape(instr)) if ln.strip()]
    elif isinstance(instr, list):
        for it in instr:
            out += _flatten_steps(it)
    elif isinstance(instr, dict):
        if "HowToSection" in _types(instr):
            if instr.get("name"):
                out.append((f"[{instr['name']}]", ""))
            out += _flatten_steps(instr.get("itemListElement", []))
        else:
            lines = _flatten_steps(instr.get("text") or instr.get("name") or "")
            img = _image_of(instr)
            if lines and img:
                lines[0] = (lines[0][0], img)
            out += lines
    return out


def _flatten_instructions(instr) -> list[str]:
    return [t for t, _ in _flatten_steps(instr)]


def hero_image_url(node: dict) -> str:
    img = node.get("image")
    if isinstance(img, list):
        img = img[0] if img else ""
    if isinstance(img, dict):
        img = img.get("url", "")
    return img if isinstance(img, str) else ""


def recipe_text_from_jsonld(node: dict) -> str:
    ing = node.get("recipeIngredient") or node.get("ingredients") or []
    ing = ing if isinstance(ing, list) else [ing]
    steps = _flatten_instructions(node.get("recipeInstructions", []))
    parts = [f"Title: {htmllib.unescape(str(node.get('name', '')))}"]
    if node.get("description"):
        parts.append(f"Description: {htmllib.unescape(str(node['description']))}")
    if node.get("recipeYield"):
        parts.append(f"Yield: {node['recipeYield']}")
    parts.append("Ingredients:\n" + "\n".join(f"- {htmllib.unescape(str(i))}" for i in ing))
    parts.append("Instructions:\n" + "\n".join(f"{n}. {s}" for n, s in enumerate(steps, 1)))
    return "\n\n".join(parts)


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "iframe"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in ("p", "br", "li", "div", "h1", "h2", "h3", "h4", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip() + " ")


def html_to_text(page: str, limit: int = MAX_TEXT_CHARS) -> str:
    p = _TextExtractor()
    p.feed(page)
    text = re.sub(r"[ \t]+", " ", "".join(p.parts))
    return re.sub(r"\n\s*\n+", "\n", text).strip()[:limit]


def load_source(arg: str, *, fetch: Callable = fetch_url) -> Source:
    """`arg` is a link or a path to a PDF / photo / text file."""
    if is_url(arg):
        final, body, ctype = fetch(arg)
        low = urllib.parse.urlparse(final).path.lower()
        if "pdf" in ctype.lower() or low.endswith(".pdf"):
            return _pdf_source(body, final)
        ext = Path(low).suffix
        if ctype.lower().startswith("image/") or ext in IMAGE_TYPES:
            return _image_source(body, ctype.split(";")[0] if ctype.lower().startswith("image/") else IMAGE_TYPES[ext], final)
        page = _decode(body, ctype)
        node = extract_jsonld_recipe(page)
        if node is not None:
            steps = _flatten_steps(node.get("recipeInstructions", []))
            return Source("web", title=str(node.get("name", "")), text=recipe_text_from_jsonld(node), link=final,
                          hero_url=urllib.parse.urljoin(final, hero_image_url(node)) if hero_image_url(node) else "",
                          step_images=[urllib.parse.urljoin(final, u) if u else "" for _, u in steps])
        text = html_to_text(page)
        if len(text) < 200:
            raise ImportErr("that page has no readable recipe (no recipe data and almost no text)")
        title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
        return Source("web", title=htmllib.unescape(title.group(1)).strip() if title else "", text=text, link=final)
    path = Path(arg).expanduser()
    if not path.is_file():
        raise ImportErr(f"{arg!r} is neither a link nor an existing file")
    data, ext = path.read_bytes(), path.suffix.lower()
    if ext == ".pdf":
        return _pdf_source(data, "", title=path.stem)
    if ext in IMAGE_TYPES:
        return _image_source(data, IMAGE_TYPES[ext], "", title=path.stem)
    return Source("text", title=path.stem, text=data.decode("utf-8", errors="replace")[:MAX_TEXT_CHARS])


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode()


def _pdf_source(data: bytes, link: str, title: str = "") -> Source:
    if not data.startswith(b"%PDF"):
        raise ImportErr("that file says it is a PDF but is not")
    return Source("pdf", title=title, link=link, blocks=[{"type": "document", "source": {
        "type": "base64", "media_type": "application/pdf", "data": _b64(data)}}])


def _image_source(data: bytes, media_type: str, link: str, title: str = "") -> Source:
    if len(data) > MAX_IMAGE_BYTES:
        raise ImportErr(f"that picture is larger than {MAX_IMAGE_BYTES // 1_000_000} MB")
    return Source("image", title=title, link=link, blocks=[{"type": "image", "source": {
        "type": "base64", "media_type": media_type, "data": _b64(data)}}])


# ───────────────────────── converting ─────────────────────────

def _first_text(resp) -> str:
    for b in resp.content:
        if getattr(b, "type", None) == "text":
            return b.text
    raise ImportErr("Claude returned no text")


def normalize(raw: dict, source: Source, name: str | None = None) -> dict:
    """Claude's structured answer -> the JSON layout of recipes/*.json."""
    objects = []
    for o in raw.get("objects", []):
        o = str(o).strip().lower()
        if o and o not in objects:
            objects.append(o)
    aliases: dict[str, list[str]] = {}
    for a in raw.get("aliases", []):
        obj = str(a.get("object", "")).strip().lower()
        alts = [str(x).strip() for x in a.get("also_called", []) if str(x).strip()]
        if obj and alts:
            aliases.setdefault(obj, []).extend(x for x in alts if x not in aliases.get(obj, []))
    steps = []
    source.step_map = [[int(i) for i in s.get("source_steps", []) if isinstance(i, int)] for s in raw.get("steps", [])]
    for s in raw.get("steps", []):
        steps.append({
            "short": str(s.get("short", "")).strip(), "goal": str(s.get("goal", "")).strip(),
            "how": [str(x).strip() for x in s.get("how", []) if str(x).strip()],
            "tips": [str(x).strip() for x in s.get("tips", []) if str(x).strip()],
            "point": [str(x).strip().lower() for x in s.get("point", []) if str(x).strip()],
            "done_when": str(s.get("done_when", "")).strip(), "fallback": str(s.get("fallback", "")).strip(),
            "image": ""})
    out = {"name": (name or raw.get("name") or source.title or "Recipe").strip(), "objects": objects,
           "aliases": aliases, "steps": steps}
    if source.link:
        out["source"] = source.link                         # keep where it came from (photos may be copyrighted)
    return out


def validate(d: dict) -> list[str]:
    """Plain-language problems; an empty list means the recipe is safe to save."""
    bad: list[str] = []
    if not str(d.get("name", "")).strip():
        bad.append("the recipe has no name")
    objs = d.get("objects", [])
    if not MIN_OBJECTS <= len(objs) <= MAX_OBJECTS:
        bad.append(f"there are {len(objs)} objects; there must be between {MIN_OBJECTS} and {MAX_OBJECTS}")
    for o in objs:
        if _MEASURE.search(o) or len(o.split()) > 3 or len(o) > 28:
            bad.append(f"object {o!r} is not a physical thing the camera can see (no quantities, notes or punctuation, 1-3 words)")
    for k in d.get("aliases", {}):
        if k not in objs:
            bad.append(f"alias key {k!r} is not one of the objects")
    steps = d.get("steps", [])
    if not MIN_STEPS <= len(steps) <= MAX_STEPS:
        bad.append(f"there are {len(steps)} steps; there must be between {MIN_STEPS} and {MAX_STEPS}")
    for n, s in enumerate(steps, 1):
        who = f"step {n} ({s.get('short') or '?'})"
        if not s.get("short") or len(s["short"]) > MAX_SHORT:
            bad.append(f"{who}: `short` must be 1 to {MAX_SHORT} characters")
        for key in ("goal", "done_when", "fallback"):
            if not s.get(key):
                bad.append(f"{who}: `{key}` is empty")
        if not s.get("how"):
            bad.append(f"{who}: `how` needs at least one action")
        if len(str(s.get("fallback", "")).split()) > MAX_FALLBACK_WORDS:
            bad.append(f"{who}: `fallback` must be one short sentence (at most {MAX_FALLBACK_WORDS} words)")
        for pt in s.get("point", []):
            if pt not in objs:
                bad.append(f"{who}: point {pt!r} is not in `objects`")
    if not bad:                                             # last: it must load through the real contract
        import tempfile

        from .contracts import Recipe
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "r.json"
            p.write_text(json.dumps(d))
            try:
                Recipe.load(p)
            except Exception as e:
                bad.append(f"it does not load as a Recipe: {e}")
    return bad


def convert(client, source: Source, *, model: str = IMPORT_MODEL, name: str | None = None, retries: int = 1) -> dict:
    """Source -> a validated recipe dict. Retries once, telling Claude exactly what was wrong."""
    intro = "Convert this recipe." + (f" Link: {source.link}" if source.link else "")
    content = list(source.blocks) + [{"type": "text", "text": f"{intro}\n\n{source.text}".strip()}]
    messages = [{"role": "user", "content": content}]
    problems: list[str] = []
    for attempt in range(retries + 1):
        resp = client.messages.create(
            model=model, max_tokens=8000, system=SYSTEM, messages=messages,
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}})
        text = _first_text(resp)
        try:
            raw = json.loads(text)
        except ValueError as e:
            raise ImportErr(f"Claude's answer was not valid JSON: {e}") from e
        recipe = normalize(raw, source, name)
        problems = validate(recipe)
        if not problems:
            return recipe
        if attempt < retries:
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "That has problems. Fix ALL of them and answer again with the "
                                                     "complete recipe:\n- " + "\n- ".join(problems)}]
    raise ImportErr("could not produce a valid recipe:\n- " + "\n- ".join(problems))


# ───────────────────────── saving ─────────────────────────

def sniff_image_ext(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def download_hero(url: str, dest_dir: Path, *, fetch: Callable = fetch_url) -> Path:
    """Save the recipe's main picture as `hero.<ext>` (no hot-linking). Checks it really is a small picture."""
    _check_http(url)
    _final, body, ctype = fetch(url, MAX_IMAGE_BYTES)
    ext = sniff_image_ext(body)
    if ext is None or not ctype.lower().startswith("image/"):
        raise ImportErr(f"{url} is not a JPEG / PNG / WebP picture")
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"hero{ext}"
    path.write_bytes(body)
    return path


def download_step_images(recipe: dict, source: Source, dest_dir: Path, *, fetch: Callable = fetch_url) -> list[Path]:
    """Save each step's own picture (when the source page had one for the instructions that step covers) as
    `step-N.<ext>`. Steps without one keep showing the finished-dish picture (hero). Failures are skipped quietly."""
    saved: list[Path] = []
    seen: set[str] = set()
    for n, numbers in enumerate(source.step_map, 1):
        urls = [source.step_images[i - 1] for i in numbers if 1 <= i <= len(source.step_images)]
        url = next((u for u in urls if u), "")
        if not url or url in seen or n > len(recipe["steps"]):
            continue                                    # no picture, or the same one as another step (don't repeat it)
        seen.add(url)
        try:
            _final, body, ctype = fetch(url, MAX_IMAGE_BYTES)
        except ImportErr:
            continue
        ext = sniff_image_ext(body)
        if ext is None or not ctype.lower().startswith("image/"):
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"step-{n}{ext}"
        path.write_bytes(body)
        saved.append(path)
    return saved


def save_recipe(recipe: dict, out_dir: Path, slug: str | None = None) -> Path:
    slug = slug or make_slug(recipe["name"])
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{slug}.json"
    path.write_text(json.dumps(recipe, indent=2, ensure_ascii=False) + "\n")
    return path


def summary(recipe: dict) -> str:
    lines = [f"{recipe['name']}", f"  objects ({len(recipe['objects'])}): {', '.join(recipe['objects'])}"]
    for n, s in enumerate(recipe["steps"], 1):
        lines.append(f"  {n}. {s['short']:<15} {s['goal']}")
    return "\n".join(lines)
