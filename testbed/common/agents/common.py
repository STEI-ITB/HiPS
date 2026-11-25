"""Shared agent infrastructure: logger, state cache, tool proxy, prompt tables."""
from __future__ import annotations

import collections
import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import Any

from common import config as C
from common.tool_server import ToolClient, ToolError
from common.tools import ACTUATION_TOOLS

LOG = logging.getLogger("agents")
PROMPTS = Path(__file__).resolve().parent / "prompts"
SCHEMAS = Path(__file__).resolve().parent / "schemas"


class AgentLog:
    """Thread-safe JSONL logger → ``agent_log.jsonl``."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def write(self, entry: dict[str, Any]) -> None:
        entry = {"ts": time.time(), **entry}
        with self.lock, open(self.path, "a") as fh:
            fh.write(json.dumps(entry, default=_default) + "\n")


def _default(o: Any) -> Any:
    try:
        return float(o)
    except (TypeError, ValueError):
        return str(o)


class StateCache(threading.Thread):
    """Polls ``get_snapshot`` every ``period_s`` and keeps a short history for all agents."""

    def __init__(self, port: int, period_s: float, stop: threading.Event, keep: int = 60):
        super().__init__(name="state-cache", daemon=True)
        self.client = ToolClient(port, "state-cache")
        self.period = period_s
        self.stop_ev = stop
        self.hist: collections.deque[dict[str, Any]] = collections.deque(maxlen=keep)
        self.lock = threading.Lock()
        self.handovers: collections.deque[tuple[float, str, str, str]] = collections.deque(maxlen=2000)

    def run(self) -> None:
        while not self.stop_ev.is_set():
            try:
                snap = self.client.call("get_snapshot", timeout_s=3.0)
                with self.lock:
                    if self.hist:
                        prev = self.hist[-1]["stas"]
                        for s, st in snap["stas"].items():
                            a0, a1 = prev.get(s, {}).get("ap"), st.get("ap")
                            if a0 and a1 and a0 != a1:
                                self.handovers.append((snap["ts"], s, a0, a1))
                    self.hist.append(snap)
            except ToolError as exc:
                LOG.debug("snapshot failed: %s", exc)
            self.stop_ev.wait(self.period)

    def latest(self) -> dict[str, Any] | None:
        with self.lock:
            return self.hist[-1] if self.hist else None

    def window(self, seconds: float) -> list[dict[str, Any]]:
        with self.lock:
            if not self.hist:
                return []
            t = self.hist[-1]["ts"]
            return [h for h in self.hist if h["ts"] > t - seconds]

    def recent_handovers(self, seconds: float) -> list[tuple[float, str, str, str]]:
        now = time.time()
        with self.lock:
            return [h for h in self.handovers if h[0] > now - seconds]


class ToolProxy:
    """Tool calls with agent-side logging (latency, post-condition, guardrail verdict)."""

    def __init__(self, port: int, caller: str, log: AgentLog):
        self.client = ToolClient(port, caller, timeout_s=6.0)
        self.caller = caller
        self.log = log

    def call(self, tool: str, t_decision: float | None = None, guardrail: str = "pass",
             reason: str = "", **params: Any) -> dict[str, Any]:
        t_call = time.time()
        t0 = time.perf_counter()
        try:
            res = self.client.call(tool, **params)
        except ToolError as exc:
            res = {"ok": False, "verified": False, "error": str(exc)}
        lat = (time.perf_counter() - t0) * 1000
        t_done = time.time()
        self.log.write({"type": "tool", "agent": self.caller, "tool": tool, "params": params,
                        "ok": res.get("ok", False), "verified": res.get("verified", False),
                        "latency_ms": lat, "tool_latency_ms": res.get("latency_ms"),
                        "exec_latency_ms": (t_done - t_decision) * 1000 if t_decision else lat,
                        "actuation": tool in ACTUATION_TOOLS and not res.get("noop", False),
                        "guardrail": guardrail, "reason": reason, "error": res.get("error"),
                        "btm_ho_ms": res.get("btm_ho_ms"), "t_call": t_call})
        return res

    def log_rejected(self, tool: str, params: dict[str, Any], reason: str) -> None:
        self.log.write({"type": "tool", "agent": self.caller, "tool": tool, "params": params, "ok": False,
                        "verified": False, "executed": False, "actuation": False, "guardrail": "reject",
                        "reason": reason})


def wait_until(t: float, stop: threading.Event) -> bool:
    """Sleep until wall time ``t``; False if stopped."""
    d = t - time.time()
    return not (d > 0 and stop.wait(d))


def load_prompt(name: str) -> str:
    return (PROMPTS / name).read_text()


def load_schema(name: str, ap_names: list[str]) -> dict[str, Any]:
    """Load a schema and specialise its AP enums to the current topology."""
    raw = (SCHEMAS / name).read_text().replace('["ap1","ap2","ap3","ap4"]', json.dumps(ap_names))
    return json.loads(raw)


# ------------------------------------------------------------------------- prompt state tables
def _fmt(v: Any, nd: int = 1) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def ap_aggregates(cache: StateCache, topo: dict[str, Any], window_s: float = 10.0) -> dict[str, dict[str, Any]]:
    """Per-AP aggregated state over the last ``window_s`` (planner input; size ∝ n_ap)."""
    win = cache.window(window_s)
    if not win:
        return {}
    last = win[-1]
    hos = cache.recent_handovers(window_s)
    out: dict[str, dict[str, Any]] = {}
    for ap, a in last["aps"].items():
        stas = {s: st for s, st in last["stas"].items() if st.get("ap") == ap}
        counts = {sl: sum(1 for st in stas.values() if st["slice"] == sl) for sl in C.SLICES}
        sla = {}
        for sl in ("telemedicine", "video"):
            vals = [st["sla_met"] for h in win for st in h["stas"].values() if st.get("ap") == ap and st["slice"] == sl]
            sla[sl] = 100.0 * sum(vals) / len(vals) if vals else float("nan")
        be_thr = sum(st["thr_mbps"] for st in stas.values() if st["slice"] == "best_effort")
        rssi = [st["rssi"] for st in stas.values() if st.get("rssi") is not None]
        dens = 0
        for st in last["stas"].values():
            est = st.get("est_rssi") or {}
            if est and max(est, key=est.get) == ap:
                dens += 1
        out[ap] = {"ch": a["channel"], "pwr": a["txpower"], "air": a["airtime"], "air10": a["airtime_10s"],
                   "n": counts, "sla_tele": sla["telemedicine"], "sla_video": sla["video"], "be_thr": be_thr,
                   "rssi_mean": sum(rssi) / len(rssi) if rssi else float("nan"),
                   "ho10": sum(1 for h in hos if ap in (h[2], h[3])), "density": dens,
                   "nbrs": topo["neighbours"].get(ap, []), "rates": a["rates"]}
    return out


def planner_user_message(cache: StateCache, topo: dict[str, Any], t_rel: float, plan_no: int,
                         current_plan: dict[str, Any] | None) -> str:
    agg = ap_aggregates(cache, topo)
    lines = [f"t={t_rel:.0f}s plan#{plan_no}",
             "AP|ch|pwr_dBm|air_1s|air_10s|n_tele|n_video|n_be|tele_SLA10s%|video_SLA10s%|be_thr_Mbps|"
             "mean_RSSI|HO_10s|density|rate_tele/video/be|neighbours"]
    for ap in sorted(agg, key=lambda s: int(s[2:])):
        a = agg[ap]
        r = a["rates"]
        lines.append("|".join([ap, str(a["ch"]), _fmt(a["pwr"], 0), _fmt(a["air"], 2), _fmt(a["air10"], 2),
                               str(a["n"]["telemedicine"]), str(a["n"]["video"]), str(a["n"]["best_effort"]),
                               _fmt(a["sla_tele"], 0), _fmt(a["sla_video"], 0), _fmt(a["be_thr"]),
                               _fmt(a["rssi_mean"], 0), str(a["ho10"]), str(a["density"]),
                               f"{_fmt(r.get('telemedicine'))}/{_fmt(r.get('video'))}/{_fmt(r.get('best_effort'))}",
                               ",".join(a["nbrs"])]))
    last = cache.latest() or {"stas": {}}
    bad = [f"{s}@{st.get('ap')}(thr={_fmt(st['thr_mbps'])},owd95={_fmt(st['owd_p95_ms'], 0)},"
           f"jit={_fmt(st['jitter_ms'])},loss={_fmt(st['loss_pct'])})"
           for s, st in last["stas"].items() if st["slice"] == "telemedicine" and not st["sla_met"]]
    lines.append("Telemedicine flows violating now: " + (", ".join(bad) if bad else "none"))
    lines.append("Current plan: " + (json.dumps(current_plan, separators=(",", ":")) if current_plan else "none"))
    lines.append("Return the new plan JSON.")
    return "\n".join(lines)


def flat_user_message(cache: StateCache, topo: dict[str, Any], t_rel: float) -> str:
    """Per-STA table (size ∝ n_sta) — the flat agent sees every station."""
    last = cache.latest() or {"stas": {}, "aps": {}}
    lines = [f"t={t_rel:.0f}s",
             "AP|ch|pwr_dBm|air_1s|rate_tele/video/be|neighbours"]
    for ap in sorted(last["aps"], key=lambda s: int(s[2:])):
        a = last["aps"][ap]
        r = a["rates"]
        lines.append(f"{ap}|{a['channel']}|{_fmt(a['txpower'], 0)}|{_fmt(a['airtime'], 2)}|"
                     f"{_fmt(r.get('telemedicine'))}/{_fmt(r.get('video'))}/{_fmt(r.get('best_effort'))}|"
                     f"{','.join(topo['neighbours'].get(ap, []))}")
    lines.append("STA|slice|ap|rssi|best_alt_ap:est_rssi|thr_Mbps|loss%|owd95_ms|jit_ms|sla")
    for s in sorted(last["stas"], key=lambda n: int(n[3:])):
        st = last["stas"][s]
        est = {a: v for a, v in (st.get("est_rssi") or {}).items() if a != st.get("ap")}
        alt = max(est, key=est.get) if est else "-"
        lines.append(f"{s}|{st['slice'][:4]}|{st.get('ap') or '-'}|{_fmt(st.get('rssi'), 0)}|"
                     f"{alt}:{_fmt(est.get(alt), 0) if est else '-'}|{_fmt(st['thr_mbps'])}|{_fmt(st['loss_pct'])}|"
                     f"{_fmt(st['owd_p95_ms'], 0)}|{_fmt(st['jitter_ms'])}|{'ok' if st['sla_met'] else 'VIOL'}")
    lines.append("Return the actions JSON.")
    return "\n".join(lines)


def system_with_intents(template: str, cfg: dict[str, Any], topo: dict[str, Any]) -> str:
    intents = C.load_intents()
    compact = {s: v["intent"] for s, v in intents["slices"].items()}
    return (template.replace("{INTENTS}", json.dumps(compact))
            .replace("{APS}", ", ".join(sorted(topo["aps"], key=lambda s: int(s[2:]))))
            .replace("{NEIGHBOURS}", json.dumps(topo["neighbours"]))
            .replace("{CAPACITY}", f"{float(cfg['ap_capacity_mbps']):g}")
            .replace("{PMIN}", f"{float(cfg['txpower_min_dbm']):g}")
            .replace("{PMAX}", f"{float(cfg['txpower_max_dbm']):g}")
            .replace("{PSTEP}", f"{float(cfg['txpower_max_step_db']):g}")
            .replace("{HOLDDOWN}", f"{float(cfg['btm_holddown_s']):g}")
            .replace("{RSSI_MIN}", f"{float(cfg['rssi_min_target_dbm']):g}"))
