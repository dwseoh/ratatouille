"""Latency telemetry (B, S1 / contracts C9): one JSON line per user turn in logs/latency.jsonl.

Each `TurnLatency` in State.turns (created by voice.AgentProcessor at the end of a user turn) collects
marks from several owners: speech_end + first_audio (B, voice), first_token (C, agent), point_called
(B intents / C agent tool), on_target (A, closed loop). They arrive at different times, so a turn is
written once it has settled:
  - SETTLE_S after its first audio (late marks like on_target have landed by then), or
  - a newer turn exists (the user moved on), or
  - MAX_AGE_S after speech end with no audio (STOP / start over / a failed turn), or
  - at shutdown (`flush()`).

One line:
  {"run": "2026-11-21T14:03:11", "turn": 3, "t": 1763749391.2, "model": "claude-opus-5-5",
   "ms": {"first_token": 640, "first_audio": 910, "point_called": -350}, "missing": []}
`ms` is milliseconds after speech end (negative = before it: the laser moved while the user was still
talking). `missing` lists the v1 marks this turn didn't get. The TFT line 3 readout is main.tft_latency_loop.

Quick summary of a run:  python -m coach.telemetry [logs/latency.jsonl]
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

from loguru import logger

from .contracts import LAT_FIRST_AUDIO, LAT_FIRST_TOKEN, LAT_POINT, LAT_SPEECH_END, State, TurnLatency

SETTLE_S = 1.5
MAX_AGE_S = 20.0
V1_MARKS = (LAT_SPEECH_END, LAT_FIRST_TOKEN, LAT_FIRST_AUDIO)   # point_called only on "where's X" turns
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "logs" / "latency.jsonl"


class LatencyLog:
    def __init__(self, state: State, path: Path | str | None = None, *, meta: dict | None = None,
                 clock=time.monotonic):
        self.state, self.meta, self._clock = state, dict(meta or {}), clock
        self.path = Path(path) if path is not None else DEFAULT_PATH
        self.run_id = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._written: set[int] = set()

    def poll(self) -> int:
        """Write every settled turn not written yet. Returns how many were written."""
        now, n = self._clock(), 0
        turns = list(self.state.turns)
        for i, turn in enumerate(turns):
            if turn.turn_id in self._written:
                continue
            newer = i < len(turns) - 1
            audio = turn.marks.get(LAT_FIRST_AUDIO)
            start = turn.marks.get(LAT_SPEECH_END, now)
            if newer or (audio is not None and now - audio >= SETTLE_S) or now - start >= MAX_AGE_S:
                self._write(turn)
                n += 1
        return n

    def flush(self) -> int:
        """Write everything left (shutdown)."""
        n = 0
        for turn in list(self.state.turns):
            if turn.turn_id not in self._written:
                self._write(turn)
                n += 1
        return n

    async def run(self, every: float = 0.5) -> None:
        while True:
            await asyncio.sleep(every)
            try:
                self.poll()
            except Exception:
                logger.exception("latency log write failed")

    def record(self, turn: TurnLatency) -> dict:
        return {"run": self.run_id, "turn": turn.turn_id, "t": round(time.time(), 1), **self.meta,
                "ms": turn.ms(), "missing": [k for k in V1_MARKS if k not in turn.marks]}

    def _write(self, turn: TurnLatency) -> None:
        self._written.add(turn.turn_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(self.record(turn)) + "\n")


def summarize(lines: list[dict]) -> str:
    """Median / p90 / max per mark, for the last run in the file."""
    if not lines:
        return "no turns logged"
    run = lines[-1]["run"]
    rows = [r for r in lines if r["run"] == run]
    out = [f"run {run}: {len(rows)} turns"]
    for key in (LAT_FIRST_TOKEN, LAT_FIRST_AUDIO, LAT_POINT):
        vals = sorted(r["ms"][key] for r in rows if key in r["ms"])
        if vals:
            p90 = vals[min(len(vals) - 1, int(0.9 * len(vals)))]
            out.append(f"  {key:13} n={len(vals):3}  median {statistics.median(vals):6.0f} ms  "
                       f"p90 {p90:6} ms  max {vals[-1]:6} ms")
    return "\n".join(out)


if __name__ == "__main__":
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    print(summarize([json.loads(x) for x in p.read_text().splitlines() if x.strip()]))
