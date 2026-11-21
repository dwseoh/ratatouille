"""Voice pipeline: mic -> VAD/STT (streaming) -> agent -> TTS (streaming) -> speaker.

Provider choice is open (decide Fri night by measuring):
- STT: Deepgram streaming (fast endpointing) vs local faster-whisper / whisper.cpp on the M2
- TTS: a streaming provider (ElevenLabs Flash, Deepgram Aura, Cartesia) vs macOS `say` as a fallback

Latency targets: end of speech -> transcript ~350 ms; first TTS audio ~150 ms after the first sentence.
"""
import asyncio
import re


class Transcriber:
    """Streams mic audio to STT. Calls on_partial(text) as you talk, on_final(text) at end of speech."""

    def __init__(self, on_partial, on_final, on_speech_start):
        self.on_partial, self.on_final, self.on_speech_start = on_partial, on_final, on_speech_start

    async def run(self):
        # TODO: open the default input device (sounddevice, 16 kHz mono), stream 20 ms chunks to STT,
        #       endpointing ~300 ms of silence. Call on_speech_start immediately (barge-in) and
        #       on_partial for each interim transcript.
        raise NotImplementedError


class Speaker:
    """Queues sentences and speaks them with streaming TTS. stop() is for barge-in."""

    def __init__(self):
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._current = None

    async def say(self, sentence: str):
        await self.queue.put(sentence)

    def stop(self):
        while not self.queue.empty():
            self.queue.get_nowait()
        # TODO: also cut off the audio that's playing right now

    async def run(self):
        while True:
            sentence = await self.queue.get()
            # TODO: stream TTS audio to the default output device as it arrives
            proc = await asyncio.create_subprocess_exec("say", sentence)  # macOS fallback
            await proc.wait()

    async def earcon(self):
        """Instant 'I heard you' sound the moment speech ends. TODO: play a short preloaded wav."""


def keyword_target(partial: str, objects: list[str]) -> str | None:
    """Point-first: if the user names a recipe object, return it so the laser moves at once."""
    text = partial.lower()
    for obj in objects:
        if re.search(rf"\b{re.escape(obj.lower())}s?\b", text):
            return obj
    return None
