"""2026-10-04 补充卡死锁修复 (F1-F6) 回归测试。

覆盖分支 (每个新分支一条, 按 skill 铁律):
  F1  advance 未带 project_workspace → 继承 run.project_workspace
  F1b advance 显式带 project_workspace → 不被 run 覆盖
  F2  resume 成功 + 槽 NULL + 卡属 ACTIVE run → 重占 current_card_id
  F2b resume 时槽被别的卡占着 → 不越权抢槽
  F3  resume 超时 → 视为已受理 (ok=True, 拨回 PENDING), 非超时错误仍抛
  F4  run ACTIVE + 槽 NULL + 名下有未结算卡 + 闲置超阈值 → 兜底敲门一次
  F4b 同窗口第二次不重复敲 (run 级去重)
  F4c 名下无未结算卡 → 不敲 (链走完了)
  F5  set_worker_report 内容变化 → 清 dead_until/attempts
  F5b _send_to_main 429 → 短退避不计 attempts; 200 → confirm_delivered
  F6  stuck 敲门活体抑制: 探 worker 最后消息 timestamp, 距 now < 窗口
      (默认 stuck_after_min × 60s, 也可 MW_REAPER_SUPPRESS_WINDOW_S 覆写)
      → SUPPRESSED_ALIVE; ≥ 窗口 → 敲; 探测失败 → 敲

脚本风格 (check + main at EOF), 跑法: python3 tests/test_slot_recovery_f1_f6.py
"""
import os
import sys
import tempfile
import time
import json
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mw.console as console
import mw.pipeline as pipeline
import mw.reaper as reaper
from mw.ledger import Ledger

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))


def fresh_ledger():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    console.LEDGER_DB = f.name
    return Ledger(f.name), f.name


def stub_dispatch_recorder():
    original = console.api_dispatch
    calls = []

    def _stub(payload):
        calls.append(payload)
        return {"ok": True, "task_id": payload.get("task_id")}
    console.api_dispatch = _stub
    return calls, lambda: setattr(console, "api_dispatch", original)


def stub_workers_registry():
    reg = {"coder": {"api_url": "http://x", "api_key": "k", "model": "m"}}
    pipeline.load_registry = lambda path: reg
    return reg


# ── F1: advance 继承 run.project_workspace ──────────────────────

def test_f1_advance_inherits_project_workspace():
    led, path = fresh_ledger()
    try:
        calls, restore = stub_dispatch_recorder()
        # REOPEN A-1: launch/advance 均机器校验 TODO 绑定, 补临时 TODO 夹具
        ws = tempfile.mkdtemp()
        Path(ws, "TODO.md").write_text(
            "# Team Work: t\n\n## 当前任务\n- 步骤: a\n- 卡: t_a\n"
            "- 状态: 进行中\n\n---\n\n## 待执行\n(无)\n", encoding="utf-8")
        r = console.api_team_work_launch({
            "initial_task": "t", "session_id": "s",
            "project_workspace": ws,
            "steps": [
                {"step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder"},
                {"step_id": "b", "title": "B", "task_id": "t_b", "assignee": "coder"},
            ],
        })
        run_id = r["run_id"]
        led.clear_team_work_current_card(run_id)
        calls.clear()
        # 模拟 main 轮转: 『当前任务』改指 t_b
        Path(ws, "TODO.md").write_text(
            "# Team Work: t\n\n## 当前任务\n- 步骤: b\n- 卡: t_b\n"
            "- 状态: 进行中\n\n---\n\n## 待执行\n(无)\n", encoding="utf-8")
        adv = console.api_team_work_advance(run_id, {
            "task_id": "t_b", "target_profile": "coder", "title": "B",
            "required_files": ["FIX_NOTES.md"],
        })
        check("F1 advance 成功", adv.get("ok") is True, f"got {adv}")
        check("F1 dispatch 继承 run.project_workspace",
              calls[0].get("project_workspace") == ws,
              f"got {calls[0].get('project_workspace')!r}")
    finally:
        restore()
        led.close()
        os.unlink(path)


def test_f1b_explicit_project_workspace_wins():
    led, path = fresh_ledger()
    try:
        calls, restore = stub_dispatch_recorder()
        # REOPEN A-1: 显式 pw 也走绑定校验, 该目录 TODO 需指向 t_b
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
        led.clear_team_work_current_card(run_id)
        calls.clear()
        console.api_team_work_advance(run_id, {
            "task_id": "t_b", "target_profile": "coder",
            "project_workspace": ws,
        })
        check("F1b 显式 project_workspace 优先于 run 继承",
              calls[0].get("project_workspace") == ws,
              f"got {calls[0].get('project_workspace')!r}")
    finally:
        restore()
        led.close()
        os.unlink(path)


# ── F2: resume 重占 current_card_id ─────────────────────────────

def _seed_run_with_card(led, run_id, task_id, session_id="sess_1",
                       current=None):
    led.create_team_work_run(run_id, "s", "task",
                           project_workspace="/proj") if False else None
    # 直接 SQL 播种 (create_team_work_run 签名以真实为准, 这里走底层表)
    led.conn.execute(
        "INSERT INTO team_work_runs (run_id, initiator_session, initial_task, "
        "project_workspace, current_card_id, status, card_count, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (run_id, "s", "task", "/proj", current, "ACTIVE", 1, time.time()))
    led.conn.execute(
        "INSERT INTO harvest_contracts (task_id, contract_json, harvest_status, "
        "pass_count, created_at, updated_at, team_work_run_id) "
        "VALUES (?,?,?,?,?,?,?)",
        (task_id, json.dumps({"required_files": []}), "BLOCKED",
         0, time.time(), time.time(), run_id))
    led.conn.execute(
        "INSERT INTO task_sessions (task_id, session_id, profile, runner_kind, "
        "workspace, created_at) VALUES (?,?,?,?,?,?)",
        (task_id, session_id, "coder", "api", "/share_workspace/tasks/" + task_id,
         time.time()))
    led.conn.execute(
        "INSERT OR REPLACE INTO task_initiators (task_id, session_id, created_at) "
        "VALUES (?,?,?)", (task_id, "init_sess", time.time()))
    led.conn.commit()


def test_f2_resume_reclaims_null_slot():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        _seed_run_with_card(led, "run_f2", "t_f2", current=None)
        # 模拟死信残留
        led.conn.execute(
            "UPDATE harvest_contracts SET notify_attempts=10, "
            "notify_dead_until=? WHERE task_id='t_f2'",
            (time.time() + 86400 * 365,))
        led.conn.commit()

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def headers(self): return {"X-Hermes-Session-Id": "sess_1"}
            def read(self): return json.dumps(
                {"choices": [{"message": {"content": "done"}}]}).encode()
            @property
            def status(self): return 200
        FakeResp.headers = {"X-Hermes-Session-Id": "sess_1"}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp()):
            r = pipeline.resume_task("t_f2", ledger=led,
                                   instruction="fix it", timeout=5)
        check("F2 resume ok", r.get("ok") is True, f"got {r}")
        run = led.get_team_work("run_f2")
        check("F2 槽位重占为 t_f2", run["current_card_id"] == "t_f2",
              f"got {run['current_card_id']!r}")
        row = led.conn.execute(
            "SELECT harvest_status, notify_attempts, notify_dead_until "
            "FROM harvest_contracts WHERE task_id='t_f2'").fetchone()
        check("F2 卡拨回 PENDING", row["harvest_status"] == "PENDING")
        check("F2 死信被清", row["notify_dead_until"] is None
              and row["notify_attempts"] == 0, f"got {tuple(row)}")
    finally:
        led.close()
        os.unlink(path)


def test_f2b_resume_does_not_steal_occupied_slot():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        _seed_run_with_card(led, "run_f2b", "t_f2b", current="t_other")

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            @property
            def headers(self): return {"X-Hermes-Session-Id": "sess_1"}
            def read(self): return json.dumps(
                {"choices": [{"message": {"content": "ok"}}]}).encode()
        with mock.patch("urllib.request.urlopen", return_value=FakeResp()):
            pipeline.resume_task("t_f2b", ledger=led,
                               instruction="x", timeout=5)
        run = led.get_team_work("run_f2b")
        check("F2b 槽被占时 resume 不抢槽",
              run["current_card_id"] == "t_other",
              f"got {run['current_card_id']!r}")
    finally:
        led.close()
        os.unlink(path)


# ── F3: resume 超时视为受理 ─────────────────────────────────────

def test_f3_resume_timeout_is_accepted():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        _seed_run_with_card(led, "run_f3", "t_f3", current=None)

        def timeout(*a, **k):
            raise TimeoutError("timed out")
        with mock.patch("urllib.request.urlopen", side_effect=timeout):
            r = pipeline.resume_task("t_f3", ledger=led,
                                    instruction="x", timeout=1)
        check("F3 超时 → ok=True (已受理)", r.get("ok") is True, f"got {r}")
        row = led.conn.execute(
            "SELECT harvest_status FROM harvest_contracts "
            "WHERE task_id='t_f3'").fetchone()
        check("F3 超时仍拨回 PENDING", row["harvest_status"] == "PENDING")
        run = led.get_team_work("run_f3")
        check("F3 超时也占槽", run["current_card_id"] == "t_f3",
              f"got {run['current_card_id']!r}")
    finally:
        led.close()
        os.unlink(path)


def test_f3b_non_timeout_error_still_raises():
    led, path = fresh_ledger()
    try:
        stub_workers_registry()
        _seed_run_with_card(led, "run_f3b", "t_f3b", current=None)

        def refused(*a, **k):
            raise ConnectionRefusedError("connection refused")
        raised = False
        with mock.patch("urllib.request.urlopen", side_effect=refused):
            try:
                pipeline.resume_task("t_f3b", ledger=led,
                                    instruction="x", timeout=1)
            except pipeline.SubmitError:
                raised = True
        check("F3b 非超时错误仍抛 SubmitError", raised)
        run = led.get_team_work("run_f3b")
        check("F3b 真失败不占槽", run["current_card_id"] is None,
              f"got {run['current_card_id']!r}")
    finally:
        led.close()
        os.unlink(path)


# ── F4: NULL 槽兜底敲门 ────────────────────────────────────────

def _sent_messages():
    sent = []

    def fake_send(sid, msg, gw, key, db_path, registry=None, notify_task=None):
        sent.append((sid, msg))
        return True
    return sent, fake_send


def test_f4_null_slot_knock():
    led, path = fresh_ledger()
    os.environ["API_SERVER_KEY"] = "test-key"
    try:
        _seed_run_with_card(led, "run_f4", "t_f4", current=None)
        # 把闲置时间拉到阈值之外
        old = time.time() - 3600
        led.conn.execute(
            "UPDATE harvest_contracts SET last_progress_at=?, updated_at=? "
            "WHERE task_id='t_f4'", (old, old))
        led.conn.commit()
        sent, fake = _sent_messages()
        with mock.patch.object(reaper, "_send_to_main", fake):
            results = []
            run = led.get_active_team_work()
            reaper._notify_null_slot(led, run, results,
                                    registry={}, stuck_after_min=5)
        check("F4 兜底敲门发出", len(sent) == 1, f"got {len(sent)}")
        check("F4 消息含兜底敲门", sent and "兜底敲门" in sent[0][1])
        check("F4 消息列出未结算卡", sent and "t_f4" in sent[0][1])
        run2 = led.get_team_work("run_f4")
        check("F4 记录 knock 时刻",
              run2.get("last_null_slot_knock_at") is not None)
        # F4b: 同窗口再调不重复敲
        with mock.patch.object(reaper, "_send_to_main", fake):
            reaper._notify_null_slot(led, led.get_active_team_work(), [],
                                    registry={}, stuck_after_min=5)
        check("F4b 同窗口不重复敲", len(sent) == 1, f"got {len(sent)}")
    finally:
        led.close()
        os.unlink(path)


def test_f4c_no_pending_no_knock():
    led, path = fresh_ledger()
    try:
        _seed_run_with_card(led, "run_f4c", "t_f4c", current=None)
        led.set_harvest("t_f4c", "HARVESTED", exit_code=0)
        old = time.time() - 3600
        led.conn.execute(
            "UPDATE harvest_contracts SET updated_at=? WHERE task_id='t_f4c'",
            (old,))
        led.conn.commit()
        sent, fake = _sent_messages()
        with mock.patch.object(reaper, "_send_to_main", fake):
            reaper._notify_null_slot(led, led.get_active_team_work(), [],
                                    registry={}, stuck_after_min=5)
        check("F4c 链走完 (无未结算卡) 不敲", len(sent) == 0, f"got {sent}")
    finally:
        led.close()
        os.unlink(path)


# ── F5: 死信自愈 ───────────────────────────────────────────────

def test_f5_worker_report_change_clears_dead_letter():
    led, path = fresh_ledger()
    try:
        _seed_run_with_card(led, "run_f5", "t_f5", current="t_f5")
        led.conn.execute(
            "UPDATE harvest_contracts SET notify_attempts=10, "
            "notify_dead_until=?, worker_report=? WHERE task_id='t_f5'",
            (time.time() + 86400 * 365,
             json.dumps({"verdict": "FAILED"})))
        led.conn.commit()
        led.set_worker_report("t_f5", {"verdict": "COMPLETE"})
        row = led.conn.execute(
            "SELECT notify_attempts, notify_dead_until, notify_confirmed_at "
            "FROM harvest_contracts WHERE task_id='t_f5'").fetchone()
        check("F5 新报告清死信", row["notify_dead_until"] is None
              and row["notify_attempts"] == 0, f"got {tuple(row)}")
        check("F5 新报告回到待发送", row["notify_confirmed_at"] is None)
    finally:
        led.close()
        os.unlink(path)


def test_f5b_429_backpressure_not_counted():
    led, path = fresh_ledger()
    try:
        _seed_run_with_card(led, "run_f5b", "t_f5b", current="t_f5b")
        import urllib.error

        def e429(*a, **k):
            raise urllib.error.HTTPError("http://x", 429, "Too Many", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=e429):
            reaper._send_to_main("s", "msg", "http://gw", "k", led.path,
                                notify_task="t_f5b")
            time.sleep(0.3)  # 等发送线程
        row = led.conn.execute(
            "SELECT notify_attempts, notify_dead_until, last_notified_at "
            "FROM harvest_contracts WHERE task_id='t_f5b'").fetchone()
        check("F5b 429 不计入 attempts", row["notify_attempts"] == 0,
              f"got {tuple(row)}")
        check("F5b 429 设短退避 (~30s)",
              row["notify_dead_until"] is not None
              and row["notify_dead_until"] <= time.time() + 35,
              f"got {row['notify_dead_until']}")
        check("F5b 429 回滚 last_notified_at", row["last_notified_at"] is None)
    finally:
        led.close()
        os.unlink(path)


def test_f5c_200_confirms_even_bad_body():
    led, path = fresh_ledger()
    try:
        _seed_run_with_card(led, "run_f5c", "t_f5c", current="t_f5c")

        class BadBodyResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            @property
            def status(self): return 200
            def getcode(self): return 200
            def read(self): return b"not-json-at-all"
        with mock.patch("urllib.request.urlopen", return_value=BadBodyResp()):
            reaper._send_to_main("s", "msg", "http://gw", "k", led.path,
                                notify_task="t_f5c")
            time.sleep(0.3)
        row = led.conn.execute(
            "SELECT notify_confirmed_at FROM harvest_contracts "
            "WHERE task_id='t_f5c'").fetchone()
        check("F5c 200 即 confirm (body 解析失败也算送达)",
              row["notify_confirmed_at"] is not None, f"got {tuple(row)}")
    finally:
        led.close()
        os.unlink(path)


# ── F6: stuck 敲门活体抑制 (2026-10-05 重写) ─────────────────────
# 改用 worker session 最后一条消息 timestamp 与窗口比较:
#   now - last_msg_ts < 窗口 → SUPPRESSED_ALIVE
#   now - last_msg_ts ≥ 窗口 → 照敲 (或探测失败 → 照敲)
# 不再落 ledger (无 worker_msg_count 列)。

def _seed_stuck_card(led, run_id, task_id):
    os.environ["API_SERVER_KEY"] = "test-key"
    _seed_run_with_card(led, run_id, task_id, current=task_id)
    old = time.time() - 3600
    led.conn.execute(
        "UPDATE harvest_contracts SET last_progress_at=?, updated_at=? "
        "WHERE task_id=?",
        (old, old, task_id))
    led.conn.commit()


def _seed_session_for_card(led, task_id, session_id, profile="coder"):
    """F8 契约: 活体抑制读 sse_bridge._worker_live[(profile, session_id)],
    需要 ledger.task_sessions 有对应 session/profile 行。F6 测试补这一行。
    """
    led.conn.execute(
        "INSERT OR REPLACE INTO task_sessions (task_id, session_id, profile, "
        "workspace, created_at) VALUES (?,?,?,?,?)",
        (task_id, session_id, profile, "/tmp/f6_ws", time.time()))
    led.conn.commit()


def _set_worker_live(profile, session_id, last_event_ts, last_event_type,
                    thinking=False):
    from mw import sse_bridge
    sse_bridge._subscriptions.clear()
    sse_bridge._worker_live.clear()
    sse_bridge._worker_live[(profile, session_id)] = {
        "session_id": session_id, "last_run_id": "run_x",
        "last_event_ts": last_event_ts, "last_event_type": last_event_type,
        "thinking": thinking, "event_count": 5}


def test_f6_alive_worker_suppresses_knock():
    led, path = fresh_ledger()
    try:
        _seed_stuck_card(led, "run_f6", "t_f6")
        _seed_session_for_card(led, "t_f6", "sid_f6", "coder")
        sent, fake = _sent_messages()
        # SSE-FIX-F3 (2026-10-08): 活体抑制改查 console /api/workers/live,
        # 不再用 session_last_msg_ts。mock 新公共函数: console 报活 → 抑制。
        live_entry = {"profile": "coder", "session_id": "sess_1",
                     "thinking": True, "last_event_ts": time.time() - 30,
                     "last_event_type": "assistant.delta", "event_count": 7}
        with mock.patch.object(reaper, "_query_console_liveness",
                             return_value=live_entry):
            results = []
            with mock.patch.object(reaper, "_send_to_main", fake):
                reaper._notify_main_of_stuck(
                    led, ".", results, registry={"coder": {"api_url": "u"}},
                    stuck_after_min=5, active_run_id="run_f6",
                    current_card_id="t_f6")
        check("F6 worker 活跃 → 抑制敲门", len(sent) == 0, f"got {sent}")
        sup = [r for r in results if r.get("verdict") == "SUPPRESSED_ALIVE"]
        check("F6 结果记录 SUPPRESSED_ALIVE", len(sup) == 1, f"got {results}")
        # 不再写 ledger 状态列 — 验证 worker_msg_count 不存在
        cols = [r["name"] for r in led.conn.execute(
            "PRAGMA table_info(harvest_contracts)").fetchall()]
        check("F6 ledger 无 worker_msg_count 列", "worker_msg_count" not in cols,
              f"got {cols}")
    finally:
        led.close()
        os.unlink(path)


def test_f6b_dead_worker_still_knocks():
    led, path = fresh_ledger()
    try:
        _seed_stuck_card(led, "run_f6b", "t_f6b")
        _seed_session_for_card(led, "t_f6b", "sid_f6b", "coder")
        sent, fake = _sent_messages()
        # worker 最后事件 2 小时前, 抑制窗口 600s → 应敲
        _set_worker_live("coder", "sid_f6b", time.time() - 7200,
                        "message.delta", thinking=False)
        results = []
        with mock.patch.object(reaper, "_send_to_main", fake):
            reaper._notify_main_of_stuck(
                led, ".", results, registry={"coder": {"api_url": "u"}},
                stuck_after_min=5, active_run_id="run_f6b",
                current_card_id="t_f6b")
        check("F6b worker 挂死 (≥2h 无消息) → 照敲", len(sent) == 1,
              f"got {sent}")
    finally:
        led.close()
        os.unlink(path)


def test_f6c_probe_failure_does_not_suppress():
    led, path = fresh_ledger()
    try:
        _seed_stuck_card(led, "run_f6c", "t_f6c")
        # 故意不 seed session → get_session 返 None → 无 SSE 信号 → 照敲
        sent, fake = _sent_messages()
        results = []
        with mock.patch.object(reaper, "_send_to_main", fake):
            reaper._notify_main_of_stuck(
                led, ".", results, registry={"coder": {"api_url": "u"}},
                stuck_after_min=5, active_run_id="run_f6c",
                current_card_id="t_f6c")
        check("F6c 无 SSE 信号不抑制 (宁可多敲不漏挂死)",
              len(sent) == 1, f"got {sent}")
    finally:
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
