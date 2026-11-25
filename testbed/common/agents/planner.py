"""P_HIER_AGENT Meta-Planner (LLM, asynchronous) and the ``none_rule`` deterministic planner (Exp 5a).

Ticks fire every ``plan_interval_s`` from t0. A tick that arrives while the previous planning
call is still running is *skipped* and logged (``plan_skipped_pct``); execution agents keep
applying the last valid plan in the meantime.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from typing import Any

from common.agents.common import (AgentLog, StateCache, ap_aggregates, load_prompt, planner_user_message,
                                  system_with_intents, wait_until)
from common.agents.guardrail import Guardrail, hold_plan, relax_schema

LOG = logging.getLogger("planner")


class PlanStore:
    """Latest valid plan, versioned; read by the execution agents."""

    def __init__(self, aps: list[str]):
        self.lock = threading.Lock()
        self.version = 0
        self.plan = hold_plan(aps)
        self.t = 0.0

    def set(self, plan: dict[str, Any]) -> int:
        with self.lock:
            self.version += 1
            self.plan = plan
            self.t = time.time()
            return self.version

    def goals(self, ap: str) -> tuple[int, list[dict[str, Any]]]:
        with self.lock:
            for item in self.plan.get("plan", []):
                if isinstance(item, dict) and item.get("ap") == ap:
                    return self.version, list(item.get("goals", []) or [])
            return self.version, []

    def get(self) -> dict[str, Any]:
        with self.lock:
            return self.plan


class LLMStats:
    """Counts LLM call outcomes for the rate-limit / failure check of a run."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls = self.failed = self.rate_limited = 0

    def add(self, ok: bool, rl: bool) -> None:
        with self.lock:
            self.calls += 1
            self.failed += int(not ok)
            self.rate_limited += int(rl)

    def as_dict(self) -> dict[str, int]:
        return {"llm_calls": self.calls, "llm_failed": self.failed, "llm_rate_limited": self.rate_limited}


def rule_plan(agg: dict[str, dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    """``none_rule``: deterministic planner with the same inputs/outputs as the LLM planner."""
    plan = []
    co_chan: dict[int, list[str]] = {}
    for ap, a in agg.items():
        co_chan.setdefault(a["ch"], []).append(ap)
    for ap in sorted(agg, key=lambda s: int(s[2:])):
        a = agg[ap]
        goals: list[dict[str, Any]] = []
        congested = a["air10"] > 0.75 or (a["sla_tele"] == a["sla_tele"] and a["sla_tele"] < 100)
        n_tele = a["n"]["telemedicine"]
        if n_tele:
            goals.append({"type": "protect_slice", "slice": "telemedicine",
                          "min_rate_mbps": round(min(20.0, max(2.0, n_tele * 1.5 * 1.3)), 1)})
        if congested:
            targets = sorted((n for n in a["nbrs"] if agg.get(n, {}).get("air10", 1) < a["air10"]),
                             key=lambda n: agg[n]["air10"])
            if targets:
                n_move = max(1, min(5, math.ceil((a["air10"] - 0.6) * max(1, sum(a["n"].values())))))
                goals.append({"type": "offload", "count": n_move, "to": targets[:2], "prefer_slice": "best_effort"})
            goals.append({"type": "limit_slice", "slice": "best_effort", "max_rate_mbps": 2})
        others = [o for o in co_chan.get(a["ch"], []) if o != ap]
        if others and max(agg[o]["air10"] for o in others) > 0.7 and a["air10"] < 0.5:
            goals.append({"type": "set_power", "dbm": max(float(cfg["txpower_min_dbm"]), a["pwr"] - 3)})
        if not goals:
            goals = [{"type": "hold"}]
        plan.append({"ap": ap, "goals": goals[:4]})
    return {"plan": plan}


class PlannerAgent(threading.Thread):
    def __init__(self, cfg: dict[str, Any], topo: dict[str, Any], cache: StateCache, store: PlanStore,
                 log: AgentLog, guard: Guardrail, schema: dict[str, Any], stop: threading.Event,
                 t0: float, t_end: float, stats: LLMStats):
        super().__init__(name="planner", daemon=True)
        self.cfg, self.topo, self.cache, self.store, self.log, self.guard = cfg, topo, cache, store, log, guard
        self.stop_ev, self.t0, self.t_end, self.stats = stop, t0, t_end, stats
        self.aps = sorted(topo["aps"], key=lambda s: int(s[2:]))
        self.interval = float(cfg["plan_interval_s"])
        self.rule_mode = cfg["planner_model"] == "none_rule"
        self.busy = threading.Lock()
        self.plan_no = 0
        self.schema = schema if guard.enabled else relax_schema(schema, self.aps)
        if not self.rule_mode:
            from common.llm import LLMClient

            self.llm = LLMClient(cfg, cfg["planner_model"], role="planner")
            self.system = system_with_intents(load_prompt("planner_system.txt"), cfg, topo)

    def warmup(self) -> None:
        if not self.rule_mode:
            r = self.llm.warmup()
            self.log.write({"type": "llm_warmup", "agent": "planner", "model": self.llm.model, "ok": r.ok,
                            "latency_ms": r.latency_ms, "error": r.error})

    def run(self) -> None:
        k = 0
        while not self.stop_ev.is_set():
            t_tick = self.t0 + k * self.interval
            if t_tick >= self.t_end or not wait_until(t_tick, self.stop_ev):
                return
            k += 1
            if not self.busy.acquire(blocking=False):
                self.log.write({"type": "tick", "agent": "planner", "tick": k, "skipped": True})
                continue
            self.log.write({"type": "tick", "agent": "planner", "tick": k, "skipped": False})
            threading.Thread(target=self._plan_safe, args=(k,), daemon=True).start()

    def _plan_safe(self, k: int) -> None:
        try:
            self._plan(k)
        except Exception:  # noqa: BLE001
            LOG.exception("planning failed")
        finally:
            self.busy.release()

    def _plan(self, k: int) -> None:
        self.plan_no += 1
        t_req = time.time()
        if self.rule_mode:
            plan = rule_plan(ap_aggregates(self.cache, self.topo), self.cfg)
            se, sem = self.guard.check_plan(plan)
            self.store.set(plan if not (se or sem) else hold_plan(self.aps))
            self.log.write({"type": "plan", "agent": "planner", "model": "none_rule", "tick": k,
                            "latency_ms": (time.time() - t_req) * 1000, "valid_first": not (se or sem),
                            "valid_final": not (se or sem), "llm": False, "plan": plan, "errors": se + sem,
                            "invalid_items": len(sem)})
            return
        user = planner_user_message(self.cache, self.topo, t_req - self.t0, self.plan_no, self.store.get())
        history: list[dict[str, str]] = []
        calls = []
        valid_first = False
        errors: list[str] = []
        plan: dict[str, Any] | None = None
        invalid_items = 0
        for attempt in range(1 + int(self.cfg.get("max_reprompts", 1))):
            res = self.llm.chat(self.system, user, self.schema, history=history)
            self.stats.add(res.ok or bool(res.content), res.rate_limited)
            calls.append(res.to_log())
            if res.rate_limited or (not res.content and not res.ok):
                self.log.write({"type": "llm_error", "agent": "planner", "tick": k, "error": res.error,
                                "rate_limited": res.rate_limited})
                break
            cand = res.parsed
            se, sem = self.guard.check_plan(cand) if cand is not None else (["unparseable JSON"], [])
            errors = se + sem
            if attempt == 0:
                valid_first = not errors
                invalid_items = len(sem)
            if not errors:
                plan = cand
                break
            if not self.guard.enabled:
                plan = cand if isinstance(cand, dict) and "plan" in cand else None  # audit only
                break
            history = [{"role": "user", "content": user}, {"role": "assistant", "content": res.content}]
            user = ("Your plan was rejected by the guardrail: " + "; ".join(errors[:6]) +
                    ". Return a corrected plan JSON for all APs.")
        fallback = plan is None
        final = plan if plan is not None else hold_plan(self.aps)
        if not (fallback and calls and calls[-1].get("rate_limited")):
            self.store.set(final)
        lat = (time.time() - t_req) * 1000
        self.log.write({"type": "plan", "agent": "planner", "model": self.llm.model, "tick": k, "llm": True,
                        "latency_ms": lat, "valid_first": valid_first, "valid_final": not fallback,
                        "reprompted": len(calls) > 1, "fallback_hold": fallback, "errors": errors,
                        "invalid_items": invalid_items,
                        "prompt_tokens": sum(c["prompt_tokens"] for c in calls),
                        "output_tokens": sum(c["output_tokens"] for c in calls),
                        "n_calls": len(calls), "calls": calls, "plan": final})


def plan_to_text(plan: dict[str, Any]) -> str:
    return json.dumps(plan, separators=(",", ":"))
