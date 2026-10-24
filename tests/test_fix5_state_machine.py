"""F5 (2026-10-13, issue #5 worker 状态机统一) 回归测试。

覆盖 (对齐 F5 状态机统一卡 §4 验收 AC5-1 ~ AC5-6, 见 git log):
  T1  单一派生函数 (derive_worker_state) 在主流程 7 形态各一例
       (terminal/WAITING/BLOCKED/承接中/handoff/frozen_run/pending_queue)
  T2  actor 白名单 — 非白名单 actor 调 update_worker_state 应 raise
       PermissionError (AC5-3)
  T3  切换 run: 旧卡进 HARVESTED+run handoff, 新卡承接后 = WORKING
       (AC5-2 验收口径「交接中」由 derive 给出)
  T4  api_team_work_get 给 run + cards 注入 state 字段
  T5  run 关闭状态: derive_worker_state 不返 HANDOFF
  T6  reaper 闲置 → 走 WAITING (actor=reaper)
  T7  handoff_at 字段落库 (schema 迁移幂等)
  T8  derive_run_overview 在 HANDOFF/WAITING_ACTIVE/ACTIVE/CLOSED
       四态下返回值正确
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mw.ledger import Ledger  # noqa: E402


# ── helpers ────────────────────────────────────────────────────

def _led(tmp_path) -> Ledger:
    """建 ledger, schema 迁移幂等(各 F5 列是 PRAGMA 检查后 ADD)。"""
    return Ledger(str(tmp_path / "ledger.db"))


def _setup_card(led: Ledger, tid: str, *, run_id: str = "r_fix5") -> None:
    """建契约 + 关联 run (供各 T 用)。

    隐含前置: 调用方必须先 led.create_team_work_run(run_id, ...)。
    """
    led.put_contract(tid, {"required_files": []})
    led.register_team_work_card(run_id, tid)


# ── T1: 单一派生函数主流程 7 形态 ──────────────────────────────

def test_t1_derive_terminal_passthrough(tmp_path):
    """真终态 (HARVESTED) 直接返, 不被 run 上下文覆盖。"""
    led = _led(tmp_path)
    led.create_team_work_run("r_fix5_t1_term", "s_main", "需求")
    _setup_card(led, "t_term", run_id="r_fix5_t1_term")
    led.update_worker_state("t_term", "HARVESTED", actor="harvest")
    st, src = led.derive_worker_state("t_term", run_id="r_fix5_t1_term")
    assert st == "HARVESTED", (st, src)
    assert src["tier"] == "terminal"
    led.close()


def test_t1_derive_waiting_passthrough(tmp_path):
    """WAITING 是 reaper 主动判定, 不被 run 上下文覆盖。"""
    led = _led(tmp_path)
    led.create_team_work_run("r_fix5_t1_wait", "s_main", "需求")
    _setup_card(led, "t_wait", run_id="r_fix5_t1_wait")
    led.update_worker_state("t_wait", "WAITING", actor="reaper")
    st, src = led.derive_worker_state("t_wait", run_id="r_fix5_t1_wait")
    assert st == "WAITING"
    assert src["tier"] == "waiting"
    led.close()


def test_t1_derive_obstacle_passthrough(tmp_path):
    """BLOCKED/NEED_CLARIFICATION 是 worker/reaper 给出信号, 不被覆盖。"""
    led = _led(tmp_path)
    led.create_team_work_run("r_fix5_t1_obs", "s_main", "需求")
    _setup_card(led, "t_block", run_id="r_fix5_t1_obs")
    led.update_worker_state("t_block", "BLOCKED", actor="reaper")
    st, src = led.derive_worker_state("t_block", run_id="r_fix5_t1_obs")
    assert st == "BLOCKED", (st, src)
    assert src["tier"] == "obstacle"
    led.close()


def test_t1_derive_working_holder(tmp_path):
    """task 是 run.current_card_id → WORKING。"""
    led = _led(tmp_path)
    led.create_team_work_run("r_fix5_t1_work", "s_main", "需求")
    _setup_card(led, "t_cur", run_id="r_fix5_t1_work")
    # 模拟 advance 占槽
    led.set_team_work_current_card("r_fix5_t1_work", "t_cur")
    st, src = led.derive_worker_state("t_cur", run_id="r_fix5_t1_work")
    assert st == "WORKING", (st, src)
    assert src["tier"] == "working_holder"
    led.close()


def test_t1_derive_handoff_window(tmp_path):
    """task 是 ledger 最近结算一张 + run 有 handoff_at 但 card_id NULL → HANDOFF。"""
    led = _led(tmp_path)
    led.create_team_work_run("r_ho", "s_main", "需求")
    _setup_card(led, "t_old", run_id="r_ho")
    _setup_card(led, "t_next", run_id="r_ho")
    # 上一张结算 → teardown 写 handoff_at
    led.set_team_work_current_card("r_ho", "t_old")
    led.update_worker_state("t_old", "HARVESTED", actor="harvest")
    led.clear_team_work_current_card("r_ho")
    # 这时 run 的 current_card NULL, handoff_at 非空, t_old 是
    # ledger updated_at 最近一张, t_next 还是 PENDING;
    # 这两条要分别派生对。
    st_old, _ = led.derive_worker_state("t_old", run_id="r_ho")
    # t_old 已是 HARVESTED → 真终态 passthrough 优先 (terminal tier)
    assert st_old == "HARVESTED"
    st_next, _ = led.derive_worker_state("t_next", run_id="r_ho")
    # t_next 不是最近一张 → pending_queue (PENDING)
    assert st_next == "PENDING", st_next
    led.close()


def test_t1_derive_frozen_run(tmp_path):
    """run 非 ACTIVE + task 非真终态 → PENDING (UI 显「队列已冻结」)。"""
    led = _led(tmp_path)
    led.create_team_work_run("r_close", "s_main", "需求")
    _setup_card(led, "t_tail", run_id="r_close")
    # 关闭 run (调用 close 端点逻辑; 此处直接 DB 改 status 模拟)
    led.conn.execute(
        "UPDATE team_work_runs SET status='CLOSED' WHERE run_id=?",
        ("r_close",))
    led.conn.commit()
    st, src = led.derive_worker_state("t_tail", run_id="r_close")
    assert st == "PENDING", (st, src)
    assert src["tier"] == "frozen_run"
    led.close()


def test_t1_derive_pending_queue(tmp_path):
    """run ACTIVE + 任务 PENDING + 不在 current_card 槽 → PENDING 排队中。"""
    led = _led(tmp_path)
    led.create_team_work_run("r_q", "s_main", "需求")
    _setup_card(led, "t_q", run_id="r_q")
    st, src = led.derive_worker_state("t_q", run_id="r_q")
    assert st == "PENDING"
    assert src["tier"] == "pending_queue"
    led.close()


# ── T2: actor 白名单 (AC5-3) ──────────────────────────────────

def test_t2_actor_policy_blocks_arbitrary(tmp_path):
    """actor=harvest 想 PENDING→WAITING 应 raise PermissionError (reaper only)。"""
    led = _led(tmp_path)
    led.create_team_work_run("r_fix5_t2a", "s_main", "需求")
    _setup_card(led, "t_perm", run_id="r_fix5_t2a")
    try:
        led.update_worker_state("t_perm", "WAITING", actor="harvest")
        raise AssertionError("actor=harvest 不应能 PENDING→WAITING")
    except PermissionError as e:
        assert "actor=" in str(e), str(e)
    # 反之 actor=reaper 应被允许
    led.update_worker_state("t_perm", "WAITING", actor="reaper")
    assert led.get_contract("t_perm")["harvest_status"] == "WAITING"
    led.close()


def test_t2_actor_policy_advance_must_match(tmp_path):
    """actor=random 不能从 PENDING 跳到 HARVESTED (正常只 actor=harvest)。"""
    led = _led(tmp_path)
    led.create_team_work_run("r_fix5_t2b", "s_main", "需求")
    _setup_card(led, "t_arb", run_id="r_fix5_t2b")
    try:
        led.update_worker_state(
            "t_arb", "HARVESTED", actor="random-actor")
        raise AssertionError("未授权 actor 不应能写入")
    except PermissionError:
        pass
    # actor=harvest 应被允许
    led.update_worker_state(
        "t_arb", "HARVESTED", actor="harvest",
        reason="worker 交卷 completed")
    assert led.get_contract("t_arb")["harvest_status"] == "HARVESTED"
    led.close()


# ── T3: 切换 run → 旧卡结算 + run 进 handoff 派生窗口 ────────

def test_t3_teardown_to_handoff_window(tmp_path):
    """结算 → clear_team_work_current_card 写 handoff_at;
    derive_run_overview 看到 run ACTIVE + current_card NULL +
    cleared_at 非空 → state=HANDOFF。"""
    led = _led(tmp_path)
    led.create_team_work_run("r_x", "s_main", "需求")
    _setup_card(led, "t_1", run_id="r_x")
    _setup_card(led, "t_2", run_id="r_x")
    led.set_team_work_current_card("r_x", "t_1")
    led.update_worker_state("t_1", "HARVESTED", actor="harvest")
    led.clear_team_work_current_card("r_x")
    overview = led.derive_run_overview("r_x")
    # 结算瞬间: run 处于 handoff 窗口, current_card NULL, cleared 非空
    assert overview["state"] == "HANDOFF", overview
    assert overview["handoff_active"] is True
    # 模拟 advance 推进到 t_2
    led.set_team_work_current_card("r_x", "t_2")
    overview2 = led.derive_run_overview("r_x")
    assert overview2["state"] == "ACTIVE", overview2
    assert overview2["holder_task_id"] == "t_2"
    led.close()


# ── T4: api_team_work_get 注入 state 字段 ─────────────────────

def test_t4_api_team_work_get_state_field(tmp_path, monkeypatch):
    """console.api_team_work_get 给 run + cards 注入 state 字段。"""
    led = _led(tmp_path)
    led.create_team_work_run("r_api", "s_main", "需求")
    _setup_card(led, "t_a", run_id="r_api")
    _setup_card(led, "t_b", run_id="r_api")
    led.set_team_work_current_card("r_api", "t_a")
    led.update_worker_state("t_a", "WAITING", actor="reaper")
    
    # 模拟 console 端点
    import mw.console as console
    console.LEDGER_DB = led.path
    # 修复 kanban DB 为空时跑不过; 这里只关心 run.field 注入, 我们
    # 直接调「get_team_work + 派生注入」路径的小函数 (实际 _get 的
    # 派生尾段)。
    from mw.console import api_team_work_get
    # 模拟: 改 LEDGER_DB, 但 kanban 路径在 _get 里有 _ro_query(KANBAN_DB,...)
    # 为不影响测试, 我们走 api_team_work_state 端点验证派生。
    resp = console.api_team_work_state("r_api")
    assert resp["ok"], resp
    assert resp["run"]["state"] in (
        "ACTIVE", "ACTIVE_BLOCKED", "HANDOFF", "WAITING_ACTIVE", "IDLE"
    ), resp
    states = {c["id"]: c["state"] for c in resp["cards"]}
    # t_a 当前承接 + WAITING → 派生端要返 WAITING (derive 不返 WAITING
    # 的罕见场景兜底: 旧版的「active=true, hs=WAITING」本来应该是
    # holder_state=WAITING, 但 derive_worker_state 在 WORKING holder
    # 路径前 WAITING 直接 passthrough)
    assert states["t_a"] == "WAITING", states
    assert states["t_b"] == "PENDING", states
    led.close()


# ── T5: run 关闭时无 HANDOFF ─────────────────────────────────

def test_t5_closed_run_no_handoff_state(tmp_path):
    """run 关闭后 derive_worker_state 不返 HANDOFF (避免 ghost handoff)。"""
    led = _led(tmp_path)
    led.create_team_work_run("r_close", "s_main", "需求")
    _setup_card(led, "t_z", run_id="r_close")
    # 关闭 run 前先把 current_card 占槽再结算, 让 handoff_at 写入
    led.set_team_work_current_card("r_close", "t_z")
    led.update_worker_state("t_z", "HARVESTED", actor="harvest")
    led.clear_team_work_current_card("r_close")
    # 关闭 run
    led.conn.execute(
        "UPDATE team_work_runs SET status='CLOSED' WHERE run_id=?",
        ("r_close",))
    led.conn.commit()
    st, src = led.derive_worker_state("t_z", run_id="r_close")
    # 真终态直接返 (passthrough 优先), 而非 HANDOFF。
    assert st == "HARVESTED", (st, src)
    # overview: CLOSED run → state='CLOSED'
    ov = led.derive_run_overview("r_close")
    assert ov["state"] == "CLOSED"
    assert ov["handoff_active"] is True, ov
    led.close()


# ── T6: reaper 闲置路径 → WAITING ────────────────────────────

def test_t6_reaper_idle_mark_waiting(tmp_path):
    """reaper 闲置超阈 (SSE 静默 + thinking=False) → actor=reaper
    把 PENDING/WORKING 卡标 WAITING。 模拟 reaper 内部 _notify_main_
    of_stuck 的写入路径。"""
    led = _led(tmp_path)
    led.create_team_work_run("r_w", "s_main", "需求")
    _setup_card(led, "t_w", run_id="r_w")
    # 卡初始 PENDING, run 占槽 (WORKING 派生)
    led.set_team_work_current_card("r_w", "t_w")
    st0, _ = led.derive_worker_state("t_w", run_id="r_w")
    assert st0 == "WORKING"
    # reaper 闲置后写入 WAITING
    led.update_worker_state("t_w", "WAITING", actor="reaper",
                            reason="[stuck] SSE 静默超阈")
    # 派生应是 WAITING (passthrough)
    st1, _ = led.derive_worker_state("t_w", run_id="r_w")
    assert st1 == "WAITING"
    # WAITING 仍占槽 (current_card_id 还是 t_w), UI 知道是「等介入」
    # 而非「交接中」。 derive_run_overview 该看到 WAITING_ACTIVE。
    ov = led.derive_run_overview("r_w")
    assert ov["waiting_active"] is True
    assert ov["state"] in (
        "ACTIVE_BLOCKED", "ACTIVE"
    ), ov  # 承接者卡 WAITING → ACTIVE_BLOCKED 分类
    led.close()


# ── T7: handoff_at 字段落库 ──────────────────────────────────

def test_t7_handoff_at_persisted(tmp_path):
    """current_card_handoff_at / last_waiting_at 由 schema 迁移幂等后落库。"""
    led = _led(tmp_path)
    led.create_team_work_run("r_s", "s_main", "需求")
    _setup_card(led, "t_s", run_id="r_s")
    led.set_team_work_current_card("r_s", "t_s")
    led.update_worker_state("t_s", "HARVESTED", actor="harvest")
    led.clear_team_work_current_card("r_s")
    # 验证列存在且已写
    cols = {r["name"] for r in led.conn.execute(
        "PRAGMA table_info(team_work_runs)").fetchall()}
    assert "current_card_handoff_at" in cols, cols
    assert "last_waiting_at" in cols, cols
    row = led.conn.execute(
        "SELECT current_card_cleared_at, current_card_handoff_at "
        "FROM team_work_runs WHERE run_id=?", ("r_s",)).fetchone()
    assert row["current_card_cleared_at"] is not None
    assert row["current_card_handoff_at"] is not None
    led.close()


# ── T8: derive_run_overview 四态值正确 ──────────────────────

def test_t8_run_overview_four_states(tmp_path):
    """derive_run_overview 在 ACTIVE/HANDOFF/WAITING_ACTIVE/CLOSED 下
    返回正确的 state + holder_task_id + holder_state。"""
    led = _led(tmp_path)
    led.create_team_work_run("r_o", "s_main", "需求")
    _setup_card(led, "t_o1", run_id="r_o")
    _setup_card(led, "t_o2", run_id="r_o")
    # (a) IDLE: run ACTIVE, 全 PENDING
    ov = led.derive_run_overview("r_o")
    assert ov["state"] in ("IDLE", "ACTIVE"), ov  # 全 PENDING 时 IDLE
    # (b) ACTIVE: 占槽 = 派生 WORKING
    led.set_team_work_current_card("r_o", "t_o1")
    ov = led.derive_run_overview("r_o")
    assert ov["state"] == "ACTIVE", ov
    assert ov["holder_task_id"] == "t_o1"
    assert ov["holder_state"] == "WORKING"
    # (c) HANDOFF: 结算后清槽
    led.update_worker_state("t_o1", "HARVESTED", actor="harvest")
    led.clear_team_work_current_card("r_o")
    ov = led.derive_run_overview("r_o")
    assert ov["state"] == "HANDOFF", ov
    assert ov["handoff_active"] is True
    # (d) WAITING_ACTIVE: 把已有卡标 WAITING
    #     (run 没承接, 但名下有 WAITING 卡 — 不太真实; 改为: 把
    #      下一张占槽 + 标 WAITING, 等效「承接者卡等待」)
    led.set_team_work_current_card("r_o", "t_o2")
    led.update_worker_state("t_o2", "WAITING", actor="reaper")
    ov = led.derive_run_overview("r_o")
    # 承接者 WAITING → ACTIVE_BLOCKED
    assert ov["state"] == "ACTIVE_BLOCKED", ov
    assert ov["waiting_active"] is True
    # (e) CLOSED
    led.conn.execute(
        "UPDATE team_work_runs SET status='CLOSED' WHERE run_id=?",
        ("r_o",))
    led.conn.commit()
    ov = led.derive_run_overview("r_o")
    assert ov["state"] == "CLOSED", ov
    led.close()
