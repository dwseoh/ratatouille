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
  PAUSE      -> cut the audio (interim), session.pause() (final): "pause", "give me a sec", "hold on a minute"
  RESUME     -> session.resume() if paused               (final; usually Session.heard() already resumed)
  REPEAT, OTHER -> nothing, the agent answers.
DONE and START_OVER wait for the final transcript because acting on half a sentence
("I'm done with the... how fine should the onion be?") is worse than 300 ms of delay there.
STOP, START_OVER and PAUSE utterances are not sent to the agent.

"Go back to step 2" / "previous step" / "skip this one" are never DONE: the agent's go_to_step tool handles them.
While the session is paused the router only acts on START_OVER and RESUME (no pointing, no step changes); safety
questions ("I cut myself") still reach the agent, see `allowed_while_paused`.
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
STOP_FILLER = {"a", "just", "please", "second", "sec", "minute", "moment", "up"}   # not counted: "hold on a second"
STOP_NEGATED = ("don't stop", "dont stop", "do not stop", "never stop", "can't wait", "cant wait",
                "don't pause", "dont pause", "do not pause")
START_OVER_PHRASES = ("start over", "restart", "start again", "begin again", "from the top",
                      "reset", "from scratch")
REPEAT_PHRASES = ("say that again", "repeat that", "repeat", "what did you say", "come again",
                  "one more time", "sorry what", "pardon", "say again", "didn't catch")
DONE_PHRASES = ("i'm done", "im done", "i am done", "i'm finished", "im finished", "finished",
                "all done", "that's done", "thats done", "what's next", "whats next", "what next",
                "next step", "ok what now", "okay what now", "what now", "done with that",
                "did that", "i've done", "ive done", "ready for the next")
QUESTION_STARTS = {"is", "are", "does", "do", "should", "how", "why", "can", "could", "will", "was"}
# PAUSE: the cook steps away. Checked before STOP ("pause" and "hold on a minute" are stop words too, for the echo
# gate). "Hold on a second" / "one sec" stay STOP: a moment's interruption, not a break.
PAUSE_PHRASES = ("pause", "give me a sec", "give me a second", "give me a minute", "give me a moment",
                 "give me a few minutes", "give me five", "hold on a minute", "hang on a minute", "take a break",
                 "need a break", "need a minute", "be right back", "brb", "back in a minute", "back in a sec",
                 "back in a second", "back in five", "take five", "pause for a bit")
PAUSE_NEGATED = ("don't pause", "dont pause", "do not pause", "no need to pause", "unpause")
PAUSE_MAX_WORDS = 12    # "ok give me a sec I need to wash my hands": yes. A long story that says "break": no.
RESUME_PHRASES = ("resume", "unpause", "i'm back", "im back", "i am back", "back again", "let's continue",
                  "lets continue", "continue", "carry on", "let's keep going", "lets keep going", "keep going",
                  "let's get back to it", "where were we", "ready to continue")
# Navigation is the agent's job (go_to_step). Never let it look like DONE ("previous step" ~ "next step").
NAV_PHRASES = ("go back", "previous step", "previous one", "the step before", "back to step", "go to step",
               "jump to", "skip this", "skip to", "skip ahead", "skip step", "skip the")
# While paused, these still get an answer: the cook may be hurt or something may be burning.
SAFETY_PHRASES = ("help", "emergency", "fire", "smoke", "burning", "cut myself", "cut my", "bleeding", "blood",
                  "burned myself", "burnt myself", "burned my", "burnt my", "i burned", "i burnt", "hurt myself",
                  "i'm hurt", "im hurt", "ouch", "ow", "first aid", "call 911", "ambulance")
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


def _stop_words(text: str) -> list[str]:
    """The words that count toward STOP_MAX_WORDS: filler ("a second", "please") dropped."""
    return [w for w in normalize(text).split() if w not in STOP_FILLER]


def is_stop(text: str) -> bool:
    """A stop word anywhere in `text`, not negated ("don't stop"). The caller limits the length."""
    t = normalize(text)
    for n in STOP_NEGATED:
        t = f" {t} ".replace(f" {n} ", " ").strip()
    return any(_has(t, w) for w in STOP_WORDS)


def is_pause(text: str) -> bool:
    """The cook wants a break: a pause phrase, not negated, not about a timer, in a short-ish utterance."""
    t = normalize(text)
    for n in PAUSE_NEGATED:
        t = f" {t} ".replace(f" {n} ", " ").strip()
    words = t.split()
    if not words or len(words) > PAUSE_MAX_WORDS or "timer" in t:
        return False
    return any(_has(t, p) for p in PAUSE_PHRASES)


def ends_with_pause(text: str, tail: int = 6) -> bool:
    """A pause phrase in the last `tail` words. For the echo gate: what the mic heard over the coach ends with it."""
    return is_pause(" ".join(normalize(text).split()[-tail:]))


def is_resume(text: str) -> bool:
    t = normalize(text)
    return any(_has(t, p) for p in RESUME_PHRASES)


def is_safety(text: str) -> bool:
    t = normalize(text)
    return any(_has(t, p) for p in SAFETY_PHRASES)


def allowed_while_paused(text: str) -> bool:
    """Speech that still gets through while paused, besides resume phrases: safety, and "start over"."""
    t = normalize(text)
    return is_safety(t) or any(_has(t, p) for p in START_OVER_PHRASES)


def is_short_stop(text: str) -> bool:
    """STOP as the router means it: a stop word in an utterance of at most STOP_MAX_WORDS real words."""
    return len(_stop_words(text)) <= STOP_MAX_WORDS and is_stop(text)


def ends_with_stop(text: str) -> bool:
    """A stop word in the last STOP_MAX_WORDS words. For the echo gate: while the coach talks, the mic
    hears its words too, so "...cut the avocado in half stop" is the user saying stop."""
    words = normalize(text).split()
    tail = []                                   # last STOP_MAX_WORDS real words, plus filler between them
    for w in reversed(words):
        if len([x for x in tail if x not in STOP_FILLER]) >= STOP_MAX_WORDS and w not in STOP_FILLER:
            break
        tail.insert(0, w)
    return is_stop(" ".join(tail))


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

        timer = _has(t, "timer") or _has(t, "timers")   # "stop the timer", "pause the timer": the agent's job
        if not timer and is_pause(t):
            return IntentResult(Intent.PAUSE)
        if not timer and is_short_stop(t):
            return IntentResult(Intent.STOP)
        if any(_has(t, p) for p in START_OVER_PHRASES):
            return IntentResult(Intent.START_OVER)
        if any(_has(t, p) for p in REPEAT_PHRASES):
            return IntentResult(Intent.REPEAT)
        if any(_has(t, p) for p in NAV_PHRASES):
            return IntentResult(Intent.OTHER, score=0.0)       # the agent's go_to_step does navigation
        if is_resume(t):                                       # before DONE: "I'm back, what's next?" mustn't skip a step
            return IntentResult(Intent.RESUME)
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
        if r.intent is Intent.PAUSE:
            r = IntentResult(Intent.STOP)       # cut the audio now; the pause itself waits for the final transcript
        if r.intent in (Intent.POINT, Intent.STOP):
            await self._act(r, text)

    async def on_final(self, text: str) -> bool:
        """Act on the finished utterance. Returns True if the agent should NOT get this turn."""
        r = self.router.classify(text)
        if self.state.session == "paused" and r.intent not in (Intent.START_OVER, Intent.RESUME):
            return False        # paused: no pointing or step changes (a safety question still reaches the agent)
        await self._act(r, text)
        return r.intent in (Intent.STOP, Intent.START_OVER, Intent.PAUSE)

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
            self.aimer.laser_off()               # "stop" means everything, not just the voice
            if self.speech:
                self.speech.stop()
        elif r.intent is Intent.DONE:
            verdict = self.steps.propose_done("user", evidence=text)
            self.state.log("user", f"said done -> {verdict}")
            if verdict in ("advance", "finished"):
                self.aimer.laser_off()           # the target belonged to the step that just ended
        elif r.intent is Intent.START_OVER:
            if self.session is not None:
                await self.session.reset()
            else:
                logger.warning("START_OVER heard but no SessionAPI wired yet")
        elif r.intent is Intent.PAUSE:
            if self.session is not None and hasattr(self.session, "pause"):
                await self.session.pause(f"heard {text!r}")
            else:                                # no session: at least go quiet
                self.aimer.laser_off()
                if self.speech:
                    self.speech.stop()
        elif r.intent is Intent.RESUME:
            if self.state.session == "paused" and self.session is not None and hasattr(self.session, "resume"):
                await self.session.resume(f"heard {text!r}")

    def take_point_time(self) -> float | None:
        t, self.point_t = self.point_t, None
        return t
