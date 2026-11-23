# Wire protocol

This is the reference for what goes over the network between the sender (the halow-alert gateway, or your own program in direct mode) and the ClearZone wearables. You need it if you implement direct mode, write your own receiver for the wearables' replies, or want to know exactly what the gateway does. For normal use, the [HTTP API](HTTP-API.md) is all you need.

Each section says where its facts come from. The [last section](#verified-vs-inferred) separates what has been seen working on real hardware from what is inferred from code and simulators.

## Contents

- [Addresses and ports](#addresses-and-ports)
- [Alert, `raw8` format (default)](#alert-raw8-format-default)
- [Alert and heartbeat request, `proto` format](#alert-and-heartbeat-request-proto-format)
- [Wearable replies: `DeviceResponse`](#wearable-replies-deviceresponse)
- [Timing of ACKs](#timing-of-acks)
- [Duplicates](#duplicates)
- [Bursts](#bursts)
- [Rate limit](#rate-limit)
- [Multicast TTL](#multicast-ttl)
- [Heartbeat requests](#heartbeat-requests)
- [Implementing direct mode](#implementing-direct-mode)
- [Verified vs inferred](#verified-vs-inferred)

## Addresses and ports

| What | Value | Notes |
|---|---|---|
| HaLowLink 2 | `192.168.12.1/24` | Router with HaLow Access Point mode (the factory default). It runs the DHCP server for the subnet. Admin page at `https://192.168.12.1` (HTTPS only). |
| LAN port and USB-C port | the same `192.168.12.0/24` network | Per the HaLowLink 2 user guide's port table. The WAN port is a separate network and is not used. |
| Wearables | `192.168.12.x`, by DHCP | Each joins the HaLow network and listens for the alert group. |
| Sender | `192.168.12.50/24`, static, no gateway | The address ClearObject's own systems send from. Where the replies go is not yet proven; see [Verified vs inferred](#verified-vs-inferred). |
| Alert group | UDP multicast `239.255.0.10`, port `5000` | One group for alerts and heartbeat requests. |
| Wearable replies | TCP port `5001` on the sender | ACKs and heartbeats. |

halow-alert chooses the outgoing interface by setting `IP_MULTICAST_IF` to the sender address on every socket, so no multicast route is needed. The Linux setup script still adds `239.0.0.0/8` on the HaLowLink interface for other programs. Sending does not need an IGMP join. The HaLowLink bridges its LAN, USB-C and HaLow interfaces by default, which is what carries the multicast onto the radio; keep it bridged.

## Alert, `raw8` format (default)

Eight bytes: the Unix time in seconds as an IEEE-754 binary64 (`double`), **little-endian**. Nothing else: no header, no length.

| Offset | Size | Content |
|---|---|---|
| 0 | 8 | `double`, little-endian: seconds since 1970-01-01T00:00:00Z, with a fractional part |

Example. 2026-01-01T00:00:00.123Z is `1767225600.123` s:

```
3b df 07 40 6e 55 da 41
```

How to produce it:

```python
struct.pack("<d", time.time())                                                          # Python
```

This is the format the ClearObject Workzone system sends. halow-alert always writes it little-endian, whatever the machine's byte order.

Source: the ClearObject Workzone alert sender and its test-alert tool.

## Alert and heartbeat request, `proto` format

A protobuf (proto3) `GatewayMessage`. The ClearObject operator app calls the same message `ComputeMessage`; the bytes are identical.

```proto
syntax = "proto3";

enum CommandCode {
  COMMAND_UNKNOWN = 0;
  COMMAND_ALERT = 1;
  COMMAND_HEARTBEAT_REQ = 2;
}

message GatewayMessage {
  uint64 timestamp = 1;      // milliseconds since the Unix epoch
  CommandCode command = 2;
}
```

| Field | Number | Type | Key byte | Value |
|---|---|---|---|---|
| `timestamp` | 1 | `uint64`, varint | `08` | Milliseconds since the Unix epoch. Wearables use it to drop duplicates (see [Duplicates](#duplicates)). |
| `command` | 2 | enum, varint | `10` | `1` = ALERT (the wearables alarm), `2` = HEARTBEAT_REQ (the wearables report in; no alarm). |

Following proto3, a field that is zero is left out.

Examples, both for 2026-01-01T00:00:00.123Z = `1767225600123` ms, as halow-alert encodes them:

```
ALERT          08 fb d0 ea b6 b7 33 10 01     (9 bytes)
HEARTBEAT_REQ  08 fb d0 ea b6 b7 33 10 02     (9 bytes)
               ── ───────────────── ── ──
               │  │                 │  └ command value (1 = ALERT, 2 = HEARTBEAT_REQ)
               │  │                 └ field 2, varint
               │  └ varint 1767225600123
               └ field 1, varint
```

The smallest valid ALERT, with `timestamp = 1`, is `08 01 10 01`. The operator app's own tests use that vector, and halow-alert's encoder produces the same bytes.

Format `both` sends one `raw8` datagram and then one `proto` ALERT, built from the same clock reading.

Sources: the operator app's HaLow protocol definitions and its test-alert command; a protocol proof-of-concept codec.

## Wearable replies: `DeviceResponse`

A wearable opens a TCP connection to port `5001`, writes one `DeviceResponse`, and closes the connection. There is no length prefix: read until the wearable closes. halow-alert and the operator app both stop reading after 5 s for the whole connection, or after 8 KiB, and decode what arrived even if the connection was reset.

halow-alert accepts connections only from the HaLowLink's `/24` (`192.168.12.x`), and logs others as `wearable_rejected`. It ignores a message that is malformed, has a field with the wrong wire type, or has a `code` other than ACK or HEARTBEAT, and prints one line about it. Unknown fields are skipped, as protobuf requires.

```proto
enum ResponseCode {
  RESPONSE_UNKNOWN = 0;
  RESPONSE_ACK = 1;
  RESPONSE_HEARTBEAT = 2;
}

message Ack {
  bool success = 1;
}

message HeartbeatResponse {
  string device_id = 1;          // at most 16 bytes on the wearable
  int32  battery_percentage = 2;
  int32  rssi = 3;               // dBm, negative
  uint32 uptime_seconds = 4;
  uint32 app_state = 5;
  uint32 device_state = 6;
}

message DeviceResponse {
  uint64 timestamp = 1;
  ResponseCode code = 2;
  Ack ack_payload = 3;                     // present on ACKs
  HeartbeatResponse heartbeat_payload = 4; // present on heartbeats
}
```

| Field | Number | Key byte | Meaning |
|---|---|---|---|
| `timestamp` | 1 | `08` | On an ACK: when the wearable **received** the alert, in ms by its own clock. On a heartbeat that answers a HEARTBEAT_REQ: the request's timestamp, echoed back, so `now - timestamp` is a round trip that needs no clock sync. |
| `code` | 2 | `10` | `1` = ACK, `2` = HEARTBEAT. |
| `ack_payload` | 3 | `1a` | `Ack { success }`. |
| `heartbeat_payload` | 4 | `22` | `HeartbeatResponse`. |

Notes:

- An ACK carries no `device_id`. Identify the wearable by the TCP peer address, and match that address to a heartbeat's `device_id`. `GET /wearables` is keyed by peer address for this reason.
- `battery_percentage` and `rssi` are `int32`. A negative value is encoded as a 10-byte varint (sign-extended to 64 bits), as proto3 requires. Keep the low 32 bits when decoding, as protobuf libraries do.
- `app_state` and `device_state` are numeric firmware states. halow-alert reports them as numbers and does not interpret them.
- A heartbeat with `timestamp = 0` is unsolicited (not an answer to a request). The operator app's wearable simulator sends those every 15 s; whether real wearables do is not yet known.

Example ACK: the wearable received the alert above 40 ms after it was sent.

```
08 a3 d1 ea b6 b7 33 10 01 1a 02 08 01
```

halow-alert decodes it as:

```json
{"timestamp_ms": 1767225600163, "code": "ACK", "ack_success": true}
```

Example heartbeat: an answer to the HEARTBEAT_REQ above, from a wearable with `device_id` `wz-07`, battery 87 %, RSSI -62 dBm, up for 3600 s, `app_state` 1, `device_state` 2.

```
08 fb d0 ea b6 b7 33                  timestamp = 1767225600123 (echo of the request)
10 02                                 code = HEARTBEAT
22 1b                                 heartbeat_payload, 27 bytes:
   0a 05 77 7a 2d 30 37                 device_id = "wz-07"
   10 57                                battery_percentage = 87
   18 c2 ff ff ff ff ff ff ff ff 01     rssi = -62
   20 90 1c                             uptime_seconds = 3600
   28 01                                app_state = 1
   30 02                                device_state = 2
```

halow-alert decodes it as:

```json
{"timestamp_ms": 1767225600123, "code": "HEARTBEAT",
 "heartbeat": {"device_id": "wz-07", "battery_percentage": 87, "rssi": -62,
               "uptime_seconds": 3600, "app_state": 1, "device_state": 2}}
```

Sources: the operator app's HaLow protocol definitions (including a hand-encoded heartbeat test, which halow-alert's decoder reproduces) and its HaLow monitor.

## Timing of ACKs

According to the operator app's HaLow monitor, a wearable sends its ACK only **after its alarm sequence finishes or is silenced**, while the ACK's `timestamp` records the moment it received the alert. So:

- An ACK can arrive several seconds after the buzz. The operator app's wearable simulator waits 1.5 to 5 s, and the monitor notes that a late silence can push it out further. Wait at least 30 s when measuring, or watch `GET /wearables` on the gateway.
- **ACK delay** (when the ACK arrives minus when the alert was sent) measures the alarm sequence plus the network.
- **Delivery latency** (the ACK's `timestamp` minus the alert's timestamp) measures the radio path, but only when the wearable's clock and the sender's clock agree. Whether the HaLowLink serves time (SNTP) to the wearables, and whether they use it, is not yet verified.

The gateway's [JSONL log](ARCHITECTURE.md#what-the-gateway-logs) has both numbers: `ts_ms` of the `alert_sent` line, and `ts_ms` (arrival) and `timestamp_ms` (the wearable's receive time) of each `wearable_ack` line.

## Duplicates

The wearables drop an alert they have already acted on, using its timestamp. The operator app's protocol definitions say so ("devices use this for deduplication"), and its monitor applies the same rule to its own records, treating a timestamp that is equal to or older than the last one as a duplicate.

What follows from that:

- For `proto` alerts, according to the operator app, repeating the identical datagram produces one alarm, not one per repeat. For `raw8` (the default burst format) this is not known yet.
- The sender's clock should move forward. An alert stamped earlier than the last one a wearable acted on may be ignored, for example after the sender's clock is set back.
- Whether this rule applies to `raw8` alerts, and how a `both` alert (one `raw8` plus one `proto` with the same instant, in seconds and in milliseconds) is treated, is **not known**. Hardware verification will record both.

## Bursts

`burst_seconds` in the `POST /alert` body (default from `HALOW_BURST_SECONDS`), or `--burst-seconds S` for the command-line `send`, makes the gateway:

1. send the alert datagram (two for `both`) once, then
2. resend the **same bytes** every 100 ms (`--burst-interval-ms`, at least 20 ms) until `S` seconds after the first send.

`S` is capped at 5 s, and timed on a clock that does not jump when the system time is corrected. A 1 s burst at 100 ms is 10 datagrams per format. It exists to ride out radio loss: one of the copies is enough, and if the wearable drops duplicates (documented for `proto`, unknown for `raw8`), it alarms once. `POST /alert` answers after the burst ends.

## Rate limit

The ClearObject Workzone system sends at most one alert every 0.5 s and drops alerts that come sooner. The gateway enforces the same gap (`--min-gap 0.5`) and answers `429`. The repeats inside one burst do not count, and neither do requests that fail. The gap is measured on a clock that does not jump when the system time is corrected. The command-line `send` does not enforce a gap; with `send -n`, `--interval` (default 1 s) sets the spacing. In direct mode, keep to one alert per 0.5 s yourself.

## Multicast TTL

halow-alert sets the multicast TTL to 4 (`HALOW_TTL`). The wearables are on the same link as the sender (the HaLowLink bridges them), so a TTL of 1 is enough and the exact value does not matter.

## Heartbeat requests

The gateway multicasts a HEARTBEAT_REQ when it starts and then every 15 s (`HALOW_HEARTBEAT`), which is also the interval the ClearObject operator app uses in production. The ClearObject Workzone system treats a wearable as offline after 60 s without a report. The command-line `wearables` sends a request every 5 s for `--seconds` (default 20). The command-line `send` sends none.

## Implementing direct mode

To drive the wearables without the gateway, your program has to do what the gateway does. The [integrations](../integrations/README.md) implement this for several languages.

**Sending (required):**

1. Give the machine `192.168.12.50/24` on the HaLowLink interface, with no gateway ([network setup](NETWORK.md)).
2. Open a UDP socket. Set `IP_MULTICAST_IF` to `192.168.12.50`, so the datagram leaves on the HaLowLink interface whatever the routing table says. Set `IP_MULTICAST_TTL` to 1 or more.
3. Send the 8-byte `raw8` payload (current time, little-endian `double`) to `239.255.0.10:5000`.
4. Keep to one alert per 0.5 s. Use a clock that moves forward.

**Receiving (optional):**

1. Listen on TCP `5001` on `0.0.0.0` or `192.168.12.50`, and allow it in the firewall.
2. For each connection, accept only peers in `192.168.12.0/24`. Read until the peer closes, for at most 5 s and 8 KiB.
3. Decode a `DeviceResponse` with any protobuf library from the schema above, or by hand: it is a handful of varint fields. Count `code = 1` as an ACK from the peer address, and `code = 2` as a heartbeat.
4. Only one program on the machine can listen on TCP 5001. Do not run the halow-alert container on the same machine at the same time.

**Heartbeats (recommended):** send a `proto` HEARTBEAT_REQ every 15 s, as the gateway does, to keep track of which wearables are reachable. Whether the wearables need these requests to stay armed is not yet known; ClearObject's own systems always send them.

**Stand-in wearable:** [`tools/fake_wearable.py`](../tools/fake_wearable.py) behaves like a wearable on this protocol, so you can test both directions on one machine without hardware ([how](../integrations/README.md#try-it-without-hardware)). The integrations' [`testdata/vectors.json`](../integrations/testdata/vectors.json) holds encoder and decoder test vectors.

## Verified vs inferred

| Claim | Status | Evidence |
|---|---|---|
| A `raw8` datagram to `239.255.0.10:5000` makes every wearable on the network alarm | **Verified** on hardware, in production | The ClearObject Workzone alert sender and its test-alert tool send exactly this. |
| Wearables answer `raw8` alerts with an ACK over TCP 5001 | **Verified** on hardware, in production | The operator app's HaLow monitor logs these ACKs for Workzone alerts. |
| The `proto` ALERT and HEARTBEAT_REQ formats | **In production use** by the operator app (its test alert and its heartbeat requests). Not yet sent from halow-alert to hardware | Operator app test-alert command and HaLow monitor. |
| `DeviceResponse` field numbers and types | **Verified** against the operator app's schema and its byte-level tests | Operator app HaLow protocol definitions. |
| USB-C and the LAN port are the same `192.168.12.0/24` network in Router mode | **Vendor documentation** | HaLowLink 2 user guide, port table. Not yet tested with halow-alert. |
| ACKs go to the **source address** of the alert datagram | **Inferred**, not proven on hardware | The protocol proof-of-concept emulator replies to the alert's source address, but its own comment says this is an assumption (the device specification says "the Gateway's IP"). The operator app's wearable simulator replies to a configured host, not to the source. The operator app's monitor says wearables reply to "the compute IP", so a fixed reply address cannot be ruled out. |
| A wearable ACKs only after its alarm sequence ends | **Documented** in the operator app, not measured with halow-alert | Operator app HaLow monitor and wearable simulator. |
| Wearables drop duplicate `proto` alerts by timestamp | **Documented** in the operator app, not tested with halow-alert | Operator app protocol definitions and monitor. |
| Duplicate handling for `raw8`, and for `both` | **Unknown** | To be measured during hardware verification. |
| Wearables stay armed without periodic heartbeat requests | **Unknown**. Production always sends them every 15 s | To be measured during hardware verification. |
| Wearables send unsolicited heartbeats, and to which address | **Unknown**. The operator app's wearable simulator does, every 15 s | To be measured during hardware verification. |
| Everything works with no internet | **By design** (local link only), not yet tested with halow-alert | To be confirmed during hardware verification. |

### Evidence sources

These are ClearObject sources, named by role:

- **ClearObject Workzone alert sender:** the production sender (`raw8`, 0.5 s debounce).
- **Workzone test-alert tool:** the command-line ring test (`raw8`).
- **Workzone configuration defaults:** the alert settings (group, ports, sender address, heartbeat interval, offline timeout).
- **Operator app HaLow protocol definitions, HaLow monitor and test-alert command:** the `proto` schema, ACK and heartbeat handling, and the test alert.
- **Operator app wearable simulator:** a software wearable used to test the operator app.
- **Protocol proof-of-concept:** an early gateway and wearable emulator for the ACK protocol.
- **Morse Micro HaLowLink 2 user guide:** modes, ports, factory reset.
