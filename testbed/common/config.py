"""Run-configuration handling.

Merge order (later wins)::

    common/defaults.yaml  <  testbed.yaml  <  experiment-N/config.yaml ``fixed``  <  grid point

The merged, flat dictionary is the *run config*. Every script of the project receives the path
of a run-config JSON file and reads every run-dependent value from it.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import socket
from pathlib import Path
from typing import Any

import yaml

LOG = logging.getLogger(__name__)

TESTBED = Path(__file__).resolve().parent.parent
COMMON = TESTBED / "common"

FRAMEWORKS = ("B0_UNMANAGED", "B1_HEURISTIC", "B2_FLAT_AGENT", "P_HIER_AGENT")
FRAMEWORK_SHORT = {"B0_UNMANAGED": "B0", "B1_HEURISTIC": "B1", "B2_FLAT_AGENT": "B2", "P_HIER_AGENT": "P"}
LLM_FRAMEWORKS = ("B2_FLAT_AGENT", "P_HIER_AGENT")
SLICES = ("telemedicine", "video", "best_effort")


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``over`` into a copy of ``base``."""
    out = copy.deepcopy(base)
    for key, val in (over or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def host_overlay() -> Path:
    """``testbed.<short hostname>.yaml``: per-host overrides (e.g. ``cores``) on top of testbed.yaml."""
    return TESTBED / f"testbed.{socket.gethostname().split('.')[0]}.yaml"


def load_base(testbed_file: Path | None = None) -> dict[str, Any]:
    """defaults.yaml merged with testbed.yaml (path overridable via $OJSDN_TESTBED), then with this
    host's ``testbed.<hostname>.yaml`` if present (only when neither override is given)."""
    cfg = load_yaml(COMMON / "defaults.yaml")
    tb = testbed_file or Path(os.environ.get("OJSDN_TESTBED", TESTBED / "testbed.yaml"))
    if tb.exists():
        cfg = deep_merge(cfg, load_yaml(tb))
    else:
        LOG.warning("testbed file %s not found, using defaults only", tb)
    if testbed_file is None and "OJSDN_TESTBED" not in os.environ and host_overlay().exists():
        cfg = deep_merge(cfg, load_yaml(host_overlay()))
        cfg["testbed_overlay"] = host_overlay().name
    cfg["guardrail"] = normalise_onoff(cfg.get("guardrail", "on"))
    # run.sh --cloud exports OJSDN_LLM_BACKEND=cloud; it overrides testbed.yaml for that campaign
    if os.environ.get("OJSDN_LLM_BACKEND"):
        cfg.setdefault("llm", {})["backend"] = os.environ["OJSDN_LLM_BACKEND"]
    return cfg


def uses_llm(cfg: dict[str, Any]) -> bool:
    """True if the run sends planner/flat-agent calls to an LLM (or is the SLM benchmark)."""
    if str(cfg.get("entry", "")).endswith("slm_bench.py"):
        return True
    return cfg.get("framework") in LLM_FRAMEWORKS and cfg.get("planner_model") != "none_rule"


def normalise_onoff(val: Any) -> str:
    if isinstance(val, bool):
        return "on" if val else "off"
    return str(val).lower()


def load_intents() -> dict[str, Any]:
    with open(COMMON / "intents.json", encoding="utf-8") as fh:
        return json.load(fh)


def slice_counts(n_sta: int, fractions: dict[str, float]) -> dict[str, int]:
    """STA count per slice preserving proportions (6/9/15 for 30 STAs)."""
    tele = round(n_sta * fractions["telemedicine"])
    video = round(n_sta * fractions["video"])
    return {"telemedicine": tele, "video": video, "best_effort": n_sta - tele - video}


def slice_of_sta(n_sta: int, fractions: dict[str, float]) -> dict[str, str]:
    """Deterministic STA → slice map: sta1..sta6 tele, sta7..sta15 video, rest best effort."""
    counts = slice_counts(n_sta, fractions)
    out: dict[str, str] = {}
    i = 1
    for sl in SLICES:
        for _ in range(counts[sl]):
            out[f"sta{i}"] = sl
            i += 1
    return out


def sta_mac(i: int) -> str:
    # not 02:00:00:00:xx:00: that is mac80211_hwsim's default radio MAC (radio xx), so sta i would
    # collide with an AP radio and Mininet-WiFi leaves the duplicate out of wmediumd
    return f"02:5a:00:00:{i:02x}:00"


def sta_ip(i: int) -> str:
    return f"10.0.0.{i}"


def fmt_num(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def model_short(model: str) -> str:
    return model.replace(":", "-").replace("-instruct", "").replace("/", "_")


def run_tag(cfg: dict[str, Any], key_params: list[str]) -> str:
    """``exp<N>_<framework>_<key-param-values>_s<seed>``."""
    abbrev = {
        "speed_mps": "v", "hotspot_size": "h", "n_ap": "ap", "n_sta": "n", "planner_model": "m",
        "plan_interval_s": "pi", "subagent_mode": "sa", "guardrail": "g", "infer_hw": "hw",
        "telemetry_period_ms": "tp", "txpower_dbm": "tx", "prompt_kind": "pk", "tool": "t",
    }
    parts = [f"exp{cfg['exp']}", cfg["framework"]]
    for k in key_params:
        val = cfg[k]
        sval = model_short(val) if k == "planner_model" else fmt_num(val)
        parts.append(f"{abbrev.get(k, k)}{sval}")
    parts.append(f"s{int(cfg['seed']):02d}")
    if cfg.get("llm", {}).get("backend") == "cloud" and uses_llm(cfg):
        parts.append("cloud")   # cloud runs never collide with (or resume) local runs
    return "_".join(parts)


def effective_model(cfg: dict[str, Any], model: str) -> str:
    """Model name actually sent to the backend (cloud backend maps local names)."""
    llm = cfg.get("llm", {})
    if llm.get("backend") == "cloud":
        return llm.get("cloud_model_map", {}).get(model, model)
    return model


def trace_name(cfg: dict[str, Any]) -> str:
    """Trace file name: identical for all frameworks of the same (layout, speed, hotspot, seed)."""
    return (f"ap{cfg['n_ap']}_n{cfg['n_sta']}_v{fmt_num(float(cfg['speed_mps']))}"
            f"_h{cfg['hotspot_size']}_{cfg['hotspot_start_s']}-{cfg['hotspot_end_s']}"
            f"_d{cfg['duration_s']}_s{int(cfg['seed']):02d}.csv")


def output_dir(cfg: dict[str, Any]) -> Path:
    p = Path(cfg.get("paths", {}).get("output", "output"))
    return p if p.is_absolute() else TESTBED / p


def hotspot_size_for(n_sta: int) -> int:
    """Experiment 4: hotspot_size = ceil(n_sta / 3)."""
    return math.ceil(n_sta / 3)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True, default=str)
    os.replace(tmp, path)


def read_run_config(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def setup_logging(level: str | int = "INFO", logfile: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if logfile is not None:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(logfile))
    logging.basicConfig(level=os.environ.get("OJSDN_LOGLEVEL", level),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers, force=True)
