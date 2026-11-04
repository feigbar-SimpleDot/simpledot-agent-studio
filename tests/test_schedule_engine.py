"""tests/test_schedule_engine.py — 排期引擎测试 (issue #164, ARCH-164 §8#11)。

monkeypatch 假时钟 + stub gateway/ledger, 不起真线程不碰真网络。覆盖:
  EN1  条件起表 / 条件破坏撤表 (AC-5.5/5.6)
  EN2  归零触发取序 added_at DESC (A8) + 触发前全量复查放弃 (AC-5.3)
  EN3  触发失败回退 pending + last_error (AC-10.1)
  EN4  重启重数: 倒计时内存态, 新引擎实例重新起表 (A10/AC-5.7)
  EN5  run 关闭观察转 done (AC-5.4/8.5) + session_id 回填 run_id
  EN6  双触发互斥: run-now 与归零共用 _fire_lock + 条件 UPDATE, 同一任务
       不可能被双发 (AC-10.2)
  EN7  run-now 边界: 无倒计时 409 (A11); 开关关 409
  EN8  探活 fail-closed: gateway 探活失败按"有 run"处理 (A9/A15)
  F1   并发双触发互斥 (REVIEW-164 P1-1 / F-1, AC-10.2): 归零与立即执行
       同时发生只发起一个 run —— 探活必须被 _fire_lock 覆盖, 且倒计时
       归零的"认领权"是一次性的
  F2   开关 OFF→ON 重开完整 5 分钟 (REVIEW-164 P1-2 / F-2, AC-4.3)
  N1   run-now 拒绝码可达 (issue #179): 无 pending ⇒ 'no_pending' ⇒
       schedule.err_no_pending_task (非泛化文案), 409 状态码不变
  N2   探活缓存时间戳取在探测结束之后 (issue #180): 探活耗时 ≥ TTL 时
       第二次调用命中缓存 (成功路径 + 失败路径各一条)
  N3   busy 归因三态 (issue #181): busy=True / busy=False+active>0 ⇒
       'gateway_busy'; 探活异常 ⇒ 'gateway_probe_unavailable'

跑法: pytest tests/test_schedule_engine.py -v
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mw import schedule as S  # noqa: E402


class FakeClock:
    """假时钟: 测试里手动推进, 不真等 5 分钟。"""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_engine(tmp_path, clock=None):
    """隔离引擎: 独立 schedule.db + 空 ledger + 假时钟 + stub 探活。

    返回 (engine, fired): fired 收集 _spawn_fire 收到的任务 (不真起线程)。
    """
    store = S.ScheduleStore(str(tmp_path / "sched.db"))
    ldb = str(tmp_path / "ledger.db")
    lc = sqlite3.connect(ldb)
    lc.execute("CREATE TABLE team_work_runs (run_id TEXT PRIMARY KEY, "
               "initiator_session TEXT, status TEXT)")
    lc.commit()
    lc.close()
    eng = S.ScheduleEngine(store, ledger_db=ldb,
                           clock=clock or time.time, countdown_s=300)
    eng._gateway_free = lambda: (True, None)     # 默认: gateway 空闲
    fired = []
    eng._spawn_fire = lambda task: fired.append(task)
    return eng, fired


# ── EN1: 起表 / 撤表 ─────────────────────────────────────────────────
def test_countdown_start_and_revoke(tmp_path):
    eng, fired = make_engine(tmp_path)
    # 无任务 ⇒ 不起表
    eng._tick()
    assert eng.countdown_ends_at is None
    # 新建任务 ⇒ 起表 (AC-5.5)
    t = eng.store.create("任务A")
    eng._tick()
    assert eng.countdown_ends_at is not None
    # 开关关闭 ⇒ 撤表 (AC-4.2/5.6)
    eng.store.set_auto_execute(False)
    eng._tick()
    assert eng.countdown_ends_at is None
    # 重开 ⇒ 重新起表
    eng.store.set_auto_execute(True)
    eng._tick()
    assert eng.countdown_ends_at is not None
    # 任务取消 ⇒ 无可执行任务 ⇒ 撤表
    eng.store.cancel(t["id"])
    eng._tick()
    assert eng.countdown_ends_at is None


def test_active_run_blocks_countdown(tmp_path):
    eng, fired = make_engine(tmp_path)
    eng.store.create("任务A")
    # ledger 有 ACTIVE run ⇒ 不起表 (A9: 任何进行中 run 都占用)
    lc = sqlite3.connect(eng.ledger_db)
    lc.execute("INSERT INTO team_work_runs VALUES ('r1','s1','ACTIVE')")
    lc.commit()
    lc.close()
    eng._tick()
    assert eng.countdown_ends_at is None
    assert eng._busy_reason() == "active_run"


# ── EN2: 归零触发取序 + 复查放弃 ─────────────────────────────────────
def test_fire_at_zero_picks_head(tmp_path):
    """归零触发取的是**创建最早**的任务 (added_at ASC)。

    issue #197 取代 A8 裁决 (原为 added_at DESC ⇒ 后建先执行, 即用户
    报的现象「后加入的任务被先执行了」)。

    MV-197-7: 把取序改回 DESC ⇒ 本用例红。
    """
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    a = eng.store.create("A")
    clock.now += 1
    b = eng.store.create("B")
    eng._tick()                       # 起表 ends_at = now+300
    assert eng.countdown_ends_at == clock.now + 300
    assert eng.countdown_task_id == a["id"]   # 持有者 = 队首 (创建最早)
    clock.now += 301                  # 归零
    eng._tick()
    # 取队首 = added_at ASC ⇒ A 先 (#197)
    assert [t["id"] for t in fired] == [a["id"]]
    assert eng.store.get(a["id"])["status"] == "running"
    assert eng.countdown_ends_at is None
    assert eng.countdown_task_id is None


def test_fire_recheck_failure_abandons(tmp_path):
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    eng.store.create("A")
    eng._tick()
    clock.now += 301
    # 归零瞬间 gateway 变忙 ⇒ 复查失败放弃, 不触发 (AC-5.3)
    eng._gateway_free = lambda: (False, "gateway_busy")
    eng._tick()
    assert fired == []
    assert eng.countdown_ends_at is None
    # 条件恢复后重新起表重数 (不秒发)
    eng._gateway_free = lambda: (True, None)
    eng._tick()
    assert eng.countdown_ends_at == clock.now + 300


# ── EN3: 触发失败回退 ────────────────────────────────────────────────
def test_fire_failure_falls_back(tmp_path):
    eng, fired = make_engine(tmp_path)
    t = eng.store.create("A")

    # 模拟 fire worker 内部失败路径: _create_session 抛错 ⇒ 回退
    def boom():
        raise RuntimeError("gateway down")

    eng._create_session = boom
    task = eng.store.take_head_pending()
    assert task is not None
    eng._fire_worker(task)            # 同步跑 fire worker
    got = eng.store.get(t["id"])
    assert got["status"] == "pending"              # AC-10.1 回退
    assert "gateway down" in (got["last_error"] or "")
    # 回退后队列可再取 (不滞留)
    assert eng.store.head_pending()["id"] == t["id"]


def test_fire_success_streams_to_eof(tmp_path):
    eng, fired = make_engine(tmp_path)
    t = eng.store.create("A")
    eng._create_session = lambda: "sid-1"
    streamed = []
    eng._stream_chat = lambda sid, message: streamed.append((sid, message))
    task = eng.store.take_head_pending()
    assert task is not None
    eng._fire_worker(task)
    assert eng.store.get(t["id"])["status"] == "running"   # 成功不回退
    assert eng.store.get(t["id"])["session_id"] == "sid-1"
    assert streamed and "【Team Work 多智能体任务】" in streamed[0][1]


# ── EN4: 重启重数 ────────────────────────────────────────────────────
def test_restart_recounts(tmp_path):
    db = str(tmp_path / "sched.db")
    ldb = str(tmp_path / "ledger.db")
    lc = sqlite3.connect(ldb)
    lc.execute("CREATE TABLE team_work_runs (run_id TEXT PRIMARY KEY, "
               "initiator_session TEXT, status TEXT)")
    lc.commit()
    lc.close()
    e1 = S.ScheduleEngine(S.ScheduleStore(db), ledger_db=ldb)
    e1._gateway_free = lambda: (True, None)
    e1.store.create("A")
    e1._tick()
    assert e1.countdown_ends_at is not None
    # "重启": 新实例 (内存倒计时丢失) ⇒ 重新起完整 300s, 不秒发 (AC-5.7)
    e2 = S.ScheduleEngine(S.ScheduleStore(db), ledger_db=ldb)
    e2._gateway_free = lambda: (True, None)
    assert e2.countdown_ends_at is None
    e2._tick()
    assert e2.countdown_ends_at is not None


# ── EN5: run 关闭观察 ────────────────────────────────────────────────
def test_run_close_marks_done(tmp_path):
    eng, fired = make_engine(tmp_path)
    t = eng.store.create("A")
    eng.store.take_head_pending()
    eng.store.backfill_session(t["id"], "s9")
    # run 行未出现 (main 还在拆解) ⇒ 保持 running
    eng._observe_running()
    assert eng.store.get(t["id"])["status"] == "running"
    # run 出现且已 CLOSED ⇒ 回填 run_id + 转 done (AC-5.4/8.5)
    lc = sqlite3.connect(eng.ledger_db)
    lc.execute("INSERT INTO team_work_runs VALUES ('r9','s9','CLOSED')")
    lc.commit()
    lc.close()
    eng._observe_running()
    got = eng.store.get(t["id"])
    assert got["status"] == "done" and got["run_id"] == "r9"


def test_run_active_keeps_running(tmp_path):
    eng, fired = make_engine(tmp_path)
    t = eng.store.create("A")
    eng.store.take_head_pending()
    eng.store.backfill_run(t["id"], "r5")
    lc = sqlite3.connect(eng.ledger_db)
    lc.execute("INSERT INTO team_work_runs VALUES ('r5','s5','ACTIVE')")
    lc.commit()
    lc.close()
    eng._observe_running()
    assert eng.store.get(t["id"])["status"] == "running"


# ── EN6: 双触发互斥 ──────────────────────────────────────────────────
def test_run_now_and_zero_no_double_fire(tmp_path):
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    t = eng.store.create("A")
    eng._tick()                       # 起表
    # run-now 触发 (锁内取走队首)
    ok, reason = eng.run_now()
    assert ok, reason
    assert [x["id"] for x in fired] == [t["id"]]
    # 归零再触发 ⇒ 已无 pending ⇒ 不双发 (AC-10.2)
    clock.now += 400
    eng._tick()
    assert len(fired) == 1


# ── EN7: run-now 边界 ────────────────────────────────────────────────
def test_run_now_requires_countdown(tmp_path):
    eng, fired = make_engine(tmp_path)
    eng.store.create("A")
    # 未起表 ⇒ countdown_inactive (A11: 按钮本就不显示, 服务端再复查)
    ok, reason = eng.run_now()
    assert not ok and reason == "countdown_inactive"
    # 开关关 ⇒ auto_execute_off
    eng.store.set_auto_execute(False)
    ok, reason = eng.run_now()
    assert not ok and reason == "auto_execute_off"


# ── EN8: 探活 fail-closed ────────────────────────────────────────────
def test_probe_fail_closed(tmp_path):
    eng, fired = make_engine(tmp_path)
    eng.store.create("A")
    eng._gateway_free = lambda: (False, "gateway_probe_unavailable")
    eng._tick()
    assert eng.countdown_ends_at is None           # 探活失败不起表
    snap = eng.snapshot()
    assert snap["busy_reason"] == "gateway_probe_unavailable"


def test_snapshot_shape(tmp_path):
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    t = eng.store.create("标题任务")
    eng._tick()
    snap = eng.snapshot()
    assert snap["auto_execute"] is True
    cd = snap["countdown"]
    assert cd["active"] is True
    assert cd["ends_at"] == clock.now + 300
    assert cd["remaining_s"] == 300
    assert cd["next_task_id"] == t["id"]
    assert cd["next_task_title"] == "标题任务"
    assert snap["running_task_id"] is None


# ── F1: 并发双触发互斥 (REVIEW-164 P1-1, AC-10.2) ─────────────────────
#
# 旧行为 (审查复现: FIRED: ['B','A'] COUNT = 2): 归零与立即执行两条路径
# 都把**可阻塞的探活**放在 _fire_lock 之外, 且 _fire_due 是"先清倒计时、
# 后取 _fire_lock"的倒序 ⇒ ticker 卡在探活时倒计时仍标着到期 ⇒ 前端
# 「立即执行」仍可点 ⇒ 两个触发源各取走一个队首 ⇒ 两个 run 并发发起。
#
# 修复后: 两条路径共用 _fire_lock 覆盖"探活 + 取队首 + 清倒计时"整段,
# 且归零的"认领权"一次性 (countdown_ends_at 被清后无人可再认领)。

def test_slow_probe_run_now_and_zero_fires_once(tmp_path):
    """探活阻塞期间点「立即执行」⇒ 只发起 1 个 run (AC-10.2)。

    构造审查同款场景 (REVIEW-164 §4.1 复现: FIRED: ['B','A'] COUNT = 2):
    探活延迟 (gateway 慢/挂), **ticker 线程**先进入 _fire_due 卡在探活;
    主线程在窗口内调 run_now (此时倒计时仍标着到期 ⇒ 前端按钮可见可点)。

    探活桩按线程分流 (只让 ticker 侧阻塞, run_now 侧立即返回) —— 与
    审查复现同构, 且无竞态。

    旧行为 (探活在 _fire_lock 外): run_now 抢先取走队首 B 并发起 run,
    ticker 恢复后再取走队首 A ⇒ 2 个 run (AC-10.2 违反)。
    修复后 (探活在 _fire_lock 内): ticker 持锁走完探活+取队首 ⇒ 只发起
    1 个 run; run_now 随后拿到锁时倒计时已被认领清空 ⇒ 409
    countdown_inactive, 不发起第二个 run。

    断言只钉**行为不变量** (恰好 1 个 run、同一任务不被双发), 不钉
    run_now 的具体返回码 —— 两种实现下 run_now 的返回本就不同, 但
    "只发起一个 run" 这一 AC-10.2 口径必须恒成立。"""
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    eng.store.create("A")
    clock.now += 1
    eng.store.create("B")
    eng._tick()                       # 起表 ends_at = now+300
    clock.now += 301                  # 归零

    probe_entered = threading.Event()
    release_probe = threading.Event()
    ticker_ident = {}
    probe_calls = []

    def slow_probe():
        probe_calls.append(threading.get_ident())
        # 只阻塞 ticker 侧 (审查复现同构): run_now 侧立即返回空闲
        if threading.get_ident() == ticker_ident.get("id"):
            probe_entered.set()
            release_probe.wait(4.0)
        return True, None

    eng._gateway_free = slow_probe

    def ticker():
        ticker_ident["id"] = threading.get_ident()
        eng._fire_due()

    t = threading.Thread(target=ticker, daemon=True)
    t.start()
    assert probe_entered.wait(2.0), "ticker 未进入探活"

    # 探活窗口内: 倒计时仍标着到期 ⇒ 「立即执行」按钮可见可点 (旧行为)
    run_now_result = {}

    def run_now_thread():
        run_now_result["v"] = eng.run_now()

    rt = threading.Thread(target=run_now_thread, daemon=True)
    rt.start()

    # 等 run_now 走到自己的探活 (旧行为) 或超时 (修复后 run_now 卡在锁上)
    deadline = time.time() + 1.5
    while time.time() < deadline and len(probe_calls) < 2:
        time.sleep(0.02)
    release_probe.set()              # 放行 ticker 侧探活 (模拟 gateway 恢复)
    t.join(6.0)
    rt.join(6.0)

    # 两个触发源同时发生 ⇒ 恰好 1 个 run (AC-10.2)
    assert len(fired) == 1, f"并发双触发: fired={fired}"
    # 且被触发的任务只被发起一次 (条件 UPDATE 语义)
    assert len({x["id"] for x in fired}) == 1


def test_slow_probe_fire_due_alone_fires_once(tmp_path):
    """只有归零一路 (无并发) 时仍正常触发 1 次 (修复不误伤正常路径)。"""
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    eng.store.create("A")
    eng._tick()
    clock.now += 301
    eng._fire_due()
    assert len(fired) == 1


def test_fire_due_claim_is_one_shot(tmp_path):
    """归零认领权一次性: 第一次 _fire_due 触发后, 第二次不再触发。

    旧行为下 _fire_due 无"认领"概念, 连续两次调用会各取一个队首 ⇒ 两个
    run (AC-10.2 违反)。修复后倒计时被清 ⇒ 第二次认领失败。"""
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    eng.store.create("A")
    clock.now += 1
    eng.store.create("B")
    eng._tick()
    clock.now += 301
    eng._fire_due()
    assert len(fired) == 1
    eng._fire_due()                    # 第二次: 无可认领的倒计时 ⇒ 放弃
    assert len(fired) == 1, f"重复触发: fired={fired}"


def test_run_now_then_zero_no_double_fire(tmp_path):
    """立即执行成功后再归零 ⇒ 不再发起第二个 run (AC-10.2)。"""
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    eng.store.create("A")
    clock.now += 1
    eng.store.create("B")
    eng._tick()
    ok, reason = eng.run_now()
    assert ok, reason
    assert len(fired) == 1
    clock.now += 301
    eng._fire_due()
    assert len(fired) == 1, f"并发双触发: fired={fired}"


def test_fire_due_does_not_fire_when_countdown_restarted(tmp_path):
    """开关 OFF→ON 重启倒计时后, 旧的"归零"不得触发 (倒计时已作废)。"""
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    eng.store.create("A")
    eng._tick()
    clock.now += 301                  # 归零时刻
    eng.restart_countdown()           # 重开 ⇒ 旧倒计时作废
    eng._fire_due()
    assert fired == []


def test_fire_due_abandons_when_recheck_fails(tmp_path):
    """归零瞬间条件复查失败 ⇒ 放弃触发, 倒计时清空, 下轮重新起表。"""
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    eng.store.create("A")
    eng._tick()
    clock.now += 301
    eng._gateway_free = lambda: (False, "gateway_busy")
    eng._fire_due()
    assert fired == []
    assert eng.countdown_ends_at is None
    eng._gateway_free = lambda: (True, None)
    eng._tick()
    assert eng.countdown_ends_at == clock.now + 300


# ── F2: 开关 OFF→ON 重开完整 5 分钟 (REVIEW-164 P1-2, AC-4.3) ──────────
#
# 旧行为: 倒计时只在 _tick() 里因 _condition_ok() 返 False (开关关) 才被
# 撤销 ⇒ OFF→ON 若发生在同一个 ticker 间隔 (≤1s) 内, ticker 从未观察到
# "关"态 ⇒ 旧倒计时原样保留并走完剩余时间 (续用而非重开)。

def test_restart_countdown_clears_ends_at(tmp_path):
    """restart_countdown 显式清零倒计时 (AC-4.3 的前提动作)。"""
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    eng.store.create("A")
    eng._tick()
    assert eng.countdown_ends_at == clock.now + 300
    eng.restart_countdown()
    assert eng.countdown_ends_at is None


def test_off_on_restarts_full_countdown(tmp_path):
    """OFF→ON 后重新起**完整** 5 分钟 (不续用关闭前剩余) (AC-4.3)。

    模拟最坏情形: ticker 从未观察到"关"态 (整个 OFF→ON 在同一 tick 内),
    旧倒计时本会被原样保留 —— 修复后必须从 0 重新起表。"""
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    eng.store.create("A")
    eng._tick()
    clock.now += 200                  # 已消耗 200s, 剩 100s
    assert eng.countdown_ends_at == clock.now + 100
    # 关→开 (期间 ticker 未跑 ⇒ 从未观察到"关"态)
    eng.store.set_auto_execute(False)
    eng.store.set_auto_execute(True)
    eng.restart_countdown()
    eng.evaluate_now()
    # 下一轮 ticker ⇒ 重新起完整 300s
    eng._tick()
    assert eng.countdown_ends_at == clock.now + 300
    assert eng.snapshot()["countdown"]["remaining_s"] == 300


# ── N1: run-now 拒绝码可达 (issue #179) ───────────────────────────────
#
# 旧行为: _condition_ok() 在 has_pending()=False 时返回 (False, None)
# ⇒ run_now() 落 `reason or "not_ready"` ⇒ _RUN_NOW_ERRORS 查不到
# 'not_ready' ⇒ 默认 'schedule.err_bad_transition' ⇒ 界面只显示泛化
# 文案「当前状态不允许该操作」, 而非「没有待执行任务」。
#
# ⚠ 测试互不覆盖 (X-6): 本组用例**显式构造** no_pending 场景并 stub
# 掉探活分支, 不依赖 #181 的行为; #181 的用例则显式构造「有 pending
# 任务」, 否则会先命中 no_pending 分支而测不到 gateway_busy。

def test_run_now_no_pending_returns_no_pending_reason(tmp_path):
    """AC-179-1/4: 自动执行开 + 无 pending ⇒ run_now() 拒绝码 'no_pending'。

    MV-179-a: 把 _condition_ok 的 has_pending 分支改回 (False, None)
    ⇒ 本用例必须转红。
    """
    eng, fired = make_engine(tmp_path)
    eng._gateway_free = lambda: (True, None)     # stub 掉探活分支
    ok, reason = eng.run_now()
    assert ok is False
    assert reason == "no_pending", f"期望 no_pending, 实得 {reason!r}"


def test_run_now_no_pending_maps_to_specific_i18n(tmp_path, monkeypatch):
    """AC-179-1/3: API 层 ⇒ 409 且文案 = schedule.err_no_pending_task。

    MV-179-b: 把 run_now 的 `reason or "not_ready"` 改成恒 "not_ready"
    ⇒ 本用例必须转红 (防「改了 _condition_ok 但在 run_now 被吞掉」)。
    """
    import mw.i18n
    monkeypatch.setenv("MW_SCHEDULE_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("MW_LEDGER_DB", str(tmp_path / "none.db"))
    monkeypatch.setattr(S, "_engine", None)
    monkeypatch.setattr(S, "_engine_path", None)
    eng = S.get_engine()
    eng._gateway_free = lambda: (True, None)
    eng._spawn_fire = lambda task: None

    code, body = S.api_schedule_run_now()
    assert code == 409 and body["code"] == 409
    assert body["error"] == mw.i18n.t("schedule.err_no_pending_task")
    # 不得退化成泛化文案
    assert body["error"] != mw.i18n.t("schedule.err_bad_transition")


def test_run_now_no_pending_only_when_all_paused(tmp_path):
    """AC-179-1 边界: 任务全为 paused (无 pending) ⇒ 同样 'no_pending'。

    判定「有 pending」用的是 status='pending' 而非「队列非空」。
    """
    eng, fired = make_engine(tmp_path)
    eng._gateway_free = lambda: (True, None)
    t = eng.store.create("A")
    eng.store.pause(t["id"])                    # pending → paused
    ok, reason = eng.run_now()
    assert ok is False and reason == "no_pending"


def test_condition_ok_reason_not_consumed_by_tick(tmp_path):
    """回归锁: #179 只改 reason 码, 不改 ok 语义。

    _tick / _fire_due 只取 ok (丢弃 reason) ⇒ 倒计时起表/撤表与归零
    触发零影响 (AC-179-3 的隐含面)。
    """
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    eng._tick()                                 # 无任务 ⇒ 不起表
    assert eng.countdown_ends_at is None
    t = eng.store.create("A")
    eng._tick()                                 # 有 pending ⇒ 起表
    assert eng.countdown_ends_at == clock.now + 300
    # 条件破坏 (任务取消) ⇒ 撤表, reason 不参与判断
    eng.store.cancel(t["id"])
    eng._tick()
    assert eng.countdown_ends_at is None


# ── N2: 探活缓存时间戳 (issue #180) ──────────────────────────────────
#
# 旧行为: now = time.monotonic() 取在探测**之前**, 缓存写回用同一个旧
# now ⇒ 记的是探测**开始**时刻 ⇒ 探活耗时 ≥ TTL(3s) 时缓存永不命中
# (gateway 故障场景每次吃满 5s 超时 ⇒ /api/schedule/state 端到端
# 3.5~5s, 前端 2s 轮询 ⇒ 请求串行堆积)。

class _FakeResp:
    """最小 urllib 响应替身: 上下文管理器 + read() 返回 JSON 字节。"""

    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def make_real_probe_engine(tmp_path):
    """建一个**不** stub 探活的引擎。

    make_engine 会把 _gateway_free 换成恒返回空闲的 lambda, 那样就测不到
    探活缓存本身了 ⇒ 这里直接构造引擎实例。
    """
    store = S.ScheduleStore(str(tmp_path / "sched.db"))
    ldb = str(tmp_path / "ledger.db")
    return S.ScheduleEngine(store, ledger_db=ldb, countdown_s=300)


def test_probe_cache_hit_when_probe_slow(tmp_path, monkeypatch):
    """AC-180-1/3: 探活耗时 ≥ TTL ⇒ 第二次调用命中缓存 (不再发第二次请求)。

    MV-180-a: 把写回时刻改回探测**之前** ⇒ 本用例必须转红。

    为不拖慢测试, 把 TTL 缩到 50ms 并让假探活睡 120ms —— 保持「探活
    耗时 ≥ TTL」这一判据本身不变。
    """
    eng = make_real_probe_engine(tmp_path)
    monkeypatch.setattr(S, "_PROBE_TTL_S", 0.05)
    calls = []

    def slow_urlopen(req, timeout=None):
        calls.append(1)
        time.sleep(0.12)                        # 耗时 120ms > TTL 50ms
        return _FakeResp({"gateway_busy": False, "active_agents": 0})

    monkeypatch.setattr(S.urllib.request, "urlopen", slow_urlopen)

    first = eng._gateway_free()
    second = eng._gateway_free()
    assert first == (True, None)
    assert second == (True, None)
    assert len(calls) == 1, f"探活被重复发起: {len(calls)} 次"


def test_probe_cache_hit_when_probe_fails_slowly(tmp_path, monkeypatch):
    """AC-180-1 失败路径: 探活**异常**且耗时 ≥ TTL ⇒ 第二次命中缓存。

    MV-180-b: 只修成功路径、不修 except 分支 ⇒ 本用例必须转红。
    """
    eng = make_real_probe_engine(tmp_path)
    monkeypatch.setattr(S, "_PROBE_TTL_S", 0.05)
    calls = []

    def slow_fail(req, timeout=None):
        calls.append(1)
        time.sleep(0.12)
        raise OSError("gateway down")

    monkeypatch.setattr(S.urllib.request, "urlopen", slow_fail)

    first = eng._gateway_free()
    second = eng._gateway_free()
    assert first == (False, "gateway_probe_unavailable")
    assert second == (False, "gateway_probe_unavailable")
    assert len(calls) == 1, f"探活被重复发起: {len(calls)} 次"


def test_probe_cache_still_expires(tmp_path, monkeypatch):
    """回归锁: 缓存不是永久的 —— TTL 到期后必须重新探活。

    防「为修 #180 把缓存写死」的假修复。
    """
    eng = make_real_probe_engine(tmp_path)
    monkeypatch.setattr(S, "_PROBE_TTL_S", 0.05)
    calls = []

    def fast_urlopen(req, timeout=None):
        calls.append(1)
        return _FakeResp({"gateway_busy": False, "active_agents": 0})

    monkeypatch.setattr(S.urllib.request, "urlopen", fast_urlopen)
    eng._gateway_free()
    eng._gateway_free()                          # TTL 内 ⇒ 命中
    assert len(calls) == 1
    time.sleep(0.08)                             # TTL 到期
    eng._gateway_free()
    assert len(calls) == 2, "TTL 到期后必须重新探活"


# ── N3: busy 归因三态 (issue #181) ────────────────────────────────────
#
# 旧行为: val = (not busy and active == 0, None if not busy else
# "gateway_busy") ⇒ busy=False 时 reason 恒 None, 无论 active_agents
# 是多少 ⇒ 「busy=False 且 active>0」这个合法忙态被误报成
# gateway_probe_unavailable (UI 把「gateway 忙」说成「探活不可用」)。
#
# ⚠ 用例显式构造「有 pending 任务」, 否则 _condition_ok 会先命中
# no_pending 分支而测不到 gateway_busy (X-6)。

def _probe_stub(monkeypatch, payload):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        return _FakeResp(payload)

    monkeypatch.setattr(S.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_busy_reason_when_busy_false_but_active(tmp_path, monkeypatch):
    """AC-181-1/3: busy=False 且 active_agents>0 ⇒ reason == 'gateway_busy'。

    MV-181-a: 把三元式改回 `None if not busy else "gateway_busy"`
    ⇒ 本用例必须转红。
    """
    eng = make_real_probe_engine(tmp_path)
    eng.store.create("A")                       # 有 pending ⇒ 不命中 no_pending
    _probe_stub(monkeypatch, {"gateway_busy": False, "active_agents": 1})

    free, reason = eng._gateway_free()
    assert free is False
    assert reason == "gateway_busy", f"期望 gateway_busy, 实得 {reason!r}"
    # 引擎层归因同样不得回退成探活不可用
    assert eng._busy_reason() == "gateway_busy"


def test_busy_reason_when_busy_true(tmp_path, monkeypatch):
    """AC-181-2: busy=True ⇒ 归因保持 'gateway_busy' 不回退。"""
    eng = make_real_probe_engine(tmp_path)
    eng.store.create("A")
    _probe_stub(monkeypatch, {"gateway_busy": True, "active_agents": 0})

    free, reason = eng._gateway_free()
    assert free is False and reason == "gateway_busy"


def test_probe_error_reason_not_overwritten(tmp_path, monkeypatch):
    """AC-181-2: 探活异常 ⇒ 'gateway_probe_unavailable' 不被 busy 覆盖。

    MV-181-b: 把 except 分支的 reason 也改成 'gateway_busy'
    ⇒ 本用例必须转红 (证明三态互不覆盖)。
    """
    eng = make_real_probe_engine(tmp_path)
    eng.store.create("A")

    def boom(req, timeout=None):
        raise OSError("gateway down")

    monkeypatch.setattr(S.urllib.request, "urlopen", boom)
    free, reason = eng._gateway_free()
    assert free is False
    assert reason == "gateway_probe_unavailable"


def test_gateway_free_when_idle(tmp_path, monkeypatch):
    """回归锁: busy=False 且 active=0 ⇒ 空闲 (True, None)。"""
    eng = make_real_probe_engine(tmp_path)
    _probe_stub(monkeypatch, {"gateway_busy": False, "active_agents": 0})
    free, reason = eng._gateway_free()
    assert free is True and reason is None


# ════════════════════════════════════════════════════════════════════
# Q197: 倒计时绑定持有者 (暂停/恢复顺位状态机)
#
# 状态机 (PLAN-Q-20261102 §1.4):
#   IDLE (双 None) / ARMED(T, e) (持有者 T = pending 任务, 到期时刻 e)
#     T1 IDLE + C(t) + head=T      ⇒ ARMED(T, now+300)      起表
#     T2 ARMED + ¬C(t)             ⇒ IDLE                    撤表
#     T3 ARMED(T) + status(T)≠pending ⇒ IDLE ⇒ 同轮重评 ⇒ ARMED(T', now+300)
#                                    (顺位继承者拿**完整新表**)
#     T4 ARMED(T) + now≥e + 复查过 ⇒ 触发 T (持有者) → running; IDLE
#     T5 ARMED(T) + now≥e + 复查不过 ⇒ IDLE (弃轮, 下轮重数)
#     T6 开关 OFF→ON                ⇒ restart_countdown ⇒ 下轮完整重数
#     R1 恢复 (paused→pending)      ⇒ **不迁移** —— 恢复不顶当前持有者
# ════════════════════════════════════════════════════════════════════


def test_q197_countdown_binds_holder(tmp_path):
    """起表时倒计时绑定持有者 (队首), 不再是「无主倒计时」。

    MV-197-8: 把 _tick 起表分支的 countdown_task_id 赋值去掉 ⇒ 本用例红。
    """
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    a = eng.store.create("A")
    clock.now += 1
    b = eng.store.create("B")
    eng._tick()
    assert eng.countdown_ends_at == clock.now + 300
    assert eng.countdown_task_id == a["id"]        # 持有者 = 队首 (最早创建)
    # 持有者与结束时刻同生同灭
    eng.restart_countdown()
    assert eng.countdown_ends_at is None
    assert eng.countdown_task_id is None


def test_q197_pause_holder_transfers_with_full_new_countdown(tmp_path):
    """AC-Q197-7: 暂停持有者 ⇒ 停表 + 顺位继承者拿**完整新 5 分钟**。

    变异反证点 M4: 若实现继承剩余 R 秒 ⇒ ends_at != 暂停时刻+300 ⇒ 红。
    """
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    t1 = eng.store.create("T1")
    clock.now += 1
    t2 = eng.store.create("T2")
    eng._tick()
    assert eng.countdown_task_id == t1["id"]
    assert eng.countdown_ends_at == clock.now + 300
    clock.now += 100                     # 已消耗 100s, 剩 200s
    # 暂停持有者 T1
    assert eng.store.pause(t1["id"]) is True
    eng._tick()                          # ticker 重评 (同轮转移)
    # 停表 + 转移给 T2 + 完整新表
    assert eng.countdown_task_id == t2["id"]
    assert eng.countdown_ends_at == clock.now + 300, "顺位继承者必须拿完整新表"
    assert eng.countdown_ends_at != (clock.now + 200)


def test_q197_pause_non_holder_keeps_countdown(tmp_path):
    """AC-Q197-8: 暂停非队首 ⇒ 其倒计时**不受影响** (不重数)。"""
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    t1 = eng.store.create("T1")
    clock.now += 1
    t2 = eng.store.create("T2")
    clock.now += 1
    t3 = eng.store.create("T3")
    eng._tick()
    ends_before = eng.countdown_ends_at
    assert eng.countdown_task_id == t1["id"]
    clock.now += 50
    assert eng.store.pause(t3["id"]) is True
    eng._tick()
    # T1 仍持有表, 剩余时间不变 (不重数)
    assert eng.countdown_task_id == t1["id"]
    assert eng.countdown_ends_at == ends_before


def test_q197_pause_only_task_revokes_countdown(tmp_path):
    """AC-Q197-9: 暂停唯一任务 ⇒ 倒计时撤销 (无 pending 可执行)。"""
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    t1 = eng.store.create("T1")
    eng._tick()
    assert eng.countdown_ends_at is not None
    assert eng.store.pause(t1["id"]) is True
    eng._tick()
    assert eng.countdown_ends_at is None
    assert eng.countdown_task_id is None
    assert eng.snapshot()["countdown"] is None


def test_q197_resume_returns_to_head_without_topping_holder(tmp_path):
    """AC-Q197-10: 恢复回队首但**不顶掉**正在倒计时的任务。

    场景: T1 暂停 → T2 继承新表 → 恢复 T1 ⇒ T2 的表继续走, 不重置。
    变异反证点 M5: 若恢复顶掉持有者 ⇒ ends_at/持有者不变断言红。
    """
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    t1 = eng.store.create("T1")
    clock.now += 1
    t2 = eng.store.create("T2")
    eng._tick()
    assert eng.countdown_task_id == t1["id"]
    assert eng.store.pause(t1["id"]) is True
    eng._tick()
    assert eng.countdown_task_id == t2["id"]
    ends_before = eng.countdown_ends_at
    clock.now += 60
    # 恢复 T1 ⇒ 回到待执行列表第一位 (创建最早)
    assert eng.store.resume(t1["id"]) is True
    eng._tick()
    # 持有者仍是 T2, 表不重置
    assert eng.countdown_task_id == t2["id"]
    assert eng.countdown_ends_at == ends_before
    # T1 回到队首 (创建时间最早 ⇒ 升序第一位)
    assert eng.store.head_pending()["id"] == t1["id"]


def test_q197_countdown_fires_holder_not_new_head(tmp_path):
    """AC-Q197-10③: 归零触发的是**持有者**, 不是归零瞬间的队首。

    变异反证点 M3: 若归零时按队首取 ⇒ fired == [T1] ⇒ 本用例红。
    """
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    t1 = eng.store.create("T1")
    clock.now += 1
    t2 = eng.store.create("T2")
    eng._tick()
    assert eng.countdown_task_id == t1["id"]
    assert eng.store.pause(t1["id"]) is True
    eng._tick()                          # T2 继承
    assert eng.countdown_task_id == t2["id"]
    assert eng.store.resume(t1["id"]) is True   # T1 回到队首
    eng._tick()
    assert eng.countdown_task_id == t2["id"], "恢复不得顶掉持有者"
    clock.now += 301
    eng._tick()
    # 归零触发持有者 T2, 不是队首 T1
    assert [t["id"] for t in fired] == [t2["id"]]
    assert eng.store.get(t2["id"])["status"] == "running"
    assert eng.store.get(t1["id"])["status"] == "pending"


def test_q197_pause_holder_again_transfers_again(tmp_path):
    """AC-Q197-11: 持有者再被暂停 ⇒ 同规则再继承 (顺位继承者拿新表)。

    场景接 AC-Q197-10: T1 暂停 → T2 继承新表 → 恢复 T1 (T1 回到队首)
    → 在 T2 归零前暂停 T2 ⇒ 顺位继承者 (T1) 进入**新的完整 5 分钟**。
    """
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    t1 = eng.store.create("T1")
    clock.now += 1
    t2 = eng.store.create("T2")
    eng._tick()
    assert eng.countdown_task_id == t1["id"]
    assert eng.store.pause(t1["id"]) is True
    eng._tick()
    assert eng.countdown_task_id == t2["id"]
    assert eng.store.resume(t1["id"]) is True      # T1 回到待执行 (队首)
    clock.now += 50
    # 持有者 T2 再被暂停 ⇒ 顺位继承者 (T1) 拿新表
    assert eng.store.pause(t2["id"]) is True
    eng._tick()
    assert eng.countdown_task_id == t1["id"]
    assert eng.countdown_ends_at == clock.now + 300


def test_q197_single_countdown_slot_invariant(tmp_path):
    """AC-Q197-12: 恢复不得让引擎同时存在两个倒计时 (单槽结构天然保证)。"""
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    t1 = eng.store.create("T1")
    clock.now += 1
    t2 = eng.store.create("T2")
    eng._tick()
    eng.store.pause(t1["id"])
    eng._tick()
    eng.store.resume(t1["id"])
    eng._tick()
    eng._tick()
    # 仍是单槽: 一个 ends_at + 一个持有者
    assert isinstance(eng.countdown_ends_at, (int, float))
    assert eng.countdown_task_id is not None
    assert eng.countdown_task_id in (t1["id"], t2["id"])


def test_q197_run_now_fires_holder(tmp_path):
    """AC-Q197-14 + #197: 「立即执行」触发的是持有者 (与归零同源)。"""
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    t1 = eng.store.create("T1")
    clock.now += 1
    t2 = eng.store.create("T2")
    eng._tick()
    assert eng.store.pause(t1["id"]) is True
    eng._tick()
    assert eng.countdown_task_id == t2["id"]
    assert eng.store.resume(t1["id"]) is True
    ok, reason = eng.run_now()
    assert ok, reason
    assert [t["id"] for t in fired] == [t2["id"]]
    assert eng.countdown_ends_at is None
    assert eng.countdown_task_id is None


def test_q197_snapshot_shows_holder(tmp_path):
    """倒计时条显示的 next_task_* 是持有者 (与触发目标同源)。"""
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    t1 = eng.store.create("标题一")
    clock.now += 1
    t2 = eng.store.create("标题二")
    eng._tick()
    assert eng.store.pause(t1["id"]) is True
    eng._tick()
    snap = eng.snapshot()
    assert snap["countdown"]["next_task_id"] == t2["id"]
    assert snap["countdown"]["next_task_title"] == "标题二"


def test_q197_pause_api_wakes_ticker(tmp_path, monkeypatch):
    """暂停动作必须唤醒 ticker (即时重评, 不等下一个 1s 轮询)。

    MV-197-9: 把 _ACTIONS['pause'] 的 wake 改回 False ⇒ 本用例红。
    """
    monkeypatch.setenv("MW_SCHEDULE_DB", str(tmp_path / "sched.db"))
    monkeypatch.setenv("MW_LEDGER_DB", str(tmp_path / "none.db"))
    monkeypatch.setattr(S, "_engine", None)
    monkeypatch.setattr(S, "_engine_path", None)
    eng = S.get_engine()
    eng._gateway_free = lambda: (True, None)
    eng._spawn_fire = lambda task: None
    code, body = S.api_schedule_create({"detail": "T1"})
    assert code == 201
    tid = body["task"]["id"]
    woke = []
    eng._wake = type("W", (), {"set": lambda self: woke.append(1),
                               "wait": lambda self, t=None: False,
                               "clear": lambda self: None})()
    code, _ = S.api_schedule_action(tid, "pause")
    assert code == 200
    assert woke, "暂停未唤醒 ticker (持有者被暂停时倒计时不会即时转移)"
