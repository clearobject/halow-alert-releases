// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// Tests for the wire formats in halow-client.mjs: raw8, GatewayMessage and DeviceResponse,
// against testdata/vectors.json, hand-made edge cases, a seeded fuzz run and, when Python 3
// is installed, the Python client (the reference implementation) on the same fuzz inputs.
//
// From the repository root:
//   node --test integrations/javascript/test/codec.test.mjs
// HALOW_TEST_FUZZ_SEED=<number> repeats the fuzz run with another seed.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';
import { Worker } from 'node:worker_threads';
import * as hc from '../halow-client.mjs';
import {
  PACKAGE_JSON, PYTHON_DIR, VECTORS, ackMessage, caught, fieldKey, findPython, fuzzCases, heartbeatMessage, hex,
  lfield, norm, rng, run, varint, vfield,
} from './helpers.mjs';

const { decodeDeviceResponse, encodeGatewayMessage, encodeRaw8 } = hc;
const vectors = JSON.parse(readFileSync(VECTORS, 'utf8'));
const FUZZ_SEED = Number((process.env.HALOW_TEST_FUZZ_SEED ?? '').trim() || 0x48414c4f);
const MAX_SAFE = BigInt(Number.MAX_SAFE_INTEGER);

/** What decodeDeviceResponse threw for these bytes (asserting it was a RangeError). */
function rejection(bytes) {
  let err = null;
  try {
    decodeDeviceResponse(bytes);
  } catch (e) {
    err = e;
  }
  assert.ok(err, `expected ${bytes.toString('hex')} to be rejected`);
  assert.ok(err instanceof RangeError, `expected a RangeError, got ${err?.name}: ${err?.message}`);
  return err.message;
}

/** The number the decoder returns for a uint64: a Number up to 2^53 - 1, a BigInt above. */
const asDecoded = (v) => (BigInt(v) <= MAX_SAFE ? Number(v) : BigInt(v));

// The examples in docs/PROTOCOL.md.
const DOC_ACK = hex('08 a3 d1 ea b6 b7 33 10 01 1a 02 08 01');
const DOC_HEARTBEAT = hex(`08 fb d0 ea b6 b7 33 10 02 22 1b 0a 05 77 7a 2d 30 37 10 57
  18 c2 ff ff ff ff ff ff ff ff 01 20 90 1c 28 01 30 02`);

describe('the module', () => {
  it('exports the documented API and constants', () => {
    for (const name of ['GatewayClient', 'HalowError', 'totalAcks', 'sendDirect', 'sendHeartbeatRequest',
      'listenForResponses', 'subnetPrefix', 'encodeRaw8', 'encodeGatewayMessage', 'decodeDeviceResponse', 'envPort']) {
      assert.equal(typeof hc[name], 'function', name);
    }
    assert.equal(hc.DEFAULT_URL, 'http://127.0.0.1:8080');
    assert.equal(hc.DEFAULT_EGRESS, '192.168.12.50');
    assert.deepEqual([hc.GROUP, hc.PORT, hc.RESPONSE_PORT, hc.TTL], ['239.255.0.10', 5000, 5001, 4]);
    assert.deepEqual(hc.FORMATS, ['raw8', 'proto', 'both']);
    assert.deepEqual([hc.CMD_ALERT, hc.CMD_HEARTBEAT_REQ], [1, 2]);
  });

  it('has the version that package.json declares', () => {
    const pkg = JSON.parse(readFileSync(PACKAGE_JSON, 'utf8'));
    assert.match(hc.VERSION, /^\d+\.\d+\.\d+([-+.][0-9A-Za-z.-]+)?$/);
    assert.equal(hc.VERSION, pkg.version);
  });
});

describe('testdata/vectors.json', () => {
  it('raw8: every vector', () => {
    assert.ok(vectors.raw8.length > 0);
    for (const c of vectors.raw8) {
      const got = encodeRaw8(c.seconds);
      assert.equal(got.length, 8);
      assert.equal(got.toString('hex'), c.hex, `seconds ${c.seconds}`);
    }
  });

  it('gateway_message: every vector, from BigInts and, where exact, from Numbers', () => {
    assert.ok(vectors.gateway_message.length > 0);
    for (const c of vectors.gateway_message) {
      const ts = BigInt(c.timestamp_ms);
      const cmd = BigInt(c.command);
      assert.equal(encodeGatewayMessage(ts, cmd).toString('hex'), c.hex, `${c.timestamp_ms}/${c.command}`);
      if (ts <= MAX_SAFE) assert.equal(encodeGatewayMessage(Number(ts), Number(cmd)).toString('hex'), c.hex);
    }
  });

  it('device_response: every vector decodes, or is rejected with a RangeError, as the gateway does', () => {
    assert.ok(vectors.device_response.length > 0);
    let rejected = 0;
    for (const c of vectors.device_response) {
      let got;
      try {
        got = norm(decodeDeviceResponse(Buffer.from(c.hex, 'hex')));
      } catch (e) {
        assert.ok(e instanceof RangeError, `${c.hex}: ${e}`);
        got = null;
        rejected += 1;
      }
      assert.deepStrictEqual(got, c.result ?? null, c.hex);
    }
    assert.ok(rejected > 0, 'the vectors should include malformed replies');
  });
});

describe('varint', () => {
  const values = [0n, 1n, 127n, 128n, 255n, 300n, 16383n, 16384n, 2n ** 21n - 1n, 2n ** 21n, 2n ** 28n, 2n ** 31n - 1n,
    2n ** 31n, 2n ** 32n - 1n, 2n ** 32n, 2n ** 35n, 1767225600123n, 2n ** 42n, 2n ** 49n, 2n ** 53n - 1n, 2n ** 53n,
    2n ** 53n + 1n, 2n ** 56n, 2n ** 63n - 1n, 2n ** 63n, 2n ** 64n - 1n];

  it('round trips edge values through encodeGatewayMessage and decodeDeviceResponse', () => {
    for (const v of values) {
      const bytes = encodeGatewayMessage(v, 0n);
      if (v === 0n) {
        assert.equal(bytes.length, 0, 'a zero timestamp is left out');
        continue;
      }
      const bits = v.toString(2).length;
      assert.equal(bytes[0], 0x08, `${v}: field 1, varint`);
      assert.equal(bytes.length, 1 + Math.ceil(bits / 7), `${v}: length`);
      assert.deepEqual(bytes.subarray(1), varint(v), `${v}: same bytes as an independent encoder`);
      for (let i = 1; i < bytes.length; i++) assert.equal(bytes[i] >= 0x80, i < bytes.length - 1, `${v}: continuation bits`);
      const r = decodeDeviceResponse(bytes);
      assert.equal(r.timestamp_ms, asDecoded(v), `${v}: decoded`);
      if (v <= MAX_SAFE) assert.deepEqual(encodeGatewayMessage(Number(v), 0), bytes, `${v}: Number and BigInt agree`);
    }
  });

  it('decodes a Number up to 2^53 - 1 and a BigInt from 2^53 on', () => {
    assert.equal(decodeDeviceResponse(encodeGatewayMessage(2n ** 53n - 1n, 1)).timestamp_ms, Number.MAX_SAFE_INTEGER);
    assert.equal(decodeDeviceResponse(encodeGatewayMessage(2 ** 53, 1)).timestamp_ms, 2n ** 53n);
    assert.equal(decodeDeviceResponse(encodeGatewayMessage(2n ** 64n - 1n, 1)).timestamp_ms, 18446744073709551615n);
  });

  it('writes 2^64 - 1 as ten bytes', () => {
    assert.equal(encodeGatewayMessage(2n ** 64n - 1n, 0).toString('hex'), '08ffffffffffffffffff01');
  });

  it('round trips 2000 seeded random 64-bit values', () => {
    const r = rng(7);
    for (let i = 0; i < 2000; i++) {
      const v = r.u64() >> BigInt(r.int(64));
      const cmd = BigInt(r.int(4));
      const bytes = encodeGatewayMessage(v, cmd);
      const want = Buffer.concat([v ? Buffer.concat([Buffer.from([0x08]), varint(v)]) : Buffer.alloc(0),
        cmd ? Buffer.concat([Buffer.from([0x10]), varint(cmd)]) : Buffer.alloc(0)]);
      assert.deepEqual(bytes, want, `${v}/${cmd}`);
      assert.equal(decodeDeviceResponse(bytes).timestamp_ms, asDecoded(v));
    }
  });

  it('keeps the low 64 bits of a ten-byte varint with more bits, as the gateway does', () => {
    assert.equal(decodeDeviceResponse(hex('08 ff ff ff ff ff ff ff ff ff 7f')).timestamp_ms, 2n ** 64n - 1n);
    assert.equal(decodeDeviceResponse(hex('08 80 80 80 80 80 80 80 80 80 02')).timestamp_ms, 0);
  });

  it('accepts a varint written with more bytes than needed', () => {
    assert.equal(decodeDeviceResponse(hex('08 81 80 80 00 10 81 00')).timestamp_ms, 1);
    assert.equal(decodeDeviceResponse(hex('08 81 80 80 00 10 81 00')).code, 'ACK');
  });

  it('rejects an eleven-byte varint as a value, a key and a length', () => {
    const eleven = 'ff ff ff ff ff ff ff ff ff ff 01';
    assert.equal(rejection(hex(`08 ${eleven}`)), 'varint longer than 10 bytes');
    assert.equal(rejection(hex(eleven)), 'varint longer than 10 bytes');
    assert.equal(rejection(hex(`1a ${eleven}`)), 'varint longer than 10 bytes');
    assert.equal(rejection(hex(`08 80 80 80 80 80 80 80 80 80 80 80 80 80 80 00`)), 'varint longer than 10 bytes');
  });

  it('refuses to encode a negative, fractional or missing number', async () => {
    for (const bad of [-1, -1n, 1.5, NaN, Infinity]) {
      const e = await caught(() => encodeGatewayMessage(bad, 1));
      assert.ok(e instanceof RangeError, `${bad}: ${e}`);
      const e2 = await caught(() => encodeGatewayMessage(1, bad));
      assert.ok(e2 instanceof RangeError, `command ${bad}: ${e2}`);
    }
    assert.ok(await caught(() => encodeGatewayMessage()) instanceof Error);
  });
});

describe('raw8', () => {
  it('is the docs example: 1767225600.123 s, little-endian double', () => {
    assert.equal(encodeRaw8(1767225600.123).toString('hex'), '3bdf07406e55da41');
  });

  it('round trips through a little-endian double, whatever the value', () => {
    for (const s of [0, -0, 1, -1.5, 1767225600.123, Date.now() / 1000, Number.MAX_VALUE, Number.MIN_VALUE, Infinity]) {
      const b = encodeRaw8(s);
      assert.ok(Buffer.isBuffer(b));
      assert.equal(b.length, 8);
      assert.ok(Object.is(b.readDoubleLE(0), s), String(s));
    }
    assert.ok(Number.isNaN(encodeRaw8(NaN).readDoubleLE(0)));
  });
});

describe('GatewayMessage', () => {
  it('matches the docs examples', () => {
    assert.equal(encodeGatewayMessage(1767225600123, hc.CMD_ALERT).toString('hex'), '08fbd0eab6b7331001');
    assert.equal(encodeGatewayMessage(1767225600123, hc.CMD_HEARTBEAT_REQ).toString('hex'), '08fbd0eab6b7331002');
    assert.equal(encodeGatewayMessage(1, 1).toString('hex'), '08011001');   // the smallest valid ALERT
  });

  it('leaves out zero fields, as proto3 does', () => {
    assert.equal(encodeGatewayMessage(0, 0).toString('hex'), '');
    assert.equal(encodeGatewayMessage(0, 1).toString('hex'), '1001');
    assert.equal(encodeGatewayMessage(5, 0).toString('hex'), '0805');
    assert.equal(encodeGatewayMessage(0n, 2n).toString('hex'), '1002');
  });

  it('returns a Buffer, timestamp first', () => {
    const b = encodeGatewayMessage(300, 300);
    assert.ok(Buffer.isBuffer(b));
    assert.equal(b.toString('hex'), '08ac0210ac02');
  });
});

describe('DeviceResponse', () => {
  it('decodes the docs ACK example', () => {
    assert.deepStrictEqual(decodeDeviceResponse(DOC_ACK), { timestamp_ms: 1767225600163, code: 'ACK', ack_success: true });
  });

  it('decodes the docs heartbeat example', () => {
    assert.deepStrictEqual(decodeDeviceResponse(DOC_HEARTBEAT), {
      timestamp_ms: 1767225600123,
      code: 'HEARTBEAT',
      heartbeat: { device_id: 'wz-07', battery_percentage: 87, rssi: -62, uptime_seconds: 3600, app_state: 1, device_state: 2 },
    });
  });

  it('accepts a Buffer, a Uint8Array, an ArrayBuffer and an array of bytes alike, and leaves the input alone', () => {
    const copy = Buffer.from(DOC_HEARTBEAT);
    const want = decodeDeviceResponse(DOC_HEARTBEAT);
    const u8 = new Uint8Array(DOC_HEARTBEAT);
    assert.deepStrictEqual(decodeDeviceResponse(u8), want);
    assert.deepStrictEqual(decodeDeviceResponse(u8.buffer), want);
    assert.deepStrictEqual(decodeDeviceResponse([...DOC_HEARTBEAT]), want);
    assert.deepEqual(DOC_HEARTBEAT, copy);
  });

  it('decodes an empty message to the defaults', () => {
    assert.deepStrictEqual(decodeDeviceResponse(Buffer.alloc(0)), { timestamp_ms: 0, code: 'UNKNOWN' });
  });

  it('names the codes it knows and gives the number of others', () => {
    const code = (n) => decodeDeviceResponse(Buffer.concat([vfield(1, 5), vfield(2, n)])).code;
    assert.deepEqual([0, 1, 2, 3, 300].map(code), ['UNKNOWN', 'ACK', 'HEARTBEAT', '3', '300']);
    assert.equal(code(2n ** 63n), '9223372036854775808');
  });

  it('reads ack_success from the ack payload', () => {
    assert.equal(decodeDeviceResponse(hex('08 01 10 01 1a 00')).ack_success, false);
    assert.equal(decodeDeviceResponse(hex('08 01 10 01 1a 02 08 00')).ack_success, false);
    assert.equal(decodeDeviceResponse(hex('08 01 10 01 1a 02 08 05')).ack_success, true);
    assert.equal(decodeDeviceResponse(Buffer.concat([vfield(2, 1), lfield(3, vfield(1, 2n ** 64n - 1n))])).ack_success, true);
    assert.ok(!('ack_success' in decodeDeviceResponse(hex('08 01 10 01'))));
    assert.ok(!('heartbeat' in decodeDeviceResponse(DOC_ACK)));
  });

  it('decodes an empty heartbeat payload to the defaults', () => {
    assert.deepStrictEqual(decodeDeviceResponse(hex('10 02 22 00')).heartbeat,
      { device_id: '', battery_percentage: 0, rssi: 0, uptime_seconds: 0, app_state: 0, device_state: 0 });
  });

  it('decodes negative battery and rssi (ten-byte varints) as int32', () => {
    for (const [battery, rssi] of [[-1, -62], [-100, -1], [-(2 ** 31), -(2 ** 31)], [2 ** 31 - 1, -120]]) {
      const h = decodeDeviceResponse(heartbeatMessage(1, { battery, rssi })).heartbeat;
      assert.deepEqual([h.battery_percentage, h.rssi], [battery, rssi]);
    }
  });

  it('keeps the low 32 bits of int32 and uint32 fields, as protobuf does', () => {
    const hb = (fields) => decodeDeviceResponse(Buffer.concat([vfield(2, 2), lfield(4, Buffer.concat(fields))])).heartbeat;
    let h = hb([vfield(2, 2n ** 32n + 5n), vfield(3, 2n ** 31n), vfield(4, 2n ** 32n + 7n), vfield(5, 2n ** 32n), vfield(6, 2n ** 33n + 1n)]);
    assert.deepEqual([h.battery_percentage, h.rssi, h.uptime_seconds, h.app_state, h.device_state], [5, -(2 ** 31), 7, 0, 1]);
    h = hb([vfield(2, 2n ** 32n - 1n), vfield(3, 2n ** 63n), vfield(4, 2n ** 64n - 1n), vfield(5, -1), vfield(6, 2n ** 32n - 1n)]);
    assert.deepEqual([h.battery_percentage, h.rssi, h.uptime_seconds, h.app_state, h.device_state],
      [-1, 0, 2 ** 32 - 1, 2 ** 32 - 1, 2 ** 32 - 1]);
  });

  it('decodes device_id as UTF-8, keeps a byte order mark and replaces invalid bytes', () => {
    const id = (bytes) => decodeDeviceResponse(Buffer.concat([vfield(2, 2), lfield(4, lfield(1, bytes))])).heartbeat.device_id;
    assert.equal(id(Buffer.from('wz-ü-\u{1F6A7}', 'utf8')), 'wz-ü-\u{1F6A7}');
    assert.equal(id(hex('ef bb bf 77 7a')), '﻿wz');
    assert.equal(id(hex('77 ff 7a')), 'w�z');
    assert.equal(id(hex('77 00 7a')), 'w\u0000z');
    assert.equal(id(Buffer.from('0123456789abcdef')), '0123456789abcdef');
  });

  it('skips unknown fields of every wire type, also inside the payloads', () => {
    const unknown = Buffer.concat([vfield(5, 9), fieldKey(9, 1), Buffer.alloc(8, 0xaa), lfield(10, 'skip me'),
      fieldKey(11, 5), Buffer.alloc(4, 0xbb), vfield(1000, 1), vfield(0, 1), lfield(2 ** 29 - 1, Buffer.alloc(300))]);
    const r = decodeDeviceResponse(Buffer.concat([unknown, DOC_ACK, unknown]));
    assert.deepStrictEqual(r, { timestamp_ms: 1767225600163, code: 'ACK', ack_success: true });
    const hb = decodeDeviceResponse(Buffer.concat([vfield(2, 2), lfield(4, Buffer.concat([unknown, lfield(1, 'x'), unknown]))]));
    assert.equal(hb.heartbeat.device_id, 'x');
    assert.equal(decodeDeviceResponse(Buffer.concat([vfield(2, 1), lfield(3, Buffer.concat([unknown, vfield(1, 1)]))])).ack_success, true);
  });

  it('takes the last value of a repeated field', () => {
    assert.equal(decodeDeviceResponse(Buffer.concat([vfield(1, 1), vfield(1, 2), vfield(2, 1)])).timestamp_ms, 2);
    const two = Buffer.concat([vfield(2, 2), lfield(4, lfield(1, 'first')), lfield(4, lfield(1, 'second'))]);
    assert.equal(decodeDeviceResponse(two).heartbeat.device_id, 'second');
  });

  it('rejects a known field with the wrong wire type', () => {
    const cases = [
      [lfield(1, 'x'), 'field 1: wrong wire type'],                                // timestamp as bytes
      [Buffer.concat([fieldKey(1, 1), Buffer.alloc(8)]), 'field 1: wrong wire type'],   // timestamp as fixed64
      [lfield(2, 'x'), 'field 2: wrong wire type'],                                // code as bytes
      [Buffer.concat([fieldKey(2, 5), Buffer.alloc(4)]), 'field 2: wrong wire type'],   // code as fixed32
      [vfield(3, 1), 'field 3: wrong wire type'],                                  // ack payload as varint
      [vfield(4, 1), 'field 4: wrong wire type'],                                  // heartbeat payload as varint
      [lfield(3, lfield(1, 'x')), 'field 1: wrong wire type'],                     // ack success as bytes
      [lfield(4, vfield(1, 7)), 'field 1: wrong wire type'],                       // device_id as varint
      [lfield(4, lfield(2, 'x')), 'field 2: wrong wire type'],                     // battery as bytes
      [lfield(4, Buffer.concat([fieldKey(3, 5), Buffer.alloc(4)])), 'field 3: wrong wire type'],   // rssi as fixed32
      [lfield(4, lfield(4, 'x')), 'field 4: wrong wire type'],                     // uptime as bytes
      [lfield(4, lfield(5, 'x')), 'field 5: wrong wire type'],
      [lfield(4, lfield(6, 'x')), 'field 6: wrong wire type'],
    ];
    for (const [bytes, msg] of cases) assert.equal(rejection(bytes), msg, bytes.toString('hex'));
  });

  it('rejects truncated input', () => {
    assert.equal(rejection(hex('08')), 'truncated varint');
    assert.equal(rejection(hex('08 fb d0')), 'truncated varint');
    assert.equal(rejection(hex('80')), 'truncated varint');                        // a key cut short
    assert.equal(rejection(hex('08 01 10 02 22')), 'truncated varint');            // a length missing
    assert.equal(rejection(DOC_HEARTBEAT.subarray(0, 20)), 'field 4 runs past the end of the message');
    // Every prefix of the heartbeat either is a valid message on its own or is rejected.
    for (let n = 0; n < DOC_HEARTBEAT.length; n++) {
      try {
        decodeDeviceResponse(DOC_HEARTBEAT.subarray(0, n));
      } catch (e) {
        assert.ok(e instanceof RangeError, `prefix ${n}: ${e}`);
      }
    }
  });

  it('rejects a field that runs past the end, without trying to allocate it', () => {
    assert.equal(rejection(hex('0a 05 01 02')), 'field 1 runs past the end of the message');
    assert.equal(rejection(hex('09 01 02 03 04 05 06 07')), 'field 1 runs past the end of the message');   // fixed64, 7 bytes
    assert.equal(rejection(hex('0d 01 02 03')), 'field 1 runs past the end of the message');               // fixed32, 3 bytes
    assert.equal(rejection(hex('1a ff ff ff ff ff ff ff ff ff 01')), 'field 3 runs past the end of the message');
    assert.equal(rejection(hex('22 ff ff ff ff 0f 00')), 'field 4 runs past the end of the message');
    assert.equal(rejection(hex('22 03 0a 05 41')), 'field 1 runs past the end of the message');          // inside the payload
  });

  it('rejects groups (wire types 3 and 4) and the unused wire types 6 and 7', () => {
    assert.equal(rejection(hex('0b')), 'wire type 3');
    assert.equal(rejection(hex('0c')), 'wire type 4');
    assert.equal(rejection(hex('1b 08 01 1c')), 'wire type 3');
    assert.equal(rejection(hex('0e')), 'wire type 6');
    assert.equal(rejection(hex('0f')), 'wire type 7');
    assert.equal(rejection(Buffer.concat([DOC_ACK, hex('ab 01')])), 'wire type 3');   // an unknown field as a group
    assert.equal(rejection(hex('22 01 0b')), 'wire type 3');                           // inside the heartbeat payload
    assert.equal(rejection(hex('1a 01 0c')), 'wire type 4');                           // inside the ack payload
  });

  it('decodes ACKs and heartbeats built by the test encoder', () => {
    assert.deepStrictEqual(decodeDeviceResponse(ackMessage(42)), { timestamp_ms: 42, code: 'ACK', ack_success: true });
    assert.deepStrictEqual(decodeDeviceResponse(ackMessage(42, false)), { timestamp_ms: 42, code: 'ACK', ack_success: false });
    const r = decodeDeviceResponse(heartbeatMessage(2n ** 60n, { deviceId: 'wz-99', uptime: 2 ** 32 - 1 }));
    assert.equal(r.timestamp_ms, 2n ** 60n);
    assert.equal(r.heartbeat.uptime_seconds, 2 ** 32 - 1);
  });
});

describe('decoder fuzz (seeded)', () => {
  const cases = fuzzCases(FUZZ_SEED);
  let outcome;

  it(`decodes ${cases.length} random and mutated inputs without an unexpected error or a hang (seed ${FUZZ_SEED})`, { timeout: 60000 }, async () => {
    const worker = new Worker(new URL('./fuzz-worker.mjs', import.meta.url), { workerData: { cases } });
    const limitMs = 30000;
    let timer;
    try {
      outcome = await Promise.race([
        new Promise((resolve, reject) => {
          worker.once('message', resolve);
          worker.once('error', reject);
        }),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error(`the decoder did not finish ${cases.length} inputs within ${limitMs} ms (seed ${FUZZ_SEED}): it hangs`)), limitMs);
        }),
      ]);
    } finally {
      clearTimeout(timer);
      await worker.terminate();
    }
    const bad = outcome.results.filter((r) => r.unexpected);
    assert.deepEqual(bad.slice(0, 10), [], `${bad.length} input(s) failed in an unexpected way (seed ${FUZZ_SEED})`);
    assert.equal(outcome.results.length, cases.length);
    // Both outcomes must be common, or the inputs do not exercise the decoder.
    assert.ok(outcome.decoded > cases.length / 20, `only ${outcome.decoded} decoded`);
    assert.ok(outcome.rejected > cases.length / 20, `only ${outcome.rejected} rejected`);
  });

  it('decodes every fuzz input exactly as the Python reference client does', { timeout: 60000 }, async (t) => {
    const python = findPython();
    if (!python) {
      t.skip('Python 3.8 or newer is not installed');
      return;
    }
    if (!outcome) assert.fail('the fuzz run above did not finish');
    // -I: ignore the environment and the current folder; -B: write no bytecode next to the source.
    const code = [
      'import json, sys',
      'sys.path.insert(0, sys.argv[1])',
      'import halow_client as hc',
      'def norm(v):',
      '    if isinstance(v, bool): return v',
      '    if isinstance(v, int): return str(v)',
      '    if isinstance(v, dict): return {k: norm(x) for k, x in v.items()}',
      '    return v',
      'out = []',
      'for line in sys.stdin:',
      '    try:',
      '        r = norm(hc.decode_device_response(bytes.fromhex(line.strip())))',
      '    except ValueError:',
      '        r = None',
      '    except Exception as e:',
      '        r = {"python raised": type(e).__name__ + ": " + str(e)}',
      '    out.append(json.dumps(r, sort_keys=True))',
      'sys.stdout.write("\\n".join(out) + "\\n")',
    ].join('\n');
    const r = await run(python, ['-B', '-I', '-c', code, PYTHON_DIR], { input: `${cases.join('\n')}\n`, timeoutMs: 50000 });
    assert.equal(r.code, 0, r.stderr);
    const py = r.stdout.trimEnd().split('\n').map((line) => JSON.parse(line));
    assert.equal(py.length, cases.length);
    const differ = [];
    for (let i = 0; i < cases.length; i++) {
      try {
        assert.deepStrictEqual(outcome.results[i].result, py[i]);
      } catch {
        differ.push({ hex: cases[i], javascript: outcome.results[i].result, python: py[i] });
      }
    }
    assert.deepEqual(differ.slice(0, 5), [], `${differ.length} of ${cases.length} inputs decode differently (seed ${FUZZ_SEED})`);
  });
});
