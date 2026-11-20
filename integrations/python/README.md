# Python client

`halow_client` raises ClearObject ClearZone wearable alerts from Python 3.8 or newer, using only the standard library. It is one module, `halow_client.py`. Install it as the package `clearobject-halow-client`, which also installs the `halow-client` command, or copy the file into your project. Read [the integrations overview](https://github.com/clearobject/halow-alert-releases/blob/v0.1.0/integrations/README.md) first: it explains gateway mode and direct mode, the settings and the errors.

> Every wearable on the HaLowLink network vibrates and sounds when an alert goes out. Warn the people nearby first.

## Install

Use one of the three ways below. Each gives you the same module, and none of them installs another package.

Install the package (ways 1 and 2) into a virtual environment: the system Python of recent Debian and Ubuntu releases, and Homebrew's Python on macOS, refuse `pip install` outside one. In your project folder:

```bash
python3 -m venv .venv          # Windows: py -m venv .venv
. .venv/bin/activate           # Windows PowerShell: .venv\Scripts\Activate.ps1
```

While the environment is active, `python` and the `halow-client` command are the environment's. Without activating it (for example when PowerShell refuses to run `Activate.ps1`), call them by path instead: `.venv/bin/python` and `.venv/bin/halow-client` (Windows: `.venv\Scripts\python` and `.venv\Scripts\halow-client`). On Debian and Ubuntu, if `venv` says that `ensurepip` is not available, install the `python3-venv` package first.

1. **The wheel from the GitHub release.** No git needed:

   ```bash
   python -m pip install https://github.com/clearobject/halow-alert-releases/releases/download/v0.1.0/clearobject_halow_client-0.1.0-py3-none-any.whl
   ```

   The [release](https://github.com/clearobject/halow-alert-releases/releases/tag/v0.1.0) also has the sdist, `clearobject_halow_client-0.1.0.tar.gz`, and a `.sha256` file for each. To check the wheel before you install it, download it and its `.sha256` file into one folder and compare them there:

   ```bash
   sha256sum -c clearobject_halow_client-0.1.0-py3-none-any.whl.sha256          # Linux
   shasum -a 256 -c clearobject_halow_client-0.1.0-py3-none-any.whl.sha256      # macOS
   ```

   On Windows, in PowerShell, this prints `True` when the file is intact:

   ```powershell
   (Get-FileHash .\clearobject_halow_client-0.1.0-py3-none-any.whl -Algorithm SHA256).Hash -eq (Get-Content .\clearobject_halow_client-0.1.0-py3-none-any.whl.sha256).Split(' ')[0]
   ```

   Then install the checked file: `python -m pip install ./clearobject_halow_client-0.1.0-py3-none-any.whl`. In a `requirements.txt`, write `clearobject-halow-client @ <the wheel URL above>`.

2. **From this repository with git**, at a release tag:

   ```bash
   python -m pip install "git+https://github.com/clearobject/halow-alert-releases@v0.1.0#subdirectory=integrations/python"
   ```

   pip builds the package on your machine and downloads the build tool, `setuptools`, from PyPI for that. From a clone of this repository or the unpacked kit, `python -m pip install ./integrations/python` does the same.

3. **Copy `halow_client.py`** next to your code, or put this folder on `PYTHONPATH`. Nothing else is needed. The command line is then `python3 halow_client.py <command>`.

**PyPI:** ClearObject does not publish this package on PyPI. Install it only in one of the ways above. A package of the same name on PyPI is ClearObject's only if its PyPI page lists this repository, https://github.com/clearobject/halow-alert-releases, under **Verified details**.

Check the installation with `halow-client --version`, which prints the version, `0.1.0`. On Windows outside a virtual environment, if the `halow-client` command is not found because Python's `Scripts` folder is not on `PATH`, use `py -m halow_client` instead.

## Gateway mode

```python
from halow_client import GatewayClient, HalowError

gw = GatewayClient("http://192.0.2.10:8080")    # or GatewayClient(): HALOW_URL; the token comes from HALOW_TOKEN
print(gw.health())                              # {"ok": true, "egress": "192.168.12.50", ...}; a 503 body is returned too
try:
    record = gw.alert(label="zone-3 intrusion") # optional: fmt="proto", burst_seconds=1.0
except HalowError as e:
    if e.status == 429:
        ...                                     # one alert per 0.5 s: retry shortly
    raise
print(gw.wearables()["devices"])                # ACKs, battery, RSSI per wearable
```

| Call | Returns |
|---|---|
| `GatewayClient(url=None, token=None, timeout=10.0)` | A client. `url` defaults to `HALOW_URL`, then `http://127.0.0.1:8080`; `token` defaults to `HALOW_TOKEN`. |
| `.health()` | The `GET /health` body for `200` and `503`. Check `["ok"]`. |
| `.wearables()` | The `GET /wearables` body: `{"devices": [...], "recent": [...]}`. |
| `.alert(label=None, fmt=None, burst_seconds=None)` | The gateway's send record. Raises `HalowError` with `.status` (`401`, `429`, `500`, ...) and `.body`, or with `.status` `None` when the gateway was not reached. |
| `total_acks(wearables)` | The number of ACKs in a `wearables()` body. |

## Direct mode

```python
from halow_client import ResponseListener, send_direct, send_heartbeat_request, subnet_prefix

egress = "192.168.12.50"                        # this machine's address on the HaLowLink
with ResponseListener(5001, allow_prefix=subnet_prefix(egress)) as replies:
    send_heartbeat_request(egress)              # no buzz; the wearables report in
    record = send_direct(egress)                # the alert: raw8, from egress, TTL 4
    ack = replies.wait_for("ACK", timeout=15, after_ms=record["ts_ms"])
    print(ack)                                  # {"code": "ACK", "ip": "192.168.12.x", "timestamp_ms": ..., ...} or None
```

A program that runs for a long time can keep the listener open, handle each reply as it arrives, and ask the wearables to report in every 15 s, as the gateway does:

```python
import time

from halow_client import ResponseListener, send_heartbeat_request, subnet_prefix

def on_reply(reply):                            # runs on the listener's thread
    if reply["code"] == "HEARTBEAT":
        hb = reply["heartbeat"]
        print(f"{reply['ip']} {hb['device_id']}: battery {hb['battery_percentage']}%, {hb['rssi']} dBm")
    else:
        print(f"ACK from {reply['ip']}")

egress = "192.168.12.50"
listener = ResponseListener(5001, allow_prefix=subnet_prefix(egress), on_response=on_reply).start()
try:
    while True:
        send_heartbeat_request(egress)          # nothing buzzes
        time.sleep(15)
finally:
    listener.stop()
```

| Call | Does |
|---|---|
| `send_direct(egress="", fmt="raw8", group="239.255.0.10", port=5000, ttl=4, burst_seconds=0.0, burst_interval_ms=100)` | Sends one alert (`raw8`, `proto` or `both`) and returns a record like the gateway's. `burst_seconds` resends the same bytes for that long (at most 5 s). Raises `OSError` when `egress` is not an address of this machine. |
| `send_heartbeat_request(egress="", group="239.255.0.10", port=5000, ttl=4)` | Asks every wearable to report in. Nothing buzzes. Returns the request's timestamp (ms), which each heartbeat echoes. |
| `ResponseListener(port=5001, bind="0.0.0.0", allow_prefix="", on_response=None)` | Listens for replies in a background thread. Use `.start()` and `.stop()`, or `with`. `.responses` holds every reply, and `.wait_for(code, timeout, after_ms)` waits for one. `on_response(reply)` runs on the listener's thread. |
| `encode_raw8(t)`, `encode_gateway_message(ms, command)`, `decode_device_response(data)` | The wire formats. |

Of the environment variables, these functions read only `HALOW_EGRESS`, and only when you pass no `egress`. The alert group and port and the reply port are arguments with the HaLowLink's values as defaults: `HALOW_GROUP`, `HALOW_PORT` and `HALOW_RESPONSE_PORT` change only the `halow-client` command and the example. To test against the stand-in wearable, pass its settings yourself, as in [Try it without hardware](#try-it-without-hardware).

## Command line

Installed, the command is `halow-client` (or `python -m halow_client`). With the copied file, it is `python3 halow_client.py`, with the same commands and options.

```bash
export HALOW_URL=http://192.0.2.10:8080 HALOW_TOKEN=<token>
halow-client health
halow-client wearables
halow-client alert --label "zone-3"            # --format proto, --burst-seconds 1
halow-client direct-heartbeat --egress 192.168.12.50 --seconds 5
halow-client direct-alert --egress 192.168.12.50 --wait-acks 15
halow-client selftest integrations/testdata/vectors.json
```

`halow-client <command> --help` lists every option. The exit code is `0` on success, `1` when the gateway call failed, and `2` for bad settings. `direct-alert` and `direct-heartbeat` exit `0` once the request is sent, whether or not replies arrive: check the printed count (`N ACK(s) from M wearable(s)`). Unlike the examples, they never exit `3`. `--wait-acks` and `--seconds` default to 5 s; a wearable ACKs only after its alarm ends, so use `--wait-acks 15` as above.

`selftest` checks the codec against `integrations/testdata/vectors.json` and sends nothing. The package does not include that file, so pass its path from a clone of this repository or the unpacked kit. In a clone or the unpacked kit, `python3 integrations/python/halow_client.py selftest` finds it by itself. A copy of `halow_client.py` elsewhere, like the installed package, needs the path: `python3 halow_client.py selftest path/to/integrations/testdata/vectors.json`.

## Try it without hardware

The stand-in wearable, `tools/fake_wearable.py`, answers like a wearable, on loopback and on ports that nothing else uses. It is not part of the package: it is in the kit zip on the [release page](https://github.com/clearobject/halow-alert-releases/releases/tag/v0.1.0) and in [this repository](https://github.com/clearobject/halow-alert-releases/blob/v0.1.0/tools/fake_wearable.py). On Linux, enable loopback multicast first, as [Try it without hardware](https://github.com/clearobject/halow-alert-releases/blob/v0.1.0/integrations/README.md#try-it-without-hardware) in the integrations overview describes; macOS needs no setup. On Windows, start the stand-in wearable in a window of its own, as the overview shows.

From the folder that holds `tools/` (the unpacked kit or a clone), with the virtual environment active:

```bash
python3 tools/fake_wearable.py --iface 127.0.0.1 --group 239.255.77.2 --port 15010 --response-port 15011 --seconds 0 &
halow-client direct-heartbeat --egress 127.0.0.1 --group 239.255.77.2 --port 15010 --response-port 15011 --seconds 2
halow-client direct-alert --egress 127.0.0.1 --group 239.255.77.2 --port 15010 --response-port 15011 --wait-acks 5
kill %1                                         # stop the stand-in wearable
```

The two commands end with `1 HEARTBEAT(s) from 1 wearable(s)` and `1 ACK(s) from 1 wearable(s)`. In your own code, pass the same settings as arguments, because the library does not read `HALOW_GROUP`, `HALOW_PORT` or `HALOW_RESPONSE_PORT`:

```python
from halow_client import ResponseListener, send_direct, subnet_prefix

egress, group, port, rport = "127.0.0.1", "239.255.77.2", 15010, 15011   # the stand-in wearable's settings
with ResponseListener(rport, allow_prefix=subnet_prefix(egress)) as replies:
    record = send_direct(egress, group=group, port=port)
    print(replies.wait_for("ACK", timeout=15, after_ms=record["ts_ms"]))
```

## Example

[`example.py`](https://github.com/clearobject/halow-alert-releases/blob/v0.1.0/integrations/python/example.py) is in this folder of the repository and the kit. It is not part of the package.

```bash
HALOW_URL=http://192.0.2.10:8080 HALOW_TOKEN=<token> python3 example.py      # gateway mode
HALOW_EGRESS=192.168.12.50 python3 example.py --direct                         # direct mode
python3 example.py --selftest                                                  # codec check, sends nothing
```

## Tests

The tests use the standard library's `unittest` and never reach a wearable: gateway mode runs against an HTTP stub on `127.0.0.1`, and direct mode against the stand-in wearable, [`tools/fake_wearable.py`](https://github.com/clearobject/halow-alert-releases/blob/v0.1.0/tools/fake_wearable.py), on loopback. From the repository root:

```bash
python3 -m unittest discover -s integrations/python/tests -v                          # the source file
HALOW_TEST_INSTALLED=1 python -m unittest discover -s integrations/python/tests -v    # the package installed in the active virtual environment
```

The direct-mode tests need loopback multicast. On Linux, enable it first, as [Try it without hardware](https://github.com/clearobject/halow-alert-releases/blob/v0.1.0/integrations/README.md#try-it-without-hardware) describes; without it they are skipped. `HALOW_TEST_REQUIRE_MULTICAST=1` turns that skip into a failure.

## Version

`halow_client.__version__` is the package's version. It equals the version of the halow-alert release, and of the gateway image `ghcr.io/clearobject/halow-alert`, that it was released with. The [changelog](https://github.com/clearobject/halow-alert-releases/blob/v0.1.0/CHANGELOG.md) lists the changes.
