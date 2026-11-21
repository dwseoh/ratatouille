"""Watcher (B3, loop 3): looks at the camera on its own, so the coach notices things without being asked.

Two jobs
  1. Event log:   a cheap vision model (Haiku) reads 3-4 recent frames and reports what changed
                  ("avocado halved") -> State.events, which the agent's state note and the dashboard read.
  2. Step done?:  the same call judges the current step's `done_when`.
                    - a confident "done" -> steps.propose_done("vision") -> "ask", and `on_ask` makes the agent
                      ask the user ("looks mashed, ready for the lime?"); the watcher never advances anything.
                    - the latest verdict is cached for B2: `latest_verdict(step)` is Steps' verdict_provider,
                      so a premature "I'm done" can be pushed back (it must not block, hence cached).

(The local knife-near-hand warning was cut: it would fire constantly while cutting. See docs/cut-list.md.
Knife safety lives in the recipe `tips` and the agent's prompt instead.)

The watcher speaks in exactly one place, and it is guarded: the "ready for the next step?" question, only when
nobody is talking AND no agent turn is running (a new agent turn cancels the one in flight, which would kill the
answer to the user's own question) AND things have been quiet for ASK_SETTLE_S, so it never lands right after the
user or the coach stops talking.

When the model is called (never while the coach or the user is talking, never while the session is idle,
never more often than MIN_GAP_S): the visible-object set changed, the user said "done", `request_check()`
was called, or STEP_INTERVAL_S passed. Calls run in a background task.

`confidence` in the model's answer is how sure it is of `step_done` (either value), which is what Steps expects:
"not done, 0.9" = a confident push-back.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Awaitable, Callable

import anthropic

from .agent import jpeg_block
from .contracts import FrameSource, State, StepsAPI
from .steps import StepVerdict

log = logging.getLogger(__name__)

TICK_S = 0.1                # loop period
MIN_GAP_S = 3.0             # never call the model more often than this
STEP_INTERVAL_S = 10.0      # ...but look at least this often while a step is running
FRAMES = 4                  # frames per call (~0.5 s apart)
FRAME_MAX_PX = 384
MAX_EVENTS_PER_CALL = 3
DONE_CONF = 0.8             # confidence needed before the watcher suggests "step done" to the user
ASK_SETTLE_S = 1.5          # the question waits until nobody has spoken for this long...
BUSY_SETTLE_S = 1.0         # ...and the agent has been idle for this long (TTS lags a moment behind the agent)
VERDICT_TTL_S = 15.0        # a cached verdict older than this isn't offered to Steps
CALL_TIMEOUT_S = 10.0
LOG_LINES = 15              # recent events shown to the model


SYSTEM = """You watch a chest camera while someone follows a recipe, and report what changed.
Reply ONLY with JSON matching the schema.
- events: up to 3 short past-tense facts about what the cook just did or how the food looks now
  ("avocado halved", "pit removed", "onion diced"). Only NEW things that are not already in the event log.
  Use an empty list when nothing changed.
- step_done: true only when the frames clearly show the current step's done_when condition is met.
- confidence: 0 to 1, how sure you are about step_done (whether you answered true or false).
- note: one short plain-words reason that can be said to the cook ("I still see whole avocados")."""

SCHEMA = {
    "type": "object",
    "properties": {
        "events": {"type": "array", "items": {"type": "string"}},
        "step_done": {"type": "boolean"},
        "confidence": {"type": "number"},
        "note": {"type": "string"},
    },
    "required": ["events", "step_done", "confidence", "note"],
    "additionalProperties": False,
}

OnAsk = Callable[[str], Awaitable[None]]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


class Watcher:
    def __init__(self, cfg, state: State, frames: FrameSource, speech, steps: StepsAPI,
                 on_ask: OnAsk | None = None, busy: Callable[[], bool] | None = None,
                 ignored: Callable[[], bool] | None = None, client=None,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg, self.state, self.frames, self.speech, self.steps = cfg, state, frames, speech, steps
        self.on_ask, self._clock = on_ask, clock
        self._agent_busy = busy or (lambda: False)         # e.g. lambda: agent.busy
        self._ignored = ignored or (lambda: False)         # e.g. lambda: agent.camera_ignored ("ignore the camera")
        self._last_busy = float("-inf")                    # last time we saw an agent turn running
        self.client = client or anthropic.AsyncAnthropic(timeout=CALL_TIMEOUT_S, max_retries=0)
        self._task: asyncio.Task | None = None             # the in-flight model call, if any
        self._last_run: float | None = None
        self._last_visible: frozenset | None = None
        self._events_seen = len(state.events)
        self._forced: str | None = None                    # reason set by request_check() / "said done"
        self._verdict: tuple[int, StepVerdict, float] | None = None   # (step, verdict, when)
        self._proposed_step: int | None = None             # step we've already suggested "done" for
        self._pending_ask: tuple[int, str] | None = None   # ask the user once it's quiet

    # ----- public -----

    def latest_verdict(self, step: int) -> StepVerdict | None:
        """Steps' `verdict_provider`: the newest cached opinion about `step`, or None (never blocks)."""
        if self._verdict is None or self._ignored():
            return None
        s, verdict, when = self._verdict
        return verdict if s == step and self._clock() - when <= VERDICT_TTL_S else None

    def request_check(self, reason: str = "requested") -> None:
        """Ask for a model check at the next tick (still subject to the quiet / min-gap rules)."""
        self._forced = reason

    async def run(self) -> None:
        """The loop. Add to main's asyncio.gather. Never raises; cancel it to stop."""
        try:
            while True:
                try:
                    await self.tick()
                except Exception:
                    log.exception("watcher tick failed")
                await asyncio.sleep(TICK_S)
        finally:
            if self._task and not self._task.done():
                self._task.cancel()

    async def tick(self) -> None:
        """One pass: maybe start a model check, then maybe deliver a pending question."""
        if self.state.session != "active":
            self._events_seen = len(self.state.events)
            return
        if self._ignored():                       # the user said to ignore the camera: no looking, no asking
            self._pending_ask = None
            self._events_seen = len(self.state.events)
            return
        if self._agent_busy():
            self._last_busy = self._clock()
        reason = self._why_check()
        if reason and not self._busy():
            self._last_run = self._clock()
            self._task = asyncio.create_task(self._check(reason, self.state.step))
        await self._flush_ask()

    # ----- when to look -----

    def _busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def _quiet(self) -> bool:
        return not self.state.speaking and not self.state.user_talking

    def _why_check(self) -> str | None:
        s = self.state
        new, self._events_seen = s.events[self._events_seen:], len(s.events)
        if any(e.source == "user" and "said done" in e.text for e in new):
            self._forced = self._forced or "user said done"
        now = self._clock()
        if (self._last_run is not None and now - self._last_run < MIN_GAP_S) or not self._quiet():
            return None
        if self._forced:
            reason, self._forced = self._forced, None
            return reason
        visible = frozenset(s.visible())
        if visible != self._last_visible:
            self._last_visible = visible
            return "visible objects changed"
        if self._last_run is None or now - self._last_run >= STEP_INTERVAL_S:
            return "periodic"
        return None

    # ----- the model call -----

    def _request(self) -> dict | None:
        imgs = [f for f in self.frames.recent(FRAMES) if f is not None]
        if not imgs:
            return None
        s = self.state
        step = s.current_step
        log_lines = "\n".join(f"- {e.source}: {e.text}" for e in s.events[-LOG_LINES:]) or "(empty)"
        content = [jpeg_block(f, FRAME_MAX_PX) for f in imgs]
        content.append({"type": "text", "text": (
            f"The {len(imgs)} images are consecutive frames, oldest first, about half a second apart.\n"
            f"Recipe: {s.recipe.name}. Current step {s.step + 1}/{len(s.recipe.steps)}: {step.short} - {step.goal}\n"
            f"done_when: {step.done_when}\n"
            f"Objects the detector sees: {', '.join(sorted(s.visible())) or 'nothing'}\n"
            f"Event log (newest last):\n{log_lines}")})
        return dict(model=self.cfg.watcher_model, max_tokens=1500, system=SYSTEM,
                    output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                    messages=[{"role": "user", "content": content}])

    async def _check(self, reason: str, step: int) -> None:
        """One model call about `step` (the step when it was launched) + applying its answer."""
        try:
            req = self._request()
            if req is None:
                log.info("watcher: no frames (%s)", reason)
                return
            resp = await asyncio.wait_for(self.client.messages.create(**req), timeout=CALL_TIMEOUT_S)
            data = self._parse(resp)
        except asyncio.CancelledError:
            raise
        except Exception as e:                     # API down, timeout, bad JSON: just skip this round
            log.warning("watcher check failed (%s): %s: %s", reason, type(e).__name__, e)
            return
        if data is None:
            return
        events, done, conf, note = data
        self._log_events(events)
        if self.state.step != step:
            return                                 # the user moved on while we were looking
        self._verdict = (step, StepVerdict(done, conf, note), self._clock())
        if done and conf >= DONE_CONF and self._proposed_step != step:
            self._proposed_step = step
            if self.steps.propose_done("vision", note) == "ask":
                short = self.state.current_step.short
                self._pending_ask = (step, f"[check-in: the camera thinks step {step + 1} ({short}) is finished"
                                           f" - {note or 'it looks done'}. Ask the user in one short sentence "
                                           f"if they're ready to move on.]")

    @staticmethod
    def _parse(resp) -> tuple[list[str], bool, float, str] | None:
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            log.warning("watcher: no text in response (stop_reason=%s)", getattr(resp, "stop_reason", None))
            return None
        d = json.loads(text)
        events = [e.strip() for e in d["events"] if isinstance(e, str) and e.strip()]
        conf = min(1.0, max(0.0, float(d["confidence"])))
        return events, bool(d["step_done"]), conf, str(d.get("note", "")).strip()

    def _log_events(self, events: list[str]) -> None:
        """Append new events, skipping anything already reported (the model likes to repeat itself)."""
        known = [_norm(e.text) for e in self.state.events[-LOG_LINES * 2:] if e.source == "vision"]
        added = 0
        for e in events:
            n = _norm(e)
            if not n or any(n == k or n in k or k in n for k in known):
                continue
            self.state.log("vision", e)
            known.append(n)
            added += 1
            if added >= MAX_EVENTS_PER_CALL:
                break

    async def _flush_ask(self) -> None:
        """Deliver the "ready for the next step?" question once nobody is talking."""
        if self._pending_ask is None:
            return
        step, text = self._pending_ask
        if self.state.step != step or self.state.session != "active":
            self._pending_ask = None               # stale: the user already moved on / reset
        elif self._ok_to_ask() and self.on_ask is not None:
            self._pending_ask = None
            try:
                await self.on_ask(text)
            except Exception:
                log.exception("watcher: asking the user failed")

    def _ok_to_ask(self) -> bool:
        """Nobody talking, no agent turn running, and quiet for a moment (see the module docstring)."""
        if not self._quiet() or self._agent_busy():
            return False
        if time.monotonic() - self.state.last_activity < ASK_SETTLE_S:     # voice stamps this with monotonic()
            return False
        return self._clock() - self._last_busy >= BUSY_SETTLE_S

