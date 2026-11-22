// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// Direct mode, sending: HalowDirect.SendAlertAsync and SendHeartbeatRequest from 127.0.0.1 to
// a test group, received here by a socket joined to that group, as a wearable would be.
// Also the argument checks, which must refuse bad input before anything is sent.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Net.Sockets;
using System.Threading.Tasks;
using Xunit;

namespace ClearObject.Halow.Tests
{
    internal static class GatewayMessage
    {
        /// <summary>(timestamp, command) of a GatewayMessage, read independently of HalowWire.</summary>
        public static (ulong Timestamp, ulong Command) Parse(byte[] b)
        {
            ulong ts = 0, cmd = 0;
            int i = 0;
            while (i < b.Length)
            {
                byte key = b[i++];
                ulong v = 0;
                int shift = 0;
                while (true)
                {
                    byte x = b[i++];
                    v |= (ulong)(x & 0x7F) << shift;
                    if ((x & 0x80) == 0) break;
                    shift += 7;
                }
                if (key == 0x08) ts = v;
                else if (key == 0x10) cmd = v;
                else throw new FormatException("unexpected key " + key);
            }
            return (ts, cmd);
        }

        public static double Raw8Seconds(byte[] b)
        {
            Assert.Equal(8, b.Length);
            return BitConverter.ToDouble(BitConverter.IsLittleEndian ? b : b.Reverse().ToArray(), 0);
        }
    }

    public sealed class DirectSendTests : IDisposable
    {
        private readonly string group = Net.NewGroup();
        private MulticastReceiver rx;

        public void Dispose()
        {
            rx?.Dispose();
        }

        private int port => rx.Port;

        /// <summary>The wearable's socket, joined before anything is sent.</summary>
        private MulticastReceiver Rx()
        {
            Multicast.Require();
            return rx = new MulticastReceiver(group);
        }

        [MulticastFact]
        public async Task Raw8_alert()
        {
            MulticastReceiver w = Rx();
            long before = Net.NowMs();
            DirectAlertResult res = await HalowDirect.SendAlertAsync(Net.Loopback, "raw8", group, port);
            long after = Net.NowMs();

            List<UdpReceiveResult> got = await w.DrainAsync();
            Assert.Single(got);
            Assert.Equal(Net.Loopback, got[0].RemoteEndPoint.Address.ToString());
            double ms = GatewayMessage.Raw8Seconds(got[0].Buffer) * 1000;
            Assert.InRange(ms, before - 1, after + 1);
            Assert.InRange(ms - res.TimestampMs, -0.01, 1.0);      // one clock reading for both

            Assert.Equal("raw8", res.Format);
            Assert.Equal(group + ":" + port, res.Group);
            Assert.Equal(Net.Loopback, res.Egress);
            Assert.Equal(1, res.Datagrams);
            Assert.InRange(res.TimestampMs, before, after);
            Assert.Equal("raw8 -> " + group + ":" + port + " from 127.0.0.1 (1 datagram(s)), ts_ms " + res.TimestampMs, res.ToString());
        }

        [MulticastFact]
        public async Task Proto_alert()
        {
            MulticastReceiver w = Rx();
            DirectAlertResult res = await HalowDirect.SendAlertAsync(" 127.0.0.1 ", " PROTO ", group, port, ttl: 1);
            List<UdpReceiveResult> got = await w.DrainAsync();
            Assert.Single(got);
            Assert.Equal("proto", res.Format);
            Assert.Equal(Net.Loopback, res.Egress);
            Assert.Equal(((ulong)res.TimestampMs, HalowDefaults.CommandAlert), GatewayMessage.Parse(got[0].Buffer));
            Assert.Equal(HalowWire.EncodeGatewayMessage((ulong)res.TimestampMs, 1), got[0].Buffer);
        }

        [MulticastFact]
        public async Task Both_sends_raw8_then_proto_from_one_clock_reading()
        {
            MulticastReceiver w = Rx();
            DirectAlertResult res = await HalowDirect.SendAlertAsync(Net.Loopback, "both", group, port, ttl: 1);
            List<UdpReceiveResult> got = await w.DrainAsync();
            Assert.Equal(2, got.Count);
            Assert.Equal(2, res.Datagrams);
            double rawMs = GatewayMessage.Raw8Seconds(got[0].Buffer) * 1000;
            (ulong ts, ulong cmd) = GatewayMessage.Parse(got[1].Buffer);
            Assert.Equal(1UL, cmd);
            Assert.Equal((ulong)res.TimestampMs, ts);
            Assert.InRange(rawMs - ts, -0.01, 1.0);
        }

        [MulticastFact]
        public async Task Null_format_means_raw8()
        {
            MulticastReceiver w = Rx();
            DirectAlertResult res = await HalowDirect.SendAlertAsync(Net.Loopback, null, group, port, ttl: 1);
            Assert.Equal("raw8", res.Format);
            Assert.Equal(8, (await w.DrainAsync()).Single().Buffer.Length);
        }

        [MulticastFact]
        public async Task Burst_repeats_identical_bytes()
        {
            MulticastReceiver w = Rx();
            var clock = Stopwatch.StartNew();
            DirectAlertResult res = await HalowDirect.SendAlertAsync(Net.Loopback, "raw8", group, port, ttl: 1, burstSeconds: 0.3, burstIntervalMs: 50);
            TimeSpan took = clock.Elapsed;
            List<UdpReceiveResult> got = await w.DrainAsync();
            Assert.InRange(res.Datagrams, 2, 7);
            Assert.Equal(res.Datagrams, got.Count);
            Assert.All(got, d => Assert.Equal(got[0].Buffer, d.Buffer));
            Assert.InRange(took, TimeSpan.FromMilliseconds(150), TimeSpan.FromSeconds(3));
        }

        [MulticastFact]
        public async Task Burst_of_both_repeats_each_pair()
        {
            MulticastReceiver w = Rx();
            DirectAlertResult res = await HalowDirect.SendAlertAsync(Net.Loopback, "both", group, port, ttl: 1, burstSeconds: 0.2, burstIntervalMs: 40);
            List<UdpReceiveResult> got = await w.DrainAsync();
            Assert.Equal(res.Datagrams, got.Count);
            Assert.True(res.Datagrams >= 4 && res.Datagrams % 2 == 0, "datagrams: " + res.Datagrams);
            for (int i = 0; i < got.Count; i++)
                Assert.Equal(got[i % 2].Buffer, got[i].Buffer);
            Assert.Equal(8, got[0].Buffer.Length);
            Assert.Equal(HalowWire.EncodeGatewayMessage((ulong)res.TimestampMs, 1), got[1].Buffer);
        }

        [MulticastFact]
        public async Task Burst_interval_has_a_20_ms_floor()
        {
            MulticastReceiver w = Rx();
            DirectAlertResult res = await HalowDirect.SendAlertAsync(Net.Loopback, "raw8", group, port, ttl: 1, burstSeconds: 0.1, burstIntervalMs: 0);
            Assert.InRange(res.Datagrams, 2, 6);
            Assert.Equal(res.Datagrams, (await w.DrainAsync()).Count);
        }

        [MulticastFact]
        public async Task Burst_values_that_mean_no_burst()
        {
            MulticastReceiver w = Rx();
            foreach (double burst in new[] { 0.0, -1.0, double.NaN, double.NegativeInfinity })
            {
                DirectAlertResult res = await HalowDirect.SendAlertAsync(Net.Loopback, "raw8", group, port, ttl: 1, burstSeconds: burst);
                Assert.Equal(1, res.Datagrams);
                Assert.Single(await w.DrainAsync());
            }
        }

        [MulticastFact]
        public async Task Burst_is_capped_at_5_seconds()
        {
            MulticastReceiver w = Rx();
            // An infinite burst is 5 s: with a 6 s interval no repeat fits, so it returns at once.
            var clock = Stopwatch.StartNew();
            DirectAlertResult res = await HalowDirect.SendAlertAsync(Net.Loopback, "raw8", group, port, ttl: 1,
                burstSeconds: double.PositiveInfinity, burstIntervalMs: 6000);
            Assert.Equal(1, res.Datagrams);
            Assert.True(clock.Elapsed < TimeSpan.FromSeconds(2), "took " + clock.Elapsed);
            Assert.Single(await w.DrainAsync());

            // 1000 s is 5 s too: one repeat after 2.6 s, then the next one would end past 5 s.
            clock.Restart();
            res = await Net.Within(HalowDirect.SendAlertAsync(Net.Loopback, "raw8", group, port, ttl: 1, burstSeconds: 1000, burstIntervalMs: 2600),
                TimeSpan.FromSeconds(8), "a 1000 s burst");
            Assert.Equal(2, res.Datagrams);
            Assert.InRange(clock.Elapsed, TimeSpan.FromSeconds(2.4), TimeSpan.FromSeconds(6));
            Assert.Equal(2, (await w.DrainAsync()).Count);
        }

        [MulticastFact]
        public async Task Heartbeat_request()
        {
            MulticastReceiver w = Rx();
            long before = Net.NowMs();
            long ts = HalowDirect.SendHeartbeatRequest(Net.Loopback, group, port, ttl: 1);
            long after = Net.NowMs();
            Assert.InRange(ts, before, after);
            List<UdpReceiveResult> got = await w.DrainAsync();
            Assert.Single(got);
            Assert.Equal(((ulong)ts, HalowDefaults.CommandHeartbeatRequest), GatewayMessage.Parse(got[0].Buffer));
            Assert.Equal(Net.Loopback, got[0].RemoteEndPoint.Address.ToString());
        }

        [MulticastFact]
        public async Task Bad_arguments_send_nothing()
        {
            MulticastReceiver w = Rx();
            await Assert.ThrowsAsync<ArgumentException>(() => HalowDirect.SendAlertAsync(Net.Loopback, "mp3", group, port));
            await Assert.ThrowsAsync<ArgumentException>(() => HalowDirect.SendAlertAsync("999.1.1.1", "raw8", group, port));
            await Assert.ThrowsAsync<ArgumentException>(() => HalowDirect.SendAlertAsync("::1", "raw8", group, port));
            await Assert.ThrowsAsync<ArgumentException>(() => HalowDirect.SendAlertAsync("127.1", "raw8", group, port));
            await Assert.ThrowsAsync<ArgumentOutOfRangeException>(() => HalowDirect.SendAlertAsync(Net.Loopback, "raw8", group, 70000));
            Assert.Throws<ArgumentException>(() => HalowDirect.SendHeartbeatRequest("localhost", group, port));
            Assert.Throws<ArgumentOutOfRangeException>(() => HalowDirect.SendHeartbeatRequest(Net.Loopback, group, -1));
            await w.AssertNothingReceivedAsync();
        }
    }

    public sealed class DirectArgumentTests
    {
        // A test group and a free port: even if a check were missing, nothing would reach a wearable.
        private readonly string group = Net.NewGroup();
        private readonly int port = Net.FreeUdpPort();

        [Theory]
        [InlineData("mp3")]
        [InlineData("")]
        [InlineData("raw")]
        [InlineData("raw8,proto")]
        public async Task Bad_format(string format)
        {
            ArgumentException e = await Assert.ThrowsAsync<ArgumentException>(() => HalowDirect.SendAlertAsync(Net.Loopback, format, group, port));
            Assert.Contains("raw8, proto or both", e.Message);
        }

        [Theory]
        [InlineData("999.1.1.1")]
        [InlineData("127.1")]
        [InlineData("1.2.3.4.5")]
        [InlineData("::1")]
        [InlineData("fe80::1")]
        [InlineData("localhost")]
        [InlineData("127.0.0.1:5000")]
        public async Task Egress_must_be_a_dotted_ipv4_address(string egress)
        {
            ArgumentException e = await Assert.ThrowsAsync<ArgumentException>(() => HalowDirect.SendAlertAsync(egress, "raw8", group, port));
            Assert.Contains("IPv4", e.Message);
            e = Assert.Throws<ArgumentException>(() => HalowDirect.SendHeartbeatRequest(egress, group, port));
            Assert.Contains("IPv4", e.Message);
        }

        [Fact]
        public async Task Egress_that_is_not_an_address_of_this_machine()
        {
            // 192.0.2.1 is reserved for documentation (RFC 5737) and is never an address of this
            // machine, so binding to it fails and nothing is sent. TTL 0 keeps it local regardless.
            InvalidOperationException e = await Assert.ThrowsAsync<InvalidOperationException>(
                () => HalowDirect.SendAlertAsync("192.0.2.1", "raw8", group, port, ttl: 0));
            Assert.StartsWith("cannot send from 192.0.2.1: ", e.Message);
            Assert.Contains("Is it an address of this machine", e.Message);
            Assert.IsAssignableFrom<SocketException>(e.InnerException);
            Assert.Throws<InvalidOperationException>(() => HalowDirect.SendHeartbeatRequest("192.0.2.1", group, port, ttl: 0));
        }

        [Fact]
        public async Task Bad_group_or_port()
        {
            await Assert.ThrowsAsync<FormatException>(() => HalowDirect.SendAlertAsync(Net.Loopback, "raw8", "not-a-group", port));
            await Assert.ThrowsAsync<ArgumentNullException>(() => HalowDirect.SendAlertAsync(Net.Loopback, "raw8", null, port));
            await Assert.ThrowsAsync<ArgumentOutOfRangeException>(() => HalowDirect.SendAlertAsync(Net.Loopback, "raw8", group, 65536));
            Assert.Throws<FormatException>(() => HalowDirect.SendHeartbeatRequest(Net.Loopback, "not-a-group", port));
        }

        [Theory]
        [InlineData("192.168.12.50", "192.168.12.")]
        [InlineData("127.0.0.1", "127.0.0.")]
        [InlineData("10.0.0.255", "10.0.0.")]
        [InlineData("1.2.3.", "1.2.3.")]
        public void SubnetPrefix_of_an_address(string address, string prefix)
        {
            Assert.Equal(prefix, HalowDirect.SubnetPrefix(address));
        }

        [Theory]
        [InlineData("abc", "abc.")]
        [InlineData("", ".")]
        [InlineData("::1", "::1.")]
        public void SubnetPrefix_without_a_dot_matches_no_peer(string address, string prefix)
        {
            // Before the fix this returned "", which HalowResponseListener reads as "accept every
            // peer". It now matches the Python reference: a prefix that no IPv4 peer has.
            Assert.Equal(prefix, HalowDirect.SubnetPrefix(address));
        }

        [Fact]
        public void SubnetPrefix_of_null_is_an_argument_error()
        {
            Assert.Throws<ArgumentNullException>(() => HalowDirect.SubnetPrefix(null));
        }
    }

    [Collection(ProcessWideState.Name)]
    public sealed class DirectEnvironmentTests
    {
        [MulticastFact]
        public async Task Egress_comes_from_HALOW_EGRESS_when_not_given()
        {
            Multicast.Require();
            using (var w = new MulticastReceiver(Net.NewGroup()))
            using (new EnvironmentScope().Set("HALOW_EGRESS", " 127.0.0.1 "))
            {
                string group = w.Group;
                int port = w.Port;
                DirectAlertResult res = await HalowDirect.SendAlertAsync(null, "raw8", group, port, ttl: 1);
                Assert.Equal(Net.Loopback, res.Egress);
                res = await HalowDirect.SendAlertAsync("  ", "raw8", group, port, ttl: 1);
                Assert.Equal(Net.Loopback, res.Egress);
                HalowDirect.SendHeartbeatRequest(null, group, port, ttl: 1);
                List<UdpReceiveResult> got = await w.DrainAsync();
                Assert.Equal(3, got.Count);
                Assert.All(got, d => Assert.Equal(Net.Loopback, d.RemoteEndPoint.Address.ToString()));
            }
        }

        [Fact]
        public async Task A_bad_HALOW_EGRESS_is_refused()
        {
            using (new EnvironmentScope().Set("HALOW_EGRESS", "not-an-address"))
            {
                await Assert.ThrowsAsync<ArgumentException>(() => HalowDirect.SendAlertAsync(null, "raw8", Net.NewGroup(), Net.FreeUdpPort()));
                Assert.Throws<ArgumentException>(() => HalowDirect.SendHeartbeatRequest(null, Net.NewGroup(), Net.FreeUdpPort()));
            }
        }
    }
}
