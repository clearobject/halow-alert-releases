# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ClearObject
"""GatewayClient against servers on loopback: every documented status and hint, odd bodies,
servers that misbehave (redirect, reset, cut short, garbage, silence), URLs, the token,
settings from the environment, argument checks, and one client shared by several threads.

Run with the rest: python -m unittest discover -s integrations/python/tests -v
"""
import json
import os
import threading
import time
import unittest
from unittest import mock

from halow_test_support import (
    LOOPBACK,
    TOKEN,
    CleanEnv,
    RawServer,
    _StubCase,
    closed_port,
    hc,
    http_response,
)

RECORD = {"event": "alert_sent", "ts_ms": 1767225600123, "format": "raw8", "egress": LOOPBACK, "datagrams": 1}
# The hint each documented status gives, as in halow_client._STATUS_HINTS.
HINTS = {
    400: "the gateway rejected the request",
    401: "missing or wrong token: set HALOW_TOKEN",
    404: "unknown path: is HALOW_URL the halow-alert gateway",
    413: "request body too large",
    429: "rate limited: the gateway sends at most one alert every 0.5 s",
    500: "the gateway could not send the alert (HaLowLink unplugged",
    503: "the gateway cannot send right now (no HaLowLink address",
}


class _Raw(CleanEnv, unittest.TestCase):
    def serve(self, script) -> RawServer:
        srv = RawServer(script)
        self.addCleanup(srv.close)
        return srv

    def answer(self, raw: bytes) -> RawServer:
        """A server that sends these bytes and closes."""
        return self.serve(lambda srv, conn, req: conn.sendall(raw))

    def fails(self, fn, status=None) -> hc.HalowError:
        with self.assertRaises(hc.HalowError) as cm:
            fn()
        self.assertEqual(cm.exception.status, status, str(cm.exception))
        self.assertIsInstance(cm.exception.body, dict)
        return cm.exception


class StatusTests(_StubCase):
    def test_every_documented_status_has_its_hint(self):
        gw = hc.GatewayClient(self.url, token=TOKEN)
        for status, hint in HINTS.items():
            for method, path, call in (("POST", "/alert", gw.alert), ("GET", "/wearables", gw.wearables)):
                with self.subTest(status=status, path=path):
                    self.route(method, path, status, {"error": "detail-%d" % status})
                    e = self.raises(call, status)
                    self.assertTrue(str(e).startswith(f"{method} {path}: HTTP {status}: {hint}"), str(e))
                    self.assertTrue(str(e).endswith(f"(detail-{status})"), str(e))
                    self.assertEqual(e.body, {"error": "detail-%d" % status})

    def test_unexpected_statuses(self):
        gw = hc.GatewayClient(self.url, token=TOKEN)
        for status in (201, 202, 204, 302, 403, 405, 418, 502, 504, 599):
            with self.subTest(status=status):
                self.route("POST", "/alert", status, {})
                e = self.raises(gw.alert, status)
                self.assertEqual(str(e), f"POST /alert: HTTP {status}: unexpected response")

    def test_health_returns_the_body_for_200_and_503_only(self):
        gw = hc.GatewayClient(self.url)
        self.route("GET", "/health", 503, {"ok": False, "error": "listener stopped"})
        self.assertEqual(gw.health(), {"ok": False, "error": "listener stopped"})
        self.route("GET", "/health", 500, {"ok": False})
        self.raises(gw.health, 500)

    def test_detail_that_is_not_a_string(self):
        self.route("POST", "/alert", 400, {"error": {"field": "burst_seconds"}})
        e = self.raises(hc.GatewayClient(self.url, token=TOKEN).alert, 400)
        self.assertIn("({'field': 'burst_seconds'})", str(e))
        self.route("POST", "/alert", 400, {"error": ""})
        e = self.raises(hc.GatewayClient(self.url, token=TOKEN).alert, 400)
        self.assertTrue(str(e).endswith("the gateway rejected the request"), str(e))


class BodyTests(_StubCase):
    def test_empty_and_blank_bodies(self):
        for raw in (b"", b"   ", b"\r\n\t "):
            with self.subTest(raw=raw):
                self.route("GET", "/wearables", 200, raw)
                self.assertEqual(hc.GatewayClient(self.url).wearables(), {})
                self.route("GET", "/wearables", 500, raw)
                self.assertEqual(self.raises(hc.GatewayClient(self.url).wearables, 500).body, {})

    def test_json_that_is_not_an_object(self):
        for value in ([1, 2], "text", 5, 2.5, None, True, []):
            with self.subTest(value=value):
                self.route("GET", "/wearables", 200, json.dumps(value).encode())
                self.assertEqual(hc.GatewayClient(self.url).wearables(), {"value": value})
                self.route("POST", "/alert", 429, json.dumps(value).encode())
                e = self.raises(hc.GatewayClient(self.url, token=TOKEN).alert, 429)
                self.assertEqual(e.body, {"value": value})

    def test_bodies_that_are_not_json(self):
        for raw in (b"<html>busy</html>", b"{", b"\xff\xfe\x00", b"\x00" * 10, b"NaN-ish"):
            with self.subTest(raw=raw):
                self.route("POST", "/alert", 500, raw)
                e = self.raises(hc.GatewayClient(self.url, token=TOKEN).alert, 500)
                self.assertEqual(e.body, {"error": raw.decode("utf-8", "replace")})
        # A health check that is not JSON is not ok.
        self.route("GET", "/health", 200, b"<html>it works</html>")
        self.assertFalse(hc.GatewayClient(self.url).health().get("ok"))

    def test_huge_bodies(self):
        self.route("POST", "/alert", 500, b"x" * (2 << 20))
        e = self.raises(hc.GatewayClient(self.url, token=TOKEN).alert, 500)
        self.assertEqual(e.body, {"error": "x" * 500}, "a body that is not JSON is cut to 500 characters")
        self.assertLess(len(str(e)), 1000)
        big = {"devices": [{"ip": "192.168.12.%d" % (i % 250), "acks": 1} for i in range(40000)], "recent": []}
        self.route("GET", "/wearables", 200, big)
        body = hc.GatewayClient(self.url).wearables()
        self.assertEqual(len(body["devices"]), 40000)
        self.assertEqual(hc.total_acks(body), 40000)

    def test_deeply_nested_json_is_not_a_crash(self):
        # json.loads raises RecursionError (not ValueError) on deep nesting; it used to escape.
        for status in (200, 500):
            with self.subTest(status=status):
                self.route("GET", "/wearables", status, b"[" * 100000)
                try:
                    body = hc.GatewayClient(self.url).wearables()
                except hc.HalowError as e:
                    self.assertEqual(e.status, status)
                    body = e.body
                self.assertEqual(body, {"error": "[" * 500})

    def test_utf8_and_escapes_in_bodies(self):
        self.route("GET", "/wearables", 200, {"devices": [{"ip": "192.168.12.9", "device_id": "wz-ü-東- "}]})
        self.assertEqual(hc.GatewayClient(self.url).wearables()["devices"][0]["device_id"], "wz-ü-東- ")


class MisbehavingServerTests(_Raw):
    def test_reset_in_the_middle_of_the_body(self):
        def script(srv, conn, req):
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{\"ok\":")
            srv.reset(conn)
        e = self.fails(hc.GatewayClient(self.serve(script).url, timeout=5).health)
        self.assertIn("cannot reach the gateway", str(e))

    def test_body_cut_short(self):
        # The server closes normally before the promised Content-Length: http.client raises
        # IncompleteRead, which is not an OSError; it used to escape as is.
        srv = self.answer(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{\"ok\":")
        e = self.fails(hc.GatewayClient(srv.url, timeout=5).health)
        self.assertIn("IncompleteRead", str(e))
        # The same for an error status: the status is kept, the partial body dropped.
        srv = self.answer(b"HTTP/1.1 500 Oops\r\nContent-Length: 100\r\n\r\n{\"error\":")
        e = self.fails(hc.GatewayClient(srv.url, timeout=5).health, 500)
        self.assertEqual(e.body, {})

    def test_not_http(self):
        cases = {
            "a line that is not a status line": b"HELLO THERE\r\n\r\n",
            "binary garbage": bytes(range(256)) * 4,
            "too many headers": b"HTTP/1.1 200 OK\r\n" + b"X-A: b\r\n" * 150 + b"Content-Length: 2\r\n\r\n{}",
            "a header line that is too long": b"HTTP/1.1 200 OK\r\nX-A: " + b"b" * 70000 + b"\r\n\r\n",
            "bad chunked encoding": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n{}\r\n0\r\n\r\n",
            "a status that is not a number": b"HTTP/1.1 abc OK\r\nContent-Length: 2\r\n\r\n{}",
        }
        for name, raw in cases.items():
            with self.subTest(name):
                e = self.fails(hc.GatewayClient(self.answer(raw).url, timeout=5).health)
                self.assertIn("cannot reach the gateway", str(e))
                self.assertNotIn("\n", str(e))

    def test_closes_without_answering(self):
        e = self.fails(hc.GatewayClient(self.answer(b"").url, timeout=5).health)
        self.assertIn("cannot reach the gateway", str(e))

    def test_never_answers(self):
        srv = self.serve(lambda srv, conn, req: srv.release.wait(30))
        start = time.monotonic()
        e = self.fails(hc.GatewayClient(srv.url, timeout=0.3).wearables)
        self.assertLess(time.monotonic() - start, 5)
        self.assertIn("cannot reach the gateway", str(e))
        self.assertEqual(len(srv.requests), 1)

    def test_tls_to_a_plain_http_server(self):
        # The server answers in plain HTTP at once, without waiting for a request.
        srv = RawServer(lambda srv, conn, req: conn.sendall(http_response(400, b"plain http")), read_first=False)
        self.addCleanup(srv.close)
        e = self.fails(hc.GatewayClient(srv.url.replace("http://", "https://"), timeout=5).health)
        self.assertIn("cannot reach the gateway at https://", str(e))


class RedirectTests(_Raw):
    """The gateway never redirects. A 3xx is an error: following it used to turn POST /alert
    into a GET that 'succeeded', and to carry the token to the new address."""

    def setUp(self):
        super().setUp()
        self.elsewhere = self.answer(http_response(200, b'{"ok": true, "event": "alert_sent"}'))

    def redirecting(self, status, location):
        return self.answer(http_response(status, b"", headers=["Location: " + location]))

    def test_redirects_are_not_followed(self):
        for status in (301, 302, 303, 307, 308):
            for target in (self.elsewhere.url + "/alert", "/health"):
                with self.subTest(status=status, target=target):
                    srv = self.redirecting(status, target)
                    gw = hc.GatewayClient(srv.url, token=TOKEN, timeout=5)
                    e = self.fails(lambda gw=gw: gw.alert(label="x"), status)
                    self.assertIn(f"POST /alert: HTTP {status}: unexpected response", str(e))
                    self.fails(gw.health, status)
                    self.assertEqual(len(srv.requests), 2, "one request each, no second one after the redirect")
        self.assertEqual(self.elsewhere.requests, [], "the token never went to the redirect target")


class UrlTests(CleanEnv, unittest.TestCase):
    def test_trailing_slashes_and_a_path_prefix(self):
        seen = []
        srv = RawServer(lambda s, conn, req: (seen.append(req.split(b" ")[1]), conn.sendall(http_response(200, b"{}"))))
        self.addCleanup(srv.close)
        for url, path in ((srv.url + "/", b"/health"), (srv.url + "///", b"/health"),
                          (srv.url + "/gw/", b"/gw/health"), (srv.url + "/gw", b"/gw/health")):
            with self.subTest(url=url):
                gw = hc.GatewayClient(url, timeout=5)
                self.assertFalse(gw.url.endswith("/"))
                gw.health()
                self.assertEqual(seen[-1], path)

    def test_schemes_that_are_refused(self):
        for url in ("ftp://example.invalid", "file:///etc/passwd", "127.0.0.1:8080", "//127.0.0.1:8080",
                    "http:/127.0.0.1", " http://127.0.0.1", "HTTP://127.0.0.1", "http://", "https:///",
                    "javascript:alert(1)", "gopher://127.0.0.1"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "must start with http:// or https://"):
                hc.GatewayClient(url)

    def test_https_is_accepted(self):
        self.assertEqual(hc.GatewayClient("https://gateway.invalid:8443/").url, "https://gateway.invalid:8443")

    def test_a_port_that_is_not_a_number(self):
        # http.client raises InvalidURL (an HTTPException); it used to escape as is.
        with self.assertRaises(hc.HalowError) as cm:
            hc.GatewayClient("http://127.0.0.1:abc", timeout=5).health()
        e = cm.exception
        self.assertIsNone(e.status)
        self.assertIn("nonnumeric port", str(e))

    def test_nothing_listening(self):
        with self.assertRaises(hc.HalowError) as cm:
            hc.GatewayClient("http://%s:%d" % (LOOPBACK, closed_port()), timeout=5).alert()
        self.assertIsNone(cm.exception.status)
        self.assertEqual(cm.exception.body, {})


class SettingsTests(_StubCase):
    def last(self):
        return self.srv.requests[-1]

    def test_token_whitespace_is_trimmed(self):
        for token in ("  %s  " % TOKEN, "\t%s\n" % TOKEN, "\r\n%s\r\n" % TOKEN):
            with self.subTest(token=token):
                gw = hc.GatewayClient(self.url, token=token)
                self.assertEqual(gw.token, TOKEN)
                self.assertEqual(gw.alert()["event"], "alert_sent")
                self.assertEqual(self.last()["headers"].get("Authorization"), "Bearer " + TOKEN)

    def test_token_with_a_line_break_inside_never_reaches_the_wire(self):
        for token in (TOKEN + "\r\nX-Injected: 1", TOKEN + "\nX-Injected: 1", "a\rb"):
            with self.subTest(token=token):
                with self.assertRaises(hc.HalowError) as cm:
                    hc.GatewayClient(self.url, token=token).alert()
                self.assertIsNone(cm.exception.status)
        self.assertEqual(self.srv.requests, [])

    def test_token_that_latin1_cannot_carry(self):
        with self.assertRaises(hc.HalowError):
            hc.GatewayClient(self.url, token="t€").alert()
        self.assertEqual(self.srv.requests, [])

    def test_token_is_sent_with_post_only(self):
        gw = hc.GatewayClient(self.url, token=TOKEN)
        gw.health()
        gw.wearables()
        gw.alert()
        auth = [(r["method"], r["headers"].get("Authorization")) for r in self.srv.requests]
        self.assertEqual(auth, [("GET", None), ("GET", None), ("POST", "Bearer " + TOKEN)])
        self.assertIsNone(self.srv.requests[0]["headers"].get("Content-Type"), "a GET has no body")

    def test_environment_defaults_and_explicit_arguments(self):
        os.environ.update(HALOW_URL="  %s/  " % self.url, HALOW_TOKEN=" %s " % TOKEN)
        gw = hc.GatewayClient()
        self.assertEqual((gw.url, gw.token, gw.timeout), (self.url, TOKEN, 10.0))
        # An explicit URL wins; an empty one falls back to HALOW_URL.
        self.assertEqual(hc.GatewayClient("http://gateway.invalid:8080").url, "http://gateway.invalid:8080")
        self.assertEqual(hc.GatewayClient("").url, self.url)
        # An explicit empty token wins over HALOW_TOKEN: no Authorization header.
        self.raises(lambda: hc.GatewayClient(self.url, token="").alert(), 401)
        self.assertNotIn("Authorization", self.last()["headers"])
        os.environ["HALOW_TOKEN"] = "   "
        self.assertEqual(hc.GatewayClient(self.url).token, "")
        os.environ["HALOW_URL"] = "   "
        self.assertEqual(hc.GatewayClient().url, hc.DEFAULT_URL)

    def test_the_environment_is_read_when_the_client_is_made(self):
        os.environ["HALOW_TOKEN"] = TOKEN
        gw = hc.GatewayClient(self.url)
        os.environ["HALOW_TOKEN"] = "changed"
        self.assertEqual(gw.alert()["event"], "alert_sent")

    def test_non_ascii_labels(self):
        gw = hc.GatewayClient(self.url, token=TOKEN)
        for label in ("zone ü", "東ゲート 3", "\U0001f6a7 work zone", "tab\there", 'quote " and \\', "\ud800", 42, ""):
            with self.subTest(label=label):
                gw.alert(label=label)
                body = self.last()["body"]
                self.assertTrue(body.isascii(), body)
                self.assertEqual(json.loads(body.decode("utf-8")), {"label": str(label)})
                self.assertEqual(int(self.last()["headers"]["Content-Length"]), len(body))

    def test_format_is_normalized(self):
        gw = hc.GatewayClient(self.url, token=TOKEN)
        for fmt, sent in (("RAW8", "raw8"), ("Proto", "proto"), ("both", "both")):
            gw.alert(fmt=fmt)
            self.assertEqual(json.loads(self.last()["body"].decode()), {"format": sent})

    def test_arguments_are_checked_before_any_request(self):
        gw = hc.GatewayClient(self.url, token=TOKEN)
        bad = [{"fmt": f} for f in ("", "mp3", " raw8", "raw8 ", 8, "raw 8")]
        bad += [{"burst_seconds": b} for b in (float("nan"), float("inf"), float("-inf"), -0.001, -1, "abc", "")]
        for kwargs in bad:
            with self.subTest(**{k: repr(v) for k, v in kwargs.items()}), self.assertRaises(ValueError):
                gw.alert(**kwargs)
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                hc.GatewayClient(self.url, timeout=timeout)
        with self.assertRaises((TypeError, ValueError)):
            hc.GatewayClient(self.url, timeout="soon")
        self.assertEqual(self.srv.requests, [])

    def test_burst_seconds_is_sent_as_given(self):
        gw = hc.GatewayClient(self.url, token=TOKEN)
        for given, sent in ((0, 0.0), ("1.5", 1.5), (5, 5.0), (12, 12.0), (True, 1.0)):
            gw.alert(burst_seconds=given)
            self.assertEqual(json.loads(self.last()["body"].decode()), {"burst_seconds": sent})


class TimeoutTests(_StubCase):
    def test_a_burst_extends_the_wait(self):
        # The gateway answers POST /alert when the burst ends.
        self.route("POST", "/alert", 200, RECORD, delay=1.2)
        gw = hc.GatewayClient(self.url, token=TOKEN, timeout=0.4)
        start = time.monotonic()
        e = self.raises(gw.alert, None)
        self.assertIn("cannot reach the gateway", str(e))
        self.assertLess(time.monotonic() - start, 1.1)
        self.assertEqual(gw.alert(burst_seconds=2)["event"], "alert_sent")

    def test_the_extension_is_capped_at_5_seconds(self):
        gw = hc.GatewayClient(self.url, token=TOKEN, timeout=3)
        for burst, wait in ((None, 3.0), (0, 3.0), (0.25, 3.25), (5, 8.0), (60, 8.0)):
            with self.subTest(burst=burst), mock.patch.object(gw, "_call", return_value={}) as call:
                gw.alert(burst_seconds=burst)
                self.assertEqual(call.call_args[1]["timeout"], wait)

    def test_get_requests_use_the_client_timeout(self):
        self.route("GET", "/wearables", 200, {"devices": []}, delay=1.0)
        start = time.monotonic()
        self.raises(hc.GatewayClient(self.url, timeout=0.3).wearables, None)
        self.assertLess(time.monotonic() - start, 0.95)


class TotalAcksTests(unittest.TestCase):
    def test_odd_bodies(self):
        cases = [
            ({}, 0), ({"devices": []}, 0), ({"recent": [{"acks": 5}]}, 0), ({"value": [{"acks": 5}]}, 0),
            ({"error": "x"}, 0), ({"devices": [{}]}, 0), ({"devices": [{"ip": "a", "heartbeats": 9}]}, 0),
            ({"devices": [{"acks": "3"}, {"acks": 2.0}, {"acks": True}]}, 6),
            ({"devices": [{"acks": 2**70}, {"acks": 1}]}, 2**70 + 1),
        ]
        for body, want in cases:
            with self.subTest(body=body):
                self.assertEqual(hc.total_acks(body), want)


class ThreadSafetyTests(_StubCase):
    def test_one_client_from_several_threads(self):
        gw = hc.GatewayClient(self.url, token=TOKEN)
        errors, results, start = [], [], threading.Barrier(8)

        def worker(n):
            try:
                start.wait(5)
                for i in range(10):
                    if i % 3 == 0:
                        results.append(gw.alert(label="t%d-%d" % (n, i))["event"])
                    elif i % 3 == 1:
                        results.append(gw.health()["ok"])
                    else:
                        results.append(hc.total_acks(gw.wearables()))
            except Exception as e:          # reported below, on the test thread
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 80)
        self.assertEqual(results.count("alert_sent"), 32)
        posts = [r for r in self.srv.requests if r["method"] == "POST"]
        self.assertEqual(len(posts), 32)
        self.assertTrue(all(r["headers"].get("Authorization") == "Bearer " + TOKEN for r in posts))
        labels = sorted(json.loads(r["body"].decode())["label"] for r in posts)
        self.assertEqual(labels, sorted("t%d-%d" % (n, i) for n in range(8) for i in (0, 3, 6, 9)))


if __name__ == "__main__":
    unittest.main()
