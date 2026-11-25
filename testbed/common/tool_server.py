"""JSON-RPC 2.0 tool server over ZeroMQ (``tcp://127.0.0.1:<tool_server_port>``) and its client.

Server: a ROUTER socket served by one I/O thread plus a worker pool, so that a slow, verified
actuation (a BTM steer waits up to 2 s for re-association) never blocks other agents' calls.
Clients use plain REQ sockets (one per agent thread) with a timeout and lazy-pirate reconnect.

Request::  {"jsonrpc":"2.0","id":7,"method":"set_tx_power","params":{"ap_id":"ap1","dbm":8},"caller":"exec-ap1"}
Response:: {"jsonrpc":"2.0","id":7,"result":{"ok":true,"verified":true,"latency_ms":21.3,...}}
Error::    {"jsonrpc":"2.0","id":7,"error":{"code":-32601,"message":"unknown method"}}

Every call is appended to ``tool_log.jsonl`` in the run folder (server view, wall-clock times).
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import zmq

LOG = logging.getLogger("tool_server")


class ToolServer(threading.Thread):
    def __init__(self, tools: Any, port: int, log_file: Path, stop: threading.Event, workers: int = 8):
        super().__init__(name="tool-server", daemon=True)
        self.tools = tools
        self.port = port
        self.log_file = log_file
        self.stop_ev = stop
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tool")
        self.replies: queue.Queue[tuple[list[bytes], dict[str, Any]]] = queue.Queue()
        self.log_lock = threading.Lock()
        self.ready = threading.Event()

    def _handle(self, envelope: list[bytes], req: dict[str, Any]) -> None:
        from common.tools import dispatch

        rid = req.get("id")
        method = req.get("method", "")
        t_wall = time.time()
        t0 = time.perf_counter()
        try:
            result = dispatch(self.tools, method, req.get("params", {}) or {})
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "result": result}
        except KeyError:
            reply = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}}
        except TypeError as exc:
            reply = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": str(exc)}}
        except Exception as exc:  # noqa: BLE001 - report every tool failure to the caller
            LOG.exception("tool %s failed", method)
            reply = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": str(exc)}}
        dt = (time.perf_counter() - t0) * 1000
        if method not in ("get_snapshot", "get_topology"):
            entry = {"ts": t_wall, "caller": req.get("caller", ""), "method": method,
                     "params": req.get("params", {}), "server_ms": dt,
                     "ok": bool(reply.get("result", {}).get("ok")) if "result" in reply else False,
                     "verified": bool(reply.get("result", {}).get("verified")) if "result" in reply else False,
                     "error": reply.get("error", {}).get("message") if "error" in reply else
                     reply["result"].get("error")}
            with self.log_lock, open(self.log_file, "a") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        self.replies.put((envelope, reply))

    def run(self) -> None:
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.ROUTER)
        sock.setsockopt(zmq.LINGER, 0)
        sock.bind(f"tcp://127.0.0.1:{self.port}")
        self.ready.set()
        LOG.info("tool server on tcp://127.0.0.1:%d", self.port)
        poller = zmq.Poller()
        poller.register(sock, zmq.POLLIN)
        while not self.stop_ev.is_set():
            events = dict(poller.poll(5))
            if sock in events:
                frames = sock.recv_multipart()
                envelope, payload = frames[:-1], frames[-1]
                try:
                    req = json.loads(payload)
                except json.JSONDecodeError:
                    self.replies.put((envelope, {"jsonrpc": "2.0", "id": None,
                                                 "error": {"code": -32700, "message": "parse error"}}))
                else:
                    self.pool.submit(self._handle, envelope, req)
            while True:
                try:
                    env, rep = self.replies.get_nowait()
                except queue.Empty:
                    break
                sock.send_multipart(env + [json.dumps(rep, default=_json_default).encode()])
        self.pool.shutdown(wait=False, cancel_futures=True)
        sock.close()


def _json_default(o: Any) -> Any:
    try:
        return float(o)
    except (TypeError, ValueError):
        return str(o)


class ToolClient:
    """Blocking JSON-RPC client (one per thread). Raises ``ToolError`` on RPC errors/timeouts."""

    def __init__(self, port: int, caller: str, timeout_s: float = 5.0):
        self.addr = f"tcp://127.0.0.1:{port}"
        self.caller = caller
        self.timeout_ms = int(timeout_s * 1000)
        self.ctx = zmq.Context.instance()
        self.sock: zmq.Socket | None = None
        self.n = 0
        self._connect()

    def _connect(self) -> None:
        if self.sock is not None:
            self.sock.close(linger=0)
        self.sock = self.ctx.socket(zmq.REQ)
        self.sock.setsockopt(zmq.LINGER, 0)
        self.sock.connect(self.addr)

    def call(self, method: str, timeout_s: float | None = None, **params: Any) -> dict[str, Any]:
        assert self.sock is not None
        self.n += 1
        self.sock.send_json({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params,
                             "caller": self.caller})
        tmo = int(timeout_s * 1000) if timeout_s else self.timeout_ms
        if self.sock.poll(tmo, zmq.POLLIN):
            rep = self.sock.recv_json()
            if "error" in rep:
                raise ToolError(rep["error"].get("message", "error"))
            return rep["result"]
        self._connect()  # lazy pirate: drop the stuck REQ socket
        raise ToolError(f"timeout calling {method}")

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close(linger=0)


class ToolError(RuntimeError):
    pass
