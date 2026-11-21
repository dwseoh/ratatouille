"""Entry point: wires the three loops together (pointing, voice, watcher)."""
import asyncio
import json
import time
from pathlib import Path

from .agent import Agent
from .aim import Aimer, Calibration
from .config import CONFIG as cfg
from .control import ControlLink
from .frames import FrameGrabber
from .vision import Detector, find_red_dot
from .voice import Speaker, Transcriber, keyword_target


# TODO(integration): replace this State with contracts.State (single writer per field).
class State:
    def __init__(self, recipe):
        self.recipe = recipe
        self.step = 0
        self.events: list[str] = []
        self.dets = []
        self.dot = None
        self.latency: dict = {}

    def note(self) -> str:
        step = self.recipe["steps"][self.step]["say"]
        return f"step {self.step + 1}/{len(self.recipe['steps'])}: {step} | recent: {'; '.join(self.events[-5:])}"


async def pointing_loop(frames, detector, aimer, st):
    """Loop 1: ~20-30 Hz, all local."""
    loop = asyncio.get_running_loop()
    while True:
        frame, _ = frames.latest()
        if frame is None:
            await asyncio.sleep(0.05); continue
        st.dets = await loop.run_in_executor(None, detector.detect, frame)
        st.dot = find_red_dot(frame, st.dot)
        tgt = next((d for d in st.dets if d.name == aimer.target_name), None)
        aimer.step(tgt.center if tgt else None, st.dot)
        await asyncio.sleep(0)


async def voice_loop(frames, agent, aimer, speaker, link, st):
    """Loop 2: speech -> Claude -> speech, everything streamed."""
    objects = st.recipe["objects"]
    finals: asyncio.Queue[str] = asyncio.Queue()

    def on_partial(text):
        name = keyword_target(text, objects)
        if name and name != aimer.target_name:
            aimer.set_target(name)          # point-first: laser moves before Claude answers

    def on_speech_start():
        speaker.stop()                      # barge-in

    stt = Transcriber(on_partial, finals.put_nowait, on_speech_start)
    asyncio.create_task(stt.run())
    while True:
        text = await finals.get()
        t_end = time.time()
        await speaker.earcon()
        link.show_text(3, "thinking...")
        recent = [f for _, f in list(frames.history)[-cfg.frames_per_turn:]]
        stats = await agent.turn(text, recent, st.note(), speaker.say)
        st.latency = {"ttft_s": round(stats["ttft"], 2), "total_s": round(time.time() - t_end, 2)}
        link.show_text(3, f"ttft {st.latency['ttft_s']}s")


async def watcher_loop(frames, st):
    """Loop 3: TODO. Every 3-5 s send 3-4 timestamped frames + event log; append new events."""
    while True:
        await asyncio.sleep(4)


async def main():
    recipe = json.loads(Path(cfg.recipe_path).read_text())
    st = State(recipe)
    frames = FrameGrabber(cfg.cam_url).start()
    link = await ControlLink(cfg.ctl_host, cfg.ctl_port, cfg.control_hz).start()
    calib = Calibration.load("calibration.json") if Path("calibration.json").exists() else None
    aimer = Aimer(link, calib, close_px=cfg.laser_close_px)
    detector = Detector(cfg.yolo_weights, cfg.yolo_device, recipe["objects"])
    speaker = Speaker()

    def next_step():
        st.step = min(st.step + 1, len(recipe["steps"]) - 1)
        link.show_text(2, f"Step {st.step + 1}: {recipe['steps'][st.step]['short']}")
        return st.note()

    handlers = {
        "point_at": lambda name: aimer.set_target(name) or f"pointing at {name}",
        "next_step": next_step,
        "set_timer": lambda seconds, label: f"TODO timer {label} {seconds}s",
        "show_text": lambda text: link.show_text(4, text) or "shown",
    }
    agent = Agent(cfg, recipe, handlers)
    await agent.prewarm()
    link.show_text(0, "cooking-coach ready")

    await asyncio.gather(
        pointing_loop(frames, detector, aimer, st),
        voice_loop(frames, agent, aimer, speaker, link, st),
        watcher_loop(frames, st),
        speaker.run(),
    )


if __name__ == "__main__":
    asyncio.run(main())
