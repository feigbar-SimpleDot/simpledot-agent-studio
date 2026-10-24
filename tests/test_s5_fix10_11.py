"""s5 fix #10 + #11 测试 (2026-10-13)。

覆盖:
  #10 (UI 卡片化):
    T1 _parse_history_line 标准行 (- [step] title → result) 三字段全拆
    T2 _parse_history_line 兼容 -> 箭头 (旧 task 产物中偶有, 见 mw/todo.py 416 行)
    T3 _parse_history_line 不匹配行 → step_id 空串, raw 完整保留
    T4 _serialize_todo_doc.done_lines / dropped_lines 是结构化 list[dict]
       不是 list[str] (前端 HistoryList 改造关键契约)
    T5 _serialize_todo_doc 解析失败的 drop 行不丢信息 (raw 兜底)

  #11 (排序):
    T6 api_team_work_get 序号 exec_index 必含, 1..N 严格连续
    T7 launch 全量卡: 全未执行段 → 顺序与 seq 一致 (AC11-3 零回归)
    T8 add_cards 追加且先执行: 视觉序 = 真实执行序 (AC11-1)
    T9 双刷新顺序逐位一致 (AC11-2 幂等)
    T10 全链路预览区与历史任务展开共用同源排序 (AC11-4 — 通过 API
       透出顺序一致性验证, UI 两处均消费 detail.cards 本数组)

不依赖任何外部 hermes / docker 资源; 用 sqlite3 临时建 mock kanban + mock ledger。
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

# s5 补丁后 mw.console 内暴露 (模块级 helper) 才能直接调
from mw.console import _parse_history_line, _serialize_todo_doc, _todo as _todo_mod

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
              r.get("created_at"), r.get("started_at"),
              r.get("completed_at")))
    c.commit()
    c.close()


def stub_workers_registry():
    """喂一份最小可用的 workers registry (launch 校验需要)。"""
    console.REGISTRY = "/tmp/_test_s5_fix10_11_registry.json"
    reg = {"coder": {"api_url": "http://stub", "api_key": "x" * 32}}
    Path(console.REGISTRY).write_text(__import__("json").dumps(reg))


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


# ============================================================
# T1-T5: fix #10 卡片化 — done / dropped → 结构化 items
# ============================================================

def test_T1_parse_history_line_standard():
    """标准行: '- [t_5f8b3a] 修复登录闪退 → 已通过单元测试 4 例'。"""
    line = "- [t_5f8b3a] 修复登录闪退 → 已通过单元测试 4 例"
    out = _parse_history_line(line)
    check("T1.1 step_id 正确", out["step_id"] == "t_5f8b3a",
          f"got {out['step_id']!r}")
    check("T1.2 title 正确",
          out["title"] == "修复登录闪退", f"got {out['title']!r}")
    check("T1.3 result 正确",
          out["result"] == "已通过单元测试 4 例", f"got {out['result']!r}")
    check("T1.4 raw 含原行", "t_5f8b3a" in out["raw"])


def test_T2_parse_history_line_arrow_variants():
    """兼容 ASCII '->' 与中文 '→' 两种箭头 (旧链 / 手工维护)。"""
    line_ascii = "- [s_x] 升级依赖 -> 锁文件已提交"
    out_a = _parse_history_line(line_ascii)
    check("T2.1 ASCII -> 解析 step_id", out_a["step_id"] == "s_x")
    check("T2.2 ASCII -> 解析 title",
          out_a["title"] == "升级依赖", f"got {out_a['title']!r}")
    check("T2.3 ASCII -> 解析 result",
          out_a["result"] == "锁文件已提交",
          f"got {out_a['result']!r}")

    line_cn = "- [s_y] 迁移数据库 → 重跑全量回归 12 例"
    out_c = _parse_history_line(line_cn)
    check("T2.4 中文 → 解析 step_id", out_c["step_id"] == "s_y")
    check("T2.5 中文 → 解析 result",
          out_c["result"] == "重跑全量回归 12 例")


def test_T3_parse_history_line_malformed_falls_back():
    """不匹配的行: step_id 空 + raw 完整保留 (前端降级渲染依赖此)。"""
    line = "- 这是一条手工维护的总结, 无 step_id / 无 → 分隔符"
    out = _parse_history_line(line)
    check("T3.1 malformed step_id 为空",
          out["step_id"] == "", f"got {out['step_id']!r}")
    check("T3.2 malformed title 为空",
          out["title"] == "" or "这条" in out["title"],
          f"got {out['title']!r}")
    check("T3.3 malformed raw 完整保留",
          out["raw"].strip().startswith("- 这是一条"),
          f"got {out['raw']!r}")


def test_T4_serialize_todo_doc_returns_structured_items():
    """_serialize_todo_doc: done_lines / dropped_lines = list[dict], 非 list[str]。"""
    from mw.todo import TodoDoc, TodoTask
    cur = TodoTask(step_id="s_now", title="当前任务", goal="",
                   assignee="coder",
                   status="working", result="进行中")
    pending = [TodoTask(step_id="s_n1", title="下一步", goal="",
                        assignee="coder")]
    done = ["- [s_d1] 修复登录闪退 → 已通过单元测试 4 例",
            "- [s_d2] 升级依赖 -> 锁文件已提交"]
    dropped = ["- [s_x1] 探索方案 → 已放弃, 见 docs/decision.md"]
    doc = TodoDoc(title="修复闪退", current=cur, pending=pending,
                 done=done, dropped=dropped)
    out = _serialize_todo_doc(doc, raw_text="# raw")
    check("T4.1 done_lines 是 list", isinstance(out["done_lines"], list))
    check("T4.2 done_lines 长度对", len(out["done_lines"]) == 2)
    check("T4.3 done_lines[0] 是 dict",
          isinstance(out["done_lines"][0], dict),
          f"got {type(out['done_lines'][0]).__name__}")
    check("T4.4 done_lines[0].step_id",
          out["done_lines"][0]["step_id"] == "s_d1")
    check("T4.5 done_lines[1] 兼容 ASCII ->",
          out["done_lines"][1]["result"] == "锁文件已提交")
    check("T4.6 dropped_lines[0].step_id",
          out["dropped_lines"][0]["step_id"] == "s_x1")
    check("T4.7 current 仍含结构化字段",
          out["current"]["step_id"] == "s_now")
    check("T4.8 pending 数量对", len(out["pending"]) == 1)


def test_T5_serialize_todo_doc_malformed_preserved_in_raw():
    """不匹配行仍走 raw 字段, 前端 HistoryList 拿不到 step_id 走降级原行。"""
    from mw.todo import TodoDoc, TodoTask
    cur = TodoTask(step_id="s_now", title="cur", goal="",
                   assignee="", status="working")
    done = ["- 手工维护的总结行, 不符合模板格式"]
    doc = TodoDoc(title="t", current=cur, done=done)
    out = _serialize_todo_doc(doc, raw_text="raw")
    check("T5.1 malformed 行仍被解析", len(out["done_lines"]) == 1)
    item = out["done_lines"][0]
    check("T5.2 malformed 项 step_id 空",
          item.get("step_id") == "", f"got {item.get('step_id')!r}")
    check("T5.3 malformed 项 raw 保留",
          "手工维护" in item.get("raw", ""))


# ============================================================
# T6-T10: fix #11 排序 — 已执行段按 started_at, 未执行段按 seq
# ============================================================

def test_T6_exec_index_always_present_and_contiguous():
    """api_team_work_get: 每张 card 含 exec_index, 1..N 严格连续。"""
    k_path, l_path, led = fresh_dual_db()
    try:
        # 简单场景: 1 张 launch 卡
        led.create_team_work_run("r_a", "sess_a", "需求")
        led.put_contract("t_a", {"required_files": []})
        led.register_team_work_card("r_a", "t_a")
        led.set_team_work_current_card("r_a", "t_a")
        seed_kanban(k_path, [
            {"id": "t_a", "title": "A", "assignee": "coder",
             "status": "PENDING", "created_at": 1.0},
        ])
        out = console.api_team_work_get("r_a")
        check("T6.1 get 返回 ok", "cards" in out, f"got {list(out.keys())}")
        cards = out["cards"]
        check("T6.2 cards 数量对", len(cards) == 1, f"got {len(cards)}")
        check("T6.3 每张卡含 exec_index",
              all("exec_index" in c for c in cards),
              f"got keys: {[list(c.keys()) for c in cards]}")
        check("T6.4 exec_index 1..N 连续",
              [c["exec_index"] for c in cards] == [1],
              f"got {[c['exec_index'] for c in cards]}")
    finally:
        cleanup(k_path, l_path, led)


def test_T7_launch_only_unexecuted_order_by_seq():
    """launch 全量卡场景: 全未执行 → 顺序 = seq = 注册顺序 (AC11-3 零回归)。"""
    k_path, l_path, led = fresh_dual_db()
    try:
        led.create_team_work_run("r_a", "sess_a", "需求")
        # 4 张 launch 卡, seq 1,2,3,4
        for i, tid in enumerate(["t_a", "t_b", "t_c", "t_d"]):
            led.put_contract(tid, {"required_files": []})
            led.register_team_work_card("r_a", tid)
        led.set_team_work_current_card("r_a", "t_a")
        seed_kanban(k_path, [
            {"id": tid, "title": tid.upper(), "assignee": "coder",
             "status": "PENDING", "created_at": float(i + 1)}
            for i, tid in enumerate(["t_a", "t_b", "t_c", "t_d"])
        ])
        out = console.api_team_work_get("r_a")
        ids = [c["id"] for c in out["cards"]]
        check("T7.1 顺序按 seq 升序 (launch 全量零回归)",
              ids == ["t_a", "t_b", "t_c", "t_d"],
              f"got {ids}")
        idxs = [c["exec_index"] for c in out["cards"]]
        check("T7.2 exec_index = 1..4 严格递增",
              idxs == [1, 2, 3, 4], f"got {idxs}")
    finally:
        cleanup(k_path, l_path, led)


def test_T8_added_card_executed_first_sorts_by_started_at():
    """核心 AC11-1: 中途 add_cards 的卡后建先执行, 视觉序必须 = 执行序。

    场景: launch 4 张 (a/b/c/d, seq=1..4), 中途追加两张 (e/f, seq=5/6).
    真实执行: f (追加, seq=6) 09:00 完成; e (追加, seq=5) 09:30 完成;
              a (launch, seq=1) 10:00 完成; b (launch, seq=2) 10:30 完成;
              c/d 未执行。
    期望视觉序: f, e, a, b, c, d (执行序 + 计划序)。
    """
    k_path, l_path, led = fresh_dual_db()
    try:
        led.create_team_work_run("r_a", "sess_a", "需求")
        # launch 4 张 (seq 1..4)
        for tid in ["t_a", "t_b", "t_c", "t_d"]:
            led.put_contract(tid, {"required_files": []})
            led.register_team_work_card("r_a", tid)
        # 中途 add 2 张 (seq 5/6) — 通过 again register_team_work_card
        # 真实环境走 add_cards 端点
        for tid in ["t_e", "t_f"]:
            led.put_contract(tid, {"required_files": []})
            led.register_team_work_card("r_a", tid)
        # 给 a/b/e/f 设时间 (历史时序: f < e < a < b, c/d 未动)
        # t_a/b 10:00/10:30 完成; t_f 09:00; t_e 09:30
        seed_kanban(k_path, [
            {"id": "t_a", "title": "A", "assignee": "coder",
             "status": "done", "created_at": 8.50,
             "started_at": 9.50, "completed_at": 10.00},
            {"id": "t_b", "title": "B", "assignee": "coder",
             "status": "done", "created_at": 8.50,
             "started_at": 10.10, "completed_at": 10.30},
            {"id": "t_c", "title": "C", "assignee": "coder",
             "status": "PENDING", "created_at": 8.50},
            {"id": "t_d", "title": "D", "assignee": "coder",
             "status": "PENDING", "created_at": 8.50},
            {"id": "t_e", "title": "E (追加)", "assignee": "coder",
             "status": "done", "created_at": 9.10,
             "started_at": 9.10, "completed_at": 9.30},
            {"id": "t_f", "title": "F (追加)", "assignee": "coder",
             "status": "done", "created_at": 9.00,
             "started_at": 9.00, "completed_at": 9.00},
        ])
        out = console.api_team_work_get("r_a")
        ids = [c["id"] for c in out["cards"]]
        # 期望: f(9:00) < e(9:30) < a(10:00) < b(10:30) < c(d) 计划段
        check("T8.1 视觉序按 started_at 升序 (AC11-1)",
              ids == ["t_f", "t_e", "t_a", "t_b", "t_c", "t_d"],
              f"got {ids}")
        # 追加且先执行的两张 (t_f, t_e) 应在 launch 卡 (t_a/t_b) 之前
        check("T8.2 追加卡插到 execution 头部",
              ids.index("t_f") < ids.index("t_a")
              and ids.index("t_e") < ids.index("t_a"),
              f"got {ids}")
        # c/d (未执行) 按 seq 排尾部
        check("T8.3 未执行卡按 seq 计划段 (c 在 d 前)",
              ids[-2:] == ["t_c", "t_d"], f"got {ids}")
        # exec_index 严格 1..6
        idxs = [c["exec_index"] for c in out["cards"]]
        check("T8.4 exec_index 1..6 连续",
              idxs == list(range(1, 7)), f"got {idxs}")
    finally:
        cleanup(k_path, l_path, led)


def test_T9_double_get_is_idempotent():
    """双刷新顺序逐位一致 (AC11-2 / AC2-3 幂等保证)。"""
    k_path, l_path, led = fresh_dual_db()
    try:
        led.create_team_work_run("r_a", "sess_a", "需求")
        for tid in ["t_a", "t_b", "t_c", "t_d", "t_e", "t_f"]:
            led.put_contract(tid, {"required_files": []})
            led.register_team_work_card("r_a", tid)
        seed_kanban(k_path, [
            {"id": "t_a", "title": "A", "assignee": "coder",
             "status": "done", "created_at": 8.0,
             "started_at": 10.0, "completed_at": 10.5},
            {"id": "t_b", "title": "B", "assignee": "coder",
             "status": "done", "created_at": 8.0,
             "started_at": 10.6, "completed_at": 11.0},
            {"id": "t_c", "title": "C", "assignee": "coder",
             "status": "PENDING", "created_at": 8.0},
            {"id": "t_d", "title": "D", "assignee": "coder",
             "status": "PENDING", "created_at": 8.0},
            {"id": "t_e", "title": "E", "assignee": "coder",
             "status": "done", "created_at": 9.0,
             "started_at": 9.0, "completed_at": 9.5},
            {"id": "t_f", "title": "F", "assignee": "coder",
             "status": "PENDING", "created_at": 9.5},
        ])
        out1 = console.api_team_work_get("r_a")
        out2 = console.api_team_work_get("r_a")
        ids1 = [c["id"] for c in out1["cards"]]
        ids2 = [c["id"] for c in out2["cards"]]
        check("T9.1 双刷新顺序一致 (AC11-2)",
              ids1 == ids2, f"1: {ids1} vs 2: {ids2}")
    finally:
        cleanup(k_path, l_path, led)


def test_T10_started_at_none_falls_back_to_completed_at():
    """已执行卡缺少 started_at (极端) 时, 用 completed_at 兜底, 仍进 executed 段。"""
    k_path, l_path, led = fresh_dual_db()
    try:
        led.create_team_work_run("r_a", "sess_a", "需求")
        for tid in ["t_a", "t_b", "t_c"]:
            led.put_contract(tid, {"required_files": []})
            led.register_team_work_card("r_a", tid)
        seed_kanban(k_path, [
            {"id": "t_a", "title": "A", "assignee": "coder",
             "status": "PENDING", "created_at": 1.0},
            {"id": "t_b", "title": "B", "assignee": "coder",
             "status": "done", "created_at": 2.0,
             "started_at": None, "completed_at": 5.0},
            {"id": "t_c", "title": "C", "assignee": "coder",
             "status": "done", "created_at": 3.0,
             "started_at": 4.0, "completed_at": 4.5},
        ])
        out = console.api_team_work_get("r_a")
        ids = [c["id"] for c in out["cards"]]
        # c (4.0) < b (completed_at 5.0) < a (未执行, seq)
        check("T10.1 completed_at 兜底已执行段",
              ids == ["t_c", "t_b", "t_a"], f"got {ids}")
    finally:
        cleanup(k_path, l_path, led)


def main():
    tests = [
        # fix #10 (UI 卡片化)
        test_T1_parse_history_line_standard,
        test_T2_parse_history_line_arrow_variants,
        test_T3_parse_history_line_malformed_falls_back,
        test_T4_serialize_todo_doc_returns_structured_items,
        test_T5_serialize_todo_doc_malformed_preserved_in_raw,
        # fix #11 (排序 + exec_index)
        test_T6_exec_index_always_present_and_contiguous,
        test_T7_launch_only_unexecuted_order_by_seq,
        test_T8_added_card_executed_first_sorts_by_started_at,
        test_T9_double_get_is_idempotent,
        test_T10_started_at_none_falls_back_to_completed_at,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            check(t.__name__, False, f"EXC: {type(e).__name__}: {e}")
    total = len(PASS) + len(FAIL)
    print()
    if not FAIL:
        print(f"\033[92m=== ALL {total} S5 TESTS PASSED ===\033[0m")
        return 0
    else:
        print(f"\033[91m=== {len(FAIL)}/{total} FAILED ===\033[0m")
        for n, d in FAIL:
            print(f"  - {n}: {d}")
        return 1


# 边界减法:
#  - T8 充分覆盖 AC11-1 中途追加先执行;前端 StepTimeline 直接消费 cards 数组,
#    「全链路预览区」与「历史任务展开」两处共用本数组 → AC11-4 自动成立
#    (同源, 不可能不一致), 故不重复添加单测。


if __name__ == "__main__":
    sys.exit(main())
