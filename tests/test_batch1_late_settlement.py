"""批次1 (#6) 迟到结算与收尸活体检查回归测试 — BATCH1-DESIGN §4 T1-T10.

覆盖:
  T1  working + 超时 + 活体 thinking=True → 不收尸, 滑宽限
  T2  working + 超时 + 活体不可得 → fail-open 照收 (现行为不回退)
  T3  working + 活体 True 但总时长超 hard_cap → 收尸, note 含 hard_cap
  T4  BLOCKED(timeout:*) + TODO.md 已 completed → 补结算 HARVESTED
  T5  T4 后再跑一轮 → 幂等
  T6  BLOCKED(failed:*) worker 自报 → 不进补结算扫描集
  T7  BLOCKED(timeout) + run 已 CLOSED → 陈旧保护不补
  T8  BLOCKED(timeout) + TODO.md 仍 working → 不补
  T9  补结算产出的 COMPLETE 经三态中继 → main 收到转发
  T10 补结算不触碰 current_card_id (串行铁律)

纯函数测试, 不依赖 hermes CLI / 网络: 活体查询 patch 掉,
harvest 走真实实现 (required_files=[] 零物理依赖)。
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mw.ledger import Ledger  # noqa: E402
from mw.todo import (  # noqa: E402
    init_todo, TodoTask,
    STATUS_COMPLETED, STATUS_WORKING,
)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_transcript_export():
    """容器无 hermes CLI: 屏蔽 transcript 导出路径 (与既有
    test_harvest_b_rewrite_guard 的既有环境排除项同理, patch 掉让
    harvest 走纯状态/物理路径, 不 shell out)。"""
    with patch("mw.harvest.export_transcript", return_value=None), \
         patch("mw.harvest.fetch_transcript_api", return_value=None):
        yield


def _setup(tmp_path, task_id="t_b1", timeout=60):
    """建 workspace + ledger + 契约 + session (带 session_id 供活体查询)。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    ledger = Ledger(str(tmp_path / "ledger.db"))
    ledger.put_contract(task_id, {"required_files": [],
                                 "timeout_seconds": timeout})
    ledger.upsert_session(task_id, f"sess-{task_id}", "coder", "api",
                         None, workspace=str(ws))
    return task_id, ws, ledger


def _make_todo(ws: Path, status: str):
    task = TodoTask(step_id="s", title="T", goal="g", assignee="coder",
                   product_workspace=str(ws), status=status)
    init_todo(ws / "TODO.md", "T", task)
    return ws / "TODO.md"


def _row(ledger, task_id):
    return dict(ledger.conn.execute(
        "SELECT * FROM harvest_contracts WHERE task_id=?",
        (task_id,)).fetchone())


def _reap(tmp_path, ledger, task_id, liveness=None):
    """跑一次 _reap_one_task, liveness 为 _query_console_liveness 的返回值。"""
    from mw.reaper import _reap_one_task
    results = []
    with patch("mw.reaper._query_console_liveness",
              return_value=liveness):
        _reap_one_task(_row(ledger, task_id), ledger, str(tmp_path),
                       str(tmp_path), 30, None, results)
    return results


# ── T1: 活体 thinking=True → 超时被抑制 ──────────────────────────

def test_t1_alive_thinking_suppresses_reap(tmp_path):
    task_id, ws, ledger = _setup(tmp_path)
    _make_todo(ws, STATUS_WORKING)
    marker = tmp_path / "spawn" / f"{task_id}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    # started 200s 前: 超 timeout+grace(90s) 但在 hard_cap(270s) 内
    marker.write_text(json.dumps({"started_at": time.time() - 200,
                                "workspace": str(ws)}))
    results = _reap(tmp_path, ledger, task_id,
                    liveness={"thinking": True, "last_event_ts": 0})
    assert results[0]["trigger"] == "timeout_suppressed_alive", results
    assert results[0]["verdict"] == "WAIT"
    assert ledger.get_contract(task_id)["harvest_status"] == "PENDING"
    # 滑宽限: last_progress_at 被 bump
    assert ledger.get_contract(task_id)["last_progress_at"] is not None
    ledger.close()


# ── T2: 活体不可得 → fail-open 照收 ─────────────────────────────

def test_t2_failopen_reaps_when_liveness_unknown(tmp_path):
    task_id, ws, ledger = _setup(tmp_path)
    _make_todo(ws, STATUS_WORKING)
    marker = tmp_path / "spawn" / f"{task_id}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"started_at": time.time() - 7200,
                                "workspace": str(ws)}))
    results = _reap(tmp_path, ledger, task_id, liveness=None)
    assert results[0]["trigger"] == "timeout", results
    assert results[0]["verdict"] == "FAILED"
    hc = ledger.get_contract(task_id)
    assert hc["harvest_status"] == "BLOCKED"
    assert (hc["harvest_log"] or "").startswith("timeout:"), hc["harvest_log"]
    ledger.close()


# ── T3: 活体 True 但超 hard_cap → 仍收 ──────────────────────────

def test_t3_hard_cap_still_reaps(tmp_path):
    # timeout=60, grace=30 → hard_cap=270s; started 10h 前 → 超硬顶
    task_id, ws, ledger = _setup(tmp_path)
    _make_todo(ws, STATUS_WORKING)
    marker = tmp_path / "spawn" / f"{task_id}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"started_at": time.time() - 36000,
                                "workspace": str(ws)}))
    results = _reap(tmp_path, ledger, task_id,
                    liveness={"thinking": True, "last_event_ts": time.time()})
    assert results[0]["trigger"] == "timeout", results
    assert "hard_cap" in results[0]["note"], results[0]
    assert ledger.get_contract(task_id)["harvest_status"] == "BLOCKED"
    ledger.close()


# ── T4: BLOCKED(timeout:*) + TODO completed → 补结算 ─────────────

def _seed_blocked_timeout(ledger, task_id, log_prefix="timeout"):
    ledger.conn.execute(
        "UPDATE harvest_contracts SET harvest_status='BLOCKED', "
        "harvest_log=?, updated_at=? WHERE task_id=?",
        (f"{log_prefix}: exit_code=124 平台收尸", time.time() - 600,
         task_id))
    ledger.conn.commit()


def test_t4_late_completed_settles(tmp_path):
    from mw.reaper import _reap_late_completions
    task_id, ws, ledger = _setup(tmp_path)
    _seed_blocked_timeout(ledger, task_id)
    # 先落一份陈旧 BLOCKED 报告, 并"确认送达"过
    ledger.set_worker_report(task_id, {"verdict": "FAILED",
                                      "why": "超时收尸"})
    ledger.conn.execute(
        "UPDATE harvest_contracts SET notify_confirmed_at=? "
        "WHERE task_id=?", (time.time(), task_id))
    ledger.conn.commit()
    # worker 后交卷: TODO.md 转 completed
    _make_todo(ws, STATUS_COMPLETED)

    results = []
    _reap_late_completions(ledger, str(tmp_path), str(tmp_path), None,
                          results, None)
    assert results and results[0]["trigger"] == "late_settlement", results
    assert results[0]["verdict"] == "COMPLETE"
    hc = ledger.get_contract(task_id)
    assert hc["harvest_status"] == "HARVESTED"
    rep = json.loads(hc["worker_report"])
    assert rep["verdict"] == "COMPLETE"  # 报告被替换
    assert hc["notify_confirmed_at"] is None  # 清确认 → 可重投
    ledger.close()


# ── T5: 幂等 ────────────────────────────────────────────────────

def test_t5_idempotent(tmp_path):
    from mw.reaper import _reap_late_completions
    task_id, ws, ledger = _setup(tmp_path)
    _seed_blocked_timeout(ledger, task_id)
    _make_todo(ws, STATUS_COMPLETED)
    results1 = []
    _reap_late_completions(ledger, str(tmp_path), str(tmp_path), None,
                          results1, None)
    assert len(results1) == 1
    results2 = []
    _reap_late_completions(ledger, str(tmp_path), str(tmp_path), None,
                          results2, None)
    assert results2 == []  # 已离开扫描集
    assert ledger.get_contract(task_id)["harvest_status"] == "HARVESTED"
    ledger.close()


# ── T6: failed: 前缀不进扫描集 ──────────────────────────────────

def test_t6_failed_prefix_excluded(tmp_path):
    from mw.reaper import _reap_late_completions
    task_id, ws, ledger = _setup(tmp_path)
    _seed_blocked_timeout(ledger, task_id, log_prefix="failed")
    _make_todo(ws, STATUS_COMPLETED)
    results = []
    _reap_late_completions(ledger, str(tmp_path), str(tmp_path), None,
                          results, None)
    assert results == []
    assert ledger.get_contract(task_id)["harvest_status"] == "BLOCKED"
    ledger.close()


# ── T7: run 已 CLOSED 不补 (F7 陈旧保护) ────────────────────────

def test_t7_closed_run_not_settled(tmp_path):
    from mw.reaper import _reap_late_completions
    task_id, ws, ledger = _setup(tmp_path)
    ledger.create_team_work_run("r_closed", "s_main", "需求")
    ledger.register_team_work_card("r_closed", task_id)
    _seed_blocked_timeout(ledger, task_id)
    _make_todo(ws, STATUS_COMPLETED)
    ledger.conn.execute(
        "UPDATE team_work_runs SET status='CLOSED' WHERE run_id='r_closed'")
    ledger.conn.commit()
    results = []
    _reap_late_completions(ledger, str(tmp_path), str(tmp_path), None,
                          results, None)
    assert results == []
    assert ledger.get_contract(task_id)["harvest_status"] == "BLOCKED"
    ledger.close()


# ── T8: TODO 仍 working 不补 ────────────────────────────────────

def test_t8_still_working_not_settled(tmp_path):
    from mw.reaper import _reap_late_completions
    task_id, ws, ledger = _setup(tmp_path)
    _seed_blocked_timeout(ledger, task_id)
    _make_todo(ws, STATUS_WORKING)
    results = []
    _reap_late_completions(ledger, str(tmp_path), str(tmp_path), None,
                          results, None)
    assert results == []
    assert ledger.get_contract(task_id)["harvest_status"] == "BLOCKED"
    ledger.close()


# ── T9: 补结算报告经三态中继送达 main ───────────────────────────

def test_t9_relay_dispatches_late_complete(tmp_path):
    from mw.reaper import _reap_late_completions, _notify_main_of_reports
    task_id, ws, ledger = _setup(tmp_path)
    ledger.record_initiator(task_id, "s_main")
    _seed_blocked_timeout(ledger, task_id)
    _make_todo(ws, STATUS_COMPLETED)
    results = []
    _reap_late_completions(ledger, str(tmp_path), str(tmp_path), None,
                          results, None)
    assert results and results[0]["verdict"] == "COMPLETE"
    sent = []
    with patch("mw.reaper._send_to_main",
              side_effect=lambda sid, msg, gw, key, db, **kw:
              (sent.append((sid, msg)), True)[1]):
        _notify_main_of_reports(ledger, results, registry=None)
    assert sent, "补结算的 COMPLETE 报告应经中继转发"
    assert sent[0][0] == "s_main"
    assert "COMPLETE" in sent[0][1]
    ledger.close()


# ── T10: 补结算不触碰 current_card_id ───────────────────────────

def test_t10_current_card_untouched(tmp_path):
    from mw.reaper import _reap_late_completions
    task_id, ws, ledger = _setup(tmp_path, task_id="t_late")
    ledger.create_team_work_run("r1", "s_main", "需求")
    ledger.register_team_work_card("r1", task_id)
    ledger.register_team_work_card("r1", "t_cur")
    ledger.set_team_work_current_card("r1", "t_cur")
    _seed_blocked_timeout(ledger, task_id)
    _make_todo(ws, STATUS_COMPLETED)
    active_run = ledger.get_active_team_work()
    results = []
    _reap_late_completions(ledger, str(tmp_path), str(tmp_path), None,
                          results, active_run)
    assert results and results[0]["verdict"] == "COMPLETE"
    assert ledger.get_contract(task_id)["harvest_status"] == "HARVESTED"
    # 当前卡槽位仍是 t_cur (harvest teardown 清掉后被还原)
    assert ledger.get_team_work("r1")["current_card_id"] == "t_cur"
    ledger.close()
