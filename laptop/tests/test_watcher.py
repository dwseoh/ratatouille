"""Watcher tests: trigger rules, applying the model's answer. Offline: fake client, frames, speech."""
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np

from coach import watcher as W
from coach.config import Config
from coach.contracts import Det, FakeSteps, Priority, Recipe, State
from coach.steps import StepVerdict, Steps
from coach.watcher import Watcher

RECIPE = Recipe.load(Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json")
CFG = Config()
FRAME = np.zeros((48, 64, 3), dtype=np.uint8)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Frames:
    connected = True

    def __init__(self, n=4):
        self.n = n

    def latest(self):
        return FRAME, 0.0

    def recent(self, n):
        return [FRAME] * min(n, self.n)


class Speech:
    def __init__(self):
        self.said = []
        self.speaking = False

    async def say(self, text, priority=Priority.REPLY):
        self.said.append((text, priority))

    def stop(self):
        pass


class Client:
    """messages.create returns scripted JSON answers (or raises)."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []
        self.messages = NS(create=self._create)

    async def _create(self, **kw):
        self.requests.append(kw)
        a = self.answers.pop(0) if self.answers else self.answers_default()
        if isinstance(a, Exception):
            raise a
        text = a if isinstance(a, str) else json.dumps(a)
        return NS(content=[NS(type="text", text=text)], stop_reason="end_turn")

    @staticmethod
    def answers_default():
        return answer()


def answer(events=(), done=False, conf=0.9, note=""):
    return {"events": list(events), "step_done": done, "confidence": conf, "note": note}


def make(*answers, step=0, visible=("avocado", "knife"), active=True, steps=None, on_ask=None, frames=None, busy=None):
    st = State(RECIPE, dets=[Det(n, 0.9, (0, 0, 10, 10)) for n in visible])
    st.step, st.session = step, "active" if active else "idle"
    st.last_activity = time.monotonic() - 60                  # nobody has spoken for a minute
    clk, speech, client = Clock(), Speech(), Client(*answers)
    steps = steps or FakeSteps(st)
    w = Watcher(CFG, st, frames or Frames(), speech, steps, on_ask=on_ask, busy=busy, client=client, clock=clk)
    return w, st, clk, speech, client, steps


async def run_check(w):
    """tick once and wait for the model call it started."""
    await w.tick()
    if w._task:
        await w._task


def run(coro):
    return asyncio.run(coro)


# ───────────── when to look ─────────────

def test_first_tick_looks_then_waits():
    async def go():
        w, st, clk, sp, client, _ = make(answer())
        await run_check(w)
        assert len(client.requests) == 1
        clk.t += 1
        await run_check(w)
        assert len(client.requests) == 1             # inside MIN_GAP_S
        clk.t += W.MIN_GAP_S
        await run_check(w)
        assert len(client.requests) == 1             # nothing changed, step interval not reached
        clk.t += W.STEP_INTERVAL_S
        await run_check(w)
        assert len(client.requests) == 2             # periodic look
    run(go())


def test_visible_objects_changed_triggers_a_look():
    async def go():
        w, st, clk, _, client, _ = make()
        await run_check(w)
        clk.t += W.MIN_GAP_S + 0.1
        st.dets = [Det("lime", 0.9, (0, 0, 10, 10))]
        await run_check(w)
        assert len(client.requests) == 2
    run(go())


def test_user_said_done_triggers_a_look():
    async def go():
        w, st, clk, _, client, _ = make()
        await run_check(w)
        clk.t += W.MIN_GAP_S + 0.1
        st.log("user", "said done -> advance")
        await run_check(w)
        assert len(client.requests) == 2
    run(go())


def test_forced_check_waits_for_the_min_gap_instead_of_being_lost():
    async def go():
        w, st, clk, _, client, _ = make()
        await run_check(w)
        w.request_check()
        clk.t += 1
        await run_check(w)
        assert len(client.requests) == 1
        clk.t += W.MIN_GAP_S
        await run_check(w)
        assert len(client.requests) == 2
    run(go())


def test_never_while_idle_speaking_or_user_talking():
    async def go():
        w, st, clk, _, client, _ = make(active=False)
        await run_check(w)
        assert client.requests == []
        st.session = "active"
        st.speaking = True
        await run_check(w)
        st.speaking, st.user_talking = False, True
        await run_check(w)
        assert client.requests == []
        st.user_talking = False
        await run_check(w)
        assert len(client.requests) == 1
    run(go())


def test_no_second_call_while_one_is_in_flight():
    async def go():
        w, st, clk, _, client, _ = make()
        gate = asyncio.Event()
        orig = client._create

        async def slow(**kw):
            await gate.wait()
            return await orig(**kw)
        client.messages.create = slow
        await w.tick()
        clk.t += W.STEP_INTERVAL_S + 5
        st.dets = [Det("lime", 0.9, (0, 0, 10, 10))]
        await w.tick()                              # would trigger, but one is running
        gate.set()
        await w._task
        assert len(client.requests) == 1
    run(go())


def test_request_has_frames_step_info_and_event_log():
    async def go():
        w, st, clk, _, client, _ = make(step=2)
        st.log("user", "hello")
        await run_check(w)
        req = client.requests[0]
        content = req["messages"][0]["content"]
        assert sum(b["type"] == "image" for b in content) == 4
        text = content[-1]["text"]
        assert RECIPE.steps[2].done_when in text and "user: hello" in text and "avocado" in text
        assert req["model"] == CFG.watcher_model and req["output_config"]["format"]["type"] == "json_schema"
    run(go())


def test_no_frames_means_no_call():
    async def go():
        w, st, clk, _, client, _ = make(frames=Frames(n=0))
        await run_check(w)
        assert client.requests == []
    run(go())


# ───────────── applying the answer ─────────────

def test_new_events_are_logged_deduped_and_capped():
    async def go():
        w, st, clk, *_ = make(answer(["Avocado halved", "avocado halved.", "pit removed", "a", "b", "c"]))
        st.log("vision", "pit removed earlier")
        await run_check(w)
        vision = [e.text for e in st.events if e.source == "vision"]
        assert vision.count("Avocado halved") == 1 and "avocado halved." not in vision
        assert "pit removed" not in vision                      # contained in an existing event
        assert len(vision) - 1 <= W.MAX_EVENTS_PER_CALL
    run(go())


def test_repeated_events_across_calls_are_not_relogged():
    async def go():
        w, st, clk, *_ = make(answer(["avocado halved"]), answer(["avocado halved"]))
        await run_check(w)
        clk.t += W.STEP_INTERVAL_S + 1
        await run_check(w)
        assert [e.text for e in st.events].count("avocado halved") == 1
    run(go())


def test_confident_done_asks_the_user_once():
    async def go():
        asked = []

        async def on_ask(t):
            asked.append(t)
        w, st, clk, sp, client, _ = make(answer(done=True, conf=0.95, note="mashed"),
                                         answer(done=True, conf=0.95, note="mashed"), step=2, on_ask=on_ask)
        steps = Steps(st)
        w.steps = steps
        await run_check(w)
        await w.tick()                                            # the question goes out on the next tick
        assert st.step == 2                                       # never advances by itself
        assert len(asked) == 1 and "[check-in" in asked[0] and "mashed" in asked[0]
        assert steps.confirm() == "advance" and st.step == 3
        # the same step isn't suggested twice
        st.step = 2
        clk.t += W.STEP_INTERVAL_S + 1
        await run_check(w)
        await w.tick()
        assert len(asked) == 1
    run(go())


def test_low_confidence_or_not_done_does_not_ask():
    async def go():
        asked = []

        async def on_ask(t):
            asked.append(t)
        w, st, clk, *_ = make(answer(done=True, conf=0.5), answer(done=False, conf=0.99), on_ask=on_ask)
        await run_check(w)
        clk.t += W.STEP_INTERVAL_S + 1
        await run_check(w)
        assert asked == [] and w._proposed_step is None
    run(go())


def test_question_waits_until_quiet_and_is_dropped_if_step_changes():
    async def go():
        asked = []

        async def on_ask(t):
            asked.append(t)
        w, st, clk, sp, client, _ = make(answer(done=True, conf=0.95, note="x"), on_ask=on_ask)
        w.steps = Steps(st)
        await w.tick()
        st.user_talking = True                                    # user starts talking during the model call
        await w._task
        await w.tick()
        assert asked == []                                        # held back
        st.user_talking = False
        await w.tick()
        assert len(asked) == 1

        # pending question for a step the user has already left is dropped
        w._pending_ask = (st.step, "[check-in: old]")
        st.step += 1
        await w.tick()
        assert w._pending_ask is None and len(asked) == 1
    run(go())


def test_verdict_cache_for_steps():
    async def go():
        w, st, clk, *_ = make(answer(done=False, conf=0.9, note="still whole"), step=1)
        assert w.latest_verdict(1) is None
        await run_check(w)
        v = w.latest_verdict(1)
        assert v == StepVerdict(False, 0.9, "still whole")
        assert w.latest_verdict(2) is None                        # other step
        clk.t += W.VERDICT_TTL_S + 1
        assert w.latest_verdict(1) is None                        # stale
    run(go())


def test_verdict_drives_a_pushback_in_steps():
    async def go():
        st = State(RECIPE)
        w, st, clk, sp, client, _ = make(answer(done=False, conf=0.9, note="whole avocados"))
        steps = Steps(st, verdict_provider=w.latest_verdict, clock=clk, pushback=True)
        w.steps = steps
        await run_check(w)
        assert steps.propose_done("user") == "push_back" and steps.reason == "whole avocados"
    run(go())


def test_result_for_a_step_the_user_already_left_is_ignored():
    async def go():
        w, st, clk, sp, client, _ = make(answer(["avocado halved"], done=True, conf=0.99), step=0)
        await w.tick()
        st.step = 1                                               # user advanced during the call
        await w._task
        assert w._verdict is None and w._proposed_step is None
        assert "avocado halved" in [e.text for e in st.events]    # the event itself is still true
    run(go())


def test_failures_are_swallowed():
    async def go():
        for bad in (RuntimeError("boom"), "not json", {"events": []}, asyncio.TimeoutError()):
            w, st, clk, *_ = make(bad)
            await run_check(w)
            assert st.events == [] and w._verdict is None
    run(go())


def test_run_loop_survives_errors_and_stops_on_cancel():
    async def go():
        w, st, clk, sp, client, _ = make()
        calls = []

        async def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
        w.tick = flaky
        task = asyncio.create_task(w.run())
        await asyncio.sleep(W.TICK_S * 3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert len(calls) >= 2
    run(go())


# ───────────── never talks over anyone ─────────────

def _pending_ask_watcher(busy=None):
    asked = []

    async def on_ask(t):
        asked.append(t)
    w, st, clk, sp, client, _ = make(on_ask=on_ask, busy=busy)
    w._pending_ask = (st.step, "[check-in: ready?]")
    return w, st, clk, asked


def test_question_never_starts_while_an_agent_turn_is_running():
    """A new agent turn cancels the one in flight, so asking mid-turn would kill the user's answer."""
    async def go():
        state = {"busy": True}
        w, st, clk, asked = _pending_ask_watcher(busy=lambda: state["busy"])
        for _ in range(5):
            clk.t += 1
            await w.tick()
        assert asked == []                                        # still thinking / speaking its answer
        state["busy"] = False
        await w.tick()
        assert asked == []                                        # agent just finished: TTS may still be starting
        clk.t += W.BUSY_SETTLE_S + 0.1
        await w.tick()
        assert len(asked) == 1
    run(go())


def test_question_waits_for_a_quiet_moment_after_anyone_spoke():
    async def go():
        w, st, clk, asked = _pending_ask_watcher()
        st.last_activity = time.monotonic()                       # the user / coach just stopped talking
        clk.t += 10
        await w.tick()
        assert asked == []
        st.last_activity = time.monotonic() - W.ASK_SETTLE_S - 0.1
        await w.tick()
        assert len(asked) == 1
    run(go())


def test_question_waits_while_coach_speaks_or_user_talks():
    async def go():
        w, st, clk, asked = _pending_ask_watcher()
        clk.t += 10
        st.speaking = True
        await w.tick()
        st.speaking, st.user_talking = False, True
        await w.tick()
        assert asked == []
        st.user_talking = False
        await w.tick()
        assert len(asked) == 1
    run(go())


def test_the_model_check_itself_never_speaks():
    """Only the question ever reaches the speaker; a normal check adds events only."""
    async def go():
        w, st, clk, sp, client, _ = make(answer(["avocado halved"], done=False, conf=0.9, note="x"))
        st.speaking = False
        await run_check(w)
        assert sp.said == []
    run(go())


# ───────────── "ignore the camera": the watcher goes quiet too ─────────────

def test_ignored_camera_means_no_looking_no_verdicts_no_questions():
    async def go():
        flag = {"ignored": False}
        asked = []

        async def on_ask(t):
            asked.append(t)
        w, st, clk, sp, client, _ = make(answer(done=False, conf=0.9, note="x"), on_ask=on_ask)
        w._ignored = lambda: flag["ignored"]
        await run_check(w)
        assert len(client.requests) == 1 and w.latest_verdict(0) is not None
        flag["ignored"] = True
        assert w.latest_verdict(0) is None                         # no push-back on a covered camera's say-so
        w._pending_ask = (st.step, "[check-in: ready?]")
        clk.t += 100
        st.dets = [Det("lime", 0.9, (0, 0, 10, 10))]
        st.last_activity = time.monotonic() - 60
        await run_check(w)
        assert len(client.requests) == 1 and asked == [] and w._pending_ask is None
        flag["ignored"] = False
        clk.t += 100
        await run_check(w)
        assert len(client.requests) == 2                           # looks again once the user wants the camera back
    run(go())
