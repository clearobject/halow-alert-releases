#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ClearObject
"""halow_client.py - raise ClearZone wearable alerts from your own software.

Python 3.8+, standard library only. Import it as a module, or run it as a command. Copy this
one file into your project, or install it as the package clearobject-halow-client, which
also installs the command halow-client. Documentation:
https://github.com/clearobject/halow-alert-releases/blob/main/integrations/python/README.md

Two ways to raise an alert:

  Gateway (HTTP)  Call the halow-alert service on the Linux gateway host that has the
                  HaLowLink attached: POST /alert with the gateway's bearer token.
                  Works from any machine that can reach the gateway.
  Direct (UDP)    From a machine that is itself plugged into the HaLowLink (address
                  192.168.12.50), send the alert datagram to the wearables yourself with
                  UDP multicast, and optionally collect their replies on TCP 5001.

An alert makes every wearable on the HaLowLink network vibrate and sound. Warn the
people nearby first.

Command line (installed: halow-client <command>, or python3 -m halow_client <command>):
  python3 halow_client.py health                                   gateway status
  python3 halow_client.py wearables                                wearables the gateway has heard from
  python3 halow_client.py alert --label "zone-3"                   alert through the gateway
  python3 halow_client.py direct-heartbeat --egress 192.168.12.50  ask wearables to report in (no buzz)
  python3 halow_client.py direct-alert --egress 192.168.12.50      alert directly, wait for ACKs
  python3 halow_client.py selftest [VECTORS]                       check the codec against VECTORS, by default
                                                                   ../testdata/vectors.json (in the repository and kit)

The command takes its settings from flags or the environment: HALOW_URL (default
http://127.0.0.1:8080), HALOW_TOKEN, HALOW_EGRESS (default 192.168.12.50), HALOW_GROUP
(239.255.0.10), HALOW_PORT (5000), HALOW_RESPONSE_PORT (5001).

As a library:
  from halow_client import GatewayClient, HalowError, send_direct, ResponseListener
  GatewayClient().alert(label="zone-3")            # HALOW_URL and HALOW_TOKEN from the environment
  send_direct("192.168.12.50")                     # direct mode, raw8
The library reads only HALOW_URL, HALOW_TOKEN and HALOW_EGRESS, and only for arguments you
leave out. The group, the port and the reply port are arguments (defaults above).
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import http.client
import json
import math
import os
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request

__version__ = "1.0.0"

DEFAULT_URL = "http://127.0.0.1:8080"
DEFAULT_EGRESS = "192.168.12.50"
GROUP = "239.255.0.10"
PORT = 5000
RESPONSE_PORT = 5001
TTL = 4
FORMATS = ("raw8", "proto", "both")
CMD_ALERT, CMD_HEARTBEAT_REQ = 1, 2
RSP_NAMES = {0: "UNKNOWN", 1: "ACK", 2: "HEARTBEAT"}
MAX_BURST_S, MIN_INTERVAL_S = 5.0, 0.02
U32, U64 = 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF
# Wearable replies: one DeviceResponse per TCP connection, read for at most this long and
# this many bytes, and at most this many connections at once (as the gateway does).
RESPONSE_DEADLINE_S, MAX_RESPONSE_BYTES, MAX_RESPONSE_CONNS = 5.0, 8192, 32


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _env_port(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    if not (raw.isdigit() and 1 <= int(raw) <= 65535):
        raise ValueError(f"{name}={raw!r} is not a port number (1-65535)")
    return int(raw)


# --------------------------------------------------------------------------- #
# Wire formats (byte-identical to the gateway's halow_alert.py; see docs/PROTOCOL.md)
# --------------------------------------------------------------------------- #
def _varint(n: int) -> bytes:
    if n < 0:      # a negative number would never end the loop below
        raise ValueError("varint must not be negative")
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def encode_raw8(t: float) -> bytes:
    """The raw8 alert: Unix time in seconds as a little-endian IEEE-754 double (8 bytes)."""
    return struct.pack("<d", t)


def encode_gateway_message(timestamp_ms: int, command: int) -> bytes:
    """protobuf GatewayMessage {uint64 timestamp = 1; CommandCode command = 2}; zero fields are left out."""
    out = b""
    if timestamp_ms:
        out += b"\x08" + _varint(timestamp_ms)
    if command:
        out += b"\x10" + _varint(command)
    return out


def _read_varint(buf: bytes, i: int):
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


def _fields(buf: bytes) -> dict:
    """{field number: int (varint) or bytes (other wire types)}; ValueError when malformed."""
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
                raise ValueError(f"field {tag} runs past the end of the message")
            v, i = buf[i:i + ln], i + ln
        else:
            raise ValueError(f"wire type {wire}")
        out[tag] = v
    return out


def _get(f: dict, tag: int, typ: type, default):
    v = f.get(tag, default)
    if not isinstance(v, typ):
        raise ValueError(f"field {tag}: wrong wire type")
    return v


def _int32(v: int) -> int:
    v &= U32
    return v - (1 << 32) if v >= 1 << 31 else v


def decode_device_response(buf: bytes) -> dict:
    """A wearable reply, protobuf DeviceResponse {timestamp=1, code=2, ack_payload=3,
    heartbeat_payload=4}, as a dict. Raises ValueError when the bytes are malformed."""
    f = _fields(buf)
    code = _get(f, 2, int, 0)
    r = {"timestamp_ms": _get(f, 1, int, 0) & U64, "code": RSP_NAMES.get(code, str(code))}
    if 3 in f:
        r["ack_success"] = bool(_get(_fields(_get(f, 3, bytes, b"")), 1, int, 0))
    if 4 in f:
        h = _fields(_get(f, 4, bytes, b""))
        r["heartbeat"] = {
            "device_id": _get(h, 1, bytes, b"").decode("utf-8", "replace"),
            "battery_percentage": _int32(_get(h, 2, int, 0)),
            "rssi": _int32(_get(h, 3, int, 0)),
            "uptime_seconds": _get(h, 4, int, 0) & U32,
            "app_state": _get(h, 5, int, 0) & U32,
            "device_state": _get(h, 6, int, 0) & U32,
        }
    return r


# --------------------------------------------------------------------------- #
# Gateway mode: the halow-alert HTTP API
# --------------------------------------------------------------------------- #
class HalowError(Exception):
    """A failed call. `status` is the HTTP status (None when the gateway was not reached),
    `body` the decoded JSON body (or {})."""

    def __init__(self, message: str, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body if body is not None else {}


_STATUS_HINTS = {
    400: "the gateway rejected the request",
    401: "missing or wrong token: set HALOW_TOKEN to the gateway's token",
    404: "unknown path: is HALOW_URL the halow-alert gateway (http://<host>:8080)?",
    413: "request body too large",
    429: "rate limited: the gateway sends at most one alert every 0.5 s; retry shortly",
    500: "the gateway could not send the alert (HaLowLink unplugged or not set up? see GET /health)",
    503: "the gateway cannot send right now (no HaLowLink address on the gateway host, "
         "or its reply listener stopped; see GET /health)",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The gateway never redirects. A 3xx answer is an error: following it would turn
    POST /alert into a GET and carry the token to another address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GatewayClient:
    """Client for the halow-alert service: POST /alert, GET /wearables, GET /health.

    url defaults to HALOW_URL (else http://127.0.0.1:8080); token to HALOW_TOKEN.
    Proxy settings from the environment are ignored: the gateway is on your local network.
    Redirects are not followed."""

    def __init__(self, url=None, token=None, timeout: float = 10.0):
        self.url = (url or _env("HALOW_URL", DEFAULT_URL)).rstrip("/")
        if not self.url.startswith(("http://", "https://")):
            raise ValueError(f"gateway URL must start with http:// or https://, not {self.url!r}")
        self.token = (os.environ.get("HALOW_TOKEN", "") if token is None else token).strip()
        self.timeout = float(timeout)
        if not (math.isfinite(self.timeout) and self.timeout > 0):
            raise ValueError("timeout must be a number of seconds above 0")
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _call(self, method: str, path: str, body=None, ok=(200,), timeout=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if method == "POST" and self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = e.read()
            except (OSError, http.client.HTTPException):
                raw = b""
        except urllib.error.URLError as e:
            raise HalowError(f"cannot reach the gateway at {self.url}: {e.reason}") from None
        except (OSError, ValueError) as e:      # timeout, reset, bad URL
            raise HalowError(f"cannot reach the gateway at {self.url}: {e}") from None
        except http.client.HTTPException as e:  # not an HTTP answer, cut short, bad port in the URL
            raise HalowError(f"cannot reach the gateway at {self.url}: {type(e).__name__}: "
                             f"{str(e).strip()[:200]}") from None
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (ValueError, RecursionError):    # not JSON, or nested too deeply to decode
            parsed = {"error": raw.decode("utf-8", "replace")[:500]}
        if not isinstance(parsed, dict):
            parsed = {"value": parsed}
        if status in ok:
            return parsed
        hint = _STATUS_HINTS.get(status, "unexpected response")
        detail = parsed.get("error")
        raise HalowError(f"{method} {path}: HTTP {status}: {hint}" + (f" ({detail})" if detail else ""),
                         status, parsed)

    def health(self) -> dict:
        """GET /health. Returns the body for 200 and for 503; check its "ok" field."""
        return self._call("GET", "/health", ok=(200, 503))

    def wearables(self) -> dict:
        """GET /wearables: {"devices": [...], "recent": [...]}."""
        return self._call("GET", "/wearables")

    def alert(self, label=None, fmt=None, burst_seconds=None) -> dict:
        """POST /alert. Every wearable on the HaLowLink network alerts. Returns the gateway's
        send record. Raises HalowError (status 401, 429, 500, ...) when it fails."""
        body = {}
        if label is not None:
            body["label"] = str(label)
        if fmt is not None:
            if str(fmt).lower() not in FORMATS:
                raise ValueError("fmt must be raw8, proto or both")
            body["format"] = str(fmt).lower()
        wait = self.timeout
        if burst_seconds is not None:
            b = float(burst_seconds)
            if not math.isfinite(b) or b < 0:
                raise ValueError("burst_seconds must be a number from 0 to 5")
            body["burst_seconds"] = b
            wait += min(b, MAX_BURST_S)      # the gateway answers when the burst ends
        return self._call("POST", "/alert", body, timeout=wait)


def total_acks(wearables: dict) -> int:
    """The sum of ACKs over every wearable in a GET /wearables body."""
    return sum(int(d.get("acks", 0)) for d in wearables.get("devices", []))


# --------------------------------------------------------------------------- #
# Direct mode: UDP multicast to the wearables, replies on TCP
# --------------------------------------------------------------------------- #
def _check_ipv4(addr: str) -> str:
    try:
        socket.inet_aton(addr)
    except OSError:
        addr = ""
    if addr.count(".") != 3:
        raise ValueError("the egress address must be an IPv4 address of this machine, "
                         "for example 192.168.12.50")
    return addr


def _mcast_socket(egress: str, ttl: int = TTL) -> socket.socket:
    """A UDP socket that sends multicast from `egress` (IP_MULTICAST_IF)."""
    _check_ipv4(egress)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.bind((egress, 0))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(egress))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    except OSError as e:
        s.close()
        raise OSError(f"cannot send from {egress}: {e.strerror or e}. Is it an address of this machine "
                      f"(the HaLowLink plugged in and the address set)?") from None
    return s


def send_direct(egress: str = "", fmt: str = "raw8", group: str = GROUP, port: int = PORT,
                ttl: int = TTL, burst_seconds: float = 0.0, burst_interval_ms: int = 100) -> dict:
    """Send one alert straight to the wearables. Every wearable on the network alerts.

    egress: this machine's address on the HaLowLink (default HALOW_EGRESS, else 192.168.12.50).
    burst_seconds > 0 resends the same bytes every burst_interval_ms for that long (max 5 s).
    Returns a record like the gateway's. Does not wait for ACKs (see ResponseListener)."""
    egress = egress or _env("HALOW_EGRESS", DEFAULT_EGRESS)
    fmt = (fmt or "raw8").lower()
    if fmt not in FORMATS:
        raise ValueError("fmt must be raw8, proto or both")
    # Checked before anything is sent: a bad burst argument must not fail after the alert went out.
    # burst_interval_ms is used, and so checked, only when there is a burst (NaN is no burst).
    burst = min(max(float(burst_seconds), 0.0), MAX_BURST_S)
    interval = max(MIN_INTERVAL_S, burst_interval_ms / 1000.0) if burst > 0 else MIN_INTERVAL_S
    s = _mcast_socket(egress, ttl)
    now = time.time()
    start = time.monotonic()
    payloads = []
    if fmt in ("raw8", "both"):
        payloads.append(encode_raw8(now))
    if fmt in ("proto", "both"):
        payloads.append(encode_gateway_message(int(now * 1000), CMD_ALERT))
    sent = 0
    try:
        for p in payloads:
            s.sendto(p, (group, port))
            sent += 1
        if burst > 0:
            end = start + burst
            while time.monotonic() + interval < end:
                time.sleep(interval)
                for p in payloads:
                    s.sendto(p, (group, port))
                    sent += 1
    finally:
        s.close()
    return {"event": "alert_sent", "ts_ms": int(now * 1000), "format": fmt,
            "group": f"{group}:{port}", "egress": egress, "datagrams": sent}


def send_heartbeat_request(egress: str = "", group: str = GROUP, port: int = PORT, ttl: int = TTL) -> int:
    """Ask every wearable to report in (battery, RSSI); they do not buzz. Returns the request's
    timestamp in ms, which the wearables echo back in their heartbeat."""
    egress = egress or _env("HALOW_EGRESS", DEFAULT_EGRESS)
    ts = int(time.time() * 1000)
    s = _mcast_socket(egress, ttl)
    try:
        s.sendto(encode_gateway_message(ts, CMD_HEARTBEAT_REQ), (group, port))
    finally:
        s.close()
    return ts


def subnet_prefix(addr: str) -> str:
    """'192.168.12.' for 192.168.12.50: replies are accepted from this /24 only."""
    return addr.rsplit(".", 1)[0] + "."


def _exclusive_port(s: socket.socket) -> bool:
    """Own the reply port: SO_EXCLUSIVEADDRUSE on Windows (SO_REUSEADDR there would let a
    second process share it); SO_REUSEADDR on POSIX, which only allows a quick rebind.
    True when the port is owned exclusively (Windows)."""
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        return True
    if os.name == "posix":
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return False


class ResponseListener:
    """Collects wearable replies on TCP (port 5001): each connection carries one
    DeviceResponse, an ACK or a HEARTBEAT. Use as a context manager:

        with ResponseListener(allow_prefix="192.168.12.") as rl:
            send_direct("192.168.12.50")
            ack = rl.wait_for("ACK", timeout=15)

    `responses` holds the last 1000 decoded replies, with "ip" (the wearable) and "received_ms" added.
    on_response(reply) is called from a background thread. Only one program on a machine
    can listen on the port: stop the gateway service there first."""

    def __init__(self, port: int = RESPONSE_PORT, bind: str = "0.0.0.0", allow_prefix: str = "",
                 on_response=None):
        self.port, self.bind, self.allow_prefix = port, bind, allow_prefix
        self.on_response = on_response
        self.responses: list = []
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(MAX_RESPONSE_CONNS)
        self._srv = None
        self._thread = None

    def start(self) -> ResponseListener:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        exclusive = False
        try:
            exclusive = _exclusive_port(srv)
            srv.bind((self.bind, self.port))
            srv.listen(32)
        except OSError as e:
            srv.close()
            hint = "is the gateway service or another listener running on this machine?"
            if exclusive:
                hint += (" On Windows the port can also stay taken for up to a few minutes after a listener "
                         "on it stopped, until the reply connections it had have fully closed")
            raise OSError(f"cannot listen on TCP {self.port} for wearable replies: {e.strerror or e} "
                          f"({hint})") from None
        srv.settimeout(0.2)              # how long stop() may wait for the accept loop to notice
        self._srv = srv
        self._stop.clear()               # a stopped listener can be started again
        self._thread = threading.Thread(target=self._loop, daemon=True, name="halow-replies")
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop listening. Replies that are already being read are still read.

        On Linux and macOS the port is free to listen on again when this returns. On Windows
        it can stay taken until the reply connections that were open at stop() have ended,
        and, after one that the listener cut off itself (at the deadline or a limit), for up
        to a few minutes more."""
        self._stop.set()
        if self._srv is not None:
            with contextlib.suppress(OSError):
                self._srv.shutdown(socket.SHUT_RDWR)   # wakes a waiting accept on Linux
            self._srv.close()
        # The accept loop may still be waiting on the socket, which keeps the port taken
        # until it returns (at most the accept timeout above).
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._srv.accept()
            except socket.timeout:
                continue
            except (ConnectionResetError, ConnectionAbortedError):
                # A client that went away before it was accepted (Windows reports these). Only
                # that connection is lost: take the next one at once.
                continue
            except OSError as e:
                if self._stop.is_set():
                    return
                if e.errno == errno.EPROTO:     # Linux: a protocol error on that one connection
                    continue
                time.sleep(0.5)     # the listener itself is in trouble (out of file descriptors, ...)
                continue
            if self.allow_prefix and not addr[0].startswith(self.allow_prefix):
                conn.close()
                continue
            if not self._slots.acquire(blocking=False):
                conn.close()
                continue
            try:
                threading.Thread(target=self._handle, args=(conn, addr[0]), daemon=True).start()
            except RuntimeError:
                self._slots.release()
                conn.close()

    def _handle(self, conn, ip):
        try:
            deadline = time.monotonic() + RESPONSE_DEADLINE_S
            buf = b""
            with conn:
                try:
                    while len(buf) < MAX_RESPONSE_BYTES:
                        left = deadline - time.monotonic()
                        if left <= 0:
                            break
                        conn.settimeout(max(0.05, left))
                        chunk = conn.recv(MAX_RESPONSE_BYTES - len(buf))
                        if not chunk:
                            break
                        buf += chunk
                except OSError:          # timeout or reset: decode what arrived
                    pass
            if not buf:
                return
            try:
                resp = decode_device_response(buf)
            except ValueError:
                return
            if resp["code"] not in ("ACK", "HEARTBEAT"):
                return
            resp["ip"] = ip
            resp["received_ms"] = int(time.time() * 1000)
            with self._cond:
                self.responses.append(resp)
                del self.responses[:-1000]
                self._cond.notify_all()
            if self.on_response:
                try:
                    self.on_response(resp)
                except Exception as e:   # a callback error must not stop the listener
                    print(f"halow_client: on_response failed: {e}", file=sys.stderr)
        finally:
            self._slots.release()

    def wait_for(self, code: str = "ACK", timeout: float = 10.0, after_ms: int = 0):
        """The first reply with this code ("ACK" or "HEARTBEAT") received at or after
        after_ms (Unix ms), waiting up to `timeout` seconds; None if none came."""
        def find():
            for r in self.responses:
                if r["code"] == code and r["received_ms"] >= after_ms:
                    return r
            return None
        with self._cond:
            self._cond.wait_for(lambda: find() is not None, timeout)
            return find()


# --------------------------------------------------------------------------- #
# Self-test against the shared vectors
# --------------------------------------------------------------------------- #
def _norm(v):
    """Decoded values with integers as strings, as testdata/vectors.json writes them."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return str(v)
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    return v


def selftest(path: str = "") -> int:
    """Check the codec against testdata/vectors.json (by default the copy next to this file's
    folder, as in the repository and the kit). Returns 0 when every vector matches."""
    if not path:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "testdata", "vectors.json")
        if not os.path.isfile(path):
            raise FileNotFoundError("no testdata/vectors.json in the folder above this file (a copied file or the "
                                    "installed package does not have it); pass its path, for example: "
                                    "selftest path/to/integrations/testdata/vectors.json")
    with open(path, encoding="utf-8") as f:
        vec = json.load(f)
    bad = 0
    for c in vec["raw8"]:
        if encode_raw8(c["seconds"]).hex() != c["hex"]:
            bad += 1
            print(f"raw8 {c['seconds']!r}: got {encode_raw8(c['seconds']).hex()}, want {c['hex']}")
    for c in vec["gateway_message"]:
        got = encode_gateway_message(int(c["timestamp_ms"]), int(c["command"])).hex()
        if got != c["hex"]:
            bad += 1
            print(f"gateway_message {c['timestamp_ms']}/{c['command']}: got {got}, want {c['hex']}")
    for c in vec["device_response"]:
        try:
            got = _norm(decode_device_response(bytes.fromhex(c["hex"])))
        except ValueError:
            got = None
        if got != c.get("result"):
            bad += 1
            print(f"device_response {c['hex']}: got {got}, want {c.get('result')}")
    n = len(vec["raw8"]) + len(vec["gateway_message"]) + len(vec["device_response"])
    print(f"selftest: {n - bad}/{n} vectors match")
    return 0 if bad == 0 else 1


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def _describe(r: dict) -> str:
    hb = r.get("heartbeat")
    if hb:
        dev = "".join(ch if ch.isprintable() else "?" for ch in hb["device_id"])
        return (f"HEARTBEAT from {r['ip']}: device_id={dev} battery={hb['battery_percentage']}% "
                f"rssi={hb['rssi']} dBm uptime={hb['uptime_seconds']} s")
    return f"{r['code']} from {r['ip']}: wearable timestamp {r['timestamp_ms']} ms"


def _port_arg(v) -> int:
    if not (v.isdigit() and 1 <= int(v) <= 65535):
        raise argparse.ArgumentTypeError(f"{v!r} is not a port number (1-65535)")
    return int(v)


def _nonneg(v) -> float:
    try:
        f = float(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{v!r} is not a number") from None
    if not math.isfinite(f) or f < 0:
        raise argparse.ArgumentTypeError(f"{v!r} must be 0 or more")
    return f


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Raise ClearZone wearable alerts through the halow-alert "
                                            "gateway (HTTP) or directly (UDP multicast).")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def gateway(sp):
        sp.add_argument("--url", default=None, help="gateway URL (default: HALOW_URL, else %s)" % DEFAULT_URL)
        sp.add_argument("--timeout", type=_nonneg, default=10.0, help="seconds (default 10)")

    def direct(sp):
        sp.add_argument("--egress", default=None,
                        help="this machine's address on the HaLowLink (default: HALOW_EGRESS, else %s)"
                             % DEFAULT_EGRESS)
        sp.add_argument("--group", default=None, help="multicast group (default: HALOW_GROUP, else %s)" % GROUP)
        sp.add_argument("--port", type=_port_arg, default=None, help="default: HALOW_PORT, else %d" % PORT)
        sp.add_argument("--response-port", type=_port_arg, default=None,
                        help="TCP port for replies (default: HALOW_RESPONSE_PORT, else %d)" % RESPONSE_PORT)

    gateway(sub.add_parser("health", help="GET /health"))
    gateway(sub.add_parser("wearables", help="GET /wearables"))
    s = sub.add_parser("alert", help="POST /alert (every wearable alerts)")
    gateway(s)
    s.add_argument("--label", default=None, help="recorded in the gateway's log")
    s.add_argument("--format", default=None, choices=FORMATS)
    s.add_argument("--burst-seconds", type=_nonneg, default=None, help="repeat the alert for this long (max 5)")

    s = sub.add_parser("direct-alert", help="send an alert straight to the wearables and wait for ACKs")
    direct(s)
    s.add_argument("--format", default="raw8", choices=FORMATS)
    s.add_argument("--burst-seconds", type=_nonneg, default=0.0)
    s.add_argument("--wait-acks", type=_nonneg, default=5.0,
                   help="seconds to collect ACKs (default 5; 0 = don't listen)")
    s = sub.add_parser("direct-heartbeat", help="ask the wearables to report in (no buzz)")
    direct(s)
    s.add_argument("--seconds", type=_nonneg, default=5.0, help="seconds to collect heartbeats (default 5)")

    s = sub.add_parser("selftest", help="check the codec against testdata/vectors.json")
    s.add_argument("vectors", nargs="?", default="")

    a = p.parse_args(argv)
    try:
        if a.cmd == "selftest":
            return selftest(a.vectors)
        if a.cmd in ("health", "wearables", "alert"):
            c = GatewayClient(a.url, timeout=a.timeout or 10.0)
            if a.cmd == "health":
                body = c.health()
                print(json.dumps(body, indent=1))
                return 0 if body.get("ok") else 1
            if a.cmd == "wearables":
                print(json.dumps(c.wearables(), indent=1))
                return 0
            print(json.dumps(c.alert(a.label, a.format, a.burst_seconds), indent=1))
            return 0
        egress = a.egress or _env("HALOW_EGRESS", DEFAULT_EGRESS)
        group = a.group or _env("HALOW_GROUP", GROUP)
        port = a.port or _env_port("HALOW_PORT", PORT)
        rport = a.response_port or _env_port("HALOW_RESPONSE_PORT", RESPONSE_PORT)
        listen_s = a.wait_acks if a.cmd == "direct-alert" else a.seconds
        rl = None
        if listen_s > 0:
            rl = ResponseListener(rport, allow_prefix=subnet_prefix(egress),
                                  on_response=lambda r: print("  " + _describe(r), flush=True)).start()
        try:
            if a.cmd == "direct-alert":
                rec = send_direct(egress, a.format, group, port, burst_seconds=a.burst_seconds)
                print(f"alert sent: {rec['format']} -> {rec['group']} from {rec['egress']} "
                      f"({rec['datagrams']} datagram(s))", flush=True)
            else:
                send_heartbeat_request(egress, group, port)
                print(f"heartbeat request sent -> {group}:{port} from {egress}", flush=True)
            if rl:
                print(f"listening {listen_s:g} s for replies on TCP {rport} ...", flush=True)
                time.sleep(listen_s)
                want = "ACK" if a.cmd == "direct-alert" else "HEARTBEAT"
                n = sum(1 for r in rl.responses if r["code"] == want)
                print(f"{n} {want}(s) from {len({r['ip'] for r in rl.responses if r['code'] == want})} wearable(s)")
        finally:
            if rl:
                rl.stop()
        return 0
    except HalowError as e:
        print(f"halow_client: {e}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        print(f"halow_client: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
