#!/usr/bin/env python3
"""Seeded Gauss-Markov + hotspot mobility: trace generator (CLI) and replayer thread.

The trace is a CSV ``t,sta,x,y`` sampled every ``dt_ms`` from t0 (traffic start). It depends only
on (layout, n_sta, speed, hotspot parameters, duration, seed) — never on the framework — so every
framework of the same seed replays the identical trace (paired design).

Gauss-Markov (Liang & Haas): speed and direction are updated once per second::

    s_n = a s_{n-1} + (1-a) s_mean + sqrt(1-a^2) sigma_s w
    d_n = a d_{n-1} + (1-a) d_mean + sqrt(1-a^2) sigma_d w

with the mean direction steered away from walls near the area border.

Hotspot: ``hotspot_size`` STAs (slice-proportional, at least one telemedicine STA) walk
in straight lines into the target AP's cell during ``[start, start + hotspot_arrive_s]``,
loiter (0.3 m/s random walk within 10 m) until ``hotspot_end_s`` and then resume Gauss-Markov.

Example::

    python3 common/mobility.py --n_sta 30 --area 120x120 --speed_mps 2.5 --alpha 0.75 \\
      --hotspot_size 10 --hotspot_target ap2 --hotspot_start_s 150 --hotspot_end_s 210 \\
      --duration_s 300 --dt_ms 100 --seed 3 --out output/traces/v2.5_h10_s03.csv
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import math
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import config as C  # noqa: E402
from common import radio  # noqa: E402

LOG = logging.getLogger("mobility")


def choose_hotspot_stas(cfg: dict[str, Any], rng: np.random.Generator) -> list[str]:
    """Slice-proportional choice; always contains >= 1 telemedicine STA when size > 0."""
    size = int(cfg["hotspot_size"])
    if size <= 0:
        return []
    smap = C.slice_of_sta(int(cfg["n_sta"]), cfg["slice_fractions"])
    by_slice: dict[str, list[str]] = {s: [k for k, v in smap.items() if v == s] for s in C.SLICES}
    want = {s: int(round(size * cfg["slice_fractions"][s])) for s in C.SLICES}
    want["telemedicine"] = max(1, want["telemedicine"])
    want["best_effort"] = size - want["telemedicine"] - want["video"]
    chosen: list[str] = []
    for s in C.SLICES:
        pool = by_slice[s]
        k = max(0, min(want[s], len(pool)))
        chosen += list(rng.choice(pool, size=k, replace=False)) if k else []
    rest = [k for k in smap if k not in chosen]
    while len(chosen) < size and rest:
        pick = str(rng.choice(rest))
        rest.remove(pick)
        chosen.append(pick)
    return sorted(chosen, key=lambda n: int(n[3:]))


def hotspot_targets(cfg: dict[str, Any], stas: list[str], rng: np.random.Generator) -> dict[str, tuple[float, float]]:
    """Target points inside the target AP's Voronoi cell, within 10 m of a cluster centre."""
    layout = radio.ap_layout(cfg)
    tgt = cfg["hotspot_target"]
    ax, ay = layout[tgt]["x"], layout[tgt]["y"]
    cx = sum(p["x"] for p in layout.values()) / len(layout)
    cy = sum(p["y"] for p in layout.values()) / len(layout)
    # centre: 5 m from the AP, away from the grid centre
    vx, vy = ax - cx, ay - cy
    n = math.hypot(vx, vy) or 1.0
    ccx, ccy = ax + 5 * vx / n, ay + 5 * vy / n
    out: dict[str, tuple[float, float]] = {}
    for s in stas:
        for _ in range(200):
            r = 10 * math.sqrt(rng.random())
            th = 2 * math.pi * rng.random()
            px, py = ccx + r * math.cos(th), ccy + r * math.sin(th)
            if radio.nearest_ap(px, py, layout) == tgt:
                break
        out[s] = (px, py)
    return out


def generate(cfg: dict[str, Any]) -> tuple[list[tuple[float, str, float, float]], dict[str, Any]]:
    """Return rows (t, sta, x, y) for the whole run and the trace metadata."""
    rng = np.random.default_rng(int(cfg["seed"]) * 1000 + 7)
    n_sta = int(cfg["n_sta"])
    W, H = radio.area_size(cfg)
    dt = float(cfg["dt_ms"]) / 1000.0
    dur = float(cfg["duration_s"])
    steps = int(round(dur / dt)) + 1
    alpha = float(cfg["gm_alpha"])
    vbar = float(cfg["speed_mps"])
    jit = float(cfg["speed_jitter"])
    margin = 5.0
    names = [f"sta{i}" for i in range(1, n_sta + 1)]

    pos = np.column_stack([rng.uniform(margin, W - margin, n_sta), rng.uniform(margin, H - margin, n_sta)])
    s_mean = vbar * (1.0 + rng.uniform(-jit, jit, n_sta))
    speed = s_mean.copy()
    d_mean = rng.uniform(0, 2 * math.pi, n_sta)
    direc = d_mean.copy()
    sig_s = 0.2 * s_mean
    sig_d = math.pi / 4

    hs = choose_hotspot_stas(cfg, rng)
    h_idx = [names.index(s) for s in hs]
    targets = hotspot_targets(cfg, hs, rng) if hs else {}
    h_start = float(cfg["hotspot_start_s"])
    h_arrive = h_start + float(cfg["hotspot_arrive_s"])
    h_end = float(cfg["hotspot_end_s"])
    h_from: dict[int, np.ndarray] = {}

    rows: list[tuple[float, str, float, float]] = []
    gm_every = int(round(1.0 / dt))
    for k in range(steps):
        t = k * dt
        if k % gm_every == 0 and k > 0:
            w1, w2 = rng.standard_normal(n_sta), rng.standard_normal(n_sta)
            speed = alpha * speed + (1 - alpha) * s_mean + math.sqrt(1 - alpha ** 2) * sig_s * w1
            speed = np.clip(speed, 0.0, 3.0 * s_mean + 0.1)
            # steer mean direction away from walls (standard GM edge handling)
            for i in range(n_sta):
                x, y = pos[i]
                if x < 10 or x > W - 10 or y < 10 or y > H - 10:
                    d_mean[i] = math.atan2(H / 2 - y, W / 2 - x)
            direc = alpha * direc + (1 - alpha) * d_mean + math.sqrt(1 - alpha ** 2) * sig_d * w2
        step = np.column_stack([speed * np.cos(direc), speed * np.sin(direc)]) * dt
        new = pos + step
        # reflect at the borders
        for dim, lim in ((0, W), (1, H)):
            lo = new[:, dim] < 0
            hi = new[:, dim] > lim
            new[lo, dim] = -new[lo, dim]
            new[hi, dim] = 2 * lim - new[hi, dim]
            direc[lo | hi] = direc[lo | hi] + math.pi
        # hotspot overrides
        for i in h_idx:
            s = names[i]
            if h_start <= t < h_arrive:
                if i not in h_from:
                    h_from[i] = pos[i].copy()
                frac = (t - h_start + dt) / (h_arrive - h_start)
                tx, ty = targets[s]
                new[i] = h_from[i] + min(1.0, frac) * (np.array([tx, ty]) - h_from[i])
            elif h_arrive <= t < h_end:
                tx, ty = targets[s]
                jitter = rng.normal(0, 0.3 * dt, 2)  # ~0.3 m/s loiter
                cand = pos[i] + jitter
                if math.hypot(cand[0] - tx, cand[1] - ty) <= 10:
                    new[i] = cand
                else:
                    new[i] = pos[i]
            elif t >= h_end and i in h_from:
                # disperse: fresh random heading, resume GM
                d_mean[i] = rng.uniform(0, 2 * math.pi)
                direc[i] = d_mean[i]
                h_from.pop(i)
        pos = new
        for i, n in enumerate(names):
            rows.append((round(t, 3), n, round(float(pos[i, 0]), 3), round(float(pos[i, 1]), 3)))
    LOG.info("trace: %d STAs, %d steps, hotspot=%s", n_sta, steps, hs)
    meta = {"hotspot_stas": hs, "hotspot_targets": {k: list(v) for k, v in targets.items()},
            "s_mean": [float(x) for x in s_mean], "area": [W, H], "seed": int(cfg["seed"])}
    return rows, meta


def write_trace(rows: list[tuple[float, str, float, float]], out: Path,
                meta: dict[str, Any] | None = None) -> str:
    out.parent.mkdir(parents=True, exist_ok=True)
    if meta is not None:
        C.write_json(Path(str(out) + ".meta.json"), meta)
    tmp = out.with_suffix(".tmp")
    with open(tmp, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["t", "sta", "x", "y"])
        w.writerows(rows)
    tmp.replace(out)
    return file_hash(out)


def file_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ensure_trace(cfg: dict[str, Any]) -> tuple[Path, str]:
    """Generate the run's trace if missing; return (path, sha256)."""
    p = Path(cfg["trace_file"])
    if not p.exists():
        rows, meta = generate(cfg)
        write_trace(rows, p, meta)
    return p, file_hash(p)


def load_trace(path: Path) -> tuple[np.ndarray, list[str], np.ndarray]:
    """Return (times[T], sta names[N], positions[T, N, 2])."""
    import pandas as pd

    df = pd.read_csv(path)
    names = sorted(df["sta"].unique(), key=lambda n: int(str(n)[3:]))
    times = np.sort(df["t"].unique())
    piv_x = df.pivot(index="t", columns="sta", values="x")[names].to_numpy()
    piv_y = df.pivot(index="t", columns="sta", values="y")[names].to_numpy()
    return times, names, np.stack([piv_x, piv_y], axis=-1)


def trace_stats(cfg: dict[str, Any], path: Path) -> dict[str, Any]:
    """Speed statistics and hotspot check used by the Phase-3 validation."""
    times, names, P = load_trace(path)
    dt = float(times[1] - times[0])
    v = np.linalg.norm(np.diff(P, axis=0), axis=-1) / dt
    import json
    meta = json.loads(Path(str(path) + ".meta.json").read_text())
    hs = meta["hotspot_stas"]
    layout = radio.ap_layout(cfg)
    t_check = (float(cfg["hotspot_start_s"]) + float(cfg["hotspot_arrive_s"]) + float(cfg["hotspot_end_s"])) / 2
    k = int(np.argmin(np.abs(times - t_check)))
    in_cell = [s for s in hs if radio.nearest_ap(*P[k, names.index(s)], layout) == cfg["hotspot_target"]]
    bg = [i for i, n in enumerate(names) if n not in hs]
    return {"mean_speed_all": float(v.mean()), "mean_speed_background": float(v[:, bg].mean()),
            "p95_speed": float(np.percentile(v, 95)), "hotspot_stas": hs,
            "hotspot_in_target_cell": f"{len(in_cell)}/{len(hs)}", "t_check": float(times[k])}


class Replayer(threading.Thread):
    """Calls ``set_position(sta_name, x, y)`` for every STA every dt on an absolute schedule."""

    def __init__(self, trace: Path, set_position: Callable[[str, float, float], None], t0: float,
                 stop: threading.Event):
        super().__init__(name="mobility", daemon=True)
        self.times, self.names, self.P = load_trace(trace)
        self.set_position = set_position
        self.t0 = t0
        self.stop_ev = stop
        self.late = 0

    def run(self) -> None:
        for k, t in enumerate(self.times):
            if self.stop_ev.is_set():
                return
            delay = self.t0 + float(t) - time.time()
            if delay > 0:
                if self.stop_ev.wait(delay):
                    return
            elif delay < -0.05:
                self.late += 1
                continue  # behind schedule: skip this sample rather than drift
            for j, n in enumerate(self.names):
                self.set_position(n, float(self.P[k, j, 0]), float(self.P[k, j, 1]))
        LOG.info("mobility replay finished (%d late samples skipped)", self.late)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n_ap", type=int, default=None)
    ap.add_argument("--n_sta", type=int, default=None)
    ap.add_argument("--area", default=None, help="WxH in metres (informative; derived from n_ap)")
    ap.add_argument("--speed_mps", type=float, default=None)
    ap.add_argument("--alpha", type=float, default=None)
    ap.add_argument("--hotspot_size", type=int, default=None)
    ap.add_argument("--hotspot_target", default=None)
    ap.add_argument("--hotspot_start_s", type=float, default=None)
    ap.add_argument("--hotspot_end_s", type=float, default=None)
    ap.add_argument("--duration_s", type=float, default=None)
    ap.add_argument("--dt_ms", type=float, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--stats", action="store_true", help="print speed / hotspot statistics")
    a = ap.parse_args(argv)
    C.setup_logging()
    cfg = C.load_base()
    for k, v in vars(a).items():
        if v is not None and k not in ("out", "stats", "area", "alpha"):
            cfg[k] = v
    if a.alpha is not None:
        cfg["gm_alpha"] = a.alpha
    cfg["trace_file"] = a.out
    rows, meta = generate(cfg)
    h = write_trace(rows, Path(a.out), meta)
    LOG.info("wrote %s sha256=%s", a.out, h[:16])
    if a.stats:
        import json
        print(json.dumps(trace_stats(cfg, Path(a.out)), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
