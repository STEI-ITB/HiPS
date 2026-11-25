#!/usr/bin/env python3
"""Experiment 1a — radio calibration: 1 AP, 1 static STA, distance sweep at one TX power.

One run unit = (txpower_dbm, rep). The STA is moved through ``distances_m`` inside one topology;
at each distance: settle → RSSI (STA view, ``iw link``; AP view, station dump) → 30 s UDP
saturating downlink → 30 s TCP downlink → retry ratio from the AP's tx counters.
No HTB cap here: this measures the raw emulated radio.

Rows → ``output/experiment-1/1a_calibration.csv``. Run via ``sudo -E ./experiment-1/run.sh 1a``.
``calib_robust: true`` (sub-experiment 1e) adds readiness and stale-position checks (``measure_robust``).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import config as C  # noqa: E402
from common import runstate as RS  # noqa: E402
from common.telemetry import station_dump  # noqa: E402

LOG = logging.getLogger("calib")
AP_POS = (10.0, 10.0)


def iperf_json(node: Any, args: str, timeout: float) -> dict[str, Any]:
    out = node.cmd(f"timeout {int(timeout)} iperf3 -c 10.0.0.100 -p 5201 -R -J {args}")
    try:
        return json.loads(out[out.find("{"):])
    except (ValueError, json.JSONDecodeError):
        return {}


def sta_rssi(node: Any) -> float:
    m = re.search(r"signal:\s*(-?\d+)", node.cmd("iw dev sta1-wlan0 link"))
    return float(m.group(1)) if m else float("nan")


def data_path_ok(nodes: dict[str, Any], timeout_s: float) -> bool:
    """``calib_robust``: STA reaches the server (ping) and the iperf3 server listens, within ``timeout_s``."""
    sta, h1 = nodes["stas"]["sta1"], nodes["h1"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if " 0% packet loss" in sta.cmd("ping -c 2 -W 1 -i 0.3 10.0.0.100") and ":5201" in h1.cmd("ss -ltn"):
            return True
        time.sleep(1.0)
    return False


def measure_robust(cfg: dict[str, Any], nodes: dict[str, Any], d: float, run_dir: Path,
                   prev_rssi: float | None) -> dict[str, Any]:
    """``calib_robust`` (Exp 1e): as ``measure``, but (1) re-applies the position if the STA's RSSI did not
    change from the previous distance (a stale position), (2) checks the data path (ping + iperf3 server)
    before measuring, and (3) repeats an iperf3 test that returned no JSON once. Each row records the
    attempts, so nothing is silently discarded."""
    sta = nodes["stas"]["sta1"]
    reposition = 0
    for _ in range(1 + int(cfg.get("calib_reposition_max", 2))):
        sta.setPosition(f"{AP_POS[0] + d},{AP_POS[1]},0")
        time.sleep(float(cfg["settle_s"]))
        rssi = sta_rssi(sta)
        if prev_rssi is None or rssi != prev_rssi:
            break
        reposition += 1
    dump = station_dump("ap1-wlan1")
    assoc = C.sta_mac(1) in dump
    st0 = dump.get(C.sta_mac(1), {})
    path_ok = assoc and data_path_ok(nodes, float(cfg.get("calib_ready_timeout_s", 20)))
    row: dict[str, Any] = {"distance_m": d, "assoc_ok": int(assoc), "rssi_dbm": rssi,
                           "rssi_ap_dbm": st0.get("signal_dbm", float("nan")), "path_ok": int(path_ok),
                           "repositions": reposition, "rssi_stale": int(prev_rssi is not None and rssi == prev_rssi)}
    if not path_ok:
        row.update({"thr_udp_mbps": 0.0, "thr_tcp_mbps": 0.0, "retry_pct": float("nan"), "udp_loss_pct": 100.0,
                    "iperf_attempts": 0})
        return row
    attempts = 0
    udp: dict[str, Any] = {}
    for _ in range(2):
        attempts += 1
        udp = iperf_json(sta, f"-u -b {cfg['udp_rate_mbps']}M -l 1400 -t {cfg['udp_s']}", cfg["udp_s"] + 15)
        if udp.get("end"):
            break
        time.sleep(2.0)
    s = udp.get("end", {}).get("sum", {})
    row["thr_udp_mbps"] = float(s.get("bits_per_second", 0.0)) / 1e6
    row["udp_loss_pct"] = float(s.get("lost_percent", float("nan")))
    tcp: dict[str, Any] = {}
    for _ in range(2):
        attempts += 1
        tcp = iperf_json(sta, f"-t {cfg['tcp_s']}", cfg["tcp_s"] + 15)
        if tcp.get("end"):
            break
        time.sleep(2.0)
    row["thr_tcp_mbps"] = float(tcp.get("end", {}).get("sum_received", {}).get("bits_per_second", 0.0)) / 1e6
    row["iperf_attempts"] = attempts
    st1 = station_dump("ap1-wlan1").get(C.sta_mac(1), {})
    dp = st1.get("tx_packets", 0) - st0.get("tx_packets", 0)
    dr = st1.get("tx_retries", 0) - st0.get("tx_retries", 0)
    row["retry_pct"] = 100.0 * dr / dp if dp > 0 else float("nan")
    return row


def measure(cfg: dict[str, Any], nodes: dict[str, Any], d: float, run_dir: Path) -> dict[str, Any]:
    sta = nodes["stas"]["sta1"]
    sta.setPosition(f"{AP_POS[0] + d},{AP_POS[1]},0")
    time.sleep(float(cfg["settle_s"]))
    dump = station_dump("ap1-wlan1")
    assoc = C.sta_mac(1) in dump
    st0 = dump.get(C.sta_mac(1), {})
    row: dict[str, Any] = {"distance_m": d, "assoc_ok": int(assoc), "rssi_dbm": sta_rssi(sta),
                           "rssi_ap_dbm": st0.get("signal_dbm", float("nan"))}
    if not assoc:
        row.update({"thr_udp_mbps": 0.0, "thr_tcp_mbps": 0.0, "retry_pct": float("nan"), "udp_loss_pct": 100.0})
        return row
    udp = iperf_json(sta, f"-u -b {cfg['udp_rate_mbps']}M -l 1400 -t {cfg['udp_s']}", cfg["udp_s"] + 15)
    s = udp.get("end", {}).get("sum", {})
    row["thr_udp_mbps"] = float(s.get("bits_per_second", 0.0)) / 1e6
    row["udp_loss_pct"] = float(s.get("lost_percent", float("nan")))
    tcp = iperf_json(sta, f"-t {cfg['tcp_s']}", cfg["tcp_s"] + 15)
    row["thr_tcp_mbps"] = float(tcp.get("end", {}).get("sum_received", {}).get("bits_per_second", 0.0)) / 1e6
    st1 = station_dump("ap1-wlan1").get(C.sta_mac(1), {})
    dp = st1.get("tx_packets", 0) - st0.get("tx_packets", 0)
    dr = st1.get("tx_retries", 0) - st0.get("tx_retries", 0)
    row["retry_pct"] = 100.0 * dr / dp if dp > 0 else float("nan")
    return row


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-config", required=True)
    a = ap.parse_args(argv)
    cfg = C.read_run_config(a.run_config)
    run_dir = Path(cfg["run_dir"])
    if run_dir.exists() and not (run_dir / RS.DONE).exists():
        RS.quarantine_incomplete(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    C.setup_logging(logfile=run_dir / "calib.log")
    if os.geteuid() != 0:
        LOG.error("calib.py needs root")
        return 2
    try:
        from common.topo import build_network, cleanup_mininet
    except ImportError as exc:
        LOG.error("mn_wifi missing: %s", exc)
        return 2
    raw = Path(cfg["raw_csv"])
    RS.delete_rows(raw, cfg["run_tag"])
    cleanup_mininet()
    layout = {"ap1": {"x": AP_POS[0], "y": AP_POS[1], "channel": 1}}
    net = None
    pcap = None
    rows: list[dict[str, Any]] = []
    try:
        net, nodes = build_network(cfg, layout, {"sta1": (AP_POS[0] + cfg["distances_m"][0], AP_POS[1])})
        nodes["h1"].cmd("iperf3 -s -p 5201 -D")
        if cfg.get("capture_pcap"):
            subprocess.run(["ip", "link", "set", "hwsim0", "up"], check=False)
            pcap = subprocess.Popen(["tcpdump", "-i", "hwsim0", "-w", str(run_dir.parent / f"1a_{cfg['run_tag']}.pcap")],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(5)
        robust = bool(cfg.get("calib_robust", False))
        if robust and not data_path_ok(nodes, float(cfg.get("calib_ready_timeout_s", 20))):
            LOG.warning("data path not ready after build; rebuilding the network once")
            net.stop()
            cleanup_mininet()
            net, nodes = build_network(cfg, layout, {"sta1": (AP_POS[0] + cfg["distances_m"][0], AP_POS[1])})
            nodes["h1"].cmd("iperf3 -s -p 5201 -D")
            time.sleep(5)
            if not data_path_ok(nodes, float(cfg.get("calib_ready_timeout_s", 20))):
                LOG.error("data path still not ready after a rebuild")
        prev_rssi: float | None = None
        for d in cfg["distances_m"]:
            if robust:
                r = measure_robust(cfg, nodes, float(d), run_dir, prev_rssi)
                prev_rssi = r["rssi_dbm"]
            else:
                r = measure(cfg, nodes, float(d), run_dir)
            r.update({"data_status": "REAL", "run_tag": cfg["run_tag"], "txpower_dbm": cfg["txpower_dbm"],
                      "rep": cfg["seed"]})
            LOG.info("d=%s m tx=%s dBm rssi=%.0f udp=%.1f tcp=%.1f", d, cfg["txpower_dbm"], r["rssi_dbm"],
                     r["thr_udp_mbps"], r["thr_tcp_mbps"])
            rows.append(r)
    except Exception:  # noqa: BLE001
        LOG.exception("calibration failed")
        return 1
    finally:
        if pcap:
            pcap.terminate()
        if net is not None:
            net.stop()
        cleanup_mininet()
    order = ["data_status", "run_tag", "txpower_dbm", "rep", "distance_m", "rssi_dbm", "rssi_ap_dbm",
             "thr_udp_mbps", "udp_loss_pct", "thr_tcp_mbps", "retry_pct", "assoc_ok"]
    if cfg.get("calib_robust", False):
        order += ["path_ok", "repositions", "rssi_stale", "iperf_attempts"]
    RS.append_rows(raw, [{k: r.get(k) for k in order} for r in rows])
    import numpy as np

    ok = [r for r in rows if r["assoc_ok"] and r["rssi_dbm"] == r["rssi_dbm"]]
    slope = float("nan")
    if len(ok) >= 3:
        slope = float(np.polyfit(np.log10([r["distance_m"] for r in ok]), [r["rssi_dbm"] for r in ok], 1)[0])
    usable = [r["distance_m"] for r in rows if r["thr_udp_mbps"] >= 1.0]
    RS.upsert_rows(Path(cfg["summary_csv"]), [{
        "data_status": "REAL", "exp": 1, "sub_exp": cfg.get("sub_exp") or (cfg.get("sub_exps") or ["1a"])[0], "run_tag": cfg["run_tag"], "framework": cfg["framework"],
        "txpower_dbm": cfg["txpower_dbm"], "seed": cfg["seed"], "rssi_slope_db_per_decade": slope,
        "max_usable_distance_m": max(usable) if usable else 0.0,
        "thr_udp_mbps_near": rows[0]["thr_udp_mbps"] if rows else float("nan"),
        "assoc_ok_pct": 100.0 * sum(r["assoc_ok"] for r in rows) / len(rows) if rows else float("nan")}])
    RS.mark_done(run_dir, {"run_tag": cfg["run_tag"]})
    from common.run_one import chown_to_invoker

    chown_to_invoker(Path(cfg["exp_dir"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
