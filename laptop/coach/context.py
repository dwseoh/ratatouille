"""What the agent knows each turn: the [state ...] note, and the recipe as the system prompt shows it.

Two halves, split by how often they change (prompt caching is a prefix match, see agent.py):
- `render_recipe()` goes into the cached system prompt once: the whole recipe as numbered plain text (step numbers
  the model can say, amounts, substitutes), not raw JSON (no 0-based list it has to count, no file paths).
- `state_note()` is rebuilt every turn and sent at the END of the request (in the newest user message): step and
  time on it, next step, where things are, the laser, timers, servings, cook notes, recent events, what the cook
  said earlier, whether they seem stuck. Cheap to compute (no model calls, no I/O): it runs on the turn's hot path.

Positions are the cook's: a chest camera looks the way they face, so image left = their left; near = bottom of
the frame (close to them).
"""
from __future__ import annotations

import math
import re
import time
from fractions import Fraction

from .contracts import Det, Recipe, State
from .timers import describe as describe_timers

EVENTS_SHOWN = 5
EVENT_MAX_AGE_S = 600       # older events are history, not news
STUCK_S = 240               # this long on one step (pauses excluded) -> "cook may be stuck"
STUCK_QUESTIONS = 3         # ...or this many questions on the same step
CONTAINERS = ("bowl", "pan", "pot", "plate", "dish", "jar", "tray", "cup", "glass")
SURFACES = ("cutting board", "board", "chopping board", "counter")
NEAR_FRAC = 0.5             # "next to" = box edges closer than this fraction of the landmark's size
_QUESTION_WORDS = {"is", "are", "does", "do", "should", "how", "why", "can", "could", "what", "where", "which",
                   "when", "will", "was", "am", "did"}


# ───────────────────────── where things are ─────────────────────────

def _best(dets) -> dict[str, Det]:
    out: dict[str, Det] = {}
    for d in sorted(dets, key=lambda d: -d.conf):
        if d.name != "hand" and d.name not in out:
            out[d.name] = d
    return out


def positions(dets, width: int, height: int) -> dict[str, str]:
    """Where each visible object is, in words: "left, near". Left/right are the user's (a chest camera looks the
    way they face); near = bottom half of the frame (close to them), far = top half. Best detection per name."""
    out: dict[str, str] = {}
    for name, d in _best(dets).items():
        cx, cy = d.center
        horizontal = "left" if cx < width / 3 else "right" if cx > 2 * width / 3 else "centre"
        out[name] = f"{horizontal}, {'near' if cy > height / 2 else 'far'}"
    return out


def _landmark(name: str) -> str | None:
    """"in" for containers, "on" for surfaces, None for anything else."""
    n = name.lower()
    if any(n == c or n.endswith(" " + c) for c in CONTAINERS):
        return "in"
    if any(n == s or n.endswith(" " + s) for s in SURFACES):
        return "on"
    return None


def relations(dets) -> dict[str, str]:
    """Each object's relation to the nearest landmark (bowl, cutting board ...): "in the bowl", "on the cutting
    board", "left of the bowl", "behind the bowl". Objects far from every landmark are left out."""
    best = _best(dets)
    marks = {n: d for n, d in best.items() if _landmark(n)}
    out: dict[str, str] = {}
    for name, d in best.items():
        cx, cy = d.center
        options = []
        for lm, L in marks.items():
            if lm == name:
                continue
            x1, y1, x2, y2 = L.box
            if x1 <= cx <= x2 and y1 <= cy <= y2:
                options.append((-1.0, f"{_landmark(lm)} the {lm}"))
                continue
            if name in marks:
                continue                        # landmark vs landmark: only "the bowl is on the board" is news
            gx = max(0.0, x1 - d.box[2], d.box[0] - x2)
            gy = max(0.0, y1 - d.box[3], d.box[1] - y2)
            gap, size = math.hypot(gx, gy), max(x2 - x1, y2 - y1)
            if gap <= NEAR_FRAC * size:
                lx, ly = L.center
                dx, dy = cx - lx, cy - ly
                side = (("left of" if dx < 0 else "right of") if abs(dx) >= abs(dy)
                        else ("in front of" if dy > 0 else "behind"))
                options.append((gap, f"{side} the {lm}"))
        if options:
            out[name] = min(options)[1]
    return out


def describe_visible(dets, frame_size: tuple[int, int] | None) -> str:
    """"bowl (centre, far), lime (left, near, next to the bowl)"; just names without a frame size."""
    names = sorted({d.name for d in dets})
    if not names:
        return "nothing"
    if frame_size is None:
        return ", ".join(names)
    w, h = frame_size
    pos, rel = positions(dets, w, h), relations(dets)
    out = []
    for n in names:
        bits = [b for b in (pos.get(n), rel.get(n)) if b]
        out.append(f"{n} ({', '.join(bits)})" if bits else n)
    return ", ".join(out)


# ───────────────────────── small formatters ─────────────────────────

def fmt_span(seconds: float) -> str:
    """45 -> "45 s", 130 -> "2 min 10 s", 840 -> "14 min", 3900 -> "1 h 5 min"."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s"
    m, sec = divmod(s, 60)
    if m < 10:
        return f"{m} min {sec} s" if sec else f"{m} min"
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if m else f"{h} h"


def fmt_qty(q: float) -> str:
    """Kitchen numbers: 0.25 -> "1/4", 1.5 -> "1 1/2", 2.0 -> "2", 0.333 -> "1/3", 0.125 -> "1/8"."""
    if q <= 0:
        return ""
    for den in (1, 2, 4, 3, 8):                     # the fractions a cook measures with, simplest first
        f = Fraction(round(q * den), den)
        if f > 0 and abs(float(f) - q) <= 0.02:
            break
    else:                                           # not a kitchen fraction: one decimal
        return f"{q:.1f}".rstrip("0").rstrip(".")
    whole, rest = divmod(f.numerator, f.denominator)
    if rest == 0:
        return str(whole)
    frac = f"{rest}/{f.denominator}"
    return f"{whole} {frac}" if whole else frac


def amounts(recipe: Recipe, servings: int | None = None) -> str:
    """"2 avocado, 1/4 onion, 1 pinch salt ..." for `servings` people (default: the recipe's own amount)."""
    base = recipe.servings or 0
    k = (servings / base) if (servings and base) else 1.0
    out = []
    for ing in recipe.ingredients:
        q = fmt_qty(ing.qty * k)
        out.append(" ".join(x for x in (q, ing.unit, ing.item) if x) if q else f"{ing.item} to taste")
    return ", ".join(out)


def is_question(text: str) -> bool:
    t = text.strip().lower()
    words = re.sub(r"[^a-z' ]+", " ", t).split()
    return t.endswith("?") or bool(words and words[0] in _QUESTION_WORDS)


# ───────────────────────── the recipe, for the system prompt ─────────────────────────

def render_recipe(recipe: Recipe) -> str:
    """The whole recipe as numbered plain text. Stable for the app's lifetime (it sits in the cached prefix)."""
    lines = [f"RECIPE: {recipe.name}" + (f", makes {recipe.servings} servings" if recipe.servings else "")]
    if recipe.ingredients:
        lines.append(f"Ingredients{f' (for {recipe.servings})' if recipe.servings else ''}:")
        for ing in recipe.ingredients:
            q = fmt_qty(ing.qty)
            amount = " ".join(x for x in (q, ing.unit) if x) or "to taste"
            extra = [ing.note] if ing.note else []
            if ing.substitutes:
                extra.append("instead you can use: " + ", ".join(ing.substitutes))
            else:
                extra.append("no substitute")
            lines.append(f"- {ing.item}: {amount} ({'; '.join(extra)})")
    lines.append("Objects the camera knows and the laser can point at: " + ", ".join(recipe.objects))
    if recipe.aliases:
        lines.append("Other names people use: " + "; ".join(
            f"{o} = {', '.join(a)}" for o, a in recipe.aliases.items() if a))
    for i, s in enumerate(recipe.steps, 1):
        lines.append(f"Step {i} ({s.short}): {s.goal}")
        lines.append("  How: " + "; ".join(s.how))
        if s.tips:
            lines.append("  Tips: " + "; ".join(s.tips))
        lines.append(f"  Laser shows at the start: {', '.join(s.point) or 'nothing'}. Done when: {s.done_when}")
    return "\n".join(lines)


# ───────────────────────── the per-turn state note ─────────────────────────

def state_note(state: State, *, frame_size: tuple[int, int] | None = None, camera: str = "ok",
               heard_earlier: list[tuple[float, str]] = (), questions_on_step: int = 0,
               now: float | None = None) -> str:
    """The app's view of the kitchen right now, one line, `|`-separated (it becomes "[...]" in the user message).

    camera: "ok" (frames this turn), "offline" (no frames) or "ignored" (the cook asked not to use it).
    heard_earlier: (time.time(), words) of things the cook said that are no longer in the kept conversation.
    """
    now = time.time() if now is None else now
    s, recipe = state, state.recipe
    step = s.current_step
    n, total = s.step + 1, len(recipe.steps)
    parts = [f"step {n}/{total} ({step.short}): {step.goal}"]
    on_step = s.step_seconds()
    if on_step >= 30:
        parts.append(f"on this step {fmt_span(on_step)}")
    parts.append(f"next: step {n + 1} ({recipe.steps[n].short})" if n < total else "next: nothing, this is the last step")
    if s.session == "paused":
        away = time.monotonic() - s.paused_at if s.paused_at is not None else 0.0
        parts.append(f"session: PAUSED {fmt_span(away)} ago, the cook stepped away (answer only what they ask, "
                     "briefly; no pointing, no step changes; resume_session if they want to carry on)")

    if camera == "ignored":
        parts.append("visible: unknown")
    else:
        parts.append(f"visible: {describe_visible(s.dets, frame_size if camera == 'ok' else None)}")
        if camera == "ok":
            missing = [o for o in step.point if o not in s.visible()]
            if missing:
                parts.append(f"needed now, not in view: {', '.join(missing)}")
    if s.target:
        parts.append(f"laser: {s.target} ({'on target' if s.on_target else 'aiming'})")
    else:
        parts.append("laser: off")

    if s.timers:
        parts.append(f"timers: {describe_timers(s)}")
    if s.servings and recipe.servings and s.servings != recipe.servings:
        parts.append(f"servings: {s.servings} (the recipe is for {recipe.servings}; amounts now: "
                     f"{amounts(recipe, s.servings)})")
    if s.cook_notes:
        parts.append(f"cook notes: {'; '.join(s.cook_notes)}")

    recent = [e for e in s.events[-EVENTS_SHOWN:] if now - e.t <= EVENT_MAX_AGE_S]
    parts.append("recent: " + ("; ".join(f"{e.source} {fmt_span(now - e.t)} ago: {e.text}" for e in recent)
                               if recent else "none"))
    if heard_earlier:
        parts.append("cook said earlier: " + "; ".join(
            f'"{text[:90]}" ({fmt_span(now - t)} ago)' for t, text in heard_earlier))
    if s.session != "paused" and (on_step >= STUCK_S or questions_on_step >= STUCK_QUESTIONS):
        why = [f"{fmt_span(on_step)} on this step"] if on_step >= STUCK_S else []
        if questions_on_step >= STUCK_QUESTIONS:
            why.append(f"{questions_on_step} questions about it")
        parts.append(f"cook may be stuck: {', '.join(why)}")
    return " | ".join(parts)
