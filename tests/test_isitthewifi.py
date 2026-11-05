import json
import time

import pytest

from fakenet import Net
from isitthewifi import probes, wifi
from isitthewifi.grade import grade, letter
from isitthewifi.measure import Config, Result, run


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------
def test_dns_query_is_well_formed():
    q = probes.dns_query("abc.example.com", 0x1234)
    assert q[:2] == b"\x12\x34" and q[12:] == b"\x03abc\x07example\x03com\x00\x00\x01\x00\x01"


def test_udp_probe_measures_round_trip():
    net = Net(base_delay=0.03)
    port = net.start_dns()
    rtt = probes.udp_dns_rtt("127.0.0.1", port)
    assert rtt is not None and 0.025 < rtt < 0.5


def test_udp_probe_times_out_and_ignores_stale_answers():
    net = Net(base_delay=0.3)
    port = net.start_dns()
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    assert probes.udp_dns_rtt("127.0.0.1", port, sock=s, timeout=0.1) is None
    time.sleep(0.35)  # the late answer to the first query is now sitting in the socket
    net.base_delay = 0.01
    rtt = probes.udp_dns_rtt("127.0.0.1", port, sock=s, timeout=1.0)
    assert rtt is not None and rtt >= 0.009  # it skipped the stale reply instead of reporting ~0 ms


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="Windows retries SYNs after a RST for ~2 s")
def test_tcp_probe_counts_refused_as_a_round_trip():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listening: the RST still comes back
    assert probes.tcp_rtt("127.0.0.1", port) is not None


def test_summarize():
    S = probes.Sample
    out = probes.summarize([S(0, 0.010), S(1, 0.020), S(2, None), S(3, 0.010)])
    assert out["n"] == 4 and out["loss"] == 0.25 and out["median"] == 0.010
    assert out["jitter"] == pytest.approx(0.010)
    assert probes.summarize([S(0, None)])["loss"] == 1.0


# ---------------------------------------------------------------------------
# Grades and diagnosis
# ---------------------------------------------------------------------------
def test_letter_thresholds():
    assert [letter(x, [5, 30, 60, 200, 400]) for x in (1, 20, 50, 100, 300, 500)] == ["A+", "A", "B", "C", "D", "F"]
    assert letter(-50, [-55, -65, -72, -80], True) == "A" and letter(-90, [-55, -65, -72, -80], True) == "F"


def fake_result(idle=0.015, router=0.003, bloat_up=0.0, router_bloat_up=0.0, rssi=-55, kind="wifi", loss=0.0,
                dns=0.02, router_jitter=0.0005):
    r = Result()
    r.link = wifi.Link(kind=kind, rssi=rssi, band="5 GHz")
    s = lambda m, j=0.001, l=0.0: {"n": 50, "loss": l, "median": m, "p90": m, "jitter": j}
    r.internet = {"idle": s(idle, l=loss), "download": s(idle + 0.01), "upload": s(idle + bloat_up)}
    r.router = {"idle": s(router, router_jitter), "download": s(router), "upload": s(router + router_bloat_up)}
    from isitthewifi.measure import Phase
    r.phases = {"idle": Phase("idle"), "download": Phase("download", mbps=300), "upload": Phase("upload", mbps=20)}
    r.dns_system, r.dns_public = dns, 0.015
    return r


def test_healthy_connection():
    rep = grade(fake_result())
    assert rep.culprit == "none" and rep.overall.startswith("A")
    assert "not the Wi-Fi" in rep.verdict


def test_bufferbloat_in_the_router_is_not_the_wifi():
    rep = grade(fake_result(bloat_up=0.300))
    assert rep.culprit == "bufferbloat"
    assert any("adds 300 ms whenever someone uploads" in f for f in rep.findings)
    assert any("SQM" in a for a in rep.advice)
    assert {r.key: r.grade for r in rep.rows}["bloat_up"] == "D"
    assert "+300 ms" in rep.share()


def test_queue_on_the_wifi_hop_is_the_wifi():
    rep = grade(fake_result(bloat_up=0.300, router_bloat_up=0.290))
    assert rep.culprit == "wifi" and rep.verdict.startswith("Yes")
    assert any("Wi-Fi link adds 300 ms" in f for f in rep.findings)


def test_weak_signal_and_slow_hop():
    rep = grade(fake_result(rssi=-78, router=0.025, router_jitter=0.02))
    assert rep.culprit == "wifi"
    assert any("25 ms just to reach your router" in f for f in rep.findings)
    assert any("Move closer" in a for a in rep.advice)


def test_isp_problems():
    rep = grade(fake_result(idle=0.090, loss=0.05))
    assert rep.culprit == "isp" and "ISP" in rep.verdict
    assert any("past your house" in f for f in rep.findings)


def test_slow_dns():
    rep = grade(fake_result(dns=0.250))
    assert rep.culprit == "dns"
    assert any("1.1.1.1" in a for a in rep.advice)


def test_wired_wording():
    rep = grade(fake_result(kind="wired", rssi=None, bloat_up=0.2))
    assert rep.verdict.startswith("It's not the Wi-Fi (you're wired)")


def test_one_f_caps_the_overall_grade():
    rep = grade(fake_result(bloat_up=0.600))
    assert rep.overall[0] in "CDF"


# ---------------------------------------------------------------------------
# End to end against a simulated network
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lag_on,culprit", [(("internet",), "bufferbloat"), (("internet", "router"), "wifi")])
def test_end_to_end_finds_the_queue(lag_on, culprit):
    net = Net(base_delay=0.002, lag=0.25, lag_on=lag_on)
    inet, router = Net.start_dns(net, "internet"), Net.start_dns(net, "router")
    cfg = Config(idle=1.5, download=4, upload=4, internet="127.0.0.1", internet_port=inet, gateway="127.0.0.1",
                 gateway_port=router, base=net.start_http(), streams=2, interval=0.05, dns_lookups=0, read_link=False)
    seen = []
    res = run(cfg, lambda phase, live: seen.append(phase))
    assert {"idle", "download", "upload", "done"} <= set(seen)
    assert res.phases["download"].mbps and res.phases["upload"].mbps
    rep = grade(res)
    assert rep.numbers["bloat_down_ms"] > 150 and rep.numbers["bloat_up_ms"] > 150
    if culprit == "bufferbloat":
        assert rep.numbers["router_bloat_up_ms"] < 50
    assert rep.culprit == culprit


def test_cli_json(monkeypatch, capsys):
    from isitthewifi import cli, measure
    net = Net(base_delay=0.002)
    inet = net.start_dns()
    base = net.start_http()
    orig = measure.run

    def patched(cfg, status=None):
        cfg.internet_port, cfg.gateway, cfg.base, cfg.read_link, cfg.dns_lookups = inet, None, base, False, 0
        cfg.idle, cfg.download, cfg.upload = 1, 0, 0
        return orig(cfg, status)

    monkeypatch.setattr(cli, "run", patched)
    assert cli.main(["--json", "--target", "127.0.0.1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["culprit"] == "none" and out["grades"]["latency"] == "A"


def test_cli_renders_report():
    from rich.console import Console
    from isitthewifi.cli import render_report
    c = Console(width=110, record=True, file=open(__import__("os").devnull, "w"))
    r = fake_result(bloat_up=0.312)
    render_report(r, grade(r), c)
    text = c.export_text()
    assert "Is it the Wi-Fi?" in text and "Lag while uploading" in text and "uvx isitthewifi" in text


# ---------------------------------------------------------------------------
# OS parsers
# ---------------------------------------------------------------------------
def test_gateway_linux():
    route = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
             "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\n"
             "wlan0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\n")
    assert wifi.gateway_linux(route) == ("192.168.1.1", "wlan0")


def test_gateway_macos():
    out = """   route to: default
destination: default
       mask: default
    gateway: 192.168.86.1
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
"""
    assert wifi.gateway_macos(out) == ("192.168.86.1", "en0")


def test_gateway_windows():
    out = """===========================================================================
IPv4 Route Table
===========================================================================
Active Routes:
Network Destination        Netmask          Gateway       Interface  Metric
          0.0.0.0          0.0.0.0      192.168.0.1    192.168.0.23     35
          0.0.0.0          0.0.0.0         10.0.0.1       10.0.0.50     25
===========================================================================
"""
    assert wifi.gateway_windows(out) == ("10.0.0.1", "10.0.0.50")


SYSTEM_PROFILER = """Wi-Fi:

      Software Versions:
          CoreWLAN: 16.0 (1657)
      Interfaces:
        en0:
          Card Type: Wi-Fi  (0x14E4, 0x4387)
          Firmware Version: wl0: Mar 19 2024 15:27:55 version 20.10.1135.4.8.7.191 FWID 01-ff15a2cc
          MAC Address: aa:bb:cc:dd:ee:ff
          Locale: FCC
          Country Code: US
          Supported PHY Modes: 802.11 a/b/g/n/ac/ax
          Status: Connected
          Current Network Information:
            HomeNet 5G:
              PHY Mode: 802.11ax
              Channel: 149 (5GHz, 80MHz)
              Country Code: US
              Network Type: Infrastructure
              Security: WPA2 Personal
              Signal / Noise: -58 dBm / -92 dBm
              Transmit Rate: 864
              MCS Index: 9
          Other Local Wi-Fi Networks:
            Neighbor:
              PHY Mode: 802.11ac
              Channel: 6 (2GHz, 20MHz)
              Signal / Noise: -80 dBm / -92 dBm
"""


def test_parse_system_profiler():
    l = wifi.parse_system_profiler(SYSTEM_PROFILER)
    assert (l.ssid, l.rssi, l.noise, l.band, l.channel, l.rate, l.standard) == \
        ("HomeNet 5G", -58, -92, "5 GHz", "149", 864, "802.11ax")
    assert wifi.parse_system_profiler("Wi-Fi:\n  Status: Off\n") is None


def test_parse_iw_link():
    out = """Connected to 11:22:33:44:55:66 (on wlp2s0)
	SSID: HomeNet
	freq: 5180
	RX: 1234567 bytes (8901 packets)
	TX: 765432 bytes (4321 packets)
	signal: -63 dBm
	rx bitrate: 866.7 MBit/s VHT-MCS 9 80MHz short GI VHT-NSS 2
	tx bitrate: 780.0 MBit/s VHT-MCS 8 80MHz short GI VHT-NSS 2
"""
    l = wifi.parse_iw_link(out)
    assert (l.ssid, l.band, l.rssi, l.rate, l.standard) == ("HomeNet", "5 GHz", -63, 780.0, "802.11ac")
    assert wifi.parse_iw_link("Not connected.") is None


def test_parse_proc_wireless():
    out = """Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE
 face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22
wlan0: 0000   54.  -56.  -256        0      0      0      0      0        0
"""
    assert wifi.parse_proc_wireless(out, "wlan0").rssi == -56


def test_parse_netsh():
    out = """
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    GUID                   : 01234567-89ab-cdef-0123-456789abcdef
    Physical address       : aa:bb:cc:dd:ee:ff
    State                  : connected
    SSID                   : HomeNet
    BSSID                  : 11:22:33:44:55:66
    Network type           : Infrastructure
    Radio type             : 802.11ax
    Authentication         : WPA2-Personal
    Cipher                 : CCMP
    Connection mode        : Auto Connect
    Band                   : 5 GHz
    Channel                : 36
    Receive rate (Mbps)    : 1201
    Transmit rate (Mbps)   : 960
    Signal                 : 84%
    Profile                : HomeNet
"""
    l = wifi.parse_netsh(out)
    assert (l.ssid, l.rssi, l.band, l.channel, l.rate, l.standard) == ("HomeNet", -58, "5 GHz", "36", 1201, "802.11ax")
    assert wifi.parse_netsh(out.replace(": connected", ": disconnected")) is None


def test_link_info_never_raises():
    assert isinstance(wifi.link_info(), wifi.Link)
