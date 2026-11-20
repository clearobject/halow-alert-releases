# C# client (.NET and Unity)

`HalowClient.cs` is one file with no packages. It uses `HttpClient` for gateway mode, and `UdpClient` and `TcpListener` for direct mode. It keeps to C# 8 and the .NET Standard 2.1 API, so the same file builds for .NET Standard 2.1 (Unity, and .NET Core 3.0 or newer); CI tests it on .NET 8, and the example needs the .NET 8 SDK or newer. Read [the integrations overview](../README.md) first: it explains gateway mode and direct mode, the settings and the errors.

> Every wearable on the HaLowLink network vibrates and sounds when an alert goes out. Warn the people nearby first.

| File | Purpose |
|---|---|
| `HalowClient.cs` | The client (namespace `ClearObject.Halow`). Copy it into your project. |
| `Example.cs`, `HalowExample.csproj` | A console example for .NET 8 or newer. It uses `System.Text.Json` to read the gateway's JSON; the client itself needs no JSON library. |

## Gateway mode

The methods return the gateway's JSON text. Parse it with the JSON library you already use: `System.Text.Json`, Newtonsoft.Json, or Unity's `JsonUtility` for flat objects.

```csharp
using ClearObject.Halow;

var gateway = new HalowGatewayClient("http://192.0.2.10:8080", token);   // null, null: HALOW_URL, HALOW_TOKEN
string health = await gateway.HealthAsync();          // {"ok": true, ...}; a 503 body is returned too
try
{
    string record = await gateway.AlertAsync("zone-3 intrusion");     // optional: format: "proto", burstSeconds: 1
}
catch (HalowException e) when (e.StatusCode == 429)
{
    // one alert per 0.5 s: retry shortly
}
string wearables = await gateway.WearablesAsync();    // {"devices": [...], "recent": [...]}
```

Keep one `HalowGatewayClient` for the life of your program, and dispose of it at the end. A failed call throws `HalowException`: `StatusCode` is the HTTP status (`401`, `429`, `500`, ...) and `Body` is the response, or `StatusCode` is null when the gateway was not reached or timed out (10 s by default; the `timeout` constructor argument changes it).

## Direct mode

```csharp
using ClearObject.Halow;

string egress = "192.168.12.50";                       // this machine's address on the HaLowLink
using (var replies = new HalowResponseListener(5001, HalowDirect.SubnetPrefix(egress)).Start())
{
    HalowDirect.SendHeartbeatRequest(egress);          // no buzz; the wearables report in
    DirectAlertResult sent = await HalowDirect.SendAlertAsync(egress);   // raw8, from egress, TTL 4
    DeviceResponse ack = await replies.WaitForAsync("ACK", TimeSpan.FromSeconds(15), sent.TimestampMs);
    Console.WriteLine(ack == null ? "no ACK yet" : "ACK from " + ack.Ip);
}
```

| Member | Does |
|---|---|
| `HalowDirect.SendAlertAsync(egress, format = "raw8", group = "239.255.0.10", port = 5000, ttl = 4, burstSeconds = 0, burstIntervalMs = 100)` | Sends one alert (`raw8`, `proto` or `both`). `burstSeconds` resends the same bytes for that long (at most 5 s). Throws `InvalidOperationException` when `egress` is not an address of this machine. |
| `HalowDirect.SendHeartbeatRequest(egress, group = "239.255.0.10", port = 5000, ttl = 4)` | Asks every wearable to report in. Nothing buzzes. Returns the request's timestamp (ms), which each heartbeat echoes. |
| `new HalowResponseListener(port = 5001, allowPrefix = "")`, `.Start()`, `.Stop()` | Listens for replies. `Responses` holds them, `WaitForAsync(code, timeout, afterMs)` waits for one, and the `Received` event fires for each one. Throws `InvalidOperationException` when the port is taken. |
| `HalowWire.EncodeRaw8`, `EncodeGatewayMessage`, `DecodeDeviceResponse` | The wire formats. |

## Run the example (.NET 8 SDK or newer)

```bash
cd integrations/csharp
HALOW_URL=http://192.0.2.10:8080 HALOW_TOKEN=<token> dotnet run      # gateway mode
HALOW_EGRESS=192.168.12.50 dotnet run -- --direct                     # direct mode
dotnet run -- --selftest ../testdata/vectors.json                     # codec check, sends nothing
```

On Windows PowerShell, set the variables first with `$env:HALOW_URL = "http://192.0.2.10:8080"` and `$env:HALOW_TOKEN = "<token>"`.

## Unity

CI compiles `HalowClient.cs` against .NET Standard 2.1 with C# 8. It has not been run inside the Unity Editor yet.

1. Copy `HalowClient.cs` into your `Assets` folder. Unity 2021.2 or newer is needed, with **Player Settings → Other Settings → Api Compatibility Level** set to **.NET Standard 2.1** (the default).
2. Call it from a `MonoBehaviour`. `await` in a `MonoBehaviour` method resumes on Unity's main thread, so you can touch scene objects after it:

   ```csharp
   using ClearObject.Halow;
   using UnityEngine;

   public class AlertOnIntrusion : MonoBehaviour
   {
       [SerializeField] string gatewayUrl = "http://192.0.2.10:8080";
       [SerializeField] string token = "";      // load it from a file or environment variable; don't commit it
       HalowGatewayClient gateway;

       void Awake() { gateway = new HalowGatewayClient(gatewayUrl, token); }
       void OnDestroy() { gateway.Dispose(); }

       public async void RaiseAlert(string zone)
       {
           try { Debug.Log(await gateway.AlertAsync(zone)); }
           catch (HalowException e) { Debug.LogWarning(e.Message); }
       }
   }
   ```

3. Mind these platform limits:
   - The `Received` event of `HalowResponseListener` fires on a thread-pool thread. Queue the reply and handle it in `Update()`, or poll `Responses`, before you touch Unity objects.
   - **WebGL** runs in a browser: it has no sockets, so direct mode is impossible, and it cannot call the gateway either, because the gateway sends no CORS headers (its answer to the browser's `OPTIONS` preflight is `501`). Send the alert from a small backend that holds the token, served from the same origin as the WebGL page or sending CORS headers itself, and call that backend from Unity with `UnityWebRequest`. Never put the token in a WebGL build.
   - **Android and iOS** block plain-HTTP requests by default. Allow HTTP to the gateway (Player Settings → Allow downloads over HTTP, and an App Transport Security exception on iOS). iOS 14 and newer also asks the user for local network access, and sending multicast (direct mode) needs Apple's multicast networking entitlement.
   - **Desktop players and the Editor** may trigger the operating system's firewall prompt the first time they listen on TCP 5001. Allow it, or no ACKs arrive.
