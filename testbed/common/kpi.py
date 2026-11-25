#!/usr/bin/env python3
"""Raw run folder → ``kpi_windows.csv`` + one summary row (all metrics).

Usage::  python3 common/kpi.py output/experiment-2/<run_tag> [--json]

Window model: ``w = floor(ts − t0)``; metrics use ``warmup_s ≤ w < duration_s``.

* Per flow & window: thr = Σbytes·8/1 s from iperf3 0.1 s intervals; loss = Σlost/Σpackets;
  telemedicine OWD p95 / mean |IPDV| from irtt probes (downlink receive delay). An *active*
  flow with no data in a window has thr 0 / loss 100 % and therefore violates.
* ``sla_<slice>_pct`` = % of windows (with ≥ 1 active flow) in which every active flow meets all
  constraints; best effort: every AP with ≥ 1 associated BE STA carries ≥ 2 Mb/s BE goodput.
* Handover = CONNECTED to a BSSID different from the previous one. Downtime = CONNECTED −
  max(DISCONNECTED, accepted BTM_RESP) within ``handover_lookback_s`` before; handovers without a
  preceding event are counted but excluded from downtime statistics. Ping-pong: B→A within
  ``t_pingpong_s`` after A→B; ``pingpong_prob`` = 0 when ``ho_count`` = 0 (flag ``ho_count_zero``).
* ``ttr_s``: tele STAs in the target AP's *geometric* cell (trace positions); search from the first
  violated window at/after onset; ``ttr_s`` = start of first ``ttr_consecutive`` met windows − onset;
  never violated → 0; not remediated before ``hotspot_end_s`` → censored at end − onset.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import config as C  # noqa: E402
from common import radio  # noqa: E402
from common.traffic import parse_iperf, parse_irtt  # noqa: E402

LOG = logging.getLogger("kpi")
NAN = float("nan")

PARAM_COLS = ["framework", "n_ap", "n_sta", "speed_mps", "hotspot_size", "hotspot_start_s", "hotspot_end_s",
              "planner_model", "planner_model_effective", "plan_interval_s", "exec_interval_s", "subagent_mode",
              "guardrail", "btm_holddown_s", "telemetry_period_ms", "ap_capacity_mbps", "be_tcp_cap_mbps", "duration_s", "warmup_s",
              "seed", "run_id", "infer_hw",
              "ctl", "heuristic_mode", "bgscan_short_s", "bgscan_long_s", "bgscan_long_jitter_s",   # Experiments 6-8
              "prop_exp", "flat_exec", "lb_rssi_lo_dbm", "heuristic_airtime_hi", "heuristic_airtime_lo",
              "medium_rcvbuf_kb", "medium_per_channel", "medium_max_queue_ms"]   # experiment-11
METRIC_COLS = ["sla_tele_pct", "sla_video_pct", "sla_be_pct", "viol_tele_pct", "thr_tele_mbps", "thr_video_mbps",
               "thr_be_mbps", "owd_p95_tele_ms", "jitter_tele_ms", "loss_tele_pct", "loss_video_pct", "ho_count",
               "ho_downtime_ms_mean", "ho_downtime_ms_p95", "pingpong_prob", "jfi_airtime", "jfi_be_thr",
               "plan_latency_ms_mean", "plan_latency_ms_p95", "exec_latency_ms_mean", "plan_skipped_pct",
               "plan_validity_pct", "invalid_actions", "actions_per_min", "prompt_tokens_mean",
               "output_tokens_mean", "ttr_s", "ttr_censored", "cpu_pct_emu", "cpu_pct_llm", "ram_gb",
               "sla_tele_hot_pct", "min_sla_tele_1s_pct", "offloaded_sta", "ap2_airtime_peak"]


def jain(x: list[float] | np.ndarray) -> float:
    a = np.asarray([v for v in x if v == v], dtype=float)
    if a.size == 0 or np.sum(a ** 2) == 0:
        return NAN
    return float(np.sum(a) ** 2 / (a.size * np.sum(a ** 2)))


def _mean(x: Any) -> float:
    a = np.asarray(list(x), dtype=float)
    a = a[~np.isnan(a)]
    return float(a.mean()) if a.size else NAN


def _p95(x: Any) -> float:
    a = np.asarray(list(x), dtype=float)
    a = a[~np.isnan(a)]
    return float(np.percentile(a, 95)) if a.size else NAN


# ============================================================================ loaders
def load_meta(run_dir: Path) -> dict[str, Any]:
    meta = json.loads((run_dir / "run_meta.json").read_text())
    cfg = meta.get("run_config") or C.read_run_config(run_dir / "run_config.json")
    meta["cfg"] = cfg
    return meta


def load_flows(run_dir: Path, t0: float) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, float]]:
    """(iperf intervals with sta/slice/w, irtt probes with sta/w, launch times)."""
    launch_f = run_dir / "iperf" / "launch.csv"
    launch = pd.read_csv(launch_f) if launch_f.exists() else pd.DataFrame(columns=["sta", "slice", "launch_ts"])
    ivs, prs = [], []
    for _, r in launch.iterrows():
        f = run_dir / "iperf" / f"{r['sta']}.json"
        df = parse_iperf(f, float(r["launch_ts"]))
        df["sta"], df["slice"] = r["sta"], r["slice"]
        ivs.append(df)
        if r["slice"] == "telemedicine":
            p = parse_irtt(run_dir / "irtt" / f"{r['sta']}.json.gz")
            p["sta"] = r["sta"]
            prs.append(p)
    iv = pd.concat(ivs, ignore_index=True) if ivs else pd.DataFrame(
        columns=["ts", "seconds", "bytes", "packets", "lost", "jitter_ms", "sta", "slice"])
    pr = pd.concat(prs, ignore_index=True) if prs else pd.DataFrame(columns=["ts", "owd_ms", "ipdv_ms", "lost", "sta"])
    iv["w"] = np.floor(iv["ts"].astype(float) - t0).astype(int) if len(iv) else pd.Series(dtype=int)
    pr["w"] = np.floor(pr["ts"].astype(float) - t0).astype(int) if len(pr) else pd.Series(dtype=int)
    return iv, pr, dict(zip(launch["sta"], launch["launch_ts"].astype(float)))


# ============================================================================ windows
def flow_windows(cfg: dict[str, Any], iv: pd.DataFrame, pr: pd.DataFrame, launch: dict[str, float], t0: float,
                 sta_slice: dict[str, str], intents: dict[str, Any]) -> pd.DataFrame:
    """Per (window, sta) KPIs and SLA flag for every active flow."""
    W0, W1 = int(cfg["warmup_s"]), int(cfg["duration_s"])
    g = iv.groupby(["sta", "w"]).agg(bytes=("bytes", "sum"), packets=("packets", "sum"), lost=("lost", "sum"),
                                     jitter=("jitter_ms", "mean")) if len(iv) else pd.DataFrame()
    p_ok = pr[~pr["lost"].astype(bool)] if len(pr) else pr
    pg = p_ok.groupby(["sta", "w"]).agg(owd_p95=("owd_ms", lambda s: np.percentile(s, 95)),
                                        ipdv=("ipdv_ms", "mean"), n_rx=("owd_ms", "size")) if len(p_ok) else pd.DataFrame()
    psent = pr.groupby(["sta", "w"]).size() if len(pr) else pd.Series(dtype=int)
    rows = []
    for sta, ts in launch.items():
        sl = sta_slice.get(sta, "best_effort")
        intent = intents["slices"][sl]["intent"]
        first = int(math.ceil(ts - t0))
        for w in range(max(W0, first), W1):
            key = (sta, w)
            if len(g) and key in g.index:
                r = g.loc[key]
                thr = float(r["bytes"]) * 8 / 1e6
                pk, lost = float(r["packets"]), float(r["lost"])
                loss = 100.0 * lost / pk if pk and pk == pk and pk > 0 else (NAN if sl == "best_effort" else 0.0)
                jit = float(r["jitter"])
            else:
                thr, loss, jit = 0.0, 100.0, NAN
            owd = ipdv = NAN
            if sl == "telemedicine":
                if len(pg) and key in pg.index:
                    owd, ipdv = float(pg.loc[key, "owd_p95"]), float(pg.loc[key, "ipdv"])
                elif len(psent) and key in psent.index:
                    owd = ipdv = math.inf  # probes sent, none received
                jit = ipdv if ipdv == ipdv else jit
            if sl == "telemedicine":
                met = (thr >= intent["thr_min_mbps"] and loss <= intent["loss_max_pct"]
                       and (jit != jit or jit <= intent["jitter_max_ms"])
                       and (owd != owd or owd <= intent["owd_p95_max_ms"]))
            elif sl == "video":
                met = thr >= intent["thr_min_mbps"] and (loss != loss or loss <= intent["loss_max_pct"])
            else:
                met = True
            rows.append({"level": "sta", "w": w, "sta": sta, "slice": sl, "thr_mbps": thr, "loss_pct": loss,
                         "jitter_ms": jit, "owd_p95_ms": owd, "met": bool(met)})
    return pd.DataFrame(rows)


def assoc_and_airtime(cfg: dict[str, Any], tel: pd.DataFrame, t0: float, aps: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(per (w, sta) serving AP, per (w, ap) mean airtime)."""
    if tel.empty:
        return pd.DataFrame(columns=["w", "sta", "ap"]), pd.DataFrame(columns=["w", "ap", "airtime"])
    tel = tel.copy()
    tel["w"] = np.floor(tel["ts"] - t0).astype(int)
    assoc = (tel.groupby(["w", "sta"])["ap"].agg(lambda s: s.value_counts().index[0]).reset_index())
    per_sample = tel.groupby(["ap", "ts", "w"])["airtime_est"].sum().clip(upper=1.0).reset_index()
    air = per_sample.groupby(["w", "ap"])["airtime_est"].mean().rename("airtime").reset_index()
    full = pd.MultiIndex.from_product([sorted(tel["w"].unique()), aps], names=["w", "ap"])
    air = air.set_index(["w", "ap"]).reindex(full, fill_value=0.0).reset_index()
    return assoc, air


# ============================================================================ handovers
def handovers(cfg: dict[str, Any], ev: pd.DataFrame, t0: float) -> tuple[list[dict[str, Any]], dict[str, float]]:
    lo, hi = t0 + float(cfg["warmup_s"]), t0 + float(cfg["duration_s"])
    look = float(cfg["handover_lookback_s"])
    tpp = float(cfg["t_pingpong_s"])
    hos: list[dict[str, Any]] = []
    if ev.empty:
        return hos, {"ho_count": 0, "ho_downtime_ms_mean": NAN, "ho_downtime_ms_p95": NAN, "pingpong_prob": 0.0,
                     "ho_count_zero": 1}
    ev = ev.sort_values("ts")
    for sta, e in ev.groupby("sta"):
        last_disc = last_btm = -math.inf
        prev_ap = None
        for _, r in e.iterrows():
            ts, kind = float(r["ts"]), r["event"]
            if kind == "DISCONNECTED":
                last_disc = ts
            elif kind == "BTM_RESP" and str(r.get("status", "")).strip() in ("0", "0.0"):
                last_btm = ts
            elif kind == "CONNECTED":
                ap_to = str(r["ap_to"]) if r["ap_to"] == r["ap_to"] else ""
                if prev_ap and ap_to and ap_to != prev_ap and lo <= ts < hi:
                    start = max(last_disc, last_btm)
                    dt = (ts - start) * 1000 if ts - start <= look else NAN
                    hos.append({"ts": ts, "sta": sta, "from": prev_ap, "to": ap_to, "downtime_ms": dt})
                if ap_to:
                    prev_ap = ap_to
                last_disc = last_btm = -math.inf
    pp = 0
    by_sta: dict[str, list[dict[str, Any]]] = {}
    for h in hos:
        by_sta.setdefault(h["sta"], []).append(h)
    for lst in by_sta.values():
        for a, b in zip(lst, lst[1:]):
            if b["from"] == a["to"] and b["to"] == a["from"] and b["ts"] - a["ts"] <= tpp:
                pp += 1
    n = len(hos)
    dts = [h["downtime_ms"] for h in hos]
    return hos, {"ho_count": n, "ho_downtime_ms_mean": _mean(dts), "ho_downtime_ms_p95": _p95(dts),
                 "pingpong_prob": pp / n if n else 0.0, "ho_count_zero": int(n == 0)}


# ============================================================================ agent log
def agent_metrics(cfg: dict[str, Any], run_dir: Path) -> dict[str, float]:
    f = run_dir / "agent_log.jsonl"
    out = {k: NAN for k in ("plan_latency_ms_mean", "plan_latency_ms_p95", "exec_latency_ms_mean",
                            "plan_skipped_pct", "plan_validity_pct", "prompt_tokens_mean", "output_tokens_mean")}
    out.update({"invalid_actions": 0, "actions_per_min": 0.0})
    if not f.exists():
        return out
    entries = []
    for line in f.read_text().splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    plans = [e for e in entries if e.get("type") == "plan"]
    ticks = [e for e in entries if e.get("type") == "tick"]
    tools = [e for e in entries if e.get("type") == "tool"]
    if plans:
        out["plan_latency_ms_mean"] = _mean(p["latency_ms"] for p in plans)
        out["plan_latency_ms_p95"] = _p95(p["latency_ms"] for p in plans)
        out["plan_validity_pct"] = 100.0 * sum(bool(p.get("valid_first")) for p in plans) / len(plans)
        calls = [c for p in plans for c in p.get("calls", [])]
        if calls:
            out["prompt_tokens_mean"] = _mean(c.get("prompt_tokens", NAN) for c in calls)
            out["output_tokens_mean"] = _mean(c.get("output_tokens", NAN) for c in calls)
    if ticks:
        out["plan_skipped_pct"] = 100.0 * sum(bool(t.get("skipped")) for t in ticks) / len(ticks)
    executed = [t for t in tools if t.get("actuation") and t.get("executed", True) and t.get("guardrail") != "reject"]
    out["exec_latency_ms_mean"] = _mean(t.get("exec_latency_ms", NAN) for t in executed)
    out["actions_per_min"] = sum(bool(t.get("ok")) for t in executed) / (float(cfg["duration_s"]) / 60.0)
    rejected = sum(t.get("guardrail") in ("reject", "audit_fail") for t in tools)
    failed = sum((not t.get("ok")) for t in executed if t.get("guardrail") != "audit_fail")
    out["invalid_actions"] = rejected + failed + sum(int(p.get("invalid_items", 0) or 0) for p in plans)
    return out


# ============================================================================ host
def host_metrics(run_dir: Path, t_lo: float, t_hi: float) -> dict[str, float]:
    f = run_dir / "host.csv"
    if not f.exists():
        return {"cpu_pct_emu": NAN, "cpu_pct_llm": NAN, "ram_gb": NAN, "cpu_emu_saturated": 0}
    h = pd.read_csv(f)
    h = h[(h["ts"] >= t_lo) & (h["ts"] <= t_hi)] if len(h) else h
    emu = float(h["cpu_emu"].mean()) if "cpu_emu" in h and len(h) else NAN
    return {"cpu_pct_emu": emu, "cpu_pct_llm": float(h["cpu_llm"].mean()) if "cpu_llm" in h and len(h) else NAN,
            "ram_gb": float(h["ram_used_gb"].mean()) if len(h) else NAN, "cpu_emu_saturated": int(emu == emu and emu > 90)}


# ============================================================================ main computation
def compute(run_dir: Path) -> tuple[dict[str, Any], pd.DataFrame]:
    meta = load_meta(run_dir)
    cfg = meta["cfg"]
    t0 = float(meta["t0"])
    intents = C.load_intents()
    sta_slice = C.slice_of_sta(int(cfg["n_sta"]), cfg["slice_fractions"])
    aps = sorted(meta.get("aps", radio.ap_layout(cfg)).keys(), key=lambda s: int(s[2:]))
    W0, W1 = int(cfg["warmup_s"]), int(cfg["duration_s"])

    iv, pr, launch = load_flows(run_dir, t0)
    fw = flow_windows(cfg, iv, pr, launch, t0, sta_slice, intents)
    tel = pd.read_parquet(run_dir / "telemetry.parquet") if (run_dir / "telemetry.parquet").exists() else pd.DataFrame()
    assoc, air = assoc_and_airtime(cfg, tel, t0, aps)
    air = air[(air["w"] >= W0) & (air["w"] < W1)]

    row: dict[str, Any] = {}
    win_rows: list[dict[str, Any]] = []
    # --- slice SLA (tele / video)
    for sl, col in (("telemedicine", "tele"), ("video", "video")):
        f = fw[fw["slice"] == sl] if len(fw) else fw
        if len(f):
            per_w = f.groupby("w")["met"].agg(["all", "mean"])
            row[f"sla_{col}_pct"] = 100.0 * per_w["all"].mean()
            row[f"thr_{col}_mbps"] = float(f.groupby("w")["thr_mbps"].sum().mean())
            for w, r in per_w.iterrows():
                win_rows.append({"level": "slice", "w": int(w), "slice": sl, "met": bool(r["all"]),
                                 "pct_flows_met": 100.0 * r["mean"]})
        else:
            row[f"sla_{col}_pct"] = NAN
            row[f"thr_{col}_mbps"] = NAN
    # --- best effort per-AP aggregate
    be = fw[fw["slice"] == "best_effort"] if len(fw) else fw
    if len(be) and len(assoc):
        be = be.merge(assoc, on=["w", "sta"], how="left")
        agg = be.groupby(["w", "ap"])["thr_mbps"].sum().reset_index()
        agg["ok"] = agg["thr_mbps"] >= intents["slices"]["best_effort"]["intent"]["ap_aggregate_thr_min_mbps"]
        per_w = agg.groupby("w")["ok"].all()
        row["sla_be_pct"] = 100.0 * per_w.mean()
        for w, ok in per_w.items():
            win_rows.append({"level": "slice", "w": int(w), "slice": "best_effort", "met": bool(ok)})
    else:
        row["sla_be_pct"] = NAN
    row["thr_be_mbps"] = float(be.groupby("w")["thr_mbps"].sum().mean()) if len(be) else NAN
    row["viol_tele_pct"] = 100.0 - row["sla_tele_pct"] if row["sla_tele_pct"] == row["sla_tele_pct"] else NAN

    # --- telemedicine probe statistics and UDP loss
    prw = pr[(pr["w"] >= W0) & (pr["w"] < W1)] if len(pr) else pr
    rx = prw[~prw["lost"].astype(bool)] if len(prw) else prw
    row["owd_p95_tele_ms"] = _p95(rx["owd_ms"]) if len(rx) else NAN
    row["jitter_tele_ms"] = _mean(rx["ipdv_ms"]) if len(rx) else NAN
    ivw = iv[(iv["w"] >= W0) & (iv["w"] < W1)] if len(iv) else iv
    for sl, col in (("telemedicine", "tele"), ("video", "video")):
        s = ivw[ivw["slice"] == sl] if len(ivw) else ivw
        pk = float(s["packets"].sum()) if len(s) else 0.0
        row[f"loss_{col}_pct"] = 100.0 * float(s["lost"].sum()) / pk if pk else NAN

    # --- handovers
    evf = run_dir / "events.csv"
    ev = pd.read_csv(evf) if evf.exists() else pd.DataFrame()
    hos, hstats = handovers(cfg, ev, t0)
    row.update(hstats)

    # --- fairness
    row["jfi_airtime"] = _mean(air.groupby("w")["airtime"].apply(lambda s: jain(s.values))) if len(air) else NAN
    be_sta = be.groupby("sta")["thr_mbps"].mean() if len(be) else pd.Series(dtype=float)
    row["jfi_be_thr"] = jain(be_sta.values) if len(be_sta) else NAN
    for _, r in air.iterrows():
        win_rows.append({"level": "ap", "w": int(r["w"]), "ap": r["ap"], "airtime": float(r["airtime"])})

    # --- hotspot metrics
    row.update(hotspot_metrics(cfg, fw, air, hos, t0))
    # --- agents, host
    row.update(agent_metrics(cfg, run_dir))
    row.update(host_metrics(run_dir, t0 + W0, t0 + W1))

    params = {k: cfg.get(k) for k in PARAM_COLS}
    head = {"data_status": "REAL", "exp": cfg["exp"], "sub_exp": cfg.get("sub_exp", ""), "run_tag": cfg["run_tag"]}
    extra = {"llm_backend": cfg.get("llm", {}).get("backend", "local") if C.uses_llm(cfg) else "none",
             "trace_sha256": meta.get("trace_sha256", ""), "associated_at_ready": meta.get("associated_at_ready"),
             "hostapd_btm_ok": int(all((meta.get("hostapd_btm") or {"x": True}).values())),
             "mobility_late_samples": meta.get("mobility_late_samples", 0),
             # emulator validity (topo.configure_medium / medium_*): frames lost inside the emulator
             **{k: meta.get(k) for k in ("medium_patched", "medium_nl_drops", "medium_maxq_drops",
                                          "medium_maxq_drop_pct", "medium_cpu_pct")}}
    summary = {**head, **params, **{k: row.get(k, NAN) for k in METRIC_COLS},
               **{k: row[k] for k in ("ho_count_zero", "cpu_emu_saturated") if k in row}, **extra}
    windows = pd.concat([fw, pd.DataFrame(win_rows)], ignore_index=True) if len(fw) else pd.DataFrame(win_rows)
    return summary, windows


def hotspot_metrics(cfg: dict[str, Any], fw: pd.DataFrame, air: pd.DataFrame, hos: list[dict[str, Any]],
                    t0: float) -> dict[str, float]:
    out = {"ttr_s": NAN, "ttr_censored": NAN, "sla_tele_hot_pct": NAN, "min_sla_tele_1s_pct": NAN,
           "offloaded_sta": NAN, "ap2_airtime_peak": NAN}
    tele = fw[fw["slice"] == "telemedicine"] if len(fw) else fw
    if len(tele):
        out["min_sla_tele_1s_pct"] = float(100.0 * tele.groupby("w")["met"].mean().min())
    if int(cfg.get("hotspot_size", 0)) <= 0:
        return out
    tgt = cfg.get("hotspot_target", "ap2")
    on, end = int(cfg["hotspot_start_s"]), int(cfg["hotspot_end_s"])
    a2 = air[air["ap"] == tgt] if len(air) else air
    out["ap2_airtime_peak"] = float(a2["airtime"].max()) if len(a2) else NAN
    out["offloaded_sta"] = sum(1 for h in hos if h["from"] == tgt and t0 + on <= h["ts"] < t0 + end)
    if not len(tele):
        return out
    hot = tele[(tele["w"] >= on) & (tele["w"] < end)]
    if len(hot):
        out["sla_tele_hot_pct"] = 100.0 * hot.groupby("w")["met"].all().mean()
    # geometric cell membership from the trace
    try:
        from common.mobility import load_trace

        times, names, P = load_trace(Path(cfg["trace_file"]))
        layout = radio.ap_layout(cfg)
        in_cell: dict[int, set[str]] = {}
        for w in range(on, end):
            k = int(np.argmin(np.abs(times - (w + 0.5))))
            in_cell[w] = {n for j, n in enumerate(names) if radio.nearest_ap(P[k, j, 0], P[k, j, 1], layout) == tgt}
    except (OSError, KeyError, ValueError):
        in_cell = {w: set(tele["sta"]) for w in range(on, end)}
    met_w: dict[int, bool] = {}
    for w in range(on, end):
        sub = tele[(tele["w"] == w) & (tele["sta"].isin(in_cell.get(w, set())))]
        met_w[w] = bool(sub["met"].all()) if len(sub) else True
    first_viol = next((w for w in range(on, end) if not met_w[w]), None)
    if first_viol is None:
        out["ttr_s"], out["ttr_censored"] = 0.0, 0
        return out
    n = int(cfg.get("ttr_consecutive", 3))
    for w in range(first_viol, end - n + 1):
        if all(met_w[w + i] for i in range(n)):
            out["ttr_s"], out["ttr_censored"] = float(w - on), 0
            return out
    out["ttr_s"], out["ttr_censored"] = float(end - on), 1
    return out


def run_kpi(run_dir: Path) -> dict[str, Any]:
    summary, windows = compute(run_dir)
    windows.to_csv(run_dir / "kpi_windows.csv", index=False)
    C.write_json(run_dir / "summary_row.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir")
    ap.add_argument("--json", action="store_true", help="print the summary row as JSON")
    a = ap.parse_args(argv)
    C.setup_logging()
    s = run_kpi(Path(a.run_dir))
    if a.json:
        print(json.dumps(s, indent=1, default=str))
    else:
        LOG.info("sla_tele_pct=%.1f pingpong=%.3f ttr=%s", s["sla_tele_pct"], s["pingpong_prob"], s["ttr_s"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
