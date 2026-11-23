#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ClearObject
"""Raise wearable alerts from inside your own program, without ever blocking it.

WARNING: every wearable on the HaLowLink network vibrates and sounds. Warn people first.

WearableAlerter sends each alert through the gateway on a background thread, so a
simulator or control loop never waits for the network. When the gateway's rate limit
(one alert per 0.5 s) answers 429, it tries once more; any other failure goes to a
callback, never into your loop as an exception. Copy the class into your project,
together with _STOP, print_result and the imports of this file.

The demo watches a made-up distance reading and alerts once each time it drops below 5 m:

    HALOW_URL=http://<gateway>:8080 HALOW_TOKEN=<token> python3 alert_from_your_app.py

Exit code: 0 every alert was sent; 1 an alert failed; 2 bad settings.
"""
import argparse
import os
import queue
import sys
import threading
import time

# The kit and a clone keep halow_client.py one folder up; an installed package works too.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import halow_client as hc  # noqa: E402

_STOP = object()


class WearableAlerter:
    """Sends alerts through the gateway on a background thread.

    alert(label) returns at once. After each alert, on_result(label, record, error) runs on
    the background thread: record is the gateway's send record, or None when the alert
    failed, and error is then the exception that says why (usually a halow_client.HalowError).
    An alert after close() is not sent; on_result reports that at once, on the caller's thread.
    """

    RETRY_AFTER_S = 0.6  # the gateway accepts one alert every 0.5 s

    def __init__(self, client=None, on_result=None):
        self.client = client or hc.GatewayClient()  # HALOW_URL and HALOW_TOKEN
        self.on_result = on_result or print_result
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="wearable-alerter", daemon=True)
        self._thread.start()

    def alert(self, label=None):
        """Queue one alert and return at once."""
        with self._lock:
            if not self._closed:
                self._queue.put(label)
                return
        self._report(label, None, RuntimeError("the alerter is closed: alert not sent"))

    def close(self, timeout=30.0):
        """Send the alerts still queued, then stop the background thread."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._queue.put(_STOP)
        self._thread.join(timeout)

    def _run(self):
        while True:
            label = self._queue.get()
            if label is _STOP:
                return
            try:
                record, error = self._send(label), None
            except Exception as e:  # any failure goes to the callback; the thread keeps running
                record, error = None, e
            self._report(label, record, error)

    def _report(self, label, record, error):
        try:
            self.on_result(label, record, error)
        except Exception as e:
            print(f"alert_from_your_app: on_result failed: {e!r}", file=sys.stderr)

    def _send(self, label):
        try:
            return self.client.alert(label=label)
        except hc.HalowError as e:
            if e.status != 429:  # 401 (token), 500 (HaLowLink), not reached, ...: retrying will not help
                raise
        time.sleep(self.RETRY_AFTER_S)  # 429: the previous alert was less than 0.5 s ago
        return self.client.alert(label=label)


def print_result(label, record, error):
    if error is None:
        print(f"alert {label!r} sent (gateway time {record.get('ts_ms')} ms)", flush=True)
    else:
        print(f"alert {label!r} FAILED: {error}", file=sys.stderr, flush=True)


# Metres to the nearest vehicle, one reading per step: two approaches.
DEMO_READINGS = (12.0, 9.5, 7.2, 4.8, 4.1, 3.6, 6.3, 8.0, 4.4, 9.9)


def watch_distance(alerter, readings, step_s, limit_m=5.0):
    """Alert once each time the distance drops below limit_m, not on every reading while it
    stays below: the wearables alarm for several seconds, and repeats only queue up."""
    inside = False
    for metres in readings:
        if metres < limit_m and not inside:
            alerter.alert(label=f"vehicle within {limit_m:g} m")
        inside = metres < limit_m
        time.sleep(step_s)  # stands for the rest of your loop's work


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Alert the wearables from a program loop (demo).")
    p.add_argument("--step", type=float, default=0.5, metavar="SECONDS",
                   help="time between two distance readings (default 0.5)")
    a = p.parse_args(argv)
    if not 0 <= a.step <= 60:  # also rejects nan
        print("alert_from_your_app: --step must be 0 to 60 seconds", file=sys.stderr)
        return 2

    failed = []

    def on_result(label, record, error):
        print_result(label, record, error)
        if error is not None:
            failed.append(error)

    try:
        alerter = WearableAlerter(on_result=on_result)
    except ValueError as e:  # HALOW_URL is not an http:// or https:// URL
        print(f"alert_from_your_app: {e}", file=sys.stderr)
        return 2
    watch_distance(alerter, DEMO_READINGS, a.step)
    alerter.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
