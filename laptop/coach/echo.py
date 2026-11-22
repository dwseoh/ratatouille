"""Echo gate (B, checkpoint 4): the always-on mic hears the coach's own voice from the speaker.

While the coach is speaking, and for `tail_s` after it stops (Bluetooth speakers lag ~0.2 s), the gate is
CLOSED: user speech is ignored, except a stop word ("stop", "wait", "hold on"...) or a pause phrase ("give me a
sec": voice.py then pauses the session) at the end of what the mic heard, which starts a user turn. That turn start is what cuts the coach's audio (Pipecat interruption)
and cancels the agent turn, as a normal barge-in would.

Two parts share one `EchoGate`:
- `EchoGateStartStrategy` decides when a user turn starts (replaces Pipecat's default VAD + transcription
  start strategies, and behaves like them while the gate is open).
- `AgentProcessor` (voice.py) drops transcripts and VAD "user talking" that arrive while the gate is
  closed and no user turn is open, so the coach's own words never reach the intent router or the agent.

Turn it off with ECHO_GATE=0 when using headphones (then talking over the coach interrupts it again).
"""
from __future__ import annotations

import time

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.turns.user_start.base_user_turn_start_strategy import BaseUserTurnStartStrategy

from .intents import ends_with_pause, ends_with_stop


class EchoGate:
    """Is the coach (probably) audible in the mic right now? Updated from Bot*SpeakingFrames by whoever
    sees them first; the updates are idempotent."""

    def __init__(self, tail_s: float = 0.3, *, enabled: bool = True, clock=time.monotonic):
        self.tail_s, self.enabled, self._clock = tail_s, enabled, clock
        self._speaking = False
        self._stopped_at = float("-inf")
        self._echo_segment = False      # the current/last VAD speech segment started while closed

    def bot_started(self) -> None:
        self._speaking = True

    def bot_stopped(self) -> None:
        if self._speaking:
            self._speaking = False
            self._stopped_at = self._clock()

    def vad_started(self) -> None:
        self._echo_segment = self.closed

    def update(self, frame: Frame) -> None:
        if isinstance(frame, BotStartedSpeakingFrame):
            self.bot_started()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.bot_stopped()
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self.vad_started()

    @property
    def closed(self) -> bool:
        """The coach is audible now: ignore new speech."""
        if not self.enabled:
            return False
        return self._speaking or self._clock() - self._stopped_at < self.tail_s

    @property
    def suppressing(self) -> bool:
        """Ignore transcripts: the gate is closed, or they belong to speech that began while it was closed
        (Deepgram's final for the coach's last words can land after the tail)."""
        return self.closed or (self.enabled and self._echo_segment)


class EchoGateStartStrategy(BaseUserTurnStartStrategy):
    """Gate open: a turn starts on VAD speech, or on a transcript if VAD missed it (Pipecat's defaults).
    Gate closed: a turn starts only on a stop word at the end of a transcript (interim or final)."""

    def __init__(self, gate: EchoGate, **kwargs):
        super().__init__(**kwargs)
        self.gate = gate

    async def process_frame(self, frame: Frame):
        self.gate.update(frame)   # sees VAD before AgentProcessor (which only reads the gate for VAD)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            if not self.gate.closed:
                await self.report_user_turn_started()
        elif isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            if not self.gate.suppressing:
                await self.report_user_turn_started()
            elif ends_with_stop(frame.text) or ends_with_pause(frame.text):     # "give me a sec" pauses
                logger.info(f"echo gate: stop word over the coach: {frame.text!r}")
                await self.report_user_turn_started()
