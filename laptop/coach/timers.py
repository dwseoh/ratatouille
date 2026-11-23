"""Kitchen timers: the agent's set_timer / cancel_timer tools, and the spoken "time's up".

    timers = Timers(state, link)              # main builds one; the agent's tool handlers call set() / cancel()
    task = asyncio.create_task(timers.run(speech.say, quiet=lambda: ...))   # once Speech exists

`State.timers` (label -> due time.monotonic()) is the shared view: the agent's state note ("timers: onions 4:12
left") and the dashboard read it. Timers keep running while the session is paused: the cook asked for them, so
they are not chatter.

When one is due it is logged ("timer done: onions"), shown on the TFT, and announced through the normal speech
queue at REPLY priority, but only once it's quiet (nobody talking, no agent turn running), so it never talks over
the cook. The echo gate then treats it like any other coach sentence. If it never gets quiet, it is said anyway
after ANNOUNCE_MAX_WAIT_S: a late reminder is better than a missed one.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from .contracts import TFT_AGENT, Priority, State

log = logging.getLogger(__name__)

MAX_TIMERS = 5
MAX_SECONDS = 4 * 3600
TICK_S = 0.25
ANNOUNCE_MAX_WAIT_S = 8.0       # wait at most this long for a quiet moment before announcing anyway
DEFAULT_LABEL = "timer"


def fmt_duration(seconds: float) -> str:
    """Spoken-friendly: 45 -> "45 seconds", 90 -> "1 minute 30 seconds", 3600 -> "1 hour"."""
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    parts = []
    for n, word in ((h, "hour"), (m, "minute"), (sec, "second")):
        if n:
            parts.append(f"{n} {word}{'s' if n != 1 else ''}")
    return " ".join(parts) or "0 seconds"


def fmt_clock(seconds: float) -> str:
    """Compact m:ss (h:mm:ss over an hour) for the state note and the TFT: 252 -> "4:12"."""
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


class Timers:
    def __init__(self, state: State, link=None, *, clock: Callable[[], float] = time.monotonic):
        self.state, self.link, self._clock = state, link, clock
        self._due: list[tuple[str, float]] = []      # fired, waiting for a quiet moment: (label, fired at)

    # ----- the agent's tools -----

    def set(self, seconds: int, label: str = "") -> str:
        label = " ".join(str(label).split())[:24].strip().lower() or DEFAULT_LABEL
        if not 1 <= seconds <= MAX_SECONDS:
            return f"error: a timer must be between 1 second and {MAX_SECONDS // 3600} hours"
        timers = self.state.timers
        if label not in timers and len(timers) >= MAX_TIMERS:
            return f"error: already {MAX_TIMERS} timers running; cancel one first"
        replaced = label in timers
        timers[label] = self._clock() + seconds
        self.state.log("agent", f"timer set: {label}, {fmt_duration(seconds)}")
        self._show(f"{label[:12]} {fmt_clock(seconds)}")
        return f"ok: {'restarted' if replaced else 'started'} {label} timer, {fmt_duration(seconds)}"

    def cancel(self, label: str = "") -> str:
        """Cancel the timer with this label; an empty label cancels them all."""
        timers = self.state.timers
        if not timers:
            return "no timers running"
        label = " ".join(str(label).split()).lower()
        names = list(timers) if not label else [n for n in timers if n == label] or [n for n in timers if label in n]
        if not names:
            return f"no timer called {label!r}; running: {', '.join(timers)}"
        for n in names:
            del timers[n]
        self.state.log("agent", f"timer cancelled: {', '.join(names)}")
        self._show("")
        return f"ok: cancelled {', '.join(names)}"

    # ----- reading -----

    def describe(self) -> str:
        """"onions 4:12 left, rice 0:30 left" (soonest first), or "" when none."""
        return describe(self.state, self._clock())

    # ----- the loop -----

    def tick(self) -> list[str]:
        """Move timers that are due from State.timers to the announce queue. Returns their labels."""
        now = self._clock()
        fired = [n for n, due in self.state.timers.items() if due <= now]
        for n in fired:
            del self.state.timers[n]
            self._due.append((n, now))
            self.state.log("system", f"timer done: {n}")
            self._show(f"TIME UP: {n}"[:20])
            log.info("timer done: %s", n)
        return fired

    async def run(self, say: Callable[..., Awaitable[None]], quiet: Callable[[], bool] = lambda: True) -> None:
        """Add to main's tasks once speech exists. Never raises; cancel to stop."""
        while True:
            try:
                self.tick()
                await self.announce(say, quiet)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("timer tick failed")
            await asyncio.sleep(TICK_S)

    async def announce(self, say, quiet: Callable[[], bool]) -> None:
        """Say the oldest due timer once it's quiet (or once it has waited ANNOUNCE_MAX_WAIT_S)."""
        if not self._due:
            return
        label, fired_at = self._due[0]
        if not quiet() and self._clock() - fired_at < ANNOUNCE_MAX_WAIT_S:
            return
        self._due.pop(0)
        text = "Your timer is done." if label == DEFAULT_LABEL else f"Time's up for the {label}."
        await say(text, Priority.REPLY)

    def _show(self, text: str) -> None:
        if self.link is not None:
            self.link.show_text(TFT_AGENT, text)


def describe(state: State, now: float | None = None) -> str:
    now = time.monotonic() if now is None else now
    items = sorted(state.timers.items(), key=lambda kv: kv[1])
    return ", ".join(f"{n} {fmt_clock(due - now)} left" for n, due in items)
