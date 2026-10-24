"""Tests for mw.sse_bridge — fake SSE server + thread coordination."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from typing import Optional

import pytest

# Allow `from mw import sse_bridge` when run from repo root
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mw import sse_bridge  # noqa: E402


# --------------------------------------------------------------------------
# Fake SSE server (stdlib, no third-party deps)
# --------------------------------------------------------------------------

class _FakeSSEServer:
    """Tiny TCP listener that hand-responds SSE frames then exits.

    Accepts one connection, writes the configured list of lines, then closes.
    `headers_done` flag flips after the first response line is sent.
    """

    def __init__(self, lines: list[str], bind: str = "127.0.0.1"):
        self.lines = lines
        self.bind = bind
        self.port: int = 0
        self.connected = threading.Event()
        self.headers_done = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self.received_request: bytes = b""

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.bind, 0))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            self.connected.set()
            # Read the request (don't try to parse, just consume)
            conn.settimeout(1.0)
            try:
                while b"\r\n\r\n" not in self.received_request:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    self.received_request += chunk
                    if len(self.received_request) > 8192:
                        break
            except socket.timeout:
                pass
            # Write SSE headers
            resp = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/event-stream\r\n"
                "Cache-Control: no-cache\r\n"
                "Connection: keep-alive\r\n\r\n"
            ).encode()
            conn.sendall(resp)
            self.headers_done.set()
            # Write configured lines (with a tiny gap to let the client process)
            for line in self.lines:
                if line:
                    conn.sendall((line + "\n\n").encode())
                    time.sleep(0.02)
                else:
                    conn.sendall(b"\n")

    def stop(self) -> None:
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass


@contextmanager
def _fake_sse(lines: list[str]):
    srv = _FakeSSEServer(lines)
    srv.start()
    base = f"http://127.0.0.1:{srv.port}"
    try:
        yield base, srv
    finally:
        srv.stop()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _event(event: str, **fields) -> str:
    return "data: " + json.dumps({"event": event, **fields})


def _wait(predicate, timeout=3.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def setup_function(_fn):
    sse_bridge._subscriptions.clear()
    sse_bridge._worker_live.clear()


def teardown_function(_fn):
    sse_bridge.close_all()
    sse_bridge._subscriptions.clear()
    sse_bridge._worker_live.clear()


def test_message_delta_updates_thinking_flag():
    lines = [
        ": keepalive",
        _event("message.delta", delta="hi"),
        _event("message.delta", delta=" there"),
        _event("reasoning.available", text="thinking..."),
        _event("run.completed", output="done", usage={}),
    ]
    with _fake_sse(lines) as (base, srv):
        sub = sse_bridge.open_bridge(
            run_id="run_test_1",
            profile="coder",
            session_id="api_test_sid",
            base_url=base,
            api_key="dummy",
        )
        assert _wait(lambda: srv.headers_done.is_set(), timeout=2)
        assert _wait(lambda: sse_bridge.is_thinking("coder", "api_test_sid") is False, timeout=3)
        live = sse_bridge.live_snapshot()[("coder", "api_test_sid")]
        assert live["event_count"] >= 4
        assert live["last_event_type"] == "run.completed"
        assert live["thinking"] is False
        assert sub.event_count >= 4


def test_run_completed_terminates_subscription_and_removes_from_active():
    lines = [
        _event("message.delta", delta="x"),
        _event("run.completed", output="done"),
    ]
    with _fake_sse(lines) as (base, srv):
        sse_bridge.open_bridge(
            run_id="run_test_2",
            profile="coder",
            session_id="api_test_sid_2",
            base_url=base,
            api_key="dummy",
        )
        assert _wait(lambda: "run_test_2" not in sse_bridge.active_subscriptions(), timeout=3)


def test_thinking_true_during_active_reasoning():
    lines = [
        _event("reasoning.available", text="plan A"),
        # 故意慢才发 run.completed, 期间 _worker_live.thinking 应为 True
    ]

    # 用一个自定义 server, 写到第二个 event 后 sleep 1s 再发 run.completed
    class SlowServer(_FakeSSEServer):
        def _serve(self):
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                self.connected.set()
                conn.settimeout(1.0)
                try:
                    while b"\r\n\r\n" not in self.received_request:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        self.received_request += chunk
                except socket.timeout:
                    pass
                resp = (
                    "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"
                ).encode()
                conn.sendall(resp)
                self.headers_done.set()
                conn.sendall(b"data: " + json.dumps({"event": "reasoning.available", "text": "plan"}).encode() + b"\n\n")
                time.sleep(0.5)
                conn.sendall(b"data: " + json.dumps({"event": "run.completed", "output": "done"}).encode() + b"\n\n")

    srv = SlowServer(lines)
    srv.start()
    try:
        sse_bridge.open_bridge(
            run_id="run_test_3",
            profile="coder",
            session_id="api_test_sid_3",
            base_url=f"http://127.0.0.1:{srv.port}",
            api_key="dummy",
        )
        assert _wait(lambda: srv.headers_done.is_set(), timeout=2)
        # reasoning.available 到了, _worker_live.thinking 应为 True
        assert _wait(lambda: sse_bridge.is_thinking("coder", "api_test_sid_3") is True, timeout=2)
        # 等 run.completed 完成后 thinking 应翻回 False
        assert _wait(lambda: sse_bridge.is_thinking("coder", "api_test_sid_3") is False, timeout=3)
    finally:
        srv.stop()


def test_cancel_stops_thread():
    # 一个不发任何 event 的 server, 让连接长挂
    class HangingServer(_FakeSSEServer):
        def _serve(self):
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                self.connected.set()
                conn.settimeout(0.5)
                try:
                    while True:
                        conn.recv(4096)
                except (socket.timeout, OSError):
                    pass

    srv = HangingServer([])
    srv.start()
    try:
        sub = sse_bridge.open_bridge(
            run_id="run_test_cancel",
            profile="coder",
            session_id="api_test_cancel",
            base_url=f"http://127.0.0.1:{srv.port}",
            api_key="dummy",
        )
        assert _wait(lambda: srv.connected.is_set(), timeout=2)
        sub.cancel()
        assert _wait(lambda: not sub._thread.is_alive(), timeout=3)
    finally:
        srv.stop()


def test_multiple_subscriptions_isolated_state():
    # 用两个独立 server, 每个 server 各服务一条订阅
    lines = [
        _event("message.delta", delta="A"),
        _event("run.completed", output="done"),
    ]
    with _fake_sse(lines) as (base_a, _srv_a), _fake_sse(lines) as (base_b, _srv_b):
        sse_bridge.open_bridge(
            run_id="run_multi_a",
            profile="coder",
            session_id="sid_multi_a",
            base_url=base_a,
            api_key="dummy",
        )
        sse_bridge.open_bridge(
            run_id="run_multi_b",
            profile="tester",
            session_id="sid_multi_b",
            base_url=base_b,
            api_key="dummy",
        )
        assert _wait(lambda: sse_bridge.live_snapshot().get(("coder", "sid_multi_a"), {}).get("last_event_type") == "run.completed", timeout=3)
        assert _wait(lambda: sse_bridge.live_snapshot().get(("tester", "sid_multi_b"), {}).get("last_event_type") == "run.completed", timeout=3)
        # 隔离：coder live 不应包含 tester sid
        snap = sse_bridge.live_snapshot()
        assert ("coder", "sid_multi_b") not in snap
        assert ("tester", "sid_multi_a") not in snap


def test_is_thinking_false_for_unknown_pair():
    assert sse_bridge.is_thinking("nobody", "nope") is False


# --------------------------------------------------------------------------
# FIX-1: 真实网关事件名活体用例 (assistant.delta / commentary / tool.progress)
# --------------------------------------------------------------------------

def test_real_gateway_events_keep_thinking_true_until_terminal():
    """assistant.delta / assistant.commentary / tool.progress 期间 thinking=True,
    终态 run.completed 后 False。用挂起 server: 发完活动事件不关流,
    避免断连重连干扰断言。"""
    activity = [
        _event("run.started"),
        _event("message.started"),
        _event("assistant.delta", delta="tok"),
        _event("assistant.commentary", text="边做边说"),
        _event("tool.progress", tool_name="_thinking"),
        _event("tool.started", tool_name="terminal"),
        _event("tool.completed", tool_name="terminal"),
    ]

    class HangingAfterServer(_FakeSSEServer):
        def _serve(self):
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                self.connected.set()
                conn.settimeout(1.0)
                try:
                    while b"\r\n\r\n" not in self.received_request:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        self.received_request += chunk
                except socket.timeout:
                    pass
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
                self.headers_done.set()
                for line in activity:
                    conn.sendall((line + "\n\n").encode())
                    time.sleep(0.02)
                # 不关流: 挂住, 让 thinking=True 状态可断言
                time.sleep(3.0)

    srv = HangingAfterServer([])
    srv.start()
    try:
        sse_bridge.open_bridge(
            run_id="run_fix1_real",
            profile="coder",
            session_id="sid_fix1_real",
            base_url=f"http://127.0.0.1:{srv.port}",
            api_key="dummy",
        )
        assert _wait(lambda: srv.headers_done.is_set(), timeout=2)
        # 首个非终态事件后 thinking 即 True, 且后续真实事件名全部维持 True
        assert _wait(lambda: sse_bridge.is_thinking("coder", "sid_fix1_real") is True, timeout=2)
        time.sleep(0.3)  # 让全部活动事件到齐
        assert sse_bridge.is_thinking("coder", "sid_fix1_real") is True
        live = sse_bridge.live_snapshot()[("coder", "sid_fix1_real")]
        assert live["event_count"] >= len(activity)
    finally:
        srv.stop()


def test_terminal_failed_and_cancelled_flip_thinking_false():
    """run.failed / run.cancelled 都是终态: thinking=False, 订阅退出, 不重连。"""
    for ev_name in ("run.failed", "run.cancelled"):
        sse_bridge._subscriptions.clear()
        sse_bridge._worker_live.clear()
        lines = [
            _event("assistant.delta", delta="x"),
            _event(ev_name, error="boom"),
        ]
        with _fake_sse(lines) as (base, _srv):
            sse_bridge.open_bridge(
                run_id=f"run_{ev_name}",
                profile="coder",
                session_id=f"sid_{ev_name}",
                base_url=base,
                api_key="dummy",
            )
            # 终态后订阅被移除 (若误重连, 假 server 只收一条连接, 不会消失)
            assert _wait(
                lambda: f"run_{ev_name}" not in sse_bridge.active_subscriptions(),
                timeout=3,
            ), f"{ev_name} 应终态退出订阅"
            live = sse_bridge.live_snapshot()[("coder", f"sid_{ev_name}")]
            assert live["thinking"] is False, f"{ev_name} 后 thinking 应为 False"
            assert live["last_event_type"] == ev_name


# --------------------------------------------------------------------------
# FIX-5: 断流重连 (指数退避持续重连 + 404 终态)
# --------------------------------------------------------------------------

class _MultiConnServer:
    """顺序服务多条连接: 第 n 条连接按 statuses[n] 应答并吐 lines[n] 后关闭。
    statuses 缺省全 200; 置 404 可模拟 run 已结束的断连重连场景。"""

    def __init__(self, per_conn_lines: list[list[str]],
                 statuses: Optional[list[int]] = None):
        self.per_conn_lines = per_conn_lines
        self.statuses = statuses or [200] * len(per_conn_lines)
        self.port: int = 0
        self.conn_count = 0
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(8)
        self._thread = threading.Thread(target=self._serve_all, daemon=True)
        self._thread.start()

    def _serve_all(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            idx = self.conn_count
            self.conn_count += 1
            threading.Thread(target=self._serve_one, args=(conn, idx),
                            daemon=True).start()

    def _serve_one(self, conn: socket.socket, idx: int) -> None:
        with conn:
            status = self.statuses[idx] if idx < len(self.statuses) else 404
            if status != 200:
                body = b'{"error": "Run not found"}'
                conn.sendall(
                    f"HTTP/1.1 {status} Not Found\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
                return
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
            lines = self.per_conn_lines[idx] if idx < len(self.per_conn_lines) else []
            for line in lines:
                conn.sendall((line + "\n\n").encode())
                time.sleep(0.02)
            # 关闭连接 = 模拟断流

    def stop(self) -> None:
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass


def test_reconnect_after_disconnect_keeps_liveness_continuous(monkeypatch):
    """首连中途断(无终态), 重连成功收到终态: 活体信号全程连续, 不断档。"""
    monkeypatch.setattr(sse_bridge, "_RECONNECT_BASE_S", 0.05)
    srv = _MultiConnServer([
        # 连接1: 活动事件后断流 (无终态)
        [
            _event("assistant.delta", delta="a"),
            _event("tool.progress", tool_name="_thinking"),
        ],
        # 连接2: 续上活动事件, 正常终态
        [
            _event("assistant.delta", delta="b"),
            _event("run.completed", output="done"),
        ],
    ])
    srv.start()
    try:
        sse_bridge.open_bridge(
            run_id="run_reconnect",
            profile="coder",
            session_id="sid_reconnect",
            base_url=f"http://127.0.0.1:{srv.port}",
            api_key="dummy",
        )
        # 断连重连后订阅仍在册 (旧实现重试3次会永久移除)
        assert _wait(lambda: srv.conn_count >= 2, timeout=5), "应发生重连"
        assert "run_reconnect" in sse_bridge.active_subscriptions()
        # 重连期间活体保持 True (陈旧不失明)
        assert sse_bridge.is_thinking("coder", "sid_reconnect") is True
        # 终态后正常退出
        assert _wait(
            lambda: "run_reconnect" not in sse_bridge.active_subscriptions(),
            timeout=5,
        )
        live = sse_bridge.live_snapshot()[("coder", "sid_reconnect")]
        assert live["thinking"] is False
        assert live["last_event_type"] == "run.completed"
        # 两条连接的事件都累计进活体
        assert live["event_count"] >= 4
    finally:
        srv.stop()


def test_reconnect_404_treated_as_terminal(monkeypatch):
    """重连拿到 404 (run 已结束): 视为终态退出, thinking 清 False, 不再重连。"""
    monkeypatch.setattr(sse_bridge, "_RECONNECT_BASE_S", 0.05)
    # 连接1: 活动事件后断流; 连接2 起: 一律 404
    srv = _MultiConnServer(
        per_conn_lines=[[_event("assistant.delta", delta="a")]],
        statuses=[200, 404, 404, 404],
    )
    srv.start()
    try:
        sse_bridge.open_bridge(
            run_id="run_gone",
            profile="coder",
            session_id="sid_gone",
            base_url=f"http://127.0.0.1:{srv.port}",
            api_key="dummy",
        )
        assert _wait(lambda: sse_bridge.is_thinking("coder", "sid_gone") is True, timeout=2)
        assert _wait(
            lambda: "run_gone" not in sse_bridge.active_subscriptions(),
            timeout=5,
        ), "404 应终态退出"
        live = sse_bridge.live_snapshot()[("coder", "sid_gone")]
        assert live["thinking"] is False
        assert live["last_event_type"] == "run.gone(404)"
        # 确认不再继续重连
        conns_at_exit = srv.conn_count
        time.sleep(0.3)
        assert "run_gone" not in sse_bridge.active_subscriptions()
        assert srv.conn_count == conns_at_exit
    finally:
        srv.stop()


def test_backoff_capped_at_60s():
    """退避序列 2→4→8→...→60 封顶 (纯函数式断言, 不实际等待)。"""
    b = sse_bridge._RECONNECT_BASE_S
    seq = []
    for _ in range(8):
        seq.append(b)
        b = min(b * 2, sse_bridge._RECONNECT_MAX_S)
    assert seq[0] == 2.0
    assert seq[-1] == 60.0
    assert all(x <= 60.0 for x in seq)