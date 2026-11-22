import pytest

from coach import telemetry


@pytest.fixture(autouse=True)
def _latency_log_in_tmp(tmp_path, monkeypatch):
    """Never write tests' turns into the real laptop/logs/latency.jsonl."""
    monkeypatch.setattr(telemetry, "DEFAULT_PATH", tmp_path / "latency.jsonl")


@pytest.fixture(autouse=True)
def _no_frame_size_from_other_tests(monkeypatch):
    """frames.last_frame_size() is process-wide (the Aimer reads it): don't let one test's camera leak into the next."""
    from coach import frames
    monkeypatch.setattr(frames, "_last_size", None)
