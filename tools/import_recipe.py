"""Import a recipe from a link, PDF, text file or photo into our recipe format. Offline tool, not on the live path.

    .venv/bin/python tools/import_recipe.py "https://example.com/some-recipe"
    .venv/bin/python tools/import_recipe.py ~/Downloads/grandmas-chili.pdf --name "Grandma's Chili"
    .venv/bin/python tools/import_recipe.py photo_of_recipe_page.jpg
    .venv/bin/python tools/import_recipe.py <source> --no-images     # don't download the recipe's main picture
    .venv/bin/python tools/import_recipe.py <source> --out /tmp/try  # write somewhere else (default: laptop/recipes)

It shows what it found and asks "looks right?" before writing anything. Costs a few cents (Claude Opus 5.5).
Then:  cd laptop && RECIPE=recipes/<name>.json ../.venv/bin/python -m coach.agent_cli
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "laptop"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "laptop" / ".env")           # ANTHROPIC_API_KEY lives here

from coach.recipe_images import slug as make_slug  # noqa: E402
from coach.recipe_import import (IMPORT_MODEL, ImportErr, convert, download_hero, download_step_images,  # noqa: E402
                                 fetch_url, load_source,
                                 save_recipe, summary)


def main(argv=None, *, client=None, fetch=fetch_url, input_fn=input, out=print) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="a link, or a path to a PDF / photo / text file")
    ap.add_argument("--name", help="recipe name (default: the one in the recipe)")
    ap.add_argument("--out", default=str(ROOT / "laptop" / "recipes"), help="folder for the JSON (default: laptop/recipes)")
    ap.add_argument("--model", default=IMPORT_MODEL)
    ap.add_argument("--no-images", action="store_true", help="don't download pictures")
    ap.add_argument("--force", action="store_true", help="overwrite an existing recipe file")
    ap.add_argument("--yes", action="store_true", help="don't ask 'looks right?' (for scripts)")
    args = ap.parse_args(argv)
    out_dir = Path(args.out)

    try:
        src = load_source(args.source, fetch=fetch)
        out(f"Read a {src.kind} recipe: {src.title or '(untitled)'}"
            + (f"  [{len(src.text)} characters of text]" if src.text else ""))
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        out(f"Converting with {args.model} (a minute or so)...")
        recipe = convert(client, src, model=args.model, name=args.name)
    except ImportErr as e:
        out(f"\nCould not import: {e}")
        return 1
    except TypeError as e:
        if "authentication" not in str(e).lower():
            raise
        out("\nCould not import: no Anthropic credentials. Put ANTHROPIC_API_KEY=... in laptop/.env")
        return 1
    except Exception as e:                       # the API being down / rate-limited etc.: say so plainly
        if type(e).__module__.split(".")[0] != "anthropic":
            raise
        out(f"\nCould not import: the Claude API failed ({type(e).__name__}: {e})")
        return 1

    slug = make_slug(recipe["name"])
    target = out_dir / f"{slug}.json"
    out("\n" + summary(recipe))
    out("\nThe objects become what the camera looks for (YOLO classes) and what the laser can point at:"
        "\ncheck they are real, visible things.")
    if target.exists() and not args.force:
        out(f"\n{target} already exists. Use --force to overwrite, or --name to pick another name.")
        return 1
    if not args.yes and input_fn(f"\nLooks right? Write {target}? [y/N] ").strip().lower() not in ("y", "yes"):
        out("Nothing written.")
        return 1

    save_recipe(recipe, out_dir, slug)
    out(f"Wrote {target}")
    if src.hero_url and not args.no_images:
        try:
            p = download_hero(src.hero_url, out_dir / "images" / slug, fetch=fetch)
            out(f"Saved the main picture to {p}  (from {src.link}: check you may use it; local demo only)")
        except ImportErr as e:
            out(f"(no picture saved: {e})")
    if src.step_images and not args.no_images:
        got = download_step_images(recipe, src, out_dir / "images" / slug, fetch=fetch)
        out(f"Saved {len(got)} step picture(s); steps without one show the finished dish." if got
            else "The page had no per-step pictures; every step shows the finished dish.")
    out(f"\nTry it:  cd laptop && RECIPE=recipes/{slug}.json ../.venv/bin/python -m coach.agent_cli")
    out("Add or replace pictures with laptop/recipes/images/" + slug + "/step-1.jpg, step-2.jpg ... (optional)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
