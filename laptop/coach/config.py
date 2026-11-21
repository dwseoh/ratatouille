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

    # Claude. Default = Opus 5.5 low effort + fast mode: it won the B0 bake-off (first sentence ~1.2 s median
    # vs ~2 s for Haiku and standard Opus; see docs). Set CLAUDE_MODEL=claude-haiku-5-5 CLAUDE_FAST=0 for the
    # cheap option (~76x cheaper per request, ~0.4 s slower).
    model: str = _env("CLAUDE_MODEL", "claude-opus-5-5")
    effort: str = _env("CLAUDE_EFFORT", "low")
    # Opus 5.5 "fast mode": same model, faster output, 2x price ($8/$40 per MTok). Ignored by other models.
    fast: bool = _env("CLAUDE_FAST", "1").lower() in ("1", "true", "yes")
    # B5: no first token (text or tool call) within this long -> speak the step's canned line. Opus fast is
    # usually under 1.5 s but has an occasional slow turn; Haiku is steadier, so 3 is enough there.
    first_token_timeout_s: float = float(_env("FIRST_TOKEN_TIMEOUT_S", "5.0"))
    # How often the coach speaks unprompted: off | gentle (45 s of silence) | normal (20 s). Users can say "talk less".
    checkins: str = _env("CHECKINS", "normal")
    # Accessibility: start in plain-language mode (short sentences, one action each, no idioms or numbers).
    # Users can also say "say it simpler" / "too fast" during the session.
    plain_language: bool = _env("PLAIN_LANGUAGE", "0").lower() in ("1", "true", "yes")
    watcher_model: str = _env("WATCHER_MODEL", "claude-haiku-5-5")   # B3: cheap model that reads frames in the background
    frames_per_turn: int = 3         # images sent with each voice turn
    frame_max_px: int = 512           # downscale before sending

    # Voice (V1). Deepgram streaming STT + Aura TTS through Pipecat.
    deepgram_api_key: str = _env("DEEPGRAM_API_KEY", "")
    tts_voice: str = _env("TTS_VOICE", "aura-2-helena-en")
    vad_stop_secs: float = float(_env("VAD_STOP_SECS", "0.2"))   # pause that triggers the smart-turn check
    turn_fallback_secs: float = float(_env("TURN_FALLBACK_SECS", "0.9"))  # silence that ends a turn when smart-turn isn't sure

    yolo_weights: str = _env("YOLO_WEIGHTS", "yolov8s-worldv2.pt")
    yolo_device: str = _env("YOLO_DEVICE", "mps")   # Apple GPU
    yolo_imgsz: int = int(_env("YOLO_IMGSZ", "640"))   # 480 is faster, 640 finds small things better
    recipe_path: str = _env("RECIPE", "recipes/guacamole.json")
    dashboard_port: int = int(_env("DASHBOARD_PORT", "8000"))
    laser_close_px: int = 10          # dot within this many px of the target = on target


CONFIG = Config()
