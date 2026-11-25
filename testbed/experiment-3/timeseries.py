#!/usr/bin/env python3
"""Experiment 3 — per-second time series averaged over seeds → ``timeseries_exp3.csv``.

For every finished run folder of Experiment 3 it reads ``kpi_windows.csv`` and takes, per 1-s
window, the % of telemedicine flows meeting their SLA (``tele_sla_pct``) and ap2's airtime;
then averages over seeds per (framework, hotspot_size, t_s). Used for the remediation plots.

Usage::  .venv/bin/python experiment-3/timeseries.py [--output-dir output]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import config as C  # noqa: E402

LOG = logging.getLogger("timeseries")


def build(exp_dir: Path) -> pd.DataFrame:
    parts = []
    for kw in sorted(exp_dir.glob("*/kpi_windows.csv")):
        rd = kw.parent
        if not (rd / "DONE").exists():
            continue
        cfg = json.loads((rd / "run_config.json").read_text())
        w = pd.read_csv(kw)
        tele = w[(w["level"] == "slice") & (w["slice"] == "telemedicine")][["w", "pct_flows_met"]]
        tgt = cfg.get("hotspot_target", "ap2")
        air = w[(w["level"] == "ap") & (w["ap"] == tgt)][["w", "airtime"]]
        df = tele.merge(air, on="w", how="outer").rename(columns={"w": "t_s", "pct_flows_met": "tele_sla_pct",
                                                                  "airtime": "ap2_airtime"})
        df["framework"], df["hotspot_size"], df["seed"] = cfg["framework"], cfg["hotspot_size"], cfg["seed"]
        df["data_status"] = "REAL"
        parts.append(df)
    if not parts:
        return pd.DataFrame(columns=["data_status", "framework", "hotspot_size", "t_s", "tele_sla_pct",
                                     "ap2_airtime", "n_runs"])
    all_ = pd.concat(parts, ignore_index=True)
    g = all_.groupby(["framework", "hotspot_size", "t_s"]).agg(
        tele_sla_pct=("tele_sla_pct", "mean"), ap2_airtime=("ap2_airtime", "mean"), n_runs=("seed", "nunique"),
        data_status=("data_status", "first")).reset_index()
    return g[["data_status", "framework", "hotspot_size", "t_s", "tele_sla_pct", "ap2_airtime", "n_runs"]]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output-dir", default=str(C.TESTBED / "output"))
    a = ap.parse_args(argv)
    C.setup_logging()
    exp_dir = Path(a.output_dir) / "experiment-3"
    df = build(exp_dir)
    out = exp_dir / "timeseries_exp3.csv"
    if df.empty:
        LOG.warning("no finished Exp-3 runs with kpi_windows.csv; %s left unchanged", out)
        return 0
    df.to_csv(out, index=False)
    LOG.info("wrote %s (%d rows)", out, len(df))
    return 0


if __name__ == "__main__":
    sys.exit(main())
