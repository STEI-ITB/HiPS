#!/usr/bin/env python3
"""Agent-process entry point (cores ``orch``): ``run_agents.py --framework <F> --run-config <cfg>``.

Loads (warms) the LLM, waits for the emulation's READY file (which carries t0), then runs
the framework's agents from t0 to t0 + duration_s. At exit it writes ``agent_stats.json``; if more
than ``llm.cloud_budget.max_failed_call_frac`` of the LLM calls failed or were rate-limited it also
writes ``RATE_LIMITED`` so that ``run_one.py`` marks the run for a later retry (exit 75).
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import config as C  # noqa: E402
from common.agents.common import AgentLog, StateCache, load_schema  # noqa: E402
from common.agents.guardrail import Guardrail  # noqa: E402
from common.agents.planner import LLMStats, PlanStore, PlannerAgent  # noqa: E402
from common.tool_server import ToolClient, ToolError  # noqa: E402

LOG = logging.getLogger("run_agents")


def wait_ready(run_dir: Path, timeout: float) -> float | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        f = run_dir / "READY"
        if f.exists():
            try:
                return float(json.loads(f.read_text())["t0"])
            except (ValueError, KeyError, json.JSONDecodeError):
                pass
        time.sleep(0.2)
    return None


def preload(cfg: dict, framework: str, log: AgentLog) -> None:
    """Warm up (load) the planner / sub-agent models before t0 so loading is never measured."""
    if not C.uses_llm(dict(cfg, framework=framework)):
        return
    from common.llm import LLMClient

    models = [(cfg["planner_model"], "planner")]
    if framework == "P_HIER_AGENT" and cfg.get("subagent_mode") == "slm":
        models.append((cfg["subagent_model"], "subagent"))
    for model, role in models:
        try:
            r = LLMClient(cfg, model, role=role).warmup()
            log.write({"type": "llm_preload", "model": model, "role": role, "ok": r.ok,
                       "latency_ms": r.latency_ms, "error": r.error})
        except Exception as exc:  # noqa: BLE001 - reported, the run continues and logs failures
            LOG.warning("preload of %s failed: %s", model, exc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--framework", required=True, choices=list(C.FRAMEWORKS))
    ap.add_argument("--run-config", required=True)
    a = ap.parse_args(argv)
    cfg = C.read_run_config(a.run_config)
    run_dir = Path(cfg["run_dir"])
    C.setup_logging(logfile=run_dir / "agents.log")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    log = AgentLog(run_dir / "agent_log.jsonl")
    if a.framework == "B0_UNMANAGED":
        return 0
    preload(cfg, a.framework, log)   # load models while the emulator is still being built
    t0 = wait_ready(run_dir, float(cfg["setup_timeout_s"]) + 120)
    if t0 is None:
        LOG.error("emulation never became READY")
        return 1
    port = int(cfg["tool_server_port"])
    try:
        topo = ToolClient(port, "run_agents").call("get_topology", timeout_s=10)
    except ToolError as exc:
        LOG.error("tool server unreachable: %s", exc)
        return 1
    aps = sorted(topo["aps"], key=lambda s: int(s[2:]))
    t_end = t0 + float(cfg["duration_s"])
    cache = StateCache(port, float(cfg["exec_interval_s"]), stop)
    cache.start()
    stats = LLMStats()
    threads: list[threading.Thread] = []

    if a.framework == "B1_HEURISTIC":
        from common.agents.heuristic import HeuristicAgent

        threads.append(HeuristicAgent(cfg, topo, cache, log, stop, t0, t_end))
    else:
        plan_schema = load_schema("subgoal.schema.json", aps)
        action_schema = load_schema("actions.schema.json", aps)
        guard = Guardrail(cfg, topo, plan_schema, action_schema)
        if a.framework == "B2_FLAT_AGENT":
            from common.agents.flat_agent import FlatAgent

            store = PlanStore(aps) if cfg.get("flat_exec", False) else None
            flat = FlatAgent(cfg, topo, cache, log, guard, action_schema, stop, t0, t_end, stats, store)
            threads.append(flat)
            if store is not None:   # flat_exec: Flat-Agent decisions applied by HiPS's 1-s execution agents
                from common.agents.executor import ExecAgent

                threads += [ExecAgent(apn, cfg, topo, cache, store, log, guard, action_schema, stop, t0, t_end,
                                      stats) for apn in aps]
        else:
            from common.agents.executor import ExecAgent

            store = PlanStore(aps)
            planner = PlannerAgent(cfg, topo, cache, store, log, guard, plan_schema, stop, t0, t_end, stats)
            threads.append(planner)
            threads += [ExecAgent(apn, cfg, topo, cache, store, log, guard, action_schema, stop, t0, t_end, stats)
                        for apn in aps]
    log.write({"type": "start", "framework": a.framework, "t0": t0, "model": cfg.get("planner_model_effective")})
    for th in threads:
        th.start()
    while time.time() < t_end + 1 and not stop.is_set():
        time.sleep(0.5)
    stop.set()
    for th in threads:
        th.join(timeout=float(cfg.get("llm", {}).get("timeout_s", 120)) + 5)
    st = stats.as_dict()
    C.write_json(run_dir / "agent_stats.json", st)
    max_frac = float(cfg.get("llm", {}).get("cloud_budget", {}).get("max_failed_call_frac", 0.10))
    if cfg.get("llm", {}).get("backend") == "cloud" and st["llm_calls"] and \
            (st["llm_failed"] + st["llm_rate_limited"]) / st["llm_calls"] > max_frac:
        (run_dir / "RATE_LIMITED").write_text(json.dumps(st))
        LOG.warning("too many failed/rate-limited cloud calls: %s", st)
    log.write({"type": "stop", **st})
    return 0


if __name__ == "__main__":
    sys.exit(main())
