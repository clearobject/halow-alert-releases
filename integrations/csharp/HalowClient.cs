// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// HalowClient.cs - raise ClearZone wearable alerts from .NET (tested on .NET 8; .NET Standard 2.1) or Unity.
//
// One file, no packages. It uses only APIs in .NET Standard 2.1 and C# 8, so it also
// compiles in Unity (2021.2 or newer, "API Compatibility Level: .NET Standard 2.1").
//
// Two ways to raise an alert:
//   Gateway (HTTP)  HalowGatewayClient calls the halow-alert service on the Linux gateway
//                   host that has the HaLowLink attached: POST /alert with the gateway's
//                   bearer token. Works from any machine that can reach the gateway.
//   Direct (UDP)    HalowDirect sends the alert datagram to the wearables yourself (UDP
//                   multicast) from a machine that is itself plugged into the HaLowLink
//                   (address 192.168.12.50); HalowResponseListener collects their replies
//                   on TCP 5001.
//
// An alert makes every wearable on the HaLowLink network vibrate and sound. Warn the
// people nearby first.
//
//   using ClearObject.Halow;
//   var gateway = new HalowGatewayClient("http://<gateway>:8080", token);   // keep one, reuse it
//   string record = await gateway.AlertAsync("zone-3");       // the gateway's JSON send record
//   await HalowDirect.SendAlertAsync("192.168.12.50");         // direct mode, raw8
//
// The wire formats are byte-identical to the gateway's (see docs/PROTOCOL.md).

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Numerics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

namespace ClearObject.Halow
{
    /// <summary>Defaults shared by the gateway and the wearables.</summary>
    public static class HalowDefaults
    {
        public const string Version = "1.0.0";
        public const string DefaultUrl = "http://127.0.0.1:8080";
        public const string DefaultEgress = "192.168.12.50";
        public const string Group = "239.255.0.10";
        public const int Port = 5000;
        public const int ResponsePort = 5001;
        public const int Ttl = 4;
        public const ulong CommandAlert = 1;
        public const ulong CommandHeartbeatRequest = 2;
        internal const double MaxBurstSeconds = 5.0;
        internal const double MinIntervalSeconds = 0.02;
        internal const int ResponseDeadlineMs = 5000;
        internal const int MaxResponseBytes = 8192;
        internal const int MaxResponseConnections = 32;

        internal static string Env(string name, string fallback)
        {
            string v = Environment.GetEnvironmentVariable(name);
            return string.IsNullOrWhiteSpace(v) ? fallback : v.Trim();
        }

        internal static void Quietly(IDisposable d)
        {
            try
            {
                if (d != null) d.Dispose();
            }
            catch (Exception)
            {
                // closing a broken socket
            }
        }
    }

    // ------------------------------------------------------------------------
    // Wire formats
    // ------------------------------------------------------------------------

    /// <summary>A heartbeat's payload: battery, signal and state of one wearable.</summary>
    public sealed class WearableHeartbeat
    {
        public string DeviceId { get; set; }
        public int BatteryPercentage { get; set; }
        public int Rssi { get; set; }
        public uint UptimeSeconds { get; set; }
        public uint AppState { get; set; }
        public uint DeviceState { get; set; }
    }

    /// <summary>A wearable reply (protobuf DeviceResponse): an ACK or a HEARTBEAT.</summary>
    public sealed class DeviceResponse
    {
        /// <summary>ACK: when the wearable received the alert (its clock, ms). HEARTBEAT: the request's timestamp, echoed.</summary>
        public ulong TimestampMs { get; set; }
        /// <summary>"ACK", "HEARTBEAT", "UNKNOWN" or the numeric code.</summary>
        public string Code { get; set; }
        /// <summary>Present on ACKs.</summary>
        public bool? AckSuccess { get; set; }
        /// <summary>Present on heartbeats.</summary>
        public WearableHeartbeat Heartbeat { get; set; }
        /// <summary>The wearable's address (set by HalowResponseListener).</summary>
        public string Ip { get; set; }
        /// <summary>When the reply arrived, Unix ms (set by HalowResponseListener).</summary>
        public long ReceivedMs { get; set; }
    }

    /// <summary>Encoders and decoder for the alert datagrams and the wearables' replies.</summary>
    public static class HalowWire
    {
        // A varint as read from the wire: Hi * 2^64 + Lo (a 10-byte varint can exceed 64 bits).
        private struct Varint
        {
            public ulong Lo;
            public ulong Hi;
        }

        private static readonly UTF8Encoding Utf8 = new UTF8Encoding(false, false);

        /// <summary>The raw8 alert: Unix time in seconds as a little-endian IEEE-754 double (8 bytes).</summary>
        public static byte[] EncodeRaw8(double unixSeconds)
        {
            byte[] b = BitConverter.GetBytes(unixSeconds);
            if (!BitConverter.IsLittleEndian) Array.Reverse(b);
            return b;
        }

        /// <summary>protobuf GatewayMessage {uint64 timestamp = 1; CommandCode command = 2}; zero fields are left out.</summary>
        public static byte[] EncodeGatewayMessage(ulong timestampMs, ulong command)
        {
            var buf = new List<byte>(12);
            if (timestampMs != 0)
            {
                buf.Add(0x08);
                WriteVarint(buf, timestampMs);
            }
            if (command != 0)
            {
                buf.Add(0x10);
                WriteVarint(buf, command);
            }
            return buf.ToArray();
        }

        private static void WriteVarint(List<byte> buf, ulong n)
        {
            while (true)
            {
                byte b = (byte)(n & 0x7F);
                n >>= 7;
                buf.Add((byte)(b | (n != 0 ? 0x80 : 0)));
                if (n == 0) return;
            }
        }

        private static Varint ReadVarint(byte[] buf, ref int i)
        {
            int shift = 0;
            ulong lo = 0, hi = 0;
            while (true)
            {
                if (shift >= 64) throw new FormatException("varint longer than 10 bytes");
                if (i >= buf.Length) throw new FormatException("truncated varint");
                ulong bits = (ulong)(buf[i] & 0x7F);
                bool more = (buf[i] & 0x80) != 0;
                i++;
                lo |= bits << shift;
                if (shift > 57) hi |= bits >> (64 - shift);     // the bits at 2^64 and above
                if (!more) return new Varint { Lo = lo, Hi = hi };
                shift += 7;
            }
        }

        // {field number: Varint (wire type 0) or byte[] (wire types 1, 2, 5)}; FormatException when malformed.
        private static Dictionary<ulong, object> Fields(byte[] buf)
        {
            var result = new Dictionary<ulong, object>();
            int i = 0;
            while (i < buf.Length)
            {
                Varint key = ReadVarint(buf, ref i);
                bool hugeTag = key.Hi != 0;                 // a field number past 2^61: never one we read
                ulong tag = key.Lo >> 3;
                int wire = (int)(key.Lo & 7);
                object value;
                if (wire == 0)
                {
                    value = ReadVarint(buf, ref i);
                }
                else if (wire == 1 || wire == 2 || wire == 5)
                {
                    ulong len;
                    if (wire == 2)
                    {
                        Varint ln = ReadVarint(buf, ref i);
                        len = ln.Hi != 0 ? ulong.MaxValue : ln.Lo;
                    }
                    else
                    {
                        len = wire == 1 ? 8UL : 4UL;
                    }
                    if (len > (ulong)(buf.Length - i))
                        throw new FormatException("field " + tag + " runs past the end of the message");
                    var bytes = new byte[(int)len];
                    Array.Copy(buf, i, bytes, 0, (int)len);
                    i += (int)len;
                    value = bytes;
                }
                else
                {
                    throw new FormatException("wire type " + wire);
                }
                if (!hugeTag) result[tag] = value;
            }
            return result;
        }

        private static Varint GetInt(Dictionary<ulong, object> f, ulong tag)
        {
            object v;
            if (!f.TryGetValue(tag, out v)) return default(Varint);
            if (!(v is Varint)) throw new FormatException("field " + tag + ": wrong wire type");
            return (Varint)v;
        }

        private static byte[] GetBytes(Dictionary<ulong, object> f, ulong tag)
        {
            object v;
            if (!f.TryGetValue(tag, out v)) return new byte[0];
            byte[] b = v as byte[];
            if (b == null) throw new FormatException("field " + tag + ": wrong wire type");
            return b;
        }

        private static int Int32Of(Varint v)
        {
            return unchecked((int)(uint)(v.Lo & 0xFFFFFFFFUL));
        }

        private static uint UInt32Of(Varint v)
        {
            return (uint)(v.Lo & 0xFFFFFFFFUL);
        }

        private static string CodeName(Varint code)
        {
            if (code.Hi != 0)
                return ((new BigInteger(code.Hi) << 64) + new BigInteger(code.Lo)).ToString(CultureInfo.InvariantCulture);
            switch (code.Lo)
            {
                case 0: return "UNKNOWN";
                case 1: return "ACK";
                case 2: return "HEARTBEAT";
                default: return code.Lo.ToString(CultureInfo.InvariantCulture);
            }
        }

        /// <summary>Decodes a wearable reply. Throws FormatException when the bytes are malformed.</summary>
        public static DeviceResponse DecodeDeviceResponse(byte[] buf)
        {
            if (buf == null) throw new ArgumentNullException(nameof(buf));
            var f = Fields(buf);
            Varint code = GetInt(f, 2);
            var r = new DeviceResponse { TimestampMs = GetInt(f, 1).Lo, Code = CodeName(code) };
            if (f.ContainsKey(3))
            {
                Varint ok = GetInt(Fields(GetBytes(f, 3)), 1);
                r.AckSuccess = ok.Lo != 0 || ok.Hi != 0;
            }
            if (f.ContainsKey(4))
            {
                var h = Fields(GetBytes(f, 4));
                r.Heartbeat = new WearableHeartbeat
                {
                    DeviceId = Utf8.GetString(GetBytes(h, 1)),
                    BatteryPercentage = Int32Of(GetInt(h, 2)),
                    Rssi = Int32Of(GetInt(h, 3)),
                    UptimeSeconds = UInt32Of(GetInt(h, 4)),
                    AppState = UInt32Of(GetInt(h, 5)),
                    DeviceState = UInt32Of(GetInt(h, 6)),
                };
            }
            return r;
        }
    }

    // ------------------------------------------------------------------------
    // Gateway mode: the halow-alert HTTP API
    // ------------------------------------------------------------------------

    /// <summary>A failed gateway call. StatusCode is null when the gateway was not reached.</summary>
    public sealed class HalowException : Exception
    {
        public HalowException(string message, int? statusCode = null, string body = null) : base(message)
        {
            StatusCode = statusCode;
            Body = body;
        }

        public int? StatusCode { get; }
        public string Body { get; }
    }

    /// <summary>
    /// Client for the halow-alert service: POST /alert, GET /wearables, GET /health.
    /// The methods return the gateway's JSON text; parse it with the JSON library you use.
    /// Proxy settings are ignored: the gateway is on your local network.
    /// </summary>
    public sealed class HalowGatewayClient : IDisposable
    {
        private static readonly Dictionary<int, string> Hints = new Dictionary<int, string>
        {
            { 400, "the gateway rejected the request" },
            { 401, "missing or wrong token: set HALOW_TOKEN to the gateway's token" },
            { 404, "unknown path: is HALOW_URL the halow-alert gateway (http://<host>:8080)?" },
            { 413, "request body too large" },
            { 429, "rate limited: the gateway sends at most one alert every 0.5 s; retry shortly" },
            { 500, "the gateway could not send the alert (HaLowLink unplugged or not set up? see GET /health)" },
            { 503, "the gateway cannot send right now (no HaLowLink address on the gateway host, or its reply listener stopped; see GET /health)" },
        };

        private static readonly Regex ErrorField = new Regex("\"error\"\\s*:\\s*\"((?:[^\"\\\\]|\\\\.)*)\"");

        private readonly HttpClient http;
        private readonly string token;

        /// <param name="baseUrl">Gateway URL, for example http://192.0.2.10:8080. Null: HALOW_URL, else http://127.0.0.1:8080.</param>
        /// <param name="token">The gateway's token. Null: HALOW_TOKEN.</param>
        /// <param name="timeout">Per request (default 10 s; an alert with a burst gets the burst added).</param>
        public HalowGatewayClient(string baseUrl = null, string token = null, TimeSpan? timeout = null)
        {
            string url = string.IsNullOrWhiteSpace(baseUrl) ? HalowDefaults.Env("HALOW_URL", HalowDefaults.DefaultUrl) : baseUrl.Trim();
            BaseUrl = url.TrimEnd('/');
            if (!BaseUrl.StartsWith("http://", StringComparison.OrdinalIgnoreCase)
                && !BaseUrl.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("gateway URL must start with http:// or https://, not \"" + BaseUrl + "\"");
            Uri parsed;
            if (!Uri.TryCreate(BaseUrl + "/", UriKind.Absolute, out parsed) || parsed.Host.Length == 0)
                throw new ArgumentException("gateway URL is not a valid URL: \"" + BaseUrl + "\"");
            this.token = (token ?? Environment.GetEnvironmentVariable("HALOW_TOKEN") ?? "").Trim();
            if (this.token.IndexOf('\r') >= 0 || this.token.IndexOf('\n') >= 0)
                throw new ArgumentException("the token must not contain line breaks");
            RequestTimeout = timeout ?? TimeSpan.FromSeconds(10);
            http = new HttpClient(new HttpClientHandler { UseProxy = false, AllowAutoRedirect = false })
            {
                Timeout = System.Threading.Timeout.InfiniteTimeSpan,
            };
        }

        public string BaseUrl { get; }
        public TimeSpan RequestTimeout { get; set; }

        /// <summary>GET /health. Returns the JSON for 200 and for 503; check its "ok" field.</summary>
        public Task<string> HealthAsync(CancellationToken cancellationToken = default(CancellationToken))
        {
            return SendAsync(HttpMethod.Get, "/health", null, new[] { 200, 503 }, RequestTimeout, cancellationToken);
        }

        /// <summary>GET /wearables: {"devices": [...], "recent": [...]}.</summary>
        public Task<string> WearablesAsync(CancellationToken cancellationToken = default(CancellationToken))
        {
            return SendAsync(HttpMethod.Get, "/wearables", null, new[] { 200 }, RequestTimeout, cancellationToken);
        }

        /// <summary>
        /// POST /alert. Every wearable on the HaLowLink network alerts. Returns the gateway's
        /// send record (JSON); throws HalowException (StatusCode 401, 429, 500, ...) when it fails.
        /// </summary>
        /// <param name="label">Recorded in the gateway's log.</param>
        /// <param name="format">"raw8", "proto" or "both"; null for the gateway's default.</param>
        /// <param name="burstSeconds">Repeat the alert for this long (0 to 5); null for the gateway's default.</param>
        public Task<string> AlertAsync(string label = null, string format = null, double? burstSeconds = null,
            CancellationToken cancellationToken = default(CancellationToken))
        {
            var parts = new List<string>();
            if (label != null) parts.Add("\"label\": " + JsonString(label));
            if (format != null)
            {
                string f = format.Trim().ToLowerInvariant();
                if (f != "raw8" && f != "proto" && f != "both")
                    throw new ArgumentException("format must be raw8, proto or both");
                parts.Add("\"format\": \"" + f + "\"");
            }
            TimeSpan timeout = RequestTimeout;
            if (burstSeconds.HasValue)
            {
                double b = burstSeconds.Value;
                if (double.IsNaN(b) || double.IsInfinity(b) || b < 0)
                    throw new ArgumentException("burstSeconds must be a number from 0 to 5");
                parts.Add("\"burst_seconds\": " + b.ToString("R", CultureInfo.InvariantCulture));
                if (timeout != System.Threading.Timeout.InfiniteTimeSpan)      // infinite stays infinite
                    timeout += TimeSpan.FromSeconds(Math.Min(b, HalowDefaults.MaxBurstSeconds));   // answered when the burst ends
            }
            return SendAsync(HttpMethod.Post, "/alert", "{" + string.Join(", ", parts) + "}", new[] { 200 }, timeout,
                cancellationToken);
        }

        public void Dispose()
        {
            http.Dispose();
        }

        private async Task<string> SendAsync(HttpMethod method, string path, string jsonBody, int[] ok, TimeSpan timeout,
            CancellationToken cancellationToken)
        {
            using (var cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken))
            using (var req = new HttpRequestMessage(method, BaseUrl + path))
            {
                cts.CancelAfter(timeout);
                req.Headers.Accept.ParseAdd("application/json");
                if (method == HttpMethod.Post && token.Length > 0)
                    req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
                if (jsonBody != null) req.Content = new StringContent(jsonBody, Encoding.UTF8, "application/json");
                int status;
                string text;
                try
                {
                    using (HttpResponseMessage res = await http.SendAsync(req, cts.Token).ConfigureAwait(false))
                    {
                        status = (int)res.StatusCode;
                        // The gateway's JSON is UTF-8; an unknown charset in Content-Type must not fail the call.
                        text = Encoding.UTF8.GetString(await res.Content.ReadAsByteArrayAsync().ConfigureAwait(false));
                    }
                }
                catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
                {
                    throw new HalowException("cannot reach the gateway at " + BaseUrl + ": timed out");
                }
                catch (HttpRequestException e)
                {
                    throw new HalowException("cannot reach the gateway at " + BaseUrl + ": " + Innermost(e).Message);
                }
                catch (IOException e)
                {
                    throw new HalowException("cannot reach the gateway at " + BaseUrl + ": " + e.Message);
                }
                if (Array.IndexOf(ok, status) >= 0) return text;
                string hint;
                if (!Hints.TryGetValue(status, out hint)) hint = "unexpected response";
                string detail = ErrorText(text);
                throw new HalowException(method.Method + " " + path + ": HTTP " + status + ": " + hint
                    + (detail.Length > 0 ? " (" + detail + ")" : ""), status, text);
            }
        }

        private static Exception Innermost(Exception e)
        {
            while (e.InnerException != null) e = e.InnerException;
            return e;
        }

        // The "error" text of a JSON error body (the gateway writes {"error": "..."}).
        private static string ErrorText(string json)
        {
            Match m = ErrorField.Match(json ?? "");
            if (!m.Success) return "";
            string s = m.Groups[1].Value;
            var sb = new StringBuilder(s.Length);
            for (int i = 0; i < s.Length; i++)
            {
                char c = s[i];
                if (c != '\\' || i + 1 >= s.Length)
                {
                    sb.Append(c);
                    continue;
                }
                char e = s[++i];
                switch (e)
                {
                    case 'n': sb.Append('\n'); break;
                    case 'r': sb.Append('\r'); break;
                    case 't': sb.Append('\t'); break;
                    case 'b': sb.Append('\b'); break;
                    case 'f': sb.Append('\f'); break;
                    case 'u':
                        int code;
                        if (i + 4 < s.Length && int.TryParse(s.Substring(i + 1, 4), NumberStyles.HexNumber,
                                CultureInfo.InvariantCulture, out code))
                        {
                            sb.Append((char)code);
                            i += 4;
                        }
                        break;
                    default: sb.Append(e); break;
                }
            }
            return sb.ToString();
        }

        private static string JsonString(string s)
        {
            var sb = new StringBuilder("\"");
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                        else sb.Append(c);
                        break;
                }
            }
            return sb.Append('"').ToString();
        }
    }

    // ------------------------------------------------------------------------
    // Direct mode: UDP multicast to the wearables, replies on TCP
    // ------------------------------------------------------------------------

    /// <summary>What a direct send did (the same fields as the gateway's send record).</summary>
    public sealed class DirectAlertResult
    {
        public long TimestampMs { get; set; }
        public string Format { get; set; }
        public string Group { get; set; }
        public string Egress { get; set; }
        public int Datagrams { get; set; }

        public override string ToString()
        {
            return Format + " -> " + Group + " from " + Egress + " (" + Datagrams + " datagram(s)), ts_ms "
                + TimestampMs.ToString(CultureInfo.InvariantCulture);
        }
    }

    /// <summary>Sends to the wearables directly, from a machine plugged into the HaLowLink.</summary>
    public static class HalowDirect
    {
        private static readonly long EpochTicks = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc).Ticks;

        /// <summary>
        /// Sends one alert straight to the wearables. Every wearable on the network alerts.
        /// Does not wait for ACKs (see HalowResponseListener).
        /// </summary>
        /// <param name="egress">This machine's address on the HaLowLink. Null: HALOW_EGRESS, else 192.168.12.50.</param>
        /// <param name="format">"raw8" (default), "proto" or "both".</param>
        /// <param name="burstSeconds">Above 0: resend the same bytes every burstIntervalMs for that long (max 5 s).</param>
        public static async Task<DirectAlertResult> SendAlertAsync(string egress = null, string format = "raw8",
            string group = HalowDefaults.Group, int port = HalowDefaults.Port, int ttl = HalowDefaults.Ttl, double burstSeconds = 0,
            int burstIntervalMs = 100)
        {
            egress = string.IsNullOrWhiteSpace(egress) ? HalowDefaults.Env("HALOW_EGRESS", HalowDefaults.DefaultEgress) : egress.Trim();
            string fmt = (format ?? "raw8").Trim().ToLowerInvariant();
            if (fmt != "raw8" && fmt != "proto" && fmt != "both")
                throw new ArgumentException("format must be raw8, proto or both");
            var target = new IPEndPoint(IPAddress.Parse(group), port);
            using (UdpClient udp = MulticastSender(egress, ttl))
            {
                long ticks = DateTime.UtcNow.Ticks - EpochTicks;      // one clock reading for both formats
                var start = Stopwatch.StartNew();
                long nowMs = ticks / TimeSpan.TicksPerMillisecond;
                var payloads = new List<byte[]>();
                if (fmt == "raw8" || fmt == "both") payloads.Add(HalowWire.EncodeRaw8(ticks / (double)TimeSpan.TicksPerSecond));
                if (fmt == "proto" || fmt == "both") payloads.Add(HalowWire.EncodeGatewayMessage((ulong)nowMs, HalowDefaults.CommandAlert));
                int sent = 0;
                foreach (byte[] p in payloads)
                {
                    udp.Send(p, p.Length, target);
                    sent++;
                }
                double burst = double.IsNaN(burstSeconds) ? 0 : Math.Min(Math.Max(burstSeconds, 0), HalowDefaults.MaxBurstSeconds);
                if (burst > 0)
                {
                    double interval = Math.Max(HalowDefaults.MinIntervalSeconds, burstIntervalMs / 1000.0);
                    while (start.Elapsed.TotalSeconds + interval < burst)
                    {
                        await Task.Delay(TimeSpan.FromSeconds(interval)).ConfigureAwait(false);
                        foreach (byte[] p in payloads)
                        {
                            udp.Send(p, p.Length, target);
                            sent++;
                        }
                    }
                }
                return new DirectAlertResult
                {
                    TimestampMs = nowMs, Format = fmt, Group = group + ":" + port.ToString(CultureInfo.InvariantCulture),
                    Egress = egress, Datagrams = sent,
                };
            }
        }

        /// <summary>
        /// Asks every wearable to report in (battery, RSSI); they do not buzz. Returns the
        /// request's timestamp in ms, which the wearables echo back in their heartbeat.
        /// </summary>
        public static long SendHeartbeatRequest(string egress = null, string group = HalowDefaults.Group, int port = HalowDefaults.Port,
            int ttl = HalowDefaults.Ttl)
        {
            egress = string.IsNullOrWhiteSpace(egress) ? HalowDefaults.Env("HALOW_EGRESS", HalowDefaults.DefaultEgress) : egress.Trim();
            long ts = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            byte[] p = HalowWire.EncodeGatewayMessage((ulong)ts, HalowDefaults.CommandHeartbeatRequest);
            using (UdpClient udp = MulticastSender(egress, ttl))
                udp.Send(p, p.Length, new IPEndPoint(IPAddress.Parse(group), port));
            return ts;
        }

        /// <summary>"192.168.12." for 192.168.12.50: replies are accepted from this /24 only.</summary>
        public static string SubnetPrefix(string address)
        {
            if (address == null) throw new ArgumentNullException(nameof(address));
            int dot = address.LastIndexOf('.');
            // No dot: a prefix that no peer address has, never "" (which would accept every peer).
            return dot < 0 ? address + "." : address.Substring(0, dot + 1);
        }

        // A UDP socket bound to egress that sends multicast from it (IP_MULTICAST_IF).
        private static UdpClient MulticastSender(string egress, int ttl)
        {
            IPAddress addr;
            if (!IPAddress.TryParse(egress, out addr) || addr.AddressFamily != AddressFamily.InterNetwork
                || egress.Split('.').Length != 4)
                throw new ArgumentException("the egress address must be an IPv4 address of this machine, for example 192.168.12.50");
            UdpClient udp = null;
            try
            {
                udp = new UdpClient(new IPEndPoint(addr, 0));
                udp.Client.SetSocketOption(SocketOptionLevel.IP, SocketOptionName.MulticastInterface, addr.GetAddressBytes());
                udp.Client.SetSocketOption(SocketOptionLevel.IP, SocketOptionName.MulticastTimeToLive, ttl);
                return udp;
            }
            catch (SocketException e)
            {
                HalowDefaults.Quietly(udp);
                throw new InvalidOperationException("cannot send from " + egress + ": " + e.Message
                    + ". Is it an address of this machine (the HaLowLink plugged in and the address set)?", e);
            }
        }
    }

    /// <summary>
    /// Collects wearable replies on TCP (port 5001): each connection carries one
    /// DeviceResponse, an ACK or a HEARTBEAT. Only one program on a machine can listen on
    /// the port: stop the gateway service there first. Received is raised on a thread-pool
    /// thread (in Unity, hand the reply to the main thread before touching scene objects).
    /// </summary>
    public sealed class HalowResponseListener : IDisposable
    {
        private readonly TcpListener listener;
        private readonly int port;
        private readonly string allowPrefix;
        private readonly object gate = new object();
        private readonly List<DeviceResponse> responses = new List<DeviceResponse>();
        private readonly SemaphoreSlim slots = new SemaphoreSlim(HalowDefaults.MaxResponseConnections, HalowDefaults.MaxResponseConnections);
        private bool listening;         // under gate: set when listener.Start succeeds, cleared by Stop
        private volatile int run;       // changed under gate by every Start and Stop; an accept loop ends once it changes

        /// <param name="port">TCP port (default 5001).</param>
        /// <param name="allowPrefix">Accept only peers whose address starts with this, for example "192.168.12."; "" accepts all.</param>
        public HalowResponseListener(int port = HalowDefaults.ResponsePort, string allowPrefix = "")
        {
            this.port = port;
            this.allowPrefix = allowPrefix ?? "";
            listener = new TcpListener(IPAddress.Any, port);
        }

        /// <summary>Raised for each ACK or HEARTBEAT, on a thread-pool thread.</summary>
        public event Action<DeviceResponse> Received;

        /// <summary>Every reply so far (the last 1000).</summary>
        public IList<DeviceResponse> Responses
        {
            get
            {
                lock (gate) return responses.ToArray();
            }
        }

        /// <summary>
        /// Starts listening and returns this listener. Calling Start() again while it is listening
        /// does nothing and returns this listener, on every platform. After Stop() it listens again
        /// on the same port. Throws InvalidOperationException when the port cannot be taken.
        /// </summary>
        public HalowResponseListener Start()
        {
            lock (gate)
            {
                if (listening) return this;
                try
                {
                    // On Windows another process could otherwise bind the same port and take half of the replies.
                    if (RuntimeInformation.IsOSPlatform(OSPlatform.Windows)) listener.ExclusiveAddressUse = true;
                    listener.Start(32);
                }
                catch (SocketException e)
                {
                    throw new InvalidOperationException("cannot listen on TCP " + port + " for wearable replies: " + e.Message
                        + " (is the gateway service or another listener running on this machine?)", e);
                }
                listening = true;
                int current = ++run;    // an accept loop left over from before a Stop() sees the change and ends
                Task.Run(() => AcceptLoopAsync(current));
            }
            return this;
        }

        /// <summary>Stops listening; Responses keeps the replies received so far. Start() listens again.</summary>
        public void Stop()
        {
            lock (gate)
            {
                listening = false;
                run++;
                try
                {
                    listener.Stop();
                }
                catch (SocketException)
                {
                    // already stopped
                }
            }
        }

        public void Dispose()
        {
            Stop();
        }

        /// <summary>
        /// The first reply with this code ("ACK" or "HEARTBEAT") received at or after afterMs
        /// (Unix ms), waiting up to timeout (Timeout.InfiniteTimeSpan: no limit); null if none came.
        /// </summary>
        public async Task<DeviceResponse> WaitForAsync(string code, TimeSpan timeout, long afterMs = 0,
            CancellationToken cancellationToken = default(CancellationToken))
        {
            var clock = Stopwatch.StartNew();
            while (true)
            {
                lock (gate)
                {
                    foreach (DeviceResponse r in responses)
                        if (r.Code == code && r.ReceivedMs >= afterMs) return r;
                }
                if (timeout != System.Threading.Timeout.InfiniteTimeSpan && clock.Elapsed >= timeout) return null;
                await Task.Delay(50, cancellationToken).ConfigureAwait(false);
            }
        }

        private async Task AcceptLoopAsync(int current)
        {
            while (run == current)
            {
                TcpClient client;
                try
                {
                    client = await listener.AcceptTcpClientAsync().ConfigureAwait(false);
                }
                catch (ObjectDisposedException)
                {
                    return;
                }
                catch (InvalidOperationException)
                {
                    return;
                }
                catch (SocketException e)
                {
                    if (run != current) return;
                    // One connection failed before it was accepted (Windows reports a peer's reset
                    // this way): take the next one at once.
                    if (e.SocketErrorCode == SocketError.ConnectionReset || e.SocketErrorCode == SocketError.ConnectionAborted) continue;
                    await Task.Delay(500).ConfigureAwait(false);    // EMFILE and the like: wait, keep listening
                    continue;
                }
                string ip;
                try
                {
                    ip = ((IPEndPoint)client.Client.RemoteEndPoint).Address.ToString();
                }
                catch (Exception)
                {
                    HalowDefaults.Quietly(client);
                    continue;
                }
                if ((allowPrefix.Length > 0 && !ip.StartsWith(allowPrefix, StringComparison.Ordinal)) || !slots.Wait(0))
                {
                    HalowDefaults.Quietly(client);
                    continue;
                }
                _ = HandleAsync(client, ip);
            }
        }

        private async Task HandleAsync(TcpClient client, string ip)
        {
            try
            {
                var buf = new byte[HalowDefaults.MaxResponseBytes];
                int n = 0;
                var clock = Stopwatch.StartNew();
                using (client)
                {
                    NetworkStream stream = client.GetStream();
                    while (n < buf.Length)
                    {
                        long left = HalowDefaults.ResponseDeadlineMs - clock.ElapsedMilliseconds;   // for the whole connection
                        if (left <= 0) break;
                        Task<int> read = stream.ReadAsync(buf, n, buf.Length - n);
                        if (await Task.WhenAny(read, Task.Delay((int)left)).ConfigureAwait(false) != read)
                        {
                            Observe(read);      // closing the connection ends the read
                            break;
                        }
                        int got;
                        try
                        {
                            got = await read.ConfigureAwait(false);
                        }
                        catch (Exception)
                        {
                            break;               // reset: decode what arrived
                        }
                        if (got == 0) break;
                        n += got;
                    }
                }
                if (n == 0) return;
                var data = new byte[n];
                Array.Copy(buf, data, n);
                DeviceResponse r;
                try
                {
                    r = HalowWire.DecodeDeviceResponse(data);
                }
                catch (FormatException)
                {
                    return;
                }
                if (r.Code != "ACK" && r.Code != "HEARTBEAT") return;
                r.Ip = ip;
                r.ReceivedMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                lock (gate)
                {
                    responses.Add(r);
                    if (responses.Count > 1000) responses.RemoveRange(0, responses.Count - 1000);
                }
                Action<DeviceResponse> handler = Received;
                if (handler != null)
                {
                    foreach (Delegate subscriber in handler.GetInvocationList())
                    {
                        try
                        {
                            ((Action<DeviceResponse>)subscriber)(r);
                        }
                        catch (Exception)
                        {
                            // a subscriber's error must not stop the listener or the other subscribers
                        }
                    }
                }
            }
            catch (Exception)
            {
                // a broken connection must not stop the listener
            }
            finally
            {
                slots.Release();
            }
        }

        private static void Observe(Task t)
        {
            t.ContinueWith(x => { var unused = x.Exception; }, TaskContinuationOptions.OnlyOnFaulted);
        }
    }
}
