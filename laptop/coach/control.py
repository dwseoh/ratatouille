"""UDP link to the ESP32-S3. Streams control packets at 30 Hz (they double as a heartbeat)."""
import asyncio
import socket
import time
from dataclasses import dataclass


@dataclass
class CtlStatus:
    pan: float = 0.0
    tilt: float = 0.0
    laser: bool = False
    rssi: int = 0
    vbat_mv: int = 0
    seq: int = 0
    at: float = 0.0


class ControlLink(asyncio.DatagramProtocol):
    def __init__(self, host: str, port: int, hz: float = 30.0):
        self.host, self.port, self.period = host, port, 1.0 / hz
        self.pan, self.tilt, self.laser = 90.0, 60.0, False
        self.status = CtlStatus()
        self._seq = 0
        self._transport = None
        self._addr = None

    @property
    def online(self) -> bool:
        return time.time() - self.status.at < 1.0

    async def start(self):
        loop = asyncio.get_running_loop()
        self._addr = (socket.gethostbyname(self.host), self.port)  # resolve mDNS once
        self._transport, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=("0.0.0.0", 0))
        asyncio.create_task(self._pump())
        return self

    async def _pump(self):
        while True:
            self._seq += 1
            self._send(f"C {self.pan:.1f} {self.tilt:.1f} {int(self.laser)} {self._seq}")
            await asyncio.sleep(self.period)

    def _send(self, msg: str):
        if self._transport:
            self._transport.sendto(msg.encode(), self._addr)

    def show_text(self, line: int, text: str):
        """Put up to 20 chars on display line 0-4."""
        self._send(f"T {line} {text[:20]}")

    def datagram_received(self, data, addr):
        parts = data.decode(errors="ignore").split()
        if parts and parts[0] == "S" and len(parts) >= 7:
            self.status = CtlStatus(float(parts[1]), float(parts[2]), parts[3] == "1",
                                    int(parts[4]), int(parts[5]), int(parts[6]), time.time())
