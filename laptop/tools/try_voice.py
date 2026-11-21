"""Talk to the voice pipeline with the echo agent (FakeAgent) in the middle.

    cd laptop && python -m tools.try_voice            # black frames
    cd laptop && python -m tools.try_voice --webcam   # real webcam frames for the agent

Say something: it answers "You said: ...". Talk over it to cut it off. Use headphones (no echo gate yet).
"""
import argparse
import asyncio
from pathlib import Path

from loguru import logger
from pipecat.workers.runner import WorkerRunner

from coach.config import CONFIG
from coach.contracts import FakeAgent, FakeFrames, Recipe, State
from coach.voice import build_pipeline


async def main(webcam: bool):
    recipe = Recipe.load(Path(__file__).resolve().parent.parent / CONFIG.recipe_path)
    state = State(recipe, session="active")      # no BOOT button yet: start active
    worker, _speech = build_pipeline(CONFIG, FakeAgent(), FakeFrames(0 if webcam else None), state,
                                     on_interim=lambda t: logger.debug(f"interim: {t}"))
    runner = WorkerRunner()
    await runner.add_workers(worker)
    await runner.run()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--webcam", action="store_true")
    asyncio.run(main(p.parse_args().webcam))
