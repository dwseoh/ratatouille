"""The agent's tools (make_handlers) and what each turn carries (state note, cached history). Offline: fakes only."""
import asyncio

import numpy as np

from coach import agent as A
from coach.agent import SYSTEM, TOOLS, Agent, build_system, make_handlers, validate_tool_input
from coach.config import Config
from coach.contracts import TOOL_NAMES, FakeAimer, FakeLink, FakeSteps, State, fake_dets
from coach.session import Session
from coach.steps import Steps
from coach.timers import Timers

from test_agent import RECIPE, FakeClient, FakeStream, Sink, assert_valid, make_agent, run, text, tool

CFG = Config()


class SpySpeech:
    def __init__(self):
        self.said, self.interrupts, self.chimes, self.turns, self.speaking = [], 0, [], [], False

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


def kit(step=0, visible=("avocado", "lime", "bowl", "knife")):
    """Real Steps + Timers + Session, fake hardware: everything a tool can touch."""
    st = State(RECIPE, dets=fake_dets(visible), session="active")
    st.step = step
    aimer, link, steps = FakeAimer(st), FakeLink(), Steps(st)
    timers = Timers(st, link)
    session = Session(st, SpySpeech(), Agent(CFG, st, {}, client=FakeClient()), aimer, steps, link)
    st.session = "active"
    h = make_handlers(st, aimer, steps, link, timers=timers, session=lambda: session)
    return h, st, aimer, steps, timers, session


# ───────────── the tool list itself ─────────────

def test_tool_names_match_contracts_handlers_and_are_strict():
    h, *_ = kit()
    assert [t["name"] for t in TOOLS] and set(t["name"] for t in TOOLS) == set(TOOL_NAMES) == set(h)
    for t in TOOLS:
        schema = t["input_schema"]
        assert schema["additionalProperties"] is False and sorted(schema["required"]) == sorted(schema["properties"])
        assert t["eager_input_streaming"] is True and len(t["description"]) > 15


def test_validate_arrays_and_numbers():
    assert validate_tool_input("point_sequence", {"names": ["lime", "bowl"]}) is None
    assert "array" in validate_tool_input("point_sequence", {"names": "lime"})
    assert "string" in validate_tool_input("point_sequence", {"names": ["lime", 3]})
    assert validate_tool_input("set_timer", {"seconds": 300, "label": "onions"}) is None
    assert "integer" in validate_tool_input("set_timer", {"seconds": 1.5, "label": ""})
    assert "integer" in validate_tool_input("set_timer", {"seconds": True, "label": ""})   # bools aren't numbers
    assert "missing 'label'" in validate_tool_input("set_timer", {"seconds": 300})
    assert validate_tool_input("pause_session", {}) is None and "unexpected" in validate_tool_input("pause_session", {"x": 1})


def test_system_prompt_is_stable_and_carries_the_numbered_recipe():
    sys1, sys2 = build_system(RECIPE), build_system(RECIPE)
    assert sys1 == sys2 and sys1[0]["cache_control"] == {"type": "ephemeral"}
    body = sys1[0]["text"]
    assert body.startswith(SYSTEM) and "Step 3 (mash):" in body and "instead you can use: parsley" in body
    assert "You are Remy" in SYSTEM and "go_to_step" in SYSTEM and "cook notes" in SYSTEM


# ───────────── navigation ─────────────

def test_go_to_step_back_and_forward():
    h, st, aimer, steps, *_ = kit(step=3)
    aimer.point_at("knife")
    changes = []
    steps.on_change(changes.append)
    assert h["go_to_step"](step=2) == f"ok: now on step 2 (remove pits): {RECIPE.steps[1].goal}"
    assert st.step == 1 and changes == [1] and st.target is None           # laser off: it was the old step's
    assert any("agent moved from step 4 to step 2" in e.text for e in st.events)
    assert h["go_to_step"](step=6).startswith("ok: now on step 6") and st.step == 5
    assert h["go_to_step"](step=6) == "already on step 6"
    assert h["go_to_step"](step=9).startswith("error: there is no step 9") and st.step == 5
    assert h["go_to_step"](step=0).startswith("error")


def test_go_to_step_after_finishing_reopens_the_recipe():
    h, st, aimer, steps, *_ = kit(step=6)
    steps._clock = lambda: 1e9
    assert steps.propose_done("user") == "finished" and steps.finished
    assert h["go_to_step"](step=7).startswith("ok") and not steps.finished


def test_go_to_step_works_with_fake_steps_and_says_so_without_support():
    st = State(RECIPE, dets=fake_dets())
    h = make_handlers(st, FakeAimer(st), FakeSteps(st), FakeLink())
    assert h["go_to_step"](step=3).startswith("ok") and st.step == 2

    class OldSteps(FakeSteps):
        go_to = None
    h = make_handlers(st, FakeAimer(st), OldSteps(st), FakeLink())
    assert h["go_to_step"](step=1).startswith("error")


def test_going_back_keeps_the_conversation():
    streams = [FakeStream([text("Okay, step two it is then. "), tool("g", "go_to_step", {"step": 2})], "tool_use"),
               FakeStream([text("Scoop the flesh into the bowl. ")])]
    agent, st, client, aimer = make_agent(*streams, step=3)
    st.step_since = st.cook_clock()
    agent.handlers = make_handlers(st, aimer, Steps(st), FakeLink())
    st.step = 3
    run(agent.turn("go back to the pits step", [], Sink()))
    assert st.step == 1
    run(agent.turn("how do I scoop it?", [], Sink()))
    msgs = client.requests[1]["messages"]
    assert len(msgs) == 3 and "go back to the pits step" in msgs[0]["content"][-1]["text"]   # not wiped as a restart
    assert_valid(msgs)


# ───────────── pointing at several things ─────────────

def test_point_sequence_tours_the_visible_objects(monkeypatch):
    monkeypatch.setattr(A, "POINT_DWELL_S", 0.02)

    async def go():
        h, st, aimer, *_ = kit()
        seen = []
        orig = aimer.point_at
        aimer.point_at = lambda n: (seen.append(n), orig(n))[1]
        out = h["point_sequence"](names=["avocado", "salt", "knife", "spoon", "knife"])
        assert out == "ok: showing avocado, knife; not visible: salt; not recipe objects: spoon"
        await asyncio.sleep(0.1)
        assert seen == ["avocado", "knife"] and st.target == "knife"            # stays on the last one
    run(go())


def test_point_sequence_stops_when_paused_or_overridden(monkeypatch):
    monkeypatch.setattr(A, "POINT_DWELL_S", 0.03)

    async def go():
        h, st, aimer, _, _, session = kit()
        h["point_sequence"](names=["avocado", "lime", "bowl"])
        await asyncio.sleep(0.01)
        h["point_at"](name="knife")                                             # the cook asked for something else
        await asyncio.sleep(0.1)
        assert st.target == "knife"
        h["point_sequence"](names=["avocado", "lime", "bowl"])
        session.pause_now("test")
        await asyncio.sleep(0.1)
        assert st.target is None                                                # paused: laser off, tour over
    run(go())


def test_point_sequence_with_nothing_visible_turns_the_laser_off():
    h, st, aimer, *_ = kit(visible=("bowl",))
    aimer.point_at("bowl")
    assert h["point_sequence"](names=["lime"]) == "not_visible (not visible: lime)" and st.target is None


# ───────────── timers, notes, servings ─────────────

def test_timer_tools():
    h, st, *_ = kit()
    assert h["set_timer"](seconds=300, label="onions") == "ok: started onions timer, 5 minutes"
    assert "onions" in st.timers
    assert h["cancel_timer"](label="") == "ok: cancelled onions" and not st.timers
    st2 = State(RECIPE)
    bare = make_handlers(st2, FakeAimer(st2), FakeSteps(st2), FakeLink())
    assert bare["set_timer"](seconds=60, label="x").startswith("error")


def test_remember_dedupes_and_keeps_the_newest():
    h, st, *_ = kit()
    assert h["remember"](fact="No cilantro.") == "ok: noted" and st.cook_notes == ["No cilantro"]
    assert h["remember"](fact="no cilantro") == "ok: already noted"
    assert h["remember"](fact="   ").startswith("error")
    for i in range(A.MAX_COOK_NOTES + 2):
        h["remember"](fact=f"fact {i}")
    assert len(st.cook_notes) == A.MAX_COOK_NOTES and st.cook_notes[-1] == f"fact {A.MAX_COOK_NOTES + 1}"
    assert any(e.text == "noted: No cilantro" for e in st.events)


def test_set_servings_scales_and_resets():
    h, st, *_ = kit()
    assert h["set_servings"](servings=4).startswith("ok: amounts for 4: 4 avocado, 1/2 onion") and st.servings == 4
    assert h["set_servings"](servings=2).startswith("ok") and st.servings == 0          # back to the recipe's own
    assert h["set_servings"](servings=0).startswith("error") and h["set_servings"](servings=99).startswith("error")
    from coach.contracts import Recipe
    plain = Recipe("Toast", ["bread"], {}, RECIPE.steps[:1])
    st2 = State(plain)
    h2 = make_handlers(st2, FakeAimer(st2), FakeSteps(st2), FakeLink())
    assert h2["set_servings"](servings=4).startswith("error: this recipe has no amounts")


# ───────────── pause / resume tools ─────────────

def test_pause_and_resume_tools_drive_the_session_without_cutting_the_reply():
    h, st, aimer, _, _, session = kit()
    aimer.point_at("lime")
    assert h["pause_session"]().startswith("ok: paused") and st.session == "paused" and st.target is None
    assert session.speech.interrupts == 0 and session.speech.said == []    # its own "sure, I'll wait" keeps playing
    assert h["pause_session"]() == "not paused: the session is paused"
    assert h["resume_session"]() == "ok: resumed" and st.session == "active"
    assert h["resume_session"]() == "not resumed: the session is active"
    st2 = State(RECIPE)
    bare = make_handlers(st2, FakeAimer(st2), FakeSteps(st2), FakeLink())
    assert bare["pause_session"]().startswith("error") and bare["resume_session"]().startswith("error")


def test_pause_tool_mid_turn_keeps_the_spoken_answer():
    s1 = FakeStream([text("Sure, I'll wait for you. "), tool("p", "pause_session", {})], "tool_use")
    agent, st, client, aimer = make_agent(s1)
    st.session = "active"
    speech = SpySpeech()
    session = Session(st, speech, agent, aimer, FakeSteps(st), FakeLink())
    st.session = "active"
    agent.handlers = make_handlers(st, aimer, FakeSteps(st), FakeLink(), session=session)
    sink = Sink()
    run(agent.turn("I need to get the door", [], sink))
    assert sink.sentences == ["Sure, I'll wait for you."] and st.session == "paused"


# ───────────── tools-only turns are never silent ─────────────

def test_backstop_lines_for_the_new_tools():
    cases = [("go_to_step", {"step": 2}, f"Okay, step 2. {RECIPE.steps[1].fallback}"),
             ("set_timer", {"seconds": 60, "label": "x"}, "Okay, timer's running."),
             ("remember", {"fact": "no salt"}, "Got it, I'll keep that in mind."),
             ("set_servings", {"servings": 4}, "Okay, I've scaled the amounts."),
             ("point_sequence", {"names": ["avocado"]}, "Here's what you need.")]
    for name, inp, line in cases:
        agent, st, client, aimer = make_agent(FakeStream([tool("t", name, inp)], "tool_use"), step=3)
        agent.handlers = make_handlers(st, aimer, Steps(st), FakeLink(), timers=Timers(st))
        st.step = 3
        sink = Sink()
        run(agent.turn("do it", [], sink))
        assert sink.sentences == [line], name


# ───────────── what each turn carries ─────────────

def test_history_gets_one_cache_breakpoint_and_the_stored_history_none():
    streams = [FakeStream([text(f"Answer number {i} for you. ")]) for i in range(3)]
    agent, st, client, _ = make_agent(*streams)
    for q in ("a", "b", "c"):
        run(agent.turn(q, [], Sink()))
    first, second, third = (r["messages"] for r in client.requests)
    assert not any("cache_control" in b for m in first for b in m["content"])        # no history yet
    marked = [(i, b) for i, m in enumerate(third) for b in m["content"] if "cache_control" in b]
    assert [i for i, _ in marked] == [3]                                            # last history message only
    assert marked[0][1]["text"] == "Answer number 1 for you."
    assert not any("cache_control" in b for e in agent._exchanges for m in (e.user, e.assistant) for b in m["content"])
    # the prefix the 2nd request cached is byte-identical inside the 3rd (minus the moving marker)
    strip = lambda ms: [{**m, "content": [{k: v for k, v in b.items() if k != "cache_control"} for b in m["content"]]}
                        for m in ms]                                                # noqa: E731
    assert strip(third[:2]) == strip(second[:2])


def test_note_carries_the_richer_context():
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    agent, st, client, _ = make_agent(FakeStream([text("Okay, here you go then. ")]), visible=("avocado", "bowl"))
    st.cook_notes.append("no cilantro")
    st.timers["onions"] = 10 ** 9
    run(agent.turn("what now?", [frame], Sink()))
    note = client.requests[0]["messages"][-1]["content"][-1]["text"]
    for part in ("[step 1/7 (halve avocados)", "next: step 2 (remove pits)", "needed now, not in view: knife",
                 "laser: off", "timers: onions", "cook notes: no cilantro", "User: what now?"):
        assert part in note, part


def test_cook_said_earlier_survives_history_trimming_and_questions_flag_a_stuck_cook():
    n = A.KEEP_TURNS + 4
    agent, st, client, _ = make_agent(*[FakeStream([text("Sure thing, here you go. ")]) for _ in range(n + 1)])
    run(agent.turn("I don't have any cilantro", [], Sink()))
    st.step = 1                                                   # moved on: step 1's turns can age out
    st.step_since = st.cook_clock()
    for i in range(n - 1):
        run(agent.turn(f"how do I do part {i}?", [], Sink()))
    note = client.requests[-1]["messages"][-1]["content"][-1]["text"]
    kept = " ".join(m["content"][-1].get("text", "") for m in client.requests[-1]["messages"][:-1])
    assert "User: I don't have any cilantro" not in kept          # trimmed from the history...
    assert 'cook said earlier: "I don\'t have any cilantro"' in note   # ...but still in the note
    assert "cook may be stuck" in note and "questions about it" in note
