#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ClearObject
"""Raise one alert from your own code, through the gateway (HTTP) or directly (UDP).

WARNING: every wearable on the HaLowLink network vibrates and sounds. Warn people first.

Gateway mode (any machine that can reach the gateway):
    HALOW_URL=http://<gateway>:8080 HALOW_TOKEN=<token> python3 example.py

Direct mode (this machine is plugged into the HaLowLink, address 192.168.12.50):
    HALOW_EGRESS=192.168.12.50 python3 example.py --direct

Codec self-test (sends nothing):
    python3 example.py --selftest

Exit code: 0 the alert was sent and a wearable ACKed it; 3 the alert was sent but no ACK
came within HALOW_ACK_WAIT seconds (default 15; a wearable ACKs only after its alarm
sequence ends); 1 the alert failed; 2 bad settings.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import halow_client as hc  # noqa: E402


def ack_wait_s() -> float:
    """HALOW_ACK_WAIT in seconds (default 15), checked before anything is sent."""
    raw = os.environ.get("HALOW_ACK_WAIT", "").strip() or "15"
    try:
        s = float(raw)
    except ValueError:
        s = -1.0
    if not 0 <= s <= 3600:  # also rejects nan and inf
        raise ValueError(f'HALOW_ACK_WAIT="{raw}" must be a number of seconds (0 to 3600)')
    return s


def via_gateway(wait_s: float) -> int:
    client = hc.GatewayClient()          # HALOW_URL, HALOW_TOKEN
    health = client.health()
    print("gateway health:", json.dumps(health))
    if not health.get("ok"):
        print(f"the gateway cannot send right now: {health.get('error')}", file=sys.stderr)
        return 1
    before = hc.total_acks(client.wearables())

    record = client.alert(label="python example")      # the wearables alert now
    print("alert sent:", json.dumps(record))

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        seen = client.wearables()
        if hc.total_acks(seen) > before:
            for d in seen["devices"]:
                print(f"wearable {d['ip']}: acks={d['acks']} heartbeats={d['heartbeats']} "
                      f"device_id={d.get('device_id', '?')} battery={d.get('battery_percentage', '?')}%")
            print("ACK received")
            return 0
        time.sleep(0.5)
    print(f"no ACK within {wait_s:g} s; check GET /wearables again later", file=sys.stderr)
    return 3


def direct(wait_s: float) -> int:
    egress = os.environ.get("HALOW_EGRESS", "").strip() or hc.DEFAULT_EGRESS
    group = os.environ.get("HALOW_GROUP", "").strip() or hc.GROUP
    port = hc._env_port("HALOW_PORT", hc.PORT)
    rport = hc._env_port("HALOW_RESPONSE_PORT", hc.RESPONSE_PORT)

    with hc.ResponseListener(rport, allow_prefix=hc.subnet_prefix(egress)) as replies:
        # 1. Ask the wearables to report in. Nothing buzzes.
        req_ts = hc.send_heartbeat_request(egress, group, port)
        hb = replies.wait_for("HEARTBEAT", timeout=3.0)
        if hb:
            h = hb["heartbeat"]
            rtt = f", round trip {hb['received_ms'] - req_ts} ms" if hb["timestamp_ms"] == req_ts else ""
            print(f"HEARTBEAT from {hb['ip']}: device_id={h['device_id']} battery={h['battery_percentage']}% "
                  f"rssi={h['rssi']} dBm{rtt}")
        else:
            print("no heartbeat within 3 s (continuing)")

        # 2. The alert. Every wearable on the network alerts.
        record = hc.send_direct(egress, os.environ.get("HALOW_FORMAT", "") or "raw8", group, port)
        print("alert sent:", json.dumps(record))
        ack = replies.wait_for("ACK", timeout=wait_s, after_ms=record["ts_ms"])
        if ack:
            print(f"ACK from {ack['ip']}: wearable timestamp {ack['timestamp_ms']} ms")
            return 0
    print(f"no ACK within {wait_s:g} s", file=sys.stderr)
    return 3


def main(argv) -> int:
    try:
        if "--selftest" in argv:
            return hc.selftest()
        wait_s = ack_wait_s()
        return direct(wait_s) if "--direct" in argv else via_gateway(wait_s)
    except hc.HalowError as e:
        print(f"example: {e}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        print(f"example: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
