"""A tiny fake network on localhost: a DNS responder, a "router", and a speed test server.

Whenever a transfer is running, `lag` seconds of queueing delay get added to whichever legs are
configured to suffer from it, which is exactly what bufferbloat looks like from the outside.
"""
from __future__ import annotations

import http.server
import socket
import socketserver
import threading
import time


class Net:
    def __init__(self, base_delay: float = 0.002, lag: float = 0.0, lag_on: tuple[str, ...] = ("internet",),
                 loss_every: int = 0, down_pause: float = 0.002, up_pause: float = 0.002):
        self.down_pause, self.up_pause = down_pause, up_pause
        self.base_delay, self.lag, self.lag_on, self.loss_every = base_delay, lag, lag_on, loss_every
        self.active = 0
        self.lock = threading.Lock()
        self.count = 0

    def delay(self, leg: str) -> float:
        return self.base_delay + (self.lag if self.active and leg in self.lag_on else 0.0)

    # --- UDP DNS responder: echoes the query id back -----------------------------------------------
    def start_dns(self, leg: str = "internet") -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))

        def loop():
            while True:
                try:
                    data, addr = sock.recvfrom(2048)
                except OSError:
                    return
                with self.lock:
                    self.count += 1
                    drop = self.loss_every and self.count % self.loss_every == 0
                if drop:
                    continue
                reply = data[:2] + b"\x81\x83" + data[4:]  # NXDOMAIN, same question
                threading.Timer(self.delay(leg), lambda r=reply, a=addr: sock.sendto(r, a)).start()

        threading.Thread(target=loop, daemon=True).start()
        return sock.getsockname()[1]

    # --- HTTP speed test server ----------------------------------------------------------------------
    def start_http(self) -> str:
        net = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                n = int(self.path.split("bytes=")[1])
                self.send_response(200)
                self.send_header("Content-Length", str(n))
                self.end_headers()
                net.active += 1
                try:
                    block = b"\0" * 65536
                    left = n
                    while left > 0:
                        k = min(left, len(block))
                        self.wfile.write(block[:k])
                        left -= k
                        time.sleep(net.down_pause)  # ~250 Mbps per stream by default, so the test isn't CPU-bound
                except OSError:
                    pass
                finally:
                    net.active -= 1

            def do_POST(self):
                n = int(self.headers["Content-Length"])
                net.active += 1
                try:
                    while n > 0:
                        chunk = self.rfile.read(min(n, 65536))
                        if not chunk:
                            break
                        n -= len(chunk)
                        time.sleep(net.up_pause)
                finally:
                    net.active -= 1
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        srv = S(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{srv.server_address[1]}"
