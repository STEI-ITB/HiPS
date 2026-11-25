"""LLM access for planner, flat agent, slm sub-agents and the SLM benchmark.

Two backends, selected by ``llm.backend`` in ``testbed.yaml``:

* ``local`` — the local (or LAN) Ollama server: ``temperature=0``, ``seed``, ``num_thread``,
  JSON-schema ``format``, ``keep_alive=-1``.
* ``cloud`` — Ollama Cloud (``https://ollama.com``, API key in ``$OLLAMA_API_KEY``). Local model
  names are mapped to cloud models via ``llm.cloud_model_map``. ``num_thread``/``keep_alive`` are
  not sent. Every call is recorded in a persistent, file-locked **ledger** that enforces the
  hourly/daily/weekly request caps and the daily token cap of ``llm.cloud_budget``.

The slm sub-agents (``subagent_mode=slm``) stay on the local server unless
``llm.subagent_backend: cloud`` is set — they are many tiny calls and would burn the cloud quota.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

EX_TEMPFAIL = 75  # process exit code: retry later (rate limit / cloud quota)


class RateLimited(RuntimeError):
    """Cloud quota exhausted (HTTP 429 or local ledger cap)."""


@dataclass
class LLMResult:
    """One chat call. Latencies in ms, token counts from Ollama's response."""

    ok: bool
    content: str = ""
    parsed: Any = None
    model: str = ""
    backend: str = "local"
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    output_tokens: int = 0
    prompt_eval_ms: float = 0.0
    eval_ms: float = 0.0
    load_ms: float = 0.0
    error: str = ""
    rate_limited: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def gen_tok_per_s(self) -> float:
        return self.output_tokens / (self.eval_ms / 1000.0) if self.eval_ms > 0 else float("nan")

    @property
    def prompt_tok_per_s(self) -> float:
        return self.prompt_tokens / (self.prompt_eval_ms / 1000.0) if self.prompt_eval_ms > 0 else float("nan")

    def to_log(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("parsed", None)
        d["content"] = self.content[:4000]
        return d


# ----------------------------------------------------------------------------- cloud ledger
class CloudLedger:
    """Append-only JSONL ledger of cloud calls shared by every process on the machine."""

    def __init__(self, budget: dict[str, Any]):
        self.budget = budget
        # one ledger per testbed folder, shared by root runs and user runs (sudo changes $HOME)
        from common.config import TESTBED

        p = os.environ.get("OJSDN_CLOUD_LEDGER") or budget.get("ledger") or str(TESTBED / "output" / "_cloud" / "ledger.jsonl")
        self.path = Path(os.path.expanduser(p))
        if not self.path.is_absolute():
            self.path = TESTBED / self.path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _entries(self, since: float) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("ts", 0) >= since:
                    out.append(e)
        return out

    def usage(self, now: float | None = None) -> dict[str, float]:
        now = now or time.time()
        week = self._entries(now - 7 * 86400)
        day = [e for e in week if e["ts"] >= now - 86400]
        hour = [e for e in day if e["ts"] >= now - 3600]
        cnt = lambda es: sum(1 for e in es if e.get("kind") == "call")  # noqa: E731
        res = lambda es: sum(e.get("n", 0) for e in es if e.get("kind") == "reserve")  # noqa: E731
        return {
            "hour_calls": cnt(hour), "day_calls": cnt(day), "week_calls": cnt(week),
            "hour_reserved": res(hour), "day_reserved": res(day),
            "day_tokens": sum(e.get("tokens", 0) for e in day if e.get("kind") == "call"),
        }

    def record(self, entry: dict[str, Any]) -> None:
        entry = {"ts": time.time(), **entry}
        with open(self.path, "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write(json.dumps(entry) + "\n")
            fcntl.flock(fh, fcntl.LOCK_UN)

    def check_call(self) -> None:
        """Raise RateLimited when a hard cap is already reached (no partial run burn)."""
        u = self.usage()
        b = self.budget
        if (u["hour_calls"] >= b.get("requests_per_hour", 10**9)
                or u["day_calls"] >= b.get("requests_per_day", 10**9)
                or u["week_calls"] >= b.get("requests_per_week", 10**9)
                or u["day_tokens"] >= b.get("tokens_per_day", 10**12)):
            raise RateLimited(f"local cloud budget cap reached: {u}")

    def wait_seconds_for(self, calls: float, tokens: float) -> float:
        """Seconds to wait before a run needing ``calls``/``tokens`` fits the budget (0 = now).

        The hourly window is evaluated with the run's calls spread over its duration, so only
        the daily/weekly caps and the current hour's backlog matter.
        """
        u = self.usage()
        b = self.budget
        if u["week_calls"] + calls > b.get("requests_per_week", 10**9):
            return 6 * 3600.0
        if (u["day_calls"] + calls > b.get("requests_per_day", 10**9)
                or u["day_tokens"] + tokens > b.get("tokens_per_day", 10**12)):
            return 3600.0
        if u["hour_calls"] + min(calls, 60) > b.get("requests_per_hour", 10**9):
            return 600.0
        return 0.0


# ----------------------------------------------------------------------------- client
class LLMClient:
    """Thin wrapper over ``ollama.Client`` that measures, logs and enforces the cloud budget."""

    def __init__(self, cfg: dict[str, Any], model: str, role: str = "planner"):
        import ollama  # local import: kpi/process-result do not need it

        self._ollama = ollama
        llm = cfg.get("llm", {})
        backend = llm.get("backend", "local")
        if role == "subagent":
            backend = llm.get("subagent_backend", "local")
        self.backend = backend
        self.seed = int(cfg.get("seed", 1))
        self.num_predict = int(cfg.get("num_predict", 512))
        self.timeout = float(llm.get("timeout_s", 120))
        self.think = llm.get("cloud_think")
        self.local_think: Any = None
        self.ledger: CloudLedger | None = None
        if backend == "cloud":
            key = os.environ.get("OLLAMA_API_KEY", "")
            if not key:
                raise RuntimeError("llm.backend=cloud but OLLAMA_API_KEY is not set (sudo -E keeps it)")
            self.model = llm.get("cloud_model_map", {}).get(model, model)
            self.client = ollama.Client(host=llm.get("cloud_host", "https://ollama.com"),
                                        headers={"Authorization": f"Bearer {key}"},
                                        timeout=self.timeout)
            self.ledger = CloudLedger(llm.get("cloud_budget", {}))
            self.options: dict[str, Any] = {"temperature": 0, "seed": self.seed}
            self.keep_alive = None
        else:
            self.model = model
            host = os.environ.get("OLLAMA_HOST_OVERRIDE") or llm.get("host", "http://127.0.0.1:11434")
            self.options = {"temperature": 0, "seed": self.seed, "num_thread": int(llm.get("num_thread", 4))}
            if cfg.get("infer_hw") == "gpu":  # GPU Ollama host (llm.host when gpu_host is empty)
                host = llm.get("gpu_host") or host
            elif llm.get("cpu_num_gpu0"):  # CPU point on a GPU server (Exp 5e): no layers offloaded
                self.options["num_gpu"] = 0
            self.client = ollama.Client(host=host, timeout=self.timeout)
            self.keep_alive = llm.get("keep_alive", -1)
            # thinking models (qwen3.5): off, otherwise num_predict is spent on reasoning tokens
            self.local_think = llm.get("think")

    def chat(self, system: str, user: str, schema: dict[str, Any] | None,
             num_predict: int | None = None, history: list[dict[str, str]] | None = None) -> LLMResult:
        """One non-streaming chat call with structured output. Never raises (except RateLimited)."""
        msgs = [{"role": "system", "content": system}] + (history or []) + [{"role": "user", "content": user}]
        opts = dict(self.options)
        opts["num_predict"] = int(num_predict or self.num_predict)
        kwargs: dict[str, Any] = {"model": self.model, "messages": msgs, "options": opts, "stream": False}
        if schema is not None:
            kwargs["format"] = schema
        if self.keep_alive is not None:
            kwargs["keep_alive"] = self.keep_alive
        if self.backend != "cloud" and self.local_think is not None:
            kwargs["think"] = self.local_think
        if self.backend == "cloud":
            assert self.ledger is not None
            try:
                self.ledger.check_call()
            except RateLimited as exc:
                return LLMResult(ok=False, model=self.model, backend=self.backend, error=str(exc), rate_limited=True)
            if self.think and "gpt-oss" in self.model:
                kwargs["think"] = self.think
                opts["num_predict"] = opts["num_predict"] + 512  # room for reasoning tokens
        t0 = time.perf_counter()
        try:
            resp = self.client.chat(**kwargs)
        except self._ollama.ResponseError as exc:  # HTTP error from server
            lat = (time.perf_counter() - t0) * 1000.0
            rl = getattr(exc, "status_code", 0) == 429
            if self.ledger:
                self.ledger.record({"kind": "error", "model": self.model, "status": getattr(exc, "status_code", -1)})
            res = LLMResult(ok=False, model=self.model, backend=self.backend, latency_ms=lat,
                            error=f"{getattr(exc, 'status_code', '')} {exc}", rate_limited=rl)
            LOG.warning("LLM error (%s): %s", self.model, res.error)
            return res
        except Exception as exc:  # timeout, connection refused, …
            lat = (time.perf_counter() - t0) * 1000.0
            LOG.warning("LLM call failed (%s): %s", self.model, exc)
            return LLMResult(ok=False, model=self.model, backend=self.backend, latency_ms=lat, error=str(exc))
        lat = (time.perf_counter() - t0) * 1000.0
        content = (resp.message.content or "") if resp.message else ""
        res = LLMResult(
            ok=True, content=content, model=self.model, backend=self.backend, latency_ms=lat,
            prompt_tokens=int(resp.prompt_eval_count or 0), output_tokens=int(resp.eval_count or 0),
            prompt_eval_ms=(resp.prompt_eval_duration or 0) / 1e6, eval_ms=(resp.eval_duration or 0) / 1e6,
            load_ms=(resp.load_duration or 0) / 1e6)
        res.extra["net_ms"] = max(0.0, lat - res.prompt_eval_ms - res.eval_ms - res.load_ms)
        if self.ledger:
            self.ledger.record({"kind": "call", "model": self.model,
                                "tokens": res.prompt_tokens + res.output_tokens})
        try:
            res.parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            res.ok = False
            res.error = f"json: {exc}"
        return res

    def warmup(self) -> LLMResult:
        """Load the model (local) / check reachability (cloud); excluded from metrics."""
        return self.chat("You are a JSON service.", "Return ok=true.",
                         {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                         num_predict=16)


def reserve_cloud_budget(cfg: dict[str, Any], calls: float, tokens: float, max_wait_s: float = 7 * 86400) -> None:
    """Block until a run's expected cloud usage fits the budget, then record a reservation."""
    llm = cfg.get("llm", {})
    if llm.get("backend") != "cloud" or calls <= 0:
        return
    ledger = CloudLedger(llm.get("cloud_budget", {}))
    waited = 0.0
    while True:
        w = ledger.wait_seconds_for(calls, tokens)
        if w <= 0:
            break
        if waited >= max_wait_s:
            raise RateLimited("cloud budget did not free up in time")
        LOG.warning("cloud budget: run needs ~%d calls/%d tokens; usage %s; sleeping %.0f s",
                    calls, tokens, ledger.usage(), w)
        time.sleep(w)
        waited += w
    ledger.record({"kind": "reserve", "n": calls, "tokens_est": tokens, "run_tag": cfg.get("run_tag")})
