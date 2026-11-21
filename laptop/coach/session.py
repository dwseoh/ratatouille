"""Session (B, contracts C13): idle -> "hey Remy" -> active. "Start over" -> reset to idle.

Start: while idle the coach only listens for the wake phrase ("hey Remy", fuzzy: "hi remi", "okay rémy"...).
  It's matched on interim transcripts, so the rising chime plays while you're still talking. When the
  utterance ends, ONE agent turn greets and explains step 1, and answers anything said after the
  wake phrase ("hey Remy, let's make guac").
Reset: saying "start over" (intent router -> reset()): stop audio, cancel the agent, laser off, steps
  back to 1, S3 `R` (laser off, servos centred), idle, falling chime.
No buttons: the S3's BOOT button isn't used, and RST/EN is only an emergency reboot of the S3 (its
firmware boots with the laser off; the laptop keeps running and reconnects).

Wiring (see tools/try_voice.py, later main.py):
    session = Session(state, speech, agent, aimer, steps, link)   # attaches itself to the pipeline
    actions.session = session                                     # START_OVER intent -> reset()
"""
from __future__ import annotations

import re
import unicodedata

from loguru import logger
from rapidfuzz import fuzz

from .contracts import TFT_AGENT, TFT_STATUS, TFT_STEP, AgentAPI, AimerAPI, ControlLinkAPI, State, StepsAPI

WAKE_NAME = "remy"
WAKE_GREETINGS = {"hey", "hi", "hay", "a", "ay", "ok", "okay", "hello", "yo", "hei", "he"}
WAKE_NAME_MIN = 75      # rapidfuzz ratio: remi/ramy/romy 75, remmy 89. "army" 50, "ready" 67.
START_TEXT = "[session start: greet briefly and explain step 1]"


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
        self._show_idle()

    # -- SessionAPI -------------------------------------------------------------------------------

    async def start(self) -> None:
        """Start without a wake phrase (tests, dashboard button): chime + greeting turn."""
        if await self._activate("start()"):
            await self.speech.agent_turn(START_TEXT)

    async def reset(self) -> None:
        # Go idle first, so nothing still in flight starts a new turn.
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

    # -- hooks ------------------------------------------------------------------------------------

    async def heard(self, text: str) -> bool:
        """Voice pipeline, while idle: wake phrase in this (partial) utterance? Then activate now."""
        if find_wake(text) is None:
            return False
        return await self._activate(f"wake phrase in {text!r}")

    def first_turn(self, text: str) -> str:
        """Agent text for the utterance that woke the session: greeting + anything said after the name."""
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

    def _show_idle(self) -> None:
        self._show(TFT_STATUS, "say: hey Remy")
        self._show(TFT_STEP, "")
        self._show(TFT_AGENT, "")

    def _show(self, line: int, text: str) -> None:
        if self.link is not None:
            self.link.show_text(line, text)
