"""100 ms telemetry collector (``iw dev apX-wlan1 station dump``) → ``telemetry.parquet``.

Runs as a thread inside the emulation process. Besides writing the Parquet file at the end of the
run it keeps a live ring buffer that the tool server uses for ``get_ap_airtime_utilization``,
``get_station_stats`` and the agents' state snapshot.

Columns: ts, ap, sta, signal_dbm, tx_bitrate_mbps, tx_retries, tx_bytes,
rx_bytes, airtime_est (+ tx_packets for retry ratios).
"""
from __future__ import annotations

import collections
import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

LOG = logging.getLogger("telemetry")

_STA_RE = re.compile(r"^Station ([0-9a-f:]{17})", re.I)
_NUM_RE = re.compile(r"(-?\d+(?:\.\d+)?)")


def parse_station_dump(text: str) -> dict[str, dict[str, float]]:
    """Parse ``iw dev <if> station dump`` into {mac: {field: value}}."""
    out: dict[str, dict[str, float]] = {}
    cur: dict[str, float] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        m = _STA_RE.match(line)
        if m:
            cur = {}
            out[m.group(1).lower()] = cur
            continue
        if cur is None or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        num = _NUM_RE.search(val)
        if not num:
            continue
        v = float(num.group(1))
        if key == "signal":
            cur["signal_dbm"] = v
        elif key == "tx bitrate":
            cur["tx_bitrate_mbps"] = v
        elif key == "rx bitrate":
            cur["rx_bitrate_mbps"] = v
        elif key == "tx retries":
            cur["tx_retries"] = v
        elif key == "tx failed":
            cur["tx_failed"] = v
        elif key == "tx bytes":
            cur["tx_bytes"] = v
        elif key == "rx bytes":
            cur["rx_bytes"] = v
        elif key == "tx packets":
            cur["tx_packets"] = v
        elif key == "connected time":
            cur["connected_s"] = v
        elif key == "inactive time":
            cur["inactive_ms"] = v
    return out


def station_dump(intf: str) -> dict[str, dict[str, float]]:
    try:
        r = subprocess.run(["iw", "dev", intf, "station", "dump"], capture_output=True, text=True, timeout=2)
        return parse_station_dump(r.stdout)
    except (subprocess.SubprocessError, OSError) as exc:
        LOG.debug("station dump %s failed: %s", intf, exc)
        return {}


class Telemetry(threading.Thread):
    """Periodic collector. ``mac2sta`` maps STA MAC → name, ``aps`` maps AP name → wlan intf."""

    def __init__(self, aps: dict[str, str], mac2sta: dict[str, str], period_ms: int,
                 out_file: Path, stop: threading.Event, t_start: float, keep_s: float = 60.0):
        super().__init__(name="telemetry", daemon=True)
        self.aps = aps
        self.mac2sta = mac2sta
        self.period = period_ms / 1000.0
        self.out_file = out_file
        self.stop_ev = stop
        self.t_start = t_start
        self.rows: list[tuple[Any, ...]] = []
        self.sample_ts: list[float] = []
        self.lock = threading.Lock()
        maxlen = int(keep_s / self.period) + 1
        self.ring: collections.deque[dict[str, Any]] = collections.deque(maxlen=maxlen)
        self.prev: dict[tuple[str, str], tuple[float, dict[str, float]]] = {}
        self.cpu_s = 0.0
        self.wall_s = 0.0

    # ---------------------------------------------------------------- live accessors
    def latest(self) -> dict[str, Any]:
        with self.lock:
            return self.ring[-1] if self.ring else {"ts": 0.0, "aps": {}, "assoc": {}}

    def window(self, seconds: float) -> list[dict[str, Any]]:
        with self.lock:
            if not self.ring:
                return []
            t_end = self.ring[-1]["ts"]
            return [s for s in self.ring if s["ts"] > t_end - seconds]

    def airtime(self, ap: str, seconds: float = 1.0) -> float:
        win = self.window(seconds)
        vals = [s["aps"].get(ap, {}).get("airtime", 0.0) for s in win]
        return sum(vals) / len(vals) if vals else 0.0

    # ---------------------------------------------------------------- collector
    def sample_once(self) -> dict[str, Any]:
        ts = time.time()
        snap: dict[str, Any] = {"ts": ts, "aps": {}, "assoc": {}}
        for ap, intf in self.aps.items():
            dump = station_dump(intf)
            air = 0.0
            stas: dict[str, dict[str, float]] = {}
            for mac, st in dump.items():
                sta = self.mac2sta.get(mac)
                if sta is None:
                    continue
                key = (ap, sta)
                a_est = 0.0
                if key in self.prev:
                    t_prev, st_prev = self.prev[key]
                    dt = max(ts - t_prev, 1e-3)
                    dbytes = max(0.0, st.get("tx_bytes", 0) - st_prev.get("tx_bytes", 0))
                    rate = max(st.get("tx_bitrate_mbps", 1.0), 1.0)
                    a_est = min(1.0, dbytes * 8 / (rate * 1e6) / dt)
                self.prev[key] = (ts, st)
                air += a_est
                st = dict(st, airtime_est=a_est)
                stas[sta] = st
                snap["assoc"][sta] = ap
                self.rows.append((ts, ap, sta, st.get("signal_dbm"), st.get("tx_bitrate_mbps"),
                                  st.get("tx_retries"), st.get("tx_bytes"), st.get("rx_bytes"),
                                  a_est, st.get("tx_packets")))
            snap["aps"][ap] = {"airtime": min(1.0, air), "stas": stas}
        with self.lock:
            self.ring.append(snap)
        return snap

    def run(self) -> None:
        delay = self.t_start - time.time()
        if delay > 0 and self.stop_ev.wait(delay):
            return
        nxt = time.time()
        c0, w0 = time.thread_time(), time.time()
        while not self.stop_ev.is_set():
            self.sample_ts.append(time.time())
            self.sample_once()
            nxt += self.period
            sl = nxt - time.time()
            if sl > 0:
                self.stop_ev.wait(sl)
            else:
                nxt = time.time()  # overrun: re-anchor
        self.cpu_s = time.thread_time() - c0
        self.wall_s = time.time() - w0

    def stats(self) -> dict[str, float]:
        import numpy as np

        d = np.diff(np.array(self.sample_ts)) * 1000.0 if len(self.sample_ts) > 2 else np.array([float("nan")])
        return {"telemetry_period_ms": self.period * 1000.0,
                "cpu_pct_collector": 100.0 * self.cpu_s / self.wall_s if self.wall_s else float("nan"),
                "sample_period_mean_ms": float(np.nanmean(d)),
                "sample_period_jitter_ms": float(np.nanstd(d)),
                "samples": len(self.sample_ts)}

    def write(self) -> None:
        import pandas as pd

        cols = ["ts", "ap", "sta", "signal_dbm", "tx_bitrate_mbps", "tx_retries", "tx_bytes", "rx_bytes",
                "airtime_est", "tx_packets"]
        df = pd.DataFrame(self.rows, columns=cols)
        self.out_file.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(self.out_file, index=False)
        LOG.info("telemetry: %d rows -> %s", len(df), self.out_file)
