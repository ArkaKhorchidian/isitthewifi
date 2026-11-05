"""isitthewifi: a report card for your connection, in about 30 seconds."""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .grade import Report, grade
from .measure import Config, Result, run

GRADE_COLORS = {"A": "bold green", "B": "bold bright_green", "C": "bold yellow", "D": "bold dark_orange", "F": "bold red"}
PHASES = [("idle", "Idle"), ("download", "Download"), ("upload", "Upload"), ("dns", "DNS")]
SPARK = "▁▂▃▄▅▆▇█"


def color(g: str | None) -> str:
    return GRADE_COLORS.get((g or "?")[0], "dim")


def sparkline(values: list[float | None], width: int = 40, ceiling: float | None = None) -> Text:
    vals = values[-width:]
    ok = [v for v in vals if v is not None]
    top = max(ceiling or 0, max(ok, default=0), 1e-9)
    t = Text()
    started = False
    for v in vals:
        if v is None:
            t.append("×" if started else " ", "red")  # × = lost probe; blank = no data yet
            continue
        started = True
        frac = min(1.0, v / top)
        style = "green" if v < 0.05 else "yellow" if v < 0.15 else "red"
        t.append(SPARK[min(len(SPARK) - 1, int(frac * (len(SPARK) - 1) + 0.5))], style)
    return t


class LiveView:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.phase = "setup"
        self.done: set[str] = set()
        self.live: dict = {}
        self.router: list[float | None] = []
        self.internet: list[float | None] = []
        self.mbps: dict[str, float] = {}
        self._last = 0.0

    def update(self, phase: str, live: dict) -> None:
        if phase != self.phase:
            if self.phase not in ("setup",):
                self.done.add(self.phase)
            self.phase = phase
        self.live = live
        now = time.monotonic()
        if live and now - self._last >= 0.25:  # one sparkline column per quarter second
            self._last = now
            self.router.append(live.get("router"))
            self.internet.append(live.get("internet"))
        if live.get("mbps"):
            self.mbps[phase] = live["mbps"]

    def __rich__(self):
        rows = Table.grid(padding=(0, 2))
        rows.add_column(width=2)
        rows.add_column(width=10)
        rows.add_column(width=24)
        rows.add_column()
        for key, label in PHASES:
            if key in ("download", "upload") and getattr(self.cfg, key) <= 0:
                continue
            if key in self.done:
                mark, bar = Text("●", "green"), Text("done", "dim")
            elif key == self.phase:
                mark = Text("◉", "cyan")
                p = self.live.get("progress")
                bar = Text("━" * int((p or 0) * 20), "cyan") + Text("─" * (20 - int((p or 0) * 20)), "dim") \
                    if p is not None else Text("…", "cyan")
            else:
                mark, bar = Text("○", "dim"), Text("")
            extra = Text(f"{self.mbps[key]:.0f} Mbps", "bold") if key in self.mbps else Text("")
            rows.add_row(mark, Text(label, "bold" if key == self.phase else ""), bar, extra)

        legs = Table.grid(padding=(0, 2))
        legs.add_column(width=20)
        legs.add_column(width=8, justify="right")
        legs.add_column()
        ceiling = max([v for v in self.router + self.internet if v] + [0.06])
        for name, hist in (("you → router", self.router), ("you → internet", self.internet)):
            cur = next((v for v in reversed(hist) if v is not None), None)
            legs.add_row(Text(name, "dim"), Text(f"{cur * 1000:.0f} ms" if cur else "—", "bold"),
                         sparkline(hist, 44, ceiling))
        return Panel(Group(rows, Text(""), legs), title="[bold]isitthewifi[/]", subtitle="[dim]testing…[/]",
                     border_style="cyan", padding=(1, 2))


def render_report(r: Result, rep: Report, console: Console) -> None:
    head = Table.grid(expand=True)
    head.add_column(ratio=1)
    head.add_column(justify="right")
    head.add_row(Text(rep.verdict, "bold"), Text(rep.overall, color(rep.overall)) + Text("  overall", "dim"))

    t = Table(box=None, show_header=True, header_style="dim", padding=(0, 2), expand=True)
    t.add_column("Grade", justify="center", width=5)
    t.add_column("Test", no_wrap=True)
    t.add_column("Result", no_wrap=True)
    t.add_column("What it means", ratio=1)
    for row in rep.rows:
        t.add_row(Text(row.grade or "–", color(row.grade)), row.subject, row.value, Text(row.note, "dim"))

    parts: list = [head, Text(""), t]
    def bullets(items: list[str], numbered: bool) -> Table:
        g = Table.grid(padding=(0, 1))
        g.add_column(width=4, justify="right")
        g.add_column(ratio=1)
        for i, x in enumerate(items, 1):
            g.add_row(Text(f"{i}." if numbered else "•", "dim"), x)
        return g

    if rep.findings:
        parts += [Text(""), Text("What we found", "bold"), bullets(rep.findings, False)]
    if rep.advice:
        parts += [Text(""), Text("What to do", "bold"), bullets(rep.advice, True)]
    if r.errors:
        parts += [Text("")] + [Text(f"  ! {e}", "yellow") for e in r.errors]
    console.print(Panel(Group(*parts), title="[bold]Is it the Wi-Fi?[/]", border_style=color(rep.overall).split()[-1],
                        padding=(1, 2)))
    console.print(Text("  Share: ", "dim") + Text(rep.share(), "italic"))
    console.print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="isitthewifi",
                                 description="Grade your connection A-F and find out whether it's the Wi-Fi, your router, "
                                             "your ISP, or DNS.")
    ap.add_argument("--quick", action="store_true", help="shorter test (about 12 s)")
    ap.add_argument("--no-upload", action="store_true", help="skip the upload test")
    ap.add_argument("--no-load", action="store_true", help="latency and DNS only; don't use any bandwidth")
    ap.add_argument("--json", action="store_true", help="print raw measurements and grades as JSON")
    ap.add_argument("--router", metavar="IP", help="router address (default: your default gateway)")
    ap.add_argument("--target", metavar="IP", default="1.1.1.1", help="internet probe target (default 1.1.1.1)")
    ap.add_argument("--server", metavar="URL", default=None, help="speed test server (default speed.cloudflare.com)")
    ap.add_argument("-V", "--version", action="version", version=f"isitthewifi {__version__}")
    a = ap.parse_args(argv)

    cfg = Config(internet=a.target, gateway=a.router)
    if a.server:
        cfg.base = a.server
    if a.quick:
        cfg.idle, cfg.download, cfg.upload, cfg.dns_lookups = 3, 5, 5, 3
    if a.no_upload:
        cfg.upload = 0
    if a.no_load:
        cfg.download = cfg.upload = 0
        cfg.idle = max(cfg.idle, 8)

    console = Console(stderr=a.json)
    try:
        if a.json or not console.is_terminal:
            res = run(cfg)
        else:
            view = LiveView(cfg)
            with Live(view, console=console, refresh_per_second=12, transient=True):
                res = run(cfg, view.update)
    except KeyboardInterrupt:
        return 130
    rep = grade(res)
    if a.json:
        out = {"verdict": rep.verdict, "culprit": rep.culprit, "overall": rep.overall,
               "grades": {x.key: x.grade for x in rep.rows}, "numbers": rep.numbers,
               "findings": rep.findings, "advice": rep.advice, "link": res.link.dict(), "gateway": res.gateway,
               "router": res.router, "internet": res.internet, "errors": res.errors}
        print(json.dumps(out, indent=2, default=str))
    else:
        render_report(res, rep, console)
    return 0 if rep.culprit != "unknown" else 1


if __name__ == "__main__":
    sys.exit(main())
