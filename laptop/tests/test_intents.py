"""Intent router tests. Run after every threshold or example change:  python -m pytest tests/test_intents.py

The phrase table uses the embedding model (downloaded once, ~80 MB, then cached).
"""
import asyncio
import time
from pathlib import Path

import pytest

from coach.contracts import FakeAimer, FakeSteps, Intent, IntentRouterAPI, Recipe, State, fake_dets
from coach.intents import IntentActions, IntentRouter, ends_with_stop, is_short_stop

RECIPE = Recipe.load(Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json")
P, D, R, S, X, O = Intent.POINT, Intent.DONE, Intent.REPEAT, Intent.STOP, Intent.START_OVER, Intent.OTHER

CASES = [
    # POINT: exact names, aliases, plurals, misspellings, paraphrases
    ("where's the lime", P, "lime"),
    ("where's the green one", P, "lime"),
    ("which one is the cilantro", P, "cilantro"),
    ("show me the knife", P, "knife"),
    ("point at the bowl", P, "bowl"),
    ("where did I put the salt", P, "salt"),
    ("I can't find the onion", P, "onion"),
    ("which knife", P, "knife"),
    ("grab the avocados", P, "avocado"),
    ("where are my avocadoes", P, "avocado"),
    ("where's the silantro", P, "cilantro"),
    ("can you show me the chopping board", P, "cutting board"),
    ("wait, where's the lime", P, "lime"),
    ("how much lime should I add", P, "lime"),          # point-first: naming a visible object shows it
    # real speech: filler, contractions, plurals after "where's", STT misspellings
    ("um where's the lime", P, "lime"),
    ("where'd the bowl go", P, "bowl"),
    ("find me the cilantro", P, "cilantro"),
    ("show me where the salt is", P, "salt"),
    ("where's the tomatoes", P, "tomato"),
    ("where's the avacado", P, "avocado"),
    ("where is the cutting bored", P, "cutting board"),
    ("where's the lime juice", P, "lime"),
    # DONE
    ("ok I'm done", D, None),
    ("finished", D, None),
    ("what's next", D, None),
    ("next step please", D, None),
    ("ok I did that", D, None),
    ("I've mashed it", D, None),
    ("all done", D, None),
    ("okay what now", D, None),
    ("got it what's next", D, None),
    ("alright I finished cutting", D, None),
    ("I think it's all chopped up now", D, None),
    ("ready to move on", D, None),
    # REPEAT
    ("say that again", R, None),
    ("sorry what", R, None),
    ("can you repeat that", R, None),
    ("I didn't catch that", R, None),
    ("what did you say", R, None),
    ("pardon", R, None),
    ("one more time", R, None),
    ("could you say that again please", R, None),
    # STOP: strict
    ("stop", S, None),
    ("wait", S, None),
    ("hold on", S, None),
    ("ok stop", S, None),
    ("shh", S, None),
    ("pause", Intent.PAUSE, None),                       # pauses the session now (cuts the audio too)
    ("one sec", S, None),
    ("wait wait", S, None),
    ("stop talking", S, None),
    ("hold on a second", S, None),                       # filler doesn't count toward STOP_MAX_WORDS
    ("wait just a minute please", S, None),
    # START_OVER
    ("start over", X, None),
    ("let's start over", X, None),
    ("can we restart", X, None),
    ("start from scratch", X, None),
    ("from the top", X, None),
    # OTHER
    ("how chunky should it be", O, None),
    ("is this ripe", O, None),
    ("is it finished", O, None),
    ("does this look right", O, None),
    ("why do we add salt at the end", P, "salt"),        # names salt -> laser shows it, agent answers
    ("what are we making today", O, None),
    ("thanks", O, None),
    ("how long do I mash it", O, None),
    ("what's the next ingredient after this", O, None),  # "next" inside a question: not DONE
    ("this is the best guacamole", O, None),
    ("don't stop", O, None),                             # negated: never cut the coach off
    ("I can't wait to eat this", O, None),
    ("I'm going to stop by the store later to get more stuff", O, None),  # long: not STOP
]


@pytest.fixture(scope="module")
def router():
    return IntentRouter(RECIPE)


@pytest.mark.parametrize("text,intent,obj", CASES, ids=[c[0] for c in CASES])
def test_phrases(router, text, intent, obj):
    r = router.classify(text)
    assert (r.intent, r.obj) == (intent, obj), f"{text!r} -> {r}"


def test_is_router_api(router):
    assert isinstance(router, IntentRouterAPI)


def test_fast_enough(router):
    texts = [f"{c[0]} {i}" for i, c in enumerate(CASES)]      # defeat the cache
    t0 = time.perf_counter()
    for t in texts:
        router.classify(t)
    per_call_ms = (time.perf_counter() - t0) * 1000 / len(texts)
    assert per_call_ms < 50, f"{per_call_ms:.1f} ms per call"


def test_keywords_and_fuzzy_without_model():
    r = IntentRouter(RECIPE, use_embeddings=False)
    assert (r.classify("where's the lyme").intent, r.classify("where are the avocadoes").obj) == (O, "avocado")
    assert r.classify("for the onion").obj == "onion"            # "for" is not fuzzy-matched to "fork"


# -- actions ----------------------------------------------------------------------------------------

class SpySpeech:
    def __init__(self):
        self.stops, self.speaking = 0, False

    async def say(self, text, priority=None):
        pass

    def stop(self):
        self.stops += 1


class SpySession:
    def __init__(self):
        self.resets = 0

    async def start(self):
        pass

    async def reset(self):
        self.resets += 1


def make_actions(router, visible=("avocado", "lime", "bowl", "knife", "cutting board")):
    state = State(RECIPE, dets=fake_dets(visible), session="active")
    a = IntentActions(router, state, FakeAimer(state), FakeSteps(state), SpySession())
    a.speech = SpySpeech()
    return a, state


def test_point_on_interim_once_per_utterance(router):
    a, state = make_actions(router)
    for partial in ("where's", "where's the li", "where's the lime", "where's the lime please"):
        asyncio.run(a.on_interim(partial))
    assert state.target == "lime" and a.take_point_time() is not None
    a.aimer.laser_off()
    asyncio.run(a.on_interim("where's the lime please now"))
    assert state.target is None                            # debounced: no second point_at this utterance
    a.new_utterance()
    asyncio.run(a.on_interim("the lime again"))
    assert state.target == "lime"


def test_point_ignored_when_not_visible(router):
    a, state = make_actions(router, visible=("avocado",))
    asyncio.run(a.on_interim("where's the salt"))
    assert state.target is None


def test_done_only_on_final(router):
    a, state = make_actions(router)
    asyncio.run(a.on_interim("ok I'm done"))
    assert state.step == 0
    skip = asyncio.run(a.on_final("ok I'm done"))
    assert state.step == 1 and not skip                    # the agent still answers (explains step 2)


def test_stop_and_start_over_skip_the_agent(router):
    a, _ = make_actions(router)
    asyncio.run(a.on_interim("stop"))
    assert a.speech.stops == 1
    a.new_utterance()
    assert asyncio.run(a.on_final("start over")) is True
    assert a.session.resets == 1


def test_stop_turns_the_laser_off(router):
    a, state = make_actions(router)
    asyncio.run(a.on_interim("where's the lime"))
    assert state.target == "lime"
    a.new_utterance()
    asyncio.run(a.on_interim("stop"))
    assert state.target is None and a.speech.stops == 1


def test_done_that_advances_turns_the_laser_off(router):
    a, state = make_actions(router)
    asyncio.run(a.on_interim("where's the lime"))
    a.new_utterance()
    asyncio.run(a.on_final("ok I'm done"))
    assert state.step == 1 and state.target is None


@pytest.mark.parametrize("text,stop", [
    ("hold on a second", True), ("wait, where's the lime", False), ("don't stop", False),
    ("no wait", True), ("I'm going to stop by the store later", False),
])
def test_short_stop(text, stop):
    assert is_short_stop(text) is stop


@pytest.mark.parametrize("heard,stop", [
    ("cut the avocado in half stop", True),               # coach's words + the user's "stop"
    ("cut the avocado in half hold on a second", True),
    ("push the knife in until it hits the pit", False),
    ("rotate it don't stop", False),
])
def test_echo_gate_tail(heard, stop):
    assert ends_with_stop(heard) is stop
