# HTTP API

The alert gateway (setup A) exposes a small HTTP API on port `8080`. Your software calls it to trigger the wearables, see which wearables are answering, and check that the gateway can send. A machine-readable description is in [openapi.yaml](openapi.yaml) (OpenAPI 3.0.3).

| Request | Token | Purpose |
|---|---|---|
| [`POST /alert`](#post-alert) | required | Alert every wearable on the HaLowLink network |
| [`GET /wearables`](#get-wearables) | no | The wearables that have answered: ACK counts, battery, signal strength |
| [`GET /health`](#get-health) (also `GET /`) | no | Whether the gateway can send right now |

The response bodies below are real responses from halow-alert, captured on a test setup. Addresses and times were edited to show a typical deployment.

## Contents

- [Conventions](#conventions)
- [Authentication](#authentication)
- [POST /alert](#post-alert)
- [GET /wearables](#get-wearables)
- [GET /health](#get-health)
- [Status codes](#status-codes)
- [Retries and timeouts](#retries-and-timeouts)
- [Calling the API](#calling-the-api)
- [Service settings](#service-settings)

## Conventions

- **Base URL:** `http://<gateway>:8080`, where `<gateway>` is the gateway's host name or address on your network. Plain HTTP; there is no TLS (see [Security model](ARCHITECTURE.md#security-model)).
- **Paths are matched exactly.** No trailing slash and no query string: `GET /health?x=1` returns `404`.
- **Responses** are JSON (`Content-Type: application/json`), indented for reading. Every error body is `{"error": "<reason>"}`.
- **Methods:** only `GET` and `POST`. Other methods, including `HEAD` and `OPTIONS`, get `501` with an HTML error page. The API sends no CORS headers, so call it from server-side code or a script, not from a web page in a browser.
- **Connections:** the gateway answers with `HTTP/1.0` and closes the connection after each response. HTTP clients handle this by themselves.
- **Request bodies** must come with a `Content-Length` header and be at most 64 KiB. Chunked transfer encoding is not supported: such a body is ignored, and the alert is sent with the default settings. All common clients send `Content-Length` for a string or byte body; see [C#](#c-sharp) for the one exception to watch for.
- **Limits:** at most 64 simultaneous connections; a client that sends nothing for 10 s is disconnected.

## Authentication

`POST /alert` needs the token the gateway was started with (`HALOW_TOKEN`), as a bearer token:

```http
Authorization: Bearer <token>
```

The header must match exactly: the word `Bearer` with a capital B, one space, then the token. A missing or different header gets `401`. Use a long random token (the [quick start](../README.md#quick-start-a-alert-gateway) makes one) of letters, digits, `-` and `_`.

`GET /wearables` and `GET /health` need no token.

The gateway refuses to start on a network address without a token, and refuses the placeholder `CHANGE-ME`. A gateway that listens on `127.0.0.1` only may run without a token; then `POST /alert` needs no header. Then any program on that machine can set off the wearables, and so can any web page open in a browser there, because browsers send a simple `POST` to another address without asking it first. Keep a token if a browser runs on that machine.

## POST /alert

Sends one alert to every wearable on the HaLowLink network. The response comes back as soon as the alert is sent, or when its burst ends. It does not wait for the wearables' ACKs; read those from [`GET /wearables`](#get-wearables).

### Request

The body is optional. Without a body, or with `{}`, the alert uses the gateway's defaults.

```http
POST /alert HTTP/1.1
Host: <gateway>:8080
Authorization: Bearer <token>
Content-Type: application/json
Content-Length: 29

{"label": "zone-3 intrusion"}
```

| Field | Type | Default | Rules |
|---|---|---|---|
| `label` | string | `""` | Free text that identifies the alert in the response and in the gateway's log, for example the zone or the scenario. The first 200 characters are kept. Other JSON types are converted to text. |
| `format` | string: `"raw8"`, `"proto"` or `"both"` | the gateway's `HALOW_FORMAT`, normally `"raw8"` | Case-insensitive. Keep the default unless ClearObject advises otherwise; the [wire protocol](PROTOCOL.md) explains the formats. Anything else gets `400`. |
| `burst_seconds` | number | the gateway's `HALOW_BURST_SECONDS`, normally `0` | Re-send the identical alert every 100 ms for this many seconds, to ride out radio loss. Values below 0 count as 0 and values above 5 as 5. Not a number (a string, `true`): `400`. |

A field that is missing or `null` gets the default. Other fields are ignored.

### Responses

**`200` OK**: the alert was sent. The body is the send record, the same line the gateway writes to its log.

```json
{
 "event": "alert_sent",
 "ts_ms": 1795111196804,
 "source": "http:192.0.2.10:zone-3 intrusion",
 "format": "raw8",
 "group": "239.255.0.10:5000",
 "egress": "192.168.12.50",
 "datagrams": 1
}
```

| Field | Meaning |
|---|---|
| `event` | Always `alert_sent`. |
| `ts_ms` | When the alert was sent, in milliseconds since the Unix epoch, by the gateway's clock. This is also the alert's timestamp on the wire. |
| `source` | `http:<caller-ip>:<label>`: the address the request came from, as the gateway saw it, and the label. |
| `format` | The format that was sent: `raw8`, `proto` or `both`. |
| `group` | The multicast group and port: `239.255.0.10:5000`. |
| `egress` | The gateway address the alert left from: `192.168.12.50`. |
| `datagrams` | How many datagrams were sent: 1, 2 for `both`, more with a burst. |

With `{"format": "both", "burst_seconds": 1}` the response arrives after about 1 s and reports `"format": "both"` and `"datagrams": 20` (10 copies of each format).

A `200` means the alert left the gateway. It does not prove that a wearable alarmed: a wearable that is off or out of range simply does not ACK.

**`400` Bad Request**: the request is malformed. Nothing was sent, and the rate limit was not used up. The `error` is one of:

| `error` | Cause |
|---|---|
| `body must be JSON` | The body is not valid JSON. |
| `body must be a JSON object, for example {"label": "zone-3"}` | The body is JSON, but not an object (an array, a string, a number). |
| `format must be "raw8", "proto" or "both"` | `format` is another string, or not a string. |
| `burst_seconds must be a number (0 to 5)` | `burst_seconds` is not a number. |
| `bad Content-Length` | The `Content-Length` header is not a whole number. |
| `body shorter than its Content-Length` | The client closed the connection before sending the whole body. |
| `body not received: <reason>` | The body did not arrive, for example within 10 s. |

```json
{
 "error": "format must be \"raw8\", \"proto\" or \"both\""
}
```

**`401` Unauthorized**: the `Authorization` header is missing or does not match. Nothing was sent.

```json
{
 "error": "missing or wrong Bearer token"
}
```

**`404` Not Found**: the path is not `/alert`. Checked before the token.

```json
{
 "error": "POST /alert"
}
```

**`413` Payload Too Large**: the body is over 65536 bytes.

```json
{
 "error": "body too large (at most 65536 bytes)"
}
```

**`429` Too Many Requests**: an alert was accepted less than 0.5 s ago (the gateway's `--min-gap`). Nothing was sent. There is no `Retry-After` header; wait at least 0.5 s.

```json
{
 "error": "alerts limited to one per 0.5 s"
}
```

**`500` Internal Server Error**: sending failed, almost always because the HaLowLink address is not on the gateway right now (the HaLowLink is unplugged, rebooting or still booting). The rate limit was not used up. The gateway recovers by itself once the address is back; [`GET /health`](#get-health) shows the same reason.

```json
{
 "error": "192.168.12.50 is not an address of this machine (Cannot assign requested address); is the HaLowLink plugged in and the address set? (see docs/NETWORK.md at github.com/clearobject/halow-alert-releases)"
}
```

An unexpected failure inside the gateway also gets `500`, with an `error` that starts with `internal error:`.

## GET /wearables

The wearables that have answered since the gateway started: one entry per wearable, and the latest replies. No token needed.

The gateway asks every wearable for a heartbeat when it starts and then every 15 s, so `last_seen_ms` stays current even without alerts. The roster is kept in memory: it starts empty when the gateway restarts, and a wearable stays listed after it goes quiet. Treat a wearable as offline when `last_seen_ms` is more than 60 s old.

```json
{
 "devices": [
  {
   "ip": "192.168.12.143",
   "first_seen_ms": 1795111180012,
   "acks": 1,
   "heartbeats": 2,
   "last_seen_ms": 1795111202917,
   "device_id": "wz-07",
   "battery_percentage": 87,
   "rssi": -62,
   "uptime_seconds": 3615,
   "app_state": 1,
   "device_state": 2,
   "last_ack_ms": 1795111202917,
   "last_ack_device_ts_ms": 1795111196851
  }
 ],
 "recent": [
  {
   "event": "wearable_heartbeat",
   "ts_ms": 1795111180012,
   "ip": "192.168.12.143",
   "timestamp_ms": 1795111179990,
   "code": "HEARTBEAT",
   "heartbeat": {
    "device_id": "wz-07",
    "battery_percentage": 87,
    "rssi": -62,
    "uptime_seconds": 3600,
    "app_state": 1,
    "device_state": 2
   }
  },
  {
   "event": "wearable_heartbeat",
   "ts_ms": 1795111195031,
   "ip": "192.168.12.143",
   "timestamp_ms": 1795111194990,
   "code": "HEARTBEAT",
   "heartbeat": {
    "device_id": "wz-07",
    "battery_percentage": 87,
    "rssi": -62,
    "uptime_seconds": 3615,
    "app_state": 1,
    "device_state": 2
   }
  },
  {
   "event": "wearable_ack",
   "ts_ms": 1795111202917,
   "ip": "192.168.12.143",
   "timestamp_ms": 1795111196851,
   "code": "ACK",
   "ack_success": true
  }
 ]
}
```

Before any wearable has answered: `{"devices": [], "recent": []}`.

**`devices`**: one object per wearable address. All times are milliseconds since the Unix epoch, by the gateway's clock unless noted.

| Field | Present | Meaning |
|---|---|---|
| `ip` | always | The wearable's address on the HaLowLink network. ACKs carry no device ID, so wearables are keyed by address. |
| `first_seen_ms`, `last_seen_ms` | always | When the gateway first and last heard from it. |
| `acks`, `heartbeats` | always | How many ACKs and heartbeats it has sent since the gateway started. |
| `last_ack_ms` | after an ACK | When the last ACK arrived. |
| `last_ack_device_ts_ms` | after an ACK | When the wearable received the alert it ACKed, **by the wearable's clock**. |
| `device_id` | after a heartbeat | The wearable's ID (at most 16 bytes). |
| `battery_percentage` | after a heartbeat | Battery charge, in percent. |
| `rssi` | after a heartbeat | Received signal strength, in dBm (negative; closer to 0 is stronger). |
| `uptime_seconds` | after a heartbeat | Seconds since the wearable started. |
| `app_state`, `device_state` | after a heartbeat | Numeric firmware states, reported as they are. |

**`recent`**: the last 50 replies, oldest first. Each is one of:

| Field | Meaning |
|---|---|
| `event` | `wearable_ack` or `wearable_heartbeat` |
| `ts_ms` | When the reply arrived at the gateway |
| `ip` | The wearable's address |
| `timestamp_ms` | ACK: when the wearable received the alert, by its clock. Heartbeat: the heartbeat request's timestamp, echoed back, so `ts_ms - timestamp_ms` is the round-trip time. |
| `code` | `ACK` or `HEARTBEAT` |
| `ack_success` | ACK only: `true` when the wearable reports success |
| `heartbeat` | Heartbeat only: `device_id`, `battery_percentage`, `rssi`, `uptime_seconds`, `app_state`, `device_state` |

**Did my alert arrive?** Note `ts_ms` from the `POST /alert` response, then poll `GET /wearables` for a few seconds. A wearable acknowledged the alert when its `last_ack_ms` is later than that `ts_ms`. Allow several seconds: a wearable sends its ACK after its alarm sequence ends or is silenced. ACKs carry no alert ID, so if you send alerts in quick succession, match each ACK to the most recent alert sent before it.

## GET /health

Whether the gateway can send right now. No token needed. `GET /` returns the same. The container's health check calls it.

**`200` OK**: a sending address is present and the wearable reply listener is running.

```json
{
 "ok": true,
 "version": "1.0.0",
 "egress": "192.168.12.50",
 "error": null,
 "group": "239.255.0.10:5000",
 "format": "raw8",
 "listener": true
}
```

**`503` Service Unavailable**: the gateway cannot send, or cannot receive replies. The body has the same fields, with `ok` set to `false` and the reason in `error`.

```json
{
 "ok": false,
 "version": "1.0.0",
 "egress": null,
 "error": "192.168.12.50 is not an address of this machine (Cannot assign requested address); is the HaLowLink plugged in and the address set? (see docs/NETWORK.md at github.com/clearobject/halow-alert-releases)",
 "group": "239.255.0.10:5000",
 "format": "raw8",
 "listener": true
}
```

| Field | Meaning |
|---|---|
| `ok` | `true` when the status is `200` |
| `version` | The halow-alert version |
| `egress` | The gateway address alerts leave from, or `null` when there is none |
| `error` | Why the gateway cannot send, or `null` |
| `group` | The multicast group and port |
| `format` | The default alert format |
| `listener` | Whether the wearable reply listener (TCP 5001) is running |

The `error` texts you may see:

| `error` | Meaning |
|---|---|
| `192.168.12.50 is not an address of this machine (...); is the HaLowLink plugged in and the address set? ...` | The fixed `HALOW_IFACE` address is missing: the HaLowLink is unplugged, rebooting or still booting, or the network setup did not run. |
| `this machine reaches 192.168.12.1 via <address>, which is not on the HaLowLink subnet; plug in the HaLowLink (USB-C or LAN port) or pass --iface` | No `HALOW_IFACE` is set and the gateway has no `192.168.12.x` address, or a VPN owns the route. |
| `the wearable response listener stopped; restart the service` | The TCP 5001 listener has stopped. Restart the container. |

`/health` does not check the wearables. Use `GET /wearables` for that.

## Status codes

| Status | Endpoint | Meaning | Uses up the rate limit | Retry? |
|---|---|---|---|---|
| `200` | all | Success | `POST /alert`: yes | |
| `400` | `POST /alert` | Malformed request | no | No: fix the request |
| `401` | `POST /alert` | Missing or wrong token | no | No: fix the token |
| `404` | all | Unknown path (or a query string) | no | No |
| `413` | `POST /alert` | Body over 64 KiB | no | No |
| `429` | `POST /alert` | Less than 0.5 s since the last accepted alert | no | After 0.5 s, if you still need a new alert |
| `500` | `POST /alert` | Sending failed; see `GET /health` | no | Yes, after the cause is fixed |
| `501` | all | Method other than `GET` and `POST` (HTML body) | no | No |
| `503` | `GET /health`, `GET /` | The gateway cannot send or receive right now | | Poll until `200` |

## Retries and timeouts

- Set the client timeout to at least 10 s. A request with `burst_seconds` is answered only when the burst ends, up to 5 s later.
- Do not retry `400`, `401`, `404` or `413`.
- On `429`, the previous alert is less than 0.5 s old and is already alarming the wearables. Retry after 0.5 s only if you need a separate alert.
- On `500`, check `GET /health`, then retry. The gateway recovers on its own when the HaLowLink comes back.
- A refused or timed-out connection means the gateway is not running, or a firewall blocks TCP 8080.
- Retrying a `POST /alert` that timed out on your side may alert twice: the first request may have been sent.

## Calling the API

These examples read the gateway's URL from `HALOW_URL` (for example `http://192.0.2.10:8080`) and the token from `HALOW_TOKEN`, the same variables the integrations use. The [integrations](../integrations/README.md) have complete clients for [Python](../integrations/python/), [JavaScript](../integrations/javascript/), [C#](../integrations/csharp/) and [curl and PowerShell](../integrations/curl/).

**curl** (Linux, macOS; `curl.exe` on Windows):

```bash
curl -sS -X POST "$HALOW_URL/alert" \
     -H "Authorization: Bearer $HALOW_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"label": "zone-3 intrusion"}'
```

**PowerShell:**

```powershell
Invoke-RestMethod -Method Post -Uri "$env:HALOW_URL/alert" `
  -Headers @{ Authorization = "Bearer $env:HALOW_TOKEN" } `
  -ContentType "application/json" -Body '{"label": "zone-3 intrusion"}'
```

**Python** (standard library):

```python
import json, os, urllib.request

req = urllib.request.Request(
    os.environ["HALOW_URL"] + "/alert",
    data=json.dumps({"label": "zone-3 intrusion"}).encode(),
    headers={"Authorization": "Bearer " + os.environ["HALOW_TOKEN"],
             "Content-Type": "application/json"},
    method="POST")
with urllib.request.urlopen(req, timeout=10) as r:
    print(json.load(r))
```

`urllib` raises `HTTPError` for `4xx` and `5xx`; its `read()` holds the JSON error body.

**JavaScript** (Node.js 18 or later):

```javascript
const r = await fetch(`${process.env.HALOW_URL}/alert`, {
  method: "POST",
  headers: { Authorization: `Bearer ${process.env.HALOW_TOKEN}`, "Content-Type": "application/json" },
  body: JSON.stringify({ label: "zone-3 intrusion" }),
  signal: AbortSignal.timeout(10000),
});
console.log(r.status, await r.json());
```

<a id="c-sharp"></a>**C#** (.NET 8 or later; the [C# integration](../integrations/csharp/) also covers Unity):

```csharp
using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
var req = new HttpRequestMessage(HttpMethod.Post, Environment.GetEnvironmentVariable("HALOW_URL") + "/alert")
{
    Content = new StringContent("{\"label\": \"zone-3 intrusion\"}", System.Text.Encoding.UTF8, "application/json"),
};
req.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue(
    "Bearer", Environment.GetEnvironmentVariable("HALOW_TOKEN"));
var res = await http.SendAsync(req);
Console.WriteLine($"{(int)res.StatusCode} {await res.Content.ReadAsStringAsync()}");
```

Use `StringContent` (or `ByteArrayContent`), which sends a `Content-Length`. `PostAsJsonAsync` and `JsonContent` can stream the body with chunked transfer encoding, which this version ignores: the alert would go out with the default settings, without your label.

## Service settings

The gateway service is configured with environment variables: for Compose, in `.env` for the variables that `docker-compose.yml` lists under `environment:` (add any other one there); for `docker run`, with `-e NAME=value`. The Compose kit always requires `HALOW_TOKEN`, so `HALOW_ALLOW_NO_TOKEN` does not apply there. A variable that should be a number but is not is reported at start-up and ignored. The network settings must match the wearables' configuration: change them only if ClearObject tells you to.

| Variable | Default | Meaning |
|---|---|---|
| `HALOW_TOKEN` | empty | The bearer token for `POST /alert`. Required, unless `HALOW_HTTP` is on `127.0.0.1` or `HALOW_ALLOW_NO_TOKEN=1`. Surrounding spaces are removed. |
| `HALOW_HTTP` | `0.0.0.0:8080` | Where the API listens: `host:port` or just `port`. Change the port here, not with `--http`, because the container's health check reads this variable. |
| `HALOW_IFACE` | empty (the Compose kit sets `192.168.12.50`) | The gateway's sending address on the HaLowLink network, or an interface name (Linux). Empty: use the local address that reaches `HALOW_ROUTER`, if it is on that `/24`. With Compose, `HALOW_IFACE=` (empty) in `.env` selects this. |
| `HALOW_ROUTER` | `192.168.12.1` | The HaLowLink's address. Wearable replies are accepted from its `/24` only. |
| `HALOW_GROUP` | `239.255.0.10` | Multicast group. |
| `HALOW_PORT` | `5000` | Multicast port. |
| `HALOW_RESPONSE_PORT` | `5001` | TCP port for the wearable replies. |
| `HALOW_FORMAT` | `raw8` | Default alert format: `raw8`, `proto` or `both`. |
| `HALOW_BURST_SECONDS` | `0` | Default burst length for `POST /alert`, 0 to 5. |
| `HALOW_HEARTBEAT` | `15` | Seconds between heartbeat requests, in whole seconds; `0` turns them off. |
| `HALOW_TTL` | `4` | Multicast TTL, 1 to 255. |
| `HALOW_LOG` | `/data/halow-alert.jsonl` in the image | The [JSONL log](ARCHITECTURE.md#what-the-gateway-logs). Empty turns it off (with Compose, `HALOW_LOG=` in `.env`). Keep it under `/data` in the container, which the Compose kit maps to `./data`. |
| `HALOW_ALLOW_NO_TOKEN` | off | `1` lets the API listen beyond `127.0.0.1` without a token. Then anyone who can reach the port can set off the wearables, including any web page open in a browser on a machine that can reach it. Compose does not pass it, on purpose: the Compose kit always requires `HALOW_TOKEN`. |

Three settings exist only as command-line options of the `serve` command. Pass them after the image name with `docker run` (`... ghcr.io/clearobject/halow-alert:1.0.0 serve --quiet`), or as `command: ["serve", "--quiet"]` in Compose:

| Option | Default | Meaning |
|---|---|---|
| `--min-gap` | `0.5` | Minimum seconds between accepted alerts (the `429` rule). |
| `--burst-interval-ms` | `100` | Milliseconds between the copies of a burst (at least 20). |
| `--quiet` | off | No per-request, per-ACK and per-heartbeat lines in the container output. |

When the settings are invalid, the service does not start and exits with code 2. The container output says why, for example `refusing to serve on 0.0.0.0 without a token` or `--http / HALOW_HTTP must be 'host:port' or 'port', with a port from 1 to 65535`.
