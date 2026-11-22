import { useState } from "react";

/** Image that falls back to the beige placeholder box from the design if it can't load. */
export default function Media({
  src,
  className,
  alt,
  onLoad,
}: {
  src?: string;
  className: string;
  alt: string;
  onLoad?: (img: HTMLImageElement) => void;
}) {
  const [failedSrc, setFailedSrc] = useState<string>();
  if (!src || failedSrc === src) return <div className={className} aria-label={alt} />;
  return (
    <img
      className={className}
      src={src}
      alt={alt}
      onLoad={onLoad && ((e) => onLoad(e.currentTarget))}
      onError={() => setFailedSrc(src)}
    />
  );
}
