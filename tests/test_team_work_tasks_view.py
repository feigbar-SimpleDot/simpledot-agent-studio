"""T4 api_tasks team_work 维度暴露测试 (2026-10-03)。

覆盖:
  1. api_tasks 返回值顶层含 team_work_active (dict | null) + team_work_active_count (int)
  2. 每条 task 含 team_work_run_id (str | null)
  3. 无 ACTIVE run → team_work_active=None, count=0
  4. 有 ACTIVE run → team_work_active 含 run_id/initiator/initial_task/
     project_workspace/current_card_id/card_count/created_at/status
  5. ACTIVE run 的 current_card_id 命中某条 task → 补 current_card_assignee
     /session/workspace/title/status/harvest_status
  6. legacy 卡 (没 team_work_run_id) → team_work_run_id=None, 不报错

不依赖外部资源; 用 sqlite3 临时建 mock kanban + mock ledger。
"""
import os
import sys
import sqlite3
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mw.console as console
from mw.ledger import Ledger

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    marker = "PASS" if cond else "FAIL"
    line = f"  {marker}  {name}"
    if detail:
        line += f"  {detail}"
    print(line)


def fresh_dual_db():
    """建一对临时 db: kanban + ledger, 都注入 console 顶层模块变量。"""
    kanban_f = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    kanban_f.close()
    ledger_f = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    ledger_f.close()
    # 建 minimal kanban 表 (console.api_tasks 用 SELECT)
    kc = sqlite3.connect(kanban_f.name)
    kc.executescript("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            assignee TEXT,
            status TEXT,
            block_kind TEXT,
            result TEXT,
            created_at REAL,
            started_at REAL,
            completed_at REAL
        );
    """)
    kc.commit()
    kc.close()
    console.KANBAN_DB = kanban_f.name
    console.LEDGER_DB = ledger_f.name
    return kanban_f.name, ledger_f.name, Ledger(ledger_f.name)


def cleanup(kanban_path, ledger_path, led):
    try:
        led.close()
    except Exception:
        pass
    for p in [kanban_path, ledger_path]:
        try:
            os.unlink(p)
        except OSError:
            pass


def seed_kanban(path, rows):
    """rows: list of dict with keys: id, title, assignee, status..."""
    c = sqlite3.connect(path)
    for r in rows:
        c.execute("""
            INSERT INTO tasks(id, title, assignee, status, block_kind, result,
                            created_at, started_at, completed_at)
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (r.get("id"), r.get("title"), r.get("assignee"),
              r.get("status"), r.get("block_kind"), r.get("result"),
              r.get("created_at"), r.get("started_at"),
              r.get("completed_at")))
    c.commit()
    c.close()


def test_team_work_active_none_when_no_run():
    """无 ACTIVE run → team_work_active=None, count=0。"""
    k_path, l_path, led = fresh_dual_db()
    try:
        seed_kanban(k_path, [{"id": "t_a", "title": "A", "assignee": "coder",
                            "status": "PENDING", "created_at": 1.0}])
        r = console.api_tasks()
        check("team_work_active is None when no run",
              r["team_work_active"] is None)
        check("team_work_active_count is 0 when no run",
              r["team_work_active_count"] == 0)
        check("tasks list has the row",
              len(r["tasks"]) == 1 and r["tasks"][0]["id"] == "t_a")
        check("legacy task has team_work_run_id=None",
              r["tasks"][0]["team_work_run_id"] is None)
    finally:
        cleanup(k_path, l_path, led)


def test_team_work_active_returns_run_summary():
    """有 ACTIVE run → 顶层 team_work_active 是 dict 含所有字段。"""
    k_path, l_path, led = fresh_dual_db()
    try:
        led.create_team_work_run("r1", "sess_x", "做一个 todo app",
                                  project_workspace="/share/turtle-app")
        led.put_contract("t_a", {"required_files": []})
        led.register_team_work_card("r1", "t_a")
        led.set_team_work_current_card("r1", "t_a")
        led.touch_progress("t_a")
        seed_kanban(k_path, [{"id": "t_a", "title": "建项目骨架",
                            "assignee": "coder",
                            "status": "PENDING", "created_at": 1.0}])
        # 加 session
        led.upsert_session("t_a", "20261003_S1", profile="coder",
                          runner_kind="docker",
                          workspace="/share/turtle-app/.tasks/t_a")
        r = console.api_tasks()
        a = r["team_work_active"]
        check("team_work_active is dict",
              isinstance(a, dict), f"got {type(a).__name__}")
        check("team_work_active.run_id == r1",
              a and a["run_id"] == "r1")
        check("initial_task echoed", a and a["initial_task"] == "做一个 todo app")
        check("project_workspace echoed",
              a and a["project_workspace"] == "/share/turtle-app")
        check("current_card_id == t_a",
              a and a["current_card_id"] == "t_a")
        check("current_card_assignee == coder",
              a and a["current_card_assignee"] == "coder")
        check("current_card_session_id present",
              a and a.get("current_card_session_id") == "20261003_S1")
        check("current_card_workspace present",
              a and "/share/turtle-app" in (a.get("current_card_workspace") or ""))
        check("current_card_title echoed",
              a and a["current_card_title"] == "建项目骨架")
        check("card_count == 1", a and a["card_count"] == 1)
        check("status == ACTIVE", a and a["status"] == "ACTIVE")
        check("team_work_active_count == 1", r["team_work_active_count"] == 1)
    finally:
        cleanup(k_path, l_path, led)


def test_team_work_run_id_attached_to_tasks():
    """每条 task 都透传 team_work_run_id (含历史 run 的卡)。"""
    k_path, l_path, led = fresh_dual_db()
    try:
        # 顺序: r1 ACTIVE → close → r2 ACTIVE。这样 api_tasks 看到的是 r2。
        led.create_team_work_run("r1", "s1", "需求A")
        led.put_contract("t_a", {"required_files": []})
        led.register_team_work_card("r1", "t_a")
        led.put_contract("t_b", {"required_files": []})
        led.register_team_work_card("r1", "t_b")
        led.set_team_work_current_card("r1", "t_a")
        led.close_team_work("r1", reason="user_aborted")

        # 现在能 create r2 了
        led.create_team_work_run("r2", "s2", "需求B")
        led.put_contract("t_c", {"required_files": []})
        led.register_team_work_card("r2", "t_c")
        led.set_team_work_current_card("r2", "t_c")

        # 另加一条无 run 的 legacy
        led.put_contract("t_legacy", {"required_files": []})

        seed_kanban(k_path, [
            {"id": "t_a", "title": "A", "assignee": "coder",
             "status": "PENDING", "created_at": 3.0},
            {"id": "t_b", "title": "B", "assignee": "coder",
             "status": "PENDING", "created_at": 2.0},
            {"id": "t_c", "title": "C", "assignee": "coder",
             "status": "ARCHIVED", "created_at": 1.5},
            {"id": "t_legacy", "title": "L", "assignee": "coder",
             "status": "PENDING", "created_at": 1.0},
        ])
        r = console.api_tasks()
        by_id = {t["id"]: t for t in r["tasks"]}
        check("t_a.team_work_run_id == r1",
              by_id["t_a"]["team_work_run_id"] == "r1")
        check("t_b.team_work_run_id == r1",
              by_id["t_b"]["team_work_run_id"] == "r1")
        check("t_c.team_work_run_id == r2 (current active)",
              by_id["t_c"]["team_work_run_id"] == "r2")
        check("t_legacy.team_work_run_id == None",
              by_id["t_legacy"]["team_work_run_id"] is None)
        check("team_work_active points to r2 (only ACTIVE)",
              r["team_work_active"] and r["team_work_active"]["run_id"] == "r2")
    finally:
        cleanup(k_path, l_path, led)


def test_no_kanban_table_does_not_explode():
    """极端: kanban 文件不存在或表不存在 → 不抛, 返回空 tasks + 顶层字段。"""
    k_path, l_path, led = fresh_dual_db()
    try:
        # 删 kanban 文件模拟表不存在
        os.unlink(k_path)
        # _ro_query 在文件不存在时 sqlite 会自己创建空 db, 返回 [] 不报错
        r = console.api_tasks()
        check("graceful empty tasks when no kanban",
              isinstance(r["tasks"], list))
        check("team_work_active still present (None)",
              "team_work_active" in r and r["team_work_active"] is None)
    finally:
        cleanup(k_path, l_path, led)


def test_legacy_payload_field_backward_compat():
    """回归保险 (issue #75 R1 更新口径): 列表白名单字段 + 详情端点补全。

    改造前此测试锁定 question/options/workspace 在列表; issue #75 R1 裁决
    将其移出列表 (瘦身投影), 全量字段改由 GET /api/tasks/<id> 提供。
    列表必须保留前端消费的字段, 详情必须补全被移走的字段。
    """
    k_path, l_path, led = fresh_dual_db()
    try:
        led.put_contract("t_a", {"required_files": []})
        led.touch_progress("t_a")
        seed_kanban(k_path, [{"id": "t_a", "title": "A", "assignee": "coder",
                            "status": "PENDING", "created_at": 1.0}])
        r = console.api_tasks()
        t = r["tasks"][0]
        check("harvest_status present", "harvest_status" in t)
        check("session_id present", "session_id" in t)
        check("team_work_run_id present", "team_work_run_id" in t)
        check("has_result present (R1 投影布尔)", "has_result" in t)
        check("R1: question/options/workspace/harvest_log 移出列表",
              all(k not in t for k in
                  ("question", "options", "workspace", "harvest_log")))
        d = console.api_task_detail("t_a")
        check("详情端点补全 question/options/session_id/workspace",
              all(k in d["task"] for k in
                  ("question", "options", "session_id", "workspace")))
        check("详情端点 404 未知 id",
              console.api_task_detail("t_missing").get("code") == 404)
    finally:
        cleanup(k_path, l_path, led)


def main():
    tests = [
        test_team_work_active_none_when_no_run,
        test_team_work_active_returns_run_summary,
        test_team_work_run_id_attached_to_tasks,
        test_no_kanban_table_does_not_explode,
        test_legacy_payload_field_backward_compat,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            check(t.__name__, False, f"EXC: {type(e).__name__}: {e}")
    total = len(PASS) + len(FAIL)
    print()
    if not FAIL:
        print(f"\033[92m=== ALL {total} T4 TESTS PASSED ===\033[0m")
        return 0
    else:
        print(f"\033[91m=== {len(FAIL)}/{total} FAILED ===\033[0m")
        for n, d in FAIL:
            print(f"  - {n}: {d}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
