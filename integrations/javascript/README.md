# JavaScript client (Node.js)

`halow-client.mjs` is one ES module for Node.js 18 or newer. It has no dependencies: it uses the built-in `fetch`, `dgram` and `net`. Copy it into your project, or import it from this folder. Read [the integrations overview](../README.md) first: it explains gateway mode and direct mode, the settings and the errors.

> Every wearable on the HaLowLink network vibrates and sounds when an alert goes out. Warn the people nearby first.

Browsers cannot send UDP multicast or receive TCP, so direct mode is Node.js only. A browser page can call the gateway's HTTP API only if it is served from the same origin, because the gateway sends no CORS headers. Put the call in your backend instead, which also keeps the token out of the page.

## Gateway mode

```js
import { GatewayClient, HalowError } from './halow-client.mjs';

const gw = new GatewayClient({ url: 'http://192.0.2.10:8080' });   // or new GatewayClient(): HALOW_URL, HALOW_TOKEN
console.log(await gw.health());                                     // { ok: true, egress: '192.168.12.50', ... }
try {
  const record = await gw.alert({ label: 'zone-3 intrusion' });     // optional: format: 'proto', burstSeconds: 1
} catch (e) {
  if (e instanceof HalowError && e.status === 429) { /* one alert per 0.5 s: retry shortly */ }
  throw e;
}
console.log((await gw.wearables()).devices);                        // ACKs, battery, RSSI per wearable
```

| Call | Returns |
|---|---|
| `new GatewayClient({ url, token, timeoutMs = 10000 })` | A client. `url` defaults to `HALOW_URL`, then `http://127.0.0.1:8080`; `token` defaults to `HALOW_TOKEN`. `timeoutMs` is the request timeout in milliseconds (an alert's burst adds its length); numeric text such as `'5000'` counts as its number, and anything that is not a number above 0 throws a `RangeError`. |
| `await .health()` | The `GET /health` body for `200` and `503`. Check `.ok`. |
| `await .wearables()` | The `GET /wearables` body: `{ devices: [...], recent: [...] }`. |
| `await .alert({ label, format, burstSeconds })` | The gateway's send record. Rejects with `HalowError` with `.status` (`401`, `429`, `500`, ...) and `.body`, or with `.status` `undefined` when the gateway was not reached. |
| `totalAcks(wearables)` | The number of ACKs in a `wearables()` body. |

## Direct mode

```js
import { listenForResponses, sendDirect, sendHeartbeatRequest, subnetPrefix } from './halow-client.mjs';

const egress = '192.168.12.50';                                    // this machine's address on the HaLowLink
const replies = await listenForResponses({ port: 5001, allowPrefix: subnetPrefix(egress) });
try {
  await sendHeartbeatRequest({ egress });                          // no buzz; the wearables report in
  const record = await sendDirect({ egress });                     // the alert: raw8, from egress, TTL 4
  const ack = await replies.waitFor('ACK', 15000, record.ts_ms);   // { code: 'ACK', ip, timestamp_ms, ... } or null
  console.log(ack);
} finally {
  await replies.close();
}
```

| Call | Does |
|---|---|
| `await sendDirect({ egress, format = 'raw8', group = '239.255.0.10', port = 5000, ttl = 4, burstSeconds = 0, burstIntervalMs = 100 })` | Sends one alert (`raw8`, `proto` or `both`) and resolves with a record like the gateway's. `burstSeconds` resends the same bytes for that long (at most 5 s). Rejects when `egress` is not an address of this machine. |
| `await sendHeartbeatRequest({ egress, group = '239.255.0.10', port = 5000, ttl = 4 })` | Asks every wearable to report in. Nothing buzzes. Resolves with the request's timestamp (ms), which each heartbeat echoes. |
| `await listenForResponses({ port = 5001, host = '0.0.0.0', allowPrefix = '', onResponse })` | Listens for replies. Resolves with `{ responses, waitFor(code, timeoutMs, afterMs), close() }`. |
| `encodeRaw8(seconds)`, `encodeGatewayMessage(ms, command)`, `decodeDeviceResponse(buffer)` | The wire formats. |

## Example

```bash
HALOW_URL=http://192.0.2.10:8080 HALOW_TOKEN=<token> node example.mjs     # gateway mode
HALOW_EGRESS=192.168.12.50 node example.mjs --direct                       # direct mode
node example.mjs --selftest                                                # codec check, sends nothing
```

`npm run selftest`, `npm run example` and `npm run example:direct` do the same. `package.json` is marked private and has no dependencies.

## Tests

The tests use Node's built-in test runner (`node:test`, Node.js 18 or newer) and never reach a wearable: gateway mode runs against an HTTP stub on `127.0.0.1`, and direct mode against sockets the tests open themselves on loopback, with their own multicast group and ports.

```bash
cd integrations/javascript
npm test
```

The direct-mode tests need loopback multicast. On Linux, enable it first, as [Try it without hardware](../README.md#try-it-without-hardware) describes; without it they are skipped. `HALOW_TEST_REQUIRE_MULTICAST=1` turns that skip into a failure. Two tests also compare the decoder with the Python client and run the example against [`tools/fake_wearable.py`](../../tools/fake_wearable.py); they are skipped when Python 3.8 or newer is not installed.
