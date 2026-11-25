#!/usr/bin/env python3
"""Experiment 1d — SLM latency on the pinned Ollama cores (no emulator, no root).

For one (planner_model, prompt_kind) unit: one warm-up call (excluded), then ``calls`` calls with
a fresh synthetic state each (planner_n4 = Meta-Planner prompt for n_ap 4; flat_n30 = flat-agent
prompt for n_sta 30), ``temperature=0``, seeded, JSON-schema output.
Experiment 9: ``bench_n_ap`` / ``bench_n_sta`` set the synthetic network size (default
4 / 30), and any ``prompt_kind`` starting with ``planner`` or ``flat`` selects that prompt.
Rows → ``output/experiment-1/1d_slm_latency.csv``; unit summary → ``summary_exp1.csv``.

Smoke test (Phase 3)::   .venv/bin/python experiment-1/slm_bench.py --calls 2
Campaign::               ./experiment-1/run.sh 1d          (or --cloud for Ollama Cloud models)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import config as C  # noqa: E402
from common import runstate as RS  # noqa: E402
from common.agents.common import (flat_user_message, load_prompt, load_schema, planner_user_message,  # noqa: E402
                                  system_with_intents)
from common.agents.guardrail import Guardrail  # noqa: E402
from common.agents.synthetic import FakeCache, topology  # noqa: E402

LOG = logging.getLogger("slm_bench")


def bench(cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    from common.llm import LLMClient, RateLimited, reserve_cloud_budget

    kind = cfg["prompt_kind"]
    bcfg = dict(cfg, n_ap=int(cfg.get("bench_n_ap", 4)), n_sta=int(cfg.get("bench_n_sta", 30)))
    is_planner = kind.startswith("planner")
    topo = topology(bcfg)
    aps = sorted(topo["aps"], key=lambda s: int(s[2:]))
    plan_schema = load_schema("subgoal.schema.json", aps)
    act_schema = load_schema("actions.schema.json", aps)
    guard = Guardrail(bcfg, topo, plan_schema, act_schema)
    if is_planner:
        system, schema = system_with_intents(load_prompt("planner_system.txt"), bcfg, topo), plan_schema
    else:
        system, schema = system_with_intents(load_prompt("flat_system.txt"), bcfg, topo), act_schema
    n_calls = int(cfg.get("calls", 30))
    try:
        reserve_cloud_budget(cfg, n_calls + 1, (n_calls + 1) * (1100 if is_planner else 2200))
    except RateLimited:
        return [], 75
    llm = LLMClient(cfg, cfg["planner_model"], role="planner")
    w = llm.warmup()
    if not w.ok:
        LOG.error("warm-up failed for %s: %s", llm.model, w.error)
        return [], 75 if w.rate_limited else 2
    rng = np.random.default_rng(int(cfg["seed"]) * 101)
    rows = []
    for i in range(n_calls):
        cache = FakeCache(bcfg, rng, hotspot=bool(i % 2))
        user = (planner_user_message(cache, topo, 120.0 + i * 10, i + 1, None) if is_planner
                else flat_user_message(cache, topo, 120.0 + i * 10))
        r = llm.chat(system, user, schema)
        if r.rate_limited:
            return rows, 75
        if is_planner:
            se, sem = guard.check_plan(r.parsed) if r.parsed is not None else (["unparseable"], [])
            valid = r.ok and not (se or sem)
        else:
            acts = (r.parsed or {}).get("actions", []) if isinstance(r.parsed, dict) else None
            valid = r.ok and acts is not None and all(
                not guard.check_action(a, cache.latest(), {a2: 14.0 for a2 in aps}) for a in acts if isinstance(a, dict))
        rows.append({"data_status": "REAL", "run_tag": cfg["run_tag"], "planner_model": cfg["planner_model"],
                     "planner_model_effective": llm.model, "llm_backend": llm.backend, "prompt_kind": kind,
                     "bench_n_ap": bcfg["n_ap"], "bench_n_sta": bcfg["n_sta"],
                     "call": i + 1, "plan_latency_ms": r.latency_ms, "prompt_tokens": r.prompt_tokens,
                     "output_tokens": r.output_tokens, "gen_tok_per_s": r.gen_tok_per_s,
                     "prompt_tok_per_s": r.prompt_tok_per_s, "load_ms": r.load_ms,
                     "net_ms": r.extra.get("net_ms", float("nan")), "valid": int(valid), "ok": int(r.ok),
                     "num_thread": cfg.get("llm", {}).get("num_thread"), "error": r.error[:120]})
    return rows, 0


def summary_row(cfg: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    lat = np.array([r["plan_latency_ms"] for r in rows if r["ok"]], dtype=float)
    def m(k: str) -> float:
        a = np.array([r[k] for r in rows if r["ok"]], dtype=float)
        return float(np.nanmean(a)) if a.size else float("nan")
    return {"data_status": "REAL", "exp": cfg.get("exp", 1), "sub_exp": cfg.get("sub_exp", "1d"), "run_tag": cfg["run_tag"],
            "bench_n_ap": int(cfg.get("bench_n_ap", 4)), "bench_n_sta": int(cfg.get("bench_n_sta", 30)),
            "framework": cfg["framework"], "planner_model": cfg["planner_model"],
            "planner_model_effective": rows[0]["planner_model_effective"] if rows else "",
            "llm_backend": cfg.get("llm", {}).get("backend", "local"), "prompt_kind": cfg["prompt_kind"],
            "calls": len(rows), "plan_latency_ms_mean": float(lat.mean()) if lat.size else float("nan"),
            "plan_latency_ms_p95": float(np.percentile(lat, 95)) if lat.size else float("nan"),
            "prompt_tokens_mean": m("prompt_tokens"), "output_tokens_mean": m("output_tokens"),
            "gen_tok_per_s": m("gen_tok_per_s"), "prompt_tok_per_s": m("prompt_tok_per_s"),
            "plan_validity_pct": 100.0 * sum(r["valid"] for r in rows) / len(rows) if rows else float("nan")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-config")
    ap.add_argument("--calls", type=int, help="override number of calls (smoke test)")
    ap.add_argument("--model", help="smoke test model (default: planner_model of defaults.yaml)")
    ap.add_argument("--prompt-kind", default="planner_n4", choices=["planner_n4", "flat_n30"])
    a = ap.parse_args(argv)
    C.setup_logging()
    if a.run_config:
        cfg = C.read_run_config(a.run_config)
        smoke = False
    else:  # smoke test: results go to output/experiment-1/_smoke, nothing is summarised
        cfg = C.load_base()
        cfg.update({"exp": 1, "sub_exp": "1d", "framework": "P_HIER_AGENT", "prompt_kind": a.prompt_kind,
                    "planner_model": a.model or cfg["planner_model"]})
        cfg["run_tag"] = "smoke_" + C.model_short(cfg["planner_model"]) + "_" + a.prompt_kind
        smoke = True
    if a.calls:
        cfg["calls"] = a.calls
    rows, rc = bench(cfg)
    for r in rows:
        LOG.info("call %d: %.0f ms, %d+%d tok, valid=%d %s", r["call"], r["plan_latency_ms"], r["prompt_tokens"],
                 r["output_tokens"], r["valid"], r["error"])
    if rc != 0:
        return rc
    if smoke:
        out = C.output_dir(cfg) / "experiment-1" / "_smoke" / "1d_smoke.csv"
        RS.append_rows(out, rows)
        LOG.info("smoke test ok (%d calls) -> %s", len(rows), out)
        return 0
    raw = Path(cfg["raw_csv"])
    RS.delete_rows(raw, cfg["run_tag"])
    RS.append_rows(raw, rows)
    RS.upsert_rows(Path(cfg["summary_csv"]), [summary_row(cfg, rows)])
    run_dir = Path(cfg["run_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)
    RS.mark_done(run_dir, {"run_tag": cfg["run_tag"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
