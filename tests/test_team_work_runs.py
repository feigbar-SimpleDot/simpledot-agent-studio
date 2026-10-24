"""T1 回归测试:team_work_runs 表 + partial unique index + 8 个方法。

锁定以下行为(若日后被改坏,本测试会失败):
  1) 双层防护:active run 已存在时,create_team_work_run 抛 IntegrityError
  2) close 后能重新创建
  3) register_card 增 card_count;set/clear_current_card 读写 current_card_id
  4) pending_in_team_work 只返回属于该 run 的 PENDING/ERROR/BLOCKED 卡
  5) partial unique index 在 race 场景下挡住第二个 ACTIVE INSERT
  6) archive_team_work_cards 把未结算卡归 ARCHIVED,返回被归档 task_id 列表
  7) list_team_works 默认只看 ACTIVE,include_closed=True 含历史
"""
import sys
import sqlite3
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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
    """每次给一个临时 db,互不干扰。"""
    f = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    f.close()
    led = Ledger(f.name)
    return led, f.name


def test_double_layer_guard():
    """case 1: 应用层守卫在已有 ACTIVE 时拒绝第二次 create。"""
    led, path = fresh_ledger()
    try:
        r1 = led.create_team_work_run('r1', 's1', '需求1')
        check("r1 created ACTIVE", r1['status'] == 'ACTIVE')
        try:
            led.create_team_work_run('r2', 's2', '需求2')
            check("app-level guard blocked r2", False,
                  "second create_team_work_run should have raised")
        except sqlite3.IntegrityError as e:
            check("app-level guard blocked r2",
                  'r1' in str(e),
                  f"error message: {e!s}")
    finally:
        led.close()
        os.unlink(path)


def test_close_then_recreate():
    """case 2: close 后能立刻再开。

    A-4 适配: 旧断言 ABORTED 编码了「终态统一写 ABORTED」旧不变量。
    新语义下空 run 无遗留 → 自动判定 CLOSED (AC3-1)。
    """
    led, path = fresh_ledger()
    try:
        led.create_team_work_run('r1', 's1', '需求1')
        led.close_team_work('r1', 'test_reason')
        r1_closed = led.get_team_work('r1')
        check("r1 status=CLOSED after close (AC3-1 空 run 自动判定)",
              r1_closed['status'] == 'CLOSED',
              f"got {r1_closed['status']}")
        check("r1 closed_reason set",
              r1_closed['closed_reason'] == 'test_reason')
        check("r1 closed_at populated",
              r1_closed['closed_at'] is not None)
        # 再开 r2 应该成功
        r2 = led.create_team_work_run('r2', 's2', '需求2')
        check("r2 created after r1 closed",
              r2['status'] == 'ACTIVE')
    finally:
        led.close()
        os.unlink(path)


def test_card_count_and_current_card():
    """case 3: register_card 增计数;set/clear_current_card 推进槽位。"""
    led, path = fresh_ledger()
    try:
        led.create_team_work_run('r1', 's1', '需求1')
        led.register_team_work_card('r1', 't_a')
        led.register_team_work_card('r1', 't_b')
        led.register_team_work_card('r1', 't_c')
        r1 = led.get_team_work('r1')
        check("card_count=3 after 3 registers",
              r1['card_count'] == 3,
              f"got {r1['card_count']}")
        # set / clear current_card_id
        led.set_team_work_current_card('r1', 't_a')
        r1 = led.get_team_work('r1')
        check("current_card_id=t_a after set",
              r1['current_card_id'] == 't_a')
        led.clear_team_work_current_card('r1')
        r1 = led.get_team_work('r1')
        check("current_card_id=None after clear",
              r1['current_card_id'] is None)
        # 初始 current_card_id 是 None(新建的另一个 run)
        led.close_team_work('r1', 'transition_for_test')
        r2 = led.create_team_work_run('r2', 's2', '需求2')
        check("newly created run has current_card_id=None",
              r2['current_card_id'] is None)
    finally:
        led.close()
        os.unlink(path)


def test_register_card_backfills_harvest_contract():
    """case 3b: 若 task 已有 harvest_contracts 行,register_team_work_card 应回填 run_id。"""
    led, path = fresh_ledger()
    try:
        led.create_team_work_run('r1', 's1', '需求1')
        # 先建 contract(模拟 launch 顺序 A:先建 contract 再 register)
        led.put_contract('t_001', {'required_files': []})
        led.register_team_work_card('r1', 't_001')
        hc = led.get_contract('t_001')
        check("harvest_contracts.team_work_run_id backfilled",
              hc['team_work_run_id'] == 'r1',
              f"got {hc['team_work_run_id']!r}")
    finally:
        led.close()
        os.unlink(path)


def test_pending_in_team_work_filters():
    """case 4: pending_in_team_work 只返回属于该 run 的 PENDING/ERROR/BLOCKED。"""
    led, path = fresh_ledger()
    try:
        led.create_team_work_run('r1', 's1', '需求1')
        # 给 run1 塞 2 张 PENDING 卡
        led.put_contract('t_run1_a', {'required_files': []})
        led.put_contract('t_run1_b', {'required_files': []})
        led.register_team_work_card('r1', 't_run1_a')
        led.register_team_work_card('r1', 't_run1_b')
        # 孤儿 PENDING(无 team_work_run_id,不应被 r1 收)
        led.put_contract('t_orphan', {'required_files': []})
        # 跑一次 set_harvest 把 t_run1_b 标 ERROR(看能否穿透到 pending 列表)
        led.set_harvest('t_run1_b', 'ERROR', exit_code=1, log='boom')
        pending = led.pending_in_team_work('r1')
        task_ids = sorted([r['task_id'] for r in pending])
        check("pending_in_team_work returns run's PENDING+ERROR",
              task_ids == ['t_run1_a', 't_run1_b'],
              f"got {task_ids}")
        # 孤儿不在 r1 视野里
        check("orphan excluded",
              't_orphan' not in task_ids)
    finally:
        led.close()
        os.unlink(path)


def test_partial_unique_index_race():
    """case 5: DB 层 partial unique index 在 race 场景下阻挡双 ACTIVE。"""
    led, path = fresh_ledger()
    try:
        # 先建一条 r1(已经 ACTIVE)模拟「单进程先到」
        led.create_team_work_run('r1', 's1', '需求1')
        # 绕过 app 层(直接 conn)再插一条 rA = ACTIVE,期待 partial unique 兜底
        try:
            with led.conn:
                led.conn.execute(
                    "INSERT INTO team_work_runs (run_id, initiator_session, "
                    "initial_task, status, created_at) "
                    "VALUES ('rA', 's', 't', 'ACTIVE', 1.0)")
            check("DB-level partial unique blocked race", False,
                  "INSERT succeeded but should have been rejected")
        except sqlite3.IntegrityError as e:
            check("DB-level partial unique blocked race",
                  'UNIQUE' in str(e) or 'constraint' in str(e),
                  f"error: {e!s}")
    finally:
        led.close()
        os.unlink(path)


def test_archive_team_work_cards():
    """case 6: archive_team_work_cards 把 run 名下未结算卡标 ARCHIVED。"""
    led, path = fresh_ledger()
    try:
        led.create_team_work_run('r1', 's1', '需求1')
        led.put_contract('t_a', {'required_files': []})
        led.put_contract('t_b', {'required_files': []})
        led.put_contract('t_c', {'required_files': []})
        led.register_team_work_card('r1', 't_a')
        led.register_team_work_card('r1', 't_b')
        led.register_team_work_card('r1', 't_c')
        # t_b 改成 BLOCKED(应也归 ARCHIVED)
        led.set_harvest('t_b', 'BLOCKED', log='wait human')
        # t_c 改成 COMPLETE(应不被 archive 动)
        led.set_harvest('t_c', 'COMPLETE', exit_code=0)
        archived = led.archive_team_work_cards('r1')
        check("archive returns [t_a, t_b]",
              sorted(archived) == ['t_a', 't_b'],
              f"got {sorted(archived)}")
        # 复核 harvest_status
        ha = led.get_contract('t_a')['harvest_status']
        hb = led.get_contract('t_b')['harvest_status']
        hc = led.get_contract('t_c')['harvest_status']
        check("t_a archived", ha == 'ARCHIVED')
        check("t_b archived", hb == 'ARCHIVED')
        check("t_c untouched (still COMPLETE)", hc == 'COMPLETE')
        # close run 后再调 archive 应是 no-op
        led.close_team_work('r1', 'after_archive')
        archived2 = led.archive_team_work_cards('r1')
        check("archive is no-op after all cards handled",
              archived2 == [],
              f"got {archived2}")
    finally:
        led.close()
        os.unlink(path)


def test_list_team_works():
    """case 7: list_team_works 默认只看 ACTIVE;include_closed 含历史。"""
    led, path = fresh_ledger()
    try:
        led.create_team_work_run('r1', 's1', '需求1')
        led.close_team_work('r1', 'user_close')
        led.create_team_work_run('r2', 's2', '需求2')
        only_active = led.list_team_works(include_closed=False)
        all_runs = led.list_team_works(include_closed=True)
        check("list_team_works default returns ACTIVE only",
              [r['run_id'] for r in only_active] == ['r2'])
        check("list_team_works include_closed returns all",
              sorted(r['run_id'] for r in all_runs) == ['r1', 'r2'])
        # 按 created_at DESC 排序(最新在前)
        check("list_team_works ordered by created_at DESC",
              all_runs[0]['created_at'] >= all_runs[-1]['created_at'])
    finally:
        led.close()
        os.unlink(path)


def test_get_active_and_get_team_work():
    """case 8: get_active_team_work / get_team_work 边界。"""
    led, path = fresh_ledger()
    try:
        # 没有任何 run
        check("get_active_team_work() is None when empty",
              led.get_active_team_work() is None)
        check("get_team_work('nope') is None when missing",
              led.get_team_work('nope') is None)
        led.create_team_work_run('r1', 's1', '需求1')
        active = led.get_active_team_work()
        check("get_active_team_work() returns r1",
              active is not None and active['run_id'] == 'r1')
        got = led.get_team_work('r1')
        check("get_team_work('r1') returns row",
              got is not None and got['run_id'] == 'r1')
        led.close_team_work('r1', 'done')
        check("get_active_team_work() None after close",
              led.get_active_team_work() is None)
        check("get_team_work still works on closed (CLOSED per AC3-1)",
              led.get_team_work('r1')['status'] == 'CLOSED')
    finally:
        led.close()
        os.unlink(path)


def main():
    test_double_layer_guard()
    test_close_then_recreate()
    test_card_count_and_current_card()
    test_register_card_backfills_harvest_contract()
    test_pending_in_team_work_filters()
    test_partial_unique_index_race()
    test_archive_team_work_cards()
    test_list_team_works()
    test_get_active_and_get_team_work()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\n失败明细:")
        for name, detail in FAIL:
            print(f"  - {name}: {detail}")
        sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    main()
