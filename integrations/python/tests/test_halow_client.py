# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ClearObject
"""Tests for halow_client: the codec, the command line, GatewayClient and direct mode.

Standard library only, Python 3.8 or newer. From the repository root, against the source
file in integrations/python:

    python -m unittest discover -s integrations/python/tests -v

or against the installed package (pip install <wheel>), which is then imported instead of
the source file, and whose halow-client command is tested too:

    HALOW_TEST_INSTALLED=1 python -m unittest discover -s integrations/python/tests -v

Nothing reaches a real wearable. The gateway tests talk to an HTTP stub on 127.0.0.1. The
direct-mode tests use the stand-in wearable, tools/fake_wearable.py, on loopback, with test
groups (239.255.77.4 and .5) and test ports (15020/15021 and 15030/15031). They need
loopback multicast; on Linux:
    sudo ip link set lo multicast on && sudo ip route add 239.0.0.0/8 dev lo
Without it they are skipped, or fail when HALOW_TEST_REQUIRE_MULTICAST=1.
"""
import contextlib
import importlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.dirname(HERE)                                # integrations/python
REPO = os.path.dirname(os.path.dirname(SOURCE_DIR))
VECTORS = os.path.join(REPO, "integrations", "testdata", "vectors.json")
FAKE_WEARABLE = os.path.join(REPO, "tools", "fake_wearable.py")
INSTALLED = os.environ.get("HALOW_TEST_INSTALLED", "").strip() == "1"
REQUIRE_MULTICAST = os.environ.get("HALOW_TEST_REQUIRE_MULTICAST", "").strip() == "1"
LOOPBACK = "127.0.0.1"
TOKEN = "good-token"
PUBLIC_API = ("GatewayClient", "HalowError", "send_direct", "send_heartbeat_request", "ResponseListener",
              "decode_device_response", "encode_raw8", "encode_gateway_message", "selftest", "main",
              "total_acks", "subnet_prefix", "__version__")
MULTICAST_HINT = (" On Linux: sudo ip link set lo multicast on && sudo ip route add 239.0.0.0/8 dev lo."
                  " Set HALOW_TEST_REQUIRE_MULTICAST=1 to make this a failure instead of a skip.")


def _same_dir(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _import_client():
    """halow_client: the installed package with HALOW_TEST_INSTALLED=1, else the source file."""
    if INSTALLED:
        # Never the source file: drop the source folder from the path, also as '' or '.'.
        sys.path[:] = [p for p in sys.path if not _same_dir(p or os.curdir, SOURCE_DIR)]
        try:
            return importlib.import_module("halow_client")
        except ImportError as e:
            raise ImportError(f"HALOW_TEST_INSTALLED=1, but halow_client is not installed for {sys.executable}; "
                              f"pip install the wheel first ({e})") from None
    sys.path.insert(0, SOURCE_DIR)
    return importlib.import_module("halow_client")


hc = _import_client()
HC_FILE = os.path.abspath(hc.__file__)
print(f"\nhalow_client {hc.__version__} imported from {HC_FILE} "
      f"({'installed package' if INSTALLED else 'source file'}; Python {sys.version.split()[0]})",
      file=sys.stderr, flush=True)

_tmp = {}


def setUpModule():
    # Commands run from an empty folder, so that "python -m halow_client" cannot pick up a
    # halow_client.py from the current folder.
    _tmp["cwd"] = tempfile.mkdtemp(prefix="halow-client-test-")


def tearDownModule():
    shutil.rmtree(_tmp.pop("cwd", ""), ignore_errors=True)


def _child_env(**extra) -> dict:
    """The environment for a child process: no HALOW_* settings from the caller, and the
    source folder on PYTHONPATH only when the source file is under test."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("HALOW_") and k != "PYTHONPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if not INSTALLED:
        env["PYTHONPATH"] = SOURCE_DIR
    env.update(extra)
    return env


def _entry_point():
    """The halow-client command installed next to this interpreter, or None."""
    names = ("halow-client.exe", "halow-client") if os.name == "nt" else ("halow-client",)
    for folder in (sysconfig.get_path("scripts"), os.path.dirname(sys.executable)):
        for name in names:
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                return path
    return None


def _commands():
    """[(label, argv prefix)]: every way to start the command line that is under test."""
    module = ("python -m halow_client", [sys.executable, "-m", "halow_client"])
    if INSTALLED:
        # A missing command fails the test (FileNotFoundError); never fall back to PATH, which
        # could find another installation.
        return [("halow-client", [_entry_point() or os.path.join(sysconfig.get_path("scripts"), "halow-client")]),
                module]
    return [module, ("python halow_client.py", [sys.executable, os.path.join(SOURCE_DIR, "halow_client.py")])]


def _run(argv, timeout: float = 60, **env):
    return subprocess.run(argv, cwd=_tmp.get("cwd") or tempfile.gettempdir(), env=_child_env(**env),
                          capture_output=True, text=True, timeout=timeout)


def _norm(v):
    """Decoded values with integers as strings, as testdata/vectors.json writes them."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return str(v)
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    return v


def _load_vectors() -> dict:
    with open(VECTORS, encoding="utf-8") as f:
        return json.load(f)


def _source_version() -> str:
    with open(os.path.join(SOURCE_DIR, "halow_client.py"), encoding="utf-8") as f:
        m = re.search(r'^__version__\s*=\s*"([^"]+)"', f.read(), re.M)
    return m.group(1) if m else ""


# --------------------------------------------------------------------------- #
# Which module is under test, and its public API
# --------------------------------------------------------------------------- #
class ModuleTests(unittest.TestCase):
    def test_imported_module_location(self):
        folder = os.path.dirname(HC_FILE)
        if INSTALLED:
            self.assertFalse(_same_dir(folder, SOURCE_DIR),
                             f"HALOW_TEST_INSTALLED=1 but the source file was imported: {HC_FILE}")
            site_dirs = {sysconfig.get_path("purelib"), sysconfig.get_path("platlib")}
            with contextlib.suppress(Exception):
                import site
                site_dirs.add(site.getusersitepackages())
            self.assertTrue(any(d and _same_dir(folder, d) for d in site_dirs),
                            f"{HC_FILE} is not in this interpreter's site-packages {sorted(site_dirs)}")
        else:
            self.assertTrue(_same_dir(folder, SOURCE_DIR), f"expected the source file, imported {HC_FILE}")

    def test_public_api(self):
        missing = [name for name in PUBLIC_API if not hasattr(hc, name)]
        self.assertEqual(missing, [], "missing from halow_client")
        self.assertRegex(hc.__version__, r"^\d+\.\d+\.\d+([-+.][0-9A-Za-z.-]+)?$")
        self.assertTrue(issubclass(hc.HalowError, Exception))

    def test_version_matches_the_checkout(self):
        self.assertEqual(hc.__version__, _source_version(),
                         "the module under test is not the version in integrations/python/halow_client.py")


# --------------------------------------------------------------------------- #
# Codec, against the vectors shared with the other languages
# --------------------------------------------------------------------------- #
class CodecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vec = _load_vectors()

    def test_raw8_vectors(self):
        self.assertTrue(self.vec["raw8"])
        for c in self.vec["raw8"]:
            with self.subTest(seconds=c["seconds"]):
                self.assertEqual(hc.encode_raw8(c["seconds"]).hex(), c["hex"])
                self.assertEqual(len(hc.encode_raw8(c["seconds"])), 8)

    def test_gateway_message_vectors(self):
        self.assertTrue(self.vec["gateway_message"])
        for c in self.vec["gateway_message"]:
            with self.subTest(timestamp_ms=c["timestamp_ms"], command=c["command"]):
                got = hc.encode_gateway_message(int(c["timestamp_ms"]), int(c["command"]))
                self.assertEqual(got.hex(), c["hex"])

    def test_device_response_vectors(self):
        self.assertTrue(self.vec["device_response"])
        rejected = 0
        for c in self.vec["device_response"]:
            with self.subTest(hex=c["hex"]):
                try:
                    got = _norm(hc.decode_device_response(bytes.fromhex(c["hex"])))
                except ValueError:
                    got = None
                    rejected += 1
                self.assertEqual(got, c.get("result"))
        self.assertGreater(rejected, 0, "the vectors should include malformed replies")

    def test_selftest_function(self):
        n = sum(len(self.vec[k]) for k in ("raw8", "gateway_message", "device_response"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = hc.selftest(VECTORS)
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn(f"selftest: {n}/{n} vectors match", out.getvalue())

    def test_constants(self):
        self.assertEqual((hc.GROUP, hc.PORT, hc.RESPONSE_PORT), ("239.255.0.10", 5000, 5001))
        self.assertEqual(hc.FORMATS, ("raw8", "proto", "both"))
        self.assertEqual(hc.subnet_prefix("192.168.12.50"), "192.168.12.")


# --------------------------------------------------------------------------- #
# Command line, without a gateway or a wearable
# --------------------------------------------------------------------------- #
class CommandLineTests(unittest.TestCase):
    def test_entry_point(self):
        if not INSTALLED:
            self.skipTest("source mode: the halow-client command belongs to an installed package "
                          "(HALOW_TEST_INSTALLED=1 tests it)")
        path = _entry_point()
        self.assertIsNotNone(path, f"no halow-client command next to {sys.executable}")
        print(f"\n  halow-client command: {path}", file=sys.stderr, flush=True)

    def test_module_resolves_to_the_module_under_test(self):
        r = _run([sys.executable, "-c", "import halow_client, os; print(os.path.abspath(halow_client.__file__))"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.path.normcase(os.path.realpath(r.stdout.strip())),
                         os.path.normcase(os.path.realpath(HC_FILE)))

    def test_version(self):
        for label, cmd in _commands():
            with self.subTest(cmd=label):
                r = _run(cmd + ["--version"])
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.strip(), hc.__version__)

    def test_help(self):
        for label, cmd in _commands():
            with self.subTest(cmd=label):
                r = _run(cmd + ["--help"])
                self.assertEqual(r.returncode, 0, r.stderr)
                for word in ("health", "wearables", "alert", "direct-alert", "direct-heartbeat", "selftest"):
                    self.assertIn(word, r.stdout)
                r = _run(cmd + ["direct-alert", "--help"])
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("--wait-acks", r.stdout)

    def test_selftest_with_the_vectors(self):
        for label, cmd in _commands():
            with self.subTest(cmd=label):
                r = _run(cmd + ["selftest", VECTORS])
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertRegex(r.stdout, r"selftest: (\d+)/\1 vectors match")

    def test_selftest_default_path(self):
        label, cmd = _commands()[0]
        r = _run(cmd + ["selftest"])
        if INSTALLED:   # the package does not include testdata/vectors.json: a clear error, exit 2
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("pass its path", r.stderr)
        else:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_bad_settings(self):
        label, cmd = _commands()[0]
        r = _run(cmd + ["alert", "--format", "mp3"])
        self.assertEqual(r.returncode, 2)
        r = _run(cmd + ["health", "--url", "ftp://example.invalid"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("http://", r.stderr)
        # Not an IPv4 address: refused before any socket is opened.
        r = _run(cmd + ["direct-alert", "--egress", "999.1.1.1", "--group", "239.255.77.9", "--port", "15090",
                        "--wait-acks", "0"])
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("IPv4", r.stderr)
        r = _run(cmd + ["direct-alert", "--egress", LOOPBACK, "--group", "239.255.77.9", "--wait-acks", "0"],
                 HALOW_PORT="70000")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("HALOW_PORT", r.stderr)


# --------------------------------------------------------------------------- #
# Gateway mode, against an HTTP stub on loopback
# --------------------------------------------------------------------------- #
class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _serve(self):
        srv = self.server
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        srv.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                             "body": body})
        status, payload, delay = srv.routes.get((self.command, self.path), (404, {"error": "not found"}, 0))
        if self.command == "POST" and self.path == "/alert" and status == 200 \
                and self.headers.get("Authorization") != "Bearer " + TOKEN:
            status, payload = 401, {"error": "unauthorized"}
        if delay:
            srv.release.wait(delay)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            pass                # the client gave up (the timeout test)

    do_GET = _serve
    do_POST = _serve


class _StubServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def _default_routes() -> dict:
    return {
        ("GET", "/health"): (200, {"ok": True, "egress": LOOPBACK, "version": hc.__version__, "listener": True}, 0),
        ("GET", "/wearables"): (200, {"devices": [{"ip": "127.0.0.1", "acks": 2, "heartbeats": 1},
                                                  {"ip": "127.0.0.2", "acks": 3, "heartbeats": 0}],
                                      "recent": []}, 0),
        ("POST", "/alert"): (200, {"event": "alert_sent", "ts_ms": 1767225600123, "format": "raw8",
                                   "egress": LOOPBACK, "datagrams": 1}, 0),
    }


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = _StubServer((LOOPBACK, 0), _StubHandler)
        cls.srv.routes, cls.srv.requests, cls.srv.release = {}, [], threading.Event()
        cls.url = "http://%s:%d" % (LOOPBACK, cls.srv.server_address[1])
        cls.thread = threading.Thread(target=cls.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.release.set()
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join(5)

    def setUp(self):
        self.srv.routes = _default_routes()
        self.srv.requests = []
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("HALOW_") and k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def route(self, method, path, status, payload, delay=0.0):
        self.srv.routes[(method, path)] = (status, payload, delay)

    def raises(self, fn, status):
        with self.assertRaises(hc.HalowError) as cm:
            fn()
        self.assertEqual(cm.exception.status, status, str(cm.exception))
        return cm.exception

    def test_health(self):
        body = hc.GatewayClient(self.url, token=TOKEN).health()
        self.assertIs(body["ok"], True)
        req = self.srv.requests[-1]
        self.assertEqual((req["method"], req["path"]), ("GET", "/health"))
        self.assertNotIn("Authorization", req["headers"], "the token is sent only with POST /alert")
        self.assertEqual(req["headers"].get("Accept"), "application/json")

    def test_health_503_returns_the_body(self):
        self.route("GET", "/health", 503, {"ok": False, "error": "no HaLowLink address"})
        body = hc.GatewayClient(self.url).health()
        self.assertEqual(body, {"ok": False, "error": "no HaLowLink address"})

    def test_wearables_and_total_acks(self):
        body = hc.GatewayClient(self.url).wearables()
        self.assertEqual(len(body["devices"]), 2)
        self.assertEqual(hc.total_acks(body), 5)
        self.assertEqual(hc.total_acks({}), 0)

    def test_alert_sends_the_token_and_the_body(self):
        record = hc.GatewayClient(self.url, token=TOKEN).alert(label="zone-3", fmt="PROTO", burst_seconds=1)
        self.assertEqual(record["event"], "alert_sent")
        req = self.srv.requests[-1]
        self.assertEqual((req["method"], req["path"]), ("POST", "/alert"))
        self.assertEqual(req["headers"].get("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(req["headers"].get("Content-Type"), "application/json")
        self.assertEqual(json.loads(req["body"].decode("utf-8")),
                         {"label": "zone-3", "format": "proto", "burst_seconds": 1.0})

    def test_settings_from_the_environment(self):
        os.environ["HALOW_URL"] = self.url + "/"
        os.environ["HALOW_TOKEN"] = "  %s  " % TOKEN
        gw = hc.GatewayClient()
        self.assertEqual((gw.url, gw.token), (self.url, TOKEN))
        self.assertEqual(gw.alert()["event"], "alert_sent")
        self.assertEqual(json.loads(self.srv.requests[-1]["body"].decode("utf-8")), {})
        os.environ.pop("HALOW_URL")
        self.assertEqual(hc.GatewayClient().url, hc.DEFAULT_URL)

    def test_401_wrong_or_missing_token(self):
        e = self.raises(lambda: hc.GatewayClient(self.url, token="wrong").alert(label="x"), 401)
        self.assertIn("HTTP 401", str(e))
        self.assertIn("HALOW_TOKEN", str(e))
        self.assertEqual(e.body, {"error": "unauthorized"})
        self.raises(lambda: hc.GatewayClient(self.url, token="").alert(), 401)
        self.assertNotIn("Authorization", self.srv.requests[-1]["headers"])

    def test_429_rate_limited(self):
        self.route("POST", "/alert", 429, {"error": "rate limited: one alert per 0.5 s"})
        e = self.raises(lambda: hc.GatewayClient(self.url, token=TOKEN).alert(), 429)
        self.assertIn("rate limited", str(e))
        self.assertIn("HTTP 429", str(e))

    def test_500_with_a_body_that_is_not_json(self):
        self.route("POST", "/alert", 500, b"boom")
        e = self.raises(lambda: hc.GatewayClient(self.url, token=TOKEN).alert(), 500)
        self.assertEqual(e.body, {"error": "boom"})
        self.assertIn("HaLowLink", str(e))

    def test_503_from_wearables_raises(self):
        self.route("GET", "/wearables", 503, {"error": "listener stopped"})
        e = self.raises(lambda: hc.GatewayClient(self.url).wearables(), 503)
        self.assertIn("listener stopped", str(e))

    def test_404_hint(self):
        e = self.raises(lambda: hc.GatewayClient(self.url + "/not-the-gateway").health(), 404)
        self.assertIn("HALOW_URL", str(e))

    def test_timeout(self):
        self.route("GET", "/health", 200, {"ok": True}, delay=5.0)
        start = time.monotonic()
        e = self.raises(lambda: hc.GatewayClient(self.url, timeout=0.3).health(), None)
        self.assertLess(time.monotonic() - start, 4.0)
        self.assertIn("cannot reach the gateway", str(e))
        self.assertEqual(e.body, {})

    def test_connection_refused(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((LOOPBACK, 0))
        port = s.getsockname()[1]
        s.close()                                   # nothing listens on this port now
        e = self.raises(lambda: hc.GatewayClient("http://%s:%d" % (LOOPBACK, port), timeout=5).health(), None)
        self.assertIn("cannot reach the gateway", str(e))

    def test_proxy_settings_are_ignored(self):
        for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
            os.environ[name] = "http://%s:9" % LOOPBACK          # nothing listens there
        os.environ.pop("no_proxy", None)
        os.environ.pop("NO_PROXY", None)
        self.assertIs(hc.GatewayClient(self.url).health()["ok"], True)

    def test_bad_arguments_send_nothing(self):
        with self.assertRaises(ValueError):
            hc.GatewayClient("ftp://example.invalid")
        gw = hc.GatewayClient(self.url, token=TOKEN)
        for kwargs in ({"fmt": "mp3"}, {"burst_seconds": -1}, {"burst_seconds": float("nan")}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                gw.alert(**kwargs)
        self.assertEqual(self.srv.requests, [])

    def test_command_line_against_the_stub(self):
        label, cmd = _commands()[0]
        r = _run(cmd + ["health", "--url", self.url])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIs(json.loads(r.stdout)["ok"], True)

        r = _run(cmd + ["wearables"], HALOW_URL=self.url)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(json.loads(r.stdout)["devices"]), 2)

        r = _run(cmd + ["alert", "--url", self.url, "--label", "cli test", "--format", "both"], HALOW_TOKEN=TOKEN)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(self.srv.requests[-1]["body"].decode("utf-8")),
                         {"label": "cli test", "format": "both"})

        r = _run(cmd + ["alert", "--url", self.url], HALOW_TOKEN="wrong")
        self.assertEqual(r.returncode, 1)
        self.assertIn("HTTP 401", r.stderr)

        self.route("GET", "/health", 503, {"ok": False, "error": "no HaLowLink address"})
        r = _run(cmd + ["health", "--url", self.url])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((LOOPBACK, 0))
        port = s.getsockname()[1]
        s.close()
        r = _run(cmd + ["health", "--url", "http://%s:%d" % (LOOPBACK, port), "--timeout", "5"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot reach the gateway", r.stderr)


# --------------------------------------------------------------------------- #
# Direct mode, against the stand-in wearable on loopback
# --------------------------------------------------------------------------- #
class DirectModeTests(unittest.TestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("HALOW_")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def unavailable(self, reason: str):
        if REQUIRE_MULTICAST:
            self.fail(f"loopback multicast is required (HALOW_TEST_REQUIRE_MULTICAST=1): {reason}")
        self.skipTest(f"loopback multicast is not available: {reason}.{MULTICAST_HINT}")

    def start_fake(self, group: str, port: int, rport: int, device_id: str) -> list:
        """Start tools/fake_wearable.py on loopback; returns its output lines (kept up to date)."""
        proc = subprocess.Popen(
            [sys.executable, FAKE_WEARABLE, "--iface", LOOPBACK, "--group", group, "--port", str(port),
             "--response-port", str(rport), "--device-id", device_id, "--seconds", "120"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=_child_env())
        lines, ready = [], threading.Event()

        def pump():
            for line in proc.stdout:
                lines.append(line.rstrip())
                if "fake wearable ready" in line:
                    ready.set()
            ready.set()

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()

        def stop():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(5)
            reader.join(5)
            proc.stdout.close()

        self.addCleanup(stop)
        ready.wait(15)
        if not any("fake wearable ready" in line for line in lines):
            self.unavailable(f"the stand-in wearable could not join {group} on {LOOPBACK}: " + " | ".join(lines))
        return lines

    def test_heartbeat_and_alerts_with_the_listener(self):
        group, port, rport = "239.255.77.4", 15020, 15021
        fake = self.start_fake(group, port, rport, "fake-unittest")
        seen = []
        with hc.ResponseListener(rport, bind=LOOPBACK, allow_prefix=hc.subnet_prefix(LOOPBACK),
                                 on_response=seen.append) as replies:
            try:
                ts = hc.send_heartbeat_request(LOOPBACK, group, port)
            except OSError as e:
                self.unavailable(f"cannot send multicast from {LOOPBACK}: {e}")
            hb = replies.wait_for("HEARTBEAT", timeout=5)
            if hb is None:
                self.unavailable("no heartbeat came back within 5 s (stand-in wearable: %s)" % " | ".join(fake))
            self.assertEqual(hb["ip"], LOOPBACK)
            self.assertEqual(hb["timestamp_ms"], ts, "a heartbeat echoes the request's timestamp")
            self.assertEqual(hb["heartbeat"]["device_id"], "fake-unittest")
            self.assertEqual((hb["heartbeat"]["battery_percentage"], hb["heartbeat"]["rssi"]), (87, -62))

            for fmt in ("raw8", "proto"):
                time.sleep(0.05)
                rec = hc.send_direct(LOOPBACK, fmt, group, port)
                self.assertEqual({k: rec[k] for k in ("event", "format", "group", "egress", "datagrams")},
                                 {"event": "alert_sent", "format": fmt, "group": f"{group}:{port}",
                                  "egress": LOOPBACK, "datagrams": 1})
                ack = replies.wait_for("ACK", timeout=5, after_ms=rec["ts_ms"])
                self.assertIsNotNone(ack, f"no ACK for a {fmt} alert (stand-in wearable: {' | '.join(fake)})")
                self.assertEqual(ack["ip"], LOOPBACK)

            # A burst repeats the same bytes; the stand-in wearable ACKs an alert once.
            time.sleep(0.05)
            rec = hc.send_direct(LOOPBACK, "raw8", group, port, burst_seconds=0.4, burst_interval_ms=50)
            self.assertGreater(rec["datagrams"], 1)
            self.assertIsNotNone(replies.wait_for("ACK", timeout=5, after_ms=rec["ts_ms"]))
            time.sleep(0.5)
            acks = [r for r in replies.responses if r["code"] == "ACK" and r["received_ms"] >= rec["ts_ms"]]
            self.assertEqual(len(acks), 1, acks)
        self.assertEqual(len(seen), len(replies.responses), "on_response sees every reply")

    def test_command_line_direct_mode(self):
        group, port, rport = "239.255.77.5", 15030, 15031
        fake = self.start_fake(group, port, rport, "fake-cli")
        label, cmd = _commands()[0]
        where = ["--egress", LOOPBACK, "--group", group, "--port", str(port), "--response-port", str(rport)]

        r = _run(cmd + ["direct-heartbeat"] + where + ["--seconds", "2"])
        if r.returncode == 2 and re.search(r"cannot send|unreachable|no route|errno", r.stderr, re.I):
            self.unavailable(r.stderr.strip())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        if "HEARTBEAT from" not in r.stdout:
            self.unavailable("no heartbeat came back within 2 s (%s; stand-in wearable: %s)"
                             % (r.stdout.strip(), " | ".join(fake)))
        self.assertIn("HEARTBEAT from 127.0.0.1: device_id=fake-cli", r.stdout)

        r = _run(cmd + ["direct-alert"] + where + ["--format", "both", "--burst-seconds", "0.3", "--wait-acks", "3"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ACK from 127.0.0.1", r.stdout)
        self.assertRegex(r.stdout, r"(?m)^[1-9][0-9]* ACK\(s\) from 1 wearable\(s\)")


if __name__ == "__main__":
    unittest.main()
