// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// Worker thread for the decoder fuzz test in codec.test.mjs. It decodes every input it is
// given and posts back one result per input. It runs in a worker so that the test can stop
// it, and fail, if the decoder ever hangs.

import { parentPort, workerData } from 'node:worker_threads';
import { decodeDeviceResponse } from '../halow-client.mjs';
import { norm } from './helpers.mjs';

// The decoder's own error messages. A RangeError with any other message (for example from a
// Buffer or BigInt operation) means the decoder failed in a way it did not intend to.
const OWN_ERRORS = /^(varint longer than 10 bytes|truncated varint|field \d+ runs past the end of the message|wire type [3467]|field \d+: wrong wire type)$/;
const U64 = (1n << 64n) - 1n;
const INT32 = [-(2 ** 31), 2 ** 31 - 1];
const UINT32 = [0, 2 ** 32 - 1];

function inRange(v, [lo, hi]) {
  return Number.isInteger(v) && v >= lo && v <= hi;
}

// Why a decoded reply does not have the documented shape, or '' when it does.
function shapeProblem(r) {
  const keys = Object.keys(r);
  if (!keys.every((k) => ['timestamp_ms', 'code', 'ack_success', 'heartbeat'].includes(k))) return `keys ${keys}`;
  const ts = r.timestamp_ms;
  if (typeof ts === 'number' ? !(Number.isSafeInteger(ts) && ts >= 0) : !(typeof ts === 'bigint' && ts > BigInt(Number.MAX_SAFE_INTEGER) && ts <= U64)) {
    return `timestamp_ms ${ts}`;
  }
  if (typeof r.code !== 'string' || !/^(UNKNOWN|ACK|HEARTBEAT|\d+)$/.test(r.code)) return `code ${r.code}`;
  if ('ack_success' in r && typeof r.ack_success !== 'boolean') return 'ack_success';
  if ('heartbeat' in r) {
    const h = r.heartbeat;
    if (typeof h.device_id !== 'string') return 'device_id';
    if (!inRange(h.battery_percentage, INT32) || !inRange(h.rssi, INT32)) return 'int32 field';
    if (![h.uptime_seconds, h.app_state, h.device_state].every((v) => inRange(v, UINT32))) return 'uint32 field';
  }
  return '';
}

const results = [];
let decoded = 0;
let rejected = 0;
for (const h of workerData.cases) {
  try {
    const r = decodeDeviceResponse(Buffer.from(h, 'hex'));
    const problem = shapeProblem(r);
    if (problem) {
      results.push({ hex: h, unexpected: `bad shape: ${problem}` });
    } else {
      results.push({ hex: h, result: norm(r) });
      decoded += 1;
    }
  } catch (e) {
    if (e instanceof RangeError && OWN_ERRORS.test(e.message)) {
      results.push({ hex: h, result: null });
      rejected += 1;
    } else {
      results.push({ hex: h, unexpected: `${e?.name}: ${e?.message}` });
    }
  }
}
parentPort.postMessage({ results, decoded, rejected });
