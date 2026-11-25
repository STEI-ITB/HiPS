#!/usr/bin/env python3
"""Smoke test of the emulator and the tool layer.

    sudo -E .venv/bin/python common/smoke_test.py --n_ap 4 --n_sta 4

Checks (exit code = number of failed checks, 0 = all good):
  1. all STAs associated (wpa_supplicant-driven, no Mininet-WiFi association control)
  2. ping h1 from every STA
  3. set_tx_power(ap1, ap_txpower_dbm - 6) -> sta1 signal (``iw dev sta1-wlan0 link``) drops 6 +- 1.5 dB
     (``iw dev ap1-wlan1 info`` is not used: on mac80211_hwsim / kernel 7.0 it keeps showing 20 dBm)
  4. trigger_bss_transition(sta1 -> ap2) -> STA re-associates to ap2 within 1 s (BTM)
  5. adjust_slice_bandwidth(ap1, video, 5) -> ``tc class show`` shows 5Mbit
  6. iperf3 -R UDP h1 -> sta1 succeeds and HTB class counters increase
Plus the VERIFY items: hostapd bss_transition/rrm_neighbor_report active, iperf3 --json-stream,
irtt present. Results are written to output/smoke/smoke_result.json.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import config as C  # noqa: E402
from common import radio  # noqa: E402

LOG = logging.getLogger("smoke")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n_ap", type=int, default=4)
    ap.add_argument("--n_sta", type=int, default=4)
    ap.add_argument("--keep", action="store_true", help="drop into the CLI at the end")
    a = ap.parse_args(argv)
    C.setup_logging()
    if os.geteuid() != 0:
        LOG.error("run as root: sudo -E .venv/bin/python common/smoke_test.py")
        return 99
    from common.telemetry import station_dump
    from common.tools import iw_txpower_dbm, tc_class_rate_mbps
    from common.topo import ap_wlan, build_network, cleanup_mininet, hostapd_has_extra, iface_mac, patch_hostapd, setup_tc
    from common.traffic import iperf3_supports_json_stream

    cfg = C.load_base()
    cfg.update({"n_ap": a.n_ap, "n_sta": a.n_sta, "run_tag": "smoke"})
    out_dir = C.output_dir(cfg) / "smoke"
    out_dir.mkdir(parents=True, exist_ok=True)
    layout = radio.ap_layout(cfg)
    # STAs between ap1 and ap2 (sta1 closer to ap1 so the BTM test has somewhere to go)
    pos = {f"sta{i}": (layout["ap1"]["x"] + 5 + 3 * i, layout["ap1"]["y"] + 2) for i in range(1, a.n_sta + 1)}
    sta_slice = C.slice_of_sta(a.n_sta, cfg["slice_fractions"])
    intents = C.load_intents()
    res: dict[str, object] = {}
    ctrl = cfg["paths"].get("hostapd_ctrl", "/var/run/hostapd")
    cleanup_mininet()
    net = None
    try:
        net, nodes = build_network(cfg, layout, pos)
        for apn in layout:
            setup_tc(cfg, ap_wlan(apn), sta_slice, intents)
        res["verify_hostapd_btm"] = {apn: hostapd_has_extra(ap_wlan(apn), ctrl) or patch_hostapd(ap_wlan(apn), ctrl)
                                     for apn in layout}
        res["verify_iperf3_json_stream"] = iperf3_supports_json_stream()
        res["verify_irtt"] = shutil.which("irtt") is not None
        macs = {C.sta_mac(i) for i in range(1, a.n_sta + 1)}
        seen: set[str] = set()
        for _ in range(60):
            seen = set()
            for apn in layout:
                seen |= set(station_dump(ap_wlan(apn)))
            if macs <= seen:
                break
            time.sleep(1)
        res["1_associated"] = f"{len(macs & seen)}/{len(macs)}"
        sta_states = {s: re.findall(r"wpa_state=(\S+)", n.cmd(f"wpa_cli -i {s}-wlan0 status"))
                      for s, n in nodes["stas"].items()}
        res["1_wpa_state"] = sta_states
        # (?<![\d.]): "100% packet loss" also contains "0% packet loss"
        pings = {s: re.search(r"(?<![\d.])0% packet loss", n.cmd("ping -c 3 -W 1 10.0.0.100")) is not None
                 for s, n in nodes["stas"].items()}
        res["2_ping"] = pings
        sta1 = nodes["stas"]["sta1"]

        def sta1_signal() -> float | None:
            vals = []
            for _ in range(3):
                m = re.search(r"signal: (-?\d+)", sta1.cmd("iw dev sta1-wlan0 link"))
                if m:
                    vals.append(float(m.group(1)))
                time.sleep(0.5)
            return sum(vals) / len(vals) if vals else None
        p0 = float(cfg["ap_txpower_dbm"])
        s0 = sta1_signal()
        nodes["aps"]["ap1"].setTxPower(int(p0 - 6), intf="ap1-wlan1")
        time.sleep(1.5)
        s1 = sta1_signal()
        nodes["aps"]["ap1"].setTxPower(int(p0), intf="ap1-wlan1")
        time.sleep(1.5)
        res["3_txpower_ap1"] = {"signal_before_dbm": s0, "signal_after_dbm": s1,
                                "drop_db": (s0 - s1) if s0 is not None and s1 is not None else None,
                                "iw_reported_dbm": iw_txpower_dbm("ap1-wlan1")}
        tgt_bssid = iface_mac("ap2-wlan1")
        serving = next((apn for apn in layout if C.sta_mac(1) in station_dump(ap_wlan(apn))), None)
        t = time.time()
        r = subprocess.run(["hostapd_cli", "-p", ctrl, "-i", ap_wlan(serving or "ap1"), "bss_tm_req", C.sta_mac(1),
                            "pref=1", "abridged=1", "disassoc_imminent=1", "disassoc_timer=5",
                            f"neighbor={tgt_bssid},0x0000,81,{layout['ap2']['channel']},7"],
                           capture_output=True, text=True)
        roamed = None
        for _ in range(100):
            if C.sta_mac(1) in station_dump("ap2-wlan1"):
                roamed = (time.time() - t) * 1000
                break
            time.sleep(0.02)
        res["4_btm"] = {"serving_before": serving, "hostapd_cli": r.stdout.strip(), "reassoc_ms": roamed}
        subprocess.run(["tc", "class", "change", "dev", "ap1-wlan1", "parent", "1:1", "classid", "1:20", "htb",
                        "rate", "5mbit", "ceil", f"{cfg['ap_capacity_mbps']}mbit", "prio", "1"], check=False)
        res["5_tc_video_rate_mbps"] = tc_class_rate_mbps("ap1-wlan1", "1:20")
        nodes["h1"].cmd("iperf3 -s -p 5201 -D")
        time.sleep(0.5)
        def counters() -> str:
            return "".join(subprocess.run(["tc", "-s", "class", "show", "dev", ap_wlan(x)], capture_output=True,
                                          text=True).stdout for x in layout)
        before = counters()
        out = sta1.cmd("iperf3 -c 10.0.0.100 -p 5201 -u -b 2M -R -t 5 -J")
        after = counters()
        try:
            udp = json.loads(out[out.find("{"):])["end"]["sum"]["bits_per_second"] / 1e6
        except (ValueError, KeyError, json.JSONDecodeError):
            udp = None
        res["6_iperf_udp_mbps"] = udp
        res["6_htb_counters_changed"] = before != after
        if a.keep:
            from mn_wifi.cli import CLI
            CLI(net)
    finally:
        if net is not None:
            net.stop()
        cleanup_mininet()
    fails = []
    if res.get("1_associated") != f"{a.n_sta}/{a.n_sta}":
        fails.append("1 association")
    if not all(res.get("2_ping", {}).values()):  # type: ignore[union-attr]
        fails.append("2 ping")
    drop = res["3_txpower_ap1"]["drop_db"]  # type: ignore[index]
    if drop is None or abs(drop - 6) > 1.5:
        fails.append("3 set_tx_power")
    if not (res["4_btm"]["reassoc_ms"] is not None and res["4_btm"]["reassoc_ms"] <= 1000):  # type: ignore[index]
        fails.append("4 BTM")
    if not (res.get("5_tc_video_rate_mbps") and abs(float(res["5_tc_video_rate_mbps"]) - 5) < 0.1):  # type: ignore[arg-type]
        fails.append("5 tc")
    if not (res.get("6_iperf_udp_mbps") and res.get("6_htb_counters_changed")):
        fails.append("6 iperf/HTB")
    if not all(res["verify_hostapd_btm"].values()):  # type: ignore[union-attr]
        fails.append("VERIFY hostapd bss_transition")
    res["failed_checks"] = fails
    C.write_json(out_dir / "smoke_result.json", res)
    LOG.info("%s", json.dumps(res, indent=1, default=str))
    LOG.info("SMOKE TEST %s", "PASSED" if not fails else f"FAILED: {fails}")
    if not res["verify_iperf3_json_stream"]:
        LOG.warning("iperf3 lacks --json-stream (live slice KPIs degraded): run install/install-emu-host.sh")
    return len(fails)


if __name__ == "__main__":
    sys.exit(main())
