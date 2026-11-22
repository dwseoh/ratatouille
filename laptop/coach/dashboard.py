"""Judge-facing dashboard (S2): what the coach sees, thinks and says, on one page. Runs in the same process.

    GET /         one HTML page (polls /state every 250 ms; works on a phone on the same hotspot)
    GET /state    JSON snapshot of State (see `snapshot`)
    GET /stream   MJPEG of the annotated camera feed (YOLO boxes, laser dot, target), at most 10 fps

Panels: live video, current step (big), captions of what the coach just said (large, high contrast: also an
accessibility feature), live transcript, speaking indicator, event log (incl. fallbacks and push-backs),
latency bars for the last turn, link status (camera, S3, battery), session (cooking / PAUSED), running timers,
the agent's cook notes and servings.

    task = asyncio.create_task(serve(build_app(state, frames, link, agent), cfg.dashboard_port))
"""
from __future__ import annotations

import asyncio
import logging
import time

import cv2
import numpy as np

from .recipe_images import RECIPES_DIR, placeholder_svg, step_image_path

try:                      # module level: FastAPI resolves `request: Request` annotations from here
    from fastapi import Request
except ImportError:       # the dashboard is optional; build_app() will say what is missing
    Request = None

log = logging.getLogger(__name__)

STREAM_FPS = 10
EVENTS_SHOWN = 20


def annotate(frame, dets, dot, target_name):
    """Boxes on the live feed. The laser's target gets a thick green box and a big label, so people who can't spot
    the little red dot can still see what Remy is pointing at."""
    out = frame.copy()
    for d in dets:
        x1, y1, x2, y2 = map(int, d.box)
        if d.name == target_name:
            continue                                            # drawn last, on top
        cv2.rectangle(out, (x1, y1), (x2, y2), (200, 200, 200), 2)
        cv2.putText(out, f"{d.name} {d.conf:.2f}", (x1, y1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
    for d in dets:
        if d.name != target_name:
            continue
        x1, y1, x2, y2 = map(int, d.box)
        cv2.rectangle(out, (x1 - 3, y1 - 3), (x2 + 3, y2 + 3), (0, 200, 0), 5)
        label = f"> {d.name}"
        (w, h), base = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.9, 2)
        top = max(y1 - h - base - 8, 0)
        cv2.rectangle(out, (x1 - 3, top), (x1 + w + 8, top + h + base + 6), (0, 140, 0), -1)
        cv2.putText(out, label, (x1 + 2, top + h + 2), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
    if dot is not None:
        cv2.circle(out, (int(dot[0]), int(dot[1])), 12, (0, 0, 255), 2)
    return out


def snapshot(state, frames=None, link=None, agent=None, captions=None, recipes_root=RECIPES_DIR) -> dict:
    """Everything /state returns. Pure: only reads. `captions` (CaptionTimeline) gives the sentence being spoken right
    now; without it (typing mode) the coach's latest sentence is shown."""
    if captions is not None:
        caption = captions.current()
    else:
        recent = list(getattr(agent, "recent_sentences", []))
        caption = recent[-1] if recent else ""
    step = state.current_step
    status = getattr(link, "status", None)
    shown = state.image_step                                   # the step whose picture is up (usually the current one)
    photo = step_image_path(state.recipe, shown, recipes_root)
    version = f"?v={int(photo.stat().st_mtime)}" if photo else ""
    now = time.monotonic()
    return {
        "session": state.session,
        "paused": state.session == "paused",
        "paused_for_s": round(now - state.paused_at) if state.paused_at is not None else 0,
        "timers": [{"label": n, "left_s": max(0, round(due - now))}
                   for n, due in sorted(state.timers.items(), key=lambda kv: kv[1])],
        "cook_notes": list(state.cook_notes),
        "servings": state.servings or state.recipe.servings,
        "step_seconds": round(state.step_seconds()),
        "step": {"n": state.step + 1, "total": len(state.recipe.steps), "short": step.short, "goal": step.goal,
                 "how": list(step.how), "tips": list(step.tips),
                 "image_url": f"/recipe-image/{shown}{version}",      # always set: photo, else the dish photo, else a card
                 "image_step": shown + 1, "image_real": photo is not None,
                 "image_note": (f"Showing step {shown + 1}: {state.recipe.steps[shown].short}"
                                if shown != state.step else "")},
        "recipe": state.recipe.name,
        "transcript": state.transcript,
        "speaking": state.speaking,
        "user_talking": state.user_talking,
        "caption": caption,
        "laser": {"target": state.target, "on_target": state.on_target},
        "visible": sorted(state.visible()),
        "events": [{"t": e.t, "source": e.source, "text": e.text} for e in state.events[-EVENTS_SHOWN:]],
        "latency": state.turns[-1].ms() if state.turns else {},
        "links": {
            "camera": bool(getattr(frames, "connected", False)),
            "camera_ignored": bool(getattr(agent, "camera_ignored", False)),
            "s3": bool(getattr(link, "online", False)),
            "vbat_mv": getattr(status, "vbat_mv", 0) or 0,
        },
        "now": time.time(),
    }


def _placeholder(text: str) -> np.ndarray:
    img = np.full((360, 640, 3), 30, dtype=np.uint8)
    cv2.putText(img, text, (150, 190), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (200, 200, 200), 2)
    return img


def _encode(img) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return buf.tobytes() if ok else b""


def build_app(state, frames=None, link=None, agent=None, captions=None, recipes_root=RECIPES_DIR):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse

    app = FastAPI(title="Remy dashboard", docs_url=None, redoc_url=None)
    app.state.port = 8000                               # serve() sets the real one

    @app.get("/state")
    async def get_state():
        return snapshot(state, frames, link, agent, captions, recipes_root)

    @app.get("/recipe-image/{n}")
    async def recipe_image(n: int):
        """The photo for step index n. The file is found server-side (never from a client-supplied path)."""
        if not 0 <= n < len(state.recipe.steps):
            raise HTTPException(status_code=404, detail="no such step")
        path = step_image_path(state.recipe, n, recipes_root)
        if path is None:                                # no photo and no dish photo: a plain card, never an empty hole
            return Response(placeholder_svg(state.recipe, n), media_type="image/svg+xml",
                            headers={"Cache-Control": "no-cache"})
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    @app.get("/cook", response_class=HTMLResponse)
    async def cook():
        """Phone page: the current step, its photo and how to do it, big and easy to read."""
        return COOK_PAGE

    def cook_url(request: Request) -> str:
        host = lan_ip() or request.url.hostname or "localhost"
        return f"http://{host}:{request.url.port or app.state.port}/cook"

    @app.get("/info")
    async def info(request: Request):
        return {"cook_url": cook_url(request), "qr": _qr_available()}

    @app.get("/cook-qr.svg")
    async def cook_qr(request: Request):
        """QR code that opens /cook on a phone on the same WiFi (needs the optional `segno` package)."""
        svg = _qr_svg(cook_url(request))
        if svg is None:
            raise HTTPException(status_code=404, detail="QR codes need: pip install segno")
        return Response(svg, media_type="image/svg+xml")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return PAGE

    async def mjpeg():
        period = 1.0 / STREAM_FPS
        while True:
            t0 = time.monotonic()
            frame = frames.latest()[0] if frames is not None else None
            img = _placeholder("no camera") if frame is None else annotate(frame, state.dets, state.dot, state.target)
            jpg = await asyncio.to_thread(_encode, img)       # imencode blocks a few ms: keep it off the loop
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
            await asyncio.sleep(max(0.0, period - (time.monotonic() - t0)))

    @app.get("/stream")
    async def stream():
        return StreamingResponse(mjpeg(), media_type="multipart/x-mixed-replace; boundary=frame")

    return app


def _qr_available() -> bool:
    try:
        import segno  # noqa: F401
        return True
    except ImportError:
        return False


def _qr_svg(url: str) -> bytes | None:
    try:
        import io

        import segno
    except ImportError:
        return None
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2, dark="#000000", light="#ffffff")
    return buf.getvalue()


def lan_ip() -> str | None:
    """This laptop's address on the local network (what a phone on the same WiFi types), or None if offline.
    A UDP "connect" picks the outgoing interface without sending any packet."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            ip = sock.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


async def serve(app, port: int) -> None:
    """Run the dashboard on the current loop. 0.0.0.0 so a judge's phone on the same hotspot can open it.
    Cancelling this stops the server cleanly (no traceback), even with a browser still streaming /stream."""
    import uvicorn
    app.state.port = port
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning",
                                           timeout_graceful_shutdown=1))
    ip = lan_ip()
    log.info("dashboard: http://localhost:%d%s", port, f"   (phone on the same WiFi: http://{ip}:{port})" if ip else "")
    task = asyncio.ensure_future(server.serve())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=3)
        raise


from .pages import COOK_PAGE  # noqa: E402
from .pages import DASHBOARD_PAGE as PAGE  # noqa: E402  (kept as dashboard.PAGE for tests and callers)
