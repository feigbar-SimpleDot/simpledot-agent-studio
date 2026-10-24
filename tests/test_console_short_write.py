"""issue #62 (2026-10-19) short-write 回归锚 — console._write_all 循环写全。

背景: BaseHTTPRequestHandler.wbufsize=0 → wfile 是无缓冲 SocketIO,
write() = 单次 sock.send(), macOS send buffer ~128KB 满时 short-write
且**不抛异常** (190 实测 GET / 642KB 40/40 截断)。170/Linux 无法复现
macOS 行为, 本单测用「每次 write 至多回 N 字节」的假 wfile 模拟短写,
作为跨平台回归锚 (ANALYSIS-61-65.md #62 测试锚指令)。

覆盖:
  SW1 _write_all 在短写 wfile 上累计写出 == len(body) 且无异常
  SW2 wfile.write 返回 0 视为断连 → 抛 OSError
  SW3 空 body 直接返回 (零 write)
  SW4 全量单次写成功路径 (Linux 常态) 行为不变

issue #64 (2026-10-19) paced write 补充 — Apple container 端口代理适配 (ANALYSIS-PROXY-TRUNC.md §4 P-4)。

背景: macOS Apple container 端口代理对 >128KB 突发 write 提前 FIN 截断
(190 实测: 642KB GET / 92/100 截断, 176KB PNG 10/10 截断; ≤90KB 全过;
SSE 涓流全过)。新增 Handler._write_paced: >64KB 时逐 64KB 写 + 25ms sleep,
给代理用户态留出排空时间 (实测唯一正性实证: SSE 涓流不截断)。

本单测验证 pacing 逻辑 (分段写, 正确 sleep 计数, 且保留≤64KB 直通无 sleep)、
MW_PACE_* env 开关、断连语义与 _write_all 对齐。

参考: docs/ANALYSIS-PROXY-TRUNC.md §4 P-4 (SW5~SW8) 指令。
"""
from __future__ import annotations
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mw.console import Handler  # noqa: E402


class ShortWfile:
    """假 wfile: 每次 write 至多写 cap 字节, 返回实际写字节数 (短写语义)。"""

    def __init__(self, cap: int):
        self.cap = cap
        self.buf = bytearray()

    def write(self, b) -> int:
        n = min(self.cap, len(b))
        self.buf += bytes(b[:n])
        return n


def test_sw1_loop_writes_full_body_under_short_write():
    """642KB body 在 64KB cap 假 wfile 上必须循环写全。"""
    body = bytes(range(256)) * (642441 // 256 + 1)  # ~642KB, 模拟 GET / 首页
    assert len(body) == 642560
    f = ShortWfile(cap=64 * 1024)
    Handler._write_all(f, body)
    assert bytes(f.buf) == body
    # 确实发生了多次短写 (证明模拟有效, 而非单写全吞)
    assert len(f.buf) > f.cap


def test_sw2_zero_return_raises_oserror():
    """write 返回 0 = 连接已断, 必须抛 OSError 交调用方兜底。"""
    class DeadWfile:
        def write(self, b) -> int:
            return 0
    try:
        Handler._write_all(DeadWfile(), b"hello")
        raise AssertionError("write 返回 0 应抛 OSError")
    except OSError:
        pass


def test_sw3_empty_body_no_write():
    class CountingWfile:
        def __init__(self):
            self.calls = 0
        def write(self, b) -> int:
            self.calls += 1
            return len(b)
    f = CountingWfile()
    Handler._write_all(f, b"")
    assert f.calls == 0


def test_sw4_single_full_write_path_unchanged():
    """wfile 一次吞下全量 (Linux 常态) 时只调一次 write。"""
    class CountingWfile(ShortWfile):
        def __init__(self, cap):
            super().__init__(cap)
            self.calls = 0
        def write(self, b) -> int:
            self.calls += 1
            return super().write(b)
    f = CountingWfile(cap=10 * 1024 * 1024)
    body = b"x" * 1000
    Handler._write_all(f, body)
    assert f.calls == 1
    assert bytes(f.buf) == body


def test_sw5_paced_write_splits_large_body_and_logs_sleep():
    """SW5: 642KB body 在 capped wfile + monkeypatch time.sleep 下,
    累计写出 == 642441, 调用 sleep 次数 = ceil(642441/65536)-1."""
    # 1. 准备假 wfile 每次吞 64KB (PACE_CHUNK 默认 64KB)
    body = bytes(range(256)) * (642441 // 256 + 1)  # ~642KB, 模拟 GET / 首页
    assert len(body) == 642560  # 可整除 256 的最近上界
    f = ShortWfile(cap=64 * 1024)
    
    # 2. monkeypatch time.sleep 记录调用
    sleep_calls = []

    def mock_sleep(sec):
        sleep_calls.append(sec)

    original_sleep = time.sleep
    try:
        time.sleep = mock_sleep
        # 3. 调用 _write_paced
        Handler._write_paced(f, body)
    finally:
        time.sleep = original_sleep
    
    # 4. 校验
    expected_sleeps = (len(body) + Handler.PACE_CHUNK - 1) // Handler.PACE_CHUNK - 1
    assert bytes(f.buf) == body
    # sleep 应被调用 expected_sleeps 次, 每次 PACE_DELAY (0.025 默认)
    assert len(sleep_calls) == expected_sleeps
    for sec in sleep_calls:
        assert sec == Handler.PACE_DELAY


def test_sw6_small_body_no_sleep():
    """SW6: body ≤ 64 KB → sleep 调用次数 == 0 (SSE 直通, 实时性零新增延迟锚)。"""
    # 1. 构造 <= 64KB 的 body
    body = b"x" * (64 * 1024)  # 正好 64KB
    f = ShortWfile(cap=10 * 1024 * 1024)  # 全吞假 wfile, 模拟单次写成功
    
    # 2. monkeypatch time.sleep
    sleep_calls = []

    def mock_sleep(sec):
        sleep_calls.append(sec)

    original_sleep = time.sleep
    try:
        time.sleep = mock_sleep
        Handler._write_paced(f, body)
    finally:
        time.sleep = original_sleep
    
    # 3. 不应调 sleep
    assert len(sleep_calls) == 0
    assert bytes(f.buf) == body


def test_sw7_pace_disabled_env_falls_back_to_write_all():
    """SW7: MW_PACE_DISABLED=1 (patch 类常量) → 行为与 _write_all 一致
    (即无 sleep)。"""
    # 1. monkeypatch 类常量
    original_disabled = Handler.PACE_DISABLED
    Handler.PACE_DISABLED = True
    
    body = bytes(range(256)) * (642441 // 256 + 1)  # ~642KB
    f = ShortWfile(cap=64 * 1024)
    sleep_calls = []

    def mock_sleep(sec):
        sleep_calls.append(sec)

    original_sleep = time.sleep
    try:
        time.sleep = mock_sleep
        Handler._write_paced(f, body)
    finally:
        time.sleep = original_sleep
        Handler.PACE_DISABLED = original_disabled
    
    # 2. 有 sleep 吗? 因 PACE_DISABLED 为 False 时, 行为应与 _write_all 相同
    # 即: body > PACE_CHUNK 且 PACE_DISABLED=False 时会 sleep; 本测试中为 True
    # → 应无 sleep, 但写出必须完整 (短写循环由 _write_all 提供)
    assert len(sleep_calls) == 0
    assert bytes(f.buf) == body


def test_sw8_paced_write_propagates_oserror_on_zero_return():
    """SW8: 假 wfile 在第3块返回 0 → 抛 OSError (断连语义与 _write_all 对齐)。"""
    call_count = 0

    class FakeWfile:
        def write(self, b) -> int:
            nonlocal call_count
            call_count += 1
            # 第3次调用时返回 0 模拟断连
            if call_count == 3:
                return 0
            return len(b)  # 前两次正常写
    
    body = b"x" * (3 * Handler.PACE_CHUNK + 100)  # 确保至少3块
    f = FakeWfile()
    
    try:
        Handler._write_paced(f, body)
        raise AssertionError("write 返回 0 应抛 OSError")
    except OSError as e:
        assert "wfile.write returned 0" in str(e)
    # 预期: 调了3次 write (2次成功, 1次返回0)
    assert call_count == 3