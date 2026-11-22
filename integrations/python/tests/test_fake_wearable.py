# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ClearObject
"""tools/fake_wearable.py, the stand-in wearable that the direct-mode tests and CI rely on.
These tests check the checker: what it sends decodes correctly with halow_client, its
decoder agrees with the client's, it understands the client's datagrams, it ACKs each alert
once, answers every heartbeat request, ignores garbage, and refuses bad flags.

The child-process tests need loopback multicast for the stand-in wearable to start (skipped
without it, or failed with HALOW_TEST_REQUIRE_MULTICAST=1). They send to it by unicast on
127.0.0.1. Run with the rest: python -m unittest discover -s integrations/python/tests -v
"""
import contextlib
import io
import socket
import time
import unittest

from halow_test_support import (
    LOOPBACK,
    U32,
    U64,
    MulticastMixin,
    free_port,
    hc,
    load_fake_wearable,
    pb_uint,
    pick_group,
    rng,
    wait_until,
)

fw = load_fake_wearable()


class EncoderTests(unittest.TestCase):
    def test_ack_decodes_with_the_client(self):
        for now_ms in (1, 127, 1767225600123, (1 << 63) + 5, U64):
            with self.subTest(now_ms=now_ms):
                self.assertEqual(hc.decode_device_response(fw.ack_message(now_ms)),
                                 {"timestamp_ms": now_ms, "code": "ACK", "ack_success": True})
        self.assertEqual(hc.decode_device_response(fw.ack_message(0))["timestamp_ms"], 0)

    def test_heartbeat_decodes_with_the_client(self):
        cases = [
            (1767225600123, "fake-01", 87, -62, 3615),
            (1, "z", 0, 0, 0),
            (U64, "wz-07 ü 東 \U0001f6a7", 100, -128, U32),
            (5, "x" * 300, -1, -(1 << 31), 1),
            (7, "id\x00with\x1bcontrol", (1 << 31) - 1, (1 << 31) - 1, 12345),
        ]
        for ts, dev, battery, rssi, uptime in cases:
            with self.subTest(dev=dev, battery=battery, rssi=rssi):
                got = hc.decode_device_response(fw.heartbeat_message(ts, dev, battery, rssi, uptime))
                self.assertEqual(got, {"timestamp_ms": ts, "code": "HEARTBEAT", "heartbeat": {
                    "device_id": dev, "battery_percentage": battery, "rssi": rssi, "uptime_seconds": uptime,
                    "app_state": 0, "device_state": 0}})

    def test_zero_and_empty_values_are_left_out(self):
        self.assertEqual(fw.enc_field(1, 0), b"")
        self.assertEqual(fw.enc_len(4, b""), b"")
        # Everything zero or empty: even the heartbeat payload is left out.
        self.assertEqual(fw.heartbeat_message(0, "", 0, 0, 0), b"\x10\x02")
        self.assertEqual(hc.decode_device_response(b"\x10\x02"), {"timestamp_ms": 0, "code": "HEARTBEAT"})
        self.assertEqual(fw.enc_field(2, 1), pb_uint(2, 1))

    def test_varint_matches_the_client(self):
        r = rng("fake-varint")
        for v in [0, 1, 127, 128, U32, U64] + [r.getrandbits(r.randint(1, 64)) for _ in range(500)]:
            self.assertEqual(fw._varint(v), hc._varint(v), v)


class DecoderParityTests(unittest.TestCase):
    """The stand-in's protobuf reader is a copy of the client's: on any input, both give the
    same fields or both raise ValueError."""

    def same(self, buf):
        results = []
        for reader in (fw._fields, hc._fields):
            try:
                results.append(("ok", reader(buf)))
            except ValueError:
                results.append(("ValueError", None))
        self.assertEqual(results[0], results[1], buf.hex())

    def test_seeded_random_input(self):
        r = rng("fake-parity")
        for _ in range(3000):
            self.same(bytes(r.getrandbits(8) for _ in range(r.randint(0, 40))))

    def test_client_datagrams_and_their_mutations(self):
        r = rng("fake-parity-mutations")
        seeds = [hc.encode_gateway_message(ts, cmd) for ts in (0, 1, 1767225600123, U64) for cmd in (0, 1, 2)]
        for _ in range(2000):
            buf = bytearray(r.choice(seeds))
            if buf:
                buf[r.randrange(len(buf))] = r.getrandbits(8)
            if r.random() < 0.3:
                del buf[r.randrange(len(buf) + 1):]
            self.same(bytes(buf))


class ClassifyTests(unittest.TestCase):
    def test_the_client_datagrams(self):
        t = 1767225600.123
        self.assertEqual(fw.classify(hc.encode_raw8(t)), ("ALERT", hc.encode_raw8(t)))
        ts = int(t * 1000)
        self.assertEqual(fw.classify(hc.encode_gateway_message(ts, hc.CMD_ALERT)), ("ALERT", ts))
        self.assertEqual(fw.classify(hc.encode_gateway_message(ts, hc.CMD_HEARTBEAT_REQ)), ("HEARTBEAT_REQ", ts))

    def test_everything_else_is_ignored(self):
        for data in (b"", b"\xff", b"\x08\x01", hc.encode_gateway_message(5, 3), pb_uint(1, 5) + b"\x12\x01\x01",
                     b"\x0a\x08" + b"\x00" * 8, b"\xff" * 9, b"GET / HTTP/1.1\r\n\r\n"):
            with self.subTest(data=data):
                self.assertIsNone(fw.classify(data)[0])

    def test_any_8_bytes_are_a_raw8_alert(self):
        r = rng("fake-raw8")
        for _ in range(100):
            data = bytes(r.getrandbits(8) for _ in range(8))
            self.assertEqual(fw.classify(data), ("ALERT", data))


class ArgumentTests(unittest.TestCase):
    def main(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            fw.main(list(argv))
        return cm.exception.code, err.getvalue()

    def test_bad_flags_exit_2(self):
        cases = [
            (("--port", "0"), "not a port number"),
            (("--port", "65536"), "not a port number"),
            (("--response-port", "x"), "not a port number"),
            (("--group", "1.2.3"), "IPv4 address"),
            (("--group", "239.255.0.300"), "IPv4 address"),
            (("--iface", "localhost"), "IPv4 address"),
            (("127.0.0.1", "--iface", "127.0.0.2"), "give the interface address once"),
            (("--battery", "full"), "invalid int value"),
            (("--rssi", "-6.5"), "invalid int value"),
            (("--seconds", "soon"), "invalid float value"),
            (("--ack-delay", "x"), "invalid float value"),
            (("--no-such-flag",), "unrecognized arguments"),
        ]
        for argv, why in cases:
            with self.subTest(argv=argv):
                code, err = self.main(*argv)
                self.assertEqual(code, 2)
                self.assertIn(why, err)

    def test_help(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            fw.main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        for flag in ("--iface", "--group", "--port", "--response-port", "--device-id", "--battery", "--rssi",
                     "--ack-delay", "--seconds"):
            self.assertIn(flag, out.getvalue())

    def test_a_group_it_cannot_join(self):
        # Not a multicast address: joining fails, and run() says so and returns 2.
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = fw.run(LOOPBACK, "10.1.2.3", free_port(), free_port(), seconds=1)
        self.assertEqual(code, 2)
        self.assertIn("fake wearable: cannot join 10.1.2.3:", err.getvalue())


class BehaviourTests(MulticastMixin, unittest.TestCase):
    """The stand-in wearable as a child process, sent unicast datagrams on 127.0.0.1."""

    def setUp(self):
        self.group, self.port, self.rport = pick_group(self.id()), free_port(), free_port()
        self.replies = hc.ResponseListener(self.rport, bind=LOOPBACK).start()
        self.addCleanup(self.replies.stop)
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.tx.close)

    def send(self, *datagrams):
        for d in datagrams:
            self.tx.sendto(d, (LOOPBACK, self.port))

    def sync(self, fake, n, ts):
        """Send heartbeat request number n (timestamp ts) and wait until the stand-in has handled
        it: it handles datagrams one at a time, in order, so everything sent before is done."""
        self.send(hc.encode_gateway_message(ts, hc.CMD_HEARTBEAT_REQ))
        self.assertTrue(fake.wait_line("HEARTBEAT_REQ from", n), fake.output())

    def replies_of(self, code):
        return [r for r in self.replies.responses if r["code"] == code]

    def test_one_ack_per_alert_heartbeats_every_time_and_garbage_ignored(self):
        fake = self.start_fake(self.group, self.port, self.rport, "--device-id", "fake-oracle", "--battery", "0",
                               "--rssi", "-100")
        a, b = hc.encode_raw8(1767225600.5), hc.encode_raw8(1767225601.5)
        self.send(a, a, a)                                       # a burst: one alert
        self.sync(fake, 1, 1001)
        self.assertEqual(fake.count("ALERT from"), 1, fake.output())

        self.send(b, b, hc.encode_gateway_message(5555, 1), hc.encode_gateway_message(5555, 1),
                  hc.encode_gateway_message(5556, 1), a, b"\x07\x07\x07", hc.encode_gateway_message(9, 3))
        self.sync(fake, 2, 1002)
        self.assertEqual(fake.count("ALERT from"), 4, "b, proto 5555 and proto 5556; not the repeats: "
                         + fake.output())
        self.assertEqual(fake.count(f"ignored 3 bytes from {LOOPBACK}"), 1, fake.output())
        self.assertEqual(fake.count("ignored"), 2, "garbage and the unknown command")

        self.sync(fake, 3, 1001)                                 # heartbeat requests are never deduplicated
        self.assertTrue(wait_until(lambda: len(self.replies_of("ACK")) == 4 and len(self.replies_of("HEARTBEAT")) == 3),
                        self.replies.responses)
        for r in self.replies_of("ACK"):
            self.assertIs(r["ack_success"], True)
            self.assertLessEqual(abs(r["timestamp_ms"] - time.time() * 1000), 60000, "ACK time is now")
        hbs = self.replies_of("HEARTBEAT")
        self.assertEqual(sorted(h["timestamp_ms"] for h in hbs), [1001, 1001, 1002], "the request's timestamp")
        for h in hbs:
            self.assertEqual(h["ip"], LOOPBACK)
            hb = h["heartbeat"]
            self.assertEqual((hb["device_id"], hb["battery_percentage"], hb["rssi"]), ("fake-oracle", 0, -100))
            self.assertTrue(0 <= hb["uptime_seconds"] < 120)

    def test_ack_delay_and_seconds(self):
        fake = self.start_fake(self.group, self.port, self.rport, "--ack-delay", "0.4", seconds=1.5)
        start = time.monotonic()
        self.send(hc.encode_raw8(1767225600.25))
        self.assertIsNotNone(self.replies.wait_for("ACK", 5), fake.output())
        self.assertGreaterEqual(time.monotonic() - start, 0.35, "the ACK waited --ack-delay")
        self.assertTrue(wait_until(lambda: fake.proc.poll() is not None, 10), "it stops after --seconds")
        self.assertEqual(fake.proc.returncode, 0)
        self.assertIn("for 1.5 s", fake.lines[0])

    def test_nobody_listening_for_the_reply(self):
        self.replies.stop()
        fake = self.start_fake(self.group, self.port, self.rport)
        self.send(hc.encode_raw8(1767225602.75))
        self.assertTrue(fake.wait_line(f"could not answer ALERT to {LOOPBACK}:{self.rport}"), fake.output())
        self.sync(fake, 1, 77)                                   # and it keeps running
        self.assertIsNone(fake.proc.poll())


if __name__ == "__main__":
    unittest.main()
