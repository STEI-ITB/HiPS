"""Agent tool shims with post-condition verification.

Every tool returns a dict with ``ok`` (command succeeded), ``verified`` (post-condition observed)
and ``latency_ms`` (call → effect verified). Actuation tools clamp only to *physically* valid
values (``txpower_phys_*``, ``0 < rate ≤ ap_capacity_mbps``); policy bounds are the guardrail's job.

AP-side commands (``iw``, ``hostapd_cli``, ``tc``) run in the root namespace (Mininet-WiFi APs are
not namespaced), so they are thread-safe and do not touch Mininet's per-node shells.
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from common import config as C
from common import radio
from common.telemetry import station_dump

LOG = logging.getLogger("tools")

ACTUATION_TOOLS = ("set_tx_power", "trigger_bss_transition", "adjust_slice_bandwidth")
READ_TOOLS = ("get_ap_airtime_utilization", "get_station_stats", "get_slice_kpis")
ALL_TOOLS = READ_TOOLS + ACTUATION_TOOLS


def sh(cmd: list[str], timeout: float = 3.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def tc_class_rate_mbps(intf: str, classid: str) -> float | None:
    """Current HTB rate of ``classid`` on ``intf`` in Mb/s (None if absent)."""
    out = sh(["tc", "class", "show", "dev", intf]).stdout
    for line in out.splitlines():
        if f"htb {classid} " in line + " ":
            m = re.search(r"rate (\d+(?:\.\d+)?)([KMG]?)bit", line)
            if m:
                mult = {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1e3}[m.group(2)]
                return float(m.group(1)) * mult
    return None


def iw_txpower_dbm(intf: str) -> float | None:
    m = re.search(r"txpower (\d+(?:\.\d+)?) dBm", sh(["iw", "dev", intf, "info"]).stdout)
    return float(m.group(1)) if m else None


class Tools:
    """Tool implementations bound to one running emulation."""

    def __init__(self, cfg: dict[str, Any], aps: dict[str, dict[str, Any]], stas: dict[str, dict[str, Any]],
                 telemetry: Any, live_kpi: Any, set_txpower_fn: Callable[[str, float], None],
                 positions: Callable[[], dict[str, tuple[float, float]]], event_log: Callable[[dict[str, Any]], None],
                 get_txpower_fn: Callable[[str], float | None] | None = None):
        self.cfg = cfg
        self.aps = aps          # name -> {intf, bssid, channel, x, y}
        self.stas = stas        # name -> {mac, slice, ip}
        self.mac2sta = {v["mac"]: k for k, v in stas.items()}
        self.telemetry = telemetry
        self.live = live_kpi
        self._set_txpower = set_txpower_fn
        # read-back for set_tx_power; default `iw dev <intf> info`, which on mac80211_hwsim with
        # kernel 7.0 keeps reporting 20 dBm although the power reaching wmediumd changed
        self._get_txpower = get_txpower_fn or (lambda ap: iw_txpower_dbm(self.aps[ap]["intf"]))
        self.positions = positions
        self.event_log = event_log
        self.intents = C.load_intents()
        self.txpower = {a: float(cfg["ap_txpower_dbm"]) for a in aps}
        self.rates = {a: dict(self._initial_rates()) for a in aps}
        self.layout = {a: {"x": v["x"], "y": v["y"], "channel": v["channel"]} for a, v in aps.items()}
        self.lock = threading.Lock()
        self.ctrl = cfg.get("paths", {}).get("hostapd_ctrl", "/var/run/hostapd")

    def _initial_rates(self) -> dict[str, float]:
        cap = float(self.cfg["ap_capacity_mbps"])
        return {s: round(cap * f, 3) for s, f in self.cfg["slice_shares_b0"].items()}

    # ------------------------------------------------------------------ helpers
    def serving_ap(self, sta: str) -> str | None:
        """Fresh association lookup (station dumps of all APs)."""
        mac = self.stas[sta]["mac"]
        for ap, info in self.aps.items():
            if mac in station_dump(info["intf"]):
                return ap
        return None

    def est_rssi(self, sta: str, ap: str) -> float:
        pos = self.positions().get(sta)
        if pos is None:
            return -100.0
        a = self.aps[ap]
        return radio.est_rssi_dbm(self.txpower[ap], radio.distance(pos[0], pos[1], a["x"], a["y"]), self.cfg)

    # ------------------------------------------------------------------ read tools
    def get_ap_airtime_utilization(self, ap_id: str) -> dict[str, Any]:
        t0 = time.perf_counter()
        val = self.telemetry.airtime(ap_id, 1.0)
        return {"ok": True, "verified": True, "ap_id": ap_id, "airtime": round(val, 4),
                "latency_ms": (time.perf_counter() - t0) * 1000}

    def get_station_stats(self, ap_id: str) -> dict[str, Any]:
        t0 = time.perf_counter()
        dump = station_dump(self.aps[ap_id]["intf"])
        stas = {self.mac2sta.get(m, m): st for m, st in dump.items()}
        return {"ok": True, "verified": True, "ap_id": ap_id, "stations": stas,
                "latency_ms": (time.perf_counter() - t0) * 1000}

    def get_slice_kpis(self, ap_id: str, slice: str, window_s: float = 5.0) -> dict[str, Any]:  # noqa: A002
        t0 = time.perf_counter()
        assoc = self.telemetry.latest().get("assoc", {})
        flows = {s: self.live.flow_kpis(s, window_s) for s, info in self.stas.items()
                 if info["slice"] == slice and assoc.get(s) == ap_id}
        intent = self.intents["slices"][slice]["intent"]
        met = {s: kpi_meets(slice, k, intent) for s, k in flows.items()}
        agg_thr = sum(k["thr_mbps"] for k in flows.values())
        return {"ok": True, "verified": True, "ap_id": ap_id, "slice": slice, "flows": flows,
                "sla_met": met, "all_met": all(met.values()) if met else True, "agg_thr_mbps": agg_thr,
                "latency_ms": (time.perf_counter() - t0) * 1000}

    # ------------------------------------------------------------------ actuation tools
    def set_tx_power(self, ap_id: str, dbm: float) -> dict[str, Any]:
        t0 = time.perf_counter()
        lo, hi = float(self.cfg["txpower_phys_min_dbm"]), float(self.cfg["txpower_phys_max_dbm"])
        req = float(dbm)
        dbm = min(hi, max(lo, req))
        try:
            with self.lock:
                self._set_txpower(ap_id, dbm)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "verified": False, "error": str(exc), "latency_ms": (time.perf_counter() - t0) * 1000}
        verified = False
        for _ in range(25):
            cur = self._get_txpower(ap_id)
            if cur is not None and abs(cur - dbm) < 0.6:
                verified = True
                break
            time.sleep(0.01)
        if verified:
            self.txpower[ap_id] = dbm
        return {"ok": True, "verified": verified, "ap_id": ap_id, "dbm": dbm, "requested_dbm": req,
                "clamped": dbm != req, "latency_ms": (time.perf_counter() - t0) * 1000}

    def trigger_bss_transition(self, sta_mac: str, target_ap: str, timeout_s: float = 2.0) -> dict[str, Any]:
        t0 = time.perf_counter()
        sta = self.mac2sta.get(sta_mac.lower(), sta_mac)
        if sta not in self.stas:
            return {"ok": False, "verified": False, "error": f"unknown sta {sta_mac}", "latency_ms": 0.0}
        mac = self.stas[sta]["mac"]
        serving = self.serving_ap(sta)
        if serving is None:
            return {"ok": False, "verified": False, "error": "sta not associated", "latency_ms":
                    (time.perf_counter() - t0) * 1000}
        if serving == target_ap:
            return {"ok": True, "verified": True, "noop": True, "latency_ms": (time.perf_counter() - t0) * 1000}
        tgt = self.aps[target_ap]
        neighbor = f"{tgt['bssid']},0x0000,81,{tgt['channel']},7"
        cmd = ["hostapd_cli", "-p", self.ctrl, "-i", self.aps[serving]["intf"], "bss_tm_req", mac,
               "pref=1", "abridged=1", "disassoc_imminent=1", "disassoc_timer=5", f"neighbor={neighbor}"]
        t_req = time.time()
        r = sh(cmd)
        ok = "OK" in r.stdout
        self.event_log({"ts": t_req, "sta": sta, "event": "BTM_REQ", "ap_from": serving, "ap_to": target_ap,
                        "status": "OK" if ok else r.stdout.strip()[:40]})
        if not ok:
            return {"ok": False, "verified": False, "error": r.stdout.strip() or r.stderr.strip(),
                    "latency_ms": (time.perf_counter() - t0) * 1000}
        verified = False
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if mac in station_dump(tgt["intf"]):
                verified = True
                break
            time.sleep(0.02)
        btm_ho_ms = (time.time() - t_req) * 1000 if verified else float("nan")
        return {"ok": True, "verified": verified, "sta": sta, "from": serving, "to": target_ap,
                "btm_ho_ms": btm_ho_ms, "latency_ms": (time.perf_counter() - t0) * 1000}

    def adjust_slice_bandwidth(self, ap_id: str, slice: str, rate_mbps: float) -> dict[str, Any]:  # noqa: A002
        t0 = time.perf_counter()
        cap = float(self.cfg["ap_capacity_mbps"])
        req = float(rate_mbps)
        rate = min(cap, max(0.1, req))
        sl = self.intents["slices"][slice]
        intf = self.aps[ap_id]["intf"]
        cmd = ["tc", "class", "change", "dev", intf, "parent", "1:1", "classid", sl["htb_classid"], "htb",
               "rate", f"{rate:g}mbit", "ceil", f"{cap:g}mbit", "prio", str(sl["htb_prio"])]
        r = sh(cmd)
        if r.returncode != 0:
            return {"ok": False, "verified": False, "error": r.stderr.strip(),
                    "latency_ms": (time.perf_counter() - t0) * 1000}
        cur = tc_class_rate_mbps(intf, sl["htb_classid"])
        verified = cur is not None and abs(cur - rate) <= max(0.05, 0.02 * rate)
        if verified:
            self.rates[ap_id][slice] = rate
        return {"ok": True, "verified": verified, "ap_id": ap_id, "slice": slice, "rate_mbps": rate,
                "requested_mbps": req, "clamped": rate != req, "latency_ms": (time.perf_counter() - t0) * 1000}

    # ------------------------------------------------------------------ helpers for agents
    def get_topology(self) -> dict[str, Any]:
        lay = {a: {"x": v["x"], "y": v["y"], "channel": v["channel"], "bssid": v["bssid"]} for a, v in self.aps.items()}
        nb = radio.neighbours(radio.ap_layout(self.cfg)) if int(self.cfg["n_ap"]) in (4, 6, 9) \
            else {a: [] for a in self.aps}
        return {"ok": True, "aps": lay, "neighbours": nb,
                "stas": {s: {"mac": v["mac"], "slice": v["slice"]} for s, v in self.stas.items()},
                "capacity_mbps": float(self.cfg["ap_capacity_mbps"])}

    def get_snapshot(self, window_s: float = 10.0) -> dict[str, Any]:
        """Aggregated state used to build agent prompts (not counted as a tool call)."""
        latest = self.telemetry.latest()
        assoc = latest.get("assoc", {})
        pos = self.positions()
        stas: dict[str, Any] = {}
        for s, info in self.stas.items():
            ap = assoc.get(s)
            st = latest.get("aps", {}).get(ap, {}).get("stas", {}).get(s, {}) if ap else {}
            k = self.live.flow_kpis(s, 5.0)
            met = kpi_meets(info["slice"], k, self.intents["slices"][info["slice"]]["intent"])
            est = {a: round(self.est_rssi(s, a), 1) for a in self.aps}
            stas[s] = {"mac": info["mac"], "slice": info["slice"], "ap": ap,
                       "rssi": st.get("signal_dbm"), "est_rssi": est, "pos": pos.get(s),
                       "thr_mbps": round(k["thr_mbps"], 3), "loss_pct": k["loss_pct"],
                       "jitter_ms": k["jitter_ms"], "owd_p95_ms": k["owd_p95_ms"], "sla_met": met}
        aps = {a: {"airtime": round(self.telemetry.airtime(a, 1.0), 3),
                   "airtime_10s": round(self.telemetry.airtime(a, window_s), 3),
                   "txpower": self.txpower[a], "rates": dict(self.rates[a]), "channel": self.aps[a]["channel"]}
               for a in self.aps}
        return {"ok": True, "ts": time.time(), "aps": aps, "stas": stas}


def kpi_meets(slice_name: str, k: dict[str, float], intent: dict[str, Any]) -> bool:
    """Per-flow intent check used live and (per 1-s window) by kpi.py."""
    def ok(val: float, lim: float) -> bool:
        return val == val and val <= lim  # NaN -> not met

    if slice_name == "telemedicine":
        return (k["thr_mbps"] >= intent["thr_min_mbps"] and ok(k["loss_pct"], intent["loss_max_pct"])
                and ok(k["jitter_ms"], intent["jitter_max_ms"])
                and (k["owd_p95_ms"] != k["owd_p95_ms"] or k["owd_p95_ms"] <= intent["owd_p95_max_ms"]))
    if slice_name == "video":
        return k["thr_mbps"] >= intent["thr_min_mbps"] and ok(k["loss_pct"], intent["loss_max_pct"])
    return True  # best-effort is evaluated per AP aggregate


def dispatch(tools: Tools, method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method not in ALL_TOOLS + ("get_topology", "get_snapshot"):
        raise KeyError(method)
    return getattr(tools, method)(**params)


def write_tool_log(path: Path, entry: dict[str, Any]) -> None:
    import json

    with open(path, "a") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")
