"""Claude agent for voice turns: streams text sentence by sentence, calls local tools.

Latency notes (see docs/architecture.md):
- the system prompt and recipe are a stable, cached prefix
- only 2-3 small frames per turn
- tool results are deferred to the next user turn, so tools never cost a second round trip
"""
import base64
import json
import re
import time

import anthropic
import cv2

SYSTEM = """You are a hands-free cooking coach speaking through an earpiece while the user cooks.
You see what a chest camera sees. Reply in 1-2 short spoken sentences, no lists or markdown.
Say your sentence first, then call tools. Use point_at whenever an object you mention is visible.
Only advance steps when the frames confirm the step is really done."""

TOOLS = [
    {
        "name": "point_at",
        "description": "Point the laser at a visible object. name must be one of the recipe objects.",
        "eager_input_streaming": True,
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}},
                         "required": ["name"], "additionalProperties": False},
    },
    {
        "name": "next_step",
        "description": "Advance to the next recipe step once the current one is verified done.",
        "eager_input_streaming": True,
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "set_timer",
        "description": "Start a countdown timer.",
        "eager_input_streaming": True,
        "input_schema": {"type": "object",
                         "properties": {"seconds": {"type": "integer"}, "label": {"type": "string"}},
                         "required": ["seconds", "label"], "additionalProperties": False},
    },
    {
        "name": "show_text",
        "description": "Show a short status line (max 20 chars) on the wearable's display.",
        "eager_input_streaming": True,
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}},
                         "required": ["text"], "additionalProperties": False},
    },
]

_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def jpeg_block(frame, max_px: int) -> dict:
    h, w = frame.shape[:2]
    s = max_px / max(h, w)
    if s < 1:
        frame = cv2.resize(frame, (int(w * s), int(h * s)))
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buf.tobytes()).decode()}}


class Agent:
    def __init__(self, cfg, recipe: dict, tool_handlers: dict):
        self.cfg = cfg
        self.client = anthropic.AsyncAnthropic()  # one client for the app's lifetime (keep-alive)
        self.system = [{"type": "text", "text": SYSTEM + "\n\nRECIPE:\n" + json.dumps(recipe),
                        "cache_control": {"type": "ephemeral"}}]
        self.handlers = tool_handlers   # name -> callable(**input) -> str
        self.messages: list = []
        self._pending_results: list = []  # tool_results deferred to the next user turn

    async def turn(self, user_text: str, frames: list, state_note: str, on_sentence):
        """Run one voice turn. on_sentence(str) is awaited for each complete sentence (feed TTS)."""
        content = list(self._pending_results)
        self._pending_results = []
        content += [jpeg_block(f, self.cfg.frame_max_px) for f in frames]
        content.append({"type": "text", "text": f"[{state_note}]\nUser: {user_text}"})
        self.messages.append({"role": "user", "content": content})

        t0, first_token_at, buf = time.time(), None, ""
        async with self.client.beta.messages.stream(
            model=self.cfg.model,
            max_tokens=4096,  # deliberately short spoken replies
            output_config={"effort": self.cfg.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # re-runs on a fallback model if this one declines
            system=self.system,
            tools=TOOLS,
            messages=self.messages,
        ) as stream:
            async for event in stream:
                if event.type == "text":
                    first_token_at = first_token_at or time.time()
                    buf += event.text
                    parts = _SENTENCE_END.split(buf)
                    for s in parts[:-1]:
                        await on_sentence(s.strip())
                    buf = parts[-1]
            final = await stream.get_final_message()
        if buf.strip():
            await on_sentence(buf.strip())

        self.messages.append({"role": "assistant", "content": final.content})
        if final.stop_reason in ("refusal", "max_tokens"):
            return {"ttft": (first_token_at or time.time()) - t0, "stop": final.stop_reason}
        for block in final.content:
            if block.type == "tool_use":
                self._pending_results.append(self._run_tool(block))
        return {"ttft": (first_token_at or time.time()) - t0, "stop": final.stop_reason}

    def _run_tool(self, block) -> dict:
        try:
            if not isinstance(block.input, dict):
                raise ValueError("bad input")
            out = self.handlers[block.name](**block.input)
            return {"type": "tool_result", "tool_use_id": block.id, "content": str(out or "ok")}
        except Exception as e:  # report the failure back instead of dropping the result
            return {"type": "tool_result", "tool_use_id": block.id, "content": f"error: {e}", "is_error": True}

    async def prewarm(self):
        """TODO: pre-warm the prompt cache at startup (max_tokens=0 request; see the claude-api docs)."""

    # TODO: trim history per recipe step (start a fresh conversation with a short summary)
    #       so requests stay small; images from old turns are the main cost.
