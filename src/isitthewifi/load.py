"""Saturate the link in one direction and measure throughput, using Cloudflare's speed test endpoints."""
from __future__ import annotations

import threading
import time
import urllib.request
from dataclasses import dataclass, field

BASE = "https://speed.cloudflare.com"
CHUNK = 64 * 1024


@dataclass
class Meter:
    """Thread-safe byte counter with a timeline, for throughput over a window."""
    lock: threading.Lock = field(default_factory=threading.Lock)
    points: list[tuple[float, int]] = field(default_factory=list)  # (monotonic time, cumulative bytes)
    total: int = 0
    errors: int = 0

    def add(self, n: int) -> None:
        with self.lock:
            self.total += n
            self.points.append((time.monotonic(), self.total))

    def mbps(self, t0: float, t1: float) -> float | None:
        with self.lock:
            pts = [p for p in self.points if t0 <= p[0] <= t1]
        if len(pts) < 2 or pts[-1][0] - pts[0][0] < 0.5:
            return None
        return (pts[-1][1] - pts[0][1]) * 8 / (pts[-1][0] - pts[0][0]) / 1e6


class _Body:
    """A file-like upload body that counts bytes as urllib reads them."""

    def __init__(self, size: int, meter: Meter, stop: threading.Event):
        self.left, self.meter, self.stop = size, meter, stop
        self.block = b"\0" * CHUNK

    def read(self, n: int = -1) -> bytes:
        if self.left <= 0 or self.stop.is_set():
            return b""
        k = min(self.left, CHUNK if n is None or n < 0 else min(n, CHUNK))
        self.left -= k
        self.meter.add(k)
        return self.block[:k]


class Load:
    """Run `streams` parallel transfers until stop() is called."""

    def __init__(self, direction: str, base: str = BASE, streams: int = 6, size: int = 25_000_000):
        assert direction in ("down", "up")
        self.direction, self.base, self.streams, self.size = direction, base.rstrip("/"), streams, size
        self.meter = Meter()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> "Load":
        for _ in range(self.streams):
            t = threading.Thread(target=self._worker, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                if self.direction == "down":
                    req = urllib.request.Request(f"{self.base}/__down?bytes={self.size}",
                                                 headers={"User-Agent": "isitthewifi"})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        while not self._stop.is_set():
                            data = r.read(CHUNK)
                            if not data:
                                break
                            self.meter.add(len(data))
                else:
                    size = 25_000_000
                    req = urllib.request.Request(f"{self.base}/__up", data=_Body(size, self.meter, self._stop),
                                                 method="POST", headers={"Content-Length": str(size),
                                                                         "Content-Type": "application/octet-stream",
                                                                         "User-Agent": "isitthewifi"})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        r.read()
            except Exception:
                self.meter.errors += 1
                if self._stop.wait(0.2):
                    return
