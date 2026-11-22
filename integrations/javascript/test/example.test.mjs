// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// Tests for example.mjs, run as a child process: the codec self-test, gateway mode against an
// HTTP stub of the gateway, and direct mode on loopback against a stand-in wearable (one in
// this process, and tools/fake_wearable.py when Python 3 is installed). They check the exit
// codes and the output lines that CI looks for.
//
// Direct mode needs loopback multicast; without it those tests are skipped, or fail when
// HALOW_TEST_REQUIRE_MULTICAST=1. From the repository root:
//   node --test integrations/javascript/test/example.test.mjs

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { after, before, beforeEach, describe, it } from 'node:test';
import {
  FAKE_WEARABLE, LOOPBACK, TOKEN, VECTORS, authorized, childEnv, findPython, freeTcpPort, freeUdpPort, multicastGate,
  respond, runExample, sendMulticast, startStub, startWearable, testGroup, waitUntil,
} from './helpers.mjs';

const mc = await multicastGate();
const python = findPython();
const RECORD = { event: 'alert_sent', ts_ms: 1767225600123, format: 'raw8', egress: '192.168.12.50', datagrams: 1 };
const BAD_ACK_WAITS = ['abc', '-1', '3600.5', '1e9', 'Infinity', 'NaN', '-0.001'];

describe('example.mjs --selftest', () => {
  it('matches every vector and exits 0', async () => {
    const v = JSON.parse(readFileSync(VECTORS, 'utf8'));
    const n = v.raw8.length + v.gateway_message.length + v.device_response.length;
    const r = await runExample(['--selftest'], {});
    assert.equal(r.code, 0, r.stdout + r.stderr);
    assert.match(r.stdout, new RegExp(`^selftest: ${n}/${n} vectors match$`, 'm'));
  });
});

describe('example.mjs in gateway mode', () => {
  let stub;
  before(async () => {
    stub = await startStub();
  });
  after(async () => {
    await stub.close();
  });
  beforeEach(() => stub.reset());

  const env = (more = {}) => ({ HALOW_URL: stub.url, HALOW_TOKEN: TOKEN, HALOW_ACK_WAIT: '5', ...more });
  const alerts = () => stub.requests.filter((r) => r.method === 'POST' && r.path === '/alert');

  /** The wearable's ACK count goes from 4 to 5 on the (polls + 1)-th GET /wearables after the alert. */
  function ackAfterAlert(polls = 0) {
    let alerted = false;
    let seen = 0;
    stub.route('POST /alert', (req, res) => {
      if (!authorized(req)) return respond(res, 401, { error: 'missing or wrong Bearer token' });
      alerted = true;
      return respond(res, 200, RECORD);
    });
    stub.route('GET /wearables', (req, res) => {
      let acks = 4;
      if (alerted && ++seen > polls) acks = 5;
      respond(res, 200, { devices: [{ ip: '192.168.12.143', acks, heartbeats: 2, device_id: 'wz-07', battery_percentage: 87 }], recent: [] });
    });
  }

  it('sends one labelled alert, sees the ACK and exits 0', async () => {
    ackAfterAlert();
    const r = await runExample([], env());
    assert.equal(r.code, 0, r.stdout + r.stderr);
    assert.match(r.stdout, /^gateway health: \{"ok":true/m);
    assert.match(r.stdout, new RegExp(`^alert sent: ${JSON.stringify(RECORD).replace(/[.]/g, '\\.')}$`, 'm'));
    assert.match(r.stdout, /^wearable 192\.168\.12\.143: acks=5 heartbeats=2 device_id=wz-07 battery=87%$/m);
    assert.match(r.stdout, /^ACK received$/m);
    assert.equal(alerts().length, 1);
    assert.deepEqual(JSON.parse(alerts()[0].body), { label: 'javascript example' });
    assert.equal(alerts()[0].headers.authorization, `Bearer ${TOKEN}`);
  });

  it('keeps polling GET /wearables until the ACK shows', async () => {
    ackAfterAlert(2);
    const r = await runExample([], env());
    assert.equal(r.code, 0, r.stdout + r.stderr);
    assert.ok(stub.requests.filter((q) => q.path === '/wearables').length >= 4);
    assert.equal(alerts().length, 1);
  });

  it('exits 3 when no ACK comes within HALOW_ACK_WAIT', async () => {
    ackAfterAlert(1e9);
    let r = await runExample([], env({ HALOW_ACK_WAIT: '0' }));
    assert.equal(r.code, 3, r.stdout + r.stderr);
    assert.match(r.stderr, /no ACK within 0 s; check GET \/wearables again later/);
    r = await runExample([], env({ HALOW_ACK_WAIT: ' 0.4 ' }));
    assert.equal(r.code, 3, r.stdout + r.stderr);
    assert.match(r.stderr, /no ACK within 0\.4 s/);
    assert.equal(alerts().length, 2);
  });

  it('accepts the largest HALOW_ACK_WAIT, 3600', async () => {
    ackAfterAlert();
    const r = await runExample([], env({ HALOW_ACK_WAIT: '3600' }));
    assert.equal(r.code, 0, r.stdout + r.stderr);
  });

  it('exits 1 with HTTP 401 for a wrong token', async () => {
    ackAfterAlert();
    const r = await runExample([], env({ HALOW_TOKEN: 'wrong' }));
    assert.equal(r.code, 1, r.stdout + r.stderr);
    assert.match(r.stderr, /example: POST \/alert: HTTP 401: missing or wrong token/);
  });

  it('exits 1 for a rate-limited alert', async () => {
    stub.route('POST /alert', { status: 429, json: { error: 'alerts limited to one per 0.5 s' } });
    const r = await runExample([], env());
    assert.equal(r.code, 1, r.stdout + r.stderr);
    assert.match(r.stderr, /HTTP 429: rate limited.*\(alerts limited to one per 0\.5 s\)/);
  });

  it('exits 1 without alerting when the gateway cannot send', async () => {
    stub.route('GET /health', { status: 503, json: { ok: false, error: 'no HaLowLink address' } });
    const r = await runExample([], env());
    assert.equal(r.code, 1, r.stdout + r.stderr);
    assert.match(r.stdout, /^gateway health: \{"ok":false/m);
    assert.match(r.stderr, /the gateway cannot send right now: no HaLowLink address/);
    assert.equal(alerts().length, 0);
  });

  it('exits 1 when the gateway cannot be reached', async () => {
    const port = await freeTcpPort();
    const r = await runExample([], env({ HALOW_URL: `http://${LOOPBACK}:${port}` }));
    assert.equal(r.code, 1, r.stdout + r.stderr);
    assert.match(r.stderr, /example: cannot reach the gateway at http:\/\/127\.0\.0\.1:\d+: /);
  });

  it('exits 2 for a HALOW_URL that is not http', async () => {
    const r = await runExample([], env({ HALOW_URL: 'ftp://192.0.2.1' }));
    assert.equal(r.code, 2, r.stdout + r.stderr);
    assert.match(r.stderr, /http:\/\//);
  });

  it('exits 2 for a bad HALOW_ACK_WAIT before calling the gateway', async () => {
    ackAfterAlert();
    for (const wait of BAD_ACK_WAITS) {
      const r = await runExample([], env({ HALOW_ACK_WAIT: wait }));
      assert.equal(r.code, 2, `${wait}: ${r.stdout}${r.stderr}`);
      assert.match(r.stderr, new RegExp(`HALOW_ACK_WAIT="${wait.replace(/[.+]/g, '\\$&')}" must be a number of seconds \\(0 to 3600\\)`));
    }
    assert.deepEqual(await stub.requestsAfterBarrier(), [], 'nothing reached the gateway');
  });
});

describe('example.mjs in direct mode', () => {
  /** A stand-in wearable on a test group, and the settings that point example.mjs at it. */
  async function setup(wearable = {}, more = {}) {
    const group = testGroup();
    const replyPort = await freeTcpPort();
    const w = await startWearable({ group, replyPort, ...wearable });
    const env = { HALOW_EGRESS: LOOPBACK, HALOW_GROUP: group, HALOW_PORT: w.port, HALOW_RESPONSE_PORT: replyPort, HALOW_ACK_WAIT: '5', ...more };
    return { w, env, group };
  }

  /** The datagrams the wearable saw, after a barrier datagram sent once example.mjs ended:
   *  any datagram example.mjs sent arrived before it. */
  async function seenAfterBarrier(w, group) {
    await sendMulticast(group, w.port, Buffer.from('barrier'));
    await waitUntil(() => w.seen.some((s) => s.data.toString() === 'barrier'), { what: 'the barrier datagram' });
    return w.seen.filter((s) => s.data.toString() !== 'barrier');
  }

  mc.it('prints the heartbeat and the ACK, and exits 0', async () => {
    const { w, env, group } = await setup();
    try {
      const r = await runExample(['--direct'], env);
      assert.equal(r.code, 0, r.stdout + r.stderr);
      assert.match(r.stdout, /^HEARTBEAT from 127\.0\.0\.1: device_id=js-wearable battery=87% rssi=-62 dBm, round trip \d+ ms$/m);
      assert.match(r.stdout, /^ACK from 127\.0\.0\.1: wearable timestamp \d+ ms$/m);
      const record = JSON.parse(r.stdout.match(/^alert sent: (.*)$/m)[1]);
      assert.deepEqual({ ...record, ts_ms: 0 }, { event: 'alert_sent', ts_ms: 0, format: 'raw8', group: `${group}:${w.port}`, egress: LOOPBACK, datagrams: 1 });
      assert.deepEqual(w.seen.map((s) => [s.kind, s.format, s.from]), [['HEARTBEAT_REQ', 'proto', LOOPBACK], ['ALERT', 'raw8', LOOPBACK]]);
    } finally {
      await w.close();
    }
  });

  mc.it('sends the format in HALOW_FORMAT', async () => {
    for (const [format, want] of [['proto', ['proto']], ['both', ['raw8', 'proto']]]) {
      const { w, env, group } = await setup({}, { HALOW_FORMAT: format });
      try {
        const r = await runExample(['--direct'], env);
        assert.equal(r.code, 0, r.stdout + r.stderr);
        const seen = await seenAfterBarrier(w, group);
        assert.deepEqual(seen.filter((s) => s.kind === 'ALERT').map((s) => s.format), want, format);
      } finally {
        await w.close();
      }
    }
  });

  mc.it('exits 3 when no ACK comes within HALOW_ACK_WAIT', async () => {
    const { w, env } = await setup({ acks: false }, { HALOW_ACK_WAIT: '0.3' });
    try {
      const r = await runExample(['--direct'], env);
      assert.equal(r.code, 3, r.stdout + r.stderr);
      assert.match(r.stdout, /^HEARTBEAT from 127\.0\.0\.1: /m);
      assert.match(r.stderr, /no ACK within 0\.3 s/);
    } finally {
      await w.close();
    }
  });

  mc.it('goes on to the alert when no heartbeat comes within 3 s', { timeout: 20000 }, async () => {
    const { w, env } = await setup({ heartbeats: false });
    try {
      const r = await runExample(['--direct'], env);
      assert.equal(r.code, 0, r.stdout + r.stderr);
      assert.match(r.stdout, /^no heartbeat within 3 s \(continuing\)$/m);
      assert.match(r.stdout, /^ACK from 127\.0\.0\.1: /m);
    } finally {
      await w.close();
    }
  });

  mc.it('exits 2 for bad settings and sends nothing', async () => {
    const { w, env, group } = await setup();
    try {
      const cases = [
        ...BAD_ACK_WAITS.map((v) => [{ HALOW_ACK_WAIT: v }, /HALOW_ACK_WAIT=/]),
        [{ HALOW_PORT: '70000' }, /HALOW_PORT="70000" is not a port number \(1-65535\)/],
        [{ HALOW_RESPONSE_PORT: 'abc' }, /HALOW_RESPONSE_PORT="abc" is not a port number/],
        [{ HALOW_EGRESS: 'not-an-ip' }, /IPv4/],
      ];
      for (const [bad, message] of cases) {
        const r = await runExample(['--direct'], { ...env, ...bad });
        assert.equal(r.code, 2, `${JSON.stringify(bad)}: ${r.stdout}${r.stderr}`);
        assert.match(r.stderr, message);
      }
      assert.deepEqual(await seenAfterBarrier(w, group), [], 'no datagram reached the wearable');
    } finally {
      await w.close();
    }
  });

  mc.it('works end to end with tools/fake_wearable.py', { skip: python ? false : 'Python 3.8 or newer is not installed', timeout: 30000 }, async () => {
    const group = testGroup();
    const port = await freeUdpPort();
    const replyPort = await freeTcpPort();
    const fake = spawn(python, ['-B', '-I', FAKE_WEARABLE, '--iface', LOOPBACK, '--group', group, '--port', String(port),
      '--response-port', String(replyPort), '--device-id', 'fake-direct', '--seconds', '60'], { env: childEnv() });
    let out = '';
    fake.stdout.setEncoding('utf8').on('data', (d) => { out += d; });
    fake.stderr.setEncoding('utf8').on('data', (d) => { out += d; });
    const exited = new Promise((resolve) => fake.on('close', resolve));
    try {
      await waitUntil(() => out.includes('fake wearable ready') || fake.exitCode !== null, { timeoutMs: 15000, what: 'the stand-in wearable to start' });
      assert.match(out, /fake wearable ready/);
      const r = await runExample(['--direct'], {
        HALOW_EGRESS: LOOPBACK, HALOW_GROUP: group, HALOW_PORT: port, HALOW_RESPONSE_PORT: replyPort, HALOW_ACK_WAIT: '10',
      });
      assert.equal(r.code, 0, `${r.stdout}${r.stderr}\nstand-in wearable: ${out}`);
      // The lines CI looks for.
      assert.match(r.stdout, /HEARTBEAT from 127\.0\.0\.1: device_id=fake-direct/);
      assert.match(r.stdout, /ACK from 127\.0\.0\.1/);
      assert.match(out, /HEARTBEAT_REQ from 127\.0\.0\.1/);
      assert.match(out, /ALERT from 127\.0\.0\.1/);
    } finally {
      fake.kill();
      await exited;
    }
  });
});
