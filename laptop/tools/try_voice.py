"""Talk to the voice pipeline with the echo agent (FakeAgent), intent router and session.

    cd laptop && python -m tools.try_voice            # black frames
    cd laptop && python -m tools.try_voice --webcam   # real webcam frames for the agent
    cd laptop && python -m tools.try_voice --no-intents

Starts IDLE: say "hey Remy" -> rising chime, then the (echo) greeting. Say "start over" -> reset to idle
(falling chime). While active, say something: it answers "You said: ...". Use headphones (no echo gate yet).
Intents: "where's the green one" prints "[laser] -> lime" (FakeAimer, fake detections: avocado, lime,
bowl, knife, cutting board), "ok I'm done" advances FakeSteps, "stop" cuts the audio.
"""
import argparse
import asyncio
from pathlib import Path

from pipecat.workers.runner import WorkerRunner

from coach.config import CONFIG
from coach.contracts import FakeAgent, FakeAimer, FakeFrames, FakeLink, FakeSteps, Recipe, State, fake_dets
from coach.intents import IntentActions, IntentRouter
from coach.session import Session
from coach.voice import build_pipeline


async def main(webcam: bool, intents: bool):
    recipe = Recipe.load(Path(__file__).resolve().parent.parent / CONFIG.recipe_path)
    state = State(recipe, dets=fake_dets())         # no YOLO yet: fixed detections
    agent, aimer, steps, link = FakeAgent(), FakeAimer(state), FakeSteps(state), FakeLink()
    actions = IntentActions(IntentRouter(recipe), state, aimer, steps) if intents else None
    worker, speech = build_pipeline(CONFIG, agent, FakeFrames(0 if webcam else None), state, actions=actions)

    session = Session(state, speech, agent, aimer, steps, link)
    if actions:
        actions.session = session
    print('\n>>> say "hey Remy" to start, "start over" to reset\n', flush=True)

    runner = WorkerRunner()
    await runner.add_workers(worker)
    await runner.run()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--webcam", action="store_true")
    p.add_argument("--no-intents", action="store_true")
    a = p.parse_args()
    asyncio.run(main(a.webcam, not a.no_intents))
