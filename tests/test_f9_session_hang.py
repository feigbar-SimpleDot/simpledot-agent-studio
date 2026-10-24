"""F9 (2026-10-08): worker session 挂死检测 + sqlite3.Row .get() 修复.

覆盖:
  - F9.1 session 挂死 (SSE 静默 + 非 run.completed + 非 thinking +
    TODO.md=working) → 敲 main, verdict=SESSION_HUNG
  - F9.2~F9.6 反向: 每种"没挂"的形态都不误报
  - F9.7 _row_get 在真实 sqlite3.Row 上可用 (F8 用 dict-fake 漏掉的坑)
  - F9.8 挂死检测绝不改 run 状态 (main 是决策者, 非用户手动不关 run)

背景: 170 实测 run 2bf49f44757f4d9e / t_642b4be6 —— worker 的 LLM
turn 死在中间, SSE 流静默但 TODO.md 仍 'working', 现有 stuck(30min)/
timeout(32min) 反应太慢。F9 用 sse_bridge._worker_live 做快速挂死判定。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")  # 不会真连

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results: list[bool] = []


def _assert(name: str, cond: bool, msg: str = ""):
    if cond:
        print(f"  {PASS} {name}")
        results.append(True)
    else:
        print(f"  {FAIL} {name}: {msg}")
        results.append(False)


def _make_todo(ws: str, status: str):
    """在 ws 下写一份带『当前任务』→『状态』的 TODO.md。"""
    os.makedirs(ws, exist_ok=True)
    with open(os.path.join(ws, "TODO.md"), "w", encoding="utf-8") as f:
        f.write("## 当前任务\n")
        f.write("- 目标: 测试\n")
        f.write(f"- 状态: {status}\n")


def _run_hang(live_entry, todo_status, *, hang_after_s=300,
              session=None, card=None):
    """跑一次 _notify_main_of_session_hang, 返回 results_list。

    monkeypatch _send_to_main 成 no-op recorder, 让 SESSION_HUNG 分支
    能落 results (真实 send 会因 127.0.0.1:1 失败)。
    """
    from mw import reaper as reaper_mod
    from mw import sse_bridge
    sse_bridge._subscriptions.clear()
    sse_bridge._worker_live.clear()
    if live_entry is not None:
        prof = (session or {}).get("profile", "coder")
        sid = (session or {}).get("session_id", "sid_h")
        sse_bridge._worker_live[(prof, sid)] = live_entry

    sent = {"n": 0}

    def _fake_send(*a, **k):
        sent["n"] += 1
    orig_send = reaper_mod._send_to_main
    reaper_mod._send_to_main = _fake_send

    now = time.time()
    _card = card or {
        "task_id": "T-H", "harvest_status": "PENDING",
        "contract_json": "{}",
        "last_progress_at": now - 3600, "updated_at": now - 3600,
    }

    class _L:
        path = "/tmp/f9_ledger.db"
        def current_card_contract(self, run_id, card_id):
            return _card
        def should_notify(self, tid, *, min_interval_s):
            return True
        def get_session(self, tid):
            return session if session is not None else {
                "session_id": "sid_h", "profile": "coder",
                "workspace": "/tmp/f9_ws"}
        def get_initiator(self, tid):
            return "sid_main"
        def mark_notified(self, tid):
            pass
        def emit_event(self, *a, **k):
            pass

    results_list = []
    try:
        reaper_mod._notify_main_of_session_hang(
            _L(), "/tmp", results_list, registry=None,
            hang_after_s=hang_after_s,
            active_run_id="run_a", current_card_id="card_c")
    finally:
        reaper_mod._send_to_main = orig_send
    return results_list, sent


# ============================================================
# F9.1: 挂死被检出
# ============================================================
def test_f9_1_hang_detected():
    ws = tempfile.mkdtemp()
    _make_todo(ws, "working")
    now = time.time()
    live = {
        "session_id": "sid_h", "last_run_id": "run_a",
        "last_event_ts": now - 600,          # 10min 静默 > 300s
        "last_event_type": "message.delta",   # 非 run.completed
        "thinking": False, "event_count": 12,
    }
    session = {"session_id": "sid_h", "profile": "coder", "workspace": ws}
    res, sent = _run_hang(live, "working", session=session)
    hung = [r for r in res if r.get("verdict") == "SESSION_HUNG"]
    _assert("f9.1 挂死检出 SESSION_HUNG", len(hung) == 1,
            f"got {res}")
    _assert("f9.1 挂死触发了 send", sent["n"] == 1, f"sent={sent}")


# ============================================================
# F9.2~F9.6: 各种"没挂"形态不误报
# ============================================================
def test_f9_2_run_completed_not_hung():
    ws = tempfile.mkdtemp()
    _make_todo(ws, "working")
    now = time.time()
    live = {"session_id": "sid_h", "last_run_id": "run_a",
            "last_event_ts": now - 600,
            "last_event_type": "run.completed",  # 已收尾
            "thinking": False, "event_count": 12}
    session = {"session_id": "sid_h", "profile": "coder", "workspace": ws}
    res, sent = _run_hang(live, "working", session=session)
    hung = [r for r in res if r.get("verdict") == "SESSION_HUNG"]
    _assert("f9.2 run.completed 不算挂死", len(hung) == 0, f"got {res}")


def test_f9_3_thinking_not_hung():
    ws = tempfile.mkdtemp()
    _make_todo(ws, "working")
    now = time.time()
    live = {"session_id": "sid_h", "last_run_id": "run_a",
            "last_event_ts": now - 600,
            "last_event_type": "message.delta",
            "thinking": True, "event_count": 12}  # 还在推理
    session = {"session_id": "sid_h", "profile": "coder", "workspace": ws}
    res, sent = _run_hang(live, "working", session=session)
    hung = [r for r in res if r.get("verdict") == "SESSION_HUNG"]
    _assert("f9.3 thinking=True 不算挂死", len(hung) == 0, f"got {res}")


def test_f9_4_recent_event_not_hung():
    ws = tempfile.mkdtemp()
    _make_todo(ws, "working")
    now = time.time()
    live = {"session_id": "sid_h", "last_run_id": "run_a",
            "last_event_ts": now - 60,   # 60s < 300s 阈值
            "last_event_type": "message.delta",
            "thinking": False, "event_count": 12}
    session = {"session_id": "sid_h", "profile": "coder", "workspace": ws}
    res, sent = _run_hang(live, "working", session=session)
    hung = [r for r in res if r.get("verdict") == "SESSION_HUNG"]
    _assert("f9.4 静默未超阈值不算挂死", len(hung) == 0, f"got {res}")


def test_f9_5_todo_not_working_not_hung():
    ws = tempfile.mkdtemp()
    _make_todo(ws, "completed")  # 已交卷, 走正常结算
    now = time.time()
    live = {"session_id": "sid_h", "last_run_id": "run_a",
            "last_event_ts": now - 600,
            "last_event_type": "message.delta",
            "thinking": False, "event_count": 12}
    session = {"session_id": "sid_h", "profile": "coder", "workspace": ws}
    res, sent = _run_hang(live, "completed", session=session)
    hung = [r for r in res if r.get("verdict") == "SESSION_HUNG"]
    _assert("f9.5 TODO 非 working 不判挂死", len(hung) == 0, f"got {res}")


def test_f9_6_no_session_not_hung():
    res, sent = _run_hang(None, "working", session=None)
    hung = [r for r in res if r.get("verdict") == "SESSION_HUNG"]
    _assert("f9.6 无 session/live 不判挂死", len(hung) == 0, f"got {res}")


# ============================================================
# F9.7: _row_get 在真实 sqlite3.Row 上可用
#   (F8 用 dict-fake 漏掉的坑: sqlite3.Row 没有 .get())
# ============================================================
def test_f9_7_row_get_on_real_sqlite_row():
    from mw.reaper import _row_get
    from mw.ledger import Ledger
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        led = Ledger(path)
        led.create_team_work_run("r1", "s_main", "需求")
        led.put_contract("t1", {})
        led.register_team_work_card("r1", "t1")
        # 直接插一条 session 行 (profile/workspace 非空)
        led.conn.execute(
            "INSERT INTO task_sessions (task_id, session_id, profile, "
            "workspace, created_at) VALUES (?,?,?,?,?)",
            ("t1", "sid_real", "coder", "/tmp/real_ws", time.time()))
        led.conn.commit()
        row = led.get_session("t1")
        # 证明它是 sqlite3.Row 且没有 .get()
        import sqlite3
        _assert("f9.7 get_session 返回 sqlite3.Row",
                isinstance(row, sqlite3.Row), f"type={type(row)}")
        _assert("f9.7 sqlite3.Row 确实没有 .get()",
                not hasattr(row, "get"), "Row 不该有 .get()")
        # _row_get 能正确取到值
        _assert("f9.7 _row_get 取到 profile",
                _row_get(row, "profile") == "coder",
                f"got {_row_get(row, 'profile')}")
        _assert("f9.7 _row_get 取到 workspace",
                _row_get(row, "workspace") == "/tmp/real_ws")
        # 缺失列返回 default
        _assert("f9.7 _row_get 缺列返 default",
                _row_get(row, "nope", "D") == "D")
        # None row 返 default
        _assert("f9.7 _row_get(None) 返 default",
                _row_get(None, "profile", "D") == "D")
        led.close()
    finally:
        os.unlink(path)


# ============================================================
# F9.8: 挂死检测绝不改 run 状态
# ============================================================
def test_f9_8_hang_never_closes_run():
    from mw import reaper as reaper_mod
    from mw import sse_bridge
    from mw.ledger import Ledger
    sse_bridge._subscriptions.clear()
    sse_bridge._worker_live.clear()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        led = Ledger(path)
        led.create_team_work_run("r_hang", "s_main", "需求")
        led.put_contract("t_h", {})
        led.register_team_work_card("r_hang", "t_h")
        led.conn.execute(
            "UPDATE team_work_runs SET current_card_id='t_h' "
            "WHERE run_id='r_hang'")
        led.conn.execute(
            "INSERT INTO task_sessions (task_id, session_id, profile, "
            "workspace, created_at) VALUES (?,?,?,?,?)",
            ("t_h", "sid_hang", "coder", "/tmp/hang_ws", time.time()))
        led.conn.commit()
        ws = "/tmp/f9_8_ws"
        _make_todo(ws, "working")
        # 把 session workspace 指到真实 TODO
        led.conn.execute(
            "UPDATE task_sessions SET workspace=? WHERE task_id='t_h'",
            (ws,))
        led.conn.commit()
        now = time.time()
        sse_bridge._worker_live[("coder", "sid_hang")] = {
            "session_id": "sid_hang", "last_run_id": "r_hang",
            "last_event_ts": now - 600,
            "last_event_type": "message.delta",
            "thinking": False, "event_count": 12}

        orig_send = reaper_mod._send_to_main
        reaper_mod._send_to_main = lambda *a, **k: None
        try:
            reaper_mod._notify_main_of_session_hang(
                led, "/tmp", [], registry=None, hang_after_s=300,
                active_run_id="r_hang", current_card_id="t_h")
        finally:
            reaper_mod._send_to_main = orig_send
        run = led.get_team_work("r_hang")
        _assert("f9.8 挂死后 run 仍 ACTIVE (未 auto-close)",
                run["status"] == "ACTIVE", f"status={run['status']}")
        _assert("f9.8 挂死后 current_card_id 未被清",
                run["current_card_id"] == "t_h",
                f"card={run['current_card_id']}")
        led.close()
    finally:
        os.unlink(path)


if __name__ == "__main__":
    test_f9_1_hang_detected()
    test_f9_2_run_completed_not_hung()
    test_f9_3_thinking_not_hung()
    test_f9_4_recent_event_not_hung()
    test_f9_5_todo_not_working_not_hung()
    test_f9_6_no_session_not_hung()
    test_f9_7_row_get_on_real_sqlite_row()
    test_f9_8_hang_never_closes_run()
    total = len(results)
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{total} passed")
    sys.exit(0 if passed == total else 1)
