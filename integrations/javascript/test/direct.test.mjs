// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// Tests for direct mode's sending side: sendDirect and sendHeartbeatRequest on loopback
// multicast, egress checks, envPort and subnetPrefix.
//
// The datagrams go from 127.0.0.1 to a test group 239.255.<100-254>.<1-254> on a free port,
// and are received here with a socket joined to that group. Without loopback multicast the
// multicast tests are skipped, or fail when HALOW_TEST_REQUIRE_MULTICAST=1. On Linux:
//   sudo ip link set lo multicast on && sudo ip route add 239.0.0.0/8 dev lo
//
// From the repository root:
//   node --test integrations/javascript/test/direct.test.mjs

import assert from 'node:assert/strict';
import { after, beforeEach, describe, it } from 'node:test';
import {
  CMD_ALERT, CMD_HEARTBEAT_REQ, encodeGatewayMessage, encodeRaw8, envPort, sendDirect, sendHeartbeatRequest, subnetPrefix,
} from '../halow-client.mjs';
import {
  LOOPBACK, caught, classifyDatagram, handles, isolateEnv, multicastGate, multicastReceiver, sendMulticast, testGroup,
  waitUntil,
} from './helpers.mjs';

const mc = await multicastGate();

/** Waits until this process has exactly `n` UDP sockets open: the client closed its own. (A
 *  closed socket stays listed for a moment after its close callback, hence the polling.) */
const udpSockets = (n) => waitUntil(() => handles('UDPWrap') === n, { timeoutMs: 2000, what: `${n} open UDP socket(s)` });

describe('envPort', () => {
  isolateEnv();

  it('returns the fallback when the variable is unset, empty or blank', () => {
    assert.equal(envPort('HALOW_TEST_PORT', 5000), 5000);
    for (const v of ['', '   ', '\t']) {
      process.env.HALOW_TEST_PORT = v;
      assert.equal(envPort('HALOW_TEST_PORT', 1234), 1234, JSON.stringify(v));
    }
  });

  it('reads a port number from 1 to 65535, trimmed', () => {
    for (const [v, want] of [['1', 1], ['15000', 15000], [' 15000 ', 15000], ['65535', 65535], ['080', 80]]) {
      process.env.HALOW_TEST_PORT = v;
      assert.equal(envPort('HALOW_TEST_PORT', 5000), want, JSON.stringify(v));
    }
  });

  it('refuses anything else, naming the variable and the value', () => {
    for (const v of ['0', '65536', '70000', '-1', '1.5', '1e3', '0x10', 'abc', '5000x', '+80', '99999999999999999999']) {
      process.env.HALOW_TEST_PORT = v;
      assert.throws(() => envPort('HALOW_TEST_PORT', 5000), (e) => e instanceof RangeError
        && e.message === `HALOW_TEST_PORT=${JSON.stringify(v)} is not a port number (1-65535)`, JSON.stringify(v));
    }
  });
});

describe('subnetPrefix', () => {
  it('is the address up to its last dot', () => {
    assert.equal(subnetPrefix('192.168.12.50'), '192.168.12.');
    assert.equal(subnetPrefix('127.0.0.1'), '127.0.0.');
    assert.equal(subnetPrefix('10.1.2.3'), '10.1.2.');
  });

  it('is never empty, which would let replies in from any address (as in the Python client)', () => {
    assert.equal(subnetPrefix('localhost'), 'localhost.');
    assert.equal(subnetPrefix(''), '.');
    assert.equal(subnetPrefix('::1'), '::1.');
  });
});

describe('egress and format checks (nothing is sent)', () => {
  isolateEnv();
  beforeEach(() => udpSockets(0));

  it('refuses an egress that is not an IPv4 address, before opening a socket', async () => {
    for (const egress of ['not-an-ip', '999.1.1.1', '1.2.3', '1.2.3.4.5', '::1', ' 127.0.0.1', '0x7f.0.0.1', '127.000.000.001']) {
      for (const send of [sendDirect, sendHeartbeatRequest]) {
        const e = await caught(() => send({ egress, group: testGroup(), port: 9 }));
        assert.ok(e instanceof TypeError && /IPv4/.test(e.message), `${send.name}(${JSON.stringify(egress)}): ${e}`);
      }
    }
    assert.equal(handles('UDPWrap'), 0);
  });

  it('refuses a bad HALOW_EGRESS the same way', async () => {
    process.env.HALOW_EGRESS = 'gateway.invalid';
    const e = await caught(() => sendDirect({ group: testGroup(), port: 9 }));
    assert.ok(e instanceof TypeError, String(e));
    const e2 = await caught(() => sendHeartbeatRequest({ group: testGroup(), port: 9 }));
    assert.ok(e2 instanceof TypeError, String(e2));
  });

  it('refuses an unknown format before opening a socket', async () => {
    for (const format of ['mp3', 'raw', 'protobuf', 'raw8 ']) {
      const e = await caught(() => sendDirect({ egress: LOOPBACK, format, group: testGroup(), port: 9 }));
      assert.ok(e instanceof RangeError && /raw8, proto or both/.test(e.message), `${format}: ${e}`);
    }
    assert.equal(handles('UDPWrap'), 0);
  });

  it('reports an egress that is not an address of this machine, and closes the socket', async () => {
    // 192.0.2.1 is a documentation address (TEST-NET-1): binding to it fails, nothing is sent.
    for (const send of [sendDirect, sendHeartbeatRequest]) {
      const e = await caught(() => send({ egress: '192.0.2.1', group: testGroup(), port: 9 }));
      assert.ok(e instanceof Error, String(e));
      assert.match(e.message, /^cannot send from 192\.0\.2\.1: .*Is it an address of this machine/);
    }
    await udpSockets(0);
  });
});

describe('sending on loopback multicast', () => {
  isolateEnv();
  let rx;
  beforeEach(async () => {
    await rx?.close();
    rx = mc.ok ? await multicastReceiver(testGroup()) : undefined;
  });
  after(async () => {
    await rx?.close();
  });
  const opts = (more = {}) => ({ egress: LOOPBACK, group: rx.group, port: rx.port, ttl: 1, ...more });
  const received = (n) => waitUntil(() => rx.messages.length >= n && rx.messages, { what: `${n} datagram(s) on ${rx.group}:${rx.port}` });


  mc.it('raw8: one 8-byte datagram with the send time, from the egress address', async () => {
    const t0 = Date.now();
    const rec = await sendDirect(opts());
    const t1 = Date.now();
    assert.deepEqual(rec, { event: 'alert_sent', ts_ms: rec.ts_ms, format: 'raw8', group: `${rx.group}:${rx.port}`, egress: LOOPBACK, datagrams: 1 });
    assert.ok(rec.ts_ms >= t0 && rec.ts_ms <= t1, 'ts_ms is the send time');
    const [m] = await received(1);
    assert.equal(m.from, LOOPBACK);
    assert.equal(m.data.length, 8);
    assert.deepEqual(m.data, encodeRaw8(rec.ts_ms / 1000));
    assert.equal(Math.round(m.data.readDoubleLE(0) * 1000), rec.ts_ms);
    await udpSockets(1);   // only the receiver
  });

  mc.it('proto: one GatewayMessage ALERT stamped with ts_ms', async () => {
    const rec = await sendDirect(opts({ format: 'proto' }));
    assert.equal(rec.format, 'proto');
    assert.equal(rec.datagrams, 1);
    const [m] = await received(1);
    assert.deepEqual(m.data, encodeGatewayMessage(rec.ts_ms, CMD_ALERT));
    const c = classifyDatagram(m.data);
    assert.deepEqual([c.kind, c.timestamp], ['ALERT', BigInt(rec.ts_ms)]);
  });

  mc.it('both: raw8 then proto, from the same clock reading', async () => {
    const rec = await sendDirect(opts({ format: 'BOTH' }));
    assert.deepEqual([rec.format, rec.datagrams], ['both', 2]);
    const [a, b] = await received(2);
    assert.equal(classifyDatagram(a.data).format, 'raw8');
    assert.equal(Math.round(a.data.readDoubleLE(0) * 1000), rec.ts_ms);
    assert.deepEqual(b.data, encodeGatewayMessage(rec.ts_ms, CMD_ALERT));
  });

  mc.it('an empty or missing format means raw8', async () => {
    assert.equal((await sendDirect(opts({ format: '' }))).format, 'raw8');
    assert.equal((await sendDirect(opts({ format: null }))).format, 'raw8');
    const msgs = await received(2);
    assert.ok(msgs.every((m) => m.data.length === 8));
  });

  mc.it('takes the egress from HALOW_EGRESS, trimmed', async () => {
    process.env.HALOW_EGRESS = `  ${LOOPBACK}  `;
    const rec = await sendDirect({ group: rx.group, port: rx.port, ttl: 1 });
    assert.equal(rec.egress, LOOPBACK);
    assert.equal((await received(1))[0].from, LOOPBACK);
  });

  mc.it('a burst resends the identical bytes, and every datagram arrives', async () => {
    const started = Date.now();
    const rec = await sendDirect(opts({ format: 'both', burstSeconds: 0.3, burstIntervalMs: 50 }));
    const took = Date.now() - started;
    assert.ok(rec.datagrams >= 4 && rec.datagrams % 2 === 0 && rec.datagrams <= 14, `datagrams ${rec.datagrams}`);
    assert.ok(took >= 150 && took < 1500, `took ${took} ms`);
    const msgs = await received(rec.datagrams);
    await new Promise((r) => setImmediate(r));
    assert.equal(msgs.length, rec.datagrams);
    const raw = msgs.filter((m) => m.data.length === 8).map((m) => m.data.toString('hex'));
    const proto = msgs.filter((m) => m.data.length !== 8).map((m) => m.data.toString('hex'));
    assert.equal(raw.length, rec.datagrams / 2);
    assert.equal(new Set(raw).size, 1, 'every raw8 copy is identical');
    assert.deepEqual([...new Set(proto)], [encodeGatewayMessage(rec.ts_ms, CMD_ALERT).toString('hex')]);
  });

  mc.it('a burst interval below 20 ms is raised to 20 ms', async () => {
    const rec = await sendDirect(opts({ burstSeconds: 0.2, burstIntervalMs: 1 }));
    assert.ok(rec.datagrams >= 2 && rec.datagrams <= 11, `datagrams ${rec.datagrams}`);
    await received(rec.datagrams);
  });

  mc.it('a burst of 0, a negative one or one that is not a number sends once', async () => {
    for (const burstSeconds of [0, -3, NaN, 'abc', undefined]) {
      assert.equal((await sendDirect(opts({ burstSeconds }))).datagrams, 1, String(burstSeconds));
    }
    await received(5);
  });

  mc.it('a burst is capped at 5 s', { timeout: 15000 }, async () => {
    // Every 900 ms until 5 s after the first send: copies at 0, 0.9, ..., 4.5 s. A cap of 5.4 s
    // or more would add one at 5.4 s; no cap would run for 60 s.
    const started = Date.now();
    const rec = await sendDirect(opts({ burstSeconds: 60, burstIntervalMs: 900 }));
    const took = Date.now() - started;
    assert.equal(rec.datagrams, 6);
    assert.ok(took >= 4300 && took < 5600, `took ${took} ms`);
    await received(rec.datagrams);
  });

  mc.it('a heartbeat request is a GatewayMessage HEARTBEAT_REQ with the returned timestamp', async () => {
    const t0 = Date.now();
    const ts = await sendHeartbeatRequest(opts());
    assert.ok(Number.isSafeInteger(ts) && ts >= t0 && ts <= Date.now());
    const [m] = await received(1);
    assert.equal(m.from, LOOPBACK);
    assert.deepEqual(m.data, encodeGatewayMessage(ts, CMD_HEARTBEAT_REQ));
    assert.deepEqual([classifyDatagram(m.data).kind, classifyDatagram(m.data).timestamp], ['HEARTBEAT_REQ', BigInt(ts)]);
    await udpSockets(1);   // only the receiver
  });

  mc.it('closes its socket also when sending fails', async () => {
    for (const port of [0, 70000]) {
      const e = await caught(() => sendDirect(opts({ port })));
      assert.ok(e, `port ${port} should fail`);
      const e2 = await caught(() => sendHeartbeatRequest(opts({ port })));
      assert.ok(e2, `port ${port} should fail`);
    }
    await udpSockets(1);   // only the receiver
    // The receiver still works: nothing above broke the group on loopback.
    await sendMulticast(rx.group, rx.port, Buffer.from('still here'));
    assert.equal((await received(1))[0].data.toString(), 'still here');
  });
});
