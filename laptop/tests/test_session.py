"""Session tests: wake phrase starts (with chime), reset clears everything."""
import asyncio
from pathlib import Path

import pytest

from coach.contracts import FakeAgent, FakeAimer, FakeLink, FakeSteps, Recipe, SessionAPI, State, fake_dets
from coach.session import START_TEXT, Session, find_wake

RECIPE = Recipe.load(Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json")


class SpySpeech:
    def __init__(self):
        self.turns, self.chimes, self.interrupts, self.speaking = [], [], 0, False

    async def say(self, text, priority=None):
        pass

    def stop(self):
        pass

    async def interrupt(self):
        self.interrupts += 1

    async def chime(self, kind):
        self.chimes.append(kind)

    async def agent_turn(self, text):
        self.turns.append(text)


class SpyAgent(FakeAgent):
    def __init__(self):
        super().__init__()
        self.cancels = 0

    def cancel(self):
        self.cancels += 1
        super().cancel()


class SpyLink(FakeLink):
    def __init__(self):
        super().__init__()
        self.resets = 0

    def reset(self):
        self.resets += 1
        super().reset()


def make():
    state = State(RECIPE, dets=fake_dets())
    link = SpyLink()
    s = Session(state, SpySpeech(), SpyAgent(), FakeAimer(state), FakeSteps(state), link)
    return s, state, link


@pytest.mark.parametrize("text", ["hey Remy", "Hey, Remy!", "hi remi", "okay Rémy let's cook",
                                  "hey ramy", "Remy?", "a remmy where do I start"])
def test_wake_phrase_variants(text):
    assert find_wake(text) is not None


@pytest.mark.parametrize("text", ["hey there", "the army is here", "I'm ready", "remind me later please",
                                  "hey", "where's the lime"])
def test_not_wake_phrase(text):
    assert find_wake(text) is None


def test_is_session_api():
    assert isinstance(make()[0], SessionAPI)


def test_wake_activates_with_chime_and_first_turn_text():
    async def go():
        s, state, _ = make()
        assert not await s.heard("hey there")
        assert state.session == "idle"
        assert await s.heard("hey Remy let's")
        assert state.session == "active" and s.speech.chimes == ["start"]
        assert not await s.heard("hey Remy let's make")            # already active: no second chime
        assert s.first_turn("hey Remy") == START_TEXT
        assert s.first_turn("Hey Remy, let's make guac") == f"{START_TEXT}\nUser: let's make guac"
    asyncio.run(go())


def test_start_then_reset_clears_everything():
    async def go():
        s, state, link = make()
        await s.start()
        assert state.session == "active" and s.speech.turns == [START_TEXT]
        s.aimer.point_at("lime")
        s.steps.confirm()

        await s.reset()
        assert state.session == "idle" and state.target is None and state.step == 0
        assert s.speech.interrupts == 1 and s.agent.cancels == 1 and link.resets == 1
        assert s.speech.chimes == ["start", "reset"]
        assert [e.text for e in state.events] == ["session started", "session reset"]
    asyncio.run(go())
