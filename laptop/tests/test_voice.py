"""V1 turn-taking tests: TurnProcessor driven with synthetic VAD / STT frames (no mic, no network)."""
import asyncio

from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    InterruptionFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.tests.utils import SleepFrame, run_test

from coach.voice import TurnProcessor, echo_turn, keyword_target


def final(text, finalized=True):
    return TranscriptionFrame(text, "user", "", finalized=finalized)


def interim(text):
    return InterimTranscriptionFrame(text, "user", "")


def spoken(down):
    return [f.text for f in down if isinstance(f, TTSSpeakFrame)]


def run(proc, frames):
    down, _ = asyncio.run(run_test(proc, frames_to_send=frames))
    return down


def test_finalized_transcript_ends_turn_and_is_spoken():
    turns = []

    async def on_turn(text, t_end, say):
        turns.append(text)
        await say("Step one: halve the avocado.")

    down = run(TurnProcessor(on_turn), [
        VADUserStartedSpeakingFrame(), interim("what's step"),
        VADUserStoppedSpeakingFrame(), final("What's step one?"), SleepFrame(sleep=0.2),
    ])
    assert turns == ["What's step one?"]
    assert spoken(down) == ["Step one: halve the avocado."]


def test_transcripts_are_not_forwarded_to_tts():
    down = run(TurnProcessor(echo_turn), [
        VADUserStartedSpeakingFrame(), interim("hello"),
        VADUserStoppedSpeakingFrame(), final("hello"), SleepFrame(sleep=0.2),
    ])
    assert not any(isinstance(f, (TranscriptionFrame, InterimTranscriptionFrame)) for f in down)
    assert spoken(down) == ["You said: hello"]


def test_timer_ends_turn_when_finalize_reply_is_empty():
    # Deepgram sent the text as a regular final before Finalize -> no finalized frame ever comes.
    turns = []

    async def on_turn(text, t_end, say):
        turns.append(text)

    run(TurnProcessor(on_turn), [
        VADUserStartedSpeakingFrame(), final("where's the lime", finalized=False),
        VADUserStoppedSpeakingFrame(), SleepFrame(sleep=0.6),
    ])
    assert turns == ["where's the lime"]


def test_pause_mid_sentence_is_one_turn():
    turns = []

    async def on_turn(text, t_end, say):
        turns.append(text)

    run(TurnProcessor(on_turn), [
        VADUserStartedSpeakingFrame(), final("grab the", finalized=False),
        VADUserStoppedSpeakingFrame(), SleepFrame(sleep=0.1),     # resumes before the timer fires
        VADUserStartedSpeakingFrame(), final("lime please", finalized=False),
        VADUserStoppedSpeakingFrame(), final("", finalized=True), SleepFrame(sleep=0.6),
    ])
    assert turns == ["grab the lime please"]


def test_barge_in_while_thinking_cancels_and_merges():
    turns, barge_ins = [], []

    async def slow(text, t_end, say):
        turns.append(text)
        await asyncio.sleep(1.0)           # "thinking" — never gets to speak
        await say("too late")

    down = run(TurnProcessor(slow, on_barge_in=lambda: barge_ins.append(1)), [
        VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame(), final("where's the"),
        SleepFrame(sleep=0.1),
        VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame(), final("lime"),
        SleepFrame(sleep=0.1),
    ])
    assert turns == ["where's the", "where's the lime"]
    assert barge_ins == [1]
    assert any(isinstance(f, InterruptionFrame) for f in down)


def test_barge_in_while_speaking_interrupts():
    barge_ins = []
    proc = TurnProcessor(echo_turn, on_barge_in=lambda: barge_ins.append(1))
    proc.speaking = True   # as if the output transport sent BotStartedSpeakingFrame upstream
    down = run(proc, [VADUserStartedSpeakingFrame(), SleepFrame(sleep=0.05)])
    assert barge_ins == [1]
    assert any(isinstance(f, InterruptionFrame) for f in down)


def test_keyword_target_plurals():
    objs = ["avocado", "lime", "cutting board"]
    assert keyword_target("where are the limes", objs) == "lime"
    assert keyword_target("grab the cutting board", objs) == "cutting board"
    assert keyword_target("hello there", objs) is None
