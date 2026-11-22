"""ControlLink against tools/fake_s3.py running as a subprocess on a free port (no hardware)."""
import asyncio
import importlib.util
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from coach.contracts import ControlLinkAPI
from coach.control import ControlLink

ROOT = Path(__file__).resolve().parents[2]
FAKE = ROOT / "tools" / "fake_s3.py"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Fake:
    def __init__(self):
        self.port = free_port()
        self.proc = subprocess.Popen([sys.executable, "-u", str(FAKE), str(self.port)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, bufsize=1)
        self.lines: queue.Queue[str] = queue.Queue()
        self.seen: list[str] = []
        threading.Thread(target=lambda: [self.lines.put(l.rstrip()) for l in self.proc.stdout], daemon=True).start()
        self.wait_for("listening")

    def wait_for(self, text: str, timeout: float = 3.0) -> str:
        end = time.time() + timeout
        while time.time() < end:
            try:
                line = self.lines.get(timeout=0.05)
            except queue.Empty:
                continue
            self.seen.append(line)
            if text in line:
                return line
        raise AssertionError(f"fake_s3 never printed {text!r}; got {self.seen}")

    def press(self):
        self.proc.stdin.write("\n")
        self.proc.stdin.flush()

    def stop(self):
        self.proc.kill()
        self.proc.wait()


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.stop()


async def until(cond, timeout=2.0):
    end = time.time() + timeout
    while not cond():
        assert time.time() < end, "timed out"
        await asyncio.sleep(0.02)


def test_satisfies_contract():
    assert isinstance(ControlLink("127.0.0.1", 4210), ControlLinkAPI)


def test_online_status_and_c_packets(fake):
    async def run():
        link = await ControlLink("127.0.0.1", fake.port).start()
        assert not link.online
        link.pan, link.tilt, link.laser = 75, 55.5, True
        await until(lambda: link.online)
        await until(lambda: link.status.pan == 75.0 and link.status.laser)
        assert link.status.tilt == 55.5 and link.status.rssi == -50 and link.status.vbat_mv == 5100
        assert link.status.seq > 0
        link.close()
    asyncio.run(run())
    fake.wait_for("C pan=75.0 tilt=55.5 laser=1")


def test_sweep_script(fake):
    spec = importlib.util.spec_from_file_location("sweep", ROOT / "tools" / "sweep.py")
    sweep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sweep)
    link = asyncio.run(sweep.main("127.0.0.1", fake.port, hold=0))
    assert link.status.at > 0
    fake.wait_for("T line 2: 'sweep test'")
    for pan in (60, 90, 120):
        fake.wait_for(f"C pan={pan}.0 tilt=60.0 laser=1")


def test_button(fake):
    async def run():
        link = await ControlLink("127.0.0.1", fake.port).start()
        presses = []
        link.on_button(lambda: presses.append(1))
        link.on_button(lambda: presses.append(2))
        await until(lambda: link.online)
        fake.press()
        await until(lambda: presses == [1, 2])
        link.close()
    asyncio.run(run())
    fake.wait_for("-> B")


def test_text_and_reset(fake):
    async def run():
        link = await ControlLink("127.0.0.1", fake.port).start()
        link.pan, link.laser = 100, True
        link.show_text(4, "this text is longer than twenty chars")
        await asyncio.sleep(0.1)
        link.reset()
        assert (link.pan, link.tilt, link.laser) == (90.0, 60.0, False)
        await until(lambda: link.online and not link.status.laser and link.status.pan == 90.0)
        link.close()
    asyncio.run(run())
    fake.wait_for("T line 4: 'this text is longer '")
    fake.wait_for("R reset")


def test_watchdog(fake):
    async def run():
        link = await ControlLink("127.0.0.1", fake.port).start()
        link.laser = True
        await until(lambda: link.online)
        link.close()        # heartbeat stops
    asyncio.run(run())
    fake.wait_for("laser=1")
    t = time.time()
    fake.wait_for("laser OFF (watchdog)")
    assert time.time() - t < 1.5


def test_unresolvable_host_does_not_crash():
    async def run():
        link = await ControlLink("no-such-host.invalid", 4210).start()
        await asyncio.sleep(0.1)
        assert not link.online
        link.show_text(0, "x")
        link.close()
    asyncio.run(run())


def test_ramp_limits_each_packet_and_reports_moving():
    """Big jumps are spread over several packets (no current spike), then `moving` goes False."""
    import asyncio
    from coach.control import MAX_STEP_DEG, ControlLink, _ramp

    assert _ramp(90, 91) == 91 and _ramp(90, 150) == 90 + MAX_STEP_DEG and _ramp(90, 30) == 90 - MAX_STEP_DEG

    class Tx:
        def __init__(self): self.sent = []
        def sendto(self, data, addr): self.sent.append(data.decode())

    async def go():
        link = ControlLink("127.0.0.1", 9, hz=200)
        link._transport, link._addr, link._resolved_at = Tx(), ("127.0.0.1", 9), 1e18
        link.status.at = 1e18                       # pretend online: no re-resolve
        link.pan, link.tilt = 150.0, 30.0
        assert link.moving
        task = asyncio.create_task(link._pump())
        await asyncio.sleep(0.4)
        task.cancel()
        return link, [m for m in link._transport.sent if m.startswith("C ")]

    link, cs = asyncio.run(go())
    pans = [float(m.split()[1]) for m in cs]
    tilts = [float(m.split()[2]) for m in cs]
    steps = [abs(b - a) for a, b in zip([90.0] + pans, pans)] + [abs(b - a) for a, b in zip([60.0] + tilts, tilts)]
    assert max(steps) <= MAX_STEP_DEG + 1e-9
    assert pans[-1] == 150.0 and tilts[-1] == 30.0 and not link.moving
    link.reset()
    assert (link._out_pan, link._out_tilt) == (90.0, 60.0) and not link.moving
