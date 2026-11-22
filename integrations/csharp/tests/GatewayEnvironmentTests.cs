// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ClearObject
// Settings that come from process-wide state: HALOW_URL, HALOW_TOKEN and HALOW_EGRESS, and
// the proxy settings that HalowGatewayClient ignores. These tests change environment
// variables and HttpClient.DefaultProxy, so they run alone, after the parallel tests.

using System;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Threading.Tasks;
using Xunit;

namespace ClearObject.Halow.Tests
{
    [Collection(ProcessWideState.Name)]
    public sealed class GatewayEnvironmentTests : IDisposable
    {
        private readonly StubGateway stub = new StubGateway();
        private readonly EnvironmentScope env = new EnvironmentScope();

        public GatewayEnvironmentTests()
        {
            env.Set("HALOW_URL", null).Set("HALOW_TOKEN", null).Set("HALOW_EGRESS", null);
        }

        public void Dispose()
        {
            env.Dispose();
            stub.Dispose();
        }

        [Fact]
        public async Task Url_and_token_come_from_the_environment_when_not_given()
        {
            env.Set("HALOW_URL", "  " + stub.Url + "/  ").Set("HALOW_TOKEN", "  " + StubGateway.Token + "  ");
            foreach (string url in new[] { null, "", "   " })
            {
                using (var c = new HalowGatewayClient(url))
                {
                    Assert.Equal(stub.Url, c.BaseUrl);
                    Assert.Equal(StubGateway.AlertRecord, await c.AlertAsync());
                }
                Assert.Equal("Bearer " + StubGateway.Token, stub.Requests.Last().Header("Authorization"));
            }
        }

        [Fact]
        public async Task An_explicit_empty_token_overrides_HALOW_TOKEN()
        {
            env.Set("HALOW_TOKEN", StubGateway.Token);
            using (var c = new HalowGatewayClient(stub.Url, ""))
            {
                HalowException e = await Assert.ThrowsAsync<HalowException>(() => c.AlertAsync());
                Assert.Equal(401, e.StatusCode);
            }
            Assert.Null(stub.Requests.Last().Header("Authorization"));

            using (var c = new HalowGatewayClient(stub.Url, "explicit"))
                await Assert.ThrowsAsync<HalowException>(() => c.AlertAsync());
            Assert.Equal("Bearer explicit", stub.Requests.Last().Header("Authorization"));
        }

        [Fact]
        public void Without_HALOW_URL_the_default_is_the_local_gateway()
        {
            using (var c = new HalowGatewayClient())
                Assert.Equal(HalowDefaults.DefaultUrl, c.BaseUrl);       // nothing is sent
            env.Set("HALOW_URL", "   ");
            using (var c = new HalowGatewayClient())
                Assert.Equal(HalowDefaults.DefaultUrl, c.BaseUrl);
        }

        [Fact]
        public void A_bad_HALOW_URL_is_refused()
        {
            env.Set("HALOW_URL", "ftp://192.0.2.10");
            Assert.Throws<ArgumentException>(() => new HalowGatewayClient());
        }

        [Fact]
        public async Task Proxy_settings_are_ignored()
        {
            // A proxy that answers every request itself: a request that goes through it gets 418.
            using (var proxy = new StubGateway(r => StubReply.Json(418, "{\"error\": \"via the proxy\"}")))
            {
                IWebProxy saved = HttpClient.DefaultProxy;
                env.Set("HTTP_PROXY", proxy.Url).Set("http_proxy", proxy.Url).Set("ALL_PROXY", proxy.Url).Set("all_proxy", proxy.Url)
                   .Set("NO_PROXY", null).Set("no_proxy", null);
                try
                {
                    HttpClient.DefaultProxy = new WebProxy(proxy.Url) { BypassProxyOnLocal = false };

                    // Control: a plain HttpClient does use this proxy for the gateway's address.
                    using (var plain = new HttpClient())
                    using (HttpResponseMessage res = await plain.GetAsync(stub.Url + "/health"))
                        Assert.Equal(418, (int)res.StatusCode);
                    Assert.Single(proxy.Requests);

                    using (var c = new HalowGatewayClient(stub.Url, StubGateway.Token))
                    {
                        Assert.Equal(StubGateway.HealthOk, await c.HealthAsync());
                        Assert.Equal(StubGateway.AlertRecord, await c.AlertAsync("direct"));
                    }
                    Assert.Single(proxy.Requests);
                    Assert.Equal(2, stub.Requests.Count);
                }
                finally
                {
                    HttpClient.DefaultProxy = saved;
                }
            }
        }
    }
}
