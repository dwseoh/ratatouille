"""UDP link to the ESP32-S3 (protocol: docs/protocol.md). Implements contracts.ControlLinkAPI.

Streams `C pan tilt laser seq` at 30 Hz nonstop (it doubles as the heartbeat), parses `S` status and `B`
(BOOT button) from the S3. Test without hardware: `python tools/fake_s3.py` + `python ../tools/sweep.py`.
"""
import asyncio
import socket
import time
from dataclasses import dataclass
from typing import Callable

SAFE_PAN, SAFE_TILT = 90.0, 60.0
ONLINE_S = 1.0          # online = an S arrived in the last second
RERESOLVE_S = 5.0       # offline this long -> resolve CTL_HOST again (mDNS can change IP)


@dataclass
class CtlStatus:
    pan: float = 0.0
    tilt: float = 0.0
    laser: bool = False
    rssi: int = 0
    vbat_mv: int = 0
    seq: int = 0           # last C seq the S3 saw
    uptime_ms: int = 0
    at: float = 0.0        # time.time() it arrived


class ControlLink(asyncio.DatagramProtocol):
    def __init__(self, host: str, port: int = 4210, hz: float = 30.0):
        self.host, self.port, self.period = host, port, 1.0 / hz
        self.pan, self.tilt, self.laser = SAFE_PAN, SAFE_TILT, False
        self.status = CtlStatus()
        self._seq = 0
        self._transport = None
        self._addr = None
        self._button_cbs: list[Callable[[], None]] = []
        self._resolved_at = 0.0
        self._resolving = False
        self._task = None

    # ── ControlLinkAPI ──────────────────────────────────────────────────────
    @property
    def online(self) -> bool:
        return time.time() - self.status.at < ONLINE_S

    def show_text(self, line: int, text: str) -> None:
        """Put up to 20 chars on display line 0-4."""
        self._send(f"T {int(line)} {str(text).replace(chr(10), ' ')[:20]}")

    def reset(self) -> None:
        """Laser off, servos centred. Also resets our own state so the next C doesn't undo it."""
        self.pan, self.tilt, self.laser = SAFE_PAN, SAFE_TILT, False
        self._send("R")

    def on_button(self, cb: Callable[[], None]) -> None:
        self._button_cbs.append(cb)

    async def start(self):
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=("0.0.0.0", 0))
        await self._resolve()
        self._task = asyncio.create_task(self._pump())
        return self

    def close(self):
        if self._task:
            self._task.cancel()
        if self._transport:
            self._transport.close()

    # ── internals ───────────────────────────────────────────────────────────
    async def _resolve(self):
        self._resolving, self._resolved_at = True, time.time()
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                self.host, self.port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
            self._addr = infos[0][4]
        except OSError as e:
            print(f"[control] can't resolve {self.host}: {e} (set CTL_HOST to the IP on TFT line 1)")
        finally:
            self._resolving = False

    async def _pump(self):
        while True:
            self._seq += 1
            self._send(f"C {self.pan:.1f} {self.tilt:.1f} {int(bool(self.laser))} {self._seq}")
            now = time.time()
            if (not self._resolving and now - self.status.at > RERESOLVE_S
                    and now - self._resolved_at > RERESOLVE_S):
                asyncio.create_task(self._resolve())
            await asyncio.sleep(self.period)

    def _send(self, msg: str):
        if self._transport and self._addr:
            self._transport.sendto(msg.encode(), self._addr)

    def datagram_received(self, data, addr):
        parts = data.decode(errors="ignore").split()
        if not parts:
            return
        if parts[0] == "S" and len(parts) >= 8:
            try:
                self.status = CtlStatus(float(parts[1]), float(parts[2]), parts[3] == "1", int(parts[4]),
                                        int(parts[5]), int(parts[6]), int(parts[7]), time.time())
            except ValueError:
                pass
        elif parts[0] == "B":
            for cb in list(self._button_cbs):
                try:
                    cb()
                except Exception as e:  # one bad callback must not kill the link
                    print(f"[control] button callback failed: {e!r}")
