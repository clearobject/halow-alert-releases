# Changelog

All notable changes for users of halow-alert are listed here: the container image `ghcr.io/clearobject/halow-alert`, the documentation, the integrations and the kit. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Version 1.0.0 will follow verification with HaLow hardware.

## [Unreleased]

## [0.1.0] - 2026-11-19

**Pre-release.** The first public release. It has been tested in software, against the stand-in wearable over loopback, but not yet with HaLow hardware; the open points are listed at the end.

### Added

- Container image `ghcr.io/clearobject/halow-alert:0.1.0` (also tagged `0.1` and `latest`) for `linux/amd64` and `linux/arm64`, containing:
  - the alert gateway service (`serve`, the default command): `POST /alert` with a bearer token and a rate limit of one alert per 0.5 s, `GET /wearables` with the ACKs and heartbeats of each wearable, and `GET /health`. It sends a heartbeat request every 15 s, and looks up the HaLowLink address again on every request, so it recovers by itself when the HaLowLink is plugged in late or re-plugged. It writes a JSONL log of every alert, ACK and heartbeat;
  - the diagnostic commands `doctor` (checks the network path and sends nothing), `wearables`, `send` and `listen`;
  - the alert formats `raw8` (the default, as sent by the ClearObject Workzone system), `proto` and `both`, and optional bursts of up to 5 s.
- Docker Compose kit: `docker-compose.yml`, which runs the published image with host networking, and `.env.example`.
- `setup-network-linux.sh`: gives a Linux host the static address `192.168.12.50/24` on the HaLowLink, with no default route, over USB-C or the LAN port (NetworkManager).
- Integrations for Python, JavaScript (Node.js), C# and curl or PowerShell, for both the HTTP API (setup A) and direct multicast (setup B), under the MIT License.
- Python package `clearobject-halow-client` 0.1.0: the Python client's `halow_client` module and the `halow-client` command, for Python 3.8 or newer, with no dependencies. The GitHub release attaches it as a wheel and an sdist, each with a SHA-256 file. It can also be installed with pip straight from this repository (`git+https://github.com/clearobject/halow-alert-releases@v0.1.0#subdirectory=integrations/python`). It is not published on PyPI.
- `tools/fake_wearable.py`: a standalone stand-in wearable, for developing without hardware.
- Documentation: overview and quick starts, alerting architecture, network setup for Linux, macOS and Windows, HTTP API reference with an OpenAPI 3.0.3 description, wire protocol, troubleshooting, and the security and support policies.

### Security

- The gateway refuses to listen beyond `127.0.0.1` without a token, and refuses the placeholder token `CHANGE-ME`. Tokens are compared in constant time and never logged.
- Wearable replies are accepted only from the HaLowLink network, `192.168.12.0/24`.
- Limits: request bodies up to 64 KiB, 64 simultaneous HTTP connections, idle HTTP clients dropped after 10 s, 32 wearable replies in flight, each read for at most 5 s and 8 KiB.
- The container runs as a non-root user.

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

[Unreleased]: https://github.com/clearobject/halow-alert-releases/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/clearobject/halow-alert-releases/releases/tag/v0.1.0
