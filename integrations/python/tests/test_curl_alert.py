# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ClearObject
"""integrations/curl/alert.sh against the HTTP stub of the gateway: the request it sends
(token header, JSON body), its exit codes (0 sent, 1 failed, 2 bad settings) and what it
prints. Skipped on Windows and wherever bash or curl is missing.

Run with the rest: python -m unittest discover -s integrations/python/tests -v
"""
import json
import os
import shutil
import stat
import tempfile
import time
import unittest

from halow_test_support import REPO, TOKEN, _StubCase, closed_port, run

ALERT_SH = os.path.join(REPO, "integrations", "curl", "alert.sh")
BASH, CURL = shutil.which("bash"), shutil.which("curl")
RECORD = {"event": "alert_sent", "ts_ms": 1767225600123, "format": "raw8", "egress": "127.0.0.1", "datagrams": 1}

# Stands in for curl to show how alert.sh calls it: writes its arguments, one per line, and
# the header file it is given to $FAKE_CURL_LOG.*, then answers 200 like the gateway.
FAKE_CURL = r"""#!/usr/bin/env bash
out=""
: > "$FAKE_CURL_LOG.args"
while [ $# -gt 0 ]; do
  printf '%s\n' "$1" >> "$FAKE_CURL_LOG.args"
  case $1 in
    -o) out=$2 ;;
    -H) case $2 in @*) cat "${2#@}" >> "$FAKE_CURL_LOG.headers" ;; esac ;;
  esac
  shift
done
printf '{"event": "alert_sent"}' > "$out"
printf '200'
"""


@unittest.skipIf(os.name == "nt", "alert.sh is a bash script for Linux and macOS")
@unittest.skipUnless(BASH and CURL, "needs bash and curl")
class AlertShTests(_StubCase):
    def sh(self, *args, **env):
        env.setdefault("HALOW_URL", self.url)
        env.setdefault("HALOW_TOKEN", TOKEN)
        env = {k: v for k, v in env.items() if v is not None}
        return run([BASH, ALERT_SH] + list(args), timeout=60, **env)

    def bodies(self):
        return [json.loads(r["body"].decode("utf-8")) for r in self.srv.requests]

    def test_sends_the_alert(self):
        self.route("POST", "/alert", 200, RECORD)
        r = self.sh()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), RECORD)
        self.assertTrue(r.stdout.endswith("}\n"))
        self.assertEqual(r.stderr, "")
        req = self.srv.requests[-1]
        self.assertEqual((req["method"], req["path"]), ("POST", "/alert"))
        self.assertEqual(req["headers"].get("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(req["headers"].get("Content-Type"), "application/json")
        self.assertEqual(self.bodies(), [{"label": "curl"}])

    def test_trailing_slash_and_options(self):
        r = self.sh("zone-3", HALOW_URL=self.url + "/", HALOW_FORMAT="both", HALOW_BURST_SECONDS="1.5")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.srv.requests[-1]["path"], "/alert")
        self.assertEqual(self.bodies(), [{"label": "zone-3", "format": "both", "burst_seconds": 1.5}])

    def test_labels_become_valid_json(self):
        cases = {
            'quote " and backslash \\': 'quote " and backslash \\',
            "line\nbreak\rand\ttab": "line\nbreak\rand\ttab",
            "bell\x07 escape\x1b[2J del\x7f": "bell escape[2J del",            # other control characters dropped
            "zone ü 東 \U0001f6a7": "zone ü 東 \U0001f6a7",
            "\\u0041 stays as written": "\\u0041 stays as written",
            "": "curl",                                                       # empty: the default label
            "-": "-",
        }
        for label, want in cases.items():
            with self.subTest(label=label):
                r = self.sh(label)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.bodies()[-1], {"label": want})

    def test_burst_values_are_valid_json_or_refused(self):
        for value, sent in (("0", 0), ("0.25", 0.25), ("5", 5), ("10", 10), ("2.50", 2.5)):
            with self.subTest(value=value):
                r = self.sh(HALOW_BURST_SECONDS=value)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.bodies()[-1]["burst_seconds"], sent)
        # A leading zero used to go into the body as it is, which is not JSON (05).
        for value in ("05", "00", "00.5", "-1", "1e3", ".5", "5.", "abc", "1 2", "0x5", "+1"):
            with self.subTest(value=value):
                n = len(self.srv.requests)
                r = self.sh(HALOW_BURST_SECONDS=value)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("HALOW_BURST_SECONDS must be a number from 0 to 5", r.stderr)
                self.assertEqual(len(self.srv.requests), n)

    def test_timeout_settings(self):
        # A leading zero used to make bash read the number as octal (08 failed, 010 was 8).
        for value in ("08", "010", "1"):
            with self.subTest(value=value):
                r = self.sh(HALOW_TIMEOUT=value, HALOW_BURST_SECONDS="0")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stderr, "")
        for value in ("abc", "-1", "1.5", "1s", " "):
            with self.subTest(value=value):
                r = self.sh(HALOW_TIMEOUT=value)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("HALOW_TIMEOUT must be a whole number", r.stderr)

    def test_a_gateway_that_does_not_answer(self):
        self.route("POST", "/alert", 200, RECORD, delay=10)
        start = time.monotonic()
        r = self.sh(HALOW_TIMEOUT="1")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertLess(time.monotonic() - start, 8)
        self.assertIn(f"alert.sh: cannot reach the gateway at {self.url}", r.stderr)

    def test_bad_settings_exit_2_and_send_nothing(self):
        cases = [
            ({"HALOW_TOKEN": None}, "set HALOW_TOKEN"),
            ({"HALOW_TOKEN": ""}, "set HALOW_TOKEN"),
            ({"HALOW_URL": "ftp://example.invalid"}, "HALOW_URL must start with http:// or https://"),
            ({"HALOW_URL": "gateway:8080"}, "HALOW_URL must start with http://"),
            ({"HALOW_FORMAT": "mp3"}, "HALOW_FORMAT must be raw8, proto or both"),
            ({"HALOW_FORMAT": "RAW8"}, "HALOW_FORMAT must be raw8, proto or both"),
        ]
        for env, why in cases:
            with self.subTest(env=env):
                r = self.sh(**env)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertTrue(r.stderr.startswith("alert.sh: " + why), r.stderr)
                self.assertEqual(r.stdout, "")
        self.assertEqual(self.srv.requests, [])

    def test_error_statuses_exit_1_with_a_hint(self):
        hints = {
            400: "the gateway rejected the request",
            401: "missing or wrong token: set HALOW_TOKEN to the gateway's token",
            404: "unknown path: is HALOW_URL the halow-alert gateway (http://<host>:8080)?",
            413: "the gateway rejected the request",
            429: "rate limited: the gateway sends at most one alert every 0.5 s; retry shortly",
            500: "the gateway could not send the alert (HaLowLink unplugged or not set up? see GET /health)",
            503: "the gateway cannot send right now (see GET /health)",
            418: "unexpected response",
            302: "unexpected response",
        }
        for status, hint in hints.items():
            with self.subTest(status=status):
                self.route("POST", "/alert", status, {"error": "detail %d" % status})
                r = self.sh()
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertEqual(r.stdout, "")
                lines = r.stderr.splitlines()
                self.assertEqual(lines[0], f"alert.sh: HTTP {status}: {hint}")
                self.assertEqual(json.loads(lines[1]), {"error": "detail %d" % status}, "the body follows")

    def test_wrong_token(self):
        r = self.sh(HALOW_TOKEN="wrong")
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stderr.startswith("alert.sh: HTTP 401: "), r.stderr)

    def test_nothing_listening(self):
        url = "http://127.0.0.1:%d" % closed_port()
        r = self.sh(HALOW_URL=url)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"alert.sh: cannot reach the gateway at {url}", r.stderr)

    def test_the_token_is_not_on_the_curl_command_line(self):
        tmp = tempfile.mkdtemp(prefix="halow-fake-curl-")
        self.addCleanup(shutil.rmtree, tmp, True)
        curl = os.path.join(tmp, "curl")
        with open(curl, "w", encoding="utf-8", newline="\n") as f:
            f.write(FAKE_CURL)
        os.chmod(curl, os.stat(curl).st_mode | stat.S_IXUSR)
        log = os.path.join(tmp, "log")
        secret = "s3cret-token-value"
        r = self.sh("x", HALOW_TOKEN=secret, FAKE_CURL_LOG=log, PATH=tmp + os.pathsep + os.environ.get("PATH", ""))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(log + ".args", encoding="utf-8") as f:
            args = f.read()
        with open(log + ".headers", encoding="utf-8") as f:
            headers = f.read()
        self.assertIn("--noproxy", args)
        self.assertNotIn(secret, args, "the token must not be on curl's command line")
        self.assertEqual(headers, f"Authorization: Bearer {secret}\n")
        self.assertEqual(self.srv.requests, [], "the fake curl answered, not the stub")


if __name__ == "__main__":
    unittest.main()
