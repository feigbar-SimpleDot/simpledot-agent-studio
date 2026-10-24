"""REOPEN A面 (issue #5): advance/launch 派发前 TODO 轮转机器校验。

覆盖 REOPEN-DESIGN 1.7 全部 11 条用例 (A-AC1~7):
  1. 卡键匹配放行  2. step 匹配放行  3. 指向他卡拒绝(expected+actual 齐)
  4. TODO 缺失拒绝  5. 拒绝零副作用  6. 双键名(kanban_task/卡)
  7. fail-closed no_binding  8. 重复 advance 串行铁律先拦
  9. 跳过快速通道放行  10. launch 首卡三态  11. 纯函数单元直测

沿用 test_team_work_console.py 的 fresh_ledger + stub_dispatch 模式。
跑法: .venv/bin/python -m pytest tests/test_advance_todo_binding.py -q
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mw.console as console
import mw.todo as todo

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))


def fresh_ledger():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    console.LEDGER_DB = f.name
    return console.Ledger(f.name), f.name


def stub_dispatch_recorder():
    calls = []
    original = console.api_dispatch

    def _stub(payload):
        calls.append(dict(payload))
        return {"ok": True, "accepted": True, "stub": True,
                "task_id": payload.get("task_id")}
    console.api_dispatch = _stub
    return calls, lambda: setattr(console, "api_dispatch", original)


def stub_workers_registry():
    import json
    console.REGISTRY = "/tmp/_test_advance_todo_binding_registry.json"
    reg = {"coder": {"api_url": "http://stub", "api_key": "x" * 32}}
    Path(console.REGISTRY).write_text(json.dumps(reg))


def write_todo(ws, current_kv, pending_lines="(无)"):
    """在临时 workspace 写一份最小 TODO.md, 『当前任务』段由 kv 决定。"""
    lines = ["# Team Work: test", "", "## 当前任务"]
    for k, v in current_kv.items():
        lines.append(f"- {k}: {v}")
    lines += ["", "---", "", "## 待执行", pending_lines, ""]
    p = Path(ws) / "TODO.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def launch_two_card_run(led, ws, calls_stub=True):
    """launch 一个两卡 run (t_a→t_b), TODO 当前段指向 t_a。返回 run_id。"""
    write_todo(ws, {"步骤": "a", "标题": "A", "卡": "t_a", "状态": "进行中"})
    r = console.api_team_work_launch({
        "initial_task": "t", "session_id": "s",
        "project_workspace": ws,
        "steps": [
            {"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"},
            {"step_id": "b", "title": "B", "task_id": "t_b", "assignee": "coder"},
        ],
    })
    assert r.get("ok"), f"launch 失败: {r}"
    return r["run_id"]


# ============================================================
# 1. 卡键匹配放行 (A-AC1a)
# ============================================================
def test_advance_pass_when_card_matches():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            ws = tempfile.mkdtemp()
            run_id = launch_two_card_run(led, ws)
            led.clear_team_work_current_card(run_id)
            write_todo(ws, {"步骤": "b", "标题": "B", "卡": "t_b", "状态": "进行中"})
            calls.clear()
            adv = console.api_team_work_advance(run_id, {
                "task_id": "t_b", "target_profile": "coder"})
            check("① 卡键匹配 → ok", adv.get("ok") is True, f"got {adv}")
            check("① dispatch 被调", len(calls) == 1, f"got {len(calls)}")
            run = led.get_team_work(run_id)
            check("① 槽=t_b", run["current_card_id"] == "t_b",
                  f"got {run['current_card_id']!r}")
        finally:
            restore()
    finally:
        led.close(); os.unlink(path)


# ============================================================
# 2. step 匹配放行 (A-AC1b)
# ============================================================
def test_advance_pass_by_step_only():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            ws = tempfile.mkdtemp()
            run_id = launch_two_card_run(led, ws)
            led.clear_team_work_current_card(run_id)
            # TODO 无卡键, 只有 步骤: b → payload step_id 匹配放行
            write_todo(ws, {"步骤": "b", "标题": "B", "状态": "进行中"})
            calls.clear()
            adv = console.api_team_work_advance(run_id, {
                "task_id": "t_b", "target_profile": "coder", "step_id": "b"})
            check("② step 匹配 (无卡键) → ok", adv.get("ok") is True, f"got {adv}")
        finally:
            restore()
    finally:
        led.close(); os.unlink(path)


# ============================================================
# 3. 指向他卡拒绝, expected+actual 齐 (A-AC2)
# ============================================================
def test_advance_reject_stale_other_card():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            ws = tempfile.mkdtemp()
            run_id = launch_two_card_run(led, ws)
            led.clear_team_work_current_card(run_id)
            # 陈旧: 『当前任务』还指着 t_a (completed+有结果)
            write_todo(ws, {"步骤": "a", "标题": "A", "卡": "t_a",
                           "状态": "completed", "结果": "旧结果"})
            calls.clear()
            adv = console.api_team_work_advance(run_id, {
                "task_id": "t_b", "target_profile": "coder", "step_id": "b"})
            check("③ 指向他卡 → 409", adv.get("code") == 409, f"got {adv}")
            check("③ reason=mismatch", adv.get("reason") == "mismatch",
                  f"got {adv.get('reason')!r}")
            check("③ expected 齐", adv.get("expected", {}).get("task_id") == "t_b"
                  and adv.get("expected", {}).get("step_id") == "b", f"got {adv.get('expected')}")
            check("③ actual 齐", adv.get("actual", {}).get("kanban_task") == "t_a"
                  and adv.get("actual", {}).get("status") == "completed",
                  f"got {adv.get('actual')}")
            check("③ hint 指路轮转", "轮转" in adv.get("hint", ""),
                  f"got {adv.get('hint')!r}")
        finally:
            restore()
    finally:
        led.close(); os.unlink(path)


# ============================================================
# 4. TODO 缺失拒绝 (A-AC2)
# ============================================================
def test_advance_reject_todo_missing():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            ws = tempfile.mkdtemp()
            run_id = launch_two_card_run(led, ws)
            led.clear_team_work_current_card(run_id)
            os.unlink(Path(ws) / "TODO.md")  # 删掉 TODO
            calls.clear()
            adv = console.api_team_work_advance(run_id, {
                "task_id": "t_b", "target_profile": "coder"})
            check("④ TODO 缺失 → 409 todo_missing",
                  adv.get("code") == 409 and adv.get("reason") == "todo_missing",
                  f"got {adv}")
        finally:
            restore()
    finally:
        led.close(); os.unlink(path)


# ============================================================
# 5. 拒绝零副作用 (A-AC3) — 核心安全断言
# ============================================================
def test_advance_reject_zero_side_effects():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            ws = tempfile.mkdtemp()
            run_id = launch_two_card_run(led, ws)
            led.clear_team_work_current_card(run_id)
            # 给 t_a 留一个 BLOCKED residue — 拒绝时不得被 settle 覆盖
            led.set_harvest("t_a", "BLOCKED", log="reaper fallback")
            write_todo(ws, {"步骤": "a", "标题": "A", "卡": "t_a", "状态": "completed"})
            calls.clear()
            adv = console.api_team_work_advance(run_id, {
                "task_id": "t_b", "target_profile": "coder"})
            check("⑤ 拒绝 → 409", adv.get("code") == 409, f"got {adv}")
            run = led.get_team_work(run_id)
            check("⑤ 拒绝后槽仍 NULL (未占槽)",
                  run["current_card_id"] is None,
                  f"got {run['current_card_id']!r}")
            check("⑤ dispatch 未被调 (无 worker 起)", len(calls) == 0,
                  f"got {len(calls)}")
            hc_a = led.get_contract("t_a")
            check("⑤ residue 未被改写 (仍 BLOCKED)",
                  hc_a["harvest_status"] == "BLOCKED",
                  f"got {hc_a['harvest_status']!r}")
            check("⑤ run 仍 ACTIVE", run["status"] == "ACTIVE",
                  f"got {run['status']}")
        finally:
            restore()
    finally:
        led.close(); os.unlink(path)


# ============================================================
# 6. 双键名: kanban_task 与 卡 都放行; 同时存在 kanban_task 优先 (A-AC7⑤/D1)
# ============================================================
def test_advance_dual_key_names():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            ws = tempfile.mkdtemp()
            run_id = launch_two_card_run(led, ws)
            led.clear_team_work_current_card(run_id)
            # kanban_task 键
            write_todo(ws, {"步骤": "b", "标题": "B",
                           "kanban_task": "t_b", "状态": "进行中"})
            calls.clear()
            adv = console.api_team_work_advance(run_id, {
                "task_id": "t_b", "target_profile": "coder"})
            check("⑥ kanban_task 键 → 放行", adv.get("ok") is True, f"got {adv}")
            # 同时有 kanban_task(本卡) 与 卡(他卡): kanban_task 优先 → 放行
            led.clear_team_work_current_card(run_id)
            write_todo(ws, {"步骤": "b", "标题": "B", "kanban_task": "t_b",
                           "卡": "t_a", "状态": "进行中"})
            calls.clear()
            adv2 = console.api_team_work_advance(run_id, {
                "task_id": "t_b", "target_profile": "coder"})
            check("⑥ 双键并存 kanban_task 优先 → 放行",
                  adv2.get("ok") is True, f"got {adv2}")
        finally:
            restore()
    finally:
        led.close(); os.unlink(path)


# ============================================================
# 7. fail-closed: payload 无 step_id + TODO 无绑定键 (A-AC5)
# ============================================================
def test_advance_fail_closed_no_binding():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            ws = tempfile.mkdtemp()
            run_id = launch_two_card_run(led, ws)
            led.clear_team_work_current_card(run_id)
            write_todo(ws, {"步骤": "", "标题": "B", "状态": "进行中"})
            calls.clear()
            adv = console.api_team_work_advance(run_id, {
                "task_id": "t_b", "target_profile": "coder"})
            check("⑦ 无绑定键 → 409 no_binding",
                  adv.get("code") == 409 and adv.get("reason") == "no_binding",
                  f"got {adv}")
            check("⑦ hint 指路补写", "补写" in adv.get("hint", ""),
                  f"got {adv.get('hint')!r}")
        finally:
            restore()
    finally:
        led.close(); os.unlink(path)


# ============================================================
# 8. 重复 advance: 串行铁律先拦, 非 TODO 校验错 (A-AC7⑥)
# ============================================================
def test_advance_serial_guard_precedes_todo_check():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            ws = tempfile.mkdtemp()
            run_id = launch_two_card_run(led, ws)
            # 槽=t_a 未清, advance(t_c 甚至 t_b) → 先被串行铁律拦
            adv = console.api_team_work_advance(run_id, {
                "task_id": "t_b", "target_profile": "coder"})
            check("⑧ 槽未清 → 串行铁律先拦 (错误含 current_card_id)",
                  adv.get("code") == 409 and adv.get("current_card_id") == "t_a",
                  f"got {adv}")
            check("⑧ 非 TODO 校验错 (reason 不是 mismatch/todo_missing)",
                  adv.get("reason") not in ("mismatch", "todo_missing",
                                            "no_binding"),
                  f"got {adv.get('reason')!r}")
        finally:
            restore()
    finally:
        led.close(); os.unlink(path)


# ============================================================
# 9. 跳过快速通道: 指向本卡 + 「跳过: 是」行 → 放行 (A-AC4)
# ============================================================
def test_advance_skip_fast_path_passes():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            ws = tempfile.mkdtemp()
            run_id = launch_two_card_run(led, ws)
            led.clear_team_work_current_card(run_id)
            write_todo(ws, {"步骤": "b", "标题": "B", "卡": "t_b",
                           "跳过": "是 [原因: 产品经理决策]", "状态": "进行中"})
            calls.clear()
            adv = console.api_team_work_advance(run_id, {
                "task_id": "t_b", "target_profile": "coder"})
            check("⑨ 跳过快速通道 → 放行", adv.get("ok") is True, f"got {adv}")
        finally:
            restore()
    finally:
        led.close(); os.unlink(path)


# ============================================================
# 10. launch 首卡三态 (A-AC6 / D4)
# ============================================================
def test_launch_first_card_check():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            # Issue#8 (2026-10-13): launch 守卫「一 run 一 session」已上线,
            # 测试三发 launch 必须三 sid。
            # a) TODO 指向首卡 → launch ok
            ws = tempfile.mkdtemp()
            write_todo(ws, {"步骤": "a", "标题": "A", "卡": "t_a", "状态": "进行中"})
            r = console.api_team_work_launch({
                "initial_task": "t", "session_id": "s_a", "project_workspace": ws,
                "steps": [{"step_id": "a", "title": "A", "task_id": "t_a",
                          "assignee": "coder"}],
            })
            check("⑩a launch 首卡绑定正确 → ok", r.get("ok") is True, f"got {r}")
            check("⑩a 无 warning", "warning" not in r, f"got {r}")
            console.api_team_work_close(r["run_id"], {"operator": "t"})
            # b) TODO 指向他卡 → 409 + run ABORTED
            ws2 = tempfile.mkdtemp()
            write_todo(ws2, {"步骤": "x", "标题": "X", "卡": "t_other",
                            "状态": "completed"})
            r2 = console.api_team_work_launch({
                "initial_task": "t2", "session_id": "s_b", "project_workspace": ws2,
                "steps": [{"step_id": "a", "title": "A", "task_id": "t_a2",
                          "assignee": "coder"}],
            })
            check("⑩b launch 首卡指向他卡 → 409", r2.get("code") == 409,
                  f"got {r2}")
            check("⑩b reason=mismatch", r2.get("reason") == "mismatch",
                  f"got {r2.get('reason')!r}")
            run2 = led.get_team_work(r2["run_id"])
            check("⑩b run 已 ABORTED (回滚)", run2["status"] == "ABORTED",
                  f"got {run2['status']}")
            # c) launch 无 project_workspace → ok + warning (D4)
            r3 = console.api_team_work_launch({
                "initial_task": "t3", "session_id": "s_c",
                "steps": [{"step_id": "a", "title": "A", "task_id": "t_a3",
                          "assignee": "coder"}],
            })
            check("⑩c launch 无 pw → ok + warning",
                  r3.get("ok") is True and "warning" in r3, f"got {r3}")
        finally:
            restore()
    finally:
        led.close(); os.unlink(path)


# ============================================================
# 11. 纯函数单元直测: verify_current_task_binding / current_binding
# ============================================================
def test_verify_pure_function():
    ws = tempfile.mkdtemp()
    p = Path(ws) / "TODO.md"
    # 文件不存在
    ok, d = todo.verify_current_task_binding(p, "t_x")
    check("⑪ 文件不存在 → todo_missing",
          not ok and d["reason"] == "todo_missing", f"got {d}")
    # 有文件无当前任务段
    p.write_text("# Team Work: x\n\n## 待执行\n(无)\n", encoding="utf-8")
    ok, d = todo.verify_current_task_binding(p, "t_x")
    check("⑪ 无当前任务段 → todo_missing",
          not ok and d["reason"] == "todo_missing", f"got {d}")
    # 段内无 kv 行
    p.write_text("# Team Work: x\n\n## 当前任务\n(空)\n\n## 待执行\n(无)\n",
                 encoding="utf-8")
    ok, d = todo.verify_current_task_binding(p, "t_x")
    check("⑪ 段内无 kv 行 → todo_missing",
          not ok and d["reason"] == "todo_missing", f"got {d}")
    # 全角冒号天然支持
    p.write_text("# Team Work: x\n\n## 当前任务\n- 卡： t_x\n- 状态： working\n",
                 encoding="utf-8")
    ok, d = todo.verify_current_task_binding(p, "t_x")
    check("⑪ 全角冒号 → 放行", ok, f"got {d}")
    # mismatch 时 detail 七字段齐
    p.write_text("# Team Work: x\n\n## 当前任务\n- 步骤: s1\n- 卡: t_y\n"
                 "- 状态: completed\n", encoding="utf-8")
    ok, d = todo.verify_current_task_binding(p, "t_x", "s2")
    need = {"reason", "expected_task_id", "expected_step_id",
            "actual_kanban_task", "actual_step_id", "actual_status", "todo_path"}
    check("⑪ mismatch detail 七字段齐",
          not ok and need.issubset(d.keys()) and d["reason"] == "mismatch",
          f"got {d}")
    # 同卡 completed 放行 (D5)
    ok, _ = todo.verify_current_task_binding(p, "t_y")
    check("⑪ 同卡 completed → 放行 (D5)", ok)
    # current_binding 轻量读取
    b = todo.current_binding(p)
    check("⑪ current_binding 返回绑定",
          b == {"step_id": "s1", "kanban_task": "t_y",
                "status": "completed", "title": ""}, f"got {b}")
    # 纯函数: 重复调用结果一致, 不落标记
    ok2, d2 = todo.verify_current_task_binding(p, "t_x", "s2")
    check("⑪ 纯函数幂等 (重复调用一致)", (ok2, d2) == (ok, d))


# ============================================================
# 12. 非法编码 fail-closed (REOPEN-FIX P1-2 / 复审必测 §5.2)
# ============================================================
def test_illegal_utf8_fail_closed():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            # a) 纯函数层: 非法 UTF-8 字节 → current_binding None / todo_missing
            ws = tempfile.mkdtemp()
            p = Path(ws) / "TODO.md"
            p.write_bytes(b"# Team Work: x\n\n## \xff\xfe\x80 \xe5\x00\n")
            check("⑫ current_binding 非法编码 → None",
                  todo.current_binding(p) is None)
            ok, d = todo.verify_current_task_binding(p, "t_b")
            check("⑫ verify 非法编码 → todo_missing",
                  not ok and d["reason"] == "todo_missing", f"got {d}")
            # b) advance 层: 合法 launch 后 TODO 被写坏 → 409 非 500, 零副作用
            ws2 = tempfile.mkdtemp()
            run_id = launch_two_card_run(led, ws2)
            led.clear_team_work_current_card(run_id)
            # bytes 内 \u 非法转义(旧写法触发 SyntaxWarning 且落盘的是字面反斜杠串);
            # \\u 与原行为逐字节等价, 零漂移。
            (Path(ws2) / "TODO.md").write_bytes(b"## \\u5f53\\u524d\\u4efb\\u52a1\n- \xff bad\n")
            calls.clear()
            adv = console.api_team_work_advance(run_id, {
                "task_id": "t_b", "target_profile": "coder"})
            check("⑫ advance 非法编码 → 409 todo_missing (非500)",
                  adv.get("code") == 409 and adv.get("reason") == "todo_missing",
                  f"got {adv}")
            run = led.get_team_work(run_id)
            check("⑫ advance 拒绝零副作用 (槽NULL+未派发)",
                  run["current_card_id"] is None and len(calls) == 0,
                  f"slot={run['current_card_id']!r} calls={len(calls)}")
            # c) launch 层: 首卡 TODO 非法编码 → 409 + run ABORTED (不留 ACTIVE)
            console.api_team_work_close(run_id, {"operator": "test"})  # 先清 ACTIVE run
            ws3 = tempfile.mkdtemp()
            (Path(ws3) / "TODO.md").write_bytes(b"\xc3\x28 invalid utf-8\n")
            # Issue#8 (2026-10-13): launch 守卫「一 run 一 session」已上线,
            # 复用 's' 会被会话守卫 409, 此处测试要的是 TODO 校验失败,
            # 必须给一个新 sid 让守卫不挡, 进到 TODO 校验层。
            r = console.api_team_work_launch({
                "initial_task": "t", "session_id": "s_utf8",
                "project_workspace": ws3,
                "steps": [{"step_id": "a", "title": "A", "task_id": "t_a9",
                          "assignee": "coder"}],
            })
            check("⑫ launch 非法编码 → 409 todo_missing (非500)",
                  r.get("code") == 409 and r.get("reason") == "todo_missing",
                  f"got {r}")
            run3 = led.get_team_work(r["run_id"])
            check("⑫ launch 不留 ACTIVE run (已 ABORTED 回滚)",
                  run3["status"] == "ABORTED", f"got {run3['status']}")
        finally:
            restore()
    finally:
        led.close(); os.unlink(path)


# ============================================================
# 入口
# ============================================================
def main():
    tests = [
        test_advance_pass_when_card_matches,
        test_advance_pass_by_step_only,
        test_advance_reject_stale_other_card,
        test_advance_reject_todo_missing,
        test_advance_reject_zero_side_effects,
        test_advance_dual_key_names,
        test_advance_fail_closed_no_binding,
        test_advance_serial_guard_precedes_todo_check,
        test_advance_skip_fast_path_passes,
        test_launch_first_card_check,
        test_verify_pure_function,
        test_illegal_utf8_fail_closed,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            check(t.__name__, False, f"unhandled: {type(e).__name__}: {e}")
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\n失败明细:")
        for name, detail in FAIL:
            print(f"  - {name}: {detail}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
