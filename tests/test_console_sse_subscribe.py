"""Integration test: api_workers_subscribe_sse -> mw.sse_bridge 接入."""

from __future__ import annotations

import os
import sys

# 测试用假 registry — 不污染真实 mw/registry.json
os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")
os.environ.setdefault("MW_HOME", "/tmp/mw_test_home")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from mw import sse_bridge
from mw import console


def setup_function(_fn):
    sse_bridge._subscriptions.clear()
    sse_bridge._worker_live.clear()


def teardown_function(_fn):
    sse_bridge.close_all()
    sse_bridge._subscriptions.clear()
    sse_bridge._worker_live.clear()


class _FakeGateway:
    """伪 worker gateway — 接 SSE GET 后, 回一条 run.completed."""
    def __init__(self):
        self.bind = "127.0.0.1"
        self.port = 0
        self._sock = None
        self._thread = None
        self.received = []

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.bind, 0))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(0.5)
            try:
                while b"\r\n\r\n" not in b"".join(self.received):
                    chunk = conn.recv(4096)
                    if not chunk: break
                    self.received.append(chunk)
            except socket.timeout: pass
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
            # 送一条 message.delta 后 run.completed, 然后关
            conn.sendall(b"data: " + json.dumps({"event": "message.delta", "delta": "hi"}).encode() + b"\n\n")
            time.sleep(0.05)
            conn.sendall(b"data: " + json.dumps({"event": "run.completed", "output": "ok"}).encode() + b"\n\n")

    def stop(self):
        try:
            self._sock.close()
        except OSError: pass


def _wait(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(): return True
        time.sleep(0.05)
    return predicate()


def test_api_workers_subscribe_sse_starts_bridge():
    gw = _FakeGateway()
    gw.start()
    try:
        payload = {
            "run_id": "run_integration_1",
            "profile": "tester",
            "session_id": "api_session_integration",
            "task_id": "T-INT-1",
            "api_url": f"http://127.0.0.1:{gw.port}",
            "api_key": "test",
        }
        r = console.api_workers_subscribe_sse(payload)
        assert r["ok"], r
        assert r["run_id"] == "run_integration_1"

        # 等 run.completed 后 _subscriptions 应清理
        assert _wait(lambda: "run_integration_1" not in sse_bridge.active_subscriptions(), timeout=3)
        # live_snapshot 应保留最后状态 (run.completed 翻 thinking=False)
        live = sse_bridge.live_snapshot().get(("tester", "api_session_integration"))
        assert live is not None
        assert live["last_event_type"] == "run.completed"
        assert live["thinking"] is False
    finally:
        gw.stop()


def test_api_workers_returns_live_data():
    gw = _FakeGateway()
    gw.start()
    try:
        # 起一个还没完成的连接
        sse_bridge.open_bridge(
            run_id="run_active_x",
            profile="tester",
            session_id="api_session_active",
            base_url=f"http://127.0.0.1:{gw.port}",
            api_key="x",
        )
        # 此刻 _worker.hugging None (gateway 还没响应) — api_workers 应有 fallback
        r = console.api_workers()
        # 即使 _worker.hugging 空, API 不抛
        assert "workers" in r
        assert "active_run_ids" in r
    finally:
        gw.stop()


def test_subscribe_sse_rejects_missing_fields():
    r = console.api_workers_subscribe_sse({"run_id": "x"})
    assert not r["ok"]
    assert "profile" in r["error"] or "session_id" in r["error"]


# ---------------------------------------------------------------------------
# SSE-FIX-F2: GET /api/workers/live 轻量活体端点形状断言
# ---------------------------------------------------------------------------

_LIVE_ITEM_KEYS = {"profile", "session_id", "thinking",
                  "last_event_ts", "last_event_type", "event_count"}


def test_workers_live_shape_empty():
    """空桥接状态: 形状仍为 {live: [], active_run_ids: []}。"""
    r = console.api_workers_live()
    assert set(r.keys()) == {"live", "active_run_ids"}
    assert r["live"] == []
    assert r["active_run_ids"] == []


def test_workers_live_shape_with_live_entries():
    """注入两条活体记录: 逐条校验六字段形状与取值。"""
    now = time.time()
    sse_bridge._worker_live[("coder", "sess_a")] = {
        "session_id": "sess_a", "last_run_id": "run_a",
        "last_event_ts": now, "last_event_type": "tool.progress",
        "thinking": True, "event_count": 7,
    }
    sse_bridge._worker_live[("tester", "sess_b")] = {
        "session_id": "sess_b", "last_run_id": "run_b",
        "last_event_ts": now - 5, "last_event_type": "run.completed",
        "thinking": False, "event_count": 3,
    }
    sse_bridge._subscriptions["run_a"] = sse_bridge.Subscription(
        run_id="run_a", profile="coder", session_id="sess_a",
        base_url="http://127.0.0.1:1", api_key="x")  # 占位, 只测 active_run_ids
    r = console.api_workers_live()
    assert set(r.keys()) == {"live", "active_run_ids"}
    assert len(r["live"]) == 2
    by_profile = {e["profile"]: e for e in r["live"]}
    assert set(by_profile) == {"coder", "tester"}
    a = by_profile["coder"]
    assert set(a.keys()) == _LIVE_ITEM_KEYS
    assert a["session_id"] == "sess_a"
    assert a["thinking"] is True
    assert a["last_event_ts"] == now
    assert a["last_event_type"] == "tool.progress"
    assert a["event_count"] == 7
    b = by_profile["tester"]
    assert set(b.keys()) == _LIVE_ITEM_KEYS
    assert b["thinking"] is False
    assert b["last_event_type"] == "run.completed"
    assert r["active_run_ids"] == ["run_a"]


def test_workers_live_zero_health_probe(monkeypatch):
    """核心红线: /api/workers/live 绝不触发 worker /health 探测副作用。"""
    def _boom(*a, **k):
        raise AssertionError("api_workers_live 不得调用 _health_probe")
    monkeypatch.setattr(console, "_health_probe", _boom)
    # 同时封掉 api_workers, 证明本端点路径与其完全无关
    monkeypatch.setattr(console, "api_workers",
                       lambda: (_ for _ in ()).throw(
                           AssertionError("不得复用 api_workers")))
    r = console.api_workers_live()  # 不抛即通过
    assert r == {"live": [], "active_run_ids": []}


def test_workers_live_http_route():
    """真实 HTTP 路由: GET /api/workers/live 命中新端点(非 worker 详情误路由)。"""
    sse_bridge._worker_live[("coder", "sess_http")] = {
        "session_id": "sess_http", "last_run_id": "run_http",
        "last_event_ts": 123.0, "last_event_type": "assistant.delta",
        "thinking": True, "event_count": 1,
    }
    import urllib.request
    from http.server import ThreadingHTTPServer
    from mw.console import Handler
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/workers/live", timeout=5) as resp:
            assert resp.status == 200
            body = json.loads(resp.read())
        assert body["live"] == [{
            "profile": "coder", "session_id": "sess_http",
            "thinking": True, "last_event_ts": 123.0,
            "last_event_type": "assistant.delta", "event_count": 1,
        }]
        assert body["active_run_ids"] == []
    finally:
        srv.shutdown()
        srv.server_close()