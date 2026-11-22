// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// Tests for listenForResponses, the receiver of wearable replies. Each test listens on a free
// TCP port on 127.0.0.1 and plays the wearable itself: it connects, writes a DeviceResponse
// (or something worse) and closes, or does not.
//
// From the repository root:
//   node --test integrations/javascript/test/listener.test.mjs

import assert from 'node:assert/strict';
import net from 'node:net';
import { describe, it } from 'node:test';
import { pathToFileURL } from 'node:url';
import { listenForResponses } from '../halow-client.mjs';
import {
  CLIENT, LOOPBACK, ackMessage, caught, freeTcpPort, handles, heartbeatMessage, hex, lfield, reply, rng, runNode,
  sleep, tcpSend, vfield, waitUntil,
} from './helpers.mjs';

/** A listener on a free port of 127.0.0.1: { l, port }. */
async function startListener(opts = {}) {
  for (let attempt = 1; ; attempt++) {
    const port = await freeTcpPort();
    try {
      return { l: await listenForResponses({ port, host: LOOPBACK, ...opts }), port };
    } catch (e) {
      if (attempt >= 3 || !/cannot listen/.test(e.message)) throw e;   // the port was taken meanwhile
    }
  }
}

/** Resolves with the promise's value, or fails the test after ms. */
function within(promise, ms, what) {
  let timer;
  return Promise.race([
    promise.finally(() => clearTimeout(timer)),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`gave up after ${ms} ms waiting for ${what}`)), ms);
    }),
  ]);
}

/** Collects console.error lines until restore(). */
function captureConsoleError() {
  const lines = [];
  const original = console.error;
  console.error = (...args) => lines.push(args.join(' '));
  return { lines, restore: () => { console.error = original; } };
}

const timestamps = (l) => l.responses.map((r) => r.timestamp_ms);

describe('listenForResponses', () => {
  it('delivers an ACK with the wearable address and the arrival time', async () => {
    const { l, port } = await startListener();
    try {
      const t0 = Date.now();
      await reply(port, ackMessage(1767225600163));
      const r = await l.waitFor('ACK', 3000);
      assert.ok(r.received_ms >= t0 && r.received_ms <= Date.now(), 'received_ms is the arrival time');
      assert.deepStrictEqual(r, { timestamp_ms: 1767225600163, code: 'ACK', ack_success: true, ip: LOOPBACK, received_ms: r.received_ms });
      assert.deepEqual(l.responses, [r]);
    } finally {
      await l.close();
    }
  });

  it('delivers a heartbeat, with negative rssi and battery', async () => {
    const { l, port } = await startListener();
    try {
      await reply(port, heartbeatMessage(1767225600123, { deviceId: 'wz-07', battery: -1, rssi: -62, uptime: 3600 }));
      const r = await l.waitFor('HEARTBEAT', 3000);
      assert.equal(r.ip, LOOPBACK);
      assert.deepEqual(r.heartbeat, { device_id: 'wz-07', battery_percentage: -1, rssi: -62, uptime_seconds: 3600, app_state: 1, device_state: 2 });
    } finally {
      await l.close();
    }
  });

  it('reassembles a reply that arrives one byte at a time', async () => {
    const { l, port } = await startListener();
    try {
      const msg = heartbeatMessage(42, { deviceId: 'slow-one' });
      const sock = await tcpSend(port, null, { keepOpen: true });
      sock.setNoDelay(true);
      for (const b of msg) {
        sock.write(Buffer.from([b]));
        await sleep(2);   // the slow sender under test
      }
      sock.end();
      await sock.whenClosed;
      assert.equal(l.responses.length, 1);
      assert.equal(l.responses[0].heartbeat.device_id, 'slow-one');
    } finally {
      await l.close();
    }
  });

  describe('waitFor', () => {
    it('returns a reply that already came, the first one that matches', async () => {
      const { l, port } = await startListener();
      try {
        await reply(port, ackMessage(1));
        await reply(port, heartbeatMessage(2));
        await reply(port, ackMessage(3));
        assert.equal((await l.waitFor('ACK', 0)).timestamp_ms, 1);
        assert.equal((await l.waitFor()).timestamp_ms, 1, 'the default code is ACK');
        assert.equal((await l.waitFor('HEARTBEAT', 0)).timestamp_ms, 2);
        assert.equal(await l.waitFor('UNKNOWN', 0), null);
      } finally {
        await l.close();
      }
    });

    it('skips replies received before afterMs', async () => {
      const { l, port } = await startListener();
      try {
        await reply(port, ackMessage(1));
        const first = l.responses[0];
        assert.equal(await l.waitFor('ACK', 30, first.received_ms + 1), null);
        const later = l.waitFor('ACK', 5000, first.received_ms + 1);
        await waitUntil(() => Date.now() > first.received_ms, { what: 'the clock to move on' });
        await reply(port, ackMessage(2));
        assert.equal((await later).timestamp_ms, 2);
      } finally {
        await l.close();
      }
    });

    it('waits for a reply that comes later, and gives up with null after timeoutMs', async () => {
      const { l, port } = await startListener();
      try {
        const pending = l.waitFor('HEARTBEAT', 5000);
        await reply(port, ackMessage(1));   // not the code it waits for
        await reply(port, heartbeatMessage(7));
        assert.equal((await pending).timestamp_ms, 7);
        const t0 = Date.now();
        assert.equal(await l.waitFor('ACK', 60, Date.now() + 60000), null);
        assert.ok(Date.now() - t0 >= 50, 'it waited');
      } finally {
        await l.close();
      }
    });

    it('waits on with timeoutMs = Infinity instead of giving up at once', async () => {
      const { l, port } = await startListener();
      const warnings = [];
      const onWarning = (w) => warnings.push(w.name);
      process.on('warning', onWarning);
      try {
        const pending = l.waitFor('ACK', Infinity);
        await sleep(20);
        await reply(port, ackMessage(5));
        const r = await within(pending, 3000, 'the ACK');
        assert.ok(r, 'waitFor gave up although it should wait without limit');
        assert.equal(r.timestamp_ms, 5);
        await sleep(0);
        assert.ok(!warnings.includes('TimeoutOverflowWarning'), warnings.join(', '));
      } finally {
        process.removeListener('warning', onWarning);
        await l.close();
      }
    });

    // These two wait 20 s, not Infinity: should one fail, the timer it leaves behind holds the
    // test file up for that long only.
    it('close() ends a pending waitFor with null at once and clears its timer', async () => {
      const { l, port } = await startListener();
      try {
        const before = handles('Timeout');
        const pending = [l.waitFor('ACK', 20000), l.waitFor('HEARTBEAT', 20000)];
        assert.equal(handles('Timeout'), before + 2, 'each waitFor has a timer');
        await reply(port, ackMessage(1));
        assert.equal((await pending[0]).timestamp_ms, 1);
        assert.equal(handles('Timeout'), before + 1, 'the answered waitFor cleared its timer');
        await l.close();
        assert.equal(await within(pending[1], 1000, 'waitFor to end on close()'), null);
        assert.equal(handles('Timeout'), before, 'no waitFor timer is left');
      } finally {
        await l.close();
      }
    });

    it('waitFor after close() still finds a reply that came, else resolves with null at once', async () => {
      const { l, port } = await startListener();
      try {
        await reply(port, ackMessage(3));
      } finally {
        await l.close();
      }
      assert.equal((await l.waitFor('ACK', 20000)).timestamp_ms, 3);
      const before = handles('Timeout');
      const none = l.waitFor('HEARTBEAT', 20000);
      assert.equal(handles('Timeout'), before, 'no timer once closed');
      assert.equal(await within(none, 1000, 'waitFor after close() to resolve'), null);
    });

    it('a program that stops waiting for waitFor(code, Infinity) and calls close() exits', async () => {
      // The same as: await Promise.race([l.waitFor('ACK', Infinity), userAbort]); await l.close();
      const port = await freeTcpPort();
      const code = `const { listenForResponses } = await import(${JSON.stringify(pathToFileURL(CLIENT).href)});
        const l = await listenForResponses({ port: ${port}, host: ${JSON.stringify(LOOPBACK)} });
        const pending = l.waitFor('ACK', Infinity);
        const gaveUp = new Promise((resolve) => setTimeout(resolve, 50, 'gave up'));
        console.log(await Promise.race([pending, gaveUp]));
        await l.close();
        console.log(await pending);`;
      const r = await runNode(['--input-type=module', '-e', code], { timeoutMs: 10000 });
      assert.equal(r.signal, null, `still running ${r.ms} ms after close(): killed`);
      assert.equal(r.code, 0, r.stderr);
      assert.deepEqual(r.stdout.trim().split('\n'), ['gave up', 'null']);
    });
  });

  describe('onResponse', () => {
    it('sees every reply, the same objects as in responses', async () => {
      const seen = [];
      const { l, port } = await startListener({ onResponse: (r) => seen.push(r) });
      try {
        await reply(port, ackMessage(1));
        await reply(port, heartbeatMessage(2));
        assert.equal(seen.length, 2);
        assert.ok(seen.every((r, i) => r === l.responses[i]));
      } finally {
        await l.close();
      }
    });

    it('whatever it throws, or rejects with, the listener goes on and says why', async () => {
      const revoked = Proxy.revocable({}, {});
      revoked.revoke();
      // [what the callback fails with, what the log says]. Some values cannot be put in a
      // template string as they are: the listener must still say something and go on.
      const values = [
        [new Error('boom'), 'boom'],
        ['plain text', 'plain text'],
        [undefined, 'undefined'],
        [null, 'null'],
        [42, '42'],
        [Symbol('sym'), 'Symbol(sym)'],
        [Object.create(null), '[object Object]'],
        [{ message: Symbol('in message') }, 'Symbol(in message)'],
        [{ get message() { throw new Error('getter'); } }, '[object Object]'],
        [revoked.proxy, '(a value that cannot be printed)'],
      ];
      // Each value once thrown, then once as the rejection of an async callback.
      const failures = [...values.map(([value, why]) => ({ value, why, async: false })),
        ...values.map(([value, why]) => ({ value, why, async: true }))];
      let n = 0;
      const unhandled = [];
      const onUnhandled = (e) => unhandled.push(e);
      process.on('unhandledRejection', onUnhandled);
      const log = captureConsoleError();
      const { l, port } = await startListener({
        onResponse: (r) => {
          n += 1;
          const f = failures[r.timestamp_ms - 1];
          if (f.async) return Promise.reject(f.value);
          throw f.value;
        },
      });
      try {
        for (let ts = 1; ts <= failures.length; ts++) await reply(port, ackMessage(ts));
        await waitUntil(() => log.lines.length === failures.length, { what: 'every failure to be reported' });
        assert.equal(n, failures.length);
        assert.deepEqual(timestamps(l), failures.map((f, i) => i + 1));
        assert.deepEqual(log.lines, failures.map((f) => `halow-client: onResponse failed: ${f.why}`));
        await reply(port, ackMessage(failures.length + 1));
        assert.equal(l.responses.length, failures.length + 1, 'still listening');
        await sleep(10);
        assert.equal(unhandled.length, 0, 'no rejection is left unhandled');
      } finally {
        process.removeListener('unhandledRejection', onUnhandled);
        log.restore();
        await l.close();
      }
    });
  });

  describe('what it refuses', () => {
    it('closes a connection from outside allowPrefix without reading it', async () => {
      const { l, port } = await startListener({ allowPrefix: '10.99.' });
      try {
        const sock = await tcpSend(port, ackMessage(1), { keepOpen: true });
        await within(sock.whenClosed, 3000, 'the listener to close the connection');
        assert.equal(l.responses.length, 0);
      } finally {
        await l.close();
      }
      const ok = await startListener({ allowPrefix: '127.0.0.' });
      try {
        await reply(ok.port, ackMessage(2));
        assert.deepEqual(timestamps(ok.l), [2]);
      } finally {
        await ok.l.close();
      }
    });

    it('drops garbage, empty connections and codes other than ACK and HEARTBEAT, and goes on', async () => {
      const { l, port } = await startListener();
      try {
        const bad = [
          null,                                                   // connects and closes
          hex('0b'),                                              // a group
          hex('08 ff ff ff ff ff ff ff ff ff ff 01'),             // an eleven-byte varint
          hex('1a 05 08'),                                        // a field past the end
          lfield(2, 'x'),                                         // code with the wrong wire type
          vfield(1, 5),                                           // code 0 (UNKNOWN)
          Buffer.concat([vfield(1, 5), vfield(2, 3)]),            // code 3
          Buffer.from('GET / HTTP/1.1\r\nHost: x\r\n\r\n'),
          rng(3).bytes(300),
        ];
        for (const b of bad) await reply(port, b);
        assert.deepEqual(l.responses, []);
        await reply(port, ackMessage(9));
        assert.deepEqual(timestamps(l), [9]);
      } finally {
        await l.close();
      }
    });

    it('reads at most 8192 bytes: a reply of exactly 8192 is decoded, one byte more is cut and dropped', async () => {
      const { l, port } = await startListener();
      try {
        const sized = (total, ts) => {
          const ack = ackMessage(ts);
          return Buffer.concat([ack, lfield(15, Buffer.alloc(total - ack.length - 3, 0x61))]);   // 3 = key + 2-byte length
        };
        assert.equal(sized(8192, 1).length, 8192);
        // Neither connection is closed by the sender: the listener stops at 8192 bytes by itself.
        const exact = await tcpSend(port, sized(8192, 1), { keepOpen: true });
        await within(exact.whenClosed, 3000, 'the listener to stop at 8192 bytes');
        assert.deepEqual(timestamps(l), [1]);
        const over = await tcpSend(port, sized(8193, 2), { keepOpen: true });
        await within(over.whenClosed, 3000, 'the listener to stop at 8192 bytes');
        assert.deepEqual(timestamps(l), [1]);
      } finally {
        await l.close();
      }
    });

    it('closes an oversized connection after 8192 bytes, well before the 5 s limit', async () => {
      const { l, port } = await startListener();
      try {
        const t0 = Date.now();
        const sock = await tcpSend(port, Buffer.alloc(64 * 1024, 0xff), { keepOpen: true });
        await within(sock.whenClosed, 3000, 'the listener to close the connection');
        assert.ok(Date.now() - t0 < 3000);
        assert.deepEqual(l.responses, []);
        await reply(port, ackMessage(3));
        assert.deepEqual(timestamps(l), [3]);
      } finally {
        await l.close();
      }
    });

    it('survives connections that are reset', async () => {
      const { l, port } = await startListener();
      try {
        for (const data of [null, ackMessage(1).subarray(0, 6), hex('ff ff')]) {   // 6: cut inside the ack payload
          const sock = await tcpSend(port, data, { keepOpen: true });
          if (sock.resetAndDestroy) sock.resetAndDestroy();   // RST (Node 18.3 and newer)
          else sock.destroy();
          await sock.whenClosed;
        }
        await reply(port, ackMessage(4));
        await waitUntil(() => l.responses.some((r) => r.timestamp_ms === 4), { what: 'the ACK after the resets' });
        assert.deepEqual(timestamps(l), [4]);
      } finally {
        await l.close();
      }
    });

    it('gives a silent connection 5 s, decodes what arrived, and serves others meanwhile', { timeout: 20000 }, async () => {
      const { l, port } = await startListener();
      try {
        const t0 = Date.now();
        const complete = await tcpSend(port, ackMessage(111), { keepOpen: true });            // never closed
        const partial = await tcpSend(port, ackMessage(222).subarray(0, 7), { keepOpen: true });   // cut short, never closed
        await reply(port, ackMessage(333));
        assert.deepEqual(timestamps(l), [333], 'a normal reply is not held up');
        await within(Promise.all([complete.whenClosed, partial.whenClosed]), 9000, 'the listener to give up on both');
        const took = Date.now() - t0;
        assert.ok(took >= 4500 && took < 8000, `gave up after ${took} ms`);
        assert.deepEqual(timestamps(l), [333, 111]);
      } finally {
        await l.close();
      }
    });

    it('takes at most 32 connections at once', async () => {
      const { l, port } = await startListener();
      const idle = [];
      try {
        for (let i = 0; i < 32; i++) idle.push(await tcpSend(port, null, { keepOpen: true }));
        const extra = await tcpSend(port, ackMessage(1), { keepOpen: true });
        await within(extra.whenClosed, 3000, 'the listener to refuse connection 33');
        assert.ok(idle.every((s) => !s.destroyed), 'the first 32 stay open');
        idle[0].end();
        await idle[0].whenClosed;
        await reply(port, ackMessage(2));   // a slot is free again
        assert.deepEqual(timestamps(l), [2]);
      } finally {
        for (const s of idle) s.destroy();
        await Promise.all(idle.map((s) => s.whenClosed));
        await l.close();
      }
    });

    it('keeps the last 1000 replies, in the same array', { timeout: 30000 }, async () => {
      const { l, port } = await startListener();
      try {
        const { responses } = l;
        const total = 1005;
        const batch = 16;   // below the 32-connection limit
        for (let start = 1; start <= total; start += batch) {
          const sends = [];
          for (let ts = start; ts < start + batch && ts <= total; ts++) sends.push(reply(port, ackMessage(ts)));
          await Promise.all(sends);
        }
        assert.equal(l.responses, responses);
        assert.equal(responses.length, 1000);
        const kept = new Set(timestamps(l));
        assert.equal(kept.size, 1000);
        for (let ts = batch + 1; ts <= total; ts++) assert.ok(kept.has(ts), `reply ${ts} was dropped`);
        assert.ok(await l.waitFor('ACK', 0));
      } finally {
        await l.close();
      }
    });
  });

  describe('starting and stopping', () => {
    it('close() frees the port at once and resolves; closing twice is fine', async () => {
      // A closed server stays listed for a moment after its close callback, hence the polling.
      const servers = (n) => waitUntil(() => handles('TCPServerWrap') === n, { what: `${n} listening socket(s)` });
      await servers(0);
      const { l, port } = await startListener();
      try {
        await servers(1);
      } finally {
        await l.close();
      }
      const e = await caught(() => tcpSend(port, null));
      assert.equal(e?.code, 'ECONNREFUSED');
      await l.close();
      await servers(0);
      const again = await listenForResponses({ port, host: LOOPBACK });
      try {
        await reply(port, ackMessage(1));
        assert.deepEqual(timestamps(again), [1]);
      } finally {
        await again.close();
      }
      await servers(0);
    });

    it('close() stops new connections at once and resolves once open ones are done', async () => {
      const { l, port } = await startListener();
      let open = null;
      let closing = null;
      try {
        open = await tcpSend(port, ackMessage(9), { keepOpen: true });
        // Both ends of the connection are in this process: two sockets once the listener has taken it.
        await waitUntil(() => handles('TCPSocketWrap') >= 2, { what: 'the listener to accept the connection' });
        let closed = false;
        closing = l.close().then(() => { closed = true; });
        await waitUntil(async () => (await caught(() => tcpSend(port, null)))?.code === 'ECONNREFUSED', { what: 'new connections to be refused' });
        assert.equal(closed, false);
      } finally {
        open?.end();
        if (!closing) await l.close();   // a failure above must not leave the listener open: the run would hang
      }
      await within(closing, 3000, 'close() to resolve');
      assert.deepEqual(timestamps(l), [9], 'the open connection was still read');
    });

    it('fails clearly when the port is taken', async () => {
      const blocker = net.createServer();
      await new Promise((resolve) => blocker.listen(0, LOOPBACK, resolve));
      const { port } = blocker.address();
      try {
        const e = await caught(() => listenForResponses({ port, host: LOOPBACK }));
        assert.ok(e instanceof Error);
        assert.match(e.message, new RegExp(`^cannot listen on TCP ${port} for wearable replies: \\w+ \\(is the gateway service or another listener running on this machine\\?\\)$`));
      } finally {
        await new Promise((resolve) => blocker.close(resolve));
      }
    });

    it('rejects a port that is out of range, leaving no socket open', async () => {
      for (const port of [-1, 70000]) {
        assert.ok(await caught(() => listenForResponses({ port, host: LOOPBACK })), `port ${port}`);
      }
      await waitUntil(() => handles('TCPServerWrap') === 0, { what: 'no listening socket' });
    });
  });
});
