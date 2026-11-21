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


def snapshot(state, frames=None, link=None, agent=None, captions=None) -> dict:
    """Everything /state returns. Pure: only reads. `captions` (CaptionTimeline) gives the sentence being spoken right
    now; without it (typing mode) the coach's latest sentence is shown."""
    if captions is not None:
        caption = captions.current()
    else:
        recent = list(getattr(agent, "recent_sentences", []))
        caption = recent[-1] if recent else ""
    step = state.current_step
    status = getattr(link, "status", None)
    return {
        "session": state.session,
        "step": {"n": state.step + 1, "total": len(state.recipe.steps), "short": step.short, "goal": step.goal},
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


def build_app(state, frames=None, link=None, agent=None, captions=None):
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, StreamingResponse

    app = FastAPI(title="Remy dashboard", docs_url=None, redoc_url=None)

    @app.get("/state")
    async def get_state():
        return snapshot(state, frames, link, agent, captions)

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


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Remy: live dashboard</title>
<script>
  // Apply the saved theme / font before the page paints (no flash). The link can force them: ?theme=light&font=dyslexic
  const OPTS = { theme: ["dark", "light"], font: ["default", "dyslexic"] };
  const store = { get(k) { try { return localStorage.getItem("remy-" + k); } catch (e) { return null; } },
                  set(k, v) { try { localStorage.setItem("remy-" + k, v); } catch (e) {} } };
  const params = new URLSearchParams(location.search);
  const pick = k => { for (const v of [params.get(k), store.get(k)]) if (OPTS[k].includes(v)) return v; return OPTS[k][0]; };
  function applyOption(k, v) { document.documentElement.dataset[k] = v; store.set(k, v);
    const b = document.getElementById(k === "theme" ? "optTheme" : "optFont");
    if (b) b.setAttribute("aria-pressed", String(v === OPTS[k][1])); }
  document.documentElement.dataset.theme = pick("theme"); document.documentElement.dataset.font = pick("font");
</script>
<style>
  /* Options (header buttons, remembered in this browser, or in the link: ?theme=light&font=dyslexic) */
  :root { --bg:#0e1116; --panel:#171c24; --line:#2a3340; --text:#f2f5f9; --dim:#98a4b3; --accent:#ffd447; --ok:#4cd07d; --bad:#ff6b6b; --vision:#6cb6ff;
          --font:-apple-system, system-ui, sans-serif; --track:#2a3340; }
  :root[data-theme="light"] { --bg:#f4f6f9; --panel:#ffffff; --line:#d3d9e2; --text:#16202c; --dim:#4f5d6e; --accent:#8f4a00; --ok:#17783b; --bad:#b3261e; --vision:#0b5cad;
          --track:#d3d9e2; }
  :root[data-font="dyslexic"] { --font:"OpenDyslexic", "Comic Sans MS", "Chalkboard SE", sans-serif; }
  :root[data-font="dyslexic"] body { letter-spacing:.03em; word-spacing:.1em; line-height:1.6; }
  @font-face { font-family:"OpenDyslexic"; font-weight:400; font-display:swap;
               src:url("https://cdn.jsdelivr.net/npm/@fontsource/opendyslexic@5.2.5/files/opendyslexic-latin-400-normal.woff2") format("woff2"); }
  @font-face { font-family:"OpenDyslexic"; font-weight:700; font-display:swap;
               src:url("https://cdn.jsdelivr.net/npm/@fontsource/opendyslexic@5.2.5/files/opendyslexic-latin-700-normal.woff2") format("woff2"); }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:16px/1.4 var(--font); }
  button { font:inherit; }
  header { display:flex; flex-wrap:wrap; gap:12px 16px; align-items:center; padding:12px 20px; border-bottom:1px solid var(--line); }
  .opts { margin-left:auto; display:flex; gap:8px; flex-wrap:wrap; }
  .opt { cursor:pointer; padding:4px 12px; border-radius:99px; border:1px solid var(--line); background:var(--panel); color:var(--text); font-size:13px; }
  .opt[aria-pressed="true"] { border-color:var(--accent); color:var(--accent); font-weight:700; }
  .opt:focus-visible { outline:3px solid var(--accent); outline-offset:2px; }
  header h1 { font-size:20px; margin:0; } .pill { padding:2px 10px; border-radius:99px; background:var(--panel); border:1px solid var(--line); font-size:13px; }
  .ok { color:var(--ok); } .bad { color:var(--bad); }
  main { display:grid; grid-template-columns: minmax(0,1.4fr) minmax(0,1fr); gap:16px; padding:16px 20px; }
  @media (max-width: 820px) { main { grid-template-columns: 1fr; } }
  /* wide screens: the right column (step, what you said, what Remy says) stays in view while the left one scrolls */
  @media (min-width: 821px) { main > section:last-child { position:sticky; top:8px; align-self:start; max-height:calc(100vh - 16px); overflow-y:auto; } }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; margin-bottom:16px; }
  .panel h2 { margin:0 0 8px; font-size:12px; letter-spacing:.08em; text-transform:uppercase; color:var(--dim); }
  #video { width:100%; border-radius:8px; background:#000; display:block; }
  #step { font-size:30px; font-weight:700; } #goal { color:var(--dim); margin-top:4px; }
  #captions .line { font-size:34px; line-height:1.25; font-weight:600; color:var(--accent); margin:2px 0; }
  #captions.idle .line { color:var(--text); opacity:.7; }
  #captions { max-height:42vh; overflow-y:auto; }
  #transcript { font-size:20px; min-height:28px; max-height:7.5em; overflow-y:auto; overflow-wrap:anywhere; } .dot { display:inline-block; width:12px; height:12px; border-radius:50%; background:var(--track); margin-right:8px; }
  .dot.on { background:var(--ok); box-shadow:0 0 10px var(--ok); }
  #events { max-height:240px; overflow:auto; font-size:14px; } #events div { padding:3px 0; border-bottom:1px solid var(--line); }
  .src { display:inline-block; min-width:62px; color:var(--dim); } .src.vision{color:var(--vision)} .src.agent{color:var(--accent)} .src.user{color:var(--ok)} .src.system{color:var(--bad)}
  .bar { display:flex; align-items:center; gap:10px; margin:6px 0; } .bar span.l { width:8.5em; flex:none; color:var(--dim); font-size:14px; }
  .bar .track { flex:1; background:var(--track); height:12px; border-radius:6px; overflow:hidden; } .bar .fill { height:100%; background:var(--accent); width:0; transition:width .2s; }
  .bar span.v { width:5.5em; flex:none; white-space:nowrap; text-align:right; font-variant-numeric:tabular-nums; }
</style></head>
<body>
<header><h1>Remy</h1><span class="pill" id="session">idle</span><span class="pill" id="cam">camera</span>
  <span class="pill" id="s3">S3</span><span class="pill" id="bat"></span><span class="pill" id="conn">connecting…</span>
  <span class="opts">
    <button class="opt" id="optTheme" aria-pressed="false" title="Switch between dark and light">Light mode</button>
    <button class="opt" id="optFont" aria-pressed="false" title="Show all text in the OpenDyslexic font">Dyslexia font</button>
  </span></header>
<main>
  <section>
    <div class="panel"><h2>What Remy sees</h2><img id="video" src="/stream" alt="live camera feed with detections">
      <div style="margin-top:8px;color:var(--dim)">Laser: <b id="laser">off</b> · Sees: <span id="visible">nothing</span></div></div>
    <div class="panel"><h2>Latency of the last turn</h2><div id="lat"></div></div>
  </section>
  <section>
    <div class="panel"><h2>Step</h2><div id="step">-</div><div id="goal"></div></div>
    <div class="panel"><h2>You said</h2><div id="transcript"></div>
      <div style="margin-top:8px"><span class="dot" id="spk"></span><span id="spkl">quiet</span></div></div>
    <div class="panel" id="captions"><h2>Remy says</h2><div id="cap"></div></div>
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
    if (s.links.camera_ignored) { $("cam").textContent = "camera ignored"; $("cam").className = "pill"; }
    $("bat").textContent = s.links.vbat_mv ? "battery " + (s.links.vbat_mv/1000).toFixed(2) + " V" : "";
    $("step").textContent = s.step.n + "/" + s.step.total + "  " + s.step.short; $("goal").textContent = s.step.goal;
    $("cap").innerHTML = '<div class="line">' + (s.caption ? esc(s.caption) : "…") + '</div>';
    $("captions").className = "panel" + (s.speaking ? "" : " idle");
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
for (const [k, id] of [["theme", "optTheme"], ["font", "optFont"]]) {
  const b = $(id), on = OPTS[k][1];
  b.setAttribute("aria-pressed", String(document.documentElement.dataset[k] === on));
  b.addEventListener("click", () => applyOption(k, document.documentElement.dataset[k] === on ? OPTS[k][0] : on));
}
tick(); setInterval(tick, 250);
</script></body></html>
"""
