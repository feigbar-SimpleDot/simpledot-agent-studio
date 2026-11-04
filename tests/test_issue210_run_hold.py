"""tests/test_issue210_run_hold.py — #210 run hold: 迁移 + 状态机 + 端点契约。

依据: docs/PLAN-U-20261104.md §1 (U 批 2026-11-04, s2 实施卡)。

覆盖 (PLAN-U §1.5 回归断言清单):
  MIG1 ledger 迁移: 旧库 (CHECK 无 HOLD) → 表重建含 HOLD + hold_* 列,
       数据保留、行数一致、备份存在、幂等重开无残留
  MIG2 schedule 迁移: v2 库 (CHECK 无 hold) → v3 重建含 hold, 数据保留
  SM   ledger 状态机: hold ACTIVE→HOLD / resume HOLD→ACTIVE /
       幂等 0 行 / UNIQUE 冲突 raise IntegrityError
  EP1  hold 端点: 404 / 成功 (hold_reason+actor 留痕) / 幂等 / 非 ACTIVE 409 /
       current_card 不动 (§1.3 不杀 worker 冻结语义)
  EP2  resume 端点: 404 / 成功 / 已 ACTIVE 幂等 / 终态 409 /
       UNIQUE 冲突 409「已有执行中的 run」
  EP3  HOLD run 允许 close / archive (§1.3 边界裁决)
  LK   schedule 联动: hold→running inbox 任务置 hold;
       resume→pending 且 added_at = min(pending)-1 回队顶
  BUSY 「唯一执行中」判定不含 hold: list_by_status('running') 不含 hold;
       _busy_reason / _has_active_run 不把 HOLD run 算作执行中
  EVT  team_work.held / team_work.resumed 事件落 mw_events

跑法: pytest tests/test_issue210_run_hold.py -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mw.console as console  # noqa: E402
from mw.ledger import Ledger  # noqa: E402
from mw import schedule as S  # noqa: E402


# ── 旧 schema 快照 (HEAD 8293fce 实测, 迁移测试的「迁移前世界」) ──
_OLD_TW_RUNS_DDL = """
CREATE TABLE team_work_runs (
    run_id              TEXT PRIMARY KEY,
    initiator_session   TEXT NOT NULL,
    initial_task        TEXT NOT NULL,
    project_workspace   TEXT,
    current_card_id     TEXT,
    status              TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK(status IN ('ACTIVE','CLOSED','ABORTED','ARCHIVED')),
    card_count          INTEGER NOT NULL DEFAULT 0,
    created_at          REAL NOT NULL,
    closed_at           REAL,
    closed_reason       TEXT,
    last_null_slot_knock_at REAL,
    current_card_cleared_at REAL,
    current_card_handoff_at REAL,
    last_waiting_at     REAL
);
CREATE UNIQUE INDEX idx_team_work_one_active
    ON team_work_runs(status) WHERE status='ACTIVE';
"""

_OLD_SCHED_TASKS_DDL = """
CREATE TABLE schedule_tasks (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    detail       TEXT NOT NULL,
    priority     TEXT NOT NULL DEFAULT 'normal'
                 CHECK(priority IN ('urgent','high','normal','low')),
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','paused','running','done','cancelled')),
    added_at     REAL NOT NULL,
    executed_at  REAL,
    finished_at  REAL,
    added_by     TEXT NOT NULL DEFAULT '',
    workflow     TEXT NOT NULL DEFAULT 'auto',
    workspace    TEXT NOT NULL DEFAULT '',
    session_id   TEXT,
    run_id       TEXT,
    last_error   TEXT,
    updated_at   REAL NOT NULL
);
CREATE TABLE schedule_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时 ledger.db 注入 console.LEDGER_DB + MW_SCHEDULE_DB。"""
    led_path = str(tmp_path / "ledger.db")
    monkeypatch.setattr(console, "LEDGER_DB", led_path)
    monkeypatch.setenv("MW_SCHEDULE_DB", str(tmp_path / "schedule.db"))
    monkeypatch.setenv("MW_LEDGER_DB", led_path)
    led = Ledger(led_path)
    yield led, led_path
    led.close()


def _tw_ddl(led):
    return led.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='team_work_runs'").fetchone()[0]


# ════════════════════════════════════════════════════════════════════
# MIG1: ledger 表重建迁移
# ════════════════════════════════════════════════════════════════════
def test_mig1_ledger_rebuild_adds_hold(tmp_path):
    db = str(tmp_path / "ledger.db")
    c = sqlite3.connect(db)
    c.executescript(_OLD_TW_RUNS_DDL)
    c.execute(
        "INSERT INTO team_work_runs VALUES "
        "('r_old','s1','初始需求',NULL,'c1','ACTIVE',1,100.0,NULL,NULL,"
        "NULL,NULL,NULL,NULL)")
    c.execute("PRAGMA user_version=6")
    c.commit()
    c.close()

    led = Ledger(db)
    try:
        # CHECK 已含 HOLD
        assert "'HOLD'" in _tw_ddl(led)
        # hold 留痕三列已建
        cols = {r["name"] for r in led.conn.execute(
            "PRAGMA table_info(team_work_runs)")}
        assert {"hold_reason", "hold_actor", "hold_at"} <= cols
        # 数据保留 + 行数一致
        row = led.get_team_work("r_old")
        assert row is not None and row["status"] == "ACTIVE"
        assert row["current_card_id"] == "c1"
        assert led.conn.execute(
            "SELECT COUNT(*) FROM team_work_runs").fetchone()[0] == 1
        # 备份文件存在 (PLAN-U §1.2 硬口径: 迁移前强制备份)
        assert os.path.exists(db + ".pre-hold-migration")
        # partial UNIQUE INDEX 仍在 (唯一 ACTIVE 兜底不因重建丢失)
        with pytest.raises(sqlite3.IntegrityError):
            led.conn.execute(
                "INSERT INTO team_work_runs (run_id, initiator_session, "
                "initial_task, status, created_at) "
                "VALUES ('r_dup','s2','x','ACTIVE',1)")
        # 无 __old 残留
        assert led.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%__old'"
        ).fetchone()[0] == 0
        led.conn.rollback()
    finally:
        led.close()

    # 幂等: 重开不再触发重建 (内容门控), 数据仍在
    led2 = Ledger(db)
    try:
        assert led2.get_team_work("r_old") is not None
        assert "'HOLD'" in _tw_ddl(led2)
    finally:
        led2.close()


def test_mig1_hold_writable_after_migration(tmp_path):
    db = str(tmp_path / "ledger.db")
    c = sqlite3.connect(db)
    c.executescript(_OLD_TW_RUNS_DDL)
    c.execute("PRAGMA user_version=6")
    c.commit()
    c.close()
    led = Ledger(db)
    try:
        led.conn.execute(
            "INSERT INTO team_work_runs (run_id, initiator_session, "
            "initial_task, status, created_at) "
            "VALUES ('r_h','s1','x','HOLD',1)")  # 迁移前会撞 CHECK
        led.conn.commit()
        assert led.get_team_work("r_h")["status"] == "HOLD"
    finally:
        led.close()


# ════════════════════════════════════════════════════════════════════
# MIG2: schedule 表重建迁移
# ════════════════════════════════════════════════════════════════════
def test_mig2_schedule_rebuild_adds_hold(tmp_path):
    db = str(tmp_path / "schedule.db")
    c = sqlite3.connect(db)
    c.executescript(_OLD_SCHED_TASKS_DDL)
    c.execute("INSERT INTO schedule_settings VALUES "
              "('auto_execute_enabled','1')")
    c.execute("INSERT INTO schedule_settings VALUES ('schema_version','2')")
    c.execute(
        "INSERT INTO schedule_tasks VALUES "
        "('t_old','标题','详情','normal','pending',100.0,NULL,NULL,"
        "'','auto','',NULL,NULL,NULL,100.0)")
    c.commit()
    c.close()

    store = S.ScheduleStore(db)
    created = store.create("迁移后新任务")  # 写连接触发迁移
    assert created["status"] == "pending"

    ddl = sqlite3.connect(db).execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='schedule_tasks'").fetchone()[0]
    assert "'hold'" in ddl
    t = store.get("t_old")
    assert t is not None and t["status"] == "pending"  # 数据保留
    # 版本行推进到 3
    ver = sqlite3.connect(db).execute(
        "SELECT value FROM schedule_settings WHERE key='schema_version'"
    ).fetchone()[0]
    assert int(ver) == S.SCHEMA_VERSION == 3
    # 迁移后 CHECK 放行 hold
    sqlite3.connect(db).execute(
        "UPDATE schedule_tasks SET status='hold' WHERE id='t_old'")


# ════════════════════════════════════════════════════════════════════
# SM: ledger 状态机
# ════════════════════════════════════════════════════════════════════
def test_sm_hold_resume_state_machine(env):
    led, _ = env
    led.create_team_work_run("r1", "s1", "需求")
    assert led.hold_team_work("r1", reason="卡死", actor="main") == "HOLD"
    r = led.get_team_work("r1")
    assert r["status"] == "HOLD"
    assert r["hold_reason"] == "卡死" and r["hold_actor"] == "main"
    assert r["hold_at"] is not None
    # 幂等: 已 HOLD 再 hold ⇒ 0 行 None
    assert led.hold_team_work("r1", reason="x", actor="y") is None
    assert led.resume_team_work("r1", actor="user") == "ACTIVE"
    assert led.get_team_work("r1")["status"] == "ACTIVE"
    # 幂等: 已 ACTIVE 再 resume ⇒ 0 行 None
    assert led.resume_team_work("r1", actor="user") is None


def test_sm_resume_unique_conflict(env):
    led, _ = env
    led.create_team_work_run("r1", "s1", "需求1")
    assert led.hold_team_work("r1", reason="冻结", actor="main") == "HOLD"
    led.create_team_work_run("r2", "s2", "需求2")  # hold 后空位, r2 可起
    # resume r1 撞 partial UNIQUE INDEX ⇒ IntegrityError (端点转 409)
    with pytest.raises(sqlite3.IntegrityError):
        led.resume_team_work("r1", actor="user")
    assert led.get_team_work("r1")["status"] == "HOLD"  # 回滚未污染


def test_sm_hold_keeps_current_card(env):
    """§1.3: hold 不杀 worker、current_card 不动 (冻结=不再推进)。"""
    led, _ = env
    led.create_team_work_run("r1", "s1", "需求")
    led.put_contract("t_cur", {"required_files": [], "timeout_seconds": 60})
    led.register_team_work_card("r1", "t_cur")
    led.set_team_work_current_card("r1", "t_cur")
    led.hold_team_work("r1", reason="x", actor="t")
    r = led.get_team_work("r1")
    assert r["status"] == "HOLD"
    assert r["current_card_id"] == "t_cur"


# ════════════════════════════════════════════════════════════════════
# EP1/EP2: hold / resume 端点契约
# ════════════════════════════════════════════════════════════════════
def test_ep1_hold_endpoint(env):
    led, _ = env
    # 404
    resp = console.api_team_work_hold("nope", {})
    assert resp["ok"] is False and resp["code"] == 404
    # 成功 + 留痕
    led.create_team_work_run("r1", "s1", "需求")
    resp = console.api_team_work_hold(
        "r1", {"reason": "card 卡死", "actor": "main"})
    assert resp["ok"] is True and resp["status"] == "HOLD"
    assert resp["reason"] == "card 卡死" and resp["actor"] == "main"
    r = led.get_team_work("r1")
    assert r["status"] == "HOLD" and r["hold_reason"] == "card 卡死"
    # 幂等 (§1.3: 重复 hold 已 HOLD ⇒ 幂等成功不报错)
    resp2 = console.api_team_work_hold("r1", {})
    assert resp2["ok"] is True and resp2.get("was_already") is True


def test_ep1_hold_non_active_409(env):
    led, _ = env
    led.create_team_work_run("r1", "s1", "需求")
    led.close_team_work("r1", "test", final_status="CLOSED")
    resp = console.api_team_work_hold("r1", {})
    assert resp["ok"] is False and resp["code"] == 409


def test_ep2_resume_endpoint(env):
    led, _ = env
    # 404
    resp = console.api_team_work_hold_resume("nope", {})
    assert resp["ok"] is False and resp["code"] == 404
    # 已 ACTIVE ⇒ 幂等
    led.create_team_work_run("r1", "s1", "需求")
    resp = console.api_team_work_hold_resume("r1", {})
    assert resp["ok"] is True and resp.get("was_already") is True
    # hold → resume 成功
    console.api_team_work_hold("r1", {"actor": "main"})
    resp = console.api_team_work_hold_resume("r1", {"actor": "user"})
    assert resp["ok"] is True and resp["status"] == "ACTIVE"
    assert led.get_team_work("r1")["status"] == "ACTIVE"


def test_ep2_resume_terminal_409(env):
    led, _ = env
    led.create_team_work_run("r1", "s1", "需求")
    led.close_team_work("r1", "test", final_status="CLOSED")
    resp = console.api_team_work_hold_resume("r1", {})
    assert resp["ok"] is False and resp["code"] == 409


def test_ep2_resume_unique_conflict_409(env):
    """§1.3: resume 撞 UNIQUE (已有新 ACTIVE run) ⇒ 409 + 人话提示。"""
    led, _ = env
    led.create_team_work_run("r1", "s1", "需求1")
    console.api_team_work_hold("r1", {})
    led.create_team_work_run("r2", "s2", "需求2")
    resp = console.api_team_work_hold_resume("r1", {})
    assert resp["ok"] is False and resp["code"] == 409
    assert "已有执行中的 run" in resp["error"]
    assert led.get_team_work("r1")["status"] == "HOLD"
    assert led.get_team_work("r2")["status"] == "ACTIVE"


# ════════════════════════════════════════════════════════════════════
# EP3: HOLD run 允许 close / archive (§1.3 边界裁决)
# ════════════════════════════════════════════════════════════════════
def test_ep3_close_hold_run(env):
    led, _ = env
    led.create_team_work_run("r1", "s1", "需求")
    console.api_team_work_hold("r1", {})
    resp = console.api_team_work_close(
        "r1", {"reason": "user_close", "operator": "op"})
    assert resp.get("ok") is True
    assert led.get_team_work("r1")["status"] in ("CLOSED", "ABORTED")


def test_ep3_archive_hold_run(env):
    led, _ = env
    led.create_team_work_run("r1", "s1", "需求")
    console.api_team_work_hold("r1", {})
    res = led.archive_team_work("r1")
    assert res["ok"] is True and res["was_already"] is False
    assert led.get_team_work("r1")["status"] == "ARCHIVED"


# ════════════════════════════════════════════════════════════════════
# LK: schedule 联动 (hold→hold 态; resume→pending 队顶)
# ════════════════════════════════════════════════════════════════════
def test_lk_schedule_linkage(env):
    led, _ = env
    store = S.ScheduleStore()
    led.create_team_work_run("r1", "s1", "需求")
    ta = store.create("先建任务")            # 队列在前的 pending
    tb = store.create("被冻结任务")          # 后建, 被派发成 running
    store.take_pending(tb["id"])
    store.backfill_run(tb["id"], "r1")
    assert store.get(tb["id"])["status"] == "running"

    # hold: running inbox 任务置 hold
    resp = console.api_team_work_hold("r1", {"actor": "main"})
    assert resp["ok"] is True
    assert resp["held_tasks"] == [tb["id"]]
    assert resp["schedule_linkage"] == "ok"
    assert store.get(tb["id"])["status"] == "hold"
    assert store.list_by_status("running") == []

    # resume: 回 pending 且 added_at = min(pending)-1 ⇒ 队顶
    resp = console.api_team_work_hold_resume("r1", {"actor": "user"})
    assert resp["ok"] is True
    assert resp["resumed_tasks"] == [tb["id"]]
    tb_after = store.get(tb["id"])
    assert tb_after["status"] == "pending"
    assert tb_after["added_at"] == pytest.approx(
        store.get(ta["id"])["added_at"] - 1.0)
    head = store.head_pending()
    assert head["id"] == tb["id"]  # 回到待执行队列顶部


def test_lk_hold_no_linked_task_is_noop(env):
    """run 无关联 running inbox 任务 (非 inbox 发起) ⇒ 联动空转不报错。"""
    led, _ = env
    S.ScheduleStore()  # 建库
    led.create_team_work_run("r1", "s1", "需求")
    resp = console.api_team_work_hold("r1", {})
    assert resp["ok"] is True and resp["held_tasks"] == []
    resp = console.api_team_work_hold_resume("r1", {})
    assert resp["ok"] is True and resp["resumed_tasks"] == []


# ════════════════════════════════════════════════════════════════════
# BUSY: 「唯一执行中」判定不含 hold
# ════════════════════════════════════════════════════════════════════
def test_busy_reason_excludes_hold(env):
    led, led_path = env
    store = S.ScheduleStore()
    engine = S.ScheduleEngine(store=store, ledger_db=led_path)
    monkey = engine
    monkey._gateway_free = lambda: (True, None)
    # ACTIVE run ⇒ busy
    led.create_team_work_run("r1", "s1", "需求")
    assert engine._busy_reason() == "active_run"
    # HOLD run ⇒ 不算执行中 (第二判据 _has_active_run 不命中)
    led.hold_team_work("r1", reason="x", actor="t")
    assert engine._has_active_run() is False
    assert engine._busy_reason() is None


def test_busy_reason_excludes_hold_inbox_task(env):
    """hold 态 inbox 任务不被 list_by_status('running') 命中 ⇒ 第一判据放行。"""
    store = S.ScheduleStore()
    t = store.create("任务")
    store.take_pending(t["id"])
    store.backfill_run(t["id"], "r_x")
    store.hold_running_for_run("r_x")
    assert store.list_by_status("running") == []
    assert store.list_by_status("hold") != []


# ════════════════════════════════════════════════════════════════════
# EVT: 事件留痕
# ════════════════════════════════════════════════════════════════════
def test_events_emitted(env):
    led, _ = env
    led.create_team_work_run("r1", "s1", "需求")
    console.api_team_work_hold("r1", {"actor": "main"})
    console.api_team_work_hold_resume("r1", {"actor": "user"})
    types = {r["event_type"] for r in led.conn.execute(
        "SELECT event_type FROM mw_events").fetchall()}
    assert "team_work.held" in types
    assert "team_work.resumed" in types
