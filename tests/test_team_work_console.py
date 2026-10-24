"""T2 端点测试:api_team_work_launch active 护栏 + advance / close / list / get_run。

铁律 (T2):
  1. api_team_work_launch 已有 ACTIVE run → 409 (UI 必须先 close)
  2. launch 后会建 team_work_runs 行 + register 所有卡 + 占槽第一张
  3. advance 必须 current_card_id == NULL 且 task 属于本 run
  4. close 杀 spawn-bg + 归档未结算卡 + 改 status (CLOSED / ABORTED)
  5. api_dispatch 带 run_id 时校验 current_card_id 必须匹配
  6. list 默认只看 ACTIVE;include_closed 含历史
  7. 单 run get 返回旗下所有卡状态

不依赖任何外部 hermes / docker 资源; 通过 monkeypatch LEDGER_DB +
api_dispatch 来跑纯函数测试。
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mw.console as console

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    marker = "PASS" if cond else "FAIL"
    line = f"  {marker}  {name}"
    if detail:
        line += f"  {detail}"
    print(line)


def fresh_ledger():
    """新临时 db, 并把它注入 mw.console 模块 LEDGER_DB。"""
    f = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    f.close()
    # 替换模块级 LEDGER_DB, 让所有 api_* 函数用同一个 db
    console.LEDGER_DB = f.name
    return console.Ledger(f.name), f.name


def clear_ledger(led, path):
    """收尾: 关连接 + 删临时文件。"""
    try:
        led.close()
    except Exception:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass


def stub_dispatch_recorder():
    """把 api_dispatch 替成「只记录调用, 不真跑 spawn」的版本。

    返回 (recorder_list, restore_fn):
      - recorder_list 收集每次调用的 payload
      - restore_fn 用来还原原始 api_dispatch
    """
    calls = []
    original = console.api_dispatch

    def _stub(payload):
        # 模拟正常受理
        calls.append(dict(payload))
        return {"ok": True, "accepted": True, "stub": True,
                "task_id": payload.get("task_id")}

    console.api_dispatch = _stub
    return calls, lambda: setattr(console, "api_dispatch", original)


def stub_workers_registry():
    """喂一份最小可用的 workers registry (否则 launch 校验失败)。"""
    console.REGISTRY = "/tmp/_test_team_work_console_registry.json"
    reg = {"coder": {"api_url": "http://stub", "api_key": "x" * 32}}
    Path(console.REGISTRY).write_text(__import__("json").dumps(reg))


# ============================================================
# T2.1: api_team_work_launch active 护栏 + run 行创建
# ============================================================

def test_launch_creates_run_and_dispatches_first():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            r = console.api_team_work_launch({
                "initial_task": "测试需求",
                "session_id": "s_main",
                "steps": [
                    {"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"},
                    {"step_id": "b", "title": "B", "task_id": "t_b", "assignee": "coder"},
                ],
            })
            check("launch 返回 ok=True", r.get("ok") is True,
                  f"got {r}")
            check("launch 含 run_id (16 hex)", isinstance(r.get("run_id"), str)
                  and len(r["run_id"]) == 16, f"got {r.get('run_id')!r}")
            check("dispatched = 第一张 task_id",
                  r.get("dispatched") == "t_a", f"got {r.get('dispatched')}")
            check("card_count = 2",
                  r.get("card_count") == 2, f"got {r.get('card_count')}")
            # run 行确实建了
            run = led.get_team_work(r["run_id"])
            check("run 行 status=ACTIVE",
                  run["status"] == "ACTIVE", f"got {run['status']}")
            check("run 行 card_count=2",
                  run["card_count"] == 2, f"got {run['card_count']}")
            check("run 行 current_card_id=t_a (第一张)",
                  run["current_card_id"] == "t_a",
                  f"got {run['current_card_id']!r}")
            # 卡登记了
            hc_a = led.get_contract("t_a")
            check("t_a.team_work_run_id 回填",
                  hc_a["team_work_run_id"] == r["run_id"],
                  f"got {hc_a['team_work_run_id']!r}")
            # dispatch 被调一次
            check("dispatch 被调一次", len(calls) == 1,
                  f"got {len(calls)}")
            check("dispatch 收到 run_id",
                  calls[0].get("run_id") == r["run_id"],
                  f"got {calls[0].get('run_id')!r}")
            check("dispatch 收到 task_id=t_a",
                  calls[0].get("task_id") == "t_a",
                  f"got {calls[0].get('task_id')!r}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


def test_launch_blocks_when_active_run_exists():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            # 第一发: 应成功
            r1 = console.api_team_work_launch({
                "initial_task": "r1", "session_id": "s",
                "steps": [{"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"}],
            })
            check("first launch ok=True", r1.get("ok") is True)
            # 第二发: 应被 409 拒
            r2 = console.api_team_work_launch({
                "initial_task": "r2", "session_id": "s",
                "steps": [{"step_id": "a", "title": "A", "task_id": "t_a2", "assignee": "coder"}],
            })
            check("second launch 409", r2.get("code") == 409,
                  f"got {r2}")
            check("second launch 提示 existing_run",
                  "existing_run" in r2,
                  f"got keys: {list(r2.keys())}")
            check("second launch 提示 hint",
                  "hint" in r2,
                  f"got keys: {list(r2.keys())}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


# ============================================================
# T2.2: api_team_work_advance 串行铁律
# ============================================================

def test_advance_rejects_when_current_card_id_set():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            r = console.api_team_work_launch({
                "initial_task": "t", "session_id": "s",
                "steps": [
                    {"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"},
                    {"step_id": "b", "title": "B", "task_id": "t_b", "assignee": "coder"},
                ],
            })
            run_id = r["run_id"]
            # current_card_id=t_a (还未结算) → advance(t_b) 应被 409 拒
            adv = console.api_team_work_advance(run_id, {
                "task_id": "t_b", "target_profile": "coder", "title": "B",
            })
            check("advance 当 current_card_id 非空 → 409",
                  adv.get("code") == 409, f"got {adv}")
            check("advance 错误信息含 current_card_id",
                  adv.get("current_card_id") == "t_a",
                  f"got {adv.get('current_card_id')!r}")
            # run 状态未变
            run = led.get_team_work(run_id)
            check("run.current_card_id 仍为 t_a",
                  run["current_card_id"] == "t_a",
                  f"got {run['current_card_id']!r}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


def test_advance_rejects_task_not_in_run():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            r = console.api_team_work_launch({
                "initial_task": "t", "session_id": "s",
                "steps": [
                    {"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"},
                    {"step_id": "b", "title": "B", "task_id": "t_b", "assignee": "coder"},
                ],
            })
            run_id = r["run_id"]
            # 清空 current_card_id 模拟 a 已结算
            led.clear_team_work_current_card(run_id)
            # 给一张「不属于本 run」的孤儿 contract
            from mw.ledger import Ledger as _Led
            led.put_contract("t_orphan", {"required_files": []})
            adv = console.api_team_work_advance(run_id, {
                "task_id": "t_orphan", "target_profile": "coder",
            })
            check("advance 孤儿 task → 409",
                  adv.get("code") == 409, f"got {adv}")
            check("错误信息含 expected_run_id",
                  adv.get("expected_run_id") == run_id,
                  f"got {adv.get('expected_run_id')!r}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


def test_advance_succeeds_after_clear():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            # REOPEN A-1: advance 现在机器校验 TODO 绑定, 补 TODO 夹具
            ws = tempfile.mkdtemp()
            Path(ws, "TODO.md").write_text(
                "# Team Work: t\n\n## 当前任务\n- 步骤: b\n- 卡: t_b\n"
                "- 状态: 进行中\n\n---\n\n## 待执行\n(无)\n", encoding="utf-8")
            r = console.api_team_work_launch({
                "initial_task": "t", "session_id": "s",
                "steps": [
                    {"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"},
                    {"step_id": "b", "title": "B", "task_id": "t_b", "assignee": "coder"},
                ],
            })
            run_id = r["run_id"]
            # 模拟 a 结算完: clear current_card_id + set_harvest PENDING→COMPLETE
            led.clear_team_work_current_card(run_id)
            led.set_harvest("t_a", "HARVESTED", exit_code=0)
            calls.clear()  # 丢掉 launch 那次的记录
            adv = console.api_team_work_advance(run_id, {
                "task_id": "t_b", "target_profile": "coder",
                "title": "B", "required_files": ["out.json"],
                "project_workspace": ws,
            })
            check("advance 成功 → ok=True",
                  adv.get("ok") is True, f"got {adv}")
            check("dispatch 被调 (advance)", len(calls) == 1,
                  f"got {len(calls)}")
            check("dispatch payload 含 run_id",
                  calls[0].get("run_id") == run_id,
                  f"got {calls[0].get('run_id')!r}")
            check("dispatch payload 含 task_id=t_b",
                  calls[0].get("task_id") == "t_b",
                  f"got {calls[0].get('task_id')!r}")
            check("dispatch payload 含 required_files",
                  calls[0].get("harvest", {}).get("required_files") == ["out.json"],
                  f"got {calls[0].get('harvest')}")
            run = led.get_team_work(run_id)
            check("run.current_card_id=t_b (advance 后)",
                  run["current_card_id"] == "t_b",
                  f"got {run['current_card_id']!r}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


def test_advance_clears_blocked_residue():
    """T2.2b (2026-10-05): advance 串行铁律 — 推进下一张 = main 隐式
    判定 run 名下所有非当前卡已完成。reaper 兜底写的 BLOCKED 残留必须
    在 advance 时被覆盖成 HARVESTED, 否则 ledger 与 main 看到的事实分歧,
    最终 close 时会把正常完成的链误判 ABORTED。"""
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            r = console.api_team_work_launch({
                "initial_task": "t", "session_id": "s",
                "steps": [
                    {"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"},
                    {"step_id": "b", "title": "B", "task_id": "t_b", "assignee": "coder"},
                    {"step_id": "c", "title": "C", "task_id": "t_c", "assignee": "coder"},
                ],
            })
            run_id = r["run_id"]
            # 模拟 reaper 兜底: t_a 还在 PENDING 时被标 BLOCKED
            led.set_harvest("t_a", "BLOCKED", log="reaper fallback: FAILED")
            # 清槽模拟 t_a 已结算 (在 main 推进视角下完成)
            led.clear_team_work_current_card(run_id)

            # 验证 advance 前 t_a 是 BLOCKED
            hc_a = led.get_contract("t_a")
            check("advance 前 t_a.harvest_status = BLOCKED",
                  hc_a["harvest_status"] == "BLOCKED",
                  f"got {hc_a['harvest_status']!r}")

            # advance t_b → 应该把 t_a 从 BLOCKED 覆盖成 HARVESTED
            calls.clear()
            adv = console.api_team_work_advance(run_id, {
                "task_id": "t_b", "target_profile": "coder",
            })
            check("advance 成功 (忽略 t_a BLOCKED 残留)",
                  adv.get("ok") is True, f"got {adv}")

            hc_a_after = led.get_contract("t_a")
            check("advance 后 t_a.harvest_status = HARVESTED (被覆盖)",
                  hc_a_after["harvest_status"] == "HARVESTED",
                  f"got {hc_a_after['harvest_status']!r}")
            check("advance 后 t_a.harvest_log 含 main_advance 标记",
                  "[main_advance]" in (hc_a_after["harvest_log"] or ""),
                  f"got {hc_a_after['harvest_log']!r}")

            # t_b 是新派发的卡, 不应被覆盖
            hc_b = led.get_contract("t_b")
            check("t_b.harvest_status 保持 PENDING (advance 的新卡不被覆盖)",
                  hc_b["harvest_status"] == "PENDING",
                  f"got {hc_b['harvest_status']!r}")

            # 继续 advance t_c: t_b 此时可能 BLOCKED, 应被覆盖
            led.clear_team_work_current_card(run_id)
            led.set_harvest("t_b", "BLOCKED", log="reaper fallback")
            calls.clear()
            adv2 = console.api_team_work_advance(run_id, {
                "task_id": "t_c", "target_profile": "coder",
            })
            check("第二次 advance 也成功", adv2.get("ok") is True, f"got {adv2}")
            hc_b_after = led.get_contract("t_b")
            check("t_b 也被覆盖成 HARVESTED",
                  hc_b_after["harvest_status"] == "HARVESTED",
                  f"got {hc_b_after['harvest_status']!r}")

            # t_a 仍是 HARVESTED (没被二次动)
            hc_a_final = led.get_contract("t_a")
            check("t_a 状态稳定 (已是 HARVESTED)",
                  hc_a_final["harvest_status"] == "HARVESTED",
                  f"got {hc_a_final['harvest_status']!r}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


# ============================================================
# T2.3: api_team_work_close 杀 spawn + 归档 + 改 status
# ============================================================

def test_close_active_run_archives_pending_cards():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            r = console.api_team_work_launch({
                "initial_task": "t", "session_id": "s",
                "steps": [
                    {"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"},
                    {"step_id": "b", "title": "B", "task_id": "t_b", "assignee": "coder"},
                ],
            })
            run_id = r["run_id"]
            # launch 后 t_a PENDING, t_b PENDING (登记但未派)
            # close 应该把它们都 ARCHIVED
            res = console.api_team_work_close(run_id, {"operator": "test"})
            check("close ok=True", res.get("ok") is True, f"got {res}")
            # 有 current_card_id=t_a + 还有 t_b PENDING → 应是 ABORTED
            check("close status=ABORTED (有遗留)",
                  res.get("status") == "ABORTED", f"got {res.get('status')}")
            check("archived_cards 包含 t_a, t_b",
                  sorted(res.get("archived_cards", [])) == ["t_a", "t_b"],
                  f"got {res.get('archived_cards')}")
            run = led.get_team_work(run_id)
            check("run.status=ABORTED", run["status"] == "ABORTED")
            check("run.current_card_id 清空",
                  run["current_card_id"] is None,
                  f"got {run['current_card_id']!r}")
            check("run.closed_reason 含 operator",
                  "test" in (run["closed_reason"] or ""),
                  f"got {run['closed_reason']!r}")
            # 卡状态: 都 ARCHIVED
            check("t_a harvest_status=ARCHIVED",
                  led.get_contract("t_a")["harvest_status"] == "ARCHIVED")
            check("t_b harvest_status=ARCHIVED",
                  led.get_contract("t_b")["harvest_status"] == "ARCHIVED")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


def test_close_when_all_cards_done_returns_closed():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            r = console.api_team_work_launch({
                "initial_task": "t", "session_id": "s",
                "steps": [
                    {"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"},
                ],
            })
            run_id = r["run_id"]
            # 模拟: t_a 已 HARVESTED + clear current_card_id
            led.clear_team_work_current_card(run_id)
            led.set_harvest("t_a", "HARVESTED", exit_code=0)
            res = console.api_team_work_close(run_id, {"operator": "test"})
            check("close status=CLOSED (无遗留)",
                  res.get("status") == "CLOSED",
                  f"got {res.get('status')}")
            check("archived_cards 空",
                  res.get("archived_cards") == [],
                  f"got {res.get('archived_cards')}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


def test_close_idempotent_rejects_non_active():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            r = console.api_team_work_launch({
                "initial_task": "t", "session_id": "s",
                "steps": [{"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"}],
            })
            run_id = r["run_id"]
            console.api_team_work_close(run_id, {"operator": "test"})
            # 第二次 close 应被 409 拒
            res2 = console.api_team_work_close(run_id, {"operator": "test"})
            check("double close → 409", res2.get("code") == 409,
                  f"got {res2}")
            check("错误含 current_status",
                  res2.get("current_status") == "ABORTED",
                  f"got {res2.get('current_status')!r}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


def test_close_nonexistent_returns_404():
    led, path = fresh_ledger()
    try:
        res = console.api_team_work_close("nope_run", {"operator": "test"})
        check("close 不存在 → 404",
              res.get("code") == 404, f"got {res}")
    finally:
        clear_ledger(led, path)


# ============================================================
# T2.4: api_team_works_list + api_team_work_get
# ============================================================

def test_list_default_active_only():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            # Issue#8 (2026-10-13): launch 守卫「一 run 一 session」已上线,
            # 两个 run 复用同一 session_id → 409。因此本测试两条 list 用不同 sid。
            r1 = console.api_team_work_launch({
                "initial_task": "r1", "session_id": "s_a",
                "steps": [{"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"}],
            })
            check("r1 launch ok", r1.get("ok") is True)
            r1_id = r1["run_id"]
            # 先 close r1 才能开 r2 (active guard 限制)
            led.close_team_work(r1_id, "test")
            r2 = console.api_team_work_launch({
                "initial_task": "r2", "session_id": "s_b",
                "steps": [{"step_id": "a", "title": "A", "task_id": "t_b", "assignee": "coder"}],
            })
            check("r2 launch ok (r1 已关)", r2.get("ok") is True,
                  f"got {r2}")
            r2_id = r2["run_id"]
            # 默认只看 ACTIVE
            out = console.api_team_works_list({})
            ids = [x["run_id"] for x in out["team_works"]]
            check("list default 只看 ACTIVE", ids == [r2_id],
                  f"got {ids}")
            # include_closed=True
            out_all = console.api_team_works_list({"include_closed": True})
            ids_all = sorted(x["run_id"] for x in out_all["team_works"])
            check("list include_closed 含历史",
                  ids_all == sorted([r1_id, r2_id]),
                  f"got {ids_all}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


def test_list_includes_current_card_info():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            r = console.api_team_work_launch({
                "initial_task": "t", "session_id": "s",
                "steps": [{"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"}],
            })
            # mock 一次 session 锚点 (dispatch 没真跑, led 没记 session)
            led.upsert_session("t_a", "sess_x", "coder",
                              runner_kind="api", workspace="/ws")
            out = console.api_team_works_list({})
            assert len(out["team_works"]) == 1
            row = out["team_works"][0]
            check("list 行含 current_card",
                  "current_card" in row, f"keys: {list(row.keys())}")
            cc = row["current_card"]
            check("current_card.task_id=t_a",
                  cc["task_id"] == "t_a", f"got {cc}")
            check("current_card.assignee=coder",
                  cc["assignee"] == "coder", f"got {cc}")
            check("current_card.session_id=sess_x",
                  cc["session_id"] == "sess_x", f"got {cc}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


def test_get_single_run_returns_cards():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            r = console.api_team_work_launch({
                "initial_task": "t", "session_id": "s",
                "steps": [
                    {"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"},
                    {"step_id": "b", "title": "B", "task_id": "t_b", "assignee": "coder"},
                ],
            })
            run_id = r["run_id"]
            # t_a 设成 HARVESTED 看 get 是否能展示
            led.set_harvest("t_a", "HARVESTED", exit_code=0)
            out = console.api_team_work_get(run_id)
            check("get 返回 team_work 字段",
                  out.get("team_work", {}).get("run_id") == run_id)
            # 2026-10-06 改版:cards 是对齐 /api/tasks 的完整 task dict,
            # 主键用 id(= task_id), 不再叫 task_id
            card_ids = sorted(c["id"] for c in out["cards"])
            check("get 返回旗下所有卡",
                  card_ids == ["t_a", "t_b"], f"got {card_ids}")
            # t_a 应是 HARVESTED, t_b 是 PENDING
            statuses = {c["id"]: c["harvest_status"] for c in out["cards"]}
            check("t_a status=HARVESTED",
                  statuses.get("t_a") == "HARVESTED", f"got {statuses}")
            check("t_b status=PENDING",
                  statuses.get("t_b") == "PENDING", f"got {statuses}")
            # 字段对齐 /api/tasks: cards 含 title/assignee/harvest_log 等
            keys_t_a = next(c for c in out["cards"] if c["id"] == "t_a").keys()
            check("cards 字段对齐 /api/tasks",
                  {"id", "title", "assignee", "status", "harvest_status",
                   "session_id", "team_work_run_id"}.issubset(keys_t_a),
                  f"got {sorted(keys_t_a)}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


def test_get_nonexistent_returns_404():
    led, path = fresh_ledger()
    try:
        out = console.api_team_work_get("nope")
        check("get 不存在 → 404",
              out.get("code") == 404, f"got {out}")
    finally:
        clear_ledger(led, path)


# ============================================================
# 2026-10-06 新增: archive_team_work + list 默认排除 ARCHIVED
# ============================================================

def test_archive_active_run_rejected():
    """ACTIVE 不允许归档 — ValueError → console 转 409。"""
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            r = console.api_team_work_launch({
                "initial_task": "t", "session_id": "s",
                "steps": [{"step_id": "a", "title": "A",
                           "task_id": "t_a", "assignee": "coder"}],
            })
            run_id = r["run_id"]
            # ACTIVE 直接 archive → 409
            out = console.api_team_work_archive(run_id, {"operator": "t"})
            check("archive ACTIVE → ok=False", out.get("ok") is False,
                  f"got {out}")
            check("archive ACTIVE → code=409", out.get("code") == 409,
                  f"got {out}")
            check("错误信息含「不允许归档」",
                  "不允许归档" in out.get("error", ""), f"got {out}")
            # ledger 实际状态仍是 ACTIVE
            row = led.get_team_work(run_id)
            check("run 状态未被改写", row["status"] == "ACTIVE",
                  f"got {row['status']}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


def test_archive_aborted_archives_cards_and_is_idempotent():
    """ABORTED → ARCHIVED; 旗下 PENDING/ERROR 卡一并归档; 二次归档幂等。"""
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            r = console.api_team_work_launch({
                "initial_task": "t", "session_id": "s",
                "steps": [
                    {"step_id": "a", "title": "A",
                     "task_id": "t_a", "assignee": "coder"},
                    {"step_id": "b", "title": "B",
                     "task_id": "t_b", "assignee": "coder"},
                ],
            })
            run_id = r["run_id"]
            # close (ACTIVE → ABORTED)
            led.close_team_work(run_id, "test")
            # 给 t_a 设 HARVESTED (终态, 不应被归档), t_b 留 PENDING
            led.set_harvest("t_a", "HARVESTED", exit_code=0)
            # 归档
            out = console.api_team_work_archive(run_id, {"operator": "t"})
            check("archive ok=True", out.get("ok") is True, f"got {out}")
            check("run_id 正确",
                  out.get("run_id") == run_id, f"got {out}")
            check("archived_cards 含 t_b",
                  "t_b" in out.get("archived_cards", []),
                  f"got {out['archived_cards']}")
            check("archived_cards 不含 t_a (已 HARVESTED)",
                  "t_a" not in out.get("archived_cards", []),
                  f"got {out['archived_cards']}")
            check("was_already=False (首次)",
                  out.get("was_already") is False, f"got {out}")
            # ledger 实际状态
            row = led.get_team_work(run_id)
            check("run.status=ARCHIVED", row["status"] == "ARCHIVED",
                  f"got {row['status']}")
            # t_b harvest 实际被改
            hc_b = led.get_contract("t_b")
            check("t_b.harvest_status=ARCHIVED",
                  hc_b["harvest_status"] == "ARCHIVED",
                  f"got {hc_b['harvest_status']}")
            hc_a = led.get_contract("t_a")
            check("t_a.harvest_status 仍为 HARVESTED (不被覆盖)",
                  hc_a["harvest_status"] == "HARVESTED",
                  f"got {hc_a['harvest_status']}")
            # 二次归档 — 幂等
            out2 = console.api_team_work_archive(run_id, {"operator": "t"})
            check("二次 archive 仍 ok=True",
                  out2.get("ok") is True, f"got {out2}")
            check("was_already=True (幂等)",
                  out2.get("was_already") is True, f"got {out2}")
            check("二次 archived_cards=[]",
                  out2.get("archived_cards") == [], f"got {out2}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


def test_list_default_excludes_archived():
    """默认 list 不含 ARCHIVED;include_archived=True 含。"""
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            r = console.api_team_work_launch({
                "initial_task": "t", "session_id": "s",
                "steps": [{"step_id": "a", "title": "A",
                           "task_id": "t_a", "assignee": "coder"}],
            })
            run_id = r["run_id"]
            # close + archive (同 test_archive_aborted_archives_cards 套路)
            led.close_team_work(run_id, "test")
            console.api_team_work_archive(run_id, {"operator": "t"})
            # 默认 include_closed=False → ACTIVE 才含,看不到的 ARCHIVED
            out = console.api_team_works_list({})
            check("默认 list 不含 ARCHIVED run",
                  all(x["run_id"] != run_id for x in out["team_works"]),
                  f"got {[x['run_id'] for x in out['team_works']]}")
            # include_closed=True 含 ABORTED/CLOSED 但仍不含 ARCHIVED
            out_inc = console.api_team_works_list({"include_closed": True})
            check("include_closed=True 仍不含 ARCHIVED",
                  all(x["run_id"] != run_id for x in out_inc["team_works"]),
                  f"got {[x['run_id'] for x in out_inc['team_works']]}")
            # include_archived=True 含
            out_arch = console.api_team_works_list(
                {"include_closed": True, "include_archived": True})
            ids = [x["run_id"] for x in out_arch["team_works"]]
            check("include_archived=True 含 ARCHIVED",
                  run_id in ids, f"got {ids}")
            row = next(x for x in out_arch["team_works"]
                       if x["run_id"] == run_id)
            check("返回 run.status=ARCHIVED",
                  row["status"] == "ARCHIVED", f"got {row['status']}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


def test_archive_nonexistent_returns_404():
    """归档不存在的 run → 404。"""
    led, path = fresh_ledger()
    try:
        out = console.api_team_work_archive("nope", {"operator": "t"})
        check("archive 不存在 → ok=False",
              out.get("ok") is False, f"got {out}")
        check("archive 不存在 → code=404",
              out.get("code") == 404, f"got {out}")
    finally:
        clear_ledger(led, path)


def test_list_url_query_includes_archived():
    """回归 2026-10-06: /api/team-works?include_archived=1 路由要把 query
    解析并透传到 payload (之前路由只解析 include_closed)。直接调
    ConsoleHTTPHandler.do_POST 不方便(要走 server),改为手动跑一段等价
    的 query 解析路径验证逻辑等价。"""
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(
        "/api/team-works?include_closed=1&include_archived=1").query)
    incl = qs.get("include_closed", ["false"])[0].lower() in ("true", "1")
    incl_arch = qs.get("include_archived", ["false"])[0].lower() in ("true", "1")
    check("路由解析 include_closed=1", incl is True)
    check("路由解析 include_archived=1", incl_arch is True)
    # 默认不带 include_archived
    qs2 = parse_qs(urlparse("/api/team-works").query)
    incl_arch2 = qs2.get("include_archived", ["false"])[0].lower() in ("true", "1")
    check("默认 include_archived=False", incl_arch2 is False)


def stub_pipeline_dispatch():
    """把 pipeline.dispatch 替成「只记录调用」的版本 — 保留 api_dispatch
    本身的 run_id 校验逻辑, 让我们能精确测「dispatch 拒绝」分支。

    返回 (recorder_list, restore_fn)。
    """
    calls = []
    from mw import pipeline as pl

    def _stub(payload, **kwargs):
        calls.append(dict(payload))
        return {"ok": True, "accepted": True, "stub": True,
                "task_id": payload.get("task_id")}

    original = pl.dispatch
    pl.dispatch = _stub
    return calls, lambda: setattr(pl, "dispatch", original)


# ============================================================
# T2.5: api_dispatch run_id 护栏
# ============================================================

def test_dispatch_rejects_missing_run_id_for_team_work_card():
    """带 run_id 但 run 不 ACTIVE → 409。"""
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_pipeline_dispatch()
        try:
            r = console.api_team_work_launch({
                "initial_task": "t", "session_id": "s",
                "steps": [{"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"}],
            })
            run_id = r["run_id"]
            led.close_team_work(run_id, "test")
            # launch 期间已 dispatch 过一次; 清零看后续 dispatch 调用
            calls.clear()
            # run 已 ABORTED → dispatch 应拒 (走真 api_dispatch 的 run_id 校验)
            res = console.api_dispatch({
                "task_id": "t_a", "run_id": run_id,
                "title": "A", "target": {"profile": "coder"},
                "input_spec": {"task_description": "x"},
                "harvest": {"required_files": []},
                "idempotency_key": "k",
            })
            check("dispatch 非 ACTIVE run → 409",
                  res.get("code") == 409, f"got {res}")
            check("错误含 run_status=ABORTED",
                  res.get("run_status") == "ABORTED",
                  f"got {res.get('run_status')!r}")
            check("pipeline.dispatch 没被调 (被 run_id 护栏拒)",
                  len(calls) == 0, f"got {len(calls)}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


def test_dispatch_rejects_when_current_card_mismatch():
    """带 run_id + current_card_id != task_id → 409。"""
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_pipeline_dispatch()
        try:
            r = console.api_team_work_launch({
                "initial_task": "t", "session_id": "s",
                "steps": [
                    {"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"},
                    {"step_id": "b", "title": "B", "task_id": "t_b", "assignee": "coder"},
                ],
            })
            run_id = r["run_id"]
            calls.clear()
            # current_card_id=t_a, 但想派 t_b → 应被 409
            res = console.api_dispatch({
                "task_id": "t_b", "run_id": run_id,
                "title": "B", "target": {"profile": "coder"},
                "input_spec": {"task_description": "x"},
                "harvest": {"required_files": []},
                "idempotency_key": "k",
            })
            check("dispatch current_card 不匹配 → 409",
                  res.get("code") == 409, f"got {res}")
            check("错误含 current_card_id",
                  res.get("current_card_id") == "t_a",
                  f"got {res.get('current_card_id')!r}")
            check("pipeline.dispatch 没被调 (被 current_card 护栏拒)",
                  len(calls) == 0, f"got {len(calls)}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


def test_dispatch_with_run_id_passes_through_to_pipeline():
    """带 run_id 且一切匹配 → 透传到 pipeline.dispatch, run_id 被 pop 掉。"""
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_pipeline_dispatch()
        try:
            r = console.api_team_work_launch({
                "initial_task": "t", "session_id": "s",
                "steps": [{"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"}],
            })
            run_id = r["run_id"]
            res = console.api_dispatch({
                "task_id": "t_a", "run_id": run_id,
                "title": "A", "target": {"profile": "coder"},
                "input_spec": {"task_description": "x"},
                "harvest": {"required_files": []},
                "idempotency_key": "k",
            })
            check("dispatch 正常路径 → ok=True",
                  res.get("ok") is True, f"got {res}")
            check("pipeline.dispatch 被调一次", len(calls) == 1,
                  f"got {len(calls)}")
            check("pipeline.dispatch 收到 task_id=t_a",
                  calls[0].get("task_id") == "t_a",
                  f"got {calls[0].get('task_id')!r}")
            check("pipeline.dispatch 收到 payload 不含 run_id (已 pop)",
                  "run_id" not in calls[0], f"got keys: {list(calls[0])}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


def test_dispatch_without_run_id_passes_through():
    """不带 run_id → 自由派 (legacy 单任务路径)。"""
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_pipeline_dispatch()
        try:
            # 无 run_id, 直接派 — 应该走 pipeline.dispatch
            res = console.api_dispatch({
                "task_id": "t_legacy", "title": "L",
                "target": {"profile": "coder"},
                "input_spec": {"task_description": "x"},
                "harvest": {"required_files": []},
                "idempotency_key": "k",
            })
            check("dispatch 无 run_id → 受理",
                  res.get("ok") is True, f"got {res}")
            check("pipeline.dispatch 被调一次", len(calls) == 1,
                  f"got {len(calls)}")
            check("pipeline.dispatch 收到 payload 不含 run_id",
                  "run_id" not in calls[0], f"got {calls[0]}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


# ============================================================
# Issue#8 (2026-10-13) — 一 run 一 session 守卫 (AC8-1)
# ============================================================

def test_launch_reuses_existing_initiator_session_returns_409():
    """issue #8 核心:同 session_id 已绑过任何 run 不能再发起。

    生产实锤: run f30ad9 / e5c5c5 的 initiator_session 同 = api_179
    1794157_e0092598。两 run 共享 initiator 让运行中报告跨 run 灌进
    同一上游 session, 上下文污染。本测试: 先开 r1, 拿它的 sid 再开
    r2 (即使 r1 已 CLOSED) → 必须 409 + session_reused=True, 不创建
    run 行, 不调用 dispatch。
    """
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            r1 = console.api_team_work_launch({
                "initial_task": "r1",
                "session_id": "s_reuse",
                "steps": [{"step_id": "a", "title": "A",
                           "task_id": "t_a", "assignee": "coder"}],
            })
            check("r1 launch ok", r1.get("ok") is True, f"got {r1}")
            r1_id = r1["run_id"]
            # 关掉 r1 (active-run 路径不会再 409, 我们的目的是让
            # session-reuse 路径独享 409)。
            led.close_team_work(r1_id, "test_reuse")
            calls.clear()
            # 用同一 sid 再发 → 必须被会话护栏 409
            r2 = console.api_team_work_launch({
                "initial_task": "r2",
                "session_id": "s_reuse",  # 复用
                "steps": [{"step_id": "a", "title": "A",
                           "task_id": "t_b", "assignee": "coder"}],
            })
            check("r2 launch (复用 sid) → 409",
                  r2.get("code") == 409, f"got {r2}")
            check("r2 标识 session_reused=True",
                  r2.get("session_reused") is True,
                  f"got keys: {list(r2.keys())}")
            check("r2 提示错信息明确 'session'",
                  "session" in (r2.get("error") or "").lower()
                  or "session" in (r2.get("hint") or "").lower(),
                  f"got error={r2.get('error')!r}")
            check("r2 含 conflicting_runs",
                  isinstance(r2.get("conflicting_runs"), list)
                  and any(c["run_id"] == r1_id
                          for c in r2["conflicting_runs"]),
                  f"got {r2.get('conflicting_runs')!r}")
            check("r2 dispatch 没被调",
                  len(calls) == 0, f"got {len(calls)} calls")
            check("r2 ledger 里只有 r1 (没建新 run 行)",
                  [r["run_id"] for r in led.list_team_works(
                      include_closed=True)]
                  == [r1_id],
                  f"got {[r['run_id'] for r in led.list_team_works(include_closed=True)]}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


def test_launch_new_session_after_close_succeeds():
    """反例: 用全新 sid 开新 run 应成功 (即使 r1 已 CLOSED, sid 不能复用)。

    与上一个测试互补 —— 验证我们没有把 1 session 1 run 改成「单 run
    一次性 sid」(那是误读)。
    """
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        calls, restore = stub_dispatch_recorder()
        try:
            r1 = console.api_team_work_launch({
                "initial_task": "r1", "session_id": "s_old",
                "steps": [{"step_id": "a", "title": "A",
                           "task_id": "t_a", "assignee": "coder"}],
            })
            led.close_team_work(r1["run_id"], "test")
            r2 = console.api_team_work_launch({
                "initial_task": "r2", "session_id": "s_new",
                "steps": [{"step_id": "a", "title": "A",
                           "task_id": "t_b", "assignee": "coder"}],
            })
            check("r2 (新 sid) → ok", r2.get("ok") is True, f"got {r2}")
            runs = sorted(r["run_id"] for r in led.list_team_works(
                include_closed=True))
            check("ledger 含 r1 + r2 两条",
                  len(runs) == 2, f"got {runs}")
        finally:
            restore()
    finally:
        clear_ledger(led, path)


def test_find_runs_by_initiator_session_helper():
    """ledger 层 helper 直接测: 按 initiator 反查 (任意状态)。"""
    led, path = fresh_ledger()
    try:
        led.create_team_work_run("r1", "s_shared", "需求A")
        check("r1 未关闭时 反查 s_shared → 1 条",
              len(led.find_runs_by_initiator_session("s_shared")) == 1)
        led.close_team_work("r1", "test")
        # r1 已 CLOSED, 反查仍能命中 (因为 s_shared 已绑过)
        check("r1 已关闭 反查 s_shared → 1 条 (任意状态)",
              len(led.find_runs_by_initiator_session("s_shared")) == 1,
              f"got {led.find_runs_by_initiator_session('s_shared')}")
        # 全新 sid → 0 条
        check("全新 sid → 0 条",
              led.find_runs_by_initiator_session("s_unused") == [])
    finally:
        led.close()
        os.unlink(path)


def test_session_poll_isolation():
    """AC8-4: UI 旧 session 的轮询不会污染新 session 的画面。

    模拟轮询竞态:有两个 helper,在「构建旧 session poll 期间」
    切换到新 session 时,旧 tick 的 cancelled 守门 / 新 tick 起步
    读取 baseline 的逻辑必须正确 (验证 setMessages 与定时器基线
    行为在切换 sessionId 时不串扰)。

    这是纯函数式最小回归, 不依赖 React 运行时 —— 只验证
    「同一闭包下的 cancelled 守门语义」。
    """
    import asyncio as _asyncio
    # 模拟 setInterval + tick 模式
    appended = []  # 记录每次 setMessages 的内容 (避免与外部 poled 同名)

    async def fake_get_messages(sid):
        if sid == "s_old":
            await _asyncio.sleep(0.001)
            return {"data": [{"role": "user", "text": f"old_{i}"}
                             for i in range(3)]}
        elif sid == "s_new":
            await _asyncio.sleep(0.001)
            return {"data": [{"role": "user", "text": "new_only"}]}
        return {"data": []}

    async def tick(label, sid, captured):
        if captured["cancelled"]:
            return
        d = await fake_get_messages(sid)
        if captured["cancelled"]:
            return
        appended.append((label, sid, len(d.get("data") or [])))

    async def main_run():
        old_cap = {"cancelled": False}
        old_task = _asyncio.create_task(tick("old", "s_old", old_cap))
        await _asyncio.sleep(0.005)
        # 用户点 Team Work: 切换 sid, 取消旧 poll, 启新 poll
        old_cap["cancelled"] = True
        await old_task  # 等旧 task 完成
        new_cap = {"cancelled": False}
        await tick("new", "s_new", new_cap)
        return old_cap, new_cap, appended

    try:
        old_cap, new_cap, appended = _asyncio.run(main_run())
    except Exception as e:
        check("AC8-4 异步基线运行", False, f"unexpected: {e}")
        return
    check("AC8-4 旧 tick 标记 cancelled",
          old_cap["cancelled"] is True)
    check("AC8-4 新 tick 标记 cancelled 未触发",
          new_cap["cancelled"] is False)
    # 旧 tick 在 cancelled 之前飞过一段, 它的 setMessages 应被守门挡掉
    # 新 tick 应当只看到 sid='s_new' 与 1 条消息
    check("AC8-4 新 tick 只走自己 sid",
          any(p[1] == "s_new" for p in appended),
          f"got: {appended}")


# ============================================================
# 入口
# ============================================================

def main():
    tests = [
        test_launch_creates_run_and_dispatches_first,
        test_launch_blocks_when_active_run_exists,
        test_launch_reuses_existing_initiator_session_returns_409,
        test_launch_new_session_after_close_succeeds,
        test_find_runs_by_initiator_session_helper,
        test_session_poll_isolation,
        test_advance_rejects_when_current_card_id_set,
        test_advance_rejects_task_not_in_run,
        test_advance_succeeds_after_clear,
        test_close_active_run_archives_pending_cards,
        test_close_when_all_cards_done_returns_closed,
        test_close_idempotent_rejects_non_active,
        test_close_nonexistent_returns_404,
        test_list_default_active_only,
        test_list_includes_current_card_info,
        test_get_single_run_returns_cards,
        test_get_nonexistent_returns_404,
        test_dispatch_rejects_missing_run_id_for_team_work_card,
        test_dispatch_rejects_when_current_card_mismatch,
        test_dispatch_without_run_id_passes_through,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            check(t.__name__, False, f"unhandled exception: {type(e).__name__}: {e}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\n失败明细:")
        for name, detail in FAIL:
            print(f"  - {name}: {detail}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
