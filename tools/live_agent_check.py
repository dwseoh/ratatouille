"""Live end-to-end check of the agent (B1) against the REAL Anthropic API. Costs a few cents.

    .venv/bin/python tools/live_agent_check.py

Needs ANTHROPIC_API_KEY in laptop/.env. Uses the model from CLAUDE_MODEL / config. Real photos from
tools/test_frames stand in for the webcam; the aimer, steps and TFT are the fakes from coach.contracts.
Prints PASS/FAIL per scenario and exits non-zero if any fail. Scenarios are independent agents, so one
failure doesn't hide the others.
"""
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "laptop"))

import anthropic  # noqa: E402
import cv2  # noqa: E402

from coach.agent import Agent, make_handlers  # noqa: E402
from coach.config import CONFIG as cfg  # noqa: E402
from coach.contracts import Det, FakeAimer, FakeLink, FakeSteps, Recipe, State, fake_dets  # noqa: E402

RECIPE = Recipe.load(ROOT / "laptop" / cfg.recipe_path)
FRAMES = {p.stem: cv2.imread(str(p)) for p in (ROOT / "tools" / "test_frames").glob("*.jpg")}
NEEDED = ("avocado_hold", "avocado_halves", "mash_bowl")
if missing := [n for n in NEEDED if n not in FRAMES]:
    sys.exit(f"Missing tools/test_frames/{', '.join(n + '.jpg' for n in missing)}. See tools/test_frames/README.md.")
ALL = tuple(RECIPE.objects)
results: list[tuple[str, bool, str]] = []


def build(visible=ALL, step=0, client=None, timers=None, session=None):
    st = State(RECIPE, dets=fake_dets(tuple(visible)), session="active")
    st.step = step
    aimer, steps, link = FakeAimer(st), FakeSteps(st), FakeLink()
    if timers is True:
        from coach.timers import Timers
        timers = Timers(st, link)
    agent = Agent(cfg, st, make_handlers(st, aimer, steps, link, timers=timers,
                                         session=(lambda: session_box[0]) if session else None), client=client)
    session_box = [None]
    if session:                                 # a real Session (pause/resume) on a do-nothing speech object
        from coach.session import Session

        class QuietSpeech:
            speaking = False

            async def say(self, text, priority=None):
                pass

            def stop(self):
                pass
        session_box[0] = Session(st, QuietSpeech(), agent, aimer, steps, link)
        st.session = "active"
    # These scenarios check BEHAVIOUR, so a slow API moment mustn't turn into a canned line and a false FAIL.
    # Latency fallbacks are checked on purpose by scenario 9 (it sets its own timeout).
    agent.first_token_timeout = 30
    usage = []                                  # real usage per request, to prove the cache is hit
    real = agent.client.beta.messages.stream

    def spy(**kw):
        cm = real(**kw)
        orig_final = None

        class Wrap:
            async def __aenter__(self):
                self.s = await cm.__aenter__()
                nonlocal orig_final
                orig_final = self.s.get_final_message

                async def final():
                    m = await orig_final()
                    usage.append(m.usage)
                    return m
                self.s.get_final_message = final
                return self.s

            async def __aexit__(self, *a):
                return await cm.__aexit__(*a)
        return Wrap()
    agent.client.beta.messages.stream = spy
    return agent, st, aimer, usage


class Sink:
    def __init__(self):
        self.sentences, self.t = [], []
        self.t0 = time.monotonic()

    async def __call__(self, s):
        self.sentences.append(s)
        self.t.append(time.monotonic() - self.t0)


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def frames(*names):
    return [FRAMES[n] for n in names]


async def s_cache():
    print("1. prewarm -> first real turn reads the cache")
    agent, st, _, usage = build()
    await agent.prewarm()
    stats = await agent.turn("Where do I start?", frames("avocado_hold"), (sink := Sink()))
    u = usage[0]
    check("first turn hit the warm cache", (u.cache_read_input_tokens or 0) > 0,
          f"cache_read={u.cache_read_input_tokens} cache_write={u.cache_creation_input_tokens}")
    check("got 1-3 spoken sentences", 1 <= len(sink.sentences) <= 3, " | ".join(sink.sentences))
    check("ttft recorded", stats.ttft_s is not None, f"{stats.ttft_s:.2f}s" if stats.ttft_s else "")


async def s_point_and_defer():
    print("2. pointing + tool result carried to the next turn")
    agent, st, aimer, usage = build()
    stats = await agent.turn("Where's the lime?", frames("avocado_hold"), Sink())
    check("point_at called and laser on lime", "point_at" in stats.tools and aimer.target == "lime",
          f"tools={stats.tools} target={aimer.target}")
    stats2 = await agent.turn("Thanks. And the knife?", frames("avocado_hold"), Sink())
    check("next turn (with deferred tool_result) accepted by the API", stats2.error is None and stats2.sentences >= 1,
          stats2.error or "")
    check("second point_at moved the laser", aimer.target == "knife", f"target={aimer.target}")


async def s_not_visible():
    print("3. object not in view")
    agent, st, aimer, _ = build(visible=("avocado", "knife", "bowl"))
    sink = Sink()
    stats = await agent.turn("Where's the lime?", frames("avocado_hold"), sink)
    said = " ".join(sink.sentences).lower()
    check("did not point at the invisible lime", aimer.target != "lime", f"target={aimer.target} tools={stats.tools}")
    check("said it can't see it", any(w in said for w in ("don't see", "can't see", "cannot see", "not seeing", "not visible", "don't spot", "can't spot", "don't have")),
          said[:120])


async def s_barge_in():
    print("4. barge-in mid-turn, then keep talking")
    agent, st, aimer, _ = build()
    sink = Sink()
    task = asyncio.create_task(agent.turn("Explain slowly how to halve and pit the avocado, step by step.",
                                          frames("avocado_hold"), sink))
    while not sink.sentences and not task.done():
        await asyncio.sleep(0.02)
    agent.cancel()
    stats = await task
    n = len(sink.sentences)
    await asyncio.sleep(0.5)
    check("cancelled flag set, nothing spoken after cancel", stats.cancelled and len(sink.sentences) == n and not agent.busy,
          f"{n} sentence(s) before cancel")
    stats2 = await agent.turn("Sorry, just tell me what to do first.", frames("avocado_hold"), Sink())
    check("next turn after a cancel works", stats2.error is None and stats2.sentences >= 1, stats2.error or "")


async def s_propose_done():
    print("5. propose_done on a finished step")
    agent, st, _, _ = build(step=2)          # step 3: mash
    sink = Sink()
    stats = await agent.turn("I think I'm done mashing, does it look right?", frames("mash_bowl"), sink)
    check("no error", stats.error is None, stats.error or "")
    check("called propose_done (and FakeSteps advanced)", "propose_done" in stats.tools and st.step == 3,
          f"tools={stats.tools} step={st.step} said={' | '.join(sink.sentences)[:100]}")


async def s_long_conversation():
    print("6. step changes + trimming over many turns (no history 400s)")
    agent, st, _, _ = build()
    qs = ["Where's the avocado?", "Point at the knife", "Is this cut right?", "Where's the bowl?", "Done halving",
          "What next?", "Where's the fork?", "Is it mashed enough?", "Point at the bowl", "Anything else?"]
    ok = True
    err = ""
    for i, q in enumerate(qs):
        if i == 5:
            st.step = 1                       # pretend the step advanced
        if i == 8:
            st.step = 2
        stats = await agent.turn(q, frames("avocado_halves"), Sink())
        if stats.error:
            ok, err = False, f"turn {i}: {stats.error}"
            break
    check("10 turns across 3 steps, all accepted", ok, err)
    check("history trimmed (<= current step + 6 exchanges)", len(agent._exchanges) <= 6 + 4, f"{len(agent._exchanges)} exchanges kept")


async def s_checkin():
    print("7. proactive check-in")
    agent, st, _, _ = build()
    sink = Sink()
    stats = await agent.turn("[check-in: user has been quiet for 20 s]", frames("avocado_hold"), sink)
    check("no error; says one line or nothing", stats.error is None and len(sink.sentences) <= 2,
          " | ".join(sink.sentences) or "(silent)")


async def s_fallback():
    print("8. API failure -> canned step line")
    bad = anthropic.AsyncAnthropic(api_key="sk-ant-invalid", max_retries=0, timeout=10)
    agent, st, _, _ = build(client=bad)
    await agent.prewarm()                                 # must not raise
    sink = Sink()
    stats = await agent.turn("Where do I start?", [], sink)
    check("error recorded, fallback line spoken", stats.error and sink.sentences == [RECIPE.steps[0].fallback],
          f"{stats.error[:50] if stats.error else None}")


async def s_timeout():
    print("9. first token too slow and no backup model -> canned line (timeout forced to 0.05 s)")
    from coach.config import Config
    agent, st, _, _ = build()
    agent.cfg = Config(model=cfg.model, fast=cfg.fast, effort=cfg.effort, backup_model="")     # retry is scenario 19
    agent.first_token_timeout = 0.05
    sink = Sink()
    stats = await agent.turn("Where do I start?", frames("avocado_hold"), sink)
    check("canned step line spoken, error says timeout", sink.sentences == [RECIPE.steps[0].fallback] and "timeout" in (stats.error or ""),
          f"{stats.error} | {sink.sentences}")
    agent.first_token_timeout = 15
    stats2 = await agent.turn("Where do I start?", frames("avocado_hold"), Sink())
    check("next turn works normally", stats2.error is None and stats2.sentences >= 1, stats2.error or "")


async def s_no_camera():
    print("10. camera offline: says so once")
    agent, st, _, _ = build()
    s1, s2 = Sink(), Sink()
    await agent.turn("Where do I start?", [], s1)
    await agent.turn("And then?", [], s2)
    first, second = " ".join(s1.sentences).lower(), " ".join(s2.sentences).lower()
    mention = ("camera", "can't see", "cannot see", "don't see", "no view", "not seeing")
    check("first reply mentions the camera is off", any(w in first for w in mention), first[:140])
    check("second reply does not repeat it", not any(w in second for w in ("camera",)), second[:140])


ASK = ["Where's the avocado?", "Point at the knife", "What next?", "Is this cut right?", "Where's the bowl?",
       "Show me the lime", "How do I hold the knife?", "Where was I?", "Point at the bowl", "Anything else?",
       "What should I do now?", "Which one is the onion?"]


async def s_always_speaks():
    print("11. every user turn gets a spoken answer (never tools alone)")
    agent, st, aimer, _ = build()
    silent = []
    for q in ASK:
        sink = Sink()
        await agent.turn(q, frames("avocado_halves"), sink)
        if not sink.sentences:
            silent.append(q)
    check(f"no silent turns out of {len(ASK)}", not silent, f"silent: {silent}" if silent else "")


async def s_no_key_drill():
    print("12. drill: no API credentials -> canned line, no crash")
    import os
    saved = {k: os.environ.pop(k, None) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    try:
        client = anthropic.AsyncAnthropic(max_retries=0, timeout=10)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    agent, st, _, _ = build(client=client)
    sink = Sink()
    stats = await agent.turn("Where do I start?", [], sink)
    if stats.error is None:
        print("  SKIP  credentials were found another way (e.g. `ant auth login`), nothing to drill")
        return
    check("canned line spoken, error says credentials", sink.sentences == [RECIPE.steps[0].fallback] and "credentials" in stats.error,
          f"{stats.error} | {sink.sentences}")


async def s_refusal_drill():
    print("13. drill: forced refusal -> canned line")
    from types import SimpleNamespace as NS

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def __aiter__(self):
            async def g():
                if False:
                    yield
            return g()

        async def get_final_message(self):
            return NS(stop_reason="refusal", content=[], usage=None)
    client = NS(beta=NS(messages=NS(stream=lambda **kw: Stream())))
    agent, st, _, _ = build(client=client)
    sink = Sink()
    stats = await agent.turn("Where do I start?", [], sink)
    check("canned line spoken, error=refusal, system event logged",
          sink.sentences == [RECIPE.steps[0].fallback] and stats.error == "refusal"
          and any("agent fallback" in e.text for e in st.events), f"{stats.error} | {sink.sentences}")


async def s_plain_language():
    print("14. plain-language mode: short sentences, no numbers")
    agent, st, _, _ = build()
    agent.plain = True
    sink = Sink()
    stats = await agent.turn("How do I cut the avocado?", frames("avocado_hold"), sink)
    words = [len(x.split()) for x in sink.sentences]
    digits = any(ch.isdigit() for x in sink.sentences for ch in x)
    meta = any(w in " ".join(sink.sentences).lower() for w in ("plain mode", "the user"))
    check("real answer (no API error), every sentence <= 14 words, no digits, no talk about the rules",
          stats.error is None and sink.sentences and max(words) <= 14 and not digits and not meta,
          f"error={stats.error} words={words} | {' | '.join(sink.sentences)}")


async def s_spatial():
    print("15. spatial wording: says where the object is")
    agent, st, aimer, _ = build()
    h, w = FRAMES["avocado_hold"].shape[:2]
    st.dets = [Det("lime", 0.9, (w * 0.10, h * 0.70, w * 0.20, h * 0.90)), Det("bowl", 0.9, (w * 0.80, h * 0.10, w * 0.95, h * 0.30)),
               Det("avocado", 0.9, (w * 0.45, h * 0.40, w * 0.55, h * 0.60))]
    sink = Sink()
    await agent.turn("Where's the lime?", frames("avocado_hold"), sink)
    said = " ".join(sink.sentences).lower()
    check("mentions left (lime is on the user's left)", "left" in said, said[:140])


async def s_where_am_i():
    print("16. 'where was I?' gives step + next action")
    agent, st, _, _ = build(step=2)
    sink = Sink()
    await agent.turn("Wait, where was I?", frames("mash_bowl"), sink)
    said = " ".join(sink.sentences).lower()
    check("names the step (3 / three / mash)", any(k in said for k in ("step 3", "step three", "third", "mash")), said[:160])


async def s_yes_advances():
    print("17. coach asked 'ready to move on?', user says yes -> propose_done")
    agent, st, _, _ = build(step=2)
    ask = "[check-in: the camera thinks step 3 (mash) is finished - it looks mashed. Ask the user in one short sentence if they're ready to move on.]"
    s1 = Sink()
    await agent.turn(ask, frames("mash_bowl"), s1)
    stats = await agent.turn("Yes, I'm ready.", frames("mash_bowl"), Sink())
    check("asked a question, then propose_done on the yes", bool(s1.sentences) and "propose_done" in stats.tools,
          f"asked={s1.sentences} tools={stats.tools}")


async def s_talk_less():
    print("18. 'talk less' lowers the check-in level and the coach acknowledges")
    from coach.checkin import CheckIns
    agent, st, _, _ = build()
    agent.checkins = CheckIns(st, agent, None, None, FakeSteps(st), level="normal")
    sink = Sink()
    await agent.turn("Please talk less.", frames("avocado_hold"), sink)
    check("level is now gentle and it said something", agent.checkins.level == "gentle" and bool(sink.sentences),
          f"level={agent.checkins.level} | {' | '.join(sink.sentences)}")


async def s_retry_on_haiku():
    print("19. Opus too slow (forced) -> one retry on Haiku gives a real answer")
    from coach.config import Config
    agent, st, aimer, usage = build()
    agent.cfg = Config(model=cfg.model, fast=cfg.fast, effort=cfg.effort, backup_model="claude-haiku-5-5", backup_timeout_s=15)
    await agent.prewarm()                                     # warms Opus AND Haiku
    agent.first_token_timeout = 0.05                          # Opus can't answer that fast
    sink = Sink()
    t0 = time.monotonic()
    stats = await agent.turn("Where's the lime?", frames("avocado_hold"), sink)
    dt = time.monotonic() - t0
    events = [e.text for e in st.events if e.source == "system"]
    check("real answer from Haiku, no error", stats.error is None and bool(sink.sentences)
          and sink.sentences != [RECIPE.steps[0].fallback], f"error={stats.error} | {' | '.join(sink.sentences)} | {dt:.2f}s")
    check("the retry + the answering model are logged", any("retrying on claude-haiku-5-5" in e for e in events)
          and any("answered by claude-haiku-5-5" in e for e in events), "; ".join(events))
    check("tool still ran on the retry (laser on lime)", "point_at" in stats.tools and aimer.target == "lime", f"tools={stats.tools}")
    cr = getattr(usage[-1], "cache_read_input_tokens", 0) if usage else 0
    check("Haiku's cache was warm (prewarmed at startup)", (cr or 0) > 0, f"cache_read={cr}")
    stats2 = await agent.turn("And the knife?", frames("avocado_hold"), Sink())
    check("conversation continues normally after a retry", stats2.error is None, stats2.error or "")


async def s_retry_also_slow():
    print("20. Opus AND the backup too slow -> canned line, exactly one retry")
    from coach.config import Config
    agent, st, _, _ = build()
    agent.cfg = Config(model=cfg.model, fast=cfg.fast, effort=cfg.effort, backup_model="claude-haiku-5-5", backup_timeout_s=0.05)
    agent.first_token_timeout = 0.05
    sink = Sink()
    stats = await agent.turn("Where do I start?", frames("avocado_hold"), sink)
    check("canned step line spoken, error=timeout", sink.sentences == [RECIPE.steps[0].fallback] and "timeout" in (stats.error or ""),
          f"{stats.error} | {sink.sentences}")


async def s_pushback_relay():
    print("21. user says done, router already pushed back -> the coach says WHY (not a generic line)")
    agent, st, _, _ = build()
    st.log("user", "said done -> push_back")
    st.log("system", "user said step 1 is done, watcher disagrees: I only see a person in a pink jacket in a lobby, "
                     "no avocado or cutting board in view yet.")
    sink = Sink()
    stats = await agent.turn("Okay. I'm done.", frames("avocado_hold"), sink)
    said = " ".join(sink.sentences).lower()
    check("explains the disagreement, doesn't just recite the step",
          stats.error is None and any(k in said for k in ("lobby", "pink", "don't see", "do not see", "no avocado", "can't see", "only see", "not yet", "hold on",
                                                    "finished", "still looks whole", "still whole", "didn't see", "couldn't see", "whole avocado"))
          and "you're on step 1" not in said, said[:200])


async def s_greeting():
    print("22. 'hey Remy' greeting: a real greeting + step 1, never silent, never a repeat")
    agent, st, _, _ = build()
    sink = Sink()
    stats = await agent.turn("[session start: greet briefly and explain step 1]", frames("avocado_hold"), sink)
    said = " ".join(sink.sentences)
    sink2 = Sink()
    await agent.turn("[session start: greet briefly and explain step 1]", frames("avocado_hold"), sink2)
    check("greets and explains step 1", stats.error is None and bool(sink.sentences) and len(said) > 20, said[:200])
    check("a second greeting never repeats the first word for word", sink2.sentences != sink.sentences, " | ".join(sink2.sentences)[:160])


async def s_ignore_camera():
    print("23. 'just ignore the camera' -> it stops mentioning the camera, for good")
    import numpy as np
    dark = np.zeros((480, 640, 3), dtype=np.uint8)               # a covered camera sees black
    agent, st, _, _ = build()
    s0 = Sink()
    await agent.turn("Where do I start?", [dark], s0)
    before = " ".join(s0.sentences).lower()
    s1 = Sink()
    await agent.turn("My camera is covered. Just ignore the camera.", [dark], s1)
    later = []
    for q in ("What's next?", "How should I cut it?", "Is that right?"):
        sink = Sink()
        await agent.turn(q, [dark], sink)
        later.append(" ".join(sink.sentences).lower())
    bad = [t for t in later if any(w in t for w in ("camera", "dark", "can't see", "cannot see", "cover", "black", "picture"))]
    check("replies after the instruction never mention the camera", agent.camera_ignored and not bad,
          f"ack={' '.join(s1.sentences)[:80]!r} | later={later[:1]} | bad={bad}")
    s2 = Sink()
    await agent.turn("Okay, you can use the camera again.", [dark], s2)
    check("it can be turned back on", agent.camera_ignored is False and bool(s2.sentences), " ".join(s2.sentences)[:100])


async def s_trust_user_done():
    print("24. user says done while the camera shows it ISN'T -> trust the user, advance")
    agent, st, _, _ = build(step=0)
    sink = Sink()
    stats = await agent.turn("Okay, I'm done with this step.", frames("avocado_hold"), sink)       # photo: a whole avocado
    check("calls propose_done and the step advances", "propose_done" in stats.tools and st.step == 1,
          f"tools={stats.tools} step={st.step} said={' | '.join(sink.sentences)[:100]}")
    check("no argument with the user", not any(w in " ".join(sink.sentences).lower() for w in ("still whole", "not done", "doesn't look", "isn't done")),
          " | ".join(sink.sentences)[:140])


async def s_go_back():
    print("25. 'go back a step' -> go_to_step, says where they are now")
    agent, st, _, _ = build(step=3)                                # step 4: dice onion
    sink = Sink()
    stats = await agent.turn("Wait, can we go back to the previous step?", frames("mash_bowl"), sink)
    said = " ".join(sink.sentences).lower()
    check("go_to_step called, now on step 3", "go_to_step" in stats.tools and st.step == 2, f"tools={stats.tools} step={st.step}")
    check("names the step it went back to", any(k in said for k in ("step 3", "step three", "mash")), said[:160])


async def s_remember():
    print("26. 'I don't have cilantro' -> remember, and it shows up in later turns")
    agent, st, _, _ = build(step=1)
    sink = Sink()
    stats = await agent.turn("By the way, I don't have any cilantro.", frames("avocado_halves"), sink)
    check("remember called and the note kept", "remember" in stats.tools and st.cook_notes, f"tools={stats.tools} notes={st.cook_notes}")
    st.step = 6                                                    # the cilantro step
    s2 = Sink()
    await agent.turn("What do I do now?", frames("mash_bowl"), s2)
    said = " ".join(s2.sentences).lower()
    check("on the cilantro step it adapts (skip / parsley / leave out)", any(k in said for k in ("skip", "parsley", "without", "leave", "no cilantro", "instead")),
          said[:160])


async def s_timer():
    print("27. 'set a timer for two minutes' -> set_timer(120)")
    agent, st, _, _ = build(timers=True)
    sink = Sink()
    stats = await agent.turn("Set a timer for two minutes for the onions please.", frames("avocado_hold"), sink)
    check("set_timer called, a 2 minute timer runs", "set_timer" in stats.tools and len(st.timers) == 1
          and 110 <= min(st.timers.values()) - time.monotonic() <= 121, f"tools={stats.tools} timers={st.timers}")
    check("confirms out loud", bool(sink.sentences), " | ".join(sink.sentences)[:120])


async def s_servings():
    print("28. 'for six people, how much onion?' -> amounts from the recipe (x3)")
    agent, st, _, _ = build(step=3)
    sink = Sink()
    stats = await agent.turn("I'm making this for six people. How much onion do I need?", frames("avocado_hold"), sink)
    said = " ".join(sink.sentences).lower()
    check("says three quarters of an onion", any(k in said for k in ("three quarter", "3/4", "three-quarter", "¾")), said[:160])
    check("set_servings recorded 6", st.servings == 6 or "set_servings" not in stats.tools, f"tools={stats.tools} servings={st.servings}")


async def s_pause_tool():
    print("29. 'someone's at the door' -> pause_session, and it says so")
    agent, st, aimer, _ = build(session=True)
    sink = Sink()
    stats = await agent.turn("Hang on, someone's at the door, I need to go get it.", frames("avocado_hold"), sink)
    check("paused through the tool, spoke first", "pause_session" in stats.tools and st.session == "paused" and bool(sink.sentences),
          f"tools={stats.tools} session={st.session} | {' | '.join(sink.sentences)[:100]}")


async def s_history_cached():
    print("30. the conversation itself is read from the cache on the next turn")
    agent, st, _, usage = build()
    await agent.prewarm()
    await agent.turn("Where do I start?", frames("avocado_hold"), Sink())
    await agent.turn("And after that?", frames("avocado_hold"), Sink())
    r1, r2 = (getattr(u, "cache_read_input_tokens", 0) or 0 for u in usage[:2])
    check("turn 2 reads more from the cache than turn 1 (history breakpoint)", r2 > r1, f"cache_read turn1={r1} turn2={r2}")


async def main():
    only = sys.argv[1:]            # optional: scenario names, e.g. `always_speaks`
    for scenario in (s_cache, s_point_and_defer, s_not_visible, s_barge_in, s_propose_done,
                     s_long_conversation, s_checkin, s_fallback, s_timeout, s_no_camera, s_always_speaks,
                     s_no_key_drill, s_refusal_drill, s_plain_language, s_spatial, s_where_am_i, s_yes_advances,
                     s_talk_less, s_retry_on_haiku, s_retry_also_slow, s_pushback_relay, s_greeting, s_ignore_camera, s_trust_user_done,
                     s_go_back, s_remember, s_timer, s_servings, s_pause_tool, s_history_cached):
        if only and scenario.__name__[2:] not in only:
            continue
        try:
            await scenario()
        except Exception as e:      # a crash is a failure of that scenario, not of the whole run
            check(f"{scenario.__name__} crashed", False, f"{type(e).__name__}: {e}")
    bad = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks passed (model={cfg.model}, effort={cfg.effort})")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    asyncio.run(main())
