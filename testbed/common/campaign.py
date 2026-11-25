#!/usr/bin/env python3
"""Expand an experiment's ``config.yaml`` into the ordered list of runs.

Ordering is *interleaved, seed-major*: outer loop over seeds, then the grid in the order the
keys appear in ``config.yaml`` (``framework`` innermost), so slow host drifts affect all
frameworks equally.
``shuffle_within_seed: true`` in an experiment's config.yaml shuffles the runs inside each seed
block (seeded by the seed number).

CLI (used by ``common/runlib.sh``)::

    campaign.py plan     --exp 2 [--sub 5a]      # writes run configs, prints TSV run list
    campaign.py status   --exp 2                 # done / pending / failed counts
    campaign.py estimate --exp 5 [--sub 5a]      # LLM calls/tokens of pending runs (cloud budget)
    campaign.py fresh    --exp 2 [--sub 5a]      # archive the selected runs so they start again
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import config as C  # noqa: E402
from common import runstate as RS  # noqa: E402

LOG = logging.getLogger("campaign")


def exp_config(exp: int) -> dict[str, Any]:
    return C.load_yaml(C.TESTBED / f"experiment-{exp}" / "config.yaml")


def _grid_points(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Cartesian product in key order; a dict value is merged into the point (zipped params)."""
    keys = list(grid.keys())
    points = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        pt: dict[str, Any] = {}
        for k, v in zip(keys, combo):
            if isinstance(v, dict):
                pt.update(v)
            else:
                pt[k] = v
        points.append(pt)
    return points


def _sub_blocks(ecfg: dict[str, Any], sub: str | None,
                include_optional: bool = False) -> list[tuple[str, dict[str, Any]]]:
    if "sub_experiments" in ecfg:
        subs = ecfg["sub_experiments"]
        if sub and sub not in subs:
            raise SystemExit(f"unknown sub-experiment {sub}; choose from {list(subs)}")
        names = [sub] if sub else [s for s in subs if include_optional or not subs[s].get("optional")]
        out = []
        for s in names:
            blk = C.deep_merge({k: v for k, v in ecfg.items() if k != "sub_experiments"}, subs[s])
            blk["fixed"] = C.deep_merge(ecfg.get("fixed", {}), subs[s].get("fixed", {}))
            out.append((s, blk))
        return out
    return [("", ecfg)]


def apply_cloud_overrides(ecfg: dict[str, Any], sub: str | None) -> dict[str, Any]:
    """Cost-efficient cloud variant: fewer seeds, cloud-model grids, restricted sub-experiments.

    ``OJSDN_CLOUD_SEEDS=all`` keeps the full local seed list.
    """
    import os

    cl = ecfg.get("cloud", {}) or {}
    out = C.deep_merge(ecfg, {})
    keep_all = os.environ.get("OJSDN_CLOUD_SEEDS", "") == "all"
    if cl.get("seeds") and not keep_all:
        out["seeds"] = cl["seeds"]
        for s in out.get("sub_experiments", {}).values():
            if "seeds" in s:
                s["seeds"] = [x for x in s["seeds"] if x in cl["seeds"]] or s["seeds"][:1]
    if cl.get("fixed"):
        out["fixed"] = C.deep_merge(out.get("fixed", {}), cl["fixed"])
    for sname, grid in (cl.get("grid_override") or {}).items():
        blk = out["sub_experiments"][sname]
        blk["grid"] = C.deep_merge(blk.get("grid", {}), grid)
    return out


def build_runs(exp: int, sub: str | None = None, base: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return ordered, de-duplicated run configs (dicts) for experiment ``exp``.

    Sub-experiment membership (``sub_exps``) is always computed over *all* sub-experiments, so a
    shared point (Exp 5 default configuration) carries every sub-experiment label no matter
    which sub-experiment is being run; it is executed once and summarised once per label.
    """
    base = base if base is not None else C.load_base()
    ecfg = exp_config(exp)
    cloud = base.get("llm", {}).get("backend") == "cloud"
    if cloud:
        ecfg = apply_cloud_overrides(ecfg, sub)
    all_blocks = _sub_blocks(ecfg, None, include_optional=True)
    selected = {s for s, _ in _sub_blocks(ecfg, sub)}
    if cloud and ecfg.get("cloud", {}).get("subs"):
        selected &= set(ecfg["cloud"]["subs"])
        if not selected:
            raise SystemExit(f"sub-experiment {sub} has no cloud variant; cloud subs: {ecfg['cloud']['subs']}")
    seeds_all = sorted({s for _, b in all_blocks for s in b["seeds"]})
    out_root = C.output_dir(base)
    runs: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for seed in seeds_all:
        for sname, blk in all_blocks:
            if seed not in blk["seeds"]:
                continue
            for pt in _grid_points(blk.get("grid", {})):
                cfg = C.deep_merge(base, blk.get("fixed", {}))
                cfg = C.deep_merge(cfg, pt)
                cfg["guardrail"] = C.normalise_onoff(cfg.get("guardrail", "on"))
                cfg["exp"] = exp
                cfg["seed"] = int(seed)
                cfg["run_id"] = int(seed)
                if cfg.get("hotspot_size") == "auto":
                    cfg["hotspot_size"] = C.hotspot_size_for(int(cfg["n_sta"]))
                cfg["entry"] = blk.get("entry", "common/run_one.py")
                tag = C.run_tag(cfg, list(blk.get("key_params", [])))
                if tag in runs:
                    if sname and sname not in runs[tag]["sub_exps"]:
                        runs[tag]["sub_exps"].append(sname)
                    continue
                exp_out = out_root / f"experiment-{exp}"
                cfg.update({
                    "run_tag": tag,
                    "sub_exp": sname,
                    "sub_exps": [sname] if sname else [],
                    "entry": blk.get("entry", "common/run_one.py"),
                    "needs_root": bool(blk.get("needs_root", True)),
                    "needs_emulator": bool(blk.get("needs_emulator", True)),
                    "exp_dir": str(exp_out),
                    "run_dir": str(exp_out / tag),
                    "summary_csv": str(exp_out / blk.get("summary", f"summary_exp{exp}.csv")),
                    "raw_csv": str(exp_out / blk["output_csv"]) if blk.get("output_csv") else "",
                    "trace_file": str(out_root / "traces" / C.trace_name(cfg)),
                })
                cfg.setdefault("paths", {})["testbed"] = str(C.TESTBED)
                cfg["planner_model_effective"] = (
                    C.effective_model(cfg, cfg["planner_model"])
                    if cfg["planner_model"] != "none_rule" else "none_rule")
                runs[tag] = cfg
                order.append(tag)
    if ecfg.get("shuffle_within_seed"):
        # random (seeded) framework order inside each seed block, so a slow drift within a
        # block cannot favour the framework that always runs first
        import random
        by_seed: dict[int, list[str]] = {}
        for t in order:
            by_seed.setdefault(runs[t]["seed"], []).append(t)
        order = [t for s in sorted(by_seed) for t in random.Random(1000 + s).sample(by_seed[s], len(by_seed[s]))]
    out = [runs[t] for t in order]
    if selected != {""}:
        out = [r for r in out if set(r["sub_exps"]) & selected]
    return out


def expected_count(exp: int, sub: str | None = None) -> int:
    ecfg = exp_config(exp)
    return sum(int(b.get("expected_runs", 0)) for _, b in _sub_blocks(ecfg, sub))


def run_status(cfg: dict[str, Any]) -> str:
    run_dir = Path(cfg["run_dir"])
    exp_out = Path(cfg["exp_dir"])
    if (run_dir / RS.DONE).exists():
        rows = RS.read_csv_rows(Path(cfg["summary_csv"]))
        have = {r.get("sub_exp", "") for r in rows if r.get("run_tag") == cfg["run_tag"]}
        need = set(cfg["sub_exps"]) or {""}
        if need <= have or (need == {""} and have):
            return "done"
        return "resummarize"
    if RS.get_attempts(exp_out, cfg["run_tag"]) >= int(cfg.get("max_attempts", 3)):
        return "failed"
    return "pending"


def llm_calls_estimate(cfg: dict[str, Any]) -> dict[str, float]:
    """Rough LLM usage of one run (planner/flat calls + optional slm sub-agents)."""
    calls = tokens = sub_calls = 0.0
    if cfg.get("entry", "").endswith("slm_bench.py"):
        calls = float(cfg.get("calls", 30)) + 1
        tokens = calls * (2200 if cfg.get("prompt_kind") == "flat_n30" else 1100)
    elif C.uses_llm(cfg):
        ticks = float(cfg["duration_s"]) / float(cfg["plan_interval_s"])
        calls = ticks * 1.1  # ~10 % re-prompts
        if cfg["framework"] == "B2_FLAT_AGENT":
            per = 600 + 65 * int(cfg["n_sta"]) + 250
        else:
            per = 600 + 75 * int(cfg["n_ap"]) + 250
        tokens = calls * per
        if cfg["framework"] == "P_HIER_AGENT" and cfg.get("subagent_mode") == "slm":
            sub_calls = int(cfg["n_ap"]) * float(cfg["duration_s"]) / 2.0
    return {"calls": calls, "tokens": tokens, "subagent_calls": sub_calls}


def write_configs(runs: list[dict[str, Any]]) -> list[Path]:
    paths = []
    for cfg in runs:
        p = Path(cfg["exp_dir"]) / "_configs" / f"{cfg['run_tag']}.json"
        C.write_json(p, cfg)
        paths.append(p)
    return paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["plan", "status", "estimate", "fresh"])
    ap.add_argument("--exp", type=int, required=True)
    ap.add_argument("--sub", default=None)
    ap.add_argument("--no-write", action="store_true", help="do not write run-config files")
    ap.add_argument("--only", default=None, help="restrict to one run tag")
    args = ap.parse_args(argv)
    logging.basicConfig(level="WARNING")
    sub = args.sub or None
    runs = build_runs(args.exp, sub)
    if args.only:
        runs = [r for r in runs if r["run_tag"] == args.only]

    if args.cmd == "plan":
        paths = write_configs(runs) if not args.no_write else [Path("-")] * len(runs)
        for cfg, p in zip(runs, paths):
            print("\t".join([cfg["run_tag"], cfg["entry"], str(p), run_status(cfg),
                             "root" if cfg["needs_root"] else "user", ",".join(cfg["sub_exps"])]))
        return 0

    if args.cmd == "status":
        st = {"done": 0, "pending": 0, "failed": 0, "resummarize": 0}
        for cfg in runs:
            st[run_status(cfg)] += 1
        exp_n = expected_count(args.exp, sub)
        print(json.dumps({"exp": args.exp, "sub": sub, "planned": len(runs),
                          "expected_rows": exp_n, **st}))
        return 0

    if args.cmd == "fresh":
        import shutil
        import time as _t

        stamp = _t.strftime("%Y%m%d-%H%M%S")
        for cfg in runs:
            rd = Path(cfg["run_dir"])
            if rd.exists():
                dest = C.output_dir(C.load_base()) / "_archive" / f"experiment-{args.exp}" / f"{rd.name}.{stamp}"
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(rd), str(dest))
            for csvp in (cfg["summary_csv"], cfg.get("raw_csv")):
                if csvp:
                    RS.delete_rows(Path(csvp), cfg["run_tag"])
            RS.reset_attempts(Path(cfg["exp_dir"]), cfg["run_tag"])
        print(f"archived {len(runs)} run(s) of experiment {args.exp}")
        return 0

    # estimate
    tot = {"runs": 0, "calls": 0.0, "tokens": 0.0, "subagent_calls": 0.0}
    for cfg in runs:
        if run_status(cfg) != "pending":
            continue
        e = llm_calls_estimate(cfg)
        if e["calls"] or e["subagent_calls"]:
            tot["runs"] += 1
            for k in ("calls", "tokens", "subagent_calls"):
                tot[k] += e[k]
    print(json.dumps({k: round(v) if isinstance(v, float) else v for k, v in tot.items()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
