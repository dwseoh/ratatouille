"""Talk to the voice pipeline with the echo agent (FakeAgent) and the intent router.

    cd laptop && python -m tools.try_voice            # black frames
    cd laptop && python -m tools.try_voice --webcam   # real webcam frames for the agent
    cd laptop && python -m tools.try_voice --no-intents

Say something: it answers "You said: ...". Talk over it to cut it off. Use headphones (no echo gate yet).
Intents: "where's the green one" prints "[laser] -> lime" (FakeAimer, fake detections: avocado, lime,
bowl, knife, cutting board), "ok I'm done" advances FakeSteps, "stop" cuts the audio.
"""
import argparse
import asyncio
from pathlib import Path

from pipecat.workers.runner import WorkerRunner

from coach.config import CONFIG
from coach.contracts import FakeAgent, FakeAimer, FakeFrames, FakeSteps, Recipe, State, fake_dets
from coach.intents import IntentActions, IntentRouter
from coach.voice import build_pipeline


async def main(webcam: bool, intents: bool):
    recipe = Recipe.load(Path(__file__).resolve().parent.parent / CONFIG.recipe_path)
    state = State(recipe, dets=fake_dets(), session="active")    # no BOOT button / YOLO yet
    actions = None
    if intents:
        actions = IntentActions(IntentRouter(recipe), state, FakeAimer(state), FakeSteps(state))
    worker, _speech = build_pipeline(CONFIG, FakeAgent(), FakeFrames(0 if webcam else None), state,
                                     actions=actions)
    runner = WorkerRunner()
    await runner.add_workers(worker)
    await runner.run()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--webcam", action="store_true")
    p.add_argument("--no-intents", action="store_true")
    a = p.parse_args()
    asyncio.run(main(a.webcam, not a.no_intents))
