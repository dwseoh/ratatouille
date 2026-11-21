"""Talk to the agent by typing. No mic, speaker, S3 or YOLO needed (B1 "done when" check).

    cd laptop && python -m coach.agent_cli                 # black frames, fake detections
    python -m coach.agent_cli --webcam 0                   # real laptop webcam frames
    python -m coach.agent_cli --say                        # also speak replies with macOS `say`
    python -m coach.agent_cli --visible avocado lime bowl  # choose which objects count as "seen"

Type a question, e.g. "where do I start?". Prints each spoken sentence, each tool call, and a timing line.
Ctrl-D or an empty line to quit.
"""
import argparse
import asyncio
import logging
from pathlib import Path

from .agent import Agent, make_handlers
from .config import CONFIG as cfg
from .contracts import FakeAimer, FakeFrames, FakeLink, FakeSpeech, FakeSteps, Recipe, State, fake_dets


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--webcam", default=None, help="camera index or video file; default: black frames")
    ap.add_argument("--say", action="store_true", help="speak replies with macOS `say`")
    ap.add_argument("--visible", nargs="*", default=None, help="objects the fake detector 'sees'")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")

    recipe = Recipe.load(Path(__file__).resolve().parent.parent / cfg.recipe_path)
    st = State(recipe, dets=fake_dets(tuple(args.visible) if args.visible is not None else tuple(recipe.objects)))
    aimer, steps, link, speech = FakeAimer(st), FakeSteps(st), FakeLink(), FakeSpeech(say_aloud=args.say)
    steps.on_change(lambda n: link.show_text(2, f"Step {n + 1}: {recipe.steps[n].short}"))
    source = int(args.webcam) if args.webcam and args.webcam.isdigit() else args.webcam
    frames = FakeFrames(source)

    agent = Agent(cfg, st, make_handlers(st, aimer, steps, link))
    print(f"model={cfg.model} effort={cfg.effort} | visible: {', '.join(sorted(st.visible()))}")
    await agent.prewarm()

    while True:
        try:
            text = (await asyncio.to_thread(input, "\nyou> ")).strip()
        except EOFError:
            break
        if not text:
            break
        stats = await agent.turn(text, frames.recent(cfg.frames_per_turn), speech.say)
        ttft = f"{stats.ttft_s:.2f}s" if stats.ttft_s is not None else "n/a"
        print(f"  ttft {ttft} | total {stats.total_s:.2f}s | {stats.sentences} sentence(s) | "
              f"tools: {', '.join(stats.tools) or 'none'}" + (f" | ERROR {stats.error}" if stats.error else ""))
        u = agent.last_usage
        if u is not None:
            print(f"  cache: read {getattr(u, 'cache_read_input_tokens', 0)} / write "
                  f"{getattr(u, 'cache_creation_input_tokens', 0)} / uncached in {getattr(u, 'input_tokens', 0)} tokens")
        print(f"  [step {st.step + 1}/{len(recipe.steps)}] {st.note()}")


if __name__ == "__main__":
    asyncio.run(main())
