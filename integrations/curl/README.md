# curl

For scripts and quick tests in gateway mode. curl cannot send UDP multicast, so there is no direct mode here; use [Python](../python/), [JavaScript](../javascript/) or [C#](../csharp/) for that. Read [the integrations overview](../README.md) first.

> Every wearable on the HaLowLink network vibrates and sounds when an alert goes out. Warn the people nearby first.

## alert.sh

```bash
export HALOW_URL=http://192.0.2.10:8080 HALOW_TOKEN=<token>
./alert.sh "zone-3 intrusion"                        # prints the gateway's send record
HALOW_FORMAT=proto HALOW_BURST_SECONDS=1 ./alert.sh  # protobuf format, repeated for 1 s
```

`alert.sh` builds the JSON body safely from the label and passes the token to curl on a file descriptor, so other users of the machine cannot read it from the process list. It prints a hint for `401`, `429`, `500` and the other errors, and exits with `0` on success, `1` when the alert failed and `2` for bad settings. It needs bash and curl 7.55 or newer.

## One-liners

```bash
# Is the gateway ready to send? 200 with "ok": true, or 503 with the reason in "error".
curl -sS --noproxy '*' "$HALOW_URL/health"

# Which wearables have answered: ACK counts, battery, RSSI, last seen.
curl -sS --noproxy '*' "$HALOW_URL/wearables"

# One alert (the token shows in the process list this way; alert.sh avoids that).
curl -sS --noproxy '*' -X POST "$HALOW_URL/alert" -H "Authorization: Bearer $HALOW_TOKEN" \
     -H 'Content-Type: application/json' -d '{"label": "zone-3"}'
```

With `jq` installed, `curl -sS --noproxy '*' "$HALOW_URL/wearables" | jq '.devices[] | {ip, acks, device_id, battery_percentage, rssi}'` gives one line per wearable.

## Windows PowerShell

These commands are not run in CI.

```powershell
$env:HALOW_URL = "http://192.0.2.10:8080"; $env:HALOW_TOKEN = "<token>"
Invoke-RestMethod "$env:HALOW_URL/health"
Invoke-RestMethod "$env:HALOW_URL/wearables"
Invoke-RestMethod -Method Post "$env:HALOW_URL/alert" -ContentType "application/json" `
  -Headers @{ Authorization = "Bearer $env:HALOW_TOKEN" } -Body '{"label": "zone-3"}'
```

`Invoke-RestMethod` throws on `503`. Use `Invoke-WebRequest -SkipHttpErrorCheck` (PowerShell 7) to see the body.
