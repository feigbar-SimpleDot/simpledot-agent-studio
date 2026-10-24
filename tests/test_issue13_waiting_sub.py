"""issue#13 (2026-10-15) subagent 误敲门修复 — 新增测试集。
测试矩阵映射 docs/issue13-env-20261015/architecture.md §9 / acceptance-criteria.md:
  W1a AC-A1 waiting_sub + 静默 10min → _notify_main_of_stuck 零 _send_to_main;
       TODO.md / harvest_status 不变; 无 worker_report.
  W1b AC-A1 waiting_sub + 静默 10min → _reap_one_task
       verdict=WAIT (trigger=todo_waiting_sub); subagent_wait_since 盖戳不覆盖.
  W1c AC-A1 waiting_sub + console live 陈旧/缺失 → _notify_main_of_session_hang
       不产生 SESSION_HUNG.
  W2  AC-A2 waiting_sub + 静默 > SUB_MIN 敲门消息含"子任务"+"resume";
       verdict=NOTIFIED; 立即二敲被 should_notify 去重; ledger 变 WAITING.
  W3  AC-A3 waiting_sub → completed / blocked / failed → reap 走既有路径;
       subagent_wait_since 戳清 NULL.
  W4  AC-A4 全量回归锚 (issue#9 guards / fix5 / fix12 等零回归, 由 CI 兜).
  W5  AC-A5 普通 working + 静默 > stuck threshold 走旧文案旧阈值 (零变化).
  W6  AC-A10 MW_REAPER_SUBAGENT_MIN=0 + waiting_sub → 无派生 / 无戳 / 走旧路径.
  W7  AC-A6 soul_template.zh-cn.md / .en.md / worker_contract_prompt 均含
       waiting_sub 规则与 "turn 结束≠任务结束" 语义.
  W8  AC-A7 derive_worker_state 三态:
        (a) 盖戳当前卡 → WAITING_SUB
        (b) 盖戳 + hs=WAITING → WAITING 优先
        (c) 非当前卡 → 不受影响 (走 WORKING 派生)
  W9  §5 waiting_sub 静默 ∈ (SUB_MIN, 2×SUB_MIN+grace) → 收尸未发生
       (敲门先于收尸).
"""
from __future__ import annotations
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")  # 不会真连
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mw.ledger import Ledger  # noqa: E402
from mw.todo import (  # noqa: E402
    init_todo, TodoTask, STATUS_COMPLETED, STATUS_WORKING, STATUS_FAILED,
    STATUS_BLOCKED, STATUS_WAITING_SUB, WAITING_SUB_ALIASES,
    parse_status,
)
import pytest  # noqa: E402
PASS = "\033[92mPASS\033[0m"
FAIL_R = "\033[91mFAIL\033[0m"
results: list[bool] = []
def _assert(name: str, cond: bool, msg: str = "") -> None:
    if cond:
        print(f"  {PASS} {name}")
        results.append(True)
    else:
        print(f"  {FAIL_R} {name}: {msg}")
        results.append(False)
@pytest.fixture(autouse=True)
def _no_transcript_export() -> None:
    with patch("mw.harvest.export_transcript", return_value=None), \
         patch("mw.harvest.fetch_transcript_api", return_value=None):
        yield


def _finalize_results() -> None:
    """issue#13 (s5b C2 修复): 收紧框架,把 results 的失败向上传播到 pytest。
    模块末尾 / fixture teardown 调一次,失败即 raise AssertionError,确保
    pytest 退出码与模块内部计数一致(否则 _assert 静默 append+print,CI 看不
    见失败)。"""
    total = len(results)
    passed = sum(1 for r in results if r)
    failed = [i for i, r in enumerate(results) if not r]
    print(f"\n[issue13] summary: {passed}/{total} passed")
    if failed:
        raise AssertionError(
            f"[issue13] {len(failed)}/{total} assertions failed "
            f"(indices={failed[:5]}{'...' if len(failed) > 5 else ''})")
def _make_todo(ws: Path, status: str) -> None:
    p = ws / "TODO.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    task = TodoTask(step_id="s", title="T", goal="g", assignee="coder",
                   product_workspace=str(ws), status=status)
    init_todo(p, "T", task)
def _setup(tmp_path: Path, *, run_id: str = "r13", task_id: str = "t13",
           ws_dirname: str = "ws", timeout_seconds: int = 60):
    """基础夹具: ws + ledger + card + session + initiator + 占槽."""
    ws = tmp_path / ws_dirname
    ws.mkdir()
    led = Ledger(str(tmp_path / "ledger.db"))
    led.create_team_work_run(run_id, "sid_main", "需求")
    led.put_contract(task_id,
                    {"required_files": [], "timeout_seconds": timeout_seconds})
    led.register_team_work_card(run_id, task_id)
    led.upsert_session(task_id, "sid_w", "coder", "api", None,
                      workspace=str(ws))
    led.record_initiator(task_id, "sid_main")
    led.set_team_work_current_card(run_id, task_id)
    return led, ws, task_id
# =============================================================
# W1a: AC-A1 — waiting_sub + 静默 10min → _notify_main_of_stuck 零敲
# =============================================================
def test_w1a_stuck_suppresses_on_waiting_sub() -> None:
    """worker TODO.md=waiting_sub, 静默 < SUB_MIN, _notify_main_of_stuck
    走专属换尺路径, threshold = sub_min*60 (默认 60min), 当前
    last_progress 仅 10min 前 → continue, 不产生 NOTIFIED。
    """
    from mw import reaper
    led, ws, tid = _setup(Path(tempfile.mkdtemp()))
    base = Path(led.path).parent
    try:
        _make_todo(ws, STATUS_WAITING_SUB)
        # 让 ledger.last_progress_at 看起来 10 分钟前
        led.conn.execute(
            "UPDATE harvest_contracts SET last_progress_at=? WHERE task_id=?",
            (time.time() - 600, tid))
        led.conn.commit()
        # 也盖一份旧戳证明盖戳幂等 (subagent_wait_since 应被 COALESCE 保留)
        led.conn.execute(
            "UPDATE harvest_contracts SET subagent_wait_since=? WHERE task_id=?",
            (time.time() - 800, tid))
        led.conn.commit()
        old_sw = led.conn.execute(
            "SELECT subagent_wait_since FROM harvest_contracts "
            "WHERE task_id=?", (tid,)).fetchone()["subagent_wait_since"]
        results_list: list[dict] = []
        with patch("mw.reaper._send_to_main") as send_mock, \
             patch("mw.reaper._query_console_liveness", return_value=None):
            reaper._notify_main_of_stuck(
                led, base, results_list, registry=None,
                stuck_after_min=30,  # 旧默认 30min, 用于对比
                active_run_id="r13",
                current_card_id=tid,
            )
        sent = [r for r in results_list
                if r.get("verdict") == "NOTIFIED"]
        _assert("W1a 静默 < SUB_MIN, _notify_main_of_stuck 零 NOTIFIED",
                len(sent) == 0,
                f"got {results_list}")
        _assert("W1a _send_to_main 未被调用",
                not send_mock.called,
                f"send_mock called={send_mock.called}")
        # TODO.md / harvest_status 不被改写
        cur_status = parse_status(ws / "TODO.md")
        _assert("W1a TODO.md 状态未被改写",
                cur_status and cur_status.strip().lower() == STATUS_WAITING_SUB,
                f"cur={cur_status!r}")
        row = led.conn.execute(
            "SELECT harvest_status FROM harvest_contracts "
            "WHERE task_id=?", (tid,)).fetchone()
        _assert("W1a harvest_status 仍 PENDING (未被改)",
                row["harvest_status"] == "PENDING",
                f"hs={row['harvest_status']}")
        # worker_report 字段不存在或保持 NULL
        wr = led.conn.execute(
            "SELECT worker_report FROM harvest_contracts "
            "WHERE task_id=?", (tid,)).fetchone()
        _assert("W1a worker_report 仍 NULL",
                wr["worker_report"] is None,
                f"worker_report={wr['worker_report']!r}")
        # subagent_wait_since 戳保持 (本测试只是敲门路径, 没走 _reap_one_task)
        sw_now = led.conn.execute(
            "SELECT subagent_wait_since FROM harvest_contracts "
            "WHERE task_id=?", (tid,)).fetchone()["subagent_wait_since"]
        _assert("W1a 敲门路径不动 subagent_wait_since",
                sw_now == old_sw,
                f"sw changed: {old_sw} -> {sw_now}")
    finally:
        led.close()
# =============================================================
# W1b: AC-A1 — waiting_sub + 静默 10min → _reap_one_task verdict=WAIT
# =============================================================
def test_w1b_reap_one_task_waiting_sub_under_threshold() -> None:
    """_reap_one_task 看到 waiting_sub + 静默 < SUB_MIN (默认 60min) →
    verdict=WAIT + trigger=todo_waiting_sub, 盖戳 (COALESCE 不覆盖),
    不 harvest / 不敲门 / 不收尸。
    """
    from mw import reaper
    tmp = Path(tempfile.mkdtemp())
    led, ws, tid = _setup(tmp)
    base = Path(led.path).parent
    try:
        _make_todo(ws, STATUS_WAITING_SUB)
        led.conn.execute(
            "UPDATE harvest_contracts SET last_progress_at=? WHERE task_id=?",
            (time.time() - 600, tid))
        led.conn.commit()
        results_list: list[dict] = []
        # D1 mock: ledger 必须有 update_worker_state (F5-4 守门);
        # 在 W1b 我们走 _reap_one_task, 不走 _notify_main_of_stuck,
        # 但 reap_once 顶层会创建 _L, 替身只暴露 _reap_one_task 需要的
        # 接口即可.
        with patch("mw.reaper.harvest") as harvest_mock, \
             patch("mw.reaper._send_to_main") as send_mock:
            reaper._reap_one_task(
                led.current_card_contract("r13", tid),
                led, str(base.parent), str(base), 120, None, results_list,
            )
        wait_results = [r for r in results_list
                        if r.get("trigger") == "todo_waiting_sub"]
        _assert("W1b _reap_one_task 触发 todo_waiting_sub",
                len(wait_results) == 1,
                f"results={results_list}")
        _assert("W1b verdict=WAIT",
                wait_results and wait_results[0].get("verdict") == "WAIT",
                f"verdict={wait_results[0].get('verdict') if wait_results else None}")
        _assert("W1b harvest 未被调用", not harvest_mock.called,
                f"harvest called={harvest_mock.called}")
        _assert("W1b _send_to_main 未被调用",
                not send_mock.called,
                f"send_mock called={send_mock.called}")
        # 盖戳幂等: 之前 NULL 现在应写; 再调一次, 戳不覆盖
        first = led.conn.execute(
            "SELECT subagent_wait_since FROM harvest_contracts "
            "WHERE task_id=?", (tid,)).fetchone()["subagent_wait_since"]
        _assert("W1b 首遇 waiting_sub 盖戳",
                first is not None,
                f"first=None")
        time.sleep(0.05)
        results_list2: list[dict] = []
        reaper._reap_one_task(
led.current_card_contract("r13", tid),
            led, str(base.parent), str(base), 120, None, results_list2,
        )
        second = led.conn.execute(
            "SELECT subagent_wait_since FROM harvest_contracts "
            "WHERE task_id=?", (tid,)).fetchone()["subagent_wait_since"]
        _assert("W1b 盖戳不覆盖 (COALESCE)",
                second == first,
                f"first={first} second={second}")
    finally:
        led.close()
# =============================================================
# W1c: AC-A1 — waiting_sub + console live 陈旧/缺失 → 不产生 SESSION_HUNG
# =============================================================
def test_w1c_session_hang_skips_waiting_sub() -> None:
    """worker 状态 waiting_sub, SSE 静默超 300s, console 端点返回陈旧
    (thinking=False, last_event_ts=now-500s) —— _notify_main_of_session_hang
    必须不产生 SESSION_HUNG verdict (waiting_sub 不在 in_progress_set,
    走 AC-A1 第三条).
    """
    from mw import reaper
    tmp = Path(tempfile.mkdtemp())
    led, ws, tid = _setup(tmp)
    base = Path(led.path).parent
    try:
        _make_todo(ws, STATUS_WAITING_SUB)
        results_list: list[dict] = []
        now = time.time()
        # console live 端点返回: 陈旧事件流 + thinking=False
        live_entry = {
            "profile": "coder",
            "session_id": "sid_w",
            "last_event_ts": now - 500,  # 超过 hang_after_s (300)
            "last_event_type": "tool.complete",
            "thinking": False,
            "event_count": 5,
        }
        with patch("mw.reaper._send_to_main") as send_mock, \
             patch("mw.reaper._query_console_liveness",
                   return_value=live_entry):
            reaper._notify_main_of_session_hang(
                led, base, results_list, registry=None,
                hang_after_s=300,
                active_run_id="r13",
                current_card_id=tid,
            )
        hung = [r for r in results_list
                if r.get("verdict") == "SESSION_HUNG"]
        _assert("W1c waiting_sub 不被 session_hang 误敲",
                len(hung) == 0,
                f"got {results_list}")
        _assert("W1c _send_to_main 未被调用",
                not send_mock.called)
    finally:
        led.close()
# =============================================================
# W2: AC-A2 — waiting_sub + 静默 > SUB_MIN → 敲门 (含 resume 建议)
# =============================================================
def test_w2_waiting_sub_over_threshold_knocks() -> None:
    """waiting_sub + 静默 > SUB_MIN (默认 60min) → 走专属文案敲门。
    文案包含 '子任务' 与 'resume' 关键词; verdict=NOTIFIED; 立即二敲
    被 should_notify 去重 (60min 窗口内只敲一次).
    """
    from mw import reaper
    tmp = Path(tempfile.mkdtemp())
    led, ws, tid = _setup(tmp)
    base = Path(led.path).parent
    try:
        _make_todo(ws, STATUS_WAITING_SUB)
        # 让 last_progress_at 在 70 分钟前, 超过默认 60min
        led.conn.execute(
            "UPDATE harvest_contracts SET last_progress_at=? WHERE task_id=?",
            (time.time() - 70 * 60, tid))
        led.conn.commit()
        results_list: list[dict] = []
        with patch("mw.reaper._send_to_main") as send_mock, \
             patch("mw.reaper._query_console_liveness", return_value=None), \
             patch.object(led, "update_worker_state") as _uws:
            reaper._notify_main_of_stuck(
                led, base, results_list, registry=None,
                stuck_after_min=30,
                active_run_id="r13",
                current_card_id=tid,
            )
        sent = [r for r in results_list
                if r.get("verdict") == "NOTIFIED"]
        _assert("W2 超阈 waiting_sub 敲一次",
                len(sent) == 1,
                f"results={results_list}")
        _assert("W2 _send_to_main 被调一次",
                send_mock.call_count == 1,
                f"calls={send_mock.call_count}")
        # 验证消息含 '子任务' + 'resume' 关键词
        msg = send_mock.call_args.args[1] if send_mock.call_args else ""
        _assert("W2 敲门文案含「子任务」",
                "子任务" in msg,
                f"msg={msg!r}")
        _assert("W2 敲门文案含「resume」",
                "resume" in msg.lower(),
                f"msg={msg!r}")
        _assert("W2 ledger.update_worker_state('WAITING') 被调",
                _uws.called,
                f"_uws.called={_uws.called}")
        # mark_notified 写 last_notified_at → 二敲被 should_notify 去重
        row = led.conn.execute(
            "SELECT last_notified_at FROM harvest_contracts "
            "WHERE task_id=?", (tid,)).fetchone()
        _assert("W2 last_notified_at 已写",
                row["last_notified_at"] is not None,
                f"lna={row['last_notified_at']!r}")
        # 二敲: should_notify 应该 False (60min 窗口内)
        _assert("W2 同卡一个 SUB_MIN 窗口内不再敲",
                led.should_notify(tid, min_interval_s=60 * 60) is False,
                f"should_notify=True")
    finally:
        led.close()
# =============================================================
# W3: AC-A3 — waiting_sub → completed/blocked/failed → reap 既有路径
# =============================================================
def test_w3a_waiting_sub_to_completed() -> None:
    """waiting_sub → 改 completed 后 reap: 走 completed 路径, 戳清 NULL.
    """
    from mw import reaper
    tmp = Path(tempfile.mkdtemp())
    led, ws, tid = _setup(tmp)
    base = Path(led.path).parent
    try:
        _make_todo(ws, STATUS_WAITING_SUB)
        # 模拟 reaper 已经盖戳 (此前有 waiting_sub 状态)
        led.conn.execute(
            "UPDATE harvest_contracts SET subagent_wait_since=? "
            "WHERE task_id=?", (time.time() - 100, tid))
        led.conn.commit()
        # worker 改 completed → 重新写 TODO.md
        _make_todo(ws, STATUS_COMPLETED)
        results_list: list[dict] = []
        with patch("mw.reaper.harvest",
                  return_value={"verdict": "COMPLETE", "settled": True}) as hv:
            reaper._reap_one_task(
                led.current_card_contract("r13", tid),
                led, str(base.parent), str(base), 120, None, results_list,
            )
        completed = [r for r in results_list
                     if r.get("trigger") == "todo_completed"]
        _assert("W3a 走 todo_completed 路径",
                len(completed) == 1,
                f"results={results_list}")
        _assert("W3a harvest 被调用",
                hv.called)
        sw = led.conn.execute(
            "SELECT subagent_wait_since FROM harvest_contracts "
            "WHERE task_id=?", (tid,)).fetchone()["subagent_wait_since"]
        _assert("W3a subagent_wait_since 戳清 NULL",
                sw is None,
                f"sw={sw!r}")
    finally:
        led.close()
def test_w3b_waiting_sub_to_failed() -> None:
    """waiting_sub → failed → reap 走 todo_failed 路径 + 清戳."""
    from mw import reaper
    tmp = Path(tempfile.mkdtemp())
    led, ws, tid = _setup(tmp)
    base = Path(led.path).parent
    try:
        _make_todo(ws, STATUS_WAITING_SUB)
        led.conn.execute(
            "UPDATE harvest_contracts SET subagent_wait_since=? "
            "WHERE task_id=?", (time.time() - 100, tid))
        led.conn.commit()
        _make_todo(ws, STATUS_FAILED)
        results_list: list[dict] = []
        with patch("mw.reaper.harvest",
                  return_value={"verdict": "FAILED", "settled": True}):
            reaper._reap_one_task(
led.current_card_contract("r13", tid),
                led, str(base.parent), str(base), 120, None, results_list,
            )
        failed = [r for r in results_list
                  if r.get("trigger") == "todo_failed"]
        _assert("W3b 走 todo_failed 路径",
                len(failed) == 1,
                f"results={results_list}")
        sw = led.conn.execute(
            "SELECT subagent_wait_since FROM harvest_contracts "
            "WHERE task_id=?", (tid,)).fetchone()["subagent_wait_since"]
        _assert("W3b subagent_wait_since 戳清 NULL", sw is None,
                f"sw={sw!r}")
    finally:
        led.close()
def test_w3c_waiting_sub_to_blocked() -> None:
    """waiting_sub → blocked → reap 走 todo_blocked 路径 + 清戳."""
    from mw import reaper
    tmp = Path(tempfile.mkdtemp())
    led, ws, tid = _setup(tmp)
    base = Path(led.path).parent
    try:
        _make_todo(ws, STATUS_WAITING_SUB)
        led.conn.execute(
            "UPDATE harvest_contracts SET subagent_wait_since=? "
            "WHERE task_id=?", (time.time() - 100, tid))
        led.conn.commit()
        _make_todo(ws, STATUS_BLOCKED)
        results_list: list[dict] = []
        with patch("mw.reaper.harvest",
                  return_value={"verdict": "NEED_CLARIFICATION",
                                "settled": True}):
            reaper._reap_one_task(
led.current_card_contract("r13", tid),
                led, str(base.parent), str(base), 120, None, results_list,
            )
        blocked = [r for r in results_list
                   if r.get("trigger") == "todo_blocked"]
        _assert("W3c 走 todo_blocked 路径",
                len(blocked) == 1,
                f"results={results_list}")
        sw = led.conn.execute(
            "SELECT subagent_wait_since FROM harvest_contracts "
            "WHERE task_id=?", (tid,)).fetchone()["subagent_wait_since"]
        _assert("W3c subagent_wait_since 戳清 NULL", sw is None,
                f"sw={sw!r}")
    finally:
        led.close()
def test_w3d_waiting_sub_to_working_clears_stamp() -> None:
    """waiting_sub → working → reap 走 todo_working 路径 + 清戳 (worker
    收齐子任务回到普通 working, 戳应清, 避免后续仍 WAITING_SUB).
    """
    from mw import reaper
    tmp = Path(tempfile.mkdtemp())
    led, ws, tid = _setup(tmp)
    base = Path(led.path).parent
    try:
        _make_todo(ws, STATUS_WAITING_SUB)
        led.conn.execute(
            "UPDATE harvest_contracts SET subagent_wait_since=? "
            "WHERE task_id=?", (time.time() - 100, tid))
        led.conn.commit()
        _make_todo(ws, STATUS_WORKING)
        results_list: list[dict] = []
        reaper._reap_one_task(
led.current_card_contract("r13", tid),
            led, str(base.parent), str(base), 120, None, results_list,
        )
        tw = [r for r in results_list
              if r.get("trigger") == "todo_working"]
        _assert("W3d working 走 todo_working",
                len(tw) == 1, f"results={results_list}")
        sw = led.conn.execute(
            "SELECT subagent_wait_since FROM harvest_contracts "
            "WHERE task_id=?", (tid,)).fetchone()["subagent_wait_since"]
        _assert("W3d subagent_wait_since 戳清 NULL", sw is None,
                f"sw={sw!r}")
    finally:
        led.close()
# =============================================================
# W5: AC-A5 — 普通卡 working + 静默 > stuck → 走旧文案旧阈值 (零变化)
# =============================================================
def test_w5_plain_working_unchanged() -> None:
    """普通卡 (TODO.md=working) + 静默 31min > stuck 30min → 走旧【mw 敲门】
    路径, threshold 不变 (stuck_after_min=30, 仍 30min). 文案不含
    「子任务」「resume」关键词 (仅 waiting_sub 才用专属文案).
    """
    from mw import reaper
    tmp = Path(tempfile.mkdtemp())
    led, ws, tid = _setup(tmp)
    base = Path(led.path).parent
    try:
        _make_todo(ws, STATUS_WORKING)
        # 让 last_progress 31 分钟前
        led.conn.execute(
            "UPDATE harvest_contracts SET last_progress_at=? WHERE task_id=?",
            (time.time() - 31 * 60, tid))
        led.conn.commit()
        results_list: list[dict] = []
        with patch("mw.reaper._send_to_main") as send_mock, \
             patch("mw.reaper._query_console_liveness", return_value=None):
            reaper._notify_main_of_stuck(
                led, base, results_list, registry=None,
                stuck_after_min=30,
                active_run_id="r13",
                current_card_id=tid,
            )
        sent = [r for r in results_list
                if r.get("verdict") == "NOTIFIED"]
        _assert("W5 普通 working 超 stuck 阈值敲一次",
                len(sent) == 1,
                f"results={results_list}")
        msg = send_mock.call_args.args[1] if send_mock.call_args else ""
        _assert("W5 文案是旧版【mw 敲门】",
                msg.startswith("【mw 敲门】"),
                f"msg={msg!r}")
        _assert("W5 文案不含「子任务」专属词",
                "子任务" not in msg,
                f"msg={msg!r}")
        _assert("W5 subagent_wait_since 戳保持 NULL (未盖)",
                led.conn.execute(
                    "SELECT subagent_wait_since FROM harvest_contracts "
                    "WHERE task_id=?", (tid,)).fetchone()[
                    "subagent_wait_since"] is None)
    finally:
        led.close()
# =============================================================
# W6: AC-A10 — MW_REAPER_SUBAGENT_MIN=0 + waiting_sub → 走旧路径
# =============================================================
def test_w6_env_zero_disables_incoming() -> None:
    """MW_REAPER_SUBAGENT_MIN=0 时, waiting_sub 卡走旧 stuck 路径:
    verdict=WAIT (旧), 不派生 WAITING_SUB, 不盖戳, 阈值回到旧
    stuck_after_min.
    s5b C4 修复: 单独用 timeout_seconds=1800 (生产默认), 避免契约
    60s + grace 120s 早于 reap 静默 5min 即触发 timeout 收尸,
    误把 todo_working 路径顶替掉.
    """
    from mw import reaper
    tmp = Path(tempfile.mkdtemp())
    led, ws, tid = _setup(tmp, timeout_seconds=1800)
    base = Path(led.path).parent
    try:
        _make_todo(ws, STATUS_WAITING_SUB)
        # 静默 5 分钟 (短于 stuck 30min 与 sub_min 60min, 都不应敲)
        led.conn.execute(
            "UPDATE harvest_contracts SET last_progress_at=? WHERE task_id=?",
            (time.time() - 5 * 60, tid))
        led.conn.commit()
        with patch.dict(os.environ, {"MW_REAPER_SUBAGENT_MIN": "0"}):
            results_list: list[dict] = []
            with patch("mw.reaper._send_to_main") as send_mock, \
                 patch("mw.reaper._query_console_liveness", return_value=None):
                reaper._notify_main_of_stuck(
                    led, base, results_list, registry=None,
                    stuck_after_min=30,
                    active_run_id="r13",
                    current_card_id=tid,
                )
            sent = [r for r in results_list
                    if r.get("verdict") == "NOTIFIED"]
            _assert("W6 开关=0 + 静默 < stuck, 不敲",
                    len(sent) == 0,
                    f"results={results_list}")
            _assert("W6 开关=0, _send_to_main 未被调",
                    not send_mock.called)
            # _reap_one_task 路径: 静默 5min, 不收尸, 走 todo_working
            # 旧 verdict (因 sub_enabled=False → 不走 waiting_sub 分支)
            results_list2: list[dict] = []
            reaper._reap_one_task(
led.current_card_contract("r13", tid),
                led, str(base.parent), str(base), 120, None, results_list2,
            )
            tw = [r for r in results_list2
                  if r.get("trigger") == "todo_working"]
            _assert("W6 开关=0, reap 走 todo_working (旧)",
                    len(tw) == 1,
                    f"results={results_list2}")
            _assert("W6 开关=0, 不派生 waiting_sub 触发",
                    not any(r.get("trigger") == "todo_waiting_sub"
                            for r in results_list2))
            sw = led.conn.execute(
                "SELECT subagent_wait_since FROM harvest_contracts "
                "WHERE task_id=?", (tid,)).fetchone()["subagent_wait_since"]
            _assert("W6 开关=0, 不盖 subagent_wait_since 戳",
                    sw is None,
                    f"sw={sw!r}")
    finally:
        led.close()
# =============================================================
# W7: AC-A6 — soul 模板双语 + pipeline prompt 含规则
# =============================================================
def test_w7_soul_and_prompt_have_waiting_sub_rules() -> None:
    """soul_template.zh-cn.md / soul_template.en.md / worker_contract_prompt
    三处均含 'waiting_sub' 字符串 + 'turn 结束 ≠ 任务结束' 语义.
    """
    zh = (ROOT / "mw" / "soul_template.zh-cn.md").read_text(encoding="utf-8")
    en = (ROOT / "mw" / "soul_template.en.md").read_text(encoding="utf-8")
    _assert("W7 zh soul 含 waiting_sub",
            "waiting_sub" in zh, f"zh not contains waiting_sub")
    _assert("W7 en soul 含 waiting_sub",
            "waiting_sub" in en, f"en not contains waiting_sub")
    _assert("W7 zh soul 含 'turn 结束' 语义",
            "turn 结束" in zh or "turn 结束 ≠ 任务结束" in zh,
            f"zh not contains turn 结束")
    _assert("W7 en soul 含 'turn ending' 语义",
            "turn ending" in en.lower(),
            f"en not contains turn ending")
    from mw import pipeline
    prompt = pipeline.worker_contract_prompt("/x/TODO.md", "/x", "/x")
    _assert("W7 pipeline prompt 含 waiting_sub",
            "waiting_sub" in prompt,
            f"prompt not contains waiting_sub")
    _assert("W7 pipeline prompt 含 'turn 结束' 语义",
            "turn 结束" in prompt or "turn 结束 ≠ 任务结束" in prompt)
# =============================================================
# W8: AC-A7 — derive_worker_state 三态
# =============================================================
def test_w8a_derive_waiting_sub_for_current_card() -> None:
    """当前承接卡 + subagent_wait_since 非空 + hs ∈ {PENDING, WORKING}
    → 派生 WAITING_SUB (tier=subagent_wait).
    """
    led = Ledger(str(Path(tempfile.mkdtemp()) / "ledger.db"))
    try:
        led.create_team_work_run("r_w8a", "s_main", "需求")
        led.put_contract("t_w8a", {"required_files": []})
        led.register_team_work_card("r_w8a", "t_w8a")
        led.set_team_work_current_card("r_w8a", "t_w8a")
        led.conn.execute(
            "UPDATE harvest_contracts SET subagent_wait_since=? "
            "WHERE task_id=?", (time.time() - 300, "t_w8a"))
        led.conn.commit()
        st, src = led.derive_worker_state("t_w8a", run_id="r_w8a")
        _assert("W8a 当前卡 + 盖戳 → WAITING_SUB",
                st == "WAITING_SUB",
                f"st={st} src={src}")
        _assert("W8a tier=subagent_wait",
                src.get("tier") == "subagent_wait",
                f"src={src}")
    finally:
        led.close()
def test_w8b_derive_waiting_overrides_waiting_sub() -> None:
    """当前承接卡 + subagent_wait_since 非空 + hs=WAITING → WAITING
    优先 (WAITING > WAITING_SUB).
    """
    led = Ledger(str(Path(tempfile.mkdtemp()) / "ledger.db"))
    try:
        led.create_team_work_run("r_w8b", "s_main", "需求")
        led.put_contract("t_w8b", {"required_files": []})
        led.register_team_work_card("r_w8b", "t_w8b")
        led.set_team_work_current_card("r_w8b", "t_w8b")
        led.update_worker_state("t_w8b", "WAITING", actor="reaper")
        led.conn.execute(
            "UPDATE harvest_contracts SET subagent_wait_since=? "
            "WHERE task_id=?", (time.time() - 300, "t_w8b"))
        led.conn.commit()
        st, src = led.derive_worker_state("t_w8b", run_id="r_w8b")
        _assert("W8b WAITING 盖过 WAITING_SUB",
                st == "WAITING",
                f"st={st} src={src}")
        _assert("W8b tier=waiting (passthrough)",
                src.get("tier") == "waiting",
                f"src={src}")
    finally:
        led.close()
def test_w8c_derive_non_current_card_unaffected() -> None:
    """非当前卡 (queued) + subagent_wait_since 非空 → 仍 PENDING
    (queued 路径不被 WAITING_SUB 影响, AC-A7).
    """
    led = Ledger(str(Path(tempfile.mkdtemp()) / "ledger.db"))
    try:
        led.create_team_work_run("r_w8c", "s_main", "需求")
        led.put_contract("t_q1", {"required_files": []})
        led.put_contract("t_q2", {"required_files": []})
        led.register_team_work_card("r_w8c", "t_q1")
        led.register_team_work_card("r_w8c", "t_q2")
        led.set_team_work_current_card("r_w8c", "t_q1")
        # t_q2 是排队卡, 即使有 subagent_wait_since 戳也应 PENDING
        led.conn.execute(
            "UPDATE harvest_contracts SET subagent_wait_since=? "
            "WHERE task_id=?", (time.time() - 300, "t_q2"))
        led.conn.commit()
        st, src = led.derive_worker_state("t_q2", run_id="r_w8c")
        _assert("W8c 排队卡不返 WAITING_SUB",
                st != "WAITING_SUB",
                f"st={st} src={src}")
        _assert("W8c 排队卡 = PENDING",
                st == "PENDING",
                f"st={st}")
    finally:
        led.close()
def test_w8d_derive_terminal_passthrough() -> None:
    """真终态 (HARVESTED) 不被 WAITING_SUB 派生覆盖 (tier=terminal
    passthrough 优先).
    """
    led = Ledger(str(Path(tempfile.mkdtemp()) / "ledger.db"))
    try:
        led.create_team_work_run("r_w8d", "s_main", "需求")
        led.put_contract("t_w8d", {"required_files": []})
        led.register_team_work_card("r_w8d", "t_w8d")
        led.update_worker_state("t_w8d", "HARVESTED", actor="harvest")
        led.conn.execute(
            "UPDATE harvest_contracts SET subagent_wait_since=? "
            "WHERE task_id=?", (time.time() - 300, "t_w8d"))
        led.conn.commit()
        st, src = led.derive_worker_state("t_w8d", run_id="r_w8d")
        _assert("W8d HARVESTED passthrough 优先",
                st == "HARVESTED",
                f"st={st} src={src}")
        _assert("W8d tier=terminal",
                src.get("tier") == "terminal",
                f"src={src}")
    finally:
        led.close()
# =============================================================
# W9: §5 — waiting_sub 静默 ∈ (SUB_MIN, 2×SUB_MIN+grace) 收尸未发生
# =============================================================
def test_w9_reap_not_fires_within_extended_window() -> None:
    """waiting_sub + 静默 90min (在 SUB_MIN=60 与 2*SUB_MIN+grace=122
    之间) → 不收尸, 只盖戳 + verdict=WAIT (敲门留给 _notify_main_of_stuck,
    本测试只验证 _reap_one_task 路径).
    """
    from mw import reaper
    tmp = Path(tempfile.mkdtemp())
    led, ws, tid = _setup(tmp, timeout_seconds=60)
    base = Path(led.path).parent
    try:
        _make_todo(ws, STATUS_WAITING_SUB)
        # 静默 90 分钟 (在 [60min, 122min) 之间)
        led.conn.execute(
            "UPDATE harvest_contracts SET last_progress_at=? WHERE task_id=?",
            (time.time() - 90 * 60, tid))
        led.conn.commit()
        results_list: list[dict] = []
        with patch("mw.reaper.harvest") as harvest_mock, \
             patch("mw.reaper._send_to_main") as send_mock:
            reaper._reap_one_task(
led.current_card_contract("r13", tid),
                led, str(base.parent), str(base), 120, None, results_list,
            )
        timeout_results = [r for r in results_list
                           if r.get("trigger") == "timeout"]
        _assert("W9 90min 静默 + waiting_sub 不收尸 (敲门先于收尸)",
                len(timeout_results) == 0,
                f"results={results_list}")
        _assert("W9 _reap_one_task 不调 harvest",
                not harvest_mock.called,
                f"harvest called={harvest_mock.called}")
        # 仍走 waiting_sub WAIT 触发
        ws_results = [r for r in results_list
                      if r.get("trigger") == "todo_waiting_sub"]
        _assert("W9 走 todo_waiting_sub verdict=WAIT",
                len(ws_results) == 1 and
                ws_results[0].get("verdict") == "WAIT",
                f"results={results_list}")
    finally:
        led.close()
# =============================================================
# W_extra: alias 兼容 (waiting-sub / 等待子任务 也算 waiting_sub)
# =============================================================
def test_w_extra_aliases_in_progress_set() -> None:
    """reaper 的 in_progress_set 接纳别名, 5min 兜底收尸不会误触."""
    from mw import reaper
    tmp = Path(tempfile.mkdtemp())
    led, ws, tid = _setup(tmp)
    base = Path(led.path).parent
    try:
        # 用 kebab-case 别名 waiting-sub
        _make_todo(ws, "waiting-sub")
        results_list: list[dict] = []
        with patch("mw.reaper.harvest") as harvest_mock:
            reaper._reap_one_task(
led.current_card_contract("r13", tid),
                led, str(base.parent), str(base), 120, None, results_list,
            )
        # 应走 waiting_sub 触发 (而不是 todo_missing_5min)
        triggers = [r.get("trigger") for r in results_list]
        _assert("W_extra 别名 'waiting-sub' 走 waiting_sub",
                "todo_waiting_sub" in triggers,
                f"triggers={triggers}")
        _assert("W_extra 别名不被 5min 兜底",
                "todo_missing_5min" not in triggers,
                f"triggers={triggers}")
        _assert("W_extra harvest 未被调",
                not harvest_mock.called)
    finally:
        led.close()
def test_w_extra_chinese_alias() -> None:
    """中文别名 等待子任务 同样被 in_progress_set 接纳."""
    from mw import reaper
    tmp = Path(tempfile.mkdtemp())
    led, ws, tid = _setup(tmp)
    base = Path(led.path).parent
    try:
        _make_todo(ws, "等待子任务")
        results_list: list[dict] = []
        reaper._reap_one_task(
led.current_card_contract("r13", tid),
            led, str(base.parent), str(base), 120, None, results_list,
        )
        triggers = [r.get("trigger") for r in results_list]
        _assert("W_extra 中文别名走 waiting_sub",
                "todo_waiting_sub" in triggers,
                f"triggers={triggers}")
    finally:
        led.close()
# =============================================================
# W_pixel: pixel_bridge WAITING_SUB 走 worker 忙碌族 (WORKING 同档)
# =============================================================
def test_w_pixel_bridge_treats_waiting_sub_as_worker_active() -> None:
    """像素桥: WAITING_SUB 派生下 worker_active=True (球在 worker
    不在 main), blocked=False (不是举手求助). 与 WAITING 不同.
    """
    from mw import pixel_bridge as pb
    # 直接验证 _BLOCK_STATUS 与 WORKING_ACTIVE 派生逻辑, 不发 sidecar.
    # 模拟 holder_state=WAITING_SUB 的场景
    held_holder = "WAITING_SUB"
    _assert("W_pixel WAITING_SUB 不在 _BLOCK_STATUS",
            held_holder not in pb._BLOCK_STATUS,
            f"got in _BLOCK_STATUS: {pb._BLOCK_STATUS}")
    # 派生语义: blocked 仅 = holder_state ∈ {BLOCKED, ERROR, NEED_CLARIFICATION}
    # 或 holder_state == 'WAITING', 不含 WAITING_SUB
    blocked_pred = (held_holder in pb._BLOCK_STATUS
                    and held_holder != "WAITING_SUB") \
                    or held_holder == "WAITING"
    _assert("W_pixel WAITING_SUB 不被 blocked",
            blocked_pred is False,
            f"got blocked_pred={blocked_pred}")
    # worker_active: WAITING_SUB 与 WORKING 同档
    worker_active = held_holder in ("WORKING", "WAITING_SUB")
    _assert("W_pixel WAITING_SUB = worker_active",
            worker_active is True)
# =============================================================
# W_console_salience: _DERIVED_SALIENCE 含 WAITING_SUB
# =============================================================
def test_w_console_salience_has_waiting_sub() -> None:
    """console._DERIVED_SALIENCE 词表含 'WAITING_SUB', 位置在
    'HANDOFF' 之后, 'NEED_CLARIFICATION' 之前 (worker 忙碌族内
    不与求助态同档).
    """
    from mw import console as cons
    assert hasattr(cons, "_DERIVED_SALIENCE"), "缺 _DERIVED_SALIENCE"
    _assert("W_salience 含 WAITING_SUB",
            "WAITING_SUB" in cons._DERIVED_SALIENCE,
            f"got {cons._DERIVED_SALIENCE}")
    idx_ws = cons._DERIVED_SALIENCE.index("WAITING_SUB")
    idx_ho = cons._DERIVED_SALIENCE.index("HANDOFF")
    idx_nc = cons._DERIVED_SALIENCE.index("NEED_CLARIFICATION")
    _assert("W_salience WAITING_SUB 在 HANDOFF 之后",
            idx_ws > idx_ho,
            f"positions: WAITING_SUB={idx_ws} HANDOFF={idx_ho}")
    _assert("W_salience WAITING_SUB 在 NEED_CLARIFICATION 之前",
            idx_ws < idx_nc,
            f"positions: WAITING_SUB={idx_ws} NEED_CLARIFICATION={idx_nc}")
# =============================================================
# W_threshold_env: 阈值 helper 与 env 容错
# =============================================================
def test_w_threshold_env_parsing() -> None:
    """_subagent_wait_min 默认 60, 0 = 停用, 非法回落 60, 负数回落 60.
    """
    from mw import reaper
    # default
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MW_REAPER_SUBAGENT_MIN", None)
        _assert("W_thr 默认 = 60",
                reaper._subagent_wait_min() == 60)
    # explicit positive
    with patch.dict(os.environ, {"MW_REAPER_SUBAGENT_MIN": "120"}):
        _assert("W_thr 显式 120",
                reaper._subagent_wait_min() == 120)
    # 0 = 停用
    with patch.dict(os.environ, {"MW_REAPER_SUBAGENT_MIN": "0"}):
        _assert("W_thr 0 = 0 (停用, 不回落 60)",
                reaper._subagent_wait_min() == 0)
    # 非法回落 60
    with patch.dict(os.environ, {"MW_REAPER_SUBAGENT_MIN": "abc"}):
        _assert("W_thr 非法值回落 60",
                reaper._subagent_wait_min() == 60)
    # 负数回落 60
    with patch.dict(os.environ, {"MW_REAPER_SUBAGENT_MIN": "-10"}):
        _assert("W_thr 负数回落 60",
                reaper._subagent_wait_min() == 60)
# =============================================================
# W_i18n_keys: i18n key 集合必须相等 (G1)
# =============================================================
def test_w_i18n_keys_pair() -> None:
    """reaper.why.waiting_sub_wait 与 console.state.waiting_sub 在
    zh-cn 与 en 两侧都存在.
    """
    zh = json.loads((ROOT / "mw" / "i18n" / "zh-cn.json").read_text())
    en = json.loads((ROOT / "mw" / "i18n" / "en.json").read_text())
    for k in ("reaper.why.waiting_sub_wait", "console.state.waiting_sub"):
        _assert(f"W_i18n zh 含 {k}", k in zh)
        _assert(f"W_i18n en 含 {k}", k in en)
        _assert(f"W_i18n zh[{k}] 非空",
                zh.get(k) and isinstance(zh[k], str) and len(zh[k]) > 0)
        _assert(f"W_i18n en[{k}] 非空",
                en.get(k) and isinstance(en[k], str) and len(en[k]) > 0)
# =============================================================
# W_aliases_constant: WAITING_SUB_ALIASES 集合
# =============================================================
def test_w_aliases_constant_in_todo() -> None:
    """mw.todo.WAITING_SUB_ALIASES = {waiting_sub, waiting-sub, 等待子任务}.
    """
    from mw import todo
    assert hasattr(todo, "WAITING_SUB_ALIASES"), \
        "todo 缺 WAITING_SUB_ALIASES"
    aliases = todo.WAITING_SUB_ALIASES
    _assert("W_aliases 包含 waiting_sub",
            "waiting_sub" in aliases)
    _assert("W_aliases 包含 waiting-sub",
            "waiting-sub" in aliases)
    _assert("W_aliases 包含 等待子任务",
            "等待子任务" in aliases)
# =============================================================
# 最后一行 summary (pytest 不消费 stdout, 用例失败仍各自 assert)
# =============================================================
def test_summary_print() -> None:
    """汇总所有 W 测试结果。s5b C2 修复: 不再仅辅助打印,而是 assert
    上抛让 pytest 退出码非零,确保框架内的失败与 pytest 报告一致."""
    _finalize_results()