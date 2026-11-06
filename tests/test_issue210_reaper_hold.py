"""tests/test_issue210_reaper_hold.py — #210: HOLD run 不被 reaper 任何扫描面命中。

PLAN-U §1.2.3: reaper 联动 = **零代码** (HOLD ∉ ACTIVE ⇒ 扫描/投递/敲门
自然全停)。本文件不测 reaper 新代码 (没有), 而是把「自然全停」钉成回归
断言 —— 若将来有人改坏 ACTIVE 门, 这里先红。

覆盖 (PLAN-U §1.5 / §1.1 扫描面清单):
  R1  get_active_team_work 不返回 HOLD run (reap_once 取数门)
  R2  reap_once: 仅 HOLD run (含 PENDING 当前卡) ⇒ reap_scope SKIP,
      卡不被结算 (§1.1 :2212-2243)
  R3  relay_precheck: HOLD run 名下卡 go=False reason=run_not_active:HOLD
      (§1.1 :1164-1180 投递门)
  R4  relay 扫描 SQL (reaper._notify_main_of_reports 的 JOIN 形状):
      HOLD run 名下 HARVESTED 未送达卡不在扫描结果
  R5  NULL 槽兜底敲门: HOLD run ⇒ active_run=None ⇒ 不敲 (无 knock 事件)
  R6  已收割未送达敲门: 同上, 不敲
  R7  迟到补结算面: reap_once 无 ACTIVE run ⇒ 不触碰 HOLD run 名下卡

跑法: pytest tests/test_issue210_reaper_hold.py -v
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mw.ledger import Ledger  # noqa: E402


@pytest.fixture()
def led(tmp_path, monkeypatch):
    monkeypatch.setenv("MW_HOME", str(tmp_path))
    led = Ledger(str(tmp_path / "ledger.db"))
    yield led
    led.close()


def _hold_run(led, run_id="r_hold", card="t_card"):
    """ACTIVE run + 一张 PENDING 当前卡, 然后置 HOLD。"""
    led.create_team_work_run(run_id, "s1", "需求")
    led.put_contract(card, {"required_files": [], "timeout_seconds": 60})
    led.register_team_work_card(run_id, card)
    led.set_team_work_current_card(run_id, card)
    assert led.hold_team_work(run_id, reason="测试冻结", actor="tester") == "HOLD"


def _event_types(led):
    return {r["event_type"] for r in led.conn.execute(
        "SELECT event_type FROM mw_events").fetchall()}


# ── R1: ACTIVE 取数门 ────────────────────────────────────────────────
def test_r1_get_active_team_work_excludes_hold(led):
    _hold_run(led)
    assert led.get_active_team_work() is None


# ── R2: reap_once 不扫 HOLD run ─────────────────────────────────────
def test_r2_reap_once_skips_hold_run(led, tmp_path):
    # reap_once 依赖链含 pydantic (schemas); 部分部署 venv 的 pydantic_core
    # 二进制损坏 (基线 test_team_work_reaper 同根因既有失败) ⇒ 环境性跳过,
    # 健康环境照常执行本断言。
    pytest.importorskip(
        "pydantic",
        reason="venv pydantic_core 损坏 (基线既有环境问题), 非 #210 回归",
        exc_type=ImportError)
    from mw import reaper
    _hold_run(led)
    results = reaper.reap_once(ledger=led, hermes_home=str(tmp_path),
                               base=str(tmp_path), grace_seconds=10,
                               registry={})
    scope_skips = [r for r in results
                   if r.get("trigger") == "reap_scope"
                   and r.get("verdict") == "SKIP"]
    assert len(scope_skips) == 1, f"期望 reap_scope SKIP: {results}"
    # 当前卡未被结算
    assert led.get_contract("t_card")["harvest_status"] == "PENDING"


# ── R3: relay 投递前二次核拦 HOLD ───────────────────────────────────
def test_r3_relay_precheck_blocks_hold(led):
    _hold_run(led)
    led.set_harvest("t_card", "HARVESTED", exit_code=0)
    led.set_worker_report("t_card", {"summary": "done"})
    pre = led.relay_precheck("t_card")
    assert pre["go"] is False
    assert pre["reason"] == "run_not_active:HOLD"


# ── R4: relay 扫描 JOIN 不含 HOLD run 的卡 ─────────────────────────
def test_r4_relay_scan_excludes_hold(led):
    """R4 修复 (U批 s4f, 缺陷见 docs/TEST-REPORT-U-20261104.md §5.1):
    旧版在测试内联抄了一份扫描 SQL, 生产 mw/reaper.py 的 JOIN 被改宽时
    守护不会红 (变异 M-210-D 实测未捕获)。现改为直接执行生产常量
    mw.reaper.RELAY_SCAN_SQL (_notify_main_of_reports 的扫描 SQL, 纯提取
    零行为变更) —— 生产 SQL 一变宽, 本断言必红。"""
    from mw import reaper
    _hold_run(led)
    led.set_harvest("t_card", "HARVESTED", exit_code=0)
    led.set_worker_report("t_card", {"summary": "done"})
    rows = led.conn.execute(reaper.RELAY_SCAN_SQL).fetchall()
    assert "t_card" not in [r["task_id"] for r in rows], (
        "HOLD run 名下 HARVESTED 未送达卡出现在 relay 扫描结果 —— "
        "生产 JOIN 的 ACTIVE 门被改宽: " + reaper.RELAY_SCAN_SQL)


def test_r4b_reaper_source_active_gate_text_guard():
    """R4 守护加固 (U批 s4f): 对 mw/reaper.py 源文件做文本断言 ——
    扫描面 ACTIVE 门必须保持 `twr.status = 'ACTIVE'` 形态, 不得出现
    `twr.status IN (...)` 放宽形态。与 test_r4 互补: 即使测试环境无法
    执行 SQL (如 venv 损坏), 变异 M-210-D 类改动仍会被本断言拦下。"""
    src = (ROOT / "mw" / "reaper.py").read_text(encoding="utf-8")
    # ACTIVE 门片段必须存在 (提取后由 TW_RUN_ACTIVE_GATE 常量持有,
    # 两处扫描共用)
    assert "AND (hc.team_work_run_id IS NULL OR twr.status = 'ACTIVE')" in src, (
        "mw/reaper.py 中找不到 relay/补结算扫描的 ACTIVE 门片段 "
        "AND (hc.team_work_run_id IS NULL OR twr.status = 'ACTIVE')")
    # 不得出现任何 twr.status IN (...) 放宽形态 (变异 M-210-D 的形态)
    widened = re.findall(r"twr\.status\s+IN\s*\(", src)
    assert not widened, (
        f"mw/reaper.py 出现 twr.status IN (...) 放宽形态 {widened} —— "
        "relay/补结算扫描的 ACTIVE 门被改宽, HOLD/终态 run 的卡会被误扫")


# ── R5: 敲门形状以 ACTIVE 为门 (R6 补档已随 #205 删除) ──────────────
def test_r5_null_slot_knock_not_for_hold(led):
    from mw import reaper
    _hold_run(led)
    # 敲门形状: 槽位清空 + 名下有未结算卡 (ACTIVE 时必敲的形状)
    led.clear_team_work_current_card("r_hold")
    results: list = []
    reaper._notify_null_slot(led, led.get_active_team_work(), results)
    assert "team_work.null_slot_knock" not in _event_types(led)


# ── R7: 迟到补结算面不触碰 HOLD run ────────────────────────────────
def test_r7_late_settlement_not_for_hold(led, tmp_path):
    pytest.importorskip(
        "pydantic",
        reason="venv pydantic_core 损坏 (基线既有环境问题), 非 #210 回归",
        exc_type=ImportError)
    from mw import reaper
    _hold_run(led)
    # BLOCKED 卡是迟到补结算面 (_reap_late_completions) 的扫描对象形状
    led.set_harvest("t_card", "BLOCKED", exit_code=None)
    results = reaper.reap_once(ledger=led, hermes_home=str(tmp_path),
                               base=str(tmp_path), grace_seconds=10,
                               registry={})
    assert led.get_contract("t_card")["harvest_status"] == "BLOCKED"
    touched = [r for r in results if r.get("task_id") == "t_card"]
    assert touched == [], f"HOLD run 的卡被 reap_once 触碰: {touched}"
