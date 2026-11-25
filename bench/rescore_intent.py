#!/usr/bin/env python3
"""Experiment 10b: rescore the intent-change bench (intent_bench.py output) without new model calls.

Regenerates the bench's synthetic states (same config, rng seed 202, hotspot every other call) and, for every
row, scores the stored SLM plan and the rules-only plan of that state:
  * partial compliance: the share of the policy's requirements that the plan meets
    - video_first: share of APs with video SLA < 100 % that get protect_slice(video) >= 2.5 Mb/s per video STA
    - drain_ap2:   mean of (offloads out of ap2) and (no offload into ap2)
    - power_cap:   share of APs above 10 dBm that get set_power <= 10 dBm, 0 if any set_power > 10 dBm
  * policy as a plan-guardrail rule: the plan after a deterministic rewrite that enforces the policy,
    whether the rewritten plan still passes the plan guardrail, whether it complies, and how many goals changed.
Invalid plans score 0, as in the strict metric.

Usage: rescore_intent.py TESTBED OUTCSV BENCHCSV [BENCHCSV ...]
"""
import copy
import json
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd

testbed, out = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(testbed))
sys.modules.setdefault("zmq", types.ModuleType("zmq"))
from common import config as C  # noqa: E402
from common.agents.common import ap_aggregates, load_schema  # noqa: E402
from common.agents.guardrail import Guardrail  # noqa: E402
from common.agents.planner import rule_plan  # noqa: E402
from common.agents.synthetic import FakeCache, topology  # noqa: E402

VIDEO_RATE = 2.5
MAX_GOALS = 4


def goals(plan, ap=None):
    for e in (plan or {}).get("plan", []) if isinstance(plan, dict) else []:
        if isinstance(e, dict) and (ap is None or e.get("ap") == ap):
            yield from (g for g in e.get("goals", []) if isinstance(g, dict))


def need_video(agg):
    return [a for a, v in agg.items() if v["n"]["video"] and v["sla_video"] == v["sla_video"] and v["sla_video"] < 100]


def partial(policy, plan, agg):
    if plan is None:
        return 0.0
    if policy == "video_first":
        need = need_video(agg)
        ok = [any(g.get("type") == "protect_slice" and g.get("slice") == "video"
                  and float(g.get("min_rate_mbps", 0)) >= VIDEO_RATE * agg[a]["n"]["video"] for g in goals(plan, a))
              for a in need]
        return float(np.mean(ok)) if ok else float("nan")
    if policy == "drain_ap2":
        into = any(g.get("type") == "offload" and "ap2" in (g.get("to") or []) for g in goals(plan))
        out_ = any(g.get("type") == "offload" for g in goals(plan, "ap2"))
        return (float(out_) + float(not into)) / 2
    if policy == "power_cap":
        hot = [a for a, v in agg.items() if float(v["pwr"]) > 10]
        if any(g.get("type") == "set_power" and float(g.get("dbm", 0)) > 10 for g in goals(plan)):
            return 0.0
        ok = [any(g.get("type") == "set_power" and float(g.get("dbm", 99)) <= 10 for g in goals(plan, a)) for a in hot]
        return float(np.mean(ok)) if ok else float("nan")
    raise ValueError(policy)


def entry(plan, ap):
    for e in plan["plan"]:
        if e.get("ap") == ap:
            return e
    e = {"ap": ap, "goals": []}
    plan["plan"].append(e)
    return e


def put(e, goal, replace_type):
    """Add ``goal`` to an AP entry, replacing a goal of ``replace_type`` or a hold, or the last goal if full."""
    gs = [g for g in e["goals"] if g.get("type") != "hold"]
    for i, g in enumerate(gs):
        if g.get("type") == replace_type and (replace_type != "protect_slice" or g.get("slice") == goal.get("slice")):
            gs[i] = goal
            e["goals"] = gs
            return
    if len(gs) >= MAX_GOALS:
        gs = gs[:MAX_GOALS - 1]
    e["goals"] = gs + [goal]


def enforce(policy, plan, agg):
    """Rewrite ``plan`` so that it implements ``policy``; returns (plan, number of goals changed)."""
    p = copy.deepcopy(plan) if isinstance(plan, dict) and "plan" in plan else {"plan": []}
    before = json.dumps(p, sort_keys=True)
    if policy == "video_first":
        for a in need_video(agg):
            r = round(VIDEO_RATE * agg[a]["n"]["video"], 1)
            if not any(g.get("type") == "protect_slice" and g.get("slice") == "video"
                       and float(g.get("min_rate_mbps", 0)) >= r for g in goals(p, a)):
                put(entry(p, a), {"type": "protect_slice", "slice": "video", "min_rate_mbps": r}, "protect_slice")
    elif policy == "drain_ap2":
        for e in p["plan"]:
            kept = []
            for g in e.get("goals", []):
                if g.get("type") == "offload" and "ap2" in (g.get("to") or []):
                    g = dict(g, to=[t for t in g["to"] if t != "ap2"])
                    if not g["to"]:
                        continue
                kept.append(g)
            e["goals"] = kept
        if not any(g.get("type") == "offload" for g in goals(p, "ap2")):
            put(entry(p, "ap2"), {"type": "offload", "count": 2, "to": list(agg["ap2"]["nbrs"])[:2],
                                  "prefer_slice": "best_effort"}, "offload")
    elif policy == "power_cap":
        for e in p["plan"]:
            for g in e.get("goals", []):
                if g.get("type") == "set_power" and float(g.get("dbm", 0)) > 10:
                    g["dbm"] = 10
        for a, v in agg.items():
            if float(v["pwr"]) > 10 and not any(g.get("type") == "set_power" for g in goals(p, a)):
                put(entry(p, a), {"type": "set_power", "dbm": 10}, "set_power")
    for e in p["plan"]:
        if not e["goals"]:
            e["goals"] = [{"type": "hold"}]
    gb = [json.dumps(g, sort_keys=True) for g in goals(json.loads(before))]
    ga = [json.dumps(g, sort_keys=True) for g in goals(p)]
    changed = len(set(ga) - set(gb)) + len(set(gb) - set(ga))
    return p, changed


cfg = C.load_base()
cfg.update({"exp": 9, "framework": "P_HIER_AGENT", "prompt_kind": "planner", "seed": 1, "n_ap": 4, "n_sta": 30})
topo = topology(cfg)
aps = sorted(topo["aps"], key=lambda s: int(s[2:]))
schema = load_schema("subgoal.schema.json", aps)
guard = Guardrail(cfg, topo, schema, load_schema("actions.schema.json", aps))


def valid(plan):
    se, sem = guard.check_plan(plan)
    return int(not (se or sem))


rows = []
for f in sys.argv[3:]:
    model = Path(f).stem.replace("intent_bench_", "")
    b = pd.read_csv(f)
    n = int(b.call.max())
    rng = np.random.default_rng(202)
    states = []
    for i in range(n):
        cache = FakeCache(cfg, rng, hotspot=bool(i % 2))
        states.append(ap_aggregates(cache, topo))
    for r in b.itertuples():
        if not r.applicable:
            continue
        agg = states[r.call - 1]
        try:
            slm = json.loads(r.slm_plan) if isinstance(r.slm_plan, str) else None
        except json.JSONDecodeError:
            slm = None
        rp = rule_plan(agg, cfg)
        row = {"model": model, "arm": r.arm, "policy": r.policy, "call": r.call, "slm_valid": r.slm_valid,
               "slm_comply": r.slm_comply, "rules_comply": r.rules_comply,
               "slm_partial": partial(r.policy, slm if r.slm_valid else None, agg),
               "rules_partial": partial(r.policy, rp, agg)}
        for who, plan, ok in (("slm", slm, r.slm_valid), ("rules", rp, 1)):
            ep, ch = enforce(r.policy, plan if ok else {"plan": []}, agg)
            ev = valid(ep)
            row.update({f"{who}_enf_valid": ev, f"{who}_enf_comply": int(ev and partial(r.policy, ep, agg) == 1.0),
                        f"{who}_enf_changed": ch})
        rows.append(row)
pd.DataFrame(rows).to_csv(out, index=False)
d = pd.DataFrame(rows)
print(d.groupby(["model", "policy", "arm"])[["slm_valid", "slm_comply", "slm_partial", "slm_enf_valid", "slm_enf_comply",
                                            "slm_enf_changed", "rules_partial", "rules_enf_comply"]].mean().round(2)
      .to_string())
