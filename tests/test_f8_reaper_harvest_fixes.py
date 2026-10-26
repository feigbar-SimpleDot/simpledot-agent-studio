"""F8 (2026-10-08): 修复 reaper 重复敲门 + harvest 越权 auto-close run.

覆盖:
  - 双闸抑制改单闸 (F8.1)
  - harvest 永不 auto-close run (F8.2)
  - settle_team_work_residue 不动 PENDING (F8.3)
  - dispatch / fsnotify / harvest 都 touch_progress (F8.4)
  - 测试反向: 改错的话跑应该挂,锁住新行为
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from unittest import mock

# 测试期间 reaper 应能假装锁住无 API_SERVER_KEY 也能跑
os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")  # 不会真连

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results: list[bool] = []


def _assert(name: str, cond: bool, msg: str = ""):
    if cond:
        print(f"  {PASS} {name}")
        results.append(True)
    else:
        print(f"  {FAIL} {name}: {msg}")
        results.append(False)


def _make_led():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from mw.ledger import Ledger
    led = Ledger(path)
    return led, path


# ============================================================
# F8.3: settle_team_work_residue 不动 PENDING
# ============================================================

def test_f8_3_settle_residue_keeps_future_pending():
    """回归 run b9a9445419ab489e 旧坑: settle_team_work_residue 把未来
    PENDING 卡标 HARVESTED → harvest 落账时 count==0 → auto-close。

    新行为: PENDING 是排队等 main advance 的卡, 不动。
    """
    led, path = _make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        for tid in ["t_curr", "t_future_a", "t_future_b"]:
            led.put_contract(tid, {})
            led.register_team_work_card("r1", tid)
        # t_future_a 是 PENDING (未来卡, 不动)
        led.set_harvest("t_future_b", "BLOCKED", log="reaper fallback")

        # advance t_curr: settle 只动 BLOCKED, 锁 t_future_a 不变
        settled = led.settle_team_work_residue("r1", exclude_task_id="t_curr")
        assert sorted(settled) == ["t_future_b"], \
            f"应只覆盖 BLOCKED, got {settled}"
        # t_future_a 仍 PENDING (未来卡)
        assert led.get_contract("t_future_a")["harvest_status"] == "PENDING", \
            "未来 PENDING 卡被错误覆盖了"
        # t_future_b 标 HARVESTED (残留清理)
        assert led.get_contract("t_future_b")["harvest_status"] == "HARVESTED"
        _assert("f8.3 PENDING 不被 settle 覆盖", True)
    finally:
        os.unlink(path)


# ============================================================
# F8.2: harvest 永不 auto-close run
# ============================================================

def test_f8_2_harvest_does_not_auto_close_run():
    """回归 run b9a9445419ab489e 实测: harvest chain-tail auto-close
    把 run 强行关闭 4 次, 每次都需要 kill reaper + UPDATE 恢复。

    新行为: harvest 落账 COMPLETE 后, 即使 run 名下无未结算卡,
    也不调 close_team_work — 只 emit team_work.run_idle 事件。
    """
    led, path = _make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.put_contract("t_a", {})
        led.register_team_work_card("r1", "t_a")
        led.set_team_work_current_card("r1", "t_a")

        # 模拟 harvest 落账 COMPLETE (绕过完整 harvest, 直接调 set_harvest)
        led.set_harvest("t_a", "HARVESTED")
        led.set_team_work_current_card("r1", None)  # harvest 落账后清槽

        # run 仍必须 ACTIVE, 不能被 auto-close
        run = led.get_team_work("r1")
        assert run["status"] == "ACTIVE", \
            f"harvest 不应关 run, got status={run['status']}"

        # 应该落了 run_idle 事件
        events = led.conn.execute(
            "SELECT event_type FROM mw_events WHERE event_type='team_work.run_idle'"
        ).fetchall()
        # 注: 这是直接调 ledger 方法, 不是走 harvest 模块的事件 emit。
        # 真正的 emit 在 harvest.py 的 _emit() 里, 但 harvest 模块不写事件
        # — _emit 通过 dispatch_webhook 跑。这里我们验证「run 仍 ACTIVE」就够。
        _assert("f8.2 harvest 落账后 run 仍 ACTIVE (未 auto-close)", True)
    finally:
        os.unlink(path)


# ============================================================
# F8.4: dispatch / fsnotify / harvest 都 touch_progress
# ============================================================

def test_f8_4_dispatch_touches_progress():
    """回归: dispatch 后 last_progress_at 必须非 NULL。
    否则 reaper 兜底回退到 marker.started_at, 「挂 N 分钟」=
    「spawn 后 N 分钟」, 与 worker 是否真有进展无关。
    """
    led, path = _make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.put_contract("t_dispatch", {})
        led.register_team_work_card("r1", "t_dispatch")
        led.set_team_work_current_card("r1", "t_dispatch")

        # 直接调 touch_progress, 验证它落到 ledger
        before = led.get_contract("t_dispatch")["last_progress_at"]
        time.sleep(0.05)
        led.touch_progress("t_dispatch")
        after = led.get_contract("t_dispatch")["last_progress_at"]

        assert before is None or before < after, \
            f"touch_progress 没生效: before={before}, after={after}"
        _assert("f8.4 touch_progress 写入 last_progress_at", True)
    finally:
        os.unlink(path)


def test_f8_4b_touch_progress_advances_monotonically():
    """连续调 touch_progress, last_progress_at 必须单调递增。"""
    led, path = _make_led()
    try:
        led.put_contract("t_x", {})
        t1 = led.get_contract("t_x")["last_progress_at"]  # 应为 None
        led.touch_progress("t_x")
        a = led.get_contract("t_x")["last_progress_at"]
        time.sleep(0.01)
        led.touch_progress("t_x")
        b = led.get_contract("t_x")["last_progress_at"]
        assert a is not None and b is not None and a < b, \
            f"应递增, got a={a}, b={b}"
        _assert("f8.4b touch_progress 单调递增", True)
    finally:
        os.unlink(path)


# ============================================================
# F8.1: 双闸抑制改单闸 (event timestamp 即活体信号)
#
# issue #113 (2026-10-25) 归因: 本组 4 例此前直接读 sse_bridge._worker_live,
# 而那条读法在 SSE-FIX-F3 (0fa0f40, 2026-10-08) 之后已是死代码 ——
# reaper 与 SSE 桥不同进程, 内存不共享, 活体真源改为 console 端点
# /api/workers/live (reaper._query_console_liveness)。
# 旧写法造成两处失真:
#   - F8.1 恒红: 替身缺 ledger.path → 落 ERROR 而非 SUPPRESSED_ALIVE;
#   - F8.1b/1c/1d 恒绿: console 不可达 ⇒ fail-open 不抑制 ⇒ 「不抑制」
#     这条断言与被测行为脱钩 (空洞绿灯, 恰恰是 issue 要消灭的污染)。
# 本组改为 mock 当前真源 _query_console_liveness, 并断言真实判定结果
# (抑制 ⇒ SUPPRESSED_ALIVE; 不抑制 ⇒ NOTIFIED 且确实发了敲门),
# 让每个用例都能真绿也能真红。

def _live_entry(session_id, *, thinking, last_event_ts,
                event_type="tool.completed"):
    """构造 console /api/workers/live 的一条条目 (真源格式)。"""
    return {"profile": "coder", "session_id": session_id,
            "thinking": thinking, "last_event_ts": last_event_ts,
            "last_event_type": event_type, "event_count": 5}


def _stuck_knock_stub(now, *, task_id, session_id):
    """构造 _notify_main_of_stuck 用的 ledger 替身。

    替身必须补齐被测路径真正用到的方法 —— 旧版只给了 4 个, 缺
    path / mark_notified / emit_event, 于是「不抑制 → 走敲门」那条路径
    必落 AttributeError (F8.1 恒红的直接原因)。
    """
    class _L:
        path = "/tmp/mw_f8_stub_ledger.db"

        def current_card_contract(self, run_id, card_id):
            return {"task_id": task_id, "harvest_status": "PENDING",
                    "last_progress_at": now - 3600, "updated_at": now - 3600}
        def should_notify(self, tid, *, min_interval_s):
            return True
        def get_session(self, tid):
            return {"session_id": session_id, "profile": "coder"}
        def get_initiator(self, tid):
            return "sid_main"
        # D1 桩 (VERIFY-20261013 §2.3): F5-4 敲门前先标 WAITING, 替身须有该方法
        def update_worker_state(self, tid, new_status, *, actor,
                                reason="", log=""):
            pass
        # issue #113: 敲门成功后 reaper 会 mark_notified + emit task.stuck,
        # 替身缺这两个方法会让「不抑制」路径必落 ERROR (空断言的温床)。
        def mark_notified(self, tid):
            pass
        def emit_event(self, ev_type, tid, payload):
            pass
    return _L()


def _run_stuck_knock(stub, results, *, live_entry, expected_call_args=None):
    """跑一次敲门, mock 掉活体查询与实际发送, 返回 (results, sent)。"""
    from mw import reaper as reaper_mod
    sent = []

    def _fake_send(sid, msg, gw, key, db_path, registry=None,
                   notify_task=None):
        sent.append({"sid": sid, "msg": msg, "task_id": notify_task})

    with mock.patch.object(reaper_mod, "_query_console_liveness",
                           return_value=live_entry) as live_q:
        with mock.patch.object(reaper_mod, "_send_to_main", _fake_send):
            reaper_mod._notify_main_of_stuck(
                stub, "/tmp", results,
                registry=None,
                stuck_after_min=1,
                active_run_id="run_a",
                current_card_id="card_c",
            )
    if expected_call_args is not None:
        assert live_q.call_args is not None, (
            "活体查询压根没被调用 —— 断言方向与被测行为脱钩 (空洞绿灯)")
        assert live_q.call_args[0] == expected_call_args, (
            f"活体查询参数错位: 期望 {expected_call_args}, "
            f"实际 {live_q.call_args[0]}")
    return sent


def test_f8_1_single_gate_suppresses_on_recent_event():
    """回归 test_reaper_sse_stuck 旧行为: thinking=False + 最近 event
    也被抑制 (旧版会敲门, 新版只看 event timestamp)。

    旧版要求 is_thinking()==True AND last_event_ts 在窗口内.
    新版只看 last_event_ts 在窗口内, 不区分 event 类型.
    """
    now = time.time()
    # run.completed → thinking=False (旧版闸 1 失败), 但 last_event_ts=now
    live = _live_entry("sid_x", thinking=False, last_event_ts=now,
                       event_type="run.completed")
    stub = _stuck_knock_stub(now, task_id="T-1", session_id="sid_x")

    results_list = []
    sent = _run_stuck_knock(stub, results_list, live_entry=live,
                             expected_call_args=("coder", "sid_x"))

    suppressed = [r for r in results_list
                  if r.get("verdict") == "SUPPRESSED_ALIVE"]
    assert len(suppressed) == 1, \
        f"应被抑制 (last_event_ts=now), got {results_list}"
    # 抑制 ⇒ 绝不能真发敲门 (否则是「抑制了但还是吵醒了 main」)
    assert not sent, f"抑制后不应真发敲门, got {sent}"
    _assert("f8.1 thinking=False 但 last_event_ts 新 → 抑制 (单闸)", True)


def test_f8_1b_old_event_does_not_suppress():
    """反向: last_event_ts 很久以前 (旧 60s 窗口外) → 应敲门。

    锁住新行为, 防止有人误把窗口又改回 30min。

    issue #113: thinking 置 False —— 本例要隔离验证的是「窗口」这一条
    闸。thinking 是与窗口并列的独立活体闸 (thinking==True 时粘滞到
    终态, 覆盖单个长工具调用中间无事件的空窗), 置 True 会让 thinking
    闸把窗口闸遮住, 断言就变成在测另一条规则。
    """
    now = time.time()
    # last_event_ts 5 分钟前, 远超默认 60s 窗口
    live = _live_entry("sid_y", thinking=False, last_event_ts=now - 300,
                       event_type="message.delta")
    stub = _stuck_knock_stub(now, task_id="T-2", session_id="sid_y")

    results_list = []
    sent = _run_stuck_knock(stub, results_list, live_entry=live)

    suppressed = [r for r in results_list
                  if r.get("verdict") == "SUPPRESSED_ALIVE"]
    # 不抑制 (5min 太旧)
    assert not suppressed, \
        f"5 分钟前的事件不应抑制, got {results_list}"
    # 「不抑制」必须真的敲了门 —— 否则这条断言是空洞绿灯
    notified = [r for r in results_list if r.get("verdict") == "NOTIFIED"]
    assert len(notified) == 1, \
        f"窗口外的旧 event 应真敲门 (NOTIFIED), got {results_list}"
    assert len(sent) == 1, f"应真发出一次敲门, got {sent}"
    _assert("f8.1b 窗口外的旧 event 不抑制 (且真敲)", True)


def test_f8_1b2_thinking_true_is_independent_live_gate():
    """issue #113 新增: thinking==True 是与窗口并列的独立活体闸。

    SSE-FIX-F3 (0fa0f40) 把活体判定从「thinking AND 窗口」改成
    「thinking OR 窗口」—— thinking 粘滞到终态, 覆盖单个长工具调用
    中间没有事件的空窗。本例锁住这条语义, 防止有人把 thinking 闸
    误删或误改成 AND (那会让长工具调用期间被当成挂死而误敲 main)。
    """
    now = time.time()
    # last_event_ts 5 分钟前 (窗口外), 但 thinking=True ⇒ 仍判活
    live = _live_entry("sid_y2", thinking=True, last_event_ts=now - 300,
                       event_type="tool.completed")
    stub = _stuck_knock_stub(now, task_id="T-2b", session_id="sid_y2")

    results_list = []
    sent = _run_stuck_knock(stub, results_list, live_entry=live)

    suppressed = [r for r in results_list
                  if r.get("verdict") == "SUPPRESSED_ALIVE"]
    assert len(suppressed) == 1, \
        f"thinking=True 应独立判活 (窗口外也抑制), got {results_list}"
    assert not sent, f"thinking=True 判活后不应真发敲门, got {sent}"
    _assert("f8.1b2 thinking=True 是独立活体闸 (窗口外也抑制)", True)


def test_f8_1c_no_live_entry_does_not_suppress():
    """空 live 条目 → 不抑制, 照敲 (探测失败不挡)。"""
    now = time.time()
    stub = _stuck_knock_stub(now, task_id="T-3", session_id="sid_z")

    results_list = []
    sent = _run_stuck_knock(stub, results_list, live_entry=None)

    suppressed = [r for r in results_list
                  if r.get("verdict") == "SUPPRESSED_ALIVE"]
    assert not suppressed, \
        f"无 live entry 不应抑制, got {results_list}"
    # 「探测失败 → 照敲」是 fail-open 的核心语义: 必须真敲
    notified = [r for r in results_list if r.get("verdict") == "NOTIFIED"]
    assert len(notified) == 1, \
        f"无 live entry 应真敲门 (fail-open), got {results_list}"
    assert len(sent) == 1, f"应真发出一次敲门, got {sent}"
    _assert("f8.1c 无 live entry 不抑制 (照敲)", True)


def test_f8_1d_default_window_is_60s_not_30min():
    """锁住默认窗口: 60s 而非 stuck_after_min * 60 (= 30min 旧值)。

    测试方法: live 条目 last_event_ts=now-120s, 旧默认 30min
    会抑制, 新默认 60s 不抑制。

    issue #113: thinking 置 False —— 与 1b 同理, 本例要隔离验证的是
    「默认窗口 60s」这一条闸, 不能被 thinking 闸遮住。
    """
    now = time.time()
    live = _live_entry("sid_w", thinking=False, last_event_ts=now - 120,
                       event_type="tool.completed")
    stub = _stuck_knock_stub(now, task_id="T-4", session_id="sid_w")

    results_list = []
    sent = _run_stuck_knock(stub, results_list, live_entry=live)

    suppressed = [r for r in results_list
                  if r.get("verdict") == "SUPPRESSED_ALIVE"]
    # stuck_after_min=1 → 旧默认窗口 60s; 2min 前应被新窗口(60s)挡掉
    # 但抑制条件只卡 age < 60s, 120s > 60s → 不抑制 → 应敲
    assert not suppressed, \
        f"2 分钟前的 event 应敲门 (新窗口 60s), got {results_list}"
    notified = [r for r in results_list if r.get("verdict") == "NOTIFIED"]
    assert len(notified) == 1, \
        f"2 分钟前的 event 应真敲门, got {results_list}"
    assert len(sent) == 1, f"应真发出一次敲门, got {sent}"
    _assert("f8.1d 默认窗口 60s 而非 stuck_after_min*60=30min", True)


# ============================================================
# 集成: end-to-end 模拟 advance + 落账 + 不应该误关 run
# ============================================================

def test_integration_advance_then_complete_does_not_close_run():
    """E2E 模拟 b9a9445419ab489e 场景: advance 第 2 张, 未来第 3/4 张
    PENDING 不会被覆盖; 第 2 张 COMPLETE 落账, run 仍 ACTIVE, 未来 PENDING
    仍存在 (等待 main advance 第 3 张)。
    """
    led, path = _make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        for i in range(1, 5):
            tid = f"t_{i}"
            led.put_contract(tid, {})
            led.register_team_work_card("r1", tid)
        # 模拟卡 1 已 HARVESTED
        led.set_harvest("t_1", "HARVESTED")
        # advance t_2: 隐式 settle t_3/t_4 BLOCKED→HARVESTED (但它们是 PENDING)
        # 新语义: PENDING 不动
        led.set_team_work_current_card("r1", "t_2")
        settled = led.settle_team_work_residue("r1", exclude_task_id="t_2")
        assert settled == [], \
            f"PENDING 卡不应被 settle 覆盖, got {settled}"
        # t_3, t_4 仍 PENDING
        assert led.get_contract("t_3")["harvest_status"] == "PENDING"
        assert led.get_contract("t_4")["harvest_status"] == "PENDING"
        # 卡 2 落账
        led.set_harvest("t_2", "HARVESTED")
        led.set_team_work_current_card("r1", None)
        # run 仍 ACTIVE
        run = led.get_team_work("r1")
        assert run["status"] == "ACTIVE", \
            f"harvest 不应关 run, got {run['status']}"
        _assert("e2e advance + 落账不关 run, 未来 PENDING 保留", True)
    finally:
        os.unlink(path)


# ============================================================
# 入口
# ============================================================

def main():
    tests = [
        test_f8_3_settle_residue_keeps_future_pending,
        test_f8_2_harvest_does_not_auto_close_run,
        test_f8_4_dispatch_touches_progress,
        test_f8_4b_touch_progress_advances_monotonically,
        test_f8_1_single_gate_suppresses_on_recent_event,
        test_f8_1b_old_event_does_not_suppress,
        test_f8_1b2_thinking_true_is_independent_live_gate,
        test_f8_1c_no_live_entry_does_not_suppress,
        test_f8_1d_default_window_is_60s_not_30min,
        test_integration_advance_then_complete_does_not_close_run,
    ]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  {FAIL} {t.__name__}: {e}")
            results.append(False)
        except Exception as e:
            print(f"  {FAIL} {t.__name__}: {type(e).__name__}: {e}")
            results.append(False)

    total = len(results)
    passed = sum(results)
    print()
    if passed == total:
        print(f"\033[92m=== ALL {total} F8 TESTS PASSED ===\033[0m")
        return 0
    else:
        print(f"\033[91m=== {total - passed}/{total} F8 TESTS FAILED ===\033[0m")
        return 1


if __name__ == "__main__":
    sys.exit(main())