import Toggle from "../components/Toggle";

export interface Settings {
  dark: boolean;
  dyslexia: boolean;
  devInfo: boolean;
}

/** Figma frame "menu" (22:50). Shown as a popover under the menu icon. */
export default function MenuPanel({
  settings,
  onChange,
  showDevToggle = true,
}: {
  settings: Settings;
  onChange: (next: Settings) => void;
  /** The phone page has no developer info, so it hides that row. */
  showDevToggle?: boolean;
}) {
  const rows: { key: keyof Settings; label: string }[] = [
    { key: "dark", label: "Light/dark mode" },
    { key: "dyslexia", label: "Dyslexia font" },
    { key: "devInfo", label: "Show developer info" },
  ];
  const visible = showDevToggle ? rows : rows.filter((r) => r.key !== "devInfo");
  return (
    <div className="menu-panel" role="dialog" aria-label="Menu">
      <h2 className="heading">Menu</h2>
      <div className="menu-panel__rows">
        {visible.map((r) => (
          <div className="menu-panel__row" key={r.key}>
            <span>{r.label}</span>
            <Toggle label={r.label} on={settings[r.key]} onChange={(v) => onChange({ ...settings, [r.key]: v })} />
          </div>
        ))}
      </div>
    </div>
  );
}
