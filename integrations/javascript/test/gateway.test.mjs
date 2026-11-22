// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// Tests for GatewayClient and totalAcks against an HTTP stub of the gateway on 127.0.0.1.
//
// From the repository root:
//   node --test integrations/javascript/test/gateway.test.mjs

import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import { after, before, beforeEach, describe, it } from 'node:test';
import { pathToFileURL } from 'node:url';
import { inspect } from 'node:util';
import { DEFAULT_URL, GatewayClient, HalowError, VERSION, totalAcks } from '../halow-client.mjs';
import {
  CLIENT, LOOPBACK, TOKEN, caught, childEnv, freeTcpPort, isolateEnv, runNode, sleep, startStub,
} from './helpers.mjs';

// The hints of the Python reference client, word for word.
const HINTS = {
  400: 'the gateway rejected the request',
  401: "missing or wrong token: set HALOW_TOKEN to the gateway's token",
  404: 'unknown path: is HALOW_URL the halow-alert gateway (http://<host>:8080)?',
  413: 'request body too large',
  429: 'rate limited: the gateway sends at most one alert every 0.5 s; retry shortly',
  500: 'the gateway could not send the alert (HaLowLink unplugged or not set up? see GET /health)',
  503: 'the gateway cannot send right now (no HaLowLink address on the gateway host, or its reply listener stopped; see GET /health)',
};

/** Asserts that fn fails with a HalowError with this status; returns the error. */
async function halowError(fn, status) {
  const e = await caught(fn);
  assert.ok(e instanceof HalowError, `expected a HalowError, got ${e}`);
  assert.equal(e.status, status, e.message);
  return e;
}

/** Calls AbortSignal.timeout receives while fn runs. */
async function timeoutsUsed(fn) {
  const original = AbortSignal.timeout;
  const seen = [];
  AbortSignal.timeout = (ms) => {
    seen.push(ms);
    return original.call(AbortSignal, ms);
  };
  try {
    await fn();
  } finally {
    AbortSignal.timeout = original;
  }
  return seen;
}

describe('GatewayClient against a stub gateway', () => {
  let stub;
  before(async () => {
    stub = await startStub();
  });
  after(async () => {
    await stub.close();
  });
  beforeEach(() => stub.reset());
  isolateEnv();

  describe('requests', () => {
    it('GET /health returns the body; no token, no body, Accept JSON', async () => {
      const body = await new GatewayClient({ url: stub.url, token: TOKEN }).health();
      assert.equal(body.ok, true);
      assert.equal(stub.requests.length, 1);
      const req = stub.requests[0];
      assert.deepEqual([req.method, req.path, req.body], ['GET', '/health', '']);
      assert.equal(req.headers.accept, 'application/json');
      assert.equal(req.headers.authorization, undefined, 'the token goes with POST /alert only');
      assert.equal(req.headers['content-type'], undefined);
    });

    it('GET /health returns the body of a 503 too', async () => {
      const down = { ok: false, error: 'no HaLowLink address' };
      stub.route('GET /health', { status: 503, json: down });
      assert.deepEqual(await new GatewayClient({ url: stub.url }).health(), down);
    });

    it('GET /wearables returns the body, without the token', async () => {
      const body = await new GatewayClient({ url: stub.url, token: TOKEN }).wearables();
      assert.equal(body.devices.length, 2);
      assert.equal(totalAcks(body), 5);
      assert.deepEqual([stub.requests[0].method, stub.requests[0].path], ['GET', '/wearables']);
      assert.equal(stub.requests[0].headers.authorization, undefined);
    });

    it('POST /alert sends the bearer token and a JSON body with a Content-Length', async () => {
      const record = await new GatewayClient({ url: stub.url, token: TOKEN }).alert({ label: 'zone-3', format: 'PROTO', burstSeconds: 1 });
      assert.equal(record.event, 'alert_sent');
      const req = stub.requests[0];
      assert.deepEqual([req.method, req.path], ['POST', '/alert']);
      assert.equal(req.headers.authorization, `Bearer ${TOKEN}`);
      assert.equal(req.headers['content-type'], 'application/json');
      assert.equal(req.headers.accept, 'application/json');
      // The gateway ignores a chunked body, so it must come with a Content-Length.
      assert.equal(req.headers['transfer-encoding'], undefined);
      assert.equal(Number(req.headers['content-length']), Buffer.byteLength(req.body));
      assert.deepEqual(JSON.parse(req.body), { label: 'zone-3', format: 'proto', burst_seconds: 1 });
    });

    it('POST /alert without options sends {}', async () => {
      await new GatewayClient({ url: stub.url, token: TOKEN }).alert();
      assert.equal(stub.requests[0].body, '{}');
    });

    it('POST /alert: a label becomes text, null options are left out, formats are lower-cased', async () => {
      const gw = new GatewayClient({ url: stub.url, token: TOKEN });
      await gw.alert({ label: 42, format: null, burstSeconds: null });
      await gw.alert({ label: null, format: 'Both', burstSeconds: 0 });
      await gw.alert({ label: '' });
      assert.deepEqual(stub.requests.map((r) => JSON.parse(r.body)),
        [{ label: '42' }, { format: 'both', burst_seconds: 0 }, { label: '' }]);
    });

    it('POST /alert: a non-ASCII label is sent as UTF-8 with the right Content-Length', async () => {
      const label = 'zone-ü \u{1F6A7} 東';
      await new GatewayClient({ url: stub.url, token: TOKEN }).alert({ label });
      const req = stub.requests[0];
      assert.equal(JSON.parse(req.body).label, label);
      assert.equal(Number(req.headers['content-length']), Buffer.byteLength(req.body, 'utf8'));
    });

    it('trailing slashes in the URL are dropped', async () => {
      const gw = new GatewayClient({ url: `${stub.url}///` });
      assert.equal(gw.url, stub.url);
      await gw.health();
      assert.equal(stub.requests[0].path, '/health');
    });
  });

  describe('settings', () => {
    it('reads HALOW_URL and HALOW_TOKEN, trimmed', async () => {
      process.env.HALOW_URL = `  ${stub.url}/  `;
      process.env.HALOW_TOKEN = `  ${TOKEN}  `;
      const gw = new GatewayClient();
      assert.deepEqual([gw.url, gw.token], [stub.url, TOKEN]);
      assert.equal((await gw.alert()).event, 'alert_sent');
      assert.equal(stub.requests[0].headers.authorization, `Bearer ${TOKEN}`);
    });

    it('arguments win over the environment; an empty url falls back to it', () => {
      process.env.HALOW_URL = 'http://192.0.2.1:9';
      process.env.HALOW_TOKEN = 'from-env';
      const gw = new GatewayClient({ url: stub.url, token: ' given ' });
      assert.deepEqual([gw.url, gw.token], [stub.url, 'given']);
      assert.equal(new GatewayClient({ url: '' }).url, 'http://192.0.2.1:9');
      assert.equal(new GatewayClient({ token: null }).token, 'from-env');
    });

    it('an explicit empty token sends no Authorization header, even with HALOW_TOKEN set', async () => {
      process.env.HALOW_TOKEN = TOKEN;
      const e = await halowError(() => new GatewayClient({ url: stub.url, token: '' }).alert(), 401);
      assert.equal(stub.requests[0].headers.authorization, undefined);
      assert.match(e.message, /HALOW_TOKEN/);
    });

    it('defaults to http://127.0.0.1:8080 and no token (nothing is sent here)', () => {
      const gw = new GatewayClient();
      assert.deepEqual([gw.url, gw.token, gw.timeoutMs], [DEFAULT_URL, '', 10000]);
      process.env.HALOW_URL = '   ';
      assert.equal(new GatewayClient().url, DEFAULT_URL);
    });

    it('refuses a URL that is not http:// or https://', () => {
      for (const url of ['ftp://192.0.2.1', '192.0.2.1:8080', 'localhost:8080', 'file:///etc/passwd']) {
        assert.throws(() => new GatewayClient({ url }), (e) => e instanceof TypeError && /http:\/\//.test(e.message), url);
      }
      process.env.HALOW_URL = 'ftp://192.0.2.1';
      assert.throws(() => new GatewayClient(), TypeError);
      assert.equal(new GatewayClient({ url: 'https://192.0.2.1:8443' }).url, 'https://192.0.2.1:8443');
    });

    it('rejects a bad format or burstSeconds before sending anything', async () => {
      const gw = new GatewayClient({ url: stub.url, token: TOKEN });
      for (const opts of [{ format: 'mp3' }, { format: '' }, { burstSeconds: -1 }, { burstSeconds: NaN },
        { burstSeconds: Infinity }, { burstSeconds: '1' }]) {
        const e = await caught(() => gw.alert(opts));
        assert.ok(e instanceof RangeError, `${JSON.stringify(opts)}: ${e}`);
      }
      assert.deepEqual(stub.requests, []);
    });
  });

  describe('errors', () => {
    for (const status of [400, 401, 404, 413, 429, 500, 503]) {
      it(`HTTP ${status} rejects with its status, the gateway's error and the hint`, async () => {
        stub.route('POST /alert', { status, json: { error: `reason ${status}` } });
        const e = await halowError(() => new GatewayClient({ url: stub.url, token: TOKEN }).alert(), status);
        assert.equal(e.message, `POST /alert: HTTP ${status}: ${HINTS[status]} (reason ${status})`);
        assert.deepEqual(e.body, { error: `reason ${status}` });
        assert.equal(e.name, 'HalowError');
        assert.ok(e instanceof Error);
      });
    }

    for (const status of [201, 418, 502]) {
      it(`HTTP ${status} is an unexpected response`, async () => {
        stub.route('GET /wearables', { status, json: {} });
        const e = await halowError(() => new GatewayClient({ url: stub.url }).wearables(), status);
        assert.equal(e.message, `GET /wearables: HTTP ${status}: unexpected response`);
      });
    }

    it('a wrong token is a 401 that names HALOW_TOKEN (the real token check)', async () => {
      const e = await halowError(() => new GatewayClient({ url: stub.url, token: 'wrong' }).alert({ label: 'x' }), 401);
      assert.match(e.message, /HTTP 401/);
      assert.match(e.message, /HALOW_TOKEN/);
      assert.deepEqual(e.body, { error: 'missing or wrong Bearer token' });
    });

    it('a URL with a path is a 404 that names HALOW_URL', async () => {
      const e = await halowError(() => new GatewayClient({ url: `${stub.url}/not-the-gateway` }).health(), 404);
      assert.equal(stub.requests[0].path, '/not-the-gateway/health');
      assert.match(e.message, /HALOW_URL/);
    });

    it('a 503 from GET /wearables rejects', async () => {
      stub.route('GET /wearables', { status: 503, json: { error: 'listener stopped' } });
      const e = await halowError(() => new GatewayClient({ url: stub.url }).wearables(), 503);
      assert.match(e.message, /listener stopped/);
    });

    it('a body that is not JSON becomes { error: text }, cut to 500 characters', async () => {
      stub.route('POST /alert', { status: 500, raw: 'boom' });
      const e = await halowError(() => new GatewayClient({ url: stub.url, token: TOKEN }).alert(), 500);
      assert.deepEqual(e.body, { error: 'boom' });
      assert.match(e.message, /HaLowLink.*\(boom\)$/);

      stub.route('GET /health', { status: 200, raw: 'hello' });
      assert.deepEqual(await new GatewayClient({ url: stub.url }).health(), { error: 'hello' });

      stub.route('GET /health', { status: 503, raw: 'x'.repeat(2000) });
      assert.deepEqual(await new GatewayClient({ url: stub.url }).health(), { error: 'x'.repeat(500) });
    });

    it('an empty or blank body is {}', async () => {
      const gw = new GatewayClient({ url: stub.url, token: TOKEN });
      stub.route('GET /health', { status: 200, raw: '' });
      assert.deepEqual(await gw.health(), {});
      stub.route('GET /health', { status: 200, raw: ' \r\n\t ' });
      assert.deepEqual(await gw.health(), {});
      stub.route('POST /alert', { status: 500, raw: '' });
      const e = await halowError(() => gw.alert(), 500);
      assert.deepEqual(e.body, {});
      assert.equal(e.message, `POST /alert: HTTP 500: ${HINTS[500]}`);
    });

    it('JSON that is not an object becomes { value }', async () => {
      const gw = new GatewayClient({ url: stub.url });
      for (const [raw, value] of [['[1,2]', [1, 2]], ['"text"', 'text'], ['42', 42], ['null', null], ['true', true]]) {
        stub.route('GET /health', { status: 200, raw });
        assert.deepEqual(await gw.health(), { value }, raw);
      }
      stub.route('GET /wearables', { status: 500, raw: '["error"]' });
      const e = await halowError(() => gw.wearables(), 500);
      assert.deepEqual(e.body, { value: ['error'] });
      assert.equal(e.message, `GET /wearables: HTTP 500: ${HINTS[500]}`);
    });

    it('a refused connection rejects with no status', async () => {
      const port = await freeTcpPort();   // nothing listens there now
      const e = await halowError(() => new GatewayClient({ url: `http://${LOOPBACK}:${port}`, timeoutMs: 5000 }).health(), undefined);
      assert.match(e.message, new RegExp(`^cannot reach the gateway at http://127\\.0\\.0\\.1:${port}: `));
      assert.deepEqual(e.body, {});
    });

    it('a gateway that never answers times out', async () => {
      stub.route('GET /health', () => {});   // accept the request, never answer
      const started = Date.now();
      const e = await halowError(() => new GatewayClient({ url: stub.url, timeoutMs: 300 }).health(), undefined);
      assert.match(e.message, /: timed out$/);
      assert.ok(Date.now() - started < 3000, `took ${Date.now() - started} ms`);
    });

    it('a body that arrives too slowly times out too', async () => {
      stub.route('GET /health', (req, res) => {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.write('{"ok"');   // and nothing more
      });
      const e = await halowError(() => new GatewayClient({ url: stub.url, timeoutMs: 300 }).health(), undefined);
      assert.match(e.message, /timed out/);
    });

    it('a connection that breaks in the middle of the body rejects with no status', async () => {
      stub.route('GET /health', (req, res) => {
        res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': '100' });
        res.write('{"ok"', () => req.socket.destroy());
      });
      const e = await halowError(() => new GatewayClient({ url: stub.url, timeoutMs: 5000 }).health(), undefined);
      assert.match(e.message, /^cannot reach the gateway/);
    });

    it('a redirect is refused, not followed', async () => {
      stub.route('POST /alert', { status: 302, json: {}, headers: { Location: '/elsewhere' } });
      const e = await halowError(() => new GatewayClient({ url: stub.url, token: TOKEN }).alert(), undefined);
      assert.match(e.message, /^cannot reach the gateway/);
      assert.deepEqual(stub.requests.map((r) => r.path), ['/alert'], 'the token must not follow a redirect');
    });

    it('HalowError defaults', () => {
      const e = new HalowError('x');
      assert.deepEqual([e.name, e.message, e.status, e.body], ['HalowError', 'x', undefined, {}]);
      const f = new HalowError('y', { status: 429, body: { error: 'z' } });
      assert.deepEqual([f.status, f.body], [429, { error: 'z' }]);
    });
  });

  describe('timeouts', () => {
    it('a burst extends the timeout, because the gateway answers when the burst ends', async () => {
      stub.route('POST /alert', { json: { event: 'alert_sent' }, delayMs: 900 });
      const gw = new GatewayClient({ url: stub.url, token: TOKEN, timeoutMs: 400 });
      assert.equal((await gw.alert({ burstSeconds: 1 })).event, 'alert_sent');   // 400 + 1000 ms
      const e = await halowError(() => gw.alert(), undefined);                     // 400 ms
      assert.match(e.message, /timed out/);
    });

    it('the timeout is timeoutMs, plus the burst up to 5 s', async () => {
      const gw = new GatewayClient({ url: stub.url, token: TOKEN });
      assert.deepEqual(await timeoutsUsed(async () => {
        await gw.health();
        await gw.wearables();
        await gw.alert();
        await gw.alert({ burstSeconds: 2 });
        await gw.alert({ burstSeconds: 10 });
        await gw.alert({ burstSeconds: 0 });
      }), [10000, 10000, 10000, 12000, 15000, 10000]);
    });

    it('a burst that is not a whole number of milliseconds works (1/3 s, 1.2345 s)', async () => {
      const gw = new GatewayClient({ url: stub.url, token: TOKEN });
      assert.equal((await gw.alert({ burstSeconds: 1 / 3 })).event, 'alert_sent');
      assert.equal((await gw.alert({ burstSeconds: 1.2345 })).event, 'alert_sent');
      const used = await timeoutsUsed(() => gw.alert({ burstSeconds: 1 / 3 }));
      assert.deepEqual(used, [10334]);
      assert.deepEqual(JSON.parse(stub.requests.at(-1).body), { burst_seconds: 1 / 3 });
    });

    it('a timeoutMs given as numeric text is taken as its number, so a burst adds to it', async () => {
      const gw = new GatewayClient({ url: stub.url, token: TOKEN, timeoutMs: '1000' });
      assert.equal(gw.timeoutMs, 1000);
      assert.deepEqual(await timeoutsUsed(async () => {
        await gw.health();
        await gw.alert();
        await gw.alert({ burstSeconds: 1 });
      }), [1000, 1000, 2000]);   // not '1000' + 1000 = 10001000
      gw.timeoutMs = '1500';     // set after construction, the sum is still a number
      assert.deepEqual(await timeoutsUsed(() => gw.alert({ burstSeconds: 0.5 })), [2000]);
    });

    it('a timeoutMs that is not a number above 0 is refused at once, nothing is sent', () => {
      for (const timeoutMs of [0, -1, -0.5, NaN, null, '', '   ', 'abc', '-5', '0', true, {}, [], [5]]) {
        assert.throws(() => new GatewayClient({ url: stub.url, timeoutMs }),
          (e) => e instanceof RangeError && e.message === 'timeoutMs must be a number of milliseconds above 0',
          inspect(timeoutMs));
      }
      assert.equal(new GatewayClient({ url: stub.url, timeoutMs: undefined }).timeoutMs, 10000);
      assert.equal(new GatewayClient({ url: stub.url, timeoutMs: ' 2.5e3 ' }).timeoutMs, 2500);
      assert.equal(new GatewayClient({ url: stub.url, timeoutMs: Infinity }).timeoutMs, Infinity);
      assert.deepEqual(stub.requests, []);
    });

    it('a timeoutMs that is fractional or beyond the timer limit works', async () => {
      stub.route('GET /health', { json: { ok: true }, delayMs: 100 });
      assert.equal((await new GatewayClient({ url: stub.url, timeoutMs: 1500.5 }).health()).ok, true);
      const warnings = [];
      const onWarning = (w) => warnings.push(w.name);
      process.on('warning', onWarning);
      try {
        // Node runs a timer of more than 2^31 - 1 ms after 1 ms; this must not time out at once.
        assert.equal((await new GatewayClient({ url: stub.url, timeoutMs: 3e9 }).health()).ok, true);
        assert.equal((await new GatewayClient({ url: stub.url, timeoutMs: Infinity }).health()).ok, true);
        await sleep(0);
      } finally {
        process.removeListener('warning', onWarning);
      }
      assert.ok(!warnings.includes('TimeoutOverflowWarning'), warnings.join(', '));
    });
  });

  describe('proxy settings', () => {
    const proxy = `http://${LOOPBACK}:9`;   // nothing listens there
    const PROXIES = { HTTP_PROXY: proxy, http_proxy: proxy, HTTPS_PROXY: proxy, https_proxy: proxy, ALL_PROXY: proxy, all_proxy: proxy };

    it('are ignored in this process', async () => {
      Object.assign(process.env, PROXIES);
      assert.equal((await new GatewayClient({ url: stub.url }).health()).ok, true);
    });

    it('are ignored by a fresh Node.js process', async () => {
      const code = `const { GatewayClient } = await import(${JSON.stringify(pathToFileURL(CLIENT).href)});
        console.log(JSON.stringify(await new GatewayClient({ url: ${JSON.stringify(stub.url)}, timeoutMs: 5000 }).health()));`;
      const r = await runNode(['--input-type=module', '-e', code], { env: childEnv(PROXIES) });
      assert.equal(r.code, 0, r.stderr);
      assert.equal(JSON.parse(r.stdout).ok, true);
    });
  });
});

describe('GatewayClient against other servers', () => {
  it('a server that closes the connection without answering: rejects with no status', async () => {
    const server = net.createServer((s) => s.destroy());
    await new Promise((resolve) => server.listen(0, LOOPBACK, resolve));
    try {
      const e = await halowError(() => new GatewayClient({ url: `http://${LOOPBACK}:${server.address().port}`, timeoutMs: 5000 }).health(), undefined);
      assert.match(e.message, /^cannot reach the gateway/);
    } finally {
      await new Promise((resolve) => server.close(resolve));
    }
  });

  it('a server that keeps the connection open between requests works too', async () => {
    const server = http.createServer((req, res) => {
      const body = JSON.stringify({ ok: true, version: VERSION });
      res.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) });
      res.end(body);
    });
    await new Promise((resolve) => server.listen(0, LOOPBACK, resolve));
    try {
      const gw = new GatewayClient({ url: `http://${LOOPBACK}:${server.address().port}` });
      for (let i = 0; i < 3; i++) assert.equal((await gw.health()).ok, true);
    } finally {
      server.closeAllConnections?.();
      await new Promise((resolve) => server.close(resolve));
    }
  });
});

describe('totalAcks', () => {
  it('sums the ACKs of every wearable', () => {
    assert.equal(totalAcks({ devices: [{ acks: 2 }, { acks: 3 }, { acks: 0 }] }), 5);
  });

  it('is 0 for a body without wearables', () => {
    for (const body of [undefined, null, {}, { devices: [] }, { devices: null }, { recent: [] }]) {
      assert.equal(totalAcks(body), 0, JSON.stringify(body));
    }
  });

  it('counts a wearable without an acks field as 0, and numeric text as its number', () => {
    assert.equal(totalAcks({ devices: [{ ip: '192.168.12.143', heartbeats: 4 }, { acks: '3' }, { acks: null }] }), 3);
  });
});
