// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// Example.cs as a child process (the built HalowExample.dll), the way CI runs it: exit codes,
// the HALOW_ACK_WAIT check, and the output lines CI looks for. Gateway mode runs against the
// stub gateway; direct mode against a stand-in wearable played by the test on 127.0.0.1.
// Build the example first: dotnet build integrations/csharp -c Release

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Sockets;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using Xunit;

namespace ClearObject.Halow.Tests
{
    /// <summary>
    /// A test that runs the built example: skipped when it is not built, unless
    /// HALOW_TEST_REQUIRE_EXAMPLE=1. With NeedsMulticast, also skipped without loopback
    /// multicast, unless HALOW_TEST_REQUIRE_MULTICAST=1.
    /// </summary>
    public sealed class ExampleFactAttribute : FactAttribute
    {
        public ExampleFactAttribute()
        {
            if (Paths.ExampleDll() == null && !TestEnv.Flag("HALOW_TEST_REQUIRE_EXAMPLE"))
                Skip = "the example is not built (dotnet build integrations/csharp -c Release). "
                    + "Set HALOW_TEST_REQUIRE_EXAMPLE=1 to make this a failure instead of a skip.";
        }

        public bool NeedsMulticast
        {
            get => false;
            set
            {
                if (value && Skip == null && !Multicast.Required && Multicast.Unavailable != null)
                    Skip = "loopback multicast is not available: " + Multicast.Unavailable + "." + Multicast.SkipHint;
            }
        }
    }

    /// <summary>ExampleFact for a theory.</summary>
    public sealed class ExampleTheoryAttribute : TheoryAttribute
    {
        public ExampleTheoryAttribute()
        {
            Skip = new ExampleFactAttribute().Skip;
        }
    }

    internal sealed class ExampleRun
    {
        public int ExitCode { get; set; }
        public string Stdout { get; set; }
        public string Stderr { get; set; }

        public override string ToString()
        {
            return "exit " + ExitCode + "\n--- stdout\n" + Stdout + "\n--- stderr\n" + Stderr;
        }
    }

    internal static class Example
    {
        public static string Dll()
        {
            string dll = Paths.ExampleDll();
            Assert.True(dll != null, "the example is not built: dotnet build integrations/csharp -c Release (HALOW_TEST_REQUIRE_EXAMPLE=1)");
            return dll;
        }

        /// <summary>Runs the example with only the given HALOW_* settings (the caller's are removed).</summary>
        public static async Task<ExampleRun> RunAsync(IDictionary<string, string> env, params string[] args)
        {
            return await RunInAsync(null, env, args);
        }

        public static async Task<ExampleRun> RunInAsync(string cwd, IDictionary<string, string> env, params string[] args)
        {
            var psi = new ProcessStartInfo(Paths.DotnetHost())
            {
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                WorkingDirectory = cwd ?? Path.GetTempPath(),
            };
            psi.ArgumentList.Add(Dll());
            foreach (string a in args) psi.ArgumentList.Add(a);
            foreach (string key in psi.Environment.Keys.ToList())
            {
                if (key.StartsWith("HALOW_", StringComparison.OrdinalIgnoreCase) || key.EndsWith("_PROXY", StringComparison.OrdinalIgnoreCase))
                    psi.Environment.Remove(key);
            }
            psi.Environment["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1";
            psi.Environment["DOTNET_NOLOGO"] = "1";
            // Safe settings for everything a test leaves out, so the example never falls back to
            // the real gateway URL, sender address, group or ports.
            psi.Environment["HALOW_URL"] = "http://127.0.0.1:" + Net.FreeTcpPort();
            psi.Environment["HALOW_EGRESS"] = Net.Loopback;
            psi.Environment["HALOW_GROUP"] = Net.NewGroup();
            psi.Environment["HALOW_PORT"] = Net.FreeUdpPort().ToString();
            psi.Environment["HALOW_RESPONSE_PORT"] = Net.FreeTcpPort().ToString();
            foreach (KeyValuePair<string, string> kv in env ?? new Dictionary<string, string>()) psi.Environment[kv.Key] = kv.Value;

            using (var p = new Process { StartInfo = psi })
            {
                p.Start();
                Task<string> stdout = p.StandardOutput.ReadToEndAsync();
                Task<string> stderr = p.StandardError.ReadToEndAsync();
                using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(45)))
                {
                    try
                    {
                        await p.WaitForExitAsync(cts.Token);
                    }
                    catch (OperationCanceledException)
                    {
                        p.Kill(true);
                        Assert.Fail("the example did not exit within 45 s: " + string.Join(" ", args));
                    }
                }
                return new ExampleRun { ExitCode = p.ExitCode, Stdout = await stdout, Stderr = await stderr };
            }
        }

        public static int VectorCount()
        {
            using (JsonDocument d = JsonDocument.Parse(File.ReadAllText(Paths.VectorsPath)))
                return new[] { "raw8", "gateway_message", "device_response" }.Sum(k => d.RootElement.GetProperty(k).GetArrayLength());
        }
    }

    public sealed class ExampleSelfTestTests
    {
        [ExampleFact]
        public async Task Selftest_with_the_vectors_path()
        {
            ExampleRun r = await Example.RunAsync(null, "--selftest", Paths.VectorsPath);
            Assert.True(r.ExitCode == 0, r.ToString());
            int n = Example.VectorCount();
            Assert.Contains("selftest: " + n + "/" + n + " vectors match", r.Stdout);
        }

        [ExampleFact]
        public async Task Selftest_finds_the_vectors_next_to_the_build()
        {
            string empty = Directory.CreateTempSubdirectory("halow-example-").FullName;
            try
            {
                ExampleRun r = await Example.RunInAsync(empty, null, "--selftest");
                Assert.True(r.ExitCode == 0, r.ToString());
                Assert.Matches(@"selftest: (\d+)/\1 vectors match", r.Stdout);
            }
            finally
            {
                Directory.Delete(empty, true);
            }
        }

        [ExampleFact]
        public async Task Selftest_with_a_missing_file_fails()
        {
            ExampleRun r = await Example.RunAsync(null, "--selftest", Path.Combine(Path.GetTempPath(), "no-such-vectors-" + Guid.NewGuid() + ".json"));
            Assert.True(r.ExitCode != 0, r.ToString());
            Assert.Contains("example: ", r.Stderr);
        }
    }

    public sealed class ExampleGatewayTests : IDisposable
    {
        private readonly StubGateway stub = new StubGateway();
        private int wearablesCalls;
        private volatile bool alerted;

        public void Dispose()
        {
            stub.Dispose();
        }

        private Dictionary<string, string> Env(string ackWait = "10", string token = StubGateway.Token)
        {
            return new Dictionary<string, string> { ["HALOW_URL"] = stub.Url, ["HALOW_TOKEN"] = token, ["HALOW_ACK_WAIT"] = ackWait };
        }

        /// <summary>The gateway: health ok; one more ACK shows up in GET /wearables after the alert (when ackAfterAlert).</summary>
        private StubReply Gateway(StubRequest r, bool ackAfterAlert)
        {
            if (r.Method == "GET" && r.Path == "/wearables")
            {
                Interlocked.Increment(ref wearablesCalls);
                int acks = alerted && ackAfterAlert ? 3 : 2;
                return StubReply.Json(200, "{\"devices\": [{\"ip\": \"127.0.0.1\", \"acks\": " + acks
                    + ", \"heartbeats\": 4, \"device_id\": \"wz-07\", \"battery_percentage\": 87}], \"recent\": []}");
            }
            StubReply reply = StubGateway.Default(r);
            if (r.Path == "/alert" && reply.Status == 200) alerted = true;
            return reply;
        }

        [ExampleFact]
        public async Task Alert_and_ack()
        {
            stub.Handler = r => Gateway(r, true);
            ExampleRun run = await Example.RunAsync(Env());
            Assert.True(run.ExitCode == 0, run.ToString());
            Assert.Contains("gateway health: {\"ok\":true,", run.Stdout);
            Assert.Contains("alert sent: {\"event\":\"alert_sent\",", run.Stdout);
            Assert.Contains("wearable 127.0.0.1: acks=3 heartbeats=4 device_id=wz-07 battery=87%", run.Stdout);
            Assert.Contains("ACK received", run.Stdout);
            StubRequest alert = stub.Requests.Single(q => q.Path == "/alert");
            Assert.Equal("Bearer " + StubGateway.Token, alert.Header("Authorization"));
            Assert.Equal("{\"label\": \"csharp example\"}", alert.BodyText);
            Assert.Equal(new[] { "/health", "/wearables", "/alert" }, stub.Requests.Take(3).Select(q => q.Path));
        }

        [ExampleFact]
        public async Task Wrong_token_exits_1_with_http_401()
        {
            stub.Handler = r => Gateway(r, true);
            ExampleRun run = await Example.RunAsync(Env(token: "wrong"));
            Assert.True(run.ExitCode == 1, run.ToString());
            Assert.Contains("HTTP 401", run.Stderr);
            Assert.StartsWith("example: POST /alert: HTTP 401: ", run.Stderr);
        }

        [ExampleFact]
        public async Task No_ack_in_time_exits_3()
        {
            stub.Handler = r => Gateway(r, false);
            ExampleRun run = await Example.RunAsync(Env(ackWait: "0.6"));
            Assert.True(run.ExitCode == 3, run.ToString());
            Assert.Contains("no ACK within 0.6 s", run.Stderr);
            Assert.Single(stub.Requests, q => q.Path == "/alert");
        }

        [ExampleFact]
        public async Task Gateway_that_cannot_send_exits_1_before_alerting()
        {
            stub.Handler = r => r.Path == "/health"
                ? StubReply.Json(503, "{\"ok\": false, \"error\": \"no HaLowLink address\", \"listener\": true}")
                : Gateway(r, true);
            ExampleRun run = await Example.RunAsync(Env());
            Assert.True(run.ExitCode == 1, run.ToString());
            Assert.Contains("the gateway cannot send right now: no HaLowLink address", run.Stderr);
            Assert.DoesNotContain(stub.Requests, q => q.Path == "/alert");
        }

        [ExampleFact]
        public async Task Unreachable_gateway_exits_1()
        {
            var env = new Dictionary<string, string> { ["HALOW_URL"] = "http://127.0.0.1:" + Net.FreeTcpPort(), ["HALOW_TOKEN"] = "t" };
            ExampleRun run = await Example.RunAsync(env);
            Assert.True(run.ExitCode == 1, run.ToString());
            Assert.Contains("cannot reach the gateway", run.Stderr);
        }

        [ExampleTheory]
        [InlineData("ftp://127.0.0.1:1")]
        [InlineData("http://exa mple:8080")]
        public async Task Bad_url_exits_2(string url)
        {
            ExampleRun run = await Example.RunAsync(new Dictionary<string, string> { ["HALOW_URL"] = url, ["HALOW_TOKEN"] = "t" });
            Assert.True(run.ExitCode == 2, run.ToString());
        }

        [ExampleTheory]
        [InlineData("abc")]
        [InlineData("-1")]
        [InlineData("3601")]
        [InlineData("nan")]
        [InlineData("Infinity")]
        [InlineData("1e999")]
        [InlineData("10s")]
        public async Task Bad_HALOW_ACK_WAIT_exits_2_before_any_request(string ackWait)
        {
            stub.Handler = r => Gateway(r, true);
            ExampleRun run = await Example.RunAsync(Env(ackWait: ackWait));
            Assert.True(run.ExitCode == 2, run.ToString());
            Assert.Contains("HALOW_ACK_WAIT=\"" + ackWait + "\" must be a number of seconds (0 to 3600)", run.Stderr);
            Assert.Equal(0, stub.Connections);
        }

        [ExampleTheory]
        [InlineData("0")]
        [InlineData(" 3600 ")]
        [InlineData("2.5")]
        [InlineData("")]
        public async Task Good_HALOW_ACK_WAIT_values(string ackWait)
        {
            stub.Handler = r => Gateway(r, true);
            ExampleRun run = await Example.RunAsync(Env(ackWait: ackWait));
            // "0" may give up before polling; the others see the ACK on the first poll.
            Assert.True(run.ExitCode == 0 || (ackWait == "0" && run.ExitCode == 3), run.ToString());
            Assert.Single(stub.Requests, q => q.Path == "/alert");
        }
    }

    /// <summary>The stand-in wearable for the direct-mode example: answers what arrives on the test group.</summary>
    internal sealed class FakeWearable : IDisposable
    {
        private readonly MulticastReceiver rx;
        private readonly CancellationTokenSource stopping = new CancellationTokenSource();
        private readonly Task loop;
        private readonly List<byte[]> received = new List<byte[]>();

        public FakeWearable(int responsePort, string deviceId, bool answerHeartbeat = true, bool answerAlert = true, byte[] heartbeatOverride = null)
        {
            rx = new MulticastReceiver(Net.NewGroup());
            Group = rx.Group;
            Port = rx.Port;
            loop = Task.Run(async () =>
            {
                var acked = new HashSet<string>();
                while (!stopping.IsCancellationRequested)
                {
                    UdpReceiveResult? d = await rx.ReceiveAsync(TimeSpan.FromMilliseconds(200));
                    if (d == null) continue;
                    byte[] data = d.Value.Buffer;
                    lock (received) received.Add(data);
                    if (data.Length == 8)
                    {
                        if (answerAlert && acked.Add(Hex.Of(data))) await Wearable.SendAsync(responsePort, Wearable.Ack((ulong)Net.NowMs()));
                        continue;
                    }
                    (ulong ts, ulong cmd) = GatewayMessage.Parse(data);
                    if (cmd == 2 && answerHeartbeat)
                        await Wearable.SendAsync(responsePort, heartbeatOverride ?? Wearable.Heartbeat(ts, deviceId));
                    else if (cmd == 1 && answerAlert && acked.Add("proto" + ts))
                        await Wearable.SendAsync(responsePort, Wearable.Ack((ulong)Net.NowMs()));
                }
            });
        }

        public string Group { get; }
        public int Port { get; }

        public List<byte[]> Received
        {
            get
            {
                lock (received) return received.ToList();
            }
        }

        public void Dispose()
        {
            stopping.Cancel();
            try
            {
                loop.Wait(TimeSpan.FromSeconds(5));
            }
            catch (AggregateException)
            {
            }
            rx.Dispose();
        }
    }

    public sealed class ExampleDirectTests
    {
        private static Dictionary<string, string> Env(FakeWearable w, int responsePort, string ackWait = "10")
        {
            return new Dictionary<string, string>
            {
                ["HALOW_EGRESS"] = "127.0.0.1",
                ["HALOW_GROUP"] = w.Group,
                ["HALOW_PORT"] = w.Port.ToString(),
                ["HALOW_RESPONSE_PORT"] = responsePort.ToString(),
                ["HALOW_ACK_WAIT"] = ackWait,
            };
        }

        [ExampleFact(NeedsMulticast = true)]
        public async Task Heartbeat_alert_and_ack()
        {
            Multicast.Require();
            int rport = Net.FreeTcpPort();
            using (var w = new FakeWearable(rport, "fake-direct"))
            {
                ExampleRun run = await Example.RunAsync(Env(w, rport), "--direct");
                Assert.True(run.ExitCode == 0, run.ToString());
                // The lines CI checks (.github/workflows/checks.yml, e2e and windows-direct).
                Assert.Matches(@"HEARTBEAT from 127\.0\.0\.1: device_id=fake-direct battery=87% rssi=-62 dBm, round trip \d+ ms", run.Stdout);
                Assert.Matches(@"alert sent: raw8 -> " + Regex.Escape(w.Group + ":" + w.Port) + @" from 127\.0\.0\.1 \(1 datagram\(s\)\), ts_ms \d+", run.Stdout);
                Assert.Matches(@"ACK from 127\.0\.0\.1: wearable timestamp \d+ ms", run.Stdout);
                Assert.Equal(2, w.Received.Count);
            }
        }

        [ExampleFact(NeedsMulticast = true)]
        public async Task Format_both_sends_two_datagrams()
        {
            Multicast.Require();
            int rport = Net.FreeTcpPort();
            using (var w = new FakeWearable(rport, "fake-both"))
            {
                Dictionary<string, string> env = Env(w, rport);
                env["HALOW_FORMAT"] = "both";
                ExampleRun run = await Example.RunAsync(env, "--direct");
                Assert.True(run.ExitCode == 0, run.ToString());
                Assert.Contains("alert sent: both -> ", run.Stdout);
                Assert.Contains("(2 datagram(s))", run.Stdout);
                Assert.Equal(3, w.Received.Count);
            }
        }

        [ExampleFact(NeedsMulticast = true)]
        public async Task No_heartbeat_and_no_ack_exits_3()
        {
            Multicast.Require();
            int rport = Net.FreeTcpPort();
            using (var w = new FakeWearable(rport, "silent", answerHeartbeat: false, answerAlert: false))
            {
                ExampleRun run = await Example.RunAsync(Env(w, rport, ackWait: "0.5"), "--direct");
                Assert.True(run.ExitCode == 3, run.ToString());
                Assert.Contains("no heartbeat within 3 s (continuing)", run.Stdout);
                Assert.Contains("alert sent: raw8", run.Stdout);
                Assert.Contains("no ACK within 0.5 s", run.Stderr);
            }
        }

        [ExampleFact(NeedsMulticast = true)]
        public async Task A_heartbeat_without_a_payload_does_not_stop_the_example()
        {
            // Before the fix the example threw NullReferenceException here and exited 1 without alerting.
            Multicast.Require();
            int rport = Net.FreeTcpPort();
            byte[] bare = new Proto().Varint(1, 5).Varint(2, 2).ToArray();
            using (var w = new FakeWearable(rport, "unused", heartbeatOverride: bare))
            {
                ExampleRun run = await Example.RunAsync(Env(w, rport), "--direct");
                Assert.True(run.ExitCode == 0, run.ToString());
                Assert.Contains("HEARTBEAT from 127.0.0.1: no heartbeat payload", run.Stdout);
                Assert.Contains("ACK from 127.0.0.1", run.Stdout);
            }
        }

        [ExampleFact(NeedsMulticast = true)]
        public async Task Bad_HALOW_ACK_WAIT_exits_2_and_sends_nothing()
        {
            Multicast.Require();
            using (var rx = new MulticastReceiver(Net.NewGroup()))
            {
                var env = new Dictionary<string, string>
                {
                    ["HALOW_EGRESS"] = "127.0.0.1", ["HALOW_GROUP"] = rx.Group, ["HALOW_PORT"] = rx.Port.ToString(),
                    ["HALOW_RESPONSE_PORT"] = Net.FreeTcpPort().ToString(),
                };
                foreach (string bad in new[] { "abc", "-5", "nan" })
                {
                    env["HALOW_ACK_WAIT"] = bad;
                    ExampleRun run = await Example.RunAsync(env, "--direct");
                    Assert.True(run.ExitCode == 2, run.ToString());
                    Assert.Contains("HALOW_ACK_WAIT", run.Stderr);
                }
                await rx.AssertNothingReceivedAsync();
            }
        }

        [ExampleTheory]
        [InlineData("HALOW_PORT", "70000")]
        [InlineData("HALOW_PORT", "abc")]
        [InlineData("HALOW_RESPONSE_PORT", "0")]
        [InlineData("HALOW_RESPONSE_PORT", "-1")]
        [InlineData("HALOW_EGRESS", "999.1.1.1")]
        [InlineData("HALOW_EGRESS", "192.0.2.1")]
        [InlineData("HALOW_GROUP", "not-a-group")]
        public async Task Bad_direct_settings_exit_2(string name, string value)
        {
            var env = new Dictionary<string, string>
            {
                ["HALOW_EGRESS"] = "127.0.0.1", ["HALOW_GROUP"] = Net.NewGroup(), ["HALOW_PORT"] = Net.FreeUdpPort().ToString(),
                ["HALOW_RESPONSE_PORT"] = Net.FreeTcpPort().ToString(), ["HALOW_ACK_WAIT"] = "1",
            };
            env[name] = value;
            ExampleRun run = await Example.RunAsync(env, "--direct");
            Assert.True(run.ExitCode == 2, run.ToString());
            if (name.EndsWith("PORT")) Assert.Contains(name, run.Stderr);
        }
    }
}
