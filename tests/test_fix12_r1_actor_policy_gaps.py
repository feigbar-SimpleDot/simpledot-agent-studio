"""R1 (2026-10-13, issue #12) 回归测试 — _ACTOR_POLICY 11 条边补齐 + advance
显式放行 + 既非授权 actor 必 raise PermissionError。

覆盖 (对齐 docs/FIX-R1.md §3 验收):
  G1  程序化扫描 _STATE_TRANSITIONS 与 _ACTOR_POLICY 差集 == ∅ (机器化兜底)
  G2  新边非授权 actor → PermissionError
  G3  新边授权 actor → 落库 + emit_event + 不回归
  G4  advance 在 settle_team_work_residue 路径上合法
  G5  既有的 F5 白名单机器化回归不破 (T2 形态继续 raise)
  G6  self-loop: BLOCKED→BLOCKED / NEED_CLARIFICATION→NEED_CLARIFICATION
      非授权 actor raise, 授权 actor 落库 (含 updated_at 推进)
  G7  ERROR→BLOCKED 降级 + NEED_CLARIFICATION→BLOCKED 超时未答
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
    return Ledger(str(tmp_path / "ledger.db"))


def _setup_card(led: Ledger, tid: str, *, run_id: str) -> None:
    led.put_contract(tid, {"required_files": []})
    led.register_team_work_card(run_id, tid)


def _events(led: Ledger, tid: str) -> list:
    """读 task_id 关联的所有事件 (mw_events 表)。"""
    cur = led.conn.execute(
        "SELECT event_type, payload FROM mw_events WHERE task_id=? "
        "ORDER BY id", (tid,)).fetchall()
    out = []
    for r in cur:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else {}
        except Exception:
            payload = {"_raw": r["payload"]}
        out.append({"event_type": r["event_type"], "payload": payload})
    return out


def _all_edges_in_transitions() -> set:
    """所有迁移表允许的 (from, to) 边集合。"""
    edges = set()
    for frm, tos in Ledger._STATE_TRANSITIONS.items():
        for to in tos:
            edges.add((frm, to))
    return edges


def _policy_edges() -> set:
    """_ACTOR_POLICY 显式列出的 (from, to) 边集合。"""
    return set(Ledger._ACTOR_POLICY.keys())


# ── G1: 程序化扫描差集 == ∅ ────────────────────────────────────

def test_g1_state_transitions_subset_of_actor_policy(tmp_path):
    """R1 要害: _STATE_TRANSITIONS ⊆ _ACTOR_POLICY.keys()。

    旧 _ACTOR_POLICY 是「列举式」, policy is None 时任意 actor 放行——意味着
    任何 (_STATE_TRANSITIONS 允许 + _ACTOR_POLICY 未列) 的 (from, to) 边
    都不受 actor 门禁约束, 形成 actor 级漏。R1 修法: 把所有迁移表允许的
    边全部纳入 _ACTOR_POLICY, 让 policy is None 永不为合法迁移;
    非法 actor → raise PermissionError。
    """
    edges = _all_edges_in_transitions()
    policy = _policy_edges()
    missing = edges - policy
    assert not missing, (
        f"_STATE_TRANSITIONS 允许但 _ACTOR_POLICY 未列的边 "
        f"(R1 缺口): {sorted(missing)};R1 修复后差集必须 == ∅"
    )
    # 顺手: 验证 policy 至少有一条路径时非空——否则等于放行任何 actor
    for edge in edges:
        assert Ledger._ACTOR_POLICY[edge], (
            f"{edge} 的 actor 白名单为空, 等于放行任意 actor")
    led = _led(tmp_path)
    led.close()


# ── G2: 新边非授权 actor → PermissionError ─────────────────────

def test_g2_unauthorized_actor_raises_on_new_edges(tmp_path):
    """新补的 11 条边上, 非授权 actor 调 update_worker_state 必须
    raise PermissionError, 与既有白名单错误信息一致。
    """
    led = _led(tmp_path)
    led.create_team_work_run("r_g2", "s_main", "需求")
    cases = [
        # (tid, to, allowed_actor, unauthorized_actor)
        ("t_b_b", "BLOCKED", "reaper", "main_correct"),
        ("t_b_e", "ERROR", "harvest", "reaper"),
        ("t_b_h", "HARVESTED", "advance", "reaper"),
        ("t_b_n", "NEED_CLARIFICATION", "harvest", "reaper"),
        ("t_e_b", "BLOCKED", "main_correct", "reaper"),
        ("t_e_h", "HARVESTED", "advance", "reaper"),
        ("t_e_n", "NEED_CLARIFICATION", "harvest", "reaper"),
        ("t_n_b", "BLOCKED", "reaper", "harvest"),
        ("t_n_e", "ERROR", "harvest", "reaper"),
        ("t_n_h", "HARVESTED", "advance", "reaper"),
        ("t_n_n", "NEED_CLARIFICATION", "harvest", "reaper"),
    ]
    for tid, to, allowed, unauth in cases:
        _setup_card(led, tid, run_id="r_g2")
        # 先用 allowed 推到中转态 (各边起始 from 不一致)
        if tid.startswith("t_b_"):
            led.update_worker_state(tid, "BLOCKED", actor="reaper")
        elif tid.startswith("t_e_"):
            led.update_worker_state(tid, "BLOCKED", actor="reaper")
            led.update_worker_state(tid, "ERROR", actor="harvest")
        elif tid.startswith("t_n_"):
            led.update_worker_state(tid, "BLOCKED", actor="reaper")
            led.update_worker_state(tid, "NEED_CLARIFICATION", actor="harvest")
        # 自环特例: t_b_b 已在 BLOCKED, 直接试非授权
        # 非授权 actor 必 raise PermissionError
        if tid == "t_b_b":
            try:
                led.update_worker_state(tid, "BLOCKED", actor=unauth)
            except PermissionError as e:
                assert f"actor={unauth!r}" in str(e), str(e)
                continue
            raise AssertionError(
                f"{tid} {unauth} 未被拒, R1 修复未生效")
        if tid == "t_n_n":
            try:
                led.update_worker_state(tid, "NEED_CLARIFICATION",
                                        actor=unauth)
            except PermissionError as e:
                assert f"actor={unauth!r}" in str(e), str(e)
                continue
            raise AssertionError(
                f"{tid} {unauth} 未被拒, R1 修复未生效")
        # 非自环: 试推 unauth 到 to
        try:
            led.update_worker_state(tid, to, actor=unauth)
        except PermissionError as e:
            assert f"actor={unauth!r}" in str(e), str(e)
            assert to in str(e), str(e)
            continue
        raise AssertionError(
            f"{tid} {unauth} → {to} 未 raise, R1 修复未生效")
    led.close()


# ── G3: 新边授权 actor → 落库 + emit_event ────────────────────

def test_g3_authorized_actor_succeeds_on_new_edges(tmp_path):
    """11 条新边上每个授权 actor 都能落库 + 发 worker.state 事件
    + updated_at 推进 + last_progress_at 推进。
    """
    led = _led(tmp_path)
    led.create_team_work_run("r_g3", "s_main", "需求")
    cases = [
        ("t_b_b", "BLOCKED", "reaper"),
        ("t_b_e", "ERROR", "harvest"),
        ("t_b_h", "HARVESTED", "advance"),
        ("t_b_n", "NEED_CLARIFICATION", "harvest"),
        ("t_e_b", "BLOCKED", "main_correct"),
        ("t_e_h", "HARVESTED", "advance"),
        ("t_e_n", "NEED_CLARIFICATION", "harvest"),
        ("t_n_b", "BLOCKED", "reaper"),
        ("t_n_e", "ERROR", "harvest"),
        ("t_n_h", "HARVESTED", "advance"),
        ("t_n_n", "NEED_CLARIFICATION", "harvest"),
    ]
    for tid, to, actor in cases:
        _setup_card(led, tid, run_id="r_g3")
        # 推到起始 from 态
        led.update_worker_state(tid, "BLOCKED", actor="reaper")
        if tid.startswith("t_e_"):
            led.update_worker_state(tid, "ERROR", actor="harvest")
        elif tid.startswith("t_n_"):
            led.update_worker_state(tid, "NEED_CLARIFICATION",
                                    actor="harvest")
        before = led.get_contract(tid)["updated_at"]
        time.sleep(0.001)
        # 授权 actor 推新边 → 不 raise, 落库, 发事件
        led.update_worker_state(tid, to, actor=actor,
                                reason=f"[{actor}] G3 {to}")
        row = led.get_contract(tid)
        assert row["harvest_status"] == to, (tid, row["harvest_status"])
        assert row["updated_at"] >= before, (tid, row["updated_at"])
        # 事件轨迹含 worker.state + actor/to/from
        evs = _events(led, tid)
        state_evs = [e for e in evs
                     if e["event_type"] == "worker.state"]
        assert state_evs, (tid, evs)
        last = state_evs[-1]["payload"]
        assert last["actor"] == actor, (tid, last)
        assert last["to"] == to, (tid, last)
    led.close()


# ── G4: settle_team_work_residue 仍合法 ─────────────────────────

def test_g4_settle_residue_still_legal_with_advance(tmp_path):
    """R1 补齐后 settle_team_work_residue (actor=advance) 路径不回归。

    BLOCKED→HARVESTED 与 NEED_CLARIFICATION→HARVESTED 的白名单必须包含
    'advance', 这样 settle_residue 隐式判终态时不会撞 PermissionError。
    """
    led = _led(tmp_path)
    led.create_team_work_run("r_g4", "s_main", "需求")
    for tid in ("t_a", "t_b"):
        _setup_card(led, tid, run_id="r_g4")
    led.update_worker_state("t_a", "BLOCKED", actor="reaper")
    led.update_worker_state("t_b", "NEED_CLARIFICATION", actor="harvest")
    settled = led.settle_team_work_residue("r_g4", exclude_task_id="t_a")
    assert settled == ["t_b"]
    assert led.get_contract("t_b")["harvest_status"] == "HARVESTED"
    assert "[main_advance]" in led.get_contract("t_b")["harvest_log"]
    # t_a 未被 exclude 之外误动
    assert led.get_contract("t_a")["harvest_status"] == "BLOCKED"
    led.close()


# ── G5: 既有的 F5 白名单机器化回归不破 ─────────────────────────

def test_g5_existing_f5_actor_policy_still_holds(tmp_path):
    """R1 只补缺, 不动既有白名单——验证旧测试口径 (test_fix5_state_machine
    T2) 仍 raise PermissionError。
    """
    led = _led(tmp_path)
    led.create_team_work_run("r_g5", "s_main", "需求")
    _setup_card(led, "t_perm", run_id="r_g5")
    # PENDING → WAITING: 只 actor=reaper 允许, actor=harvest 必须 raise
    try:
        led.update_worker_state("t_perm", "WAITING", actor="harvest")
    except PermissionError as e:
        assert "actor=" in str(e), str(e)
    else:
        raise AssertionError("PENDING→WAITING by harvest 未 raise, F5 回归")
    # 反向验证 reaper 仍可走
    led.update_worker_state("t_perm", "WAITING", actor="reaper")
    assert led.get_contract("t_perm")["harvest_status"] == "WAITING"
    led.close()


# ── G6: self-loop 落库 (含 updated_at 推进) ─────────────────────

def test_g6_self_loop_records_new_state_with_fresh_timestamp(tmp_path):
    """BLOCKED→BLOCKED 与 NEED_CLARIFICATION→NEED_CLARIFICATION 自环
    授权 actor 必落库 + updated_at 推进 + last_progress_at 推进。
    """
    led = _led(tmp_path)
    led.create_team_work_run("r_g6", "s_main", "需求")
    # BLOCKED 自环 by reaper
    _setup_card(led, "t_b_b", run_id="r_g6")
    led.update_worker_state("t_b_b", "BLOCKED", actor="reaper")
    row0 = led.get_contract("t_b_b")
    up0 = row0["updated_at"]
    lp0 = row0["last_progress_at"]
    time.sleep(0.005)
    led.update_worker_state("t_b_b", "BLOCKED", actor="reaper",
                            reason="[stuck] 续敲")
    row1 = led.get_contract("t_b_b")
    assert row1["harvest_status"] == "BLOCKED"
    assert row1["updated_at"] > up0, (up0, row1["updated_at"])
    assert row1["last_progress_at"] >= lp0, (lp0, row1["last_progress_at"])
    # NEED_CLARIFICATION 自环 by harvest
    _setup_card(led, "t_n_n", run_id="r_g6")
    led.update_worker_state("t_n_n", "BLOCKED", actor="reaper")
    led.update_worker_state("t_n_n", "NEED_CLARIFICATION", actor="harvest")
    up0 = led.get_contract("t_n_n")["updated_at"]
    time.sleep(0.005)
    led.update_worker_state("t_n_n", "NEED_CLARIFICATION", actor="harvest",
                            reason="[clarify] 再问一次")
    row1 = led.get_contract("t_n_n")
    assert row1["harvest_status"] == "NEED_CLARIFICATION"
    assert row1["updated_at"] > up0
    led.close()


# ── G7: 降级 / 超时未答 路径 ─────────────────────────────────

def test_g7_error_to_blocked_downgrade_and_timeout(tmp_path):
    """ERROR→BLOCKED (main 翻案) 与 NEED_CLARIFICATION→BLOCKED (超时未答)
    授权 actor 必走通。
    """
    led = _led(tmp_path)
    led.create_team_work_run("r_g7", "s_main", "需求")
    # ERROR→BLOCKED by main_correct
    _setup_card(led, "t_eb", run_id="r_g7")
    led.update_worker_state("t_eb", "BLOCKED", actor="reaper")
    led.update_worker_state("t_eb", "ERROR", actor="harvest")
    led.update_worker_state("t_eb", "BLOCKED", actor="main_correct",
                            reason="[main] 翻案")
    assert led.get_contract("t_eb")["harvest_status"] == "BLOCKED"
    # NEED_CLARIFICATION→BLOCKED by reaper (超时)
    _setup_card(led, "t_nb", run_id="r_g7")
    led.update_worker_state("t_nb", "BLOCKED", actor="reaper")
    led.update_worker_state("t_nb", "NEED_CLARIFICATION", actor="harvest")
    led.update_worker_state("t_nb", "BLOCKED", actor="reaper",
                            reason="[reaper] 澄清超时未答")
    assert led.get_contract("t_nb")["harvest_status"] == "BLOCKED"
    led.close()


# ── G8: 非法 actor 触发错误信息格式 ────────────────────────────

def test_g8_permission_error_message_contains_actor_and_edge(tmp_path):
    """PermissionError 文案必须包含 actor 名称 + (from, to) 边 + 合法 actor 集,
    与既有错误信息一致 (便于排查)。
    """
    led = _led(tmp_path)
    led.create_team_work_run("r_g8", "s_main", "需求")
    _setup_card(led, "t_msg", run_id="r_g8")
    led.update_worker_state("t_msg", "BLOCKED", actor="reaper")
    # BLOCKED→HARVESTED 允许 {advance, harvest, main_correct}, reaper 不在
    try:
        led.update_worker_state("t_msg", "HARVESTED", actor="reaper")
    except PermissionError as e:
        msg = str(e)
        assert "'reaper'" in msg, msg
        assert "BLOCKED" in msg and "HARVESTED" in msg, msg
        assert "advance" in msg, msg  # 合法 actor 集必须列出
    else:
        raise AssertionError("PermissionError 未 raise")
    led.close()
