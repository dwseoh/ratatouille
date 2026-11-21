"""Judge-facing dashboard: annotated live feed + state + latency numbers.

TODO (Sun AM): FastAPI app with
- GET /stream: MJPEG of annotated frames (YOLO boxes, dot, target)
- GET /state: JSON {step, events, transcript, latency: {endpoint, ttft, tts_first_audio}, links}
- GET /: a single HTML page polling /state every 250 ms
"""
import cv2


def annotate(frame, dets, dot, target_name):
    out = frame.copy()
    for d in dets:
        x1, y1, x2, y2 = map(int, d.box)
        color = (0, 200, 0) if d.name == target_name else (200, 200, 200)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        cv2.putText(out, f"{d.name} {d.conf:.2f}", (x1, y1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
    if dot is not None:
        cv2.circle(out, (int(dot[0]), int(dot[1])), 12, (0, 0, 255), 2)
    return out
