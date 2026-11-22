// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// The codec (HalowWire): every vector in integrations/testdata/vectors.json, varint edge
// values, and the decoder's handling of unusual and malformed replies.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using Xunit;

namespace ClearObject.Halow.Tests
{
    public sealed class VectorTests
    {
        private static readonly Lazy<JsonDocument> Vectors = new Lazy<JsonDocument>(() => JsonDocument.Parse(File.ReadAllText(Paths.VectorsPath)));

        private static IEnumerable<JsonElement> Cases(string kind)
        {
            return Vectors.Value.RootElement.GetProperty(kind).EnumerateArray();
        }

        /// <summary>The expected decoder output in the vectors, in the same one-line form as Canonical.Of.</summary>
        internal static string Expected(JsonElement result)
        {
            if (result.ValueKind == JsonValueKind.Null) return "null";
            var sb = new StringBuilder();
            sb.Append("ts=").Append(result.GetProperty("timestamp_ms").GetString()).Append(" code=").Append(result.GetProperty("code").GetString());
            if (result.TryGetProperty("ack_success", out JsonElement ack)) sb.Append(" ack=").Append(ack.GetBoolean() ? "true" : "false");
            if (result.TryGetProperty("heartbeat", out JsonElement hb))
            {
                sb.Append(" hb=").Append(hb.GetProperty("device_id").GetString());
                foreach (string k in new[] { "battery_percentage", "rssi", "uptime_seconds", "app_state", "device_state" })
                    sb.Append('|').Append(hb.GetProperty(k).GetString());
            }
            return sb.ToString();
        }

        internal static IEnumerable<(byte[] Bytes, string Expected)> DeviceResponses()
        {
            foreach (JsonElement c in Cases("device_response"))
            {
                JsonElement result = c.TryGetProperty("result", out JsonElement r) ? r : default;
                yield return (Hex.Parse(c.GetProperty("hex").GetString()), result.ValueKind == JsonValueKind.Undefined ? "null" : Expected(result));
            }
        }

        [Fact]
        public void The_vectors_file_has_every_kind()
        {
            Assert.True(File.Exists(Paths.VectorsPath), Paths.VectorsPath);
            Assert.NotEmpty(Cases("raw8"));
            Assert.NotEmpty(Cases("gateway_message"));
            Assert.True(Cases("device_response").Count() > 100);
            Assert.Contains(DeviceResponses(), c => c.Expected == "null");
        }

        [Fact]
        public void Raw8_matches_every_vector()
        {
            var bad = new List<string>();
            foreach (JsonElement c in Cases("raw8"))
            {
                byte[] got = HalowWire.EncodeRaw8(c.GetProperty("seconds").GetDouble());
                Assert.Equal(8, got.Length);
                if (Hex.Of(got) != c.GetProperty("hex").GetString())
                    bad.Add(c.GetProperty("seconds").GetRawText() + ": got " + Hex.Of(got) + ", want " + c.GetProperty("hex").GetString());
            }
            Assert.Empty(bad);
        }

        [Fact]
        public void GatewayMessage_matches_every_vector()
        {
            var bad = new List<string>();
            foreach (JsonElement c in Cases("gateway_message"))
            {
                ulong ts = ulong.Parse(c.GetProperty("timestamp_ms").GetString(), CultureInfo.InvariantCulture);
                ulong cmd = ulong.Parse(c.GetProperty("command").GetString(), CultureInfo.InvariantCulture);
                string got = Hex.Of(HalowWire.EncodeGatewayMessage(ts, cmd));
                if (got != c.GetProperty("hex").GetString()) bad.Add(ts + "/" + cmd + ": got " + got + ", want " + c.GetProperty("hex").GetString());
            }
            Assert.Empty(bad);
        }

        [Fact]
        public void DeviceResponse_matches_every_vector()
        {
            var bad = new List<string>();
            foreach ((byte[] bytes, string want) in DeviceResponses())
            {
                string got = Canonical.Decode(bytes);      // any exception but FormatException fails the test
                if (got != want) bad.Add(Hex.Of(bytes) + ": got " + got + ", want " + want);
            }
            Assert.Empty(bad);
        }

        [Fact]
        public void The_reference_decoder_matches_every_vector()
        {
            // ReferenceDecoder (below) is the oracle of the fuzz test: check it against the vectors first.
            var bad = new List<string>();
            foreach ((byte[] bytes, string want) in DeviceResponses())
            {
                string got = ReferenceDecoder.Decode(bytes);
                if (got != want) bad.Add(Hex.Of(bytes) + ": got " + got + ", want " + want);
            }
            Assert.Empty(bad);
        }
    }

    public sealed class EncoderTests
    {
        public static IEnumerable<object[]> VarintEdges()
        {
            var values = new List<ulong> { 1, 2, 127, 128, 255, 256, 300, 16383, 16384, uint.MaxValue, (ulong)uint.MaxValue + 1,
                long.MaxValue, (ulong)long.MaxValue + 1, ulong.MaxValue - 1, ulong.MaxValue, 1767225600123 };
            for (int bits = 7; bits < 64; bits += 7)
            {
                values.Add((1UL << bits) - 1);      // the largest value of n bytes
                values.Add(1UL << bits);            // the smallest value of n + 1 bytes
            }
            return values.Distinct().Select(v => new object[] { v });
        }

        [Theory]
        [MemberData(nameof(VarintEdges))]
        public void GatewayMessage_varint_edges(ulong value)
        {
            byte[] ts = HalowWire.EncodeGatewayMessage(value, 0);
            Assert.Equal("08" + Hex.Of(Proto.VarintBytes(value)), Hex.Of(ts));
            byte[] cmd = HalowWire.EncodeGatewayMessage(0, value);
            Assert.Equal("10" + Hex.Of(Proto.VarintBytes(value)), Hex.Of(cmd));

            // GatewayMessage and DeviceResponse share fields 1 and 2, so the decoder reads them back.
            DeviceResponse r = HalowWire.DecodeDeviceResponse(HalowWire.EncodeGatewayMessage(value, value));
            Assert.Equal(value, r.TimestampMs);
            Assert.Equal(value == 1 ? "ACK" : value == 2 ? "HEARTBEAT" : value.ToString(CultureInfo.InvariantCulture), r.Code);
        }

        [Theory]
        [InlineData(0UL, 0UL, "")]
        [InlineData(0UL, 1UL, "1001")]
        [InlineData(5UL, 0UL, "0805")]
        [InlineData(1UL, 1UL, "08011001")]
        [InlineData(1767225600123UL, 1UL, "08fbd0eab6b7331001")]
        [InlineData(1767225600123UL, 2UL, "08fbd0eab6b7331002")]
        [InlineData(ulong.MaxValue, 2UL, "08ffffffffffffffffff011002")]
        [InlineData(9223372036854775808UL, 1UL, "0880808080808080808001" + "1001")]
        public void GatewayMessage_known_bytes(ulong timestampMs, ulong command, string hex)
        {
            Assert.Equal(hex, Hex.Of(HalowWire.EncodeGatewayMessage(timestampMs, command)));
        }

        [Fact]
        public void Command_constants_match_the_protocol()
        {
            Assert.Equal(1UL, HalowDefaults.CommandAlert);
            Assert.Equal(2UL, HalowDefaults.CommandHeartbeatRequest);
        }

        [Theory]
        [InlineData(1767225600.123, "3bdf07406e55da41")]
        [InlineData(0.0, "0000000000000000")]
        [InlineData(-0.0, "0000000000000080")]
        [InlineData(1.0, "000000000000f03f")]
        [InlineData(double.Epsilon, "0100000000000000")]
        [InlineData(double.MaxValue, "ffffffffffffef7f")]
        [InlineData(double.PositiveInfinity, "000000000000f07f")]
        [InlineData(double.NegativeInfinity, "000000000000f0ff")]
        public void Raw8_is_a_little_endian_double(double seconds, string hex)
        {
            Assert.Equal(hex, Hex.Of(HalowWire.EncodeRaw8(seconds)));
        }

        [Fact]
        public void Raw8_round_trips_and_keeps_nan()
        {
            foreach (double v in new[] { 1767225600.123, 1e-300, -12345.678, double.NaN })
            {
                byte[] b = HalowWire.EncodeRaw8(v);
                Assert.Equal(8, b.Length);
                double back = BitConverter.ToDouble(BitConverter.IsLittleEndian ? b : b.Reverse().ToArray(), 0);
                Assert.Equal(BitConverter.DoubleToInt64Bits(v), BitConverter.DoubleToInt64Bits(back));
            }
        }

        [Fact]
        public void Defaults_match_the_documentation()
        {
            Assert.Equal("239.255.0.10", HalowDefaults.Group);
            Assert.Equal(5000, HalowDefaults.Port);
            Assert.Equal(5001, HalowDefaults.ResponsePort);
            Assert.Equal(4, HalowDefaults.Ttl);
            Assert.Equal("192.168.12.50", HalowDefaults.DefaultEgress);
            Assert.Equal("http://127.0.0.1:8080", HalowDefaults.DefaultUrl);
        }

        [Fact]
        public void Version_matches_the_example_project()
        {
            string csproj = File.ReadAllText(Path.Combine(Paths.CsharpDir, "HalowExample.csproj"));
            Assert.Contains("<Version>" + HalowDefaults.Version + "</Version>", csproj);
            Assert.Matches(@"^\d+\.\d+\.\d+([-+.][0-9A-Za-z.-]+)?$", HalowDefaults.Version);
        }
    }

    public sealed class DecoderTests
    {
        private static DeviceResponse Decode(string hex)
        {
            return HalowWire.DecodeDeviceResponse(Hex.Parse(hex));
        }

        private static FormatException Rejects(byte[] bytes)
        {
            return Assert.Throws<FormatException>(() => HalowWire.DecodeDeviceResponse(bytes));
        }

        private static FormatException Rejects(string hex)
        {
            return Rejects(Hex.Parse(hex));
        }

        [Fact]
        public void Null_is_an_argument_error()
        {
            Assert.Throws<ArgumentNullException>(() => HalowWire.DecodeDeviceResponse(null));
        }

        [Fact]
        public void Empty_message_is_all_defaults()
        {
            DeviceResponse r = HalowWire.DecodeDeviceResponse(new byte[0]);
            Assert.Equal(0UL, r.TimestampMs);
            Assert.Equal("UNKNOWN", r.Code);
            Assert.Null(r.AckSuccess);
            Assert.Null(r.Heartbeat);
            Assert.Null(r.Ip);
            Assert.Equal(0, r.ReceivedMs);
        }

        [Fact]
        public void Ack_from_the_protocol_document()
        {
            DeviceResponse r = Decode("08 a3 d1 ea b6 b7 33 10 01 1a 02 08 01");
            Assert.Equal(1767225600163UL, r.TimestampMs);
            Assert.Equal("ACK", r.Code);
            Assert.True(r.AckSuccess);
            Assert.Null(r.Heartbeat);
        }

        [Fact]
        public void Ack_with_an_empty_payload_is_not_a_success()
        {
            Assert.False(HalowWire.DecodeDeviceResponse(Wearable.Ack(5, success: false)).AckSuccess);
            // success = 2 is still true (any non-zero varint), as protobuf reads a bool.
            Assert.True(Decode("08 05 10 01 1a 02 08 02").AckSuccess);
            // success as a 10-byte varint whose low 64 bits are zero: still non-zero.
            Assert.True(Decode("10 01 1a 0b 08 80 80 80 80 80 80 80 80 80 02").AckSuccess);
        }

        [Fact]
        public void Heartbeat_from_the_protocol_document()
        {
            DeviceResponse r = Decode("08 fb d0 ea b6 b7 33 10 02 22 1b 0a 05 77 7a 2d 30 37 10 57 18 c2 ff ff ff ff ff ff ff ff 01 20 90 1c 28 01 30 02");
            Assert.Equal(1767225600123UL, r.TimestampMs);
            Assert.Equal("HEARTBEAT", r.Code);
            Assert.Null(r.AckSuccess);
            WearableHeartbeat h = r.Heartbeat;
            Assert.Equal("wz-07", h.DeviceId);
            Assert.Equal(87, h.BatteryPercentage);
            Assert.Equal(-62, h.Rssi);
            Assert.Equal(3600u, h.UptimeSeconds);
            Assert.Equal(1u, h.AppState);
            Assert.Equal(2u, h.DeviceState);
        }

        [Theory]
        [InlineData(-1)]
        [InlineData(-62)]
        [InlineData(-128)]
        [InlineData(int.MinValue)]
        [InlineData(int.MaxValue)]
        [InlineData(0)]
        public void Negative_and_extreme_int32_values(int value)
        {
            WearableHeartbeat h = HalowWire.DecodeDeviceResponse(Wearable.Heartbeat(1, "x", battery: value, rssi: value)).Heartbeat;
            Assert.Equal(value, h.BatteryPercentage);
            Assert.Equal(value, h.Rssi);
        }

        [Fact]
        public void Int32_fields_keep_the_low_32_bits()
        {
            byte[] hb = new Proto()
                .Varint(2, (1UL << 32) + 5)        // battery: 5
                .Varint(3, 0x80000000UL)           // rssi: int.MinValue
                .Varint(4, (1UL << 32) + 7)        // uptime: 7
                .Varint(5, 0xFFFFFFFFUL)           // app_state: uint.MaxValue
                .Varint(6, ulong.MaxValue)         // device_state: uint.MaxValue
                .ToArray();
            WearableHeartbeat h = HalowWire.DecodeDeviceResponse(new Proto().Varint(2, 2).Bytes(4, hb).ToArray()).Heartbeat;
            Assert.Equal(5, h.BatteryPercentage);
            Assert.Equal(int.MinValue, h.Rssi);
            Assert.Equal(7u, h.UptimeSeconds);
            Assert.Equal(uint.MaxValue, h.AppState);
            Assert.Equal(uint.MaxValue, h.DeviceState);
            Assert.Equal("", h.DeviceId);
        }

        [Fact]
        public void An_empty_heartbeat_payload_gives_defaults()
        {
            WearableHeartbeat h = Decode("10 02 22 00").Heartbeat;
            Assert.NotNull(h);
            Assert.Equal("", h.DeviceId);
            Assert.Equal(0, h.BatteryPercentage);
            Assert.Equal(0u, h.UptimeSeconds);
        }

        [Fact]
        public void Heartbeat_code_without_a_payload_has_no_heartbeat()
        {
            DeviceResponse r = Decode("08 05 10 02");
            Assert.Equal("HEARTBEAT", r.Code);
            Assert.Null(r.Heartbeat);
        }

        [Theory]
        [InlineData("10 00", "UNKNOWN")]
        [InlineData("10 01", "ACK")]
        [InlineData("10 02", "HEARTBEAT")]
        [InlineData("10 07", "7")]
        [InlineData("10 ff ff ff ff ff ff ff ff ff 01", "18446744073709551615")]
        [InlineData("10 80 80 80 80 80 80 80 80 80 02", "18446744073709551616")]
        [InlineData("10 81 80 80 80 80 80 80 80 80 02", "18446744073709551617")]
        [InlineData("10 ff ff ff ff ff ff ff ff ff 7f", "1180591620717411303423")]   // 2^70 - 1, the largest 10-byte varint
        public void Code_names_and_numbers_past_64_bits(string hex, string code)
        {
            Assert.Equal(code, Decode(hex).Code);
        }

        [Fact]
        public void Timestamp_keeps_the_low_64_bits_of_a_10_byte_varint()
        {
            Assert.Equal(ulong.MaxValue, Decode("08 ff ff ff ff ff ff ff ff ff 7f 10 01").TimestampMs);
            Assert.Equal(0UL, Decode("08 80 80 80 80 80 80 80 80 80 02 10 01").TimestampMs);
        }

        [Fact]
        public void Varint_longer_than_10_bytes_is_rejected()
        {
            FormatException e = Rejects("08 ff ff ff ff ff ff ff ff ff ff 01");
            Assert.Contains("10 bytes", e.Message);
            Rejects("08 80 80 80 80 80 80 80 80 80 80 00");   // 11 bytes, even with a zero value
            Rejects("10 01 1a 0c 08 ff ff ff ff ff ff ff ff ff ff 01");   // inside a payload
        }

        [Fact]
        public void Unknown_fields_of_every_wire_type_are_skipped()
        {
            byte[] msg = new Proto()
                .Varint(9, 12345)
                .Fixed64(10, 0x0102030405060708)
                .Bytes(11, new byte[] { 1, 2, 3 })
                .Fixed32(12, 7)
                .Varint(1, 42)
                .Varint(2, 2)
                .Varint(1000000, 1)
                .Varint(0, 9)                                  // field 0 (invalid in protobuf), ignored as the reference does
                .Bytes(4, new Proto().Bytes(1, Encoding.UTF8.GetBytes("wz-9")).Fixed32(99, 1).Varint(2, 50).Bytes(77, new byte[0]).ToArray())
                .ToArray();
            DeviceResponse r = HalowWire.DecodeDeviceResponse(msg);
            Assert.Equal(42UL, r.TimestampMs);
            Assert.Equal("HEARTBEAT", r.Code);
            Assert.Equal("wz-9", r.Heartbeat.DeviceId);
            Assert.Equal(50, r.Heartbeat.BatteryPercentage);
        }

        [Fact]
        public void Huge_field_numbers_are_skipped_and_do_not_alias_known_ones()
        {
            // A key of 2^64 + 8 has low 64 bits 0x08 (field 1, varint), but it is field 2^61 + 1.
            byte[] msg = new Proto().Raw(0x88, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x02, 0x05).Varint(2, 1).ToArray();
            DeviceResponse r = HalowWire.DecodeDeviceResponse(msg);
            Assert.Equal(0UL, r.TimestampMs);
            Assert.Equal("ACK", r.Code);
        }

        [Fact]
        public void The_last_occurrence_of_a_field_wins()
        {
            Assert.Equal(2UL, Decode("08 01 08 02").TimestampMs);
            // An embedded message is replaced, not merged (the reference does the same).
            DeviceResponse r = Decode("10 02 22 03 0a 01 61 22 02 10 05");
            Assert.Equal("", r.Heartbeat.DeviceId);
            Assert.Equal(5, r.Heartbeat.BatteryPercentage);
        }

        [Theory]
        [InlineData("0a 01 78 10 01")]          // timestamp as bytes
        [InlineData("09 00 00 00 00 00 00 00 00")]   // timestamp as fixed64
        [InlineData("15 01 00 00 00")]          // code as fixed32
        [InlineData("12 01 01")]                // code as bytes
        [InlineData("10 01 18 01")]             // ack_payload as a varint
        [InlineData("10 02 20 05")]             // heartbeat_payload as a varint
        [InlineData("10 01 1a 05 0d 01 00 00 00")]   // ack.success as fixed32
        [InlineData("10 02 22 02 08 05")]       // device_id as a varint
        [InlineData("10 02 22 03 12 01 05")]    // battery as bytes
        [InlineData("10 02 22 05 1d 01 00 00 00")]   // rssi as fixed32
        public void Wrong_wire_types_for_known_fields_are_rejected(string hex)
        {
            Assert.Contains("wrong wire type", Rejects(hex).Message);
        }

        [Theory]
        [InlineData("0b")]                      // field 1, start group (wire type 3)
        [InlineData("0c")]                      // field 1, end group (wire type 4)
        [InlineData("0b 08 01 0c")]
        [InlineData("10 01 1b 08 01 1c")]
        [InlineData("0e")]                      // wire type 6
        [InlineData("0f")]                      // wire type 7
        [InlineData("10 02 22 01 0b")]          // a group inside the heartbeat payload
        public void Groups_and_reserved_wire_types_are_rejected(string hex)
        {
            Assert.Contains("wire type", Rejects(hex).Message);
        }

        [Theory]
        [InlineData("08")]                      // key without a value
        [InlineData("08 80")]                   // varint cut short
        [InlineData("1a")]                      // length missing
        [InlineData("1a 05 08 01")]             // payload shorter than its length
        [InlineData("1a ff ff ff ff 0f")]       // length past the end (2^32 - 1)
        [InlineData("1a ff ff ff ff ff ff ff ff 7f")]   // length near 2^63
        [InlineData("1a ff ff ff ff ff ff ff ff ff 01")]   // length 2^64 - 1
        [InlineData("1a 80 80 80 80 80 80 80 80 80 02")]   // length 2^64 (past 64 bits)
        [InlineData("09 01 02 03")]             // fixed64 cut short
        [InlineData("0d 01 02")]                // fixed32 cut short
        [InlineData("10 02 22 02 0a 05")]       // device_id past the end of the payload
        public void Truncated_input_and_fields_past_the_end_are_rejected(string hex)
        {
            Rejects(hex);
        }

        [Fact]
        public void Every_prefix_of_a_heartbeat_decodes_or_is_rejected_like_the_reference()
        {
            byte[] full = Wearable.Heartbeat(1767225600123, "wz-07");
            for (int n = 0; n <= full.Length; n++)
            {
                byte[] prefix = full.Take(n).ToArray();
                Assert.Equal(ReferenceDecoder.Decode(prefix), Canonical.Decode(prefix));
            }
        }

        [Fact]
        public void Device_id_utf8_handling()
        {
            Assert.Equal("wär 🚧", Id(Encoding.UTF8.GetBytes("wär 🚧")));
            Assert.Equal("﻿bom", Id(new byte[] { 0xef, 0xbb, 0xbf, 0x62, 0x6f, 0x6d }));      // a BOM is kept
            Assert.Equal("a�b", Id(new byte[] { 0x61, 0xff, 0x62 }));                          // invalid byte: U+FFFD
            Assert.Equal("a�", Id(new byte[] { 0x61, 0xe2, 0x9c }));                           // cut-off sequence
            Assert.Equal("\0", Id(new byte[] { 0 }));
            Assert.Equal(new string('x', 4000), Id(Enumerable.Repeat((byte)'x', 4000).ToArray()));

            static string Id(byte[] raw)
            {
                byte[] hb = new Proto().Bytes(1, raw).ToArray();
                return HalowWire.DecodeDeviceResponse(new Proto().Varint(2, 2).Bytes(4, hb).ToArray()).Heartbeat.DeviceId;
            }
        }

        [Fact]
        public void Large_inputs_decode_or_fail_quickly()
        {
            var clock = System.Diagnostics.Stopwatch.StartNew();
            Rejects(Enumerable.Repeat((byte)0x80, 1 << 20).ToArray());                  // 1 MiB of continuation bytes
            byte[] many = Enumerable.Range(0, 200000).SelectMany(i => new byte[] { 0x08, (byte)(i & 0x7F) }).ToArray();
            Assert.Equal((ulong)(199999 & 0x7F), HalowWire.DecodeDeviceResponse(many).TimestampMs);
            byte[] bigField = new Proto().Bytes(15, new byte[1 << 20]).Varint(2, 1).ToArray();
            Assert.Equal("ACK", HalowWire.DecodeDeviceResponse(bigField).Code);
            Assert.True(clock.Elapsed < TimeSpan.FromSeconds(10), "took " + clock.Elapsed);
        }
    }
}
