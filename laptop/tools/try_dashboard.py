"""Look at the dashboard with fake data (no hardware, no API key).

    cd laptop && python -m tools.try_dashboard            # black frames + fake detections
    python -m tools.try_dashboard --cam 0                 # your webcam
Then open http://localhost:8000 (or http://<this-laptop-ip>:8000 from a phone on the same WiFi).
The fake coach "talks", the step advances every 8 s, so you can see every panel change.
"""
import argparse
import asyncio
import logging
import time
from pathlib import Path

from coach import dashboard
from coach.config import CONFIG as cfg
from coach.contracts import FakeFrames, Recipe, State, TurnLatency, fake_dets

LINES = ["Lay the avocado flat on the board.", "Push the knife in until it hits the pit.",
         "Keep your fingers curled away from the blade.", "Nice, that looks like two clean halves."]


class FakeLink:
    online = True
    status = type("S", (), {"vbat_mv": 5120})()


class FakeAgent:
    def __init__(self):
        from collections import deque
        self.recent_sentences = deque(maxlen=3)


async def drive(st: State, agent: FakeAgent) -> None:
    i = 0
    st.session = "active"
    while True:
        i += 1
        agent.recent_sentences.append(LINES[i % len(LINES)])
        st.speaking = True
        st.transcript = ["where do I start?", "where's the lime?", "ok I'm done", "how chunky should it be?"][i % 4]
        st.target = ["avocado", "knife", "lime", "bowl"][i % 4]
        st.on_target = i % 2 == 0
        st.log(["user", "vision", "agent", "system"][i % 4], f"example event {i}")
        t = TurnLatency(i)
        t.marks.update(speech_end=1.0, first_token=1.0 + 0.6 + (i % 3) * 0.2, first_audio=1.0 + 0.9 + (i % 3) * 0.2,
                       point_called=1.2)
        st.turns.append(t)
        if i % 4 == 0:
            st.step = (st.step + 1) % len(st.recipe.steps)
        await asyncio.sleep(2)
        st.speaking = False
        await asyncio.sleep(2)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")     # shows the dashboard URL(s)
    recipe = Recipe.load(Path(__file__).resolve().parent.parent / cfg.recipe_path)
    st = State(recipe, dets=fake_dets(tuple(recipe.objects[:5])))
    agent = FakeAgent()
    frames = FakeFrames(int(args.cam) if args.cam and args.cam.isdigit() else args.cam)
    st.dot = (210.0, 160.0)
    app = dashboard.build_app(st, frames, FakeLink(), agent)
    await asyncio.gather(drive(st, agent), dashboard.serve(app, cfg.dashboard_port))


if __name__ == "__main__":
    asyncio.run(main())
