"""Check-ins: what makes the coach talk on its own. One normal agent turn with fresh frames, no second model.

Two triggers
  - step intro:  a step just started (Steps.on_change) -> after a second of quiet, introduce it and point at
                 what the user needs. (Step 1 is introduced by Session's greeting, so it isn't repeated here.)
  - silence:     nobody has spoken for a while -> "comment only if something is worth saying, else SKIP".

It speaks only when ALL hold: session active, the coach isn't speaking, the user isn't talking, and no agent turn is
running. If the user starts talking mid check-in, the voice pipeline's barge-in cancels it like any other turn.
This module never writes `State.last_activity` (voice owns it); it keeps its own `last_checkin` time.

How loud it is (CHECKINS in .env, or "talk less" / "talk more" while cooking):
    off      never speaks unprompted
    gentle   intros + a comment after 45 s of silence      (some people find constant comments overwhelming)
    normal   intros + a comment after 20 s of silence
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from .contracts import Priority, State, StepsAPI

log = logging.getLogger(__name__)

LEVELS = {"off": None, "gentle": 45.0, "normal": 20.0}     # level -> seconds of silence before a comment
INTRO_QUIET_S = 1.0         # a new step is introduced once things have been quiet this long
TICK_S = 1.0
FRAMES = 3


class CheckIns:
    def __init__(self, state: State, agent, frames, say, steps: StepsAPI, *,
                 ask: Callable[[str], Awaitable[None]] | None = None, level: str = "normal",
                 clock: Callable[[], float] = time.monotonic):
        """`say(sentence, priority)` speaks (Speech.say). `ask(text)` starts an agent turn the voice pipeline
        knows about (voice.Speech.agent_turn: cancellable by barge-in); without it we call agent.turn directly."""
        self.state, self.agent, self.frames, self.say, self.ask = state, agent, frames, say, ask
        self._clock = clock
        self.level = "normal"
        self.set_level(level)
        self.last_checkin = float("-inf")
        self._intro_for: int | None = None             # step waiting for its introduction
        steps.on_change(self._on_step)

    # ----- settings -----

    def set_level(self, level: str) -> None:
        level = level.strip().lower()
        if level not in LEVELS:
            log.warning("unknown CHECKINS level %r (use off / gentle / normal)", level)
            return
        self.level = level

    @property
    def silence_s(self) -> float | None:
        return LEVELS[self.level]

    # ----- triggers -----

    def _on_step(self, n: int) -> None:
        # a reset (n == 0) ends the session; the greeting introduces step 1 when the next one starts
        self._intro_for = n if n > 0 and self.state.session == "active" else None

    def _text(self, now: float) -> str | None:
        s = self.state
        if self.level == "off":
            return None
        quiet = now - s.last_activity               # since the user or the coach last spoke (voice writes it)
        if self._intro_for is not None:
            if self._intro_for != s.step:
                self._intro_for = None               # moved on again before we got to it
            elif quiet >= INTRO_QUIET_S:
                step = s.current_step
                objs = ", ".join(step.point) or "what they need"
                self._intro_for = None
                return (f"[check-in: step {s.step + 1} ({step.short}) just started. Introduce it in your own words, "
                        f"then point at {objs}. If you already introduced it in your last message, reply with just SKIP.]")
        gap = self.silence_s
        if gap is not None and quiet >= gap and now - self.last_checkin >= gap:
            return (f"[check-in: nobody has spoken for {int(quiet)} s. Say one short helpful thing only if something "
                    f"is worth saying, otherwise reply with just SKIP.]")
        return None

    def _quiet_enough_to_speak(self) -> bool:
        s = self.state
        return s.session == "active" and not s.speaking and not s.user_talking and not self.agent.busy

    # ----- the loop -----

    async def tick(self) -> bool:
        """One pass. Returns True if a check-in was started."""
        if self.state.session != "active":
            self._intro_for = None
            return False
        if not self._quiet_enough_to_speak():
            return False
        now = self._clock()
        text = self._text(now)
        if text is None:
            return False
        self.last_checkin = now
        log.info("check-in: %s", text[:80])
        if self.ask is not None:
            await self.ask(text)                     # returns once the turn has started
        else:
            frames = self.frames.recent(FRAMES) if getattr(self.frames, "connected", True) else []
            await self.agent.turn(text, frames, lambda s: self.say(s, Priority.PROACTIVE))
        return True

    async def run(self) -> None:
        """Add to main's asyncio.gather. Never raises; cancel to stop."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("check-in failed")
            await asyncio.sleep(TICK_S)
