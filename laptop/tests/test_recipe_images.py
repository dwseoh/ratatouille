"""Step photos: by name, by folder convention, hero fallback, and never outside recipes/."""
import json
from pathlib import Path

from coach.contracts import Recipe
from coach.recipe_images import slug, step_image_path

RECIPE = Recipe.load(Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json")


def touch(p: Path, data=b"\xff\xd8\xff"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_slug():
    assert slug("Guacamole") == "guacamole" and slug("Mom's Pad Thai!") == "mom-s-pad-thai" and slug("???") == "recipe"


def test_convention_then_hero_then_nothing(tmp_path):
    assert step_image_path(RECIPE, 0, tmp_path) is None                       # no photos at all: nothing, no crash
    hero = touch(tmp_path / "images" / "guacamole" / "hero.png")
    assert step_image_path(RECIPE, 3, tmp_path) == hero                       # no step photo: the hero picture
    step3 = touch(tmp_path / "images" / "guacamole" / "step-3.jpg")
    assert step_image_path(RECIPE, 2, tmp_path) == step3                      # step-N is 1-based
    assert step_image_path(RECIPE, 3, tmp_path) == hero


def test_a_step_can_name_its_photo(tmp_path):
    d = json.loads((Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json").read_text())
    d["steps"][1]["image"] = "images/custom/mash.webp"
    p = tmp_path / "r.json"
    p.write_text(json.dumps(d))
    r = Recipe.load(p)
    named = touch(tmp_path / "images" / "custom" / "mash.webp")
    touch(tmp_path / "images" / "guacamole" / "step-2.jpg")
    assert step_image_path(r, 1, tmp_path) == named                           # explicit beats convention


def test_only_files_inside_recipes_are_ever_served(tmp_path):
    root = tmp_path / "recipes"
    root.mkdir()
    secret = touch(tmp_path / "secret.jpg")
    d = json.loads((Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json").read_text())
    d["steps"][0]["image"] = "../secret.jpg"
    p = root / "r.json"
    p.write_text(json.dumps(d))
    assert secret.exists() and step_image_path(Recipe.load(p), 0, root) is None
    d["steps"][0]["image"] = str(secret)                                       # an absolute path too
    p.write_text(json.dumps(d))
    assert step_image_path(Recipe.load(p), 0, root) is None


def test_only_picture_files_and_valid_steps(tmp_path):
    touch(tmp_path / "images" / "guacamole" / "step-1.txt")                    # wrong type: ignored
    assert step_image_path(RECIPE, 0, tmp_path) is None
    assert step_image_path(RECIPE, 99, tmp_path) is None and step_image_path(RECIPE, -1, tmp_path) is None
