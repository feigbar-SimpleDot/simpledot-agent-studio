"""issues #1/#2/#3 后端修复回归测试 (2026-10-09 impl-backend 交付)。

覆盖 DESIGN.md A/B 节新增用例 (上游: /share_workspace/projects/mw-issue-fixes):
  Issue #3 (close 终态判定):
    T-A1 全完成自动判定 → CLOSED (AC3-1/AC3-4, F4b 场景夹具)
    T-A2 有 PENDING 遗留 → ABORTED (AC3-2)
    T-A3 current_card_id 非空 → ABORTED (AC3-2, R-1)
    T-A4 显式 final_status 非法值 → ValueError (AC3-3)
    T-A5 非 ACTIVE run 调 close → None, 行不变 (幂等回归)
    T-A6 close 后 current_card_id 确认为 NULL (收编副作用)
  Issue #2 (卡注册顺序持久化):
    T-B1 GET 顺序 = launch 顺序; 首卡 set_harvest 后不变 (AC2-1)
    T-B2 put_contract 重复调用 → seq 不变 (B-3 护栏, R-2)
    T-B3 add_cards 追加 2 张 → 排在既有卡之后 (AC2-4)
    T-B4 连续两次 GET 顺序逐位相同 (AC2-3)
    T-B5 无 seq 旧库 → 迁移按 updated_at 回填, 重开不变 (AC2-2)
    T-B6 孤儿卡 (kanban 无行) 固定末尾 (AC2-5)

不依赖任何外部 hermes / docker 资源; 纯 SQLite + Ledger + console API。
"""
import os
import sqlite3
import sys
import tempfile
import time
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


def fresh_ledger():
    """新临时 db, 注入 mw.console.Ledger 模块级变量。"""
    f = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    f.close()
    console.LEDGER_DB = f.name
    return Ledger(f.name), f.name


def clear_ledger(led, path):
    try:
        led.close()
    except Exception:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass


def make_kanban_db(rows):
    """建 minimal kanban db (api_team_work_get 用), 返回 (path, cleanup_fn)。"""
    f = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    f.close()
    kc = sqlite3.connect(f.name)
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
    for r in rows:
        kc.execute(
            "INSERT INTO tasks(id, title, assignee, status, block_kind, "
            "result, created_at, started_at, completed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (r.get("id"), r.get("title"), r.get("assignee"),
             r.get("status"), r.get("block_kind"), r.get("result"),
             r.get("created_at"), r.get("started_at"), r.get("completed_at")))
    kc.commit()
    kc.close()
    return f.name, lambda: os.unlink(f.name)


def stub_dispatch_recorder():
    """api_dispatch 替成「只记录调用」; launch 需它不真 spawn。"""
    calls = []
    original = console.api_dispatch

    def _stub(payload):
        calls.append(dict(payload))
        return {"ok": True, "accepted": True, "stub": True,
                "task_id": payload.get("task_id")}

    console.api_dispatch = _stub
    return calls, lambda: setattr(console, "api_dispatch", original)


def stub_workers_registry():
    console.REGISTRY = "/tmp/_test_team_work_issue_fixes_registry.json"
    import json
    Path(console.REGISTRY).write_text(json.dumps(
        {"coder": {"api_url": "http://stub", "api_key": "x" * 32}}))


# ============================================================
# Issue #3: close 终态判定 (A-1/A-2/A-3)
# ============================================================

def test_a1_close_all_done_autodetects_closed():
    """T-A1 (AC3-1/AC3-4): 7 卡全 HARVESTED + current_card_id=NULL →
    close(reason='complete_all') 自动判定 → status='CLOSED' (F4b 场景夹具)。
    旧语义硬编码 ABORTED, 本用例即 P0 误报的回归。
    """
    led, path = fresh_ledger()
    try:
        led.create_team_work_run("r1", "s1", "需求")
        # F4b 场景夹具: 7 张卡全部结算完成
        for i in range(7):
            tid = f"t_{i}"
            led.put_contract(tid, {"required_files": []})
            led.register_team_work_card("r1", tid)
            led.set_harvest(tid, "HARVESTED", exit_code=0)
        led.clear_team_work_current_card("r1")
        result = led.close_team_work("r1", reason="complete_all")
        check("T-A1 close 返回终态 CLOSED",
              result == "CLOSED", f"got {result!r}")
        r1 = led.get_team_work("r1")
        check("T-A1 run.status=CLOSED", r1["status"] == "CLOSED",
              f"got {r1['status']}")
        check("T-A1 closed_reason=complete_all",
              r1["closed_reason"] == "complete_all",
              f"got {r1['closed_reason']!r}")
    finally:
        clear_ledger(led, path)


def test_a2_close_with_pending_residue_aborted():
    """T-A2 (AC3-2): run 名下有 1 张 PENDING → 自动判定 → 'ABORTED'。"""
    led, path = fresh_ledger()
    try:
        led.create_team_work_run("r1", "s1", "需求")
        led.put_contract("t_a", {"required_files": []})
        led.register_team_work_card("r1", "t_a")
        led.set_harvest("t_a", "HARVESTED", exit_code=0)
        led.put_contract("t_b", {"required_files": []})
        led.register_team_work_card("r1", "t_b")  # t_b 保持 PENDING
        led.clear_team_work_current_card("r1")
        result = led.close_team_work("r1", reason="user_close")
        check("T-A2 有 PENDING → ABORTED", result == "ABORTED",
              f"got {result!r}")
        r1 = led.get_team_work("r1")
        check("T-A2 run.status=ABORTED", r1["status"] == "ABORTED",
              f"got {r1['status']}")
    finally:
        clear_ledger(led, path)


def test_a3_close_with_current_card_set_aborted():
    """T-A3 (AC3-2/R-1): current_card_id 非空 (worker 在跑) + 无遗留
    → 自动判定 → 'ABORTED'。自动判定不得因无遗留误得 CLOSED。
    """
    led, path = fresh_ledger()
    try:
        led.create_team_work_run("r1", "s1", "需求")
        led.put_contract("t_a", {"required_files": []})
        led.register_team_work_card("r1", "t_a")
        # 不 clear 槽: current_card_id 仍 = t_a (模拟 worker 在跑)
        result = led.close_team_work("r1", reason="user_close")
        check("T-A3 current_card_id 非空 → ABORTED",
              result == "ABORTED", f"got {result!r}")
        r1 = led.get_team_work("r1")
        check("T-A3 run.status=ABORTED", r1["status"] == "ABORTED",
              f"got {r1['status']}")
    finally:
        clear_ledger(led, path)


def test_a4_close_rejects_invalid_final_status():
    """T-A4 (AC3-3): 显式 final_status='FOO' → ValueError; reason 语义保留。"""
    led, path = fresh_ledger()
    try:
        led.create_team_work_run("r1", "s1", "需求")
        try:
            led.close_team_work("r1", reason="test", final_status="FOO")
            check("T-A4 非法 final_status 应 raise ValueError", False,
                  "no exception raised")
        except ValueError as e:
            check("T-A4 非法 final_status → ValueError",
                  "final_status" in str(e), f"got {e!r}")
        # close 未执行 → run 仍 ACTIVE
        r1 = led.get_team_work("r1")
        check("T-A4 run 未被关 (仍 ACTIVE)", r1["status"] == "ACTIVE",
              f"got {r1['status']}")
    finally:
        clear_ledger(led, path)


def test_a5_close_non_active_run_is_noop():
    """T-A5 (AC3-3 幂等回归): 非 ACTIVE run 调 close → 返回 None, 行不变。"""
    led, path = fresh_ledger()
    try:
        led.create_team_work_run("r1", "s1", "需求")
        led.close_team_work("r1", reason="first")
        before = led.get_team_work("r1")
        r1 = led.create_team_work_run("r2", "s2", "需求2")
        # r2 是 ACTIVE; 对已终态 r1 再 close
        result = led.close_team_work("r1", reason="second")
        check("T-A5 非 ACTIVE close → None (未变更)", result is None,
              f"got {result!r}")
        after = led.get_team_work("r1")
        check("T-A5 行未变",
              after["status"] == before["status"]
              and after["closed_reason"] == before["closed_reason"],
              f"before={before['status']} after={after['status']}")
        check("T-A5 r2 不受影响",
              led.get_team_work("r2")["status"] == "ACTIVE")
    finally:
        clear_ledger(led, path)


def test_a6_close_clears_current_card_id():
    """T-A6: close 后 current_card_id 确认为 NULL (收编进 ledger 的副作用,
    原 console 裸 UPDATE 职责)。
    """
    led, path = fresh_ledger()
    try:
        led.create_team_work_run("r1", "s1", "需求")
        led.put_contract("t_a", {"required_files": []})
        led.register_team_work_card("r1", "t_a")
        led.set_team_work_current_card("r1", "t_a")
        result = led.close_team_work("r1", reason="test")
        check("T-A6 close 成功", result is not None, f"got {result!r}")
        r1 = led.get_team_work("r1")
        check("T-A6 current_card_id=NULL 已清",
              r1["current_card_id"] is None,
              f"got {r1['current_card_id']!r}")
    finally:
        clear_ledger(led, path)


# ============================================================
# Issue #2: 卡注册顺序持久化 (B-1/B-2/B-3/B-4)
# ============================================================

def launch_test_run(led, steps):
    """建一个测试 run (stub dispatch), 返回 (run_id, calls, restore, cleanup)。"""
    calls, restore = stub_dispatch_recorder()
    stub_workers_registry()
    r = console.api_team_work_launch({
        "initial_task": "t", "session_id": "s",
        "steps": steps,
    })
    if not r.get("ok"):
        check("launch 应成功", False, f"got {r}")
    return r["run_id"], calls, restore


def test_b1_get_order_stable_under_harvest_updates():
    """T-B1 (AC2-1): launch 3 卡 → GET 顺序 = steps 顺序; 把第 1 张
    set_harvest (updated_at 变最新) → GET 顺序不变。核心回归。
    """
    led, path = fresh_ledger()
    kanban_path, cleanup_kanban = make_kanban_db([
        {"id": "t_a", "title": "A", "assignee": "coder", "status": "PENDING",
         "created_at": 1.0},
        {"id": "t_b", "title": "B", "assignee": "coder", "status": "PENDING",
         "created_at": 1.1},
        {"id": "t_c", "title": "C", "assignee": "coder", "status": "PENDING",
         "created_at": 1.2},
    ])
    console.KANBAN_DB = kanban_path
    run_id, calls, restore = launch_test_run(led, [
        {"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"},
        {"step_id": "b", "title": "B", "task_id": "t_b", "assignee": "coder"},
        {"step_id": "c", "title": "C", "task_id": "t_c", "assignee": "coder"},
    ])
    try:
        g1 = console.api_team_work_get(run_id)
        order1 = [c["id"] for c in g1["cards"]]
        check("T-B1 初始 GET 顺序 = launch 顺序",
              order1 == ["t_a", "t_b", "t_c"], f"got {order1}")
        # 首卡已结算 (set_harvest 刷新 updated_at)
        led.set_harvest("t_a", "HARVESTED", exit_code=0)
        g2 = console.api_team_work_get(run_id)
        order2 = [c["id"] for c in g2["cards"]]
        check("T-B1 首卡 set_harvest 后 GET 顺序不变",
              order2 == ["t_a", "t_b", "t_c"], f"got {order2}")
    finally:
        restore()
        clear_ledger(led, path)
        cleanup_kanban()


def test_b2_put_contract_preserves_seq():
    """T-B2 (B-3 护栏/R-2): put_contract 对已登记卡重复调用 → seq 不变。
    整行替换若漏保留 seq, 每次 dispatch 都会把排序键打回 NULL。
    """
    led, path = fresh_ledger()
    try:
        led.create_team_work_run("r1", "s1", "需求")
        led.put_contract("t_a", {"required_files": []})
        led.register_team_work_card("r1", "t_a")
        seq_after_register = led.get_contract("t_a")["seq"]
        check("T-B2 register 后 seq 已写入",
              seq_after_register is not None,
              f"got {seq_after_register!r}")
        # 重复 put_contract (dispatch 内部同场景)
        led.put_contract("t_a", {"required_files": ["x.md"]})
        hc = led.get_contract("t_a")
        check("T-B2 put_contract 后 seq 不变",
              hc["seq"] == seq_after_register,
              f"before={seq_after_register!r} after={hc['seq']!r}")
        check("T-B2 run_id 也保留 (既有回归不变)",
              hc["team_work_run_id"] == "r1",
              f"got {hc['team_work_run_id']!r}")
    finally:
        clear_ledger(led, path)


def test_b3_add_cards_appended_after_existing():
    """T-B3 (AC2-4): add_cards 追加 2 张 → 排在既有卡之后, 既有顺序不变。"""
    led, path = fresh_ledger()
    kanban_path, cleanup_kanban = make_kanban_db([
        {"id": "t_a", "title": "A", "assignee": "coder", "status": "PENDING",
         "created_at": 1.0},
        {"id": "t_b", "title": "B", "assignee": "coder", "status": "PENDING",
         "created_at": 1.1},
        {"id": "t_x", "title": "X", "assignee": "coder", "status": "PENDING",
         "created_at": 1.2},
        {"id": "t_y", "title": "Y", "assignee": "coder", "status": "PENDING",
         "created_at": 1.3},
    ])
    console.KANBAN_DB = kanban_path
    run_id, calls, restore = launch_test_run(led, [
        {"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"},
        {"step_id": "b", "title": "B", "task_id": "t_b", "assignee": "coder"},
    ])
    try:
        r = console.api_team_work_add_cards(run_id, {"cards": [
            {"task_id": "t_x", "assignee": "coder"},
            {"task_id": "t_y", "assignee": "coder"},
        ]})
        check("T-B3 add_cards ok", r.get("ok") is True, f"got {r}")
        check("T-B3 两张都登记",
              sorted(r.get("registered", [])) == ["t_x", "t_y"],
              f"got {r.get('registered')}")
        g = console.api_team_work_get(run_id)
        order = [c["id"] for c in g["cards"]]
        check("T-B3 追加卡排尾部, 既有顺序不变",
              order == ["t_a", "t_b", "t_x", "t_y"], f"got {order}")
    finally:
        restore()
        clear_ledger(led, path)
        cleanup_kanban()


def test_b4_consecutive_gets_same_order():
    """T-B4 (AC2-3): 同一 run 连续两次 GET /api/team-work/<id> →
    cards 顺序逐位相同。"""
    led, path = fresh_ledger()
    kanban_path, cleanup_kanban = make_kanban_db([
        {"id": "t_a", "title": "A", "assignee": "coder", "status": "PENDING",
         "created_at": 1.0},
        {"id": "t_b", "title": "B", "assignee": "coder", "status": "PENDING",
         "created_at": 1.1},
        {"id": "t_c", "title": "C", "assignee": "coder", "status": "PENDING",
         "created_at": 1.2},
    ])
    console.KANBAN_DB = kanban_path
    run_id, calls, restore = launch_test_run(led, [
        {"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"},
        {"step_id": "b", "title": "B", "task_id": "t_b", "assignee": "coder"},
        {"step_id": "c", "title": "C", "task_id": "t_c", "assignee": "coder"},
    ])
    try:
        # 中间干扰: 只动 b 的 harvest_status (刷新其 updated_at)
        led.set_harvest("t_b", "ERROR", exit_code=1, log="boom")
        g1 = console.api_team_work_get(run_id)
        g2 = console.api_team_work_get(run_id)
        o1 = [c["id"] for c in g1["cards"]]
        o2 = [c["id"] for c in g2["cards"]]
        check("T-B4 两次 GET 逐位相同",
              o1 == o2 and o1 == ["t_a", "t_b", "t_c"],
              f"got {o1} / {o2}")
    finally:
        restore()
        clear_ledger(led, path)
        cleanup_kanban()


def test_b5_seq_migration_backfills_old_db():
    """T-B5 (AC2-2): 手工建无 seq 旧库 (updated_at 乱序, 行不写 seq 列),
    重新 Ledger() 打开 → seq 已按 updated_at ASC 回填, 重开不变。
    口径: REQ 已知风险#2 — updated_at 一次性快照, 此后不再变。
    """
    f = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    f.close()
    try:
        c = sqlite3.connect(f.name)
        c.executescript("""
            CREATE TABLE harvest_contracts (
                task_id         TEXT PRIMARY KEY,
                contract_json   TEXT NOT NULL,
                harvest_status  TEXT NOT NULL DEFAULT 'PENDING',
                exit_code       INTEGER,
                harvest_log     TEXT,
                pass_count      INTEGER NOT NULL DEFAULT 0,
                required_passes INTEGER,
                created_at      REAL NOT NULL,
                updated_at      REAL NOT NULL,
                worker_report   TEXT,
                team_work_run_id TEXT
            );
            CREATE TABLE team_work_runs (
                run_id              TEXT PRIMARY KEY,
                initiator_session   TEXT NOT NULL,
                initial_task        TEXT NOT NULL,
                current_card_id     TEXT,
                status              TEXT NOT NULL DEFAULT 'ACTIVE'
                CHECK(status IN ('ACTIVE','CLOSED','ABORTED')),
                card_count          INTEGER NOT NULL DEFAULT 0,
                created_at          REAL NOT NULL
            );
            CREATE TABLE task_sessions (
                task_id            TEXT PRIMARY KEY,
                profile            TEXT NOT NULL
            );
            CREATE TABLE task_initiators (
                task_id    TEXT PRIMARY KEY,
                session_id TEXT NOT NULL
            );
            -- 故意: 没有 seq 列 → 模拟旧库
        """)
        c.execute(
            "INSERT INTO team_work_runs (run_id, initiator_session, "
            "initial_task, status, card_count, created_at) "
            "VALUES ('r_old', 's', '老需求', 'ACTIVE', 3, 1.0)")
        # updated_at 乱序: t_c 最早, t_a 中间, t_b 最晚
        for tid, ts in [("t_c", 10.0), ("t_a", 20.0), ("t_b", 30.0)]:
            c.execute(
                "INSERT INTO harvest_contracts (task_id, contract_json, "
                "harvest_status, pass_count, created_at, updated_at, "
                "team_work_run_id) "
                "VALUES (?, '{}', 'PENDING', 0, ?, ?, 'r_old')",
                (tid, ts, ts))
        c.commit()
        c.close()

        led = Ledger(f.name)
        try:
            rows = led.conn.execute(
                "SELECT task_id, seq FROM harvest_contracts "
                "WHERE team_work_run_id='r_old' "
                "ORDER BY seq").fetchall()
            order = [r["task_id"] for r in rows]
            check("T-B5 迁移回填按 updated_at ASC",
                  order == ["t_c", "t_a", "t_b"], f"got {order}")
            seqs = [r["seq"] for r in rows]
            check("T-B5 seq 为 1,2,3", seqs == [1, 2, 3],
                  f"got {seqs}")
            # 重开: 迁移幂等, seq 不变
            led.close()
            led2 = Ledger(f.name)
            rows2 = led2.conn.execute(
                "SELECT task_id, seq FROM harvest_contracts "
                "WHERE team_work_run_id='r_old' "
                "ORDER BY seq").fetchall()
            check("T-B5 重开后 seq 不变",
                  [(r["task_id"], r["seq"]) for r in rows2]
                  == [(r["task_id"], r["seq"]) for r in rows])
        finally:
            led.close()
    finally:
        os.unlink(f.name)


def test_b6_orphan_card_fixed_last():
    """T-B6 (AC2-5): 孤儿卡 (ledger 有行 + run 归属, kanban 无行) 在
    GET 结果中固定末尾, 不参与主序。
    """
    led, path = fresh_ledger()
    kanban_path, cleanup_kanban = make_kanban_db([
        {"id": "t_a", "title": "A", "assignee": "coder", "status": "PENDING",
         "created_at": 1.0},
        {"id": "t_b", "title": "B", "assignee": "coder", "status": "PENDING",
         "created_at": 1.1},
    ])
    console.KANBAN_DB = kanban_path
    run_id, calls, restore = launch_test_run(led, [
        {"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"},
        {"step_id": "b", "title": "B", "task_id": "t_b", "assignee": "coder"},
    ])
    try:
        # 孤儿: 有 contract + run 归属, 但 kanban 表无行
        led.put_contract("t_orph", {"required_files": []})
        led.register_team_work_card(run_id, "t_orph")
        g = console.api_team_work_get(run_id)
        cards = g["cards"]
        check("T-B6 孤儿在结果里 (orphan=true)",
              any(c.get("orphan") is True and c["id"] == "t_orph"
                  for c in cards),
              f"got {[c['id'] for c in cards]}")
        check("T-B6 孤儿固定末尾",
              cards[-1]["id"] == "t_orph", f"got {[c['id'] for c in cards]}")
        check("T-B6 主序不受影响",
              [c["id"] for c in cards[:2]] == ["t_a", "t_b"],
              f"got {[c['id'] for c in cards]}")
    finally:
        restore()
        clear_ledger(led, path)
        cleanup_kanban()


# ============================================================
# 主入口
# ============================================================

def main():
    tests = [
        test_a1_close_all_done_autodetects_closed,
        test_a2_close_with_pending_residue_aborted,
        test_a3_close_with_current_card_set_aborted,
        test_a4_close_rejects_invalid_final_status,
        test_a5_close_non_active_run_is_noop,
        test_a6_close_clears_current_card_id,
        test_b1_get_order_stable_under_harvest_updates,
        test_b2_put_contract_preserves_seq,
        test_b3_add_cards_appended_after_existing,
        test_b4_consecutive_gets_same_order,
        test_b5_seq_migration_backfills_old_db,
        test_b6_orphan_card_fixed_last,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            check(t.__name__, False, f"EXC: {type(e).__name__}: {e}")

    total = len(PASS) + len(FAIL)
    print()
    if not FAIL:
        print(f"\033[92m=== ALL {total} TESTS PASSED ===\033[0m")
        return 0
    print(f"\033[91m=== {len(FAIL)}/{total} FAILED ===\033[0m")
    for n, d in FAIL:
        print(f"  - {n}: {d}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
