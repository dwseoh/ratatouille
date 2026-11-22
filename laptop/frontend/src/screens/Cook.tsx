import { MOCK, useAppState } from "../api";
import { REMY_FRAMES } from "../assets/frames";
import MenuButton from "../components/MenuButton";
import Media from "../components/Media";
import TalkingAvatar from "../components/TalkingAvatar";
import TextBubble from "../components/TextBubble";
import Title from "../components/Title";
import { useMenu, useSettings } from "../hooks/useSettings";
import MenuPanel from "./MenuPanel";

/** The phone page (served at /cook): one step at a time, large text, for reading while cooking. */
export default function Cook() {
  const { state, offline } = useAppState();
  const [settings, setSettings] = useSettings();
  const [menuOpen, toggleMenu] = useMenu();
  const st = state?.step;

  return (
    <main className="cook">
      <header className="dashboard__header">
        <Title />
        <div className="menu-anchor">
          <MenuButton onClick={toggleMenu} />
          {menuOpen && <MenuPanel settings={settings} onChange={setSettings} showDevToggle={false} />}
        </div>
      </header>

      {offline && (
        <div className="cook__offline">
          <TextBubble tone="error">Reconnecting…</TextBubble>
        </div>
      )}

      <section className="cook__progress" aria-label={st ? `Step ${st.n} of ${st.total}` : "Loading"}>
        <p className="bold">{st ? `Step ${st.n} of ${st.total}` : "…"}</p>
        <div className="dots" aria-hidden="true">
          {Array.from({ length: st?.total ?? 0 }, (_, i) => (
            <span key={i} className={i + 1 < st!.n ? "done" : i + 1 === st!.n ? "now" : ""} />
          ))}
        </div>
      </section>

      <section className="cook__step">
        <h1 className="heading">{st?.short ?? "…"}</h1>
        <p>{st?.goal ?? ""}</p>
      </section>

      <div className="cook__photo-wrap">
        <Media
          className="cook__photo"
          src={MOCK ? undefined : st?.image_url}
          alt={st ? `What step ${st.n} should look like` : "What this step should look like"}
        />
        {st?.image_note && <p className="cook__note">{st.image_note}</p>}
      </div>

      {st && st.how.length > 0 && (
        <section className="card">
          <h2 className="bold">How to do this step</h2>
          <ol>
            {st.how.map((h, i) => (
              <li key={i}>{h}</li>
            ))}
          </ol>
        </section>
      )}

      {st && st.tips.length > 0 && (
        <section className="card">
          <h2 className="bold">Good to know</h2>
          <ul>
            {st.tips.map((t, i) => (
              <li key={i}>{t}</li>
            ))}
          </ul>
        </section>
      )}

      <section className="card cook__say" aria-live="polite">
        <TalkingAvatar frames={REMY_FRAMES} active={!!state?.speaking} label="Remy" />
        <p className="bold">{state?.caption || "…"}</p>
      </section>
    </main>
  );
}
