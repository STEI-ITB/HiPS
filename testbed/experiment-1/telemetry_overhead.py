"""Experiment 1c post-hook: telemetry overhead row from a B0 run with a given telemetry period.

Called by ``run_one.py`` (``post_hook``) after ``kpi.py``. Appends one row to
``output/experiment-1/1c_telemetry_overhead.csv`` and returns the Exp-1 summary row.
Metrics: ``cpu_pct_collector`` (collector thread CPU / wall, % of one core),
``sample_period_jitter_ms`` (std of the inter-sample interval), ``thr_total_mbps`` (all slices).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common import runstate as RS


def post(run_dir: Path, cfg: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    meta = json.loads((run_dir / "run_meta.json").read_text())
    ts = meta.get("telemetry_stats", {})
    thr = sum(float(summary.get(k) or 0.0) for k in ("thr_tele_mbps", "thr_video_mbps", "thr_be_mbps")
              if summary.get(k) == summary.get(k))
    row = {"data_status": summary.get("data_status", "REAL"), "run_tag": cfg["run_tag"],
           "telemetry_period_ms": cfg["telemetry_period_ms"], "rep": cfg["seed"],
           "cpu_pct_collector": ts.get("cpu_pct_collector"), "sample_period_mean_ms": ts.get("sample_period_mean_ms"),
           "sample_period_jitter_ms": ts.get("sample_period_jitter_ms"), "samples": ts.get("samples"),
           "thr_total_mbps": thr, "cpu_pct_emu": summary.get("cpu_pct_emu")}
    raw = Path(cfg["raw_csv"])
    RS.delete_rows(raw, cfg["run_tag"])
    RS.append_rows(raw, [row])
    keep = ("data_status", "exp", "sub_exp", "run_tag", "framework", "n_ap", "n_sta", "seed", "telemetry_period_ms")
    return {**{k: summary.get(k) for k in keep}, **{k: row[k] for k in
            ("cpu_pct_collector", "sample_period_jitter_ms", "thr_total_mbps", "cpu_pct_emu")}}
