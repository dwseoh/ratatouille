"""Agent tests: sentence splitter, tools, cancel, history trimming, prewarm. Offline: a fake Anthropic client."""
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace as NS

import anthropic
import httpx
import numpy as np

from coach.agent import KEEP_TURNS, Agent, SentenceSplitter, make_handlers, validate_tool_input
from coach.config import Config
from coach.contracts import Det, FakeAimer, FakeLink, FakeSteps, Recipe, State, fake_dets

RECIPE = Recipe.load(Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json")
CFG = Config()


# ───────────── fake streaming client ─────────────

def text(t):
    return NS(type="text", text=t)


def text_done():
    return NS(type="content_block_stop", content_block=NS(type="text"))


def tool(id_, name, inp):
    return NS(type="content_block_stop", content_block=NS(type="tool_use", id=id_, name=name, input=inp))


class FakeStream:
    def __init__(self, events, stop_reason="end_turn", delay=0.0, fail=None, delays=None, enter_error=None):
        self.events, self.delay, self.fail, self.stop_reason = events, delay, fail, stop_reason
        self.delays, self.enter_error = delays, enter_error      # delays[i] = pause before event i
        self.consumed, self.exited = 0, False

    async def __aenter__(self):
        if self.enter_error:
            raise self.enter_error
        return self

    async def __aexit__(self, *exc):
        self.exited = True
        return False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for i, e in enumerate(self.events):
            pause = self.delays[i] if self.delays else self.delay
            if pause:
                await asyncio.sleep(pause)
            self.consumed += 1
            yield e
        if self.fail:
            raise self.fail

    async def get_final_message(self):
        blocks = [e.content_block for e in self.events if e.type == "content_block_stop" and e.content_block.type == "tool_use"]
        return NS(stop_reason=self.stop_reason, content=blocks)


class FakeClient:
    """client.beta.messages.stream(...) pops scripted streams; client.messages.create(...) is the prewarm."""

    def __init__(self, *streams, create_error=None):
        self.streams, self.requests, self.creates, self.create_error = list(streams), [], [], create_error
        self.beta = NS(messages=NS(stream=self._stream))
        self.messages = NS(create=self._create)

    def _stream(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        return self.streams.pop(0)

    async def _create(self, **kw):
        self.creates.append(kw)
        if self.create_error:
            raise self.create_error
        return NS(usage=NS(cache_creation_input_tokens=100, cache_read_input_tokens=0))


def make_agent(*streams, step=0, visible=None, **kw):
    st = State(RECIPE, dets=fake_dets(tuple(visible) if visible is not None else ("avocado", "lime", "bowl", "knife")))
    st.step = step
    aimer, steps, link = FakeAimer(st), FakeSteps(st), FakeLink()
    client = FakeClient(*streams, **kw)
    return Agent(CFG, st, make_handlers(st, aimer, steps, link), client=client), st, client, aimer


class Sink:
    def __init__(self):
        self.sentences = []

    async def __call__(self, s):
        self.sentences.append(s)


def run(coro):
    return asyncio.run(coro)


def assert_valid(messages):
    """The API rules that matter here: alternating roles, tool_use answered by the very next user message."""
    assert messages[0]["role"] == "user"
    for i, m in enumerate(messages):
        assert m["role"] == ("user" if i % 2 == 0 else "assistant"), "roles must alternate"
        uses = [b["id"] for b in m["content"] if b.get("type") == "tool_use"] if m["role"] == "assistant" else []
        results = [b["tool_use_id"] for b in m["content"] if b.get("type") == "tool_result"] if m["role"] == "user" else []
        if uses and i + 1 < len(messages):
            assert sorted(results_of(messages[i + 1])) == sorted(uses), f"tool_use {uses} not answered"
        if results:
            assert i > 0 and sorted(results) == sorted(
                b["id"] for b in messages[i - 1]["content"] if b.get("type") == "tool_use"), "orphan tool_result"


def results_of(msg):
    return [b["tool_use_id"] for b in msg["content"] if b.get("type") == "tool_result"]


# ───────────── splitter ─────────────

def test_splitter_basic_and_chunked():
    sp = SentenceSplitter()
    out = []
    for chunk in ["Cut the avocado in half.", " Then twist", " the halves apart. Nice", "!"]:
        out += sp.feed(chunk)
    assert out == ["Cut the avocado in half.", "Then twist the halves apart."]
    assert sp.flush() == "Nice!"


def test_splitter_merges_short_sentences():
    sp = SentenceSplitter()
    assert sp.feed("Okay. Next, slice the lime thinly. ") == ["Okay. Next, slice the lime thinly."]
    assert sp.flush() is None


def test_splitter_keeps_decimals_and_abbreviations():
    sp = SentenceSplitter()
    out = sp.feed("Use about 1.5 limes, e.g. the green ones, for this. ")
    assert out == ["Use about 1.5 limes, e.g. the green ones, for this."]


def test_splitter_waits_for_whitespace_then_flush_emits_short_tail():
    sp = SentenceSplitter()
    assert sp.feed("Looks great.") == []          # can't know it's the end until whitespace / flush
    assert sp.flush() == "Looks great."


# ───────────── tool validation ─────────────

def test_validate_tool_input():
    assert validate_tool_input("point_at", {"name": "lime"}) is None
    assert validate_tool_input("laser_off", {}) is None
    assert "missing" in validate_tool_input("point_at", {})
    assert "unexpected" in validate_tool_input("point_at", {"name": "lime", "x": 1})
    assert "string" in validate_tool_input("point_at", {"name": 3})
    assert "not an object" in validate_tool_input("point_at", "lime")
    assert "unknown" in validate_tool_input("set_timer", {})


# ───────────── a turn ─────────────

def test_turn_streams_sentences_runs_tool_midstream_and_reports_stats():
    order = []
    s1 = FakeStream([text("Start by halving the avocados. "), text("Here they are"), text("."), text_done(),
                     tool("t1", "point_at", {"name": "avocado"})], stop_reason="tool_use")
    agent, st, client, aimer = make_agent(s1)
    orig = agent.handlers["point_at"]
    agent.handlers["point_at"] = lambda **kw: (order.append("tool"), orig(**kw))[1]

    async def on_sentence(s):
        order.append(s)

    stats = run(agent.turn("where do I start?", [], on_sentence))
    assert order == ["Start by halving the avocados.", "Here they are.", "tool"]   # last sentence flushed before the tool
    assert stats.sentences == 2 and stats.tools == ["point_at"] and stats.ttft_s is not None and not stats.cancelled
    assert aimer.target == "avocado"
    req = client.requests[0]
    assert req["model"] == CFG.model and req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["messages"][-1]["content"][-1]["text"].startswith("[step 1/")
    assert "User: where do I start?" in req["messages"][-1]["content"][-1]["text"]


def test_tool_results_are_deferred_to_next_turn_and_not_visible_is_noted():
    s1 = FakeStream([text("Here is the lime now. "), tool("t1", "point_at", {"name": "lime"})], "tool_use")
    s2 = FakeStream([text("Sorry, I can't see it. ")])
    agent, st, client, aimer = make_agent(s1, s2, visible=("avocado",))       # lime NOT visible
    run(agent.turn("where is the lime?", [], Sink()))
    run(agent.turn("really?", [], Sink()))
    second = client.requests[1]["messages"]
    assert_valid(second)
    first_block = second[-1]["content"][0]
    assert first_block == {"type": "tool_result", "tool_use_id": "t1", "content": "not_visible"}
    assert "point_at(lime) -> not_visible" in second[-1]["content"][-1]["text"]


def test_bad_or_unknown_tool_input_returns_error_result_and_skips_handler():
    called = []
    s1 = FakeStream([text("Pointing now for you. "), tool("a", "point_at", {}), tool("b", "set_timer", {"seconds": 5})], "tool_use")
    s2 = FakeStream([text("Okay, noted that one. ")])
    agent, *_ = make_agent(s1, s2)
    agent.handlers["point_at"] = lambda **kw: called.append(kw)
    run(agent.turn("point", [], Sink()))
    run(agent.turn("again", [], Sink()))
    second = agent.client.requests[1]["messages"]
    assert_valid(second)
    results = [b for b in second[-1]["content"] if b.get("type") == "tool_result"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"] and all(r["is_error"] for r in results)
    assert called == []


def test_handler_exception_becomes_error_result():
    s1 = FakeStream([text("Pointing at it now. "), tool("a", "point_at", {"name": "lime"})], "tool_use")
    s2 = FakeStream([text("Let me try that again. ")])
    agent, *_ = make_agent(s1, s2)
    agent.handlers["point_at"] = lambda **kw: 1 / 0
    run(agent.turn("point", [], Sink()))
    run(agent.turn("again", [], Sink()))
    res = agent.client.requests[1]["messages"][-1]["content"][0]
    assert res["is_error"] and "division" in res["content"]


def test_propose_done_goes_through_steps():
    s1 = FakeStream([text("Looks mashed to me. "), tool("a", "propose_done", {})], "tool_use")
    agent, st, *_ = make_agent(s1, step=2)
    run(agent.turn("I'm done mashing", [], Sink()))
    assert st.step == 3          # FakeSteps advances on "llm"
    assert any("proposed step done" in e.text for e in st.events)


# ───────────── errors / fallbacks / check-ins ─────────────

def test_api_error_speaks_step_fallback_and_sets_error():
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
    s1 = FakeStream([], fail=err)
    agent, st, *_ = make_agent(s1)
    sink = Sink()
    stats = run(agent.turn("hello", [], sink))
    assert sink.sentences == [RECIPE.steps[0].fallback]
    assert stats.error and "APIConnectionError" in stats.error
    assert not agent.busy


def test_checkin_skip_says_nothing_and_leaves_no_history():
    s1 = FakeStream([text("SKIP")])
    agent, *_ = make_agent(s1)
    sink = Sink()
    stats = run(agent.turn("[check-in: quiet for 20s]", [], sink))
    assert sink.sentences == [] and stats.sentences == 0 and agent._exchanges == []


# ───────────── cancel ─────────────

def test_cancel_mid_stream_stops_sentences_and_keeps_history_valid():
    events = [text("First sentence is spoken now. "), tool("t1", "point_at", {"name": "avocado"}),
              text("Second sentence never gets said. "), text("Third neither. ")]
    s1 = FakeStream(events, "tool_use", delay=0.05)
    s2 = FakeStream([text("Okay, carrying on from there. ")])
    agent, st, client, _ = make_agent(s1, s2)
    sink = Sink()

    async def scenario():
        task = asyncio.create_task(agent.turn("go", [], sink))
        await asyncio.sleep(0.12)         # after sentence 1 + the tool, before the rest
        agent.cancel()
        stats = await task
        assert stats.cancelled and not agent.busy
        n = len(sink.sentences)
        await asyncio.sleep(0.2)
        assert len(sink.sentences) == n   # nothing arrives after cancel
        await agent.turn("hello again", [], Sink())

    run(scenario())
    assert sink.sentences == ["First sentence is spoken now."]
    second = client.requests[1]["messages"]
    assert_valid(second)
    assert second[1]["content"][0]["text"] == "First sentence is spoken now."       # what the user actually heard
    assert second[-1]["content"][0]["type"] == "tool_result"                        # the tool that did run is answered


def test_cancel_before_anything_produced_leaves_pending_results_for_next_turn():
    s1 = FakeStream([text("Pointing there now. "), tool("t1", "point_at", {"name": "lime"})], "tool_use")
    s2 = FakeStream([text("never reached")], delay=0.5)
    s3 = FakeStream([text("Back to it now, then. ")])
    agent, st, client, _ = make_agent(s1, s2, s3)
    run(agent.turn("one", [], Sink()))

    async def scenario():
        task = asyncio.create_task(agent.turn("two", [], Sink()))
        await asyncio.sleep(0.05)
        agent.cancel()
        await task
        await agent.turn("three", [], Sink())

    run(scenario())
    assert_valid(client.requests[2]["messages"])
    assert client.requests[2]["messages"][-1]["content"][0]["tool_use_id"] == "t1"   # still owed, delivered once


def test_outer_task_cancel_propagates_and_commits():
    s1 = FakeStream([text("Hello there my friend. "), tool("t1", "laser_off", {}), text("More text later. ")],
                    "tool_use", delay=0.05)
    agent, *_ = make_agent(s1)

    async def scenario():
        task = asyncio.create_task(agent.turn("go", [], Sink()))
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"

    assert run(scenario()) == "cancelled"
    assert not agent.busy and len(agent._exchanges) == 1


# ───────────── history trimming ─────────────

def _script(n, tools=True):
    out = []
    for i in range(n):
        ev = [text(f"Reply number {i} for you. ")]
        if tools:
            ev.append(tool(f"t{i}", "laser_off", {}))
        out.append(FakeStream(ev, "tool_use" if tools else "end_turn"))
    return out


def test_history_trims_old_steps_to_last_turns_and_stays_valid():
    agent, st, client, _ = make_agent(*_script(KEEP_TURNS + 5))
    for i in range(KEEP_TURNS + 4):               # many tool-using turns in step 0
        run(agent.turn(f"q{i}", [], Sink()))
    st.step = 1                                   # step changes: step 0 may now age out
    run(agent.turn("next", [], Sink()))
    last = client.requests[-1]["messages"]
    assert_valid(last)
    assert len(last) == 2 * KEEP_TURNS + 1
    assert last[0]["content"][-1]["text"].endswith("User: q%d" % 4)    # oldest kept = 5th of 10 turns
    assert all(b["type"] != "tool_result" for b in last[0]["content"])  # its tool_use was trimmed away


def test_history_keeps_whole_current_step_even_if_longer_than_keep_turns():
    agent, st, client, _ = make_agent(*_script(KEEP_TURNS + 5, tools=False), step=1)
    for i in range(KEEP_TURNS + 5):
        run(agent.turn(f"q{i}", [], Sink()))
    assert len(client.requests[-1]["messages"]) == 2 * (KEEP_TURNS + 4) + 1      # nothing dropped yet (all in step 1)


def test_step_change_lets_old_step_age_out():
    agent, st, client, _ = make_agent(*_script(KEEP_TURNS + 3, tools=False))
    for i in range(KEEP_TURNS + 1):
        run(agent.turn(f"q{i}", [], Sink()))
    st.step = 1
    run(agent.turn("new step", [], Sink()))
    run(agent.turn("again", [], Sink()))
    assert len(client.requests[-1]["messages"]) == 2 * KEEP_TURNS + 1


def test_restart_clears_history():
    agent, st, client, _ = make_agent(*_script(3, tools=False), step=2)
    run(agent.turn("a", [], Sink()))
    run(agent.turn("b", [], Sink()))
    st.step = 0                                   # Steps.reset()
    run(agent.turn("c", [], Sink()))
    assert len(client.requests[-1]["messages"]) == 1


def test_images_only_in_the_current_request():
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    agent, st, client, _ = make_agent(*_script(2, tools=False))
    run(agent.turn("a", [frame, frame], Sink()))
    run(agent.turn("b", [frame], Sink()))
    first, second = client.requests
    assert sum(b["type"] == "image" for b in first["messages"][-1]["content"]) == 2
    assert sum(b["type"] == "image" for m in second["messages"][:-1] for b in m["content"]) == 0
    assert sum(b["type"] == "image" for b in second["messages"][-1]["content"]) == 1


# ───────────── prewarm ─────────────

def test_prewarm_sends_max_tokens_zero_with_cached_system():
    agent, _, client, _ = make_agent()
    run(agent.prewarm())
    kw = client.creates[0]
    assert kw["max_tokens"] == 0 and kw["system"][0]["cache_control"] == {"type": "ephemeral"} and kw["tools"]


def test_prewarm_never_raises():
    agent, _, client, _ = make_agent(create_error=RuntimeError("boom"))
    run(agent.prewarm())      # just logs


def test_fallbacks_only_for_supported_models():
    agent, *_ = make_agent()
    agent.cfg = Config(model="claude-haiku-5-5")
    assert "fallbacks" not in agent._params([])                      # Haiku 5.5 has no server-side fallback
    agent.cfg = Config(model="claude-opus-5-5")
    assert agent._params([])["fallbacks"] == "default"


def test_pushback_reason_reaches_the_model_via_the_tool_result():
    from coach.steps import StepVerdict, Steps
    s1 = FakeStream([text("Looks mashed to me. "), tool("a", "propose_done", {})], "tool_use")
    s2 = FakeStream([text("Hold on, not yet then. ")])
    agent, st, client, aimer = make_agent(s1, s2, step=2)
    steps = Steps(st, None, lambda step: StepVerdict(False, 0.9, "still big chunks of avocado"))
    agent.handlers = make_handlers(st, aimer, steps, FakeLink())
    run(agent.turn("I'm done", [], Sink()))
    run(agent.turn("really?", [], Sink()))
    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["content"] == "push_back: still big chunks of avocado" and st.step == 2


# ───────────── B5: fallbacks ─────────────

from coach.agent import REPEAT_FALLBACK  # noqa: E402


def test_no_first_token_in_time_speaks_the_canned_line():
    slow = FakeStream([text("Too late to matter now. ")], delay=0.3)
    nxt = FakeStream([text("Back to normal now, thanks. ")])
    agent, st, client, _ = make_agent(slow, nxt)
    agent.first_token_timeout = 0.05
    sink = Sink()
    stats = run(agent.turn("where do I start?", [], sink))
    assert sink.sentences == [RECIPE.steps[0].fallback]
    assert stats.error and "timeout" in stats.error and not agent.busy
    assert slow.exited                                          # the slow request was closed, not left running
    assert any(e.source == "system" and "agent fallback" in e.text for e in st.events)
    run(agent.turn("hello?", [], Sink()))                       # and the agent is usable afterwards
    assert_valid(client.requests[1]["messages"])


def test_timeout_only_guards_the_wait_for_the_first_token():
    # first token arrives at once; the rest is slower than the timeout and must NOT trigger a fallback
    s1 = FakeStream([text("Starting right here now. "), text("Then a slow second sentence. "), text_done()],
                    delays=[0.0, 0.15, 0.15])
    agent, st, *_ = make_agent(s1)
    agent.first_token_timeout = 0.05
    sink = Sink()
    stats = run(agent.turn("go", [], sink))
    assert stats.error is None and len(sink.sentences) == 2


def test_tool_call_first_also_ends_the_wait():
    s1 = FakeStream([tool("t1", "laser_off", {}), text("All done with that. ")], "tool_use", delays=[0.0, 0.15])
    agent, *_ = make_agent(s1)
    agent.first_token_timeout = 0.05
    assert run(agent.turn("go", [], Sink())).error is None


def test_canned_line_once_per_step_then_a_short_apology():
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
    agent, st, *_ = make_agent(*[FakeStream([], fail=err) for _ in range(5)])
    sinks = [Sink() for _ in range(5)]
    run(agent.turn("a", [], sinks[0]))
    run(agent.turn("b", [], sinks[1]))
    st.step = 1                                                 # new step: canned line again
    run(agent.turn("c", [], sinks[2]))
    run(agent.turn("d", [], sinks[3]))
    agent.reset()                                               # new session: canned line again
    st.step = 0
    run(agent.turn("e", [], sinks[4]))
    assert [x.sentences for x in sinks] == [[RECIPE.steps[0].fallback], [REPEAT_FALLBACK],
                                            [RECIPE.steps[1].fallback], [REPEAT_FALLBACK],
                                            [RECIPE.steps[0].fallback]]


def test_fallback_uses_the_step_after_the_intent_router_advanced_it():
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
    agent, st, *_ = make_agent(FakeStream([], fail=err))
    st.step = 1                      # "done" was handled locally just before the agent turn
    sink = Sink()
    run(agent.turn("I'm done", [], sink))
    assert sink.sentences == [RECIPE.steps[1].fallback]


def test_refusal_with_nothing_said_speaks_the_canned_line():
    agent, st, *_ = make_agent(FakeStream([], stop_reason="refusal"))
    sink = Sink()
    stats = run(agent.turn("hi", [], sink))
    assert sink.sentences == [RECIPE.steps[0].fallback] and stats.error == "refusal"


def test_refusal_after_speaking_adds_nothing():
    agent, st, *_ = make_agent(FakeStream([text("Here is what I can say. ")], stop_reason="refusal"))
    sink = Sink()
    stats = run(agent.turn("hi", [], sink))
    assert sink.sentences == ["Here is what I can say."] and stats.error is None


def test_checkin_failure_stays_silent_but_is_recorded():
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
    agent, st, *_ = make_agent(FakeStream([], fail=err))
    sink = Sink()
    stats = run(agent.turn("[check-in: quiet for 20 s]", [], sink))
    assert sink.sentences == [] and stats.error


def test_missing_credentials_speaks_canned_line_but_real_type_errors_are_not_hidden():
    no_key = TypeError("Could not resolve authentication method. Expected one of api_key, auth_token")
    agent, st, *_ = make_agent(FakeStream([], enter_error=no_key))
    sink = Sink()
    stats = run(agent.turn("hi", [], sink))
    assert sink.sentences == [RECIPE.steps[0].fallback] and "credentials" in stats.error
    agent, *_ = make_agent(FakeStream([], enter_error=TypeError("unsupported operand")))
    try:
        run(agent.turn("hi", [], Sink()))
        raise AssertionError("a genuine bug must not be swallowed")
    except TypeError:
        pass


def test_prewarm_with_no_credentials_logs_a_clear_error(caplog):
    agent, *_ = make_agent(create_error=TypeError("Could not resolve authentication method."))
    with caplog.at_level("ERROR"):
        run(agent.prewarm())
    assert "ANTHROPIC_API_KEY" in caplog.text


def test_no_camera_is_flagged_once_per_outage():
    streams = [FakeStream([text("Okay, noted that for you. ")]) for _ in range(4)]
    agent, st, client, _ = make_agent(*streams)
    frame = np.zeros((48, 64, 3), dtype=np.uint8)

    def note(i):
        return client.requests[i]["messages"][-1]["content"][-1]["text"]
    run(agent.turn("a", [], Sink()))
    run(agent.turn("b", [], Sink()))
    run(agent.turn("c", [frame], Sink()))        # camera back
    run(agent.turn("d", [], Sink()))             # a new outage: tell again
    assert "OFFLINE" in note(0) and "tell the user once" in note(0)
    assert "OFFLINE" in note(1) and "already told" in note(1)
    assert "OFFLINE" not in note(2)
    assert "tell the user once" in note(3)
    assert all(b["type"] != "image" for b in client.requests[0]["messages"][-1]["content"])


def test_default_client_has_a_short_timeout():
    from coach.agent import CLIENT_TIMEOUT_S
    st = State(RECIPE)
    agent = Agent(CFG, st, {})
    assert agent.client.timeout == CLIENT_TIMEOUT_S and agent.first_token_timeout == CFG.first_token_timeout_s


def test_fast_mode_is_opt_in_and_reaches_requests_and_prewarm():
    agent, _, client, _ = make_agent()
    agent.cfg = Config(model="claude-opus-5-5", fast=False)
    assert "speed" not in agent._params([])
    agent.cfg = Config(model="claude-opus-5-5", fast=True)
    assert agent._params([])["speed"] == "fast"
    run(agent.prewarm())
    assert client.creates[0]["speed"] == "fast"


# ───────────── tools-only turns get a spoken backstop ─────────────

def test_tools_only_pointing_turn_is_not_silent():
    s1 = FakeStream([tool("a", "point_at", {"name": "lime"})], "tool_use")
    agent, st, *_ = make_agent(s1)
    sink = Sink()
    stats = run(agent.turn("show me the lime", [], sink))
    assert sink.sentences == ["Here's the lime."] and stats.sentences == 1 and stats.tools == ["point_at"]


def test_tools_only_not_visible_says_so():
    s1 = FakeStream([tool("a", "point_at", {"name": "lime"})], "tool_use")
    agent, st, *_ = make_agent(s1, visible=("avocado",))
    sink = Sink()
    run(agent.turn("show me the lime", [], sink))
    assert sink.sentences == ["I can't see the lime right now."]


def test_tools_only_pushback_relays_the_reason_and_other_tools_fall_back_to_the_step():
    from coach.steps import StepVerdict, Steps
    s1 = FakeStream([tool("a", "propose_done", {})], "tool_use")
    agent, st, client, aimer = make_agent(s1, step=2)
    agent.handlers = make_handlers(st, aimer, Steps(st, None, lambda s: StepVerdict(False, 0.9, "it's still chunky")), FakeLink())
    sink = Sink()
    run(agent.turn("I'm done", [], sink))
    assert sink.sentences == ["Hold on, it's still chunky."]

    s2 = FakeStream([tool("b", "show_text", {"text": "hi"})], "tool_use")
    agent, st, *_ = make_agent(s2, step=2)
    sink = Sink()
    run(agent.turn("where was I?", [], sink))
    assert sink.sentences == [f"You're on step 3: {RECIPE.steps[2].short}."]


def test_backstop_not_used_when_the_model_spoke_or_for_checkins():
    s1 = FakeStream([text("Here is the lime now. "), tool("a", "point_at", {"name": "lime"})], "tool_use")
    s2 = FakeStream([tool("b", "laser_off", {})], "tool_use")
    agent, *_ = make_agent(s1, s2)
    sink = Sink()
    run(agent.turn("lime?", [], sink))
    assert sink.sentences == ["Here is the lime now."]
    sink2 = Sink()
    run(agent.turn("[check-in: quiet]", [], sink2))
    assert sink2.sentences == []


def test_fast_mode_is_never_sent_to_models_that_dont_support_it():
    agent, _, client, _ = make_agent()
    agent.cfg = Config(model="claude-haiku-5-5", fast=True)      # fast on by default, but Haiku has no fast mode
    assert "speed" not in agent._params([])
    run(agent.prewarm())
    assert "speed" not in client.creates[0]


# ───────────── accessibility (C.md 4b) ─────────────

from coach.agent import SYSTEM, positions  # noqa: E402


def _det(name, cx, cy, conf=0.9):
    return Det(name, conf, (cx - 5, cy - 5, cx + 5, cy + 5))


def test_positions_in_words():
    dets = [_det("lime", 100, 400), _det("bowl", 320, 100), _det("onion", 600, 300), _det("hand", 320, 400),
            _det("lime", 500, 100, conf=0.2)]
    assert positions(dets, 640, 480) == {"lime": "left, near", "bowl": "centre, far", "onion": "right, near"}


def test_note_says_where_things_are_and_what_the_coach_last_said():
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    s1 = FakeStream([text("Press down with the fork now. ")])
    s2 = FakeStream([text("Okay, keep going like that. ")])
    agent, st, client, _ = make_agent(s1, s2)
    st.dets = [_det("lime", 100, 400), _det("bowl", 320, 100)]
    run(agent.turn("how's this?", [frame], Sink()))
    run(agent.turn("and now?", [frame], Sink()))
    note = client.requests[1]["messages"][-1]["content"][-1]["text"]
    assert "visible: bowl (centre, far), lime (left, near) |" in note
    assert 'you last said: "Press down with the fork now."' in note


def test_no_positions_without_frames():
    agent, st, client, _ = make_agent(FakeStream([text("Okay, I'll go from the recipe. ")]))
    run(agent.turn("hi", [], Sink()))
    note = client.requests[0]["messages"][-1]["content"][-1]["text"]
    assert "(left" not in note and "(centre" not in note and "(right" not in note and "visible: avocado" in note


def test_plain_language_can_be_switched_on_and_off_by_voice():
    streams = [FakeStream([text("Sure, using simple words now. ")]) for _ in range(4)]
    agent, st, client, _ = make_agent(*streams)

    def turn_text(i):
        return client.requests[i]["messages"][-1]["content"][-1]["text"]
    run(agent.turn("how do I cut it?", [], Sink()))
    run(agent.turn("can you say it simpler", [], Sink()))
    run(agent.turn("and then?", [], Sink()))
    run(agent.turn("ok back to normal", [], Sink()))
    assert "[mode: plain]" not in turn_text(0)
    assert "[mode: plain]" in turn_text(1) and "[setting changed: plain language is now ON]" in turn_text(1)
    assert "[mode: plain]" in turn_text(2) and "setting changed" not in turn_text(2)          # sticky, no repeat
    assert "[mode: plain]" not in turn_text(3) and "plain language is now OFF" in turn_text(3)


def test_plain_language_can_start_on_from_config():
    agent, *_ = make_agent()
    assert agent.plain is False
    st = State(RECIPE)
    assert Agent(Config(plain_language=True), st, {}).plain is True


def test_talk_less_and_more_change_the_checkin_level():
    from coach.checkin import CheckIns
    streams = [FakeStream([text("Okay, I'll stay quieter. ")]) for _ in range(5)]
    agent, st, client, _ = make_agent(*streams)
    agent.checkins = CheckIns(st, agent, None, None, FakeSteps(st), level="normal")
    run(agent.turn("please talk less", [], Sink()))
    assert agent.checkins.level == "gentle"
    run(agent.turn("talk less", [], Sink()))
    assert agent.checkins.level == "off"
    run(agent.turn("talk less", [], Sink()))                                   # already off: nothing to change
    assert agent.checkins.level == "off"
    run(agent.turn("you can talk more", [], Sink()))
    assert agent.checkins.level == "gentle"
    last = client.requests[3]["messages"][-1]["content"][-1]["text"]
    assert "[setting changed:" in last and "check-ins: gentle" in last
    assert "setting changed" not in client.requests[2]["messages"][-1]["content"][-1]["text"]


def test_preferences_ignore_check_in_turns_and_unrelated_speech():
    from coach.checkin import CheckIns
    streams = [FakeStream([text("Fine, that sounds good. ")]), FakeStream([text("SKIP")])]
    agent, st, client, _ = make_agent(*streams)
    agent.checkins = CheckIns(st, agent, None, None, FakeSteps(st), level="normal")
    run(agent.turn("how do I cut the avocado, and how simple can it be?", [], Sink()))      # not a request
    run(agent.turn("[check-in: they said talk less simpler too fast]", [], Sink()))           # app text, never a preference
    assert agent.plain is False and agent.checkins.level == "normal"


def test_prompt_has_the_accessibility_rules_and_no_time_pressure():
    for must in ("[mode: plain]", "Where am I?", "on your left", "Never rush"):
        assert must in SYSTEM
    assert "quickly" not in SYSTEM.replace('no "quickly"', "")                # only appears as the thing to avoid


def test_backstop_says_where_the_object_is():
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    s1 = FakeStream([tool("a", "point_at", {"name": "lime"})], "tool_use")
    agent, st, *_ = make_agent(s1)
    st.dets = [_det("lime", 100, 400)]
    sink = Sink()
    run(agent.turn("show me the lime", [frame], sink))
    assert sink.sentences == ["Here's the lime, on your left."]
