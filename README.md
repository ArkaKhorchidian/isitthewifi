# isitthewifi

**Is it the Wi-Fi?** One command grades your connection A–F and tells you, in plain English, whether the problem is your Wi-Fi, your router, your ISP, or DNS.

```bash
uvx isitthewifi
```

<p align="center"><img src="https://raw.githubusercontent.com/ArkaKhorchidian/isitthewifi/main/docs/demo.gif" alt="isitthewifi demo" width="760"></p>

<sub>Demo recorded on a simulated home network with 300 ms of upload bufferbloat, so the problem is reproducible. A real run tests your actual router and Cloudflare.</sub>

Speed tests answer "how fast?", but that's rarely why a video call freezes. What usually breaks is **latency under load**: your connection is fine until someone uploads a photo library, and then every packet waits in a queue for 300 ms. isitthewifi measures that, and then works out *where* the queue is.

## How it figures out whose fault it is

It runs a ~30 second test while probing **two legs at the same time**, ten times a second:

| Leg | What it tells us |
|---|---|
| **You → router** | The Wi-Fi hop. Healthy Wi-Fi answers in 1–5 ms with no loss. |
| **You → internet** (1.1.1.1) | Everything past your house: modem, ISP, the route. |

Then it saturates your download, then your upload (using Cloudflare's speed test servers), and watches what happens to each leg:

- **The router leg is slow, jittery, or dropping packets** → it's the Wi-Fi. Weak signal, interference, 2.4 GHz congestion.
- **The internet leg balloons under load but the router leg stays flat** → it's not the Wi-Fi. The traffic jam is in your router's connection to the ISP (bufferbloat). Fix: turn on SQM / Smart Queue.
- **Both legs balloon together** → the backlog is building up on the Wi-Fi hop itself.
- **The internet leg is slow or lossy even when idle, router leg clean** → it's your ISP.
- **New lookups take 150 ms+ through your resolver but 15 ms through 1.1.1.1** → it's your DNS.

It also reads your Wi-Fi signal strength, band and link rate from the OS (`system_profiler` on macOS, `iw` on Linux, `netsh` on Windows).

## The grades

| Test | A | B | C | D |
|---|---|---|---|---|
| Wi-Fi signal | ≥ -55 dBm | ≥ -65 | ≥ -72 | ≥ -80 |
| Wi-Fi hop | < 3 ms | < 6 ms | < 15 ms | < 40 ms |
| Latency (idle) | < 20 ms | < 40 ms | < 70 ms | < 120 ms |
| Stability (jitter / loss) | < 3 ms / 0.5% | < 8 ms / 2% | < 20 ms / 5% | < 50 ms / 10% |
| Lag while downloading / uploading | < 30 ms (A+ < 5) | < 60 ms | < 200 ms | < 400 ms |
| DNS (uncached lookup) | < 25 ms | < 60 ms | < 120 ms | < 300 ms |
| Download / Upload | 200 / 50 Mbps | 100 / 20 | 25 / 5 | 10 / 2 |

The bufferbloat thresholds match the ones used by the Waveform bufferbloat test. Lag under load counts 1.5× toward the overall grade; raw speed counts 0.5×, because past a point it isn't what you feel.

## Options

```
isitthewifi              # full test, ~30 s
isitthewifi --quick      # ~15 s
isitthewifi --no-upload  # skip the upload test
isitthewifi --no-load    # latency + DNS only, uses almost no data
isitthewifi --json       # machine-readable results
isitthewifi --router 192.168.1.1 --target 8.8.8.8
```

A full run transfers a few hundred MB on a fast connection. Use `--no-load` on metered connections.

## No admin rights needed

`ping` needs raw sockets, which means root on Linux. isitthewifi doesn't use ICMP at all: it times tiny DNS queries over UDP (to 1.1.1.1) and TCP handshakes (to your router, whose kernel answers them instantly, or refuses them, which takes exactly one round trip too). Everything runs as a normal user on macOS, Linux and Windows.

## Install

```bash
uvx isitthewifi          # run without installing
pipx install isitthewifi # or install it
```

Requires Python 3.9+. The only dependency is `rich`.

## License

MIT
