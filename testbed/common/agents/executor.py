"""P_HIER_AGENT per-AP Execution Agents (1 s loop): sub-goals → tool calls.

``subagent_mode=rule`` (default): deterministic, pre-compiled solvers:

* offload — pick, per tick, one non-telemedicine STA of ``prefer_slice`` (then the other non-tele
  slice) whose estimated RSSI at a target AP is ≥ ``rssi_min_target_dbm``, largest gain first;
  until ``count`` steers of this plan version succeeded (with ``exec_offload_sustain``: no count limit,
  one steer per tick for as long as the goal is in the current plan);
* set_power — step ≤ ``txpower_max_step_db`` per tick toward the target;
* protect_slice / limit_slice — HTB rate change; when protected rates exceed the capacity the
  best-effort rate is lowered (not below its floor).

* steer — (``flat_exec`` only) one named STA to a named target, retried each tick until verified.

``subagent_mode=slm``: the same work detection, but the concrete tool calls are produced by a tiny
SLM (``subagent_model``) from the goals and the AP-local station table; invalid output → no action.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from common import config as C
from common.agents.common import AgentLog, StateCache, ToolProxy, load_prompt, system_with_intents, wait_until
from common.agents.guardrail import Guardrail
from common.agents.planner import LLMStats, PlanStore

LOG = logging.getLogger("executor")


class ExecAgent(threading.Thread):
    def __init__(self, ap: str, cfg: dict[str, Any], topo: dict[str, Any], cache: StateCache, store: PlanStore,
                 log: AgentLog, guard: Guardrail, action_schema: dict[str, Any], stop: threading.Event,
                 t0: float, t_end: float, stats: LLMStats):
        super().__init__(name=f"exec-{ap}", daemon=True)
        self.ap, self.cfg, self.topo, self.cache, self.store = ap, cfg, topo, cache, store
        self.log, self.guard, self.stop_ev, self.t0, self.t_end, self.stats = log, guard, stop, t0, t_end, stats
        self.proxy = ToolProxy(int(cfg["tool_server_port"]), f"exec-{ap}", log)
        self.period = float(cfg["exec_interval_s"])
        self.intents = C.load_intents()["slices"]
        self.cap = float(cfg["ap_capacity_mbps"])
        self.rssi_min = float(cfg["rssi_min_target_dbm"])
        self.progress: dict[tuple[int, int], int] = {}
        self.sustain = bool(cfg.get("exec_offload_sustain", False))
        self.mode = cfg.get("subagent_mode", "rule")
        self.schema = action_schema
        self.last_slm = 0.0
        if self.mode == "slm":
            from common.llm import LLMClient

            self.llm = LLMClient(cfg, cfg["subagent_model"], role="subagent")
            self.system = system_with_intents(load_prompt("executor_system.txt"), cfg, topo)

    # ------------------------------------------------------------------ rule solver
    def rule_actions(self, version: int, goals: list[dict[str, Any]], state: dict[str, Any]) -> list[dict[str, Any]]:
        acts: list[dict[str, Any]] = []
        apst = state["aps"].get(self.ap, {})
        rates = dict(apst.get("rates", {}))
        mine = {s: st for s, st in state["stas"].items() if st.get("ap") == self.ap}
        now = time.time()
        for gi, g in enumerate(goals):
            t = g.get("type")
            if t == "offload":
                done = self.progress.get((version, gi), 0)
                if done >= int(g.get("count", 0) or 0) and not self.sustain:
                    continue
                prefer = g.get("prefer_slice", "best_effort")
                order = [prefer] + [s for s in ("best_effort", "video") if s != prefer]
                best = None
                for sl in order:
                    for s, st in mine.items():
                        if st["slice"] != sl:
                            continue
                        if self.guard.enabled and now - self.guard.last_btm.get(s, -1e9) < self.guard.holddown:
                            continue
                        est = st.get("est_rssi") or {}
                        for tgt in g.get("to", []) or []:
                            e = est.get(tgt)
                            if e is None or e < self.rssi_min:
                                continue
                            gain = e - (st.get("rssi") if st.get("rssi") is not None else est.get(self.ap, -90))
                            if best is None or gain > best[0]:
                                best = (gain, s, tgt)
                    if best:
                        break
                if best:
                    acts.append({"tool": "trigger_bss_transition", "sta": best[1], "target_ap": best[2],
                                 "_goal": (version, gi)})
            elif t == "steer":
                # flat_exec: one named STA to one named target, retried every tick until a verified
                # steer of this plan version, while the STA is still at this AP; same hold-down and RSSI floor
                if self.progress.get((version, gi), 0) >= 1:
                    continue
                s, tgt = g.get("sta"), g.get("to")
                st = mine.get(s)
                if st is None or st.get("slice") == "telemedicine":
                    continue
                if self.guard.enabled and now - self.guard.last_btm.get(s, -1e9) < self.guard.holddown:
                    continue
                e = (st.get("est_rssi") or {}).get(tgt)
                if e is None or e < self.rssi_min:
                    continue
                acts.append({"tool": "trigger_bss_transition", "sta": s, "target_ap": tgt, "_goal": (version, gi)})
            elif t == "set_power":
                cur = float(apst.get("txpower", self.cfg["ap_txpower_dbm"]))
                tgt = float(g.get("dbm", cur))
                if abs(tgt - cur) >= 0.5:
                    step = max(-self.guard.pstep, min(self.guard.pstep, tgt - cur))
                    acts.append({"tool": "set_tx_power", "ap_id": self.ap, "dbm": round(cur + step, 1)})
            elif t == "protect_slice":
                sl, r = g.get("slice"), float(g.get("min_rate_mbps", 0))
                if sl in rates and rates[sl] < r - 0.05:
                    acts.append({"tool": "adjust_slice_bandwidth", "ap_id": self.ap, "slice": sl, "rate_mbps": r})
                    rates[sl] = r
                    total = sum(rates.values())
                    if total > self.cap and "best_effort" in rates:
                        floor = float(self.intents["best_effort"]["rate_floor_mbps"])
                        be = max(floor, rates["best_effort"] - (total - self.cap))
                        if be < rates["best_effort"] - 0.05:
                            acts.append({"tool": "adjust_slice_bandwidth", "ap_id": self.ap,
                                         "slice": "best_effort", "rate_mbps": round(be, 2)})
                            rates["best_effort"] = be
            elif t == "limit_slice":
                sl, r = g.get("slice"), float(g.get("max_rate_mbps", self.cap))
                if sl in rates and rates[sl] > r + 0.05:
                    acts.append({"tool": "adjust_slice_bandwidth", "ap_id": self.ap, "slice": sl, "rate_mbps": r})
                    rates[sl] = r
        return acts

    # ------------------------------------------------------------------ slm solver
    def slm_actions(self, goals: list[dict[str, Any]], state: dict[str, Any]) -> list[dict[str, Any]]:
        apst = state["aps"].get(self.ap, {})
        now = time.time()
        rows = []
        for s, st in sorted(state["stas"].items(), key=lambda kv: int(kv[0][3:])):
            if st.get("ap") != self.ap:
                continue
            hd = max(0.0, self.guard.holddown - (now - self.guard.last_btm.get(s, -1e9)))
            est = {a: v for a, v in (st.get("est_rssi") or {}).items() if a != self.ap}
            rows.append(f"{s}|{st['slice']}|{st.get('rssi')}|" + ",".join(f"{a}:{v:.0f}" for a, v in est.items())
                        + f"|holddown={hd:.0f}s")
        user = (f"AP {self.ap}: power={apst.get('txpower')} dBm, rates={json.dumps(apst.get('rates'))}\n"
                f"Goals: {json.dumps(goals)}\nStations (sta|slice|rssi|est_rssi_other_aps|holddown):\n"
                + "\n".join(rows) + "\nReturn the actions JSON.")
        res = self.llm.chat(self.system, user, self.schema, num_predict=int(self.cfg.get("subagent_num_predict", 320)))
        self.stats.add(res.ok, res.rate_limited)
        self.log.write({"type": "slm_exec", "agent": f"exec-{self.ap}", "ok": res.ok, "latency_ms": res.latency_ms,
                        "prompt_tokens": res.prompt_tokens, "output_tokens": res.output_tokens, "error": res.error})
        if not res.ok or not isinstance(res.parsed, dict):
            return []
        return [a for a in res.parsed.get("actions", []) if isinstance(a, dict)]

    # ------------------------------------------------------------------ loop
    def execute(self, acts: list[dict[str, Any]], state: dict[str, Any], t_dec: float) -> None:
        power = {a: v["txpower"] for a, v in state["aps"].items()}
        for act in acts:
            goal = act.pop("_goal", None)
            errs = self.guard.check_action(act, state, power)
            verdict = self.guard.verdict(errs)
            params = {k: v for k, v in act.items() if k != "tool"}
            if verdict == "reject":
                self.proxy.log_rejected(act.get("tool", "?"), params, "; ".join(errs))
                continue
            tool = act.get("tool")
            if tool == "trigger_bss_transition":
                sta = act.get("sta", "")
                mac = self.topo["stas"].get(sta, {}).get("mac", sta)
                res = self.proxy.call(tool, t_decision=t_dec, guardrail=verdict, reason="; ".join(errs),
                                      sta_mac=mac, target_ap=act.get("target_ap"))
                if res.get("ok"):
                    self.guard.note_btm(sta)
                    if goal is not None and res.get("verified"):
                        self.progress[goal] = self.progress.get(goal, 0) + 1
            elif tool in ("set_tx_power", "adjust_slice_bandwidth"):
                self.proxy.call(tool, t_decision=t_dec, guardrail=verdict, reason="; ".join(errs), **params)
                if tool == "set_tx_power":
                    power[params.get("ap_id", self.ap)] = float(params.get("dbm", 0) or 0)
            else:
                self.proxy.log_rejected(str(tool), params, "unknown tool")

    def run(self) -> None:
        k = 0
        while not self.stop_ev.is_set():
            t_tick = self.t0 + k * self.period
            if t_tick >= self.t_end or not wait_until(t_tick, self.stop_ev):
                return
            k += 1
            state = self.cache.latest()
            if state is None:
                continue
            version, goals = self.store.goals(self.ap)
            t_dec = time.time()
            acts = self.rule_actions(version, goals, state)
            if self.mode == "slm" and acts:
                if time.time() - self.last_slm < 2.0:
                    continue
                self.last_slm = time.time()
                slm = self.slm_actions(goals, state)
                # keep offload progress accounting for the goal the SLM acted on
                goal_tag = next((a["_goal"] for a in acts if "_goal" in a), None)
                for a in slm:
                    if a.get("tool") == "trigger_bss_transition" and goal_tag:
                        a["_goal"] = goal_tag
                acts = slm
            if acts:
                self.execute(acts, state, t_dec)
