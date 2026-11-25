#!/usr/bin/env python3
"""Experiment 10b: offline intent-change benchmark (Exp 9 harness, 4 APs / 30 STAs, no emulator).

An operator changes the policy without touching code: the SLM planner gets one natural-language policy
sentence appended to its system prompt; the rules-only planner (``none_rule``) is unchanged, as it would be
until someone rewrites it. Both plan on the same synthetic states. Per call we record plan validity
(guardrail) and compliance with the policy. A control arm runs the SLM with the unmodified prompt.

Usage: intent_bench.py TESTBED OUTCSV [MODEL] [N_CALLS]
"""
import csv
import sys
import types
from pathlib import Path

import numpy as np

testbed, out = Path(sys.argv[1]), Path(sys.argv[2])
MODEL = sys.argv[3] if len(sys.argv) > 3 else "qwen3.5:4b"
N_CALLS = int(sys.argv[4]) if len(sys.argv) > 4 else 30
sys.path.insert(0, str(testbed))
sys.modules.setdefault("zmq", types.ModuleType("zmq"))  # tool server not used offline
from common import config as C  # noqa: E402
from common.agents.common import ap_aggregates, load_prompt, load_schema, planner_user_message, system_with_intents  # noqa: E402
from common.agents.guardrail import Guardrail  # noqa: E402
from common.agents.planner import rule_plan  # noqa: E402
from common.agents.synthetic import FakeCache, topology  # noqa: E402
from common.llm import LLMClient  # noqa: E402

VIDEO_RATE = 2.5  # Mb/s per video station (intents.json)
POLICIES = {
    "video_first": "Video now has the highest priority, above telemedicine. On every AP whose video SLA is below "
                   "100 %, protect video with about 2.5 Mb/s per video station plus 30 % headroom.",
    "drain_ap2": "ap2 is being drained for maintenance: never offload any station to ap2, and offload best_effort "
                 "and video stations from ap2 to its neighbours.",
    "power_cap": "A new regulatory limit caps every AP at 10 dBm: every AP whose power is above 10 dBm must get a "
                 "set_power goal of at most 10 dBm.",
}


def goals(plan, ap=None):
    for e in (plan or {}).get("plan", []) if isinstance(plan, dict) else []:
        if isinstance(e, dict) and (ap is None or e.get("ap") == ap):
            yield from (g for g in e.get("goals", []) if isinstance(g, dict))


def comply(policy, plan, agg):
    """(applicable, compliant) of one plan for one policy on the state summarised by ``agg``."""
    if policy == "video_first":
        need = [a for a, v in agg.items() if v["n"]["video"] and v["sla_video"] == v["sla_video"] and v["sla_video"] < 100]
        if not need:
            return 0, 0
        ok = all(any(g.get("type") == "protect_slice" and g.get("slice") == "video"
                     and float(g.get("min_rate_mbps", 0)) >= VIDEO_RATE * agg[a]["n"]["video"] for g in goals(plan, a))
                 for a in need)
        return 1, int(ok)
    if policy == "drain_ap2":
        into = any(g.get("type") == "offload" and "ap2" in (g.get("to") or []) for g in goals(plan))
        out_ = any(g.get("type") == "offload" for g in goals(plan, "ap2"))
        return 1, int(out_ and not into)
    if policy == "power_cap":
        hot = [a for a, v in agg.items() if float(v["pwr"]) > 10]
        if not hot:
            return 0, 0
        cap = all(any(g.get("type") == "set_power" and float(g.get("dbm", 99)) <= 10 for g in goals(plan, a)) for a in hot)
        over = any(g.get("type") == "set_power" and float(g.get("dbm", 0)) > 10 for g in goals(plan))
        return 1, int(cap and not over)
    raise ValueError(policy)


cfg = C.load_base()
cfg.update({"exp": 9, "framework": "P_HIER_AGENT", "prompt_kind": "planner", "planner_model": MODEL, "seed": 1,
            "n_ap": 4, "n_sta": 30})
# qwen3.5:4b/0.8b are pinned on the GPU node (keep_alive -1): do not change their state; any other model is
# loaded only for this bench and unloaded at the end (keep_alive 0)
PINNED = MODEL in ("qwen3.5:4b", "qwen3.5:0.8b")
cfg["llm"] = dict(cfg["llm"], keep_alive=-1 if PINNED else "10m")
topo = topology(cfg)
aps = sorted(topo["aps"], key=lambda s: int(s[2:]))
schema = load_schema("subgoal.schema.json", aps)
guard = Guardrail(cfg, topo, schema, load_schema("actions.schema.json", aps))
base_system = system_with_intents(load_prompt("planner_system.txt"), cfg, topo)
llm = LLMClient(cfg, MODEL, role="planner")
w = llm.warmup()
if not w.ok:
    sys.exit(f"warm-up failed: {w.error}")

rows = []
# arms: control (unmodified prompt, scored against every policy); <policy>/system (policy appended to the
# system prompt); <policy>/both (also restated at the end of each user message)
ARMS = [("control", None, None)] + [(f"{p}/{w}", p, w) for p in POLICIES for w in ("system", "both")]
for arm, pol_arm, where in ARMS:
    note = "" if pol_arm is None else "Operator policy update (overrides the guidelines above where they conflict): " + POLICIES[pol_arm]
    system = base_system + ("\n\n" + note if note else "")
    rng = np.random.default_rng(202)  # same states in every arm
    for i in range(N_CALLS):
        cache = FakeCache(cfg, rng, hotspot=bool(i % 2))
        agg = ap_aggregates(cache, topo)
        rp = rule_plan(agg, cfg)
        user = planner_user_message(cache, topo, 120.0 + i * 10, i + 1, None)
        if where == "both":
            user += "\n" + note
        r = llm.chat(system, user, schema)
        se, sem = guard.check_plan(r.parsed) if r.parsed is not None else (["unparseable"], [])
        valid = int(r.ok and not (se or sem))
        for pol in POLICIES if pol_arm is None else [pol_arm]:
            a_s, c_s = comply(pol, r.parsed if valid else None, agg)
            a_r, c_r = comply(pol, rp, agg)
            rows.append({"arm": arm, "policy": pol, "call": i + 1, "hotspot": i % 2, "slm_valid": valid,
                         "applicable": a_s, "slm_comply": c_s, "rules_comply": c_r,
                         "latency_ms": r.latency_ms, "output_tokens": r.output_tokens,
                         "slm_plan": (r.content or "")[:2000], "error": (r.error or "")[:100]})
    g = [x for x in rows if x["arm"] == arm]
    for pol in sorted({x["policy"] for x in g}):
        h = [x for x in g if x["policy"] == pol and x["applicable"]]
        print(f"{arm:18s} {pol:12s} n={len(h):2d} valid {100*np.mean([x['slm_valid'] for x in h]):5.1f} %  "
              f"SLM comply {100*np.mean([x['slm_comply'] for x in h]):5.1f} %  rules comply "
              f"{100*np.mean([x['rules_comply'] for x in h]):5.1f} %", flush=True)

if not PINNED:
    llm.client.generate(model=MODEL, prompt="", keep_alive=0)  # unload

with out.open("w", newline="") as f:
    wr = csv.DictWriter(f, fieldnames=list(rows[0]))
    wr.writeheader()
    wr.writerows(rows)
