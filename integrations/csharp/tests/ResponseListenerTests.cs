// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// Direct mode, receiving: HalowResponseListener on a free TCP port, with the test playing the
// wearables (and misbehaving peers) over 127.0.0.1.

using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Tasks;
using Xunit;

namespace ClearObject.Halow.Tests
{
    internal static class Listeners
    {
        /// <summary>A started listener on a free port (retried if another test takes the port first).</summary>
        public static HalowResponseListener Start(out int port, string allowPrefix = "")
        {
            for (int attempt = 0; ; attempt++)
            {
                port = Net.FreeTcpPort();
                var l = new HalowResponseListener(port, allowPrefix);
                try
                {
                    return l.Start();
                }
                catch (InvalidOperationException) when (attempt < 5)
                {
                    l.Dispose();
                }
            }
        }

        public static async Task<bool> Count(HalowResponseListener l, int n, double seconds = 10)
        {
            return await Net.Until(() => l.Responses.Count >= n, TimeSpan.FromSeconds(seconds));
        }

        /// <summary>Opens a connection and returns it without sending anything.</summary>
        public static async Task<TcpClient> Open(int port)
        {
            var c = new TcpClient(AddressFamily.InterNetwork);
            await c.ConnectAsync(IPAddress.Loopback, port);
            return c;
        }
    }

    public sealed class ResponseListenerTests
    {
        [Fact]
        public async Task Ack_is_decoded_and_recorded()
        {
            var seen = new ConcurrentQueue<DeviceResponse>();
            using (HalowResponseListener l = Listeners.Start(out int port, "127.0.0."))
            {
                l.Received += seen.Enqueue;
                long before = Net.NowMs();
                await Wearable.SendAsync(port, Wearable.Ack(1767225600163));
                Assert.True(await Listeners.Count(l, 1));
                DeviceResponse r = l.Responses.Single();
                Assert.Equal("ACK", r.Code);
                Assert.Equal(1767225600163UL, r.TimestampMs);
                Assert.True(r.AckSuccess);
                Assert.Null(r.Heartbeat);
                Assert.Equal("127.0.0.1", r.Ip);
                Assert.InRange(r.ReceivedMs, before, Net.NowMs());
                Assert.True(await Net.Until(() => seen.Count == 1, TimeSpan.FromSeconds(5)));
                Assert.Same(r, seen.Single());
            }
        }

        [Fact]
        public async Task Heartbeat_is_decoded_with_negative_rssi()
        {
            using (HalowResponseListener l = Listeners.Start(out int port))
            {
                await Wearable.SendAsync(port, Wearable.Heartbeat(1767225600123, "wz-07", battery: 87, rssi: -62, uptime: 3600));
                Assert.True(await Listeners.Count(l, 1));
                DeviceResponse r = l.Responses.Single();
                Assert.Equal("HEARTBEAT", r.Code);
                Assert.Equal(1767225600123UL, r.TimestampMs);
                Assert.Equal("wz-07", r.Heartbeat.DeviceId);
                Assert.Equal(87, r.Heartbeat.BatteryPercentage);
                Assert.Equal(-62, r.Heartbeat.Rssi);
                Assert.Equal(3600u, r.Heartbeat.UptimeSeconds);
            }
        }

        [Fact]
        public async Task A_reply_written_in_slow_pieces_is_put_together()
        {
            byte[] hb = Wearable.Heartbeat(7, "slow-one");
            using (HalowResponseListener l = Listeners.Start(out int port))
            using (TcpClient c = await Listeners.Open(port))
            {
                NetworkStream s = c.GetStream();
                foreach (byte b in hb)
                {
                    await s.WriteAsync(new[] { b }, 0, 1);
                    await s.FlushAsync();
                    await Task.Delay(15);
                }
                c.Client.Shutdown(SocketShutdown.Send);
                Assert.True(await Listeners.Count(l, 1));
                Assert.Equal("slow-one", l.Responses.Single().Heartbeat.DeviceId);
            }
        }

        [Fact]
        public async Task Only_ack_and_heartbeat_are_kept()
        {
            using (HalowResponseListener l = Listeners.Start(out int port))
            {
                await Wearable.SendAsync(port, new Proto().Varint(1, 5).ToArray());                 // code UNKNOWN
                await Wearable.SendAsync(port, new Proto().Varint(1, 5).Varint(2, 7).ToArray());    // code 7
                await Wearable.SendAsync(port, new Proto().Varint(2, 3).ToArray());                 // code 3
                await Wearable.SendAsync(port, Wearable.Ack(42));
                Assert.True(await Listeners.Count(l, 1));
                Assert.Equal(42UL, l.Responses.Single().TimestampMs);
            }
        }

        [Fact]
        public async Task Garbage_empty_and_oversized_connections_do_not_break_it()
        {
            var rng = new Random(99);
            using (HalowResponseListener l = Listeners.Start(out int port))
            {
                await Wearable.SendAsync(port, new byte[0]);                                         // connect and close
                await Wearable.SendAsync(port, new byte[] { 0x0b, 0x0c });                           // a group
                await Wearable.SendAsync(port, new byte[] { 0x08, 0xff });                           // cut-off varint
                await Wearable.SendAsync(port, Enumerable.Range(0, 64).Select(i => (byte)rng.Next(256)).ToArray());
                await Wearable.SendAsync(port, System.Text.Encoding.ASCII.GetBytes("GET / HTTP/1.1\r\nHost: x\r\n\r\n"));

                // More than 8 KiB: the listener reads 8 KiB, so the long field is cut off and the reply dropped.
                byte[] big = new Proto().Varint(2, 1).Bytes(15, new byte[9000]).ToArray();
                await Wearable.SendAsync(port, big);
                // Exactly 8 KiB that ends in a complete message is still read and decoded.
                byte[] ack = Wearable.Ack(77);
                int fill = 8192 - ack.Length;
                byte[] padding = new byte[0];
                for (int len = fill - 2; padding.Length != fill; len--) padding = new Proto().Bytes(15, new byte[len]).ToArray();
                byte[] exact = padding.Concat(ack).ToArray();
                Assert.Equal(8192, exact.Length);
                await Wearable.SendAsync(port, exact);

                await Wearable.SendAsync(port, Wearable.Ack(1));
                Assert.True(await Listeners.Count(l, 2));
                Assert.Equal(new ulong[] { 77, 1 }, l.Responses.Select(r => r.TimestampMs).OrderByDescending(x => x));
            }
        }

        [Fact]
        public async Task A_reset_connection_does_not_break_it()
        {
            using (HalowResponseListener l = Listeners.Start(out int port))
            {
                for (int i = 0; i < 5; i++)
                {
                    using (TcpClient c = await Listeners.Open(port))
                    {
                        await c.GetStream().WriteAsync(new byte[] { 0x08, 0x85 }, 0, 2);
                        c.Client.LingerState = new LingerOption(true, 0);       // close with a reset
                    }
                }
                await Wearable.SendAsync(port, Wearable.Ack(3));
                Assert.True(await Net.Until(() => l.Responses.Any(r => r.TimestampMs == 3), TimeSpan.FromSeconds(10)));
            }
        }

        [Fact]
        public async Task A_throwing_callback_does_not_stop_the_listener_or_other_callbacks()
        {
            int good = 0;
            using (HalowResponseListener l = Listeners.Start(out int port))
            {
                l.Received += r => throw new InvalidOperationException("subscriber failure");
                l.Received += r => Interlocked.Increment(ref good);
                for (ulong i = 1; i <= 3; i++) await Wearable.SendAsync(port, Wearable.Ack(i));
                Assert.True(await Listeners.Count(l, 3));
                // Before the fix the second subscriber never ran: the first one's exception ended the invocation.
                Assert.True(await Net.Until(() => Volatile.Read(ref good) == 3, TimeSpan.FromSeconds(5)), "good subscriber saw " + good);
            }
        }

        [Fact]
        public async Task Concurrent_replies_are_all_recorded()
        {
            var seen = new ConcurrentBag<ulong>();
            using (HalowResponseListener l = Listeners.Start(out int port))
            {
                l.Received += r => seen.Add(r.TimestampMs);
                await Task.WhenAll(Enumerable.Range(1, 24).Select(i => Wearable.SendAsync(port, Wearable.Ack((ulong)i))));
                Assert.True(await Listeners.Count(l, 24));
                Assert.Equal(Enumerable.Range(1, 24).Select(i => (ulong)i), l.Responses.Select(r => r.TimestampMs).OrderBy(x => x));
                Assert.True(await Net.Until(() => seen.Count == 24, TimeSpan.FromSeconds(5)));
            }
        }

        [Fact]
        public async Task Responses_keeps_the_last_1000()
        {
            int total = 0;
            using (HalowResponseListener l = Listeners.Start(out int port))
            {
                l.Received += r => Interlocked.Increment(ref total);
                // The first five one at a time, so they are certainly the oldest.
                for (ulong i = 1; i <= 5; i++)
                {
                    await Wearable.SendAsync(port, Wearable.Ack(i));
                    Assert.True(await Net.Until(() => Volatile.Read(ref total) == (int)i, TimeSpan.FromSeconds(10)));
                }
                for (ulong from = 6; from <= 1005; from += 20)
                {
                    int n = (int)Math.Min(20, 1006 - from);
                    await Task.WhenAll(Enumerable.Range(0, n).Select(k => Wearable.SendAsync(port, Wearable.Ack(from + (ulong)k))));
                    int want = (int)from - 1 + n;
                    Assert.True(await Net.Until(() => Volatile.Read(ref total) == want, TimeSpan.FromSeconds(10)), "stuck at " + total + " of " + want);
                }
                IList<DeviceResponse> all = l.Responses;
                Assert.Equal(1000, all.Count);
                Assert.Equal(Enumerable.Range(6, 1000).Select(i => (ulong)i), all.Select(r => r.TimestampMs).OrderBy(x => x));
            }
        }

        [Fact]
        public async Task Responses_is_a_snapshot()
        {
            using (HalowResponseListener l = Listeners.Start(out int port))
            {
                IList<DeviceResponse> before = l.Responses;
                await Wearable.SendAsync(port, Wearable.Ack(1));
                Assert.True(await Listeners.Count(l, 1));
                Assert.Empty(before);
                Assert.Single(l.Responses);
            }
        }
    }

    public sealed class ResponseListenerPeerTests
    {
        [Fact]
        public async Task Peers_outside_the_allowed_prefix_are_refused()
        {
            using (HalowResponseListener l = Listeners.Start(out int port, "192.0.2."))
            {
                using (TcpClient c = await Listeners.Open(port))
                {
                    // Refused at accept: the listener closes the connection without reading.
                    Assert.True(await Wearable.WaitClosedAsync(c, TimeSpan.FromSeconds(10)), "the refused connection was not closed");
                }
                await Wearable.SendAsync(port, Wearable.Ack(1));
                await Wearable.SendAsync(port, Wearable.Heartbeat(1, "outsider"));
                Assert.Empty(l.Responses);
            }
        }

        [Theory]
        [InlineData("")]
        [InlineData(null)]
        [InlineData("127.")]
        [InlineData("127.0.0.1")]
        public async Task Allowed_prefixes(string prefix)
        {
            using (HalowResponseListener l = Listeners.Start(out int port, prefix))
            {
                await Wearable.SendAsync(port, Wearable.Ack(1));
                Assert.True(await Listeners.Count(l, 1));
            }
        }

        [Fact]
        public async Task A_listener_built_with_SubnetPrefix_accepts_its_own_subnet()
        {
            using (HalowResponseListener l = Listeners.Start(out int port, HalowDirect.SubnetPrefix(Net.Loopback)))
            {
                await Wearable.SendAsync(port, Wearable.Ack(1));
                Assert.True(await Listeners.Count(l, 1));
            }
            using (HalowResponseListener l = Listeners.Start(out int port, HalowDirect.SubnetPrefix("no-dots")))
            {
                await Wearable.SendAsync(port, Wearable.Ack(1));
                await Wearable.SendAsync(port, Wearable.Ack(2));
                Assert.Empty(l.Responses);
            }
        }

        [Fact]
        public async Task At_most_32_connections_at_once()
        {
            using (HalowResponseListener l = Listeners.Start(out int port))
            {
                var idle = new List<TcpClient>();
                try
                {
                    for (int i = 0; i < 32; i++) idle.Add(await Listeners.Open(port));
                    // The 33rd is accepted after the first 32 and closed at once, unread.
                    using (TcpClient extra = await Listeners.Open(port))
                    {
                        // A connection that got a slot would be kept open for 5 s.
                        Assert.True(await Wearable.WaitClosedAsync(extra, TimeSpan.FromSeconds(4)), "the 33rd connection was not refused");
                    }
                }
                finally
                {
                    foreach (TcpClient c in idle) c.Dispose();
                }
                // The slots come back once the idle connections close.
                await Wearable.SendAsync(port, Wearable.Ack(1));
                Assert.True(await Listeners.Count(l, 1));
                Assert.Equal(1UL, l.Responses.Single().TimestampMs);
            }
        }
    }

    public sealed class ResponseListenerDeadlineTests
    {
        [Fact]
        public async Task A_silent_connection_is_closed_after_5_s_and_what_arrived_is_decoded()
        {
            using (HalowResponseListener l = Listeners.Start(out int port))
            using (TcpClient c = await Listeners.Open(port))
            {
                // A complete ACK, but the peer never closes: the listener stops reading at 5 s.
                byte[] ack = Wearable.Ack(55);
                await c.GetStream().WriteAsync(ack, 0, ack.Length);
                var clock = Stopwatch.StartNew();

                // Meanwhile other replies are handled at once.
                await Wearable.SendAsync(port, Wearable.Ack(1));
                Assert.True(await Listeners.Count(l, 1));
                Assert.True(clock.Elapsed < TimeSpan.FromSeconds(3));

                Assert.True(await Wearable.WaitClosedAsync(c, TimeSpan.FromSeconds(10)), "the silent connection was not closed");
                Assert.InRange(clock.Elapsed, TimeSpan.FromSeconds(3.5), TimeSpan.FromSeconds(8));
                Assert.True(await Listeners.Count(l, 2));
                Assert.Contains(l.Responses, r => r.TimestampMs == 55);
            }
        }
    }

    // These tests need a stopped listener's port to be free at once. On Linux and macOS a child
    // process that another test is starting (the example tests) holds a copy of every open socket
    // until it runs its program, which can keep a just-stopped listener's port in use for a
    // moment; so they run alone, after the parallel tests.
    [Collection(ProcessWideState.Name)]
    public sealed class ResponseListenerLifecycleTests
    {
        [Fact]
        public async Task Stop_releases_the_port()
        {
            HalowResponseListener l = Listeners.Start(out int port);
            await Wearable.SendAsync(port, Wearable.Ack(1));
            Assert.True(await Listeners.Count(l, 1));
            l.Stop();
            l.Stop();
            l.Dispose();

            await Assert.ThrowsAnyAsync<SocketException>(() => Listeners.Open(port));
            using (var again = new HalowResponseListener(port).Start())
            {
                await Wearable.SendAsync(port, Wearable.Ack(2));
                Assert.True(await Listeners.Count(again, 1));
            }
            Assert.Single(l.Responses);         // the stopped listener keeps what it had
        }

        [NotOnWindowsFact("on Windows the listener owns its port exclusively (SO_EXCLUSIVEADDRUSE), which can hold it until TIME_WAIT ends; the Python client does the same")]
        public async Task Dispose_releases_the_port_after_a_server_side_close()
        {
            // The listener closes first after a deadline or a refusal, which leaves the port in
            // TIME_WAIT; on Linux and macOS a new listener must still be able to take it. .NET may
            // close the native socket a moment after Dispose (its pending accept is cancelled
            // first), so allow a few seconds rather than requiring the very next instant.
            int port;
            using (HalowResponseListener l = Listeners.Start(out port, "192.0.2."))
            {
                for (int i = 0; i < 3; i++)
                    using (TcpClient c = await Listeners.Open(port))
                        Assert.True(await Wearable.WaitClosedAsync(c, TimeSpan.FromSeconds(10)));
            }
            HalowResponseListener reopened = null;
            var waited = System.Diagnostics.Stopwatch.StartNew();
            while (reopened == null)
            {
                try
                {
                    reopened = new HalowResponseListener(port).Start();
                }
                catch (InvalidOperationException) when (waited.Elapsed < TimeSpan.FromSeconds(3))
                {
                    await Task.Delay(50);
                }
            }
            using (var again = reopened)
            {
                await Wearable.SendAsync(port, Wearable.Ack(9));
                Assert.True(await Listeners.Count(again, 1));
            }
        }

        [Fact]
        public async Task Start_after_Stop_listens_again()
        {
            HalowResponseListener l = Listeners.Start(out int port);
            try
            {
                l.Stop();
                Assert.Same(l, l.Start());
                await Wearable.SendAsync(port, Wearable.Ack(4));
                // Before the fix the restarted listener held the port but never accepted.
                Assert.True(await Listeners.Count(l, 1, 5), "no reply after a restart");
            }
            finally
            {
                l.Dispose();
            }
        }

        [Fact]
        public async Task Start_while_listening_does_nothing()
        {
            // Before the fix a second Start() threw on Windows (the port option there can only be set
            // while stopped) and started a second accept loop on Linux and macOS.
            HalowResponseListener l = Listeners.Start(out int port);
            try
            {
                Assert.Same(l, l.Start());
                Assert.Same(l, l.Start());
                await Wearable.SendAsync(port, Wearable.Ack(1));
                Assert.True(await Listeners.Count(l, 1));

                l.Stop();                       // one Stop() ends it
                await Assert.ThrowsAnyAsync<SocketException>(() => Listeners.Open(port));
                Assert.Same(l, l.Start());      // and it can start again
                Assert.Same(l, l.Start());
                await Wearable.SendAsync(port, Wearable.Ack(2));
                Assert.True(await Listeners.Count(l, 2, 5), "no reply after a restart");
                Assert.Equal(new ulong[] { 1, 2 }, l.Responses.Select(r => r.TimestampMs));
            }
            finally
            {
                l.Dispose();
            }
        }

        [Fact]
        public void A_taken_port_is_reported()
        {
            using (HalowResponseListener first = Listeners.Start(out int port))
            {
                var second = new HalowResponseListener(port);
                InvalidOperationException e = Assert.Throws<InvalidOperationException>(() => second.Start());
                Assert.StartsWith("cannot listen on TCP " + port + " for wearable replies: ", e.Message);
                Assert.IsAssignableFrom<SocketException>(e.InnerException);
                second.Dispose();
            }
        }

        [Fact]
        public void Stop_and_Dispose_before_Start_are_harmless()
        {
            var l = new HalowResponseListener(Net.FreeTcpPort());
            l.Stop();
            l.Dispose();
            Assert.Empty(l.Responses);
        }

        [Fact]
        public void Bad_port_is_refused()
        {
            Assert.Throws<ArgumentOutOfRangeException>(() => new HalowResponseListener(70000));
            Assert.Throws<ArgumentOutOfRangeException>(() => new HalowResponseListener(-1));
        }
    }

    public sealed class ResponseListenerWaitTests
    {
        [Fact]
        public async Task WaitFor_returns_null_after_the_timeout()
        {
            using (HalowResponseListener l = Listeners.Start(out _))
            {
                var clock = Stopwatch.StartNew();
                Assert.Null(await l.WaitForAsync("ACK", TimeSpan.FromMilliseconds(200)));
                Assert.InRange(clock.Elapsed, TimeSpan.FromMilliseconds(150), TimeSpan.FromSeconds(3));
                clock.Restart();
                Assert.Null(await l.WaitForAsync("ACK", TimeSpan.Zero));
                Assert.Null(await l.WaitForAsync("ACK", TimeSpan.FromSeconds(-1)));
                Assert.True(clock.Elapsed < TimeSpan.FromSeconds(1));
            }
        }

        [Fact]
        public async Task WaitFor_returns_a_reply_that_arrives_while_waiting()
        {
            using (HalowResponseListener l = Listeners.Start(out int port))
            {
                Task<DeviceResponse> wait = l.WaitForAsync("HEARTBEAT", TimeSpan.FromSeconds(10));
                await Wearable.SendAsync(port, Wearable.Ack(1));
                await Wearable.SendAsync(port, Wearable.Heartbeat(2, "late"));
                DeviceResponse hb = await Net.Within(wait, TimeSpan.FromSeconds(5), "WaitForAsync");
                Assert.Equal("late", hb.Heartbeat.DeviceId);
            }
        }

        [Fact]
        public async Task WaitFor_matches_code_and_afterMs()
        {
            using (HalowResponseListener l = Listeners.Start(out int port))
            {
                await Wearable.SendAsync(port, Wearable.Ack(1));
                await Wearable.SendAsync(port, Wearable.Heartbeat(2, "hb"));
                Assert.True(await Listeners.Count(l, 2));
                DeviceResponse ack = await l.WaitForAsync("ACK", TimeSpan.FromSeconds(1));
                Assert.Equal(1UL, ack.TimestampMs);
                Assert.Equal("HEARTBEAT", (await l.WaitForAsync("HEARTBEAT", TimeSpan.FromSeconds(1))).Code);
                Assert.Null(await l.WaitForAsync("ack", TimeSpan.Zero));                 // codes are case-sensitive
                Assert.Null(await l.WaitForAsync(null, TimeSpan.Zero));
                Assert.Same(ack, await l.WaitForAsync("ACK", TimeSpan.Zero, ack.ReceivedMs));
                Assert.Null(await l.WaitForAsync("ACK", TimeSpan.FromMilliseconds(100), ack.ReceivedMs + 1));

                // Waiting for an ACK after an alert: the old ACK does not count, a new one does.
                long alertMs = Net.NowMs() + 1;
                Task<DeviceResponse> wait = l.WaitForAsync("ACK", TimeSpan.FromSeconds(10), alertMs);
                Assert.True(await Net.Until(() => Net.NowMs() > alertMs, TimeSpan.FromSeconds(2)));
                await Wearable.SendAsync(port, Wearable.Ack(3));
                Assert.Equal(3UL, (await Net.Within(wait, TimeSpan.FromSeconds(5), "WaitForAsync")).TimestampMs);
            }
        }

        [Fact]
        public async Task WaitFor_with_an_infinite_timeout_waits()
        {
            // Before the fix Timeout.InfiniteTimeSpan (-1 ms) returned null at once.
            using (HalowResponseListener l = Listeners.Start(out int port))
            using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(20)))
            {
                Task<DeviceResponse> wait = l.WaitForAsync("ACK", Timeout.InfiniteTimeSpan, 0, cts.Token);
                Assert.False(wait.IsCompleted, "WaitForAsync returned before any reply");
                await Wearable.SendAsync(port, Wearable.Ack(8));
                Assert.Equal(8UL, (await Net.Within(wait, TimeSpan.FromSeconds(5), "WaitForAsync")).TimestampMs);
            }
        }

        [Fact]
        public async Task WaitFor_can_be_cancelled()
        {
            using (HalowResponseListener l = Listeners.Start(out _))
            using (var cts = new CancellationTokenSource(TimeSpan.FromMilliseconds(150)))
            {
                var clock = Stopwatch.StartNew();
                Exception e = await Record.ExceptionAsync(() => l.WaitForAsync("ACK", TimeSpan.FromSeconds(30), 0, cts.Token));
                Assert.IsAssignableFrom<OperationCanceledException>(e);
                Assert.True(clock.Elapsed < TimeSpan.FromSeconds(5));
            }
        }
    }
}
