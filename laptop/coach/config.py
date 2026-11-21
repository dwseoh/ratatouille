"""Settings, overridable from environment / .env."""
import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Config:
    cam_url: str = _env("CAM_URL", "http://coach-cam.local:81/stream")
    ctl_host: str = _env("CTL_HOST", "coach-ctl.local")
    ctl_port: int = int(_env("CTL_PORT", "4210"))
    control_hz: float = 30.0

    # Claude. Measure latency of these against each other on day 1 (see docs/architecture.md).
    model: str = _env("CLAUDE_MODEL", "claude-opus-5-5")
    effort: str = _env("CLAUDE_EFFORT", "low")
    frames_per_turn: int = 3          # images sent with each voice turn
    frame_max_px: int = 512           # downscale before sending

    yolo_weights: str = _env("YOLO_WEIGHTS", "yolov8s-worldv2.pt")
    yolo_device: str = _env("YOLO_DEVICE", "mps")   # M2 GPU
    recipe_path: str = _env("RECIPE", "recipes/guacamole.json")
    dashboard_port: int = int(_env("DASHBOARD_PORT", "8000"))
    laser_close_px: int = 10          # dot within this many px of the target = on target


CONFIG = Config()
