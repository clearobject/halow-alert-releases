# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ClearObject
"""Shared helpers for the halow_client tests in this folder. Not a test module itself.

The module under test (`hc`), the HTTP stub of the gateway and the environment for child
processes come from test_halow_client.py, so every test module uses the same halow_client:
the source file, or the installed package with HALOW_TEST_INSTALLED=1.

Everything here stays on 127.0.0.1. Multicast tests use a test group drawn from
239.255.100-254.1-254 (never the real 239.255.0.10) and ports the operating system hands out
(never 5000, 5001 or 8080).
"""
import atexit
import contextlib
import importlib.util
import io
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from unittest import mock

import test_halow_client as base
from test_halow_client import (
    FAKE_WEARABLE,
    INSTALLED,
    LOOPBACK,
    MULTICAST_HINT,
    REPO,
    REQUIRE_MULTICAST,
    SOURCE_DIR,
    TOKEN,
    VECTORS,
    _child_env,
    _commands,
    _default_routes,
    _ScriptedClient,
    _StubCase,
    hc,
)

__all__ = [
    "FAKE_WEARABLE", "INSTALLED", "LOOPBACK", "MULTICAST_HINT", "REPO", "REQUIRE_MULTICAST", "SOURCE_DIR",
    "TOKEN", "VECTORS", "U32", "U64", "SEED", "base", "hc", "_child_env", "_commands", "_default_routes",
    "_ScriptedClient", "_StubCase", "CleanEnv", "FakeWearable", "MulticastMixin", "RawServer", "call_main",
    "closed_port", "device_response", "empty_cwd", "free_port", "heartbeat_payload", "http_response",
    "load_fake_wearable", "load_module", "pb_bytes", "pb_key", "pb_uint", "pb_varint", "rng", "run",
    "pick_group", "send_tcp", "wait_until",
]

U32, U64 = 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF
SEED = 20261122            # every seeded draw (fuzz cases, test groups) is the same on every run


def rng(salt: str) -> random.Random:
    """A random generator seeded from SEED and `salt` (a string seed is hashed the same way on
    every platform and Python version)."""
    return random.Random(f"{SEED}:{salt}")


def pick_group(salt: str) -> str:
    """A test multicast group in 239.255.100-254.1-254: never the real group 239.255.0.10."""
    r = rng("group:" + salt)
    return "239.255.%d.%d" % (r.randint(100, 254), r.randint(1, 254))


def free_port() -> int:
    """A port that is free on 127.0.0.1 for both TCP and UDP right now.

    Candidates come from 20000-48999, below the OS temporary (ephemeral) range: on Windows a
    UDP bind to a port the OS just handed out for TCP can be refused, and Hyper-V reserves
    blocks inside that range, which made the old "bind port 0, then reuse it" approach run
    out of candidates.
    """
    r = random.Random()
    for _ in range(200):
        port = r.randint(20000, 48999)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as t, \
                    socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
                t.bind((LOOPBACK, port))
                u.bind((LOOPBACK, port))
        except OSError:
            continue
        return port
    raise RuntimeError("no free port on 127.0.0.1")


def closed_port() -> int:
    """A TCP port on 127.0.0.1 that nothing listens on."""
    return free_port()


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """Poll `predicate` until it is true or `timeout` seconds pass; returns its last answer."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return bool(predicate())
        time.sleep(interval)


_cwd = []


def empty_cwd() -> str:
    """An empty folder for child processes, so that "python -m halow_client" cannot pick up a
    halow_client.py from the current folder."""
    if not _cwd:
        d = tempfile.mkdtemp(prefix="halow-client-test-")
        atexit.register(shutil.rmtree, d, True)
        _cwd.append(d)
    return _cwd[0]


def run(argv, timeout: float = 60, **env):
    """Run a command in an empty folder, with no HALOW_* settings but `env`."""
    return subprocess.run(argv, cwd=empty_cwd(), env=_child_env(**env), capture_output=True, text=True,
                          timeout=timeout)


def call_main(argv):
    """hc.main(argv) in this process: (exit code, stdout, stderr). argparse errors give 2."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = hc.main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


def load_module(path: str, name: str):
    """Import a script as a module. Scripts put their own folder first on sys.path to find
    halow_client; this undoes that, so the module under test stays the one imported above."""
    saved = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved
    return module


def load_fake_wearable():
    """tools/fake_wearable.py as a module."""
    return load_module(FAKE_WEARABLE, "fake_wearable_under_test")


class CleanEnv:
    """Mixin: no HALOW_* settings and no proxy settings from the caller during each test."""

    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("HALOW_") and k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)


# --------------------------------------------------------------------------- #
# Protobuf, written independently of the client's own encoder
# --------------------------------------------------------------------------- #
def pb_varint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    out.append(n)
    return bytes(out)


def pb_key(tag: int, wire: int) -> bytes:
    return pb_varint((tag << 3) | wire)


def pb_uint(tag: int, n: int) -> bytes:
    return pb_key(tag, 0) + pb_varint(n)


def pb_bytes(tag: int, payload: bytes) -> bytes:
    return pb_key(tag, 2) + pb_varint(len(payload)) + payload


def heartbeat_payload(device_id="wz-07", battery=87, rssi=-62, uptime=3615, app_state=1, device_state=2) -> bytes:
    return (pb_bytes(1, device_id.encode("utf-8")) + pb_uint(2, battery & U64) + pb_uint(3, rssi & U64)
            + pb_uint(4, uptime) + pb_uint(5, app_state) + pb_uint(6, device_state))


def device_response(ts=1767225600123, code=1, ack=True, heartbeat=None) -> bytes:
    """DeviceResponse bytes: an ACK by default; code=2 with heartbeat=heartbeat_payload(...)
    for a heartbeat."""
    out = pb_uint(1, ts) + pb_uint(2, code)
    if ack is not None and code == 1:
        out += pb_bytes(3, pb_uint(1, 1 if ack else 0))
    if heartbeat is not None:
        out += pb_bytes(4, heartbeat)
    return out


def send_tcp(port: int, data: bytes, host: str = LOOPBACK) -> None:
    """One wearable reply: connect, send, close."""
    with socket.create_connection((host, port), timeout=5) as c:
        c.sendall(data)


# --------------------------------------------------------------------------- #
# A TCP server on loopback that misbehaves on purpose
# --------------------------------------------------------------------------- #
def http_response(status: int, body: bytes = b"", reason: str = "X", headers=()) -> bytes:
    head = ["HTTP/1.1 %d %s" % (status, reason), "Content-Length: %d" % len(body), "Connection: close"]
    head += list(headers)
    return ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body


def _read_request(conn) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            return data
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    m = re.search(rb"(?im)^content-length:\s*(\d+)", head)
    want = int(m.group(1)) if m else 0
    while len(body) < want:
        chunk = conn.recv(65536)
        if not chunk:
            break
        body += chunk
    return head + b"\r\n\r\n" + body


class RawServer:
    """A TCP server on 127.0.0.1. For each connection it reads the HTTP request and calls
    script(server, conn, request), which answers however the test wants (or not at all).
    With read_first=False it calls the script at once, with an empty request. `requests`
    holds every request; `release` is set when the server closes."""

    def __init__(self, script, read_first=True):
        self.script = script
        self.read_first = read_first
        self.requests = []
        self.release = threading.Event()
        self._conns = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind((LOOPBACK, 0))
        self._sock.listen(64)
        self._sock.settimeout(0.1)
        self.port = self._sock.getsockname()[1]
        self.url = "http://%s:%d" % (LOOPBACK, self.port)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self.release.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(10)
            request = _read_request(conn) if self.read_first else b""
            self.requests.append(request)
            self.script(self, conn, request)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    @staticmethod
    def reset(conn):
        """Close with a TCP reset instead of a normal close."""
        linger = struct.pack("HH", 1, 0) if os.name == "nt" else struct.pack("ii", 1, 0)
        with contextlib.suppress(OSError):
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        conn.close()

    def close(self):
        self.release.set()
        self._thread.join(5)
        self._sock.close()
        for c in self._conns:
            with contextlib.suppress(OSError):
                c.close()


# --------------------------------------------------------------------------- #
# The stand-in wearable, tools/fake_wearable.py, as a child process
# --------------------------------------------------------------------------- #
class FakeWearable:
    """tools/fake_wearable.py running in a child process; `lines` is its output so far."""

    def __init__(self, args):
        self.proc = subprocess.Popen([sys.executable, FAKE_WEARABLE] + list(args), stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, env=_child_env(), cwd=empty_cwd())
        self.lines = []
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self):
        for line in self.proc.stdout:
            with self._lock:
                self.lines.append(line.rstrip())

    def count(self, text: str) -> int:
        with self._lock:
            return sum(1 for line in self.lines if text in line)

    def wait_line(self, text: str, n: int = 1, timeout: float = 10.0) -> bool:
        """Wait until at least n output lines contain `text`."""
        def done():
            return self.count(text) >= n or not self._reader.is_alive()     # or the process ended
        wait_until(done, timeout)
        return self.count(text) >= n

    def output(self) -> str:
        with self._lock:
            return " | ".join(self.lines)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self._reader.join(5)
        self.proc.stdout.close()


class MulticastMixin:
    """For tests that need loopback multicast: skipped without it, or failed when
    HALOW_TEST_REQUIRE_MULTICAST=1 (as in CI)."""

    def unavailable(self, reason: str):
        if REQUIRE_MULTICAST:
            self.fail(f"loopback multicast is required (HALOW_TEST_REQUIRE_MULTICAST=1): {reason}")
        self.skipTest(f"loopback multicast is not available: {reason}.{MULTICAST_HINT}")

    def start_fake(self, group: str, port: int, rport: int, *extra, seconds: float = 120) -> FakeWearable:
        fake = FakeWearable(["--iface", LOOPBACK, "--group", group, "--port", str(port), "--response-port",
                             str(rport), "--seconds", "%g" % seconds] + list(extra))
        self.addCleanup(fake.stop)
        if not fake.wait_line("fake wearable ready", timeout=15):
            self.unavailable(f"the stand-in wearable could not join {group} on {LOOPBACK}: {fake.output()}")
        return fake
