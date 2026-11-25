#!/usr/bin/env python3
"""Emulation process: Mininet-WiFi topology + tc + event loggers + tool server + telemetry +
mobility replay + traffic. Runs as root, pinned to the ``cores.emu`` set by ``run_one.py``.

Timeline (all wall-clock, written to ``run_meta.json``)::

    build → tc → hostapd checks → event loggers → tool server → wait all STAs associated
    → write READY {t0} (t0 = now + agent_lead_s) → at t0: telemetry, mobility, traffic
    → t0 + duration_s: stop → events.csv, telemetry.parquet, run_meta.json → EMU_DONE → net.stop()

Usage::  sudo .venv/bin/python common/topo.py --run-config output/experiment-2/_configs/<tag>.json
         sudo .venv/bin/python common/topo.py --run-config <cfg> --cli   # interactive debugging

Lines marked VERIFY depend on the Mininet-WiFi / hostapd version.
"""
from __future__ import annotations

import argparse
import csv
import glob
import logging
import os
import platform
import random
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import config as C  # noqa: E402
from common import radio  # noqa: E402
from common.mobility import Replayer, ensure_trace, load_trace  # noqa: E402

LOG = logging.getLogger("topo")
AGENT_LEAD_S = 3.0
HOSTAPD_EXTRA = ("bss_transition=1", "rrm_neighbor_report=1")


def hostapd_config(cfg: dict[str, Any], ap_name: str) -> str:
    """Comma-separated hostapd lines for addAccessPoint(config=...).

    With ``config`` Mininet-WiFi writes ONLY these lines (plus wpa=2 and ctrl_interface): it skips
    its own WPA2 and 802.11r blocks, so the full WPA2-PSK + FT set it would generate is repeated
    here (r0kh/r1kh are not needed with ft_psk_generate_local), followed by HOSTAPD_EXTRA."""
    if "," in cfg["passphrase"]:
        raise ValueError("passphrase must not contain ',' (Mininet-WiFi splits config on commas)")
    lines = ["auth_algs=1", "wpa_key_mgmt=FT-PSK WPA-PSK", "rsn_pairwise=CCMP",
             f"wpa_passphrase={cfg['passphrase']}", f"mobility_domain={cfg['mobility_domain']}",
             f"nas_identifier={ap_name}.example.com", "ft_over_ds=1", "ft_psk_generate_local=1",
             "pmk_r1_push=1", "rsn_preauth=1", *HOSTAPD_EXTRA]
    return ",".join(lines)


# ============================================================================ network build
def build_network(cfg: dict[str, Any], layout: dict[str, dict[str, Any]],
                  sta_pos: dict[str, tuple[float, float]]) -> tuple[Any, dict[str, Any]]:
    """Create and start the Mininet-WiFi network. Returns (net, nodes)."""
    from mininet.link import TCLink
    from mininet.node import OVSKernelSwitch
    from mn_wifi.link import wmediumd
    from mn_wifi.net import Mininet_wifi
    from mn_wifi.node import OVSKernelAP
    from mn_wifi.wmediumdConnector import interference

    from common.traffic import SERVER_IP

    # ipBase: Mininet's default 10.0.0.0/8 would cover the LAN (10.90/21, 10.91/21, 10.10/24);
    # every node gets an explicit 10.0.0.x/24 address anyway (install/check-net.sh)
    net = Mininet_wifi(accessPoint=OVSKernelAP, link=wmediumd, wmediumd_mode=interference,
                       ipBase="10.0.0.0/24")
    h1 = net.addHost("h1", ip=f"{SERVER_IP}/24")
    s1 = net.addSwitch("s1", cls=OVSKernelSwitch, failMode="standalone")
    aps = {}
    for name, p in layout.items():
        aps[name] = net.addAccessPoint(
            name, ssid=cfg["ssid"], mode=cfg["mode"], channel=str(p["channel"]),
            position=f"{p['x']},{p['y']},0", txpower=int(cfg.get("txpower_dbm", cfg["ap_txpower_dbm"])),
            failMode="standalone", encrypt="wpa2", passwd=cfg["passphrase"],
            ieee80211r="yes", mobility_domain=cfg["mobility_domain"],
            config=hostapd_config(cfg, name))
    stas = {}
    jit = int(cfg.get("bgscan_long_jitter_s", 0) or 0)
    for name, (x, y) in sta_pos.items():
        i = int(name[3:])
        # per-STA long interval (desynchronises the STAs' background scans); seeded, so identical
        # for every framework of the same seed
        l_int = int(cfg["bgscan_long_s"]) + (random.Random(int(cfg["seed"]) * 1000 + i).randint(-jit, jit) if jit else 0)
        kw: dict[str, Any] = dict(ip=f"{C.sta_ip(i)}/24", mac=C.sta_mac(i), position=f"{x},{y},0",
                                  encrypt="wpa2", passwd=cfg["passphrase"], ieee80211r="yes",
                                  bgscan_threshold=int(cfg["bgscan_threshold_dbm"]),
                                  s_inverval=int(cfg["bgscan_short_s"]),     # sic: Mininet-WiFi spelling, VERIFY
                                  l_interval=l_int, bgscan_module="simple")
        stas[name] = net.addStation(name, **kw)
    net.setPropagationModel(model="logDistance", exp=float(cfg["prop_exp"]))
    if hasattr(net, "configureNodes"):
        net.configureNodes()
    else:  # older Mininet-WiFi
        net.configureWifiNodes()
    # with config= Mininet-WiFi leaves these AP attributes unset, and the stations' wpa_supplicant
    # network block (written in net.build()) copies them: key_mgmt=None would make it refuse to start
    for ap in aps.values():
        for intf in ap.wintfs.values():
            intf.wpa_key_mgmt, intf.rsn_pairwise = "FT-PSK WPA-PSK", "CCMP"
    for ap in aps.values():
        net.addLink(ap, s1, cls=TCLink, bw=1000, delay="1ms")
    net.addLink(h1, s1)
    net.build()
    s1.start([])
    for ap in aps.values():
        ap.start([])
    return net, {"h1": h1, "s1": s1, "aps": aps, "stas": stas}


def ap_wlan(ap_name: str) -> str:
    return f"{ap_name}-wlan1"


def iface_mac(intf: str) -> str:
    try:
        return Path(f"/sys/class/net/{intf}/address").read_text().strip().lower()
    except OSError:
        return ""


# ============================================================================ hostapd checks
def hostapd_has_extra(intf: str, ctrl: str) -> bool:
    """True if hostapd for ``intf`` accepts BTM (bss_transition=1 active)."""
    r = subprocess.run(["hostapd_cli", "-p", ctrl, "-i", intf, "get", "bss_transition"],
                       capture_output=True, text=True, timeout=3)
    if r.stdout.strip() == "1":
        return True
    # older hostapd: GET of this key not supported -> inspect the config file written by Mininet-WiFi
    for f in _apconf_files(intf):
        if "bss_transition=1" in Path(f).read_text():
            return True
    return False


def _apconf_files(intf: str) -> list[str]:
    pats = [f"*{intf}*.apconf", f"/tmp/*{intf}*.apconf", f"/tmp/*{intf}*.conf", f"*{intf}.conf"]
    out: list[str] = []
    for p in pats:
        out += glob.glob(p)
    return out


def patch_hostapd(intf: str, ctrl: str) -> bool:
    """Fallback: append extra lines to that config file, restart."""
    files = _apconf_files(intf)
    if not files:
        LOG.error("no hostapd config file found for %s; cannot enable BTM", intf)
        return False
    conf = files[0]
    text = Path(conf).read_text()
    add = [ln for ln in HOSTAPD_EXTRA if ln not in text]
    if add:
        with open(conf, "a") as fh:
            fh.write("\n" + "\n".join(add) + "\n")
    subprocess.run(["pkill", "-f", f"hostapd.*{conf}"], check=False)
    time.sleep(0.5)
    subprocess.run(["hostapd", "-B", conf], check=False)
    time.sleep(1.0)
    ok = hostapd_has_extra(intf, ctrl)
    LOG.warning("patched hostapd config %s for %s (now BTM=%s)", conf, intf, ok)
    return ok


# ============================================================================ tc shaping
def setup_tc(cfg: dict[str, Any], intf: str, sta_slice: dict[str, str], intents: dict[str, Any]) -> None:
    """HTB root (ap_capacity_mbps) + 3 slice classes + fq_codel leaves + per-STA filters (§4.2)."""
    cap = float(cfg["ap_capacity_mbps"])
    shares = cfg["slice_shares_b0"]
    cmds = [f"tc qdisc replace dev {intf} root handle 1: htb default 30",
            f"tc class add dev {intf} parent 1: classid 1:1 htb rate {cap:g}mbit ceil {cap:g}mbit"]
    for sl, info in intents["slices"].items():
        rate = cap * float(shares[sl])
        cmds.append(f"tc class add dev {intf} parent 1:1 classid {info['htb_classid']} htb "
                    f"rate {rate:g}mbit ceil {cap:g}mbit prio {info['htb_prio']}")
        cmds.append(f"tc qdisc add dev {intf} parent {info['htb_classid']} fq_codel")
    for sta, sl in sta_slice.items():
        ip = C.sta_ip(int(sta[3:]))
        cmds.append(f"tc filter add dev {intf} parent 1: protocol ip prio 1 u32 match ip dst {ip}/32 "
                    f"flowid {intents['slices'][sl]['htb_classid']}")
    for c in cmds:
        r = subprocess.run(c.split(), capture_output=True, text=True)
        if r.returncode != 0:
            LOG.warning("tc failed: %s -> %s", c, r.stderr.strip())


# ============================================================================ event loggers
class HostapdMonitor(threading.Thread):
    """Attached ``hostapd_cli`` per AP; logs AP-STA-CONNECTED/DISCONNECTED and BSS-TM-RESP."""

    def __init__(self, ap: str, intf: str, ctrl: str, mac2sta: dict[str, str], out: Path, stop: threading.Event,
                 raw: Path | None = None):
        super().__init__(name=f"hapd-{ap}", daemon=True)
        self.ap, self.intf, self.ctrl, self.mac2sta, self.out, self.stop_ev = ap, intf, ctrl, mac2sta, out, stop
        self.raw = raw   # sta_debug_log: every hostapd_cli line, time-stamped
        self.proc: subprocess.Popen[str] | None = None

    def run(self) -> None:
        # VERIFY: interactive hostapd_cli prints unsolicited events when stdout is a pipe
        self.proc = subprocess.Popen(["stdbuf", "-oL", "hostapd_cli", "-p", self.ctrl, "-i", self.intf],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1)
        assert self.proc.stdout is not None
        rawfh = open(self.raw, "a", buffering=1) if self.raw else None   # line-buffered: the thread is a daemon
        with open(self.out, "a") as fh:
            for line in self.proc.stdout:
                if self.stop_ev.is_set():
                    break
                ts = time.time()
                if rawfh:
                    rawfh.write(f"{ts:.6f} {line.rstrip()}\n")
                for key in ("AP-STA-CONNECTED", "AP-STA-DISCONNECTED", "BSS-TM-RESP"):
                    if key in line:
                        parts = line.split(key, 1)[1].split()
                        mac = parts[0].lower() if parts else ""
                        status = ""
                        for p in parts[1:]:
                            if p.startswith("status_code="):
                                status = p.split("=", 1)[1]
                        fh.write(f"{ts:.6f},{self.mac2sta.get(mac, mac)},{key},{self.ap},{status}\n")
                        fh.flush()

    def close(self) -> None:
        """Terminate AND reap hostapd_cli: an unreaped zombie still matches ``pgrep -f hostapd``,
        and Mininet-WiFi's cleanup (net.stop, cleanup_wifi) loops until that pgrep is empty."""
        if not self.proc:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=3)


# ============================================================================ emulation
class Emulation:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.run_dir = Path(cfg["run_dir"])
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.intents = C.load_intents()
        self.layout = radio.ap_layout(cfg)
        self.sta_slice = C.slice_of_sta(int(cfg["n_sta"]), cfg["slice_fractions"])
        self.stop_ev = threading.Event()
        self.mn_lock = threading.Lock()
        self.pos: dict[str, tuple[float, float]] = {}
        self.events_extra: list[dict[str, Any]] = []
        self.ev_lock = threading.Lock()
        self.ctrl = cfg.get("paths", {}).get("hostapd_ctrl", "/var/run/hostapd")
        self.meta: dict[str, Any] = {"run_tag": cfg["run_tag"], "framework": cfg["framework"],
                                     "host": platform.node(), "uname": " ".join(platform.uname())}

    # ------------------------------------------------------------- callbacks for tools / mobility
    def set_position(self, sta: str, x: float, y: float) -> None:
        with self.mn_lock:
            self.nodes["stas"][sta].setPosition(f"{x:.2f},{y:.2f},0")
        self.pos[sta] = (x, y)

    def set_txpower(self, ap: str, dbm: float) -> None:
        with self.mn_lock:
            self.nodes["aps"][ap].setTxPower(int(round(dbm)), intf=ap_wlan(ap))

    def get_txpower(self, ap: str) -> float | None:
        """Power Mininet-WiFi applied (and pushed to wmediumd); `iw` misreports it on hwsim/kernel 7.0."""
        with self.mn_lock:
            p = self.nodes["aps"][ap].wintfs[0].txpower
        return float(p) if p is not None else None

    def log_event(self, ev: dict[str, Any]) -> None:
        with self.ev_lock:
            self.events_extra.append(ev)

    # ------------------------------------------------------------- phases
    def build(self) -> None:
        trace, sha = ensure_trace(self.cfg)
        self.meta.update({"trace_file": str(trace), "trace_sha256": sha})
        times, names, P = load_trace(trace)
        start = {n: (float(P[0, j, 0]), float(P[0, j, 1])) for j, n in enumerate(names)}
        self.pos = dict(start)
        self.net, self.nodes = build_network(self.cfg, self.layout, start)
        self.aps = {a: {"intf": ap_wlan(a), "bssid": iface_mac(ap_wlan(a)), "channel": p["channel"],
                        "x": p["x"], "y": p["y"]} for a, p in self.layout.items()}
        self.stas = {s: {"mac": C.sta_mac(int(s[3:])), "slice": sl, "ip": C.sta_ip(int(s[3:]))}
                     for s, sl in self.sta_slice.items()}
        self.mac2sta = {v["mac"]: k for k, v in self.stas.items()}
        self.meta["aps"] = self.aps
        for a in self.aps:
            setup_tc(self.cfg, ap_wlan(a), self.sta_slice, self.intents)
        btm = {}
        for a in self.aps:
            ok = hostapd_has_extra(ap_wlan(a), self.ctrl) or patch_hostapd(ap_wlan(a), self.ctrl)
            btm[a] = ok
        self.meta["hostapd_btm"] = btm

    def start_loggers(self) -> None:
        ev_raw = self.run_dir / "events_raw.csv"
        ev_raw.write_text("ts,ifname,event,bssid\n")
        script = C.COMMON / "wpa_event.sh"
        for s, node in self.nodes["stas"].items():
            intf = f"{s}-wlan0"
            node.cmd(f"OJSDN_EVENTS={ev_raw} wpa_cli -i {intf} -a {script} -B")
        self.ev_ap = self.run_dir / "events_ap.csv"
        self.ev_ap.write_text("ts,sta,event,ap,status\n")
        dbg = bool(self.cfg.get("sta_debug_log", False))
        dbg_dir = self.run_dir / "debug_log"
        if dbg:
            # Experiment 6a diagnostic: an attached wpa_cli per STA logs scan / auth / reassociation events with
            # bash's $EPOCHREALTIME (no fork per line). The sleep keeps wpa_cli's stdin open for the run
            # and bounds the logger's lifetime; cleanup_mininet() also kills wpa_cli.
            dbg_dir.mkdir(exist_ok=True)
            life = int(self.cfg["duration_s"]) + int(self.cfg["warmup_s"]) + int(self.cfg["setup_timeout_s"]) + 120
            lvl = int(self.cfg.get("sta_debug_level", 2))
            for s, node in self.nodes["stas"].items():
                intf = f"{s}-wlan0"
                node.cmd(f"( (printf 'level {lvl}\\n'; sleep {life}) | stdbuf -oL wpa_cli -i {intf} "
                         f"| while IFS= read -r l; do echo \"$EPOCHREALTIME $l\"; done >> {dbg_dir / (s + '.log')} ) "
                         f"> /dev/null 2>&1 &")
        self.hmon = [HostapdMonitor(a, ap_wlan(a), self.ctrl, self.mac2sta, self.ev_ap, self.stop_ev,
                                    raw=(dbg_dir / f"hostapd_{a}.log") if dbg else None)
                     for a in self.aps]
        for m in self.hmon:
            m.start()

    def wait_associated(self, timeout: float) -> int:
        from common.telemetry import station_dump

        deadline = time.time() + timeout
        n = 0
        while time.time() < deadline:
            seen: set[str] = set()
            for a in self.aps.values():
                seen |= set(station_dump(a["intf"]))
            n = len(seen & set(self.mac2sta))
            if n == len(self.stas):
                return n
            time.sleep(1.0)
        return n

    def run(self, cli: bool = False) -> int:
        from common.telemetry import Telemetry
        from common.tool_server import ToolServer
        from common.tools import Tools
        from common.traffic import LiveKPI, Traffic

        cfg = self.cfg
        self.meta["t_build_start"] = time.time()
        self.meta.update(configure_medium(cfg, self.run_dir))
        self.build()
        self.meta["medium_cores"] = pin_medium(cfg.get("cores", {}).get("medium", ""))
        self.start_loggers()
        if cli:
            from mn_wifi.cli import CLI
            CLI(self.net)
            return 0
        n_assoc = self.wait_associated(float(cfg["setup_timeout_s"]))
        self.meta["associated_at_ready"] = n_assoc
        if n_assoc < len(self.stas):
            LOG.error("only %d/%d STAs associated before timeout", n_assoc, len(self.stas))
            if n_assoc < 0.9 * len(self.stas):
                return 1
        t0 = time.time() + AGENT_LEAD_S
        self.meta["t0"] = t0
        drops0, mcpu0, mwall0 = medium_netlink_drops(), medium_cpu_s(), time.time()
        mstat0 = medium_stats(self.run_dir)
        telem = Telemetry({a: v["intf"] for a, v in self.aps.items()}, self.mac2sta,
                          int(cfg["telemetry_period_ms"]), self.run_dir / "telemetry.parquet", self.stop_ev, t0)
        traffic = Traffic(cfg, self.intents, self.run_dir, self.sta_slice)
        traffic.start_servers(self.nodes["h1"], len(self.stas))
        live = LiveKPI(self.run_dir, self.sta_slice, traffic.launch, self.stop_ev)
        tools = Tools(cfg, self.aps, self.stas, telem, live, self.set_txpower, lambda: dict(self.pos),
                      self.log_event, get_txpower_fn=self.get_txpower)
        server = ToolServer(tools, int(cfg["tool_server_port"]), self.run_dir / "tool_log.jsonl", self.stop_ev)
        server.start()
        server.ready.wait(5)
        telem.start()
        live.start()
        C.write_json(self.run_dir / "READY", {"t0": t0, "associated": n_assoc})
        repl = Replayer(Path(self.meta["trace_file"]), self.set_position, t0, self.stop_ev)
        repl.start()
        traffic.start_clients(self.nodes["stas"], t0, self.stop_ev)
        t_end = t0 + float(cfg["duration_s"])
        while time.time() < t_end and not self.stop_ev.is_set():
            if traffic.error:
                break
            time.sleep(0.5)
        if traffic.error:
            # without traffic the run measures nothing: fail it (run_one retries) instead of writing NaN KPIs
            LOG.error("traffic launch failed (%s): aborting the run", traffic.error)
            traffic.stop()
            self.stop_ev.set()
            return 1
        self.meta["t_end"] = time.time()
        drops1, mcpu1 = medium_netlink_drops(), medium_cpu_s()
        mstat1 = medium_stats(self.run_dir)
        if mstat0 is not None and mstat1 is not None:
            self.meta["medium_frames"] = mstat1[0] - mstat0[0]
            self.meta["medium_maxq_drops"] = mstat1[1] - mstat0[1]
            self.meta["medium_maxq_drop_pct"] = (100.0 * self.meta["medium_maxq_drops"] / self.meta["medium_frames"]
                                                 if self.meta["medium_frames"] else 0.0)
        self.meta["medium_cpu_pct"] = (100.0 * (mcpu1 - mcpu0) / (time.time() - mwall0)
                                       if mcpu0 is not None and mcpu1 is not None else None)
        self.meta["medium_nl_drops"] = drops1 - drops0 if drops0 is not None and drops1 is not None else None
        if self.meta["medium_nl_drops"]:
            LOG.warning("wmediumd netlink dropped %d frames during the run: emulation lost traffic "
                        "(medium overloaded)", self.meta["medium_nl_drops"])
        traffic.stop()
        time.sleep(2.0)  # let iperf3/irtt write their final JSON
        self.stop_ev.set()
        telem.join(timeout=5)
        telem.write()
        self.meta["telemetry_stats"] = telem.stats()
        self.meta["mobility_late_samples"] = repl.late
        self.meta["iperf_json_stream"] = traffic.json_stream
        for m in self.hmon:
            m.close()
        self.write_events()
        C.write_json(self.run_dir / "run_meta.json", {**self.meta, "run_config": cfg})
        (self.run_dir / "EMU_DONE").write_text(str(time.time()))
        return 0

    def write_events(self) -> None:
        """Merge wpa_cli, hostapd and tool events into events.csv."""
        bssid2ap = {v["bssid"]: a for a, v in self.aps.items()}
        rows: list[dict[str, Any]] = []
        last_ap: dict[str, str] = {}
        raw = self.run_dir / "events_raw.csv"
        wpa = []
        if raw.exists():
            with open(raw) as fh:
                for r in csv.DictReader(fh):
                    try:
                        wpa.append((float(r["ts"]), r["ifname"].split("-")[0], r["event"],
                                    r.get("bssid", "").split("=")[-1].strip().lower()))
                    except (ValueError, KeyError):
                        continue
        ap_ev: list[tuple[float, str, str, str, str]] = []
        if self.ev_ap.exists():
            with open(self.ev_ap) as fh:
                for r in csv.DictReader(fh):
                    try:
                        ap_ev.append((float(r["ts"]), r["sta"], r["event"], r["ap"], r.get("status", "")))
                    except (ValueError, KeyError):
                        continue
        assoc = sorted(e for e in ap_ev if e[2] in ("AP-STA-CONNECTED", "AP-STA-DISCONNECTED"))
        if assoc:
            # CONNECTED/DISCONNECTED from hostapd: `wpa_cli -a` runs its action script only when the
            # connection state or network id changes, so a roam inside the same network (FT/BTM
            # reassociation) never reaches events_raw.csv. hostapd logs every (re)association.
            cur: dict[str, str] = {}
            for ts, sta, ev, ap, _ in assoc:
                if ev == "AP-STA-CONNECTED":
                    rows.append({"ts": ts, "sta": sta, "event": "CONNECTED", "ap_from": last_ap.get(sta, ""),
                                 "ap_to": ap, "status": ""})
                    last_ap[sta] = cur[sta] = ap
                elif cur.get(sta) == ap:  # the old AP dropping a STA that already roamed is no outage
                    rows.append({"ts": ts, "sta": sta, "event": "DISCONNECTED", "ap_from": ap,
                                 "ap_to": "", "status": ""})
                    cur[sta] = ""
        else:  # no hostapd events: fall back to the wpa_cli action events
            for ts, sta, ev, bssid in sorted(wpa):
                ap = bssid2ap.get(bssid, "")
                if ev == "CONNECTED":
                    rows.append({"ts": ts, "sta": sta, "event": "CONNECTED", "ap_from": last_ap.get(sta, ""),
                                 "ap_to": ap, "status": ""})
                    last_ap[sta] = ap
                elif ev == "DISCONNECTED":
                    rows.append({"ts": ts, "sta": sta, "event": "DISCONNECTED", "ap_from": last_ap.get(sta, ""),
                                 "ap_to": "", "status": ""})
        for ts, sta, ev, ap, status in ap_ev:
            if ev == "BSS-TM-RESP":
                rows.append({"ts": ts, "sta": sta, "event": "BTM_RESP", "ap_from": ap, "ap_to": "", "status": status})
        rows += [{k: e.get(k, "") for k in ("ts", "sta", "event", "ap_from", "ap_to", "status")}
                 for e in self.events_extra]
        rows.sort(key=lambda r: float(r["ts"]))
        with open(self.run_dir / "events.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["ts", "sta", "event", "ap_from", "ap_to", "status"])
            w.writeheader()
            w.writerows(rows)

    def stop(self) -> None:
        self.stop_ev.set()
        for m in getattr(self, "hmon", []):  # also on the failure path; close() is idempotent
            m.close()
        try:
            self.net.stop()
        except Exception:  # noqa: BLE001
            LOG.exception("net.stop failed")


def medium_netlink_drops() -> int | None:
    """Frames the kernel dropped on wmediumd's hwsim netlink socket(s) so far (None if not found).

    A full receive buffer makes the kernel discard hwsim frames silently, which shows up as
    load-independent loss and EAPOL timeouts when wmediumd cannot keep up with the offered frames;
    every run records this counter (``medium_nl_drops`` in run_meta.json); a run with drops lost
    frames inside the emulator. (A larger buffer does not help: wmediumd then falls seconds behind.)"""
    pids = subprocess.run(["pgrep", "-x", "wmediumd"], capture_output=True, text=True).stdout.split()
    inodes: set[str] = set()
    for pid in pids:
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                tgt = os.readlink(f"/proc/{pid}/fd/{fd}")
                if tgt.startswith("socket:["):
                    inodes.add(tgt[8:-1])
        except OSError:
            continue
    if not inodes:
        return None
    total, found = 0, False
    with open("/proc/net/netlink") as fh:
        next(fh)
        for line in fh:
            f = line.split()
            if len(f) >= 10 and f[9] in inodes:
                total += int(f[8])
                found = True
    return total if found else None


MEDIUM_PATCH_MARK = b"OJSDN_WMD_MAXQ_US"   # install/wmediumd_ojsdn.py; also carries the other OJSDN_WMD_* patches


def configure_medium(cfg: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Environment for the ojsdn-patched wmediumd (install/wmediumd_ojsdn.py), read when Mininet-WiFi
    starts it. Unset variables leave wmediumd's stock behaviour.

    * medium_rcvbuf_kb: hwsim netlink receive buffer (libnl's 32 KiB drops frames on bursts)
    * medium_per_channel: contention only among frames on the same channel (stock: one medium for all)
    * medium_max_queue_ms: a frame waiting longer for the medium is dropped as a failed transmission
      (stock: unbounded queue; once offered airtime exceeds real time the emulation stalls for good)
    * per-second counters to <run_dir>/medium_stats.txt (see medium_stats)"""
    env = {}
    if cfg.get("medium_rcvbuf_kb"):
        env["OJSDN_WMD_RCVBUF"] = str(int(cfg["medium_rcvbuf_kb"]) * 1024)
    if cfg.get("medium_per_channel"):
        env["OJSDN_WMD_PER_CHANNEL"] = "1"
    if cfg.get("medium_max_queue_ms"):
        env["OJSDN_WMD_MAXQ_US"] = str(int(float(cfg["medium_max_queue_ms"]) * 1000))
    env["OJSDN_WMD_STATS"] = str(run_dir / "medium_stats.txt")
    for k in ("OJSDN_WMD_RCVBUF", "OJSDN_WMD_PER_CHANNEL", "OJSDN_WMD_MAXQ_US", "OJSDN_WMD_STATS"):
        os.environ.pop(k, None)
    os.environ.update(env)
    wmd = shutil.which("wmediumd")
    patched = bool(wmd) and MEDIUM_PATCH_MARK in Path(wmd).read_bytes()
    if not patched:
        LOG.error("%s is not the ojsdn-patched wmediumd (run install/patch-wmediumd.sh): medium_* settings "
                  "are ignored and the run uses stock wmediumd", wmd)
    return {"medium_rcvbuf_kb": cfg.get("medium_rcvbuf_kb"), "medium_per_channel": bool(cfg.get("medium_per_channel")),
            "medium_max_queue_ms": cfg.get("medium_max_queue_ms"), "medium_patched": patched}


def medium_stats(run_dir: Path) -> tuple[int, int] | None:
    """(frames queued, frames dropped by the bounded queue) so far, from the patched wmediumd's stats file."""
    try:
        a, b = (run_dir / "medium_stats.txt").read_text().split()[:2]
        return int(a), int(b)
    except (OSError, ValueError):
        return None


def pin_medium(cores: str) -> str:
    """Pin wmediumd (all threads) to ``cores`` (testbed.yaml ``cores.medium``; "" = leave it in the
    emu set). It is single-threaded and carries every simulated frame, so a dedicated physical core
    with an idle SMT sibling spares it migrations and cache misses."""
    if not cores:
        return ""
    for pid in subprocess.run(["pgrep", "-x", "wmediumd"], capture_output=True, text=True).stdout.split():
        r = subprocess.run(["taskset", "-a", "-pc", cores, pid], capture_output=True, text=True)
        if r.returncode != 0:
            LOG.warning("could not pin wmediumd %s to %s: %s", pid, cores, r.stderr.strip())
            return ""
    return cores


def medium_cpu_s() -> float | None:
    """CPU seconds (user + system) wmediumd has used so far; it is single-threaded, so over a
    window ``Δcpu / Δwall`` near 1.0 means the medium simulation is the bottleneck."""
    total, found = 0.0, False
    for pid in subprocess.run(["pgrep", "-x", "wmediumd"], capture_output=True, text=True).stdout.split():
        try:
            f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        total += (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")
        found = True
    return total if found else None


def cleanup_mininet() -> None:
    """Mininet + Mininet-WiFi cleanup and leftover kill. Safe to call repeatedly.

    Both cleanups are called directly: the ``mn`` on PATH may be plain Mininet's, whose ``-c`` leaves
    hostapd, wmediumd, the mac80211_hwsim radios and the *.apconf/*.staconf files behind, and the
    next run then cannot associate. The pkill patterns match only the emulation's own processes
    (``-x`` / the ``-B`` daemons Mininet-WiFi starts), not NetworkManager's wpa_supplicant or any
    command line that merely mentions these names."""
    for name in ("iperf3", "irtt", "wpa_cli"):
        subprocess.run(["pkill", "-x", name], check=False)
    subprocess.run([sys.executable, "-c", "from mininet.clean import cleanup; from mn_wifi.clean import cleanup_wifi; "
                    "cleanup(); cleanup_wifi()"], cwd=str(C.TESTBED), capture_output=True, check=False, timeout=180)
    for pat in ("^hostapd -B", "^wpa_supplicant -B", "^wmediumd "):
        subprocess.run(["pkill", "-f", pat], check=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-config", required=True)
    ap.add_argument("--cli", action="store_true", help="drop into the Mininet-WiFi CLI after build")
    a = ap.parse_args(argv)
    cfg = C.read_run_config(a.run_config)
    C.setup_logging(logfile=Path(cfg["run_dir"]) / "topo.log")
    if os.geteuid() != 0:
        LOG.error("topo.py must run as root")
        return 2
    try:
        import mn_wifi  # noqa: F401
    except ImportError:
        LOG.error("mn_wifi not importable: run install/install-emu-host.sh")
        return 2
    emu = Emulation(cfg)
    signal.signal(signal.SIGTERM, lambda *_: emu.stop_ev.set())
    rc = 1
    try:
        rc = emu.run(cli=a.cli)
    except Exception:  # noqa: BLE001
        LOG.exception("emulation failed")
        rc = 1
    finally:
        emu.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
