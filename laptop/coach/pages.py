"""HTML for the dashboard (`/`) and the phone page (`/cook`). Both share the theme (dark / light) and the dyslexia
font option, so they are defined once here."""

# ---- shared: applies the saved theme/font before the page paints, and the CSS variables they switch ----
SHARED_SCRIPT = r"""<script>
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
  function wireOptions() {            // call once at the end of the page, after the two buttons exist
    for (const [k, id] of [["theme", "optTheme"], ["font", "optFont"]]) {
      const b = document.getElementById(id), on = OPTS[k][1];
      if (!b) continue;
      b.setAttribute("aria-pressed", String(document.documentElement.dataset[k] === on));
      b.addEventListener("click", () => applyOption(k, document.documentElement.dataset[k] === on ? OPTS[k][0] : on));
    }
  }
</script>"""

SHARED_CSS = r"""  /* Options (header buttons, remembered in this browser, or in the link: ?theme=light&font=dyslexic) */
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
"""

# ---- the dashboard ----
_DASHBOARD = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Remy: live dashboard</title>
%%SHARED_SCRIPT%%
<style>
%%SHARED_CSS%%  header h1 { font-size:20px; margin:0; } .pill { padding:2px 10px; border-radius:99px; background:var(--panel); border:1px solid var(--line); font-size:13px; }
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
  .note { font-size:.85em; opacity:.75; margin:.25em 0; }
  [hidden] { display:none !important; }
  .howrow { display:flex; gap:16px; align-items:flex-start; flex-wrap:wrap; }
  #stepimg { width:min(100%, 260px); max-height:220px; object-fit:cover; border-radius:8px; border:1px solid var(--line); }
  #how { margin:0; padding-left:1.3em; flex:1; min-width:200px; font-size:16px; line-height:1.5; } #how li { margin:4px 0; }
  .qrrow { display:flex; gap:16px; align-items:center; flex-wrap:wrap; } #qr { width:130px; height:130px; border-radius:8px; background:#fff; padding:4px; }
  #cookurl { overflow-wrap:anywhere; font-size:14px; color:var(--dim); }
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
    <div class="panel" id="howp"><h2>How to do this step</h2>
      <div class="howrow"><div class="imgcol"><img id="stepimg" alt="what this step should look like"><div id="imgnote" class="note" hidden></div></div><ol id="how"></ol></div></div>
    <div class="panel"><h2>Latency of the last turn</h2><div id="lat"></div></div>
    <div class="panel"><h2>Follow along on your phone</h2>
      <div class="qrrow"><img id="qr" alt="QR code that opens the step-by-step page" hidden><div id="cookurl">…</div></div></div>
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
    $("how").innerHTML = (s.step.how || []).map(h => "<li>" + esc(h) + "</li>").join("");
    const img = $("stepimg");
    if (img.dataset.src !== s.step.image_url) { img.dataset.src = s.step.image_url; img.src = s.step.image_url; }
    $("imgnote").textContent = s.step.image_note || ""; $("imgnote").hidden = !s.step.image_note;
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
wireOptions();
fetch("/info").then(r => r.json()).then(i => {            // phone link + QR code
  $("cookurl").textContent = i.cook_url;
  if (i.qr) { $("qr").src = "/cook-qr.svg"; $("qr").hidden = false; }
}).catch(() => {});
tick(); setInterval(tick, 250);
</script></body></html>
"""

DASHBOARD_PAGE = _DASHBOARD.replace("%%SHARED_SCRIPT%%", SHARED_SCRIPT).replace("%%SHARED_CSS%%", SHARED_CSS)


# ---- the phone page: one step at a time, big ----
_OPTS_HTML = """<span class="opts">
    <button class="opt" id="optTheme" aria-pressed="false" title="Switch between dark and light">Light mode</button>
    <button class="opt" id="optFont" aria-pressed="false" title="Show all text in the OpenDyslexic font">Dyslexia font</button>
  </span>"""

_COOK = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Remy: step by step</title>
%%SHARED_SCRIPT%%
<style>
%%SHARED_CSS%%
  .note { font-size:.85em; opacity:.75; margin:.25em 0; }
  [hidden] { display:none !important; }
  header h1 { font-size:20px; margin:0; } .pill { padding:2px 10px; border-radius:99px; background:var(--panel); border:1px solid var(--line); font-size:14px; }
  main { max-width:680px; margin:0 auto; padding:14px 16px 48px; }
  .dots { display:flex; gap:6px; margin-bottom:12px; } .dots span { flex:1; height:8px; border-radius:4px; background:var(--track); }
  .dots span.done { background:var(--ok); } .dots span.now { background:var(--accent); }
  #title { font-size:clamp(30px, 8vw, 44px); font-weight:700; line-height:1.15; }
  #goal { font-size:clamp(18px, 4.8vw, 22px); color:var(--dim); margin:6px 0 14px; }
  #photo { width:100%; max-height:46vh; object-fit:cover; border-radius:14px; border:1px solid var(--line); margin-bottom:14px; }
  h2.sec { font-size:13px; letter-spacing:.08em; text-transform:uppercase; color:var(--dim); margin:18px 0 6px; }
  #how { margin:0; padding-left:1.3em; } #how li { font-size:clamp(20px, 5.4vw, 26px); line-height:1.35; margin:10px 0; }
  #tips { margin:0; padding-left:1.2em; color:var(--dim); font-size:clamp(16px, 4.4vw, 19px); } #tips li { margin:6px 0; }
  #say { background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:12px 16px; margin-top:20px; }
  #say .t { font-size:clamp(22px, 6vw, 30px); font-weight:600; color:var(--accent); line-height:1.25; }
</style></head>
<body>
<header><h1>Remy</h1><span class="pill" id="prog">…</span>
  %%OPTS%%</header>
<main>
  <div class="dots" id="dots"></div>
  <div id="title">…</div><div id="goal"></div>
  <img id="photo" alt="what this step should look like"><div id="photonote" class="note" hidden></div>
  <h2 class="sec">How</h2><ol id="how"></ol>
  <h2 class="sec" id="tipsh">Good to know</h2><ul id="tips"></ul>
  <div id="say"><h2 class="sec" style="margin-top:0">Remy says</h2><div class="t" id="cap">…</div></div>
</main>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function tick() {
  try {
    const s = await (await fetch("/state", {cache:"no-store"})).json(), st = s.step;
    $("prog").textContent = "Step " + st.n + " of " + st.total;
    $("dots").innerHTML = Array.from({length: st.total}, (_, i) => '<span class="' + (i + 1 < st.n ? "done" : i + 1 === st.n ? "now" : "") + '"></span>').join("");
    $("title").textContent = st.short; $("goal").textContent = st.goal;
    $("how").innerHTML = st.how.map(h => "<li>" + esc(h) + "</li>").join("");
    $("tips").innerHTML = st.tips.map(t => "<li>" + esc(t) + "</li>").join(""); $("tipsh").hidden = !st.tips.length;
    const img = $("photo");
    if (img.dataset.src !== st.image_url) { img.dataset.src = st.image_url; img.src = st.image_url; }
    $("photonote").textContent = st.image_note || ""; $("photonote").hidden = !st.image_note;
    $("cap").textContent = s.caption || "…";
  } catch (e) { $("prog").textContent = "reconnecting…"; }
}
wireOptions(); tick(); setInterval(tick, 500);
</script></body></html>
"""

COOK_PAGE = _COOK.replace("%%SHARED_SCRIPT%%", SHARED_SCRIPT).replace("%%SHARED_CSS%%", SHARED_CSS).replace("%%OPTS%%", _OPTS_HTML)
