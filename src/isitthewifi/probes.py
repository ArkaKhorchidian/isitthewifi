"""Latency probes that need no admin rights.

`ping` needs raw sockets (root on Linux). Instead we time tiny DNS queries over UDP, which home
routers and public resolvers answer instantly, and fall back to timing TCP handshakes, where a
refused connection still costs exactly one round trip.
"""
from __future__ import annotations

import os
import socket
import statistics
import struct
import threading
import time
from dataclasses import dataclass, field


def dns_query(name: str, qid: int) -> bytes:
    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.strip(".").split("."))
    return struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + labels + b"\x00" + struct.pack(">HH", 1, 1)


@dataclass
class Sample:
    t: float  # when it was sent (monotonic)
    rtt: float | None  # seconds; None = lost


@dataclass
class Series:
    samples: list[Sample] = field(default_factory=list)

    def window(self, t0: float, t1: float) -> list[Sample]:
        return [s for s in self.samples if t0 <= s.t < t1]


def summarize(samples: list[Sample]) -> dict:
    ok = [s.rtt for s in samples if s.rtt is not None]
    n = len(samples)
    if not ok:
        return {"n": n, "loss": 1.0 if n else 0.0, "median": None, "p90": None, "jitter": None}
    ok_sorted = sorted(ok)
    jitter = statistics.mean(abs(a - b) for a, b in zip(ok, ok[1:])) if len(ok) > 1 else 0.0
    return {"n": n, "loss": 1 - len(ok) / n, "median": statistics.median(ok),
            "p90": ok_sorted[min(len(ok_sorted) - 1, int(0.9 * len(ok_sorted)))], "jitter": jitter}


class Prober(threading.Thread):
    """Sends one probe every `interval` seconds to `host` until stopped."""

    def __init__(self, host: str, method: str, port: int, interval: float = 0.1, timeout: float = 1.0,
                 name: str = "example.com"):
        super().__init__(daemon=True)
        self.host, self.method, self.port = host, method, port
        self.interval, self.timeout, self.qname = interval, timeout, name
        self.series = Series()
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        sock = None
        if self.method == "dns":
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(self.timeout)
        while not self._stop.is_set():
            start = time.monotonic()
            rtt = self.once(sock)
            self.series.samples.append(Sample(start, rtt))
            elapsed = time.monotonic() - start
            self._stop.wait(max(0.0, self.interval - elapsed))
        if sock:
            sock.close()

    def once(self, sock=None) -> float | None:
        if self.method == "dns":
            return udp_dns_rtt(self.host, self.port, sock, self.timeout, self.qname)
        return tcp_rtt(self.host, self.port, self.timeout)


def udp_dns_rtt(host: str, port: int = 53, sock=None, timeout: float = 1.0, name: str = "example.com") -> float | None:
    own = sock is None
    if own:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
    qid = int.from_bytes(os.urandom(2), "big")
    try:
        t0 = time.perf_counter()
        sock.sendto(dns_query(name, qid), (host, port))
        deadline = t0 + timeout
        while True:
            sock.settimeout(max(0.001, deadline - time.perf_counter()))
            data, _ = sock.recvfrom(2048)
            if len(data) >= 2 and int.from_bytes(data[:2], "big") == qid:  # ignore late answers to old probes
                return time.perf_counter() - t0
    except (OSError, socket.timeout):
        return None
    finally:
        if own:
            sock.close()


def tcp_rtt(host: str, port: int, timeout: float = 1.0) -> float | None:
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return time.perf_counter() - t0
    except ConnectionRefusedError:  # the RST came back: that's still one round trip
        return time.perf_counter() - t0
    except OSError:
        return None


def pick_method(host: str, tries: int = 3, prefer_tcp: bool = False) -> tuple[str, int] | None:
    """Find a way to probe `host`: DNS over UDP if it answers, else a TCP port.

    For a home router, TCP handshakes are answered by its kernel, which is steadier than its DNS
    forwarder (a busy userspace process), so `prefer_tcp` tries those first.
    """
    def dns():
        return any(udp_dns_rtt(host, 53, timeout=0.7) is not None for _ in range(tries))

    def tcp():
        for port in (80, 443, 53):
            if tcp_rtt(host, port, timeout=0.7) is not None:
                return port
        return None

    if prefer_tcp and (port := tcp()):
        return ("tcp", port)
    if dns():
        return ("dns", 53)
    port = None if prefer_tcp else tcp()
    return ("tcp", port) if port else None


def resolve_time(name: str) -> float | None:
    """How long the system resolver takes for a name it can't have cached."""
    t0 = time.perf_counter()
    try:
        socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        pass  # NXDOMAIN is still a full recursive lookup
    except OSError:
        return None
    return time.perf_counter() - t0


def random_name(domain: str = "example.com") -> str:
    return f"{os.urandom(6).hex()}.{domain}"
