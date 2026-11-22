import type { ReactNode } from "react";

/** Figma component "text-bubble" (22:66). Border color: neutral = beige, ok = green, error = red. */
export type BubbleTone = "neutral" | "ok" | "error";

export default function TextBubble({ tone = "neutral", children }: { tone?: BubbleTone; children: ReactNode }) {
  return <span className={`bubble bubble--${tone}`}>{children}</span>;
}
