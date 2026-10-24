"""tests/test_issue63_proxy_drain.py — issue #63 同源代理断连排空保活 (PLAN-63 §5 步骤4)

背景 (PLAN-63 §1.3): _proxy_stream 转发循环是「浏览器→console→gateway」的串行
耦合体 —— 浏览器断开后写 self.wfile 抛 BrokenPipe, 异常穿出代理函数,
`with resp:` 提前关闭 console→gateway 连接, gateway 判定 "SSE client
disconnected" → agent.interrupt 掐死在跑回合。

修复: 转发循环内捕获下游写失败, 转排空模式继续读尽上游到 EOF/上限
(MW_PROXY_DRAIN_MAX_S, 默认 3600s; 0=不排空回滚开关), 异常不再穿出函数。
gateway 全程不见断连 → 不触发 interrupt。

覆盖 (PLAN-63 §5 步骤4 用例清单):
  D1 排空核心: wfile 第 3 次写抛 BrokenPipe → 上游迭代器被消费到耗尽,
     _proxy_stream 不抛异常, 日志含 downstream gone
  D2 正常路径回归: wfile 全程可写 → 逐行透传内容与顺序不变, 无 drain 日志
  D3 drain 超时: 上游慢而不 EOF + DRAIN_MAX_S 调小 → deadline 内退出 +
     drain timeout 日志, 不悬挂
  D4 DRAIN_MAX_S=0 回滚语义: 断连后不排空 (上游剩余帧不消费), 行为退化旧版
  D5 ConnectionResetError 同样进入排空 (异常集合完整)
  D6 非流式代理回归: urlopen HTTPError → _send(e.code) 分支不受影响;
     urlopen 通用异常 → 502 分支不受影响
  D7 排空后 close_connection=True (下游已死, 不再等 keep-alive 复用)
     + 正常完成路径不改 close_connection (keep-alive 语义零回退)

跑法: 仓库根 .venv 下 `pytest tests/test_issue63_proxy_drain.py -v`
"""
from __future__ import annotations

import io
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mw.console import Handler  # noqa: E402


# ──────────────────────────────────────────────────────────────────
# 测试脚手架: 不绑 socket 的 Handler 函数级直测 (同 test_console_short_write
# 口径 —— 类名先 grep 再实例化; send_* 用实例属性遮蔽, 不落真实 HTTP)
# ──────────────────────────────────────────────────────────────────

class FakeWfile:
    """假下游 wfile: 前 ok_writes 次写成功, 之后抛 exc_type (模拟断连)。"""

    def __init__(self, ok_writes: int, exc_type=BrokenPipeError):
        self.ok_writes = ok_writes
        self.exc_type = exc_type
        self.buf = bytearray()
        self.writes = 0
        self.flushes = 0

    def write(self, b) -> int:
        self.writes += 1
        if self.writes > self.ok_writes:
            raise self.exc_type("downstream gone")
        self.buf += bytes(b)
        return len(b)

    def flush(self):
        self.flushes += 1


class FakeResp:
    """假 gateway 响应: 可控行迭代器 + 上下文管理 (与 urlopen 返回值同形态)。

    关键语义: 真实 HTTPResponse 是文件对象 (自身即迭代器), drain 循环二次
    `for line in resp` 从**当前位置**继续而非重头 —— 本桩用单一常驻迭代器
    复刻该语义, 否则排空会把已投递的帧重复消费, 断言失真。
    """

    def __init__(self, lines, status=200, ctype="text/event-stream"):
        self.status = status
        self.headers = {"Content-Type": ctype}
        self.state = {"exhausted": False, "delivered": 0}

        def _gen():
            for line in lines:
                self.state["delivered"] += 1
                yield line
            self.state["exhausted"] = True
        self._it = _gen()

    def __iter__(self):
        return self._it

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FailingUpstream:
    """假上游: 读到第 N 帧后上游断开 (模拟 B 型断连 —— gateway 侧断了)。

    issue #72 场景: console→gateway 上游段断开。

    关键语义 (复刻真实 socket): 断连是**粘性**的 —— 断开之后每一次读都抛同一
    个异常 (真实 socket 处于错误态后再读必再抛), 而不是抛一次就变 StopIteration。
    用生成器做不到这点 (生成器抛一次后即耗尽), 会让「排空期再读一次」永远读不到
    异常 → B 型指纹打不出来 → 用例假绿。故此处用类迭代器显式复刻粘性语义。
    """

    def __init__(self, exc=ConnectionResetError("upstream gone"),
                 deliver=1, status=200):
        self.status = status
        self.headers = {"Content-Type": "text/event-stream"}
        self.exc = exc
        self.deliver = deliver
        self.state = {"exhausted": False, "delivered": 0}
        self._n = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self._n < self.deliver:
            self._n += 1
            self.state["delivered"] += 1
            return SSE_FRAMES[0]
        # 粘性断连: 之后每次读都抛同一个异常
        raise self.exc

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_handler(wfile):
    """构造未绑 socket 的 Handler 实例 (函数级直测)。"""
    h = Handler.__new__(Handler)
    h.command = "POST"
    h.path = "/api/sessions/sess-x/chat/stream?x=1"
    h.headers = {}                      # 无 Content-Length → body=None
    h.rfile = io.BytesIO(b"")
    h.wfile = wfile
    h.close_connection = False
    h.sent = []                         # _send 记录
    h.headers_sent = []                 # send_response/send_header 记录
    h.send_response = lambda code: h.headers_sent.append(("status", code))
    h.send_header = lambda k, v: h.headers_sent.append((k, v))
    h.end_headers = lambda: h.headers_sent.append(("end", None))
    h._send = lambda code, obj, ctype="application/json": h.sent.append(code)
    h.log_message = lambda *a, **k: None
    return h


SSE_FRAMES = [
    b"event: run.started\ndata: {}\n\n",
    b"data: chunk1\n\n",
    b"data: chunk2\n\n",
    b"data: chunk3\n\n",
    b"data: chunk4\n\n",
    b"event: run.completed\ndata: {}\n\n",
]


def run_proxy(monkeypatch, h, resp):
    """monkeypatch urlopen 返回假 resp 后调用 _proxy_stream。"""
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: resp)
    h._proxy_stream("http://gateway.local/api/sessions/sess-x/chat/stream", "k")


# ──────────────────────────────────────────────────────────────────
# D1 排空核心: 下游写失败 → 上游消费到耗尽 + 不抛异常 + drain 日志
# ──────────────────────────────────────────────────────────────────
def test_d1_downstream_break_drains_upstream_to_exhaustion(monkeypatch, capsys):
    w = FakeWfile(ok_writes=2)          # 第 3 行写时炸
    h = make_handler(w)
    resp = FakeResp(SSE_FRAMES)
    run_proxy(monkeypatch, h, resp)     # 不得抛异常 (旧行为会穿出)
    # 排空生效: 上游迭代器被消费到耗尽 (gateway 可见到 EOF, 不见断连)
    assert resp.state["exhausted"] is True, "drain 未生效: 上游未消费到耗尽"
    assert resp.state["delivered"] == len(SSE_FRAMES)
    out = capsys.readouterr().out
    assert "downstream gone; draining upstream to completion" in out
    # 断连前的帧确实透传给了下游 (前 2 行)
    assert w.buf.startswith(SSE_FRAMES[0])


# ──────────────────────────────────────────────────────────────────
# D2 正常路径回归: 全程可写 → 逐行透传, 行为与旧一致
# ──────────────────────────────────────────────────────────────────
def test_d2_normal_path_unchanged(monkeypatch, capsys):
    w = FakeWfile(ok_writes=100)
    h = make_handler(w)
    resp = FakeResp(SSE_FRAMES)
    run_proxy(monkeypatch, h, resp)
    assert bytes(w.buf) == b"".join(SSE_FRAMES), "透传内容/顺序必须逐字节不变"
    assert resp.state["exhausted"] is True
    out = capsys.readouterr().out
    assert "downstream gone" not in out   # 正常路径零新增日志
    # 响应头三件套与旧行为一致 (status 直传 + no-cache + no buffering)
    assert ("status", 200) in h.headers_sent
    assert ("Cache-Control", "no-cache") in h.headers_sent
    assert ("X-Accel-Buffering", "no") in h.headers_sent


# ──────────────────────────────────────────────────────────────────
# D3 drain 超时: 上游慢而不 EOF → deadline 内退出 + timeout 日志
# ──────────────────────────────────────────────────────────────────
def test_d3_drain_timeout_exits_not_hangs(monkeypatch, capsys):
    w = FakeWfile(ok_writes=0, exc_type=BrokenPipeError)  # 首行写即断
    h = make_handler(w)

    resp = FakeResp([])

    def slow_gen():
        # 慢而不 EOF 的上游 (每帧 60ms, 200 帧硬上限防测试进程悬挂)
        for _ in range(200):
            resp.state["delivered"] += 1
            time.sleep(0.06)
            yield SSE_FRAMES[0]
        resp.state["exhausted"] = True
    resp._it = slow_gen()
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: resp)
    orig = Handler.DRAIN_MAX_S
    Handler.DRAIN_MAX_S = 0.1           # 同 SW 系列口径: 直接改类常量
    try:
        t0 = time.monotonic()
        h._proxy_stream("http://gateway.local/x", "k")
        elapsed = time.monotonic() - t0
    finally:
        Handler.DRAIN_MAX_S = orig
    # deadline ≈ 0.1s 生效: 远早于 200 帧自然耗尽 (200×60ms=12s)
    assert elapsed < 3.0, f"drain 未按 deadline 退出 (elapsed={elapsed:.2f}s)"
    assert resp.state["exhausted"] is False, "应是被 deadline 弃排空而非自然 EOF"
    out = capsys.readouterr().out
    assert "drain timeout, abandoning" in out


# ──────────────────────────────────────────────────────────────────
# D4 回滚开关: DRAIN_MAX_S=0 → 断连后不排空 (旧行为近似)
# ──────────────────────────────────────────────────────────────────
def test_d4_zero_drain_max_disables_drain(monkeypatch, capsys):
    w = FakeWfile(ok_writes=1)
    h = make_handler(w)
    resp = FakeResp(SSE_FRAMES)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: resp)
    orig = Handler.DRAIN_MAX_S
    Handler.DRAIN_MAX_S = 0.0
    try:
        h._proxy_stream("http://gateway.local/x", "k")
    finally:
        Handler.DRAIN_MAX_S = orig
    out = capsys.readouterr().out
    # 日志照打 (A 型断连指纹保留), 但上游剩余帧不再消费 = 不排空。
    # (常驻迭代器语义: 断连前转发循环已消费 2 帧 —— 帧1 写成功, 帧2 写时抛)
    assert "downstream gone" in out
    assert resp.state["delivered"] == 2, "DRAIN_MAX_S=0 不得继续消费上游剩余帧"
    assert resp.state["exhausted"] is False


# ──────────────────────────────────────────────────────────────────
# D5 异常集合: ConnectionResetError 同样进入排空
# ──────────────────────────────────────────────────────────────────
def test_d5_connection_reset_also_drains(monkeypatch, capsys):
    w = FakeWfile(ok_writes=1, exc_type=ConnectionResetError)
    h = make_handler(w)
    resp = FakeResp(SSE_FRAMES)
    run_proxy(monkeypatch, h, resp)
    assert resp.state["exhausted"] is True
    assert "downstream gone" in capsys.readouterr().out


# ──────────────────────────────────────────────────────────────────
# D6 非流式代理回归: HTTPError / 通用异常分支不受影响
# ──────────────────────────────────────────────────────────────────
def test_d6_httperror_branch_unchanged(monkeypatch):
    h = make_handler(FakeWfile(ok_writes=100))
    err = urllib.error.HTTPError("http://gw/x", 409, "busy", {}, io.BytesIO(b"{}"))

    def raise_http_error(req, timeout=None):
        raise err
    monkeypatch.setattr(urllib.request, "urlopen", raise_http_error)
    h._proxy_stream("http://gateway.local/x", "k")
    assert h.sent == [409], "HTTPError 分支必须原样 _send(e.code)"


def test_d6b_unreachable_branch_unchanged(monkeypatch):
    h = make_handler(FakeWfile(ok_writes=100))

    def raise_conn_error(req, timeout=None):
        raise ConnectionRefusedError("no gateway")
    monkeypatch.setattr(urllib.request, "urlopen", raise_conn_error)
    h._proxy_stream("http://gateway.local/x", "k")
    assert h.sent == [502], "gateway unreachable 分支必须原样 502"


# ──────────────────────────────────────────────────────────────────
# D7 close_connection 语义: 断连排空路径置 True, 正常路径不动
# ──────────────────────────────────────────────────────────────────
def test_d7_close_connection_only_on_downstream_gone(monkeypatch):
    w_ok = FakeWfile(ok_writes=100)
    h_ok = make_handler(w_ok)
    run_proxy(monkeypatch, h_ok, FakeResp(SSE_FRAMES))
    assert h_ok.close_connection is False, "正常路径不得关掉 keep-alive"

    w_dead = FakeWfile(ok_writes=1)
    h_dead = make_handler(w_dead)
    run_proxy(monkeypatch, h_dead, FakeResp(SSE_FRAMES))
    assert h_dead.close_connection is True, "下游已死必须显式断本侧连接"


# ──────────────────────────────────────────────────────────────────
# issue #72: 排空期上游断连 (B 型) 的日志分型指纹
#   背景: 排空期读上游抛错此前被 suppress(Exception) 静默吞掉, 现场既无
#         drain timeout 也无任何指纹 → B 型 (上游 gateway 断) 会被误判成
#         A 型 (下游浏览器断)。修法 = 排空期读异常时补打独立日志
#         "upstream gone during drain"; A/B 靠该行有无一眼分型。
#   D8  B 型: 排空期上游读抛错 → stdout 含 "upstream gone during drain"
#   D9  A 型: 排空读到 EOF → stdout 不含该行 (A 型语义零变化)
#   D10 指纹不含请求头/凭据/查询串 (只打 method + console path + gateway path)
# ──────────────────────────────────────────────────────────────────
def test_d8_btype_upstream_error_logs_fingerprint(monkeypatch, capsys):
    # 下游先断 (A 型入口), 排空期上游读抛 ConnectionResetError (B 型)
    w = FakeWfile(ok_writes=1)          # 第 2 行写时断 → 进入排空模式
    h = make_handler(w)
    resp = FailingUpstream(exc=ConnectionResetError("upstream gone"),
                            deliver=2)  # 转发循环读 1 帧 (写成功) 后进入排空
    run_proxy(monkeypatch, h, resp)     # 不得抛异常 (排空期读错仍被吞)
    out = capsys.readouterr().out
    assert "upstream gone during drain" in out, \
        f"排空期上游读异常必须打出 B 型指纹, 实际 stdout={out!r}"
    # 该行必须含 method + console path + gateway path (运维分型所需三要素)
    assert "POST" in out
    assert "/api/sessions/sess-x/chat/stream" in out
    # A 型入口日志仍在 (两行并存 = 可分型)
    assert "downstream gone; draining upstream to completion" in out


def test_d9_atype_eof_does_not_log_upstream_fingerprint(monkeypatch, capsys):
    # A 型: 下游断 → 排空读到 EOF (正常耗尽) → 不打 B 型指纹
    w = FakeWfile(ok_writes=1)
    h = make_handler(w)
    resp = FakeResp(SSE_FRAMES)
    run_proxy(monkeypatch, h, resp)
    assert resp.state["exhausted"] is True
    out = capsys.readouterr().out
    assert "upstream gone during drain" not in out, \
        f"A 型 (读到 EOF) 不得打 B 型指纹, 实际 stdout={out!r}"
    assert "downstream gone" in out


def test_d10_fingerprint_has_no_headers_or_query(monkeypatch, capsys):
    # 指纹行不得含请求头/凭据/查询串 (只打 method + path(去 query) + gateway path)
    w = FakeWfile(ok_writes=1)
    h = make_handler(w)
    h.headers = {"Authorization": "Bearer secret-token-value",
                 "X-Api-Key": "super-secret-key"}
    resp = FailingUpstream(exc=ConnectionResetError("upstream gone"),
                            deliver=2)
    run_proxy(monkeypatch, h, resp)
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines()
             if "upstream gone during drain" in ln]
    assert len(lines) == 1, f"应恰好一行 B 型指纹, 实际={lines!r}"
    line = lines[0]
    assert "secret" not in line and "Bearer" not in line, \
        f"指纹行泄露了凭据: {line!r}"
    assert "x=1" not in line, f"指纹行泄露了查询串: {line!r}"
    assert "sess-x/chat/stream" in line


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

