"""Kitchen timers (timers.py): set / cancel, firing, and the spoken "time's up" that waits for a quiet moment."""
import asyncio
from pathlib import Path

from coach import timers as T
from coach.contracts import TFT_AGENT, FakeLink, Priority, Recipe, State
from coach.timers import Timers, fmt_clock, fmt_duration

RECIPE = Recipe.load(Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json")


class Clock:
    def __init__(self):
        self.t = 500.0

    def __call__(self):
        return self.t


class Link(FakeLink):
    def __init__(self):
        super().__init__()
        self.shown = []

    def show_text(self, line, text):
        self.shown.append((line, text))


class Say:
    def __init__(self):
        self.said = []

    async def __call__(self, text, priority=Priority.REPLY):
        self.said.append((text, priority))


def make():
    st, clk, link = State(RECIPE), Clock(), Link()
    return Timers(st, link, clock=clk), st, clk, link


def test_formatting():
    assert fmt_duration(45) == "45 seconds" and fmt_duration(90) == "1 minute 30 seconds"
    assert fmt_duration(3600) == "1 hour" and fmt_duration(0) == "0 seconds"
    assert fmt_clock(252) == "4:12" and fmt_clock(3725) == "1:02:05"


def test_set_describe_restart_and_cancel():
    tm, st, clk, link = make()
    assert tm.set(300, "Onions") == "ok: started onions timer, 5 minutes"
    assert st.timers == {"onions": 800.0} and (TFT_AGENT, "onions 5:00") in link.shown
    clk.t += 48
    assert tm.describe() == "onions 4:12 left"
    assert tm.set(60, "onions").startswith("ok: restarted") and st.timers["onions"] == clk.t + 60
    tm.set(30, "")                                                   # no label: "timer"
    assert tm.describe() == "timer 0:30 left, onions 1:00 left"     # soonest first
    assert tm.cancel("onion") == "ok: cancelled onions"              # a partial label is enough
    assert tm.cancel("pasta").startswith("no timer called 'pasta'")
    tm.set(10, "a")
    assert tm.cancel("") == "ok: cancelled timer, a" and st.timers == {}
    assert tm.cancel("") == "no timers running"
    assert any("timer set: onions" in e.text for e in st.events) and any("cancelled" in e.text for e in st.events)


def test_limits():
    tm, st, *_ = make()
    assert tm.set(0, "x").startswith("error") and tm.set(T.MAX_SECONDS + 1, "x").startswith("error")
    for i in range(T.MAX_TIMERS):
        assert tm.set(60, f"t{i}").startswith("ok")
    assert tm.set(60, "one more").startswith("error: already")
    assert tm.set(90, "t0").startswith("ok: restarted")             # an existing label can still be restarted


def test_due_timer_is_logged_shown_and_announced_when_quiet():
    async def go():
        tm, st, clk, link = make()
        say = Say()
        tm.set(60, "rice")
        clk.t += 59
        assert tm.tick() == [] and st.timers
        clk.t += 1
        assert tm.tick() == ["rice"] and st.timers == {}
        assert any(e.text == "timer done: rice" for e in st.events) and (TFT_AGENT, "TIME UP: rice") in link.shown
        busy = {"quiet": False}
        await tm.announce(say, lambda: busy["quiet"])
        assert say.said == []                                        # someone is talking: wait
        busy["quiet"] = True
        await tm.announce(say, lambda: busy["quiet"])
        assert say.said == [("Time's up for the rice.", Priority.REPLY)]
        await tm.announce(say, lambda: True)
        assert len(say.said) == 1                                    # said once
    asyncio.run(go())


def test_announced_anyway_if_it_never_gets_quiet():
    async def go():
        tm, st, clk, _ = make()
        say = Say()
        tm.set(1, "")
        clk.t += 1
        tm.tick()
        await tm.announce(say, lambda: False)
        assert say.said == []
        clk.t += T.ANNOUNCE_MAX_WAIT_S
        await tm.announce(say, lambda: False)
        assert say.said == [("Your timer is done.", Priority.REPLY)]
    asyncio.run(go())


def test_timers_keep_running_while_paused_and_the_loop_never_raises():
    async def go():
        st = State(RECIPE, session="paused")
        tm, say = Timers(st), Say()
        tm.set(1, "tea")
        st.timers["tea"] -= 1                                       # due now (real monotonic clock)
        task = asyncio.create_task(tm.run(say))
        await asyncio.sleep(T.TICK_S * 3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert say.said == [("Time's up for the tea.", Priority.REPLY)]
    asyncio.run(go())
