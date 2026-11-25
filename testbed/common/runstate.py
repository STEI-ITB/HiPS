"""Persistent run state: DONE markers, attempt counters, summary-CSV appends.

All writers take an exclusive ``fcntl`` lock so that a crashed or concurrent process can never
leave a half-written summary. Summary appends are idempotent: a row with the same
``(run_tag, sub_exp)`` replaces the previous one, which makes re-runs after a crash safe.
"""
from __future__ import annotations

import contextlib
import csv
import fcntl
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Iterator

LOG = logging.getLogger(__name__)

DONE = "DONE"
KEY_COLS = ("run_tag", "sub_exp")


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """Exclusive lock on ``<path>.lock``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _row_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("run_tag", "")), str(row.get("sub_exp", "") or "")


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def upsert_rows(path: Path, rows: list[dict[str, Any]], key_cols: tuple[str, ...] = KEY_COLS) -> None:
    """Insert or replace rows keyed by ``key_cols``; header is the union of all columns."""
    with locked(path):
        existing = read_csv_rows(path)
        new_keys = {tuple(str(r.get(k, "") or "") for k in key_cols) for r in rows}
        kept = [r for r in existing
                if tuple(str(r.get(k, "") or "") for k in key_cols) not in new_keys]
        allrows: list[dict[str, Any]] = kept + rows
        header: list[str] = []
        for r in allrows:
            for k in r:
                if k not in header:
                    header.append(k)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
            w.writeheader()
            for r in allrows:
                w.writerow({k: _fmt(r.get(k, "")) for k in header})
        os.replace(tmp, path)


def append_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    """Plain append for raw per-call CSVs (header written once)."""
    if not rows:
        return
    with locked(path):
        new = not path.exists() or path.stat().st_size == 0
        header = list(rows[0].keys())
        if not new:
            with open(path, newline="", encoding="utf-8") as fh:
                header = next(csv.reader(fh), header)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
            if new:
                w.writeheader()
            for r in rows:
                w.writerow({k: _fmt(r.get(k, "")) for k in header})


def delete_rows(path: Path, run_tag: str) -> None:
    """Remove every row of ``run_tag`` (used before re-running an incomplete unit)."""
    if not path.exists():
        return
    with locked(path):
        rows = read_csv_rows(path)
        keep = [r for r in rows if r.get("run_tag") != run_tag]
        if len(keep) == len(rows):
            return
        header = list(rows[0].keys()) if rows else []
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=header)
            w.writeheader()
            w.writerows(keep)
        os.replace(tmp, path)


def _fmt(v: Any) -> Any:
    if isinstance(v, float):
        if v != v:  # NaN
            return ""
        return f"{v:.6g}"
    if isinstance(v, (list, dict)):
        return json.dumps(v)
    return v


def summary_tags(path: Path) -> set[str]:
    return {r.get("run_tag", "") for r in read_csv_rows(path)}


def is_done(run_dir: Path, summary_csv: Path) -> bool:
    return (run_dir / DONE).exists() and run_dir.name in summary_tags(summary_csv)


def mark_done(run_dir: Path, info: dict[str, Any] | None = None) -> None:
    payload = {"finished": time.time(), **(info or {})}
    (run_dir / DONE).write_text(json.dumps(payload, default=str))


def state_dir(exp_out: Path) -> Path:
    d = exp_out / "_state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_attempts(exp_out: Path, tag: str) -> int:
    f = state_dir(exp_out) / "attempts.json"
    if not f.exists():
        return 0
    return int(json.loads(f.read_text() or "{}").get(tag, 0))


def bump_attempts(exp_out: Path, tag: str, delta: int = 1) -> int:
    f = state_dir(exp_out) / "attempts.json"
    with locked(f):
        data = json.loads(f.read_text() or "{}") if f.exists() else {}
        data[tag] = int(data.get(tag, 0)) + delta
        f.write_text(json.dumps(data, indent=1, sort_keys=True))
        return data[tag]


def reset_attempts(exp_out: Path, tag: str | None = None) -> None:
    f = state_dir(exp_out) / "attempts.json"
    if not f.exists():
        return
    with locked(f):
        data = json.loads(f.read_text() or "{}")
        if tag is None:
            data = {}
        else:
            data.pop(tag, None)
        f.write_text(json.dumps(data, indent=1, sort_keys=True))


def record_failure(exp_out: Path, tag: str, reason: str) -> None:
    append_rows(exp_out / "failed_runs.csv",
                [{"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "run_tag": tag, "reason": reason}])


def quarantine_incomplete(run_dir: Path) -> Path | None:
    """Move a partial run folder aside so the run restarts from a clean directory."""
    if not run_dir.exists():
        return None
    dest_root = run_dir.parent / "_incomplete"
    dest_root.mkdir(exist_ok=True)
    dest = dest_root / f"{run_dir.name}.{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.move(str(run_dir), str(dest))
    LOG.info("moved incomplete run %s -> %s", run_dir.name, dest)
    return dest
