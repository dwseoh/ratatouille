"""Captions: what the dashboard shows as Remy's words. ONE sentence at a time, in step with the voice.

The coach streams several sentences. They are queued to the text-to-speech here (`queue`); a tap after the TTS reports
how many seconds of audio each sentence became (`audio_started` / `audio_chunk` / `audio_stopped`); the voice pipeline
says when playback starts and stops. From that we know which sentence is being spoken right now, and the next caption
only appears once the voice has finished the previous one. When the voice goes quiet the last caption stays on screen.

Pure logic with an injectable clock; the pipecat wiring is in voice.py (`CaptionTap`).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class _Item:
    text: str
    seconds: float = 0.0         # audio length so far
    started: bool = False        # its audio has begun to arrive
    closed: bool = False         # all of its audio has arrived, so `seconds` is final


class CaptionTimeline:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._items: list[_Item] = []       # sentences not yet fully spoken, oldest first
        self._rec: _Item | None = None      # the sentence whose audio is arriving now
        self._play_start: float | None = None
        self._shown = ""                    # the last caption shown; stays up after the voice stops

    # ----- fed by the coach / the TTS tap / the voice pipeline -----

    def queue(self, sentence: str) -> None:
        sentence = sentence.strip()
        if sentence:
            self._items.append(_Item(sentence))

    def audio_started(self) -> None:
        self._rec = next((i for i in self._items if not i.started), None)
        if self._rec is not None:
            self._rec.started = True

    def audio_chunk(self, seconds: float) -> None:
        if self._rec is not None:
            self._rec.seconds += seconds

    def audio_stopped(self) -> None:
        if self._rec is not None:
            self._rec.closed = True
            self._rec = None

    def playback_started(self) -> None:
        """The speaker started playing (BotStartedSpeaking). The first queued sentence is the one being spoken."""
        self._play_start = self._clock()

    def playback_stopped(self) -> None:
        """The speaker went quiet: every sentence whose audio finished has been spoken."""
        self.current()
        self._items = [i for i in self._items if not i.closed]
        self._play_start = None

    def reset(self) -> None:
        """Barge-in / stop: whatever was queued will not be spoken."""
        self.current()
        self._items, self._rec, self._play_start = [], None, None

    # ----- read by the dashboard -----

    def current(self) -> str:
        """The caption to show right now."""
        if not self._items:
            return self._shown
        if self._play_start is None:                # queued or being generated: show the first one
            self._shown = self._items[0].text
            return self._shown
        elapsed, t, item = self._clock() - self._play_start, 0.0, self._items[-1]
        for i in self._items:
            if not i.closed:                        # still arriving: it is being spoken (or next)
                item = i
                break
            t += i.seconds
            if elapsed < t:
                item = i
                break
        self._shown = item.text
        return self._shown

    @property
    def speaking(self) -> bool:
        return self._play_start is not None and bool(self._items)
