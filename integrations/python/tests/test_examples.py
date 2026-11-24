# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ClearObject
"""The examples, beyond test_halow_client.ExampleScriptTests: example.py (gateway and
direct mode, HALOW_ACK_WAIT), WearableAlerter's close() and alerts after it, the distance
demo, and wearable_status.py's arguments and edge cases.

Run with the rest: python -m unittest discover -s integrations/python/tests -v
"""
import contextlib
import io
import os
import sys
import threading
import time
import unittest
from unittest import mock

from halow_test_support import (
    LOOPBACK,
    SOURCE_DIR,
    TOKEN,
    CleanEnv,
    MulticastMixin,
    _ScriptedClient,
    _StubCase,
    closed_port,
    device_response,
    free_port,
    hc,
    load_module,
    pick_group,
    run,
    send_tcp,
    wait_until,
)

EXAMPLE_PY = os.path.join(SOURCE_DIR, "example.py")
EXAMPLES_DIR = os.path.join(SOURCE_DIR, "examples")


def _load(path, name):
    module = load_module(path, name)
    assert module.hc is hc, "the example imported another halow_client"
    return module


def _device(ip, acks=0, heartbeats=0, **more):
    return dict(ip=ip, acks=acks, heartbeats=heartbeats, **more)


class ExamplePyTests(_StubCase):
    def example(self, *args, **env):
        return run([sys.executable, EXAMPLE_PY] + list(args), **env)

    def test_ack_wait_setting(self):
        mod = _load(EXAMPLE_PY, "example_py")
        for raw, want in (("", 15.0), ("  ", 15.0), ("0", 0.0), ("2.5", 2.5), (" 3600 ", 3600.0)):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"HALOW_ACK_WAIT": raw}):
                self.assertEqual(mod.ack_wait_s(), want)
        for raw in ("-1", "3600.5", "nan", "inf", "-inf", "abc", "1e9", "5s"):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"HALOW_ACK_WAIT": raw}):
                with self.assertRaisesRegex(ValueError, 'HALOW_ACK_WAIT="%s"' % raw.strip()):
                    mod.ack_wait_s()

    def test_bad_ack_wait_exits_2_before_anything_is_sent(self):
        for raw in ("nan", "-1"):
            with self.subTest(raw=raw):
                r = self.example(HALOW_URL=self.url, HALOW_TOKEN=TOKEN, HALOW_ACK_WAIT=raw)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("HALOW_ACK_WAIT", r.stderr)
        self.assertEqual(self.srv.requests, [])

    def test_gateway_mode_without_an_ack(self):
        r = self.example(HALOW_URL=self.url, HALOW_TOKEN=TOKEN, HALOW_ACK_WAIT="0")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("no ACK within 0 s", r.stderr)
        self.assertEqual([(q["method"], q["path"]) for q in self.srv.requests],
                         [("GET", "/health"), ("GET", "/wearables"), ("POST", "/alert")])
        self.assertEqual(self.srv.requests[-1]["body"], b'{"label": "python example"}')

    def test_gateway_mode_failures(self):
        r = self.example(HALOW_URL=self.url, HALOW_TOKEN="wrong", HALOW_ACK_WAIT="0")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("HTTP 401", r.stderr)
        r = self.example(HALOW_URL="http://%s:%d" % (LOOPBACK, closed_port()), HALOW_ACK_WAIT="0")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("cannot reach the gateway", r.stderr)
        self.route("GET", "/health", 503, {"ok": False, "error": "no HaLowLink address"})
        n = len(self.srv.requests)
        r = self.example(HALOW_URL=self.url, HALOW_TOKEN=TOKEN)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("the gateway cannot send right now: no HaLowLink address", r.stderr)
        self.assertEqual(len(self.srv.requests), n + 1, "only GET /health; no alert")

    def test_gateway_mode_with_an_ack(self):
        mod = _load(EXAMPLE_PY, "example_py")
        before = {"devices": [_device("192.168.12.143", acks=2)]}
        after = {"devices": [_device("192.168.12.143", acks=3, heartbeats=1, device_id="wz-07",
                                     battery_percentage=87)]}
        client = _ScriptedClient(wearables=[before, after], alerts=[lambda: {"event": "alert_sent", "ts_ms": 1}])
        out = io.StringIO()
        with mock.patch.object(hc, "GatewayClient", lambda: client), contextlib.redirect_stdout(out):
            self.assertEqual(mod.via_gateway(5.0), 0)
        self.assertEqual(client.alert_labels, ["python example"])
        self.assertIn("wearable 192.168.12.143: acks=3 heartbeats=1 device_id=wz-07 battery=87%", out.getvalue())
        self.assertTrue(out.getvalue().endswith("ACK received\n"))

    def test_selftest_flag(self):
        r = self.example("--selftest")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"selftest: (\d+)/\1 vectors match")
        self.assertEqual(self.srv.requests, [])

    def test_direct_mode_heartbeat_without_a_payload(self):
        # A HEARTBEAT reply with no heartbeat payload (code 2, no field 4) used to raise
        # KeyError before the alert was sent.
        mod = _load(EXAMPLE_PY, "example_py")
        rport, sends = free_port(), []

        def heartbeat_request(egress, group, port):
            sends.append("heartbeat")
            send_tcp(rport, device_response(ts=4242, code=2, ack=None))
            return 4242

        def alert(egress, fmt, group, port):
            sends.append(fmt)
            send_tcp(rport, device_response(ts=5))
            return {"event": "alert_sent", "ts_ms": 0, "format": fmt, "group": f"{group}:{port}", "egress": egress,
                    "datagrams": 1}

        out = io.StringIO()
        with mock.patch.object(hc, "send_heartbeat_request", heartbeat_request), \
                mock.patch.object(hc, "send_direct", alert), \
                mock.patch.dict(os.environ, {"HALOW_EGRESS": LOOPBACK, "HALOW_RESPONSE_PORT": str(rport)}), \
                contextlib.redirect_stdout(out):
            code = mod.main(["--direct"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(sends, ["heartbeat", "raw8"])
        self.assertRegex(out.getvalue(), r"HEARTBEAT from 127\.0\.0\.1: no heartbeat payload, round trip -?\d+ ms\n")
        self.assertIn("ACK from 127.0.0.1: wearable timestamp 5\n", out.getvalue())

    def test_direct_mode_bad_settings_exit_2_before_anything_is_sent(self):
        mod = _load(EXAMPLE_PY, "example_py")
        sends = []
        with mock.patch.object(hc, "send_direct", lambda *a, **k: sends.append(a)), \
                mock.patch.object(hc, "send_heartbeat_request", lambda *a, **k: sends.append(a)):
            for env in ({"HALOW_PORT": "0"}, {"HALOW_RESPONSE_PORT": "70000"}, {"HALOW_ACK_WAIT": "x"}):
                with self.subTest(env=env), mock.patch.dict(os.environ, env):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        self.assertEqual(mod.main(["--direct"]), 2)
                    self.assertIn(next(iter(env)), err.getvalue())
        self.assertEqual(sends, [])


class ExamplePyDirectTests(MulticastMixin, CleanEnv, unittest.TestCase):
    def test_direct_mode_against_the_stand_in_wearable(self):
        group, port, rport = pick_group("example-direct"), free_port(), free_port()
        fake = self.start_fake(group, port, rport, "--device-id", "fake-example")
        r = run([sys.executable, EXAMPLE_PY, "--direct"], HALOW_EGRESS=LOOPBACK, HALOW_GROUP=group,
                HALOW_PORT=str(port), HALOW_RESPONSE_PORT=str(rport), HALOW_ACK_WAIT="5", HALOW_FORMAT="proto")
        if r.returncode == 2 and "cannot send" in r.stderr:
            self.unavailable(r.stderr.strip())
        if "no heartbeat within 3 s" in r.stdout:
            self.unavailable(f"no heartbeat came back ({fake.output()})")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr + fake.output())
        self.assertRegex(r.stdout, r"HEARTBEAT from 127\.0\.0\.1: device_id=fake-example battery=87% rssi=-62 dBm, "
                                   r"round trip \d+ ms")
        self.assertIn('"format": "proto"', r.stdout)
        self.assertIn("ACK from 127.0.0.1", r.stdout)


class _BlockingClient:
    """Stands in for GatewayClient: alert() waits until `release` is set."""

    def __init__(self):
        self.release, self.labels = threading.Event(), []

    def alert(self, label=None):
        self.release.wait(30)
        self.labels.append(label)
        return {"ts_ms": len(self.labels)}


class WearableAlerterTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load(os.path.join(EXAMPLES_DIR, "alert_from_your_app.py"), "example_alert_from_your_app")
        self.results = []

    def on_result(self, label, record, error):
        self.results.append((label, record, error))

    def test_close_sends_what_is_still_queued(self):
        client = _BlockingClient()
        alerter = self.mod.WearableAlerter(client, on_result=self.on_result)
        for i in range(50):
            alerter.alert("a%d" % i)
        start = time.monotonic()
        alerter.close(timeout=0.2)                      # the gateway is stuck: close() gives up in time
        self.assertLess(time.monotonic() - start, 5)
        self.assertTrue(alerter._thread.is_alive())
        client.release.set()
        alerter.close(timeout=10)                       # then everything queued goes out, in order
        self.assertFalse(alerter._thread.is_alive())
        self.assertEqual(client.labels, ["a%d" % i for i in range(50)])
        self.assertEqual([r[0] for r in self.results], client.labels)
        self.assertTrue(all(r[2] is None for r in self.results))

    def test_an_alert_after_close_is_reported_not_lost(self):
        # It used to go into the queue after the stop marker and vanish without a callback.
        client = _ScriptedClient(alerts=[lambda: {"ts_ms": 1}])
        alerter = self.mod.WearableAlerter(client, on_result=self.on_result)
        alerter.alert("before")
        alerter.close()
        alerter.alert("after")
        self.assertEqual(client.alert_labels, ["before"])
        self.assertEqual([(r[0], r[1]) for r in self.results], [("before", {"ts_ms": 1}), ("after", None)])
        self.assertIsInstance(self.results[1][2], RuntimeError)
        self.assertIn("closed", str(self.results[1][2]))
        alerter.close()                                 # closing again is harmless

    def test_alerts_from_many_threads_and_close(self):
        client = _ScriptedClient(alerts=[lambda: {"ts_ms": 0}] * 400)
        alerter = self.mod.WearableAlerter(client, on_result=self.on_result)
        go = threading.Barrier(5)

        def raise_alerts(n):
            go.wait(5)
            for i in range(100):
                alerter.alert("t%d-%d" % (n, i))

        threads = [threading.Thread(target=raise_alerts, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        go.wait(5)
        alerter.close()                                 # while the others are still queueing
        for t in threads:
            t.join(10)
        self.assertTrue(wait_until(lambda: len(self.results) == 400, 10), len(self.results))
        sent = [r[0] for r in self.results if r[2] is None]
        refused = [r[0] for r in self.results if r[2] is not None]
        self.assertEqual(sorted(sent + refused), sorted("t%d-%d" % (n, i) for n in range(4) for i in range(100)))
        self.assertEqual(sorted(client.alert_labels), sorted(sent), "every alert was sent or reported")

    def test_the_default_callback_prints(self):
        def fails():
            raise hc.HalowError("POST /alert: HTTP 500: boom", 500)

        client = _ScriptedClient(alerts=[lambda: {"ts_ms": 7}, fails])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            alerter = self.mod.WearableAlerter(client)
            alerter.alert("ok")
            alerter.alert("bad")
            alerter.close()
        self.assertEqual(out.getvalue(), "alert 'ok' sent (gateway time 7 ms)\n")
        self.assertEqual(err.getvalue(), "alert 'bad' FAILED: POST /alert: HTTP 500: boom\n")

    def test_the_distance_demo_alerts_once_per_approach(self):
        class Recorder:
            def __init__(self):
                self.labels = []

            def alert(self, label=None):
                self.labels.append(label)

        cases = [
            (self.mod.DEMO_READINGS, 2),
            ((4.0, 3.0, 2.0), 1),                       # starts inside: one alert
            ((9.0, 8.0), 0),
            ((4.9, 5.0, 4.9, 5.1, 4.9), 3),             # exactly 5 m is outside
            ((), 0),
        ]
        for readings, want in cases:
            with self.subTest(readings=readings):
                rec = Recorder()
                self.mod.watch_distance(rec, readings, step_s=0)
                self.assertEqual(rec.labels, ["vehicle within 5 m"] * want)
        rec = Recorder()
        self.mod.watch_distance(rec, (2.0, 1.0, 3.0), step_s=0, limit_m=2.5)
        self.assertEqual(rec.labels, ["vehicle within 2.5 m"])

    def test_bad_step(self):
        for step in ("nan", "61", "-0.1", "abc"):
            with self.subTest(step=step):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    try:
                        code = self.mod.main(["--step", step])
                    except SystemExit as e:
                        code = e.code
                self.assertEqual(code, 2, err.getvalue())


class WearableStatusTests(CleanEnv, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.mod = _load(os.path.join(EXAMPLES_DIR, "wearable_status.py"), "example_wearable_status")

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.mod.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_bad_arguments_exit_2(self):
        for argv in (["--wait", "nan"], ["--wait", "601"], ["--wait", "-1"], ["--wait", "inf"],
                     ["--expect", "-1"], ["--expect", "1.5"], ["--expect", "abc"], ["--bogus"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv)[0], 2)

    def test_gateway_not_reached_exits_1(self):
        os.environ["HALOW_URL"] = "http://%s:%d" % (LOOPBACK, closed_port())
        code, out, err = self.main("--wait", "0")
        self.assertEqual(code, 1, out + err)
        self.assertIn("cannot reach the gateway", err)

    def test_no_wearables_at_all(self):
        lines = []
        client = _ScriptedClient(wearables=[{"devices": [], "recent": []}])
        self.assertEqual(self.mod.check(client, expect=0, wait_s=0, out=lines.append), 0)
        self.assertEqual(lines[-1], "0 of 0 wearable(s) answered within 0 s")
        lines = []
        self.assertEqual(self.mod.check(client, expect=1, wait_s=0, out=lines.append), 3)
        self.assertIn("want at least 1", lines[-1])

    def test_wait_0_still_sees_an_answer_between_the_two_reads(self):
        before = {"devices": [_device("192.168.12.143", heartbeats=4)]}
        after = {"devices": [_device("192.168.12.143", heartbeats=5)]}
        lines = []
        client = _ScriptedClient(wearables=[before, after])
        self.assertEqual(self.mod.check(client, expect=1, wait_s=0, out=lines.append), 0, lines)
        self.assertIn("1 of 1 wearable(s) answered within 0 s", lines[-1])

    def test_more_expected_than_answered(self):
        before = {"devices": [_device("192.168.12.%d" % i, heartbeats=1) for i in (10, 11, 12)]}
        after = {"devices": [_device("192.168.12.10", heartbeats=2), _device("192.168.12.11", heartbeats=1),
                             _device("192.168.12.12", heartbeats=3, acks=1)]}
        lines = []
        client = _ScriptedClient(wearables=[before, after])
        self.assertEqual(self.mod.check(client, expect=3, wait_s=0.1, poll_s=0.02, out=lines.append), 3, lines)
        self.assertIn("2 of 3 wearable(s) answered within 0.1 s", lines[-2])
        statuses = [line.split()[-1] for line in lines[2:5]]
        self.assertEqual(statuses, ["answered", "silent", "answered"], "sorted by address")


if __name__ == "__main__":
    unittest.main()
