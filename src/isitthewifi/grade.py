"""Turn measurements into letter grades, a verdict, and plain-English explanations."""
from __future__ import annotations

from dataclasses import dataclass, field

from .measure import Result

POINTS = {"A+": 4.3, "A": 4.0, "B": 3.0, "C": 2.0, "D": 1.0, "F": 0.0}


def letter(value: float | None, cuts: list[float], higher_is_better: bool = False) -> str | None:
    """cuts are the A, B, C, D thresholds (and an optional leading A+ one, marked by a 5-long list)."""
    if value is None:
        return None
    names = ["A+", "A", "B", "C", "D"] if len(cuts) == 5 else ["A", "B", "C", "D"]
    for name, cut in zip(names, cuts):
        if (value >= cut) if higher_is_better else (value < cut):
            return name
    return "F"


def worst(*grades: str | None) -> str | None:
    g = [x for x in grades if x]
    return min(g, key=lambda x: POINTS[x]) if g else None


def from_gpa(gpa: float) -> str:
    for cut, name in [(4.15, "A+"), (3.85, "A"), (3.5, "A-"), (3.15, "B+"), (2.85, "B"), (2.5, "B-"), (2.15, "C+"),
                      (1.85, "C"), (1.5, "C-"), (1.15, "D+"), (0.85, "D"), (0.5, "D-")]:
        if gpa >= cut:
            return name
    return "F"


def ms(seconds: float | None) -> float | None:
    return None if seconds is None else seconds * 1000


@dataclass
class Row:
    key: str
    subject: str
    grade: str | None
    value: str
    note: str
    weight: float = 1.0


@dataclass
class Report:
    rows: list[Row] = field(default_factory=list)
    overall: str = "?"
    verdict: str = ""
    culprit: str = ""  # "wifi", "bufferbloat", "isp", "dns", "none", "unknown"
    findings: list[str] = field(default_factory=list)
    advice: list[str] = field(default_factory=list)
    numbers: dict = field(default_factory=dict)

    def share(self) -> str:
        n = self.numbers
        art = "an" if self.overall[0] in "AF" else "a"
        parts = [f"My internet got {art} {self.overall}"]
        if n.get("down_mbps") and n.get("up_mbps"):
            parts.append(f"{n['down_mbps']:.0f}↓ / {n['up_mbps']:.0f}↑ Mbps")
        bloat = max(n.get("bloat_down_ms") or 0, n.get("bloat_up_ms") or 0)
        if bloat >= 30:
            parts.append(f"+{bloat:.0f} ms of lag when the line is busy")
        return " · ".join(parts) + f". {self.verdict} Test yours: uvx isitthewifi"


def _what_breaks(added_ms: float) -> str:
    if added_ms < 30:
        return "nobody will notice"
    if added_ms < 100:
        return "games feel a bit sluggish"
    if added_ms < 250:
        return "video calls lag and people talk over each other"
    return "video calls freeze and games become unplayable"


def _short_break(added_ms: float) -> str:
    return ("Nobody will notice." if added_ms < 30 else "Games feel sluggish." if added_ms < 100 else
            "Video calls lag." if added_ms < 250 else "Calls freeze, games lag.")


def grade(r: Result) -> Report:
    rep = Report()
    I, R = r.internet, r.router
    idle_i, idle_r = I.get("idle", {}), R.get("idle", {})
    base_i, base_r = ms(idle_i.get("median")), ms(idle_r.get("median"))
    n = rep.numbers
    n.update(idle_ms=base_i, router_ms=base_r, jitter_ms=ms(idle_i.get("jitter")), loss=idle_i.get("loss"),
             router_jitter_ms=ms(idle_r.get("jitter")), router_loss=idle_r.get("loss"),
             dns_ms=ms(r.dns_system), dns_public_ms=ms(r.dns_public),
             down_mbps=r.phases["download"].mbps if "download" in r.phases else None,
             up_mbps=r.phases["upload"].mbps if "upload" in r.phases else None,
             rssi=r.link.rssi, link=r.link.kind)

    def added(series: dict, phase: str, base: float | None) -> float | None:
        m = ms(series.get(phase, {}).get("median"))
        return None if m is None or base is None else max(0.0, m - base)

    for d in ("down", "up"):
        phase = "download" if d == "down" else "upload"
        n[f"bloat_{d}_ms"] = added(I, phase, base_i)
        n[f"router_bloat_{d}_ms"] = added(R, phase, base_r)
        n[f"loss_{d}"] = I.get(phase, {}).get("loss")

    on_wifi = r.link.kind == "wifi"

    # --- the Wi-Fi itself -------------------------------------------------------------------------
    if r.link.rssi is not None:
        g = letter(r.link.rssi, [-55, -65, -72, -80], higher_is_better=True)
        extra = " · ".join(x for x in (r.link.band, f"{r.link.rate:.0f} Mbps" if r.link.rate else None) if x)
        quality = {"A": "excellent", "B": "good", "C": "weak", "D": "poor", "F": "barely there"}[g]
        rep.rows.append(Row("signal", "Wi-Fi signal", g, f"{r.link.rssi} dBm" + (f" · {extra}" if extra else ""),
                            f"{quality.capitalize()}."))
    if base_r is not None:
        jit_r = n["router_jitter_ms"] or 0
        loss_r = n["router_loss"] or 0
        g = worst(letter(base_r, [3, 6, 15, 40]), letter(jit_r, [2, 5, 15, 40]), letter(loss_r, [0.005, 0.02, 0.05, 0.1]))
        hop = "Wi-Fi hop" if on_wifi else "You → router"
        note = "Healthy." if g in ("A", "B") else ""
        if loss_r >= 0.005:
            note = f"Drops {loss_r:.0%} of packets."
        elif jit_r >= 5:
            note = f"Wobbles by ±{jit_r:.0f} ms."
        elif base_r >= 6:
            note = "Slow for one hop (should be 1–5 ms)."
        rep.rows.append(Row("hop", hop, g, f"{base_r:.1f} ms ±{jit_r:.1f} · {loss_r:.0%} loss", note))

    # --- the internet path -------------------------------------------------------------------------
    if base_i is not None:
        g = letter(base_i, [20, 40, 70, 120])
        rep.rows.append(Row("latency", "Latency", g, f"{base_i:.0f} ms to {'Cloudflare'}",
                            "Good." if g in ("A", "B") else "Slow, even when idle."))
        jit, loss = n["jitter_ms"] or 0, n["loss"] or 0
        g = worst(letter(jit, [3, 8, 20, 50]), letter(loss, [0.005, 0.02, 0.05, 0.1]))
        rep.rows.append(Row("stability", "Stability", g, f"±{jit:.1f} ms · {loss:.1%} loss",
                            "Steady." if g in ("A", "B") else "Choppy: robotic call audio."))
    for d, label in (("down", "downloading"), ("up", "uploading")):
        b = n[f"bloat_{d}_ms"]
        if b is None:
            continue
        g = letter(b, [5, 30, 60, 200, 400])
        loss = n[f"loss_{d}"] or 0
        if loss > 0.05:
            g = worst(g, "D")
        rep.rows.append(Row(f"bloat_{d}", f"Lag while {label}", g, f"+{b:.0f} ms",
                            _short_break(b), weight=1.5))

    # --- DNS and speed -------------------------------------------------------------------------------
    if r.dns_system is not None:
        dms = n["dns_ms"]
        g = letter(dms, [25, 60, 120, 300])
        note = "Fast." if g in ("A", "B") else "Slows down every new site."
        rep.rows.append(Row("dns", "DNS", g, f"{dms:.0f} ms per new lookup", note))
    if n["down_mbps"]:
        rep.rows.append(Row("down", "Download", letter(n["down_mbps"], [200, 100, 25, 10], True),
                            f"{n['down_mbps']:.0f} Mbps", "", weight=0.5))
    if n["up_mbps"]:
        rep.rows.append(Row("up", "Upload", letter(n["up_mbps"], [50, 20, 5, 2], True),
                            f"{n['up_mbps']:.0f} Mbps", "", weight=0.5))

    graded = [x for x in rep.rows if x.grade]
    if graded:
        gpa = sum(POINTS[x.grade] * x.weight for x in graded) / sum(x.weight for x in graded)
        rep.overall = from_gpa(gpa)
        # one F means something is genuinely broken; don't let good speeds average it away
        if any(x.grade == "F" for x in graded) and POINTS.get(rep.overall[0], 0) > 2:
            rep.overall = "C"
    _diagnose(r, rep)
    return rep


def _diagnose(r: Result, rep: Report) -> None:
    n = rep.numbers
    on_wifi = r.link.kind == "wifi"
    f, a = rep.findings, rep.advice
    culprits: list[str] = []

    # 1. is the hop to the router itself bad?
    br, jr, lr = n["router_ms"], n["router_jitter_ms"] or 0, n["router_loss"] or 0
    if br is not None and (br > 10 or jr > 10 or lr > 0.02):
        culprits.append("wifi")
        bits = []
        if br > 10:
            bits.append(f"{br:.0f} ms just to reach your router (a healthy Wi-Fi hop is 1–5 ms)")
        if lr > 0.02:
            bits.append(f"{lr:.0%} of packets lost before they even leave the house")
        elif jr > 10:
            bits.append(f"±{jr:.0f} ms of jitter on the hop to the router")
        f.append("Your Wi-Fi is the weak link: " + " and ".join(bits) + ".")
    if r.link.rssi is not None and r.link.rssi < -70:
        culprits.append("wifi")
        f.append(f"The signal is weak ({r.link.rssi} dBm). Below about -70 dBm, Wi-Fi slows down and retries a lot.")
        a.append("Move closer to the router, or put the router somewhere central and high, not in a cabinet.")
    if on_wifi and r.link.band == "2.4 GHz":
        a.append("You're on 2.4 GHz, which is crowded and slow. Switch to the router's 5 GHz network if it has one.")

    # 2. bufferbloat: where does the queue build up?
    for d, verb, who in (("up", "uploads", "uploading"), ("down", "downloads", "downloading")):
        b, rb = n.get(f"bloat_{d}_ms"), n.get(f"router_bloat_{d}_ms")
        if b is None or b < 60:
            continue
        if rb is not None and rb >= 0.5 * b:
            culprits.append("wifi")
            f.append(f"Your Wi-Fi link adds {b:.0f} ms of delay whenever something {verb}: the backlog builds up on "
                     f"the Wi-Fi hop itself, so {_what_breaks(b)}.")
        else:
            culprits.append("bufferbloat")
            where = "your router" if rb is not None else "your router or modem"
            f.append(f"{where.capitalize()} adds {b:.0f} ms whenever someone {verb}. Your Wi-Fi is fine; "
                     f"the line to your ISP gets a traffic jam, and every packet waits in it, so {_what_breaks(b)}.")
    if "bufferbloat" in culprits:
        a.append("Turn on SQM / Smart Queue Management (sometimes called \"QoS\" or \"Smart Queue\") in your router and set "
                 "it to about 90% of your measured speeds. eero, Ubiquiti, OpenWrt (cake), and most gaming routers have it.")
        a.append("If your router doesn't support it, a cheap OpenWrt or Ubiquiti router in front of the ISP box usually fixes this.")

    # 3. the ISP path
    bi, li = n["idle_ms"], n["loss"] or 0
    hop_ok = "wifi" not in culprits
    if bi is not None and bi > 60 and hop_ok:
        culprits.append("isp")
        f.append(f"It takes {bi:.0f} ms to reach the internet even when idle, while your router answers in "
                 f"{br:.0f} ms. The slowness is past your house." if br is not None else
                 f"It takes {bi:.0f} ms to reach the internet even when idle.")
        a.append("Run this again at a different time of day; if it's always slow, it's worth a call to your ISP.")
    if li > 0.02 and hop_ok:
        culprits.append("isp")
        f.append(f"{li:.0%} of packets to the internet get lost while the hop to your router is clean: "
                 "that points at the modem, the cable, or your ISP.")
        a.append("Restart the modem, check the cable into it, and if loss persists, call your ISP with these numbers.")

    # 4. DNS
    ds, dp = n["dns_ms"], n["dns_public_ms"]
    if ds is not None and ds > 120:
        culprits.append("dns")
        faster = f" Cloudflare's 1.1.1.1 answered the same kind of lookup in {dp:.0f} ms." if dp and dp < ds / 2 else ""
        f.append(f"Your DNS server takes {ds:.0f} ms for each new website name.{faster}")
        a.append("Switch your DNS to 1.1.1.1 or 8.8.8.8 (in your router, or in your computer's network settings).")

    if not culprits:
        if on_wifi and n.get("rssi") is not None and n["rssi"] < -65:
            a.append("Your signal is only OK. Moving closer or onto 5 GHz would give you more headroom.")

    order = ["wifi", "bufferbloat", "isp", "dns"]
    culprits = sorted(set(culprits), key=order.index)
    rep.culprit = culprits[0] if culprits else ("unknown" if not r.internet else "none")
    rep.verdict = {
        "wifi": "Yes, it's the Wi-Fi.",
        "bufferbloat": "No, it's not the Wi-Fi. It's your router's queue (bufferbloat).",
        "isp": "No, it's not the Wi-Fi. It's your ISP.",
        "dns": "No, it's not the Wi-Fi. It's your DNS.",
        "none": "No, it's not the Wi-Fi. Your connection is healthy.",
        "unknown": "Couldn't tell: the internet didn't answer.",
    }[rep.culprit]
    if r.link.kind == "wired" and rep.culprit != "unknown":
        rep.verdict = rep.verdict.replace("Yes, it's the Wi-Fi.", "It's the link to your router.") \
            .replace("No, it's not the Wi-Fi.", "It's not the Wi-Fi (you're wired).")
    if not f and rep.culprit == "none":
        f.append("Nothing's wrong with your connection right now. If something feels slow, it's probably the website.")
