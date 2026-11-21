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

Echo gate (echo.py, ECHO_GATE=1 by default): while the coach is audible, the mic hears it, so only a stop
word starts a user turn (and cuts the audio); everything else heard then is dropped. With headphones,
ECHO_GATE=0 brings back barge-in by any speech.

Devices: MIC_NAME / SPEAKER_NAME (.env) pick the mic and speaker by name substring, never "default"
(macOS switches the default whenever something connects). `python -m tools.try_voice --devices` lists them.

State fields written here (contracts.State, B is the only writer):
  transcript, user_talking, speaking, last_activity, turns (one TurnLatency per user turn).

Try it:  cd laptop && python -m tools.try_voice
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import numpy as np
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
    InterruptionFrame,
    OutputAudioRawFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
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

from .echo import EchoGate, EchoGateStartStrategy
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
CHIME_RATE = 24000      # = PipelineParams.audio_out_sample_rate


def _chime_frame(kind: str) -> OutputAudioRawFrame:
    """Two soft notes, ~0.25 s. Rising = session starting, falling = reset."""
    notes = (660.0, 990.0) if kind == "start" else (660.0, 440.0)
    parts = []
    for f in notes:
        t = np.arange(int(CHIME_RATE * 0.12)) / CHIME_RATE
        env = np.minimum(1.0, np.minimum(t / 0.01, (t[-1] - t) / 0.04))   # 10 ms attack, 40 ms release
        parts.append(0.3 * env * np.sin(2 * np.pi * f * t))
    pcm = (np.concatenate(parts) * 32767).astype(np.int16).tobytes()
    return OutputAudioRawFrame(audio=pcm, sample_rate=CHIME_RATE, num_channels=1)


@dataclass
class _TurnEndFrame(DataFrame):
    """Internal marker. UserStoppedSpeakingFrame is a system frame and overtakes the data queue, so the
    final TranscriptionFrame (pushed just before it) may not be processed yet. Re-queueing this marker
    behind it makes the turn start only after every transcript of the turn has been read."""


class AgentProcessor(FrameProcessor):
    """Sits after the turn detector, before TTS. One `agent.turn` per finished user turn."""

    def __init__(self, agent: AgentAPI, frames: FrameSource, state: State, *,
                 actions: IntentActions | None = None, gate: EchoGate | None = None, **kwargs):
        super().__init__(**kwargs)
        self.agent, self.frames, self.state = agent, frames, state
        self.gate = gate or EchoGate(enabled=False)   # shared with EchoGateStartStrategy
        self._stop_turn = False            # this user turn began over the coach: it's a stop word
        self.actions = actions             # intent router reflexes (point, stop, done, start over)
        self.session = None                # Session attaches itself: wake phrase while idle
        self.captions = None               # CaptionTimeline (dashboard): one caption at a time, in step with the voice
        self._woke = False                 # this utterance contained the wake phrase
        self._text: list[str] = []         # final transcripts of the current user turn
        self._carry = ""                   # a turn cut off before it said anything: what was sent to the agent...
        self._carry_user = ""              # ...and what the USER actually said (no internal prompts)
        self._speech_end: float | None = None
        self._task: asyncio.Task | None = None
        self._task_text = ""               # text sent to the agent (may hold an internal prompt, e.g. the greeting)
        self._task_user = ""               # just the user's words from that turn
        self._task_spoke = False
        self._lat: TurnLatency | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        st = self.state
        self.gate.update(frame)

        # Transcripts are consumed here, never forwarded: TTS would read any TextFrame out loud.
        if isinstance(frame, (InterimTranscriptionFrame, TranscriptionFrame)) and (
                self.gate.suppressing or self._stop_turn):
            logger.debug(f"echo gate: dropped {frame.text!r}")   # the coach's own voice (or a stop word)
            return
        if isinstance(frame, InterimTranscriptionFrame):
            st.transcript = " ".join(self._text + [frame.text])
            if st.session != "active":
                if self.session and not self._woke and await self.session.heard(st.transcript):
                    self._woke = True          # chime plays now; the greeting turn runs at utterance end
            elif self.actions:
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
            if not self.gate.suppressing:                 # else probably the coach in the mic
                st.user_talking, st.last_activity = True, time.monotonic()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            st.user_talking, st.last_activity = False, time.monotonic()
            self._speech_end = time.monotonic() - frame.stop_secs   # when the user actually went quiet
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._stop_turn = self.gate.suppressing      # only a stop word starts a turn over the coach
            st.user_talking, st.last_activity = True, time.monotonic()
            await self._cancel_turn("stop word" if self._stop_turn else "barge-in")   # audio already cut
            self._woke = False
            if self.actions:
                self.actions.new_utterance()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            await self.queue_frame(_TurnEndFrame())      # runs after the queued transcripts
        elif isinstance(frame, BotStartedSpeakingFrame):
            st.speaking, st.last_activity = True, time.monotonic()
            if self.captions:
                self.captions.playback_started()
            if self._lat:
                self._lat.mark(LAT_FIRST_AUDIO)
                logger.info(f"turn {self._lat.turn_id} latency ms: {self._lat.ms()}")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            st.speaking, st.last_activity = False, time.monotonic()
            if self.captions:
                self.captions.playback_stopped()

        await self.push_frame(frame, direction)

    # -- turns ------------------------------------------------------------------------------------

    async def _start_turn(self):
        text = " ".join(t for t in self._text if t).strip()
        self._text.clear()
        if self._stop_turn:                    # audio cut + agent cancelled at turn start; nothing to answer
            self._stop_turn = False
            logger.info("stop word over the coach: stopped, no agent turn")
            return
        if not text:
            return
        if self.state.session != "active":     # idle: only the wake phrase counts
            if not (self.session and not self._woke and await self.session.heard(text)):
                return
            self._woke = True
        user_text = text
        if self._woke:                         # "hey Remy, ..." -> one greeting turn (+ whatever else was said)
            self._woke, self._carry, self._carry_user = False, "", ""
            text = self.session.first_turn(text)
        elif self._carry:
            # The agent gets both turns (it may still need the greeting instruction); the transcript and the intent
            # router only ever get what the user said.
            text, user_text = f"{self._carry} {text}", f"{self._carry_user} {text}".strip()
            self._carry = self._carry_user = ""
        self.state.transcript = user_text
        self._lat = TurnLatency(turn_id=len(self.state.turns) + 1)
        self._lat.marks[LAT_SPEECH_END] = self._speech_end or time.monotonic()
        self.state.turns.append(self._lat)
        if self.actions:
            skip_agent = await self.actions.on_final(user_text)
            point_t = self.actions.take_point_time()
            if point_t is not None:     # usually before speech_end: the laser moved while you talked
                self._lat.marks[LAT_POINT] = point_t
            if skip_agent or self.state.session != "active":   # STOP / START_OVER
                logger.info(f"user turn {self._lat.turn_id}: {text!r} handled locally, no agent turn")
                return
        logger.info(f"user turn {self._lat.turn_id}: {text!r} "
                    f"(+{(time.monotonic() - self._lat.marks[LAT_SPEECH_END]) * 1000:.0f} ms after speech end)")
        self._launch(text, user_text=user_text)

    def _launch(self, text: str, *, carry: bool = True, user_text: str | None = None):
        self._task_text, self._task_spoke = (text if carry else ""), False
        self._task_user = (user_text if user_text is not None else text) if carry else ""
        self._task = self.create_task(self._run_turn(text), "agent_turn")

    async def agent_turn(self, text: str) -> None:
        """Start an agent turn the user didn't trigger (session greeting, check-ins). Returns at once.
        Cancelled by barge-in like any other turn, but never merged into the user's next turn."""
        await self._cancel_turn("new agent turn")
        self._launch(text, carry=False)

    async def _run_turn(self, text: str):
        try:
            # camera dropped: send no frames at all (the agent then tells the user once) rather than stale ones
            frames = self.frames.recent(FRAMES_PER_TURN) if self.frames.connected else []
            stats = await self.agent.turn(text, frames, self.speak)
            logger.debug(f"agent turn done: {stats}")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("agent turn failed")

    async def _cancel_turn(self, why: str):
        if self._task is None or self._task.done():
            return
        if not self._task_spoke and self._task_text:   # still thinking: answer both utterances in one turn
            self._carry, self._carry_user = self._task_text, self._task_user
        logger.info(f"{why}: cancelling agent turn")
        self.agent.cancel()
        task, self._task = self._task, None
        await self.cancel_task(task)

    # -- SpeechAPI backing ------------------------------------------------------------------------

    async def speak(self, text: str) -> None:
        text = text.strip()
        if text:
            self._task_spoke = True
            if self.captions:
                self.captions.queue(text)
            await self.push_frame(TTSSpeakFrame(text))

    async def stop(self) -> None:
        if self.captions:
            self.captions.reset()
        await self._cancel_turn("stop")
        await self.broadcast_interruption()   # cuts TTS + queued audio

    async def chime(self, kind: str) -> None:
        """Play a short local tone: "start" (rising) or "reset" (falling). No network, plays at once."""
        await self.push_frame(_chime_frame(kind))


class CaptionTap(FrameProcessor):
    """Sits right after the TTS. Passes everything through untouched, but measures how many seconds of audio each
    sentence became, so the dashboard caption changes exactly when the voice moves on to the next sentence."""

    def __init__(self, captions, **kwargs):
        super().__init__(**kwargs)
        self.captions = captions

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSStartedFrame):
            self.captions.audio_started()
        elif isinstance(frame, TTSAudioRawFrame):
            self.captions.audio_chunk(len(frame.audio) / (2 * max(frame.num_channels, 1) * frame.sample_rate))
        elif isinstance(frame, TTSStoppedFrame):
            self.captions.audio_stopped()
        elif isinstance(frame, InterruptionFrame):       # the user talked over the coach: the rest won't be spoken
            self.captions.reset()
        await self.push_frame(frame, direction)


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

    async def interrupt(self) -> None:
        """Like stop(), but returns once the audio is cut (so a chime pushed next isn't cut too)."""
        await self._proc.stop()

    async def chime(self, kind: str) -> None:
        await self._proc.chime(kind)

    def attach_session(self, session) -> None:
        """Session hooks the pipeline: heard(text) -> bool while idle, first_turn(text) -> agent text."""
        self._proc.session = session

    async def agent_turn(self, text: str) -> None:
        """Not in SpeechAPI: run an agent turn without user speech (Session greeting, check-ins).
        Returns once started; the sentences are spoken as they stream in."""
        await self._proc.agent_turn(text)


def list_devices() -> list[dict]:
    """PortAudio devices as PyAudio numbers them (LocalAudioTransport wants PyAudio indexes)."""
    import pyaudio
    pa = pyaudio.PyAudio()
    try:
        return [pa.get_device_info_by_index(i) for i in range(pa.get_device_count())]
    finally:
        pa.terminate()


def find_device(name: str, kind: str, devices: list[dict] | None = None) -> int | None:
    """Index of the first `kind` ("input" / "output") device whose name contains `name` (any case).
    Empty name -> None (the macOS default). No match -> RuntimeError listing what exists."""
    if not name:
        return None
    devices = devices if devices is not None else list_devices()
    key = "maxInputChannels" if kind == "input" else "maxOutputChannels"
    usable = [d for d in devices if d.get(key, 0) > 0]
    for d in usable:
        if name.lower() in d["name"].lower():
            return int(d["index"])
    names = ", ".join(repr(d["name"]) for d in usable) or "none"
    raise RuntimeError(f"no {kind} device matching {name!r}. {kind} devices: {names}")


def build_pipeline(cfg, agent: AgentAPI, frames: FrameSource, state: State, *,
                   actions: IntentActions | None = None, captions=None,
                   input_device: int | None = None, output_device: int | None = None):
    """Return (worker, speech). Run the worker with a WorkerRunner (see tools/try_voice.py).
    Devices: explicit indexes win, else MIC_NAME / SPEAKER_NAME from cfg, else the macOS default."""
    if not cfg.deepgram_api_key:
        raise RuntimeError("DEEPGRAM_API_KEY is not set (laptop/.env)")
    if input_device is None:
        input_device = find_device(cfg.mic_name, "input")
    if output_device is None:
        output_device = find_device(cfg.speaker_name, "output")
    logger.info(f"audio: mic {cfg.mic_name or 'default'!r} -> {input_device}, "
                f"speaker {cfg.speaker_name or 'default'!r} -> {output_device}, "
                f"echo gate {'on' if cfg.echo_gate else 'OFF'}")
    transport = LocalAudioTransport(LocalAudioTransportParams(
        audio_in_enabled=True, audio_out_enabled=True,
        input_device_index=input_device, output_device_index=output_device))
    gate = EchoGate(cfg.echo_tail_s, enabled=cfg.echo_gate)
    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=cfg.vad_stop_secs)))
    stt = DeepgramSTTService(
        api_key=cfg.deepgram_api_key,
        # Boost every way the recipe objects get said, so "cilantro" isn't heard as "silent row".
        settings=DeepgramSTTService.Settings(keyterm=sorted(state.recipe.names()) + ["Remy", "hey Remy"]))
    turns = UserTurnProcessor(user_turn_strategies=UserTurnStrategies(
        start=[EchoGateStartStrategy(gate)],     # = Pipecat's VAD + transcription defaults when the gate is open
        stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3(
            params=SmartTurnParams(stop_secs=cfg.turn_fallback_secs)))]))
    proc = AgentProcessor(agent, frames, state, actions=actions, gate=gate)
    tts = DeepgramTTSService(api_key=cfg.deepgram_api_key,
                             settings=DeepgramTTSService.Settings(voice=cfg.tts_voice))
    stages = [transport.input(), vad, stt, turns, proc, tts]
    if captions is not None:                  # dashboard captions in step with the voice
        proc.captions = captions
        stages.append(CaptionTap(captions))
    pipeline = Pipeline(stages + [transport.output()])
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
        idle_timeout_secs=None,   # the coach listens all session; never auto-cancel when quiet
        enable_rtvi=False,        # no web client
        # A dead STT/TTS (bad key, no network) would leave the coach silently deaf: stop instead.
        # (An offline fallback was cut, see docs/cut-list.md: if STT/TTS dies, the coach stops.)
        processor_unusable_policy=ProcessorUnusablePolicy.CANCEL,
    )
    speech = Speech(proc, state)
    if actions:
        actions.speech = speech        # STOP intent -> speech.stop()
    return worker, speech
