# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ClearObject
"""Codec tests beyond the shared vectors: varints at their edges, encoder properties,
DeviceResponse edge cases, a seeded fuzz of the decoder, and selftest().

Run with the rest: python -m unittest discover -s integrations/python/tests -v
"""
import contextlib
import io
import json
import math
import os
import shutil
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock

from halow_test_support import (
    U32,
    U64,
    VECTORS,
    device_response,
    hc,
    heartbeat_payload,
    pb_bytes,
    pb_key,
    pb_uint,
    pb_varint,
    rng,
)

INT32_MIN, INT32_MAX = -(1 << 31), (1 << 31) - 1
# Every value where a varint gains a byte, its neighbours, and the 32- and 64-bit limits.
EDGES = sorted({0, 1, 2, 255, 256, (1 << 31) - 1, 1 << 31, U32, U32 + 1, (1 << 63) - 1, 1 << 63, U64 - 1, U64}
               | {(1 << (7 * k)) + d for k in range(1, 10) for d in (-1, 0, 1)})


def _bits(r, n):
    """n random bits; Python 3.8's getrandbits(0) raises instead of returning 0."""
    return r.getrandbits(n) if n else 0


def _no_hang(fn, seconds=5.0):
    """Run fn() on a helper thread; (finished, result or exception). A call that never ends
    must fail the test, not hang the suite."""
    box = {}

    def target():
        try:
            box["result"] = fn()
        except BaseException as e:      # handed back to the test
            box["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    return not t.is_alive(), box.get("error", box.get("result"))


class VarintTests(unittest.TestCase):
    def test_edge_values_round_trip(self):
        for v in EDGES:
            with self.subTest(value=v):
                enc = hc._varint(v)
                self.assertEqual(enc, pb_varint(v), "differs from an independent encoder")
                self.assertEqual(len(enc), max(1, math.ceil(v.bit_length() / 7)))
                self.assertTrue(all(b & 0x80 for b in enc[:-1]) and enc[-1] < 0x80, enc.hex())
                self.assertEqual(hc._read_varint(enc + b"\xff\x01", 0), (v, len(enc)))
                self.assertEqual(hc._read_varint(b"\x00\x00" + enc, 2), (v, 2 + len(enc)))
                # And through the public decoder, as the timestamp field.
                self.assertEqual(hc.decode_device_response(pb_uint(1, v))["timestamp_ms"], v)

    def test_seeded_random_values_round_trip(self):
        r = rng("varint")
        for _ in range(3000):
            v = _bits(r, r.randint(0, 64))
            enc = hc._varint(v)
            self.assertEqual(hc._read_varint(enc, 0), (v, len(enc)), v)

    def test_ten_byte_limit(self):
        self.assertEqual(hc._read_varint(b"\xff" * 9 + b"\x01", 0), (U64, 10))
        # A 10th byte above 1 carries bits past 64; the decoder keeps the low 64 bits.
        self.assertEqual(hc.decode_device_response(b"\x08" + b"\xff" * 9 + b"\x7f")["timestamp_ms"], U64)
        for buf in (b"\xff" * 10 + b"\x01", b"\x80" * 10 + b"\x00", b"\x80" * 11, b"\xff" * 64):
            with self.subTest(hex=buf.hex()), self.assertRaisesRegex(ValueError, "longer than 10 bytes"):
                hc._read_varint(buf, 0)

    def test_non_minimal_encodings_are_accepted(self):
        self.assertEqual(hc._read_varint(b"\x80\x00", 0), (0, 2))
        self.assertEqual(hc._read_varint(b"\x81\x80\x80\x00", 0), (1, 4))

    def test_truncated(self):
        for buf in (b"", b"\x80", b"\xff\xff", b"\x80" * 9):
            with self.subTest(hex=buf.hex()), self.assertRaisesRegex(ValueError, "truncated"):
                hc._read_varint(buf, 0)
        with self.assertRaisesRegex(ValueError, "truncated"):
            hc._read_varint(b"\x01", 1)

    def test_negative_values_raise_instead_of_looping_forever(self):
        # A negative number used to make the encoder loop forever (and grow without bound).
        for args in ((-1, 1), (1, -1), (-(1 << 63), 0)):
            with self.subTest(args=args):
                finished, result = _no_hang(lambda a=args: hc.encode_gateway_message(*a))
                self.assertTrue(finished, f"encode_gateway_message{args} never returned")
                self.assertIsInstance(result, ValueError)


class EncoderTests(unittest.TestCase):
    def test_gateway_message_properties(self):
        r = rng("gateway-message")
        cases = [(0, 0), (0, 1), (1, 0), (U64, 2), (1767225600123, 1), (1767225600123, 2)]
        cases += [(_bits(r, r.randint(0, 64)), r.choice((0, 1, 2, 3, 127, 128, U32))) for _ in range(500)]
        for ts, cmd in cases:
            msg = hc.encode_gateway_message(ts, cmd)
            want = (pb_uint(1, ts) if ts else b"") + (pb_uint(2, cmd) if cmd else b"")
            self.assertEqual(msg, want, (ts, cmd))
            self.assertEqual(hc._fields(msg), {k: v for k, v in ((1, ts), (2, cmd)) if v}, (ts, cmd))
        self.assertEqual(hc.encode_gateway_message(0, 0), b"")

    def test_gateway_message_is_short_enough_not_to_look_like_raw8(self):
        # Wearables tell the formats apart by length: raw8 is exactly 8 bytes. A proto alert
        # with a timestamp of today (2^40 to 2^42 ms) is 9 bytes.
        for ts in (1 << 40, 1767225600123, 4102444800000, (1 << 42) - 1):
            for cmd in (hc.CMD_ALERT, hc.CMD_HEARTBEAT_REQ):
                self.assertEqual(len(hc.encode_gateway_message(ts, cmd)), 9)

    def test_raw8_properties(self):
        r = rng("raw8")
        values = [0.0, -0.0, 1.5, -1.5, 1e-300, 1e300, 2.0 ** 53, 1767225600.123, float("inf"), float("-inf")]
        values += [r.uniform(0, 4102444800.0) for _ in range(500)]
        for t in values:
            enc = hc.encode_raw8(t)
            self.assertEqual(len(enc), 8)
            self.assertEqual(struct.unpack("<d", enc)[0], t)
            self.assertEqual(enc, struct.pack("<d", t))
        self.assertEqual(hc.encode_raw8(-0.0), b"\x00" * 7 + b"\x80")
        self.assertTrue(math.isnan(struct.unpack("<d", hc.encode_raw8(float("nan")))[0]))
        self.assertEqual(hc.encode_raw8(1767225600), hc.encode_raw8(1767225600.0), "an int works like a float")


class DeviceResponseTests(unittest.TestCase):
    def decode(self, buf):
        return hc.decode_device_response(buf)

    def rejects(self, buf, regex=""):
        with self.assertRaisesRegex(ValueError, regex):
            self.decode(buf)

    def test_empty_message(self):
        self.assertEqual(self.decode(b""), {"timestamp_ms": 0, "code": "UNKNOWN"})

    def test_ack_and_heartbeat(self):
        self.assertEqual(self.decode(device_response(ts=5, code=1)),
                         {"timestamp_ms": 5, "code": "ACK", "ack_success": True})
        self.assertEqual(self.decode(device_response(ts=5, code=1, ack=False))["ack_success"], False)
        hb = self.decode(device_response(ts=9, code=2, heartbeat=heartbeat_payload()))
        self.assertEqual(hb, {"timestamp_ms": 9, "code": "HEARTBEAT", "heartbeat": {
            "device_id": "wz-07", "battery_percentage": 87, "rssi": -62, "uptime_seconds": 3615,
            "app_state": 1, "device_state": 2}})

    def test_unknown_codes_keep_their_number(self):
        for code in (3, 7, 127, 128, U32, U64):
            self.assertEqual(self.decode(pb_uint(2, code))["code"], str(code))

    def test_any_non_zero_ack_success_is_true(self):
        for v, want in ((0, False), (1, True), (2, True), (U64, True)):
            self.assertIs(self.decode(pb_uint(2, 1) + pb_bytes(3, pb_uint(1, v)))["ack_success"], want)
        self.assertIs(self.decode(pb_uint(2, 1) + pb_bytes(3, b""))["ack_success"], False)

    def test_empty_heartbeat_payload(self):
        self.assertEqual(self.decode(pb_uint(2, 2) + pb_bytes(4, b""))["heartbeat"], {
            "device_id": "", "battery_percentage": 0, "rssi": 0, "uptime_seconds": 0, "app_state": 0,
            "device_state": 0})

    def test_signed_and_unsigned_heartbeat_fields(self):
        cases = [  # (battery, rssi, uptime) on the wire -> decoded
            ((U64, U64 - 61, 0), (-1, -62, 0)),          # int32 sent as 64-bit two's complement
            ((U32, U32 - 61, 0), (-1, -62, 0)),          # or as 32 bits
            ((1 << 31, INT32_MAX, U32), (INT32_MIN, INT32_MAX, U32)),
            ((0, 0, U32 + 6), (0, 0, 5)),                # uint32: only the low 32 bits
            ((100, -128 & U64, 1 << 40), (100, -128, 0)),
        ]
        for (battery, rssi, uptime), want in cases:
            hb = pb_uint(2, battery) + pb_uint(3, rssi) + pb_uint(4, uptime)
            got = self.decode(pb_uint(2, 2) + pb_bytes(4, hb))["heartbeat"]
            self.assertEqual((got["battery_percentage"], got["rssi"], got["uptime_seconds"]), want)

    def test_device_id_that_is_not_utf8(self):
        got = self.decode(pb_uint(2, 2) + pb_bytes(4, pb_bytes(1, b"wz\xff\xfe-07")))["heartbeat"]["device_id"]
        self.assertEqual(got, "wz��-07")

    def test_groups_and_reserved_wire_types_are_rejected(self):
        for wire in (3, 4, 6, 7):
            for tag in (1, 2, 5, 99):
                with self.subTest(wire=wire, tag=tag):
                    self.rejects(pb_key(tag, wire) + pb_uint(1, 1), f"wire type {wire}")
        # A group inside a nested payload is rejected too.
        self.rejects(pb_uint(2, 2) + pb_bytes(4, pb_key(9, 3) + pb_key(9, 4)), "wire type 3")
        self.rejects(pb_uint(2, 1) + pb_bytes(3, pb_key(1, 4)), "wire type 4")

    def test_known_fields_with_the_wrong_wire_type(self):
        cases = {
            "timestamp as bytes": pb_bytes(1, b"\x01"),
            "code as fixed32": pb_key(2, 5) + b"\x01\x00\x00\x00",
            "code as fixed64": pb_key(2, 1) + b"\x01" + b"\x00" * 7,
            "ack_payload as varint": pb_uint(3, 1),
            "heartbeat as varint": pb_uint(4, 1),
            "ack success as bytes": pb_bytes(3, pb_bytes(1, b"\x01")),
            "device_id as varint": pb_bytes(4, pb_uint(1, 7)),
            "battery as bytes": pb_bytes(4, pb_bytes(2, b"\x01")),
            "device_state as fixed32": pb_bytes(4, pb_key(6, 5) + b"\x00" * 4),
        }
        for name, buf in cases.items():
            with self.subTest(name):
                self.rejects(buf, "wrong wire type")

    def test_malformed_nested_payloads(self):
        cases = {
            "truncated varint in ack": pb_bytes(3, b"\x08"),
            "truncated key in ack": pb_bytes(3, b"\x80"),
            "heartbeat bytes past its end": pb_bytes(4, pb_key(1, 2) + pb_varint(50) + b"abc"),
            "overlong varint in heartbeat": pb_bytes(4, b"\x10" + b"\xff" * 11),
        }
        for name, buf in cases.items():
            with self.subTest(name):
                self.rejects(pb_uint(2, 2) + buf)

    def test_repeated_fields_last_one_wins(self):
        self.assertEqual(self.decode(pb_uint(2, 1) + pb_uint(2, 2))["code"], "HEARTBEAT")
        self.assertEqual(self.decode(pb_uint(1, 5) + pb_uint(2, 1) + pb_uint(1, 6))["timestamp_ms"], 6)
        two = pb_bytes(4, heartbeat_payload("first", battery=10)) + pb_bytes(4, heartbeat_payload("second"))
        hb = self.decode(pb_uint(2, 2) + two)["heartbeat"]
        self.assertEqual((hb["device_id"], hb["battery_percentage"]), ("second", 87))
        # A later field of the wrong type replaces a good one, and is rejected.
        self.rejects(pb_uint(2, 1) + pb_bytes(2, b"\x01"), "wrong wire type")

    def test_unknown_fields_of_every_wire_type_are_skipped(self):
        # Field numbers that neither DeviceResponse nor its payloads use, one of each wire type.
        extra = (pb_uint(10, 9) + pb_key(11, 1) + b"\x00" * 8 + pb_bytes(12, b"\xff" * 300) + pb_key(13, 5)
                 + b"\x00" * 4 + pb_uint((1 << 29) - 1, U64) + pb_uint(0, 1))
        self.assertEqual(self.decode(extra + device_response(ts=7) + extra),
                         {"timestamp_ms": 7, "code": "ACK", "ack_success": True})
        hb = self.decode(pb_uint(2, 2) + pb_bytes(4, extra + heartbeat_payload() + extra))["heartbeat"]
        self.assertEqual(hb["device_id"], "wz-07")

    def test_fixed_width_fields_cut_short(self):
        for buf in (pb_key(9, 1) + b"\x00" * 7, pb_key(9, 5) + b"\x00" * 3, pb_key(9, 1), pb_key(9, 5)):
            with self.subTest(hex=buf.hex()):
                self.rejects(buf, "past the end")

    def test_huge_length_prefixes(self):
        for n in (4, 255, 1 << 20, 1 << 32, 1 << 62, 1 << 63, U64):
            for tag in (3, 4, 9):
                with self.subTest(length=n, tag=tag):
                    finished, result = _no_hang(lambda t=tag, n=n: self.decode(pb_key(t, 2) + pb_varint(n) + b"abc"))
                    self.assertTrue(finished)
                    self.assertIsInstance(result, ValueError)
        self.assertEqual(self.decode(pb_bytes(9, b"abc"))["code"], "UNKNOWN", "a length that fits is fine")

    def test_large_input_is_decoded_in_linear_time(self):
        filler = pb_bytes(15, b"\x00" * 100) * 10000          # about 1 MB of unknown fields
        start = time.monotonic()
        self.assertEqual(self.decode(filler + device_response(ts=3))["timestamp_ms"], 3)
        self.assertLess(time.monotonic() - start, 5.0)


class DecoderFuzzTests(unittest.TestCase):
    """decode_device_response on seeded random bytes and on mutations of valid replies: it
    returns a well-formed dict or raises ValueError, never anything else, and never hangs."""

    CASES = 3000

    @classmethod
    def setUpClass(cls):
        with open(VECTORS, encoding="utf-8") as f:
            vec = json.load(f)
        cls.valid = [bytes.fromhex(c["hex"]) for c in vec["device_response"] if c.get("result") is not None]
        cls.valid += [device_response(ts=1767225600123), device_response(code=2, heartbeat=heartbeat_payload()),
                      device_response(code=2, heartbeat=heartbeat_payload("x" * 200, -1, -128, U32, U32, U32))]

    def check(self, buf):
        try:
            r = hc.decode_device_response(buf)
        except ValueError:
            return "rejected"
        except Exception as e:                    # any other exception is the failure
            self.fail(f"{type(e).__name__}: {e} for {bytes(buf).hex()}")
        self.assertLessEqual(set(r), {"timestamp_ms", "code", "ack_success", "heartbeat"})
        self.assertTrue(0 <= r["timestamp_ms"] <= U64)
        self.assertTrue(r["code"] in ("UNKNOWN", "ACK", "HEARTBEAT") or r["code"].isdigit(), r["code"])
        if "ack_success" in r:
            self.assertIsInstance(r["ack_success"], bool)
        hb = r.get("heartbeat")
        if hb is not None:
            self.assertIsInstance(hb["device_id"], str)
            for k in ("battery_percentage", "rssi"):
                self.assertTrue(INT32_MIN <= hb[k] <= INT32_MAX, hb)
            for k in ("uptime_seconds", "app_state", "device_state"):
                self.assertTrue(0 <= hb[k] <= U32, hb)
        return "decoded"

    def test_random_bytes(self):
        r = rng("fuzz-random")
        outcomes = {"decoded": 0, "rejected": 0}
        start = time.monotonic()
        for _ in range(self.CASES):
            n = r.choice((r.randint(0, 4), r.randint(0, 24), r.randint(0, 96)))
            outcomes[self.check(bytes(r.getrandbits(8) for _ in range(n)))] += 1
        self.assertLess(time.monotonic() - start, 30)
        self.assertGreater(outcomes["decoded"], 0, outcomes)
        self.assertGreater(outcomes["rejected"], 0, outcomes)

    def test_mutations_of_valid_replies(self):
        r = rng("fuzz-mutations")
        outcomes = {"decoded": 0, "rejected": 0}
        start = time.monotonic()
        for _ in range(self.CASES):
            buf = bytearray(r.choice(self.valid))
            for _ in range(r.randint(1, 4)):
                op = r.randrange(6)
                i = r.randrange(len(buf) + 1)
                if op == 0 and buf:                      # flip one bit
                    j = r.randrange(len(buf))
                    buf[j] ^= 1 << r.randrange(8)
                elif op == 1:                            # cut the end off
                    del buf[i:]
                elif op == 2:                            # insert random bytes
                    buf[i:i] = bytes(r.getrandbits(8) for _ in range(r.randint(1, 4)))
                elif op == 3 and buf:                    # repeat a slice
                    j = r.randrange(len(buf))
                    buf[i:i] = buf[j:j + r.randint(1, 8)]
                elif op == 4 and buf:                    # set a byte to an edge value
                    buf[r.randrange(len(buf))] = r.choice((0x00, 0x7F, 0x80, 0xFF, 0x0B, 0x0C, 0x0E, 0x0F))
                else:                                    # drop a byte
                    del buf[i:i + 1]
            outcomes[self.check(bytes(buf))] += 1
        self.assertLess(time.monotonic() - start, 30)
        self.assertGreater(outcomes["decoded"], 0, outcomes)
        self.assertGreater(outcomes["rejected"], 0, outcomes)

    def test_every_truncation_of_valid_replies(self):
        for buf in self.valid:
            for n in range(len(buf)):
                self.check(buf[:n])


class SelftestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(VECTORS, encoding="utf-8") as f:
            cls.vec = json.load(f)
        cls.n = sum(len(cls.vec[k]) for k in ("raw8", "gateway_message", "device_response"))

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="halow-selftest-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, vec) -> str:
        path = os.path.join(self.tmp, "vectors.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(vec, f)
        return path

    def selftest(self, path):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = hc.selftest(path)
        return rc, out.getvalue()

    def test_doctored_vectors_fail_with_a_useful_line_for_each(self):
        vec = json.loads(json.dumps(self.vec))
        next(c for c in vec["raw8"] if c["hex"] != "00" * 8)["hex"] = "00" * 8
        vec["gateway_message"][0]["hex"] = "ff"
        accepted = next(c for c in vec["device_response"] if c.get("result") is not None)
        accepted["result"] = dict(accepted["result"], code="NOT-A-CODE")
        rejected = next(c for c in vec["device_response"] if c.get("result") is None)
        rejected["result"] = {"timestamp_ms": "0", "code": "UNKNOWN"}
        rc, out = self.selftest(self.write(vec))
        self.assertEqual(rc, 1, out)
        lines = out.splitlines()
        self.assertTrue(any(line.startswith("raw8 ") and "want 0000000000000000" in line for line in lines), out)
        self.assertTrue(any(line.startswith("gateway_message ") and line.endswith("want ff") for line in lines), out)
        self.assertTrue(any(line.startswith("device_response " + accepted["hex"]) and "NOT-A-CODE" in line
                            for line in lines), out)
        self.assertTrue(any(line.startswith("device_response " + rejected["hex"] + ": got None") for line in lines),
                        out)
        self.assertEqual(lines[-1], f"selftest: {self.n - 4}/{self.n} vectors match")

    def test_bad_hex_counts_as_rejected(self):
        vec = {"raw8": [], "gateway_message": [], "device_response": [
            {"hex": "zz", "result": None}, {"hex": "0801", "result": None}]}
        rc, out = self.selftest(self.write(vec))
        self.assertEqual(rc, 1, out)
        self.assertIn("device_response 0801: got {'timestamp_ms': '1', 'code': 'UNKNOWN'}, want None", out)
        self.assertIn("selftest: 1/2 vectors match", out)

    def test_missing_default_path(self):
        # As for a copied halow_client.py or the installed package: no ../testdata next to it.
        with mock.patch.object(hc, "__file__", os.path.join(self.tmp, "lib", "halow_client.py")):
            with self.assertRaises(FileNotFoundError) as cm:
                hc.selftest()
        self.assertIn("pass its path", str(cm.exception))

    def test_missing_explicit_path(self):
        with self.assertRaises(OSError):
            hc.selftest(os.path.join(self.tmp, "no-such-vectors.json"))

    def test_the_default_path_is_next_to_the_module(self):
        lib = os.path.join(self.tmp, "lib")              # must exist: "lib/.." is resolved on disk
        os.makedirs(lib)
        os.makedirs(os.path.join(self.tmp, "testdata"))
        shutil.copy(VECTORS, os.path.join(self.tmp, "testdata", "vectors.json"))
        with mock.patch.object(hc, "__file__", os.path.join(lib, "halow_client.py")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(hc.selftest(), 0, out.getvalue())
        self.assertIn(f"selftest: {self.n}/{self.n} vectors match", out.getvalue())


if __name__ == "__main__":
    unittest.main()
