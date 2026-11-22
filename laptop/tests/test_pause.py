"""Pause / resume: the router's PAUSE and RESUME, Session.pause()/resume(), and everything that must go quiet."""
import asyncio
import time
from pathlib import Path

import pytest

from coach.contracts import (TFT_STATUS, FakeAgent, FakeAimer, FakeLink, FakeSteps, Intent, KeywordRouter, Recipe,
                             State, fake_dets)
from coach.intents import (IntentActions, IntentRouter, allowed_while_paused, ends_with_pause, is_pause, is_resume,
                           is_safety)
from coach.session import PAUSE_LINE, PAUSED_STATUS, START_TEXT, Session

RECIPE = Recipe.load(Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json")
P, S, PA, RE, D, O, X = (Intent.POINT, Intent.STOP, Intent.PAUSE, Intent.RESUME, Intent.DONE, Intent.OTHER,
                         Intent.START_OVER)


def run(coro):
    return asyncio.run(coro)


# ───────────── the router ─────────────

@pytest.fixture(scope="module")
def router():
    return IntentRouter(RECIPE, use_embeddings=False)


@pytest.mark.parametrize("text,intent", [
    ("pause", PA), ("pause please", PA), ("hold on a minute", PA), ("give me a sec", PA), ("give me a minute", PA),
    ("ok give me a sec I need to wash my hands", PA), ("I need a break", PA), ("be right back", PA),
    ("hold on a second", S), ("one sec", S), ("wait just a minute please", S), ("stop", S),     # a moment, not a break
    ("pause the timer", O), ("stop the timer", O),             # timers are the agent's job
    ("don't pause", O),
    ("I'm back", RE), ("resume", RE), ("ok let's continue", RE), ("I'm back, what's next?", RE),   # never skips a step
    ("go back to the previous step", O), ("skip this step", O), ("previous step please", O),       # agent navigates
    ("what's next", D), ("where's the lime", P), ("start over", X),
    ("I was telling my friend that I really needed a break from all the work this week", O),     # long: not PAUSE
])
def test_router_pause_resume_and_navigation(router, text, intent):
    assert router.classify(text).intent is intent, text


def test_phrase_helpers():
    assert is_pause("Give me a second!") and not is_pause("don't pause") and not is_pause("pause the timer")
    assert ends_with_pause("push the knife in until it hits the pit pause")
    assert ends_with_pause("rotate the avocado hold on a minute") and not ends_with_pause("rotate the avocado")
    assert is_resume("ok I'm back") and is_resume("hey, let's keep going") and not is_resume("I'm done")
    assert is_safety("help") and is_safety("ow I cut my finger") and is_safety("something's burning")
    assert not is_safety("how do I cut the onion")
    assert allowed_while_paused("I burned myself") and allowed_while_paused("let's start over")
    assert not allowed_while_paused("how chunky should it be")


def test_keyword_router_knows_pause_and_resume():
    k = KeywordRouter(RECIPE)
    assert k.classify("pause").intent is PA and k.classify("ok I'm back").intent is RE
    assert k.classify("stop").intent is S


# ───────────── the session ─────────────

class SpySpeech:
    def __init__(self):
        self.said, self.chimes, self.turns, self.interrupts, self.speaking = [], [], [], 0, False

    async def say(self, text, priority=None):
        self.said.append(text)

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


class Link(FakeLink):
    def __init__(self):
        super().__init__()
        self.shown = []

    def show_text(self, line, text):
        self.shown.append((line, text))


def make(session="active", step=2):
    st = State(RECIPE, dets=fake_dets())
    link = Link()
    s = Session(st, SpySpeech(), SpyAgent(), FakeAimer(st), FakeSteps(st), link)
    st.session, st.step = session, step
    return s, st, link


def test_pause_goes_quiet_keeps_the_step_and_says_how_to_come_back():
    async def go():
        s, st, link = make()
        s.aimer.point_at("lime")
        assert await s.pause("heard 'pause'")
        assert st.session == "paused" and st.step == 2 and st.target is None and st.paused_at is not None
        assert s.speech.interrupts == 1 and s.agent.cancels == 1 and s.speech.chimes == ["pause"]
        assert s.speech.said == [PAUSE_LINE] and (TFT_STATUS, PAUSED_STATUS) in link.shown
        assert len(PAUSED_STATUS) <= 20 and st.events[-1].text == "session paused (heard 'pause')"
        assert not await s.pause("again") and s.speech.said == [PAUSE_LINE]     # already paused: nothing
    run(go())


def test_pause_only_from_active():
    async def go():
        s, st, _ = make(session="idle")
        assert not await s.pause() and st.session == "idle"
    run(go())


def test_ordinary_speech_does_not_resume_but_resume_phrases_and_the_wake_word_do():
    async def go():
        for phrase in ("I'm back", "ok resume", "let's continue", "hey Remy"):
            s, st, link = make()
            await s.pause()
            assert not await s.heard("how chunky should it be") and st.session == "paused"
            assert await s.heard(phrase) and st.session == "active", phrase
            assert s.speech.chimes == ["pause", "start"] and (TFT_STATUS, "cooking") in link.shown
            text = s.first_turn(f"{phrase}, where was I?")
            assert text.startswith("[session resumed after 0 seconds:") and text.endswith(f"User: {phrase}, where was I?")
            assert s.first_turn("hey Remy") == START_TEXT                 # the welcome back is used once
            assert "session resumed after" in st.events[-1].text
    run(go())


def test_resume_counts_the_pause_out_of_the_step_time():
    async def go():
        s, st, _ = make()
        st.step_since = st.cook_clock() - 630                           # the step began 630 s ago...
        await s.pause()
        st.paused_at -= 600                                             # ...and the cook left 600 s ago
        assert 29 <= st.step_seconds() <= 31
        await s.resume()
        assert st.paused_at is None and st.paused_total_s >= 600 and 29 <= st.step_seconds() <= 31
    run(go())


def test_button_style_resume_greets_through_an_agent_turn():
    async def go():
        s, st, _ = make()
        await s.pause()
        assert await s.resume("BOOT button", greet=True)
        assert s.speech.turns and s.speech.turns[0].startswith("[session resumed after")
    run(go())


def test_reset_while_paused_ends_the_pause():
    async def go():
        s, st, _ = make()
        await s.pause()
        await s.reset()
        assert st.session == "idle" and st.paused_at is None and st.step == 0
        assert await s.heard("hey Remy") and s.first_turn("hey Remy") == START_TEXT
    run(go())


def test_wake_from_idle_still_greets():
    async def go():
        s, st, _ = make(session="idle")
        assert not await s.heard("I'm back")                            # idle: only the wake phrase
        assert await s.heard("hey Remy") and s.first_turn("hey Remy, let's go") == f"{START_TEXT}\nUser: let's go"
    run(go())


# ───────────── the router's actions ─────────────

class SpySession:
    def __init__(self, state):
        self.state, self.calls = state, []

    async def pause(self, why="user"):
        self.calls.append("pause")
        self.state.session = "paused"
        return True

    async def resume(self, why="user", **kw):
        self.calls.append("resume")
        self.state.session = "active"
        return True

    async def reset(self):
        self.calls.append("reset")
        self.state.session = "idle"


class Speech:
    def __init__(self):
        self.stops = 0

    def stop(self):
        self.stops += 1


def actions(router, session="active"):
    st = State(RECIPE, dets=fake_dets(), session=session)
    a = IntentActions(router, st, FakeAimer(st), FakeSteps(st), None)
    a.session, a.speech = SpySession(st), Speech()
    return a, st


def test_pause_cuts_audio_on_the_partial_and_pauses_on_the_final(router):
    a, st = actions(router)
    run(a.on_interim("pause"))
    assert a.speech.stops == 1 and a.session.calls == [] and st.session == "active"
    assert run(a.on_final("pause")) is True                     # no agent turn
    assert a.session.calls == ["pause"] and st.session == "paused"


def test_while_paused_only_resume_and_start_over_act(router):
    a, st = actions(router, session="paused")
    assert run(a.on_final("where's the lime")) is False and st.target is None      # no pointing
    assert run(a.on_final("ok I'm done")) is False and st.step == 0                # no step change
    assert run(a.on_final("help I cut myself on the knife")) is False and st.target is None
    assert run(a.on_final("I'm back")) is False and a.session.calls == ["resume"]  # the agent welcomes them back
    a2, st2 = actions(router, session="paused")
    assert run(a2.on_final("start over")) is True and a2.session.calls == ["reset"]


def test_pause_without_a_session_still_goes_quiet(router):
    st = State(RECIPE, dets=fake_dets(), session="active")
    a = IntentActions(router, st, FakeAimer(st), FakeSteps(st), None)
    a.speech = Speech()
    a.aimer.point_at("lime")
    assert run(a.on_final("pause")) is True and st.target is None and a.speech.stops == 1


# ───────────── nothing proactive while paused ─────────────

def test_checkins_and_watcher_stay_quiet_while_paused():
    from coach.checkin import CheckIns
    from coach.config import Config
    from coach.watcher import Watcher

    class Agent:
        busy = False
        turns = []

        async def turn(self, text, frames, on_sentence):
            self.turns.append(text)

    class Client:
        requests = []

    class Frames:
        connected = True

        def recent(self, n):
            return []

    async def go():
        st = State(RECIPE, session="paused")
        st.last_activity = time.monotonic() - 600                       # a long silence
        agent = Agent()
        ci = CheckIns(st, agent, Frames(), None, FakeSteps(st), level="normal")
        assert not await ci.tick() and agent.turns == []
        w = Watcher(Config(), st, Frames(), None, FakeSteps(st), client=Client())
        await w.tick()
        assert w._task is None                                          # no look while paused
        st.session = "active"
        assert await ci.tick() and len(agent.turns) == 1                # back to normal once resumed
    run(go())


def test_steps_go_to_restarts_the_step_timer_and_reopens_a_finished_recipe():
    from coach.steps import Steps
    st = State(RECIPE)
    steps = Steps(st, Link(), clock=lambda: 1e9)
    st.step_since = st.cook_clock() - 100
    assert steps.go_to(3) == "ok" and st.step == 3 and st.step_seconds() < 1
    assert steps.go_to(3) == "already on step 4" and steps.go_to(7).startswith("error")
    st.step = 6
    assert steps.propose_done("user") == "finished" and steps.finished
    assert steps.go_to(6) == "ok" and not steps.finished                 # same step, but the recipe reopens
    assert steps.link.shown[-1] == (2, "Step 7: cilantro, mix")
