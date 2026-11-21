"""Config: the effort setting is COACH_EFFORT, and Claude Code's own CLAUDE_EFFORT must not leak in."""
import os
import subprocess
import sys
from pathlib import Path

LAPTOP = Path(__file__).resolve().parent.parent


def effort(**env) -> str:
    e = {**os.environ, **env}
    for k in ("COACH_EFFORT", "CLAUDE_EFFORT"):
        if k not in env:
            e.pop(k, None)
    out = subprocess.run([sys.executable, "-c", "from coach.config import CONFIG; print(CONFIG.effort)"],
                         cwd=LAPTOP, env=e, capture_output=True, text=True, check=True, timeout=30)
    return out.stdout.strip().splitlines()[-1]


def test_coach_effort_is_read():
    assert effort(COACH_EFFORT="high") == "high"


def test_claude_codes_shell_variable_is_ignored():
    assert effort(CLAUDE_EFFORT="medium") != "medium"          # falls back to .env / the default, not the shell
