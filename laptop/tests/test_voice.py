"""Voice tests: AgentProcessor + Speech driven with synthetic turn/STT frames (no mic, no network)."""
import asyncio
from pathlib import Path

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.tests.utils import SleepFrame, run_test

from coach.contracts import (LAT_FIRST_AUDIO, LAT_SPEECH_END, FakeAgent, FakeAimer, FakeFrames, FakeSteps, Recipe,
                             SpeechAPI, State)
from coach.session import START_TEXT, Session
from coach.voice import AgentProcessor, CaptionTap, Speech

RECIPE = Recipe.load(Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json")


class SpyAgent(FakeAgent):
    def __init__(self, delay=0.0):
        super().__init__()
        self.texts, self.cancels, self.delay = [], 0, delay
        self.frame_counts = []

    async def turn(self, user_text, frames, on_sentence):
        self.texts.append(user_text)
        self.frame_counts.append(len(frames))
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


def test_frames_go_to_the_agent_only_while_the_camera_is_connected():
    proc, agent, state = setup()
    run(proc, user_says("what's step one") + [SleepFrame(sleep=0.5)])
    assert agent.frame_counts == [3]
    proc, agent, state = setup()
    proc.frames.connected = False                      # camera dropped: no stale frames, the agent says so once
    run(proc, user_says("what's step one") + [SleepFrame(sleep=0.5)])
    assert agent.frame_counts == [0]


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


def test_wake_phrase_chimes_then_one_greeting_turn():
    proc, agent, state = setup(session="idle")
    Session(state, Speech(proc, state), agent, FakeAimer(state), FakeSteps(state))
    down = run(proc, user_says("hello there") + [SleepFrame(sleep=0.1)]          # ignored: no wake phrase
               + [VADUserStartedSpeakingFrame(), UserStartedSpeakingFrame(),
                  InterimTranscriptionFrame("hey Remy let's", "user", ""), final("hey Remy, let's make guac"),
                  VADUserStoppedSpeakingFrame(stop_secs=0.2), UserStoppedSpeakingFrame(), SleepFrame(sleep=0.6)])
    assert state.session == "active"
    assert sum(isinstance(f, OutputAudioRawFrame) for f in down) >= 1            # start chime
    assert agent.texts == [f"{START_TEXT}\nUser: let's make guac"]
    assert state.transcript == "hey Remy, let's make guac"


def test_interrupted_greeting_is_merged_but_its_internal_prompt_never_reaches_the_transcript_or_intents():
    """Say "hey Remy", then keep talking before the coach answers: the greeting instruction is still sent to the
    agent (so it greets), but `state.transcript` and the intent router only ever see what the user said."""
    class SpyActions:
        def __init__(self):
            self.finals = []

        async def on_final(self, text):
            self.finals.append(text)
            return False

        async def on_interim(self, text):
            pass

        def new_utterance(self):
            pass

        def take_point_time(self):
            return None
    agent = SpyAgent(delay=1.0)
    proc, _, state = setup(agent, session="idle")
    proc.actions = SpyActions()
    Session(state, Speech(proc, state), agent, FakeAimer(state), FakeSteps(state))
    run(proc, user_says("hey Remy") + [SleepFrame(sleep=0.1)] + user_says("let's start") + [SleepFrame(sleep=0.1)])
    assert agent.cancels == 1 and len(agent.texts) == 2
    assert START_TEXT in agent.texts[1] and "let's start" in agent.texts[1]       # the agent still greets, once
    assert "[session start" not in state.transcript and "[session start" not in " ".join(proc.actions.finals)
    assert state.transcript == "hey Remy let's start"


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


def test_agent_turn_speaks_and_is_not_merged_on_barge_in():
    proc, agent, state = setup(SpyAgent(delay=1.0))
    speech = Speech(proc, state)

    async def go():
        async def kick():
            await asyncio.sleep(0.05)
            await speech.agent_turn("[session start]")
        k = asyncio.create_task(kick())
        down, _ = await run_test(proc, frames_to_send=[SleepFrame(sleep=0.2)] + user_says("hi")
                                 + [SleepFrame(sleep=1.6)])
        await k
        return down

    down = asyncio.run(go())
    assert agent.texts == ["[session start]", "hi"]       # greeting cut by barge-in, NOT merged into "hi"
    assert spoken(down) == ["You said: hi.", "I can see 3 frames."]


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


def test_caption_tap_measures_audio_per_sentence_and_passes_frames_through():
    from pipecat.frames.frames import TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame

    from coach.captions import CaptionTimeline
    clock = {"t": 0.0}
    cap = CaptionTimeline(lambda: clock["t"])
    cap.queue("One."); cap.queue("Two.")
    tap = CaptionTap(cap)
    second = 24000 * 2                                               # 1 s of 24 kHz mono 16-bit audio
    frames = [TTSStartedFrame(), TTSAudioRawFrame(b"\0" * second, 24000, 1), TTSStoppedFrame(),
              TTSStartedFrame(), TTSAudioRawFrame(b"\0" * (second * 2), 24000, 1), TTSStoppedFrame()]
    down, _ = asyncio.run(run_test(tap, frames_to_send=frames))
    assert [type(f).__name__ for f in down][-6:] == [type(f).__name__ for f in frames]       # untouched
    cap.playback_started()
    assert cap.current() == "One."
    clock["t"] = 1.2
    assert cap.current() == "Two."                                   # sentence 1 was exactly 1.0 s of audio
    clock["t"] = 5
    assert cap.current() == "Two."


def test_speaking_queues_the_caption_and_stopping_clears_it():
    from coach.captions import CaptionTimeline
    proc, agent, state = setup()
    proc.captions = CaptionTimeline()
    run(proc, user_says("hello") + [SleepFrame(sleep=0.5)])
    assert proc.captions.current() == "You said: hello."              # both sentences were queued; the first is shown
    assert [i.text for i in proc.captions._items] == ["You said: hello.", "I can see 3 frames."]
    proc.captions.playback_started()
    assert proc.captions.speaking
    proc.captions.reset()
    assert not proc.captions.speaking and proc.captions.current() == "You said: hello."
