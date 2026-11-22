/** Figma component "bar" (22:121). `value` is 0..1 and fills the track; omit for an empty track. */
export default function Bar({ value = 0 }: { value?: number }) {
  const pct = Math.max(0, Math.min(1, value)) * 100;
  return (
    <div className="bar" role="presentation">
      <div className="bar__fill" style={{ width: `${pct}%` }} />
    </div>
  );
}
