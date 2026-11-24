# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ClearObject
"""Direct mode without hardware: ResponseListener against wearable replies (and abuse) on
loopback TCP, send_direct and send_heartbeat_request with a recording socket and a fake
clock, and the multicast socket itself.

Only the last class needs loopback multicast (skipped without it, or failed with
HALOW_TEST_REQUIRE_MULTICAST=1). Run with the rest:
python -m unittest discover -s integrations/python/tests -v
"""
import contextlib
import errno
import io
import os
import socket
import struct
import threading
import time
import unittest
from decimal import Decimal
from unittest import mock

from halow_test_support import (
    LOOPBACK,
    CleanEnv,
    MulticastMixin,
    device_response,
    free_port,
    hc,
    heartbeat_payload,
    pb_uint,
    pick_group,
    rng,
    send_tcp,
    wait_until,
)

ACK = device_response(ts=1767225600123)
HEARTBEAT = device_response(ts=42, code=2, heartbeat=heartbeat_payload())
TAKEN_HINT = "(is the gateway service or another listener running on this machine?)"
TAKEN_HINT_WINDOWS = ("(is the gateway service or another listener running on this machine? On Windows the port "
                      "can also stay taken for up to a few minutes after a listener on it stopped, until the "
                      "reply connections it had have fully closed)")


class _Accepts:
    """Stands in for a listener's socket. accept() first raises each error in `errors`, then
    hands over to the real socket. With winsock=True a connection that the client reset before
    it was accepted fails to accept with ConnectionResetError, as on Windows. `calls` holds the
    time of every accept()."""

    def __init__(self, real, errors=(), winsock=False):
        self.real, self.errors, self.winsock = real, list(errors), winsock
        self.calls, self.resets = [], 0

    def accept(self):
        self.calls.append(time.monotonic())
        if self.errors:
            raise self.errors.pop(0)
        conn, addr = self.real.accept()
        if self.winsock and conn.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == errno.ECONNRESET:
            conn.close()
            self.resets += 1
            raise ConnectionResetError(errno.ECONNRESET, "the client reset the connection before it was accepted")
        return conn, addr

    def __getattr__(self, name):                    # shutdown(), close(), ...
        return getattr(self.real, name)


class _ListenerCase(CleanEnv, unittest.TestCase):
    def listen(self, **kwargs) -> hc.ResponseListener:
        """A started ResponseListener on 127.0.0.1 and a free port, stopped after the test."""
        kwargs.setdefault("bind", LOOPBACK)
        rl = hc.ResponseListener(kwargs.pop("port", None) or free_port(), **kwargs).start()
        self.addCleanup(rl.stop)
        return rl

    def connect(self, rl) -> socket.socket:
        c = socket.create_connection((LOOPBACK, rl.port), timeout=5)
        self.addCleanup(c.close)
        return c

    def accept_through(self, rl, **kwargs) -> _Accepts:
        """Puts an _Accepts in front of the listener's socket; returns once the accept loop uses it."""
        accepts = _Accepts(rl._srv, **kwargs)
        rl._srv = accepts
        self.assertTrue(wait_until(lambda: accepts.calls, 5), "the accept loop runs")
        return accepts

    @staticmethod
    def closed_by_peer(c, timeout=5.0) -> bool:
        """True when the other end closes (or resets) the connection within `timeout`."""
        c.settimeout(timeout)
        try:
            return c.recv(1) == b""
        except socket.timeout:
            return False
        except OSError:
            return True


class ResponseListenerTests(_ListenerCase):
    def test_acks_and_heartbeats_are_recorded(self):
        seen = []
        rl = self.listen(on_response=seen.append)
        before = int(time.time() * 1000)
        send_tcp(rl.port, ACK)
        send_tcp(rl.port, HEARTBEAT)
        self.assertTrue(wait_until(lambda: len(rl.responses) == 2), rl.responses)
        ack = rl.wait_for("ACK", 0)
        hb = rl.wait_for("HEARTBEAT", 0)
        self.assertEqual({k: ack[k] for k in ("timestamp_ms", "code", "ack_success", "ip")},
                         {"timestamp_ms": 1767225600123, "code": "ACK", "ack_success": True, "ip": LOOPBACK})
        self.assertEqual(hb["heartbeat"]["device_id"], "wz-07")
        self.assertGreaterEqual(ack["received_ms"], before)
        self.assertTrue(wait_until(lambda: len(seen) == 2))
        self.assertIs(seen[0], rl.responses[0], "on_response gets the same dict")

    def test_replies_that_are_not_ack_or_heartbeat_are_dropped(self):
        rl = self.listen()
        for data in (device_response(code=0, ack=None), device_response(code=3, ack=None), b"", b"\xff\xff",
                     b"\x08", device_response()[:-1], b"GET / HTTP/1.1\r\n\r\n"):
            send_tcp(rl.port, data)
        send_tcp(rl.port, ACK)                         # a good one afterwards still counts
        self.assertIsNotNone(rl.wait_for("ACK", 5))
        self.assertEqual(len(rl.responses), 1, rl.responses)

    def test_allow_prefix_rejects_other_addresses(self):
        rl = self.listen(allow_prefix="192.0.2.")          # 127.0.0.1 is not in it
        c = self.connect(rl)
        c.sendall(ACK)
        self.assertTrue(self.closed_by_peer(c), "closed at once")
        ok = self.listen(allow_prefix=hc.subnet_prefix(LOOPBACK))
        send_tcp(ok.port, ACK)
        self.assertIsNotNone(ok.wait_for("ACK", 5))
        self.assertEqual(rl.responses, [])

    def test_connection_cap(self):
        with mock.patch.object(hc, "MAX_RESPONSE_CONNS", 2), mock.patch.object(hc, "RESPONSE_DEADLINE_S", 10.0):
            rl = self.listen()
            idle = [self.connect(rl), self.connect(rl)]     # each holds one of the 2 slots
            third = self.connect(rl)
            third.sendall(device_response(ts=3))
            start = time.monotonic()
            self.assertTrue(self.closed_by_peer(third), "a third connection is closed at once")
            self.assertLess(time.monotonic() - start, 5)
            for c in idle:
                c.close()                                   # frees both slots
            # Once the slots are free, replies are taken again.
            def delivered():
                with contextlib.suppress(OSError):
                    send_tcp(rl.port, ACK)
                return rl.wait_for("ACK", 0.2) is not None
            self.assertTrue(wait_until(delivered, 10, 0.05))
            self.assertEqual({r["timestamp_ms"] for r in rl.responses}, {1767225600123}, "the third was not read")

    def test_per_connection_deadline(self):
        with mock.patch.object(hc, "RESPONSE_DEADLINE_S", 0.4):
            rl = self.listen()
            # A whole reply on a connection that stays open: decoded at the deadline.
            c = self.connect(rl)
            start = time.monotonic()
            c.sendall(ACK)
            self.assertIsNotNone(rl.wait_for("ACK", 5))
            self.assertGreaterEqual(time.monotonic() - start, 0.3)
            self.assertTrue(self.closed_by_peer(c))

    def test_slowloris(self):
        with mock.patch.object(hc, "RESPONSE_DEADLINE_S", 0.4), mock.patch.object(hc, "MAX_RESPONSE_CONNS", 2):
            rl = self.listen()
            slow = [self.connect(rl), self.connect(rl)]
            for c in slow:
                c.sendall(ACK[:5])                          # half a reply, then nothing
            start = time.monotonic()
            for c in slow:
                self.assertTrue(self.closed_by_peer(c), "cut off at the deadline")
            self.assertLess(time.monotonic() - start, 3)
            self.assertEqual(rl.responses, [], "half a reply is not a reply")
            send_tcp(rl.port, ACK)
            self.assertIsNotNone(rl.wait_for("ACK", 5), "the slots are free again")

    def test_byte_cap(self):
        rl = self.listen()
        cap = hc.MAX_RESPONSE_BYTES
        # An ACK padded with an unknown field to exactly the cap is read whole; one byte more is cut.
        def padded(ts, pad):                     # field 15, 2-byte length, `pad` zero bytes
            return device_response(ts=ts) + b"\x7a" + bytes([0x80 | (pad & 0x7F), pad >> 7]) + b"\x00" * pad

        pad = cap - len(device_response(ts=7)) - 3
        exact, over = padded(7, pad), padded(8, pad + 1)
        self.assertEqual((len(exact), len(over)), (cap, cap + 1))
        send_tcp(rl.port, over)
        send_tcp(rl.port, exact)
        send_tcp(rl.port, device_response(ts=9))
        self.assertTrue(wait_until(lambda: {7, 9} <= {r["timestamp_ms"] for r in rl.responses}), rl.responses)
        self.assertNotIn(8, [r["timestamp_ms"] for r in rl.responses], "one byte over the cap is cut off")

    def test_a_flood_is_cut_at_the_byte_cap(self):
        rl = self.listen()
        c = self.connect(rl)
        with contextlib.suppress(OSError):
            for _ in range(256):                            # up to 4 MB; the listener stops at 8 KiB
                c.sendall(ACK * 1600)
        self.assertTrue(self.closed_by_peer(c))
        self.assertEqual(rl.responses, [])

    def test_reset_and_garbage(self):
        rl = self.listen()
        # On every platform, as on Windows: a connection reset before the listener accepted it
        # makes accept() fail. Each such failure used to stop the listener for 0.5 s.
        self.accept_through(rl, winsock=True)
        r = rng("listener-garbage")
        for i in range(30):
            c = socket.create_connection((LOOPBACK, rl.port), timeout=5)
            c.sendall(ACK[:4] if i % 2 else bytes(r.getrandbits(8) for _ in range(r.randint(1, 64))))
            c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                         struct.pack("HH", 1, 0) if os.name == "nt" else struct.pack("ii", 1, 0))
            c.close()                                       # a reset, not a normal close
        send_tcp(rl.port, ACK)
        self.assertIsNotNone(rl.wait_for("ACK", 5), "the listener keeps working")
        self.assertTrue(all(x["code"] in ("ACK", "HEARTBEAT") for x in rl.responses))

    def test_clients_gone_before_accept_cost_no_time(self):
        rl = self.listen()
        gone = [ConnectionResetError(errno.ECONNRESET, "reset before accept (WSAECONNRESET on Windows)"),
                ConnectionAbortedError(errno.ECONNABORTED, "aborted before accept"),
                OSError(errno.EPROTO, "protocol error on the pending connection (Linux)")] * 4
        accepts = self.accept_through(rl, errors=gone)
        send_tcp(rl.port, ACK)
        # 1.5 s: losing any one of the three kinds (4 x 0.5 s back-off) would miss it.
        self.assertIsNotNone(rl.wait_for("ACK", 1.5), "accepted straight after them, not 0.5 s later for each")
        self.assertEqual(accepts.errors, [])

    def test_a_listener_error_still_backs_off(self):
        # An error of the listener itself (no file descriptors left, ...) is retried after 0.5 s,
        # so that the accept loop does not spin.
        rl = self.listen()
        accepts = self.accept_through(rl, errors=[OSError(errno.EMFILE, "Too many open files")])
        send_tcp(rl.port, ACK)
        self.assertIsNotNone(rl.wait_for("ACK", 5), "the listener keeps working")
        self.assertGreaterEqual(accepts.calls[1] - accepts.calls[0], 0.45)

    def test_on_response_that_raises(self):
        calls = []

        def boom(reply):
            calls.append(reply)
            raise RuntimeError("callback broke")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rl = self.listen(on_response=boom)
            send_tcp(rl.port, ACK)
            send_tcp(rl.port, HEARTBEAT)
            self.assertTrue(wait_until(lambda: err.getvalue().count("on_response failed") == 2), err.getvalue())
            rl.stop()
        self.assertEqual(len(rl.responses), 2, "the replies are kept")
        self.assertEqual(err.getvalue().count("halow_client: on_response failed: callback broke"), 2)

    def test_wait_for(self):
        rl = self.listen()
        self.assertIsNone(rl.wait_for("ACK", 0))
        start = time.monotonic()
        self.assertIsNone(rl.wait_for("ACK", 0.3))
        self.assertGreaterEqual(time.monotonic() - start, 0.25, "it waits the whole timeout")
        send_tcp(rl.port, HEARTBEAT)
        self.assertIsNotNone(rl.wait_for("HEARTBEAT", 5))
        self.assertIsNone(rl.wait_for("ACK", 0), "a heartbeat is not an ACK")
        # It wakes up when a reply arrives, well before its timeout.
        timer = threading.Timer(0.2, send_tcp, (rl.port, device_response(ts=1)))
        timer.start()
        self.addCleanup(timer.cancel)
        start = time.monotonic()
        first = rl.wait_for("ACK", 10)
        self.assertEqual(first["timestamp_ms"], 1)
        self.assertLess(time.monotonic() - start, 5)
        # after_ms: only replies received at or after it (Unix ms).
        self.assertIs(rl.wait_for("ACK", 0, after_ms=first["received_ms"]), first)
        cutoff = first["received_ms"] + 1
        self.assertIsNone(rl.wait_for("ACK", 0.1, after_ms=cutoff))
        self.assertTrue(wait_until(lambda: time.time() * 1000 >= cutoff + 1))
        send_tcp(rl.port, device_response(ts=2))
        self.assertEqual(rl.wait_for("ACK", 5, after_ms=cutoff)["timestamp_ms"], 2)
        self.assertEqual(rl.wait_for("ACK", 0)["timestamp_ms"], 1, "the first one that matches")
        self.assertIsNone(rl.wait_for("UNKNOWN", 0), "UNKNOWN replies are never kept")

    def test_history_keeps_the_last_1000(self):
        count = []
        rl = self.listen(on_response=count.append)
        sent = 0
        for batch in range(0, 1005, 25):
            for i in range(batch, min(batch + 25, 1005)):
                send_tcp(rl.port, device_response(ts=i + 1))
                sent += 1
            self.assertTrue(wait_until(lambda n=sent: len(count) == n, 10), (len(count), sent))
        # The listener keeps the last 1000 replies in the order they ARRIVED. Replies on
        # concurrent connections can arrive out of timestamp order, so the 5 that drop out
        # are 5 of the first batch, not necessarily timestamps 1-5 (seen on Python 3.8).
        kept = [r["timestamp_ms"] for r in rl.responses]
        self.assertEqual(len(kept), 1000)
        self.assertEqual(len(set(kept)), 1000)
        dropped = set(range(1, 1006)) - set(kept)
        self.assertEqual(len(dropped), 5, sorted(dropped))
        self.assertTrue(all(ts <= 25 for ts in dropped), sorted(dropped))

    def test_stop_frees_the_port_at_once(self):
        # stop() used to return while the accept loop still held the socket, so listening on
        # the same port again right away failed ("cannot listen ... address in use").
        # Every reply connection here has ended before stop(): on Windows one still open could
        # keep the port taken for a while (see stop()).
        port = free_port()
        for _ in range(5):
            # Strictest form: a plain socket, without SO_REUSEADDR, and no connections before.
            hc.ResponseListener(port, bind=LOOPBACK).start().stop()
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((LOOPBACK, port))
                s.listen(1)
        for _ in range(3):
            rl = hc.ResponseListener(port, bind=LOOPBACK).start()
            send_tcp(port, ACK)
            self.assertIsNotNone(rl.wait_for("ACK", 5))
            rl.stop()
        rl = hc.ResponseListener(port, bind=LOOPBACK).start()
        self.addCleanup(rl.stop)
        send_tcp(port, ACK)
        self.assertIsNotNone(rl.wait_for("ACK", 5))

    def test_stop_ends_the_accept_thread(self):
        before = {t.ident for t in threading.enumerate() if t.name == "halow-replies"}
        rl = self.listen()
        rl.stop()
        rl.stop()                                           # twice is fine
        left = [t for t in threading.enumerate() if t.name == "halow-replies" and t.ident not in before]
        self.assertEqual(left, [])

    def test_a_stopped_listener_can_start_again(self):
        # start() after stop() used to return a listener whose accept loop had already ended.
        rl = self.listen()
        send_tcp(rl.port, device_response(ts=1))
        self.assertIsNotNone(rl.wait_for("ACK", 5))
        rl.stop()
        rl.start()
        send_tcp(rl.port, device_response(ts=2))
        self.assertTrue(wait_until(lambda: len(rl.responses) == 2), rl.responses)
        rl.stop()
        with rl:                                            # and as a context manager
            send_tcp(rl.port, device_response(ts=3))
            self.assertTrue(wait_until(lambda: len(rl.responses) == 3), rl.responses)

    def test_a_taken_port(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(holder.close)
        holder.bind((LOOPBACK, 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        def accept_threads():
            return sum(1 for t in threading.enumerate() if t.name == "halow-replies")

        threads = accept_threads()
        with self.assertRaises(OSError) as cm:
            hc.ResponseListener(port, bind=LOOPBACK).start()
        msg = str(cm.exception)
        self.assertTrue(msg.startswith(f"cannot listen on TCP {port} for wearable replies: "), msg)
        windows = hasattr(socket, "SO_EXCLUSIVEADDRUSE")
        self.assertTrue(msg.endswith(TAKEN_HINT_WINDOWS if windows else TAKEN_HINT), msg)
        self.assertEqual(accept_threads(), threads, "no thread was started")
        # Where the listener owns its port exclusively (Windows), the port can stay taken for a
        # while after a listener stopped with replies still open (see stop()): the error says so.
        for exclusive, hint in ((True, TAKEN_HINT_WINDOWS), (False, TAKEN_HINT)):
            with self.subTest(exclusive=exclusive), mock.patch.object(hc, "_exclusive_port", lambda s, x=exclusive: x):
                with self.assertRaises(OSError) as cm:
                    hc.ResponseListener(port, bind=LOOPBACK).start()
                self.assertTrue(str(cm.exception).endswith(hint), str(cm.exception))
        # Two listeners cannot share a port either.
        rl = self.listen()
        with self.assertRaisesRegex(OSError, "cannot listen on TCP %d" % rl.port):
            hc.ResponseListener(rl.port, bind=LOOPBACK).start()

    def test_context_manager_stops_on_error(self):
        port = free_port()
        with self.assertRaisesRegex(RuntimeError, "inside"):
            with hc.ResponseListener(port, bind=LOOPBACK):
                raise RuntimeError("inside")
        hc.ResponseListener(port, bind=LOOPBACK).start().stop()      # the port is free again


class _Recorder:
    """Stands in for the UDP socket that _mcast_socket returns."""

    def __init__(self, fail=None):
        self.sent, self.closed, self.fail = [], False, fail

    def sendto(self, data, addr):
        if self.fail:
            raise self.fail
        self.sent.append((bytes(data), addr))

    def close(self):
        self.closed = True


class _Clock:
    """Stands in for the time module in halow_client: sleep() moves the clock on at once."""

    def __init__(self, wall=1767225600.125, mono=1000.0):
        self.wall0, self.mono0, self.mono, self.sleeps = wall, mono, mono, []

    def time(self):
        return self.wall0 + (self.mono - self.mono0)

    def monotonic(self):
        return self.mono

    def sleep(self, s):
        self.sleeps.append(s)
        self.mono += s


class SendTests(CleanEnv, unittest.TestCase):
    GROUP, PORT = "239.255.200.9", 15999            # never opened: the socket is a recorder

    def setUp(self):
        super().setUp()
        self.clock = _Clock()
        self.sockets, self.egresses, self.send_error = [], [], None

        def factory(egress, ttl=hc.TTL):
            self.egresses.append((egress, ttl))
            s = _Recorder(self.send_error)
            self.sockets.append(s)
            return s

        for p in (mock.patch.object(hc, "_mcast_socket", factory), mock.patch.object(hc, "time", self.clock)):
            p.start()
            self.addCleanup(p.stop)

    def send(self, **kw):
        kw.setdefault("egress", LOOPBACK)
        return hc.send_direct(group=self.GROUP, port=self.PORT, **kw)

    def payloads(self):
        return [d for d, _ in self.sockets[-1].sent]

    def test_formats(self):
        raw8 = struct.pack("<d", self.clock.time())
        proto = hc.encode_gateway_message(1767225600125, hc.CMD_ALERT)
        for fmt, want, name in (("raw8", [raw8], "raw8"), ("proto", [proto], "proto"),
                                ("both", [raw8, proto], "both"), ("BOTH", [raw8, proto], "both"),
                                ("", [raw8], "raw8"), (None, [raw8], "raw8")):
            with self.subTest(fmt=fmt):
                rec = self.send(fmt=fmt)
                self.assertEqual(self.payloads(), want)
                self.assertEqual(rec, {"event": "alert_sent", "ts_ms": 1767225600125, "format": name,
                                       "group": f"{self.GROUP}:{self.PORT}", "egress": LOOPBACK,
                                       "datagrams": len(want)})
                self.assertTrue(all(addr == (self.GROUP, self.PORT) for _, addr in self.sockets[-1].sent))
                self.assertTrue(self.sockets[-1].closed)
                self.assertEqual(self.clock.sleeps, [])

    def test_ttl_and_egress(self):
        self.send(ttl=1)
        self.assertEqual(self.egresses[-1], (LOOPBACK, 1))
        os.environ["HALOW_EGRESS"] = " 127.0.0.9 "
        self.assertEqual(hc.send_direct(group=self.GROUP, port=self.PORT)["egress"], "127.0.0.9")
        self.assertEqual(self.send(egress="127.0.0.2")["egress"], "127.0.0.2", "an explicit egress wins")
        del os.environ["HALOW_EGRESS"]
        self.assertEqual(hc.send_direct(group=self.GROUP, port=self.PORT)["egress"], hc.DEFAULT_EGRESS)

    def test_burst_repeats_the_same_bytes(self):
        # 1 s every 125 ms: the first send, then one after each of 7 sleeps (the 8th would end
        # at or after the burst's end).
        rec = self.send(fmt="both", burst_seconds=1.0, burst_interval_ms=125)
        sent = self.payloads()
        self.assertEqual(rec["datagrams"], 16)
        self.assertEqual(len(sent), 16)
        self.assertEqual(sent[0::2], [sent[0]] * 8, "every raw8 datagram is identical")
        self.assertEqual(sent[1::2], [sent[1]] * 8, "every proto datagram is identical")
        self.assertEqual(self.clock.sleeps, [0.125] * 7)

    def test_burst_is_capped_at_5_seconds(self):
        for burst in (5, 99, float("inf")):
            with self.subTest(burst=burst):
                self.clock.sleeps.clear()
                self.assertEqual(self.send(burst_seconds=burst, burst_interval_ms=125)["datagrams"], 40)
                self.assertAlmostEqual(sum(self.clock.sleeps), 4.875)

    def test_burst_interval_has_a_floor(self):
        for interval in (0, 1, -50, 20):
            with self.subTest(interval=interval):
                self.clock.sleeps.clear()
                n = self.send(burst_seconds=0.5, burst_interval_ms=interval)["datagrams"]
                self.assertEqual(set(self.clock.sleeps), {hc.MIN_INTERVAL_S})
                self.assertEqual(n, 1 + len(self.clock.sleeps))
                self.assertIn(len(self.clock.sleeps), (24, 25), "0.5 s every 20 ms (24, or 25 by rounding)")

    def test_no_burst(self):
        for burst in (0, 0.0, -1, -1e9, float("nan"), float("-inf"), False):
            with self.subTest(burst=burst):
                self.assertEqual(self.send(fmt="both", burst_seconds=burst)["datagrams"], 2)
                self.assertEqual(self.clock.sleeps, [])

    def test_no_burst_ignores_the_interval(self):
        # Without a burst the interval is not used, so it is not checked either: a placeholder
        # such as None used to fail the call, and the alert was not sent.
        for burst in (0, 0.0, -1, float("nan")):
            for interval in (None, "100", Decimal(100), "x", 100):
                with self.subTest(burst=burst, interval=interval):
                    self.assertEqual(self.send(burst_seconds=burst, burst_interval_ms=interval)["datagrams"], 1)
                    self.assertEqual(self.clock.sleeps, [])

    def test_bad_arguments_send_nothing(self):
        # A bad burst argument used to raise only after the first datagram had gone out.
        bad = [{"fmt": "mp3"}, {"fmt": "raw 8"}, {"burst_seconds": "abc"}, {"burst_seconds": None},
               {"burst_seconds": 1, "burst_interval_ms": "x"}, {"burst_seconds": 1, "burst_interval_ms": None}]
        for kwargs in bad:
            with self.subTest(**{k: repr(v) for k, v in kwargs.items()}):
                with self.assertRaises((TypeError, ValueError)):
                    self.send(**kwargs)
                self.assertEqual(self.sockets, [], "no socket was opened")

    def test_the_socket_is_closed_when_sending_fails(self):
        self.send_error = OSError(101, "Network is unreachable")
        with self.assertRaisesRegex(OSError, "unreachable"):
            self.send(fmt="both", burst_seconds=1)
        self.assertTrue(self.sockets[-1].closed)
        with self.assertRaisesRegex(OSError, "unreachable"):
            hc.send_heartbeat_request(LOOPBACK, self.GROUP, self.PORT)
        self.assertTrue(self.sockets[-1].closed)

    def test_heartbeat_request(self):
        ts = hc.send_heartbeat_request(LOOPBACK, self.GROUP, self.PORT, ttl=2)
        self.assertEqual(ts, 1767225600125)
        self.assertEqual(self.sockets[-1].sent, [(pb_uint(1, ts) + pb_uint(2, 2), (self.GROUP, self.PORT))])
        self.assertEqual(hc._fields(self.sockets[-1].sent[0][0]), {1: ts, 2: hc.CMD_HEARTBEAT_REQ})
        self.assertEqual(self.egresses[-1], (LOOPBACK, 2))
        self.assertTrue(self.sockets[-1].closed)
        os.environ["HALOW_EGRESS"] = "127.0.0.3"
        hc.send_heartbeat_request(group=self.GROUP, port=self.PORT)
        self.assertEqual(self.egresses[-1][0], "127.0.0.3")


class McastSocketTests(MulticastMixin, unittest.TestCase):
    def test_an_address_that_is_not_on_this_machine(self):
        for egress in ("192.0.2.123", "203.0.113.7"):            # documentation addresses: never local
            with self.subTest(egress=egress), self.assertRaises(OSError) as cm:
                hc._mcast_socket(egress)
            msg = str(cm.exception)
            self.assertTrue(msg.startswith(f"cannot send from {egress}: "), msg)
            self.assertIn("Is it an address of this machine", msg)

    def test_not_an_ipv4_address(self):
        for egress in ("", "999.1.1.1", "1.2.3", "fe80::1", "host.invalid"):
            with self.subTest(egress=egress), self.assertRaisesRegex(ValueError, "IPv4 address of this machine"):
                hc._mcast_socket(egress)

    def test_bad_egress_is_refused_before_a_socket_is_opened(self):
        with mock.patch.object(hc.socket, "socket", side_effect=AssertionError("a socket was opened")):
            for egress in ("999.1.1.1", "1.2.3", "localhost", "::1", "192.168.12.50/24", "1.2.3.4.5", "abc"):
                with self.subTest(egress=egress), self.assertRaisesRegex(ValueError, "IPv4 address"):
                    hc.send_direct(egress, group="239.255.200.9", port=15999)
                with self.subTest(egress=egress), self.assertRaisesRegex(ValueError, "IPv4 address"):
                    hc.send_heartbeat_request(egress, group="239.255.200.9", port=15999)

    def test_loopback_socket_options(self):
        try:
            s = hc._mcast_socket(LOOPBACK, ttl=3)
        except OSError as e:
            self.unavailable(str(e))
        with s:
            self.assertEqual(s.getsockname()[0], LOOPBACK)
            self.assertEqual(s.getsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL), 3)

    def test_the_datagrams_on_the_wire(self):
        group, port = pick_group("on-the-wire"), free_port()
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.addCleanup(rx.close)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            rx.bind(("", port))
            rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                          struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(LOOPBACK)))
            rec = hc.send_direct(LOOPBACK, "both", group, port, burst_seconds=0.3, burst_interval_ms=50)
        except OSError as e:
            self.unavailable(str(e))
        rx.settimeout(2)
        got = []
        with contextlib.suppress(socket.timeout):
            while len(got) < rec["datagrams"]:
                got.append(rx.recvfrom(2048))
        if not got:
            self.unavailable(f"nothing arrived on {group}:{port}")
        self.assertEqual(len(got), rec["datagrams"])
        self.assertTrue(all(addr[0] == LOOPBACK for _, addr in got))
        data = [d for d, _ in got]
        self.assertEqual(set(data[0::2]), {data[0]})
        self.assertEqual(set(data[1::2]), {data[1]})
        self.assertEqual(int(struct.unpack("<d", data[0])[0] * 1000), rec["ts_ms"])
        self.assertEqual(hc._fields(data[1]), {1: rec["ts_ms"], 2: hc.CMD_ALERT})


if __name__ == "__main__":
    unittest.main()
