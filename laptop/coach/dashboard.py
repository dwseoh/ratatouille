"""Judge-facing dashboard (S2): what the coach sees, thinks and says, on one page. Runs in the same process.

    GET /         one HTML page (polls /state every 250 ms; works on a phone on the same hotspot)
    GET /state    JSON snapshot of State (see `snapshot`)
    GET /stream   MJPEG of the annotated camera feed (YOLO boxes, laser dot, target), at most 10 fps

Panels: live video, current step (big), captions of what the coach just said (large, high contrast: also an
accessibility feature), live transcript, speaking indicator, event log (incl. fallbacks and push-backs),
latency bars for the last turn, link status (camera, S3, battery).

    task = asyncio.create_task(serve(build_app(state, frames, link, agent), cfg.dashboard_port))
"""
from __future__ import annotations

import asyncio
import logging
import time

import cv2
import numpy as np

log = logging.getLogger(__name__)

STREAM_FPS = 10
EVENTS_SHOWN = 20


def annotate(frame, dets, dot, target_name):
    out = frame.copy()
    for d in dets:
        x1, y1, x2, y2 = map(int, d.box)
        color = (0, 200, 0) if d.name == target_name else (200, 200, 200)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        cv2.putText(out, f"{d.name} {d.conf:.2f}", (x1, y1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
    if dot is not None:
        cv2.circle(out, (int(dot[0]), int(dot[1])), 12, (0, 0, 255), 2)
    return out


def snapshot(state, frames=None, link=None, agent=None) -> dict:
    """Everything /state returns. Pure: only reads."""
    step = state.current_step
    status = getattr(link, "status", None)
    return {
        "session": state.session,
        "step": {"n": state.step + 1, "total": len(state.recipe.steps), "short": step.short, "goal": step.goal},
        "transcript": state.transcript,
        "speaking": state.speaking,
        "user_talking": state.user_talking,
        "captions": list(getattr(agent, "recent_sentences", [])),
        "laser": {"target": state.target, "on_target": state.on_target},
        "visible": sorted(state.visible()),
        "events": [{"t": e.t, "source": e.source, "text": e.text} for e in state.events[-EVENTS_SHOWN:]],
        "latency": state.turns[-1].ms() if state.turns else {},
        "links": {
            "camera": bool(getattr(frames, "connected", False)),
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


def build_app(state, frames=None, link=None, agent=None):
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, StreamingResponse

    app = FastAPI(title="Remy dashboard", docs_url=None, redoc_url=None)

    @app.get("/state")
    async def get_state():
        return snapshot(state, frames, link, agent)

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


async def serve(app, port: int) -> None:
    """Run the dashboard on the current loop. 0.0.0.0 so a judge's phone on the same hotspot can open it.
    Cancelling this stops the server cleanly (no traceback), even with a browser still streaming /stream."""
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning",
                                           timeout_graceful_shutdown=1))
    log.info("dashboard on http://0.0.0.0:%d", port)
    task = asyncio.ensure_future(server.serve())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=3)
        raise


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Remy: live dashboard</title>
<style>
  :root { --bg:#0e1116; --panel:#171c24; --line:#2a3340; --text:#f2f5f9; --dim:#98a4b3; --accent:#ffd447; --ok:#4cd07d; --bad:#ff6b6b; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:16px/1.4 -apple-system, system-ui, sans-serif; }
  header { display:flex; gap:16px; align-items:center; padding:12px 20px; border-bottom:1px solid var(--line); }
  header h1 { font-size:20px; margin:0; } .pill { padding:2px 10px; border-radius:99px; background:var(--panel); border:1px solid var(--line); font-size:13px; }
  .ok { color:var(--ok); } .bad { color:var(--bad); }
  main { display:grid; grid-template-columns: minmax(0,1.4fr) minmax(0,1fr); gap:16px; padding:16px 20px; }
  @media (max-width: 820px) { main { grid-template-columns: 1fr; } }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; margin-bottom:16px; }
  .panel h2 { margin:0 0 8px; font-size:12px; letter-spacing:.08em; text-transform:uppercase; color:var(--dim); }
  #video { width:100%; border-radius:8px; background:#000; display:block; }
  #step { font-size:30px; font-weight:700; } #goal { color:var(--dim); margin-top:4px; }
  #captions .line { font-size:34px; line-height:1.25; font-weight:600; color:var(--text); margin:6px 0; opacity:.55; }
  #captions .line:last-child { opacity:1; color:var(--accent); }
  #transcript { font-size:20px; min-height:28px; } .dot { display:inline-block; width:12px; height:12px; border-radius:50%; background:#444; margin-right:8px; }
  .dot.on { background:var(--ok); box-shadow:0 0 10px var(--ok); }
  #events { max-height:240px; overflow:auto; font-size:14px; } #events div { padding:3px 0; border-bottom:1px solid var(--line); }
  .src { display:inline-block; min-width:62px; color:var(--dim); } .src.vision{color:#6cb6ff} .src.agent{color:var(--accent)} .src.user{color:var(--ok)} .src.system{color:var(--bad)}
  .bar { display:flex; align-items:center; gap:10px; margin:6px 0; } .bar span.l { width:110px; color:var(--dim); font-size:14px; }
  .bar .track { flex:1; background:var(--line); height:12px; border-radius:6px; overflow:hidden; } .bar .fill { height:100%; background:var(--accent); width:0; transition:width .2s; }
  .bar span.v { width:70px; text-align:right; font-variant-numeric:tabular-nums; }
</style></head>
<body>
<header><h1>Remy</h1><span class="pill" id="session">idle</span><span class="pill" id="cam">camera</span>
  <span class="pill" id="s3">S3</span><span class="pill" id="bat"></span><span class="pill" id="conn">connecting…</span></header>
<main>
  <section>
    <div class="panel"><h2>What Remy sees</h2><img id="video" src="/stream" alt="live camera feed with detections">
      <div style="margin-top:8px;color:var(--dim)">Laser: <b id="laser">off</b> · Sees: <span id="visible">nothing</span></div></div>
    <div class="panel"><h2>Latency of the last turn</h2><div id="lat"></div></div>
  </section>
  <section>
    <div class="panel"><h2>Step</h2><div id="step">-</div><div id="goal"></div></div>
    <div class="panel" id="captions"><h2>Remy says</h2><div id="cap"></div></div>
    <div class="panel"><h2>You said</h2><div id="transcript"></div>
      <div style="margin-top:8px"><span class="dot" id="spk"></span><span id="spkl">quiet</span></div></div>
    <div class="panel"><h2>Event log</h2><div id="events"></div></div>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const LAT = [["first_token","First word"],["first_audio","First sound"],["point_called","Laser moved"],["on_target","On target"]];
function flag(el, ok, label) { el.textContent = label + (ok ? " ✓" : " ✗"); el.className = "pill " + (ok ? "ok" : "bad"); }
async function tick() {
  try {
    const s = await (await fetch("/state", {cache:"no-store"})).json();
    $("conn").textContent = "live"; $("conn").className = "pill ok";
    $("session").textContent = s.session === "active" ? "cooking" : "waiting for “hey Remy”";
    flag($("cam"), s.links.camera, "camera"); flag($("s3"), s.links.s3, "S3");
    $("bat").textContent = s.links.vbat_mv ? "battery " + (s.links.vbat_mv/1000).toFixed(2) + " V" : "";
    $("step").textContent = s.step.n + "/" + s.step.total + "  " + s.step.short; $("goal").textContent = s.step.goal;
    $("cap").innerHTML = s.captions.length ? s.captions.map(c => '<div class="line">' + esc(c) + '</div>').join("") : '<div class="line">…</div>';
    $("transcript").textContent = s.transcript || "";
    $("spk").className = "dot" + (s.speaking ? " on" : ""); $("spkl").textContent = s.speaking ? "Remy is speaking" : (s.user_talking ? "listening to you" : "quiet");
    $("laser").textContent = s.laser.target ? s.laser.target + (s.laser.on_target ? " (on target)" : " (moving)") : "off";
    $("visible").textContent = s.visible.join(", ") || "nothing";
    $("events").innerHTML = s.events.slice().reverse().map(e =>
      '<div><span class="src ' + e.source + '">' + e.source + '</span>' + esc(e.text) + '</div>').join("");
    $("lat").innerHTML = LAT.map(([k, label]) => {
      const ms = s.latency[k]; const pct = ms == null ? 0 : Math.min(100, ms / 20);
      return '<div class="bar"><span class="l">' + label + '</span><div class="track"><div class="fill" style="width:' + pct + '%"></div></div><span class="v">' + (ms == null ? "-" : ms + " ms") + '</span></div>';
    }).join("");
  } catch (e) { $("conn").textContent = "disconnected"; $("conn").className = "pill bad"; }
}
tick(); setInterval(tick, 250);
</script></body></html>
"""
