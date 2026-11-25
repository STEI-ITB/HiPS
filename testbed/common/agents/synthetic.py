"""Synthetic but realistic network states (no emulator).

Used by ``experiment-1/slm_bench.py`` (SLM latency on representative planner / flat prompts; also
Experiment 9) and by the Experiment 10b benchmark (``bench/``). A fresh random state per call means the
user message differs every call, so Ollama only reuses the KV cache of the static system prompt —
as in a real run.
"""
from __future__ import annotations

import time
from typing import Any

import numpy as np

from common import config as C
from common import radio


def topology(cfg: dict[str, Any]) -> dict[str, Any]:
    lay = radio.ap_layout(cfg)
    smap = C.slice_of_sta(int(cfg["n_sta"]), cfg["slice_fractions"])
    return {"aps": {a: {"x": p["x"], "y": p["y"], "channel": p["channel"], "bssid": f"02:00:00:aa:00:{i:02x}"}
                    for i, (a, p) in enumerate(lay.items(), 1)},
            "neighbours": radio.neighbours(lay),
            "stas": {s: {"mac": C.sta_mac(int(s[3:])), "slice": sl} for s, sl in smap.items()},
            "capacity_mbps": float(cfg["ap_capacity_mbps"])}


def snapshot(cfg: dict[str, Any], rng: np.random.Generator, hotspot: bool = False, ts: float | None = None) -> dict[str, Any]:
    lay = radio.ap_layout(cfg)
    W, H = radio.area_size(cfg)
    smap = C.slice_of_sta(int(cfg["n_sta"]), cfg["slice_fractions"])
    intents = C.load_intents()["slices"]
    stas: dict[str, Any] = {}
    load: dict[str, float] = {a: 0.0 for a in lay}
    tgt = cfg.get("hotspot_target", "ap2")
    for s, sl in smap.items():
        if hotspot and rng.random() < 0.35:
            x, y = lay[tgt]["x"] + rng.normal(0, 6), lay[tgt]["y"] + rng.normal(0, 6)
        else:
            x, y = rng.uniform(0, W), rng.uniform(0, H)
        est = {a: round(radio.est_rssi_dbm(float(cfg["ap_txpower_dbm"]), radio.distance(x, y, p["x"], p["y"]), cfg), 1)
               for a, p in lay.items()}
        ap = max(est, key=est.get)
        rate = intents[sl]["traffic"].get("rate_mbps", 4.0)
        load[ap] += rate
        stas[s] = {"mac": C.sta_mac(int(s[3:])), "slice": sl, "ap": ap, "rssi": est[ap] + rng.normal(0, 2),
                   "est_rssi": est, "pos": (round(x, 1), round(y, 1))}
    aps = {}
    for a, p in lay.items():
        air = float(np.clip(load[a] / float(cfg["ap_capacity_mbps"]) * rng.uniform(0.7, 1.1), 0.05, 1.0))
        aps[a] = {"airtime": round(air, 3), "airtime_10s": round(float(np.clip(air + rng.normal(0, 0.05), 0, 1)), 3),
                  "txpower": float(cfg["ap_txpower_dbm"]), "channel": p["channel"],
                  "rates": {sl: round(float(cfg["ap_capacity_mbps"]) * f, 2) for sl, f in cfg["slice_shares_b0"].items()}}
    for s, st in stas.items():
        congested = aps[st["ap"]]["airtime"] > 0.8
        sl = st["slice"]
        rate = intents[sl]["traffic"].get("rate_mbps", 3.0)
        thr = rate * (rng.uniform(0.6, 0.95) if congested else rng.uniform(0.95, 1.0))
        owd = float(rng.uniform(25, 80) if congested else rng.uniform(4, 20))
        jit = float(rng.uniform(3, 9) if congested else rng.uniform(0.5, 3))
        loss = float(rng.uniform(1, 6) if congested else rng.uniform(0, 0.5))
        it = intents[sl]["intent"]
        met = True
        if sl == "telemedicine":
            met = thr >= it["thr_min_mbps"] and loss <= it["loss_max_pct"] and jit <= it["jitter_max_ms"] and owd <= it["owd_p95_max_ms"]
        elif sl == "video":
            met = thr >= it["thr_min_mbps"] and loss <= it["loss_max_pct"]
        st.update({"thr_mbps": round(thr, 3), "loss_pct": round(loss, 2), "jitter_ms": round(jit, 2),
                   "owd_p95_ms": round(owd, 1), "sla_met": bool(met)})
    return {"ok": True, "ts": ts if ts is not None else time.time(), "aps": aps, "stas": stas}


class FakeCache:
    """Duck-typed stand-in for ``StateCache`` holding a synthetic 10 s history."""

    def __init__(self, cfg: dict[str, Any], rng: np.random.Generator, hotspot: bool = False, n: int = 10):
        now = time.time()
        self.hist = [snapshot(cfg, rng, hotspot, ts=now - (n - 1 - i)) for i in range(n)]
        self.ho = [(now - float(rng.uniform(0, 10)), f"sta{int(rng.integers(7, int(cfg['n_sta']) + 1))}",
                    "ap1", "ap2") for _ in range(int(rng.integers(0, 4)))]

    def latest(self) -> dict[str, Any]:
        return self.hist[-1]

    def window(self, seconds: float) -> list[dict[str, Any]]:
        t = self.hist[-1]["ts"]
        return [h for h in self.hist if h["ts"] > t - seconds]

    def recent_handovers(self, seconds: float) -> list[tuple[float, str, str, str]]:
        return self.ho
