// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// HalowGatewayClient against the stub gateway on 127.0.0.1: requests, headers, the token,
// argument checks, every error status, broken answers, timeouts and cancellation.
// Every client gets an explicit URL and token, so HALOW_URL and HALOW_TOKEN in the
// environment of whoever runs the tests do not matter here (see GatewayEnvironmentTests).

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Xunit;

namespace ClearObject.Halow.Tests
{
    public sealed class GatewayRequestTests : IDisposable
    {
        private readonly StubGateway stub = new StubGateway();

        public void Dispose()
        {
            stub.Dispose();
        }

        private HalowGatewayClient Client(string token = StubGateway.Token)
        {
            return new HalowGatewayClient(stub.Url, token);
        }

        private StubRequest Last => stub.Requests.Last();

        [Fact]
        public async Task Health_returns_the_body_and_sends_no_token()
        {
            using (HalowGatewayClient c = Client())
                Assert.Equal(StubGateway.HealthOk, await c.HealthAsync());
            StubRequest r = Last;
            Assert.Equal("GET", r.Method);
            Assert.Equal("/health", r.Path);
            Assert.Equal("HTTP/1.1", r.Version);
            Assert.Equal("application/json", r.Header("Accept"));
            Assert.Null(r.Header("Authorization"));
            Assert.Null(r.Header("Content-Type"));
            Assert.Empty(r.Body);
            Assert.Equal("127.0.0.1:" + stub.Port, r.Header("Host"));
        }

        [Fact]
        public async Task Health_503_returns_the_body()
        {
            const string body = "{\"ok\": false, \"error\": \"no HaLowLink address\"}";
            stub.Handler = r => StubReply.Json(503, body);
            using (HalowGatewayClient c = Client())
                Assert.Equal(body, await c.HealthAsync());
        }

        [Fact]
        public async Task Health_other_errors_raise()
        {
            stub.Handler = r => StubReply.Json(500, "{\"error\": \"internal error: x\"}");
            using (HalowGatewayClient c = Client())
            {
                HalowException e = await Assert.ThrowsAsync<HalowException>(() => c.HealthAsync());
                Assert.Equal(500, e.StatusCode);
                Assert.StartsWith("GET /health: HTTP 500: ", e.Message);
            }
        }

        [Fact]
        public async Task Wearables_returns_the_body_and_sends_no_token()
        {
            using (HalowGatewayClient c = Client())
                Assert.Equal(StubGateway.WearablesBody, await c.WearablesAsync());
            Assert.Equal(("GET", "/wearables"), (Last.Method, Last.Path));
            Assert.Null(Last.Header("Authorization"));
        }

        [Fact]
        public async Task Wearables_503_raises()
        {
            stub.Handler = r => StubReply.Json(503, "{\"error\": \"listener stopped\"}");
            using (HalowGatewayClient c = Client())
            {
                HalowException e = await Assert.ThrowsAsync<HalowException>(() => c.WearablesAsync());
                Assert.Equal(503, e.StatusCode);
                Assert.Contains("listener stopped", e.Message);
            }
        }

        [Fact]
        public async Task Alert_sends_the_token_and_the_body()
        {
            string record;
            using (HalowGatewayClient c = Client())
                record = await c.AlertAsync("zone-3", "PROTO", 1);
            Assert.Equal(StubGateway.AlertRecord, record);
            StubRequest r = Last;
            Assert.Equal(("POST", "/alert"), (r.Method, r.Path));
            Assert.Equal("Bearer " + StubGateway.Token, r.Header("Authorization"));
            Assert.Equal("application/json", r.Header("Accept"));
            Assert.StartsWith("application/json", r.Header("Content-Type"));
            // The gateway ignores a chunked body, so the length must be sent (docs/HTTP-API.md).
            Assert.Equal(r.Body.Length.ToString(CultureInfo.InvariantCulture), r.Header("Content-Length"));
            Assert.Null(r.Header("Transfer-Encoding"));
            Assert.Null(r.Header("Expect"));
            using (JsonDocument d = JsonDocument.Parse(r.Body))
            {
                Assert.Equal("zone-3", d.RootElement.GetProperty("label").GetString());
                Assert.Equal("proto", d.RootElement.GetProperty("format").GetString());
                Assert.Equal(1.0, d.RootElement.GetProperty("burst_seconds").GetDouble());
                Assert.Equal(3, d.RootElement.EnumerateObject().Count());
            }
        }

        [Fact]
        public async Task Alert_without_arguments_sends_an_empty_object()
        {
            using (HalowGatewayClient c = Client())
                await c.AlertAsync();
            Assert.Equal("{}", Last.BodyText);
            Assert.Equal("2", Last.Header("Content-Length"));
        }

        [Fact]
        public async Task Token_is_trimmed_and_an_empty_token_sends_no_header()
        {
            using (var c = new HalowGatewayClient(stub.Url, "  " + StubGateway.Token + "\t"))
                await c.AlertAsync();
            Assert.Equal("Bearer " + StubGateway.Token, Last.Header("Authorization"));

            using (var c = new HalowGatewayClient(stub.Url, ""))
            {
                HalowException e = await Assert.ThrowsAsync<HalowException>(() => c.AlertAsync());
                Assert.Equal(401, e.StatusCode);
            }
            Assert.Null(Last.Header("Authorization"));

            using (var c = new HalowGatewayClient(stub.Url, "   "))
                await Assert.ThrowsAsync<HalowException>(() => c.AlertAsync());
            Assert.Null(Last.Header("Authorization"));
        }

        [Theory]
        [InlineData("zone \"3\" \\ back")]
        [InlineData("line\nbreak\r\ttab")]
        [InlineData("\u0001\u001f control \u007f")]
        [InlineData("wär 🚧 ✓ \u2028 \u2029")]
        [InlineData("</script>{\"label\": 1}")]
        [InlineData("")]
        public async Task Alert_label_is_valid_json_and_round_trips(string label)
        {
            using (HalowGatewayClient c = Client())
                await c.AlertAsync(label);
            using (JsonDocument d = JsonDocument.Parse(Last.Body))
                Assert.Equal(label, d.RootElement.GetProperty("label").GetString());
        }

        [Fact]
        public async Task Alert_label_of_200_kilobytes_is_sent_whole()
        {
            string label = new string('x', 200 * 1024);
            using (HalowGatewayClient c = Client())
                await c.AlertAsync(label);
            Assert.Equal(label.Length + 13, Last.Body.Length);
        }

        [Theory]
        [InlineData("raw8", "raw8")]
        [InlineData("RAW8", "raw8")]
        [InlineData(" both ", "both")]
        [InlineData("Proto", "proto")]
        public async Task Alert_format_is_normalized(string format, string sent)
        {
            using (HalowGatewayClient c = Client())
                await c.AlertAsync(format: format);
            Assert.Equal("{\"format\": \"" + sent + "\"}", Last.BodyText);
        }

        [Theory]
        [InlineData(0.0)]
        [InlineData(0.5)]
        [InlineData(5.0)]
        [InlineData(7.25)]
        [InlineData(1e-7)]
        [InlineData(100.0)]
        [InlineData(0.1 + 0.2)]
        public async Task Alert_burst_seconds_is_a_json_number_that_round_trips(double burst)
        {
            using (HalowGatewayClient c = Client())
                await c.AlertAsync(burstSeconds: burst);
            using (JsonDocument d = JsonDocument.Parse(Last.Body))
            {
                JsonElement v = d.RootElement.GetProperty("burst_seconds");
                Assert.Equal(JsonValueKind.Number, v.ValueKind);
                Assert.Equal(burst, v.GetDouble());
            }
        }

        public static IEnumerable<object[]> BadAlertArguments()
        {
            yield return new object[] { "mp3", null };
            yield return new object[] { "", null };
            yield return new object[] { "   ", null };
            yield return new object[] { "raw", null };
            yield return new object[] { "raw8 proto", null };
            yield return new object[] { null, double.NaN };
            yield return new object[] { null, -1.0 };
            yield return new object[] { null, -0.001 };
            yield return new object[] { null, double.PositiveInfinity };
            yield return new object[] { null, double.NegativeInfinity };
        }

        [Theory]
        [MemberData(nameof(BadAlertArguments))]
        public void Alert_bad_arguments_throw_before_any_request(string format, double? burst)
        {
            // Thrown by the call itself, not by the returned task, as the Python client raises at once.
            using (HalowGatewayClient c = Client())
            {
                Exception e = Record.Exception(() => { _ = c.AlertAsync("x", format, burst); });
                Assert.IsType<ArgumentException>(e);
            }
            Assert.Equal(0, stub.Connections);
        }

        [Fact]
        public async Task Requests_go_below_a_base_path_and_trailing_slashes_are_dropped()
        {
            using (var c = new HalowGatewayClient("  " + stub.Url + "/prefix///  ", StubGateway.Token))
            {
                Assert.Equal(stub.Url + "/prefix", c.BaseUrl);
                HalowException e = await Assert.ThrowsAsync<HalowException>(() => c.HealthAsync());
                Assert.Equal(404, e.StatusCode);
                Assert.Contains("HALOW_URL", e.Message);
            }
            Assert.Equal("/prefix/health", Last.Path);
        }

        [Fact]
        public async Task Concurrent_calls_on_one_client()
        {
            using (HalowGatewayClient c = Client())
            {
                Task<string>[] calls = Enumerable.Range(0, 24)
                    .Select(i => i % 3 == 0 ? c.AlertAsync("n" + i) : i % 3 == 1 ? c.HealthAsync() : c.WearablesAsync()).ToArray();
                string[] results = await Task.WhenAll(calls);
                Assert.Equal(8, results.Count(r => r == StubGateway.AlertRecord));
                Assert.Equal(8, results.Count(r => r == StubGateway.HealthOk));
            }
            Assert.Equal(24, stub.Requests.Count);
        }

        [Fact]
        public async Task Response_text_is_returned_as_is()
        {
            using (HalowGatewayClient c = Client())
            {
                stub.Handler = r => StubReply.Json(200, "not json at all");
                Assert.Equal("not json at all", await c.HealthAsync());
                stub.Handler = r => StubReply.Json(200, "");
                Assert.Equal("", await c.WearablesAsync());
                stub.Handler = r => StubReply.Json(200, "{\"label\": \"wär 🚧\"}");
                Assert.Equal("{\"label\": \"wär 🚧\"}", await c.AlertAsync());
                string big = "{\"devices\": [" + string.Join(", ", Enumerable.Repeat("{\"ip\": \"127.0.0.1\", \"acks\": 1}", 5000)) + "]}";
                stub.Handler = r => StubReply.Json(200, big);
                Assert.Equal(big, await c.WearablesAsync());
            }
        }

        [Fact]
        public async Task An_http10_answer_that_ends_by_closing_is_read_whole()
        {
            // The gateway answers with HTTP/1.0 and closes the connection (docs/HTTP-API.md).
            stub.Handler = r => new StubReply { Http10WithoutLength = true, Body = StubGateway.AlertRecord };
            using (HalowGatewayClient c = Client())
            {
                for (int i = 0; i < 3; i++) Assert.Equal(StubGateway.AlertRecord, await c.AlertAsync());
            }
        }

        [Fact]
        public async Task A_kept_alive_connection_that_the_server_closes_is_not_a_failure()
        {
            stub.Handler = r =>
            {
                var reply = StubReply.Json(200, StubGateway.HealthOk);
                reply.KeepAlive = true;
                reply.Headers.Add(new KeyValuePair<string, string>("Keep-Alive", "timeout=5"));
                return reply;
            };
            using (HalowGatewayClient c = Client())
            {
                for (int i = 0; i < 5; i++) Assert.Equal(StubGateway.HealthOk, await c.HealthAsync());
            }
        }

        [Fact]
        public async Task Dispose_then_call_throws_ObjectDisposedException()
        {
            HalowGatewayClient c = Client();
            c.Dispose();
            c.Dispose();
            await Assert.ThrowsAsync<ObjectDisposedException>(() => c.HealthAsync());
            Assert.Equal(0, stub.Connections);
        }

        [Fact]
        public void Defaults_and_settings()
        {
            using (HalowGatewayClient c = Client())
            {
                Assert.Equal(TimeSpan.FromSeconds(10), c.RequestTimeout);
                c.RequestTimeout = TimeSpan.FromSeconds(3);
                Assert.Equal(TimeSpan.FromSeconds(3), c.RequestTimeout);
            }
            using (var c = new HalowGatewayClient(stub.Url, "t", TimeSpan.FromSeconds(42)))
                Assert.Equal(TimeSpan.FromSeconds(42), c.RequestTimeout);
            using (var c = new HalowGatewayClient("HTTPS://gateway.example:8443", "t"))
                Assert.Equal("HTTPS://gateway.example:8443", c.BaseUrl);
        }

        [Theory]
        [InlineData("ftp://192.0.2.10:8080")]
        [InlineData("192.0.2.10:8080")]
        [InlineData("localhost:8080")]
        [InlineData("http:/192.0.2.10")]
        [InlineData("http://")]
        [InlineData("file:///etc/passwd")]
        public void Constructor_rejects_a_url_without_http(string url)
        {
            ArgumentException e = Assert.Throws<ArgumentException>(() => new HalowGatewayClient(url, "t"));
            Assert.Contains("http://", e.Message);
        }

        [Theory]
        [InlineData("http://gateway:8080")]
        [InlineData("http://my_gateway.local:8080")]
        [InlineData("http://192.0.2.10:8080/base")]
        [InlineData("http://[::1]:8080")]
        [InlineData("https://192.0.2.10")]
        [InlineData("http://192.0.2.10:8080?unused=1")]
        public void Constructor_accepts_valid_urls(string url)
        {
            using (var c = new HalowGatewayClient(url, "t"))
                Assert.Equal(url, c.BaseUrl);      // nothing is sent
        }

        [Theory]
        [InlineData("http://exa mple:8080")]
        [InlineData("http://[::1")]
        [InlineData("http://127.0.0.1:99999")]
        [InlineData("http:///health")]
        [InlineData("http://:8080")]
        public void Constructor_rejects_a_malformed_url(string url)
        {
            // Before the fix these passed the constructor and every call then threw
            // UriFormatException instead of HalowException.
            Assert.Throws<ArgumentException>(() => new HalowGatewayClient(url, "t"));
        }
    }

    public sealed class GatewayErrorTests : IDisposable
    {
        private readonly StubGateway stub = new StubGateway();

        public void Dispose()
        {
            stub.Dispose();
        }

        [Theory]
        [InlineData(400, "the gateway rejected the request")]
        [InlineData(401, "missing or wrong token: set HALOW_TOKEN to the gateway's token")]
        [InlineData(404, "unknown path: is HALOW_URL the halow-alert gateway (http://<host>:8080)?")]
        [InlineData(413, "request body too large")]
        [InlineData(429, "rate limited: the gateway sends at most one alert every 0.5 s; retry shortly")]
        [InlineData(500, "the gateway could not send the alert (HaLowLink unplugged or not set up? see GET /health)")]
        [InlineData(503, "the gateway cannot send right now (no HaLowLink address on the gateway host, or its reply listener stopped; see GET /health)")]
        [InlineData(418, "unexpected response")]
        [InlineData(502, "unexpected response")]
        [InlineData(201, "unexpected response")]
        public async Task Every_status_has_its_hint_and_detail(int status, string hint)
        {
            string body = "{\n \"error\": \"reason " + status + "\"\n}";
            stub.Handler = r => StubReply.Json(status, body);
            using (var c = new HalowGatewayClient(stub.Url, StubGateway.Token))
            {
                HalowException e = await Assert.ThrowsAsync<HalowException>(() => c.AlertAsync("x"));
                Assert.Equal(status, e.StatusCode);
                Assert.Equal(body, e.Body);
                Assert.Equal("POST /alert: HTTP " + status + ": " + hint + " (reason " + status + ")", e.Message);
            }
        }

        [Fact]
        public async Task Wrong_token_gives_401_with_the_token_hint()
        {
            using (var c = new HalowGatewayClient(stub.Url, "wrong"))
            {
                HalowException e = await Assert.ThrowsAsync<HalowException>(() => c.AlertAsync("x"));
                Assert.Equal(401, e.StatusCode);
                Assert.Contains("HTTP 401", e.Message);
                Assert.Contains("HALOW_TOKEN", e.Message);
                Assert.Contains("missing or wrong Bearer token", e.Message);
            }
        }

        [Theory]
        [InlineData("{\"error\": \"a \\\"q\\\" \\\\ b\\nc\\td\"}", "a \"q\" \\ b\nc\td")]
        [InlineData("{\"error\": \"\\u00e9\\ud83d\\ude00 \\/ \\b\\f\\r\"}", "é😀 / \b\f\r")]
        [InlineData("{\"error\":\"tight\"}", "tight")]
        [InlineData("{\"x\": 1, \"error\"  :  \"spaced\", \"y\": 2}", "spaced")]
        [InlineData("{\"error\": \"bad \\u12 escape\"}", "bad 12 escape")]
        public async Task Error_text_is_unescaped(string body, string detail)
        {
            stub.Handler = r => StubReply.Json(400, body);
            using (var c = new HalowGatewayClient(stub.Url, StubGateway.Token))
            {
                HalowException e = await Assert.ThrowsAsync<HalowException>(() => c.AlertAsync());
                Assert.Equal("POST /alert: HTTP 400: the gateway rejected the request (" + detail + ")", e.Message);
            }
        }

        [Theory]
        [InlineData("boom")]
        [InlineData("")]
        [InlineData("{\"error\": null}")]
        [InlineData("{\"error\": 5}")]
        [InlineData("<html><body>Internal Server Error</body></html>")]
        [InlineData("[1, 2]")]
        public async Task Error_bodies_without_an_error_text_give_the_hint_alone(string body)
        {
            stub.Handler = r => StubReply.Json(500, body);
            using (var c = new HalowGatewayClient(stub.Url, StubGateway.Token))
            {
                HalowException e = await Assert.ThrowsAsync<HalowException>(() => c.AlertAsync());
                Assert.Equal(500, e.StatusCode);
                Assert.Equal(body, e.Body);
                Assert.Equal("POST /alert: HTTP 500: the gateway could not send the alert (HaLowLink unplugged or not set up? see GET /health)", e.Message);
            }
        }

        [Fact]
        public async Task A_redirect_is_not_followed()
        {
            stub.Handler = r =>
            {
                if (r.Path != "/alert") return StubReply.Json(200, "{}");
                var reply = StubReply.Json(302, "");
                reply.Headers.Add(new KeyValuePair<string, string>("Location", stub.Url + "/elsewhere"));
                return reply;
            };
            using (var c = new HalowGatewayClient(stub.Url, StubGateway.Token))
            {
                HalowException e = await Assert.ThrowsAsync<HalowException>(() => c.AlertAsync());
                Assert.Equal(302, e.StatusCode);
                Assert.Contains("unexpected response", e.Message);
            }
            Assert.Single(stub.Requests);
        }

        [Fact]
        public async Task A_closed_connection_without_an_answer()
        {
            stub.Handler = r => new StubReply { CloseWithoutReply = true };
            using (var c = new HalowGatewayClient(stub.Url, StubGateway.Token))
            {
                HalowException e = await Assert.ThrowsAsync<HalowException>(() => c.AlertAsync());
                Assert.Null(e.StatusCode);
                Assert.Null(e.Body);
                Assert.StartsWith("cannot reach the gateway at " + stub.Url + ": ", e.Message);
            }
        }

        [Theory]
        [InlineData("HELLO\r\n\r\n")]
        [InlineData("HTTP/1.1 abc Nope\r\n\r\n")]
        [InlineData("SSH-2.0-OpenSSH_9.6\r\n")]
        [InlineData("HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort")]
        public async Task An_answer_that_is_not_http(string raw)
        {
            stub.Handler = r => new StubReply { Raw = Encoding.ASCII.GetBytes(raw) };
            using (var c = new HalowGatewayClient(stub.Url, StubGateway.Token))
            {
                HalowException e = await Assert.ThrowsAsync<HalowException>(() => c.HealthAsync());
                Assert.Null(e.StatusCode);
                Assert.StartsWith("cannot reach the gateway at " + stub.Url + ": ", e.Message);
            }
        }

        [Fact]
        public async Task An_unknown_charset_still_returns_the_text()
        {
            // Before the fix ReadAsStringAsync threw InvalidOperationException here, which the
            // example reported as bad settings (exit 2). The gateway's JSON is always UTF-8.
            stub.Handler = r => new StubReply { Body = "{\"ok\": true, \"id\": \"wär\"}", ContentType = "application/json; charset=no-such-charset" };
            using (var c = new HalowGatewayClient(stub.Url, StubGateway.Token))
                Assert.Equal("{\"ok\": true, \"id\": \"wär\"}", await c.HealthAsync());
            stub.Handler = r => new StubReply { Status = 429, Body = "{\"error\": \"slow down\"}", ContentType = "application/json; charset=\"\"" };
            using (var c = new HalowGatewayClient(stub.Url, StubGateway.Token))
            {
                HalowException e = await Assert.ThrowsAsync<HalowException>(() => c.AlertAsync());
                Assert.Equal(429, e.StatusCode);
                Assert.Contains("(slow down)", e.Message);
            }
        }

        [Fact]
        public void A_token_with_a_line_break_is_refused_by_the_constructor()
        {
            // Before the fix every call threw FormatException ("New-line characters are not
            // allowed in header values") instead of HalowException. Surrounding line breaks, as
            // in a token read from a file, are trimmed as before.
            foreach (string token in new[] { "to\nken", "a\r\nX-Injected: 1", "tok\ren" })
                Assert.Throws<ArgumentException>(() => new HalowGatewayClient(stub.Url, token));
            using (new HalowGatewayClient(stub.Url, "token\r\n"))
            {
            }
            Assert.Equal(0, stub.Connections);
        }

        [Fact]
        public async Task A_token_that_cannot_be_sent_is_a_HalowException()
        {
            using (var c = new HalowGatewayClient(stub.Url, "tök€n"))
            {
                HalowException e = await Assert.ThrowsAsync<HalowException>(() => c.AlertAsync());
                Assert.Null(e.StatusCode);
            }
            Assert.Empty(stub.Requests);
        }
    }

    public sealed class GatewayConnectionTests
    {
        [Fact]
        public async Task Connection_refused()
        {
            int port = Net.FreeTcpPort();          // nothing listens there now
            string url = "http://127.0.0.1:" + port;
            using (var c = new HalowGatewayClient(url, "t", TimeSpan.FromSeconds(8)))
            {
                HalowException e = await Assert.ThrowsAsync<HalowException>(() => c.HealthAsync());
                Assert.Null(e.StatusCode);
                Assert.Null(e.Body);
                Assert.StartsWith("cannot reach the gateway at " + url + ": ", e.Message);
                Assert.DoesNotContain("timed out", e.Message);
            }
        }

        [Fact]
        public async Task Timeout_when_the_gateway_never_answers()
        {
            using (var stub = new StubGateway(r => new StubReply { Hang = true }))
            using (var c = new HalowGatewayClient(stub.Url, "t", TimeSpan.FromMilliseconds(300)))
            {
                var clock = Stopwatch.StartNew();
                HalowException e = await Assert.ThrowsAsync<HalowException>(() => c.HealthAsync());
                Assert.InRange(clock.Elapsed, TimeSpan.FromMilliseconds(250), TimeSpan.FromSeconds(5));
                Assert.Equal("cannot reach the gateway at " + stub.Url + ": timed out", e.Message);
                Assert.Null(e.StatusCode);
                Assert.Single(stub.Requests);
            }
        }

        [Fact]
        public async Task RequestTimeout_can_be_changed_after_construction()
        {
            using (var stub = new StubGateway(r => new StubReply { Hang = true }))
            using (var c = new HalowGatewayClient(stub.Url, "t"))
            {
                c.RequestTimeout = TimeSpan.FromMilliseconds(200);
                var clock = Stopwatch.StartNew();
                await Assert.ThrowsAsync<HalowException>(() => c.WearablesAsync());
                Assert.True(clock.Elapsed < TimeSpan.FromSeconds(5), "took " + clock.Elapsed);
            }
        }

        [Fact]
        public async Task A_burst_extends_the_alert_timeout()
        {
            // The gateway answers POST /alert when the burst ends: the client waits the burst longer.
            using (var stub = new StubGateway(r => new StubReply { Delay = TimeSpan.FromMilliseconds(1000), Body = StubGateway.AlertRecord }))
            using (var c = new HalowGatewayClient(stub.Url, StubGateway.Token, TimeSpan.FromMilliseconds(300)))
            {
                await Assert.ThrowsAsync<HalowException>(() => c.AlertAsync("no burst"));
                Assert.Equal(StubGateway.AlertRecord, await c.AlertAsync("burst", burstSeconds: 2.0));
            }
        }

        [Fact]
        public async Task An_infinite_timeout_stays_infinite_with_a_burst()
        {
            // Before the fix Timeout.InfiniteTimeSpan (-1 ms) plus a 1 s burst became a 999 ms
            // timeout: the call failed while the gateway was still sending, and a retry would alert twice.
            using (var stub = new StubGateway(r => new StubReply { Delay = TimeSpan.FromMilliseconds(1500), Body = StubGateway.AlertRecord }))
            using (var c = new HalowGatewayClient(stub.Url, StubGateway.Token, Timeout.InfiniteTimeSpan))
            {
                Assert.Equal(StubGateway.AlertRecord, await Net.Within(c.AlertAsync("x", burstSeconds: 1), TimeSpan.FromSeconds(10), "the alert"));
                Assert.Equal(StubGateway.AlertRecord, await Net.Within(c.AlertAsync("x"), TimeSpan.FromSeconds(10), "the alert"));
            }
        }

        [Fact]
        public async Task Cancellation_while_waiting_throws_OperationCanceledException()
        {
            using (var stub = new StubGateway(r => new StubReply { Hang = true }))
            using (var c = new HalowGatewayClient(stub.Url, "t", TimeSpan.FromSeconds(30)))
            using (var cts = new CancellationTokenSource())
            {
                Task<string> call = c.AlertAsync("x", cancellationToken: cts.Token);
                Assert.True(await Net.Until(() => stub.Requests.Count == 1, TimeSpan.FromSeconds(10)));
                var clock = Stopwatch.StartNew();
                cts.Cancel();
                Exception e = await Record.ExceptionAsync(() => Net.Within(call, TimeSpan.FromSeconds(5), "the cancelled call"));
                Assert.IsAssignableFrom<OperationCanceledException>(e);
                Assert.True(clock.Elapsed < TimeSpan.FromSeconds(3));
            }
        }

        [Fact]
        public async Task An_already_cancelled_token_throws_OperationCanceledException()
        {
            using (var stub = new StubGateway())
            using (var c = new HalowGatewayClient(stub.Url, "t"))
            using (var cts = new CancellationTokenSource())
            {
                cts.Cancel();
                Exception e = await Record.ExceptionAsync(() => c.HealthAsync(cts.Token));
                Assert.IsAssignableFrom<OperationCanceledException>(e);
                e = await Record.ExceptionAsync(() => c.WearablesAsync(cts.Token));
                Assert.IsAssignableFrom<OperationCanceledException>(e);
            }
        }

        [Fact]
        public async Task A_slow_body_counts_against_the_timeout()
        {
            // Headers arrive, the body never completes: the timeout still applies.
            using (var listener = new TcpListenerScope())
            {
                listener.OnConnection = async (s, token) =>
                {
                    byte[] head = Encoding.ASCII.GetBytes("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n{\"ok\"");
                    await s.WriteAsync(head, 0, head.Length, token);
                    await Task.Delay(Timeout.Infinite, token);
                };
                using (var c = new HalowGatewayClient(listener.Url, "t", TimeSpan.FromMilliseconds(400)))
                {
                    var clock = Stopwatch.StartNew();
                    HalowException e = await Assert.ThrowsAsync<HalowException>(() => c.HealthAsync());
                    Assert.Contains("timed out", e.Message);
                    Assert.True(clock.Elapsed < TimeSpan.FromSeconds(5), "took " + clock.Elapsed);
                }
            }
        }
    }

    /// <summary>A bare TCP server on 127.0.0.1 that hands each connection to the test.</summary>
    internal sealed class TcpListenerScope : IDisposable
    {
        private readonly TcpListener listener = new TcpListener(IPAddress.Loopback, 0);
        private readonly CancellationTokenSource stopping = new CancellationTokenSource();

        public TcpListenerScope()
        {
            listener.Start();
            _ = Task.Run(async () =>
            {
                while (!stopping.IsCancellationRequested)
                {
                    TcpClient c;
                    try
                    {
                        c = await listener.AcceptTcpClientAsync(stopping.Token);
                    }
                    catch (Exception)
                    {
                        return;
                    }
                    _ = Task.Run(async () =>
                    {
                        using (c)
                        {
                            try
                            {
                                if (OnConnection != null) await OnConnection(c.GetStream(), stopping.Token);
                            }
                            catch (Exception)
                            {
                            }
                        }
                    });
                }
            });
        }

        public Func<NetworkStream, CancellationToken, Task> OnConnection { get; set; }
        public string Url => "http://127.0.0.1:" + ((IPEndPoint)listener.LocalEndpoint).Port;

        public void Dispose()
        {
            stopping.Cancel();
            listener.Stop();
        }
    }
}
