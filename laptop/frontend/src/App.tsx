import { useAppState } from "./api";
import { useMenu, useSettings } from "./hooks/useSettings";
import Dashboard from "./screens/Dashboard";
import MenuPanel from "./screens/MenuPanel";

export default function App() {
  const { state, offline } = useAppState();
  const [settings, setSettings] = useSettings();
  const [menuOpen, toggleMenu] = useMenu();

  return (
    <Dashboard
      state={state}
      offline={offline}
      showDevInfo={settings.devInfo}
      onMenu={toggleMenu}
      menu={menuOpen ? <MenuPanel settings={settings} onChange={setSettings} /> : null}
    />
  );
}
