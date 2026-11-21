"""Check-in tests: when the coach speaks up on its own, and (more importantly) when it never does."""
import asyncio
from pathlib import Path

from coach import checkin as CI
from coach.checkin import CheckIns
from coach.contracts import FakeSteps, Priority, Recipe, State

RECIPE = Recipe.load(Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Agent:
    def __init__(self):
        self.busy, self.turns = False, []

    async def turn(self, text, frames, on_sentence):
        self.turns.append((text, len(frames)))
        await on_sentence("Nice work so far.")


class Frames:
    connected = True

    def recent(self, n):
        return ["f"] * n


class Say:
    def __init__(self):
        self.said = []

    async def __call__(self, text, priority=Priority.REPLY):
        self.said.append((text, priority))


def make(level="normal", step=0, ask=None):
    st = State(RECIPE)
    st.step, st.session = step, "active"
    clk, agent, say = Clock(), Agent(), Say()
    st.last_activity = clk.t                      # someone just spoke
    steps = FakeSteps(st)
    ci = CheckIns(st, agent, Frames(), say, steps, ask=ask, level=level, clock=clk)
    return ci, st, clk, agent, say, steps


def run(coro):
    return asyncio.run(coro)


def test_silence_checkin_after_20s_then_waits_another_20s():
    async def go():
        ci, st, clk, agent, say, _ = make()
        clk.t += 19
        assert not await ci.tick()
        clk.t += 1.5
        assert await ci.tick() and len(agent.turns) == 1
        assert "SKIP" in agent.turns[0][0] and agent.turns[0][1] == 3          # 3 fresh frames
        assert say.said == [("Nice work so far.", Priority.PROACTIVE)]
        clk.t += 10                                                            # still quiet, but only 10 s since last
        assert not await ci.tick()
        clk.t += 11
        assert await ci.tick() and len(agent.turns) == 2
    run(go())


def test_gentle_waits_45s_and_off_never_speaks():
    async def go():
        ci, st, clk, agent, *_ = make("gentle")
        clk.t += 30
        assert not await ci.tick()
        clk.t += 16
        assert await ci.tick()
        off, st2, clk2, agent2, *_ = make("off")
        st2.step = 2
        off._on_step(2)
        clk2.t += 500
        assert not await off.tick() and agent2.turns == []                     # not even intros
    run(go())


def test_step_intro_once_after_a_second_of_quiet_with_the_steps_objects():
    async def go():
        ci, st, clk, agent, say, steps = make(step=0)
        clk.t += 5
        st.step = 3
        ci._on_step(3)                                                         # Steps.on_change(3)
        st.last_activity = clk.t                                               # the user just said "done"
        clk.t += 0.5
        assert not await ci.tick()                                             # not quiet yet
        clk.t += 1
        assert await ci.tick()
        text = agent.turns[0][0]
        assert f"step 4 ({RECIPE.steps[3].short})" in text and ", ".join(RECIPE.steps[3].point) in text
        assert "already introduced" in text                                    # lets the model skip a duplicate
        clk.t += 1
        assert not await ci.tick()                                             # intro is one-shot
    run(go())


def test_no_intro_for_step_1_or_after_a_reset():
    async def go():
        ci, st, clk, agent, *_ = make()
        ci._on_step(0)                                                         # reset / session start
        st.last_activity = clk.t - 5
        assert not await ci.tick() and agent.turns == []
        st.session = "idle"
        ci._on_step(2)                                                         # step change while idle: ignored
        st.session = "active"
        assert ci._intro_for is None
    run(go())


def test_intro_dropped_if_the_step_moved_on_again():
    async def go():
        ci, st, clk, agent, *_ = make()
        st.step = 2
        ci._on_step(2)
        st.step = 3                                                            # user said done again
        ci._on_step(3)
        st.last_activity = clk.t - 5
        assert await ci.tick()
        assert "step 4" in agent.turns[0][0] and len(agent.turns) == 1
    run(go())


def test_never_while_speaking_talking_busy_or_idle():
    async def go():
        ci, st, clk, agent, *_ = make()
        clk.t += 100
        for attr, val in (("speaking", True), ("user_talking", True)):
            setattr(st, attr, val)
            assert not await ci.tick()
            setattr(st, attr, False)
        agent.busy = True
        assert not await ci.tick()
        agent.busy = False
        st.session = "idle"
        assert not await ci.tick()
        assert agent.turns == []
        st.session = "active"
        assert await ci.tick()                                                 # all clear: now it speaks
    run(go())


def test_a_waiting_intro_survives_until_it_is_quiet():
    async def go():
        ci, st, clk, agent, *_ = make()
        st.step = 1
        ci._on_step(1)
        st.last_activity = clk.t - 30
        st.speaking = True
        assert not await ci.tick()
        st.speaking = False
        assert await ci.tick() and "step 2" in agent.turns[0][0]
    run(go())


def test_never_writes_last_activity():
    async def go():
        ci, st, clk, *_ = make()
        before = st.last_activity
        clk.t += 30
        await ci.tick()
        assert st.last_activity == before
    run(go())


def test_uses_ask_when_given_so_barge_in_can_cancel_it():
    async def go():
        asked = []

        async def ask(text):
            asked.append(text)
        ci, st, clk, agent, *_ = make(ask=ask)
        clk.t += 30
        assert await ci.tick()
        assert len(asked) == 1 and agent.turns == []
    run(go())


def test_no_frames_sent_when_the_camera_is_down():
    async def go():
        ci, st, clk, agent, *_ = make()
        ci.frames.connected = False
        clk.t += 30
        await ci.tick()
        assert agent.turns[0][1] == 0
    run(go())


def test_level_can_change_while_running_and_bad_values_are_ignored():
    ci, *_ = make()
    ci.set_level("gentle")
    assert ci.silence_s == 45
    ci.set_level("loud")
    assert ci.level == "gentle"


def test_run_loop_survives_an_error_and_stops_on_cancel(monkeypatch):
    async def go():
        ci, st, clk, agent, *_ = make()
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return False
        ci.tick = flaky
        monkeypatch.setattr(CI, "TICK_S", 0.01)
        task = asyncio.create_task(ci.run())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert len(calls) >= 2
    run(go())
