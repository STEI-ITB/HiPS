#!/usr/bin/env python3
"""Run ONE emulated run end-to-end and append its summary row.

Usage::  sudo -E .venv/bin/python common/run_one.py --run-config output/experiment-2/_configs/<tag>.json
         .venv/bin/python common/run_one.py --run-config <cfg> --summarize-only   # re-run KPIs only

Steps: clean (mn -c, pkill) → quarantine a partial folder → ensure mobility trace → reserve
cloud budget (cloud backend only) → host monitor → topo.py (taskset cores.emu) → agents
(taskset cores.orch) → wait for EMU_DONE / watchdog → stop agents → clean → kpi.py →
upsert summary row(s) (one per sub-experiment label) → DONE marker.

Exit codes: 0 ok · 1 run failed (retry) · 2 fatal environment error · 75 rate-limited (retry later).
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import logging
import os
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
from common import runstate as RS  # noqa: E402

LOG = logging.getLogger("run_one")
EX_OK, EX_FAIL, EX_FATAL, EX_TEMPFAIL = 0, 1, 2, 75


def python_exe() -> str:
    return sys.executable


def chown_to_invoker(path: Path) -> None:
    """Give root-created outputs back to the sudo user so analysis runs without sudo."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if os.geteuid() == 0 and uid and gid and path.exists():
        subprocess.run(["chown", "-R", f"{uid}:{gid}", str(path)], check=False)


def start_pinned(cores: str, args: list[str], log: Path) -> subprocess.Popen[bytes]:
    cmd = (["taskset", "-c", cores] if cores and shutil.which("taskset") else []) + args
    LOG.info("start: %s", " ".join(cmd))
    return subprocess.Popen(cmd, stdout=open(log, "ab"), stderr=subprocess.STDOUT, start_new_session=True)


def stop_proc(p: subprocess.Popen[bytes] | None, grace: float = 10.0) -> None:
    if p is None or p.poll() is not None:
        return
    try:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=grace)
    except (subprocess.TimeoutExpired, ProcessLookupError, PermissionError):
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def summarize(cfg: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    from common.kpi import run_kpi

    summary = run_kpi(run_dir)
    hook = cfg.get("post_hook")
    if hook:
        spec = importlib.util.spec_from_file_location("post_hook", C.TESTBED / hook)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        summary = mod.post(run_dir, cfg, summary) or summary
    labels = cfg.get("sub_exps") or [cfg.get("sub_exp", "")]
    rows = [dict(summary, sub_exp=lab) for lab in labels]
    RS.upsert_rows(Path(cfg["summary_csv"]), rows)
    return summary


def compress_live(run_dir: Path) -> None:
    for f in list((run_dir / "irtt").glob("*.live.txt")) + [run_dir / "events_raw.csv", run_dir / "tool_log.jsonl"]:
        if f.exists():
            with open(f, "rb") as src, gzip.open(str(f) + ".gz", "wb") as dst:
                shutil.copyfileobj(src, dst)
            f.unlink()


def run(cfg: dict[str, Any], cfg_path: Path) -> int:
    from common.hostmon import HostMonitor
    from common.mobility import ensure_trace
    from common.topo import cleanup_mininet

    run_dir = Path(cfg["run_dir"])
    if run_dir.exists() and not (run_dir / RS.DONE).exists():
        RS.quarantine_incomplete(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    C.setup_logging(logfile=run_dir / "run_one.log")
    LOG.info("run %s (%s)", cfg["run_tag"], cfg["framework"])
    C.write_json(run_dir / "run_config.json", cfg)

    if cfg.get("needs_root", True) and os.geteuid() != 0:
        LOG.error("needs root: use sudo -E")
        return EX_FATAL
    if shutil.which("mn") is None:
        LOG.error("Mininet not installed (mn not found)")
        return EX_FATAL

    cleanup_mininet()
    ensure_trace(cfg)

    if cfg.get("llm", {}).get("backend") == "cloud":
        from common.campaign import llm_calls_estimate
        from common.llm import RateLimited, reserve_cloud_budget

        est = llm_calls_estimate(cfg)
        try:
            reserve_cloud_budget(cfg, est["calls"], est["tokens"], max_wait_s=6 * 3600)
        except RateLimited as exc:
            LOG.warning("%s", exc)
            return EX_TEMPFAIL

    stop = threading.Event()
    cores = cfg.get("cores", {})
    hm = HostMonitor({"orch": cores.get("orch", "0-1"), "emu": cores.get("emu", "2-7"),
                      "llm": cores.get("llm", "8-11")}, run_dir / "host.csv", stop)
    hm.start()
    py = python_exe()
    topo = start_pinned(cores.get("emu", ""), [py, str(C.COMMON / "topo.py"), "--run-config", str(cfg_path)],
                        run_dir / "topo.out")
    agents = None
    agent_entry = cfg.get("agent_entry")
    if agent_entry:
        agents = start_pinned(cores.get("orch", ""), [py, str(C.TESTBED / agent_entry), "--run-config", str(cfg_path)],
                              run_dir / "agents.out")
    elif cfg["framework"] != "B0_UNMANAGED":
        agents = start_pinned(cores.get("orch", ""), [py, str(C.COMMON / "agents" / "run_agents.py"),
                                                      "--framework", cfg["framework"], "--run-config", str(cfg_path)],
                              run_dir / "agents.out")
    budget_s = float(cfg["setup_timeout_s"]) + float(cfg["duration_s"]) + 180
    deadline = time.time() + budget_s
    rc_topo = None
    try:
        while time.time() < deadline:
            rc_topo = topo.poll()
            if rc_topo is not None:
                break
            time.sleep(1.0)
        else:
            LOG.error("watchdog: run exceeded %.0f s", budget_s)
    except KeyboardInterrupt:
        LOG.warning("interrupted")
    finally:
        stop_proc(topo)
        if agents is not None:
            if agent_entry and agents.poll() is None:
                try:
                    agents.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    pass
            stop_proc(agents)
        stop.set()
        hm.join(timeout=3)
        cleanup_mininet()
        chown_to_invoker(run_dir)

    if rc_topo == 2:
        return EX_FATAL
    if not (run_dir / "EMU_DONE").exists():
        LOG.error("emulation did not finish (rc=%s); see %s", rc_topo, run_dir / "topo.log")
        return EX_FAIL
    if (run_dir / "RATE_LIMITED").exists():
        LOG.warning("cloud calls failed / rate-limited: run will be retried later")
        return EX_TEMPFAIL
    compress_live(run_dir)
    summarize(cfg, run_dir)
    RS.mark_done(run_dir, {"run_tag": cfg["run_tag"]})
    chown_to_invoker(Path(cfg["exp_dir"]))
    return EX_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-config", required=True)
    ap.add_argument("--summarize-only", action="store_true", help="recompute KPIs/summary from existing raw data")
    a = ap.parse_args(argv)
    cfg_path = Path(a.run_config).resolve()
    cfg = C.read_run_config(cfg_path)
    if a.summarize_only:
        C.setup_logging()
        run_dir = Path(cfg["run_dir"])
        if not (run_dir / "run_meta.json").exists():
            LOG.error("no raw data in %s", run_dir)
            return EX_FAIL
        summarize(cfg, run_dir)
        RS.mark_done(run_dir, {"run_tag": cfg["run_tag"], "resummarized": True})
        return EX_OK
    try:
        return run(cfg, cfg_path)
    except Exception:  # noqa: BLE001
        LOG.exception("run failed")
        return EX_FAIL


if __name__ == "__main__":
    sys.exit(main())
