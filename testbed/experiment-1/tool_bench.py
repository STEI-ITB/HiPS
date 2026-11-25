#!/usr/bin/env python3
"""Experiment 1b — tool latency: ``calls_per_tool`` calls of each of the six tools.

Started by ``run_one.py`` as the run's ``agent_entry`` (default topology, background traffic on,
static STAs). Measures the server-side latency *call → effect verified* (``tool_latency_ms``),
the client round trip (``rpc_latency_ms``), BTM re-association time (``btm_ho_ms``) and success.
Also provides ``post()`` (the run's ``post_hook``) that adds per-tool aggregates to the summary.
Rows → ``output/experiment-1/1b_tool_latency.csv``.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import config as C  # noqa: E402
from common import runstate as RS  # noqa: E402
from common.tools import ALL_TOOLS  # noqa: E402

LOG = logging.getLogger("tool_bench")


def bench(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    from common.agents.run_agents import wait_ready
    from common.tool_server import ToolClient, ToolError

    run_dir = Path(cfg["run_dir"])
    t0 = wait_ready(run_dir, float(cfg["setup_timeout_s"]) + 120)
    if t0 is None:
        raise RuntimeError("emulation not READY")
    time.sleep(max(0.0, t0 + float(cfg["warmup_s"]) - time.time()))
    cli = ToolClient(int(cfg["tool_server_port"]), "tool_bench", timeout_s=10)
    topo = cli.call("get_topology")
    aps = sorted(topo["aps"], key=lambda s: int(s[2:]))
    n = int(cfg["calls_per_tool"])
    rows: list[dict[str, Any]] = []
    last_steered: str | None = None
    for tool in ALL_TOOLS:
        for i in range(n):
            ap = aps[i % len(aps)]
            params: dict[str, Any]
            if tool in ("get_ap_airtime_utilization", "get_station_stats"):
                params = {"ap_id": ap}
            elif tool == "get_slice_kpis":
                params = {"ap_id": ap, "slice": C.SLICES[i % 3]}
            elif tool == "set_tx_power":
                params = {"ap_id": ap, "dbm": 12 if (i // len(aps)) % 2 == 0 else 14}
            elif tool == "adjust_slice_bandwidth":
                params = {"ap_id": ap, "slice": "video", "rate_mbps": 5 if (i // len(aps)) % 2 == 0 else 8}
            else:  # trigger_bss_transition: non-tele STA, best-heard neighbour of its serving AP
                snap = cli.call("get_snapshot")
                cands = [(s, st) for s, st in snap["stas"].items()
                         if st["slice"] != "telemedicine" and st.get("ap") and s != last_steered]
                if not cands:
                    time.sleep(1)
                    continue
                s, st = cands[i % len(cands)]
                nb = topo["neighbours"].get(st["ap"], []) or [a for a in aps if a != st["ap"]]
                tgt = max(nb, key=lambda a: (st.get("est_rssi") or {}).get(a, -200))
                params = {"sta_mac": topo["stas"][s]["mac"], "target_ap": tgt}
                last_steered = s
            t = time.perf_counter()
            try:
                res = cli.call(tool, **params)
            except ToolError as exc:
                res = {"ok": False, "verified": False, "error": str(exc)}
            rpc = (time.perf_counter() - t) * 1000
            rows.append({"data_status": "REAL", "run_tag": cfg["run_tag"], "tool": tool, "call": i + 1,
                         "tool_latency_ms": res.get("latency_ms", float("nan")), "rpc_latency_ms": rpc,
                         "btm_ho_ms": res.get("btm_ho_ms", float("nan")) if tool == "trigger_bss_transition" else "",
                         "ok": int(bool(res.get("ok"))), "verified": int(bool(res.get("verified"))),
                         "error": str(res.get("error") or "")[:120]})
            if tool == "trigger_bss_transition":
                time.sleep(1.0)  # let the STA settle before the next steer
    return rows


def post(run_dir: Path, cfg: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    """post_hook: per-tool aggregates from the raw CSV into the Exp-1 summary row."""
    rows = [r for r in RS.read_csv_rows(Path(cfg["raw_csv"])) if r["run_tag"] == cfg["run_tag"]]
    out = {k: summary[k] for k in ("data_status", "exp", "sub_exp", "run_tag", "framework", "n_ap", "n_sta", "seed")}
    for tool in ALL_TOOLS:
        rr = [r for r in rows if r["tool"] == tool]
        lat = np.array([float(r["tool_latency_ms"]) for r in rr if r["tool_latency_ms"] not in ("", "nan")])
        out[f"{tool}_latency_ms_mean"] = float(lat.mean()) if lat.size else float("nan")
        out[f"{tool}_latency_ms_p95"] = float(np.percentile(lat, 95)) if lat.size else float("nan")
        out[f"{tool}_success_pct"] = 100.0 * sum(int(r["ok"]) and int(r["verified"]) for r in rr) / len(rr) if rr else float("nan")
    btm = np.array([float(r["btm_ho_ms"]) for r in rows if r["tool"] == "trigger_bss_transition"
                    and r["btm_ho_ms"] not in ("", "nan")])
    out["btm_ho_ms_mean"] = float(np.nanmean(btm)) if btm.size else float("nan")
    out["tool_success_pct"] = 100.0 * sum(int(r["ok"]) and int(r["verified"]) for r in rows) / len(rows) if rows else float("nan")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-config", required=True)
    a = ap.parse_args(argv)
    cfg = C.read_run_config(a.run_config)
    C.setup_logging(logfile=Path(cfg["run_dir"]) / "tool_bench.log")
    raw = Path(cfg["raw_csv"])
    RS.delete_rows(raw, cfg["run_tag"])
    try:
        rows = bench(cfg)
    except Exception:  # noqa: BLE001
        LOG.exception("tool bench failed")
        return 1
    RS.append_rows(raw, rows)
    LOG.info("tool bench: %d rows", len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
