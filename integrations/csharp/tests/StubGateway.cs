// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// A stand-in for the halow-alert gateway's HTTP API: a TcpListener on 127.0.0.1 that speaks
// just enough HTTP/1.1. (HttpListener would need URL reservations on Windows.) It records
// every request and answers with whatever the test's handler returns, including answers a
// real server would not give: none at all, a closed connection, or bytes that are not HTTP.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace ClearObject.Halow.Tests
{
    internal sealed class StubRequest
    {
        public string Method { get; set; }
        public string Path { get; set; }
        public string Version { get; set; }
        public List<KeyValuePair<string, string>> HeaderList { get; } = new List<KeyValuePair<string, string>>();
        public byte[] Body { get; set; } = new byte[0];
        public string BodyText => Encoding.UTF8.GetString(Body);

        /// <summary>The header's value (several joined with ", "), or null when it is absent.</summary>
        public string Header(string name)
        {
            string[] values = HeaderList.Where(h => h.Key.Equals(name, StringComparison.OrdinalIgnoreCase)).Select(h => h.Value).ToArray();
            return values.Length == 0 ? null : string.Join(", ", values);
        }
    }

    internal sealed class StubReply
    {
        public int Status { get; set; } = 200;
        public string Body { get; set; } = "";
        public string ContentType { get; set; } = "application/json";
        public TimeSpan Delay { get; set; }
        /// <summary>Never answer; the connection stays open until the stub is disposed.</summary>
        public bool Hang { get; set; }
        /// <summary>Close the connection without answering.</summary>
        public bool CloseWithoutReply { get; set; }
        /// <summary>Write these bytes instead of an HTTP response, then close.</summary>
        public byte[] Raw { get; set; }
        /// <summary>Answer as an HTTP/1.0 server that ends the body by closing (no Content-Length).</summary>
        public bool Http10WithoutLength { get; set; }
        /// <summary>Leave out "Connection: close", so the client may keep the connection for the next request (the stub still closes it).</summary>
        public bool KeepAlive { get; set; }
        public List<KeyValuePair<string, string>> Headers { get; } = new List<KeyValuePair<string, string>>();

        public static StubReply Json(int status, string body)
        {
            return new StubReply { Status = status, Body = body };
        }
    }

    internal sealed class StubGateway : IDisposable
    {
        public const string Token = "good-token";
        public const string HealthOk = "{\"ok\": true, \"version\": \"1.0.0\", \"egress\": \"127.0.0.1\", \"error\": null, \"listener\": true}";
        public const string AlertRecord = "{\"event\": \"alert_sent\", \"ts_ms\": 1767225600123, \"format\": \"raw8\", \"egress\": \"127.0.0.1\", \"datagrams\": 1}";
        public const string WearablesBody = "{\"devices\": [{\"ip\": \"127.0.0.1\", \"acks\": 2, \"heartbeats\": 1}], \"recent\": []}";

        private readonly TcpListener listener;
        private readonly CancellationTokenSource stopping = new CancellationTokenSource();
        private readonly object gate = new object();
        private readonly List<StubRequest> requests = new List<StubRequest>();
        private readonly List<TcpClient> connections = new List<TcpClient>();
        private int accepted;

        public StubGateway(Func<StubRequest, StubReply> handler = null)
        {
            Handler = handler ?? Default;
            listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start(64);
            Port = ((IPEndPoint)listener.LocalEndpoint).Port;
            _ = Task.Run(AcceptLoopAsync);
        }

        public int Port { get; }
        public string Url => "http://127.0.0.1:" + Port.ToString(CultureInfo.InvariantCulture);
        public Func<StubRequest, StubReply> Handler { get; set; }

        /// <summary>How many TCP connections were accepted.</summary>
        public int Connections => Volatile.Read(ref accepted);

        public IReadOnlyList<StubRequest> Requests
        {
            get
            {
                lock (gate) return requests.ToArray();
            }
        }

        /// <summary>Like the gateway: exact paths, and POST /alert needs "Bearer good-token".</summary>
        public static StubReply Default(StubRequest r)
        {
            if (r.Method == "GET" && (r.Path == "/health" || r.Path == "/")) return StubReply.Json(200, HealthOk);
            if (r.Method == "GET" && r.Path == "/wearables") return StubReply.Json(200, WearablesBody);
            if (r.Method == "POST" && r.Path == "/alert")
            {
                return r.Header("Authorization") == "Bearer " + Token
                    ? StubReply.Json(200, AlertRecord)
                    : StubReply.Json(401, "{\"error\": \"missing or wrong Bearer token\"}");
            }
            return StubReply.Json(404, "{\"error\": \"" + r.Method + " " + r.Path + "\"}");
        }

        private async Task AcceptLoopAsync()
        {
            while (!stopping.IsCancellationRequested)
            {
                TcpClient c;
                try
                {
                    c = await listener.AcceptTcpClientAsync(stopping.Token).ConfigureAwait(false);
                }
                catch (Exception)
                {
                    return;
                }
                Interlocked.Increment(ref accepted);
                lock (gate) connections.Add(c);
                _ = Task.Run(() => ServeAsync(c));
            }
        }

        private async Task ServeAsync(TcpClient c)
        {
            try
            {
                using (c)
                {
                    NetworkStream s = c.GetStream();
                    StubRequest req = await ReadRequestAsync(s).ConfigureAwait(false);
                    if (req == null) return;
                    lock (gate) requests.Add(req);
                    StubReply reply = Handler(req);
                    if (reply.Delay > TimeSpan.Zero) await Task.Delay(reply.Delay, stopping.Token).ConfigureAwait(false);
                    if (reply.Hang)
                    {
                        await Task.Delay(Timeout.Infinite, stopping.Token).ConfigureAwait(false);
                        return;
                    }
                    if (reply.CloseWithoutReply) return;
                    byte[] bytes = reply.Raw ?? Encode(reply);
                    await s.WriteAsync(bytes, 0, bytes.Length, stopping.Token).ConfigureAwait(false);
                    c.Client.Shutdown(SocketShutdown.Send);
                    // Let the client read everything before the socket closes.
                    var drain = new byte[1024];
                    using (var cts = CancellationTokenSource.CreateLinkedTokenSource(stopping.Token))
                    {
                        cts.CancelAfter(TimeSpan.FromSeconds(5));
                        while (await s.ReadAsync(drain.AsMemory(), cts.Token).ConfigureAwait(false) > 0)
                        {
                        }
                    }
                }
            }
            catch (Exception)
            {
                // the client gave up, or the stub is stopping
            }
            finally
            {
                lock (gate) connections.Remove(c);
            }
        }

        private static byte[] Encode(StubReply reply)
        {
            byte[] body = Encoding.UTF8.GetBytes(reply.Body ?? "");
            var head = new StringBuilder();
            if (reply.Http10WithoutLength)
            {
                head.Append("HTTP/1.0 ").Append(reply.Status).Append(" Stub\r\n");
            }
            else
            {
                head.Append("HTTP/1.1 ").Append(reply.Status).Append(" Stub\r\n");
                head.Append("Content-Length: ").Append(body.Length).Append("\r\n");
                if (!reply.KeepAlive) head.Append("Connection: close\r\n");
            }
            if (reply.ContentType != null) head.Append("Content-Type: ").Append(reply.ContentType).Append("\r\n");
            foreach (KeyValuePair<string, string> h in reply.Headers) head.Append(h.Key).Append(": ").Append(h.Value).Append("\r\n");
            head.Append("\r\n");
            return Encoding.ASCII.GetBytes(head.ToString()).Concat(body).ToArray();
        }

        private async Task<StubRequest> ReadRequestAsync(NetworkStream s)
        {
            var buf = new List<byte>();
            var chunk = new byte[4096];
            int headerEnd = -1;
            using (var cts = CancellationTokenSource.CreateLinkedTokenSource(stopping.Token))
            {
                cts.CancelAfter(TimeSpan.FromSeconds(20));
                while (headerEnd < 0)
                {
                    int n = await s.ReadAsync(chunk.AsMemory(), cts.Token).ConfigureAwait(false);
                    if (n == 0) return null;
                    buf.AddRange(chunk.Take(n));
                    headerEnd = IndexOf(buf, new byte[] { 13, 10, 13, 10 });
                    if (buf.Count > 65536) return null;
                }
                string[] lines = Encoding.ASCII.GetString(buf.Take(headerEnd).ToArray()).Split("\r\n");
                string[] start = lines[0].Split(' ');
                var req = new StubRequest { Method = start[0], Path = start.Length > 1 ? start[1] : "", Version = start.Length > 2 ? start[2] : "" };
                foreach (string line in lines.Skip(1))
                {
                    int colon = line.IndexOf(':');
                    if (colon > 0) req.HeaderList.Add(new KeyValuePair<string, string>(line.Substring(0, colon).Trim(), line.Substring(colon + 1).Trim()));
                }
                var body = buf.Skip(headerEnd + 4).ToList();
                if (int.TryParse(req.Header("Content-Length"), NumberStyles.None, CultureInfo.InvariantCulture, out int length))
                {
                    while (body.Count < length)
                    {
                        int n = await s.ReadAsync(chunk.AsMemory(), cts.Token).ConfigureAwait(false);
                        if (n == 0) break;
                        body.AddRange(chunk.Take(n));
                    }
                }
                req.Body = body.ToArray();
                return req;
            }
        }

        private static int IndexOf(List<byte> hay, byte[] needle)
        {
            for (int i = 0; i + needle.Length <= hay.Count; i++)
            {
                int j = 0;
                while (j < needle.Length && hay[i + j] == needle[j]) j++;
                if (j == needle.Length) return i;
            }
            return -1;
        }

        public void Dispose()
        {
            stopping.Cancel();
            try
            {
                listener.Stop();
            }
            catch (SocketException)
            {
            }
            TcpClient[] open;
            lock (gate) open = connections.ToArray();
            foreach (TcpClient c in open)
            {
                try
                {
                    c.Dispose();
                }
                catch (Exception)
                {
                }
            }
        }
    }
}
