"""Step machine tests: talk through all 7 steps, incl. push-back, vision-asks-first, double calls, reset."""
from pathlib import Path

from coach.contracts import TFT_STEP, FakeLink, Recipe, State, StepsAPI
from coach.steps import DEBOUNCE_S, StepVerdict, Steps

RECIPE = Recipe.load(Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json")
N = len(RECIPE.steps)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def wait(self, s=DEBOUNCE_S + 1):         # time passes between separate utterances
        self.t += s


class Link(FakeLink):
    def __init__(self):
        super().__init__()
        self.shown = []

    def show_text(self, line, text):
        self.shown.append((line, text))


def make(provider=None, step=0):
    st = State(RECIPE)
    st.step = step
    clk, link = Clock(), Link()
    steps = Steps(st, link, provider, clock=clk)
    changes = []
    steps.on_change(changes.append)
    return steps, st, clk, link, changes


def say_done(steps, clk, source="user"):
    clk.wait()
    return steps.propose_done(source)


def test_implements_stepsapi():
    assert isinstance(Steps(State(RECIPE)), StepsAPI)


def test_talk_through_all_steps():
    steps, st, clk, link, changes = make()
    for i in range(N - 1):
        assert st.step == i
        assert say_done(steps, clk) == "advance"
    assert st.step == N - 1 and not steps.finished
    assert say_done(steps, clk) == "finished"
    assert st.step == N - 1 and steps.finished           # stays on the last step
    assert say_done(steps, clk) == "finished"
    assert changes == list(range(1, N))                  # on_change fired once per advance
    assert link.shown[0] == (TFT_STEP, f"Step 2: {RECIPE.steps[1].short}")
    assert link.shown[-1] == (TFT_STEP, "All done!")
    assert sum("step" in e.text and "done" in e.text for e in st.events) >= N


def test_pushback_once_then_advance_when_user_insists():
    verdict = StepVerdict(done=False, confidence=0.9, note="I still see whole avocados")
    steps, st, clk, link, changes = make(lambda step: verdict)
    assert say_done(steps, clk) == "push_back"
    assert st.step == 0 and steps.reason == "I still see whole avocados" and changes == []
    assert say_done(steps, clk) == "advance"             # user insists
    assert st.step == 1 and steps.reason == ""


def test_pushback_is_once_per_step():
    verdict = StepVerdict(False, 0.9, "not yet")
    steps, st, clk, *_ = make(lambda step: verdict)
    assert say_done(steps, clk) == "push_back"
    assert say_done(steps, clk) == "advance"
    assert say_done(steps, clk) == "push_back"           # a new step gets its own push-back
    assert say_done(steps, clk) == "advance"
    assert st.step == 2


def test_llm_proposal_follows_the_same_rules():
    steps, st, clk, *_ = make(lambda step: StepVerdict(False, 0.9, "nope"))
    assert say_done(steps, clk, "llm") == "push_back"
    assert say_done(steps, clk, "llm") == "advance"


def test_no_blocking_when_watcher_is_missing_unsure_or_agrees():
    for provider in (None, lambda s: None, lambda s: StepVerdict(False, 0.3, "maybe not"),
                     lambda s: StepVerdict(True, 0.95, "looks done")):
        steps, st, clk, *_ = make(provider)
        assert say_done(steps, clk) == "advance" and st.step == 1


def test_provider_gets_the_current_step_index():
    seen = []
    steps, st, clk, *_ = make(lambda step: seen.append(step), step=3)
    say_done(steps, clk)
    assert seen == [3]


def test_vision_asks_first_then_confirm_advances():
    steps, st, clk, link, changes = make(step=2)
    assert steps.propose_done("vision", "mashed texture") == "ask"
    assert st.step == 2 and changes == []                 # never advances on its own
    assert steps.propose_done("vision") == "ask"          # repeated sightings don't spam
    assert sum("asking the user" in e.text for e in st.events) == 1
    assert steps.confirm() == "advance"
    assert st.step == 3 and changes == [3]
    assert steps.confirm() == "push_back" and st.step == 3   # a second, stray yes does nothing


def test_confirm_without_a_pending_ask_does_nothing():
    steps, st, *_ = make()
    assert steps.confirm() == "push_back" and st.step == 0


def test_user_saying_done_after_a_vision_ask_advances_and_clears_it():
    steps, st, clk, *_ = make()
    steps.propose_done("vision")
    assert say_done(steps, clk) == "advance" and st.step == 1
    assert steps.confirm() == "push_back"                # the ask is gone


def test_vision_ask_is_dropped_when_the_step_changes():
    steps, st, clk, *_ = make()
    steps.propose_done("vision")
    say_done(steps, clk)                                  # user moves on without answering
    assert steps.confirm() == "push_back" and st.step == 1


def test_vision_ask_on_last_step_finishes_on_confirm():
    steps, st, *_ = make(step=N - 1)
    assert steps.propose_done("vision") == "ask"
    assert steps.confirm() == "finished" and steps.finished


def test_one_utterance_seen_twice_advances_only_once():
    """Intent router and agent tool both call propose_done for the same 'I'm done'."""
    steps, st, clk, link, changes = make()
    clk.wait()
    assert steps.propose_done("user") == "advance"
    clk.t += 0.8
    assert steps.propose_done("llm") == "advance"
    assert st.step == 1 and changes == [1]


def test_pushback_is_not_undone_by_the_second_call_for_the_same_utterance():
    steps, st, clk, *_ = make(lambda s: StepVerdict(False, 0.9, "not yet"))
    clk.wait()
    assert steps.propose_done("user") == "push_back"
    clk.t += 0.8
    assert steps.propose_done("llm") == "push_back"       # same utterance, same answer
    assert st.step == 0
    clk.wait()
    assert steps.propose_done("user") == "advance"        # a genuine second attempt


def test_reset_mid_recipe_clears_everything():
    steps, st, clk, link, changes = make(lambda s: StepVerdict(False, 0.9, "not yet"))
    say_done(steps, clk)                                  # push_back
    say_done(steps, clk)                                  # advance -> step 1
    steps.propose_done("vision")
    steps.reset()
    assert st.step == 0 and not steps.finished and steps.reason == ""
    assert changes == [1, 0] and link.shown[-1] == (TFT_STEP, f"Step 1: {RECIPE.steps[0].short}")
    assert steps.confirm() == "push_back"                 # pending ask cleared
    assert say_done(steps, clk) == "push_back"            # push-back budget restored for step 0


def test_reset_after_finishing_allows_a_second_run():
    steps, st, clk, *_ = make(step=N - 1)
    assert say_done(steps, clk) == "finished"
    steps.reset()
    assert not steps.finished and st.step == 0
    assert say_done(steps, clk) == "advance"


def test_reset_at_step_zero_is_quiet():
    steps, st, clk, link, changes = make()
    steps.reset()
    assert changes == [] and link.shown == []


def test_works_without_a_link():
    st = State(RECIPE)
    steps = Steps(st)
    assert steps.propose_done("user") == "advance" and st.step == 1
