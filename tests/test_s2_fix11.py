"""s2-fix-11 测试 (2026-10-17, issue #11 reopen 修复回归)。

覆盖 (基于 docs/VERIFY-11-15.md §3 复现路径 + s1 修复建议):
  #11 (排队预览呈现真实队伍顺序, 含中途插卡):

    T11  核心 bug 复现: launch 4 张后 add_cards 追加 2 张,
         **没有任何卡执行过** → 旧 seq ASC 排序下追加卡恒排尾
         (verify §3.4 重现路径 step 4)。s2-fix-11 引入 plan_seq 列
         + add_cards(position=...), 允许指定『计划位』插入到中间。
         期望: 排队预览按 COALESCE(plan_seq, seq) 排, 中途插入卡
         可显式位于 launch 卡之间。
    T12  plan_seq 与 seq 双层不可变性: 重复 put_contract 不抹 plan_seq
         (与 seq 同样护栏, B-3/R-2 的 plan_seq 版本)。
    T13  plan_seq 与 seq 双层不可变性: 重复 register 不覆盖既有 plan_seq
         (B-3/R-2 的 plan_seq 版本)。
    T14  老数据零回归: 历史 run plan_seq 全为 NULL, 排序键仍退化为 seq,
         (seq IS NULL, seq, updated_at) → 与 s5 fix 一致 (AC11-3)。
    T15  GET 幂等 (AC2-3 / AC11-2): 同一 run 两次 GET 顺序逐位相同。
    T16  AC11-1 不破坏: 中途插入卡『已执行』时, 按 started_at 排在
         executed 段头部 (与 s5 T8 一致; plan_seq 仅作用在未执行段)。
    T17  AC11-4 不破坏: 全链路预览区与历史任务展开共用同源排序
         (api_team_work_get cards 数组, 单源排序)。
    T18  fractional position: position=2.5 等小数位插入中间, 验证
         INTEGER 改 REAL 后仍可『插队』(verify §3.4 推荐方案有效)。

不依赖任何外部 hermes / docker 资源; 纯 SQLite + Ledger + console API。
"""
import os
import sqlite3
import sys
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
    """建一对临时 db: kanban + ledger, 注入 console 顶层模块变量。"""
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
    # 先 Ledger() 一次初始化 schema
    _pre_led = Ledger(ledger_f.name)
    _pre_led.close()
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
              r.get("created_at"), r.get("started_at"),
              r.get("completed_at")))
    c.commit()
    c.close()


def stub_workers_registry():
    console.REGISTRY = "/tmp/_test_s2_fix11_registry.json"
    import json
    Path(console.REGISTRY).write_text(json.dumps(
        {"coder": {"api_url": "http://stub", "api_key": "x" * 32}}))


def stub_dispatch_recorder():
    """api_dispatch 替成只记录调用, 不真跑 spawn。"""
    calls = []
    original = console.api_dispatch

    def _stub(payload):
        calls.append(dict(payload))
        return {"ok": True, "accepted": True, "stub": True,
                "task_id": payload.get("task_id")}

    console.api_dispatch = _stub
    return calls, lambda: setattr(console, "api_dispatch", original)


def launch_test_run(steps):
    """建一个测试 run (stub dispatch), 返回 run_id."""
    calls, restore = stub_dispatch_recorder()
    stub_workers_registry()
    r = console.api_team_work_launch({
        "initial_task": "需求", "session_id": "s_s2",
        "steps": steps,
    })
    if not r.get("ok"):
        raise RuntimeError(f"launch 失败: {r}")
    return r["run_id"], calls, restore


# ============================================================
# T11: 核心 bug 复现 + plan_seq 修复验证
# ============================================================

def test_T11_add_cards_position_inserts_into_middle():
    """核心 bug: launch 4 张 (a/b/c/d, seq=1..4) → add_cards 追加 2 张
    (e/f), 全部未执行。旧 seq ASC 排序下 e/f 恒排尾; 新 plan_seq 允许
    add_cards(position=...) 指定『计划位』插到中间。

    期望视觉序: a, b, e(plan_seq=2.5), c, d, f(plan_seq=5.5)。
    即 plan_seq 2.5 在 seq=2 之后、seq=3 之前;
       plan_seq 5.5 在 seq=4 之后、seq=6 之前 (seq 仍 MAX+1)。
    """
    k_path, l_path, led = fresh_dual_db()
    try:
        # launch 4 张
        run_id, calls, restore = launch_test_run([
            {"step_id": f"s_{c}", "title": c.upper(), "task_id": f"t_{c}",
             "assignee": "coder"}
            for c in ["a", "b", "c", "d"]
        ])
        # 写 kanban (全部 PENDING, started_at=None)
        seed_kanban(k_path, [
            {"id": f"t_{c}", "title": c.upper(), "assignee": "coder",
             "status": "PENDING", "created_at": float(i)}
            for i, c in enumerate(["a", "b", "c", "d"], 1)
        ])
        # add_cards 追加 e (插到 b 与 c 之间) + f (插到 d 之后)
        ar = console.api_team_work_add_cards(run_id, {"cards": [
            {"task_id": "t_e", "assignee": "coder", "position": 2.5},
            {"task_id": "t_f", "assignee": "coder", "position": 5.5},
        ]})
        # 写 e/f kanban
        seed_kanban(k_path, [
            {"id": "t_e", "title": "E", "assignee": "coder",
             "status": "PENDING", "created_at": 5.0},
            {"id": "t_f", "title": "F", "assignee": "coder",
             "status": "PENDING", "created_at": 6.0},
        ])
        check("T11.1 add_cards ok + 两张都登记",
              ar.get("ok") is True
              and sorted(ar.get("registered", [])) == ["t_e", "t_f"],
              f"got {ar}")
        # 拉 GET 看视觉序
        out = console.api_team_work_get(run_id)
        ids = [c["id"] for c in out["cards"]]
        check("T11.2 plan_seq=2.5 插在 b(seq=2) 与 c(seq=3) 之间",
              ids == ["t_a", "t_b", "t_e", "t_c", "t_d", "t_f"],
              f"got {ids}")
        # seq 仍 MAX+1 (Issue#2 不变)
        e_seq = led.get_contract("t_e")["seq"]
        f_seq = led.get_contract("t_f")["seq"]
        check("T11.3 seq 仍 MAX+1 (e=5, f=6), 与 plan_seq 解耦",
              e_seq == 5 and f_seq == 6,
              f"got e={e_seq} f={f_seq}")
        # plan_seq 已落库
        e_plan = led.get_contract("t_e")["plan_seq"]
        f_plan = led.get_contract("t_f")["plan_seq"]
        check("T11.4 plan_seq 已落库 (e=2.5, f=5.5)",
              e_plan == 2.5 and f_plan == 5.5,
              f"got e={e_plan} f={f_plan}")
        # exec_index 严格 1..6
        idxs = [c["exec_index"] for c in out["cards"]]
        check("T11.5 exec_index 1..6 连续",
              idxs == list(range(1, 7)),
              f"got {idxs}")
        restore()
    finally:
        cleanup(k_path, l_path, led)


# ============================================================
# T12: put_contract 重复调用不抹 plan_seq (与 seq 同样护栏)
# ============================================================

def test_T12_put_contract_preserves_plan_seq():
    """B-3 / R-2 的 plan_seq 版本: put_contract 整行替换必须保留 plan_seq,
    否则每次 dispatch 都会把『计划位』抹成 NULL, #11 修复原地复发。
    """
    k_path, l_path, led = fresh_dual_db()
    try:
        led.create_team_work_run("r1", "sess_1", "需求")
        led.put_contract("t_a", {"required_files": []})
        led.register_team_work_card("r1", "t_a", plan_seq=3.5)
        plan_after_register = led.get_contract("t_a")["plan_seq"]
        check("T12.1 register 后 plan_seq 已写入",
              plan_after_register == 3.5,
              f"got {plan_after_register!r}")
        # 重复 put_contract (dispatch 内部同场景)
        led.put_contract("t_a", {"required_files": ["x.md"]})
        hc = led.get_contract("t_a")
        check("T12.2 put_contract 后 plan_seq 不变",
              hc["plan_seq"] == 3.5,
              f"before=3.5 after={hc['plan_seq']!r}")
        check("T12.3 run_id 也保留 (既有回归不变)",
              hc["team_work_run_id"] == "r1",
              f"got {hc['team_work_run_id']!r}")
        check("T12.4 seq 也保留 (既有回归不变)",
              hc["seq"] is not None,
              f"got {hc['seq']!r}")
    finally:
        cleanup(k_path, l_path, led)


# ============================================================
# T13: 重复 register 不覆盖既有 plan_seq
# ============================================================

def test_T13_register_does_not_overwrite_plan_seq():
    """重复 register (例如 launch 失败重发) 不应覆盖已写入的 plan_seq,
    与 seq 同口径用 COALESCE 保留 (B-3)。
    """
    k_path, l_path, led = fresh_dual_db()
    try:
        led.create_team_work_run("r1", "sess_1", "需求")
        led.put_contract("t_a", {"required_files": []})
        # 第一次 register: plan_seq=2.5
        led.register_team_work_card("r1", "t_a", plan_seq=2.5)
        a_seq_1 = led.get_contract("t_a")["seq"]
        a_plan_1 = led.get_contract("t_a")["plan_seq"]
        check("T13.1 首次 register 后 seq + plan_seq",
              a_seq_1 is not None and a_plan_1 == 2.5,
              f"seq={a_seq_1} plan_seq={a_plan_1}")
        # 第二次 register: 不重传 → plan_seq 应保留
        led.register_team_work_card("r1", "t_a")
        hc = led.get_contract("t_a")
        check("T13.2 重复 register 不抹 plan_seq (COALESCE 保留)",
              hc["plan_seq"] == 2.5,
              f"before=2.5 after={hc['plan_seq']!r}")
        check("T13.3 重复 register 不重写 seq (Issue#2 不变)",
              hc["seq"] == a_seq_1,
              f"before={a_seq_1} after={hc['seq']}")
        # 第三次 register: 显式传 None → 也应保留
        led.register_team_work_card("r1", "t_a", plan_seq=None)
        hc2 = led.get_contract("t_a")
        check("T13.4 显式 plan_seq=None 也不抹既有",
              hc2["plan_seq"] == 2.5,
              f"got {hc2['plan_seq']!r}")
        # 第四次 register: 显式传新值 9.9 → 应**不**覆盖 (COALESCE 保护)
        led.register_team_work_card("r1", "t_a", plan_seq=9.9)
        hc3 = led.get_contract("t_a")
        check("T13.5 已存在 plan_seq 不会被新 register 值覆盖",
              hc3["plan_seq"] == 2.5,
              f"got {hc3['plan_seq']!r} (expect 2.5)")
    finally:
        cleanup(k_path, l_path, led)


# ============================================================
# T14: 老数据零回归 — 历史 run 全部 plan_seq=NULL, 排序键退化为 seq
# ============================================================

def test_T14_legacy_run_null_plan_seq_falls_back_to_seq():
    """模拟历史 run: 全部 plan_seq IS NULL → 排序键 = seq ASC,
    与 s5 fix (#11 旧版) 完全一致 (AC11-3 零回归)。
    """
    k_path, l_path, led = fresh_dual_db()
    try:
        led.create_team_work_run("r1", "sess_1", "需求")
        # launch 4 张 (register 不传 plan_seq → 列 NULL)
        for i, tid in enumerate(["t_a", "t_b", "t_c", "t_d"], 1):
            led.put_contract(tid, {"required_files": []})
            led.register_team_work_card("r1", tid)
            # 显式验证 NULL
            ps = led.get_contract(tid)["plan_seq"]
            assert ps is None, f"plan_seq 应为 NULL, got {ps}"
        seed_kanban(k_path, [
            {"id": tid, "title": tid, "assignee": "coder",
             "status": "PENDING", "created_at": float(i)}
            for i, tid in enumerate(["t_a", "t_b", "t_c", "t_d"], 1)
        ])
        # 中途 add_cards 追加 (不传 position)
        console.api_team_work_add_cards("r1", {"cards": [
            {"task_id": "t_e", "assignee": "coder"},
            {"task_id": "t_f", "assignee": "coder"},
        ]})
        seed_kanban(k_path, [
            {"id": "t_e", "title": "E", "assignee": "coder",
             "status": "PENDING", "created_at": 5.0},
            {"id": "t_f", "title": "F", "assignee": "coder",
             "status": "PENDING", "created_at": 6.0},
        ])
        out = console.api_team_work_get("r1")
        ids = [c["id"] for c in out["cards"]]
        # 老行为: 追加卡恒排尾 (s5 fix T8 一致; 显式 NULL 退化)
        check("T14.1 plan_seq=NULL 时退化为 seq ASC (AC11-3 零回归)",
              ids == ["t_a", "t_b", "t_c", "t_d", "t_e", "t_f"],
              f"got {ids}")
        check("T14.2 全部 6 张 plan_seq 仍 NULL",
              all(led.get_contract(t)["plan_seq"] is None
                  for t in ["t_a","t_b","t_c","t_d","t_e","t_f"]))
    finally:
        cleanup(k_path, l_path, led)


# ============================================================
# T15: GET 幂等 (AC2-3 / AC11-2)
# ============================================================

def test_T15_double_get_is_idempotent_with_plan_seq():
    """同一 run 两次 GET → cards 顺序逐位相同 (AC2-3 / AC11-2)。
    即使中途混用 plan_seq 与 seq 排序, 也不得不一致。
    """
    k_path, l_path, led = fresh_dual_db()
    try:
        led.create_team_work_run("r1", "sess_1", "需求")
        for tid in ["t_a", "t_b", "t_c", "t_d"]:
            led.put_contract(tid, {"required_files": []})
            led.register_team_work_card("r1", tid)
        console.api_team_work_add_cards("r1", {"cards": [
            {"task_id": "t_e", "assignee": "coder", "position": 2.5},
            {"task_id": "t_f", "assignee": "coder", "position": 5.5},
        ]})
        seed_kanban(k_path, [
            {"id": tid, "title": tid, "assignee": "coder",
             "status": "PENDING", "created_at": float(i)}
            for i, tid in enumerate(
                ["t_a", "t_b", "t_c", "t_d", "t_e", "t_f"], 1)
        ])
        out1 = console.api_team_work_get("r1")
        out2 = console.api_team_work_get("r1")
        ids1 = [c["id"] for c in out1["cards"]]
        ids2 = [c["id"] for c in out2["cards"]]
        check("T15.1 plan_seq 介入后两次 GET 仍逐位一致 (AC2-3/AC11-2)",
              ids1 == ids2,
              f"1: {ids1} vs 2: {ids2}")
        check("T15.2 中途插卡后顺序为 a/b/e/c/d/f",
              ids1 == ["t_a", "t_b", "t_e", "t_c", "t_d", "t_f"],
              f"got {ids1}")
    finally:
        cleanup(k_path, l_path, led)


# ============================================================
# T16: AC11-1 不破坏 — 中途插入卡『已执行』时仍按 started_at 排 executed 段
# ============================================================

def test_T16_executed_inserted_card_still_sorted_by_started_at():
    """中途插入且已执行的卡, 按 started_at 排在 executed 段头部
    (s5 T8 的不破坏验证; plan_seq 仅作用在 unexecuted 段)。
    """
    k_path, l_path, led = fresh_dual_db()
    try:
        led.create_team_work_run("r1", "sess_1", "需求")
        for tid in ["t_a", "t_b", "t_c", "t_d"]:
            led.put_contract(tid, {"required_files": []})
            led.register_team_work_card("r1", tid)
        # add_cards 传 plan_seq=99.0 (远超 seq), 但实际『已执行』
        console.api_team_work_add_cards("r1", {"cards": [
            {"task_id": "t_e", "assignee": "coder", "position": 99.0},
        ]})
        # t_e『已执行』started_at 早于 t_a, 但 plan_seq 巨大
        seed_kanban(k_path, [
            {"id": "t_a", "title": "A", "assignee": "coder",
             "status": "PENDING", "created_at": 1.0},
            {"id": "t_b", "title": "B", "assignee": "coder",
             "status": "PENDING", "created_at": 1.1},
            {"id": "t_c", "title": "C", "assignee": "coder",
             "status": "PENDING", "created_at": 1.2},
            {"id": "t_d", "title": "D", "assignee": "coder",
             "status": "PENDING", "created_at": 1.3},
            {"id": "t_e", "title": "E", "assignee": "coder",
             "status": "done", "created_at": 0.5,
             "started_at": 0.5, "completed_at": 0.8},
        ])
        out = console.api_team_work_get("r1")
        ids = [c["id"] for c in out["cards"]]
        # t_e 进了 executed 段 (started_at=0.5), 应在最前; plan_seq 不应
        # 把已执行段拉到 executed 段的内部顺序上来
        check("T16.1 已执行的 e 仍在 executed 段头部 (AC11-1 不破)",
              ids[0] == "t_e",
              f"got {ids}")
        # 其余未执行按 plan_seq 排 (NULL 落 seq; a/b/c/d = seq 1..4)
        check("T16.2 未执行段按 seq 排 a/b/c/d (无 plan_seq)",
              ids[1:] == ["t_a", "t_b", "t_c", "t_d"],
              f"got {ids[1:]}")
    finally:
        cleanup(k_path, l_path, led)


# ============================================================
# T17: AC11-4 — 全链路预览区与历史任务展开共用同源 cards 数组
# ============================================================

def test_T17_single_source_cards_array_for_all_views():
    """api_team_work_get 返回的 cards 数组是单一排序源 (AC11-4);
    任何『预览区』『历史展开』消费 cards 数组即同源, 不可能不一致。
    """
    k_path, l_path, led = fresh_dual_db()
    try:
        led.create_team_work_run("r1", "sess_1", "需求")
        for tid in ["t_a", "t_b", "t_c"]:
            led.put_contract(tid, {"required_files": []})
            led.register_team_work_card("r1", tid)
        console.api_team_work_add_cards("r1", {"cards": [
            {"task_id": "t_e", "assignee": "coder", "position": 1.5},
        ]})
        seed_kanban(k_path, [
            {"id": tid, "title": tid, "assignee": "coder",
             "status": "PENDING", "created_at": float(i)}
            for i, tid in enumerate(["t_a", "t_b", "t_c", "t_e"], 1)
        ])
        out = console.api_team_work_get("r1")
        cards = out["cards"]
        # 单一源: 所有 cards 必须有 exec_index, 1..N 连续
        idxs = [c["exec_index"] for c in cards]
        check("T17.1 cards 数组 exec_index 1..4 严格连续 (AC11-4)",
              idxs == [1, 2, 3, 4],
              f"got {idxs}")
        # 单源校验: cards 是 list, 按位置即视觉序
        check("T17.2 cards[0]=t_a, cards[1]=t_e (plan_seq=1.5)",
              cards[0]["id"] == "t_a" and cards[1]["id"] == "t_e",
              f"got {[c['id'] for c in cards]}")
    finally:
        cleanup(k_path, l_path, led)


# ============================================================
# T18: fractional position — verify §3.4 推荐方案
# ============================================================

def test_T18_fractional_position_inserts_in_middle():
    """verify §3.4 推荐方案: position 是 REAL, 支持 2.5 等小数位插入中间。
    INTEGER 改 REAL 后允许『插队』, 验证这是有效路径。
    """
    k_path, l_path, led = fresh_dual_db()
    try:
        led.create_team_work_run("r1", "sess_1", "需求")
        for tid in ["t_a", "t_b", "t_c", "t_d"]:
            led.put_contract(tid, {"required_files": []})
            led.register_team_work_card("r1", tid)
        # 三张全中段插入 (1.5/2.5/3.5)
        console.api_team_work_add_cards("r1", {"cards": [
            {"task_id": "t_e", "assignee": "coder", "position": 1.5},
            {"task_id": "t_f", "assignee": "coder", "position": 2.5},
            {"task_id": "t_g", "assignee": "coder", "position": 3.5},
        ]})
        seed_kanban(k_path, [
            {"id": tid, "title": tid, "assignee": "coder",
             "status": "PENDING", "created_at": float(i)}
            for i, tid in enumerate(
                ["t_a","t_b","t_c","t_d","t_e","t_f","t_g"], 1)
        ])
        out = console.api_team_work_get("r1")
        ids = [c["id"] for c in out["cards"]]
        check("T18.1 三张中途插入全在中段 (a/e/b/f/c/g/d)",
              ids == ["t_a", "t_e", "t_b", "t_f", "t_c", "t_g", "t_d"],
              f"got {ids}")
        # seq 仍 MAX+1 (e=5, f=6, g=7), plan_seq=1.5/2.5/3.5
        for tid, plan in [("t_e", 1.5), ("t_f", 2.5), ("t_g", 3.5)]:
            hc = led.get_contract(tid)
            check(f"T18.2 {tid} plan_seq={plan} 落库",
                  hc["plan_seq"] == plan,
                  f"got {hc['plan_seq']!r}")
    finally:
        cleanup(k_path, l_path, led)


# ============================================================
# T19: AC11-1 不破 — 中途插入卡已执行 + plan_seq 同时存在 (混合场景)
# ============================================================

def test_T19_mixed_executed_and_unexecuted_with_plan_seq():
    """混合场景: 一部分中途插入卡已执行 (executed 段), 一部分未执行
    (plan_seq 介入未执行段), 一部分 plan_seq=NULL (旧 launch 卡)。
    验证三类卡在 GET 输出中各得其所。
    """
    k_path, l_path, led = fresh_dual_db()
    try:
        led.create_team_work_run("r1", "sess_1", "需求")
        for tid in ["t_a", "t_b", "t_c", "t_d"]:
            led.put_contract(tid, {"required_files": []})
            led.register_team_work_card("r1", tid)  # plan_seq=NULL
        # 中途插入: e 已执行 (kanban 设 started_at), f 未执行且 plan_seq=1.5
        console.api_team_work_add_cards("r1", {"cards": [
            {"task_id": "t_e", "assignee": "coder"},  # plan_seq=NULL
            {"task_id": "t_f", "assignee": "coder", "position": 1.5},
        ]})
        # 执行流: e 08:00 完成 (早于所有 launch 卡), f 没执行
        seed_kanban(k_path, [
            {"id": "t_a", "title": "A", "assignee": "coder",
             "status": "PENDING", "created_at": 1.0},
            {"id": "t_b", "title": "B", "assignee": "coder",
             "status": "PENDING", "created_at": 1.1},
            {"id": "t_c", "title": "C", "assignee": "coder",
             "status": "PENDING", "created_at": 1.2},
            {"id": "t_d", "title": "D", "assignee": "coder",
             "status": "PENDING", "created_at": 1.3},
            {"id": "t_e", "title": "E", "assignee": "coder",
             "status": "done", "created_at": 0.5,
             "started_at": 7.5, "completed_at": 8.0},
            {"id": "t_f", "title": "F", "assignee": "coder",
             "status": "PENDING", "created_at": 0.7},
        ])
        out = console.api_team_work_get("r1")
        ids = [c["id"] for c in out["cards"]]
        # 期望: e(已执行, 最早 7.5) → a, f(plan_seq=1.5), b, c, d
        check("T19.1 已执行 e 排前 (started_at=7.5), 未执行段 f 插在 a 后",
              ids == ["t_e", "t_a", "t_f", "t_b", "t_c", "t_d"],
              f"got {ids}")
    finally:
        cleanup(k_path, l_path, led)


# ============================================================
# 主入口
# ============================================================

def main():
    tests = [
        test_T11_add_cards_position_inserts_into_middle,
        test_T12_put_contract_preserves_plan_seq,
        test_T13_register_does_not_overwrite_plan_seq,
        test_T14_legacy_run_null_plan_seq_falls_back_to_seq,
        test_T15_double_get_is_idempotent_with_plan_seq,
        test_T16_executed_inserted_card_still_sorted_by_started_at,
        test_T17_single_source_cards_array_for_all_views,
        test_T18_fractional_position_inserts_in_middle,
        test_T19_mixed_executed_and_unexecuted_with_plan_seq,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            check(t.__name__, False,
                  f"EXC: {type(e).__name__}: {e}")

    total = len(PASS) + len(FAIL)
    print()
    if not FAIL:
        print(f"\033[92m=== ALL {total} S2-FIX-11 TESTS PASSED ===\033[0m")
        return 0
    else:
        print(f"\033[91m=== {len(FAIL)}/{total} FAILED ===\033[0m")
        for n, d in FAIL:
            print(f"  - {n}: {d}")
        return 1


if __name__ == "__main__":
    sys.exit(main())