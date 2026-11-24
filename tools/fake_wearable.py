#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ClearObject
"""fake_wearable.py - a stand-in ClearZone wearable, for developing without hardware.

Python 3.8+, standard library only. A development aid: it follows the documented
message formats (docs/PROTOCOL.md), and does NOT prove that the real wearable firmware
behaves the same way.

It joins the alert group on one local interface and, for each datagram:
  ALERT (raw8 or proto)  opens TCP to the sender's address on the response port and
                         sends DeviceResponse{ACK}. Repeats of one alert (the same bytes
                         or the same proto timestamp, as in a burst) get one ACK.
  HEARTBEAT_REQ          sends DeviceResponse{HEARTBEAT} with the request's timestamp,
                         this --device-id, battery and RSSI.

Run it on loopback, with ports that nothing else uses:

    python3 tools/fake_wearable.py --iface 127.0.0.1 --group 239.255.77.2 --port 15010 --response-port 15011 --seconds 0

then point an integration's direct mode at it (HALOW_EGRESS=127.0.0.1 HALOW_GROUP=239.255.77.2
HALOW_PORT=15010 HALOW_RESPONSE_PORT=15011), or the gateway image (HALOW_IFACE=127.0.0.1
HALOW_ROUTER=127.0.0.1 and the same HALOW_GROUP, HALOW_PORT and HALOW_RESPONSE_PORT).
integrations/README.md, "Try it without hardware", has the commands for Linux, macOS and
Windows. On Linux, loopback multicast needs:
    sudo ip link set lo multicast on && sudo ip route add 239.0.0.0/8 dev lo

It prints "fake wearable ready: ..." once it has joined the group.
"""
import argparse
import socket
import struct
import sys
import threading
import time

GROUP, PORT, RESPONSE_PORT = "239.255.0.10", 5000, 5001
U64 = 0xFFFFFFFFFFFFFFFF


def _varint(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _read_varint(buf, i):
    shift = result = 0
    while True:
        if shift >= 64:
            raise ValueError("varint longer than 10 bytes")
        if i >= len(buf):
            raise ValueError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _fields(buf):
    """{field number: int (varint) or bytes}; ValueError when malformed."""
    out, i = {}, 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        tag, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _read_varint(buf, i)
        elif wire in (1, 2, 5):
            if wire == 2:
                ln, i = _read_varint(buf, i)
            else:
                ln = 8 if wire == 1 else 4
            if i + ln > len(buf):
                raise ValueError("field runs past the end of the message")
            v, i = buf[i:i + ln], i + ln
        else:
            raise ValueError(f"wire type {wire}")
        out[tag] = v
    return out


def enc_field(tag, v):
    """Varint field; proto3 leaves out zero values."""
    return bytes([tag << 3]) + _varint(v) if v else b""


def enc_len(tag, payload):
    """Length-delimited field; proto3 leaves out empty values."""
    return bytes([(tag << 3) | 2]) + _varint(len(payload)) + payload if payload else b""


def ack_message(now_ms):
    """DeviceResponse{timestamp = when the alert arrived, code = ACK, ack_payload = {success: true}}."""
    return enc_field(1, now_ms) + enc_field(2, 1) + enc_len(3, b"\x08\x01")


def heartbeat_message(request_ts_ms, dev, battery, rssi, uptime):
    """DeviceResponse{timestamp = the request's, code = HEARTBEAT, heartbeat_payload}."""
    hb = (enc_len(1, dev.encode("utf-8")) + enc_field(2, battery & U64)
          + enc_field(3, rssi & U64) + enc_field(4, uptime))
    return enc_field(1, request_ts_ms) + enc_field(2, 2) + enc_len(4, hb)


def classify(data):
    """("ALERT" | "HEARTBEAT_REQ" | None, dedupe key)."""
    if len(data) == 8:
        return "ALERT", data             # raw8: 8-byte little-endian double
    try:
        f = _fields(data)
    except ValueError:
        return None, None
    cmd, ts = f.get(2, 0), f.get(1, 0)
    if not isinstance(cmd, int) or not isinstance(ts, int):
        return None, None
    return {1: "ALERT", 2: "HEARTBEAT_REQ"}.get(cmd), ts


def _reply(host, port, msg, kind, delay):
    if delay > 0:
        time.sleep(delay)
    try:
        with socket.create_connection((host, port), timeout=2) as c:
            c.sendall(msg)
    except OSError as e:
        print(f"fake wearable: could not answer {kind} to {host}:{port}: {e}", flush=True)


def run(iface="127.0.0.1", group=GROUP, port=PORT, rport=RESPONSE_PORT, dev="fake-01",
        seconds=10.0, battery=87, rssi=-62, ack_delay=0.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    try:
        s.bind(("", port))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(iface)))
    except OSError as e:
        s.close()
        print(f"fake wearable: cannot join {group}:{port} on {iface}: {e}", file=sys.stderr, flush=True)
        return 2
    s.settimeout(0.5)
    started = time.monotonic()
    print(f"fake wearable ready: {dev} joined {group}:{port} on {iface}; responses to TCP {rport}"
          f"{'' if seconds <= 0 else f' for {seconds:g} s'}", flush=True)
    seen = set()
    try:
        while seconds <= 0 or time.monotonic() < started + seconds:
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            kind, key = classify(data)
            delay = 0.0
            if kind == "ALERT":
                if key in seen:
                    continue            # one ACK per alert, however many repeats arrive
                seen.add(key)
                msg = ack_message(int(time.time() * 1000))
                delay = ack_delay
            elif kind == "HEARTBEAT_REQ":
                msg = heartbeat_message(key, dev, battery, rssi, int(time.monotonic() - started))
            else:
                print(f"fake wearable: ignored {len(data)} bytes from {addr[0]}", flush=True)
                continue
            print(f"fake wearable: {kind} from {addr[0]} -> TCP {addr[0]}:{rport}", flush=True)
            threading.Thread(target=_reply, args=(addr[0], rport, msg, kind, delay), daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        s.close()
    return 0


def _port(v):
    if not (v.isdigit() and 1 <= int(v) <= 65535):
        raise argparse.ArgumentTypeError(f"{v!r} is not a port number (1-65535)")
    return int(v)


def _ipv4(v):
    try:
        socket.inet_aton(v)
    except OSError:
        v = ""
    if v.count(".") != 3:
        raise argparse.ArgumentTypeError("must be an IPv4 address of this machine, for example 127.0.0.1")
    return v


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Stand-in ClearZone wearable for development without hardware. It follows the "
                    "documented message formats; it does not prove the real wearable firmware.")
    p.add_argument("iface_pos", nargs="?", metavar="IFACE", type=_ipv4,
                   help="same as --iface")
    p.add_argument("--iface", type=_ipv4, default=None,
                   help="local IPv4 to join the group on (default 127.0.0.1)")
    p.add_argument("--group", type=_ipv4, default=GROUP, help=f"multicast group (default {GROUP})")
    p.add_argument("--port", type=_port, default=PORT, help=f"alert port (default {PORT})")
    p.add_argument("--response-port", type=_port, default=RESPONSE_PORT,
                   help=f"TCP port the replies go to, on the sender's address (default {RESPONSE_PORT})")
    p.add_argument("--device-id", default="fake-01", help="reported in heartbeats (default fake-01)")
    p.add_argument("--battery", type=int, default=87, help="battery percentage in heartbeats (default 87)")
    p.add_argument("--rssi", type=int, default=-62, help="RSSI in dBm in heartbeats (default -62)")
    p.add_argument("--ack-delay", type=float, default=0.0,
                   help="seconds to wait before each ACK (real wearables ACK after their alarm ends)")
    p.add_argument("--seconds", type=float, default=10.0, help="how long to run (default 10; 0 = until stopped)")
    a = p.parse_args(argv)
    if a.iface and a.iface_pos and a.iface != a.iface_pos:
        p.error("give the interface address once (--iface or positional), not two different ones")
    iface = a.iface or a.iface_pos or "127.0.0.1"
    return run(iface, a.group, a.port, a.response_port, a.device_id, a.seconds, a.battery, a.rssi,
               max(0.0, a.ack_delay))


if __name__ == "__main__":
    sys.exit(main())
