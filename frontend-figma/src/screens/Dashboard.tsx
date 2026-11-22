import { useEffect, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { MOCK, QR_URL, STREAM_URL, useCookUrl } from "../api";
import type { AppState } from "../api";
import Bar from "../components/Bar";
import MenuButton from "../components/MenuButton";
import Media from "../components/Media";
import TalkingAvatar from "../components/TalkingAvatar";
import { REMY_FRAMES, USER_FRAMES } from "../assets/frames";
import TextBubble from "../components/TextBubble";
import Title from "../components/Title";

const MOCK_EARLIER = ["Let’s begin!", "The first step is to cut the avocado in half."]; // mock mode only
const LATENCY_MAX_MS = 2000; // a bar is full at this many ms
const LATENCY_ROWS = [
  { label: "First word", key: "first_token" },
  { label: "First sound", key: "first_audio" },
  { label: "Laser moved", key: "point_called" },
  { label: "On target", key: "on_target" },
] as const;

function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`card ${className}`}>{children}</section>;
}

/** Figma frame "dashboard" (20:2). */
export default function Dashboard({
  state,
  offline,
  showDevInfo,
  onMenu,
  menu,
}: {
  state: AppState | null;
  offline: boolean;
  showDevInfo: boolean;
  onMenu: () => void;
  menu: ReactNode;
}) {
  // The backend only sends the sentence being spoken now; keep the two before it for the lighter lines.
  const history = useRef<string[]>(MOCK ? [...MOCK_EARLIER] : []);
  const caption = state?.caption ?? "";
  const [earlier, setEarlier] = useState<string[]>(MOCK ? MOCK_EARLIER : []);
  useEffect(() => {
    if (!caption) return;
    const h = history.current;
    if (h[h.length - 1] === caption) return;
    h.push(caption);
    if (h.length > 3) h.shift();
    setEarlier(h.slice(0, -1));
  }, [caption]);

  // The camera box takes the real shape of the video, so there are no beige bars around it. Until the first
  // frame arrives (or in mock mode) it uses the default from index.css.
  const [camAspect, setCamAspect] = useState<number>();
  const camVars = camAspect ? ({ "--camera-aspect": camAspect } as CSSProperties) : undefined;

  const links = state?.links;
  const camOk = !!links?.camera;
  const s3Ok = !!links?.s3;
  const connected = !offline && camOk && s3Ok;
  const events = state?.events ?? [];
  const cookUrl = useCookUrl();

  // Pieces shared by both layouts. With developer info off they sit inside the two cards (big pictures);
  // with it on, the pictures move to a full-height third column so the dev card can't squash them.
  const stepBlock = (
    <div className="step">
      <h2 className="heading">{state ? `${state.step.n}. ${state.step.short}` : "…"}</h2>
      <p>{state?.step.goal ?? ""}</p>
    </div>
  );
  const coachBlock = (
    <div className="coach">
      <TalkingAvatar frames={REMY_FRAMES} active={!!state?.speaking} label="Remy" />
      <div className="coach__text">
        {earlier.map((s, i) => (
          <p key={i}>{s}</p>
        ))}
        {caption && <p className="bold">{caption}</p>}
      </div>
    </div>
  );
  const bubblesBlock = (
    <div className="bubbles">
      {state?.session === "active" ? (
        <TextBubble tone="ok">Cooking ✓</TextBubble>
      ) : (
        <TextBubble>Waiting for “Hey Remy!”</TextBubble>
      )}
      <TextBubble tone={camOk ? "ok" : "error"}>{camOk ? "Camera ✓" : "Camera ✗"}</TextBubble>
      <TextBubble tone={s3Ok ? "ok" : "error"}>{s3Ok ? "S3 ✓" : "S3 ✗"}</TextBubble>
      <TextBubble tone={connected ? "ok" : "error"}>{connected ? "Connected" : "Disconnected"}</TextBubble>
    </div>
  );
  const userBlock = (
    <div className="user-row">
      <TalkingAvatar frames={USER_FRAMES} active={!!state?.user_talking} label="You" />
      <div className="user-row__text">
        <p>{state?.transcript ?? ""}</p>
      </div>
    </div>
  );
  // "192.168.1.20:8000/cook" -> host+port, then path; the line may break between them
  const linkText = cookUrl?.replace(/^https?:\/\//, "") ?? "";
  const slash = linkText.indexOf("/");
  const linkHost = slash < 0 ? linkText : linkText.slice(0, slash);
  const linkPath = slash < 0 ? "" : linkText.slice(slash);
  const qr = (
    <div className="qr-col">
      {MOCK ? (
        <div className="media media--qr" />
      ) : (
        <Media className="media media--qr" src={QR_URL} alt="QR code to the phone page" />
      )}
      <a className="qr-link" href={cookUrl} target="_blank" rel="noreferrer">
        {linkHost}
        <wbr />
        {linkPath}
      </a>
    </div>
  );
  const recipe = (
    <Media
      className="media media--recipe"
      src={MOCK ? undefined : state?.step.image_url}
      alt={state ? `What step ${state.step.n} should look like` : "Recipe image"}
    />
  );
  const camera = MOCK ? (
    <div className="camera" />
  ) : (
    <Media
      className="camera"
      src={STREAM_URL}
      alt="Camera view"
      onLoad={(img) => img.naturalWidth && img.naturalHeight && setCamAspect(img.naturalWidth / img.naturalHeight)}
    />
  );
  const how = state?.step.how ?? [];
  const howBlock = how.length > 0 && (
    <div className="how">
      <p className="bold">How to do this step</p>
      <ol>
        {how.map((h, i) => (
          <li key={i}>{h}</li>
        ))}
      </ol>
    </div>
  );
  const laser = state?.laser;
  const laserText = !laser?.target ? "off" : laser.on_target ? `${laser.target} ✓` : `pointing at ${laser.target}`;
  const seen = state?.visible ?? [];
  const battery = state?.links.vbat_mv ?? 0;
  const devInfoBlock = (
    <div className="devinfo">
      <p>
        Laser: <b>{laserText}</b>
      </p>
      <p>
        Sees: <b>{seen.length ? seen.join(", ") : "nothing"}</b>
      </p>
      <div className="devinfo__badges">
        {battery > 0 && <TextBubble>Battery {(battery / 1000).toFixed(2)} V</TextBubble>}
        {links?.camera_ignored && <TextBubble tone="error">Camera ignored</TextBubble>}
      </div>
    </div>
  );
  const devCard = (
    <section className="card dev">
      <div className="dev__col">
        <h2 className="heading">Latency of last turn</h2>
        <div className="latency">
          <div className="latency__labels">
            {LATENCY_ROWS.map((r) => (
              <p key={r.key}>{r.label}</p>
            ))}
          </div>
          <div className="latency__bars">
            {LATENCY_ROWS.map((r) => (
              <Bar key={r.key} value={(state?.latency[r.key] ?? 0) / LATENCY_MAX_MS} />
            ))}
          </div>
          <div className="latency__values">
            {LATENCY_ROWS.map((r) => {
              const ms = state?.latency[r.key];
              return <p key={r.key}>{ms === undefined ? "-" : `${ms} ms`}</p>;
            })}
          </div>
        </div>
      </div>
      <div className="dev__col">
        <h2 className="heading">Event log</h2>
        <div className="events">
          {events.map((e, i) => (
            <p key={i}>
              <span className="events__t">{new Date(e.t * 1000).toLocaleTimeString([], { hour12: false })}</span>{" "}
              {e.text}
            </p>
          ))}
        </div>
      </div>
    </section>
  );

  return (
    <main className="dashboard">
      <header className="dashboard__header">
        <Title />
        <div className="menu-anchor">
          <MenuButton onClick={onMenu} />
          {menu}
        </div>
      </header>

      {showDevInfo ? (
        <>
        <div className="dashboard__main dashboard__main--dev" style={camVars}>
          <Card>
            {stepBlock}
            {coachBlock}
            {howBlock}
          </Card>
          <Card>
            {bubblesBlock}
            {userBlock}
            {devInfoBlock}
          </Card>
          <Card className="area-media">
            <div className="media-row media-row--stacked">
              {qr}
              {recipe}
            </div>
            <div className="camera-wrap">{camera}</div>
          </Card>
        </div>
        {devCard}
        </>
      ) : (
        <div className="dashboard__main" style={camVars}>
          <Card>
            {stepBlock}
            {coachBlock}
            {howBlock}
            <div className="media-row">
              {qr}
              {recipe}
            </div>
          </Card>
          <Card>
            {bubblesBlock}
            {userBlock}
            <div className="camera-wrap">{camera}</div>
          </Card>
        </div>
      )}
    </main>
  );
}
