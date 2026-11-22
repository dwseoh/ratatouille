"""Shared interfaces between the three of us. Code against THESE, not against each other's files.

Rules
- Frozen after K0. To change something here: post in the team channel and get a 👍 from whoever it affects.
- Every interface has a Fake at the bottom, so you can build and test your part alone.
- One writer per State field (see State). Everyone may read everything.
- Only touch State from the asyncio loop. Threads (YOLO, frame grabber) hand results back with
  loop.call_soon_threadsafe(...).

Who builds what (A = dw127, B = Hanjing, C = Chloe)
    A: ControlLinkAPI, FrameSource, AimerAPI                   (hardware + pointing)
    B: SpeechAPI, IntentRouterAPI, SessionAPI, detector -> State.dets, telemetry   (voice + YOLO)
    C: AgentAPI, StepsAPI, Recipe loading, check-ins, dashboard  (brain + dashboard)

Smoke test (should print a fake turn):  cd laptop && python -m coach.contracts
"""
from __future__ import annotations

import asyncio
import enum
import json
import re
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    import numpy as np
    Frame = np.ndarray          # BGR image, shape (h, w, 3), dtype uint8 (what OpenCV gives you)
else:
    Frame = object


# ═══════════════════════════════════════════════════════════════════════════════
# C8. Recipe  (owner C; loaded once at startup, read-only after that)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Step:
    short: str                  # <= ~15 chars, for the TFT and dashboard ("mash")
    goal: str                   # what this step achieves, one sentence
    how: list[str]              # the actions, in order. The agent explains these in its own words.
    tips: list[str]             # technique + safety tips ("claw grip")
    point: list[str]            # objects the laser shows when the step starts (in order)
    done_when: str              # what the camera should see when it's finished
    fallback: str               # canned line, spoken only if the API is down/slow
    image: str = ""             # optional "what it should look like" photo, relative to recipes/ (default: by convention)


@dataclass(frozen=True)
class Recipe:
    name: str
    objects: list[str]          # = YOLO classes = point_at names = keyword list. One list for all three.
    aliases: dict[str, list[str]]   # object -> other ways people say it ("lime": ["limes", "green one"])
    steps: list[Step]

    @classmethod
    def load(cls, path: str | Path) -> "Recipe":
        d = json.loads(Path(path).read_text())
        return cls(d["name"], d["objects"], d.get("aliases", {}), [Step(**s) for s in d["steps"]])

    def names(self) -> dict[str, str]:
        """Every spoken form -> canonical object name. {"limes": "lime", "green one": "lime", "lime": "lime", ...}"""
        out = {o.lower(): o for o in self.objects}
        for obj, alts in self.aliases.items():
            for a in alts:
                out[a.lower()] = obj
        return out


# ═══════════════════════════════════════════════════════════════════════════════
# Vision data (C2 parts)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Det:
    """One YOLO detection. Pixel coordinates in the camera frame."""
    name: str                   # a Recipe.objects name (or "hand")
    conf: float                 # 0..1
    box: tuple[float, float, float, float]   # x1, y1, x2, y2

    @property
    def center(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.box
        return ((x1 + x2) / 2, (y1 + y2) / 2)


EventSource = Literal["vision", "user", "agent", "system"]


@dataclass(frozen=True)
class Event:
    """C10. One line in the event log. Append-only: nobody edits or deletes events."""
    t: float                    # time.time()
    source: EventSource
    text: str


# ═══════════════════════════════════════════════════════════════════════════════
# C9. Latency record (owner B, but anyone with a timestamp calls mark())
# ═══════════════════════════════════════════════════════════════════════════════

# The 4 v1 marks (+ optional extras). Times are time.monotonic().
LAT_SPEECH_END = "speech_end"           # B: VAD says the user stopped
LAT_FIRST_TOKEN = "first_token"         # C: first text token from Claude
LAT_FIRST_AUDIO = "first_audio"         # B: first TTS audio played
LAT_POINT = "point_called"              # B/C: point_at() called for this turn
LAT_ON_TARGET = "on_target"             # A: dot on target (only if closed loop exists)


@dataclass
class TurnLatency:
    turn_id: int
    marks: dict[str, float] = field(default_factory=dict)

    def mark(self, key: str) -> None:
        self.marks.setdefault(key, time.monotonic())   # first mark wins

    def ms(self) -> dict[str, int]:
        """Milliseconds from speech_end to each other mark: {"first_token": 640, "first_audio": 910, ...}"""
        t0 = self.marks.get(LAT_SPEECH_END)
        if t0 is None:
            return {}
        return {k: round((v - t0) * 1000) for k, v in self.marks.items() if k != LAT_SPEECH_END}


# ═══════════════════════════════════════════════════════════════════════════════
# C2 + C13. State: the shared blackboard. ONE WRITER PER FIELD.
# ═══════════════════════════════════════════════════════════════════════════════

Session = Literal["idle", "active"]


@dataclass
class State:
    recipe: Recipe
    step: int = 0                           # writer: StepsAPI (C) only
    dets: list[Det] = field(default_factory=list)   # writer: YOLO detector (B)
    dot: tuple[float, float] | None = None  # writer: dot finder (A), None in open-loop v1
    target: str | None = None               # writer: Aimer (A)
    on_target: bool = False                 # writer: Aimer (A)
    transcript: str = ""                    # writer: voice (B), the current/last user utterance
    speaking: bool = False                  # writer: voice (B), True while TTS audio plays
    user_talking: bool = False              # writer: voice (B), True while VAD hears the user
    session: Session = "idle"               # writer: SessionAPI (B)
    events: list[Event] = field(default_factory=list)   # writer: anyone, via log() only
    turns: list[TurnLatency] = field(default_factory=list)  # writer: telemetry (B)
    last_activity: float = field(default_factory=time.monotonic)  # writer: voice (B), user or coach spoke
    shown_image: tuple[int, int] | None = None   # writer: show_image tool (C): (step whose picture to show, step it was asked on)

    def log(self, source: EventSource, text: str) -> None:
        self.events.append(Event(time.time(), source, text))

    @property
    def current_step(self) -> Step:
        return self.recipe.steps[self.step]

    @property
    def image_step(self) -> int:
        """Which step's picture the dashboard shows: the one the user asked for ("show me the onion step"), but only
        while the recipe is still on the step it was asked on; otherwise the current step."""
        if self.shown_image and self.shown_image[1] == self.step:
            return self.shown_image[0]
        return self.step

    def visible(self) -> set[str]:
        """Object names YOLO currently sees (smoothed by the detector)."""
        return {d.name for d in self.dets}

    def det(self, name: str) -> Det | None:
        """Best detection for an object name, or None if not visible."""
        hits = [d for d in self.dets if d.name == name]
        return max(hits, key=lambda d: d.conf) if hits else None

    def note(self) -> str:
        """Short state summary the agent sends with every turn."""
        s = self.current_step
        recent = "; ".join(e.text for e in self.events[-5:]) or "none"
        return (f"step {self.step + 1}/{len(self.recipe.steps)} ({s.short}): {s.goal} | "
                f"visible: {', '.join(sorted(self.visible())) or 'nothing'} | "
                f"laser: {self.target or 'off'} | recent: {recent}")


# ═══════════════════════════════════════════════════════════════════════════════
# C1 + C12. Control link to the S3 (owner A)
# ═══════════════════════════════════════════════════════════════════════════════

TFT_STATUS, TFT_IP, TFT_STEP, TFT_LATENCY, TFT_AGENT = 0, 1, 2, 3, 4   # display lines, 20 chars each


@runtime_checkable
class ControlLinkAPI(Protocol):
    pan: float                  # degrees. The Aimer writes these; the link sends them at 30 Hz.
    tilt: float
    laser: bool

    @property
    def online(self) -> bool: ...                   # S3 status seen in the last 1 s

    def show_text(self, line: int, text: str) -> None: ...   # TFT line 0-4, cut to 20 chars
    def reset(self) -> None: ...                    # send R: laser off, servos centred
    def on_button(self, cb: Callable[[], None]) -> None: ...  # cb() runs on each BOOT press (S3 sends "B")


# ═══════════════════════════════════════════════════════════════════════════════
# C4. Frames (owner A)
# ═══════════════════════════════════════════════════════════════════════════════

@runtime_checkable
class FrameSource(Protocol):
    connected: bool

    def latest(self) -> tuple[Frame | None, float]: ...     # newest frame + its time.time(), (None, 0) if none yet
    def recent(self, n: int) -> list[Frame]: ...           # last n history frames (~0.5 s apart), oldest first


# ═══════════════════════════════════════════════════════════════════════════════
# C3. Aimer (owner A). B's intent router and C's agent tools call it.
# ═══════════════════════════════════════════════════════════════════════════════

PointResult = Literal["ok", "not_visible", "unknown_object"]
LASER_IDLE_OFF_S = 30.0         # laser turns itself off after this long without a new point_at


@runtime_checkable
class AimerAPI(Protocol):
    target: str | None
    on_target: bool

    def point_at(self, name: str) -> PointResult: ...
    """Returns at once (doesn't wait for the servos). Not in State.dets -> "not_visible" and laser off.
    Not a recipe object -> "unknown_object". Writes State.target / State.on_target."""

    def laser_off(self) -> None: ...


# ═══════════════════════════════════════════════════════════════════════════════
# C5. Speech (owner B). C's agent + check-ins call say().
# ═══════════════════════════════════════════════════════════════════════════════

class Priority(enum.IntEnum):
    PROACTIVE = 0               # check-ins: held while the user talks / gets an answer, dropped after 3 s
    REPLY = 1                   # answers to the user
    SAFETY = 2                  # cuts the current audio and agent turn, spoken at once


@runtime_checkable
class SpeechAPI(Protocol):
    speaking: bool

    async def say(self, text: str, priority: Priority = Priority.REPLY) -> None: ...
    """Queue one sentence for TTS. Returns when it's queued, not when it's spoken."""

    def stop(self) -> None: ...
    """Barge-in: cut the current audio now and drop everything queued."""


# ═══════════════════════════════════════════════════════════════════════════════
# C14. Intent router (owner B). Local + fast (<50 ms). Runs in parallel with the agent, never before it.
# ═══════════════════════════════════════════════════════════════════════════════

class Intent(str, enum.Enum):
    POINT = "point"             # "where's the lime" -> obj = "lime"
    DONE = "done"               # "ok I finished", "what's next"
    REPEAT = "repeat"           # "say that again"
    STOP = "stop"               # "stop", "wait", "hold on" -> Speech.stop()
    START_OVER = "start_over"   # "start over" -> Session.reset()
    OTHER = "other"             # everything else: only the agent handles it


@dataclass(frozen=True)
class IntentResult:
    intent: Intent
    obj: str | None = None      # canonical object name for POINT
    score: float = 1.0          # 0..1 confidence (1.0 for exact keyword hits)


@runtime_checkable
class IntentRouterAPI(Protocol):
    def classify(self, text: str) -> IntentResult: ...   # works on partial (interim) transcripts too


# ═══════════════════════════════════════════════════════════════════════════════
# C6. Agent (owner C). Plain Python, knows nothing about Pipecat.
# ═══════════════════════════════════════════════════════════════════════════════

OnSentence = Callable[[str], Awaitable[None]]
TOOL_NAMES = ("point_at", "laser_off", "propose_done", "show_text")


@dataclass
class TurnStats:
    ttft_s: float | None = None     # request sent -> first text token
    total_s: float = 0.0
    sentences: int = 0
    tools: list[str] = field(default_factory=list)   # tool names called this turn
    cancelled: bool = False
    error: str | None = None        # set if the API failed and the fallback line was used


@runtime_checkable
class AgentAPI(Protocol):
    busy: bool                  # True while a turn is running (check-ins wait for False)

    async def turn(self, user_text: str, frames: list[Frame], on_sentence: OnSentence) -> TurnStats: ...
    """One turn. Calls `await on_sentence(s)` for each complete sentence as soon as it exists.
    user_text starting with "[check-in" = proactive check-in: may produce no sentences at all."""

    def cancel(self) -> None: ...   # barge-in: abort the running turn, no more on_sentence calls
    async def prewarm(self) -> None: ...   # warm the prompt cache at startup


# ═══════════════════════════════════════════════════════════════════════════════
# C7. Steps (owner C). The ONLY code that changes State.step.
# ═══════════════════════════════════════════════════════════════════════════════

DoneSource = Literal["user", "llm", "vision"]
Verdict = Literal["advance", "push_back", "ask", "finished"]


@runtime_checkable
class StepsAPI(Protocol):
    def propose_done(self, source: DoneSource, evidence: str = "") -> Verdict: ...
    """v1: user/llm -> "advance" (or "finished" after the last step). "vision" -> "ask" (wait for confirm())."""

    def confirm(self) -> Verdict: ...       # user said yes after an "ask"
    def reset(self) -> None: ...            # back to step 0
    def on_change(self, cb: Callable[[int], None]) -> None: ...   # cb(new_step) after every change


# ═══════════════════════════════════════════════════════════════════════════════
# C13. Session (owner B). idle -> BOOT press -> active. BOOT again / "start over" -> reset to idle.
# ═══════════════════════════════════════════════════════════════════════════════

@runtime_checkable
class SessionAPI(Protocol):
    async def start(self) -> None: ...      # State.session = "active", greet + say step 1 via the agent
    async def reset(self) -> None: ...      # Speech.stop, Agent.cancel, Aimer.laser_off, Steps.reset, idle


# ═══════════════════════════════════════════════════════════════════════════════
# FAKES: use these until the real thing exists. Same interface, no hardware, no API keys.
# ═══════════════════════════════════════════════════════════════════════════════

def fake_dets(names=("avocado", "lime", "bowl", "knife", "cutting board")) -> list[Det]:
    """A fixed set of detections spread over a 640x480 frame."""
    out = []
    for i, n in enumerate(names):
        x, y = 80 + (i % 4) * 140, 120 + (i // 4) * 180
        out.append(Det(n, 0.9, (x, y, x + 100, y + 90)))
    return out


class FakeLink:
    """Prints instead of talking UDP. Press Enter in fake_s3.py for the real-ish button test."""

    def __init__(self):
        self.pan, self.tilt, self.laser = 90.0, 60.0, False
        self._button_cbs: list[Callable[[], None]] = []

    @property
    def online(self) -> bool:
        return True

    def show_text(self, line: int, text: str) -> None:
        print(f"[TFT {line}] {text[:20]}")

    def reset(self) -> None:
        self.pan, self.tilt, self.laser = 90.0, 60.0, False
        print("[S3] reset")

    def on_button(self, cb: Callable[[], None]) -> None:
        self._button_cbs.append(cb)

    def press_button(self) -> None:         # call from a test to simulate a BOOT press
        for cb in self._button_cbs:
            cb()


class FakeFrames:
    """Black frames, or frames from a webcam/video if you pass a source (0 = default webcam)."""

    def __init__(self, source: int | str | None = None):
        import numpy as np
        self._np, self._cap, self.connected = np, None, True
        self._hist: deque = deque(maxlen=8)
        if source is not None:
            import cv2
            self._cap = cv2.VideoCapture(source)

    def latest(self):
        if self._cap is not None:
            ok, f = self._cap.read()
            if not ok:
                return None, 0.0
        else:
            f = self._np.zeros((480, 640, 3), dtype=self._np.uint8)
        t = time.time()
        self._hist.append((t, f))
        return f, t

    def recent(self, n: int):
        while len(self._hist) < min(n, self._hist.maxlen):
            if self.latest()[0] is None:
                break
        return [f for _, f in list(self._hist)[-n:]]


class FakeAimer:
    """Logs instead of moving servos, but follows the real rules (visible check, writes State)."""

    def __init__(self, state: State):
        self.state, self.target, self.on_target = state, None, False

    def point_at(self, name: str) -> PointResult:
        if name not in self.state.recipe.objects:
            return "unknown_object"
        if name not in self.state.visible():
            self.laser_off()
            return "not_visible"
        self.target = self.state.target = name
        self.on_target = self.state.on_target = True
        print(f"[laser] -> {name}")
        return "ok"

    def laser_off(self) -> None:
        if self.target:
            print("[laser] off")
        self.target = self.state.target = None
        self.on_target = self.state.on_target = False


class FakeSpeech:
    """Prints each sentence. say_aloud=True also speaks it with macOS `say` (one at a time)."""

    def __init__(self, say_aloud: bool = False):
        self.speaking = False
        self._aloud = say_aloud and shutil.which("say") is not None
        self._proc = None

    async def say(self, text: str, priority: Priority = Priority.REPLY) -> None:
        print(f"[coach:{priority.name.lower()}] {text}")
        if self._aloud:
            self.speaking = True
            self._proc = await asyncio.create_subprocess_exec("say", text)
            await self._proc.wait()
            self.speaking = False

    def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
        self.speaking = False


class FakeAgent:
    """Echo bot: lets B test the voice pipeline before C's agent exists."""

    def __init__(self):
        self._cancelled = False
        self.busy = False

    async def turn(self, user_text: str, frames: list, on_sentence: OnSentence) -> TurnStats:
        self._cancelled, self.busy = False, True
        try:
            return await self._turn(user_text, frames, on_sentence)
        finally:
            self.busy = False

    async def _turn(self, user_text: str, frames: list, on_sentence: OnSentence) -> TurnStats:
        t0 = time.monotonic()
        if user_text.startswith("[check-in"):
            return TurnStats(ttft_s=0.0, total_s=0.0)
        await asyncio.sleep(0.3)                     # pretend network time
        stats = TurnStats(ttft_s=time.monotonic() - t0)
        for s in (f"You said: {user_text.rstrip('.!?')}.", f"I can see {len(frames)} frames."):
            if self._cancelled:
                stats.cancelled = True
                break
            await on_sentence(s)
            stats.sentences += 1
        stats.total_s = time.monotonic() - t0
        return stats

    def cancel(self) -> None:
        self._cancelled = True

    async def prewarm(self) -> None:
        pass


class FakeSteps:
    """Always advances. Use until C's real step machine exists."""

    def __init__(self, state: State):
        self.state = state
        self._cbs: list[Callable[[int], None]] = []

    def propose_done(self, source: DoneSource, evidence: str = "") -> Verdict:
        if source == "vision":
            return "ask"
        return self.confirm()

    def confirm(self) -> Verdict:
        if self.state.step >= len(self.state.recipe.steps) - 1:
            return "finished"
        self.state.step += 1
        for cb in self._cbs:
            cb(self.state.step)
        return "advance"

    def reset(self) -> None:
        self.state.step = 0
        for cb in self._cbs:
            cb(0)

    def on_change(self, cb: Callable[[int], None]) -> None:
        self._cbs.append(cb)


class KeywordRouter:
    """Simplest IntentRouterAPI: exact words + aliases. B replaces it with the embedding version."""

    def __init__(self, recipe: Recipe):
        self._names = sorted(recipe.names().items(), key=lambda kv: -len(kv[0]))   # longest phrase first

    def classify(self, text: str) -> IntentResult:
        t = " " + " ".join(re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()) + " "   # no punctuation, single spaces
        if any(w in t for w in (" stop ", " wait ", " hold on ")):
            return IntentResult(Intent.STOP)
        if "start over" in t:
            return IntentResult(Intent.START_OVER)
        if any(w in t for w in ("again", "repeat")):
            return IntentResult(Intent.REPEAT)
        if any(w in t for w in (" done", "finished", "what's next", "what next", "next step")):
            return IntentResult(Intent.DONE)
        for phrase, obj in self._names:
            if f" {phrase} " in t:
                return IntentResult(Intent.POINT, obj)
        return IntentResult(Intent.OTHER, score=0.0)


# ═══════════════════════════════════════════════════════════════════════════════
# Smoke test: python -m coach.contracts   (from the laptop/ folder)
# ═══════════════════════════════════════════════════════════════════════════════

async def _demo() -> None:
    recipe = Recipe.load(Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json")
    st = State(recipe, dets=fake_dets())
    aimer, speech, agent, steps = FakeAimer(st), FakeSpeech(), FakeAgent(), FakeSteps(st)
    router, frames = KeywordRouter(recipe), FakeFrames()
    for name, impl, proto in [("link", FakeLink(), ControlLinkAPI), ("frames", frames, FrameSource),
                              ("aimer", aimer, AimerAPI), ("speech", speech, SpeechAPI),
                              ("agent", agent, AgentAPI), ("steps", steps, StepsAPI), ("router", router, IntentRouterAPI)]:
        assert isinstance(impl, proto), f"Fake{name} doesn't match its interface"

    for text in ("where's the green one", "ok I'm done", "how chunky should it be?"):
        r = router.classify(text)
        print(f"\nuser: {text!r} -> {r.intent.value} {r.obj or ''}")
        if r.intent is Intent.POINT:
            print("  point_at:", aimer.point_at(r.obj))
        elif r.intent is Intent.DONE:
            print("  steps:", steps.propose_done("user"), "->", st.current_step.short)
        stats = await agent.turn(text, frames.recent(3), speech.say)
        print(f"  ttft {stats.ttft_s:.2f}s, {stats.sentences} sentences")
    print("\nstate note:", st.note())
    print("point_at('salt') with salt not visible:", aimer.point_at("salt"))


if __name__ == "__main__":
    asyncio.run(_demo())
