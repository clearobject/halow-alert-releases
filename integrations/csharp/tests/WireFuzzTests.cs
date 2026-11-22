// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// A seeded, deterministic fuzz test of HalowWire.DecodeDeviceResponse: random bytes, mutations
// of valid replies and random field soups. The decoder may only throw FormatException, must
// finish quickly, and must agree with ReferenceDecoder, a line-by-line port of the Python
// reference client's decode_device_response (integrations/python/halow_client.py).

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Numerics;
using System.Text;
using System.Threading.Tasks;
using Xunit;

namespace ClearObject.Halow.Tests
{
    /// <summary>
    /// The Python reference decoder, with arbitrary-precision integers as Python has them.
    /// Returns the reply as one line (see Canonical) or "null" when the reference raises ValueError.
    /// </summary>
    internal static class ReferenceDecoder
    {
        private static readonly BigInteger U32 = 0xFFFFFFFFUL;
        private static readonly BigInteger U64 = ulong.MaxValue;

        private sealed class Rejected : Exception
        {
        }

        public static string Decode(byte[] buf)
        {
            try
            {
                return DecodeOrThrow(buf);
            }
            catch (Rejected)
            {
                return "null";
            }
        }

        private static BigInteger ReadVarint(byte[] buf, ref int i)
        {
            int shift = 0;
            BigInteger result = BigInteger.Zero;
            while (true)
            {
                if (shift >= 64) throw new Rejected();
                if (i >= buf.Length) throw new Rejected();
                byte b = buf[i];
                i++;
                result |= new BigInteger(b & 0x7F) << shift;
                if ((b & 0x80) == 0) return result;
                shift += 7;
            }
        }

        private static Dictionary<BigInteger, object> Fields(byte[] buf)
        {
            var output = new Dictionary<BigInteger, object>();
            int i = 0;
            while (i < buf.Length)
            {
                BigInteger key = ReadVarint(buf, ref i);
                BigInteger tag = key >> 3;
                int wire = (int)(key & 7);
                object v;
                if (wire == 0)
                {
                    v = ReadVarint(buf, ref i);
                }
                else if (wire == 1 || wire == 2 || wire == 5)
                {
                    BigInteger ln = wire == 2 ? ReadVarint(buf, ref i) : wire == 1 ? 8 : 4;
                    if (i + ln > buf.Length) throw new Rejected();
                    v = buf.Skip(i).Take((int)ln).ToArray();
                    i += (int)ln;
                }
                else
                {
                    throw new Rejected();
                }
                output[tag] = v;
            }
            return output;
        }

        private static BigInteger GetInt(Dictionary<BigInteger, object> f, int tag)
        {
            if (!f.TryGetValue(tag, out object v)) return BigInteger.Zero;
            if (v is BigInteger n) return n;
            throw new Rejected();
        }

        private static byte[] GetBytes(Dictionary<BigInteger, object> f, int tag)
        {
            if (!f.TryGetValue(tag, out object v)) return new byte[0];
            if (v is byte[] b) return b;
            throw new Rejected();
        }

        private static BigInteger Int32(BigInteger v)
        {
            v &= U32;
            return v >= (BigInteger.One << 31) ? v - (BigInteger.One << 32) : v;
        }

        private static string DecodeOrThrow(byte[] buf)
        {
            var f = Fields(buf);
            BigInteger code = GetInt(f, 2);
            string name = code == 0 ? "UNKNOWN" : code == 1 ? "ACK" : code == 2 ? "HEARTBEAT" : code.ToString(CultureInfo.InvariantCulture);
            var sb = new StringBuilder();
            sb.Append("ts=").Append((GetInt(f, 1) & U64).ToString(CultureInfo.InvariantCulture)).Append(" code=").Append(name);
            if (f.ContainsKey(3))
                sb.Append(" ack=").Append(GetInt(Fields(GetBytes(f, 3)), 1) != 0 ? "true" : "false");
            if (f.ContainsKey(4))
            {
                var h = Fields(GetBytes(f, 4));
                sb.Append(" hb=").Append(new UTF8Encoding(false, false).GetString(GetBytes(h, 1)))
                  .Append('|').Append(Int32(GetInt(h, 2)).ToString(CultureInfo.InvariantCulture))
                  .Append('|').Append(Int32(GetInt(h, 3)).ToString(CultureInfo.InvariantCulture))
                  .Append('|').Append((GetInt(h, 4) & U32).ToString(CultureInfo.InvariantCulture))
                  .Append('|').Append((GetInt(h, 5) & U32).ToString(CultureInfo.InvariantCulture))
                  .Append('|').Append((GetInt(h, 6) & U32).ToString(CultureInfo.InvariantCulture));
            }
            return sb.ToString();
        }
    }

    public sealed class WireFuzzTests
    {
        private const int Seed = 1234567;
        private static readonly byte[] Interesting = { 0x00, 0x01, 0x02, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x10, 0x12, 0x18,
            0x1a, 0x1b, 0x20, 0x22, 0x28, 0x30, 0x7f, 0x80, 0x81, 0xfe, 0xff };

        private static IEnumerable<byte[]> Seeds()
        {
            foreach ((byte[] bytes, string expected) in VectorTests.DeviceResponses())
                if (expected != "null") yield return bytes;
            yield return Wearable.Ack(1767225600163);
            yield return Wearable.Heartbeat(1767225600123, "wz-07");
            yield return Wearable.Heartbeat(ulong.MaxValue, "wär 🚧", int.MinValue, -1, uint.MaxValue, 7, 9);
        }

        private static byte Pick(Random rng)
        {
            return rng.Next(3) == 0 ? (byte)rng.Next(256) : Interesting[rng.Next(Interesting.Length)];
        }

        private static byte[] RandomBytes(Random rng, bool biased)
        {
            var b = new byte[rng.Next(0, 49)];
            for (int i = 0; i < b.Length; i++) b[i] = biased ? Pick(rng) : (byte)rng.Next(256);
            return b;
        }

        private static byte[] Mutate(Random rng, byte[] original, byte[] other)
        {
            var b = new List<byte>(original);
            int ops = rng.Next(1, 4);
            for (int k = 0; k < ops; k++)
            {
                int at = b.Count == 0 ? 0 : rng.Next(b.Count);
                switch (rng.Next(8))
                {
                    case 0:
                        if (b.Count > 0) b[at] ^= (byte)(1 << rng.Next(8));
                        break;
                    case 1:
                        if (b.Count > 0) b[at] = Pick(rng);
                        break;
                    case 2:
                        b.Insert(at, Pick(rng));
                        break;
                    case 3:
                        if (b.Count > 0) b.RemoveAt(at);
                        break;
                    case 4:
                        if (b.Count > 0) b.RemoveRange(at, b.Count - at);    // truncate
                        break;
                    case 5:
                        if (b.Count > 0) b.InsertRange(at, b.Skip(at).Take(rng.Next(1, 6)).ToList());    // duplicate a slice
                        break;
                    case 6:
                        b.AddRange(RandomBytes(rng, true).Take(rng.Next(1, 8)));
                        break;
                    default:
                        b.InsertRange(at, other.Take(rng.Next(other.Length + 1)));    // splice in another reply
                        break;
                }
            }
            return b.ToArray();
        }

        private static byte[] FieldSoup(Random rng, int depth)
        {
            var p = new Proto();
            int n = rng.Next(0, 7);
            for (int k = 0; k < n; k++)
            {
                ulong tag = (ulong)(rng.Next(5) == 0 ? rng.Next(0, 1 << 20) : rng.Next(0, 8));
                int wire = rng.Next(10) == 0 ? rng.Next(3, 8) : new[] { 0, 0, 0, 1, 2, 2, 2, 5 }[rng.Next(8)];
                switch (wire)
                {
                    case 0:
                        ulong v = rng.Next(4) switch
                        {
                            0 => (ulong)rng.Next(0, 4),
                            1 => ulong.MaxValue - (ulong)rng.Next(0, 3),
                            2 => (ulong)(long)-rng.Next(1, 200),
                            _ => ((ulong)(uint)rng.Next() << 32) | (uint)rng.Next(),
                        };
                        p.Varint(tag, v);
                        break;
                    case 1:
                        p.Fixed64(tag, (ulong)rng.NextInt64());
                        break;
                    case 2:
                        p.Bytes(tag, depth < 2 && rng.Next(2) == 0 ? FieldSoup(rng, depth + 1) : RandomBytes(rng, rng.Next(2) == 0));
                        break;
                    case 5:
                        p.Fixed32(tag, (uint)rng.Next());
                        break;
                    default:
                        p.Key(tag, wire);
                        break;
                }
            }
            return p.ToArray();
        }

        private static List<byte[]> Cases()
        {
            var rng = new Random(Seed);
            byte[][] seeds = Seeds().ToArray();
            var cases = new List<byte[]>();
            for (int i = 0; i < 1500; i++) cases.Add(RandomBytes(rng, false));
            for (int i = 0; i < 1500; i++) cases.Add(RandomBytes(rng, true));
            for (int i = 0; i < 2500; i++) cases.Add(Mutate(rng, seeds[rng.Next(seeds.Length)], seeds[rng.Next(seeds.Length)]));
            for (int i = 0; i < 1500; i++) cases.Add(FieldSoup(rng, 0));
            return cases;
        }

        [Fact]
        public async Task Decoder_only_throws_FormatException_and_agrees_with_the_reference()
        {
            List<byte[]> cases = Cases();
            Assert.Equal(7000, cases.Count);
            int current = -1;
            var failures = new List<string>();
            int rejected = 0;

            var run = Task.Run(() =>
            {
                for (int i = 0; i < cases.Count; i++)
                {
                    current = i;
                    byte[] input = cases[i];
                    string got;
                    try
                    {
                        got = Canonical.Of(HalowWire.DecodeDeviceResponse(input));
                    }
                    catch (FormatException)
                    {
                        got = "null";
                        rejected++;
                    }
                    catch (Exception e)
                    {
                        failures.Add("case " + i + " " + Hex.Of(input) + ": " + e.GetType().Name + ": " + e.Message);
                        continue;
                    }
                    string want = ReferenceDecoder.Decode(input);
                    if (got != want) failures.Add("case " + i + " " + Hex.Of(input) + ": got " + got + ", reference " + want);
                }
            });
            if (await Task.WhenAny(run, Task.Delay(TimeSpan.FromSeconds(30))) != run)
                Assert.Fail("the decoder did not finish within 30 s; stuck on case " + current + ": " + Hex.Of(cases[Math.Max(current, 0)]));
            await run;

            Assert.True(failures.Count == 0, failures.Count + " failure(s), seed " + Seed + ":\n" + string.Join("\n", failures.Take(20)));
            // The mix must exercise both outcomes, or the generator is broken.
            Assert.InRange(rejected, 1000, cases.Count - 500);
        }

        [Fact]
        public void The_cases_are_deterministic()
        {
            List<byte[]> a = Cases(), b = Cases();
            Assert.Equal(a.Select(Hex.Of), b.Select(Hex.Of));
        }
    }
}
