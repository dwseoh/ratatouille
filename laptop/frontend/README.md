# Frontend (dashboard + phone page)

The two web pages Remy's Python app serves: the **dashboard** (`/`, for a laptop screen) and the **phone page**
(`/cook`, one step at a time, reached by the QR code). Vite + React + TypeScript, plain CSS variables. Designed in
the Figma file "Ratatouille"; the phone page isn't in Figma and reuses the dashboard's styles.

```
laptop/frontend/     this folder: the source
laptop/coach/static/ the BUILT pages, committed, served by laptop/coach/dashboard.py
```

**The demo laptop needs no Node.** Python serves `coach/static/` as it is. You only need Node (22 or newer) to change the UI.

## Changing the UI

```bash
cd laptop/frontend
npm install
npm run dev            # http://localhost:5173, proxies the Python app on :8000 (start it first)
npm run build          # type-check + build into ../coach/static/   <- commit the result
npm run lint
```

- **No backend?** Open `http://localhost:5173/?mock` for fake data. Extras: `&remy` / `&user` keep the talking
  animations on, `&remy=2` stops after 2 s, `&note` shows the "Showing step N" note. Phone page: `/cook?mock`.
- Backend on another port: `BACKEND_URL=http://localhost:9000 npm run dev`.
- Try the phone page on a real phone: `npm run dev -- --host`, then open `http://<laptop-ip>:5173/cook` (same WiFi).
- **After any change, run `npm run build` and commit `laptop/coach/static/` too**, or the app keeps serving the old build.
  `laptop/tests/test_dashboard.py` checks that the built pages only load files that exist.

## How it talks to Python

Everything goes through `src/api.ts` (the only file that does), all GET, same origin:

| Call | Used for |
|---|---|
| `/state`, every 250 ms | everything in `AppState` (mirrors `snapshot()` in `laptop/coach/dashboard.py`) |
| `/stream` | camera feed (MJPEG in an `<img>`) |
| `/cook-qr.svg` | QR code for the phone page (needs `segno`; a beige box if missing) |
| `/recipe-image/{n}` | step picture; the URL comes from `state.step.image_url` |
| `/info`, once | `cook_url`, shown as text under the QR code |

`test_dashboard.py` fails if a field the UI reads disappears from `/state` or from `api.ts`. When you add a field,
add it to `AppState` in `src/api.ts` and to `FRONTEND_FIELDS` in that test.

| UI | `/state` field |
|---|---|
| Step title / goal | `step.n` + `step.short` / `step.goal` |
| How to do this step; "Good to know" (phone) | `step.how`; `step.tips` |
| Remy's bold line (+ two lighter earlier lines, kept in the browser) | `caption` |
| Your text | `transcript` |
| Talking animations | `speaking` (Remy), `user_talking` (you) |
| Bubbles | `session`; `links.camera`, `links.s3`; "Connected" only if `/state` answers and camera and S3 are ok |
| Recipe picture note ("Showing step 4...") | `step.image_note` |
| Latency bars (full at 2000 ms, `LATENCY_MAX_MS` in Dashboard.tsx) | `latency.first_token`, `first_audio`, `point_called`, `on_target` |
| Event log | `events` |
| Developer info: Laser / Sees / Battery / Camera ignored | `laser`, `visible`, `links.vbat_mv` (hidden at 0), `links.camera_ignored` |

## Behaviour

- **Menu** (top right): Light/dark mode, Dyslexia font, Show developer info. Saved per browser (localStorage). A link can
  set them for one visit: `?theme=dark|light`, `?font=dyslexic|default`. Default: light, developer info on.
- **Developer info on:** three cards (step | conversation | pictures: QR + recipe on top, camera below) with the latency +
  event-log bar full width underneath. The picture column's width follows the window height (`--media-col` in
  `src/index.css`, 280-440 px) so the pictures fill it. **Off:** the pictures sit inside the two cards.
  Toggling re-mounts the pictures, so the camera stream reconnects.
- **Picture proportions never stretch:** QR 1:1; recipe 16:9 (make recipe photos 16:9, e.g. 1280x720); camera takes the
  video's real shape from its first frame (never cropped, no backdrop). `--camera-aspect` (4:3) is only the default.
- **QR link text:** `cook_url` without `http://`, bottom-aligned with the recipe picture (`--link-h` in `src/index.css`).
- **Talking animations** (`src/components/TalkingAvatar.tsx`): 6 fps, ping-pong 1,2,3,4,3,2. After a flag drops it keeps
  going 300 ms (hides voice-detector flicker), then finishes the loop and rests on frame 1. `prefers-reduced-motion`
  keeps both still.
- **Dyslexia font:** OpenDyslexic replaces both fonts; text sizes drop a little (20/14/16 px instead of 24/16/18) as it is wider.
- **Dark mode:** same variable names as Figma with dark values at the bottom of `src/index.css` (`--white` is the page
  background, so it holds the darkest colour). Title, tab icon and menu icon switch with the theme.
- **Phone page:** one column, max 560 px, text 20 px (18 with the dyslexia font), sticky "Remy says" box.
- **Offline-safe:** Nunito, Protest Riot and OpenDyslexic are bundled from npm; nothing loads from the internet.
- Windows narrower than about 1100 px make the two text cards cramped (they scroll inside themselves).

## Images

`python3 tools/make-frames.py` (needs Pillow) reads `laptop/imgs/` and writes the web-sized copies; re-run it when the art changes:

| From `laptop/imgs/` | To |
|---|---|
| `remy_talking-1..4.png`, `user_talking-1..4.png` | `src/assets/*-talking-N.png` (512 px) |
| `title-light.png`, `title-dark.png` | `src/assets/title-{light,dark}.png` (1024x300). The crop in `index.css` assumes the drawing sits at x 258-1847, y 104-447 of the 2048x600 original. |
| `icon-light.png`, `icon-dark.png` | `public/favicon-32-{light,dark}.png`, `public/favicon-180.png` (light). The tab icon follows the app's theme, not the OS. |

`public/menu.svg` (menu icon) is drawn as a CSS mask in the text colour, so it works in both themes.
