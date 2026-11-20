# halow-alert

[![checks](https://github.com/clearobject/halow-alert-releases/actions/workflows/checks.yml/badge.svg)](https://github.com/clearobject/halow-alert-releases/actions/workflows/checks.yml)

**Make ClearObject ClearZone HaLow wearables alert from your own software.**

ClearZone wearables (vests and handhelds) vibrate and sound when they receive an alert over a Wi-Fi HaLow (IEEE 802.11ah) network served by a Morse Micro HaLowLink 2. **halow-alert** is a small gateway service that lets any program, such as a simulator, a control application or a test script, trigger them with a single HTTP request, and that reports which wearables acknowledged. Everything runs on your local network: no cloud, no internet connection and no ClearObject Workzone camera system are needed. This repository holds the user documentation, ready-made integrations, the Docker Compose kit and the releases. The service itself ships as the container image `ghcr.io/clearobject/halow-alert`.

> [!NOTE]
> **Version 0.1.0 is a pre-release.** It has been tested in software against a stand-in wearable, but it has not yet been verified with HaLow hardware. The [changelog](CHANGELOG.md) lists what is still open. Version 1.0.0 will follow hardware verification.

## How it works

```mermaid
flowchart LR
    client["Your application<br/>or any HTTP client<br/>(any OS, any machine)"]
    subgraph GW["Alert gateway (Linux host)"]
        svc["halow-alert container<br/>host network<br/>192.168.12.50"]
    end
    direct["Direct mode<br/>your program on macOS,<br/>Windows or Linux<br/>at 192.168.12.50"]
    hl["HaLowLink 2<br/>192.168.12.1<br/>Router mode"]
    w["ClearZone wearables<br/>192.168.12.x"]

    client -- "HTTP POST /alert<br/>TCP 8080 + token" --> svc
    svc -- "UDP multicast<br/>239.255.0.10:5000" --> hl
    direct -. "UDP multicast<br/>239.255.0.10:5000" .-> hl
    hl -- "HaLow radio<br/>WPA3-SAE" --> w
    w -- "TCP 5001 via the HaLowLink<br/>ACKs and heartbeats" --> svc
    w -. "TCP 5001 (optional)" .-> direct
```

1. Your software calls `POST /alert` on the **alert gateway**, a Linux machine that is cabled to the HaLowLink.
2. The gateway sends one UDP multicast datagram to `239.255.0.10:5000`. The HaLowLink carries it over the HaLow radio, and **every wearable on that network alerts**.
3. Each wearable reports back over TCP port `5001` with an acknowledgement (ACK). Every 15 s the gateway also asks the wearables for a heartbeat (battery, signal strength), so `GET /wearables` always shows who is reachable.

In **direct mode** there is no gateway: the program that runs on the machine plugged into the HaLowLink sends the multicast datagram itself. It takes a few lines of code in any language.

The [architecture guide](docs/ARCHITECTURE.md) covers the components, message flows, delivery guarantees, failure handling and security model in detail.

## Choose your setup

| | **A. Alert gateway + HTTP API** (recommended) | **B. Direct mode** |
|---|---|---|
| In the integrations | Gateway mode | Direct mode |
| Your software runs on | Any machine and OS that can reach the gateway over your network | The machine plugged into the HaLowLink (macOS, Windows or Linux) |
| Extra hardware | A Linux host next to the HaLowLink: Raspberry Pi 4 or 5 (64-bit OS), NVIDIA Jetson, a mini PC, any amd64 or arm64 Linux box | None |
| Your code does | One HTTP `POST` with a token | Sends one UDP multicast datagram; optionally listens on TCP 5001 for ACKs |
| You get for free | Token check, rate limit, heartbeats, wearable roster, health check, JSONL log, auto-recovery when the HaLowLink is re-plugged | Full control, nothing to install |
| Start with | [Quick start A](#quick-start-a-alert-gateway) | [Quick start B](#quick-start-b-direct-mode) |

Docker Desktop (on macOS, Windows or Linux) and rootless Docker run containers inside their own virtual network and **cannot reach the HaLowLink**. Setup A needs Docker Engine running as root on a Linux host. On a Mac or Windows PC without a Linux host, use setup B.

> [!WARNING]
> The wearables vibrate and sound at full volume. Tell the people nearby before you send an alert.

## Quick start A: alert gateway

On the Linux gateway, in the unpacked kit from the [latest release](https://github.com/clearobject/halow-alert-releases/releases) (or a clone of this repository). You need Docker Engine with the Compose plugin, and internet access once to download the image. If `docker` says `permission denied` on `/var/run/docker.sock`, run the commands with `sudo`, or add your user to the `docker` group ([Docker's post-install steps](https://docs.docker.com/engine/install/linux-postinstall/)).

```bash
# 1. Plug the HaLowLink's USB-C into the gateway, wait about 60 s, then give the
#    gateway its static address 192.168.12.50/24 (NetworkManager; see docs/NETWORK.md):
sudo ./setup-network-linux.sh usb

# 2. Check the path (this sends nothing to the wearables). Every row should say PASS,
#    with egress address 192.168.12.50 and no [NOTE] line:
docker run --rm --network host ghcr.io/clearobject/halow-alert:0.1.0 doctor

# 3. Create a long random token (anyone who has it can set off the wearables) and start:
echo "HALOW_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" > .env
docker compose up -d
docker compose ps        # "healthy" after about 30 s
grep HALOW_TOKEN .env    # the token: give it to the machines that call the API
```

The container runs as uid and gid 1000 (the first user on Ubuntu, Jetson and Raspberry Pi OS) so that it can write its log to `./data/halow-alert.jsonl`. If `id -u` or `id -g` print something else, add `HALOW_UID=<uid>` and `HALOW_GID=<gid>` to `.env`, as [`.env.example`](.env.example) shows. Keep `.env` out of version control and shared documents.

**Without Compose**, the same service as a single `docker run`:

```bash
export HALOW_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')   # keep it
docker run -d --name halow-alert --network host --restart unless-stopped \
  -e HALOW_IFACE=192.168.12.50 -e HALOW_TOKEN \
  -v halow-alert-data:/data \
  ghcr.io/clearobject/halow-alert:0.1.0
```

`--network host` is required: the multicast has to leave through the host's HaLowLink interface.

**Trigger an alert** from any machine that can reach the gateway. First set the token from the gateway's `.env` there (`export HALOW_TOKEN=<token>`; in PowerShell `$env:HALOW_TOKEN='<token>'`):

```bash
curl -X POST http://<gateway>:8080/alert \
     -H "Authorization: Bearer $HALOW_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"label": "zone-3 intrusion"}'

curl http://<gateway>:8080/wearables   # who answered: ACK counts, battery, signal strength
curl http://<gateway>:8080/health      # 200 when the gateway can send, 503 when it cannot
```

On Windows PowerShell, call `curl.exe` (plain `curl` is an alias for a different command), or use `Invoke-RestMethod`. Allow TCP 8080 from your client machines and TCP 5001 from `192.168.12.0/24` in the gateway's firewall ([how](docs/NETWORK.md#firewall)). The [HTTP API reference](docs/HTTP-API.md) documents every field and status code.

## Quick start B: direct mode

1. Give the machine that is plugged into the HaLowLink the static address `192.168.12.50/24`, with no gateway: [Linux, macOS and Windows steps](docs/NETWORK.md#set-the-address).
2. Send the alert from your code. In Python, for example:

   ```python
   import socket, struct, time
   s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
   s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("192.168.12.50"))
   s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
   s.sendto(struct.pack("<d", time.time()), ("239.255.0.10", 5000))   # 8 bytes: little-endian double, Unix seconds
   ```

3. Optionally, to learn which wearables acknowledged, accept TCP connections on port 5001 and decode the reply. Allow TCP 5001 from `192.168.12.0/24` in your firewall.

The [integrations](integrations/README.md) contain complete, tested examples for both modes, and the [wire protocol](docs/PROTOCOL.md) documents every byte.

**No hardware yet?** [`tools/fake_wearable.py`](tools/fake_wearable.py) is a stand-in wearable (Python 3.8+, no dependencies) that answers alerts and heartbeat requests like the real one, over loopback, so you can develop and test your integration on your own machine. [Try it without hardware](integrations/README.md#try-it-without-hardware) gives the exact commands for Linux, macOS and Windows, for direct mode and for the gateway image. It follows the documented protocol; it does not prove the real wearable's behaviour.

## Requirements

| Item | Value |
|---|---|
| HaLowLink 2 | Router with HaLow Access Point mode (the factory default), `192.168.12.1/24`. ClearObject sets it up and joins the wearable to it before handover. |
| Link to the HaLowLink | Its USB-C port (one cable, also powers it) or its **LAN** port. Never the WAN port, and never connect the LAN port to another network. |
| Address on the HaLowLink side | **`192.168.12.50/24`, static, no gateway**, on the gateway (setup A) or on your machine (setup B) |
| Alerts | UDP multicast **out** to `239.255.0.10:5000` (multicast TTL 1 or more) |
| Wearable replies | TCP **in** on port `5001`, from `192.168.12.0/24` |
| HTTP API (setup A) | TCP **in** on port `8080`, from your client machines |
| Gateway host (setup A) | Linux on amd64 or arm64, Docker Engine running as root with host networking. Not Docker Desktop, not rootless Docker. 32-bit ARM is not supported. |
| Internet | Not needed (only once, to download the image) |
| VPN | Must not route `192.168.12.0/24`. See [VPNs](docs/NETWORK.md#vpns). |

## Documentation

| Guide | What it covers |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | Components, message flows, delivery semantics, failure modes, security model, logs |
| [Network setup](docs/NETWORK.md) | USB-C or LAN port; the static address on Linux, macOS and Windows; firewall; VPNs |
| [HTTP API](docs/HTTP-API.md) | `POST /alert`, `GET /wearables`, `GET /health`, status codes, service settings. Machine-readable: [openapi.yaml](docs/openapi.yaml) |
| [Wire protocol](docs/PROTOCOL.md) | The UDP alert and the TCP replies byte by byte, and what is verified on hardware |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | `doctor`, common symptoms, what to include in a bug report |

## Integrations and kit files

| Path | What it is |
|---|---|
| [`integrations/`](integrations/README.md) | Ready-made clients for your software, for both setups |
| [`integrations/python/`](integrations/python/) | Python 3.8 or later: one file to copy, or the package `clearobject-halow-client` for pip, with the `halow-client` command |
| [`integrations/javascript/`](integrations/javascript/) | JavaScript (Node.js 18 or later) |
| [`integrations/csharp/`](integrations/csharp/) | C# (.NET 8 or later; the library also builds for .NET Standard 2.1, so Unity 2021.2 or later) |
| [`integrations/curl/`](integrations/curl/) | A curl script, and curl and PowerShell one-liners (gateway mode) |
| [`tools/fake_wearable.py`](tools/fake_wearable.py) | Stand-in wearable for development without hardware |
| [`docker-compose.yml`](docker-compose.yml) | Runs the gateway service from the published image |
| [`.env.example`](.env.example) | The settings `docker-compose.yml` reads from `.env` |
| [`setup-network-linux.sh`](setup-network-linux.sh) | Gives a Linux host the address `192.168.12.50/24` on the HaLowLink (NetworkManager) |

## Versions and releases

The image `ghcr.io/clearobject/halow-alert` is public (no login needed) and built for `linux/amd64` and `linux/arm64`.

| Image tag | Points to | Use it for |
|---|---|---|
| `0.1.0` (`X.Y.Z`) | One exact release; never moved | Anything you depend on. The kit's `docker-compose.yml` pins it. |
| `0.1` (`X.Y`) | The newest patch release of `X.Y` | Picking up fixes automatically |
| `latest` | The newest release | Quick trials only |

Each [GitHub release](https://github.com/clearobject/halow-alert-releases/releases) attaches the kit as a zip (documentation, integrations, Compose file, network setup script and stand-in wearable) with its SHA-256 checksum, and the Python client package for pip as a wheel and an sdist, each with its SHA-256 checksum ([how to install it](integrations/python/README.md#install)). The project follows [Semantic Versioning](https://semver.org/). While the version is 0.x, a minor release may change behaviour; the [changelog](CHANGELOG.md) always says what changed. Version 0.1.0 is a pre-release, not yet verified on hardware.

## Support

- Questions and bug reports: [open an issue](https://github.com/clearobject/halow-alert-releases/issues/new/choose) in this repository. **Never paste tokens, passwords or HaLow network passphrases.**
- The wearable or HaLowLink hardware (powering, charging, silencing an alarm, passwords): your ClearObject contact.
- Security vulnerabilities: report them privately, as described in [SECURITY.md](SECURITY.md).

More in [SUPPORT.md](SUPPORT.md). About ClearObject: [www.clearobject.com](https://www.clearobject.com).

## License

Copyright (c) 2026 ClearObject. All rights reserved. In short:

- **Documentation** in this repository may be read, copied and shared. The code examples in the documentation, and `docs/openapi.yaml`, may also be used, copied and changed under the MIT License.
- **Code** in [`integrations/`](integrations/) and [`tools/`](tools/) is under the MIT License ([`integrations/LICENSE`](integrations/LICENSE)).
- **The container image** may be used free of charge to operate ClearObject HaLow wearables. It may not be redistributed or included in another product without ClearObject's written permission. Third-party components in it, such as the Debian base system and Python, stay under their own licenses.
- **Kit files** (`docker-compose.yml`, `.env.example`, `setup-network-linux.sh`) may be used, copied and changed to run the image.

The full terms are in [LICENSE](LICENSE). Product names of other companies are trademarks of their respective owners.
