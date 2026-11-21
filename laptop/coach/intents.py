"""Intent router (B, V4b): the coach's reflexes. Local and fast, runs on interim transcripts.

`IntentRouter.classify(text)` implements IntentRouterAPI (contracts C14). Cheapest check first,
stop at the first confident answer:

  1. exact phrases + recipe aliases                     (~0 ms)   "where's the green one" -> lime
  2. rapidfuzz on object names for STT misspellings     (~1 ms)   "avocadoes", "silantro"
  3. sentence-transformers all-MiniLM-L6-v2 paraphrases (~6 ms)   "that bit's all chopped up" -> DONE

STOP is deliberately strict (exact words in a short utterance only): a false STOP kills a good answer.
For POINT the object always comes from step 1 or 2. No object named -> never POINT.

`IntentActions` turns intents into actions, once per (intent, object) per utterance:
  POINT      -> aimer.point_at(obj) if YOLO sees it      (interim: the laser moves while you talk)
  STOP       -> speech.stop()                            (interim)
  DONE       -> steps.propose_done("user")               (final transcript only)
  START_OVER -> session.reset()                          (final transcript only)
  REPEAT, OTHER -> nothing, the agent answers.
DONE and START_OVER wait for the final transcript because acting on half a sentence
("I'm done with the... how fine should the onion be?") is worse than 300 ms of delay there.
STOP and START_OVER utterances are not sent to the agent.
"""
from __future__ import annotations

import re
import time
from functools import lru_cache

from loguru import logger
from rapidfuzz import fuzz

from .contracts import AimerAPI, Intent, IntentResult, Recipe, SpeechAPI, State, StepsAPI

EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
FUZZY_MIN = 85          # rapidfuzz ratio to accept a misspelled object name
FUZZY_MIN_LEN = 5       # shorter names are exact-only ("fork" vs "for" scores 86)
EMBED_MARGIN = 0.05     # best intent must beat the runner-up by this much
EMBED_MIN = {           # per-intent cosine threshold; START_OVER resets the session, so it's strictest
    Intent.POINT: 0.55, Intent.DONE: 0.6, Intent.REPEAT: 0.6, Intent.START_OVER: 0.75,
}
STOP_MAX_WORDS = 3      # "stop", "ok stop", "wait wait": yes. "wait, where's the lime": no.

STOP_WORDS = {"stop", "wait", "hold on", "shh", "shush", "quiet", "pause", "one sec", "one second"}
START_OVER_PHRASES = ("start over", "restart", "start again", "begin again", "from the top",
                      "reset", "from scratch")
REPEAT_PHRASES = ("say that again", "repeat that", "repeat", "what did you say", "come again",
                  "one more time", "sorry what", "pardon", "say again", "didn't catch")
DONE_PHRASES = ("i'm done", "im done", "i am done", "i'm finished", "im finished", "finished",
                "all done", "that's done", "thats done", "what's next", "whats next", "what next",
                "next step", "ok what now", "okay what now", "what now", "done with that",
                "did that", "i've done", "ive done", "ready for the next")
QUESTION_STARTS = {"is", "are", "does", "do", "should", "how", "why", "can", "could", "will", "was"}
POINT_CUES = ("where", "which", "show me", "point", "find", "grab", "get the", "can't see", "cant see")

# Paraphrase examples for the embedding fallback. Grow these from real test sessions.
EXAMPLES: dict[Intent, list[str]] = {
    Intent.POINT: [
        "where's the lime", "which one is the cilantro", "show me the knife", "point at the bowl",
        "where did I put the salt", "I can't find the onion", "which knife should I use",
        "where is it", "which one", "can you show me where that is",
    ],
    Intent.DONE: [
        "ok I'm done", "finished", "what's next", "next step", "I did that", "that's done",
        "ok what now", "all cut", "I've mashed it", "I think that's good, what's the next step",
        "it's all chopped up", "I've added it", "that part is finished", "ready to move on",
    ],
    Intent.REPEAT: [
        "say that again", "repeat that", "what did you say", "sorry what", "come again",
        "one more time", "I didn't hear you", "can you repeat the last step", "huh, what was that",
    ],
    Intent.START_OVER: [
        "start over", "restart", "let's begin again", "reset", "let's start from the beginning",
        "start the recipe from scratch",
    ],
    Intent.OTHER: [
        "how chunky should it be", "is this ripe", "how much salt", "why do we add lime",
        "does this look right", "how long does this take", "can I use a spoon instead",
        "what does dice mean", "is it too soft", "how do I hold the knife", "thanks",
        "that smells good", "what are we making",
    ],
}


def normalize(text: str) -> str:
    """Lowercase, no punctuation except apostrophes, single spaces."""
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", text.lower().replace("’", "'")).split())


def _has(t: str, phrase: str) -> bool:
    return f" {phrase} " in f" {t} "


def is_stop(text: str) -> bool:
    """A stop word anywhere in `text` (the caller limits the length: STOP_MAX_WORDS)."""
    t = normalize(text)
    return any(_has(t, w) for w in STOP_WORDS)


def ends_with_stop(text: str) -> bool:
    """A stop word in the last STOP_MAX_WORDS words. For the echo gate: while the coach talks, the mic
    hears its words too, so "...cut the avocado in half stop" is the user saying stop."""
    return is_stop(" ".join(normalize(text).split()[-STOP_MAX_WORDS:]))


class IntentRouter:
    """IntentRouterAPI. `use_embeddings=False` skips the model (tests, or if it won't load)."""

    def __init__(self, recipe: Recipe, *, use_embeddings: bool = True, device: str = "cpu"):
        self._names = sorted(recipe.names().items(), key=lambda kv: -len(kv[0]))   # longest first
        self._fuzzy = [(n, o) for n, o in self._names if len(n) >= FUZZY_MIN_LEN]
        self._model = self._ex_vecs = None
        if use_embeddings:
            try:
                self._load_embeddings(device)
            except Exception:   # no network on first run, etc.: keywords + fuzzy still work
                logger.exception("intent embeddings unavailable, using keywords + fuzzy only")
        self.classify = lru_cache(maxsize=512)(self._classify)    # interims repeat a lot

    def _load_embeddings(self, device: str):
        from sentence_transformers import SentenceTransformer
        t0 = time.monotonic()
        self._model = SentenceTransformer(EMBED_MODEL, device=device)
        self._ex_intents = [i for i, exs in EXAMPLES.items() for _ in exs]
        self._ex_vecs = self._model.encode([e for exs in EXAMPLES.values() for e in exs],
                                           normalize_embeddings=True)
        logger.info(f"intent embeddings ready in {time.monotonic() - t0:.1f}s")

    # -- the router -------------------------------------------------------------------------------

    def _classify(self, text: str) -> IntentResult:
        t = normalize(text)
        if not t:
            return IntentResult(Intent.OTHER, score=0.0)
        words = t.split()

        if len(words) <= STOP_MAX_WORDS and is_stop(t):
            return IntentResult(Intent.STOP)
        if any(_has(t, p) for p in START_OVER_PHRASES):
            return IntentResult(Intent.START_OVER)
        if any(_has(t, p) for p in REPEAT_PHRASES):
            return IntentResult(Intent.REPEAT)
        # "is it finished?" is a question about the food, not "I'm done".
        if words[0] not in QUESTION_STARTS and any(_has(t, p) for p in DONE_PHRASES):
            return IntentResult(Intent.DONE)

        obj, obj_score = self.find_object(t)
        if obj and any(_has(t, c) or t.startswith(c) for c in POINT_CUES):
            return IntentResult(Intent.POINT, obj, obj_score)

        if self._model is not None:
            r = self._embed_classify(t)
            if r.intent is Intent.POINT:
                return IntentResult(Intent.POINT, obj, r.score) if obj else IntentResult(Intent.OTHER, score=0.0)
            if r.intent is Intent.DONE and words[0] in QUESTION_STARTS:
                r = IntentResult(Intent.OTHER, score=0.0)       # "is it finished?" asks, doesn't report
            if r.intent is not Intent.OTHER:
                return r
        # Point-first: naming a visible object is enough to show it ("how much lime?" lights the lime).
        if obj:
            return IntentResult(Intent.POINT, obj, obj_score * 0.8)
        return IntentResult(Intent.OTHER, score=0.0)

    def find_object(self, t: str) -> tuple[str | None, float]:
        """Canonical object named in normalized text t: exact phrase/alias first, then fuzzy."""
        for phrase, obj in self._names:
            if _has(t, phrase):
                return obj, 1.0
        words = t.split()
        best, best_score = None, 0.0
        for phrase, obj in self._fuzzy:
            n = len(phrase.split())
            for i in range(len(words) - n + 1):
                s = fuzz.ratio(" ".join(words[i:i + n]), phrase)
                if s >= FUZZY_MIN and s > best_score:
                    best, best_score = obj, s
        return best, best_score / 100

    def _embed_classify(self, t: str) -> IntentResult:
        v = self._model.encode([t], normalize_embeddings=True)[0]
        sims = self._ex_vecs @ v
        best: dict[Intent, float] = {}
        for intent, s in zip(self._ex_intents, sims):
            best[intent] = max(best.get(intent, -1.0), float(s))
        ranked = sorted(best.items(), key=lambda kv: -kv[1])
        (top, s1), (_, s2) = ranked[0], ranked[1]
        if top is Intent.OTHER or s1 < EMBED_MIN[top] or s1 - s2 < EMBED_MARGIN:
            return IntentResult(Intent.OTHER, score=s1 if top is Intent.OTHER else 0.0)
        return IntentResult(top, score=s1)


class IntentActions:
    """Runs the router on the live transcript and does the fast actions. Owned by AgentProcessor.

    `speech` is attached by voice.build_pipeline (the Speech object only exists once the pipeline does).
    `session` is optional until SessionAPI exists.
    """

    def __init__(self, router, state: State, aimer: AimerAPI, steps: StepsAPI, session=None):
        self.router, self.state, self.aimer, self.steps, self.session = router, state, aimer, steps, session
        self.speech: SpeechAPI | None = None
        self.point_t: float | None = None     # time.monotonic() of the last point_at, for telemetry
        self._acted: set[tuple[Intent, str | None]] = set()

    def new_utterance(self) -> None:
        self._acted.clear()

    async def on_interim(self, text: str) -> None:
        r = self.router.classify(text)
        if r.intent in (Intent.POINT, Intent.STOP):
            await self._act(r, text)

    async def on_final(self, text: str) -> bool:
        """Act on the finished utterance. Returns True if the agent should NOT get this turn."""
        r = self.router.classify(text)
        await self._act(r, text)
        return r.intent in (Intent.STOP, Intent.START_OVER)

    async def _act(self, r: IntentResult, text: str) -> None:
        key = (r.intent, r.obj)
        if r.intent is Intent.OTHER or key in self._acted:
            return
        if r.intent is Intent.POINT:
            # Not visible -> do nothing: the agent will say "I don't see it". Already there -> nothing.
            if r.obj not in self.state.visible() or self.aimer.target == r.obj:
                return
            self._acted.add(key)
            self.point_t = time.monotonic()
            result = self.aimer.point_at(r.obj)
            logger.info(f"intent POINT {r.obj} ({r.score:.2f}) from {text!r} -> {result}")
            return
        self._acted.add(key)
        logger.info(f"intent {r.intent.name} ({r.score:.2f}) from {text!r}")
        if r.intent is Intent.STOP:
            if self.speech:
                self.speech.stop()
        elif r.intent is Intent.DONE:
            verdict = self.steps.propose_done("user", evidence=text)
            self.state.log("user", f"said done -> {verdict}")
        elif r.intent is Intent.START_OVER:
            if self.session is not None:
                await self.session.reset()
            else:
                logger.warning("START_OVER heard but no SessionAPI wired yet")

    def take_point_time(self) -> float | None:
        t, self.point_t = self.point_t, None
        return t
