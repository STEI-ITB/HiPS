#!/usr/bin/env python3
"""Consolidate every experiment's output into ``results.xlsx``.

Usage::

    .venv/bin/python process-result.py [--output-dir output] [--xlsx results.xlsx] [--recompute]

1. ``--recompute``: re-run ``common/kpi.py`` on every finished raw run folder (DONE marker) and
   re-build ``experiment-3/timeseries_exp3.csv`` before consolidating.
2. Read ``output/experiment-*/summary_exp*.csv`` and the auxiliary CSVs (1a–1d, Exp-3 time series).
3. Validate: required columns, run counts per group against the planned grid (``common/campaign.py``),
   value ranges and ``viol_tele_pct = 100 − sla_tele_pct``. Print and save a completeness report.
4. Write the workbook: README, Exp1_Calibration, Exp1_ToolLatency, Exp1_Telemetry, Exp1_SLMLatency,
   Exp2_Runs, Exp2_Summary, Exp3_Runs, Exp3_Summary, Exp3_Timeseries, Exp4_Runs, Exp4_Summary,
   Exp5_Runs, Exp5_Summary, Stats — title row 1, header row 2 (frozen), auto-sized columns.

Statistics: per group n, mean, std, 95 % CI (t-distribution); paired Wilcoxon signed-rank of
P_HIER_AGENT vs each baseline (paired by seed) with effect size r = |Z|/√n and matched-pairs
rank-biserial correlation; Friedman test across the four frameworks; Holm correction per
(experiment, metric) family. LLM frameworks are analysed separately per ``llm_backend``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import logging
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config as C  # noqa: E402
from common.kpi import METRIC_COLS, PARAM_COLS  # noqa: E402

LOG = logging.getLogger("process-result")
P = "P_HIER_AGENT"
BASELINES = ["B0_UNMANAGED", "B1_HEURISTIC", "B2_FLAT_AGENT"]
FW_ORDER = BASELINES + [P]

GROUPS = {
    2: ["framework", "speed_mps", "llm_backend"],
    3: ["framework", "hotspot_size", "llm_backend"],
    4: ["framework", "n_ap", "n_sta", "llm_backend"],
    5: ["sub_exp", "framework", "planner_model", "plan_interval_s", "subagent_mode", "guardrail", "infer_hw",
        "llm_backend"],
}
PAIR_KEYS = {2: ["speed_mps"], 3: ["hotspot_size"], 4: ["n_ap", "n_sta"]}
STAT_METRICS = {
    2: ["sla_tele_pct", "pingpong_prob", "ho_downtime_ms_mean", "jfi_airtime"],
    3: ["ttr_s", "sla_tele_hot_pct", "pingpong_prob"],
    4: ["sla_tele_pct", "plan_latency_ms_mean", "pingpong_prob"],
    5: ["sla_tele_pct", "plan_latency_ms_mean", "plan_validity_pct", "pingpong_prob", "invalid_actions"],
}
EXP5_DEFAULT = {"planner_model": "qwen3.5:4b", "plan_interval_s": 10, "subagent_mode": "rule",
                "guardrail": "on", "infer_hw": "gpu"}
UNITS = {
    "sla_tele_pct": "%", "sla_video_pct": "%", "sla_be_pct": "%", "viol_tele_pct": "%", "thr_tele_mbps": "Mb/s",
    "thr_video_mbps": "Mb/s", "thr_be_mbps": "Mb/s", "owd_p95_tele_ms": "ms", "jitter_tele_ms": "ms",
    "loss_tele_pct": "%", "loss_video_pct": "%", "ho_count": "count", "ho_downtime_ms_mean": "ms",
    "ho_downtime_ms_p95": "ms", "pingpong_prob": "0-1", "jfi_airtime": "0-1", "jfi_be_thr": "0-1",
    "plan_latency_ms_mean": "ms", "plan_latency_ms_p95": "ms", "exec_latency_ms_mean": "ms",
    "plan_skipped_pct": "%", "plan_validity_pct": "%", "invalid_actions": "count", "actions_per_min": "1/min",
    "prompt_tokens_mean": "tokens", "output_tokens_mean": "tokens", "ttr_s": "s", "ttr_censored": "0/1",
    "cpu_pct_emu": "%", "cpu_pct_llm": "%", "ram_gb": "GB", "sla_tele_hot_pct": "%", "min_sla_tele_1s_pct": "%",
    "offloaded_sta": "count", "ap2_airtime_peak": "0-1",
}
RANGES = {"pct": (0, 100), "prob": (0, 1)}


# ============================================================================ loading
def read_csv(p: Path) -> pd.DataFrame:
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p)
    if "llm_backend" in df.columns and "framework" in df.columns:
        llm_fw = df["framework"].isin(C.LLM_FRAMEWORKS) & (df.get("planner_model", "") != "none_rule")
        df.loc[~llm_fw, "llm_backend"] = "none"
    return df


def recompute(out: Path) -> None:
    from common import runstate as RS
    from common.kpi import run_kpi

    n = 0
    for exp in range(2, 6):
        for rd in sorted((out / f"experiment-{exp}").glob("exp*")):
            if not (rd / "DONE").exists() or not (rd / "run_meta.json").exists():
                continue
            cfg = C.read_run_config(rd / "run_config.json")
            s = run_kpi(rd)
            n += 1
            labels = cfg.get("sub_exps") or [cfg.get("sub_exp", "")]
            RS.upsert_rows(Path(cfg["summary_csv"]) if Path(cfg["summary_csv"]).is_absolute()
                           else out / f"experiment-{exp}" / Path(cfg["summary_csv"]).name,
                           [dict(s, sub_exp=lab) for lab in labels])
    import importlib.util

    spec = importlib.util.spec_from_file_location("ts", C.TESTBED / "experiment-3" / "timeseries.py")
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        df = mod.build(out / "experiment-3")
        if not df.empty:
            df.to_csv(out / "experiment-3" / "timeseries_exp3.csv", index=False)
    LOG.info("recomputed KPIs of %d run folders", n)


# ============================================================================ statistics
def ci95(x: np.ndarray) -> float:
    x = x[~np.isnan(x)]
    if x.size < 2:
        return float("nan")
    return float(stats.t.ppf(0.975, x.size - 1) * x.std(ddof=1) / math.sqrt(x.size))


def summarize(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    metrics = [m for m in METRIC_COLS if m in df.columns and pd.api.types.is_numeric_dtype(df[m])
               and df[m].notna().any()]
    keys = [k for k in keys if k in df.columns]
    rows = []
    for gk, g in df.groupby(keys, dropna=False, sort=False):
        gk = gk if isinstance(gk, tuple) else (gk,)
        r: dict[str, Any] = dict(zip(keys, gk))
        r["n"] = len(g)
        for m in metrics:
            x = g[m].to_numpy(dtype=float)
            r[f"{m}_mean"] = float(np.nanmean(x)) if np.isfinite(x).any() else float("nan")
            r[f"{m}_std"] = float(np.nanstd(x, ddof=1)) if np.isfinite(x).sum() > 1 else float("nan")
            r[f"{m}_ci95"] = ci95(x)
        rows.append(r)
    out = pd.DataFrame(rows)
    if "framework" in out:
        out["_fw"] = out["framework"].map({f: i for i, f in enumerate(FW_ORDER)})
        out = out.sort_values([k for k in keys if k != "framework"] + ["_fw"]).drop(columns="_fw")
    return out


def wilcoxon_paired(a: np.ndarray, b: np.ndarray) -> dict[str, float]:
    m = ~(np.isnan(a) | np.isnan(b))
    a, b = a[m], b[m]
    d = a - b
    n = int(np.sum(d != 0))
    res = {"n_pairs": int(m.sum()), "mean_diff": float(np.mean(d)) if d.size else float("nan"),
           "statistic": float("nan"), "p_value": float("nan"), "effect_r": float("nan"), "rank_biserial": float("nan")}
    if d.size < 2:
        return res
    if n == 0:
        res.update({"statistic": 0.0, "p_value": 1.0, "effect_r": 0.0, "rank_biserial": 0.0})
        return res
    w = stats.wilcoxon(a, b, zero_method="wilcox", alternative="two-sided")
    nz = d[d != 0]
    ranks = stats.rankdata(np.abs(nz))
    wp, wm = float(ranks[nz > 0].sum()), float(ranks[nz < 0].sum())
    mu, sd = n * (n + 1) / 4, math.sqrt(n * (n + 1) * (2 * n + 1) / 24)
    z = (min(wp, wm) - mu) / sd if sd > 0 else 0.0
    res.update({"statistic": float(w.statistic), "p_value": float(w.pvalue), "effect_r": abs(z) / math.sqrt(n),
                "rank_biserial": (wp - wm) / (wp + wm)})
    return res


def holm(p: list[float]) -> list[float]:
    idx = [i for i, v in enumerate(p) if v == v]
    order = sorted(idx, key=lambda i: p[i])
    out = [float("nan")] * len(p)
    running = 0.0
    m = len(order)
    for k, i in enumerate(order):
        running = max(running, min(1.0, (m - k) * p[i]))
        out[i] = running
    return out


def stats_framework_exps(runs: dict[int, pd.DataFrame]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for exp in (2, 3, 4):
        df = runs.get(exp, pd.DataFrame())
        if df.empty:
            continue
        for backend in sorted(set(df.get("llm_backend", pd.Series(["local"]))) - {"none"}) or ["local"]:
            sub = df[df["llm_backend"].isin(["none", backend])] if "llm_backend" in df else df
            for gk, g in sub.groupby(PAIR_KEYS[exp]):
                gk = gk if isinstance(gk, tuple) else (gk,)
                group = ", ".join(f"{k}={v}" for k, v in zip(PAIR_KEYS[exp], gk))
                for m in STAT_METRICS[exp]:
                    if m not in g or not g[m].notna().any():
                        continue
                    piv = g.pivot_table(index="seed", columns="framework", values=m, aggfunc="first")
                    for base in BASELINES:
                        if P not in piv or base not in piv:
                            continue
                        r = wilcoxon_paired(piv[P].to_numpy(float), piv[base].to_numpy(float))
                        rows.append({"experiment": exp, "llm_backend": backend, "group": group, "metric": m,
                                     "test": "Wilcoxon signed-rank (paired by seed)", "comparison": f"P vs {base}",
                                     **r})
                    have = [f for f in FW_ORDER if f in piv]
                    comp = piv[have].dropna()
                    if len(have) >= 3 and len(comp) >= 2:
                        try:
                            fr = stats.friedmanchisquare(*[comp[f].to_numpy(float) for f in have])
                            chi, pv = float(fr.statistic), float(fr.pvalue)
                        except ValueError:
                            chi, pv = float("nan"), float("nan")
                        k = len(have)
                        rows.append({"experiment": exp, "llm_backend": backend, "group": group, "metric": m,
                                     "test": "Friedman", "comparison": " / ".join(C.FRAMEWORK_SHORT[f] for f in have),
                                     "n_pairs": len(comp), "statistic": chi, "p_value": pv,
                                     "effect_r": chi / (len(comp) * (k - 1)) if len(comp) else float("nan"),  # Kendall W
                                     "rank_biserial": float("nan"), "mean_diff": float("nan")})
    return pd.DataFrame(rows)


def stats_exp5(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if df.empty:
        return pd.DataFrame()
    dims = ["planner_model", "plan_interval_s", "subagent_mode", "guardrail", "infer_hw"]
    for (sub, fw, backend), g in df.groupby(["sub_exp", "framework", "llm_backend"]):
        g = g.copy()
        g["_cfg"] = g[dims].astype(str).agg("|".join, axis=1)
        ref_mask = np.ones(len(g), dtype=bool)
        for k, v in EXP5_DEFAULT.items():
            ref_mask &= g[k].astype(str).to_numpy() == str(v)
        if not ref_mask.any():
            ref_cfg = g["_cfg"].iloc[0]
        else:
            ref_cfg = g.loc[ref_mask, "_cfg"].iloc[0]
        for m in STAT_METRICS[5]:
            if m not in g or not g[m].notna().any():
                continue
            piv = g.pivot_table(index="seed", columns="_cfg", values=m, aggfunc="first")
            if ref_cfg not in piv:
                continue
            for c in piv.columns:
                if c == ref_cfg:
                    continue
                r = wilcoxon_paired(piv[c].to_numpy(float), piv[ref_cfg].to_numpy(float))
                rows.append({"experiment": 5, "llm_backend": backend, "group": f"{sub} {C.FRAMEWORK_SHORT.get(fw, fw)}",
                             "metric": m, "test": "Wilcoxon signed-rank (paired by seed)",
                             "comparison": f"[{c}] vs default [{ref_cfg}]", **r})
    return pd.DataFrame(rows)


def add_holm(st: pd.DataFrame) -> pd.DataFrame:
    if st.empty:
        return st
    st = st.copy()
    st["p_holm"] = float("nan")
    for _, idx in st.groupby(["experiment", "llm_backend", "metric", "test"]).groups.items():
        st.loc[idx, "p_holm"] = holm(st.loc[idx, "p_value"].tolist())
    st["significant_0.05"] = st["p_holm"] < 0.05
    return st


# ============================================================================ validation
def validate(runs: dict[int, pd.DataFrame], aux: dict[str, pd.DataFrame]) -> tuple[list[str], dict[int, dict[str, Any]]]:
    from common.campaign import build_runs

    report: list[str] = []
    detail: dict[int, dict[str, Any]] = {}
    base = C.load_base()
    base.setdefault("llm", {})["backend"] = "local"
    for exp in range(1, 6):
        df = runs.get(exp, pd.DataFrame())
        try:
            planned = build_runs(exp, None, base)
        except Exception as exc:  # noqa: BLE001
            report.append(f"Exp {exp}: cannot expand plan ({exc})")
            continue
        if exp == 5:
            from common.campaign import _sub_blocks, exp_config

            ecfg = exp_config(5)
            present_subs = set(df["sub_exp"]) if "sub_exp" in df else set()
            subs = {s for s, b in _sub_blocks(ecfg, None, include_optional=True)
                    if not b.get("optional") or s in present_subs}
            for s_opt in subs - {s for s, _ in _sub_blocks(ecfg, None)}:
                planned += build_runs(5, s_opt, base)
            planned_keys = {(r["run_tag"], s) for r in planned for s in r["sub_exps"] if s in subs}
            have_keys = set(zip(df.get("run_tag", []), df.get("sub_exp", []))) if not df.empty else set()
        else:
            planned_keys = {(r["run_tag"], r.get("sub_exp", "")) for r in planned}
            have_keys = set(zip(df.get("run_tag", []), df.get("sub_exp", pd.Series([""] * len(df)))
                                .fillna(""))) if not df.empty else set()
            if exp != 1:
                planned_keys = {(t, "") for t, _ in planned_keys}
                have_keys = {(t, "") for t, _ in have_keys}
        missing = sorted(planned_keys - have_keys)
        info = {"planned": len(planned_keys), "present": len(planned_keys & have_keys), "missing": len(missing),
                "extra": len(have_keys - planned_keys)}
        line = (f"Exp {exp}: {info['present']}/{info['planned']} planned summary rows present, "
                f"{info['missing']} missing, {info['extra']} extra (cloud variants / unplanned)")
        report.append(line)
        if missing:
            report.append("   missing (first 8): " + ", ".join(f"{t}{'/' + s if s else ''}" for t, s in missing[:8]))
        if exp >= 2 and not df.empty:
            need = ["run_tag", "framework", "seed"] + [m for m in ("sla_tele_pct", "pingpong_prob") if True]
            miss_cols = [c for c in need if c not in df.columns]
            if miss_cols:
                report.append(f"   MISSING COLUMNS: {miss_cols}")
            exp_n = 10 if exp in (2, 3) else 5
            loc = df[df["llm_backend"].isin(["none", "local"])] if "llm_backend" in df else df
            gk = [k for k in GROUPS[exp] if k in loc.columns and k != "llm_backend"]
            cnt = loc.groupby(gk).size()
            bad = cnt[cnt != exp_n]
            if len(bad):
                report.append(f"   groups with n != {exp_n}: {len(bad)} (e.g. {dict(list(bad.items())[:3])})")
            oor = 0
            for c in df.columns:
                if not pd.api.types.is_numeric_dtype(df[c]):
                    continue
                if c.endswith("_pct"):
                    oor += int(((df[c] < -1e-9) | (df[c] > 100 + 1e-9)).sum())
                elif c in ("pingpong_prob", "jfi_airtime", "jfi_be_thr", "ap2_airtime_peak"):
                    oor += int(((df[c] < 0) | (df[c] > 1 + 1e-9)).sum())
                elif c.endswith("_ms_mean") or c.endswith("_ms") or c.endswith("_mbps"):
                    oor += int((df[c] < 0).sum())
            if {"sla_tele_pct", "viol_tele_pct"} <= set(df.columns):
                oor += int((np.abs(df["viol_tele_pct"] - (100 - df["sla_tele_pct"])) > 1e-6).sum())
            report.append(f"   out-of-range / inconsistent values: {oor}")
            info["out_of_range"] = oor
        detail[exp] = info
    for name, a in aux.items():
        report.append(f"{name}: {len(a)} rows")
    return report, detail


# ============================================================================ workbook
def write_workbook(path: Path, sheets: list[tuple[str, str, pd.DataFrame]], readme: list[list[Any]]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    head_fill = PatternFill("solid", fgColor="D9E1F2")
    bold = Font(bold=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "README"
    r0 = 1
    for i, row in enumerate(readme):
        for j, v in enumerate(row):
            c = ws.cell(r0 + i, 1 + j, v)
            if j == 0 and len(row) == 1 and isinstance(v, str) and v.isupper():
                c.font = bold
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 60
    ws.column_dimensions["C"].width = 18
    ws.column_dimensions["D"].width = 40
    for name, title, df in sheets:
        sh = wb.create_sheet(name)
        sh.cell(1, 1, title)
        sh.cell(1, 1).font = Font(bold=True, size=12)
        if df.empty:
            sh.cell(2, 1, "(no data)")
            continue
        cols = list(df.columns)
        for j, c in enumerate(cols, 1):
            cell = sh.cell(2, j, c)
            cell.font = bold
            cell.fill = head_fill
            cell.alignment = Alignment(wrap_text=False)
        vals = df.to_numpy(dtype=object)
        for i in range(vals.shape[0]):
            for j in range(vals.shape[1]):
                v = vals[i, j]
                if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                    v = None
                elif isinstance(v, (np.integer,)):
                    v = int(v)
                elif isinstance(v, (np.floating,)):
                    v = None if not np.isfinite(v) else float(v)
                elif isinstance(v, (np.bool_,)):
                    v = bool(v)
                sh.cell(3 + i, 1 + j, v)
        for j, c in enumerate(cols, 1):
            s = df[c]
            fmt = None
            if pd.api.types.is_integer_dtype(s):
                fmt = "0"
            elif pd.api.types.is_float_dtype(s):
                x = s.dropna()
                if len(x) and (x == x.round()).all() and x.abs().max() < 1e6 and not c.endswith(("_mean", "_std", "_ci95")):
                    fmt = "0"
                elif c.startswith("p_") or c == "p_value" or c.endswith("_prob") or "jfi" in c or c.startswith("p_holm"):
                    fmt = "0.0000"
                else:
                    fmt = "0.00"
            if fmt:
                for i in range(len(df)):
                    sh.cell(3 + i, j).number_format = fmt
            width = max([len(str(c))] + [len(f"{v:.2f}") if isinstance(v, float) else len(str(v))
                                         for v in s.head(200).tolist()]) + 2
            sh.column_dimensions[get_column_letter(j)].width = min(max(width, 8), 45)
        sh.freeze_panes = "A3"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def readme_rows(report: list[str], inputs: list[Path]) -> list[list[Any]]:
    base = C.load_yaml(C.COMMON / "defaults.yaml")
    rows: list[list[Any]] = [
        ["HIERARCHICAL PLAN-AND-SOLVE AGENTIC AI FOR MULTI-CELL WIRELESS SLA MANAGEMENT IN MININET-WIFI"],
        ["Consolidated results workbook built by process-result.py"],
        ["Created", dt.datetime.now().isoformat(timespec="seconds")],
        ["Data status", "REAL (measured)"],
        [],
        ["SHEETS"],
        ["Exp1_*", "raw rows of Experiment 1a (calibration), 1b (tool latency), 1c (telemetry), 1d (SLM latency)"],
        ["ExpN_Runs", "one row per run (parameters + all metrics); Exp5 has a sub_exp column"],
        ["ExpN_Summary", "n, mean, std, 95 % CI half-width (t) per group and metric"],
        ["Exp3_Timeseries", "per-second telemedicine SLA % and ap2 airtime averaged over seeds"],
        ["Stats", "paired Wilcoxon (P vs baselines), Friedman, Holm-corrected p (p_holm); effect r = |Z|/sqrt(n)"],
        [],
        ["PARAMETER GLOSSARY", "default"],
    ]
    for k in PARAM_COLS:
        if k in base:
            rows.append([k, str(base[k])])
    rows += [[], ["METRICS AND UNITS", "unit"]]
    rows += [[m, UNITS.get(m, "")] for m in METRIC_COLS]
    rows += [[], ["COMPLETENESS REPORT"]] + [[ln] for ln in report]
    rows += [[], ["PROVENANCE: INPUT FILES"]] + [[str(p.relative_to(C.TESTBED)) if p.is_relative_to(C.TESTBED) else str(p)]
                                                  for p in inputs]
    return rows


# ============================================================================ main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output-dir", default=str(C.TESTBED / "output"))
    ap.add_argument("--xlsx", default=str(C.TESTBED / "results.xlsx"))
    ap.add_argument("--recompute", action="store_true", help="re-run kpi.py on raw run folders first")
    a = ap.parse_args(argv)
    C.setup_logging()
    out = Path(a.output_dir)
    if a.recompute:
        recompute(out)
    elif not (out / "experiment-3" / "timeseries_exp3.csv").exists():
        # only --recompute used to build it, so the campaign's workbook had an empty Exp3_Timeseries
        spec = importlib.util.spec_from_file_location("ts", C.TESTBED / "experiment-3" / "timeseries.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        df = mod.build(out / "experiment-3")
        if not df.empty:
            df.to_csv(out / "experiment-3" / "timeseries_exp3.csv", index=False)
    inputs: list[Path] = []
    runs: dict[int, pd.DataFrame] = {}
    for exp in range(1, 6):
        p = out / f"experiment-{exp}" / f"summary_exp{exp}.csv"
        runs[exp] = read_csv(p)
        if p.exists():
            inputs.append(p)
    aux_files = {"Exp1_Calibration": "experiment-1/1a_calibration.csv", "Exp1_ToolLatency": "experiment-1/1b_tool_latency.csv",
                 "Exp1_Telemetry": "experiment-1/1c_telemetry_overhead.csv", "Exp1_SLMLatency": "experiment-1/1d_slm_latency.csv",
                 "Exp3_Timeseries": "experiment-3/timeseries_exp3.csv"}
    aux = {}
    for k, rel in aux_files.items():
        aux[k] = read_csv(out / rel)
        if (out / rel).exists():
            inputs.append(out / rel)
    report, _ = validate(runs, aux)
    for ln in report:
        LOG.info("%s", ln)
    (out / "completeness_report.txt").write_text("\n".join(report) + "\n")

    st = pd.concat([stats_framework_exps(runs), stats_exp5(runs.get(5, pd.DataFrame()))], ignore_index=True)
    st = add_holm(st)
    sheets: list[tuple[str, str, pd.DataFrame]] = [
        ("Exp1_Calibration", "Experiment 1a — radio calibration (raw rows)", aux["Exp1_Calibration"]),
        ("Exp1_ToolLatency", "Experiment 1b — tool latency (raw rows)", aux["Exp1_ToolLatency"]),
        ("Exp1_Telemetry", "Experiment 1c — telemetry overhead (raw rows)", aux["Exp1_Telemetry"]),
        ("Exp1_SLMLatency", "Experiment 1d — SLM latency (raw rows)", aux["Exp1_SLMLatency"]),
    ]
    titles = {2: "Experiment 2 — framework comparison under mobility", 3: "Experiment 3 — hotspot remediation",
              4: "Experiment 4 — scalability", 5: "Experiment 5 — ablation"}
    for exp in (2, 3, 4, 5):
        df = runs.get(exp, pd.DataFrame())
        sheets.append((f"Exp{exp}_Runs", titles[exp] + " (one row per run)", df))
        sheets.append((f"Exp{exp}_Summary", titles[exp] + " (mean, std, 95 % CI per group)",
                       summarize(df, GROUPS[exp]) if not df.empty else pd.DataFrame()))
        if exp == 3:
            sheets.append(("Exp3_Timeseries", titles[3] + " (per-second, averaged over seeds)", aux["Exp3_Timeseries"]))
    sheets.append(("Stats", "Paired Wilcoxon / Friedman tests with Holm correction", st))
    order = ["Exp1_Calibration", "Exp1_ToolLatency", "Exp1_Telemetry", "Exp1_SLMLatency", "Exp2_Runs", "Exp2_Summary",
             "Exp3_Runs", "Exp3_Summary", "Exp3_Timeseries", "Exp4_Runs", "Exp4_Summary", "Exp5_Runs", "Exp5_Summary",
             "Stats"]
    sheets.sort(key=lambda s: order.index(s[0]))
    write_workbook(Path(a.xlsx), sheets, readme_rows(report, inputs))
    LOG.info("wrote %s", a.xlsx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
