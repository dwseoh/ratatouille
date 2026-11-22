import { useEffect, useState } from "react";
import type { Settings } from "../screens/MenuPanel";

const KEY = "ratatouille.settings";
const DEFAULTS: Settings = { dark: false, dyslexia: false, devInfo: true };

function load(): Settings {
  try {
    return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) ?? "{}") };
  } catch {
    return DEFAULTS;
  }
}

/** Menu settings, saved in localStorage and applied to <html> (data-theme / data-dyslexia). Shared by every page. */
export function useSettings(): [Settings, (next: Settings) => void] {
  const [settings, setSettings] = useState<Settings>(load);
  useEffect(() => {
    const root = document.documentElement;
    root.dataset.theme = settings.dark ? "dark" : "light";
    root.dataset.dyslexia = String(settings.dyslexia);
    // the tab icon follows the app's theme (not the OS): light icon on the light theme, dark icon on the dark one
    const icon = document.querySelector<HTMLLinkElement>('link[rel="icon"]');
    if (icon) icon.href = settings.dark ? "/favicon-32-dark.png" : "/favicon-32-light.png";
    try {
      localStorage.setItem(KEY, JSON.stringify(settings));
    } catch {
      /* storage blocked: settings just won't persist */
    }
  }, [settings]);
  return [settings, setSettings];
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
