// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// Shared helpers for the tests of halow-client.mjs and example.mjs: paths, a seeded random
// generator, an independent DeviceResponse encoder, an HTTP stub of the gateway, loopback
// multicast, a stand-in wearable and child processes.
//
// Nothing here reaches a real wearable or HaLowLink. Everything stays on 127.0.0.1, multicast
// uses a test group 239.255.<100-254>.<1-254>, and ports are free ports chosen by the OS,
// never the real group 239.255.0.10 or the ports 5000, 5001 and 8080.

import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import dgram from 'node:dgram';
import http from 'node:http';
import net from 'node:net';
import path from 'node:path';
import { afterEach, beforeEach, it } from 'node:test';
import { fileURLToPath } from 'node:url';

export const TEST_DIR = path.dirname(fileURLToPath(import.meta.url));
export const JS_DIR = path.dirname(TEST_DIR);
export const REPO = path.resolve(JS_DIR, '..', '..');
export const CLIENT = path.join(JS_DIR, 'halow-client.mjs');
export const EXAMPLE = path.join(JS_DIR, 'example.mjs');
export const PACKAGE_JSON = path.join(JS_DIR, 'package.json');
export const VECTORS = path.join(REPO, 'integrations', 'testdata', 'vectors.json');
export const PYTHON_DIR = path.join(REPO, 'integrations', 'python');
export const FAKE_WEARABLE = path.join(REPO, 'tools', 'fake_wearable.py');

export const LOOPBACK = '127.0.0.1';
export const TOKEN = 'good-token';
export const REQUIRE_MULTICAST = (process.env.HALOW_TEST_REQUIRE_MULTICAST ?? '').trim() === '1';
const FORBIDDEN_PORTS = new Set([5000, 5001, 8080]);   // the real ones: never used by a test

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Polls cond() every intervalMs until it returns something truthy, and returns that.
 *  Throws after timeoutMs, naming `what` it waited for. */
export async function waitUntil(cond, { timeoutMs = 5000, intervalMs = 10, what = 'the condition' } = {}) {
  const end = Date.now() + timeoutMs;
  for (;;) {
    const v = await cond();
    if (v) return v;
    if (Date.now() > end) throw new Error(`gave up after ${timeoutMs} ms waiting for ${what}`);
    await sleep(intervalMs);
  }
}

/** Runs fn and returns what it threw or rejected with (null if nothing). Works the same
 *  whether fn throws synchronously or returns a rejected promise. */
export async function caught(fn) {
  try {
    await fn();
  } catch (e) {
    return e;
  }
  return null;
}

// --------------------------------------------------------------------------
// Environment
// --------------------------------------------------------------------------
const PROXY_VARS = /^(https?_proxy|all_proxy|no_proxy)$/i;

/** Inside a describe(): every test starts without HALOW_* or proxy settings, and whatever
 *  a test sets in process.env is undone afterwards. */
export function isolateEnv() {
  let saved;
  beforeEach(() => {
    saved = { ...process.env };
    for (const k of Object.keys(process.env)) {
      if (k.startsWith('HALOW_') || PROXY_VARS.test(k)) delete process.env[k];
    }
  });
  afterEach(() => {
    for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
    Object.assign(process.env, saved);
  });
}

/** The environment for a child process: this one's without HALOW_*, proxy settings and the
 *  test runner's own variables, plus `extra` (undefined removes a variable). */
export function childEnv(extra = {}) {
  const env = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (k.startsWith('HALOW_') || PROXY_VARS.test(k) || k === 'NODE_TEST_CONTEXT' || k === 'NODE_USE_ENV_PROXY') continue;
    env[k] = v;
  }
  for (const [k, v] of Object.entries(extra)) {
    if (v === undefined) delete env[k];
    else env[k] = String(v);
  }
  return env;
}

/** Runs a command; resolves with { code, signal, stdout, stderr, ms }. Kills it after timeoutMs. */
export function run(cmd, args, { env = childEnv(), timeoutMs = 30000, input } = {}) {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const child = spawn(cmd, args, { cwd: TEST_DIR, env, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout.setEncoding('utf8').on('data', (d) => { stdout += d; });
    child.stderr.setEncoding('utf8').on('data', (d) => { stderr += d; });
    const timer = setTimeout(() => child.kill('SIGKILL'), timeoutMs);
    child.on('error', (e) => {
      clearTimeout(timer);
      reject(e);
    });
    child.on('close', (code, signal) => {
      clearTimeout(timer);
      resolve({ code, signal, stdout, stderr, ms: Date.now() - started });
    });
    child.stdin.on('error', () => {});
    child.stdin.end(input ?? '');
  });
}

export const runNode = (args, opts) => run(process.execPath, args, opts);
export const runExample = (args, env, opts = {}) => runNode([EXAMPLE, ...args], { env: childEnv(env), ...opts });

/** 'python3' (or 'python') when a Python 3.8 or newer is on PATH, else null. */
export function findPython() {
  for (const cmd of ['python3', 'python']) {
    const r = spawnSync(cmd, ['-c', 'import sys; print(sys.version_info >= (3, 8))'], { encoding: 'utf8' });
    if (r.status === 0 && r.stdout.trim() === 'True') return cmd;
  }
  return null;
}

// --------------------------------------------------------------------------
// Ports, groups, handles
// --------------------------------------------------------------------------
function listen(server, port, host) {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, host, () => {
      server.removeListener('error', reject);
      resolve();
    });
  });
}

/** A TCP port on 127.0.0.1 that was free a moment ago (never 5000, 5001 or 8080). */
export async function freeTcpPort() {
  for (;;) {
    const s = net.createServer();
    await listen(s, 0, LOOPBACK);
    const { port } = s.address();
    await new Promise((resolve) => s.close(resolve));
    if (!FORBIDDEN_PORTS.has(port)) return port;
  }
}

/** A UDP socket bound to address:port (port 0: any free port). */
export function bindUdp(address, port = 0) {
  return new Promise((resolve, reject) => {
    const sock = dgram.createSocket({ type: 'udp4', reuseAddr: true });
    const fail = (e) => {
      try {
        sock.close();
      } catch {
        // never bound
      }
      reject(e);
    };
    sock.once('error', fail);
    sock.bind({ address, port }, () => {
      sock.removeListener('error', fail);
      sock.on('error', () => {});
      resolve(sock);
    });
  });
}

const closeUdp = (sock) => new Promise((resolve) => {
  try {
    sock.close(resolve);
  } catch {
    resolve();   // already closed
  }
});

/** A UDP port on 127.0.0.1 that was free a moment ago (never 5000, 5001 or 8080). */
export async function freeUdpPort() {
  for (;;) {
    const s = await bindUdp(LOOPBACK);
    const { port } = s.address();
    await closeUdp(s);
    if (!FORBIDDEN_PORTS.has(port)) return port;
  }
}

let groupCounter = 0;
/** A test multicast group 239.255.<100-254>.<1-254>. It depends on the process id, so test
 *  files that run side by side use different groups; their ports differ anyway. */
export function testGroup() {
  const n = process.pid * 13 + groupCounter++;
  return `239.255.${100 + (Math.floor(n / 254) % 155)}.${1 + (n % 254)}`;
}

/** How many of this process's open handles are of this type ('UDPWrap', 'TCPServerWrap'). */
export const handles = (type) => process.getActiveResourcesInfo().filter((t) => t === type).length;

// --------------------------------------------------------------------------
// Seeded random numbers (mulberry32)
// --------------------------------------------------------------------------
export function rng(seed) {
  let a = seed >>> 0;
  const next = () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const int = (n) => Math.floor(next() * n);
  return {
    next,
    int,
    pick: (arr) => arr[int(arr.length)],
    bytes: (n) => Buffer.from(Array.from({ length: n }, () => int(256))),
    u64: () => (BigInt(int(2 ** 32)) << 32n) | BigInt(int(2 ** 32)),
  };
}

// --------------------------------------------------------------------------
// Protobuf, written independently of the client so that it can check the client
// --------------------------------------------------------------------------
const U64 = (1n << 64n) - 1n;

/** A protobuf varint. Negative numbers are sign-extended to 64 bits (10 bytes), as protobuf
 *  encodes a negative int32. */
export function varint(value) {
  let n = BigInt(value);
  if (n < 0n) n &= U64;
  const out = [];
  do {
    let b = Number(n & 0x7fn);
    n >>= 7n;
    if (n) b |= 0x80;
    out.push(b);
  } while (n);
  return Buffer.from(out);
}

export const fieldKey = (tag, wire) => varint((BigInt(tag) << 3n) | BigInt(wire));
/** A varint field (written even when the value is 0). */
export const vfield = (tag, value) => Buffer.concat([fieldKey(tag, 0), varint(value)]);
/** A length-delimited field. */
export const lfield = (tag, bytes) => {
  const b = typeof bytes === 'string' ? Buffer.from(bytes, 'utf8') : Buffer.from(bytes);
  return Buffer.concat([fieldKey(tag, 2), varint(b.length), b]);
};
export const hex = (s) => Buffer.from(s.replace(/\s+/g, ''), 'hex');

/** DeviceResponse{timestamp, code = ACK, ack_payload = {success}}, as a wearable sends it. */
export function ackMessage(timestampMs, success = true) {
  return Buffer.concat([vfield(1, timestampMs), vfield(2, 1), lfield(3, success ? vfield(1, 1) : Buffer.alloc(0))]);
}

/** DeviceResponse{timestamp, code = HEARTBEAT, heartbeat_payload}. */
export function heartbeatMessage(timestampMs, {
  deviceId = 'js-wearable', battery = 87, rssi = -62, uptime = 3600, appState = 1, deviceState = 2,
} = {}) {
  const hb = Buffer.concat([lfield(1, deviceId), vfield(2, battery), vfield(3, rssi), vfield(4, uptime),
    vfield(5, appState), vfield(6, deviceState)]);
  return Buffer.concat([vfield(1, timestampMs), vfield(2, 2), lfield(4, hb)]);
}

/** What a wearable makes of a datagram: { kind: 'ALERT' | 'HEARTBEAT_REQ' | null, format,
 *  timestamp }, read the way docs/PROTOCOL.md describes it. */
export function classifyDatagram(data) {
  if (data.length === 8) return { kind: 'ALERT', format: 'raw8', seconds: data.readDoubleLE(0) };
  const f = new Map();
  let i = 0;
  const read = () => {
    let shift = 0n;
    let v = 0n;
    for (;;) {
      if (i >= data.length || shift > 63n) throw new Error('bad varint');
      const b = data[i++];
      v |= BigInt(b & 0x7f) << shift;
      if (!(b & 0x80)) return v;
      shift += 7n;
    }
  };
  try {
    while (i < data.length) {
      const k = read();
      if ((k & 7n) !== 0n) return { kind: null };
      f.set(k >> 3n, read());
    }
  } catch {
    return { kind: null };
  }
  const kinds = { 1: 'ALERT', 2: 'HEARTBEAT_REQ' };
  return { kind: kinds[String(f.get(2n) ?? 0n)] ?? null, format: 'proto', timestamp: f.get(1n) ?? 0n };
}

/** Decoded values as testdata/vectors.json writes them: integers as decimal strings. */
export function norm(v) {
  if (v === null || typeof v === 'boolean' || typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'bigint') return String(v);
  return Object.fromEntries(Object.keys(v).sort().map((k) => [k, norm(v[k])]));
}

// --------------------------------------------------------------------------
// Fuzz inputs for decodeDeviceResponse
// --------------------------------------------------------------------------
const EDGE_INTS = [0n, 1n, 127n, 128n, 255n, 16383n, 16384n, 2n ** 31n - 1n, 2n ** 31n, 2n ** 32n - 1n, 2n ** 32n,
  2n ** 53n - 1n, 2n ** 53n, 2n ** 63n, U64, -1n, -62n, -(2n ** 31n)];

function structured(r, depth) {
  const parts = [];
  const n = r.int(7);
  for (let k = 0; k < n; k++) {
    const tag = r.pick([1, 2, 3, 4, 5, 6, 1, 2, 3, 4, r.int(40), 1000, 2 ** 29 - 1]);
    const wire = r.pick([0, 0, 0, 2, 2, 2, 1, 5, 3, 4, 6, 7]);
    if (wire === 0) {
      parts.push(fieldKey(tag, 0), varint(r.next() < 0.5 ? r.pick(EDGE_INTS) : r.u64()));
    } else if (wire === 2) {
      let body;
      if (depth < 2 && r.next() < 0.6) body = structured(r, depth + 1);
      else if (r.next() < 0.5) body = Buffer.from(r.pick(['wz-07', 'fake-01', '', 'ü', '\u{1F6A7}', '﻿id']), 'utf8');
      else body = r.bytes(r.int(20));
      const len = r.next() < 0.1 ? Math.max(0, body.length + r.int(7) - 3) : body.length;   // sometimes wrong
      parts.push(fieldKey(tag, 2), varint(len), body);
    } else if (wire === 1 || wire === 5) {
      parts.push(fieldKey(tag, wire), r.bytes(wire === 1 ? 8 : 4));
    } else {
      parts.push(fieldKey(tag, wire), r.bytes(r.int(4)));
    }
  }
  return Buffer.concat(parts);
}

function mutate(r, seed, seeds) {
  let b = Buffer.from(seed);
  const ops = 1 + r.int(3);
  for (let k = 0; k < ops; k++) {
    const at = r.int(b.length + 1);
    switch (r.int(9)) {
      case 0:   // flip a bit
        if (b.length) b[r.int(b.length)] ^= 1 << r.int(8);
        break;
      case 1:   // overwrite a byte
        if (b.length) b[r.int(b.length)] = r.int(256);
        break;
      case 2:   // insert bytes
        b = Buffer.concat([b.subarray(0, at), r.bytes(1 + r.int(4)), b.subarray(at)]);
        break;
      case 3:   // delete bytes
        b = Buffer.concat([b.subarray(0, at), b.subarray(at + 1 + r.int(4))]);
        break;
      case 4:   // truncate
        b = b.subarray(0, at);
        break;
      case 5:   // repeat a slice
        b = Buffer.concat([b.subarray(0, at), b.subarray(r.int(b.length + 1)), b.subarray(at)]);
        break;
      case 6:   // splice with another valid message
        b = Buffer.concat([b.subarray(0, at), r.pick(seeds).subarray(r.int(8))]);
        break;
      case 7:   // a varint edge byte
        if (b.length) b[r.int(b.length)] = r.pick([0x00, 0x7f, 0x80, 0xff]);
        break;
      default:  // append random bytes
        b = Buffer.concat([b, r.bytes(1 + r.int(12))]);
    }
  }
  return b;
}

/** Deterministic fuzz inputs (hex strings): random bytes, random protobuf-shaped messages,
 *  and mutations of valid replies. The same seed always gives the same inputs. */
export function fuzzCases(seed, { random = 3000, mutated = 3000 } = {}) {
  const r = rng(seed);
  const seeds = [
    hex('08a3d1eab6b73310011a020801'),
    hex('08fbd0eab6b7331002221b0a05777a2d3037105718c2ffffffffffffffff0120901c28013002'),
    ackMessage(1767225600163, false),
    heartbeatMessage(0, { deviceId: 'wz-ü', battery: -1, rssi: -(2 ** 31), uptime: 2 ** 32 + 5 }),
    Buffer.concat([vfield(9, 5), lfield(10, 'x'), ackMessage(2n ** 63n)]),
    Buffer.concat([heartbeatMessage(1, { deviceId: '﻿wz' }), ackMessage(2)]),
  ];
  const cases = [];
  for (let k = 0; k < random; k++) cases.push(k % 2 ? r.bytes(r.int(48)) : structured(r, 0));
  for (let k = 0; k < mutated; k++) cases.push(mutate(r, r.pick(seeds), seeds));
  return cases.map((b) => b.toString('hex'));
}

// --------------------------------------------------------------------------
// HTTP stub of the gateway
// --------------------------------------------------------------------------
export const authorized = (req) => req.headers.authorization === `Bearer ${TOKEN}`;

/** Answers like the gateway: JSON, a Content-Length, and the connection closed afterwards
 *  (the gateway answers with HTTP/1.0 and closes after each response). */
export function respond(res, status, payload, headers = {}) {
  if (res.destroyed || res.socket?.destroyed) return;
  res.shouldKeepAlive = false;
  const body = Buffer.isBuffer(payload) || typeof payload === 'string' ? Buffer.from(payload) : Buffer.from(JSON.stringify(payload));
  res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': body.length, ...headers });
  res.end(body);
}

export function defaultRoutes() {
  return {
    'GET /health': { json: { ok: true, version: '1.0.0', egress: LOOPBACK, error: null, listener: true } },
    'GET /wearables': {
      json: { devices: [{ ip: '127.0.0.1', acks: 2, heartbeats: 1 }, { ip: '127.0.0.2', acks: 3, heartbeats: 0 }], recent: [] },
    },
    'POST /alert': { json: { event: 'alert_sent', ts_ms: 1767225600123, format: 'raw8', egress: LOOPBACK, datagrams: 1 } },
  };
}

/** An HTTP stub of the gateway on 127.0.0.1. A route is `METHOD path` -> { status, json | raw,
 *  delayMs, headers } or a function (req, res, entry). POST /alert with status 200 answers 401
 *  unless the request carries `Bearer good-token`, as the gateway does. */
export async function startStub() {
  const requests = [];
  const sockets = new Set();
  let routes = defaultRoutes();
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const entry = { method: req.method, path: req.url, headers: req.headers, body: Buffer.concat(chunks).toString('utf8') };
    requests.push(entry);
    const route = routes[`${req.method} ${req.url}`] ?? { status: 404, json: { error: `${req.method} ${req.url}` } };
    if (typeof route === 'function') {
      route(req, res, entry);
      return;
    }
    let { status = 200, json = {}, raw } = route;
    if (req.method === 'POST' && req.url === '/alert' && status === 200 && !authorized(req)) {
      status = 401;
      json = { error: 'missing or wrong Bearer token' };
      raw = undefined;
    }
    if (route.delayMs) await sleep(route.delayMs);
    respond(res, status, raw ?? json, route.headers);
  });
  server.on('connection', (s) => {
    sockets.add(s);
    s.on('close', () => sockets.delete(s));
  });
  await listen(server, 0, LOOPBACK);
  return {
    url: `http://${LOOPBACK}:${server.address().port}`,
    requests,
    route(key, spec) {
      routes[key] = spec;
    },
    reset() {
      routes = defaultRoutes();
      requests.length = 0;
    },
    /** Requests other than the barrier request. Any request a finished child process made
     *  reaches the stub before a request made after it ended. */
    async requestsAfterBarrier() {
      routes['GET /barrier'] = { json: {} };
      await fetch(`http://${LOOPBACK}:${server.address().port}/barrier`).then((r) => r.text());
      return requests.filter((r) => r.path !== '/barrier');
    },
    async close() {
      for (const s of sockets) s.destroy();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

// --------------------------------------------------------------------------
// Loopback multicast
// --------------------------------------------------------------------------
/** A UDP socket joined to `group` on 127.0.0.1, on a free port. Collects { data, from }. */
export async function multicastReceiver(group, onMessage) {
  let sock;
  for (;;) {
    try {
      sock = await bindUdp(group);         // only this group's datagrams, where the OS allows it
    } catch {
      sock = await bindUdp('0.0.0.0');     // Windows cannot bind a group address
    }
    if (!FORBIDDEN_PORTS.has(sock.address().port)) break;
    await closeUdp(sock);
  }
  try {
    sock.addMembership(group, LOOPBACK);
  } catch (e) {
    await closeUdp(sock);
    throw e;
  }
  const messages = [];
  sock.on('message', (data, rinfo) => {
    const m = { data: Buffer.from(data), from: rinfo.address };
    messages.push(m);
    onMessage?.(m);
  });
  return { group, port: sock.address().port, messages, close: () => closeUdp(sock) };
}

/** Sends one datagram to group:port from 127.0.0.1, without the client under test. */
export async function sendMulticast(group, port, data) {
  const tx = await bindUdp(LOOPBACK);
  try {
    tx.setMulticastInterface(LOOPBACK);
    tx.setMulticastTTL(1);
    await new Promise((resolve, reject) => tx.send(data, port, group, (e) => (e ? reject(e) : resolve())));
  } finally {
    await closeUdp(tx);
  }
}

let probe;
/** Whether multicast from 127.0.0.1 to a test group comes back on loopback. Checked once, with
 *  plain sockets, so that a fault in the client is never mistaken for missing multicast. */
export function multicastStatus() {
  probe ??= (async () => {
    let rx;
    try {
      const group = testGroup();
      rx = await multicastReceiver(group);
      await sendMulticast(group, rx.port, Buffer.from('probe'));
      await waitUntil(() => rx.messages.length > 0, { timeoutMs: 2000, what: `a datagram to ${group} on ${LOOPBACK}` });
      return { ok: true, reason: '' };
    } catch (e) {
      return { ok: false, reason: e.message };
    } finally {
      await rx?.close();
    }
  })();
  return probe;
}

const MULTICAST_HINT = 'On Linux: sudo ip link set lo multicast on && sudo ip route add 239.0.0.0/8 dev lo. '
  + 'Set HALOW_TEST_REQUIRE_MULTICAST=1 to make this a failure instead of a skip.';

/** { ok, it }: whether loopback multicast works, and node:test's it() for a test that needs
 *  it. Without it the test is skipped, or fails when HALOW_TEST_REQUIRE_MULTICAST=1. */
export async function multicastGate() {
  const st = await multicastStatus();
  const skip = st.ok || REQUIRE_MULTICAST ? false : `loopback multicast does not work here (${st.reason}). ${MULTICAST_HINT}`;
  const check = () => {
    if (!st.ok) assert.fail(`loopback multicast is required (HALOW_TEST_REQUIRE_MULTICAST=1) but does not work: ${st.reason}`);
  };
  return {
    ok: st.ok,
    it(name, opts, fn) {
      if (typeof opts === 'function') [opts, fn] = [{}, opts];
      return it(name, { ...opts, skip: opts.skip || skip }, (t) => {
        check();
        return fn(t);
      });
    },
  };
}

// --------------------------------------------------------------------------
// TCP: playing the wearable's reply
// --------------------------------------------------------------------------
/** Connects to 127.0.0.1:port, writes data and closes (end) unless keepOpen. Resolves with the
 *  socket once connected and written; `whenClosed` resolves when the connection is gone. */
export function tcpSend(port, data, { keepOpen = false } = {}) {
  return new Promise((resolve, reject) => {
    const sock = net.connect({ port, host: LOOPBACK });
    sock.whenClosed = new Promise((r) => sock.once('close', r));
    sock.on('error', () => {});   // a reset by the listener is expected in some tests
    sock.once('connect', () => {
      sock.removeListener('error', reject);
      const done = () => resolve(sock);
      if (data?.length) {
        if (keepOpen) sock.write(data, done);
        else sock.end(data, done);
      } else {
        if (!keepOpen) sock.end();
        done();
      }
    });
    sock.once('error', reject);
  });
}

/** Sends one reply and waits until the connection has closed. */
export async function reply(port, data) {
  const sock = await tcpSend(port, data);
  await sock.whenClosed;
}

/** A stand-in wearable in this process: joins `group` on 127.0.0.1 and answers on TCP
 *  127.0.0.1:replyPort, a heartbeat for each HEARTBEAT_REQ and an ACK for the first alert. */
export async function startWearable({ group, replyPort, deviceId = 'js-wearable', heartbeats = true, acks = true }) {
  const seen = [];
  let acked = false;
  const rx = await multicastReceiver(group, (m) => {
    const c = classifyDatagram(m.data);
    seen.push({ ...c, from: m.from, data: m.data });
    if (c.kind === 'HEARTBEAT_REQ' && heartbeats) {
      reply(replyPort, heartbeatMessage(c.timestamp, { deviceId })).catch(() => {});
    } else if (c.kind === 'ALERT' && acks && !acked) {
      acked = true;
      reply(replyPort, ackMessage(Date.now())).catch(() => {});
    }
  });
  return { port: rx.port, seen, close: rx.close };
}
