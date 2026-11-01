"""issue #178 守护: 投递失败字段上列表常驻轮询面 (s6 / 组 D)。

覆盖 (PLAN-N2 §2.7 的 MV 牙齿):
  1. AC-178-1: 有当前卡且 notify_last_error 非空 → 轻量端点
     team_work_active.current_card_delivery_failed is True;
     notify_last_error 为空 → False (MV-178-a 的反向: 判据不是恒 False)。
  2. AC-178-4 / MV-178-c: 轻量端点不新增 DB 查询 —— mock _ro_query 计数,
     断言 harvest_contracts 查询条数不因新字段而增加 (既有 3 条: kanban
     当前卡 / task_sessions / harvest_contracts)。
  3. MV-178-b (同源纪律): _api_tasks_active() 与 api_tasks() 的
     team_work_active 键集逐字一致 (含新字段)。
  4. AC-178-3 (空态安全): 无 ACTIVE run → team_work_active=None, 两端点
     均不抛。
不依赖外部资源; 用 sqlite3 临时建 mock kanban + mock ledger
(与 tests/test_team_work_tasks_view.py 同口径)。

双口径断言 (s8 复测 P2-1 修复): 修复前 check() 只往 PASS/FAIL 列表 append
并 print, 永不 raise ⇒ 被 pytest 收集的 5 个用例恒绿 (守护空转)。
现在 check() 在 pytest 收集口径下失败即抛 AssertionError ⇒ 变异即转红;
脚本直跑口径 (main() 置 _COLLECT_ONLY=True) 仍只收集不抛, 逐条 10 断言
明细 + rc 汇总与 s6 原样 (不回退)。
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

# 口径开关 (s8 复测 P2-1 修复):
#   False = pytest 收集口径 —— check() 失败即 raise AssertionError, 守护有牙齿;
#   True  = 脚本直跑口径 (main() 置位) —— check() 只收集不抛, 逐条明细 + rc 汇总。
_COLLECT_ONLY = False


def check(name, cond, detail=""):
    """单条断言点: 收集 + 打印 + (pytest 口径) 失败即红。

    修复前本函数只 append/print, 永不 raise ⇒ 5 个被 pytest 收集的用例恒绿
    (s8 复测 P2-1: 变异 MV-178-a/b/c 在 pytest 口径全绿)。现改为函数内
    `assert cond, f"{name} {detail}"` 形态: 每个 check 调用点都是真实断言点。
    """
    (PASS if cond else FAIL).append((name, detail))
    marker = "PASS" if cond else "FAIL"
    line = f"  {marker}  {name}"
    if detail:
        line += f"  {detail}"
    print(line)
    # pytest 口径: 显式 assert ⇒ 变异即转红 (守护真正有牙齿)
    if not _COLLECT_ONLY:
        assert cond, f"{name} {detail}"


def fresh_dual_db():
    """建一对临时 db: kanban + ledger, 都注入 console 顶层模块变量。"""
    kanban_f = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    kanban_f.close()
    ledger_f = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    ledger_f.close()
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
    c = sqlite3.connect(path)
    for r in rows:
        c.execute("""
            INSERT INTO tasks(id, title, assignee, status, block_kind, result,
                            created_at, started_at, completed_at)
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (r.get("id"), r.get("title"), r.get("assignee"),
              r.get("status"), r.get("block_kind"), r.get("result"),
              r.get("created_at"), r.get("started_at"), r.get("completed_at")))
    c.commit()
    c.close()


def seed_active_run(led, k_path, run_id="r1", task_id="t_a"):
    """建 ACTIVE run + 当前卡 + kanban 行, 返回 task_id。"""
    led.create_team_work_run(run_id, "sess_x", "需求")
    led.put_contract(task_id, {"required_files": []})
    led.register_team_work_card(run_id, task_id)
    led.set_team_work_current_card(run_id, task_id)
    seed_kanban(k_path, [{"id": task_id, "title": "卡A", "assignee": "coder",
                          "status": "running", "created_at": 1.0}])
    return task_id


def test_delivery_failed_true_when_notify_error():
    """AC-178-1: notify_last_error 非空 → 轻量端点字段为 True (MV-178-a)。"""
    k_path, l_path, led = fresh_dual_db()
    try:
        tid = seed_active_run(led, k_path)
        led.record_notify_failure(tid, "relay 404", attempts=3,
                                  dead_until=0.0)
        r = console.api_tasks(fields="active")
        a = r["team_work_active"]
        check("active 端点: notify_last_error 非空 → True",
              a and a.get("current_card_delivery_failed") is True,
              f"got {a and a.get('current_card_delivery_failed')!r}")
        # 同源: 全量端点同值
        r2 = console.api_tasks()
        a2 = r2["team_work_active"]
        check("全量端点同值 True (同源纪律)",
              a2 and a2.get("current_card_delivery_failed") is True)
    finally:
        cleanup(k_path, l_path, led)


def test_delivery_failed_false_when_no_error():
    """反向: notify_last_error 为空 → False (字段不是恒 False 也不是恒 True)。"""
    k_path, l_path, led = fresh_dual_db()
    try:
        seed_active_run(led, k_path)
        r = console.api_tasks(fields="active")
        a = r["team_work_active"]
        check("无投递失败留痕 → False",
              a and a.get("current_card_delivery_failed") is False,
              f"got {a and a.get('current_card_delivery_failed')!r}")
        r2 = console.api_tasks()
        check("全量端点同值 False",
              r2["team_work_active"] and
              r2["team_work_active"].get("current_card_delivery_failed") is False)
    finally:
        cleanup(k_path, l_path, led)


def test_no_extra_db_query():
    """AC-178-4 / MV-178-c: 扩展既有查询取列, 不新增 harvest_contracts 查询。"""
    k_path, l_path, led = fresh_dual_db()
    orig = console._ro_query
    calls = []

    def counting(db, sql, args=()):
        calls.append(sql)
        return orig(db, sql, args)

    try:
        seed_active_run(led, k_path)
        console._ro_query = counting
        try:
            console.api_tasks(fields="active")
        finally:
            console._ro_query = orig
        hc = [s for s in calls if "harvest_contracts" in s]
        check("轻量端点 harvest_contracts 查询条数 == 1 (未新增)",
              len(hc) == 1, f"got {len(hc)}: {hc}")
        check("既有查询已扩展 SELECT notify_last_error",
              hc and "notify_last_error" in hc[0], f"got {hc}")
    finally:
        cleanup(k_path, l_path, led)


def test_two_endpoints_field_set_identical():
    """MV-178-b: 两端点 team_work_active 键集逐字一致 (docstring 承诺)。"""
    k_path, l_path, led = fresh_dual_db()
    try:
        tid = seed_active_run(led, k_path)
        led.record_notify_failure(tid, "boom", attempts=1, dead_until=0.0)
        led.upsert_session(tid, "S1", profile="coder",
                           runner_kind="docker", workspace="/w")
        a_light = console.api_tasks(fields="active")["team_work_active"]
        a_full = console.api_tasks()["team_work_active"]
        check("两端点键集逐字一致 (含 current_card_delivery_failed)",
              set(a_light) == set(a_full),
              f"light-only={set(a_light)-set(a_full)} full-only={set(a_full)-set(a_light)}")
        check("新字段在两侧都存在",
              "current_card_delivery_failed" in a_light
              and "current_card_delivery_failed" in a_full)
    finally:
        cleanup(k_path, l_path, led)


def test_two_endpoints_field_set_identical_without_contract_row():
    """issue #190: 「当前卡存在但不种 harvest_contracts 行」⇒ 两端点键集
    仍逐字一致 (修前分叉: 轻量端点缺 current_card_harvest_status /
    current_card_delivery_failed 两键, 全量端点有键)。

    边界形态 = 卡刚被 claim、契约行尚未落库的短暂窗口。
    """
    k_path, l_path, led = fresh_dual_db()
    try:
        # 只建 run + 占槽, **不** put_contract ⇒ 当前卡无 harvest_contracts 行
        led.create_team_work_run("r190", "sess_x", "需求")
        led.set_team_work_current_card("r190", "t_190")
        seed_kanban(k_path, [{"id": "t_190", "title": "卡B", "assignee": "coder",
                              "status": "running", "created_at": 1.0}])
        a_light = console.api_tasks(fields="active")["team_work_active"]
        a_full = console.api_tasks()["team_work_active"]
        check("无契约行边界: 两端点键集逐字一致",
              set(a_light) == set(a_full),
              f"light-only={set(a_light)-set(a_full)} "
              f"full-only={set(a_full)-set(a_light)}")
        check("无契约行边界: 两键在轻量端点存在 (缺省 None / False)",
              "current_card_harvest_status" in a_light
              and "current_card_delivery_failed" in a_light)
        check("无契约行边界: 轻量端点取缺省值 (None / False)",
              a_light.get("current_card_harvest_status") is None
              and a_light.get("current_card_delivery_failed") is False,
              f"got {a_light.get('current_card_harvest_status')!r} / "
              f"{a_light.get('current_card_delivery_failed')!r}")
    finally:
        cleanup(k_path, l_path, led)


def test_empty_state_safe():
    """AC-178-3: 无 ACTIVE run → team_work_active=None, 两端点不抛。"""
    k_path, l_path, led = fresh_dual_db()
    try:
        r = console.api_tasks(fields="active")
        check("轻量端点空态 team_work_active is None",
              r["team_work_active"] is None)
        r2 = console.api_tasks()
        check("全量端点空态 team_work_active is None",
              r2["team_work_active"] is None)
    finally:
        cleanup(k_path, l_path, led)


def main():
    """脚本直跑口径: 置 _COLLECT_ONLY=True ⇒ check() 只收集不抛。

    保留 s6 原口径: 逐条 10 断言 PASS/FAIL 明细 + 末尾 ALL PASSED / N FAILED
    汇总 + rc (0/1)。pytest 收集口径 (直接跑 pytest 该文件) 走 check() 内的
    assert 分支, 失败即红。
    """
    global _COLLECT_ONLY
    _COLLECT_ONLY = True
    tests = [
        test_delivery_failed_true_when_notify_error,
        test_delivery_failed_false_when_no_error,
        test_no_extra_db_query,
        test_two_endpoints_field_set_identical,
        # issue #190 新增: 「当前卡无 harvest_contracts 行」边界的同源纪律
        test_two_endpoints_field_set_identical_without_contract_row,
        test_empty_state_safe,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            check(t.__name__, False, f"EXC: {type(e).__name__}: {e}")
    total = len(PASS) + len(FAIL)
    print()
    if not FAIL:
        print(f"\033[92m=== ALL {total} #178 TESTS PASSED ===\033[0m")
        return 0
    else:
        print(f"\033[91m=== {len(FAIL)}/{total} FAILED ===\033[0m")
        for n, d in FAIL:
            print(f"  - {n}: {d}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
