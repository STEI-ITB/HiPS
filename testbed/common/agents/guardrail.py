"""Guardrail layer: JSON-schema + semantic checks for plans and tool calls.

* ``guardrail=on``: invalid plans are re-prompted once and then replaced by ``hold``; invalid
  actions are rejected (logged ``guardrail="reject"``) and never reach the tools.
* ``guardrail=off``: the same checks run in **audit** mode — nothing is blocked, but violations
  are logged (``guardrail="audit_fail"``) so that ``invalid_actions`` can be counted. The LLM is
  then also given a *relaxed* output schema (no numeric bounds / AP enums), because constrained
  decoding with the strict schema is itself part of the guardrail.
"""
from __future__ import annotations

import copy
import math
import threading
import time
from typing import Any

import jsonschema

from common import config as C


def relax_schema(schema: dict[str, Any], ap_names: list[str]) -> dict[str, Any]:
    """Strip bounds, maxItems, patterns and AP enums (guardrail=off output format)."""
    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "enum" in node and set(node["enum"]) <= set(ap_names):
                return {"type": "string"}
            return {k: _walk(v) for k, v in node.items()
                    if k not in ("minimum", "maximum", "maxItems", "minItems", "pattern", "maxLength")}
        if isinstance(node, list):
            return [_walk(x) for x in node]
        return node
    return _walk(copy.deepcopy(schema))


class Guardrail:
    def __init__(self, cfg: dict[str, Any], topo: dict[str, Any], plan_schema: dict[str, Any],
                 action_schema: dict[str, Any], holddown_s: float | None = None):
        self.enabled = C.normalise_onoff(cfg.get("guardrail", "on")) == "on"
        self.cfg = cfg
        self.aps = set(topo["aps"])
        self.nbrs: dict[str, list[str]] = topo["neighbours"]
        self.stas: dict[str, dict[str, Any]] = topo["stas"]
        self.intents = C.load_intents()["slices"]
        self.plan_v = jsonschema.Draft202012Validator(plan_schema)
        self.action_v = jsonschema.Draft202012Validator(action_schema)
        self.pmin, self.pmax = float(cfg["txpower_min_dbm"]), float(cfg["txpower_max_dbm"])
        self.pstep = float(cfg["txpower_max_step_db"])
        self.cap = float(cfg["ap_capacity_mbps"])
        self.holddown = float(cfg["btm_holddown_s"] if holddown_s is None else holddown_s)
        self.rssi_min = float(cfg["rssi_min_target_dbm"])
        self.last_btm: dict[str, float] = {}
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ plans
    def check_plan(self, plan: Any) -> tuple[list[str], list[str]]:
        """Return (schema_errors, semantic_errors) for a sub-goal plan."""
        schema_err = [f"{'/'.join(map(str, e.path))}: {e.message}" for e in self.plan_v.iter_errors(plan)]
        sem: list[str] = []
        if not isinstance(plan, dict) or not isinstance(plan.get("plan"), list):
            return schema_err or ["plan missing"], sem
        seen: set[str] = set()
        for item in plan["plan"]:
            if not isinstance(item, dict):
                continue
            ap = item.get("ap")
            if ap not in self.aps:
                sem.append(f"unknown AP {ap}")
                continue
            if ap in seen:
                sem.append(f"duplicate entry for {ap}")
            seen.add(ap)
            protect = 0.0
            for g in item.get("goals", []) or []:
                if not isinstance(g, dict):
                    continue
                t = g.get("type")
                if t == "offload":
                    for tgt in g.get("to", []) or []:
                        if tgt not in self.aps:
                            sem.append(f"{ap}: offload to unknown AP {tgt}")
                        elif tgt not in self.nbrs.get(ap, []):
                            sem.append(f"{ap}: offload target {tgt} is not a neighbour of {ap}")
                elif t == "set_power":
                    d = _num(g.get("dbm"))
                    if not (self.pmin <= d <= self.pmax):
                        sem.append(f"{ap}: set_power {g.get('dbm')} outside [{self.pmin:g},{self.pmax:g}] dBm")
                elif t == "protect_slice":
                    r = _num(g.get("min_rate_mbps"))
                    sl = self.intents.get(str(g.get("slice")), {})
                    if not sl or not (sl["rate_floor_mbps"] <= r <= min(sl["rate_ceil_mbps"], self.cap)):
                        sem.append(f"{ap}: protect_slice rate {g.get('min_rate_mbps')} out of bounds")
                    else:
                        protect += r
                elif t == "limit_slice":
                    r = _num(g.get("max_rate_mbps"))
                    sl = self.intents.get(str(g.get("slice")), {})
                    if not sl or not (sl["rate_floor_mbps"] <= r <= self.cap):
                        sem.append(f"{ap}: limit_slice rate {g.get('max_rate_mbps')} out of bounds")
            if protect > self.cap:
                sem.append(f"{ap}: protected rates {protect:g} exceed capacity {self.cap:g}")
        return schema_err, sem

    # ------------------------------------------------------------------ actions
    def check_action(self, act: dict[str, Any], state: dict[str, Any] | None,
                     current_power: dict[str, float] | None = None, now: float | None = None) -> list[str]:
        """Semantic + schema check of one tool call. ``state`` = latest snapshot."""
        errs = [e.message for e in self.action_v.iter_errors({"actions": [act]})]
        now = now or time.time()
        tool = act.get("tool")
        if tool == "set_tx_power":
            ap, d = act.get("ap_id"), _num(act.get("dbm"))
            if ap not in self.aps:
                errs.append(f"unknown AP {ap}")
            if not (self.pmin <= d <= self.pmax):
                errs.append(f"tx power {act.get('dbm')} outside [{self.pmin:g},{self.pmax:g}]")
            cur = (current_power or {}).get(ap)
            if cur is not None and abs(d - cur) > self.pstep + 1e-9:
                errs.append(f"tx power step {d - cur:+g} dB exceeds ±{self.pstep:g} dB")
        elif tool == "trigger_bss_transition":
            sta, tgt = act.get("sta"), act.get("target_ap")
            if sta not in self.stas:
                errs.append(f"unknown station {sta}")
            elif self.stas[sta]["slice"] == "telemedicine":
                errs.append(f"{sta} is telemedicine (never steered)")
            if tgt not in self.aps:
                errs.append(f"unknown AP {tgt}")
            serving = (state or {}).get("stas", {}).get(sta, {}).get("ap") if state else None
            if state is not None and sta in self.stas:
                if serving is None:
                    errs.append(f"{sta} not associated")
                elif tgt in self.aps and tgt not in self.nbrs.get(serving, []):
                    errs.append(f"{tgt} is not a neighbour of serving AP {serving}")
                est = (state["stas"][sta].get("est_rssi") or {}).get(tgt)
                if est is not None and est < self.rssi_min:
                    errs.append(f"{sta} would hear {tgt} at {est:.0f} dBm < {self.rssi_min:g}")
            with self.lock:
                last = self.last_btm.get(str(sta))
            if last is not None and now - last < self.holddown:
                errs.append(f"{sta} in BTM hold-down ({now - last:.1f} s < {self.holddown:g} s)")
        elif tool == "adjust_slice_bandwidth":
            ap, sl, r = act.get("ap_id"), act.get("slice"), _num(act.get("rate_mbps"))
            if ap not in self.aps:
                errs.append(f"unknown AP {ap}")
            info = self.intents.get(str(sl))
            if not info:
                errs.append(f"unknown slice {sl}")
            elif not (info["rate_floor_mbps"] <= r <= min(info["rate_ceil_mbps"], self.cap)):
                errs.append(f"rate {act.get('rate_mbps')} outside [{info['rate_floor_mbps']},{self.cap:g}]")
        else:
            errs.append(f"unknown tool {tool}")
        return errs

    def note_btm(self, sta: str, now: float | None = None) -> None:
        with self.lock:
            self.last_btm[sta] = now or time.time()

    def verdict(self, errs: list[str]) -> str:
        if not errs:
            return "pass"
        return "reject" if self.enabled else "audit_fail"


def _num(v: Any) -> float:
    try:
        f = float(v)
        return f if not math.isnan(f) else float("inf")
    except (TypeError, ValueError):
        return float("inf")


HOLD_PLAN_GOAL = {"type": "hold"}


def hold_plan(aps: list[str]) -> dict[str, Any]:
    return {"plan": [{"ap": a, "goals": [dict(HOLD_PLAN_GOAL)]} for a in aps]}
