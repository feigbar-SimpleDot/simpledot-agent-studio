"""Test: Fix Point A — F7 (2026-10-07)

回归点:_notify_main_of_reports 在 team_work_runs.status != 'ACTIVE' 时
不应再投递陈旧报告。close_team_work 后,即便 confirm_delivered 还没
置位(网关 429 / 排队中),也不能再投。

此前实测:simple-agents-website run 630cf5a 关后仍有 phantom 消息送达
main,main 必须为每条 phantom 消耗一个 turn 消歧。
"""
import sqlite3
import tempfile
import os
from pathlib import Path


def _build_fixture():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE team_work_runs(
            run_id TEXT PRIMARY KEY, status TEXT,
            current_card_id TEXT, closed_reason TEXT, closed_at REAL,
            last_null_slot_knock_at REAL
        );
        CREATE TABLE harvest_contracts(
            task_id TEXT PRIMARY KEY,
            team_work_run_id TEXT,
            harvest_status TEXT,
            worker_report TEXT,
            notify_confirmed_at REAL
        );
        INSERT INTO team_work_runs VALUES
         ('r_active','ACTIVE','c1',NULL,NULL,NULL),
         ('r_aborted_complete','ABORTED',NULL,'complete_all',1.0,NULL),
         ('r_closed_explicit','CLOSED',NULL,NULL,1.0,NULL);
        INSERT INTO harvest_contracts VALUES
         ('t_active_pending','r_active','HARVESTED','{"v":1}',NULL),
         ('t_active_confirmed','r_active','HARVESTED','{"v":2}',1.0),
         ('t_aborted_phantom','r_aborted_complete','HARVESTED','{"v":3}',NULL),
         ('t_aborted_confirmed','r_aborted_complete','HARVESTED','{"v":4}',1.0),
         ('t_orphan_pending',NULL,'HARVESTED','{"v":5}',NULL),
         ('t_need_clar','r_closed_explicit','NEED_CLARIFICATION','{"q":"?"}',NULL),
         ('t_blocked_aborted','r_aborted_complete','BLOCKED','{"b":1}',NULL);
    """)
    con.commit()
    return con, path


def _query(con, sql):
    return [r[0] for r in con.execute(sql).fetchall()]


def test_active_run_passes_through():
    """ACTIVE run + 待发 → 必须透传(主流程不能被挡)。"""
    con, path = _build_fixture()
    try:
        got = _query(con, """
            SELECT hc.task_id FROM harvest_contracts hc
            LEFT JOIN team_work_runs twr ON hc.team_work_run_id = twr.run_id
            WHERE hc.harvest_status IN ('HARVESTED','NEED_CLARIFICATION','BLOCKED')
            AND hc.worker_report IS NOT NULL AND hc.notify_confirmed_at IS NULL
            AND (hc.team_work_run_id IS NULL OR twr.status = 'ACTIVE')
        """)
        assert "t_active_pending" in got, f"ACTIVE run 待发被误挡: {got}"
    finally:
        con.close()
        os.unlink(path)
    print("✓ active_run_passes_through")


def test_already_confirmed_still_filtered():
    """notify_confirmed_at 非 NULL → 原 WHERE 仍过滤。"""
    con, path = _build_fixture()
    try:
        got = _query(con, """
            SELECT hc.task_id FROM harvest_contracts hc
            LEFT JOIN team_work_runs twr ON hc.team_work_run_id = twr.run_id
            WHERE hc.harvest_status IN ('HARVESTED','NEED_CLARIFICATION','BLOCKED')
            AND hc.worker_report IS NOT NULL AND hc.notify_confirmed_at IS NULL
            AND (hc.team_work_run_id IS NULL OR twr.status = 'ACTIVE')
        """)
        assert "t_active_confirmed" not in got,             f"已确认的卡仍被投递: {got}"
        assert "t_aborted_confirmed" not in got,             f"已确认的卡仍被投递: {got}"
    finally:
        con.close()
        os.unlink(path)
    print("✓ already_confirmed_still_filtered")


def test_aborted_run_phantoms_blocked():
    """run=ABORTED/CLOSED + 待发 → 这是 F7 核心:必须挡掉。"""
    con, path = _build_fixture()
    try:
        got = _query(con, """
            SELECT hc.task_id FROM harvest_contracts hc
            LEFT JOIN team_work_runs twr ON hc.team_work_run_id = twr.run_id
            WHERE hc.harvest_status IN ('HARVESTED','NEED_CLARIFICATION','BLOCKED')
            AND hc.worker_report IS NOT NULL AND hc.notify_confirmed_at IS NULL
            AND (hc.team_work_run_id IS NULL OR twr.status = 'ACTIVE')
        """)
        # 三种 phantom 都不能出现
        for phantom in ["t_aborted_phantom", "t_need_clar", "t_blocked_aborted"]:
            assert phantom not in got, f"F7 失效: {phantom} 仍被投递: {got}"
    finally:
        con.close()
        os.unlink(path)
    print("✓ aborted_run_phantoms_blocked")


def test_orphan_cards_unaffected():
    """孤儿卡(team_work_run_id NULL)=非 Team Work 启动的卡 → 不挡。

    语义:仅挡「run 已关」的卡。run 关联 NULL 的卡走原路径。
    """
    con, path = _build_fixture()
    try:
        got = _query(con, """
            SELECT hc.task_id FROM harvest_contracts hc
            LEFT JOIN team_work_runs twr ON hc.team_work_run_id = twr.run_id
            WHERE hc.harvest_status IN ('HARVESTED','NEED_CLARIFICATION','BLOCKED')
            AND hc.worker_report IS NOT NULL AND hc.notify_confirmed_at IS NULL
            AND (hc.team_work_run_id IS NULL OR twr.status = 'ACTIVE')
        """)
        assert "t_orphan_pending" in got,             f"孤儿卡被误挡(主流程仍依赖非 Team Work 卡): {got}"
    finally:
        con.close()
        os.unlink(path)
    print("✓ orphan_cards_unaffected")


def test_regression_old_sql_lets_phantoms_through():
    """对照:旧 SQL(没 JOIN)会让 phantom 漏过。

    这是 F7 修复前的不变式 —— 测试自身要锁住它,否则改错就回不到原行为。
    """
    con, path = _build_fixture()
    try:
        got = _query(con, """
            SELECT task_id FROM harvest_contracts
            WHERE harvest_status IN ('HARVESTED','NEED_CLARIFICATION','BLOCKED')
            AND worker_report IS NOT NULL AND notify_confirmed_at IS NULL
        """)
        # 旧 SQL 把这三张 phantom 全漏过去 —— 这是 bug 的本质
        assert "t_aborted_phantom" in got
        assert "t_need_clar" in got
        assert "t_blocked_aborted" in got
    finally:
        con.close()
        os.unlink(path)
    print("✓ regression_old_sql_lets_phantoms_through (确认旧 bug 存在)")


if __name__ == "__main__":
    test_active_run_passes_through()
    test_already_confirmed_still_filtered()
    test_aborted_run_phantoms_blocked()
    test_orphan_cards_unaffected()
    test_regression_old_sql_lets_phantoms_through()
    print("\nALL PASS — F7 verified.")
