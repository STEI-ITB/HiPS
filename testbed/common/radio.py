"""Radio geometry: AP grid layouts, neighbour sets and the log-distance RSSI estimate.

The RSSI estimate is the surrogate for 802.11k beacon reports / an RTLS:
every framework uses the same function, computed from STA positions and the propagation model.
"""
from __future__ import annotations

import math
from typing import Any

C = 299_792_458.0


def grid_shape(n_ap: int) -> tuple[int, int]:
    """(columns, rows) for the supported layouts."""
    shapes = {4: (2, 2), 6: (3, 2), 9: (3, 3)}
    if n_ap not in shapes:
        raise ValueError(f"unsupported n_ap={n_ap}; use 4, 6 or 9")
    return shapes[n_ap]


def channel_plan(n_ap: int) -> list[int]:
    if n_ap == 4:
        return [1, 6, 11, 1]          # ap1/ap4 diagonal co-channel pair
    if n_ap == 6:
        return [1, 6, 11, 1, 6, 11]   # 3 x 2 (vertical co-channel pairs)
    return [1, 6, 11, 6, 11, 1, 11, 1, 6]  # 3 x 3 Latin square


def ap_layout(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """AP name → {x, y, channel, col, row}. Grid starts at (spacing, spacing).

    For n_ap=4: ap1 (40,40) ch1, ap2 (80,40) ch6, ap3 (40,80) ch11,
    ap4 (80,80) ch1.
    """
    n_ap = int(cfg["n_ap"])
    sp = float(cfg["ap_spacing_m"])
    cols, rows = grid_shape(n_ap)
    chans = channel_plan(n_ap)
    out: dict[str, dict[str, Any]] = {}
    k = 0
    for r in range(rows):
        for c in range(cols):
            out[f"ap{k + 1}"] = {"x": sp * (c + 1), "y": sp * (r + 1), "channel": chans[k],
                                 "col": c, "row": r}
            k += 1
    return out


def area_size(cfg: dict[str, Any]) -> tuple[float, float]:
    """Area grows with the grid: (cols + 1) * spacing, min area_m."""
    cols, rows = grid_shape(int(cfg["n_ap"]))
    sp = float(cfg["ap_spacing_m"])
    a = float(cfg["area_m"])
    return max(a, (cols + 1) * sp), max(a, (rows + 1) * sp)


def neighbours(layout: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Grid-adjacent APs (shared edge, not diagonal). Used by the guardrail."""
    out: dict[str, list[str]] = {}
    for a, pa in layout.items():
        out[a] = sorted(
            (b for b, pb in layout.items()
             if b != a and abs(pa["col"] - pb["col"]) + abs(pa["row"] - pb["row"]) == 1),
            key=lambda s: int(s[2:]))
    return out


def path_loss_db(d_m: float, cfg: dict[str, Any]) -> float:
    """Log-distance path loss with d0 = 1 m free-space reference (Mininet-WiFi logDistance)."""
    d = max(d_m, 0.1)
    f = float(cfg["freq_ghz"]) * 1e9
    pl0 = 20.0 * math.log10(4.0 * math.pi * 1.0 * f / C)
    return pl0 + 10.0 * float(cfg["prop_exp"]) * math.log10(d / 1.0)


def est_rssi_dbm(tx_dbm: float, d_m: float, cfg: dict[str, Any]) -> float:
    g = float(cfg["antenna_gain_dbi"])
    return tx_dbm + 2 * g - float(cfg["prop_sL"]) - path_loss_db(d_m, cfg)


def distance(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


def nearest_ap(x: float, y: float, layout: dict[str, dict[str, Any]]) -> str:
    return min(layout, key=lambda a: distance(x, y, layout[a]["x"], layout[a]["y"]))
