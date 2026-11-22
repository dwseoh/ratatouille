"""Recipe importer: reading links / files, converting (fake Claude), validating, saving. All offline."""
import base64
import copy
import functools
import http.server
import importlib.util
import json
import threading
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from coach import recipe_import as RI
from coach.contracts import Recipe
from coach.recipe_images import step_image_path

HERE = Path(__file__).resolve().parent
PAGE = (HERE / "fixtures" / "recipe_page.html").read_text()
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64

GOOD = {
    "name": "Easy Weeknight Pad Thai",
    "objects": ["rice noodles", "tamarind paste", "lime", "egg", "peanuts", "wok", "bowl"],
    "aliases": [{"object": "lime", "also_called": ["limes", "the green one"]}, {"object": "egg", "also_called": ["eggs"]}],
    "steps": [
        {"short": "soak noodles", "goal": "Soften the rice noodles.", "how": ["put the noodles in a bowl", "cover with warm water"],
         "tips": [], "point": ["rice noodles", "bowl"], "done_when": "noodles are bendy in water of a bowl",
         "fallback": "Soak the noodles in warm water for 20 minutes."},
        {"short": "scramble eggs", "goal": "Cook the eggs.", "how": ["heat the wok", "scramble the eggs"],
         "tips": ["careful, the wok is hot"], "point": ["egg", "wok"], "done_when": "eggs set in the wok",
         "fallback": "Scramble the eggs in the hot wok."},
        {"short": "toss and serve", "goal": "Finish the dish.", "how": ["add noodles and sauce", "toss", "top with peanuts and lime"],
         "tips": [], "point": ["lime", "peanuts"], "done_when": "noodles coated in sauce with peanuts on top",
         "fallback": "Toss everything together and serve with lime."}]}


class FakeClaude:
    """messages.create returns scripted answers (dicts are JSON-encoded) and records every request."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []
        self.messages = NS(create=self._create)

    def _create(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        a = self.answers.pop(0)
        return NS(content=[NS(type="text", text=a if isinstance(a, str) else json.dumps(a))])


def fake_fetch(pages):
    def fetch(url, max_bytes=RI.MAX_PAGE_BYTES):
        if url not in pages:
            raise RI.ImportErr(f"could not fetch {url}: 404")
        body, ctype = pages[url]
        return url, body, ctype
    return fetch


# ───────────── reading ─────────────

def test_jsonld_recipe_is_found_inside_a_graph_and_flattened():
    node = RI.extract_jsonld_recipe(PAGE)
    assert node["name"] == "Easy Weeknight Pad Thai"
    text = RI.recipe_text_from_jsonld(node)
    assert "- 8 oz rice noodles" in text and "[Prep]" in text and "1. [Prep]" in text and "Scramble the eggs" in text
    assert "pad thai & sauce" in text                                       # HTML entities decoded
    assert RI.hero_image_url(node) == "/img/pad-thai-hero.jpg"


def test_web_source_uses_the_structured_data_and_makes_the_picture_link_absolute():
    src = RI.load_source("https://example.com/pad-thai", fetch=fake_fetch({"https://example.com/pad-thai": (PAGE.encode(), "text/html; charset=utf-8")}))
    assert src.kind == "web" and src.title == "Easy Weeknight Pad Thai" and "Instructions:" in src.text
    assert src.hero_url == "https://example.com/img/pad-thai-hero.jpg" and src.link == "https://example.com/pad-thai"
    assert "Ignore this ad text" not in src.text                            # the structured data, not the page


def test_page_without_structured_data_falls_back_to_visible_text():
    html = ("<html><head><title>Granny's Soup</title><style>x{}</style><script>var a=1;</script></head><body><nav>menu menu</nav>"
            + "<p>Boil the water. Add the carrots and onion and simmer for twenty minutes.</p>" * 12 + "</body></html>")
    src = RI.load_source("https://x.test/soup", fetch=fake_fetch({"https://x.test/soup": (html.encode(), "text/html")}))
    assert src.title == "Granny's Soup" and "Boil the water" in src.text and "var a=1" not in src.text and "menu menu" not in src.text
    with pytest.raises(RI.ImportErr, match="no readable recipe"):
        RI.load_source("https://x.test/empty", fetch=fake_fetch({"https://x.test/empty": (b"<html><body>hi</body></html>", "text/html")}))


def test_local_files_pdf_photo_and_text(tmp_path):
    pdf = tmp_path / "chili.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    src = RI.load_source(str(pdf))
    assert src.kind == "pdf" and src.blocks[0]["type"] == "document" and src.blocks[0]["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(src.blocks[0]["source"]["data"]) == b"%PDF-1.4 fake"
    img = tmp_path / "page.JPG"
    img.write_bytes(b"\xff\xd8\xff" + b"0" * 50)
    src = RI.load_source(str(img))
    assert src.kind == "image" and src.blocks[0]["type"] == "image" and src.blocks[0]["source"]["media_type"] == "image/jpeg"
    txt = tmp_path / "notes.txt"
    txt.write_text("Mix flour and water. Bake.")
    assert RI.load_source(str(txt)).text == "Mix flour and water. Bake."
    with pytest.raises(RI.ImportErr, match="neither a link nor"):
        RI.load_source(str(tmp_path / "missing.pdf"))
    (tmp_path / "fake.pdf").write_bytes(b"not a pdf")
    with pytest.raises(RI.ImportErr, match="not"):
        RI.load_source(str(tmp_path / "fake.pdf"))


def test_only_http_links_are_followed():
    for bad in ("file:///etc/passwd", "ftp://x/y", "gopher://x"):
        with pytest.raises(RI.ImportErr, match="only http"):
            RI.fetch_url(bad)


def test_a_link_to_a_pdf_or_photo_becomes_that_kind():
    pages = {"https://x.test/r.pdf": (b"%PDF-1.7 hi", "application/pdf"), "https://x.test/r": (b"\xff\xd8\xff" + b"1" * 20, "image/jpeg")}
    assert RI.load_source("https://x.test/r.pdf", fetch=fake_fetch(pages)).kind == "pdf"
    assert RI.load_source("https://x.test/r", fetch=fake_fetch(pages)).kind == "image"


# ───────────── validating ─────────────

def good():
    return RI.normalize(copy.deepcopy(GOOD), RI.Source("web", link="https://example.com/pad-thai"))


def test_a_good_recipe_validates_and_loads_through_the_real_contract(tmp_path):
    d = good()
    assert RI.validate(d) == [] and d["source"] == "https://example.com/pad-thai"
    assert d["aliases"]["lime"] == ["limes", "the green one"]
    p = RI.save_recipe(d, tmp_path)
    r = Recipe.load(p)
    assert r.name == "Easy Weeknight Pad Thai" and len(r.steps) == 3 and r.steps[0].image == "" and r.names()["the green one"] == "lime"


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d["objects"].append("1 tsp kosher salt, divided"), "not a physical thing"),
    (lambda d: d["objects"].append("a very long object name here"), "not a physical thing"),
    (lambda d: d["steps"][0]["point"].append("spaceship"), "not in `objects`"),
    (lambda d: d["steps"][0].update(short="this name is far too long"), "`short` must be"),
    (lambda d: d["steps"][1].update(done_when=""), "`done_when` is empty"),
    (lambda d: d["steps"][1].update(how=[]), "`how` needs"),
    (lambda d: d["steps"][1].update(fallback=" ".join(["word"] * 40)), "one short sentence"),
    (lambda d: d.update(steps=[]), "0 steps"),
    (lambda d: d.update(objects=["bowl"]), "1 objects"),
    (lambda d: d["aliases"].update(ghost=["boo"]), "alias key 'ghost'"),
    (lambda d: d.update(name=""), "no name"),
])
def test_validation_catches_each_kind_of_problem(mutate, expect):
    d = good()
    mutate(d)
    problems = RI.validate(d)
    assert any(expect in p for p in problems), problems


# ───────────── converting (fake Claude) ─────────────

def test_convert_sends_a_structured_output_request_and_returns_a_valid_recipe():
    client = FakeClaude(GOOD)
    src = RI.Source("web", text="Ingredients: ...", link="https://example.com/pad-thai")
    d = RI.convert(client, src)
    kw = client.requests[0]
    assert kw["model"] == RI.IMPORT_MODEL == "claude-opus-5-5"
    assert kw["output_config"]["format"]["type"] == "json_schema" and kw["output_config"]["effort"] == "high"
    assert "PHYSICAL THINGS" in kw["system"] and "Ingredients: ..." in kw["messages"][0]["content"][-1]["text"]
    assert d["name"] == "Easy Weeknight Pad Thai" and RI.validate(d) == []
    assert RI.convert(FakeClaude(GOOD), src, name="My Pad Thai")["name"] == "My Pad Thai"


def test_pdf_and_photo_blocks_are_passed_to_claude(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4 x")
    client = FakeClaude(GOOD)
    RI.convert(client, RI.load_source(str(tmp_path / "a.pdf")))
    assert client.requests[0]["messages"][0]["content"][0]["type"] == "document"


def test_an_invalid_first_answer_is_retried_once_with_the_problems_listed():
    bad = copy.deepcopy(GOOD)
    bad["steps"][0]["point"].append("spaceship")
    client = FakeClaude(bad, GOOD)
    d = RI.convert(client, RI.Source("text", text="x"))
    assert len(client.requests) == 2 and RI.validate(d) == []
    follow = client.requests[1]["messages"][-1]["content"]
    assert "spaceship" in follow and "not in `objects`" in follow and client.requests[1]["messages"][-2]["role"] == "assistant"


def test_it_gives_up_after_the_retry_and_says_why():
    bad = copy.deepcopy(GOOD)
    bad["steps"] = []
    with pytest.raises(RI.ImportErr, match="could not produce a valid recipe") as e:
        RI.convert(FakeClaude(bad, bad), RI.Source("text", text="not a recipe"))
    assert "0 steps" in str(e.value)
    with pytest.raises(RI.ImportErr, match="not valid JSON"):
        RI.convert(FakeClaude("this is not json"), RI.Source("text", text="x"))


# ───────────── pictures ─────────────

@pytest.fixture
def local_server(tmp_path):
    (tmp_path / "hero.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"j" * 200)
    (tmp_path / "page.html").write_text("<html>not a picture</html>")
    (tmp_path / "big.png").write_bytes(PNG + b"0" * 10_000)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_hero_picture_is_downloaded_checked_and_stored_by_convention(tmp_path, local_server):
    p = RI.download_hero(f"{local_server}/hero.jpg", tmp_path / "images" / "pad-thai")
    assert p.name == "hero.jpg" and p.read_bytes().startswith(b"\xff\xd8\xff")
    rec = Recipe.load(RI.save_recipe(good(), tmp_path / "recipes"))
    (tmp_path / "recipes" / "images" / "easy-weeknight-pad-thai").mkdir(parents=True)
    (tmp_path / "recipes" / "images" / "easy-weeknight-pad-thai" / "hero.jpg").write_bytes(b"\xff\xd8\xff")
    assert step_image_path(rec, 1, tmp_path / "recipes").name == "hero.jpg"          # the dashboard picks it up


def test_hero_download_refuses_non_pictures_and_huge_files(tmp_path, local_server):
    with pytest.raises(RI.ImportErr, match="not a JPEG"):
        RI.download_hero(f"{local_server}/page.html", tmp_path)
    with pytest.raises(RI.ImportErr, match="larger than"):
        RI.download_hero(f"{local_server}/big.png", tmp_path, fetch=lambda u, n: RI.fetch_url(u, 500))
    with pytest.raises(RI.ImportErr, match="only http"):
        RI.download_hero("file:///etc/hosts", tmp_path)


# ───────────── the command-line tool ─────────────

def cli():
    spec = importlib.util.spec_from_file_location("import_recipe_cli", HERE.parent.parent / "tools" / "import_recipe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PAGES = {"https://example.com/pad-thai": (PAGE.encode(), "text/html"),
         "https://example.com/img/pad-thai-hero.jpg": (b"\xff\xd8\xff\xe0" + b"j" * 300, "image/jpeg")}


def test_cli_asks_first_then_writes_recipe_and_picture(tmp_path):
    lines = []
    rc = cli().main(["https://example.com/pad-thai", "--out", str(tmp_path)], client=FakeClaude(GOOD), fetch=fake_fetch(PAGES),
                    input_fn=lambda prompt: "y", out=lines.append)
    text = "\n".join(lines)
    assert rc == 0 and "Looks right?" not in text                       # the prompt goes through input_fn, not out
    assert (tmp_path / "easy-weeknight-pad-thai.json").exists() and (tmp_path / "images" / "easy-weeknight-pad-thai" / "hero.jpg").exists()
    assert "rice noodles" in text and "RECIPE=recipes/easy-weeknight-pad-thai.json" in text
    assert Recipe.load(tmp_path / "easy-weeknight-pad-thai.json").steps[2].short == "toss and serve"


def test_cli_writes_nothing_when_you_say_no_or_the_file_exists(tmp_path):
    rc = cli().main(["https://example.com/pad-thai", "--out", str(tmp_path)], client=FakeClaude(GOOD), fetch=fake_fetch(PAGES),
                    input_fn=lambda prompt: "n", out=lambda *_: None)
    assert rc == 1 and list(tmp_path.iterdir()) == []
    args = ["https://example.com/pad-thai", "--out", str(tmp_path), "--yes", "--no-images"]
    assert cli().main(args, client=FakeClaude(GOOD), fetch=fake_fetch(PAGES), out=lambda *_: None) == 0
    assert not (tmp_path / "images").exists()                           # --no-images
    lines = []
    assert cli().main(args, client=FakeClaude(GOOD), fetch=fake_fetch(PAGES), out=lines.append) == 1
    assert "already exists" in "\n".join(lines)
    assert cli().main(args + ["--force"], client=FakeClaude(GOOD), fetch=fake_fetch(PAGES), out=lambda *_: None) == 0


def test_cli_reports_problems_plainly(tmp_path):
    lines = []
    rc = cli().main(["https://example.com/missing", "--out", str(tmp_path)], client=FakeClaude(), fetch=fake_fetch({}),
                    out=lines.append)
    assert rc == 1 and "Could not import" in "\n".join(lines)
    bad = copy.deepcopy(GOOD)
    bad["steps"] = []
    lines.clear()
    rc = cli().main(["https://example.com/pad-thai", "--out", str(tmp_path)], client=FakeClaude(bad, bad), fetch=fake_fetch(PAGES),
                    out=lines.append)
    assert rc == 1 and "0 steps" in "\n".join(lines) and list(tmp_path.iterdir()) == []


def test_cli_explains_a_missing_key_or_api_failure_instead_of_crashing(tmp_path):
    import anthropic
    import httpx

    class NoKey:
        messages = NS(create=lambda **kw: (_ for _ in ()).throw(TypeError("Could not resolve authentication method. Expected one of api_key")))

    class Down:
        messages = NS(create=lambda **kw: (_ for _ in ()).throw(anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))))
    for client, expect in ((NoKey(), "ANTHROPIC_API_KEY"), (Down(), "Claude API failed")):
        lines = []
        rc = cli().main(["https://example.com/pad-thai", "--out", str(tmp_path), "--yes"], client=client, fetch=fake_fetch(PAGES), out=lines.append)
        assert rc == 1 and expect in "\n".join(lines) and list(tmp_path.iterdir()) == []
    with pytest.raises(TypeError):                                         # a real bug is still a crash, not hidden
        class Buggy:
            messages = NS(create=lambda **kw: (_ for _ in ()).throw(TypeError("unsupported operand")))
        cli().main(["https://example.com/pad-thai", "--out", str(tmp_path), "--yes"], client=Buggy(), fetch=fake_fetch(PAGES), out=lambda *_: None)


# ───────────── per-step pictures (from the page) with the dish picture as the fallback ─────────────

STEP_PAGE = (PAGE.replace('{"@type":"HowToStep","text":"Soak the rice noodles in warm water for 20 minutes."}',
                          '{"@type":"HowToStep","text":"Soak the rice noodles in warm water for 20 minutes.","image":"/img/soak.jpg"}')
             .replace('{"@type":"HowToStep","text":"Scramble the eggs in a hot wok."}',
                      '{"@type":"HowToStep","text":"Scramble the eggs in a hot wok.","image":{"@type":"ImageObject","url":"/img/eggs.jpg"}}'))
JPG = (b"\xff\xd8\xff\xe0" + b"j" * 300, "image/jpeg")
STEP_PAGES = {"https://example.com/pad-thai": (STEP_PAGE.encode(), "text/html"), "https://example.com/img/pad-thai-hero.jpg": JPG,
              "https://example.com/img/soak.jpg": JPG, "https://example.com/img/eggs.jpg": JPG}


def mapped(*maps):
    d = copy.deepcopy(GOOD)
    for st, m in zip(d["steps"], maps):
        st["source_steps"] = m
    return d


def test_the_page_pictures_are_lined_up_with_the_numbered_instructions():
    src = RI.load_source("https://example.com/pad-thai", fetch=fake_fetch(STEP_PAGES))
    assert src.step_images == ["", "https://example.com/img/soak.jpg", "", "", "https://example.com/img/eggs.jpg", ""]
    assert "2. Soak the rice noodles" in src.text and "5. Scramble the eggs" in src.text      # same numbers Claude is shown


def test_each_step_gets_the_picture_of_the_instructions_it_covers(tmp_path):
    src = RI.load_source("https://example.com/pad-thai", fetch=fake_fetch(STEP_PAGES))
    recipe = RI.convert(FakeClaude(mapped([2, 3], [5], [6])), src)
    assert src.step_map == [[2, 3], [5], [6]]
    got = RI.download_step_images(recipe, src, tmp_path / "images" / "pad-thai", fetch=fake_fetch(STEP_PAGES))
    assert sorted(p.name for p in got) == ["step-1.jpg", "step-2.jpg"]                     # step 3 has none -> dish picture


def test_steps_without_their_own_picture_fall_back_to_the_dish_picture(tmp_path):
    src = RI.load_source("https://example.com/pad-thai", fetch=fake_fetch(STEP_PAGES))
    recipe = RI.convert(FakeClaude(mapped([2, 3], [5], [6])), src)
    root = tmp_path / "recipes"
    folder = root / "images" / "easy-weeknight-pad-thai"
    RI.download_hero(src.hero_url, folder, fetch=fake_fetch(STEP_PAGES))
    RI.download_step_images(recipe, src, folder, fetch=fake_fetch(STEP_PAGES))
    (root).mkdir(exist_ok=True)
    (root / "r.json").write_text(json.dumps(recipe))
    rec = Recipe.load(root / "r.json")
    assert [step_image_path(rec, n, root).name for n in range(3)] == ["step-1.jpg", "step-2.jpg", "hero.jpg"]


def test_a_picture_shared_by_two_steps_is_saved_once_and_bad_downloads_are_skipped(tmp_path):
    src = RI.load_source("https://example.com/pad-thai", fetch=fake_fetch(STEP_PAGES))
    recipe = RI.convert(FakeClaude(mapped([2], [2], [5])), src)
    pages = dict(STEP_PAGES, **{"https://example.com/img/eggs.jpg": (b"<html>not a picture</html>", "text/html")})
    got = RI.download_step_images(recipe, src, tmp_path, fetch=fake_fetch(pages))
    assert [p.name for p in got] == ["step-1.jpg"]


def test_the_cli_reports_the_step_pictures(tmp_path):
    lines = []
    rc = cli().main(["https://example.com/pad-thai", "--out", str(tmp_path), "--yes"],
                    client=FakeClaude(mapped([2], [5], [6])), fetch=fake_fetch(STEP_PAGES), input_fn=lambda p: "y", out=lines.append)
    folder = tmp_path / "images" / "easy-weeknight-pad-thai"
    assert rc == 0 and (folder / "step-1.jpg").exists() and (folder / "step-2.jpg").exists() and (folder / "hero.jpg").exists()
    assert "Saved 2 step picture(s)" in "\n".join(lines)
    rc = cli().main(["https://example.com/pad-thai", "--out", str(tmp_path / "b"), "--yes", "--no-images"],
                    client=FakeClaude(mapped([2], [5], [6])), fetch=fake_fetch(STEP_PAGES), input_fn=lambda p: "y", out=lines.append)
    assert rc == 0 and not (tmp_path / "b" / "images").exists()
