"""Telemetry: one JSON line per settled turn in logs/latency.jsonl."""
import asyncio
import json
from pathlib import Path

from coach.contracts import (LAT_FIRST_AUDIO, LAT_FIRST_TOKEN, LAT_POINT, LAT_SPEECH_END, FakeAgent, FakeFrames,
                             Recipe, State, TurnLatency)
from coach import telemetry
from coach.telemetry import MAX_AGE_S, SETTLE_S, LatencyLog, summarize

RECIPE = Recipe.load(Path(__file__).resolve().parent.parent / "recipes" / "guacamole.json")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def turn(tid, t0, **ms):
    tl = TurnLatency(turn_id=tid)
    tl.marks[LAT_SPEECH_END] = t0
    for k, v in ms.items():
        tl.marks[k] = t0 + v / 1000
    return tl


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_turn_written_once_after_it_settles(tmp_path):
    clk, st = Clock(), State(RECIPE)
    log = LatencyLog(st, tmp_path / "l.jsonl", meta={"model": "m"}, clock=clk)
    st.turns.append(turn(1, clk.t, first_token=600, first_audio=900, point_called=-300))
    clk.t += 1.0
    assert log.poll() == 0                         # audio just started: on_target etc. may still come
    clk.t += SETTLE_S
    assert log.poll() == 1 and log.poll() == 0     # written exactly once
    (r,) = lines(tmp_path / "l.jsonl")
    assert r["turn"] == 1 and r["model"] == "m" and r["missing"] == []
    assert r["ms"] == {LAT_FIRST_TOKEN: 600, LAT_FIRST_AUDIO: 900, LAT_POINT: -300}


def test_newer_turn_or_age_settles_a_silent_turn(tmp_path):
    clk, st = Clock(), State(RECIPE)
    log = LatencyLog(st, tmp_path / "l.jsonl", clock=clk)
    st.turns.append(turn(1, clk.t))                  # STOP: no agent, no audio
    assert log.poll() == 0
    st.turns.append(turn(2, clk.t + 2))
    assert log.poll() == 1                           # turn 1: the user moved on
    clk.t += MAX_AGE_S + 3
    assert log.poll() == 1                           # turn 2: too old to get more marks
    rs = lines(tmp_path / "l.jsonl")
    assert [r["turn"] for r in rs] == [1, 2]
    assert rs[0]["missing"] == [LAT_FIRST_TOKEN, LAT_FIRST_AUDIO]


def test_flush_writes_the_rest(tmp_path):
    st = State(RECIPE)
    log = LatencyLog(st, tmp_path / "sub" / "l.jsonl")
    st.turns.append(turn(1, 0.0, first_audio=800))
    assert log.flush() == 1 and log.flush() == 0
    assert len(lines(tmp_path / "sub" / "l.jsonl")) == 1


def test_summary_uses_the_last_run():
    rows = [{"run": "a", "ms": {LAT_FIRST_AUDIO: 5000}},
            {"run": "b", "ms": {LAT_FIRST_AUDIO: 900, LAT_FIRST_TOKEN: 600}},
            {"run": "b", "ms": {LAT_FIRST_AUDIO: 1100}}]
    s = summarize(rows)
    assert "run b: 2 turns" in s and "median   1000 ms" in s and "5000" not in s


def test_main_app_logs_turns_on_shutdown():
    from coach import main as app_main
    from test_main import RecLink, args, linear_calib

    async def go():
        app = await app_main.App.create(args(), frames=FakeFrames(), link=RecLink(), calib=linear_calib(),
                                        agent=FakeAgent())
        app.state.turns.append(turn(1, 0.0, first_audio=700))
        await app.shutdown()

    asyncio.run(go())
    (r,) = lines(telemetry.DEFAULT_PATH)             # conftest: a tmp file, never the real logs/
    assert r["ms"] == {LAT_FIRST_AUDIO: 700} and "model" in r
