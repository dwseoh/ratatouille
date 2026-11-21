"""Voice tests: AgentProcessor + Speech driven with synthetic turn/STT frames (no mic, no network)."""
import asyncio
from pathlib import Path

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.tests.utils import SleepFrame, run_test

from coach.contracts import LAT_FIRST_AUDIO, LAT_SPEECH_END, FakeAgent, FakeFrames, Recipe, SpeechAPI, State
from coach.voice import AgentProcessor, Speech

RECIPE = Recipe.load(Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json")


class SpyAgent(FakeAgent):
    def __init__(self, delay=0.0):
        super().__init__()
        self.texts, self.cancels, self.delay = [], 0, delay

    async def turn(self, user_text, frames, on_sentence):
        self.texts.append(user_text)
        await asyncio.sleep(self.delay)
        return await super().turn(user_text, frames, on_sentence)

    def cancel(self):
        self.cancels += 1
        super().cancel()


def setup(agent=None, session="active"):
    state = State(RECIPE, session=session)
    agent = agent or SpyAgent()
    return AgentProcessor(agent, FakeFrames(), state), agent, state


def final(text):
    return TranscriptionFrame(text, "user", "", finalized=True)


def user_says(text):
    return [VADUserStartedSpeakingFrame(), UserStartedSpeakingFrame(),
            InterimTranscriptionFrame(text[:5], "user", ""), final(text),
            VADUserStoppedSpeakingFrame(stop_secs=0.2), UserStoppedSpeakingFrame()]


def run(proc, frames, direction=FrameDirection.DOWNSTREAM):
    down, up = asyncio.run(run_test(proc, frames_to_send=frames, frames_to_send_direction=direction))
    return down if direction == FrameDirection.DOWNSTREAM else up


def spoken(frames):
    return [f.text for f in frames if isinstance(f, TTSSpeakFrame)]


def test_turn_calls_agent_and_speaks_each_sentence():
    proc, agent, state = setup()
    down = run(proc, user_says("what's step one") + [SleepFrame(sleep=0.5)])
    assert agent.texts == ["what's step one"]
    assert spoken(down) == ["You said: what's step one.", "I can see 3 frames."]
    assert state.transcript == "what's step one"
    assert len(state.turns) == 1 and LAT_SPEECH_END in state.turns[0].marks


def test_transcripts_are_not_forwarded_to_tts():
    proc, _, _ = setup()
    down = run(proc, user_says("hello") + [SleepFrame(sleep=0.5)])
    assert not any(isinstance(f, (TranscriptionFrame, InterimTranscriptionFrame)) for f in down)


def test_idle_session_ignores_speech():
    proc, agent, state = setup(session="idle")
    down = run(proc, user_says("hello") + [SleepFrame(sleep=0.5)])
    assert agent.texts == [] and spoken(down) == [] and state.turns == []


def test_barge_in_while_thinking_cancels_and_merges():
    proc, agent, _ = setup(SpyAgent(delay=1.0))
    run(proc, user_says("where's the") + [SleepFrame(sleep=0.1)] + user_says("lime") + [SleepFrame(sleep=0.1)])
    assert agent.texts == ["where's the", "where's the lime"]
    assert agent.cancels == 1


def test_barge_in_after_speaking_does_not_merge():
    proc, agent, _ = setup()
    run(proc, user_says("hello") + [SleepFrame(sleep=0.35)]        # first sentence out, second pending
        + user_says("stop that") + [SleepFrame(sleep=0.5)])
    assert agent.texts == ["hello", "stop that"]


def test_vad_and_bot_frames_update_state():
    proc, _, state = setup()
    run(proc, [VADUserStartedSpeakingFrame(), SleepFrame(sleep=0.05)])
    assert state.user_talking
    proc, _, state = setup()
    run(proc, [BotStartedSpeakingFrame(), SleepFrame(sleep=0.05)], FrameDirection.UPSTREAM)
    assert state.speaking


def test_first_audio_is_marked_on_the_turn():
    proc, _, state = setup()
    run(proc, user_says("hi") + [SleepFrame(sleep=0.5), BotStartedSpeakingFrame(), SleepFrame(sleep=0.05)])
    assert LAT_FIRST_AUDIO in state.turns[0].marks
    assert state.turns[0].ms()[LAT_FIRST_AUDIO] >= 0


def test_speech_say_and_stop():
    proc, agent, state = setup(SpyAgent(delay=1.0))
    speech = Speech(proc, state)
    assert isinstance(speech, SpeechAPI)

    async def go():
        async def poke():
            await asyncio.sleep(0.2)
            await speech.say("check-in test")
            await asyncio.sleep(0.1)
            speech.stop()
        poker = asyncio.create_task(poke())
        down, _ = await run_test(proc, frames_to_send=user_says("hello") + [SleepFrame(sleep=0.6)])
        await poker
        return down

    down = asyncio.run(go())
    assert "check-in test" in spoken(down)
    assert any(isinstance(f, InterruptionFrame) for f in down)
    assert agent.cancels == 1                      # stop() cancelled the running turn
