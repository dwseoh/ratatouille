"""Sweep the pan servo through the control link. Run with: cd laptop && python ../tools/sweep.py [host] [port]

Start `python tools/fake_s3.py` first (or point host at the real S3, e.g. coach-ctl.local).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "laptop"))
from coach.control import ControlLink  # noqa: E402


async def main(host: str = "127.0.0.1", port: int = 4210, hold: float = 10.0):
    link = await ControlLink(host, port).start()
    link.on_button(lambda: print("BUTTON", flush=True))
    link.show_text(2, "sweep test")
    for pan in list(range(60, 121, 5)) + list(range(120, 59, -5)):
        link.pan, link.laser = pan, True
        await asyncio.sleep(0.1)
    print("online:", link.online, link.status, flush=True)
    link.laser = False
    if hold:
        print(f"press Enter in fake_s3 (or BOOT on the S3) within {hold:.0f} s to test BUTTON", flush=True)
        await asyncio.sleep(hold)
    link.close()
    return link


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1",
                     int(sys.argv[2]) if len(sys.argv) > 2 else 4210))
