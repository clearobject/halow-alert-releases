#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ClearObject
"""Check before a session that the gateway can send, and which wearables answer.

Nothing buzzes: this only reads GET /health and GET /wearables, which need no token.
The gateway asks every wearable to report in every 15 s (a heartbeat request; the
gateway's HALOW_HEARTBEAT setting, 0 = never), so a wearable that is on and in range
answers within about 15 s. Keep --wait longer than that interval. This script waits for
those answers by watching the gateway's counts change, so it does not depend on either
machine's clock. It prints one line per wearable the gateway has heard from since it started.

    HALOW_URL=http://<gateway>:8080 python3 wearable_status.py
    HALOW_URL=http://<gateway>:8080 python3 wearable_status.py --expect 3 --wait 30

Exit code: 0 the gateway can send and at least --expect wearables (default 1) answered
within --wait seconds (default 20); 3 fewer answered; 1 the gateway cannot send or was
not reached; 2 bad settings.
"""
import argparse
import os
import sys
import time

# The kit and a clone keep halow_client.py one folder up; an installed package works too.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import halow_client as hc  # noqa: E402


def _replies(body):
    """{wearable address: ACKs + heartbeats} from a GET /wearables body."""
    return {d["ip"]: int(d.get("acks", 0)) + int(d.get("heartbeats", 0)) for d in body.get("devices", [])}


def _safe(text) -> str:
    """Printable ASCII only: a device ID comes from the network, and must neither send control
    characters to the terminal nor fail on a console that cannot print other characters."""
    return "".join(ch if ch.isascii() and ch.isprintable() else "?" for ch in str(text))


def check(client, expect=1, wait_s=20.0, poll_s=1.0, out=print) -> int:
    """Print the gateway's state and each wearable's; return the exit code (0, 1 or 3)."""
    health = client.health()
    if not health.get("ok"):
        out(f"gateway at {client.url} cannot send: {health.get('error')}")
        return 1
    out(f"gateway {health.get('version', '?')} at {client.url}: sends from {health.get('egress')}")

    before = _replies(client.wearables())
    deadline = time.monotonic() + wait_s
    while True:
        body = client.wearables()
        now = _replies(body)
        # A count changes only when a reply arrives, or when the gateway restarts: then the
        # counts start again from 0 and a wearable is listed again only after it replies.
        answered = {ip for ip, n in now.items() if n != before.get(ip, 0)}
        # Stop when every known wearable has answered (and at least --expect), or at the deadline.
        if (len(answered) >= expect and answered >= set(now)) or time.monotonic() >= deadline:
            break
        time.sleep(poll_s)

    out(f"{'WEARABLE':<17}{'ADDRESS':<17}{'BATTERY':>8}{'SIGNAL':>10}{'ACKS':>6}  STATUS")
    for d in sorted(body.get("devices", []), key=lambda d: d["ip"]):
        battery = d.get("battery_percentage")
        rssi = d.get("rssi")
        out(f"{_safe(d.get('device_id', '?')):<17}{_safe(d['ip']):<17}"
            f"{'?' if battery is None else str(battery) + '%':>8}"
            f"{'?' if rssi is None else str(rssi) + ' dBm':>10}"
            f"{d.get('acks', 0):>6}  {'answered' if d['ip'] in answered else 'silent'}")
    out(f"{len(answered)} of {len(now)} wearable(s) answered within {wait_s:g} s")
    if len(answered) < expect:
        out(f"want at least {expect}: is each wearable on, charged and joined to the HaLowLink? Is the "
            "gateway's HALOW_HEARTBEAT shorter than --wait, and not 0? (docs/TROUBLESHOOTING.md)")
        return 3
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Show whether the gateway can send and which wearables answer.")
    p.add_argument("--expect", type=int, default=1, metavar="N",
                   help="how many wearables must answer (default 1; with 0, only the gateway decides the exit code)")
    p.add_argument("--wait", type=float, default=20.0, metavar="SECONDS",
                   help="how long to wait for the wearables to answer (default 20); keep it longer "
                        "than the gateway's heartbeat interval, HALOW_HEARTBEAT (15 s by default)")
    a = p.parse_args(argv)
    if a.expect < 0 or not 0 <= a.wait <= 600:  # also rejects nan
        print("wearable_status: --expect must be 0 or more, --wait 0 to 600 seconds", file=sys.stderr)
        return 2
    try:
        client = hc.GatewayClient()  # HALOW_URL; no token needed
        return check(client, a.expect, a.wait)
    except ValueError as e:  # HALOW_URL is not an http:// or https:// URL
        print(f"wearable_status: {e}", file=sys.stderr)
        return 2
    except hc.HalowError as e:  # not reached, or GET /wearables failed
        print(f"wearable_status: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
