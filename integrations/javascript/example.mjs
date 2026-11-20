// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// Raise one alert from your own code, through the gateway (HTTP) or directly (UDP).
//
// WARNING: every wearable on the HaLowLink network vibrates and sounds. Warn people first.
//
// Gateway mode (any machine that can reach the gateway):
//   HALOW_URL=http://<gateway>:8080 HALOW_TOKEN=<token> node example.mjs
// Direct mode (this machine is plugged into the HaLowLink, address 192.168.12.50):
//   HALOW_EGRESS=192.168.12.50 node example.mjs --direct
// Codec self-test (sends nothing):
//   node example.mjs --selftest
//
// Exit code: 0 the alert was sent and a wearable ACKed it; 3 the alert was sent but no ACK
// came within HALOW_ACK_WAIT seconds (default 15; a wearable ACKs only after its alarm
// sequence ends); 1 the alert failed; 2 bad settings.

import { readFileSync } from 'node:fs';
import {
  DEFAULT_EGRESS, GROUP, PORT, RESPONSE_PORT, GatewayClient, HalowError, decodeDeviceResponse,
  encodeGatewayMessage, encodeRaw8, envPort, listenForResponses, sendDirect, sendHeartbeatRequest,
  subnetPrefix, totalAcks,
} from './halow-client.mjs';

let ACK_WAIT_MS = 15000;
// HALOW_ACK_WAIT in seconds (default 15), checked before anything is sent.
function ackWaitMs() {
  const raw = (process.env.HALOW_ACK_WAIT || '').trim() || '15';
  const s = Number(raw);
  if (!Number.isFinite(s) || s < 0 || s > 3600) throw new Error(`HALOW_ACK_WAIT="${raw}" must be a number of seconds (0 to 3600)`);
  return s * 1000;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function viaGateway() {
  const client = new GatewayClient();            // HALOW_URL, HALOW_TOKEN
  const health = await client.health();
  console.log('gateway health:', JSON.stringify(health));
  if (!health.ok) {
    console.error(`the gateway cannot send right now: ${health.error}`);
    return 1;
  }
  const before = totalAcks(await client.wearables());

  const record = await client.alert({ label: 'javascript example' });   // the wearables alert now
  console.log('alert sent:', JSON.stringify(record));

  const deadline = Date.now() + ACK_WAIT_MS;
  while (Date.now() < deadline) {
    const seen = await client.wearables();
    if (totalAcks(seen) > before) {
      for (const d of seen.devices) {
        console.log(`wearable ${d.ip}: acks=${d.acks} heartbeats=${d.heartbeats} device_id=${d.device_id ?? '?'} battery=${d.battery_percentage ?? '?'}%`);
      }
      console.log('ACK received');
      return 0;
    }
    await sleep(500);
  }
  console.error(`no ACK within ${ACK_WAIT_MS / 1000} s; check GET /wearables again later`);
  return 3;
}

async function direct() {
  const egress = (process.env.HALOW_EGRESS || '').trim() || DEFAULT_EGRESS;
  const group = (process.env.HALOW_GROUP || '').trim() || GROUP;
  const port = envPort('HALOW_PORT', PORT);
  const responsePort = envPort('HALOW_RESPONSE_PORT', RESPONSE_PORT);

  const replies = await listenForResponses({ port: responsePort, allowPrefix: subnetPrefix(egress) });
  try {
    // 1. Ask the wearables to report in. Nothing buzzes.
    const reqTs = await sendHeartbeatRequest({ egress, group, port });
    const hb = await replies.waitFor('HEARTBEAT', 3000);
    if (hb) {
      const h = hb.heartbeat;
      const rtt = hb.timestamp_ms === reqTs ? `, round trip ${hb.received_ms - reqTs} ms` : '';
      console.log(`HEARTBEAT from ${hb.ip}: device_id=${h.device_id} battery=${h.battery_percentage}% rssi=${h.rssi} dBm${rtt}`);
    } else {
      console.log('no heartbeat within 3 s (continuing)');
    }

    // 2. The alert. Every wearable on the network alerts.
    const record = await sendDirect({ egress, format: process.env.HALOW_FORMAT || 'raw8', group, port });
    console.log('alert sent:', JSON.stringify(record));
    const ack = await replies.waitFor('ACK', ACK_WAIT_MS, record.ts_ms);
    if (ack) {
      console.log(`ACK from ${ack.ip}: wearable timestamp ${ack.timestamp_ms} ms`);
      return 0;
    }
    console.error(`no ACK within ${ACK_WAIT_MS / 1000} s`);
    return 3;
  } finally {
    await replies.close();
  }
}

// Checks the codec against the vectors produced by the gateway's own encoder and decoder.
function selftest(path = new URL('../testdata/vectors.json', import.meta.url)) {
  const vec = JSON.parse(readFileSync(path, 'utf8'));
  // Integers as strings and keys sorted, so the comparison does not depend on key order.
  const norm = (v) => {
    if (v === null || typeof v === 'boolean' || typeof v === 'string') return v;
    if (typeof v === 'number' || typeof v === 'bigint') return String(v);
    return Object.fromEntries(Object.keys(v).sort().map((k) => [k, norm(v[k])]));
  };
  let bad = 0;
  for (const c of vec.raw8) {
    const got = encodeRaw8(c.seconds).toString('hex');
    if (got !== c.hex) { bad += 1; console.log(`raw8 ${c.seconds}: got ${got}, want ${c.hex}`); }
  }
  for (const c of vec.gateway_message) {
    const got = encodeGatewayMessage(BigInt(c.timestamp_ms), BigInt(c.command)).toString('hex');
    if (got !== c.hex) { bad += 1; console.log(`gateway_message ${c.timestamp_ms}/${c.command}: got ${got}, want ${c.hex}`); }
  }
  for (const c of vec.device_response) {
    let got = null;
    try {
      got = norm(decodeDeviceResponse(Buffer.from(c.hex, 'hex')));
    } catch (e) {
      if (!(e instanceof RangeError)) throw e;
    }
    if (JSON.stringify(got) !== JSON.stringify(norm(c.result))) {
      bad += 1;
      console.log(`device_response ${c.hex}: got ${JSON.stringify(got)}, want ${JSON.stringify(norm(c.result))}`);
    }
  }
  const n = vec.raw8.length + vec.gateway_message.length + vec.device_response.length;
  console.log(`selftest: ${n - bad}/${n} vectors match`);
  return bad ? 1 : 0;
}

async function main(args) {
  try {
    if (args.includes('--selftest')) return selftest();
    ACK_WAIT_MS = ackWaitMs();
    return args.includes('--direct') ? await direct() : await viaGateway();
  } catch (e) {
    console.error(`example: ${e.message}`);
    return e instanceof HalowError ? 1 : 2;
  }
}

process.exitCode = await main(process.argv.slice(2));
