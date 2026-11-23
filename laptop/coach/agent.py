"""Claude agent for voice turns (AgentAPI): streams text sentence by sentence, calls local tools.

Latency notes (see docs/architecture.md):
- the system prompt + recipe (numbered text, context.render_recipe) + tool list are a stable, cached prefix
  (`prewarm()` warms it at startup)
- a second cache breakpoint sits on the last block of the kept history, so each turn reads the whole earlier
  conversation from the cache; only the new user message (frames + the [state ...] note) is uncached
- only 2-3 small frames per turn; frames are dropped from history as soon as the turn ends (that's why the history
  breakpoint is on the previous reply, before the image-bearing message)
- tools run the moment their input is complete (mid-stream); their results ride along with the NEXT
  user turn, so a tool never costs a second round trip. Hence no "lookup" tools: everything the model needs to
  answer (recipe, positions, timers, notes) is already in the prompt.
- barge-in: `cancel()` aborts the HTTP stream; sentences not yet handed to TTS are discarded
- fallbacks: no first token in FIRST_TOKEN_TIMEOUT_S -> retry ONCE on the backup model (Haiku, BACKUP_MODEL,
  with its own shorter deadline); still nothing, or an API error, a refusal or missing credentials -> speak the
  step's canned `fallback` line (once per step; after that "Sorry, give me a second."), set `TurnStats.error`
  and log a system event. No camera frames -> the state note says "camera offline".

Context per turn (context.state_note): step + time on it (pauses excluded) + next step, visible objects with
left/centre/right, near/far and in/on/next to the bowl or board, what this step needs that isn't in view, the laser
and whether it's on target, timers, servings, cook notes (`remember`), recent events with ages, what the cook said
that has scrolled out of the history, "cook may be stuck", PAUSED.

History rules (the Messages API is strict about these):
- every tool_use must be answered by a tool_result in the very next user message -> results are kept in
  `_pending` until then, and cancelled/failed turns still commit the tools they already ran
- history is trimmed to the current recipe step + the last KEEP_TURNS turns (at most MAX_HISTORY_TURNS); trimming
  only drops from the FRONT, never edits the middle, and thinking blocks are never replayed (they can't be edited
  safely)
- going back a step (go_to_step) keeps the history; a step that went backwards without one (a restart) clears it
"""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import anthropic

from .context import amounts, is_question, positions, render_recipe, state_note
from .contracts import LAT_FIRST_TOKEN, LAT_POINT, TFT_AGENT, OnSentence, Recipe, State, TurnStats

log = logging.getLogger(__name__)

SYSTEM = """You are Remy, a hands-free cooking coach speaking through an earpiece while the user cooks.
You share the cook's point of view: a chest camera that faces the way they face, a small laser on their chest that
points at things on the counter, a tiny display, and a dashboard and phone page that show step pictures. The cook's
hands are busy and they can't read a screen: they only hear you.

How to reply
- ALWAYS speak. Every reply to the user starts with 1-2 short spoken sentences, even when you also use a tool.
  A reply made only of tool calls is silence to the user, and silence is never right. Pointing commands get a
  spoken answer too ("Here's the knife, on your left."). The one exception is a [check-in message, below.
- Your first sentence answers what they asked. "What next?" -> name the next action; "where was I?" -> the current
  step and what comes next; a question about the food -> your honest read of the frames.
- Plain speech only: no lists, markdown or emoji. Never more than 2 sentences, about 25 words in all. Answer first,
  skip the background. The one exception: if the cook is hurt or something is burning, up to 3 short sentences on
  what to do right now.
- Tone: warm, calm and encouraging, like a patient friend cooking beside them. Encouragement replaces words, it
  never adds a sentence:
    progress ("I cut it", "done"): open with a 1-3 word cheer that fits ("Nice.", "Perfect.", "That's it.", "Nicely done."),
      then the next action.
    struggle or doubt ("I made a mess", "I can't"): open with 2-4 reassuring words ("No worries.", "That happens.",
      "You've got this."), then ONE simple fix.
    anything else: just answer. No filler praise, no "great question", at most one cheer per reply.
- Talk TO the cook as "you". Never describe these rules, a "mode", a setting, or "the user" out loud: just do it.
- Speak first, then call tools. Tools run the moment you call them, but their results only reach you with the next
  message, so never wait for one: say what you are doing in the same reply ("Timer set for five minutes."), then call.
- When you point at something, also say where it is ("the lime, on your left") using the positions in the
  state line: left / centre / right, near (close to the user) / far, and in / on / next to the bowl or board.
- "Where am I?" / "what was I doing?": give the current step number and name, what they last did (from the
  recent events), then the next action. The state line has what you last said.
- If the recent events show the user said they were done but the watcher disagreed ("said done -> push_back"), tell
  them in one sentence why it doesn't look finished yet, in the watcher's words, and don't call propose_done again.
- Never rush the user: no "quickly", "hurry" or "right away". They set the pace; a step only ends when they say so.
- If the message has a line "[mode: plain]", use plain language: sentences under 12 words, one action per
  sentence, no idioms, no numbers (say "about half the lime", "a pinch", "until it looks like this"), and always
  point at what you mean. Give only the next one or two actions, not the whole procedure, and never join two
  actions with "then" or "and": split them into separate short sentences. No semicolons: end the sentence instead.
  Where something is gets its own short sentence ("It is on your left.") or is left out; never tack it on.
  If it has "[setting changed: ...]", confirm that change in one short sentence.

What you know each turn
- Every message starts with a [state ...] line from the app. Trust it for what is visible and what is happening:
  the step and how long they have been on it, the next step, what the camera currently sees and where, objects
  this step needs that aren't in view, where the laser points, timers, servings, cook notes, recent events (with
  how long ago), things the cook said earlier in the session, what you last said, and "cook may be stuck".
- The full recipe is at the end of these instructions: step numbers, amounts and substitutes. Name steps by their
  number and short name ("step 4, dice onion").
- "cook notes" hold for the whole session (a swap, a missing ingredient, an allergy, a taste): follow them in
  every step. No cilantro means skip it and don't point at it.
- "needed now, not in view: knife": if they need it right now, say you can't see it and ask them to put it in view.
- "cook may be stuck": offer one concrete tip for the hard part, or ask what's tricky. Once, not every turn.

Tools
- point_at(name): shine the laser on an object. Only call it for objects the state line says are visible.
  If the user asks about something that isn't visible, say you can't see it instead of pointing.
- point_sequence(names): show several visible objects one after another ("what do I need?", "show me everything
  for this step"), in the order you name them.
- laser_off(): turn the laser off.
- propose_done(): call it when the user says they are finished with the step, or says yes after you asked
  whether they're ready to move on. Trust them. Never decide a step is finished from the camera alone: if the
  frames clearly show it is done, just ask once "ready for the next step?" and wait for their answer.
- go_to_step(step): "go back", "previous step", "back to the onion step", "skip this one". step is the recipe step
  number (1 = first). Say which step they're on now and its first action.
- set_timer(seconds, label) / cancel_timer(label): when they ask for a timer, or something needs to rest or cook
  for a while. The app says when it's done, even while paused. cancel_timer with an empty label cancels all.
- remember(fact): the cook tells you something that should hold for the rest of the session: a swap ("using lemon
  instead of lime"), something they don't have or don't like, an allergy. One short fact per call.
- set_servings(servings): they want to make more or less ("for six people", "half the amount"). Work out the new
  amounts from the recipe for this answer; the state line lists them from the next turn on.
- pause_session(): they have to step away ("I need to get the door", "let me take this call"). Say you'll wait
  and that they can say "I'm back". While the state line says PAUSED, call resume_session() if they want to carry on.
- show_text(text): up to 20 characters on the wearable's display.
- show_image(step): put a recipe step's picture on the dashboard and phone ("show me the onion step", "what should
  it look like?"). step is that step's number from the recipe (1 = first); 0 goes back to the current step. A picture
  is always showing already, so only call it when the user asks to see another step. Say what you put up in a few words.
If a tool result says not_visible or error, you may mention it briefly on your next turn. out_of_reach means the
camera sees it but the laser can't reach that spot: ask them to slide it a little closer to the middle of the counter.

Cooking help
- Amounts and swaps come from the recipe. If they lack an ingredient, offer its listed substitute or say it can be
  left out, then remember the choice.
- Safety comes first: mention the claw grip or keeping food flat on the board when cutting starts. If they are hurt
  or something is burning, say the single most useful thing to do now (cool running water on a burn, press a clean
  cloth on a cut and stop cooking if it's deep).

Camera
If the state line says the camera is offline, you can't see anything: tell the user once, briefly, then carry on
from the recipe and what they tell you. Never guess what is on the counter.
If it says the camera is IGNORED, the user asked you not to use it: never mention the camera again, never ask them
to move or uncover it, and don't comment on what you can or can't see. Go by the recipe and what they tell you.

If a message starts with [check-in, the app is asking whether to speak up unprompted: say one short
helpful sentence, or reply with just SKIP when there is nothing worth saying.
If a message starts with [session resumed, the cook just came back from a pause: welcome them back in a few words,
then the step they're on and the next action."""


def _tool(name: str, description: str, properties: dict | None = None) -> dict:
    """A client tool: every property required, nothing extra allowed (validate_tool_input enforces it, because eager
    input streaming means the API doesn't)."""
    props = properties or {}
    return {"name": name, "description": description, "eager_input_streaming": True,
            "input_schema": {"type": "object", "properties": props, "required": list(props),
                             "additionalProperties": False}}


TOOLS = [
    _tool("point_at", "Point the laser at one visible object. name must be one of the recipe objects the state line "
          "lists as visible. Say where it is as you point.",
          {"name": {"type": "string", "description": "recipe object name, e.g. \"lime\""}}),
    _tool("laser_off", "Turn the laser off."),
    _tool("propose_done", "Say the current recipe step looks finished. The app verifies it and advances if appropriate."),
    _tool("show_image", "Show the picture of recipe step N (1 = first step) on the dashboard and phone; 0 = back to the "
          "current step. Use when the user asks to see a step or what it should look like.",
          {"step": {"type": "integer"}}),
    _tool("show_text", "Show a short status line (max 20 chars) on the wearable's display.",
          {"text": {"type": "string"}}),
    _tool("point_sequence", "Show several visible objects one after another with the laser, about 2 seconds each "
          "(\"what do I need for this step?\"). The laser stays on the last one. Use point_at for a single object.",
          {"names": {"type": "array", "items": {"type": "string"},
                     "description": "visible recipe object names, in the order you mention them"}}),
    _tool("go_to_step", "Move to another recipe step: back (\"go back\", \"previous step\", \"back to the onion\") or "
          "forward (\"skip this step\"). For \"I'm done\" use propose_done instead.",
          {"step": {"type": "integer", "description": "recipe step number, 1 = first"}}),
    _tool("set_timer", "Start a kitchen timer. The app announces when it's done (also while paused). Setting a label "
          "that is already running restarts it.",
          {"seconds": {"type": "integer", "description": "duration in seconds, e.g. 300 for five minutes"},
           "label": {"type": "string", "description": "one or two words for what it's for, e.g. \"onions\"; \"\" if none"}}),
    _tool("cancel_timer", "Cancel a running timer.",
          {"label": {"type": "string", "description": "the timer's label; \"\" cancels all timers"}}),
    _tool("remember", "Remember a fact about this cook for the rest of the session, e.g. \"using lemon instead of "
          "lime\", \"no cilantro\", \"allergic to onion\", \"likes it spicy\". It appears in every state line as a cook note.",
          {"fact": {"type": "string", "description": "one short fact, under 15 words"}}),
    _tool("set_servings", "Scale the recipe to a number of servings (people). The state line then lists the amounts "
          "for every ingredient. Use the recipe's own number to go back to the original amounts.",
          {"servings": {"type": "integer"}}),
    _tool("pause_session", "Pause the session because the cook has to step away: the coach stays quiet, the laser goes "
          "off, the step is kept. They come back by saying \"I'm back\" or \"resume\"."),
    _tool("resume_session", "Resume a paused session (only when the state line says PAUSED and the cook wants to "
          "carry on)."),
]

MAX_TOKENS = 4096          # deliberately short spoken replies; thinking tokens count against this
KEEP_TURNS = 6             # history: current step + at least this many recent turns
MIN_SENTENCE_WORDS = 4     # "Okay." alone is not worth a TTS call
FIRST_TOKEN_TIMEOUT_S = 3.0   # no first text/tool call by then -> give up and speak the canned line
CLIENT_TIMEOUT_S = 15.0       # hard cap per request, so a dead connection can't hang a turn
REPEAT_FALLBACK = "Sorry, give me a second."   # the 2nd+ failure on the same step
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept server-side refusal fallbacks (Haiku 5.5 does not).
FAST_MODE_MODELS = {"claude-opus-5-5", "claude-fable-5-2"}   # `speed="fast"` on any other model is a 400
FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
MAX_HISTORY_TURNS = 24     # hard cap, even inside one long step (keeps the uncached tail and the bill bounded)
POINT_DWELL_S = 2.0        # point_sequence: how long the laser stays on each object
MAX_COOK_NOTES = 8
HEARD_EARLIER = 3          # "cook said earlier": at most this many utterances that fell out of the history


# ───────────────────────── sentence splitter ─────────────────────────

_BOUNDARY = re.compile(r"[.!?]+[\"')\]]*\s")
_ABBREVIATIONS = ("e.g.", "i.e.", "vs.", "approx.", "mr.", "mrs.", "ms.", "dr.")


class SentenceSplitter:
    """Turns a stream of text chunks into spoken-length sentences.

    Cuts after . ! ? followed by whitespace, but never inside "1.5" / "e.g." and never produces a sentence
    shorter than MIN_SENTENCE_WORDS (a short one is merged into the next). `flush()` returns whatever is
    left at the end of the stream, however short.
    """

    def __init__(self, min_words: int = MIN_SENTENCE_WORDS):
        self.min_words = min_words
        self._buf = ""

    def feed(self, chunk: str) -> list[str]:
        self._buf += chunk
        out, start, pos = [], 0, 0
        while True:
            m = _BOUNDARY.search(self._buf, pos)
            if not m:
                break
            candidate = self._buf[start:m.end()]
            pos = m.end()
            if candidate.rstrip().lower().endswith(_ABBREVIATIONS):
                continue
            if len(candidate.split()) < self.min_words:
                continue                    # too short: keep going, merge with the next sentence
            out.append(candidate.strip())
            start = m.end()
        self._buf = self._buf[start:]
        return out

    def flush(self) -> str | None:
        tail, self._buf = self._buf.strip(), ""
        return tail or None

    def discard(self) -> None:
        self._buf = ""


# ───────────────────────── tools ─────────────────────────

_SCHEMAS = {t["name"]: t["input_schema"] for t in TOOLS}
_PY_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list}


def _type_problem(key: str, val, spec: dict) -> str | None:
    want = _PY_TYPES.get(spec.get("type"))
    if want is not None and (not isinstance(val, want) or (isinstance(val, bool) and spec.get("type") != "boolean")):
        return f"{key!r} must be a{'n' if spec['type'][0] in 'aeiou' else ''} {spec['type']}"
    if "enum" in spec and val not in spec["enum"]:
        return f"{key!r} must be one of {spec['enum']}"
    if spec.get("type") == "array" and "items" in spec:
        for item in val:
            if (problem := _type_problem(f"{key}[]", item, spec["items"])) is not None:
                return problem
    return None


def validate_tool_input(name: str, inp) -> str | None:
    """None if `inp` is a valid input for tool `name`, else a short reason. Needed because eager input
    streaming means the API no longer validates tool input for us (it can be truncated or malformed)."""
    schema = _SCHEMAS.get(name)
    if schema is None:
        return f"unknown tool {name!r}"
    if not isinstance(inp, dict):
        return "input is not an object"
    props = schema["properties"]
    for key in schema.get("required", []):
        if key not in inp:
            return f"missing {key!r}"
    for key, val in inp.items():
        if key not in props:
            return f"unexpected {key!r}"
        if (problem := _type_problem(key, val, props[key])) is not None:
            return problem
    return None


def make_handlers(state: State, aimer, steps, link, *, timers=None, session=None) -> dict[str, Callable[..., str]]:
    """Tool name -> callable(**input) -> short result text. Built from the Aimer, Steps and ControlLink interfaces.

    `timers` is a timers.Timers. `session` is the Session, or a zero-arg callable returning it (it is built after the
    agent in voice mode). Without them those tools answer with an error the model can relay."""
    recipe = state.recipe
    seq: dict = {"task": None}          # the running point_sequence walk, if any

    def _stop_sequence() -> None:
        task, seq["task"] = seq["task"], None
        if task is not None and not task.done():
            task.cancel()

    def _point(name: str) -> str:
        result = aimer.point_at(name)
        if state.turns:
            state.turns[-1].mark(LAT_POINT)
        state.log("agent", f"pointed at {name}" if result == "ok" else f"couldn't point at {name}: {result}")
        return result

    def point_at(name: str) -> str:
        _stop_sequence()
        return _point(name)

    async def _walk(names: list[str]) -> None:
        for prev, nxt in zip(names, names[1:]):
            await asyncio.sleep(POINT_DWELL_S)
            if state.session == "paused" or state.target != prev:
                return                          # paused, stopped, or someone else moved the laser: stop the tour
            _point(nxt)

    def point_sequence(names: list[str]) -> str:
        _stop_sequence()
        names = [n for n in dict.fromkeys(" ".join(n.split()) for n in names) if n]
        unknown = [n for n in names if n not in recipe.objects]
        hidden = [n for n in names if n in recipe.objects and n not in state.visible()]
        show = [n for n in names if n in recipe.objects and n in state.visible()]
        notes = ([f"not visible: {', '.join(hidden)}"] if hidden else []) + (
            [f"not recipe objects: {', '.join(unknown)}"] if unknown else [])
        if not show:
            aimer.laser_off()
            return "not_visible" + (f" ({'; '.join(notes)})" if notes else "")
        _point(show[0])
        if len(show) > 1:
            try:
                seq["task"] = asyncio.get_running_loop().create_task(_walk(show))
            except RuntimeError:                # no event loop (a plain unit test): just the first one
                pass
        return f"ok: showing {', '.join(show)}" + (f"; {'; '.join(notes)}" if notes else "")

    def laser_off() -> str:
        _stop_sequence()
        aimer.laser_off()
        return "ok"

    def propose_done() -> str:
        verdict = steps.propose_done("llm")
        state.log("agent", f"proposed step done -> {verdict}")
        reason = getattr(steps, "reason", "")       # set by Steps on a push_back, e.g. "still whole avocados"
        return f"{verdict}: {reason}" if reason and verdict == "push_back" else verdict

    def go_to_step(step: int) -> str:
        total = len(recipe.steps)
        if not 1 <= step <= total:
            return f"error: there is no step {step}; the recipe has steps 1 to {total}"
        go = getattr(steps, "go_to", None)
        if go is None:
            return "error: this step tracker can't jump between steps"
        result = go(step - 1, "agent")
        if result != "ok":
            return result
        _stop_sequence()
        aimer.laser_off()                       # the target belonged to the old step
        s = recipe.steps[step - 1]
        return f"ok: now on step {step} ({s.short}): {s.goal}"

    def show_text(text: str) -> str:
        link.show_text(TFT_AGENT, text[:20])
        return "ok"

    def show_image(step: int) -> str:
        total = len(recipe.steps)
        if step == 0:
            state.shown_image = None
            return "ok: showing the current step"
        if not 1 <= step <= total:
            return f"error: there is no step {step}; the recipe has steps 1 to {total}"
        state.shown_image = (step - 1, state.step)      # follows the cook again as soon as the step changes
        state.log("agent", f"showed the picture for step {step} ({recipe.steps[step - 1].short})")
        return f"ok: showing step {step}, {recipe.steps[step - 1].short}"

    def set_timer(seconds: int, label: str) -> str:
        return timers.set(seconds, label) if timers is not None else "error: timers aren't available here"

    def cancel_timer(label: str) -> str:
        return timers.cancel(label) if timers is not None else "error: timers aren't available here"

    def remember(fact: str) -> str:
        fact = " ".join(fact.split()).rstrip(".")[:120]
        if not fact:
            return "error: nothing to remember"
        notes = state.cook_notes
        if fact.lower() in (n.lower() for n in notes):
            return "ok: already noted"
        notes.append(fact)
        del notes[:-MAX_COOK_NOTES]                     # oldest out
        state.log("agent", f"noted: {fact}")
        return "ok: noted"

    def set_servings(servings: int) -> str:
        if not recipe.servings or not recipe.ingredients:
            return "error: this recipe has no amounts to scale; estimate from the steps"
        if not 1 <= servings <= 50:
            return "error: servings must be between 1 and 50"
        state.servings = 0 if servings == recipe.servings else servings
        state.log("agent", f"servings set to {servings}")
        return f"ok: amounts for {servings}: {amounts(recipe, servings)}"

    def _session():
        return session() if callable(session) else session

    def pause_session() -> str:
        sess = _session()
        if sess is None or not hasattr(sess, "pause_now"):
            return "error: pausing isn't available here"
        _stop_sequence()
        if sess.pause_now("agent"):
            return "ok: paused; you stay quiet until they say they're back"
        return f"not paused: the session is {state.session}"

    def resume_session() -> str:
        sess = _session()
        if sess is None or not hasattr(sess, "resume_now"):
            return "error: resuming isn't available here"
        return "ok: resumed" if sess.resume_now("agent") else f"not resumed: the session is {state.session}"

    return {"point_at": point_at, "laser_off": laser_off, "propose_done": propose_done, "show_text": show_text,
            "show_image": show_image, "point_sequence": point_sequence, "go_to_step": go_to_step,
            "set_timer": set_timer, "cancel_timer": cancel_timer, "remember": remember,
            "set_servings": set_servings, "pause_session": pause_session, "resume_session": resume_session}


# ───────────────────────── helpers ─────────────────────────

def jpeg_block(frame, max_px: int) -> dict:
    import cv2
    h, w = frame.shape[:2]
    s = max_px / max(h, w)
    if s < 1:
        frame = cv2.resize(frame, (int(w * s), int(h * s)))
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buf.tobytes()).decode()}}


def build_system(recipe: Recipe | dict) -> list[dict]:
    """Stable, cacheable system prefix: instructions + the whole recipe as numbered text (context.render_recipe).
    Built once per app run and never edited: any change here would invalidate every cached turn."""
    body = (render_recipe(recipe) if isinstance(recipe, Recipe)
            else "RECIPE:\n" + json.dumps(recipe, ensure_ascii=False, sort_keys=True))
    return [{"type": "text", "text": SYSTEM + "\n\n" + body, "cache_control": {"type": "ephemeral"}}]


@dataclass
class _Exchange:
    step: int           # recipe step when the turn started
    user: dict
    assistant: dict
    said: str = ""      # the cook's own words this turn ("" for check-ins and app prompts)


@dataclass
class _Turn:
    """Everything one turn produces. Lives outside the streaming task so it survives cancel()."""
    user: dict
    step: int
    checkin: bool
    had_pending: bool
    greeting: bool = False                                  # the session-start turn ("hey Remy")
    said: str = ""                                          # the cook's own words (no app prompts)
    stats: TurnStats = field(default_factory=TurnStats)
    spoken: list[str] = field(default_factory=list)       # sentences handed to on_sentence
    tool_uses: list[dict] = field(default_factory=list)   # tool_use blocks (for the assistant message)
    results: list[dict] = field(default_factory=list)     # tool_result blocks (for the NEXT user message)
    notices: list[str] = field(default_factory=list)      # failed tool calls the next state note mentions
    handled: set = field(default_factory=set)
    stop_reason: str | None = None


# What to say when the model called only this tool and said nothing (see Agent._backstop).
_BACKSTOP_LINES = {"set_timer": "Okay, timer's running.", "cancel_timer": "Okay, timer cancelled.",
                   "remember": "Got it, I'll keep that in mind.", "set_servings": "Okay, I've scaled the amounts.",
                   "pause_session": "Sure, I'll wait. Say I'm back when you're ready.",
                   "resume_session": "Welcome back."}

# Sentences that talk ABOUT the instructions instead of to the cook ("In plain mode I give one action...",
# "The user is holding the avocado..."): the model occasionally thinks out loud. Never speak them.
_META = re.compile(r"\b(the user|plain mode|plain-language mode|system prompt|my instructions|the app says)\b", re.I)

_PLAIN_ON = ("say it simpler", "say that simpler", "simpler words", "simpler please", "too fast", "slow down",
             "slower please", "explain it simply", "keep it simple", "use simple words")
_PLAIN_OFF = ("back to normal", "normal mode", "you can use bigger words", "more detail please")
_TALK_LESS = ("talk less", "be quieter", "fewer comments", "stop commenting", "don't interrupt", "dont interrupt",
              "say less")
_TALK_MORE = ("talk more", "say more", "more comments", "check in more")


_CAM_OFF_WORDS = ("ignore", "forget", "don't use", "dont use", "do not use", "stop using", "stop mentioning",
                  "stop saying", "stop talking about", "don't mention", "dont mention", "don't worry about",
                  "dont worry about", "never mind", "turn off", "switch off", "without", "not using", "skip")
_CAM_ON_WORDS = ("use the camera", "use it again", "look again", "back on", "turn on", "switch on", "uncovered",
                 "look now", "see now", "start looking", "use camera")


def _camera_preference(text: str) -> bool | None:
    """True = the user wants the camera ignored, False = wants it used again, None = not about the camera."""
    t = " " + re.sub(r"[^a-z' ]+", " ", text.lower()) + " "
    t = re.sub(r"\s+", " ", t)
    if " stop looking " in t or " don't look " in t or " dont look " in t:
        return True
    if " you can look now " in t or " you can see now " in t or " look again " in t:
        return False
    if "camera" in t and any(w in t for w in _CAM_OFF_WORDS):
        return True
    if "camera" in t and any(w in t for w in _CAM_ON_WORDS):
        return False
    return None


def _user_words(text: str) -> str:
    """The cook's own words in an agent turn's text: what follows "User: " in an app prompt ("[session start ...]
    User: let's make guac"), nothing for a bare app prompt, else all of it."""
    if "User: " in text:
        return text.rsplit("User: ", 1)[1].strip()
    return "" if text.startswith("[") else text.strip()


def _has(text: str, phrases) -> bool:
    t = " " + re.sub(r"[^a-z' ]+", " ", text.lower()) + " "
    t = re.sub(r"\s+", " ", t)
    return any(f" {p} " in t for p in phrases)


def _missing_credentials(e: BaseException) -> bool:
    """The SDK raises a bare TypeError (not an APIError) when no API key / token is configured."""
    return isinstance(e, TypeError) and "authentication" in str(e).lower()


def _describe(e: BaseException) -> str:
    if isinstance(e, TimeoutError):
        return "timeout: no first token in time"
    if _missing_credentials(e):
        return "no Anthropic credentials (set ANTHROPIC_API_KEY)"
    return f"{type(e).__name__}: {e}"


# ───────────────────────── the agent ─────────────────────────

class Agent:
    """AgentAPI. `handlers` is tool name -> callable(**input) -> str (see make_handlers)."""

    def __init__(self, cfg, state: State, handlers: dict[str, Callable[..., str]], client=None):
        self.cfg, self.state, self.handlers = cfg, state, handlers
        # One client for the app's lifetime (keep-alive). Short timeout + 1 retry: a voice turn that
        # takes 30 s is worse than the canned fallback line.
        self.client = client or anthropic.AsyncAnthropic(timeout=CLIENT_TIMEOUT_S, max_retries=1)
        self.first_token_timeout = getattr(cfg, "first_token_timeout_s", FIRST_TOKEN_TIMEOUT_S)
        self.system = build_system(state.recipe)
        self.busy = False
        self._exchanges: list[_Exchange] = []
        self._pending: list[dict] = []     # tool_results owed to the next user message
        self._notices: list[str] = []
        self._run: asyncio.Future | None = None
        self._cancelled = False
        self._fallback_steps: set[int] = set()     # steps whose canned line we've already spoken
        self._camera_told = False                  # told the user the camera is offline (once per outage)
        self.recent_sentences: deque[str] = deque(maxlen=3)   # what the coach last said (dashboard captions)
        self.plain = bool(getattr(cfg, "plain_language", False))   # plain-language mode (accessibility)
        self.camera_ignored = False                # "ignore the camera": no images, never mention it, watcher off
        self.checkins = None                       # CheckIns, set by main, so "talk less" can change its level
        self._positions: dict[str, str] = {}       # object -> "left, near" for the frames of the current turn
        self.last_usage = None                     # token usage of the last request (cache_read_input_tokens ...)
        self._heard: deque[tuple[float, str, int]] = deque(maxlen=12)   # (time.time(), the cook's words, step)
        self._events_seen = 0                      # State.events index at the last turn start

    # ----- public API -----

    async def turn(self, user_text: str, frames: list, on_sentence: OnSentence) -> TurnStats:
        """One voice turn. Awaits `on_sentence(s)` for each complete sentence as soon as it exists."""
        self.busy, self._cancelled = True, False
        t0 = time.monotonic()
        ts = self._begin(user_text, frames)
        self._run = asyncio.ensure_future(self._stream(ts, on_sentence, t0))
        try:
            await self._run
        except asyncio.CancelledError:
            ts.stats.cancelled = True
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise                      # the caller cancelled the whole task: let it unwind
        except (anthropic.APIError, TimeoutError, TypeError) as e:
            if isinstance(e, TypeError) and not _missing_credentials(e):
                raise                              # a real bug, not an API problem
            await self._failed(ts, on_sentence, _describe(e))
        else:
            if ts.stop_reason == "refusal" and not ts.spoken:
                await self._failed(ts, on_sentence, "refusal")
            elif not ts.spoken and ts.tool_uses and not ts.checkin:
                await self._backstop(ts, on_sentence)
        finally:
            ts.stats.total_s = time.monotonic() - t0
            self._commit(ts)
            self._run, self.busy = None, False
        return ts.stats

    def cancel(self) -> None:
        """Barge-in: abort the HTTP stream now. No more on_sentence calls after this returns."""
        self._cancelled = True
        if self._run is not None and not self._run.done():
            self._run.cancel()

    def reset(self) -> None:
        """Forget the conversation (session reset / 'start over')."""
        self.cancel()
        self._exchanges.clear()
        self._pending, self._notices = [], []
        self._heard.clear()
        self._fallback_steps.clear()

    async def prewarm(self) -> None:
        """Write the prompt cache now (max_tokens=0: prefill only, no output billed) so the first real
        turn hits a warm cache. Also warms the backup model, whose cache is separate, so a retry isn't cold.
        Never raises: a failed pre-warm just means a cold first turn."""
        models = [self.cfg.model] + ([self._backup_model()] if self._backup_model() else [])
        await asyncio.gather(*(self._prewarm_model(m) for m in models))

    async def _prewarm_model(self, model: str) -> None:
        kw = dict(model=model, max_tokens=0, output_config={"effort": self.cfg.effort},
                  system=self.system, tools=TOOLS, messages=[{"role": "user", "content": "warmup"}])
        if self._fast(model):
            kw["speed"] = "fast"                    # a different speed is a different cache: warm the one we use
        try:
            try:
                resp = await asyncio.wait_for(self.client.messages.create(**kw), timeout=15)
            except anthropic.BadRequestError:
                # in case the API rejects the streaming-only tool flag on a non-streaming request
                kw["tools"] = [{k: v for k, v in t.items() if k != "eager_input_streaming"} for t in TOOLS]
                resp = await asyncio.wait_for(self.client.messages.create(**kw), timeout=15)
            u = resp.usage
            log.info("prewarm ok (%s): cache_creation=%s cache_read=%s", model,
                     getattr(u, "cache_creation_input_tokens", None), getattr(u, "cache_read_input_tokens", None))
        except Exception as e:
            if _missing_credentials(e):
                log.error("No Anthropic credentials (ANTHROPIC_API_KEY in laptop/.env): "
                          "the coach will only speak the canned step lines.")
            else:
                log.warning("prewarm of %s failed (first turn will be cold): %s: %s", model, type(e).__name__, e)

    # ----- turn plumbing -----

    def _note(self, frames: list | None = None) -> str:
        """The [state ...] line: context.state_note() + what only the agent knows (its last sentence, failed tools,
        what the cook said that has scrolled out of the kept history)."""
        self._positions, size = {}, None
        if frames and not self.camera_ignored:  # say where things are, not just that they are visible
            h, w = frames[-1].shape[:2]
            size = (w, h)
            self._positions = positions(self.state.dets, w, h)
        camera = "ignored" if self.camera_ignored else ("ok" if frames else "offline")
        kept = {e.said for e in self._exchanges if e.said}
        earlier = [(t, w) for t, w, _ in self._heard if w not in kept][-HEARD_EARLIER:]
        note = state_note(self.state, frame_size=size, camera=camera, heard_earlier=earlier,
                          questions_on_step=self._questions_on_step())
        if self.recent_sentences:
            note += f' | you last said: "{self.recent_sentences[-1][:120]}"'
        if self._notices:
            note += " | last turn: " + "; ".join(self._notices)
        return note

    def _questions_on_step(self) -> int:
        """Questions the cook asked since the current step started (a hint that they may be stuck)."""
        since = time.time() - self.state.step_seconds()
        return sum(1 for t, w, step in self._heard if step == self.state.step and t >= since and is_question(w))

    def _moved_since_last_turn(self) -> bool:
        """A deliberate jump (go_to_step) since the last turn, as opposed to the recipe starting over."""
        return any("moved from step" in e.text for e in self.state.events[self._events_seen:])

    def _apply_preferences(self, text: str) -> list[str]:
        """"say it simpler", "talk less" ...: change the setting locally and tell the model, so it confirms."""
        changes = []
        if _has(text, _PLAIN_ON) and not self.plain:
            self.plain = True
            changes.append("plain language is now ON")
        elif _has(text, _PLAIN_OFF) and self.plain:
            self.plain = False
            changes.append("plain language is now OFF")
        cam = _camera_preference(text)
        if cam is True and not self.camera_ignored:
            self.camera_ignored = True
            changes.append("the user asked you to IGNORE the camera: never mention it again, don't ask them to move it")
        elif cam is False and self.camera_ignored:
            self.camera_ignored = False
            changes.append("the camera is back in use: you can look at the frames again")
        ci = self.checkins
        if ci is not None:
            order = ["off", "gentle", "normal"]
            if _has(text, _TALK_LESS) and ci.level != "off":
                ci.set_level(order[order.index(ci.level) - 1])
                changes.append(f"you will now speak up on your own less often (check-ins: {ci.level})")
            elif _has(text, _TALK_MORE) and ci.level != "normal":
                ci.set_level(order[order.index(ci.level) + 1])
                changes.append(f"you will now speak up on your own more often (check-ins: {ci.level})")
        return changes

    def _begin(self, user_text: str, frames: list) -> _Turn:
        step = self.state.step
        if self._exchanges and step < self._exchanges[-1].step and not self._moved_since_last_turn():
            self._exchanges.clear()                                  # session restarted
            self._pending, self._notices = [], []
            self._heard.clear()
            self.camera_ignored = False                              # a new session starts with the camera in use
        self._events_seen = len(self.state.events)
        checkin = user_text.startswith("[check-in")
        said = "" if checkin else _user_words(user_text)
        body = user_text if checkin or user_text.startswith("[") else f"User: {user_text}"
        changes = [] if checkin else self._apply_preferences(user_text)
        if self.camera_ignored:
            frames = []                     # the user said to ignore the camera: no images at all
        self._trim(step)                    # first, so "cook said earlier" knows what the history still holds
        note = self._note(frames)
        if self.camera_ignored:
            note += " | camera: IGNORED at the user's request (never mention it; go by the recipe and what they say)"
        elif frames:
            self._camera_told = False
        else:                               # no camera: say so (once per outage) and send no images
            note += " | camera: OFFLINE, no images" + (" (user already told)" if self._camera_told else " (tell the user once)")
            self._camera_told = self._camera_told or not checkin
        if said:
            self._heard.append((time.time(), said, step))
        extra = ("[mode: plain]\n" if self.plain else "") + "".join(f"[setting changed: {c}]\n" for c in changes)
        content = list(self._pending)
        content += [jpeg_block(f, self.cfg.frame_max_px) for f in frames]
        content.append({"type": "text", "text": f"[{note}]\n{extra}{body}"})
        return _Turn(user={"role": "user", "content": content}, step=step, checkin=checkin,
                     had_pending=bool(self._pending), greeting=user_text.startswith("[session start"), said=said)

    def _trim(self, step: int) -> None:
        """Keep the current step's turns + at least KEEP_TURNS recent ones (at most MAX_HISTORY_TURNS). Only ever
        drops from the FRONT, so the kept history stays a byte-identical prefix (cache hits, valid tool pairs)."""
        ex = self._exchanges
        first_current = len(ex)             # start of the trailing run of turns on this step
        while first_current > 0 and ex[first_current - 1].step == step:
            first_current -= 1
        start = max(min(first_current, len(ex) - KEEP_TURNS), len(ex) - MAX_HISTORY_TURNS, 0)
        if start:
            del ex[:start]
        if ex:   # its tool_results answered a tool_use we just dropped (or never existed): remove them
            ex[0].user["content"] = [b for b in ex[0].user["content"] if b.get("type") != "tool_result"]

    def _messages(self, ts: _Turn) -> list[dict]:
        """Trimmed history + this turn's user message. A cache breakpoint goes on the last block of the history (the
        previous reply): everything before it is byte-identical to the last request, so the next request reads it
        from the cache. The new user message (images, state note) is after it: those change every turn."""
        self._trim(ts.step)
        msgs = []
        for e in self._exchanges:
            msgs += [e.user, e.assistant]
        if msgs:
            last = msgs[-1]
            blocks = list(last["content"])
            blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
            msgs[-1] = {**last, "content": blocks}       # a copy: the stored history never carries the marker
        return msgs + [ts.user]

    def _params(self, messages: list[dict], model: str | None = None) -> dict:
        model = model or self.cfg.model
        p = dict(model=model, max_tokens=MAX_TOKENS, output_config={"effort": self.cfg.effort},
                 system=self.system, tools=TOOLS, messages=messages)
        if model in FALLBACK_MODELS:
            p.update(betas=[FALLBACK_BETA], fallbacks="default")    # re-run on a fallback model on refusal
        if self._fast(model):
            p["speed"] = "fast"                                     # Opus fast mode (CLAUDE_FAST=1)
        return p

    def _fast(self, model: str | None = None) -> bool:
        return bool(getattr(self.cfg, "fast", False)) and (model or self.cfg.model) in FAST_MODE_MODELS

    def _backup_model(self) -> str | None:
        """The faster model to retry on when the main one is too slow (None = retries off)."""
        backup = (getattr(self.cfg, "backup_model", "") or "").strip()
        return backup if backup and backup != self.cfg.model else None

    async def _stream(self, ts: _Turn, on_sentence: OnSentence, t0: float) -> None:
        messages = copy.copy(self._messages(ts))        # trimmed once, so a retry sends exactly the same history
        try:
            await self._stream_once(ts, on_sentence, t0, messages, self.cfg.model, self.first_token_timeout)
        except TimeoutError:
            # Nothing was said and no tool ran (the deadline only guards the wait for the first token), so it is
            # safe to ask again. One retry, on the faster model, with its own short deadline.
            backup = self._backup_model()
            if backup is None:
                raise
            log.warning("%s gave no first token in %.1fs: retrying once on %s", self.cfg.model,
                        self.first_token_timeout, backup)
            self.state.log("system", f"agent: {self.cfg.model} too slow, retrying on {backup}")
            await self._stream_once(ts, on_sentence, t0, messages, backup, getattr(self.cfg, "backup_timeout_s", 3.0))
            self.state.log("system", f"agent: answered by {backup}")

    async def _stream_once(self, ts: _Turn, on_sentence: OnSentence, t0: float, messages: list[dict],
                           model: str, timeout: float) -> None:
        splitter = SentenceSplitter()
        # No first text / tool call within `timeout` -> TimeoutError (-> backup model, then the canned line).
        async with asyncio.timeout(timeout) as deadline:
            async with self.client.beta.messages.stream(**self._params(messages, model)) as stream:
                async for event in stream:
                    if event.type == "text":
                        if ts.stats.ttft_s is None:
                            ts.stats.ttft_s = time.monotonic() - t0
                            deadline.reschedule(None)
                            if self.state.turns and not ts.checkin:
                                self.state.turns[-1].mark(LAT_FIRST_TOKEN)
                        for s in splitter.feed(event.text):
                            await self._emit(ts, s, on_sentence)
                    elif event.type == "content_block_stop":
                        block = event.content_block
                        if block.type == "tool_use":
                            deadline.reschedule(None)
                            self._run_tool(ts, block.id, block.name, block.input)
                        elif block.type == "text" and (tail := splitter.flush()):
                            await self._emit(ts, tail, on_sentence)     # don't hold the last sentence back
                if tail := splitter.flush():
                    await self._emit(ts, tail, on_sentence)
                final = await stream.get_final_message()
                self.last_usage = getattr(final, "usage", None)
        for block in final.content:        # anything whose stop event we didn't see
            if block.type == "tool_use":
                self._run_tool(ts, block.id, block.name, block.input)
        ts.stop_reason = final.stop_reason
        if final.stop_reason in ("refusal", "max_tokens"):
            log.warning("turn ended with stop_reason=%s", final.stop_reason)

    async def _emit(self, ts: _Turn, sentence: str, on_sentence: OnSentence) -> None:
        sentence = sentence.strip()
        if not sentence or self._cancelled:
            return
        if ts.checkin and sentence.strip(".! ").upper() == "SKIP":
            return
        if _META.search(sentence):
            log.warning("dropped a sentence that talks about the instructions: %r", sentence)
            return
        ts.spoken.append(sentence)
        self.recent_sentences.append(sentence)
        ts.stats.sentences += 1
        await on_sentence(sentence)

    async def _failed(self, ts: _Turn, on_sentence: OnSentence, error: str) -> None:
        """The API was too slow, down, refused, or has no credentials. Record it, and unless the user already
        heard something (or this was a silent check-in) speak the step's canned line instead of going quiet.
        The canned line is spoken once per step; later failures on the same step say a short apology."""
        ts.stats.error = error
        log.warning("agent turn failed: %s", error)
        self.state.log("system", f"agent fallback: {error}")
        if ts.spoken or ts.checkin:
            return
        step = self.state.step              # not ts.step: the intent router may have advanced it meanwhile
        line = REPEAT_FALLBACK if step in self._fallback_steps else self.state.current_step.fallback
        self._fallback_steps.add(step)
        try:
            await self._emit(ts, line, on_sentence)
        except Exception:
            log.exception("could not speak the fallback line")

    def _recent_pushback(self) -> str | None:
        """Why the watcher disagreed with a "done" the intent router already handled (and nobody has told the user)."""
        now = time.time()
        for e in reversed(self.state.events[-6:]):
            if e.source == "system" and "watcher disagrees: " in e.text and now - e.t < 15:
                return e.text.split("watcher disagrees: ", 1)[1].strip().rstrip(".")
        return None

    async def _backstop(self, ts: _Turn, on_sentence: OnSentence) -> None:
        """The model answered with tool calls only. The user only hears audio, so that is silence: say a short
        line that fits the situation. (The prompt forbids this and Haiku obeys; Opus still does it ~1 in 4.)
        Never repeats a line the coach just said: repeating itself is worse than a plain step line."""
        uses = {u["id"]: u for u in ts.tool_uses}
        line = None
        pushback = self._recent_pushback()
        step = self.state.current_step
        if pushback:                                   # "I'm done" was pushed back by the router: say why
            line = f"Hold on, {pushback}."
        elif ts.greeting:                              # the "hey Remy" greeting: introduce yourself and step 1
            line = f"Hi, I'm Remy. {step.fallback}"
        for r in ts.results:
            if line is not None:
                break
            use = uses.get(r["tool_use_id"], {})
            result, obj = str(r.get("content", "")), use.get("input", {}).get("name")
            ok = result.startswith("ok")
            if use.get("name") == "go_to_step" and ok:
                line = f"Okay, step {self.state.step + 1}. {step.fallback}"
            elif use.get("name") in _BACKSTOP_LINES and ok:
                line = _BACKSTOP_LINES[use["name"]]
            elif use.get("name") == "point_sequence" and ok:
                line = "Here's what you need."
            elif use.get("name") == "propose_done" and result.startswith("push_back"):
                reason = result.partition(": ")[2]
                line = f"Hold on, {reason}." if reason else "Hold on, that doesn't look finished yet."
            elif use.get("name") == "propose_done" and result == "advance":
                line = f"Nice work. {self.state.current_step.fallback}"          # state.step already moved on
            elif use.get("name") == "propose_done" and result == "finished":
                line = f"That's it, you did it! Enjoy your {self.state.recipe.name.lower()}."
            elif use.get("name") == "point_at" and obj:
                where = self._positions.get(obj, "").split(",")[0]       # "left" / "centre" / "right"
                here = f"Here's the {obj}" + (f", on your {where}." if where in ("left", "right") else
                                              ", in the middle." if where == "centre" else ".")
                line = here if result == "ok" else (f"I can't see the {obj} right now." if result == "not_visible" else
                                                    f"I can see the {obj}, but the laser can't reach it. Slide it a bit closer to the middle?"
                                                    if result == "out_of_reach" else line)
        if line is None:                               # nothing to describe: at least say where they are
            line = f"You're on step {self.state.step + 1}: {step.short}."
        said = {x.lower() for x in self.recent_sentences}
        if line.lower() in said:                       # just said that: walk through the step's own instructions
            options = [step.fallback] + [a[:1].upper() + a[1:] + "." for a in step.how]
            line = next((o for o in options if o.lower() not in said), None)
        if line is None:
            log.info("silent turn (tools only: %s): nothing new to say, staying quiet", ts.stats.tools)
            return
        log.info("silent turn (tools only: %s): speaking %r", ts.stats.tools, line)
        await self._emit(ts, line, on_sentence)

    def _run_tool(self, ts: _Turn, tool_id: str, name: str, inp) -> None:
        """Run one tool as soon as its input is complete. The result is deferred to the next user turn."""
        if tool_id in ts.handled:
            return
        ts.handled.add(tool_id)
        ts.stats.tools.append(name)
        ts.tool_uses.append({"type": "tool_use", "id": tool_id, "name": name,
                             "input": inp if isinstance(inp, dict) else {}})
        result = {"type": "tool_result", "tool_use_id": tool_id}
        problem = validate_tool_input(name, inp) or (None if name in self.handlers else f"no handler for {name!r}")
        if problem:
            result.update(content=f"error: {problem}", is_error=True)
            ts.notices.append(f"{name} failed ({problem})")
        else:
            try:
                out = str(self.handlers[name](**inp) or "ok")
                result["content"] = out
                if out in ("not_visible", "unknown_object", "out_of_reach"):
                    arg = next(iter(inp.values()), "")
                    ts.notices.append(f"{name}({arg}) -> {out}")
            except Exception as e:          # report it to the model instead of dropping the result
                log.exception("tool %s failed", name)
                result.update(content=f"error: {e}", is_error=True)
                ts.notices.append(f"{name} failed ({e})")
        ts.results.append(result)

    def _commit(self, ts: _Turn) -> None:
        """Record the finished (or cancelled / failed) turn in history, keeping it API-valid."""
        text = " ".join(ts.spoken)
        produced = bool(ts.spoken or ts.tool_uses)
        if not produced or (ts.checkin and not ts.tool_uses and not ts.had_pending):
            return                          # nothing to remember; any owed tool_results stay pending
        content = ([{"type": "text", "text": text}] if text else []) + ts.tool_uses
        # images are the main cost of old turns, and the next turn sends fresh frames anyway
        user = {"role": "user", "content": [b for b in ts.user["content"] if b.get("type") != "image"]}
        self._exchanges.append(_Exchange(ts.step, user, {"role": "assistant", "content": content}, ts.said))
        self._pending = ts.results
        self._notices = ts.notices
