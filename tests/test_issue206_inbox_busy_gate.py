"""tests/test_issue206_inbox_busy_gate.py — issue #206 守护套件。

inbox 倒计时「进行中」判据修复: _busy_reason() 第一判据 = schedule_tasks
存在 running 行 ⇒ busy (拒绝码 inbox_running), 覆盖「任务已交付 main 但
卡片/run 行尚未生成」窗口 —— 该窗口内 ledger 无 ACTIVE run 行, 旧判据
(_has_active_run + gateway 探活) 双盲区 ⇒ 下一任务倒计时照常走秒。
用户裁决: 判据以「有进行中任务」为准, 不再以是否有 run 为判断依据。

覆盖:
  I206-1  running 行存在 + ledger 无 run 行 ⇒ _condition_ok() False 且
          reason=inbox_running; _tick 不起表 (核心场景)
  I206-2  running 行消失 (mark_done / mark_running_failed) ⇒ busy 解除,
          下一轮 tick 给下一任务起完整新表
  I206-3  暂停/恢复回归: A running (无 run 行) + B pending ⇒ B 不起表;
          A done 后 B 起完整 300s 新表 (issue 原文场景)
  I206-4  run_now 在 inbox busy 时拒绝 (reason=inbox_running) 且
          _RUN_NOW_ERRORS 映射 schedule.err_run_busy (409 文案复用)
  I206-5  snapshot: busy_reason=inbox_running 且 running_task_id 同步,
          countdown=null (前端倒计时条自动隐藏, 前端零改动)
  I206-6  判据优先级: running 行存在时先于 active_run / gateway 归因
  I206-7  既有语义回归: 无 running 行时行为与旧版一致 (active_run /
          gateway_busy 归因不变, 起表照常)
  I206-8  僵尸 running 兜底: updated_at 超阈值 ⇒ 告警日志恰一次,
          不自动回退 pending (OQ-R1 未裁决)

变异反证 (实施卡实跑): 删除 _busy_reason 第一判据 ⇒ I206-1/2/3/4/5/6 转红。

跑法: pytest tests/test_issue206_inbox_busy_gate.py -v
"""
from __future__ import annotations

import logging
import sqlite3
import sys
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

    与 tests/test_schedule_engine.py::make_engine 同构 (不真起线程不碰真网络)。
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


def set_updated_at(eng, task_id, ts):
    """直改 schedule_tasks.updated_at (僵尸场景构造用)。"""
    c = sqlite3.connect(str(eng.store.path))
    c.execute("UPDATE schedule_tasks SET updated_at=? WHERE id=?", (ts, task_id))
    c.commit()
    c.close()


# ── I206-1: 核心场景 —— running 行 + 无 run 行 ⇒ busy ────────────────
def test_running_task_blocks_next_countdown(tmp_path):
    """issue #206 主场景: A 已交付 main (running) 但 run 行未生成,
    B 的倒计时不得起表。旧判据在此窗口双盲区 ⇒ 倒计时照走 (原缺陷)。"""
    eng, fired = make_engine(tmp_path)
    a = eng.store.create("任务A")
    b = eng.store.create("任务B")
    # A 触发转 running (take_pending 置位), ledger 无 run 行 (main 还在拆解)
    assert eng.store.take_pending(a["id"]) is not None
    assert eng._has_active_run() is False          # 旧判据盲区 (前提锚定)

    ok, reason = eng._condition_ok()
    assert ok is False and reason == "inbox_running"

    eng._tick()                                    # 不起表 (AC: B 不倒计时)
    assert eng.countdown_ends_at is None
    assert eng.countdown_task_id is None
    assert fired == []
    assert eng.store.get(b["id"])["status"] == "pending"


# ── I206-2: running 消失 ⇒ busy 解除, 下轮起完整新表 ─────────────────
def test_busy_clears_when_running_converges(tmp_path):
    """run 关闭 (mark_done) 或发起失败回退 (mark_running_failed) ⇒
    busy 自动解除, 无泄漏 (TRIAGE §2.4 风险 1)。"""
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    a = eng.store.create("任务A")
    b = eng.store.create("任务B")
    eng.store.take_pending(a["id"])
    eng._tick()
    assert eng.countdown_ends_at is None           # busy 中

    eng.store.mark_done(a["id"])                   # run 关闭 ⇒ 转 done
    ok, reason = eng._condition_ok()
    assert ok is True and reason is None
    eng._tick()
    # B 起**完整** 300s 新表 (不继承剩余)
    assert eng.countdown_ends_at == clock.now + 300
    assert eng.countdown_task_id == b["id"]

    # 回退路径同样解除
    eng2, _ = make_engine(tmp_path / "t2")
    c = eng2.store.create("任务C")
    d = eng2.store.create("任务D")
    eng2.store.take_pending(c["id"])
    assert eng2._condition_ok()[0] is False
    eng2.store.mark_running_failed(c["id"], "gateway down")
    assert eng2._condition_ok()[0] is True


# ── I206-3: 暂停/恢复回归 (issue 原文「暂停再恢复倒计时立即开始」) ────
def test_pause_resume_while_inbox_running(tmp_path):
    """A running (无 run 行) + B pending: 对 B 暂停再恢复 ⇒ 仍不起表;
    A 完成后 B 起完整新表 (与 #197 持有者顺位语义零冲突)。"""
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    a = eng.store.create("任务A")
    b = eng.store.create("任务B")
    eng.store.take_pending(a["id"])                # A 交付 main, 无 run 行

    eng._tick()
    assert eng.countdown_ends_at is None           # B 不起表
    assert eng.store.pause(b["id"]) is True        # 暂停 B
    eng._tick()
    assert eng.store.resume(b["id"]) is True       # 恢复 B
    eng._tick()
    # A 仍 running ⇒ B 依旧不起表 (原缺陷: 恢复即起表走秒)
    assert eng.countdown_ends_at is None

    eng.store.mark_done(a["id"])                   # A 完成
    eng._tick()
    assert eng.countdown_ends_at == clock.now + 300   # B 完整新表
    assert eng.countdown_task_id == b["id"]


# ── I206-4: run_now 在 inbox busy 时拒绝 ─────────────────────────────
def test_run_now_rejected_while_inbox_running(tmp_path):
    eng, fired = make_engine(tmp_path)
    a = eng.store.create("任务A")
    eng.store.create("任务B")
    eng._tick()                                    # B 起表
    eng.store.take_pending(a["id"])                # A 随后交付 main

    ok, reason = eng.run_now()
    assert ok is False and reason == "inbox_running"
    assert fired == []
    # 拒绝码 → i18n 映射: 复用「A run is in progress」文案, 不新增键
    assert S._RUN_NOW_ERRORS["inbox_running"] == "schedule.err_run_busy"


# ── I206-5: snapshot 可观测性 ────────────────────────────────────────
def test_snapshot_exposes_inbox_running(tmp_path):
    eng, _ = make_engine(tmp_path)
    a = eng.store.create("任务A")
    eng.store.create("任务B")
    eng._tick()                                    # B 起表
    eng.store.take_pending(a["id"])
    eng._tick()                                    # busy ⇒ 撤表
    snap = eng.snapshot()
    assert snap["busy_reason"] == "inbox_running"
    assert snap["running_task_id"] == a["id"]
    assert snap["countdown"] is None               # 前端倒计时条自动隐藏


# ── I206-6: 判据优先级 inbox_running > active_run > gateway ─────────
def test_busy_reason_precedence(tmp_path):
    eng, _ = make_engine(tmp_path)
    a = eng.store.create("任务A")
    eng.store.take_pending(a["id"])
    # ledger 同时有 ACTIVE run ⇒ 第一判据先归因
    lc = sqlite3.connect(eng.ledger_db)
    lc.execute("INSERT INTO team_work_runs VALUES ('r1','s1','ACTIVE')")
    lc.commit()
    lc.close()
    assert eng._busy_reason() == "inbox_running"
    # 无 running 行时 gateway 归因不受影响 (先关闭 run 行, 隔离归因源)
    eng.store.mark_done(a["id"])
    lc = sqlite3.connect(eng.ledger_db)
    lc.execute("UPDATE team_work_runs SET status='CLOSED'")
    lc.commit()
    lc.close()
    eng._gateway_free = lambda: (False, "gateway_busy")
    assert eng._busy_reason() == "gateway_busy"


# ── I206-7: 无 running 行 ⇒ 既有语义零回归 ──────────────────────────
def test_no_running_row_behavior_unchanged(tmp_path):
    clock = FakeClock()
    eng, fired = make_engine(tmp_path, clock)
    t = eng.store.create("任务A")
    eng._tick()                                    # 无 running ⇒ 照常起表
    assert eng.countdown_ends_at == clock.now + 300
    assert eng._busy_reason() is None
    # active_run 归因保留 (第二判据, 「或」叠加)
    eng2, _ = make_engine(tmp_path / "t2")
    eng2.store.create("任务B")
    lc = sqlite3.connect(eng2.ledger_db)
    lc.execute("INSERT INTO team_work_runs VALUES ('r9','s9','ACTIVE')")
    lc.commit()
    lc.close()
    assert eng2._busy_reason() == "active_run"
    eng2._tick()
    assert eng2.countdown_ends_at is None


# ── I206-8: 僵尸 running 兜底告警 (仅告警, 不自动回退) ───────────────
def test_zombie_running_warns_once(tmp_path, caplog):
    eng, _ = make_engine(tmp_path)
    a = eng.store.create("任务A")
    eng.store.take_pending(a["id"])
    # 伪造陈旧 updated_at: 超过 2×3600s 阈值
    set_updated_at(eng, a["id"], time.time() - S._ZOMBIE_RUNNING_WARN_S - 10)
    with caplog.at_level(logging.WARNING, logger="mw.schedule"):
        eng._observe_running()
        eng._observe_running()                     # 第二次 ⇒ 去重不刷屏
    warns = [r for r in caplog.records
             if r.levelno >= logging.WARNING and a["id"] in r.getMessage()]
    assert len(warns) == 1
    # OQ-R1 未裁决 ⇒ 不自动回退 pending (行仍 running, busy 仍生效)
    assert eng.store.get(a["id"])["status"] == "running"
    assert eng._busy_reason() == "inbox_running"
    # 未超阈值 ⇒ 不告警
    eng2, _ = make_engine(tmp_path / "t2")
    b = eng2.store.create("任务B")
    eng2.store.take_pending(b["id"])
    with caplog.at_level(logging.WARNING, logger="mw.schedule"):
        eng2._observe_running()
    assert not [r for r in caplog.records
                if r.levelno >= logging.WARNING and b["id"] in r.getMessage()]
