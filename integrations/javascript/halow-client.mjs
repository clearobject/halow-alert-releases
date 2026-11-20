// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// halow-client.mjs - raise ClearZone wearable alerts from your own software.
//
// Node.js 18+, ES module, no dependencies (built-in fetch, dgram and net).
//
// Two ways to raise an alert:
//   Gateway (HTTP)  Call the halow-alert service on the Linux gateway host that has the
//                   HaLowLink attached: POST /alert with the gateway's bearer token.
//   Direct (UDP)    From a machine that is itself plugged into the HaLowLink (address
//                   192.168.12.50), send the alert datagram to the wearables yourself with
//                   UDP multicast, and optionally collect their replies on TCP 5001.
//
// An alert makes every wearable on the HaLowLink network vibrate and sound. Warn the
// people nearby first.
//
//   import { GatewayClient, sendDirect, listenForResponses } from './halow-client.mjs';
//   await new GatewayClient().alert({ label: 'zone-3' });   // HALOW_URL, HALOW_TOKEN
//   await sendDirect({ egress: '192.168.12.50' });           // direct mode, raw8

import dgram from 'node:dgram';
import net from 'node:net';

export const VERSION = '0.1.0';
export const DEFAULT_URL = 'http://127.0.0.1:8080';
export const DEFAULT_EGRESS = '192.168.12.50';
export const GROUP = '239.255.0.10';
export const PORT = 5000;
export const RESPONSE_PORT = 5001;
export const TTL = 4;
export const FORMATS = ['raw8', 'proto', 'both'];
export const CMD_ALERT = 1;
export const CMD_HEARTBEAT_REQ = 2;

const MAX_BURST_S = 5.0;
const MIN_INTERVAL_S = 0.02;
const RESPONSE_DEADLINE_MS = 5000;
const MAX_RESPONSE_BYTES = 8192;
const MAX_RESPONSE_CONNS = 32;
const U32 = 0xffffffffn;
const U64 = 0xffffffffffffffffn;

function env(name, fallback) {
  const v = (process.env[name] ?? '').trim();
  return v === '' ? fallback : v;
}

export function envPort(name, fallback) {
  const raw = env(name, '');
  if (raw === '') return fallback;
  const n = Number(raw);
  if (!/^\d+$/.test(raw) || n < 1 || n > 65535) {
    throw new RangeError(`${name}=${JSON.stringify(raw)} is not a port number (1-65535)`);
  }
  return n;
}

// --------------------------------------------------------------------------
// Wire formats (byte-identical to the gateway; see docs/PROTOCOL.md)
// --------------------------------------------------------------------------
function varint(value) {
  let n = BigInt(value);
  if (n < 0n) throw new RangeError('varint must not be negative');
  const out = [];
  for (;;) {
    const b = Number(n & 0x7fn);
    n >>= 7n;
    out.push(b | (n ? 0x80 : 0));
    if (!n) return out;
  }
}

/** The raw8 alert: Unix time in seconds as a little-endian IEEE-754 double (8 bytes). */
export function encodeRaw8(seconds) {
  const b = Buffer.alloc(8);
  b.writeDoubleLE(seconds, 0);
  return b;
}

/** protobuf GatewayMessage {uint64 timestamp = 1; CommandCode command = 2}; zero fields are left out.
 *  timestampMs and command may be numbers or BigInts. */
export function encodeGatewayMessage(timestampMs, command) {
  const out = [];
  if (BigInt(timestampMs) !== 0n) out.push(0x08, ...varint(timestampMs));
  if (BigInt(command) !== 0n) out.push(0x10, ...varint(command));
  return Buffer.from(out);
}

function readVarint(buf, i) {
  let shift = 0n;
  let result = 0n;
  for (;;) {
    if (shift >= 64n) throw new RangeError('varint longer than 10 bytes');
    if (i >= buf.length) throw new RangeError('truncated varint');
    const b = buf[i];
    i += 1;
    result |= BigInt(b & 0x7f) << shift;
    if (!(b & 0x80)) return [result, i];
    shift += 7n;
  }
}

// Field number (BigInt, as a string key) -> BigInt (varint) or Buffer (other wire types).
function fields(buf) {
  const out = new Map();
  let i = 0;
  while (i < buf.length) {
    let key;
    [key, i] = readVarint(buf, i);
    const tag = key >> 3n;
    const wire = Number(key & 7n);
    let v;
    if (wire === 0) {
      [v, i] = readVarint(buf, i);
    } else if (wire === 1 || wire === 2 || wire === 5) {
      let len;
      if (wire === 2) {
        [len, i] = readVarint(buf, i);
      } else {
        len = wire === 1 ? 8n : 4n;
      }
      if (BigInt(i) + len > BigInt(buf.length)) {
        throw new RangeError(`field ${tag} runs past the end of the message`);
      }
      v = buf.subarray(i, i + Number(len));
      i += Number(len);
    } else {
      throw new RangeError(`wire type ${wire}`);
    }
    out.set(tag.toString(), v);
  }
  return out;
}

function getInt(f, tag) {
  const v = f.has(tag) ? f.get(tag) : 0n;
  if (typeof v !== 'bigint') throw new RangeError(`field ${tag}: wrong wire type`);
  return v;
}

function getBytes(f, tag) {
  const v = f.has(tag) ? f.get(tag) : Buffer.alloc(0);
  if (!Buffer.isBuffer(v)) throw new RangeError(`field ${tag}: wrong wire type`);
  return v;
}

function int32(v) {
  const x = v & U32;
  return Number(x >= 0x80000000n ? x - 0x100000000n : x);
}

const RSP_NAMES = new Map([[0n, 'UNKNOWN'], [1n, 'ACK'], [2n, 'HEARTBEAT']]);
const utf8 = new TextDecoder('utf-8', { ignoreBOM: true });   // keep a BOM, as the gateway does

/** A wearable reply (protobuf DeviceResponse) as an object:
 *  { timestamp_ms, code: 'ACK' | 'HEARTBEAT' | ..., ack_success?, heartbeat? }.
 *  timestamp_ms is a Number (a BigInt only if it exceeds 2^53). Throws RangeError when malformed. */
export function decodeDeviceResponse(buf) {
  const f = fields(Buffer.from(buf));
  const code = getInt(f, '2');
  const ts = getInt(f, '1') & U64;
  const r = {
    timestamp_ms: ts <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(ts) : ts,
    code: RSP_NAMES.get(code) ?? code.toString(),
  };
  if (f.has('3')) r.ack_success = getInt(fields(getBytes(f, '3')), '1') !== 0n;
  if (f.has('4')) {
    const h = fields(getBytes(f, '4'));
    r.heartbeat = {
      device_id: utf8.decode(getBytes(h, '1')),
      battery_percentage: int32(getInt(h, '2')),
      rssi: int32(getInt(h, '3')),
      uptime_seconds: Number(getInt(h, '4') & U32),
      app_state: Number(getInt(h, '5') & U32),
      device_state: Number(getInt(h, '6') & U32),
    };
  }
  return r;
}

// --------------------------------------------------------------------------
// Gateway mode: the halow-alert HTTP API
// --------------------------------------------------------------------------
const STATUS_HINTS = {
  400: 'the gateway rejected the request',
  401: "missing or wrong token: set HALOW_TOKEN to the gateway's token",
  404: 'unknown path: is HALOW_URL the halow-alert gateway (http://<host>:8080)?',
  413: 'request body too large',
  429: 'rate limited: the gateway sends at most one alert every 0.5 s; retry shortly',
  500: 'the gateway could not send the alert (HaLowLink unplugged or not set up? see GET /health)',
  503: 'the gateway cannot send right now (no HaLowLink address on the gateway host, or its reply listener stopped; see GET /health)',
};

/** A failed call. `status` is the HTTP status (undefined when the gateway was not reached). */
export class HalowError extends Error {
  constructor(message, { status, body } = {}) {
    super(message);
    this.name = 'HalowError';
    this.status = status;
    this.body = body ?? {};
  }
}

/** Client for the halow-alert service: POST /alert, GET /wearables, GET /health.
 *  url defaults to HALOW_URL (else http://127.0.0.1:8080), token to HALOW_TOKEN. */
export class GatewayClient {
  constructor({ url, token, timeoutMs = 10000 } = {}) {
    this.url = (url || env('HALOW_URL', DEFAULT_URL)).replace(/\/+$/, '');
    if (!/^https?:\/\//.test(this.url)) {
      throw new TypeError(`gateway URL must start with http:// or https://, not ${JSON.stringify(this.url)}`);
    }
    this.token = (token ?? process.env.HALOW_TOKEN ?? '').trim();
    this.timeoutMs = timeoutMs;
  }

  async #call(method, path, { body, ok = [200], timeoutMs } = {}) {
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (method === 'POST' && this.token) headers.Authorization = `Bearer ${this.token}`;
    let res;
    let text;
    try {
      res = await fetch(this.url + path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(timeoutMs ?? this.timeoutMs),
        redirect: 'error',
      });
      text = await res.text();
    } catch (e) {
      const why = e?.name === 'TimeoutError' ? 'timed out' : (e?.cause?.code || e?.cause?.message || e?.message);
      throw new HalowError(`cannot reach the gateway at ${this.url}: ${why}`);
    }
    let parsed = {};
    if (text.trim()) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = { error: text.slice(0, 500) };
      }
    }
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) parsed = { value: parsed };
    if (ok.includes(res.status)) return parsed;
    const hint = STATUS_HINTS[res.status] ?? 'unexpected response';
    const detail = parsed.error ? ` (${parsed.error})` : '';
    throw new HalowError(`${method} ${path}: HTTP ${res.status}: ${hint}${detail}`, { status: res.status, body: parsed });
  }

  /** GET /health. Resolves with the body for 200 and for 503; check its `ok` field. */
  health() {
    return this.#call('GET', '/health', { ok: [200, 503] });
  }

  /** GET /wearables: { devices: [...], recent: [...] }. */
  wearables() {
    return this.#call('GET', '/wearables');
  }

  /** POST /alert. Every wearable on the HaLowLink network alerts. Resolves with the gateway's
   *  send record; rejects with HalowError (status 401, 429, 500, ...). */
  alert({ label, format, burstSeconds } = {}) {
    const body = {};
    if (label !== undefined && label !== null) body.label = String(label);
    if (format !== undefined && format !== null) {
      if (!FORMATS.includes(String(format).toLowerCase())) throw new RangeError('format must be raw8, proto or both');
      body.format = String(format).toLowerCase();
    }
    let timeoutMs = this.timeoutMs;
    if (burstSeconds !== undefined && burstSeconds !== null) {
      if (!Number.isFinite(burstSeconds) || burstSeconds < 0) throw new RangeError('burstSeconds must be a number from 0 to 5');
      body.burst_seconds = burstSeconds;
      timeoutMs += Math.min(burstSeconds, MAX_BURST_S) * 1000;   // the gateway answers when the burst ends
    }
    return this.#call('POST', '/alert', { body, timeoutMs });
  }
}

/** The sum of ACKs over every wearable in a GET /wearables body. */
export function totalAcks(wearables) {
  return (wearables?.devices ?? []).reduce((n, d) => n + Number(d.acks ?? 0), 0);
}

// --------------------------------------------------------------------------
// Direct mode: UDP multicast to the wearables, replies on TCP
// --------------------------------------------------------------------------
function checkIPv4(addr) {
  if (net.isIPv4(addr)) return addr;
  throw new TypeError('the egress address must be an IPv4 address of this machine, for example 192.168.12.50');
}

// A UDP socket bound to `egress` that sends multicast from it (IP_MULTICAST_IF).
function mcastSocket(egress, ttl) {
  checkIPv4(egress);
  return new Promise((resolve, reject) => {
    const s = dgram.createSocket({ type: 'udp4' });
    const fail = (e) => {
      try {
        s.close();
      } catch {
        // already closed
      }
      reject(new Error(`cannot send from ${egress}: ${e.code || e.message}. Is it an address of this machine (the HaLowLink plugged in and the address set)?`));
    };
    s.once('error', fail);
    s.bind({ address: egress, port: 0, exclusive: true }, () => {
      try {
        s.setMulticastInterface(egress);
        s.setMulticastTTL(ttl);
      } catch (e) {
        fail(e);
        return;
      }
      s.removeListener('error', fail);
      resolve(s);
    });
  });
}

function sendOne(s, payload, group, port) {
  return new Promise((resolve, reject) => {
    s.send(payload, port, group, (err) => (err ? reject(err) : resolve()));
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Send one alert straight to the wearables. Every wearable on the network alerts.
 *  egress: this machine's address on the HaLowLink (default HALOW_EGRESS, else 192.168.12.50).
 *  burstSeconds > 0 resends the same bytes every burstIntervalMs for that long (max 5 s).
 *  Resolves with a record like the gateway's. Does not wait for ACKs (see listenForResponses). */
export async function sendDirect({
  egress, format = 'raw8', group = GROUP, port = PORT, ttl = TTL, burstSeconds = 0, burstIntervalMs = 100,
} = {}) {
  egress = egress || env('HALOW_EGRESS', DEFAULT_EGRESS);
  format = String(format || 'raw8').toLowerCase();
  if (!FORMATS.includes(format)) throw new RangeError('format must be raw8, proto or both');
  const s = await mcastSocket(egress, ttl);
  const nowMs = Date.now();
  const start = performance.now();
  const payloads = [];
  if (format === 'raw8' || format === 'both') payloads.push(encodeRaw8(nowMs / 1000));
  if (format === 'proto' || format === 'both') payloads.push(encodeGatewayMessage(nowMs, CMD_ALERT));
  let sent = 0;
  try {
    for (const p of payloads) {
      await sendOne(s, p, group, port);
      sent += 1;
    }
    const burst = Math.min(Math.max(Number(burstSeconds) || 0, 0), MAX_BURST_S);
    if (burst) {
      const interval = Math.max(MIN_INTERVAL_S, burstIntervalMs / 1000) * 1000;
      const end = start + burst * 1000;
      while (performance.now() + interval < end) {
        await sleep(interval);
        for (const p of payloads) {
          await sendOne(s, p, group, port);
          sent += 1;
        }
      }
    }
  } finally {
    s.close();
  }
  return { event: 'alert_sent', ts_ms: nowMs, format, group: `${group}:${port}`, egress, datagrams: sent };
}

/** Ask every wearable to report in (battery, RSSI); they do not buzz. Resolves with the
 *  request's timestamp in ms, which the wearables echo back in their heartbeat. */
export async function sendHeartbeatRequest({ egress, group = GROUP, port = PORT, ttl = TTL } = {}) {
  egress = egress || env('HALOW_EGRESS', DEFAULT_EGRESS);
  const ts = Date.now();
  const s = await mcastSocket(egress, ttl);
  try {
    await sendOne(s, encodeGatewayMessage(ts, CMD_HEARTBEAT_REQ), group, port);
  } finally {
    s.close();
  }
  return ts;
}

/** '192.168.12.' for 192.168.12.50: replies are accepted from this /24 only. */
export function subnetPrefix(addr) {
  return addr.slice(0, addr.lastIndexOf('.') + 1);
}

/** Collect wearable replies on TCP (port 5001): each connection carries one DeviceResponse,
 *  an ACK or a HEARTBEAT. Resolves once listening with
 *  { responses, waitFor(code, timeoutMs, afterMs), close() }. Each reply gets `ip` (the
 *  wearable) and `received_ms`. Only one program on a machine can listen on the port: stop
 *  the gateway service there first. */
export function listenForResponses({ port = RESPONSE_PORT, host = '0.0.0.0', allowPrefix = '', onResponse } = {}) {
  const responses = [];
  const waiters = new Set();
  let active = 0;

  const deliver = (r) => {
    responses.push(r);
    if (responses.length > 1000) responses.splice(0, responses.length - 1000);
    for (const w of [...waiters]) w(r);
    if (onResponse) {
      try {
        onResponse(r);
      } catch (e) {
        console.error(`halow-client: onResponse failed: ${e.message}`);
      }
    }
  };

  const server = net.createServer((sock) => {
    const ip = (sock.remoteAddress || '').replace(/^::ffff:/, '');
    if ((allowPrefix && !ip.startsWith(allowPrefix)) || active >= MAX_RESPONSE_CONNS) {
      sock.destroy();
      return;
    }
    active += 1;
    const chunks = [];
    let size = 0;
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      active -= 1;
      clearTimeout(timer);
      sock.destroy();
      if (!size) return;
      let r;
      try {
        r = decodeDeviceResponse(Buffer.concat(chunks).subarray(0, MAX_RESPONSE_BYTES));
      } catch {
        return;
      }
      if (r.code !== 'ACK' && r.code !== 'HEARTBEAT') return;
      deliver({ ...r, ip, received_ms: Date.now() });
    };
    const timer = setTimeout(finish, RESPONSE_DEADLINE_MS);   // for the whole connection
    sock.on('data', (c) => {
      chunks.push(c);
      size += c.length;
      if (size >= MAX_RESPONSE_BYTES) finish();
    });
    sock.on('end', finish);
    sock.on('close', finish);
    sock.on('error', () => {});
  });

  return new Promise((resolve, reject) => {
    server.once('error', (e) => {
      reject(new Error(`cannot listen on TCP ${port} for wearable replies: ${e.code || e.message} (is the gateway service or another listener running on this machine?)`));
    });
    server.listen({ port, host, exclusive: true }, () => {
      server.removeAllListeners('error');
      server.on('error', (e) => console.error(`halow-client: reply listener: ${e.message}`));
      resolve({
        responses,
        /** The first reply with this code ('ACK' or 'HEARTBEAT') received at or after afterMs,
         *  waiting up to timeoutMs; null if none came. */
        waitFor(code = 'ACK', timeoutMs = 10000, afterMs = 0) {
          const match = (r) => r.code === code && r.received_ms >= afterMs;
          const found = responses.find(match);
          if (found) return Promise.resolve(found);
          return new Promise((res) => {
            const t = setTimeout(() => {
              waiters.delete(w);
              res(null);
            }, timeoutMs);
            const w = (r) => {
              if (!match(r)) return;
              clearTimeout(t);
              waiters.delete(w);
              res(r);
            };
            waiters.add(w);
          });
        },
        close() {
          return new Promise((res) => server.close(() => res()));
        },
      });
    });
  });
}
