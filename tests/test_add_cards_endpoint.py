"""POST /api/team-work/<run_id>/cards 追加卡端点测试 (2026-10-04 补根)。

背景: run 建好后 HTTP 面无「追加卡」入口 → 回流场景 main 徒手调 ledger。
本端点正式化这条路。铁律:
  1. 只登记, 不占槽, 不派发 (派活归 advance)
  2. 非 ACTIVE run → 409
  3. 幂等: 重复登记不重复递增 card_count
  4. 一张卡不能挂两个 run
  5. 追加卡继承 run.project_workspace 进契约 (F1 同源)
  6. 登记后 advance 立即可用 (契约反查通过)

跑法: python3 tests/test_add_cards_endpoint.py
"""
import os
import sys
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mw.console as console

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
    original = console.api_dispatch
    calls = []

    def _stub(payload):
        calls.append(payload)
        return {"ok": True, "task_id": payload.get("task_id")}
    console.api_dispatch = _stub
    return calls, lambda: setattr(console, "api_dispatch", original)


def launch_run(led, run_pw=None):
    calls, restore = stub_dispatch_recorder()
    # REOPEN A-1: launch/advance 机器校验 TODO 绑定, 默认用临时 ws + TODO 夹具
    if run_pw is None:
        run_pw = tempfile.mkdtemp()
        Path(run_pw, "TODO.md").write_text(
            "# Team Work: t\n\n## 当前任务\n- 步骤: a\n- 卡: t_a\n"
            "- 状态: 进行中\n\n---\n\n## 待执行\n(无)\n", encoding="utf-8")
    r = console.api_team_work_launch({
        "initial_task": "t", "session_id": "main_sess",
        "project_workspace": run_pw,
        "steps": [
            {"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"},
        ],
    })
    return r["run_id"], calls, restore


def test_add_cards_registers_without_dispatch_or_slot():
    led, path = fresh_ledger()
    try:
        run_id, calls, restore = launch_run(led)
        # REOPEN-FIX P1-3: 夹具改造后 launch pw 是临时目录, F1 断言改与夹具
        # 实际 pw 变量比对 (原硬编码 /share_workspace/projects/demo 已失真)。
        run_pw = led.get_team_work(run_id)["project_workspace"]
        calls.clear()
        led.clear_team_work_current_card(run_id)  # 模拟上一张已结算
        r = console.api_team_work_add_cards(run_id, {"cards": [
            {"task_id": "t_fix", "title": "修复", "assignee": "coder",
             "required_files": ["FIX.md"]},
            {"task_id": "t_re", "title": "复审", "assignee": "code-review",
             "required_files": ["REVIEW2.md"]},
        ]})
        check("加卡 ok", r.get("ok") is True, f"got {r}")
        check("两张都登记", r.get("registered") == ["t_fix", "t_re"],
              f"got {r.get('registered')}")
        check("card_count 5 (1 launch + 2 追加 + launch 计 1... 实为 3)",
              r.get("card_count") == 3, f"got {r.get('card_count')}")
        check("不派发 (dispatch 未被调)", len(calls) == 0, f"got {calls}")
        run = led.get_team_work(run_id)
        check("不占槽 (current_card_id 仍 NULL)",
              run["current_card_id"] is None, f"got {run['current_card_id']!r}")
        # 契约正确写入
        hc = led.get_contract("t_fix")
        ct = json.loads(hc["contract_json"])
        check("契约 required_files 落对", ct["required_files"] == ["FIX.md"])
        check("契约继承 run.project_workspace (F1 同源)",
              ct.get("product_workspace") == run_pw,
              f"got {ct.get('product_workspace')!r} != {run_pw!r}")
        check("契约 team_work_run_id 归属正确",
              hc["team_work_run_id"] == run_id)
        # initiator 登记 (报告中继路由)
        check("initiator 登记到 main session",
              led.get_initiator("t_fix") == "main_sess",
              f"got {led.get_initiator('t_fix')!r}")
    finally:
        restore()
        led.close()
        os.unlink(path)


def test_add_cards_rejects_non_active_run():
    led, path = fresh_ledger()
    try:
        run_id, calls, restore = launch_run(led)
        led.close_team_work(run_id, "test")
        r = console.api_team_work_add_cards(run_id, {"cards": [
            {"task_id": "t_x", "assignee": "coder"}]})
        check("非 ACTIVE run → 409", r.get("code") == 409, f"got {r}")
        check("未登记", not r.get("registered"))
    finally:
        restore()
        led.close()
        os.unlink(path)


def test_add_cards_idempotent():
    led, path = fresh_ledger()
    try:
        run_id, calls, restore = launch_run(led)
        led.clear_team_work_current_card(run_id)
        r1 = console.api_team_work_add_cards(run_id, {"cards": [
            {"task_id": "t_fix", "assignee": "coder"}]})
        r2 = console.api_team_work_add_cards(run_id, {"cards": [
            {"task_id": "t_fix", "assignee": "coder"}]})
        check("首次登记 registered", r1.get("registered") == ["t_fix"])
        check("重复登记归 already", r2.get("already_registered") == ["t_fix"],
              f"got {r2}")
        check("card_count 不重复递增", r2.get("card_count") == r1.get("card_count"),
              f"{r1.get('card_count')} vs {r2.get('card_count')}")
    finally:
        restore()
        led.close()
        os.unlink(path)


def test_add_cards_rejects_cross_run():
    led, path = fresh_ledger()
    try:
        run_id, calls, restore = launch_run(led)
        led.clear_team_work_current_card(run_id)
        console.api_team_work_add_cards(run_id, {"cards": [
            {"task_id": "t_shared", "assignee": "coder"}]})
        # 造第二个 run (直接改状态绕单 ACTIVE 限制)
        led.conn.execute(
            "UPDATE team_work_runs SET status='CLOSED' WHERE run_id=?", (run_id,))
        led.conn.execute(
            "INSERT INTO team_work_runs (run_id, initiator_session, initial_task, "
            "project_workspace, status, card_count, created_at) "
            "VALUES ('run_two','s','t','/p2','ACTIVE',0,?)",
            (__import__("time").time(),))
        led.conn.commit()
        r = console.api_team_work_add_cards("run_two", {"cards": [
            {"task_id": "t_shared", "assignee": "coder"}]})
        check("卡已属别的 run → skip 并说明",
              any("已属" in s.get("why", "") for s in r.get("skipped", [])),
              f"got {r.get('skipped')}")
        check("registered 为空", r.get("registered") == [])
    finally:
        restore()
        led.close()
        os.unlink(path)


def test_add_cards_then_advance_works():
    """端到端: 加卡 → advance 该卡 → 契约反查通过 → 派发成功。"""
    led, path = fresh_ledger()
    try:
        run_id, calls, restore = launch_run(led)
        led.clear_team_work_current_card(run_id)
        console.api_team_work_add_cards(run_id, {"cards": [
            {"task_id": "t_fix", "title": "修复", "assignee": "coder",
             "required_files": ["FIX.md"]}]})
        calls.clear()
        # 模拟 main 轮转: 『当前任务』改指 t_fix (advance 绑定校验要求)
        pw = tempfile.mkdtemp()
        Path(pw, "TODO.md").write_text(
            "# Team Work: t\n\n## 当前任务\n- 步骤: fix\n- 卡: t_fix\n"
            "- 状态: 进行中\n\n---\n\n## 待执行\n(无)\n", encoding="utf-8")
        adv = console.api_team_work_advance(run_id, {
            "task_id": "t_fix", "target_profile": "coder",
            "required_files": ["FIX.md"], "project_workspace": pw})
        check("追加的卡可直接 advance", adv.get("ok") is True, f"got {adv}")
        check("advance 派发了 t_fix",
              len(calls) == 1 and calls[0]["task_id"] == "t_fix")
        check("advance 派发继承 project_workspace (F1)",
              calls[0].get("project_workspace") == pw,
              f"got {calls[0].get('project_workspace')!r}")
    finally:
        restore()
        led.close()
        os.unlink(path)


def test_add_cards_bad_payload():
    led, path = fresh_ledger()
    try:
        run_id, calls, restore = launch_run(led)
        r = console.api_team_work_add_cards(run_id, {"cards": []})
        check("空 cards → 400", r.get("code") == 400, f"got {r}")
        r = console.api_team_work_add_cards(run_id, {})
        check("缺 cards → 400", r.get("code") == 400, f"got {r}")
        led.clear_team_work_current_card(run_id)
        r = console.api_team_work_add_cards(run_id, {"cards": [
            {"assignee": "coder"}, "junk"]})
        check("缺 task_id / 非 object → skip 不崩",
              r.get("ok") is True and len(r.get("skipped", [])) == 2,
              f"got {r}")
    finally:
        restore()
        led.close()
        os.unlink(path)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(f"\n▶ {t.__name__}")
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
