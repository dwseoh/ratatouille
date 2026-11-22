import { useEffect, useState } from "react";
import type { Settings } from "../screens/MenuPanel";

const KEY = "ratatouille.settings";
const DEFAULTS: Settings = { dark: false, dyslexia: false, devInfo: true };

function stored(): Partial<Settings> {
  try {
    return JSON.parse(localStorage.getItem(KEY) ?? "{}");
  } catch {
    return {};
  }
}

/** Saved settings, overridden by the link: ?theme=dark|light and ?font=dyslexic|default (not saved by themselves). */
function load(): Settings {
  let s: Settings = { ...DEFAULTS, ...stored() };
  const q = new URLSearchParams(window.location.search);
  const theme = q.get("theme");
  const font = q.get("font");
  if (theme === "dark" || theme === "light") s = { ...s, dark: theme === "dark" };
  if (font === "dyslexic" || font === "default") s = { ...s, dyslexia: font === "dyslexic" };
  return s;
}

/** Menu settings, applied to <html> (data-theme / data-dyslexia). Only what the user changes is saved in localStorage. */
export function useSettings(): [Settings, (next: Settings) => void] {
  const [settings, setSettings] = useState<Settings>(load);
  useEffect(() => {
    const root = document.documentElement;
    root.dataset.theme = settings.dark ? "dark" : "light";
    root.dataset.dyslexia = String(settings.dyslexia);
    // the tab icon follows the app's theme (not the OS): light icon on the light theme, dark icon on the dark one
    const icon = document.querySelector<HTMLLinkElement>('link[rel="icon"]');
    if (icon) icon.href = settings.dark ? "/favicon-32-dark.png" : "/favicon-32-light.png";
  }, [settings]);
  const update = (next: Settings) => {
    const saved: Partial<Settings> = stored();
    for (const k of Object.keys(next) as (keyof Settings)[]) if (next[k] !== settings[k]) saved[k] = next[k];
    try {
      localStorage.setItem(KEY, JSON.stringify(saved));
    } catch {
      /* storage blocked: settings just won't persist */
    }
    setSettings(next);
  };
  return [settings, update];
}

/** Open/closed state for the menu popover; closes on Esc or a click outside `.menu-anchor`. */
export function useMenu(): [boolean, () => void] {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    const onClick = (e: MouseEvent) => {
      if (!(e.target as Element).closest(".menu-anchor")) setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onClick);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onClick);
    };
  }, [open]);
  return [open, () => setOpen((o) => !o)];
}
