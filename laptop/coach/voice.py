"""Voice (B): mic -> Silero VAD -> Deepgram STT -> smart-turn -> AgentProcessor -> Deepgram Aura TTS -> speaker.

Built on Pipecat 1.16 (pinned in requirements.txt). Implements SpeechAPI from contracts.py and is the
seam to C's AgentAPI. Pipecat is NOT used for the LLM: `AgentProcessor` hands each finished user
utterance to `agent.turn(text, frames.recent(3), on_sentence)`, and every sentence it passes to
`on_sentence` becomes a TTS frame at once.

Turn end (who decides "the user is finished"):
  Silero VAD reports a pause after `cfg.vad_stop_secs` (0.2 s). Pipecat's smart-turn model then judges
  from the audio whether the sentence sounds finished. If yes, the turn ends as soon as Deepgram's
  finalized transcript is in; if it isn't sure, the turn ends after `cfg.turn_fallback_secs` (0.9 s)
  of silence. `UserTurnProcessor` emits UserStartedSpeakingFrame / UserStoppedSpeakingFrame for this.

Barge-in: when a user turn starts, `UserTurnProcessor` cuts the coach's audio (InterruptionFrame) and
`AgentProcessor` cancels the running agent turn (`agent.cancel()` + the task). If the cut-off turn
hadn't said anything yet, its text is merged into the next turn instead of being answered twice.

State fields written here (contracts.State, B is the only writer):
  transcript, user_talking, speaking, last_activity, turns (one TurnLatency per user turn).

Try it:  cd laptop && python -m tools.try_voice     (headphones until the echo gate exists)
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from loguru import logger
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    DataFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from .intents import IntentActions
from .contracts import (
    LAT_FIRST_AUDIO,
    LAT_POINT,
    LAT_SPEECH_END,
    AgentAPI,
    FrameSource,
    Priority,
    State,
    TurnLatency,
)

FRAMES_PER_TURN = 3


@dataclass
class _TurnEndFrame(DataFrame):
    """Internal marker. UserStoppedSpeakingFrame is a system frame and overtakes the data queue, so the
    final TranscriptionFrame (pushed just before it) may not be processed yet. Re-queueing this marker
    behind it makes the turn start only after every transcript of the turn has been read."""


class AgentProcessor(FrameProcessor):
    """Sits after the turn detector, before TTS. One `agent.turn` per finished user turn."""

    def __init__(self, agent: AgentAPI, frames: FrameSource, state: State, *,
                 actions: IntentActions | None = None, **kwargs):
        super().__init__(**kwargs)
        self.agent, self.frames, self.state = agent, frames, state
        self.actions = actions             # intent router reflexes (point, stop, done, start over)
        self._text: list[str] = []         # final transcripts of the current user turn
        self._carry = ""                   # a turn cut off before it said anything
        self._speech_end: float | None = None
        self._task: asyncio.Task | None = None
        self._task_text = ""
        self._task_spoke = False
        self._lat: TurnLatency | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        st = self.state

        # Transcripts are consumed here, never forwarded: TTS would read any TextFrame out loud.
        if isinstance(frame, InterimTranscriptionFrame):
            st.transcript = " ".join(self._text + [frame.text])
            if self.actions and st.session == "active":
                await self.actions.on_interim(st.transcript)
            return
        if isinstance(frame, TranscriptionFrame):
            self._text.append(frame.text)
            st.transcript = " ".join(self._text)
            return
        if isinstance(frame, _TurnEndFrame):
            await self._start_turn()
            return

        if isinstance(frame, VADUserStartedSpeakingFrame):
            st.user_talking, st.last_activity = True, time.monotonic()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            st.user_talking, st.last_activity = False, time.monotonic()
            self._speech_end = time.monotonic() - frame.stop_secs   # when the user actually went quiet
        elif isinstance(frame, UserStartedSpeakingFrame):
            await self._cancel_turn("barge-in")        # Pipecat already cut the audio
            if self.actions:
                self.actions.new_utterance()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            await self.queue_frame(_TurnEndFrame())      # runs after the queued transcripts
        elif isinstance(frame, BotStartedSpeakingFrame):
            st.speaking, st.last_activity = True, time.monotonic()
            if self._lat:
                self._lat.mark(LAT_FIRST_AUDIO)
                logger.info(f"turn {self._lat.turn_id} latency ms: {self._lat.ms()}")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            st.speaking, st.last_activity = False, time.monotonic()

        await self.push_frame(frame, direction)

    # -- turns ------------------------------------------------------------------------------------

    async def _start_turn(self):
        text = " ".join(t for t in self._text if t).strip()
        self._text.clear()
        if not text or self.state.session != "active":     # idle: speech is ignored
            return
        if self._carry:
            text, self._carry = f"{self._carry} {text}", ""
        self.state.transcript = text
        self._lat = TurnLatency(turn_id=len(self.state.turns) + 1)
        self._lat.marks[LAT_SPEECH_END] = self._speech_end or time.monotonic()
        self.state.turns.append(self._lat)
        if self.actions:
            skip_agent = await self.actions.on_final(text)
            point_t = self.actions.take_point_time()
            if point_t is not None:     # usually before speech_end: the laser moved while you talked
                self._lat.marks[LAT_POINT] = point_t
            if skip_agent or self.state.session != "active":   # STOP / START_OVER
                logger.info(f"user turn {self._lat.turn_id}: {text!r} handled locally, no agent turn")
                return
        logger.info(f"user turn {self._lat.turn_id}: {text!r} "
                    f"(+{(time.monotonic() - self._lat.marks[LAT_SPEECH_END]) * 1000:.0f} ms after speech end)")
        self._task_text, self._task_spoke = text, False
        self._task = self.create_task(self._run_turn(text), "agent_turn")

    async def _run_turn(self, text: str):
        try:
            stats = await self.agent.turn(text, self.frames.recent(FRAMES_PER_TURN), self.speak)
            logger.debug(f"agent turn done: {stats}")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("agent turn failed")

    async def _cancel_turn(self, why: str):
        if self._task is None or self._task.done():
            return
        if not self._task_spoke:              # still thinking: answer both utterances in one turn
            self._carry = self._task_text
        logger.info(f"{why}: cancelling agent turn")
        self.agent.cancel()
        task, self._task = self._task, None
        await self.cancel_task(task)

    # -- SpeechAPI backing ------------------------------------------------------------------------

    async def speak(self, text: str) -> None:
        text = text.strip()
        if text:
            self._task_spoke = True
            await self.push_frame(TTSSpeakFrame(text))

    async def stop(self) -> None:
        await self._cancel_turn("stop")
        await self.broadcast_interruption()   # cuts TTS + queued audio


class Speech:
    """SpeechAPI (contracts C5) on top of the running pipeline. C's agent and check-ins call this."""

    def __init__(self, proc: AgentProcessor, state: State):
        self._proc, self._state = proc, state

    @property
    def speaking(self) -> bool:
        return self._state.speaking

    async def say(self, text: str, priority: Priority = Priority.REPLY) -> None:
        # v1: no priority queue. PROACTIVE callers check State.user_talking / speaking themselves.
        await self._proc.speak(text)

    def stop(self) -> None:
        self._proc.create_task(self._proc.stop(), "speech_stop")


def build_pipeline(cfg, agent: AgentAPI, frames: FrameSource, state: State, *,
                   actions: IntentActions | None = None,
                   input_device: int | None = None, output_device: int | None = None):
    """Return (worker, speech). Run the worker with a WorkerRunner (see tools/try_voice.py)."""
    if not cfg.deepgram_api_key:
        raise RuntimeError("DEEPGRAM_API_KEY is not set (laptop/.env)")
    transport = LocalAudioTransport(LocalAudioTransportParams(
        audio_in_enabled=True, audio_out_enabled=True,
        input_device_index=input_device, output_device_index=output_device))
    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=cfg.vad_stop_secs)))
    stt = DeepgramSTTService(
        api_key=cfg.deepgram_api_key,
        # Boost every way the recipe objects get said, so "cilantro" isn't heard as "silent row".
        settings=DeepgramSTTService.Settings(keyterm=sorted(state.recipe.names())))
    turns = UserTurnProcessor(user_turn_strategies=UserTurnStrategies(stop=[
        TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3(
            params=SmartTurnParams(stop_secs=cfg.turn_fallback_secs)))]))
    proc = AgentProcessor(agent, frames, state, actions=actions)
    tts = DeepgramTTSService(api_key=cfg.deepgram_api_key,
                             settings=DeepgramTTSService.Settings(voice=cfg.tts_voice))
    pipeline = Pipeline([transport.input(), vad, stt, turns, proc, tts, transport.output()])
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
        idle_timeout_secs=None,   # the coach listens all session; never auto-cancel when quiet
        enable_rtvi=False,        # no web client
        # A dead STT/TTS (bad key, no network) would leave the coach silently deaf: stop instead.
        # TODO(B5): switch to the offline fallback (macOS `say` + typed input) rather than exiting.
        processor_unusable_policy=ProcessorUnusablePolicy.CANCEL,
    )
    speech = Speech(proc, state)
    if actions:
        actions.speech = speech        # STOP intent -> speech.stop()
    return worker, speech
