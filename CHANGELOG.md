# Changelog

All notable changes for users of halow-alert are listed here: the container image `ghcr.io/clearobject/halow-alert`, the documentation, the Python client and the kit. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-11-22

The first release. It has been tested in software on Linux, macOS and Windows, against the stand-in wearable over loopback, but not yet with HaLow hardware; the points still to check are listed at the end.

Start the gateway on a Linux host that is connected to the HaLowLink, as a service that restarts by itself and comes back after a reboot (the README's Quick start A has the network setup before it):

```bash
HALOW_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))'); echo "token: $HALOW_TOKEN"
docker run -d --name halow-alert --restart unless-stopped --network host \
  -e HALOW_TOKEN="${HALOW_TOKEN:?make the token first}" -e HALOW_IFACE=192.168.12.50 \
  -v halow-alert-data:/data \
  ghcr.io/clearobject/halow-alert:1.0.0
```

Then alert the wearables from Python, on any machine that can reach the gateway. Install the client into a virtual environment (Windows commands: `integrations/python/README.md`):

```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install https://github.com/clearobject/halow-alert-releases/releases/download/v1.0.0/clearobject_halow_client-1.0.0-py3-none-any.whl
```

```python
from halow_client import GatewayClient
GatewayClient("http://<gateway>:8080", token="<token>").alert(label="zone-3 intrusion")
```

### Added

- Container image `ghcr.io/clearobject/halow-alert:1.0.0` (also tagged `1.0` and `latest`) for `linux/amd64` and `linux/arm64`, containing:
  - the alert gateway service (`serve`, the default command): `POST /alert` with a bearer token and a rate limit of one alert per 0.5 s, `GET /wearables` with the ACKs and heartbeats of each wearable, and `GET /health`. It sends a heartbeat request every 15 s, and looks up the HaLowLink address again on every request, so it recovers by itself when the HaLowLink is plugged in late or re-plugged. It writes a JSONL log of every alert, ACK and heartbeat;
  - the diagnostic commands `doctor` (checks the network path and sends nothing), `wearables`, `send` and `listen`;
  - the alert formats `raw8` (the default, as sent by the ClearObject Workzone system), `proto` and `both`, and optional bursts of up to 5 s.
- One `docker run` command that runs the gateway as a service (restart policy, log volume), with the commands to check, update and remove it, in the README.
- Docker Compose kit, as an alternative: `docker-compose.yml`, which runs the published image with host networking, and `.env.example`.
- `setup-network-linux.sh`: gives a Linux host the static address `192.168.12.50/24` on the HaLowLink, with no default route, over USB-C or the LAN port (NetworkManager).
- A Python client, `integrations/python/halow_client.py`, for both the HTTP API (setup A) and direct multicast (setup B), under the MIT License. From other languages, call the HTTP API directly: `docs/HTTP-API.md` has curl, PowerShell and Python examples.
- Python package `clearobject-halow-client` 1.0.0: the Python client's `halow_client` module and the `halow-client` command, for Python 3.8 or newer, with no dependencies. The GitHub release attaches it as a wheel and an sdist, each with a SHA-256 file. It can also be installed with pip straight from this repository (`git+https://github.com/clearobject/halow-alert-releases@v1.0.0#subdirectory=integrations/python`). It is not published on PyPI.
- Python examples: `example.py` (one alert, then wait for the ACK, through the gateway or directly), `examples/alert_from_your_app.py` (alerts from inside a program loop, on a background thread, with a retry on `429`) and `examples/wearable_status.py` (a check before a session: can the gateway send, and which wearables answer).
- `tools/fake_wearable.py`: a standalone stand-in wearable, for developing without hardware.
- Tests for the Python client (`unittest`, in `integrations/python/tests/`), with seeded fuzzing of the reply decoder and loopback tests of direct mode. CI runs them against the source file and against the installed wheel, on Linux, macOS and Windows. None of them reaches a wearable.
- Documentation: overview and quick starts, alerting architecture, network setup for Linux, macOS and Windows, HTTP API reference with an OpenAPI 3.0.3 description, wire protocol, troubleshooting, and the security and support policies.

### Security

- The gateway refuses to listen beyond `127.0.0.1` without a token, and refuses the placeholder token `CHANGE-ME`. Tokens are compared in constant time and never logged.
- Wearable replies are accepted only from the HaLowLink network, `192.168.12.0/24`.
- Limits: request bodies up to 64 KiB, 64 simultaneous HTTP connections, idle HTTP clients dropped after 10 s, 32 wearable replies in flight, each read for at most 5 s and 8 KiB.
- The container runs as a non-root user.
- The Python client never follows a redirect, so the token is sent only to the gateway address you configured.

### Known limitations

- A gateway without a token (allowed only on `127.0.0.1`, or with `--allow-no-token`) can be triggered by any program on that machine, and by any web page open in a browser that can reach it. Keep a token whenever a browser runs there.
- A `POST /alert` body sent with chunked transfer encoding (no `Content-Length`) is ignored, and the alert goes out with the default settings. All common HTTP clients send a `Content-Length` for a string body; in .NET, use `StringContent` rather than `PostAsJsonAsync`.

### Not yet verified on hardware

- That a wearable alarms on an alert from halow-alert, over USB-C and over the HaLowLink's LAN port.
- Where the wearables send their ACKs: to the address the alert came from, as expected, or to a fixed address.
- How long after the alert the ACK arrives, and whether the default 3 s wait of the `send` command is long enough.
- Whether the wearables accept the `proto` alert format from halow-alert.
- How the wearables treat the repeated datagrams of a `raw8` burst, and the `both` format: one alarm or more.
- Whether the wearables need heartbeat requests to stay armed, and whether they send heartbeats on their own.
- The gateway container on a Linux host with a HaLowLink: recovery after the HaLowLink is unplugged and re-plugged, and after a host reboot.
- Operation with no internet connection, end to end.
- The HaLowLink's USB-C link on macOS and Windows.

[Unreleased]: https://github.com/clearobject/halow-alert-releases/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/clearobject/halow-alert-releases/releases/tag/v1.0.0
