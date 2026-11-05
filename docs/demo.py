"""Record the README demo on a simulated home network: healthy 5 GHz Wi-Fi, a router with 300 ms of
upload bufferbloat. (Real runs hit your router and Cloudflare; this just makes the problem reproducible.)"""
import random
import sys
import threading
import time

sys.path.insert(0, "tests")
from fakenet import Net  # noqa: E402

from isitthewifi import cli, measure, wifi  # noqa: E402


class Home(Net):
    def __init__(self):
        super().__init__(down_pause=0.005, up_pause=0.075)
        self.up = 0

    def delay(self, leg):
        base = 0.0025 if leg == "router" else 0.014
        lag = 0.0
        if leg == "internet" and self.active:
            lag = 0.31 if self.up else 0.022
        return base + lag + random.uniform(0, 0.0015) + (random.random() < 0.05) * 0.004


net = Home()
inet, router = net.start_dns("internet"), net.start_dns("router")
base = net.start_http()

# mark uploads so the fake router can queue them
def patch_upload():
    from isitthewifi import load
    orig = load.Load.start

    def start(self):
        if self.direction == "up":
            net.up += 1
            orig_stop = self.stop

            def stop():
                net.up -= 1
                orig_stop()
            self.stop = stop
        return orig(self)
    load.Load.start = start


patch_upload()
real_run = measure.run


def run(cfg, status=None):
    cfg.internet, cfg.internet_port = "127.0.0.1", inet
    cfg.gateway, cfg.gateway_port, cfg.base, cfg.streams = "127.0.0.1", router, base, 3
    cfg.idle, cfg.download, cfg.upload, cfg.dns_lookups = 3, 6, 6, 3
    return real_run(cfg, status)


wifi.link_info = lambda iface="": wifi.Link(kind="wifi", rssi=-54, band="5 GHz", rate=866, channel="149")
wifi.default_gateway = lambda: ("192.168.1.1", "en0")
cli.run = run
sys.exit(cli.main([]))
