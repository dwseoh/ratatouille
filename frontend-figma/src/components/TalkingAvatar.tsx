import { useEffect, useRef, useState } from "react";

const FPS = 6;
/** Ping-pong over 4 frames (0-based): 1,2,3,4,3,2 then repeat. Index 0 is the rest pose. */
const SEQUENCE = [0, 1, 2, 3, 2, 1];
/** Keep animating this long after the flag drops, so a flickering voice detector doesn't make the avatar jitter. */
const HOLD_MS = 300;

/**
 * A character that plays its talking loop while `active` is true.
 * When `active` turns false it keeps going for HOLD_MS, then finishes the current loop and rests on frame 1.
 * All frames are in the DOM at once (only one visible) so nothing flashes while an image decodes.
 */
export default function TalkingAvatar({
  frames,
  active,
  label,
}: {
  frames: readonly string[];
  active: boolean;
  label: string;
}) {
  const [pos, setPos] = useState(0); // index into SEQUENCE
  const posRef = useRef(0);
  const activeRef = useRef(active);
  const lastChangeAt = useRef(0); // when `active` last changed (the drop time matters)
  const timer = useRef<number | null>(null);

  const stop = () => {
    if (timer.current !== null) window.clearInterval(timer.current);
    timer.current = null;
  };

  useEffect(() => {
    activeRef.current = active;
    lastChangeAt.current = performance.now();
    if (!active || timer.current !== null) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return; // stay on frame 1

    timer.current = window.setInterval(() => {
      const next = (posRef.current + 1) % SEQUENCE.length;
      posRef.current = next;
      setPos(next);
      const holding = activeRef.current || performance.now() - lastChangeAt.current < HOLD_MS;
      if (next === 0 && !holding) stop(); // loop finished and nobody is talking: rest on frame 1
    }, 1000 / FPS);
  }, [active]);

  useEffect(() => stop, []); // clear the timer on unmount

  const visible = SEQUENCE[pos];
  return (
    <div className="avatar" role="img" aria-label={label}>
      {frames.map((src, i) => (
        <img key={src} src={src} alt="" style={{ opacity: i === visible ? 1 : 0 }} draggable={false} />
      ))}
    </div>
  );
}
