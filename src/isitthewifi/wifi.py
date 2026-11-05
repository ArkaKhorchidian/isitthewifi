"""Find the router and read the Wi-Fi link quality, on macOS, Linux and Windows, without admin rights.

Every OS-specific bit is a pure parser over command output, so it can be tested anywhere.
"""
from __future__ import annotations

import re
import socket
import struct
import subprocess
import sys
from dataclasses import asdict, dataclass


@dataclass
class Link:
    kind: str = "unknown"  # "wifi", "wired" or "unknown"
    rssi: int | None = None  # dBm, e.g. -58
    noise: int | None = None  # dBm
    band: str | None = None  # "2.4 GHz", "5 GHz", "6 GHz"
    channel: str | None = None
    rate: float | None = None  # negotiated PHY rate, Mbps
    standard: str | None = None  # "802.11ax"
    ssid: str | None = None

    def dict(self) -> dict:
        return asdict(self)


def _run(cmd: list[str], timeout: float = 8) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _num(s: str | None) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", s or "")
    return float(m.group()) if m else None


def band_of(freq_mhz: float | None = None, channel: int | None = None) -> str | None:
    if freq_mhz:
        return "2.4 GHz" if freq_mhz < 3000 else "5 GHz" if freq_mhz < 5925 else "6 GHz"
    if channel:
        return "2.4 GHz" if channel <= 14 else "5 GHz"
    return None


# ---------------------------------------------------------------------------
# Default gateway
# ---------------------------------------------------------------------------
def gateway_linux(proc_net_route: str) -> tuple[str, str] | None:
    """(gateway ip, interface) from /proc/net/route."""
    for line in proc_net_route.splitlines()[1:]:
        f = line.split()
        if len(f) >= 3 and f[1] == "00000000" and f[2] != "00000000":
            return socket.inet_ntoa(struct.pack("<I", int(f[2], 16))), f[0]
    return None


def gateway_macos(route_out: str) -> tuple[str, str] | None:
    """From `route -n get default`."""
    gw = re.search(r"gateway:\s*(\d+\.\d+\.\d+\.\d+)", route_out)
    iface = re.search(r"interface:\s*(\S+)", route_out)
    return (gw.group(1), iface.group(1) if iface else "") if gw else None


def gateway_windows(route_out: str) -> tuple[str, str] | None:
    """From `route print -4 0.0.0.0`: the active default route with the lowest metric."""
    best = None
    for m in re.finditer(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+)", route_out, re.M):
        if best is None or int(m.group(3)) < best[2]:
            best = (m.group(1), m.group(2), int(m.group(3)))
    return (best[0], best[1]) if best else None


def default_gateway() -> tuple[str, str] | None:
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/net/route") as fh:
                return gateway_linux(fh.read())
        except OSError:
            return None
    if sys.platform == "darwin":
        return gateway_macos(_run(["route", "-n", "get", "default"]))
    if sys.platform == "win32":
        return gateway_windows(_run(["route", "print", "-4", "0.0.0.0"]))
    return None


# ---------------------------------------------------------------------------
# Wi-Fi link
# ---------------------------------------------------------------------------
def parse_system_profiler(out: str) -> Link | None:
    """macOS `system_profiler SPAirPortDataType` (works on Sonoma+, where `airport` is gone)."""
    m = re.search(r"Current Network Information:\s*\n(.*?)(?:\n\s*Other Local Wi-Fi Networks:|\Z)", out, re.S)
    if not m:
        return None
    block = m.group(1)

    def field(name):
        f = re.search(rf"^\s*{name}:\s*(.+)$", block, re.M)
        return f.group(1).strip() if f else None

    ssid = re.search(r"^\s*(.+?):\s*$", block, re.M)
    link = Link(kind="wifi", ssid=ssid.group(1).strip() if ssid else None)
    sn = field("Signal / Noise")
    if sn:
        vals = re.findall(r"-?\d+", sn)
        if vals:
            link.rssi = int(vals[0])
        if len(vals) > 1:
            link.noise = int(vals[1])
    ch = field("Channel")
    if ch:
        link.channel = ch.split()[0]
        b = re.search(r"(2(?:\.4)?|5|6)\s*GHz", ch)
        link.band = {"2": "2.4 GHz", "2.4": "2.4 GHz", "5": "5 GHz", "6": "6 GHz"}[b.group(1)] if b else \
            band_of(channel=int(_num(ch) or 0))
    link.rate = _num(field("Transmit Rate"))
    phy = field("PHY Mode")
    link.standard = phy
    return link


def parse_iw_link(out: str) -> Link | None:
    """Linux `iw dev <if> link`."""
    if "Not connected" in out or "Connected to" not in out:
        return None
    link = Link(kind="wifi")
    if (m := re.search(r"SSID:\s*(.+)", out)):
        link.ssid = m.group(1).strip()
    if (m := re.search(r"freq:\s*([\d.]+)", out)):
        link.band = band_of(freq_mhz=float(m.group(1)))
    if (m := re.search(r"signal:\s*(-?\d+)", out)):
        link.rssi = int(m.group(1))
    if (m := re.search(r"tx bitrate:\s*([\d.]+)\s*MBit/s(.*)", out)):
        link.rate = float(m.group(1))
        rest = m.group(2)
        link.standard = "802.11be" if "EHT" in rest else "802.11ax" if "HE-" in rest else \
            "802.11ac" if "VHT" in rest else "802.11n" if "MCS" in rest else None
    return link


def parse_proc_wireless(out: str, iface: str | None = None) -> Link | None:
    """Linux /proc/net/wireless: `wlan0: 0000   54.  -56.  -256  ...` (fallback when `iw` is missing)."""
    for line in out.splitlines()[2:]:
        name, _, rest = line.partition(":")
        if iface and name.strip() != iface:
            continue
        f = rest.split()
        if len(f) >= 3:
            level = int(float(f[2].rstrip(".")))
            return Link(kind="wifi", rssi=level if level < 0 else level - 256)
    return None


def parse_netsh(out: str) -> Link | None:
    """Windows `netsh wlan show interfaces` (English labels; others fall back to numbers we can find)."""
    def field(*names):
        for name in names:
            m = re.search(rf"^\s*{name}\s*:\s*(.+)$", out, re.M | re.I)
            if m:
                return m.group(1).strip()
        return None

    state = field("State")
    if not state or "connected" not in state.lower() or "disconnected" in state.lower():
        return None
    link = Link(kind="wifi", ssid=field("SSID"))
    sig = _num(field("Signal"))
    if sig is not None:
        link.rssi = int(round(sig / 2 - 100))  # Windows reports quality 0-100%, which maps linearly to -100..-50 dBm
    link.standard = field("Radio type")
    link.channel = field("Channel")
    b = field("Band")
    link.band = b if b else band_of(channel=int(_num(link.channel) or 0))
    rx, tx = _num(field(r"Receive rate \(Mbps\)")), _num(field(r"Transmit rate \(Mbps\)"))
    link.rate = max(filter(None, (rx, tx)), default=None)
    return link


def link_info(iface: str = "") -> Link:
    """Best effort; never raises. `iface` is the default route's interface, if known."""
    try:
        if sys.platform == "darwin":
            # en0 is Wi-Fi on most Macs; a different default interface usually means Ethernet/Thunderbolt
            l = parse_system_profiler(_run(["system_profiler", "SPAirPortDataType"], timeout=15))
            if l and (not iface or iface == "en0" or l.rssi is not None):
                return l
            return Link(kind="wired" if iface else "unknown")
        if sys.platform == "win32":
            l = parse_netsh(_run(["netsh", "wlan", "show", "interfaces"]))
            return l or Link(kind="wired")
        if sys.platform.startswith("linux"):
            import os
            if iface and not os.path.isdir(f"/sys/class/net/{iface}/wireless"):
                return Link(kind="wired")
            l = parse_iw_link(_run(["iw", "dev", iface, "link"])) if iface else None
            if not l:
                try:
                    with open("/proc/net/wireless") as fh:
                        l = parse_proc_wireless(fh.read(), iface or None)
                except OSError:
                    l = None
            return l or Link(kind="wifi" if iface else "unknown")
    except Exception:
        pass
    return Link()
