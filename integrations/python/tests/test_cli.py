# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ClearObject
"""The command line, halow_client.main(): exit codes 0, 1 and 2 for every command, settings
from flags and the environment, and what it prints. Most tests call main() in this process
(fast, and able to prove that nothing was sent); a few run the real command.

Exit codes: 0 done; 1 the gateway failed or was not reached (health: not ok); 2 bad
settings or arguments, or this machine cannot send or listen.

Run with the rest: python -m unittest discover -s integrations/python/tests -v
"""
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from halow_test_support import (
    LOOPBACK,
    TOKEN,
    VECTORS,
    CleanEnv,
    _commands,
    _StubCase,
    call_main,
    closed_port,
    device_response,
    free_port,
    hc,
    heartbeat_payload,
    run,
    send_tcp,
)

GROUP, PORT = "239.255.201.7", 15987       # direct-mode tests below never really send


class GatewayCommandTests(_StubCase):
    def cli(self, *argv):
        return call_main(list(argv))

    def test_health(self):
        code, out, err = self.cli("health", "--url", self.url)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, json.dumps(hc.GatewayClient(self.url).health(), indent=1) + "\n")
        for status, body in ((503, {"ok": False, "error": "no HaLowLink address"}), (200, {"ok": False}),
                             (200, {"version": "x"}), (200, b"<html>not the gateway</html>"), (200, b"")):
            with self.subTest(status=status, body=body):
                self.route("GET", "/health", status, body)
                code, out, err = self.cli("health", "--url", self.url)
                self.assertEqual(code, 1, out + err)
                self.assertTrue(out.startswith("{"), out)
        self.route("GET", "/health", 500, {"error": "boom"})
        code, out, err = self.cli("health", "--url", self.url)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "halow_client: GET /health: HTTP 500: " + hc._STATUS_HINTS[500] + " (boom)\n")

    def test_wearables(self):
        code, out, err = self.cli("wearables", "--url", self.url)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["devices"][0]["acks"], 2)
        self.assertIn('\n "devices": [\n', out, "indented by one space")
        self.route("GET", "/wearables", 503, {"error": "listener stopped"})
        code, out, err = self.cli("wearables", "--url", self.url)
        self.assertEqual(code, 1)
        self.assertIn("HTTP 503", err)
        self.assertIn("(listener stopped)", err)

    def test_alert(self):
        os.environ["HALOW_TOKEN"] = TOKEN
        code, out, err = self.cli("alert", "--url", self.url, "--label", "zone ü", "--format", "proto",
                                  "--burst-seconds", "1.5")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["event"], "alert_sent")
        self.assertEqual(json.loads(self.srv.requests[-1]["body"].decode()),
                         {"label": "zone ü", "format": "proto", "burst_seconds": 1.5})
        for status in (400, 401, 404, 413, 429, 500, 503, 418):
            with self.subTest(status=status):
                self.route("POST", "/alert", status, {"error": "e%d" % status})
                code, out, err = self.cli("alert", "--url", self.url)
                self.assertEqual((code, out), (1, ""))
                self.assertTrue(err.startswith("halow_client: POST /alert: HTTP %d: " % status), err)

    def test_alert_token_from_the_environment_only(self):
        code, _, err = self.cli("alert", "--url", self.url)
        self.assertEqual(code, 1)
        self.assertIn("HALOW_TOKEN", err)
        self.assertNotIn("Authorization", self.srv.requests[-1]["headers"])

    def test_url_from_the_environment_and_flag(self):
        os.environ["HALOW_URL"] = self.url + "/"
        self.assertEqual(self.cli("health")[0], 0)
        os.environ["HALOW_URL"] = "http://%s:%d" % (LOOPBACK, closed_port())
        self.assertEqual(self.cli("health", "--url", self.url)[0], 0, "--url wins")
        code, _, err = self.cli("health")
        self.assertEqual(code, 1)
        self.assertIn("cannot reach the gateway", err)

    def test_timeout(self):
        # 0 means the default (10 s), not "fail at once".
        self.assertEqual(self.cli("health", "--url", self.url, "--timeout", "0")[0], 0)
        self.assertEqual(self.cli("health", "--url", self.url, "--timeout", "0.5")[0], 0)
        self.route("GET", "/health", 200, {"ok": True}, delay=3)
        code, _, err = self.cli("health", "--url", self.url, "--timeout", "0.3")
        self.assertEqual(code, 1)
        self.assertIn("cannot reach the gateway", err)

    def test_bad_arguments_exit_2_and_send_nothing(self):
        cases = [
            ("health", "--url", "ftp://example.invalid"),
            ("wearables", "--url", "gateway:8080"),
            ("health", "--url", self.url, "--timeout", "-1"),
            ("health", "--url", self.url, "--timeout", "nan"),
            ("health", "--url", self.url, "--timeout", "inf"),
            ("health", "--url", self.url, "--timeout", "soon"),
            ("alert", "--url", self.url, "--format", "mp3"),
            ("alert", "--url", self.url, "--format", "RAW8"),
            ("alert", "--url", self.url, "--burst-seconds", "-0.5"),
            ("alert", "--url", self.url, "--burst-seconds", "nan"),
            ("alert", "--url", self.url, "--burst-seconds", "abc"),
            ("alert", "--url", self.url, "--label"),
            ("alert", "--url", self.url, "--unknown-flag"),
            ("frobnicate",),
            (),
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                code, out, err = self.cli(*argv)
                self.assertEqual(code, 2, out + err)
                self.assertTrue(err, "a reason is printed")
        self.assertEqual(self.srv.requests, [])

    def test_version(self):
        code, out, _ = self.cli("--version")
        self.assertEqual((code, out.strip()), (0, hc.__version__))


class SelftestCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="halow-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_exit_codes(self):
        code, out, _ = call_main(["selftest", VECTORS])
        self.assertEqual(code, 0)
        self.assertRegex(out, r"selftest: (\d+)/\1 vectors match")
        with open(VECTORS, encoding="utf-8") as f:
            vec = json.load(f)
        vec["gateway_message"][0]["hex"] = "00"
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            json.dump(vec, f)
        code, out, _ = call_main(["selftest", bad])
        self.assertEqual(code, 1, out)
        self.assertIn("want 00", out)
        code, _, err = call_main(["selftest", os.path.join(self.tmp, "missing.json")])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("halow_client: "), err)
        broken = os.path.join(self.tmp, "broken.json")
        with open(broken, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(call_main(["selftest", broken])[0], 2)


class _Sent:
    """Records the direct-mode sends main() makes, instead of sending."""

    def __init__(self, test, reply=None):
        self.calls, self.reply = [], reply
        for name, fn in (("send_direct", self.direct), ("send_heartbeat_request", self.heartbeat)):
            p = mock.patch.object(hc, name, fn)
            p.start()
            test.addCleanup(p.stop)

    def direct(self, egress, fmt, group, port, burst_seconds=0.0):
        self.calls.append(("direct", egress, fmt, group, port, burst_seconds))
        if self.reply:
            self.reply()
        return {"event": "alert_sent", "ts_ms": 1, "format": fmt, "group": f"{group}:{port}", "egress": egress,
                "datagrams": 3}

    def heartbeat(self, egress, group, port):
        self.calls.append(("heartbeat", egress, group, port))
        if self.reply:
            self.reply()
        return 1


class DirectCommandTests(CleanEnv, unittest.TestCase):
    def where(self, rport=None):
        return ["--egress", LOOPBACK, "--group", GROUP, "--port", str(PORT), "--response-port",
                str(rport or free_port())]

    def test_direct_alert_without_listening(self):
        sent = _Sent(self)
        code, out, err = call_main(["direct-alert"] + self.where() + ["--wait-acks", "0", "--format", "both",
                                                                      "--burst-seconds", "0.5"])
        self.assertEqual(code, 0, err)
        self.assertEqual(sent.calls, [("direct", LOOPBACK, "both", GROUP, PORT, 0.5)])
        self.assertEqual(out, f"alert sent: both -> {GROUP}:{PORT} from {LOOPBACK} (3 datagram(s))\n")

    def test_direct_heartbeat_without_listening(self):
        sent = _Sent(self)
        code, out, err = call_main(["direct-heartbeat"] + self.where() + ["--seconds", "0"])
        self.assertEqual(code, 0, err)
        self.assertEqual(sent.calls, [("heartbeat", LOOPBACK, GROUP, PORT)])
        self.assertEqual(out, f"heartbeat request sent -> {GROUP}:{PORT} from {LOOPBACK}\n")

    def test_replies_are_printed_and_counted(self):
        rport = free_port()

        def wearables_answer():         # two wearables answer on the reply port, as real ones would
            def go():
                send_tcp(rport, device_response(ts=11))
                send_tcp(rport, device_response(ts=12, code=2, heartbeat=heartbeat_payload("wz\x1b[2Jé", 55)))
                send_tcp(rport, device_response(ts=13))
            threading.Thread(target=go, daemon=True).start()

        sent = _Sent(self, reply=wearables_answer)
        code, out, err = call_main(["direct-alert"] + self.where(rport) + ["--wait-acks", "0.5"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(sent.calls), 1)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"alert sent: raw8 -> {GROUP}:{PORT} from {LOOPBACK} (3 datagram(s))")
        # Replies print as they arrive, from another thread: possibly before the next line.
        self.assertIn(f"listening 0.5 s for ACKs on TCP {rport} (a wearable ACKs when its alarm ends, "
                      f"about 5 s after the alert) ...", lines)
        self.assertIn(f"  ACK from {LOOPBACK}: wearable timestamp 11", lines)
        self.assertIn(f"  ACK from {LOOPBACK}: wearable timestamp 13", lines)
        self.assertIn(f"  HEARTBEAT from {LOOPBACK}: device_id=wz?[2Jé battery=55% rssi=-62 dBm uptime=3615 s",
                      lines)
        self.assertEqual(lines[-1], "2 ACK(s) from 1 wearable(s)")

        code, out, err = call_main(["direct-heartbeat"] + self.where(rport) + ["--seconds", "0.5"])
        self.assertEqual(code, 0, err)
        self.assertIn(f"listening 0.5 s for replies on TCP {rport} ...", out.splitlines())
        self.assertEqual(out.splitlines()[-1], "1 HEARTBEAT(s) from 1 wearable(s)")

    def test_default_listening_times(self):
        # direct-alert listens 15 s: a wearable ACKs only when its alarm ends, about 5 s after
        # the alert. direct-heartbeat listens 5 s: heartbeat replies take well under a second.
        # The alert is sent before the wait starts. sleep() is recorded, not slept.
        sent, slept = _Sent(self), []

        def sleep(s):
            slept.append((s, len(sent.calls)))

        clock = SimpleNamespace(time=time.time, monotonic=time.monotonic, sleep=sleep)
        with mock.patch.object(hc, "time", clock):
            code, out, err = call_main(["direct-alert"] + self.where())
            self.assertEqual(code, 0, err)
            self.assertEqual(slept, [(15.0, 1)], "listened 15 s, after the alert was sent")
            lines = out.splitlines()
            self.assertEqual(lines[0], f"alert sent: raw8 -> {GROUP}:{PORT} from {LOOPBACK} (3 datagram(s))")
            self.assertIn("listening 15 s for ACKs", lines[1])
            self.assertEqual(lines[-1], "0 ACK(s) from 0 wearable(s)")
            slept.clear()
            code, out, err = call_main(["direct-heartbeat"] + self.where())
            self.assertEqual(code, 0, err)
            self.assertEqual(slept, [(5.0, 2)])
            self.assertEqual(out.splitlines()[-1], "0 HEARTBEAT(s) from 0 wearable(s)")

    def test_replies_from_another_subnet_are_ignored(self):
        rport = free_port()
        _Sent(self, reply=lambda: send_tcp(rport, device_response()))
        code, out, _ = call_main(["direct-alert", "--egress", "127.0.1.1", "--group", GROUP, "--port", str(PORT),
                                  "--response-port", str(rport), "--wait-acks", "0.5"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[-1], "0 ACK(s) from 0 wearable(s)", "127.0.0.1 is not in 127.0.1.")

    def test_settings_from_the_environment(self):
        sent = _Sent(self)
        os.environ.update(HALOW_EGRESS=" 127.0.0.5 ", HALOW_GROUP=" 239.255.201.8 ", HALOW_PORT=" 15988 ",
                          HALOW_RESPONSE_PORT=str(free_port()))
        self.assertEqual(call_main(["direct-alert", "--wait-acks", "0"])[0], 0)
        self.assertEqual(sent.calls[-1], ("direct", "127.0.0.5", "raw8", "239.255.201.8", 15988, 0.0))
        self.assertEqual(call_main(["direct-heartbeat", "--seconds", "0", "--port", "15989"])[0], 0)
        self.assertEqual(sent.calls[-1], ("heartbeat", "127.0.0.5", "239.255.201.8", 15989), "a flag wins")

    def test_bad_port_settings_exit_2_before_anything_is_sent(self):
        sent = _Sent(self)
        for name in ("HALOW_PORT", "HALOW_RESPONSE_PORT"):
            for value in ("0", "65536", "-1", "50 01", "abc", "5000.0", "0x1389"):
                for cmd in (["direct-alert", "--wait-acks", "0"], ["direct-heartbeat", "--seconds", "0"],
                            ["direct-alert"]):
                    with self.subTest(name=name, value=value, cmd=cmd[0]):
                        with mock.patch.dict(os.environ, {name: value, "HALOW_EGRESS": LOOPBACK}):
                            code, out, err = call_main(cmd)
                        self.assertEqual(code, 2, out + err)
                        self.assertIn(name, err)
        for flag in ("--port", "--response-port"):
            for value in ("0", "65536", "x", ""):
                with self.subTest(flag=flag, value=value):
                    self.assertEqual(call_main(["direct-alert", "--egress", LOOPBACK, flag, value])[0], 2)
        self.assertEqual(sent.calls, [])

    def test_bad_arguments_exit_2_before_anything_is_sent(self):
        sent = _Sent(self)
        for argv in (["direct-alert", "--format", "mp3"], ["direct-alert", "--burst-seconds", "-1"],
                     ["direct-alert", "--wait-acks", "inf"], ["direct-heartbeat", "--seconds", "nan"],
                     ["direct-heartbeat", "--seconds", "-2"]):
            with self.subTest(argv=argv):
                self.assertEqual(call_main(argv + ["--egress", LOOPBACK])[0], 2)
        self.assertEqual(sent.calls, [])

    def test_a_taken_reply_port_exits_2_before_anything_is_sent(self):
        sent = _Sent(self)
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(holder.close)
        holder.bind(("0.0.0.0", 0))
        holder.listen(1)
        rport = holder.getsockname()[1]
        for cmd in (["direct-alert", "--wait-acks", "1"], ["direct-heartbeat", "--seconds", "1"]):
            with self.subTest(cmd=cmd[0]):
                code, out, err = call_main(cmd + ["--egress", LOOPBACK, "--group", GROUP, "--port", str(PORT),
                                                  "--response-port", str(rport)])
                self.assertEqual(code, 2, out + err)
                self.assertIn(f"cannot listen on TCP {rport}", err)
        self.assertEqual(sent.calls, [])

    def test_an_egress_that_cannot_send_exits_2(self):
        for egress, why in (("192.0.2.123", "cannot send from 192.0.2.123"), ("999.1.1.1", "IPv4"),
                            ("localhost", "IPv4")):
            with self.subTest(egress=egress):
                code, out, err = call_main(["direct-alert", "--egress", egress, "--group", GROUP, "--port", str(PORT),
                                            "--wait-acks", "0"])
                self.assertEqual(code, 2, out + err)
                self.assertIn(why, err)
                self.assertEqual(out, "")


class InstalledCommandTests(CleanEnv, unittest.TestCase):
    """The real command (python -m halow_client, and halow-client or the source file)."""

    def test_exit_codes_of_the_real_command(self):
        for label, cmd in _commands():
            with self.subTest(cmd=label):
                r = run(cmd + ["health", "--url", "http://%s:%d" % (LOOPBACK, closed_port()), "--timeout", "5"])
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                r = run(cmd + ["direct-heartbeat", "--seconds", "0"], HALOW_RESPONSE_PORT="99999",
                        HALOW_EGRESS=LOOPBACK, HALOW_GROUP=GROUP, HALOW_PORT=str(PORT))
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("HALOW_RESPONSE_PORT='99999' is not a port number", r.stderr)
                self.assertEqual(r.stdout, "", "nothing was sent")
                r = run(cmd)
                self.assertEqual(r.returncode, 2)
                self.assertIn("usage:", r.stderr)


if __name__ == "__main__":
    unittest.main()
