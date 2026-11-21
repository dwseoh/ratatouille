"""Hardware check: is the S3 online, does the camera stream, does BOOT work? Prints PASS/FAIL per item.

    cd laptop && python ../tools/check_hw.py                         # uses CTL_HOST / CAM_URL from .env
    python ../tools/check_hw.py --ctl 172.20.10.5 --cam http://172.20.10.6:81/stream
    python ../tools/check_hw.py --skip-cam                           # S3 only
    python ../tools/check_hw.py --skip-s3 --cam 0                    # camera only (0 = webcam)
    python ../tools/check_hw.py --button 15                          # also wait 15 s for a BOOT press

The servo wiggle is small (pan 80..100) and the laser stays OFF the whole time.
Saves one camera frame as check_hw.jpg so you can see the orientation.
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "laptop"))
from coach.config import CONFIG  # noqa: E402

results: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


async def check_s3(host: str, port: int, button_s: float) -> None:
    from coach.control import ControlLink
    print(f"\nS3 at {host}:{port}")
    link = await ControlLink(host, port).start()
    pressed = asyncio.Event()
    link.on_button(pressed.set)          # listen from the start, so an early press still counts
    try:
        t0 = time.monotonic()
        while not link.online and time.monotonic() - t0 < 5:
            await asyncio.sleep(0.1)
        s = link.status
        report("S3 online (status S within 5 s)", link.online,
               f"rssi {s.rssi} dBm, vbat {s.vbat_mv} mV, up {s.uptime_ms / 1000:.0f} s" if link.online
               else "check: same hotspot? IP on TFT line 1? CTL_HOST in .env?")
        if not link.online:
            return
        link.show_text(0, "hw check")
        link.laser = False
        for pan in (80, 100, 90):
            link.pan = pan
            await asyncio.sleep(0.6)
        moved = abs(link.status.pan - 90) < 3
        report("S3 echoes the commanded pan", moved, f"status pan {link.status.pan:.0f}")
        seq_ok = link.status.seq > 0
        report("S3 receives C packets", seq_ok, f"last seq {link.status.seq}")
        if button_s > 0:
            print(f"  ... press BOOT on the S3 within {button_s:.0f} s", flush=True)
            try:
                await asyncio.wait_for(pressed.wait(), button_s)
                report("BOOT button (B message)", True)
            except asyncio.TimeoutError:
                report("BOOT button (B message)", False, "no B received: firmware without the BOOT change?")
        link.show_text(0, "hw check done")
        await asyncio.sleep(0.2)
    finally:
        link.laser = False
        link.close()


def check_cam(url, seconds: float = 3.0) -> None:
    from coach.frames import FrameGrabber
    import cv2
    print(f"\nCamera {url!r}")
    g = FrameGrabber(url).start()
    try:
        t0 = time.monotonic()
        while g.latest()[0] is None and time.monotonic() - t0 < 8:
            time.sleep(0.1)
        frame, _ = g.latest()
        report("camera gives frames (within 8 s)", frame is not None,
               f"{frame.shape[1]}x{frame.shape[0]}" if frame is not None
               else "check: URL in a browser, same hotspot, CAM_URL in .env")
        if frame is None:
            return
        stamps, last, t0 = set(), None, time.monotonic()
        while time.monotonic() - t0 < seconds:
            _, t = g.latest()
            if t != last:
                stamps.add(t)
                last = t
            time.sleep(0.005)
        fps = len(stamps) / seconds
        report("camera >= 15 fps", fps >= 15, f"{fps:.1f} fps")
        out = Path.cwd() / "check_hw.jpg"
        cv2.imwrite(str(out), g.latest()[0])
        print(f"  saved {out} (is it upright?)")
    finally:
        g.stop()


async def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ctl", default=CONFIG.ctl_host)
    ap.add_argument("--port", type=int, default=CONFIG.ctl_port)
    ap.add_argument("--cam", default=CONFIG.cam_url)
    ap.add_argument("--button", type=float, default=0, help="seconds to wait for a BOOT press (0 = skip)")
    ap.add_argument("--skip-s3", action="store_true")
    ap.add_argument("--skip-cam", action="store_true")
    a = ap.parse_args(argv)
    results.clear()
    if not a.skip_s3:
        await check_s3(a.ctl, a.port, a.button)
    if not a.skip_cam:
        await asyncio.to_thread(check_cam, a.cam)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + ("" if not failed else
          ": " + "; ".join(r[0] for r in failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
