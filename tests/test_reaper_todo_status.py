"""reaper 状态字段决策表端到端测试。

模拟 worker 写 TODO.md 状态, reaper 应该按状态触发 harvest。
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from mw.ledger import Ledger
from mw.todo import (
    init_todo, TodoTask, write_status_atomic,
    STATUS_COMPLETED, STATUS_BLOCKED, STATUS_FAILED, STATUS_WORKING,
)


def _setup_workspace_and_ledger(tmpdir: Path):
    """建一个 workspace + ledger + 一份 task contract。"""
    ws = tmpdir / "workspace"
    ws.mkdir()
    ledger = Ledger(str(tmpdir / "ledger.db"))
    task_id = "t_probe01"
    ledger.record_submit("probe-key-01", task_id, {
        "title": "PROBE",
        "target": {"profile": "coder"},
        "input_spec": {"task_description": "echo ok"},
        "harvest": {"required_files": [], "timeout_seconds": 60},
        "idempotency_key": "probe-key-01",
    })
    ledger.put_contract(task_id, {
        "required_files": [], "timeout_seconds": 60,
    })
    ledger.upsert_session(task_id, None, "coder", "api", None, workspace=str(ws))
    return task_id, ws, ledger


def _make_todo(ws: Path, status: str = "进行中"):
    p = ws / "TODO.md"
    task = TodoTask(
        step_id="probe", title="PROBE", goal="test",
        assignee="coder", product_workspace=str(ws), status=status,
    )
    init_todo(p, "PROBE", task)
    return p


def test_completed_triggers_harvest(tmp_path):
    """TODO.md 写 completed → reaper 应判 todo_completed 触发 harvest。"""
    from mw.reaper import _reap_one_task

    task_id, ws, ledger = _setup_workspace_and_ledger(tmp_path)
    _make_todo(ws, status=STATUS_COMPLETED)

    # mock harvest 避免真去查 transcript
    captured = {}
    def fake_harvest(tid, **kw):
        captured["task_id"] = tid
        captured["spawn_result"] = kw.get("spawn_result")
        return {"verdict": "COMPLETE", "settled": True}

    row = ledger.conn.execute(
        "SELECT * FROM harvest_contracts WHERE task_id=?", (task_id,)).fetchone()
    row_dict = dict(row)
    results = []
    with patch("mw.reaper.harvest", fake_harvest):
        _reap_one_task(row_dict, ledger, "/tmp", str(tmp_path), 30, None, results)
    assert len(results) == 1
    r = results[0]
    assert r["trigger"] == "todo_completed"
    assert r["verdict"] == "COMPLETE"
    assert captured["spawn_result"] is None  # 不依赖 exit_code
    print("  PASS  test_completed_triggers_harvest")


def test_working_no_action(tmp_path):
    """TODO.md 写 working, marker.started_at 未超时 → reaper 不动。"""
    from mw.reaper import _reap_one_task

    task_id, ws, ledger = _setup_workspace_and_ledger(tmp_path)
    _make_todo(ws, status=STATUS_WORKING)
    # 写 marker 但 started_at 是现在 (未超时)
    marker_path = tmp_path / "spawn" / f"{task_id}.json"
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps({
        "started_at": time.time(),
        "workspace": str(ws),
    }))

    row = ledger.conn.execute(
        "SELECT * FROM harvest_contracts WHERE task_id=?", (task_id,)).fetchone()
    results = []
    with patch("mw.reaper.harvest", lambda *a, **kw: {"verdict": "X"}):
        _reap_one_task(dict(row), ledger, "/tmp", str(tmp_path), 30, None, results)
    assert results[0]["trigger"] == "todo_working"
    assert results[0]["verdict"] == "WAIT"
    print("  PASS  test_working_no_action")


def test_working_timeout_triggers_physical_reap(tmp_path):
    """状态=working 但 started_at 超过 timeout → 物理收尸。"""
    from mw.reaper import _reap_one_task

    task_id, ws, ledger = _setup_workspace_and_ledger(tmp_path)
    _make_todo(ws, status=STATUS_WORKING)
    marker_path = tmp_path / "spawn" / f"{task_id}.json"
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    # started_at = 2 小时前, timeout=60s, grace=30s → 已超时
    marker_path.write_text(json.dumps({
        "started_at": time.time() - 7200,
        "workspace": str(ws),
    }))

    captured = {}
    def fake_harvest(tid, **kw):
        captured["spawn_result"] = kw.get("spawn_result")
        captured["worker_status"] = kw.get("worker_status")
        return {"verdict": "FAILED"}

    row = ledger.conn.execute(
        "SELECT * FROM harvest_contracts WHERE task_id=?", (task_id,)).fetchone()
    results = []
    with patch("mw.reaper.harvest", fake_harvest):
        _reap_one_task(dict(row), ledger, "/tmp", str(tmp_path), 30, None, results)
    assert results[0]["trigger"] == "timeout"
    # 边界减法 (2026-10-08): 超时收尸归 FAILED (非 PHYSICAL_ERROR)
    assert results[0]["verdict"] == "FAILED"
    assert captured["worker_status"] == "timeout", captured
    assert captured["spawn_result"]["exit_code"] == 124
    assert "TIMEOUT" in captured["spawn_result"]["stderr"]
    print("  PASS  test_working_timeout_triggers_physical_reap")


def test_blocked_with_clarify_triggers_need_clarification(tmp_path):
    """状态=blocked + .clarify.json → harvest 走 NEED_CLARIFICATION。"""
    from mw.reaper import _reap_one_task

    task_id, ws, ledger = _setup_workspace_and_ledger(tmp_path)
    _make_todo(ws, status=STATUS_BLOCKED)
    (ws / ".clarify.json").write_text(json.dumps({"question": "?"}))

    captured = {}
    def fake_harvest(tid, **kw):
        captured["task_id"] = tid
        return {"verdict": "NEED_CLARIFICATION"}

    row = ledger.conn.execute(
        "SELECT * FROM harvest_contracts WHERE task_id=?", (task_id,)).fetchone()
    results = []
    with patch("mw.reaper.harvest", fake_harvest):
        _reap_one_task(dict(row), ledger, "/tmp", str(tmp_path), 30, None, results)
    assert results[0]["trigger"] == "todo_blocked"
    assert results[0]["verdict"] == "NEED_CLARIFICATION"
    assert captured["task_id"] == task_id
    print("  PASS  test_blocked_with_clarify_triggers_need_clarification")


def test_blocked_without_clarify_still_settles(tmp_path):
    """状态=blocked 但 .clarify.json 缺失 → 仍结算 (边界减法 2026-10-08:
    写序铁律保证 blocked 与 clarify 原子产生, 旧 WAIT-等下轮分支已删,
    clarify 缺失兜底结算, 不干等)。"""
    from mw.reaper import _reap_one_task

    task_id, ws, ledger = _setup_workspace_and_ledger(tmp_path)
    _make_todo(ws, status=STATUS_BLOCKED)
    # 不写 .clarify.json (模拟 worker 违约)

    row = ledger.conn.execute(
        "SELECT * FROM harvest_contracts WHERE task_id=?", (task_id,)).fetchone()
    results = []
    captured = {}
    def fake_harvest(tid, **kw):
        captured["task_id"] = tid
        captured["worker_status"] = kw.get("worker_status")
        return {"verdict": "NEED_CLARIFICATION"}
    with patch("mw.reaper.harvest", fake_harvest):
        _reap_one_task(dict(row), ledger, "/tmp", str(tmp_path), 30, None, results)
    assert results[0]["trigger"] == "todo_blocked"
    assert results[0]["verdict"] == "NEED_CLARIFICATION"
    assert captured.get("worker_status") == "blocked"
    print("  PASS  test_blocked_without_clarify_still_settles")


def test_failed_triggers_harvest(tmp_path):
    """状态=failed → harvest 收到 worker_status='failed' → verdict=FAILED。

    边界减法 (2026-10-08): failed 不再走物理验收落 PHYSICAL_ERROR。
    worker 自报 failed 是它自己的决策, harvest 如实上报 FAILED。
    """
    from mw.reaper import _reap_one_task

    task_id, ws, ledger = _setup_workspace_and_ledger(tmp_path)
    _make_todo(ws, status=STATUS_FAILED)

    row = ledger.conn.execute(
        "SELECT * FROM harvest_contracts WHERE task_id=?", (task_id,)).fetchone()
    results = []
    captured = {}
    def _fake_harvest(tid, **kw):
        captured["worker_status"] = kw.get("worker_status")
        return {"verdict": "FAILED"}
    with patch("mw.reaper.harvest", _fake_harvest):
        _reap_one_task(dict(row), ledger, "/tmp", str(tmp_path), 30, None, results)
    assert results[0]["trigger"] == "todo_failed"
    assert results[0]["verdict"] == "FAILED"
    assert captured["worker_status"] == "failed", captured
    print("  PASS  test_failed_triggers_harvest")


def test_no_marker_skip(tmp_path):
    """没 marker + 没 TODO.md → SKIP (跟现状兼容)。"""
    from mw.reaper import _reap_one_task

    task_id, ws, ledger = _setup_workspace_and_ledger(tmp_path)
    # 既不写 TODO.md 也不写 marker

    row = ledger.conn.execute(
        "SELECT * FROM harvest_contracts WHERE task_id=?", (task_id,)).fetchone()
    results = []
    with patch("mw.reaper.harvest", lambda *a, **kw: {"verdict": "X"}):
        _reap_one_task(dict(row), ledger, "/tmp", str(tmp_path), 30, None, results)
    assert results[0]["trigger"] == "no_marker"
    assert results[0]["verdict"] == "SKIP"
    print("  PASS  test_no_marker_skip")


def test_reaper_does_not_import_session_msg_count(tmp_path):
    """回归: reaper 不再 import 旧的 message_count 探测 (2026-10-05 改用
    session_last_msg_ts 时间戳路径, 无 ledger 写入)。"""
    import mw.reaper as r
    # 不该有 _lookup_session_via_api / _cross_check_session
    assert not hasattr(r, "_lookup_session_via_api"), \
        "_lookup_session_via_api 应该已删"
    assert not hasattr(r, "_cross_check_session"), \
        "_cross_check_session 应该已删"
    # reaper 不再 import 旧的 session_msg_count (F6 路径已替换)
    import mw.harvest as h
    assert not hasattr(h, "session_msg_count"), \
        "session_msg_count 已删, 改用 session_last_msg_ts"
    assert hasattr(h, "session_last_msg_ts"), \
        "session_last_msg_ts 必须存在 (F6 用)"
    print("  PASS  test_reaper_does_not_import_session_msg_count")


if __name__ == "__main__":
    import inspect
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    fail = 0
    for name, fn in tests:
        with tempfile.TemporaryDirectory() as td:
            try:
                fn(Path(td))
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}")
                fail += 1
            except Exception as e:
                print(f"  ERROR {name}: {e!r}")
                fail += 1
    if fail:
        print(f"\n{fail} failures")
        sys.exit(1)
    print(f"\nall {len(tests)} tests passed")
