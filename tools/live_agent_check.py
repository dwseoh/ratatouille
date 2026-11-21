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
from coach.contracts import FakeAimer, FakeLink, FakeSteps, Recipe, State, fake_dets  # noqa: E402

RECIPE = Recipe.load(ROOT / "laptop" / cfg.recipe_path)
FRAMES = {p.stem: cv2.imread(str(p)) for p in (ROOT / "tools" / "test_frames").glob("*.jpg")}
ALL = tuple(RECIPE.objects)
results: list[tuple[str, bool, str]] = []


def build(visible=ALL, step=0, client=None):
    st = State(RECIPE, dets=fake_dets(tuple(visible)))
    st.step = step
    aimer, steps, link = FakeAimer(st), FakeSteps(st), FakeLink()
    agent = Agent(cfg, st, make_handlers(st, aimer, steps, link), client=client)
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
    print("9. first token too slow -> canned line (timeout forced to 0.05 s)")
    agent, st, _, _ = build()
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


async def main():
    only = sys.argv[1:]            # optional: scenario names, e.g. `always_speaks`
    for scenario in (s_cache, s_point_and_defer, s_not_visible, s_barge_in, s_propose_done,
                     s_long_conversation, s_checkin, s_fallback, s_timeout, s_no_camera, s_always_speaks):
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
