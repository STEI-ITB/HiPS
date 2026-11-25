"""B2_FLAT_AGENT: one SLM agent, invoked every ``plan_interval_s``, sees per-STA state and emits tool
calls for all APs directly (ReAct-style: brief thought + actions). Same guardrail as P.

Ticks are asynchronous exactly like the planner: a tick arriving while the previous call is still
running is skipped (``plan_skipped_pct``).

``flat_exec: true`` ("Flat-Agent + 1-s executor"): the same per-STA prompt, model, schema, re-prompt and
action guardrail, but the accepted actions are not called directly. They are turned into per-AP goals
(``steer`` a named STA to a named AP, ``set_power``, ``protect_slice``/``limit_slice``) and published to
a plan store that HiPS's own execution agents apply every second. It separates the 1-s inner loop
from the per-AP aggregation in the HiPS versus Flat-Agent comparison.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from common.agents.common import (AgentLog, StateCache, ToolProxy, flat_user_message, load_prompt,
                                  system_with_intents, wait_until)
from common.agents.guardrail import Guardrail, relax_schema
from common.agents.planner import LLMStats

LOG = logging.getLogger("flat")


class FlatAgent(threading.Thread):
    def __init__(self, cfg: dict[str, Any], topo: dict[str, Any], cache: StateCache, log: AgentLog,
                 guard: Guardrail, schema: dict[str, Any], stop: threading.Event, t0: float, t_end: float,
                 stats: LLMStats, store: Any = None):
        from common.llm import LLMClient

        super().__init__(name="flat", daemon=True)
        self.cfg, self.topo, self.cache, self.log, self.guard = cfg, topo, cache, log, guard
        self.stop_ev, self.t0, self.t_end, self.stats = stop, t0, t_end, stats
        self.store = store   # flat_exec: PlanStore read by the execution agents; None = call tools directly
        aps = sorted(topo["aps"], key=lambda s: int(s[2:]))
        self.schema = schema if guard.enabled else relax_schema(schema, aps)
        self.proxy = ToolProxy(int(cfg["tool_server_port"]), "flat", log)
        self.interval = float(cfg["plan_interval_s"])
        self.busy = threading.Lock()
        self.llm = LLMClient(cfg, cfg["planner_model"], role="planner")
        self.system = system_with_intents(load_prompt("flat_system.txt"), cfg, topo)

    def warmup(self) -> None:
        r = self.llm.warmup()
        self.log.write({"type": "llm_warmup", "agent": "flat", "model": self.llm.model, "ok": r.ok,
                        "latency_ms": r.latency_ms, "error": r.error})

    def run(self) -> None:
        k = 0
        while not self.stop_ev.is_set():
            t_tick = self.t0 + k * self.interval
            if t_tick >= self.t_end or not wait_until(t_tick, self.stop_ev):
                return
            k += 1
            if not self.busy.acquire(blocking=False):
                self.log.write({"type": "tick", "agent": "flat", "tick": k, "skipped": True})
                continue
            self.log.write({"type": "tick", "agent": "flat", "tick": k, "skipped": False})
            threading.Thread(target=self._step_safe, args=(k,), daemon=True).start()

    def _step_safe(self, k: int) -> None:
        try:
            self._step(k)
        except Exception:  # noqa: BLE001
            LOG.exception("flat agent step failed")
        finally:
            self.busy.release()

    def _validate(self, parsed: Any, state: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[str], int]:
        if not isinstance(parsed, dict) or not isinstance(parsed.get("actions"), list):
            return [], ["output is not {actions:[...]}"], 0
        power = {a: v["txpower"] for a, v in (state or {}).get("aps", {}).items()}
        errs: list[str] = []
        n_bad = 0
        acts = []
        for a in parsed["actions"]:
            e = self.guard.check_action(a, state, power) if isinstance(a, dict) else ["not an object"]
            if e:
                n_bad += 1
                errs.append(f"{a}: {'; '.join(e)}")
            acts.append((a, e))
        return acts, errs, n_bad  # type: ignore[return-value]

    def _step(self, k: int) -> None:
        t_req = time.time()
        state = self.cache.latest()
        user = flat_user_message(self.cache, self.topo, t_req - self.t0)
        history: list[dict[str, str]] = []
        calls = []
        valid_first = False
        acts: list[Any] = []
        errs: list[str] = []
        invalid_items = 0
        for attempt in range(1 + int(self.cfg.get("max_reprompts", 1))):
            res = self.llm.chat(self.system, user, self.schema, history=history)
            self.stats.add(res.ok or bool(res.content), res.rate_limited)
            calls.append(res.to_log())
            if res.rate_limited or (not res.content and not res.ok):
                self.log.write({"type": "llm_error", "agent": "flat", "tick": k, "error": res.error,
                                "rate_limited": res.rate_limited})
                acts = []
                break
            acts, errs, n_bad = self._validate(res.parsed, state)
            if attempt == 0:
                valid_first = not errs
                invalid_items = n_bad
            if not errs or not self.guard.enabled:
                break
            history = [{"role": "user", "content": user}, {"role": "assistant", "content": res.content}]
            user = ("Some actions were rejected by the guardrail: " + " | ".join(errs[:6]) +
                    ". Return a corrected actions JSON (drop actions you cannot fix).")
        t_dec = time.time()
        self.log.write({"type": "plan", "agent": "flat", "model": self.llm.model, "tick": k, "llm": True,
                        "latency_ms": (t_dec - t_req) * 1000, "valid_first": valid_first,
                        "valid_final": not errs, "reprompted": len(calls) > 1, "errors": errs,
                        "invalid_items": invalid_items,
                        "prompt_tokens": sum(c["prompt_tokens"] for c in calls),
                        "output_tokens": sum(c["output_tokens"] for c in calls), "n_calls": len(calls),
                        "calls": calls, "actions": [a for a, _ in acts]})
        state = self.cache.latest() or state
        if self.store is not None:
            self._publish(acts, state, k)
            return
        for act, e in acts:
            if not isinstance(act, dict):
                continue
            verdict = self.guard.verdict(e)
            params = {kk: v for kk, v in act.items() if kk != "tool"}
            if verdict == "reject":
                self.proxy.log_rejected(str(act.get("tool")), params, "; ".join(e))
                continue
            tool = act.get("tool")
            if tool == "trigger_bss_transition":
                sta = str(act.get("sta", ""))
                mac = self.topo["stas"].get(sta, {}).get("mac", sta)
                r = self.proxy.call(tool, t_decision=t_dec, guardrail=verdict, reason="; ".join(e),
                                    sta_mac=mac, target_ap=act.get("target_ap"))
                if r.get("ok"):
                    self.guard.note_btm(sta)
            elif tool in ("set_tx_power", "adjust_slice_bandwidth"):
                self.proxy.call(tool, t_decision=t_dec, guardrail=verdict, reason="; ".join(e), **params)
            else:
                self.proxy.log_rejected(str(tool), params, "unknown tool")

    def _publish(self, acts: list[Any], state: dict[str, Any] | None, k: int) -> None:
        """flat_exec: accepted actions -> per-AP goals in the plan store (rejected ones are logged, as before)."""
        aps = sorted(self.topo["aps"], key=lambda s: int(s[2:]))
        goals: dict[str, list[dict[str, Any]]] = {a: [] for a in aps}
        rates = {a: dict(v.get("rates", {})) for a, v in (state or {}).get("aps", {}).items()}
        for act, e in acts:
            if not isinstance(act, dict):
                continue
            params = {kk: v for kk, v in act.items() if kk != "tool"}
            if self.guard.verdict(e) == "reject":
                self.proxy.log_rejected(str(act.get("tool")), params, "; ".join(e))
                continue
            tool = act.get("tool")
            if tool == "trigger_bss_transition":
                sta = str(act.get("sta", ""))
                ap = ((state or {}).get("stas", {}).get(sta) or {}).get("ap")
                if ap in goals:
                    goals[ap].append({"type": "steer", "sta": sta, "to": act.get("target_ap")})
            elif tool == "set_tx_power" and act.get("ap_id") in goals:
                goals[act["ap_id"]].append({"type": "set_power", "dbm": act.get("dbm")})
            elif tool == "adjust_slice_bandwidth" and act.get("ap_id") in goals:
                ap, sl, r = act["ap_id"], act.get("slice"), float(act.get("rate_mbps", 0) or 0)
                if r >= rates.get(ap, {}).get(sl, 0.0):
                    goals[ap].append({"type": "protect_slice", "slice": sl, "min_rate_mbps": r})
                else:
                    goals[ap].append({"type": "limit_slice", "slice": sl, "max_rate_mbps": r})
            else:
                self.proxy.log_rejected(str(tool), params, "unknown tool")
        plan = {"plan": [{"ap": a, "goals": g or [{"type": "hold"}]} for a, g in goals.items()]}
        v = self.store.set(plan)
        self.log.write({"type": "flat_exec_plan", "agent": "flat", "tick": k, "version": v,
                        "n_goals": sum(len(g) for g in goals.values())})
