"""Claude agent for voice turns (contract C6): streams text sentence by sentence, calls local tools.

Latency notes (see docs/architecture.md):
- the system prompt + recipe + tool list are a stable, cached prefix (`prewarm()` warms it at startup)
- only 2-3 small frames per turn; frames are dropped from history as soon as the turn ends
- tools run the moment their input is complete (mid-stream); their results ride along with the NEXT
  user turn, so a tool never costs a second round trip
- barge-in: `cancel()` aborts the HTTP stream; sentences not yet handed to TTS are discarded

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
from dataclasses import dataclass, field
from typing import Callable

import anthropic

from .contracts import LAT_FIRST_TOKEN, LAT_POINT, TFT_AGENT, OnSentence, State, TurnStats

log = logging.getLogger(__name__)

SYSTEM = """You are a hands-free cooking coach speaking through an earpiece while the user cooks.
You see what a chest camera sees.

How to reply
- Your words are spoken aloud: 1-2 short sentences, plain speech, no lists, markdown or emoji.
- Say your sentence first, then call tools.
- Every message starts with a [state ...] line from the app: the current step, which objects the camera
  currently sees, where the laser points, and recent events. Trust it for what is visible.

Tools
- point_at(name): shine the laser on an object. Only call it for objects the state line says are visible.
  If the user asks about something that isn't visible, say you can't see it instead of pointing.
- laser_off(): turn the laser off.
- propose_done(): call it only when the frames and the user's words show the current step is really
  finished. The app decides whether to advance.
- show_text(text): up to 20 characters on the wearable's display.
If a tool result says not_visible or error, you may mention it briefly on your next turn.

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
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept server-side refusal fallbacks (Haiku 5.5 does not).
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


# ───────────────────────── the agent ─────────────────────────

class Agent:
    """C6 AgentAPI. `handlers` is tool name -> callable(**input) -> str (see make_handlers)."""

    def __init__(self, cfg, state: State, handlers: dict[str, Callable[..., str]], client=None):
        self.cfg, self.state, self.handlers = cfg, state, handlers
        # One client for the app's lifetime (keep-alive). Short timeout + 1 retry: a voice turn that
        # takes 30 s is worse than the canned fallback line.
        self.client = client or anthropic.AsyncAnthropic(timeout=30.0, max_retries=1)
        self.system = build_system(dataclasses.asdict(state.recipe))
        self.busy = False
        self._exchanges: list[_Exchange] = []
        self._pending: list[dict] = []     # tool_results owed to the next user message
        self._notices: list[str] = []
        self._run: asyncio.Future | None = None
        self._cancelled = False

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
        except anthropic.APIError as e:
            ts.stats.error = f"{type(e).__name__}: {e}"
            log.warning("agent turn failed: %s", ts.stats.error)
            if not ts.spoken and not ts.checkin:
                await self._fallback(ts, on_sentence)
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

    async def prewarm(self) -> None:
        """Write the prompt cache now (max_tokens=0: prefill only, no output billed) so the first real
        turn hits a warm cache. Never raises: a failed pre-warm just means a cold first turn."""
        kw = dict(model=self.cfg.model, max_tokens=0, output_config={"effort": self.cfg.effort},
                  system=self.system, tools=TOOLS, messages=[{"role": "user", "content": "warmup"}])
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
            log.warning("prewarm failed (first turn will be cold): %s: %s", type(e).__name__, e)

    # ----- turn plumbing -----

    def _note(self) -> str:
        note = self.state.note()
        if self._notices:
            note += " | last turn: " + "; ".join(self._notices)
        return note

    def _begin(self, user_text: str, frames: list) -> _Turn:
        step = self.state.step
        if self._exchanges and step < self._exchanges[-1].step:     # session restarted
            self._exchanges.clear()
            self._pending, self._notices = [], []
        checkin = user_text.startswith("[check-in")
        body = user_text if checkin else f"User: {user_text}"
        content = list(self._pending)
        content += [jpeg_block(f, self.cfg.frame_max_px) for f in frames]
        content.append({"type": "text", "text": f"[{self._note()}]\n{body}"})
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
        return p

    async def _stream(self, ts: _Turn, on_sentence: OnSentence, t0: float) -> None:
        splitter = SentenceSplitter()
        messages = copy.copy(self._messages(ts))
        async with self.client.beta.messages.stream(**self._params(messages)) as stream:
            async for event in stream:
                if event.type == "text":
                    if ts.stats.ttft_s is None:
                        ts.stats.ttft_s = time.monotonic() - t0
                        if self.state.turns and not ts.checkin:
                            self.state.turns[-1].mark(LAT_FIRST_TOKEN)
                    for s in splitter.feed(event.text):
                        await self._emit(ts, s, on_sentence)
                elif event.type == "content_block_stop":
                    block = event.content_block
                    if block.type == "tool_use":
                        self._run_tool(ts, block.id, block.name, block.input)
                    elif block.type == "text" and (tail := splitter.flush()):
                        await self._emit(ts, tail, on_sentence)     # don't hold the last sentence back
            if tail := splitter.flush():
                await self._emit(ts, tail, on_sentence)
            final = await stream.get_final_message()
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
        ts.stats.sentences += 1
        await on_sentence(sentence)

    async def _fallback(self, ts: _Turn, on_sentence: OnSentence) -> None:
        """API down or failing: say the step's canned line so the user isn't left in silence."""
        await self._emit(ts, self.state.current_step.fallback, on_sentence)

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
