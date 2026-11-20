# Integrations

Small clients for raising ClearZone wearable alerts from your own software, for example a simulator. There is one for Python, JavaScript (Node.js), C# (.NET and Unity) and curl. None of them needs a third-party package, and they are MIT licensed ([LICENSE](LICENSE)), so you can copy them into your own code. The Python client is also a package for pip, `clearobject-halow-client`, which installs the `halow-client` command ([how to install it](python/README.md#install)).

> **Alerts are real.** Every wearable on the HaLowLink network vibrates and sounds when an alert goes out, including when you run an example. Warn the people nearby first. To develop without hardware, use the stand-in wearable in [`tools/fake_wearable.py`](../tools/fake_wearable.py) (see [Try it without hardware](#try-it-without-hardware)).

## Pick a mode

Each client can raise an alert in two ways.

| | Gateway mode (HTTP) | Direct mode (UDP) |
|---|---|---|
| Where your code runs | Any machine that can reach the gateway over your network | A machine plugged into the HaLowLink (USB-C or its LAN port) with the address `192.168.12.50` |
| What it does | `POST /alert` to the halow-alert service on the Linux gateway host, with a token | Sends the alert datagram itself: UDP multicast to `239.255.0.10:5000` |
| Replies from the wearables | `GET /wearables` on the gateway (ACKs, battery, signal) | Listen on TCP `5001` yourself (each client includes a listener) |
| Needs | The gateway running (Docker on Linux), the URL and the token | Nothing else; works on Linux, macOS and Windows |
| Rate limit, log, heartbeat requests | Done by the gateway (one alert per 0.5 s, JSONL log, a heartbeat request every 15 s) | Up to you |

Use **gateway mode** when you can: it keeps the token check, the rate limit and the log in one place, and your code can run anywhere on your network. Use **direct mode** when there is no Linux gateway, for example on a macOS or Windows laptop plugged straight into the HaLowLink. Docker Desktop and rootless Docker cannot reach the HaLowLink, so the gateway container does not help there.

In direct mode:

- Send at most one alert every 0.5 s, as the gateway does.
- Only one program on a machine can listen on TCP 5001. Don't run direct mode on the gateway host while the service runs.
- Allow inbound TCP 5001 from `192.168.12.0/24` in your firewall, or no ACKs arrive.
- A wearable sends its ACK only after its alarm sequence ends, which can take several seconds.
- The gateway asks the wearables to report in every 15 s, as ClearObject's production systems do. Whether the wearables need those requests to stay ready is not yet verified on hardware. If your direct-mode program runs for a long time, send a heartbeat request every 15 s (each client has a function for it; nothing buzzes).

## Pick a language

| Folder | Runtime | Library | Example |
|---|---|---|---|
| [`python/`](python/) | Python 3.8 or newer | `halow_client.py`, also the `halow-client` command; installable with pip as `clearobject-halow-client` | `example.py` |
| [`javascript/`](javascript/) | Node.js 18 or newer | `halow-client.mjs` (ES module) | `example.mjs` |
| [`csharp/`](csharp/) | .NET 8 or newer (tested), any .NET Standard 2.1 runtime, or Unity 2021.2 or newer | `HalowClient.cs` (one file) | `Example.cs` in `HalowExample.csproj` (.NET 8 or newer) |
| [`curl/`](curl/) | bash and curl | `alert.sh` (gateway mode only) | |

Each folder's README lists its functions. Every example runs the same steps:

- **Gateway mode** (the default): check `GET /health`, send one alert, then poll `GET /wearables` until the ACK shows up.
- **Direct mode** (`--direct`): start the TCP 5001 listener, send a heartbeat request (no buzz) and print the heartbeat, then send one alert and wait for its ACK.
- **`--selftest`**: check the client's encoder and decoder against [`testdata/vectors.json`](testdata/vectors.json). Nothing is sent.

## Settings

The examples and the Python `halow-client` command read these environment variables. The libraries read only `HALOW_URL` and `HALOW_TOKEN` (gateway client) and `HALOW_EGRESS` (direct mode), and only when you pass no value. The alert group and port and the reply port are function arguments whose defaults are the values below, so pass them yourself when you test with the stand-in wearable.

| Variable | Used by | Default | Meaning |
|---|---|---|---|
| `HALOW_URL` | gateway mode | `http://127.0.0.1:8080` | The gateway's address, for example `http://192.0.2.10:8080`. |
| `HALOW_TOKEN` | gateway mode | none | The gateway's token, sent as `Authorization: Bearer <token>` on `POST /alert`. Keep it out of your code and repository. |
| `HALOW_EGRESS` | direct mode | `192.168.12.50` | This machine's IPv4 address on the HaLowLink. Alerts leave from it (`IP_MULTICAST_IF`). |
| `HALOW_GROUP`, `HALOW_PORT` | direct mode | `239.255.0.10`, `5000` | The alert group. Change them only for testing with the stand-in wearable. |
| `HALOW_RESPONSE_PORT` | direct mode | `5001` | TCP port for the wearables' replies. |
| `HALOW_FORMAT` | direct-mode examples, curl | `raw8` in direct mode; for curl, the gateway's own `HALOW_FORMAT` | `raw8`, `proto` or `both` (see [the protocol](../docs/PROTOCOL.md)). The gateway-mode examples do not send a format, so the gateway's setting applies. |
| `HALOW_ACK_WAIT` | examples | `15` | Seconds an example waits for the ACK, from 0 to 3600. Any other value is a bad setting (exit code 2), checked before anything is sent. |
| `HALOW_BURST_SECONDS`, `HALOW_TIMEOUT` | curl | none, `10` | Repeat the alert for this long (0 to 5 s); request timeout in seconds. |

The clients ignore HTTP proxy settings, because the gateway is on your local network.

## Errors

In gateway mode, every client turns a failed call into one error with the HTTP status and a hint. The error is `HalowError` in Python and JavaScript and `HalowException` in C#.

| Status | Meaning | What to do |
|---|---|---|
| none | The gateway was not reached: connection refused, timeout, wrong address | Check `HALOW_URL`, the network, and the gateway host's firewall (TCP 8080). |
| `401` | Missing or wrong token | Set `HALOW_TOKEN` to the gateway's token. |
| `429` | Less than 0.5 s since the previous alert | Retry after 0.5 s. Requests that fail do not count towards the limit. |
| `500` | The gateway could not send: its HaLowLink address is missing, for example because the HaLowLink is unplugged | Check `GET /health` and the gateway host's network setup. |
| `503` | From `GET /health`: the gateway cannot send right now | The `error` field says why. `health()` returns this body instead of raising. |
| `400`, `413` | Malformed request | The `error` field says what is wrong. |

The examples exit with `0` when the alert was sent and a wearable ACKed it, `3` when the alert was sent but no ACK came within `HALOW_ACK_WAIT` seconds, `1` when the alert failed, and `2` for bad settings (for example an egress address that is not on this machine, or TCP 5001 already in use).

## Try it without hardware

The stand-in wearable answers like a wearable, over loopback, on ports that nothing else uses. It is a development aid. It follows the documented protocol, but it does not prove that the real wearables behave the same way.

On Linux, allow multicast on loopback first. Do this only on a machine that does not drive a HaLowLink, or remove the route afterwards (`sudo ip route del 239.0.0.0/8 dev lo`). macOS and Windows need no setup.

```bash
sudo ip link set lo multicast on
sudo ip route add 239.0.0.0/8 dev lo
```

Direct mode on Linux or macOS, from the repository root:

```bash
python3 tools/fake_wearable.py --iface 127.0.0.1 --group 239.255.77.2 --port 15010 --response-port 15011 --seconds 0 &
export HALOW_EGRESS=127.0.0.1 HALOW_GROUP=239.255.77.2 HALOW_PORT=15010 HALOW_RESPONSE_PORT=15011
python3 integrations/python/example.py --direct
node integrations/javascript/example.mjs --direct
dotnet run --project integrations/csharp -- --direct
```

Direct mode on **Windows** (PowerShell), from the repository root. In one window, start the stand-in wearable (use `python` if `py` is missing; if Windows Firewall asks, allow it):

```powershell
py tools\fake_wearable.py --iface 127.0.0.1 --group 239.255.77.2 --port 15010 --response-port 15011 --seconds 0
```

In a second window:

```powershell
$env:HALOW_EGRESS = '127.0.0.1'; $env:HALOW_GROUP = '239.255.77.2'; $env:HALOW_PORT = '15010'; $env:HALOW_RESPONSE_PORT = '15011'
py integrations\python\example.py --direct
node integrations\javascript\example.mjs --direct
dotnet run --project integrations\csharp -- --direct
```

Not yet tested on Windows.

Gateway mode needs a Linux host with Docker Engine. Run the gateway image on loopback with a second stand-in wearable:

```bash
python3 tools/fake_wearable.py --iface 127.0.0.1 --group 239.255.77.1 --port 15000 --response-port 15001 --seconds 0 &
docker run -d --name halow-dev --network host \
  -e HALOW_IFACE=127.0.0.1 -e HALOW_ROUTER=127.0.0.1 -e HALOW_HTTP=127.0.0.1:18080 \
  -e HALOW_GROUP=239.255.77.1 -e HALOW_PORT=15000 -e HALOW_RESPONSE_PORT=15001 \
  -e HALOW_TOKEN=dev-only ghcr.io/clearobject/halow-alert:0.1.0
export HALOW_URL=http://127.0.0.1:18080 HALOW_TOKEN=dev-only
python3 integrations/python/example.py
node integrations/javascript/example.mjs
integrations/curl/alert.sh "test alert"
dotnet run --project integrations/csharp
docker rm -f halow-dev
```

The repository's CI runs these same steps on every change.

To test gateway-mode code that runs on Windows or macOS, run the block above on a Linux machine on your network, with `-e HALOW_HTTP=0.0.0.0:18080 -e HALOW_TOKEN=<long random token>` in place of the loopback address and `dev-only`. Allow TCP 18080 from your workstation in that machine's firewall, then set `HALOW_URL=http://<linux-machine>:18080` and `HALOW_TOKEN` on your workstation. Without any Linux host, only direct mode can be tried.

## Wire formats

All clients put the same bytes on the wire as the gateway. [`testdata/vectors.json`](testdata/vectors.json) holds inputs and the exact bytes or decoded values that the gateway's own encoder and decoder produce, and each client's `--selftest` checks itself against them. [`docs/PROTOCOL.md`](../docs/PROTOCOL.md) describes the formats byte by byte.

- **Alert, `raw8`** (the default): 8 bytes, the Unix time in seconds as a little-endian IEEE-754 double.
- **Alert or heartbeat request, `proto`**: protobuf `GatewayMessage { uint64 timestamp = 1 (ms); CommandCode command = 2 }`, where `1` is ALERT and `2` is HEARTBEAT_REQ.
- **Wearable reply**: one protobuf `DeviceResponse` per TCP connection to port 5001, either an ACK or a HEARTBEAT (device ID, battery, RSSI, uptime). The clients decode it by hand, so no protobuf library is needed.

## Status

Version 0.1.0 is a pre-release. The clients are tested in software only: CI runs every example against the published gateway image and the stand-in wearable on loopback, and installs the Python package from its wheel and tests it on Linux and macOS, and on Windows except direct mode, whose tests may be skipped there (see "Not yet tested on Windows" above). They have not yet been tested against a real wearable. For questions, open an issue on this repository or ask your ClearObject contact.
