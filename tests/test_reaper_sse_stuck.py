"""SSE-FIX-F3 (2026-10-08): reaper 敲门抑制改查 console 活体端点。

覆盖 (对应 SSE 活体系列修复卡 §4.3/§4.4, 见 git log):
  1. sqlite3.Row 安全访问回归 — 真实 sqlite3.Row 喂给 _query_console_liveness
     / _row_get, 不再抛 'sqlite3.Row' object has no attribute 'get' (根因 2.1)。
  2. console live 报 thinking=True → SUPPRESSED_ALIVE, 不敲门。
  3. thinking=False 且 last_event_ts 陈旧 → 照敲。
  4. console 不可达 (urlopen 抛异常) → 照敲 (fail-open 不反转)。
  5. console 可达但无该 (profile, session_id) 条目 → 照敲。
  6. _notify_null_slot: 名下只有未派发卡 → 不拿 launch 时刻误报 worker
     闲置; 全未派发且 run 行也陈旧 → 文案「等待 main 派发」。
  7. _notify_null_slot: 已派发卡 + console 报活 → SUPPRESSED_ALIVE 不敲。
  8. 死代码清除 — reaper 源码不再 import sse_bridge.is_thinking/_worker_live。

跑法: pytest tests/test_reaper_sse_stuck.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# reaper 敲门路径要求 API_SERVER_KEY 非空
os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")  # 不会真连

from mw import reaper as reaper_mod  # noqa: E402


# ── 测试脚手架 ──────────────────────────────────────────────────

def _real_row(mapping: dict) -> sqlite3.Row:
    """构造**真实** sqlite3.Row — 复现 ledger.get_session() 的返回类型,
    Row 没有 .get(), 旧代码 sess.get("profile") 必崩 (根因 2.1)。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ", ".join(f'"{k}" TEXT' for k in mapping)
    conn.execute(f"CREATE TABLE t ({cols})")
    conn.execute(f"INSERT INTO t VALUES ({', '.join('?' * len(mapping))})",
                 tuple(mapping.values()))
    return conn.execute("SELECT * FROM t").fetchone()


class _FakeLedger:
    """最小 ledger 替身: 契约行 + 真实 sqlite3.Row 的 session 行。"""

    def __init__(self, *, active_run_id, current_card_id, sess_row,
                 last_progress_at, last_notified_at=None):
        self._active = active_run_id
        self._cur = current_card_id
        self._sess = sess_row
        self._lpa = last_progress_at
        self._lna = last_notified_at or 0.0
        self.path = ":memory:"

    def current_card_contract(self, run_id, card_id):
        return {
            "task_id": "T-1",
            "harvest_status": "PENDING",
            "last_progress_at": self._lpa,
            "updated_at": self._lpa,
        }

    def should_notify(self, task_id, *, min_interval_s):
        return (time.time() - self._lna) >= min_interval_s

    def get_session(self, task_id):
        return self._sess  # 真实 sqlite3.Row

    def get_initiator(self, task_id):
        return "sid_main_initiator"

    # D1 桩 (VERIFY-20261013 §2.3): F5-4 敲门前先标 WAITING, 替身须有
    # 该方法; 记录调用供 WAITING-arming 断言, 生产语义等价 no-op。
    def update_worker_state(self, task_id, new_status, *, actor,
                            reason="", log=""):
        self.waiting_calls = getattr(self, "waiting_calls", [])
        self.waiting_calls.append((task_id, new_status, actor))


def _live_entry(thinking, ts, etype="assistant.delta"):
    return {"profile": "coder", "session_id": "sid_x",
            "thinking": thinking, "last_event_ts": ts,
            "last_event_type": etype, "event_count": 42}


def _patch_liveness(entry=None, boom=False):
    """monkeypatch reaper 的活体查询: 返回 entry / 抛异常模拟不可达。"""
    if boom:
        return mock.patch.object(reaper_mod, "_query_console_liveness",
                               side_effect=ConnectionRefusedError("console down"))
    return mock.patch.object(reaper_mod, "_query_console_liveness",
                           return_value=entry)


def _call_stuck(ledger, results=None, base="/nonexistent-base"):
    if results is None:
        results = []
    reaper_mod._notify_main_of_stuck(
        ledger, base, results,
        registry=None,
        stuck_after_min=1,
        active_run_id=ledger._active,
        current_card_id=ledger._cur,
    )
    return results


# ── 1. sqlite3.Row 安全访问 (根因 2.1 回归) ─────────────────────

def test_real_sqlite_row_no_attributeerror():
    """真实 sqlite3.Row 喂进活体查询链路, 不得抛 AttributeError。"""
    row = _real_row({"task_id": "T-1", "session_id": "sid_x",
                     "profile": "coder"})
    # 前置断言: Row 确实没有 .get() —— 旧代码必崩的原因
    assert not hasattr(row, "get")
    with _patch_liveness(entry=_live_entry(True, time.time())):
        # _row_get 走 keys() 路径, 不碰 .get()
        assert reaper_mod._row_get(row, "profile") == "coder"
        assert reaper_mod._row_get(row, "missing_col") is None
        ledger = _FakeLedger(active_run_id="run_a", current_card_id="card_c",
                            sess_row=row, last_progress_at=time.time() - 3600)
        sent = []
        with mock.patch.object(reaper_mod, "_send_to_main",
                             lambda *a, **k: sent.append(a) or True):
            results = _call_stuck(ledger)
    # 关键: 没有 sqlite3.Row AttributeError, 且抑制正常生效
    assert not any("AttributeError" in str(r) for r in results), results
    assert len(sent) == 0


def test_row_without_profile_fails_open():
    """session 行缺 profile 列 → 不抑制, 照敲 (fail-open)。"""
    row = _real_row({"task_id": "T-1", "session_id": "sid_x",
                     "profile": None})
    ledger = _FakeLedger(active_run_id="run_a", current_card_id="card_c",
                         sess_row=row, last_progress_at=time.time() - 3600)
    sent = []
    with mock.patch.object(reaper_mod, "_send_to_main",
                          lambda *a, **k: sent.append(a) or True):
        results = _call_stuck(ledger)
    assert len(sent) == 1, f"缺 profile 应照敲, got {results}"


# ── 2~5. 抑制判定矩阵 ──────────────────────────────────────────

def _stuck_ledger():
    row = _real_row({"task_id": "T-1", "session_id": "sid_x",
                     "profile": "coder"})
    return _FakeLedger(active_run_id="run_a", current_card_id="card_c",
                       sess_row=row, last_progress_at=time.time() - 3600)


def test_thinking_true_suppresses_knock():
    now = time.time()
    ledger = _stuck_ledger()
    sent = []
    with _patch_liveness(entry=_live_entry(True, now)), \
         mock.patch.object(reaper_mod, "_send_to_main",
                          lambda *a, **k: sent.append(a) or True):
        results = _call_stuck(ledger)
    sup = [r for r in results if r.get("verdict") == "SUPPRESSED_ALIVE"]
    assert len(sup) == 1, f"thinking=True 应抑制, got {results}"
    # note 里带 last_event_type / age, 供 main 一眼判定
    assert "assistant.delta" in sup[0]["note"]
    assert len(sent) == 0, "抑制时不得敲门"


def test_thinking_false_stale_ts_knocks():
    ledger = _stuck_ledger()
    sent = []
    with _patch_liveness(entry=_live_entry(False, time.time() - 7200)), \
         mock.patch.object(reaper_mod, "_send_to_main",
                          lambda *a, **k: sent.append(a) or True):
        results = _call_stuck(ledger)
    assert not any(r.get("verdict") == "SUPPRESSED_ALIVE" for r in results)
    assert len(sent) == 1, "thinking=False + ts 陈旧应照敲"


def test_console_unreachable_knocks_fail_open():
    ledger = _stuck_ledger()
    sent = []
    with _patch_liveness(boom=True), \
         mock.patch.object(reaper_mod, "_send_to_main",
                          lambda *a, **k: sent.append(a) or True):
        results = _call_stuck(ledger)
    assert not any(r.get("verdict") == "SUPPRESSED_ALIVE" for r in results)
    assert len(sent) == 1, "console 不可达必须照敲 (fail-open)"


def test_no_matching_entry_knocks():
    ledger = _stuck_ledger()
    sent = []
    with _patch_liveness(entry=None), \
         mock.patch.object(reaper_mod, "_send_to_main",
                          lambda *a, **k: sent.append(a) or True):
        results = _call_stuck(ledger)
    assert not any(r.get("verdict") == "SUPPRESSED_ALIVE" for r in results)
    assert len(sent) == 1, "无该 session 条目应照敲"


def test_stale_thinking_true_still_suppresses():
    """thinking 粘滞 True (长工具调用空窗) → 即使 ts 陈旧也抑制。"""
    ledger = _stuck_ledger()
    sent = []
    with _patch_liveness(entry=_live_entry(True, time.time() - 7200)), \
         mock.patch.object(reaper_mod, "_send_to_main",
                          lambda *a, **k: sent.append(a) or True):
        results = _call_stuck(ledger)
    assert any(r.get("verdict") == "SUPPRESSED_ALIVE" for r in results)
    assert len(sent) == 0


def test_no_active_run_skips_entirely():
    row = _real_row({"task_id": "T-1", "session_id": "sid_w",
                     "profile": "coder"})
    ledger = _FakeLedger(active_run_id=None, current_card_id=None,
                        sess_row=row, last_progress_at=time.time() - 3600)
    results = _call_stuck(ledger)
    assert results == []


# ── 6~7. _notify_null_slot (FIX-4) ─────────────────────────────

def _null_slot_setup(tmpdir, *, dispatched: bool, run_age_s: float):
    """播种: ACTIVE run + 槽 NULL + 一张未结算卡。

    dispatched=True: 卡有 spawn marker + last_progress_at 陈旧 (真派发过);
    dispatched=False: 无 marker 且 last_progress_at NULL (从未派发的排队卡)。
    run 行 created_at 拉老 run_age_s 秒。
    """
    dbf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    dbf.close()
    from mw.ledger import Ledger
    led = Ledger(dbf.name)
    now = time.time()
    led.conn.execute(
        "INSERT INTO team_work_runs (run_id, initiator_session, initial_task, "
        "project_workspace, current_card_id, status, card_count, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("run_ns", "s", "task", str(tmpdir), None, "ACTIVE", 1,
         now - run_age_s))
    led.conn.execute(
        "INSERT INTO harvest_contracts (task_id, contract_json, harvest_status, "
        "pass_count, created_at, updated_at, team_work_run_id, last_progress_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("t_ns", json.dumps({"required_files": []}), "PENDING", 0,
         now - run_age_s, now - run_age_s, "run_ns",
         (now - 3600) if dispatched else None))
    led.conn.execute(
        "INSERT INTO task_sessions (task_id, session_id, profile, runner_kind, "
        "workspace, created_at) VALUES (?,?,?,?,?,?)",
        ("t_ns", "sess_ns", "coder", "api", str(tmpdir), now))
    led.conn.execute(
        "INSERT OR REPLACE INTO task_initiators (task_id, session_id, created_at) "
        "VALUES (?,?,?)", ("t_ns", "init_sess", now))
    led.conn.commit()
    if dispatched:
        reaper_mod.write_marker(str(tmpdir), "t_ns", started_at=now - 3600)
    return led, dbf.name


def test_null_slot_undispatched_recent_run_no_knock():
    """名下只有未派发排队卡 + run 刚建 → 不拿 launch 时刻误报闲置。"""
    with tempfile.TemporaryDirectory() as td:
        led, db = _null_slot_setup(td, dispatched=False, run_age_s=120)
        try:
            sent = []
            with mock.patch.object(reaper_mod, "_send_to_main",
                                  lambda *a, **k: sent.append(a) or True), \
                 mock.patch.object(reaper_mod, "_query_console_liveness",
                                  return_value=None):
                results = []
                reaper_mod._notify_null_slot(
                    led, led.get_active_team_work(), results,
                    registry={}, stuck_after_min=5, base=td)
            assert len(sent) == 0, f"run 仅 2min, 不该敲, got {sent}"
        finally:
            led.close()
            os.unlink(db)


def test_null_slot_all_undispatched_old_run_says_wait_dispatch():
    """全未派发 + run 行也陈旧 → 敲, 文案是「等待 main 派发」。"""
    with tempfile.TemporaryDirectory() as td:
        led, db = _null_slot_setup(td, dispatched=False, run_age_s=3600)
        try:
            sent = []
            with mock.patch.object(reaper_mod, "_send_to_main",
                                  lambda *a, **k: sent.append(a) or True), \
                 mock.patch.object(reaper_mod, "_query_console_liveness",
                                  return_value=None):
                reaper_mod._notify_null_slot(
                    led, led.get_active_team_work(), [],
                    registry={}, stuck_after_min=5, base=td)
            assert len(sent) == 1, f"run 闲置 1h 应敲, got {sent}"
            assert "等待 main 派发" in sent[0][1], sent[0][1]
            assert "worker 活跃可忽略" not in sent[0][1]
        finally:
            led.close()
            os.unlink(db)


def test_null_slot_dispatched_live_worker_suppressed():
    """已派发卡 + console 报活 → SUPPRESSED_ALIVE, 不敲。"""
    with tempfile.TemporaryDirectory() as td:
        led, db = _null_slot_setup(td, dispatched=True, run_age_s=3600)
        try:
            sent = []
            live = {"profile": "coder", "session_id": "sess_ns",
                   "thinking": True, "last_event_ts": time.time(),
                   "last_event_type": "tool.progress", "event_count": 9}
            with mock.patch.object(reaper_mod, "_send_to_main",
                                  lambda *a, **k: sent.append(a) or True), \
                 mock.patch.object(reaper_mod, "_query_console_liveness",
                                  return_value=live):
                results = []
                reaper_mod._notify_null_slot(
                    led, led.get_active_team_work(), results,
                    registry={}, stuck_after_min=5, base=td)
            assert len(sent) == 0, f"worker 活着不该敲, got {sent}"
            sup = [r for r in results
                   if r.get("verdict") == "SUPPRESSED_ALIVE"]
            assert len(sup) == 1, f"应记 SUPPRESSED_ALIVE, got {results}"
        finally:
            led.close()
            os.unlink(db)


def test_null_slot_dispatched_dead_worker_knocks():
    """已派发卡 + console 报死 (thinking=False + ts 陈旧) → 照敲。"""
    with tempfile.TemporaryDirectory() as td:
        led, db = _null_slot_setup(td, dispatched=True, run_age_s=3600)
        try:
            sent = []
            dead = {"profile": "coder", "session_id": "sess_ns",
                   "thinking": False, "last_event_ts": time.time() - 7200,
                   "last_event_type": "run.completed", "event_count": 9}
            with mock.patch.object(reaper_mod, "_send_to_main",
                                  lambda *a, **k: sent.append(a) or True), \
                 mock.patch.object(reaper_mod, "_query_console_liveness",
                                  return_value=dead):
                reaper_mod._notify_null_slot(
                    led, led.get_active_team_work(), [],
                    registry={}, stuck_after_min=5, base=td)
            assert len(sent) == 1, f"worker 挂死应敲, got {sent}"
            assert "兜底敲门" in sent[0][1]
        finally:
            led.close()
            os.unlink(db)


# ── 8. 死代码清除 ──────────────────────────────────────────────

def test_no_cross_process_sse_import_in_reaper_source():
    src = (ROOT / "mw" / "reaper.py").read_text()
    # 只看真实 import 语句 (注释里提及死代码名是允许的)
    import re as _re
    bad = [ln for ln in src.splitlines()
           if _re.match(r"\s*(from\s+\.sse_bridge\s+import|import\s+.*sse_bridge)", ln)
           and not ln.lstrip().startswith("#")]
    assert not bad, f"跨进程死代码 import 必须删除 (reaper 与 SSE 桥不同进程): {bad}"
    # 且不得在运行期动态引用 _worker_live。检查前先剥离三引号 docstring
    # (F3b 移植后 docstring 里允许提及死代码名作说明, 只有可执行代码禁止)。
    no_doc = _re.sub(r'"""[\s\S]*?"""', "", src)
    no_doc = "\n".join(ln for ln in no_doc.splitlines()
                      if not ln.lstrip().startswith("#"))
    assert "_worker_live" not in no_doc, \
        "可执行代码不得引用 _worker_live (跨进程内存不共享)"
