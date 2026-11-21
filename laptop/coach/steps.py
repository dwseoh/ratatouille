"""Step machine (contract C7): the ONLY code that changes `State.step`.

Three things can say "this step is finished", and they can disagree:

  user   "I'm done" / "what's next"   (intent router -> propose_done("user"))
  llm    the agent's `propose_done` tool                         (propose_done("llm"))
  vision the watcher sees the step finish                        (propose_done("vision"))

Rules
  user / llm  -> advance. The user is trusted: if they say the step is done, it is done, whatever the camera shows.
                 (Optional, off by default, `pushback=True` / PUSHBACK_DONE=1: if the watcher confidently says "not
                 done", push back ONCE per step; asking again advances. Useful when the camera is reliable.)
  vision      -> never advance by itself: return "ask". The agent asks "looks mashed, ready for the lime?"
                 and `confirm()` advances after the user's yes. Saying "done" instead also advances.
  last step   -> "finished" instead of advancing; the step number stays on the last step.

Double calls: one spoken "I'm done" reaches us twice (the intent router AND the agent's tool call). User/llm
proposals within DEBOUNCE_S of the previous user/llm decision just repeat that verdict, so one utterance never
advances two steps and a push-back isn't undone by the second call.

Every change updates the TFT step line and fires the on_change callbacks. Sync on purpose (C7): the watcher
verdict comes from `verdict_provider`, which must return the latest CACHED verdict and never block.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .contracts import TFT_STEP, ControlLinkAPI, DoneSource, State, Verdict

DEBOUNCE_S = 3.0        # repeat user/llm proposals inside this window are the same utterance
PUSHBACK_CONF = 0.6     # the watcher must be at least this sure that the step is NOT done to push back


@dataclass(frozen=True)
class StepVerdict:
    """What the watcher (B3) thinks of the current step."""
    done: bool
    confidence: float = 1.0
    note: str = ""              # why, in plain words: "I still see whole avocados"


VerdictProvider = Callable[[int], "StepVerdict | None"]    # step index -> latest verdict for it, or None


class Steps:
    """StepsAPI implementation. `link` and `verdict_provider` are optional (tests, before B3 exists)."""

    def __init__(self, state: State, link: ControlLinkAPI | None = None,
                 verdict_provider: VerdictProvider | None = None, clock: Callable[[], float] = time.monotonic,
                 pushback: bool = False):
        self.state, self.link, self.verdict_provider, self._clock = state, link, verdict_provider, clock
        self.pushback = pushback            # let the camera veto a spoken "done" once (off: trust the user)
        self.reason = ""                    # why the last push_back happened (the agent reads this)
        self.finished = False
        self._cbs: list[Callable[[int], None]] = []
        self._pushed_back: set[int] = set()     # steps we've already pushed back on once
        self._asking = False                    # a vision "ask" is waiting for the user's yes
        self._last: tuple[float, Verdict] | None = None   # (time, verdict) of the last user/llm decision

    # ----- StepsAPI -----

    def propose_done(self, source: DoneSource, evidence: str = "") -> Verdict:
        if self.finished:
            return "finished"
        if source == "vision":
            return self._ask(evidence)
        # user / llm
        now = self._clock()
        if self._last and now - self._last[0] < DEBOUNCE_S:
            return self._last[1]                # same utterance seen twice: repeat, don't act again
        verdict = self._decide(source)
        self._last = (now, verdict)
        return verdict

    def confirm(self) -> Verdict:
        """The user said yes after an "ask". A stray yes with nothing pending changes nothing."""
        if self.finished:
            return "finished"
        if not self._asking:
            return "push_back"
        return self._advance("user confirmed")

    def reset(self) -> None:
        changed = self.state.step != 0
        self.state.step = 0
        self.finished = False
        self._clear_step_state()
        self._pushed_back.clear()
        self._last = None
        self.reason = ""
        if changed:
            self._changed()

    def on_change(self, cb: Callable[[int], None]) -> None:
        self._cbs.append(cb)

    # ----- rules -----

    def _ask(self, evidence: str) -> Verdict:
        if not self._asking:
            self._asking = True
            self.state.log("system", f"vision thinks step {self.state.step + 1} is done"
                           + (f": {evidence}" if evidence else "") + " - asking the user")
        return "ask"

    def _decide(self, source: DoneSource) -> Verdict:
        step = self.state.step
        v = self.verdict_provider(step) if (self.pushback and self.verdict_provider) else None
        if v is not None and not v.done and v.confidence >= PUSHBACK_CONF and step not in self._pushed_back:
            self._pushed_back.add(step)
            self.reason = v.note or "it doesn't look finished yet"
            self.state.log("system", f"{source} said step {step + 1} is done, watcher disagrees: {self.reason}")
            return "push_back"
        return self._advance(f"{source} said done")

    def _advance(self, why: str) -> Verdict:
        s = self.state
        if s.step >= len(s.recipe.steps) - 1:
            self.finished = True
            self._clear_step_state()
            s.log("system", f"step {s.step + 1} done ({why}) - recipe finished")
            self._show("All done!")
            return "finished"
        s.log("system", f"step {s.step + 1} done ({why}) -> step {s.step + 2}: {s.recipe.steps[s.step + 1].short}")
        s.step += 1
        self._clear_step_state()
        self._changed()
        return "advance"

    # ----- plumbing -----

    def _clear_step_state(self) -> None:
        self._asking = False
        self.reason = ""

    def _changed(self) -> None:
        self._show(f"Step {self.state.step + 1}: {self.state.current_step.short}")
        for cb in self._cbs:
            cb(self.state.step)

    def _show(self, text: str) -> None:
        if self.link is not None:
            self.link.show_text(TFT_STEP, text)
