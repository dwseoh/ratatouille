"""Per-turn context (context.py): where things are, the recipe as the system prompt shows it, the state note."""
import time
from pathlib import Path

from coach.context import (amounts, describe_visible, fmt_qty, fmt_span, is_question, relations, render_recipe,
                           state_note)
from coach.contracts import Det, Recipe, State

RECIPE = Recipe.load(Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json")


def box(name, x1, y1, x2, y2, conf=0.9):
    return Det(name, conf, (x1, y1, x2, y2))


# ───────────── where things are ─────────────

def test_relations_in_on_and_beside_landmarks():
    dets = [box("bowl", 300, 100, 400, 200), box("cutting board", 50, 250, 300, 450),
            box("fork", 330, 130, 360, 160),        # inside the bowl's box
            box("onion", 100, 300, 150, 350),       # on the board
            box("lime", 250, 120, 290, 160),        # just left of the bowl
            box("salt", 600, 20, 630, 50)]          # far from everything
    rel = relations(dets)
    assert rel["fork"] == "in the bowl"
    assert rel["onion"] == "on the cutting board"
    assert rel["lime"] == "left of the bowl"
    assert "salt" not in rel and "bowl" not in rel and "cutting board" not in rel     # landmarks only nest

    nested = relations([box("cutting board", 0, 0, 500, 400), box("bowl", 100, 100, 200, 200)])
    assert nested == {"bowl": "on the cutting board"}


def test_relations_front_and_behind_use_the_cooks_view():
    dets = [box("bowl", 300, 100, 400, 200), box("knife", 320, 210, 380, 240), box("lime", 320, 60, 380, 95)]
    rel = relations(dets)
    assert rel["knife"] == "in front of the bowl" and rel["lime"] == "behind the bowl"


def test_describe_visible_with_and_without_a_frame():
    dets = [box("bowl", 300, 100, 400, 200), box("lime", 250, 120, 290, 160), box("hand", 0, 0, 5, 5)]
    assert describe_visible(dets, None) == "bowl, hand, lime"
    assert describe_visible(dets, (640, 480)) == "bowl (centre, far), hand, lime (centre, far, left of the bowl)"
    assert describe_visible([], (640, 480)) == "nothing"


# ───────────── formatting + amounts ─────────────

def test_fmt_span_and_qty():
    assert [fmt_span(x) for x in (5, 60, 130, 840, 3900)] == ["5 s", "1 min", "2 min 10 s", "14 min", "1 h 5 min"]
    assert [fmt_qty(x) for x in (0.25, 0.5, 1.5, 2.0, 1 / 3, 0.75, 0, 1.2, 0.125, 2 / 3)] == \
        ["1/4", "1/2", "1 1/2", "2", "1/3", "3/4", "", "1.2", "1/8", "2/3"]


def test_amounts_scale_with_servings():
    assert amounts(RECIPE) == "2 avocado, 1/4 onion, 1 tomato, 1/2 lime, 1 pinch salt, 1 small handful cilantro"
    assert amounts(RECIPE, 4) == "4 avocado, 1/2 onion, 2 tomato, 1 lime, 2 pinch salt, 2 small handful cilantro"
    assert amounts(RECIPE, 1).startswith("1 avocado, 1/8 onion, 1/2 tomato, 1/4 lime")


def test_is_question():
    assert is_question("how fine should it be") and is_question("this ok?") and not is_question("I'm done")


# ───────────── the recipe as the system prompt shows it ─────────────

def test_render_recipe_numbers_steps_and_lists_amounts_and_substitutes():
    text = render_recipe(RECIPE)
    assert text.startswith("RECIPE: Guacamole, makes 2 servings")
    assert "Step 1 (halve avocados): Cut each avocado in half lengthwise" in text
    assert "Step 7 (cilantro, mix):" in text and "Step 8" not in text
    assert "- lime: 1/2 (for juice; instead you can use: half a lemon, a splash of vinegar)" in text
    assert "- avocado: 2 (ripe: the skin gives a little when you press it; no substitute)" in text
    assert "lime = limes, green one" in text
    assert "fallback" not in text.lower() and ".jpg" not in text           # no app-internal fields
    assert render_recipe(RECIPE) == text                                    # deterministic: it's the cached prefix


def test_render_recipe_without_amounts():
    bare = Recipe("Toast", ["bread"], {}, RECIPE.steps[:1])
    text = render_recipe(bare)
    assert text.startswith("RECIPE: Toast\n") and "Ingredients" not in text and "Step 1" in text


# ───────────── the state note ─────────────

def make_state(**kw):
    st = State(RECIPE, dets=[box("avocado", 50, 300, 150, 400), box("bowl", 300, 100, 400, 200)], session="active")
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def test_note_has_step_next_positions_laser_and_whats_missing():
    st = make_state(target="avocado", on_target=True)
    note = state_note(st, frame_size=(640, 480))
    assert note.startswith("step 1/7 (halve avocados): Cut each avocado")
    assert "next: step 2 (remove pits)" in note
    assert "visible: avocado (left, near), bowl (centre, far) |" in note
    assert "needed now, not in view: knife" in note                 # step 1 points at avocado + knife
    assert "laser: avocado (on target)" in note
    st.on_target = False
    assert "laser: avocado (aiming)" in state_note(st, frame_size=(640, 480))
    st.target = None
    assert "laser: off" in state_note(st)


def test_note_on_the_last_step_and_camera_states():
    st = make_state(step=6)
    assert "next: nothing, this is the last step" in state_note(st)
    offline = state_note(st, camera="offline")
    assert "visible: avocado, bowl" in offline and "not in view" not in offline       # no frames: no guessing
    ignored = state_note(st, camera="ignored")
    assert "visible: unknown" in ignored and "avocado" not in ignored.split("visible:")[1].split("|")[0]


def test_note_lists_timers_servings_notes_and_recent_events_with_ages():
    st = make_state()
    st.timers["onions"] = time.monotonic() + 252
    st.servings = 4
    st.cook_notes += ["no cilantro", "using lemon instead of lime"]
    st.log("vision", "avocado halved")
    st.events[-1] = type(st.events[-1])(time.time() - 40, "vision", "avocado halved")
    note = state_note(st)
    assert "timers: onions 4:1" in note                              # 4:11 or 4:12 depending on timing
    assert "servings: 4 (the recipe is for 2; amounts now: 4 avocado, 1/2 onion" in note
    assert "cook notes: no cilantro; using lemon instead of lime" in note
    assert "recent: vision 40 s ago: avocado halved" in note


def test_note_drops_old_events_and_shows_what_the_cook_said_earlier():
    st = make_state()
    st.log("vision", "ancient news")
    st.events[-1] = type(st.events[-1])(time.time() - 3600, "vision", "ancient news")
    note = state_note(st, heard_earlier=[(time.time() - 125, "I don't have any cilantro")])
    assert "recent: none" in note
    assert 'cook said earlier: "I don\'t have any cilantro" (2 min 5 s ago)' in note


def test_time_on_step_and_stuck_hint_ignore_paused_time():
    st = make_state()
    st.step_since = st.cook_clock() - 300                        # 5 min on this step
    note = state_note(st)
    assert "on this step 5 min" in note and "cook may be stuck: 5 min on this step" in note
    st2 = make_state()                                            # step began 700 s ago, cook away for the last 600
    now = time.monotonic()
    st2.step_since, st2.paused_at = now - 700, now - 600
    assert 99 <= st2.step_seconds() <= 101                        # only the 100 s of actual cooking count
    assert "on this step 1 min 40 s" in state_note(st2)
    st3 = make_state()
    assert "stuck" in state_note(st3, questions_on_step=3) and "3 questions" in state_note(st3, questions_on_step=3)
    assert "stuck" not in state_note(st3, questions_on_step=2) and "on this step" not in state_note(st3)


def test_cook_clock_stands_still_while_paused():
    st = make_state()
    t0 = st.cook_clock()
    st.paused_at = time.monotonic()
    time.sleep(0.05)
    assert st.cook_clock() - t0 < 0.02                           # paused: the clock didn't move
    st.paused_total_s, st.paused_at = time.monotonic() - st.paused_at, None
    time.sleep(0.03)
    assert 0.02 <= st.cook_clock() - t0 < 0.06                   # running again, minus the pause


def test_paused_note_says_so_and_never_calls_the_cook_stuck():
    st = make_state(session="paused", paused_at=time.monotonic() - 70)
    st.step_since = st.cook_clock() - 600
    note = state_note(st)
    assert "session: PAUSED 1 min 10 s ago" in note and "resume_session" in note and "stuck" not in note
