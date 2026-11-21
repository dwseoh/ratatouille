"""Voice pipeline (V1): mic -> Silero VAD -> Deepgram STT -> TurnProcessor -> Deepgram Aura TTS -> speaker.

Built on Pipecat 1.16 (pinned in requirements.txt). The LLM step is NOT Pipecat's LLM service:
`TurnProcessor` hands the finished user utterance to a plain async callback,

    on_turn(text, t_speech_end, say)

and every sentence the callback passes to `await say(sentence)` is spoken at once. C's agent plugs in
here (`agent.turn(text, frames, on_sentence=say)`) once contracts.py lands; until then `echo_turn`
stands in.

Turn end: Silero waits `cfg.vad_stop_secs` (0.8 s) of silence, Pipecat then asks Deepgram to finalize,
and the finalized transcript ends the turn. If Deepgram already sent everything (its finalize reply
is empty) a short timer ends the turn instead.

Barge-in: when the user starts speaking, any audio playing is cut (Pipecat InterruptionFrame), the
running turn task is cancelled and `on_barge_in()` fires (wire to `agent.cancel()`). If the cancelled
turn hadn't spoken yet, its text is merged into the next turn instead of being answered twice.

Run standalone:  python -m coach.voice   (echo bot; use headphones until V3 echo handling exists)
"""
import asyncio
import re
import time
from typing import Awaitable, Callable

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
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
from pipecat.workers.runner import WorkerRunner

Say = Callable[[str], Awaitable[None]]
OnTurn = Callable[[str, float, Say], Awaitable[None]]   # (user_text, t_speech_end monotonic, say)


class TurnProcessor(FrameProcessor):
    """Sits between STT and TTS. Turns transcripts into one call to `on_turn` per user utterance."""

    FINALIZE_WAIT_S = 0.3   # after VAD stop, wait this long for Deepgram's finalized transcript
    MAX_WAIT_S = 1.0        # ...or up to this long if only interim text has arrived so far

    def __init__(self, on_turn: OnTurn, *, on_interim: Callable[[str], None] | None = None,
                 on_barge_in: Callable[[], None] | None = None, **kwargs):
        super().__init__(**kwargs)
        self._on_turn = on_turn
        self._on_interim = on_interim          # V4 keyword pointing + V5 wake phrase hook here
        self._on_barge_in = on_barge_in
        self._finals: list[str] = []
        self._interim = ""
        self._carry = ""                        # text of a turn cut off before it said anything
        self._user_speaking = False
        self._speech_end: float | None = None
        self._end_timer: asyncio.Task | None = None
        self._turn: asyncio.Task | None = None
        self._turn_text = ""
        self._turn_spoke = False
        self.speaking = False                   # bot audio is playing (from the output transport)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._user_speaking, self._speech_end = True, None
            await self._cancel_end_timer()
            await self._barge_in()
            await self.push_frame(frame, direction)
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._user_speaking, self._speech_end = False, time.monotonic()
            await self._cancel_end_timer()
            self._end_timer = self.create_task(self._end_after_wait(), "turn_end")
            await self.push_frame(frame, direction)
        elif isinstance(frame, InterimTranscriptionFrame):
            # Consumed, not forwarded: TTS would read any TextFrame out loud.
            self._interim = frame.text
            if self._on_interim:
                self._on_interim(frame.text)
        elif isinstance(frame, TranscriptionFrame):
            self._finals.append(frame.text)
            self._interim = ""
            if frame.finalized and not self._user_speaking:
                await self._cancel_end_timer()
                await self._end_turn()
        elif isinstance(frame, BotStartedSpeakingFrame):
            self.speaking = True
            await self.push_frame(frame, direction)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.speaking = False
            await self.push_frame(frame, direction)
        else:
            await self.push_frame(frame, direction)

    async def speak(self, text: str) -> None:
        """Queue one sentence for TTS. Also the hook V2's Speech.say builds on."""
        text = text.strip()
        if text:
            self._turn_spoke = True
            await self.push_frame(TTSSpeakFrame(text))

    # -- turn end ---------------------------------------------------------------------------------

    async def _end_after_wait(self):
        deadline = time.monotonic() + self.MAX_WAIT_S
        await asyncio.sleep(self.FINALIZE_WAIT_S)
        while not self._finals and self._interim and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self._end_timer = None
        await self._end_turn()

    async def _cancel_end_timer(self):
        if self._end_timer:
            task, self._end_timer = self._end_timer, None
            await self.cancel_task(task)

    async def _end_turn(self):
        text = " ".join(self._finals).strip() or self._interim.strip()
        self._finals.clear()
        self._interim = ""
        if not text:
            return
        if self._carry:
            text, self._carry = f"{self._carry} {text}", ""
        t_end = self._speech_end or time.monotonic()
        logger.info(f"user turn ({(time.monotonic() - t_end) * 1000:.0f} ms after speech end): {text!r}")
        self._turn_text, self._turn_spoke = text, False
        self._turn = self.create_task(self._run_turn(text, t_end), "user_turn")

    async def _run_turn(self, text: str, t_end: float):
        try:
            await self._on_turn(text, t_end, self.speak)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("turn handler failed")

    # -- barge-in ---------------------------------------------------------------------------------

    async def _barge_in(self):
        busy = self._turn is not None and not self._turn.done()
        if busy:
            if not self._turn_spoke:          # still thinking: answer both utterances in one turn
                self._carry = self._turn_text
            await self.cancel_task(self._turn)
        self._turn = None
        # Always interrupt: clears TTS + output audio even if it is between sentences.
        await self.broadcast_interruption()
        if (busy or self.speaking) and self._on_barge_in:
            self._on_barge_in()


def build_pipeline(on_turn: OnTurn, cfg, *, on_interim=None, on_barge_in=None,
                   input_device: int | None = None, output_device: int | None = None):
    """Return (worker, turn_processor). Run the worker with a WorkerRunner."""
    if not cfg.deepgram_api_key:
        raise RuntimeError("DEEPGRAM_API_KEY is not set (laptop/.env)")
    transport = LocalAudioTransport(LocalAudioTransportParams(
        audio_in_enabled=True, audio_out_enabled=True,
        input_device_index=input_device, output_device_index=output_device))
    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=cfg.vad_stop_secs)))
    stt = DeepgramSTTService(api_key=cfg.deepgram_api_key)
    tts = DeepgramTTSService(api_key=cfg.deepgram_api_key,
                             settings=DeepgramTTSService.Settings(voice=cfg.tts_voice))
    turns = TurnProcessor(on_turn, on_interim=on_interim, on_barge_in=on_barge_in)
    pipeline = Pipeline([transport.input(), vad, stt, turns, tts, transport.output()])
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
        idle_timeout_secs=None,   # the coach listens all session; never auto-cancel when quiet
        enable_rtvi=False,        # no web client
        # A dead STT/TTS (bad key, no network) would leave the coach silently deaf: stop instead.
        # TODO(B5): switch to the offline fallback (macOS `say` + typed input) rather than exiting.
        processor_unusable_policy=ProcessorUnusablePolicy.CANCEL,
    )
    return worker, turns


async def echo_turn(text: str, t_speech_end: float, say: Say) -> None:
    """Stand-in for C's agent: repeats the user, one sentence at a time."""
    await say(f"You said: {text}")


def keyword_target(partial: str, objects: list[str]) -> str | None:
    """Point-first: if the user names a recipe object, return it so the laser moves at once."""
    text = partial.lower()
    for obj in objects:
        if re.search(rf"\b{re.escape(obj.lower())}s?\b", text):
            return obj
    return None


async def _main():
    from .config import CONFIG

    worker, _ = build_pipeline(echo_turn, CONFIG,
                               on_interim=lambda t: logger.debug(f"interim: {t}"),
                               on_barge_in=lambda: logger.info("barge-in"))
    runner = WorkerRunner()
    await runner.add_workers(worker)
    await runner.run()


if __name__ == "__main__":
    asyncio.run(_main())
