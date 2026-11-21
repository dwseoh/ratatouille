import pytest

from coach import telemetry


@pytest.fixture(autouse=True)
def _latency_log_in_tmp(tmp_path, monkeypatch):
    """Never write tests' turns into the real laptop/logs/latency.jsonl."""
    monkeypatch.setattr(telemetry, "DEFAULT_PATH", tmp_path / "latency.jsonl")
