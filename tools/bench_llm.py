"""Model bake-off: which model says its first words fastest?

Sends the SAME request (system prompt + recipe + 3 camera frames + one question) to each candidate
model several times and prints a table of timings. The agent's real prompt and image resizing are
reused, so the numbers match what the live app will see.

Run from the repo root, with the venv active and keys in laptop/.env:

    python tools/bench_llm.py                      # default candidates, 10 runs each
    python tools/bench_llm.py --runs 5             # quicker
    python tools/bench_llm.py --models claude-haiku-5-5:low claude-opus-5-5:low:fast

Candidate syntax:
    claude-opus-5-5:low          Claude model : effort
    claude-opus-5-5:low:fast     ... with fast mode (speed="fast", costs 2x)

What each column means:
    ttft      time to first token  = request sent -> first piece of text arrives
    1st sent  time to first sentence = when text-to-speech could START speaking (this is the one that matters)
    total     time until the whole reply is done
"""
import argparse
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "laptop"))   # so we can import coach.agent

import cv2                                  # noqa: E402
from dotenv import load_dotenv              # noqa: E402

load_dotenv(ROOT / "laptop" / ".env")

from coach.agent import SYSTEM, TOOLS, jpeg_block  # noqa: E402  (the real prompt, tools + image shrinker)

FRAMES_DIR = ROOT / "tools" / "test_frames"
RECIPE_PATH = ROOT / "laptop" / "recipes" / "guacamole.json"
FRAME_MAX_PX = 512                          # same as Config.frame_max_px
MAX_TOKENS = 1500   # hidden "thinking" tokens count against this, so too small a cap cuts the spoken reply off
# Frames in cooking order (hold avocado -> halves -> mashed with onion), so the question has a clear answer.
FRAME_ORDER = ["avocado_hold", "avocado_halves", "mash_bowl"]
QUESTION = ("[step 3/7: mash | recent: avocados halved; pits removed]\n"
            "User: I think I'm done mashing. Does it look right, and what's next?")

# Default candidates. Edit freely.
DEFAULT_MODELS = [
    "claude-opus-5-5:low",
    "claude-opus-5-5:low:fast",
    "claude-haiku-5-5:low",
]

# $ per million tokens (input, output). From the model table.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5-5:fast": (8.0, 40.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}

SENTENCE_END = re.compile(r"[.!?](\s|$)")


# ------------------------------------------------------------------ inputs
def load_inputs():
    recipe = json.loads(RECIPE_PATH.read_text())
    system = SYSTEM + "\n\nRECIPE:\n" + json.dumps(recipe)
    paths = [FRAMES_DIR / f"{n}.jpg" for n in FRAME_ORDER if (FRAMES_DIR / f"{n}.jpg").exists()]
    if not paths:
        sys.exit(f"No test frames found in {FRAMES_DIR}")
    blocks = [jpeg_block(cv2.imread(str(p)), FRAME_MAX_PX) for p in paths]   # Anthropic image blocks
    return system, blocks


# ------------------------------------------------------------------ one run
# Returns (ttft_s, first_sentence_s, total_s, input_tokens, output_tokens, text).
def _timing(t0):
    """Small helper that records when the first token / first sentence arrive."""
    state = {"ttft": None, "sent": None, "text": ""}

    def on_text(chunk):
        now = time.perf_counter() - t0
        if state["ttft"] is None and chunk.strip():
            state["ttft"] = now
        state["text"] += chunk
        if state["sent"] is None and SENTENCE_END.search(state["text"]):
            state["sent"] = now

    return state, on_text


def run_claude(client, model, effort, fast, system, blocks):
    kwargs = dict(
        model=model,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": blocks + [{"type": "text", "text": QUESTION}]}],
        output_config={"effort": effort},
        tools=TOOLS,
    )
    if fast:
        kwargs["speed"] = "fast"
    t0 = time.perf_counter()
    state, on_text = _timing(t0)
    with client.messages.stream(**kwargs) as stream:
        for chunk in stream.text_stream:
            on_text(chunk)
        final = stream.get_final_message()
    total = time.perf_counter() - t0
    u = final.usage
    in_tok = (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) \
        + (getattr(u, "cache_creation_input_tokens", 0) or 0)
    return state["ttft"], state["sent"] or total, total, in_tok, u.output_tokens, state["text"]


def _is_overload(e):
    text = str(e)
    return any(k in text for k in ("503", "429", "529", "overloaded"))


def with_retries(fn, tries=5):
    """Retry on overloaded / rate-limited servers (429, 529) with a growing pause."""
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if i == tries - 1 or not _is_overload(e):
                raise
            time.sleep(2 * (i + 1))


# ------------------------------------------------------------------ driver
def bench(spec, runs, system, blocks, clients):
    parts = spec.split(":")
    name = parts[0]
    effort = parts[1] if len(parts) > 1 and parts[1] != "fast" else "low"
    fast = "fast" in parts[1:]

    if "claude" not in clients:
        import anthropic
        clients["claude"] = anthropic.Anthropic()
    call = lambda: run_claude(clients["claude"], name, effort, fast, system, blocks)  # noqa: E731

    print(f"-- {spec}: ", end="", flush=True)
    rows, sample = [], ""
    try:
        with_retries(call)                        # warm-up run (primes cache + connection); not counted
        print("warm-up done, ", end="", flush=True)
        for _ in range(runs):
            ttft, sent, total, in_tok, out_tok, text = with_retries(call)
            rows.append((ttft, sent, total, in_tok, out_tok))
            sample = sample or text
            print(".", end="", flush=True)
            time.sleep(0.5)                       # be gentle with rate limits
    except Exception as e:  # noqa: BLE001
        print(f"\n   FAILED: {type(e).__name__}: {str(e)[:200]}")
        if not rows:
            return None
    print()
    return {"spec": spec, "name": name, "fast": fast, "rows": rows, "sample": sample}


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p * (len(values) - 1))))]


def cost_per_request(res):
    price = PRICES.get(res["name"] + (":fast" if res["fast"] else ""))
    if not price:
        return None
    in_tok = statistics.mean(r[3] for r in res["rows"])
    out_tok = statistics.mean(r[4] for r in res["rows"])
    return (in_tok * price[0] + out_tok * price[1]) / 1_000_000


def print_table(results):
    header = f"{'candidate':<28}{'ttft med':>9}{'ttft p90':>9}{'1st sent med':>14}{'1st sent p90':>14}{'total med':>10}{'out tok':>8}{'$/req':>9}"
    print("\n" + header + "\n" + "-" * len(header))
    for res in sorted(results, key=lambda r: statistics.median(x[1] for x in r["rows"])):
        ttft = [r[0] for r in res["rows"] if r[0] is not None]
        sent = [r[1] for r in res["rows"]]
        total = [r[2] for r in res["rows"]]
        c = cost_per_request(res)
        print(f"{res['spec']:<28}{statistics.median(ttft):>8.2f}s{pct(ttft, .9):>8.2f}s"
              f"{statistics.median(sent):>13.2f}s{pct(sent, .9):>13.2f}s{statistics.median(total):>9.2f}s"
              f"{statistics.mean(r[4] for r in res['rows']):>8.0f}{('$%.4f' % c) if c else '   n/a':>9}")
    print("\nSorted fastest first by median time to first sentence. Seconds; p90 = slowest-ish run.")
    print("\nSample replies (read these! the fastest model is useless if its advice is bad):")
    for res in results:
        print(f"\n[{res['spec']}]\n{res['sample'].strip()}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=10, help="timed runs per candidate (default 10)")
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS, help="candidates, see syntax above")
    args = ap.parse_args()

    system, blocks = load_inputs()
    print(f"{len(blocks)} frames at {FRAME_MAX_PX}px, {args.runs} timed runs + 1 warm-up per candidate\n")
    clients, results = {}, []
    for spec in args.models:
        res = bench(spec, args.runs, system, blocks, clients)
        if res:
            results.append(res)
    if results:
        print_table(results)


if __name__ == "__main__":
    main()
