"""Host monitor: 1 s per-core-set CPU and RAM sampler → ``host.csv``."""
from __future__ import annotations

import csv
import threading
import time
from pathlib import Path


def parse_cores(spec: str) -> list[int]:
    """'0-1,8-11' → [0, 1, 8, 9, 10, 11]."""
    out: list[int] = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


class HostMonitor(threading.Thread):
    def __init__(self, cores: dict[str, str], out: Path, stop: threading.Event, period_s: float = 1.0):
        super().__init__(name="hostmon", daemon=True)
        self.sets = {k: parse_cores(v) for k, v in cores.items()}
        self.out = out
        self.stop_ev = stop
        self.period = period_s

    def run(self) -> None:
        import psutil

        psutil.cpu_percent(percpu=True)
        ncpu = psutil.cpu_count() or 1
        with open(self.out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["ts"] + [f"cpu_{k}" for k in self.sets] + ["cpu_all", "ram_used_gb", "ram_total_gb", "load1"])
            while not self.stop_ev.wait(self.period):
                per = psutil.cpu_percent(percpu=True)
                row: list[object] = [f"{time.time():.3f}"]
                for cores in self.sets.values():
                    vals = [per[c] for c in cores if c < ncpu]
                    row.append(f"{sum(vals) / len(vals):.1f}" if vals else "")
                vm = psutil.virtual_memory()
                row += [f"{sum(per) / len(per):.1f}", f"{(vm.total - vm.available) / 2**30:.3f}",
                        f"{vm.total / 2**30:.3f}", f"{psutil.getloadavg()[0]:.2f}"]
                w.writerow(row)
                fh.flush()
