// The ONLY file that talks to the Python backend (laptop/coach/dashboard.py).
// To change how the UI gets its data, edit this file; components just consume `AppState`.
//
// Backend endpoints used (all GET, same origin; Vite proxies them in dev, see vite.config.ts):
//   /state             JSON snapshot, polled every POLL_MS
//   /stream            MJPEG camera feed (annotated)
//   /cook-qr.svg       QR code for the phone page
//   /recipe-image/{n}  picture for step n (the URL arrives in state.step.image_url)
//   /info              {cook_url}: the phone-page address, shown as text under the QR code
import { useEffect, useState } from "react";

export const POLL_MS = 250;
export const STREAM_URL = "/stream";
export const QR_URL = "/cook-qr.svg";

/** Mirrors `snapshot()` in laptop/coach/dashboard.py (only the fields the UI uses). */
export interface AppState {
  session: "idle" | "active";
  step: {
    n: number;
    total: number;
    short: string;
    goal: string;
    how: string[];
    tips: string[];
    image_url: string;
    /** Non-empty when the picture shown is for a different step than the current one. */
    image_note: string;
  };
  recipe: string;
  transcript: string;
  caption: string;
  /** True while Remy's voice is playing. Drives Remy's talking animation. */
  speaking: boolean;
  /** True while the microphone hears the user. Drives the user's talking animation. */
  user_talking: boolean;
  events: { t: number; source: "vision" | "user" | "agent" | "system"; text: string }[];
  /** ms from the end of the user's speech to each mark. Empty before the first turn. */
  latency: { first_token?: number; first_audio?: number; point_called?: number; on_target?: number };
  /** The laser: `target` is the object being pointed at (null = off). */
  laser: { target: string | null; on_target: boolean };
  /** Objects the camera currently sees. */
  visible: string[];
  links: { camera: boolean; camera_ignored: boolean; s3: boolean; vbat_mv: number };
}

/** True when the UI should run on fake data (no backend): add ?mock to the URL or set VITE_MOCK=1. */
export const MOCK: boolean =
  import.meta.env.VITE_MOCK === "1" || new URLSearchParams(window.location.search).has("mock");

// In mock mode, add `&remy` and/or `&user` to the URL to force the talking animations on.
// `&remy=2` means: talking for 2 seconds, then stop (to preview how the animation winds down).
// `&note` shows the "Showing step N" note on the recipe picture.
const MOCK_PARAMS = new URLSearchParams(window.location.search);

const MOCK_STATE: AppState = {
  speaking: MOCK_PARAMS.has("remy"),
  user_talking: MOCK_PARAMS.has("user"),
  session: "idle",
  step: {
    n: 1,
    total: 5,
    short: "Halve the avocados",
    goal: "Cut each avocado in half lengthwise, around the pit.",
    how: [
      "Hold the avocado steady on the cutting board.",
      "Cut lengthwise around the pit, all the way around.",
      "Twist the two halves apart.",
    ],
    tips: ["Curl your fingers away from the blade (claw grip)."],
    image_url: "",
    image_note: MOCK_PARAMS.has("note") ? "Showing step 4: Mash it" : "",
  },
  recipe: "Guacamole",
  transcript: "Hey Remi, I wanna start making the guacamole please!",
  caption: "Now, scoop the avocado out of the skin using a spoon. Put the green flesh in a bowl and throw out the seed.",
  events: [
    { t: Date.now() / 1000 - 8, source: "system", text: "Lorem ipsum" },
    { t: Date.now() / 1000 - 4, source: "user", text: "Hey Remi, I wanna start making the guacamole please!" },
  ],
  latency: { first_token: 640, first_audio: 910, point_called: 1300 },
  laser: { target: "avocado", on_target: false },
  visible: ["avocado", "bowl"],
  links: { camera: true, camera_ignored: false, s3: true, vbat_mv: 3920 },
};

/** Latest backend state, or null until the first response. `offline` is true while /state fails. */
export function useAppState(): { state: AppState | null; offline: boolean } {
  const [state, setState] = useState<AppState | null>(MOCK ? MOCK_STATE : null);
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    if (MOCK) {
      const ids = (["remy", "user"] as const).flatMap((k) => {
        const secs = Number(MOCK_PARAMS.get(k));
        if (!(secs > 0)) return [];
        const key = k === "remy" ? "speaking" : "user_talking";
        return [window.setTimeout(() => setState((s) => (s ? { ...s, [key]: false } : s)), secs * 1000)];
      });
      return () => ids.forEach((id) => window.clearTimeout(id));
    }
    let stopped = false;
    const tick = async () => {
      try {
        const res = await fetch("/state", { cache: "no-store" });
        if (!res.ok) throw new Error(String(res.status));
        const data = (await res.json()) as AppState;
        if (!stopped) {
          setState(data);
          setOffline(false);
        }
      } catch {
        if (!stopped) setOffline(true);
      }
    };
    void tick();
    const id = window.setInterval(tick, POLL_MS);
    return () => {
      stopped = true;
      window.clearInterval(id);
    };
  }, []);

  return { state, offline };
}

const MOCK_COOK_URL = "http://192.168.1.20:8000/cook";

/** The phone-page address from /info (shown as a text link under the QR code). Undefined until known. */
export function useCookUrl(): string | undefined {
  const [url, setUrl] = useState<string | undefined>(MOCK ? MOCK_COOK_URL : undefined);
  useEffect(() => {
    if (MOCK) return;
    let stopped = false;
    fetch("/info")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((i: { cook_url?: string }) => !stopped && setUrl(i.cook_url))
      .catch(() => {
        /* no link text: the QR code still works */
      });
    return () => {
      stopped = true;
    };
  }, []);
  return url;
}
