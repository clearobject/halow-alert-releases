# Troubleshooting

Start with [`doctor`](#doctor): it checks the whole path from the gateway to the HaLowLink in a few seconds and sends nothing to the wearables. Then look up your symptom below. If you are still stuck, [open an issue](https://github.com/clearobject/halow-alert-releases/issues/new/choose) with the [information listed at the end](#what-to-include-in-a-bug-report).

## Contents

- [doctor](#doctor)
- [Diagnostic commands in the image](#diagnostic-commands-in-the-image)
- [Network and HaLowLink](#network-and-halowlink)
- [The gateway service (setup A)](#the-gateway-service-setup-a)
- [Alerts and ACKs](#alerts-and-acks)
- [Direct mode](#direct-mode)
- [What to include in a bug report](#what-to-include-in-a-bug-report)

## doctor

On the Linux gateway:

```bash
docker run --rm --network host ghcr.io/clearobject/halow-alert:0.1.0 doctor
```

A healthy gateway prints:

```text
halow_alert 0.1.0  group 239.255.0.10:5000  router 192.168.12.1
  [PASS] egress address - 192.168.12.50
  [PASS] route to HaLowLink - 192.168.12.1 is reached from 192.168.12.50
  [PASS] HaLowLink reachable - https://192.168.12.1 answers
  [PASS] multicast socket - IP_MULTICAST_IF 192.168.12.50, TTL 4
  [PASS] TCP 5001 free for wearable responses
doctor sent nothing to the wearables.
```

The exit code is 0 when every row passes, and 1 otherwise.

| Row | What it checks | When it fails |
|---|---|---|
| `egress address` | The gateway has an address on the HaLowLink network (`192.168.12.x`), or the address in `--iface` / `HALOW_IFACE` | The HaLowLink is unplugged or still booting (allow 60 s), the [network setup](NETWORK.md#set-the-address) has not run, or a VPN owns the route. `doctor` stops after this row. |
| `route to HaLowLink` | Traffic to `192.168.12.1` leaves from the HaLowLink subnet, not through a VPN or another network | See [VPNs](NETWORK.md#vpns). Alerts may still go out, but ACKs and heartbeats will not come back. |
| `HaLowLink reachable` | `https://192.168.12.1` (TCP 443) answers within 3 s | The cable is in the WAN port instead of LAN, the address is wrong, or a VPN blocks local traffic. Try opening `https://192.168.12.1` in a browser. |
| `multicast socket` | A multicast socket can be pinned to the egress address | Rare: the address disappeared during the check. Run `doctor` again. |
| `TCP 5001 free for wearable responses` | Nothing else holds the wearable reply port | **Expected while the gateway service is running**, because the service holds the port. Otherwise, find the other program: `sudo ss -ltnp 'sport = :5001'`. |

A `[NOTE] the standard sender address is 192.168.12.50` line means the gateway sends from another `192.168.12.x` address, usually one it got by DHCP. Give it the static address (see [why](NETWORK.md#why-a-static-address-without-a-gateway)).

A failing first row looks like this:

```text
  [FAIL] egress address - this machine reaches 192.168.12.1 via 192.0.2.23, which is not on the HaLowLink subnet; plug in the HaLowLink (USB-C or LAN port) or pass --iface
```

`doctor` needs Docker Engine on Linux. On macOS and Windows (direct mode), use the [manual checks](NETWORK.md#check-the-setup).

## Diagnostic commands in the image

The image has a few more commands besides the gateway service (`serve`, the default). Run them the same way as `doctor`:

```bash
docker run --rm --network host ghcr.io/clearobject/halow-alert:0.1.0 <command> [options]
```

| Command | What it does |
|---|---|
| `doctor` | Checks the path to the HaLowLink. Sends nothing. |
| `wearables` | Asks the wearables to report in (a heartbeat request every 5 s, **no alarm**) for `--seconds` (default 20), then prints them as JSON: address, `device_id`, battery, signal strength. Exit code 0 if any wearable answered, 1 if none did. |
| `send` | **Alerts every wearable now** (they vibrate and sound), then waits `--wait-acks` seconds (default 3) and prints the ACKs. Options: `-n` count, `--interval` seconds between alerts, `--format raw8\|proto\|both`, `--burst-seconds` (at most 5). |
| `listen` | Prints every datagram sent to the alert group, including the gateway's own alerts and heartbeat requests. Add `-it` to `docker run`, and stop it with Ctrl+C. |
| `--version` | Prints the version. |

Every command also takes `--iface`, `--router`, `--group`, `--port`, `--response-port` and `--log`, which work like the [service settings](HTTP-API.md#service-settings) of the same name.

`wearables` and `send` listen on TCP 5001 for replies, which the running gateway service already holds. Either stop the service first (`docker compose stop`, then `docker compose start` afterwards), or use the service's `GET /wearables` instead. While the service runs, `send` still sends but prints `not listening for ACKs: TCP 5001 is taken`, and `wearables` exits with `cannot listen for replies`.

Example output of `send --wait-acks 30` with one wearable:

```text
[1/1] alert sent: raw8 -> 239.255.0.10:5000 from 192.168.12.50 (1 datagram(s))
waiting 30 s for wearable ACKs on TCP 5001 ...
  ACK       from 192.168.12.143
1 ACK(s) from 1 wearable(s)
```

A wearable ACKs after its alarm ends or is silenced, so the default 3 s wait can be too short. Use `--wait-acks 30` when testing.

## Network and HaLowLink

| Symptom | What to check |
|---|---|
| No new network adapter appears after plugging in the HaLowLink | Wait 60 s: it has to boot. Try another cable, another port, or a powered USB hub. On Linux, run `ip -br link` (the adapter may be `enx…`, `usb0`, `eth1` or `enp…u…`) and `sudo dmesg \| tail`. On macOS or Windows, use the LAN port instead ([details](NETWORK.md#connect-the-halowlink)). |
| `setup-network-linux.sh usb`: `no USB network interface found` | The HaLowLink is not plugged in by USB-C, or has not finished booting. |
| `setup-network-linux.sh usb`: `several USB network interfaces` | Another USB network adapter or a dock is connected. Find the HaLowLink's adapter with `ip -br link` (it appears when you plug the HaLowLink in), then run `sudo ./setup-network-linux.sh eth <interface>`. |
| `setup-network-linux.sh`: `carries this machine's default route` | You picked your uplink, not the HaLowLink. Choose the other interface with `eth <interface>`. |
| `setup-network-linux.sh`: `NetworkManager (nmcli) not found` | Use the [commands without NetworkManager](NETWORK.md#linux-without-networkmanager). |
| `WARN: 192.168.12.1 does not answer ping yet` | The HaLowLink is still booting, or the cable is in the WAN port. Wait, then run `./setup-network-linux.sh show` and `ping 192.168.12.1`. |
| Internet stops working on the gateway after connecting the HaLowLink | The HaLowLink interface got an address by DHCP, with a default route through the HaLowLink. Use the [static address with no gateway](NETWORK.md#set-the-address). |
| Works, then stops after the HaLowLink is re-plugged | On Linux, `./setup-network-linux.sh show` should list the `halow` profile as active again, with `192.168.12.50`. The gateway service recovers by itself on the next request. |
| Machines on your building network get `192.168.12.x` addresses | The HaLowLink's LAN port is connected to that network. Unplug it at once: [never do that](NETWORK.md#connect-the-halowlink). |

## The gateway service (setup A)

| Symptom | What to check |
|---|---|
| `docker compose up`: `required variable HALOW_TOKEN is missing a value` | Create `.env` next to `docker-compose.yml` with `HALOW_TOKEN=<your token>` ([quick start](../README.md#quick-start-a-alert-gateway)). |
| The container keeps restarting. `docker compose logs` says `refusing to serve on 0.0.0.0 without a token` | `HALOW_TOKEN` is empty. Set it in `.env`. |
| … says `refusing to serve with the documentation's placeholder token CHANGE-ME` | Choose your own long random token. |
| … says `[Errno 98] Address already in use` | Another program holds TCP 5001 or the API port (8080): a second copy of the gateway, or `send` or `wearables` running in another container. Find it: `sudo ss -ltnp '( sport = :5001 or sport = :8080 )'`. |
| … says `must be 'host:port' or 'port'` or `HALOW_FORMAT must be raw8, proto or both` | Fix that setting in `.env` or `docker-compose.yml` ([settings](HTTP-API.md#service-settings)). |
| `docker compose ps` shows `unhealthy`, and `GET /health` returns `503` | The `error` field says why. Usually the HaLowLink is unplugged or booting, or its address is not set. The service keeps running and becomes healthy again by itself once the address is back; Docker does not restart it. |
| `GET /health` is `503` right after `docker compose up`, on a host where `doctor` passes | Read the `error` field. If it says `192.168.12.50 is not an address of this machine` and `doctor` printed `[NOTE] the standard sender address is 192.168.12.50`, the gateway has another `192.168.12.x` address (usually from DHCP), but the Compose kit sends from `192.168.12.50`. Run the [network setup](NETWORK.md#set-the-address) (`sudo ./setup-network-linux.sh usb`), then check that `doctor` shows `egress address - 192.168.12.50`. If it already does, check that `docker compose` uses Docker Engine as root, the same daemon `doctor` ran on: `docker context show` must not print `desktop-linux`, and `docker info --format '{{.SecurityOptions}}'` must not contain `rootless`. Docker Desktop and rootless Docker cannot see the HaLowLink interface. |
| `docker compose logs` shows `log write failed: [Errno 13] Permission denied: '/data/halow-alert.jsonl'` | The container's user does not own `./data`. Alerts still go out. The Compose kit runs as uid and gid 1000; if `id -u` or `id -g` print something else, put `HALOW_UID=<uid>` and `HALOW_GID=<gid>` in `.env` (see [`.env.example`](../.env.example)) and run `docker compose up -d` again. |
| `curl http://<gateway>:8080/...` works on the gateway itself but times out from another machine | The gateway's firewall blocks TCP 8080 ([firewall](NETWORK.md#firewall)), or `HALOW_HTTP` is set to `127.0.0.1:...`. |
| `401 missing or wrong Bearer token`, although the token looks right | The header must be exactly `Authorization: Bearer <token>`, with a capital B and one space. Check for quotes, spaces or a line break around the token in your client's configuration. |
| Frequent `429` | Your software sends more than one alert per 0.5 s. Debounce on your side: one alert already alarms every wearable. |
| `POST /alert` returns `500` | `GET /health` gives the same reason. Usually the HaLowLink address is missing. |
| `docker pull` or `docker compose up` cannot find the image, or says `no matching manifest` | Check the image name and tag. The gateway needs internet access once to download it. The image is for 64-bit Linux (`amd64`, `arm64`): on a Raspberry Pi, install a 64-bit OS. |

## Alerts and ACKs

| Symptom | What to check |
|---|---|
| `200` from `POST /alert`, but no buzz and no ACK | Is the wearable on and charged? Is it joined? On the HaLowLink admin page (`https://192.168.12.1`), the **Connected Devices** card on the Home page should list it. Run `wearables` (or watch `GET /wearables`): if heartbeats come back, the network path works. If the wearable still does not alarm, contact ClearObject. |
| The wearable buzzes, but `GET /wearables` shows no new ACK | Wait longer: the ACK comes after the alarm ends or is silenced. Otherwise the gateway's firewall blocks TCP 5001 from `192.168.12.0/24`, or a VPN captures the replies (`doctor` fails its route check). |
| `GET /wearables` is empty, even after 30 s | No heartbeats are coming back. Same checks as above. Also make sure `HALOW_HEARTBEAT` is not `0`. |
| Two alarms for one alert, or none for a burst | Duplicate handling for the `raw8` format and for `both` is not yet verified on hardware. Report what you see in an issue. |
| ACKs arrive, but the latencies look wrong | The wearable's clock may differ from the gateway's. Use the ACK arrival time (`last_ack_ms`), not the wearable's own timestamp, unless the clocks are synchronised. |

## Direct mode

| Symptom | What to check |
|---|---|
| Sending fails with `Cannot assign requested address` (Linux), `Can't assign requested address` (macOS), or `WinError 10049` (Windows) | `192.168.12.50` is not on the machine right now: the HaLowLink is unplugged or booting, or the address is not set ([network setup](NETWORK.md#set-the-address)). |
| macOS: sending fails with `No route to host` | Allow your terminal app or IDE under **System Settings → Privacy & Security → Local Network**, then restart it. Also check for a VPN. |
| The send succeeds, but no wearable alarms | The datagram may have left on another interface. Set `IP_MULTICAST_IF` to `192.168.12.50`, as in the [integrations](../integrations/README.md); Windows in particular picks an interface by itself otherwise. Check that the payload is 8 bytes, little-endian. |
| No ACKs reach your listener | Start the listener on TCP 5001 before sending. Allow TCP 5001 from `192.168.12.0/24` in the firewall. Wait long enough: ACKs come after the alarm ends. Only one program can listen on 5001. Check for a VPN. |
| Windows: no ACKs, although the firewall rule exists | Clicking **Cancel** on the Windows Firewall prompt creates a **block** rule for your program, and block rules win. List them with `Get-NetFirewallRule -Direction Inbound -Action Block \| Where-Object DisplayName -like "*python*"` (or your program's name) and remove them, or change them to allow. |
| It works with the stand-in wearable but not with the real one | The stand-in proves your code and your local setup. For the real wearable, check that it is joined (Connected Devices card), and that your machine reaches the HaLowLink: `ping 192.168.12.1` and the [route checks](NETWORK.md#check-the-setup). |

## What to include in a bug report

**Never paste tokens, passwords, HaLow network passphrases, your `.env` file or the output of `docker compose config`.** The gateway's own logs and `doctor` never contain the token.

Issues are public. Replace host names, user names, alert labels you do not want published and every address outside `192.168.12.x` with `<removed>`, or send full logs to your ClearObject contact instead.

On the gateway (setup A):

```bash
docker run --rm ghcr.io/clearobject/halow-alert:0.1.0 --version
docker run --rm --network host ghcr.io/clearobject/halow-alert:0.1.0 doctor
docker compose ps
docker compose logs --tail 200
curl -s http://127.0.0.1:8080/health
curl -s http://127.0.0.1:8080/wearables
ip -br -4 addr | grep -F 192.168.12.
ip route get 192.168.12.1
docker version
uname -srm
```

Also say:

- setup A or B, and the operating system of each machine involved;
- how the HaLowLink is connected (USB-C or LAN port);
- for direct mode, which integration you use, or a minimal code sample;
- what you did, what you expected, and what happened, with times;
- the HaLowLink firmware version, from its admin page, if you can.

The [bug report form](https://github.com/clearobject/halow-alert-releases/issues/new/choose) asks for all of this.
