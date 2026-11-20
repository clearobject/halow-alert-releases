// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// Raise one alert from your own code, through the gateway (HTTP) or directly (UDP).
//
// WARNING: every wearable on the HaLowLink network vibrates and sounds. Warn people first.
//
// Gateway mode (any machine that can reach the gateway):
//   HALOW_URL=http://<gateway>:8080 HALOW_TOKEN=<token> dotnet run
// Direct mode (this machine is plugged into the HaLowLink, address 192.168.12.50):
//   HALOW_EGRESS=192.168.12.50 dotnet run -- --direct
// Codec self-test (sends nothing):
//   dotnet run -- --selftest ../testdata/vectors.json
//
// Exit code: 0 the alert was sent and a wearable ACKed it; 3 the alert was sent but no ACK
// came within HALOW_ACK_WAIT seconds (default 15; a wearable ACKs only after its alarm
// sequence ends); 1 the alert failed; 2 bad settings.
//
// This file uses System.Text.Json (in .NET 8 and newer) to read the gateway's JSON. HalowClient.cs
// itself needs no JSON library.

using System;
using System.Globalization;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using ClearObject.Halow;

namespace HalowExample
{
    public static class Program
    {
        public static async Task<int> Main(string[] args)
        {
            try
            {
                int i = Array.IndexOf(args, "--selftest");
                if (i >= 0) return SelfTest(i + 1 < args.Length ? args[i + 1] : null);
                return Array.IndexOf(args, "--direct") >= 0 ? await DirectAsync() : await ViaGatewayAsync();
            }
            catch (HalowException e)
            {
                Console.Error.WriteLine("example: " + e.Message);
                return 1;
            }
            catch (Exception e) when (e is ArgumentException || e is InvalidOperationException || e is FormatException)
            {
                Console.Error.WriteLine("example: " + e.Message);
                return 2;
            }
            catch (Exception e)
            {
                Console.Error.WriteLine("example: " + e.GetType().Name + ": " + e.Message);
                return 1;
            }
        }

        private static async Task<int> ViaGatewayAsync()
        {
            TimeSpan ackWait = AckWait;                            // checked before anything is sent
            using (var client = new HalowGatewayClient())          // HALOW_URL, HALOW_TOKEN
            {
                string health = await client.HealthAsync();
                Console.WriteLine("gateway health: " + Compact(health));
                using (JsonDocument h = JsonDocument.Parse(health))
                {
                    if (!h.RootElement.GetProperty("ok").GetBoolean())
                    {
                        Console.Error.WriteLine("the gateway cannot send right now: " + h.RootElement.GetProperty("error"));
                        return 1;
                    }
                }
                long before = TotalAcks(await client.WearablesAsync());

                string record = await client.AlertAsync("csharp example");   // the wearables alert now
                Console.WriteLine("alert sent: " + Compact(record));

                DateTime deadline = DateTime.UtcNow + ackWait;
                while (DateTime.UtcNow < deadline)
                {
                    string seen = await client.WearablesAsync();
                    if (TotalAcks(seen) > before)
                    {
                        using (JsonDocument doc = JsonDocument.Parse(seen))
                        {
                            foreach (JsonElement d in doc.RootElement.GetProperty("devices").EnumerateArray())
                                Console.WriteLine("wearable " + d.GetProperty("ip").GetString() + ": acks=" + d.GetProperty("acks")
                                    + " heartbeats=" + d.GetProperty("heartbeats") + " device_id=" + Field(d, "device_id")
                                    + " battery=" + Field(d, "battery_percentage") + "%");
                        }
                        Console.WriteLine("ACK received");
                        return 0;
                    }
                    await Task.Delay(500);
                }
                Console.Error.WriteLine("no ACK within " + ackWait.TotalSeconds + " s; check GET /wearables again later");
                return 3;
            }
        }

        private static async Task<int> DirectAsync()
        {
            TimeSpan ackWait = AckWait;                            // checked before anything is sent
            string egress = HalowEnv("HALOW_EGRESS", HalowDefaults.DefaultEgress);
            string group = HalowEnv("HALOW_GROUP", HalowDefaults.Group);
            int port = EnvPort("HALOW_PORT", HalowDefaults.Port);
            int responsePort = EnvPort("HALOW_RESPONSE_PORT", HalowDefaults.ResponsePort);

            using (var replies = new HalowResponseListener(responsePort, HalowDirect.SubnetPrefix(egress)).Start())
            {
                // 1. Ask the wearables to report in. Nothing buzzes.
                long requestTs = HalowDirect.SendHeartbeatRequest(egress, group, port);
                DeviceResponse hb = await replies.WaitForAsync("HEARTBEAT", TimeSpan.FromSeconds(3));
                if (hb != null)
                {
                    WearableHeartbeat h = hb.Heartbeat;
                    string rtt = hb.TimestampMs == (ulong)requestTs ? ", round trip " + (hb.ReceivedMs - requestTs) + " ms" : "";
                    Console.WriteLine("HEARTBEAT from " + hb.Ip + ": device_id=" + h.DeviceId + " battery=" + h.BatteryPercentage
                        + "% rssi=" + h.Rssi + " dBm" + rtt);
                }
                else
                {
                    Console.WriteLine("no heartbeat within 3 s (continuing)");
                }

                // 2. The alert. Every wearable on the network alerts.
                DirectAlertResult record = await HalowDirect.SendAlertAsync(egress, HalowEnv("HALOW_FORMAT", "raw8"), group, port);
                Console.WriteLine("alert sent: " + record);
                DeviceResponse ack = await replies.WaitForAsync("ACK", ackWait, record.TimestampMs);
                if (ack != null)
                {
                    Console.WriteLine("ACK from " + ack.Ip + ": wearable timestamp " + ack.TimestampMs + " ms");
                    return 0;
                }
            }
            Console.Error.WriteLine("no ACK within " + ackWait.TotalSeconds + " s");
            return 3;
        }

        // Checks the codec against the vectors produced by the gateway's own encoder and decoder.
        private static int SelfTest(string path)
        {
            if (path == null)
            {
                foreach (string candidate in new[]
                         {
                             Path.Combine("..", "testdata", "vectors.json"),
                             Path.Combine("integrations", "testdata", "vectors.json"),
                             Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "testdata", "vectors.json"),
                         })
                {
                    if (File.Exists(candidate))
                    {
                        path = candidate;
                        break;
                    }
                }
                if (path == null) throw new ArgumentException("testdata/vectors.json not found; pass its path after --selftest");
            }
            using (JsonDocument vec = JsonDocument.Parse(File.ReadAllText(path)))
            {
                JsonElement root = vec.RootElement;
                int n = 0, bad = 0;
                foreach (JsonElement c in root.GetProperty("raw8").EnumerateArray())
                {
                    n++;
                    string got = Hex(HalowWire.EncodeRaw8(c.GetProperty("seconds").GetDouble()));
                    if (got != c.GetProperty("hex").GetString())
                    {
                        bad++;
                        Console.WriteLine("raw8 " + c.GetProperty("seconds") + ": got " + got + ", want " + c.GetProperty("hex"));
                    }
                }
                foreach (JsonElement c in root.GetProperty("gateway_message").EnumerateArray())
                {
                    n++;
                    ulong ts = ulong.Parse(c.GetProperty("timestamp_ms").GetString(), CultureInfo.InvariantCulture);
                    ulong cmd = ulong.Parse(c.GetProperty("command").GetString(), CultureInfo.InvariantCulture);
                    string got = Hex(HalowWire.EncodeGatewayMessage(ts, cmd));
                    if (got != c.GetProperty("hex").GetString())
                    {
                        bad++;
                        Console.WriteLine("gateway_message " + ts + "/" + cmd + ": got " + got + ", want " + c.GetProperty("hex"));
                    }
                }
                foreach (JsonElement c in root.GetProperty("device_response").EnumerateArray())
                {
                    n++;
                    string hex = c.GetProperty("hex").GetString();
                    string got;
                    try
                    {
                        got = Canonical(HalowWire.DecodeDeviceResponse(FromHex(hex)));
                    }
                    catch (FormatException)
                    {
                        got = "null";
                    }
                    string want = Canonical(c.GetProperty("result"));
                    if (got != want)
                    {
                        bad++;
                        Console.WriteLine("device_response " + hex + ": got " + got + ", want " + want);
                    }
                }
                Console.WriteLine("selftest: " + (n - bad) + "/" + n + " vectors match");
                return bad == 0 ? 0 : 1;
            }
        }

        // One line per decoded reply, the same for the decoder's output and for the expected JSON.
        private static string Canonical(DeviceResponse r)
        {
            var sb = new StringBuilder();
            sb.Append("ts=").Append(r.TimestampMs.ToString(CultureInfo.InvariantCulture)).Append(" code=").Append(r.Code);
            if (r.AckSuccess.HasValue) sb.Append(" ack=").Append(r.AckSuccess.Value ? "true" : "false");
            if (r.Heartbeat != null)
            {
                WearableHeartbeat h = r.Heartbeat;
                sb.Append(" hb=").Append(h.DeviceId).Append('|').Append(h.BatteryPercentage.ToString(CultureInfo.InvariantCulture))
                  .Append('|').Append(h.Rssi.ToString(CultureInfo.InvariantCulture))
                  .Append('|').Append(h.UptimeSeconds.ToString(CultureInfo.InvariantCulture))
                  .Append('|').Append(h.AppState.ToString(CultureInfo.InvariantCulture))
                  .Append('|').Append(h.DeviceState.ToString(CultureInfo.InvariantCulture));
            }
            return sb.ToString();
        }

        private static string Canonical(JsonElement e)
        {
            if (e.ValueKind == JsonValueKind.Null) return "null";
            var sb = new StringBuilder();
            sb.Append("ts=").Append(e.GetProperty("timestamp_ms").GetString()).Append(" code=").Append(e.GetProperty("code").GetString());
            JsonElement v;
            if (e.TryGetProperty("ack_success", out v)) sb.Append(" ack=").Append(v.GetBoolean() ? "true" : "false");
            if (e.TryGetProperty("heartbeat", out v))
            {
                sb.Append(" hb=").Append(v.GetProperty("device_id").GetString());
                foreach (string k in new[] { "battery_percentage", "rssi", "uptime_seconds", "app_state", "device_state" })
                    sb.Append('|').Append(v.GetProperty(k).GetString());
            }
            return sb.ToString();
        }

        private static long TotalAcks(string wearablesJson)
        {
            using (JsonDocument doc = JsonDocument.Parse(wearablesJson))
            {
                long n = 0;
                foreach (JsonElement d in doc.RootElement.GetProperty("devices").EnumerateArray())
                {
                    JsonElement acks;
                    if (d.TryGetProperty("acks", out acks)) n += acks.GetInt64();
                }
                return n;
            }
        }

        private static string Field(JsonElement e, string name)
        {
            JsonElement v;
            return e.TryGetProperty(name, out v) ? v.ToString() : "?";
        }

        private static string Compact(string json)
        {
            using (JsonDocument doc = JsonDocument.Parse(json)) return JsonSerializer.Serialize(doc.RootElement);
        }

        // How long to wait for an ACK: HALOW_ACK_WAIT seconds (default 15).
        private static TimeSpan AckWait
        {
            get
            {
                double s;
                string raw = HalowEnv("HALOW_ACK_WAIT", "15");
                if (!double.TryParse(raw, NumberStyles.Float, CultureInfo.InvariantCulture, out s) || double.IsNaN(s) || s < 0 || s > 3600)
                    throw new ArgumentException("HALOW_ACK_WAIT=\"" + raw + "\" must be a number of seconds (0 to 3600)");
                return TimeSpan.FromSeconds(s);
            }
        }

        private static string HalowEnv(string name, string fallback)
        {
            string v = Environment.GetEnvironmentVariable(name);
            return string.IsNullOrWhiteSpace(v) ? fallback : v.Trim();
        }

        private static int EnvPort(string name, int fallback)
        {
            string raw = HalowEnv(name, "");
            if (raw.Length == 0) return fallback;
            int n;
            if (!int.TryParse(raw, NumberStyles.None, CultureInfo.InvariantCulture, out n) || n < 1 || n > 65535)
                throw new ArgumentException(name + "=\"" + raw + "\" is not a port number (1-65535)");
            return n;
        }

        private static string Hex(byte[] b)
        {
            var sb = new StringBuilder(b.Length * 2);
            foreach (byte x in b) sb.Append(x.ToString("x2", CultureInfo.InvariantCulture));
            return sb.ToString();
        }

        private static byte[] FromHex(string hex)
        {
            var b = new byte[hex.Length / 2];
            for (int i = 0; i < b.Length; i++)
                b[i] = byte.Parse(hex.Substring(2 * i, 2), NumberStyles.HexNumber, CultureInfo.InvariantCulture);
            return b;
        }
    }
}
