// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// Shared helpers for the HalowClient.cs tests: paths, free ports, test multicast groups,
// loopback multicast detection, a stand-in wearable's TCP replies, and protobuf helpers.
//
// Safety rules every test follows: only 127.0.0.1; never the real group 239.255.0.10 or the
// ports 5000, 5001 and 8080; a fresh test group 239.255.<100-254>.<1-254> and free ports.
//
// Environment variables:
//   HALOW_TEST_REQUIRE_MULTICAST=1   fail, instead of skip, when loopback multicast does not work
//   HALOW_TEST_REQUIRE_EXAMPLE=1     fail, instead of skip, when the example is not built
//   HALOW_TEST_SEED=<n>              the seed for the test groups (the fuzz test has its own fixed seed)
//
// On Linux, loopback multicast needs:
//   sudo ip link set lo multicast on && sudo ip route add 239.0.0.0/8 dev lo

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Xunit;

namespace ClearObject.Halow.Tests
{
    /// <summary>
    /// Tests that change process-wide state (environment variables, the default proxy), or that need a
    /// just-stopped listener's port to be free at once, run alone, after the others.
    /// </summary>
    [CollectionDefinition(Name, DisableParallelization = true)]
    public sealed class ProcessWideState
    {
        public const string Name = "process-wide state";
    }

    internal static class TestEnv
    {
        public static bool Flag(string name)
        {
            return (Environment.GetEnvironmentVariable(name) ?? "").Trim() == "1";
        }

        public static readonly int Seed = int.TryParse(Environment.GetEnvironmentVariable("HALOW_TEST_SEED"),
            NumberStyles.Integer, CultureInfo.InvariantCulture, out int s) ? s : 20261122;
    }

    // ------------------------------------------------------------------------
    // Paths
    // ------------------------------------------------------------------------

    internal static class Paths
    {
        /// <summary>integrations/csharp: the folder that holds HalowClient.cs and HalowExample.csproj.</summary>
        public static readonly string CsharpDir = FindCsharpDir();

        public static string VectorsPath => Path.GetFullPath(Path.Combine(CsharpDir, "..", "testdata", "vectors.json"));

        private static string FindCsharpDir()
        {
            for (var d = new DirectoryInfo(AppContext.BaseDirectory); d != null; d = d.Parent)
            {
                if (File.Exists(Path.Combine(d.FullName, "HalowClient.cs")) && File.Exists(Path.Combine(d.FullName, "HalowExample.csproj")))
                    return d.FullName;
            }
            throw new InvalidOperationException("cannot find integrations/csharp above " + AppContext.BaseDirectory);
        }

        /// <summary>The built example (bin/&lt;configuration&gt;/net8.0/HalowExample.dll), the configuration of this test build first.</summary>
        public static string ExampleDll()
        {
            string own = new DirectoryInfo(AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)).Parent?.Name ?? "Release";
            foreach (string config in new[] { own, "Release", "Debug" }.Distinct())
            {
                string dll = Path.Combine(CsharpDir, "bin", config, "net8.0", "HalowExample.dll");
                if (File.Exists(dll)) return dll;
            }
            return null;
        }

        /// <summary>The dotnet host that runs this test process, to start the example with.</summary>
        public static string DotnetHost()
        {
            string exe = OperatingSystem.IsWindows() ? "dotnet.exe" : "dotnet";
            string fromEnv = Environment.GetEnvironmentVariable("DOTNET_HOST_PATH");
            if (!string.IsNullOrEmpty(fromEnv) && File.Exists(fromEnv)) return fromEnv;
            // <dotnet root>/shared/Microsoft.NETCore.App/<version>/
            var runtimeDir = new DirectoryInfo(System.Runtime.InteropServices.RuntimeEnvironment.GetRuntimeDirectory());
            DirectoryInfo root = runtimeDir.Parent?.Parent?.Parent;
            if (root != null && File.Exists(Path.Combine(root.FullName, exe))) return Path.Combine(root.FullName, exe);
            string main = Process.GetCurrentProcess().MainModule?.FileName;
            if (main != null && Path.GetFileName(main).Equals(exe, StringComparison.OrdinalIgnoreCase)) return main;
            return "dotnet";
        }
    }

    // ------------------------------------------------------------------------
    // Ports, groups, polling
    // ------------------------------------------------------------------------

    internal static class Net
    {
        public const string Loopback = "127.0.0.1";
        private static readonly object Gate = new object();
        private static readonly Random Rng = new Random(TestEnv.Seed);
        private static readonly HashSet<string> Groups = new HashSet<string>();
        private static readonly HashSet<int> Ports = new HashSet<int>();
        private static readonly int[] Forbidden = { HalowDefaults.Port, HalowDefaults.ResponsePort, 8080 };

        /// <summary>A test multicast group, 239.255.&lt;100-254&gt;.&lt;1-254&gt;, never the same twice in one run.</summary>
        public static string NewGroup()
        {
            lock (Gate)
            {
                while (true)
                {
                    string g = "239.255." + Rng.Next(100, 255).ToString(CultureInfo.InvariantCulture) + "."
                        + Rng.Next(1, 255).ToString(CultureInfo.InvariantCulture);
                    if (g != HalowDefaults.Group && Groups.Add(g)) return g;
                }
            }
        }

        /// <summary>A TCP port that is free on every address now (HalowResponseListener binds 0.0.0.0).</summary>
        public static int FreeTcpPort()
        {
            while (true)
            {
                var l = new TcpListener(IPAddress.Any, 0);
                l.Start();
                int p = ((IPEndPoint)l.LocalEndpoint).Port;
                l.Stop();
                if (Take(p)) return p;
            }
        }

        /// <summary>A UDP port that is free on every address now.</summary>
        public static int FreeUdpPort()
        {
            while (true)
            {
                int p;
                using (var u = new UdpClient(new IPEndPoint(IPAddress.Any, 0)))
                    p = ((IPEndPoint)u.Client.LocalEndPoint).Port;
                if (Take(p)) return p;
            }
        }

        private static bool Take(int port)
        {
            lock (Gate) return Array.IndexOf(Forbidden, port) < 0 && Ports.Add(port);
        }

        /// <summary>Polls the condition every 10 ms until it holds or the timeout passes.</summary>
        public static async Task<bool> Until(Func<bool> condition, TimeSpan timeout)
        {
            var clock = Stopwatch.StartNew();
            while (true)
            {
                if (condition()) return true;
                if (clock.Elapsed > timeout) return false;
                await Task.Delay(10).ConfigureAwait(false);
            }
        }

        public static long NowMs()
        {
            return DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        }

        /// <summary>Awaits the task, failing the test when it takes longer than the timeout.</summary>
        public static async Task<T> Within<T>(Task<T> task, TimeSpan timeout, string what)
        {
            if (await Task.WhenAny(task, Task.Delay(timeout)).ConfigureAwait(false) != task)
                throw new TimeoutException(what + " did not finish within " + timeout.TotalSeconds + " s");
            return await task.ConfigureAwait(false);
        }

        public static async Task Within(Task task, TimeSpan timeout, string what)
        {
            if (await Task.WhenAny(task, Task.Delay(timeout)).ConfigureAwait(false) != task)
                throw new TimeoutException(what + " did not finish within " + timeout.TotalSeconds + " s");
            await task.ConfigureAwait(false);
        }
    }

    /// <summary>Sets environment variables for one test and puts the old values back afterwards.</summary>
    internal sealed class EnvironmentScope : IDisposable
    {
        private readonly Dictionary<string, string> saved = new Dictionary<string, string>();

        public EnvironmentScope Set(string name, string value)
        {
            if (!saved.ContainsKey(name)) saved[name] = Environment.GetEnvironmentVariable(name);
            Environment.SetEnvironmentVariable(name, value);
            return this;
        }

        public void Dispose()
        {
            foreach (KeyValuePair<string, string> kv in saved) Environment.SetEnvironmentVariable(kv.Key, kv.Value);
        }
    }

    // ------------------------------------------------------------------------
    // Loopback multicast
    // ------------------------------------------------------------------------

    internal static class Multicast
    {
        public const string Hint = " On Linux: sudo ip link set lo multicast on && sudo ip route add 239.0.0.0/8 dev lo.";
        public const string SkipHint = Hint + " Set HALOW_TEST_REQUIRE_MULTICAST=1 to make this a failure instead of a skip.";

        public static bool Required => TestEnv.Flag("HALOW_TEST_REQUIRE_MULTICAST");

        private static readonly Lazy<string> Problem = new Lazy<string>(Probe, LazyThreadSafetyMode.ExecutionAndPublication);

        /// <summary>Null when a datagram sent from 127.0.0.1 to a test group comes back on loopback; otherwise why not.</summary>
        public static string Unavailable => Problem.Value;

        /// <summary>Fails the test with the reason when loopback multicast does not work.</summary>
        public static void Require()
        {
            if (Unavailable != null)
                Assert.Fail("loopback multicast is required (HALOW_TEST_REQUIRE_MULTICAST=1) but does not work: " + Unavailable + "." + Hint);
        }

        private static string Probe()
        {
            try
            {
                using (var rx = new MulticastReceiver(Net.NewGroup()))
                {
                    byte[] probe = Encoding.ASCII.GetBytes("probe-" + Guid.NewGuid().ToString("N"));
                    rx.SendFromLoopback(probe);
                    UdpReceiveResult? got = rx.ReceiveAsync(TimeSpan.FromSeconds(2)).GetAwaiter().GetResult();
                    if (got == null) return "a datagram sent to " + rx.Group + ":" + rx.Port + " from 127.0.0.1 did not come back within 2 s";
                    return got.Value.Buffer.SequenceEqual(probe) ? null : "an unexpected datagram arrived on the test port";
                }
            }
            catch (Exception e)
            {
                return e.GetType().Name + ": " + e.Message;
            }
        }
    }

    /// <summary>A test that needs loopback multicast: skipped without it, unless HALOW_TEST_REQUIRE_MULTICAST=1.</summary>
    public sealed class MulticastFactAttribute : FactAttribute
    {
        public MulticastFactAttribute()
        {
            if (!Multicast.Required && Multicast.Unavailable != null)
                Skip = "loopback multicast is not available: " + Multicast.Unavailable + "." + Multicast.SkipHint;
        }
    }

    /// <summary>A test of POSIX socket behaviour, skipped on Windows with the reason given.</summary>
    public sealed class NotOnWindowsFactAttribute : FactAttribute
    {
        public NotOnWindowsFactAttribute(string reason)
        {
            if (OperatingSystem.IsWindows()) Skip = reason;
        }
    }

    /// <summary>
    /// A UDP socket joined to a test group on 127.0.0.1: it receives what HalowDirect multicasts,
    /// as a wearable would.
    /// </summary>
    internal sealed class MulticastReceiver : IDisposable
    {
        private readonly UdpClient udp;

        /// <summary>Joins the group on a new free port (Port), which it holds until disposed.</summary>
        public MulticastReceiver(string group)
        {
            Group = group;
            udp = new UdpClient(AddressFamily.InterNetwork);
            try
            {
                // 0.0.0.0, as a wearable binds: Linux delivers multicast only to sockets bound to
                // the wildcard or the group, and Windows cannot bind a group address.
                udp.Client.Bind(new IPEndPoint(IPAddress.Any, 0));
                Port = ((IPEndPoint)udp.Client.LocalEndPoint).Port;
                Assert.True(Port != HalowDefaults.Port && Port != HalowDefaults.ResponsePort && Port != 8080, "unlucky port " + Port);
                udp.JoinMulticastGroup(IPAddress.Parse(group), IPAddress.Loopback);
            }
            catch
            {
                udp.Dispose();
                throw;
            }
        }

        public string Group { get; }
        public int Port { get; }

        public async Task<UdpReceiveResult?> ReceiveAsync(TimeSpan timeout)
        {
            using (var cts = new CancellationTokenSource(timeout))
            {
                try
                {
                    return await udp.ReceiveAsync(cts.Token).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    return null;
                }
            }
        }

        /// <summary>Sends a datagram to the group from 127.0.0.1, as HalowDirect does.</summary>
        public void SendFromLoopback(byte[] payload)
        {
            using (var tx = new UdpClient(new IPEndPoint(IPAddress.Loopback, 0)))
            {
                tx.Client.SetSocketOption(SocketOptionLevel.IP, SocketOptionName.MulticastInterface, IPAddress.Loopback.GetAddressBytes());
                tx.Client.SetSocketOption(SocketOptionLevel.IP, SocketOptionName.MulticastTimeToLive, 1);
                tx.Send(payload, payload.Length, new IPEndPoint(IPAddress.Parse(Group), Port));
            }
        }

        /// <summary>
        /// Every datagram sent to the group so far. It sends a marker datagram and collects what
        /// arrives up to it: loopback delivers in order, so anything sent before the marker
        /// arrives before it. What comes within a short grace period after the marker is
        /// included too, in case a system delivers slightly out of order.
        /// </summary>
        public async Task<List<UdpReceiveResult>> DrainAsync()
        {
            byte[] marker = Encoding.ASCII.GetBytes("marker-" + Guid.NewGuid().ToString("N"));
            SendFromLoopback(marker);
            var got = new List<UdpReceiveResult>();
            while (true)
            {
                UdpReceiveResult? r = await ReceiveAsync(TimeSpan.FromSeconds(5)).ConfigureAwait(false);
                Assert.True(r != null, "the marker datagram did not arrive within 5 s");
                if (r.Value.Buffer.SequenceEqual(marker)) break;
                got.Add(r.Value);
            }
            while (true)
            {
                UdpReceiveResult? late = await ReceiveAsync(TimeSpan.FromMilliseconds(100)).ConfigureAwait(false);
                if (late == null) return got;
                got.Add(late.Value);
            }
        }

        /// <summary>Proves that nothing was sent to the group so far (see DrainAsync).</summary>
        public async Task AssertNothingReceivedAsync()
        {
            List<UdpReceiveResult> got = await DrainAsync().ConfigureAwait(false);
            Assert.True(got.Count == 0, got.Count + " datagram(s) arrived, the first " + (got.Count > 0 ? Hex.Of(got[0].Buffer) : ""));
        }

        public void Dispose()
        {
            udp.Dispose();
        }
    }

    // ------------------------------------------------------------------------
    // A stand-in wearable's replies (TCP)
    // ------------------------------------------------------------------------

    internal static class Wearable
    {
        /// <summary>
        /// Connects to 127.0.0.1:port, writes the bytes and closes, as a wearable does, then waits
        /// until the listener has closed its end. A listener may refuse or cut off a connection
        /// with a reset; that is not an error here (the tests check what the listener recorded).
        /// </summary>
        public static async Task SendAsync(int port, byte[] bytes)
        {
            using (var c = new TcpClient(AddressFamily.InterNetwork))
            {
                await c.ConnectAsync(IPAddress.Loopback, port).ConfigureAwait(false);
                NetworkStream s = c.GetStream();      // before Shutdown, which marks the TcpClient as disconnected
                try
                {
                    if (bytes.Length > 0) await s.WriteAsync(bytes, 0, bytes.Length).ConfigureAwait(false);
                    c.Client.Shutdown(SocketShutdown.Send);
                }
                catch (IOException)
                {
                    return;
                }
                catch (SocketException)
                {
                    return;
                }
                Assert.True(await WaitClosedAsync(s, TimeSpan.FromSeconds(10)).ConfigureAwait(false), "the listener kept the connection open");
            }
        }

        /// <summary>Reads until the peer closes (0 bytes or a reset); false when it is still open after the timeout.</summary>
        public static Task<bool> WaitClosedAsync(TcpClient c, TimeSpan timeout)
        {
            return WaitClosedAsync(c.GetStream(), timeout);
        }

        public static async Task<bool> WaitClosedAsync(NetworkStream s, TimeSpan timeout)
        {
            var buf = new byte[256];
            using (var cts = new CancellationTokenSource(timeout))
            {
                try
                {
                    while (await s.ReadAsync(buf.AsMemory(), cts.Token).ConfigureAwait(false) > 0)
                    {
                    }
                    return true;
                }
                catch (OperationCanceledException)
                {
                    return false;
                }
                catch (IOException)
                {
                    return true;
                }
                catch (SocketException)
                {
                    return true;
                }
            }
        }

        /// <summary>DeviceResponse{timestamp, code = ACK, ack_payload = {success}}.</summary>
        public static byte[] Ack(ulong timestampMs, bool success = true)
        {
            return new Proto().Varint(1, timestampMs).Varint(2, 1).Bytes(3, success ? new byte[] { 0x08, 0x01 } : new byte[0]).ToArray();
        }

        /// <summary>DeviceResponse{timestamp, code = HEARTBEAT, heartbeat_payload}, as a wearable encodes it.</summary>
        public static byte[] Heartbeat(ulong timestampMs, string deviceId, int battery = 87, int rssi = -62, uint uptime = 3600,
            uint appState = 1, uint deviceState = 2)
        {
            byte[] hb = new Proto().Bytes(1, Encoding.UTF8.GetBytes(deviceId)).Int32(2, battery).Int32(3, rssi)
                .Varint(4, uptime).Varint(5, appState).Varint(6, deviceState).ToArray();
            return new Proto().Varint(1, timestampMs).Varint(2, 2).Bytes(4, hb).ToArray();
        }
    }

    // ------------------------------------------------------------------------
    // Protobuf and hex helpers (written independently of HalowClient.cs)
    // ------------------------------------------------------------------------

    /// <summary>A minimal protobuf writer for test messages. Zero varints are written too, unlike proto3.</summary>
    internal sealed class Proto
    {
        private readonly List<byte> buf = new List<byte>();

        public static byte[] VarintBytes(ulong v)
        {
            var b = new List<byte>();
            do
            {
                byte x = (byte)(v & 0x7F);
                v >>= 7;
                b.Add(v != 0 ? (byte)(x | 0x80) : x);
            }
            while (v != 0);
            return b.ToArray();
        }

        public Proto Key(ulong tag, int wire)
        {
            buf.AddRange(VarintBytes((tag << 3) | (uint)wire));
            return this;
        }

        public Proto Varint(ulong tag, ulong v)
        {
            Key(tag, 0);
            buf.AddRange(VarintBytes(v));
            return this;
        }

        /// <summary>An int32 field: negative values are sign-extended to 10 bytes, as proto3 requires.</summary>
        public Proto Int32(ulong tag, int v)
        {
            return Varint(tag, unchecked((ulong)(long)v));
        }

        public Proto Bytes(ulong tag, byte[] payload)
        {
            Key(tag, 2);
            buf.AddRange(VarintBytes((ulong)payload.Length));
            buf.AddRange(payload);
            return this;
        }

        public Proto Fixed64(ulong tag, ulong v)
        {
            Key(tag, 1);
            buf.AddRange(BitConverter.GetBytes(v));
            return this;
        }

        public Proto Fixed32(ulong tag, uint v)
        {
            Key(tag, 5);
            buf.AddRange(BitConverter.GetBytes(v));
            return this;
        }

        public Proto Raw(params byte[] bytes)
        {
            buf.AddRange(bytes);
            return this;
        }

        public byte[] ToArray()
        {
            return buf.ToArray();
        }
    }

    internal static class Hex
    {
        public static string Of(byte[] b)
        {
            var sb = new StringBuilder(b.Length * 2);
            foreach (byte x in b) sb.Append(x.ToString("x2", CultureInfo.InvariantCulture));
            return sb.ToString();
        }

        public static byte[] Parse(string hex)
        {
            hex = hex.Replace(" ", "");
            var b = new byte[hex.Length / 2];
            for (int i = 0; i < b.Length; i++)
                b[i] = byte.Parse(hex.Substring(2 * i, 2), NumberStyles.HexNumber, CultureInfo.InvariantCulture);
            return b;
        }
    }

    /// <summary>A decoded reply as one line, the format Example.cs's self-test uses; "null" for a rejected message.</summary>
    internal static class Canonical
    {
        public static string Of(DeviceResponse r)
        {
            if (r == null) return "null";
            var sb = new StringBuilder();
            sb.Append("ts=").Append(r.TimestampMs.ToString(CultureInfo.InvariantCulture)).Append(" code=").Append(r.Code);
            if (r.AckSuccess.HasValue) sb.Append(" ack=").Append(r.AckSuccess.Value ? "true" : "false");
            if (r.Heartbeat != null)
            {
                WearableHeartbeat h = r.Heartbeat;
                sb.Append(" hb=").Append(h.DeviceId)
                  .Append('|').Append(h.BatteryPercentage.ToString(CultureInfo.InvariantCulture))
                  .Append('|').Append(h.Rssi.ToString(CultureInfo.InvariantCulture))
                  .Append('|').Append(h.UptimeSeconds.ToString(CultureInfo.InvariantCulture))
                  .Append('|').Append(h.AppState.ToString(CultureInfo.InvariantCulture))
                  .Append('|').Append(h.DeviceState.ToString(CultureInfo.InvariantCulture));
            }
            return sb.ToString();
        }

        /// <summary>Decodes, turning a FormatException into "null". Any other exception escapes.</summary>
        public static string Decode(byte[] bytes)
        {
            try
            {
                return Of(HalowWire.DecodeDeviceResponse(bytes));
            }
            catch (FormatException)
            {
                return "null";
            }
        }
    }
}
