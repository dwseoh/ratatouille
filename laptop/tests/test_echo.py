"""Echo gate: the coach's own voice in the mic must not start turns, fire intents or reach the
agent; a stop word over the coach still cuts it. Plus picking audio devices by name."""
import asyncio

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.tests.utils import SleepFrame

from coach.contracts import FakeFrames, State
from coach.echo import EchoGate, EchoGateStartStrategy
from coach.intents import ends_with_stop
from coach.voice import AgentProcessor, find_device

from test_voice import RECIPE, SpyAgent, final, run, spoken, user_says


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


# -- EchoGate ----------------------------------------------------------------------------------------

def test_gate_closed_while_speaking_and_for_the_tail():
    clk = Clock()
    g = EchoGate(0.3, clock=clk)
    assert not g.closed
    g.bot_started()
    assert g.closed
    clk.t += 5
    g.bot_stopped()
    clk.t += 0.2
    assert g.closed                       # speaker still ringing / Bluetooth lag
    clk.t += 0.2
    assert not g.closed


def test_disabled_gate_never_closes():
    g = EchoGate(enabled=False)
    g.bot_started()
    g.vad_started()
    assert not g.closed and not g.suppressing


def test_speech_that_began_over_the_coach_stays_suppressed_until_new_speech():
    clk = Clock()
    g = EchoGate(0.3, clock=clk)
    g.bot_started()
    g.vad_started()                       # the mic hears the coach
    g.bot_stopped()
    clk.t += 1.0
    assert not g.closed and g.suppressing  # Deepgram's late final for the coach's words: still dropped
    g.vad_started()                       # the user talks, gate open
    assert not g.suppressing


@pytest.mark.parametrize("text,expected", [
    ("stop", True),
    ("now cut the avocado in half stop", True),
    ("mash it with a fork hold on", True),
    ("ok wait wait", True),
    ("wait for the onion to soften then add it", False),
    ("put the knife down", False),
])
def test_ends_with_stop(text, expected):
    assert ends_with_stop(text) is expected


# -- start strategy ----------------------------------------------------------------------------------

def strategy(gate):
    s = EchoGateStartStrategy(gate)
    s.reports = 0

    async def report(**_):
        s.reports += 1
    s.report_user_turn_started = report
    return s


def feed(s, *frames):
    async def go():
        for f in frames:
            await s.process_frame(f)
    asyncio.run(go())
    return s.reports


def test_strategy_open_gate_starts_on_vad_or_transcript():
    s = strategy(EchoGate())
    assert feed(s, VADUserStartedSpeakingFrame()) == 1
    assert feed(s, InterimTranscriptionFrame("hi", "user", "")) == 2


def test_strategy_closed_gate_only_stop_words_start_a_turn():
    s = strategy(EchoGate())
    n = feed(s, BotStartedSpeakingFrame(), VADUserStartedSpeakingFrame(),
             InterimTranscriptionFrame("now cut the avocado", "user", ""), final("now cut the avocado in half"))
    assert n == 0
    assert feed(s, InterimTranscriptionFrame("in half stop", "user", "")) == 1


def test_strategy_closed_gate_also_opens_for_a_pause_phrase():
    s = strategy(EchoGate())
    assert feed(s, BotStartedSpeakingFrame(), InterimTranscriptionFrame("now cut the avocado", "user", "")) == 0
    assert feed(s, InterimTranscriptionFrame("cut the avocado give me a sec", "user", "")) == 1


def test_strategy_reopens_after_the_tail():
    s = strategy(EchoGate(tail_s=0.0))
    feed(s, BotStartedSpeakingFrame(), BotStoppedSpeakingFrame())
    assert feed(s, VADUserStartedSpeakingFrame()) == 1


# -- AgentProcessor with the gate ----------------------------------------------------------------------

def gated(tail_s=0.3, agent=None):
    state = State(RECIPE, session="active")
    agent = agent or SpyAgent()
    return AgentProcessor(agent, FakeFrames(), state, gate=EchoGate(tail_s)), agent, state


def echo(text):
    """What the mic hears while the coach talks: VAD + transcripts, but no user turn (the strategy refused)."""
    return [VADUserStartedSpeakingFrame(), InterimTranscriptionFrame(text[:8], "user", ""), final(text),
            VADUserStoppedSpeakingFrame(stop_secs=0.2)]


def test_echo_is_dropped_while_the_coach_talks():
    proc, agent, state = gated()
    down = run(proc, [BotStartedSpeakingFrame()] + echo("the lime is on your left") + [SleepFrame(sleep=0.3)])
    assert agent.texts == [] and spoken(down) == []
    assert state.transcript == "" and not state.user_talking


def test_late_final_after_the_tail_is_still_dropped():
    proc, agent, state = gated(tail_s=0.05)
    run(proc, [BotStartedSpeakingFrame(), VADUserStartedSpeakingFrame(), BotStoppedSpeakingFrame(),
               SleepFrame(sleep=0.1), final("wait for it to soften"), VADUserStoppedSpeakingFrame(stop_secs=0.2),
               SleepFrame(sleep=0.1)]
        + user_says("what's next") + [SleepFrame(sleep=0.5)])
    assert agent.texts == ["what's next"]           # the echo never joined the user's turn


def test_stop_word_over_the_coach_cancels_and_never_reaches_the_agent():
    proc, agent, state = gated(agent=SpyAgent(delay=1.0))
    run(proc, user_says("how do I cut it") + [SleepFrame(sleep=0.1), BotStartedSpeakingFrame(),
                                              VADUserStartedSpeakingFrame(), UserStartedSpeakingFrame(),
                                              InterimTranscriptionFrame("in half stop", "user", ""),
                                              final("cut it in half stop"),
                                              VADUserStoppedSpeakingFrame(stop_secs=0.2), UserStoppedSpeakingFrame(),
                                              SleepFrame(sleep=0.3)])
    assert agent.texts == ["how do I cut it"]      # no second turn with the echo + "stop"
    assert agent.cancels == 1


def test_gate_off_keeps_normal_barge_in():
    state = State(RECIPE, session="active")
    agent = SpyAgent()
    proc = AgentProcessor(agent, FakeFrames(), state)   # default gate: disabled
    run(proc, [BotStartedSpeakingFrame()] + user_says("hello") + [SleepFrame(sleep=0.5)])
    assert agent.texts == ["hello"]


# -- devices -------------------------------------------------------------------------------------------

DEVICES = [
    {"index": 0, "name": "MacBook Air Microphone", "maxInputChannels": 1, "maxOutputChannels": 0},
    {"index": 1, "name": "MacBook Air Speakers", "maxInputChannels": 0, "maxOutputChannels": 2},
    {"index": 2, "name": "JBL Flip 6", "maxInputChannels": 1, "maxOutputChannels": 2},
    {"index": 3, "name": "RØDE Wireless GO II RX", "maxInputChannels": 2, "maxOutputChannels": 0},
]


def test_find_device_by_name_substring_and_kind():
    assert find_device("", "input", DEVICES) is None             # default
    assert find_device("wireless go", "input", DEVICES) == 3
    assert find_device("jbl", "output", DEVICES) == 2
    assert find_device("speakers", "output", DEVICES) == 1
    with pytest.raises(RuntimeError, match="no input device matching 'Speakers'"):
        find_device("Speakers", "input", DEVICES)                 # output-only device can't be the mic
