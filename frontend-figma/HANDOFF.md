# Handoff: new frontend (Figma "Ratatouille")

Standalone Vite + React + TypeScript app. Nothing outside this folder was changed.

## Run

```bash
npm install
npm run dev            # http://localhost:5173  (proxies the backend on :8000)
npm run dev -- --open  # same, opens a browser
BACKEND_URL=http://localhost:9000 npm run dev   # backend on another port
```

Open `http://localhost:5173/?mock` to run on fake data with no backend (or set `VITE_MOCK=1`).

```bash
npm run build          # type-check + production build into dist/
npm run lint
```

## Old page -> new screen

| Old (laptop/coach/pages.py) | New |
|---|---|
| `DASHBOARD_PAGE`, served at `GET /` | `src/screens/Dashboard.tsx` (+ `MenuPanel.tsx`) |
| `COOK_PAGE`, served at `GET /cook` (phone page) | `src/screens/Cook.tsx` + `cook.html` (NOT in Figma: designed from the dashboard's styles, single column) |

## Backend calls (all in `src/api.ts` and the two `<img>` tags below)

| Call | Used for |
|---|---|
| `GET /state` every 250 ms | everything in `AppState` (step, transcript, caption, links, latency, events) |
| `GET /stream` (MJPEG) | camera box (`<img src>`) |
| `GET /cook-qr.svg` | QR box (currently encodes the phone page URL, not a video link) (needs `segno` on the backend; shows the beige box if 404) |
| `GET /recipe-image/{n}` | recipe image; URL comes from `state.step.image_url` |

`GET /info` (once) gives `cook_url`, shown as text under the QR code. No WebSocket and no POSTs.

## How the UI maps to `/state`

| UI | Field |
|---|---|
| Step heading / line under it | `step.n` + `step.short` / `step.goal` |
| Remy's text (bold line) | `caption` (the sentence being spoken). The two lighter lines above it are the previous captions, remembered in the browser. |
| User text | `transcript` |
| Remy / user talking animation | `speaking` / `user_talking` (see below) |
| Bubble 1 | `session` (idle: "Waiting for Hey Remy!", active: "Cooking") |
| Camera / S3 bubbles | `links.camera` / `links.s3` (green ok, red not) |
| Last bubble | green "Connected" only if `/state` answers and camera and S3 are ok, else red "Disconnected" |
| Latency rows | `latency.first_token` / `first_audio` / `point_called` / `on_target`; bar full at 2000 ms (`LATENCY_MAX_MS` in Dashboard.tsx) |
| Event log | `events` (backend sends the last 20) |
| "How to do this step" list (dashboard + phone) | `step.how` |
| "Good to know" (phone only) | `step.tips` |
| Picture-is-for-another-step note (phone only) | `step.image_note` |
| Dev info: Laser / Sees | `laser.target`, `laser.on_target` / `visible` |
| Dev info: Battery / Camera ignored | `links.vbat_mv` (hidden when 0) / `links.camera_ignored` |

## Replacing the old frontend (your part)

The build has TWO pages: `dist/index.html` (dashboard, serve at `/`) and `dist/cook.html` (phone page, serve at
`/cook`, so the QR code keeps working). In dev, Vite already maps `/cook` to `cook.html`.

Pick one:
- **Python serves the build.** `npm run build`, then mount `frontend-figma/dist` as static files in
  `dashboard.py` at `/` (keep `/state`, `/stream`, etc. as they are). One process, same origin, so no config.
- **Separate dev server.** Keep `npm run dev` for development only.

Both pages reference `/assets/...`, so mount `dist/` at the site root.

## Decisions I made (change freely)

- **Dark mode**: toggle sets `data-theme="dark"` on `<html>` (saved in localStorage). Dark values are at the bottom of
  `src/index.css` (same variable names as Figma; `--white` is the page background so it is the darkest color:
  `--white #2b2624`, `--beige #4a423f`, `--dark-brown #e5dbd2`, plus a `--knob` token for the toggle). The
  title, tab icon and menu icon switch with the theme (see "Theme-aware images" below).
  Consider adding a matching "Dark" mode to the Figma variables.
- **Dyslexia font**: OpenDyslexic, bundled (npm package `@fontsource/opendyslexic`), replaces both fonts. It is wider
  than Nunito, so text sizes drop a little in this mode (20/14/16 px instead of 24/16/18) to avoid overflow.
- **Show developer info** shows/hides the latency + event log bar. Default on. With it ON: three cards side by side
  (step | conversation | pictures: QR + recipe on top, camera below, tightly packed) and the dev bar full width
  underneath. The picture column's width is computed from the window height so the pictures fill it
  (`--media-col` in `src/index.css`, min 280 px, max 440 px). With it OFF the pictures sit inside the two cards as in
  Figma. Toggling re-mounts the pictures (the camera stream reconnects). Below about 1100 px wide the two text cards
  get narrow and scroll inside themselves.
- **Menu** opens under the menu icon; click outside or press Esc to close. I added a 2px beige border so it
  reads as a popover (the Figma frame has none).
- Latency bars have a green fill; Figma only drew the empty track.
- Layout fills the window (min height 640 px). Header: title centred and cropped to its drawing (64 px tall, `--header-h`), menu icon 22x19 at the right, so the page has more room than Figma's 103 px header.
- **Picture boxes keep fixed proportions** (they scale with the window but never stretch): QR 1:1, recipe photo 16:9,
  camera = the video's own shape (never cropped, no backdrop: the UI reads the stream's real width/height when its first
  frame loads, so 4:3, 3:2 or 16:9 all fit with no bars). `--camera-aspect` in `src/index.css` is only the default used
  before the first frame (4:3); `--recipe-aspect` (16:9) is fixed.
  Recipe photos should be 16:9, e.g. 1280x720 JPEG.

## Known gaps

- Not tested against the live Python backend (only `?mock`); the field names come from reading `snapshot()` in `dashboard.py`.
- Figma asset URLs expire; all images are already downloaded into `src/assets/`.

## Talking animations

`src/components/TalkingAvatar.tsx`. Frames come from `laptop/imgs/{remy,user}_talking-1..4.png`, resized to 512 px
into `src/assets/` by `python3 tools/make-frames.py` (re-run it if the art changes; needs Pillow).

- 6 fps, ping-pong 1,2,3,4,3,2 (frame 1 is the rest pose).
- Remy plays while `state.speaking`, the user while `state.user_talking`; they run independently.
- When the flag drops, the animation keeps going for 300 ms (hides voice-detector flicker), then finishes the current
  loop and rests on frame 1.
- All four frames stay in the DOM (one visible) so nothing flashes on first play.
- `prefers-reduced-motion` keeps both on frame 1.
- Preview without a backend: `?mock&remy&user` (always talking) or `?mock&remy=2` (talks for 2 s, then stops).

## Fonts

Nunito, Protest Riot and OpenDyslexic are bundled from npm (`@fontsource/*`), so the UI works with no internet
(hotspot-only kitchen). No requests go to Google Fonts.

## Phone page (`/cook`)

One column, max 560 px: progress dots, step title + goal, photo (16:9), "How to do this step", "Good to know", and a
sticky "Remy says" box with the talking animation. Text is 20 px (18 px with the dyslexia font). Same menu (light/dark,
dyslexia font; no developer-info row), same tokens. Preview: `http://localhost:5173/cook?mock`.

## QR code text link

The link under the QR code is `cook_url` from `/info` without `http://`; its bottom lines up with the recipe image's
bottom (the QR is shorter by the link's height; `--link-h` in `src/index.css`).

## Theme-aware images

- **Title**: `laptop/imgs/title-light.png` / `title-dark.png` -> `src/assets/` (1024x300) by `python3 tools/make-frames.py`.
  Both are in the page; CSS shows the one for the current theme. The crop in `index.css` assumes the drawing sits at
  x 258..1847, y 104..447 of the original 2048x600 canvas, so new art should keep that placement.
- **Tab icon**: `icon-light.png` / `icon-dark.png` -> `public/favicon-32-{light,dark}.png`; the tab icon follows the
  app's theme setting (not the OS). `favicon-180.png` (home-screen icon) is the light one.
- **Menu icon**: `public/menu.svg`, drawn as a CSS mask in the text color, so it works in both themes.
- The talking-animation frames are the same in both themes.
