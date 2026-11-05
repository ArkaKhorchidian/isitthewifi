"""Run the test: idle, then saturate download, then upload, probing the router and the internet the whole time."""
from __future__ import annotations

import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import probes, wifi
from .load import BASE, Load

INTERNET = "1.1.1.1"
WARMUP = 2.0  # seconds of each load phase to ignore while TCP ramps up


@dataclass
class Config:
    idle: float = 5.0
    download: float = 10.0
    upload: float = 10.0
    internet: str = INTERNET
    internet_port: int | None = None  # None = autodetect
    gateway: str | None = None  # None = autodetect
    gateway_port: int | None = None
    base: str = BASE
    streams: int = 6
    interval: float = 0.1
    dns_lookups: int = 5
    resolver_domain: str = "example.com"
    read_link: bool = True


@dataclass
class Phase:
    name: str
    t0: float = 0.0
    t1: float = 0.0
    mbps: float | None = None


@dataclass
class Result:
    link: wifi.Link = field(default_factory=wifi.Link)
    gateway: str | None = None
    phases: dict[str, Phase] = field(default_factory=dict)
    router: dict[str, dict] = field(default_factory=dict)  # phase -> summary
    internet: dict[str, dict] = field(default_factory=dict)
    dns_system: float | None = None  # seconds, median of uncached lookups via the OS resolver
    dns_public: float | None = None  # same, asked straight to 1.1.1.1
    errors: list[str] = field(default_factory=list)


Status = Callable[[str, dict], None]  # (phase, live numbers) -> None


def _start_prober(host: str, port: int | None, interval: float, prefer_tcp: bool = False) -> probes.Prober | None:
    if port is None:
        found = probes.pick_method(host, prefer_tcp=prefer_tcp)
        if not found:
            return None
        method, port = found
    else:
        method = "dns" if probes.udp_dns_rtt(host, port, timeout=0.7) is not None else "tcp"
    p = probes.Prober(host, method, port, interval=interval)
    p.start()
    return p


def _recent(p: probes.Prober | None, seconds: float = 1.0) -> float | None:
    if not p:
        return None
    now = time.monotonic()
    ok = [s.rtt for s in list(p.series.samples) if s.t > now - seconds and s.rtt is not None]
    return statistics.median(ok) if ok else None


def run(cfg: Config | None = None, status: Status | None = None) -> Result:
    cfg = cfg or Config()
    status = status or (lambda phase, live: None)
    res = Result()
    status("setup", {})

    # find the router and read the radio while we get started
    link_box: list[wifi.Link] = []
    lt = None
    if cfg.read_link:
        gw = wifi.default_gateway()
        iface = gw[1] if gw else ""
        lt = threading.Thread(target=lambda: link_box.append(wifi.link_info(iface)), daemon=True)
        lt.start()
        if cfg.gateway is None and gw:
            cfg.gateway = gw[0]
    res.gateway = cfg.gateway

    router = _start_prober(cfg.gateway, cfg.gateway_port, cfg.interval, prefer_tcp=True) if cfg.gateway else None
    net = _start_prober(cfg.internet, cfg.internet_port, cfg.interval)
    if not net:
        res.errors.append(f"couldn't reach {cfg.internet}; are you online?")
    if cfg.gateway and not router:
        res.errors.append(f"your router ({cfg.gateway}) doesn't answer probes; skipping the Wi-Fi leg")

    def watch(name: str, seconds: float, load: Load | None) -> None:
        ph = Phase(name, time.monotonic())
        res.phases[name] = ph
        end = ph.t0 + seconds
        while (now := time.monotonic()) < end:
            live = {"router": _recent(router), "internet": _recent(net), "progress": (now - ph.t0) / seconds,
                    "mbps": load.meter.mbps(now - 1.5, now) if load else None}
            status(name, live)
            time.sleep(0.1)
        ph.t1 = time.monotonic()
        if load:
            load.stop()
            ph.mbps = load.meter.mbps(ph.t0 + WARMUP, ph.t1)
            if load.meter.total == 0:
                res.errors.append(f"the {name} test couldn't connect to {cfg.base}")

    watch("idle", cfg.idle, None)
    if cfg.download > 0:
        watch("download", cfg.download, Load("down", cfg.base, cfg.streams).start())
        time.sleep(0.5)  # let queues drain
    if cfg.upload > 0:
        watch("upload", cfg.upload, Load("up", cfg.base, cfg.streams).start())

    for p in (router, net):
        if p:
            p.stop()
    for name, ph in res.phases.items():
        t0 = ph.t0 + (WARMUP if name != "idle" else 0)
        if router:
            res.router[name] = probes.summarize(router.series.window(t0, ph.t1))
        if net:
            res.internet[name] = probes.summarize(net.series.window(t0, ph.t1))

    # DNS: uncached names through the OS resolver vs. straight to a fast public resolver
    status("dns", {})
    sys_t = [probes.resolve_time(probes.random_name(cfg.resolver_domain)) for _ in range(cfg.dns_lookups)]
    pub_t = [probes.udp_dns_rtt(cfg.internet, cfg.internet_port or 53, timeout=2,
                                name=probes.random_name(cfg.resolver_domain)) for _ in range(cfg.dns_lookups)]
    sys_ok, pub_ok = [x for x in sys_t if x is not None], [x for x in pub_t if x is not None]
    res.dns_system = statistics.median(sys_ok) if sys_ok else None
    res.dns_public = statistics.median(pub_ok) if pub_ok else None

    if lt:
        lt.join(timeout=15)
        if link_box:
            res.link = link_box[0]
    status("done", {})
    return res
