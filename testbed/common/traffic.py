"""Traffic: iperf3 / irtt servers and clients, plus parsers shared by live tools and ``kpi.py``.

All traffic is downlink (h1 → STA, ``iperf3 -R``). Per-flow files:

* ``iperf/<sta>.json`` — iperf3 ``--json-stream`` (one JSON object per line; iperf3 ≥ 3.17) or,
  as a fallback, classic ``-J`` output (single JSON document written at the end). With
  json-stream a client that fails or stalls is relaunched; each attempt starts with an
  ``{"event": "ojsdn_attempt", "ts": <epoch>}`` line that anchors its (relative) intervals.
* ``irtt/<sta>.<n>.json.gz`` — irtt JSON of attempt n (telemedicine STAs; older runs:
  ``irtt/<sta>.json.gz``); ``irtt/<sta>.live.txt`` — irtt's per-packet stdout used for live OWD/IPDV.
* ``iperf/launch.csv`` — epoch of every client's first launch.
"""
from __future__ import annotations

import collections
import gzip
import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

LOG = logging.getLogger("traffic")

IPERF_BASE_PORT = 5200
IRTT_PORT = 2112
IRTT_END_MARGIN_S = 2  # irtt stops sending this long before the run ends (see irtt_client_cmd)
IRTT_STOP_GRACE_S = 6.0  # Traffic.stop() waits this long for irtt clients to finish on their own
IPERF_RCV_TIMEOUT_MS = 3000  # a client that receives nothing this long ends and is relaunched
IPERF_MIN_ATTEMPT_S = 3  # no relaunch with less than this left
SERVER_IP = "10.0.0.100"


def iperf3_supports_json_stream() -> bool:
    try:
        out = subprocess.run(["iperf3", "--help"], capture_output=True, text=True, timeout=5).stdout
        return "--json-stream" in out
    except (OSError, subprocess.SubprocessError):
        return False


# ----------------------------------------------------------------------------- command builders
def iperf_client_cmd(sta_idx: int, slice_name: str, intents: dict[str, Any], end_ts: float,
                     json_stream: bool, logfile: str, tcp_cap_mbps: float | None = None) -> str:
    """iperf3 client that runs until ``end_ts`` (epoch s).

    With json-stream it is relaunched whenever it ends early — connect failure (STA still settling,
    server busy with the previous attempt) or ``--rcv-timeout`` after a stall — so one outage costs
    only its own duration instead of the rest of the run. Each attempt is preceded by a marker line
    with its launch epoch (see ``_iter_iperf_intervals``). ``tcp_cap_mbps`` paces a TCP flow (``-b``)."""
    sl = intents["slices"][slice_name]
    tr = sl["traffic"]
    port = IPERF_BASE_PORT + sta_idx
    opts = f"-u -b {tr['rate_mbps']}M -l {tr['pkt_bytes']} -S {sl['tos']}" if tr["proto"] == "udp" \
        else f"-C {tr.get('cc', 'cubic')}" + (f" -b {tcp_cap_mbps:g}M" if tcp_cap_mbps else "")
    if not json_stream:
        dur_s = max(1, int(end_ts - time.time()))
        return f"iperf3 -c {SERVER_IP} -p {port} -R -t {dur_s} -i 0.1 {opts} -J --logfile {logfile}"
    client = (f"iperf3 -c {SERVER_IP} -p {port} -R -t $d -i 0.1 {opts} --rcv-timeout {IPERF_RCV_TIMEOUT_MS} "
              f"--connect-timeout 3000 --json-stream")
    return (f"while :; do d=$(( {int(end_ts)} - $(date +%s) )); [ $d -ge {IPERF_MIN_ATTEMPT_S} ] || break; "
            f"printf '{{\"event\":\"ojsdn_attempt\",\"ts\":%s}}\\n' $(date +%s.%N); "
            f"{client} && break; sleep 1; done > {logfile} 2>&1")


def irtt_client_cmd(end_ts: float, intents: dict[str, Any], out_prefix: str, live_txt: str) -> str:
    """irtt client that sends until IRTT_END_MARGIN_S before ``end_ts`` (epoch s).

    irtt writes its JSON only when it finishes by itself: interrupted while waiting for the last
    replies (``--wait``) it exits with "context canceled" and no file, so it must be done before the
    emulation stops at end_ts. Whenever it ends early — connect timeout while the STA is still
    settling, or a receive error such as a late duplicate open reply ("UnexpectedOpenFlag", which
    exits 0) — it is relaunched for the remaining duration; attempt n writes ``<out_prefix>.<n>.json.gz``."""
    tr = intents["slices"]["telemedicine"]["traffic"]
    client = (f"irtt client -i {tr['irtt_interval_ms']}ms -l {tr['irtt_len']} --wait=1s "
              f"--dscp={intents['slices']['telemedicine']['tos'].lower()} {SERVER_IP}:{IRTT_PORT}")
    return (f"n=0; while :; do d=$(( {int(end_ts)} - {IRTT_END_MARGIN_S} - $(date +%s) )); [ $d -ge 5 ] || break; "
            f"n=$((n+1)); {client} -o {out_prefix}.$n.json.gz -d ${{d}}s; sleep 1; done > {live_txt} 2>&1")


# ----------------------------------------------------------------------------- launcher
class Traffic:
    """Starts servers on h1 and staggered clients on the STAs (Mininet node objects)."""

    def __init__(self, cfg: dict[str, Any], intents: dict[str, Any], run_dir: Path,
                 sta_slice: dict[str, str]):
        self.cfg = cfg
        self.intents = intents
        self.run_dir = run_dir
        self.sta_slice = sta_slice
        self.json_stream = iperf3_supports_json_stream()
        self.procs: list[Any] = []
        self.irtt_procs: list[Any] = []
        self.launch: dict[str, float] = {}
        self.error: str | None = None  # set when the client launcher thread fails
        (run_dir / "iperf").mkdir(parents=True, exist_ok=True)
        (run_dir / "irtt").mkdir(parents=True, exist_ok=True)
        if not self.json_stream:
            LOG.warning("iperf3 lacks --json-stream: live slice KPIs fall back to tc/telemetry")

    def start_servers(self, h1: Any, n_sta: int) -> None:
        for i in range(1, n_sta + 1):
            h1.cmd(f"iperf3 -s -p {IPERF_BASE_PORT + i} -D")
        # -i 0: no minimum packet interval — probes bunched up behind a stall would otherwise be
        # dropped by the server ("[ShortInterval]") and counted as loss
        h1.cmd(f"irtt server -i 0 -b {SERVER_IP}:{IRTT_PORT} > {self.run_dir}/irtt/server.log 2>&1 &")
        time.sleep(1.0)

    def start_clients(self, stas: dict[str, Any], t0: float, stop: threading.Event) -> None:
        """Launch every client at t0 + U(0, 2) s (seeded) in a background thread."""
        rng = np.random.default_rng(int(self.cfg["seed"]) * 7919 + 11)
        offsets = {name: float(rng.uniform(0.0, 2.0)) for name in sorted(stas, key=lambda n: int(n[3:]))}
        dur = int(self.cfg["duration_s"])
        # optional pacing of the greedy best-effort TCP flows: every frame costs wmediumd CPU, and an
        # unpaced TCP flow sends as many as the medium accepts (see topo.medium_netlink_drops)
        cap = self.cfg.get("be_tcp_cap_mbps")

        def _run() -> None:
            for name in sorted(offsets, key=offsets.get):
                at = t0 + offsets[name]
                d = at - time.time()
                if d > 0 and stop.wait(d):
                    return
                idx = int(name[3:])
                sl = self.sta_slice[name]
                logf = self.run_dir / "iperf" / f"{name}.json"
                node = stas[name]
                self.launch[name] = time.time()
                self.procs.append(node.popen(iperf_client_cmd(idx, sl, self.intents, t0 + dur, self.json_stream,
                                                              str(logf), cap if sl == "best_effort" else None),
                                             shell=True))
                if sl == "telemedicine":
                    self.irtt_procs.append(node.popen(irtt_client_cmd(
                        t0 + dur, self.intents, str(self.run_dir / "irtt" / name),
                        str(self.run_dir / "irtt" / f"{name}.live.txt")), shell=True))
            with open(self.run_dir / "iperf" / "launch.csv", "w") as fh:
                fh.write("sta,slice,launch_ts\n")
                for n, ts in sorted(self.launch.items(), key=lambda kv: kv[1]):
                    fh.write(f"{n},{self.sta_slice[n]},{ts:.6f}\n")

        def _guarded() -> None:
            try:
                _run()
            except Exception as e:  # noqa: BLE001 - recorded; topo.py fails the run on it
                self.error = f"{type(e).__name__}: {e}"
                LOG.exception("traffic launch failed")

        threading.Thread(target=_guarded, name="traffic-launch", daemon=True).start()

    def stop(self) -> None:
        for p in self.procs:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001 - best effort
                pass
        subprocess.run(["pkill", "-INT", "-f", "iperf3 -c"], check=False)
        # an interrupted irtt writes no JSON; a late connect can push its end (send + --wait) past the
        # margin, so let the clients finish by themselves first
        deadline = time.time() + IRTT_STOP_GRACE_S
        for p in self.irtt_procs:
            try:
                p.wait(timeout=max(0.0, deadline - time.time()))
            except Exception:  # noqa: BLE001 - timeout or already gone
                pass
        subprocess.run(["pkill", "-INT", "-f", "irtt client"], check=False)


# ----------------------------------------------------------------------------- parsers
def _iter_iperf_intervals(path: Path) -> list[dict[str, Any]]:
    """Return interval ``sum`` dicts from json-stream or classic -J output.

    In json-stream files written by the relaunch loop, each ``sum`` gets ``_base``: the epoch of
    the attempt it belongs to (its ``start``/``end`` are relative to that attempt)."""
    text = path.read_text(errors="replace") if path.exists() else ""
    if not text.strip():
        return []
    out: list[dict[str, Any]] = []
    stripped = text.lstrip()
    if stripped.startswith("{") and '"event"' in stripped[:200]:
        base = None
        for line in text.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") == "ojsdn_attempt":
                base = float(ev["ts"])
            elif ev.get("event") == "interval":
                s = ev["data"]["sum"]
                out.append(s if base is None else dict(s, _base=base))
    else:
        try:
            doc = json.loads(text)
            out = [iv["sum"] for iv in doc.get("intervals", [])]
        except json.JSONDecodeError:
            return []
    return out


def parse_iperf(path: Path, launch_ts: float) -> "Any":
    """DataFrame: ts (abs start), seconds, bytes, packets, lost, jitter_ms."""
    import pandas as pd

    rows = []
    for s in _iter_iperf_intervals(path):
        if s.get("omitted"):
            continue
        rows.append({"ts": float(s.get("_base", launch_ts)) + float(s.get("start", 0.0)), "seconds": float(s.get("seconds", 0.1)),
                     "bytes": float(s.get("bytes", 0)), "packets": float(s.get("packets", np.nan)),
                     "lost": float(s.get("lost_packets", np.nan)), "jitter_ms": float(s.get("jitter_ms", np.nan))})
    return pd.DataFrame(rows, columns=["ts", "seconds", "bytes", "packets", "lost", "jitter_ms"])


def _ns(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def parse_irtt(path: Path) -> "Any":
    """DataFrame from irtt JSON ``irtt/<sta>.json.gz`` plus its attempt files ``irtt/<sta>.<n>.json.gz``:
    ts (client send wall, s), owd_ms (receive delay), ipdv_ms, lost."""
    import pandas as pd

    sta = path.name[:-len(".json.gz")] if path.name.endswith(".json.gz") else path.stem
    files = ([path] if path.exists() else []) + sorted(
        path.parent.glob(f"{sta}.*.json.gz"), key=lambda f: int(f.name.split(".")[1]) if f.name.split(".")[1].isdigit() else 0)
    frames = [_parse_irtt_file(f) for f in files]
    frames = [f for f in frames if len(f)]
    if not frames:
        return pd.DataFrame(columns=["ts", "owd_ms", "ipdv_ms", "lost"])
    return pd.concat(frames, ignore_index=True).sort_values("ts", ignore_index=True)


def _parse_irtt_file(path: Path) -> "Any":
    import pandas as pd

    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt") as fh:  # type: ignore[operator]
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError, EOFError):
        LOG.warning("unreadable irtt file %s", path)
        return pd.DataFrame(columns=["ts", "owd_ms", "ipdv_ms", "lost"])
    rows = []
    for rt in doc.get("round_trips", []):
        lost = str(rt.get("lost", "false")).lower()
        ts_ns = _ns(rt.get("timestamps", {}).get("client", {}).get("send", {}).get("wall"))
        rd = _ns(rt.get("delay", {}).get("receive"))
        ipdv = _ns(rt.get("ipdv", {}).get("receive"))
        rows.append({"ts": ts_ns / 1e9, "owd_ms": rd / 1e6, "ipdv_ms": abs(ipdv) / 1e6,
                     "lost": lost in ("true", "true_down")})
    return pd.DataFrame(rows, columns=["ts", "owd_ms", "ipdv_ms", "lost"])


_IRTT_LIVE = re.compile(r"(rtt|rd|sd|ipdv)=(-?[\d.]+)(ns|µs|us|ms|s)")
_UNIT = {"ns": 1e-6, "µs": 1e-3, "us": 1e-3, "ms": 1.0, "s": 1000.0}


def parse_irtt_live_line(line: str) -> dict[str, float]:
    return {k: float(v) * _UNIT[u] for k, v, u in _IRTT_LIVE.findall(line)}


# ----------------------------------------------------------------------------- live KPIs
class LiveKPI(threading.Thread):
    """Tails every flow's live files and keeps the last ``keep_s`` seconds of samples."""

    def __init__(self, run_dir: Path, sta_slice: dict[str, str], launch: dict[str, float],
                 stop: threading.Event, keep_s: float = 15.0):
        super().__init__(name="live-kpi", daemon=True)
        self.run_dir = run_dir
        self.sta_slice = sta_slice
        self.launch = launch
        self.stop_ev = stop
        self.keep_s = keep_s
        self.lock = threading.Lock()
        self.iv: dict[str, collections.deque[tuple[float, float, float, float, float]]] = \
            collections.defaultdict(lambda: collections.deque(maxlen=400))
        self.probes: dict[str, collections.deque[tuple[float, float, float]]] = \
            collections.defaultdict(lambda: collections.deque(maxlen=1000))
        self._pos: dict[Path, int] = {}
        self._base: dict[str, float] = {}  # epoch of each flow's current iperf attempt

    def _tail(self, path: Path) -> list[str]:
        if not path.exists():
            return []
        pos = self._pos.get(path, 0)
        with open(path, "rb") as fh:
            fh.seek(pos)
            data = fh.read()
        # keep an incomplete last line for the next pass
        cut = data.rfind(b"\n") + 1
        self._pos[path] = pos + cut
        return data[:cut].decode(errors="replace").splitlines()

    def run(self) -> None:
        while not self.stop_ev.wait(0.25):
            now = time.time()
            for sta, sl in self.sta_slice.items():
                if self.launch.get(sta) is None:
                    continue
                for line in self._tail(self.run_dir / "iperf" / f"{sta}.json"):
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("event") == "ojsdn_attempt":
                        self._base[sta] = float(ev["ts"])
                    if ev.get("event") != "interval":
                        continue
                    base = self._base.get(sta, self.launch[sta])
                    s = ev["data"]["sum"]
                    with self.lock:
                        self.iv[sta].append((base + float(s.get("end", 0)), float(s.get("bytes", 0)),
                                             float(s.get("packets", 0) or 0), float(s.get("lost_packets", 0) or 0),
                                             float(s.get("jitter_ms", 0) or 0)))
                if sl == "telemedicine":
                    for line in self._tail(self.run_dir / "irtt" / f"{sta}.live.txt"):
                        d = parse_irtt_live_line(line)
                        if "rd" in d:
                            with self.lock:
                                self.probes[sta].append((now, d["rd"], abs(d.get("ipdv", 0.0))))

    def flow_kpis(self, sta: str, seconds: float = 5.0) -> dict[str, float]:
        """Throughput (Mb/s), loss %, jitter (ms), OWD p95 (ms) of one flow over the last window."""
        now = time.time()
        with self.lock:
            iv = [x for x in self.iv.get(sta, ()) if x[0] > now - seconds]
            pr = [x for x in self.probes.get(sta, ()) if x[0] > now - seconds]
        thr = sum(x[1] for x in iv) * 8 / 1e6 / seconds if iv else 0.0
        pk = sum(x[2] for x in iv)
        lost = sum(x[3] for x in iv)
        jit = float(np.mean([x[4] for x in iv])) if iv else float("nan")
        owd = float(np.percentile([x[1] for x in pr], 95)) if pr else float("nan")
        ipdv = float(np.mean([x[2] for x in pr])) if pr else jit
        return {"thr_mbps": thr, "loss_pct": 100.0 * lost / pk if pk else float("nan"),
                "jitter_ms": ipdv, "owd_p95_ms": owd, "samples": len(iv)}
