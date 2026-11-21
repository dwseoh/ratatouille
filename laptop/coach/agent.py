"""Claude agent for voice turns (contract C6): streams text sentence by sentence, calls local tools.

Latency notes (see docs/architecture.md):
- the system prompt + recipe + tool list are a stable, cached prefix (`prewarm()` warms it at startup)
- only 2-3 small frames per turn; frames are dropped from history as soon as the turn ends
- tools run the moment their input is complete (mid-stream); their results ride along with the NEXT
  user turn, so a tool never costs a second round trip
- barge-in: `cancel()` aborts the HTTP stream; sentences not yet handed to TTS are discarded
- fallbacks (B5): no first token in FIRST_TOKEN_TIMEOUT_S, an API error, a refusal or missing credentials ->
  speak the step's canned `fallback` line (once per step; after that "Sorry, give me a second."), set
  `TurnStats.error` and log a system event. No camera frames -> the state note says "camera offline".

History rules (the Messages API is strict about these):
- every tool_use must be answered by a tool_result in the very next user message -> results are kept in
  `_pending` until then, and cancelled/failed turns still commit the tools they already ran
- history is trimmed to the current recipe step + the last KEEP_TURNS turns; trimming only drops from the
  FRONT, never edits the middle, and thinking blocks are never replayed (they can't be edited safely)
"""
from __future__ import annotations

import asyncio
import base64
import copy
import dataclasses
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import anthropic

from .contracts import LAT_FIRST_TOKEN, LAT_POINT, TFT_AGENT, OnSentence, State, TurnStats

log = logging.getLogger(__name__)

SYSTEM = """You are a hands-free cooking coach speaking through an earpiece while the user cooks.
You see what a chest camera sees. The user's hands are busy and they can't read a screen: they only hear you.

How to reply
- ALWAYS speak. Every reply to the user starts with 1-2 short spoken sentences, even when you also use a tool.
  A reply made only of tool calls is silence to the user, and silence is never right. Pointing commands get a
  spoken answer too ("Here's the knife, on your left."). The one exception is a [check-in message, below.
- Your first sentence answers what they asked. "What next?" -> name the next action; "where was I?" -> the current
  step and what comes next; a question about the food -> your honest read of the frames.
- Plain speech only: no lists, markdown or emoji. Never more than 2 sentences.
- Speak first, then call tools.
- When you point at something, also say where it is ("the lime, on your left") using the positions in the
  state line: left / centre / right, and near (close to the user) / far.
- "Where am I?" / "what was I doing?": give the current step number and name, what they last did (from the
  recent events), then the next action. The state line has what you last said.
- Never rush the user: no "quickly", "hurry" or "right away". They set the pace; a step only ends when they say so.
- If the message has a line "[mode: plain]", use plain language: sentences under 12 words, one action per
  sentence, no idioms, no numbers (say "about half the lime", "a pinch", "until it looks like this"), and always
  point at what you mean. Give only the next one or two actions, not the whole procedure, and never join two
  actions with "then" or "and": split them into separate short sentences.
  If it has "[setting changed: ...]", confirm that change in one short sentence.
- Every message starts with a [state ...] line from the app: the current step, which objects the camera
  currently sees, where the laser points, and recent events. Trust it for what is visible.

Tools
- point_at(name): shine the laser on an object. Only call it for objects the state line says are visible.
  If the user asks about something that isn't visible, say you can't see it instead of pointing.
- laser_off(): turn the laser off.
- propose_done(): call it only when the frames and the user's words show the current step is really
  finished. The app decides whether to advance. If you just asked whether they're ready to move on and they
  say yes, call it.
- show_text(text): up to 20 characters on the wearable's display.
If a tool result says not_visible or error, you may mention it briefly on your next turn.
If the state line says the camera is offline, you can't see anything: tell the user once, briefly, then carry on
from the recipe and what they tell you. Never guess what is on the counter.

If a message starts with [check-in, the app is asking whether to speak up unprompted: say one short
helpful sentence, or reply with just SKIP when there is nothing worth saying."""

TOOLS = [
    {
        "name": "point_at",
        "description": "Point the laser at a visible object. name must be one of the recipe objects.",
        "eager_input_streaming": True,
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}},
                         "required": ["name"], "additionalProperties": False},
    },
    {
        "name": "laser_off",
        "description": "Turn the laser off.",
        "eager_input_streaming": True,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "propose_done",
        "description": "Say the current recipe step looks finished. The app verifies it and advances if appropriate.",
        "eager_input_streaming": True,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "show_text",
        "description": "Show a short status line (max 20 chars) on the wearable's display.",
        "eager_input_streaming": True,
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}},
                         "required": ["text"], "additionalProperties": False},
    },
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
_PY_TYPES = {"string": str, "integer": int}


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
        want = _PY_TYPES.get(props[key].get("type"))
        if want is not None and (not isinstance(val, want) or isinstance(val, bool)):
            return f"{key!r} must be a {props[key]['type']}"
    return None


def make_handlers(state: State, aimer, steps, link) -> dict[str, Callable[..., str]]:
    """Tool name -> callable(**input) -> result text. Built from the C3/C7/C1 interfaces."""

    def point_at(name: str) -> str:
        result = aimer.point_at(name)
        if state.turns:
            state.turns[-1].mark(LAT_POINT)
        state.log("agent", f"pointed at {name}" if result == "ok" else f"couldn't point at {name}: {result}")
        return result

    def laser_off() -> str:
        aimer.laser_off()
        return "ok"

    def propose_done() -> str:
        verdict = steps.propose_done("llm")
        state.log("agent", f"proposed step done -> {verdict}")
        reason = getattr(steps, "reason", "")       # set by Steps on a push_back, e.g. "still whole avocados"
        return f"{verdict}: {reason}" if reason and verdict == "push_back" else verdict

    def show_text(text: str) -> str:
        link.show_text(TFT_AGENT, text[:20])
        return "ok"

    return {"point_at": point_at, "laser_off": laser_off, "propose_done": propose_done, "show_text": show_text}


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


def build_system(recipe: dict) -> list[dict]:
    """Stable, cacheable system prefix: instructions + the whole recipe."""
    return [{"type": "text", "text": SYSTEM + "\n\nRECIPE:\n" + json.dumps(recipe, ensure_ascii=False),
             "cache_control": {"type": "ephemeral"}}]


@dataclass
class _Exchange:
    step: int           # recipe step when the turn started
    user: dict
    assistant: dict


@dataclass
class _Turn:
    """Everything one turn produces. Lives outside the streaming task so it survives cancel()."""
    user: dict
    step: int
    checkin: bool
    had_pending: bool
    stats: TurnStats = field(default_factory=TurnStats)
    spoken: list[str] = field(default_factory=list)       # sentences handed to on_sentence
    tool_uses: list[dict] = field(default_factory=list)   # tool_use blocks (for the assistant message)
    results: list[dict] = field(default_factory=list)     # tool_result blocks (for the NEXT user message)
    notices: list[str] = field(default_factory=list)      # failed tool calls the next state note mentions
    handled: set = field(default_factory=set)
    stop_reason: str | None = None


def positions(dets, width: int, height: int) -> dict[str, str]:
    """Where each visible object is, in words: "left, near". Left/right are the user's (a chest camera looks the
    way they face); near = bottom half of the frame (close to them), far = top half. Best detection per name."""
    out: dict[str, str] = {}
    for d in sorted(dets, key=lambda d: -d.conf):
        if d.name in out or d.name == "hand":
            continue
        cx, cy = d.center
        horizontal = "left" if cx < width / 3 else "right" if cx > 2 * width / 3 else "centre"
        out[d.name] = f"{horizontal}, {'near' if cy > height / 2 else 'far'}"
    return out


_PLAIN_ON = ("say it simpler", "say that simpler", "simpler words", "simpler please", "too fast", "slow down",
             "slower please", "explain it simply", "keep it simple", "use simple words")
_PLAIN_OFF = ("back to normal", "normal mode", "you can use bigger words", "more detail please")
_TALK_LESS = ("talk less", "be quieter", "fewer comments", "stop commenting", "don't interrupt", "dont interrupt",
              "say less")
_TALK_MORE = ("talk more", "say more", "more comments", "check in more")


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
    """C6 AgentAPI. `handlers` is tool name -> callable(**input) -> str (see make_handlers)."""

    def __init__(self, cfg, state: State, handlers: dict[str, Callable[..., str]], client=None):
        self.cfg, self.state, self.handlers = cfg, state, handlers
        # One client for the app's lifetime (keep-alive). Short timeout + 1 retry: a voice turn that
        # takes 30 s is worse than the canned fallback line.
        self.client = client or anthropic.AsyncAnthropic(timeout=CLIENT_TIMEOUT_S, max_retries=1)
        self.first_token_timeout = getattr(cfg, "first_token_timeout_s", FIRST_TOKEN_TIMEOUT_S)
        self.system = build_system(dataclasses.asdict(state.recipe))
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
        self.checkins = None                       # CheckIns, set by main, so "talk less" can change its level
        self._positions: dict[str, str] = {}       # object -> "left, near" for the frames of the current turn
        self.last_usage = None                     # token usage of the last request (cache_read_input_tokens ...)

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
        self._fallback_steps.clear()

    async def prewarm(self) -> None:
        """Write the prompt cache now (max_tokens=0: prefill only, no output billed) so the first real
        turn hits a warm cache. Never raises: a failed pre-warm just means a cold first turn."""
        kw = dict(model=self.cfg.model, max_tokens=0, output_config={"effort": self.cfg.effort},
                  system=self.system, tools=TOOLS, messages=[{"role": "user", "content": "warmup"}])
        if self._fast():
            kw["speed"] = "fast"                    # a different speed is a different cache: warm the one we use
        try:
            try:
                resp = await asyncio.wait_for(self.client.messages.create(**kw), timeout=15)
            except anthropic.BadRequestError:
                # in case the API rejects the streaming-only tool flag on a non-streaming request
                kw["tools"] = [{k: v for k, v in t.items() if k != "eager_input_streaming"} for t in TOOLS]
                resp = await asyncio.wait_for(self.client.messages.create(**kw), timeout=15)
            u = resp.usage
            log.info("prewarm ok: cache_creation=%s cache_read=%s",
                     getattr(u, "cache_creation_input_tokens", None), getattr(u, "cache_read_input_tokens", None))
        except Exception as e:
            if _missing_credentials(e):
                log.error("No Anthropic credentials (ANTHROPIC_API_KEY in laptop/.env): "
                          "the coach will only speak the canned step lines.")
            else:
                log.warning("prewarm failed (first turn will be cold): %s: %s", type(e).__name__, e)

    # ----- turn plumbing -----

    def _note(self, frames: list | None = None) -> str:
        note = self.state.note()
        self._positions = {}
        if frames:                          # say where things are, not just that they are visible
            h, w = frames[-1].shape[:2]
            self._positions = positions(self.state.dets, w, h)
        if self._positions:
            where = ", ".join(f"{n} ({p})" for n, p in sorted(self._positions.items()))
            note, hit = re.subn(r"visible: [^|]*", lambda m: f"visible: {where} ", note, count=1)
            if not hit:
                note += f" | visible: {where}"
        if self.recent_sentences:
            note += f' | you last said: "{self.recent_sentences[-1][:120]}"'
        if self._notices:
            note += " | last turn: " + "; ".join(self._notices)
        return note

    def _apply_preferences(self, text: str) -> list[str]:
        """"say it simpler", "talk less" ...: change the setting locally and tell the model, so it confirms."""
        changes = []
        if _has(text, _PLAIN_ON) and not self.plain:
            self.plain = True
            changes.append("plain language is now ON")
        elif _has(text, _PLAIN_OFF) and self.plain:
            self.plain = False
            changes.append("plain language is now OFF")
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
        if self._exchanges and step < self._exchanges[-1].step:     # session restarted
            self._exchanges.clear()
            self._pending, self._notices = [], []
        checkin = user_text.startswith("[check-in")
        body = user_text if checkin else f"User: {user_text}"
        changes = [] if checkin else self._apply_preferences(user_text)
        note = self._note(frames)
        if frames:
            self._camera_told = False
        else:                               # no camera: say so (once per outage) and send no images
            note += " | camera: OFFLINE, no images" + (" (user already told)" if self._camera_told else " (tell the user once)")
            self._camera_told = self._camera_told or not checkin
        extra = ("[mode: plain]\n" if self.plain else "") + "".join(f"[setting changed: {c}]\n" for c in changes)
        content = list(self._pending)
        content += [jpeg_block(f, self.cfg.frame_max_px) for f in frames]
        content.append({"type": "text", "text": f"[{note}]\n{extra}{body}"})
        return _Turn(user={"role": "user", "content": content}, step=step, checkin=checkin,
                     had_pending=bool(self._pending))

    def _messages(self, ts: _Turn) -> list[dict]:
        """Trimmed history + this turn's user message."""
        ex = self._exchanges
        first_current = next((i for i, e in enumerate(ex) if e.step >= ts.step), len(ex))
        start = min(first_current, max(len(ex) - KEEP_TURNS, 0))
        if start:
            del ex[:start]                  # only ever drop from the front
        if ex:   # its tool_results answered a tool_use we just dropped (or never existed): remove them
            ex[0].user["content"] = [b for b in ex[0].user["content"] if b.get("type") != "tool_result"]
        msgs = []
        for e in ex:
            msgs += [e.user, e.assistant]
        return msgs + [ts.user]

    def _params(self, messages: list[dict]) -> dict:
        p = dict(model=self.cfg.model, max_tokens=MAX_TOKENS, output_config={"effort": self.cfg.effort},
                 system=self.system, tools=TOOLS, messages=messages)
        if self.cfg.model in FALLBACK_MODELS:
            p.update(betas=[FALLBACK_BETA], fallbacks="default")    # re-run on a fallback model on refusal
        if self._fast():
            p["speed"] = "fast"                                     # Opus fast mode (CLAUDE_FAST=1)
        return p

    def _fast(self) -> bool:
        return bool(getattr(self.cfg, "fast", False)) and self.cfg.model in FAST_MODE_MODELS

    async def _stream(self, ts: _Turn, on_sentence: OnSentence, t0: float) -> None:
        splitter = SentenceSplitter()
        messages = copy.copy(self._messages(ts))
        # No first text / tool call within first_token_timeout -> TimeoutError -> canned fallback line.
        async with asyncio.timeout(self.first_token_timeout) as deadline:
            async with self.client.beta.messages.stream(**self._params(messages)) as stream:
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

    async def _backstop(self, ts: _Turn, on_sentence: OnSentence) -> None:
        """The model answered with tool calls only. The user only hears audio, so that is silence: say a short
        line built from what the tools did. (The prompt forbids this and Haiku obeys; Opus still does it ~1 in 4.)"""
        uses = {u["id"]: u for u in ts.tool_uses}
        line = None
        for r in ts.results:
            use = uses.get(r["tool_use_id"], {})
            result, obj = str(r.get("content", "")), use.get("input", {}).get("name")
            if use.get("name") == "propose_done" and result.startswith("push_back"):
                reason = result.partition(": ")[2]
                line = f"Hold on, {reason}." if reason else "Hold on, that doesn't look finished yet."
                break
            if use.get("name") == "point_at" and obj:
                where = self._positions.get(obj, "").split(",")[0]       # "left" / "centre" / "right"
                here = f"Here's the {obj}" + (f", on your {where}." if where in ("left", "right") else
                                              ", in the middle." if where == "centre" else ".")
                line = here if result == "ok" else (
                    f"I can't see the {obj} right now." if result == "not_visible" else line)
        if line is None:                    # nothing to describe: at least say where they are
            line = f"You're on step {self.state.step + 1}: {self.state.current_step.short}."
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
                if out in ("not_visible", "unknown_object"):
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
        self._exchanges.append(_Exchange(ts.step, user, {"role": "assistant", "content": content}))
        self._pending = ts.results
        self._notices = ts.notices
