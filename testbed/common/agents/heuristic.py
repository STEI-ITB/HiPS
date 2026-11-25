"""B1_HEURISTIC: centralized reactive controller, 1 s loop.

Per AP, every ``exec_interval_s``:
1. if airtime > ``heuristic_airtime_hi`` or a telemedicine flow at the AP violates its SLA:
   steer the weakest-RSSI non-telemedicine STA to the least-loaded grid neighbour whose estimated
   RSSI is ≥ ``rssi_min_target_dbm`` (hold-down ``b1_holddown_s``, 10 s as the agents' ``btm_holddown_s``);
2. TX power −``heuristic_power_step_db`` if a co-channel AP's airtime > ``heuristic_cochannel_hi``
   (and +step back toward the initial power when all co-channel APs are < 0.5 — hysteresis added
   so the rule cannot ratchet the power to the minimum for the rest of the run);
3. on a telemedicine (video) violation move ``heuristic_shift_mbps`` from best effort to that slice.
No guardrail (tools still clamp physically); actions are logged with ``guardrail="n/a"``.

``heuristic_mode: lb`` (tuned load-balancing / association baseline): per AP and tick, steer one
non-telemedicine STA that is weak (RSSI below ``lb_rssi_lo_dbm``; null = off) or sits in a cell above
the airtime hysteresis of ``airtime_lb``; the target is the neighbour with the best estimated RSSI
(≥ ``rssi_min_target_dbm``) whose airtime is below ``heuristic_airtime_lo``. No telemedicine trigger,
no power or slice rules; thresholds are tuned on separate seeds (experiment-11, 11t).

``heuristic_mode: airtime_lb`` turns this into an airtime-only load balancer: rule 1
fires on airtime alone, with hysteresis (an AP starts offloading above ``heuristic_airtime_hi`` and
keeps going until it falls below ``heuristic_airtime_lo``), it steers only to neighbours below
``heuristic_airtime_lo``, and rules 2 and 3 are off.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from common import config as C
from common.agents.common import AgentLog, StateCache, ToolProxy, wait_until

LOG = logging.getLogger("heuristic")


class HeuristicAgent(threading.Thread):
    def __init__(self, cfg: dict[str, Any], topo: dict[str, Any], cache: StateCache, log: AgentLog,
                 stop: threading.Event, t0: float, t_end: float):
        super().__init__(name="heuristic", daemon=True)
        self.cfg, self.topo, self.cache, self.log, self.stop_ev, self.t0, self.t_end = cfg, topo, cache, log, stop, t0, t_end
        self.proxy = ToolProxy(int(cfg["tool_server_port"]), "heuristic", log)
        self.holddown = float(cfg["b1_holddown_s"])
        self.last_btm: dict[str, float] = {}
        self.intents = C.load_intents()["slices"]
        self.p0 = float(cfg["ap_txpower_dbm"])
        self.mode = cfg.get("heuristic_mode", "full")
        self.lb_only = self.mode in ("airtime_lb", "lb")
        rlo = cfg.get("lb_rssi_lo_dbm")
        self.rssi_lo = None if rlo is None else float(rlo)
        self.offloading: set[str] = set()   # airtime_lb: APs between the hi and lo thresholds

    def step(self, state: dict[str, Any]) -> None:
        cfg = self.cfg
        now = time.time()
        t_dec = now
        for ap in sorted(state["aps"], key=lambda s: int(s[2:])):
            a = state["aps"][ap]
            mine = {s: st for s, st in state["stas"].items() if st.get("ap") == ap}
            viol = {sl: any(st["slice"] == sl and not st["sla_met"] for st in mine.values())
                    for sl in ("telemedicine", "video")}
            # 1. steering
            if self.lb_only:
                if a["airtime"] > float(cfg["heuristic_airtime_hi"]):
                    self.offloading.add(ap)
                elif a["airtime"] < float(cfg["heuristic_airtime_lo"]):
                    self.offloading.discard(ap)
                trigger = ap in self.offloading
            else:
                trigger = a["airtime"] > float(cfg["heuristic_airtime_hi"]) or viol["telemedicine"]
            if self.mode == "lb":
                self.lb_step(state, ap, mine, trigger, now, t_dec)
                continue
            if trigger:
                cands = [(st.get("rssi") if st.get("rssi") is not None else -100, s) for s, st in mine.items()
                         if st["slice"] != "telemedicine" and now - self.last_btm.get(s, -1e9) >= self.holddown]
                for _, s in sorted(cands):
                    est = state["stas"][s].get("est_rssi") or {}
                    nb = [n for n in self.topo["neighbours"].get(ap, [])
                          if est.get(n, -200) >= float(cfg["rssi_min_target_dbm"])
                          and (not self.lb_only or state["aps"][n]["airtime"] < float(cfg["heuristic_airtime_lo"]))]
                    if not nb:
                        continue
                    tgt = min(nb, key=lambda n: state["aps"][n]["airtime"])
                    res = self.proxy.call("trigger_bss_transition", t_decision=t_dec, guardrail="n/a",
                                          sta_mac=self.topo["stas"][s]["mac"], target_ap=tgt)
                    if res.get("ok"):
                        self.last_btm[s] = time.time()
                    break
            if self.lb_only:
                continue
            # 2. TX power
            co = [o for o, oa in state["aps"].items() if o != ap and oa["channel"] == a["channel"]]
            if co:
                co_air = max(state["aps"][o]["airtime"] for o in co)
                step = float(cfg["heuristic_power_step_db"])
                if co_air > float(cfg["heuristic_cochannel_hi"]) and a["txpower"] - step >= float(cfg["txpower_min_dbm"]):
                    self.proxy.call("set_tx_power", t_decision=t_dec, guardrail="n/a", ap_id=ap,
                                    dbm=a["txpower"] - step)
                elif co_air < 0.5 and a["txpower"] + step <= self.p0:
                    self.proxy.call("set_tx_power", t_decision=t_dec, guardrail="n/a", ap_id=ap,
                                    dbm=a["txpower"] + step)
            # 3. slice re-weighting
            shift = float(cfg["heuristic_shift_mbps"])
            floor = float(self.intents["best_effort"]["rate_floor_mbps"])
            for sl in ("telemedicine", "video"):
                if not viol[sl]:
                    continue
                be = a["rates"].get("best_effort", 0.0)
                mv = min(shift, max(0.0, be - floor))
                if mv <= 0:
                    continue
                self.proxy.call("adjust_slice_bandwidth", t_decision=t_dec, guardrail="n/a", ap_id=ap,
                                slice="best_effort", rate_mbps=round(be - mv, 2))
                self.proxy.call("adjust_slice_bandwidth", t_decision=t_dec, guardrail="n/a", ap_id=ap,
                                slice=sl, rate_mbps=round(min(float(cfg["ap_capacity_mbps"]),
                                                              a["rates"].get(sl, 0.0) + mv), 2))
                break

    def lb_step(self, state: dict[str, Any], ap: str, mine: dict[str, Any], air_trigger: bool, now: float,
                t_dec: float) -> None:
        """heuristic_mode lb: at most one steer per AP and tick (weak STAs first, then airtime offload)."""
        cfg = self.cfg
        lo = float(cfg["heuristic_airtime_lo"])
        floor = float(cfg["rssi_min_target_dbm"])
        cands = []
        for s, st in mine.items():
            if st["slice"] == "telemedicine" or now - self.last_btm.get(s, -1e9) < self.holddown:
                continue
            r = st.get("rssi") if st.get("rssi") is not None else -100.0
            weak = self.rssi_lo is not None and r < self.rssi_lo
            if weak or air_trigger:
                cands.append((0 if weak else 1, r, s))
        for _, r, s in sorted(cands):
            est = state["stas"][s].get("est_rssi") or {}
            nb = [n for n in self.topo["neighbours"].get(ap, [])
                  if est.get(n, -200) >= floor and est.get(n, -200) > r and state["aps"][n]["airtime"] < lo]
            if not nb:
                continue
            tgt = max(nb, key=lambda n: est[n])
            res = self.proxy.call("trigger_bss_transition", t_decision=t_dec, guardrail="n/a",
                                  sta_mac=self.topo["stas"][s]["mac"], target_ap=tgt)
            if res.get("ok"):
                self.last_btm[s] = time.time()
            return

    def run(self) -> None:
        k = 0
        period = float(self.cfg["exec_interval_s"])
        while not self.stop_ev.is_set():
            t_tick = self.t0 + k * period
            if t_tick >= self.t_end or not wait_until(t_tick, self.stop_ev):
                return
            k += 1
            state = self.cache.latest()
            if state is not None:
                try:
                    self.step(state)
                except Exception:  # noqa: BLE001
                    LOG.exception("heuristic step failed")
