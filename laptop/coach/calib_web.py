"""Calibration in the browser: python -m coach.calib_web   (run from laptop/, then open http://127.0.0.1:8001)

Same steps as coach.calibrate, same calibration.json: live camera frame, the servos visit the grid (5x5, each point
approached from below), you click the laser dot on the image (s skip, r redo, t test mode, Save). A click near the dot
snaps to its centre; clicks only count once the servos stopped and a newer frame is on screen. "Auto" runs
calibrate --auto's dot finder + blink check instead of clicks. "Verify" sends the laser to random spots and measures
where the dot lands (click it, or "Auto verify" finds it), printing the error in degrees and cm; "Add verify points"
refits with them. All the logic (grid, session, fit + save gate, auto sweep, verify) comes from calibrate.py.

    python -m coach.calib_web --cam black --no-s3          # no hardware: black frames, in-process FakeLink
    python -m coach.calib_web --cam 0 --ctl 127.0.0.1      # webcam + tools/fake_s3.py
    python -m coach.calib_web                              # ESP cam + S3 (CAM_URL / CTL_HOST)

For C's dashboard (one FastAPI app, same event loop as the link):
    from coach.calib_web import mount
    calib = mount(app, link, frames, prefix="/calib")      # page at /calib/
    asyncio.create_task(calib.watchdog())                   # from startup: laser off when the page goes away
The calibration owns the laser while a run is active, so don't point_at() at the same time.
"""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import Callable

import cv2
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from .aim import Calibration
from .calibrate import (
    AUTO_SETTLE_S,
    DEFAULT_N,
    DEFAULT_OUT,
    SETTLE_S,
    VERIFY_N,
    CalibSession,
    Mover,
    VerifySession,
    add_grid_args,
    aim_at_pixel,
    auto_sweep,
    auto_verify,
    draw_points,
    finish,
    frame_fresh,
    frame_size,
    make_grid,
    snap_to_dot,
)
from .contracts import ControlLinkAPI, FrameSource
from .dot import LAG_S

PAGE_TIMEOUT_S = 3.0    # no /state poll for this long (tab closed) -> laser off, run stopped
LOG_LINES = 60
JPEG_QUALITY = 80


class GridIn(BaseModel):
    pan_min: float | None = None
    pan_max: float | None = None
    tilt_min: float | None = None
    tilt_max: float | None = None
    n: int | None = None


class PointIn(BaseModel):
    x: float
    y: float


class CalibWeb:
    """Web calibration state. Call every method from the event loop that runs the link (the request handlers do)."""

    def __init__(self, link: ControlLinkAPI, frames: FrameSource, out: Path | str = DEFAULT_OUT,
                 grid: dict | None = None, settle_s: float = SETTLE_S, auto_settle_s: float = AUTO_SETTLE_S,
                 lag_s: float = LAG_S, clock: Callable[[], float] = time.monotonic):
        self.link, self.frames, self.out = link, frames, Path(out)
        self.grid = {"pan_min": 60.0, "pan_max": 120.0, "tilt_min": 30.0, "tilt_max": 90.0, "n": DEFAULT_N,
                     **(grid or {})}
        self.settle_s, self.auto_settle_s, self.lag_s, self.clock = settle_s, auto_settle_s, lag_s, clock
        self.calib = Calibration.load(self.out) if self.out.exists() else None
        self.sess: CalibSession | None = None
        self.verify: VerifySession | None = None
        self.mover = Mover(link, clock=clock)
        self.mode = "idle"              # idle | click | test | auto | verify | auto_verify
        self.saved = False
        self.lines: list[str] = []
        self.moved_at = 0.0
        self.seen_at = clock()
        self._auto: asyncio.Task | None = None

    # ── actions ─────────────────────────────────────────────────────────────
    def log(self, line: str) -> None:
        self.lines = (self.lines + [line])[-LOG_LINES:]

    def _grid(self, g: GridIn | None) -> list[tuple[float, float]]:
        if g is not None:
            self.grid.update({k: v for k, v in g.model_dump().items() if v is not None})
        d = self.grid
        if int(d["n"]) < 2:
            raise ValueError("n must be at least 2")
        return make_grid((d["pan_min"], d["pan_max"]), (d["tilt_min"], d["tilt_max"]), int(d["n"]))

    def start(self, g: GridIn | None = None) -> None:
        """New click run: laser on, servos to the first grid point."""
        self._cancel_auto()
        self.sess, self.mode, self.saved = CalibSession(self._grid(g)), "click", False
        self.log(f"click calibration: {len(self.sess.grid)} points")
        self.link.laser = True
        self._move()

    def _move(self) -> None:
        if self.mode == "verify":
            goal = self.verify.current()[1] if self.verify and not self.verify.done else None
        else:
            goal = self.sess.current() if self.sess else None
        if goal is not None:
            self.mover.go(goal)         # from below on both axes, like the Aimer (servo backlash)
            self.moved_at = self.clock()

    @property
    def settled(self) -> bool:
        """Servos stopped (ControlLink's ramp done) + settle time passed + a frame newer than that arrived."""
        return self.mover.settled(self.settle_s) and frame_fresh(self.frames, self.mover, self.settle_s)

    def _snap(self, x: float, y: float) -> tuple[float, float, str]:
        x, y, snapped = snap_to_dot(self.frames.latest()[0], x, y)
        return x, y, " (snapped to the dot)" if snapped else ""

    def click(self, x: float, y: float) -> None:
        if self.mode == "test":
            self.test(x, y)
            return
        if self.mode == "verify":
            self._verify_click(x, y)
            return
        self._need_click_mode()
        if self.sess.done:
            raise ValueError("grid done: save, redo or start again")
        if not self.settled:
            raise ValueError("still settling, click again")
        goal = self.sess.current()
        x, y, tag = self._snap(x, y)
        self.sess.click(x, y)
        self.log(f"point {self.sess.i}/{len(self.sess.grid)}: ({x:.1f},{y:.1f}) -> {goal}{tag}")
        self._advance()

    def skip(self) -> None:
        if self.mode == "verify" and self.verify is not None:
            self.verify.skip()
            self._verify_next()
            return
        self._need_click_mode()
        self.sess.skip()
        self._advance()

    def redo(self) -> None:
        self._need_click_mode()
        self.sess.redo()
        self.saved = False
        self._move()

    def _advance(self) -> None:
        """Next grid point, or save once the grid is done (like the CLI)."""
        self._move()
        if self.sess.done and not self.saved:
            self.save()

    def save(self) -> bool:
        """finish() from calibrate.py: fit, save calibration.json, report. False if too few points."""
        if self.sess is None or self.mode in ("auto", "auto_verify"):
            raise ValueError("nothing to save")
        calib = finish(self.sess, self.out, self.log, frame=frame_size(self.frames))
        self.saved = calib is not None
        self.calib = calib or self.calib
        return self.saved

    def toggle_test(self) -> None:
        if self.mode in ("auto", "auto_verify"):
            raise ValueError("auto run in progress")
        if self.mode == "test":
            self.mode = "click" if self.sess else "idle"
            self.link.laser = self.mode == "click"
            self._move()
        else:
            if self.calib is None:
                raise ValueError("no calibration yet")
            self.mode = "test"
            self.link.laser = True
        self.log(f"test mode {'on' if self.mode == 'test' else 'off'}")

    def test(self, x: float, y: float) -> None:
        p, t = aim_at_pixel(self.calib, self.link, (x, y), self.mover)
        self.log(f"test: ({x:.0f},{y:.0f}) -> pan {p:.1f} tilt {t:.1f}")

    # ── verify ──────────────────────────────────────────────────────────────
    def start_verify(self, n: int = VERIFY_N) -> None:
        """The laser visits n random reachable spots; click where the dot really is (s if it isn't visible)."""
        if self.mode in ("auto", "auto_verify"):
            raise ValueError("auto run in progress")
        if self.calib is None:
            raise ValueError("no calibration yet")
        self.verify = VerifySession(self.calib, n, frame=frame_size(self.frames))
        if not self.verify.targets:
            raise ValueError("no reachable spots to verify (check PAN_*/TILT_* in .env)")
        self.mode = "verify"
        self.link.laser = True
        self.log(f"verify: {len(self.verify.targets)} random spots. Click the dot each time (s if not visible).")
        self._move()

    def _verify_click(self, x: float, y: float) -> None:
        if self.verify is None or self.verify.done:
            raise ValueError("verify done")
        if not self.settled:
            raise ValueError("still settling, click again")
        x, y, tag = self._snap(x, y)
        (tx, ty), _ = self.verify.current()
        self.verify.record((x, y))
        self.log(f"verify {self.verify.i}/{len(self.verify.targets)}: sent to ({tx:.0f},{ty:.0f}), dot at "
                 f"({x:.1f},{y:.1f}){tag}")
        self._verify_next()

    def _verify_next(self) -> None:
        if self.verify.done:
            for ln in self.verify.summary()[0][-1:]:
                self.log(ln)
            self.mode = "click" if self.sess else "idle"
            self.link.laser = False
        else:
            self._move()

    def start_auto_verify(self, n: int = VERIFY_N) -> None:
        """Verify with the dot finder + blink check in the background."""
        self.start_verify(n)
        self.mode = "auto_verify"
        self._auto = asyncio.create_task(self._run_auto_verify(self.verify))

    async def _run_auto_verify(self, vs: VerifySession) -> None:
        try:
            await auto_verify(self.link, self.frames, vs, settle_s=self.auto_settle_s, lag_s=self.lag_s,
                              log=self.log, mover=self.mover)
        except asyncio.CancelledError:
            self.log("auto verify stopped")
            raise
        except Exception as e:
            self.log(f"auto verify failed: {e!r}")
        finally:
            self.link.laser = False
            if self.mode == "auto_verify":
                self.mode = "click" if self.sess else "idle"

    def add_verify(self) -> None:
        """Refit with the verify measurements added to the calibration points (they're real points too)."""
        if self.verify is None or not self.verify.done:
            raise ValueError("run a verify first")
        if self.sess is None:
            self.sess = _session_from(self.calib)
        n = self.verify.add_to(self.sess)
        self.log(f"added {n} verify points, refitting")
        self.verify = None
        self.mode = "click"
        self.save()

    def start_auto(self, g: GridIn | None = None) -> None:
        """calibrate --auto in the background; the page shows its progress."""
        if self.mode == "auto_verify":
            raise ValueError("auto verify in progress")
        self._cancel_auto()
        grid = self._grid(g)
        self.sess, self.mode, self.saved = CalibSession(grid), "auto", False
        self.log(f"auto calibration: {len(grid)} points, keep the counter clear of people")
        self._auto = asyncio.create_task(self._run_auto(self.sess))

    async def _run_auto(self, sess: CalibSession) -> None:
        try:
            await auto_sweep(self.link, self.frames, sess.grid, settle_s=self.auto_settle_s, lag_s=self.lag_s,
                             log=self.log, sess=sess, mover=self.mover)
            self.link.laser = False
            self.mode = "click"         # redo / skip / test work on the result, like after a click run
            self.save()
        except asyncio.CancelledError:
            self.log("auto calibration stopped")
            raise
        except Exception as e:          # keep the server up, show it on the page
            self.log(f"auto calibration failed: {e!r}")
            self.link.laser, self.mode = False, "idle"

    def stop(self) -> None:
        """Laser off, nothing running. Points so far are kept (Save still works)."""
        self._cancel_auto()
        self.link.laser = False
        if self.mode != "idle":
            self.log("stopped, laser off")
        self.mode = "idle"

    def _cancel_auto(self) -> None:
        if self._auto is not None and not self._auto.done():
            self._auto.cancel()
        self._auto = None

    def _need_click_mode(self) -> None:
        if self.mode != "click" or self.sess is None:
            raise ValueError("start a click calibration first")

    async def watchdog(self, timeout_s: float = PAGE_TIMEOUT_S) -> None:
        """Laser off if no request (the page polls /state) for timeout_s: tab closed. Auto runs are left alone."""
        while True:
            await asyncio.sleep(0.5)
            if self.mode in ("click", "test", "verify") and self.clock() - self.seen_at > timeout_s:
                self.log("page gone")
                self.stop()

    # ── views ───────────────────────────────────────────────────────────────
    def status(self) -> dict:
        s, v = self.sess, self.verify
        goal = s.current() if s else None
        if self.mode in ("verify", "auto_verify") and v is not None and not v.done:
            goal = v.current()[1]
        return {
            "mode": self.mode,
            "index": s.i if s else 0,
            "total": len(s.grid) if s else 0,
            "goal": goal,
            "settled": self.settled,
            "n_points": s.n_points if s else 0,
            "can_save": bool(s and s.can_save()),
            "done": bool(s and s.done),
            "points": s.points()[0] if s else [],
            "saved": self.saved,
            "has_calib": self.calib is not None,
            "out": str(self.out),
            "laser": bool(self.link.laser),
            "camera": bool(getattr(self.frames, "connected", True)),
            "verify": None if v is None else {"index": v.i, "total": len(v.targets), "done": v.done,
                                              "target": None if v.done else v.current()[0],
                                              "stats": v.summary()[1] if v.done else None},
            "model": self.calib.model if self.calib is not None else None,
            "grid": self.grid,
            "log": self.lines,
        }

    def jpeg(self) -> bytes | None:
        frame, _ = self.frames.latest()
        if frame is None:
            return None
        if self.sess is not None or self.verify is not None:
            frame = draw_points(frame, self.sess, self.verify)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        return buf.tobytes() if ok else None


def _session_from(calib: Calibration) -> CalibSession:
    """A finished session holding a saved calibration's points (so verify points can be added and refitted)."""
    pts = (calib.points or {}) if calib is not None else {}
    sess = CalibSession([])
    sess.extra = [(tuple(p), tuple(a)) for p, a in zip(pts.get("px", []), pts.get("angles", []))]
    return sess


def make_router(cw: CalibWeb, prefix: str = "") -> APIRouter:
    """The page + JSON API. Every POST returns the new status (or 409 with {"detail": reason})."""
    r = APIRouter()

    def act(fn, *a) -> dict:
        cw.seen_at = cw.clock()         # any request = the page is still open
        try:
            fn(*a)
        except ValueError as e:
            raise HTTPException(409, str(e)) from None
        return cw.status()

    @r.get("/", response_class=HTMLResponse)
    async def page():
        return PAGE.replace("__BASE__", prefix)

    @r.get("/state")
    async def state():
        cw.seen_at = cw.clock()
        return cw.status()

    @r.get("/frame.jpg")
    async def frame():
        data = cw.jpeg()
        if data is None:
            raise HTTPException(503, "no camera frame yet")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @r.post("/start")
    async def start(g: GridIn | None = None):
        return act(cw.start, g)

    @r.post("/auto")
    async def auto(g: GridIn | None = None):
        return act(cw.start_auto, g)

    @r.post("/click")
    async def click(p: PointIn):
        return act(cw.click, p.x, p.y)

    @r.post("/skip")
    async def skip():
        return act(cw.skip)

    @r.post("/redo")
    async def redo():
        return act(cw.redo)

    @r.post("/test")
    async def test():
        return act(cw.toggle_test)

    @r.post("/save")
    async def save():
        st = act(cw.save)
        if not cw.saved:
            raise HTTPException(409, cw.lines[-1] if cw.lines else "not saved")
        return st

    @r.post("/stop")
    async def stop():
        return act(cw.stop)

    @r.post("/verify")
    async def verify():
        return act(cw.start_verify)

    @r.post("/verify_auto")
    async def verify_auto():
        return act(cw.start_auto_verify)

    @r.post("/verify_add")
    async def verify_add():
        return act(cw.add_verify)

    return r


def mount(app: FastAPI, link: ControlLinkAPI, frames: FrameSource, prefix: str = "/calib", **kw) -> CalibWeb:
    """Add the calibration page + API to an existing FastAPI app under `prefix`. kw go to CalibWeb.

    Returns the CalibWeb; start its watchdog() from the app's startup so the laser goes off when the tab closes.
    """
    prefix = prefix.rstrip("/")
    cw = CalibWeb(link, frames, **kw)
    app.include_router(make_router(cw, prefix), prefix=prefix)
    return cw


def create_app(link: ControlLinkAPI, frames: FrameSource, **kw) -> tuple[FastAPI, CalibWeb]:
    """Standalone app with the page at /. kw go to CalibWeb."""
    app = FastAPI(title="calibration")
    return app, mount(app, link, frames, prefix="", **kw)


# ═══════════════════════════════════════════════════════════════════════════════
# python -m coach.calib_web
# ═══════════════════════════════════════════════════════════════════════════════

async def _main(args) -> None:
    import uvicorn

    from .config import CONFIG
    from .contracts import FakeFrames, FakeLink

    if args.cam == "black":
        frames = FakeFrames()
    else:
        from .frames import FrameGrabber
        frames = FrameGrabber(args.cam if args.cam is not None else CONFIG.cam_url).start()
    if args.no_s3:
        link = FakeLink()
    else:
        from .control import ControlLink
        link = await ControlLink(args.ctl or CONFIG.ctl_host, CONFIG.ctl_port).start()
    grid = {"pan_min": args.pan_min, "pan_max": args.pan_max, "tilt_min": args.tilt_min,
            "tilt_max": args.tilt_max, "n": args.n}
    settle = args.settle_ms / 1000 if args.settle_ms is not None else SETTLE_S
    app, cw = create_app(link, frames, out=args.out, grid=grid, settle_s=settle)
    dog = asyncio.create_task(cw.watchdog())
    print(f"calibration page: http://{args.host}:{args.port}/  (saves to {args.out})", flush=True)
    try:
        await uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")).serve()
    finally:
        dog.cancel()
        cw.stop()
        await asyncio.sleep(0.1)        # one more packet with the laser off
        for fn in (getattr(frames, "stop", None), getattr(link, "close", None)):
            if fn is not None:
                fn()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m coach.calib_web", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_grid_args(ap)
    ap.add_argument("--cam", default=None, help="CAM_URL override: 0 = webcam, a file, a URL, or 'black'")
    ap.add_argument("--ctl", default=None, help="S3 host override (127.0.0.1 for tools/fake_s3.py)")
    ap.add_argument("--no-s3", action="store_true", help="in-process FakeLink instead of UDP")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to open it from another device")
    ap.add_argument("--port", type=int, default=8001)
    args = ap.parse_args(argv)
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Laser calibration</title>
<style>
 :root { color-scheme: light dark; --bg:#fff; --fg:#111; --mute:#666; --line:#ccc; --accent:#0a7d32; --warn:#b3261e; }
 @media (prefers-color-scheme: dark) { :root { --bg:#141414; --fg:#eee; --mute:#999; --line:#333; } }
 body { margin:0; padding:16px; background:var(--bg); color:var(--fg); font:14px/1.4 system-ui, sans-serif; }
 h1 { font-size:18px; margin:0 0 8px; }
 .wrap { display:flex; flex-wrap:wrap; gap:16px; }
 .cam { position:relative; flex:1 1 640px; max-width:960px; }
 .cam img { width:100%; display:block; border:1px solid var(--line); cursor:crosshair; background:#000; min-height:120px; }
 .side { flex:1 1 280px; min-width:0; }
 .bar { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0; }
 button { font:inherit; padding:6px 10px; border:1px solid var(--line); border-radius:6px; background:transparent; color:inherit; cursor:pointer; }
 button:disabled { opacity:.4; cursor:default; }
 button.primary { border-color:var(--accent); color:var(--accent); }
 #msg { font-weight:600; min-height:1.4em; }
 #err { color:var(--warn); min-height:1.4em; }
 .mute { color:var(--mute); }
 input { width:4.5em; font:inherit; background:transparent; color:inherit; border:1px solid var(--line); border-radius:4px; padding:2px 4px; }
 pre { font:12px/1.35 ui-monospace, monospace; border:1px solid var(--line); padding:8px; max-height:320px; overflow:auto; white-space:pre-wrap; }
 kbd { border:1px solid var(--line); border-radius:3px; padding:0 4px; font-size:12px; }
</style></head><body>
<h1>Laser calibration</h1>
<div class="wrap">
 <div class="cam"><img id="cam" alt="camera"><div id="msg"></div><div id="err"></div></div>
 <div class="side">
  <div class="bar">
   <button class="primary" id="b-start">Start click run</button>
   <button id="b-auto">Auto</button>
   <button id="b-stop">Stop (laser off)</button>
  </div>
  <div class="bar">
   <button id="b-skip">Skip <kbd>s</kbd></button>
   <button id="b-redo">Redo <kbd>r</kbd></button>
   <button id="b-test">Test mode <kbd>t</kbd></button>
   <button class="primary" id="b-save">Save</button>
  </div>
  <div class="bar">
   <button id="b-verify">Verify <kbd>v</kbd></button>
   <button id="b-vauto">Auto verify</button>
   <button id="b-vadd">Add verify points</button>
  </div>
  <div class="bar mute">
   pan <input id="pan_min"> to <input id="pan_max"><br>
   tilt <input id="tilt_min"> to <input id="tilt_max"> &nbsp; n <input id="n">
  </div>
  <div class="mute" id="info"></div>
  <pre id="log"></pre>
 </div>
</div>
<script>
const BASE = "__BASE__", $ = id => document.getElementById(id), cam = $("cam");
const FIELDS = ["pan_min", "pan_max", "tilt_min", "tilt_max", "n"];
let st = null, gridShown = false;

async function post(path, body) {
  const r = await fetch(BASE + path, {method: "POST", headers: {"Content-Type": "application/json"},
                                      body: body ? JSON.stringify(body) : null});
  const j = await r.json();
  $("err").textContent = r.ok ? "" : (j.detail || r.statusText);
  if (r.ok) render(j); else poll();
}
function grid() {
  const g = {};
  for (const f of FIELDS) { const v = parseFloat($(f).value); if (!isNaN(v)) g[f] = v; }
  return g;
}
function render(s) {
  st = s;
  if (!gridShown) { for (const f of FIELDS) $(f).value = s.grid[f]; gridShown = true; }
  let m;
  if (s.mode === "test") m = "TEST: click a spot, the laser goes there";
  else if (s.mode === "verify" && s.verify) m = `verify ${s.verify.index + 1}/${s.verify.total}: ` +
      (s.settled ? "click the laser dot (s: not visible)" : "settling...");
  else if (s.mode === "auto_verify" && s.verify) m = `auto verify ${Math.min(s.verify.index + 1, s.verify.total)}/${s.verify.total}...`;
  else if (s.mode === "auto") m = `auto: point ${Math.min(s.index + 1, s.total)}/${s.total}, finding the dot...`;
  else if (s.mode === "click" && s.goal) m = `${s.index + 1}/${s.total} pan ${s.goal[0].toFixed(0)} tilt ${s.goal[1].toFixed(0)}: `
      + (s.settled ? "click the laser dot" : "settling...");
  else if (s.mode === "click") m = s.saved ? "grid done and saved. Test mode to check it." : "grid done. Redo points or Save.";
  else m = "Start a click run (or Auto). The laser turns on.";
  $("msg").textContent = m;
  $("info").textContent = `${s.n_points} points` + (s.can_save ? "" : " (need 9 to save)") +
    ` | laser ${s.laser ? "ON" : "off"} | camera ${s.camera ? "ok" : "DOWN"} | ${s.has_calib ? "calibration loaded (" + s.model + ")" : "no calibration yet"} | ${s.out}` +
    (s.verify && s.verify.stats && s.verify.stats.n ? ` | verify mean ${s.verify.stats.mean_deg.toFixed(2)} deg (~${s.verify.stats.mean_cm.toFixed(1)} cm), max ${s.verify.stats.max_deg.toFixed(2)} deg` : "");
  const click = s.mode === "click", busy = s.mode === "auto" || s.mode === "auto_verify";
  $("b-verify").disabled = busy || !s.has_calib;
  $("b-vauto").disabled = busy || !s.has_calib;
  $("b-vadd").disabled = busy || !(s.verify && s.verify.done);
  $("b-skip").disabled = !(click || s.mode === "verify") || (click && s.done);
  $("b-redo").disabled = !click || s.index === 0;
  $("b-save").disabled = !click || !s.can_save;
  $("b-test").disabled = busy || (!s.has_calib && s.mode !== "test");
  $("b-test").textContent = s.mode === "test" ? "Leave test mode" : "Test mode";
  const log = $("log"), atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
  log.textContent = s.log.join("\\n");
  if (atEnd) log.scrollTop = log.scrollHeight;
}
async function poll() {
  try { render(await (await fetch(BASE + "/state")).json()); } catch (e) { $("err").textContent = "server gone"; }
}
function refreshFrame() {
  const next = new Image();
  next.onload = () => { cam.src = next.src; setTimeout(refreshFrame, 80); };
  next.onerror = () => setTimeout(refreshFrame, 500);
  next.src = BASE + "/frame.jpg?t=" + Date.now();
}
cam.addEventListener("click", e => {
  if (!cam.naturalWidth) return;
  const r = cam.getBoundingClientRect();
  post("/click", {x: (e.clientX - r.left) * cam.naturalWidth / r.width,
                  y: (e.clientY - r.top) * cam.naturalHeight / r.height});
});
$("b-start").onclick = () => post("/start", grid());
$("b-auto").onclick = () => post("/auto", grid());
$("b-stop").onclick = () => post("/stop");
$("b-skip").onclick = () => post("/skip");
$("b-redo").onclick = () => post("/redo");
$("b-test").onclick = () => post("/test");
$("b-save").onclick = () => post("/save");
$("b-verify").onclick = () => post("/verify");
$("b-vauto").onclick = () => post("/verify_auto");
$("b-vadd").onclick = () => post("/verify_add");
document.addEventListener("keydown", e => {
  if (e.target.tagName === "INPUT") return;
  const b = {s: "b-skip", r: "b-redo", t: "b-test", v: "b-verify"}[e.key];
  if (b && !$(b).disabled) $(b).click();
});
setInterval(poll, 250); poll(); refreshFrame();
</script></body></html>
"""


if __name__ == "__main__":
    main()
