"""Session (SessionAPI): idle -> "hey Remy" -> active <-> paused. "Start over" -> reset to idle.

Start: while idle the coach only listens for the wake phrase ("hey Remy", fuzzy: "hi remi", "okay rémy"...).
  It's matched on interim transcripts, so the rising chime plays while you're still talking. When the
  utterance ends, ONE agent turn greets and explains step 1, and answers anything said after the
  wake phrase ("hey Remy, let's make guac").
Reset: saying "start over" (intent router -> reset()): stop audio, cancel the agent, laser off, steps
  back to 1, S3 `R` (laser off, servos centred), idle, falling chime.
Pause: "pause" / "give me a sec" / "hold on a minute" (intent router -> pause()), the agent's pause_session tool
  (-> pause_now(): its own spoken "sure, I'll wait" isn't cut), or BOOT with --button. While paused: the step is
  kept, the laser is off, check-ins and the watcher stay quiet (they only run while "active"), the TFT says
  "paused: say I'm back", and speech is ignored except a resume phrase ("I'm back", "resume", "continue",
  "hey Remy"), "start over", and safety ("help", "I cut myself": answered, still paused). Timers keep running.
  Time spent paused doesn't count as time on the step (State.cook_clock()).
Resume: a resume phrase -> one agent turn that welcomes the cook back and says where they are (first_turn());
  the BOOT button does the same through agent_turn().
Buttons: only with --button (main.py): BOOT starts when idle, pauses when active, resumes when paused. RST/EN is
only an emergency reboot of the S3 (its firmware boots with the laser off; the laptop keeps running and reconnects).

Wiring (see tools/try_voice.py, later main.py):
    session = Session(state, speech, agent, aimer, steps, link)   # attaches itself to the pipeline
    actions.session = session                                     # START_OVER intent -> reset()
"""
from __future__ import annotations

import re
import time
import unicodedata

from loguru import logger
from rapidfuzz import fuzz

from .contracts import TFT_AGENT, TFT_STATUS, TFT_STEP, AgentAPI, AimerAPI, ControlLinkAPI, State, StepsAPI
from .intents import is_resume
from .timers import fmt_duration

WAKE_NAME = "remy"
WAKE_GREETINGS = {"hey", "hi", "hay", "a", "ay", "ok", "okay", "hello", "yo", "hei", "he"}
WAKE_NAME_MIN = 75      # rapidfuzz ratio: remi/ramy/romy 75, remmy 89. "army" 50, "ready" 67.
START_TEXT = "[session start: greet briefly and explain step 1]"
RESUME_TEXT = ("[session resumed after {away}: welcome the cook back in a few words, then say which step they are on "
               "and the very next action]")
PAUSE_LINE = "Sure, take your time. Say I'm back when you're ready."
PAUSED_STATUS = "paused: say I'm back"      # TFT line 0, exactly 20 characters


def _words(text: str) -> list[str]:
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))         # rémy -> remy
    return re.sub(r"[^a-z0-9' ]+", " ", t).split()


def find_wake(text: str) -> int | None:
    """Index of the first word after the wake phrase, or None. Needs a greeting before the name,
    except when the whole utterance is just the name ("Remy?")."""
    words = _words(text)
    for i, w in enumerate(words):
        if fuzz.ratio(w, WAKE_NAME) < WAKE_NAME_MIN:
            continue
        if (i > 0 and words[i - 1] in WAKE_GREETINGS) or len(words) <= 2:
            return i + 1
    return None


class Session:
    """SessionAPI. `speech` is voice.Speech (needs agent_turn / chime / interrupt); `link` is optional."""

    def __init__(self, state: State, speech, agent: AgentAPI, aimer: AimerAPI, steps: StepsAPI,
                 link: ControlLinkAPI | None = None):
        self.state, self.speech, self.agent, self.aimer, self.steps, self.link = (
            state, speech, agent, aimer, steps, link)
        if hasattr(speech, "attach_session"):
            speech.attach_session(self)
        self._resumed_after: float | None = None    # set by a resume; the next first_turn() welcomes the cook back
        self._show_idle()

    # -- SessionAPI -------------------------------------------------------------------------------

    async def start(self) -> None:
        """Start without a wake phrase (tests, dashboard button): chime + greeting turn."""
        if await self._activate("start()"):
            await self.speech.agent_turn(START_TEXT)

    async def reset(self) -> None:
        # Go idle first, so nothing still in flight starts a new turn.
        self._end_pause()
        self.state.session = "idle"
        await self.speech.interrupt()
        self.agent.cancel()
        self.aimer.laser_off()
        self.steps.reset()
        if self.link is not None:
            self.link.reset()                 # S3: laser off, servos centred
        self.state.log("system", "session reset")
        logger.info("session: reset -> idle")
        self._show_idle()
        await self.speech.chime("reset")

    # -- pause / resume (not in SessionAPI) ----------------------------------------------------------

    async def pause(self, why: str = "user", *, speak: bool = True) -> bool:
        """Cook steps away: cut the audio and any agent turn, laser off, say how to come back. False if not active."""
        if not self.pause_now(why):
            return False
        await self._interrupt()
        self.agent.cancel()
        await self._chime("pause")
        if speak:
            await self.speech.say(PAUSE_LINE)
        return True

    def pause_now(self, why: str = "agent") -> bool:
        """The synchronous part of pause() (the agent's pause tool runs mid-turn and must not cut its own reply)."""
        s = self.state
        if s.session != "active":
            return False
        s.session, s.paused_at = "paused", time.monotonic()
        self.aimer.laser_off()
        s.log("system", f"session paused ({why})")
        logger.info(f"session: paused ({why})")
        self._show(TFT_STATUS, PAUSED_STATUS)
        return True

    async def resume(self, why: str = "user", *, greet: bool = False) -> bool:
        """Back to active. greet=True (BOOT button) also runs the welcome-back agent turn; a spoken resume gets it
        from first_turn() instead. False if not paused."""
        if not self.resume_now(why):
            return False
        await self._chime("start")
        if greet and hasattr(self.speech, "agent_turn"):
            await self.speech.agent_turn(self.resume_text())
        return True

    def resume_now(self, why: str = "agent") -> bool:
        s = self.state
        if s.session != "paused":
            return False
        away = self._end_pause()
        s.session = "active"
        self._resumed_after = away
        s.log("system", f"session resumed after {fmt_duration(away)} ({why})")
        logger.info(f"session: resumed after {away:.0f} s ({why})")
        self._show(TFT_STATUS, "cooking")
        return True

    def resume_text(self) -> str:
        away, self._resumed_after = self._resumed_after or 0.0, None
        return RESUME_TEXT.format(away=fmt_duration(away))

    # -- hooks ------------------------------------------------------------------------------------

    async def heard(self, text: str) -> bool:
        """Voice pipeline, while not active. Idle: wake phrase in this (partial) utterance? Then activate now.
        Paused: a resume phrase or the wake phrase resumes. True = the utterance (re)started the session."""
        if self.state.session == "paused":
            if find_wake(text) is None and not is_resume(text):
                return False
            return await self.resume(f"heard {text!r}")
        if find_wake(text) is None:
            return False
        self._resumed_after = None
        return await self._activate(f"wake phrase in {text!r}")

    def first_turn(self, text: str) -> str:
        """Agent text for the utterance that woke the session: greeting + anything said after the name.
        After a resume: the welcome-back instruction + everything the cook said."""
        if self._resumed_after is not None:
            return f"{self.resume_text()}\nUser: {text.strip()}"
        i = find_wake(text)
        rest = " ".join(_words(text)[i:]) if i is not None else ""
        return f"{START_TEXT}\nUser: {rest}" if rest else START_TEXT

    # -- internals --------------------------------------------------------------------------------

    async def _activate(self, why: str) -> bool:
        if self.state.session == "active":
            return False
        self.state.session = "active"
        self.state.log("system", "session started")
        logger.info(f"session: active ({why})")
        self._show(TFT_STATUS, "cooking")
        self._show(TFT_STEP, f"Step 1: {self.state.current_step.short}")
        await self.speech.chime("start")
        return True

    def _end_pause(self) -> float:
        """Close the running pause (if any) and add it to State.paused_total_s. Returns its length in seconds."""
        s = self.state
        if s.paused_at is None:
            return 0.0
        away = max(0.0, time.monotonic() - s.paused_at)
        s.paused_total_s += away
        s.paused_at = None
        return away

    async def _interrupt(self) -> None:
        fn = getattr(self.speech, "interrupt", None)
        if fn is not None:
            await fn()
        else:
            self.speech.stop()

    async def _chime(self, kind: str) -> None:
        fn = getattr(self.speech, "chime", None)
        if fn is not None:
            await fn(kind)

    def _show_idle(self) -> None:
        self._show(TFT_STATUS, "say: hey Remy")
        self._show(TFT_STEP, "")
        self._show(TFT_AGENT, "")

    def _show(self, line: int, text: str) -> None:
        if self.link is not None:
            self.link.show_text(line, text)
