"""issue #65 (2026-10-19) reaper WAITING 全盲区修复回归锚 — C-1/C-2/C-3。

覆盖 (ANALYSIS-61-65.md #65 既有测试盘点指令):
  C1a WAITING→PENDING actor=resume 放行 (白名单扩集)
  C1b WAITING→PENDING 非法 actor (harvest) 仍拒
  C2a resume_task 对 PENDING 卡幂等不炸 (PENDING 无自环, 跳过迁移)
  C2b resume_task 对 WAITING 卡正常回拨 PENDING + state_reset=done
  C3a M3 _reap_late_completions 对 WAITING+TODO completed 卡补结算
  C3b M3 对 WAITING+TODO working 卡跳过 (worker 被唤醒仍在跑, 不误结算)
  C3c M3 WAITING 补结算幂等 (结算后离开扫描集)

纯函数测试, 不依赖 hermes CLI / 网络 (仿 test_t13 假 resp /
test_issue13 造 TODO 模式)。
"""
from __future__ import annotations
import json
import os
import sys
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
    """容器无 hermes CLI: 屏蔽 transcript 导出 (与 batch1 测试集同纪律)。"""
    with patch("mw.harvest.export_transcript", return_value=None), \
         patch("mw.harvest.fetch_transcript_api", return_value=None):
        yield


def _led(tmp_path):
    return Ledger(str(tmp_path / "ledger.db"))


class FakeResp:
    """假 resume API: 受理并回同 session id (仿 test_t13)。"""

    def __init__(self, sid):
        self.headers = {"X-Hermes-Session-Id": sid}

    def read(self):
        return json.dumps({"choices": [{"message":
                   {"content": "ok"}}]}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _make_todo(ws: Path, status: str):
    task = TodoTask(step_id="s", title="T", goal="g", assignee="coder",
                   product_workspace=str(ws), status=status)
    init_todo(ws / "TODO.md", "T", task)
    return ws / "TODO.md"


# ── C1a/C1b: 白名单 WAITING→PENDING 扩 resume ───────────────────

def test_c1a_waiting_to_pending_resume_allowed(tmp_path):
    led = _led(tmp_path)
    led.put_contract("t1", {"required_files": []})
    led.update_worker_state("t1", "WAITING", actor="reaper")
    out = led.update_worker_state("t1", "PENDING", actor="resume",
                                 reason="resume 续作")
    assert out == "PENDING"
    assert led.get_contract("t1")["harvest_status"] == "PENDING"
    led.close()


def test_c1b_waiting_to_pending_illegal_actor_still_rejected(tmp_path):
    """C-1 只扩 actor 集, 非法 actor 的 PermissionError 语义必须保留。"""
    led = _led(tmp_path)
    led.put_contract("t2", {"required_files": []})
    led.update_worker_state("t2", "WAITING", actor="reaper")
    try:
        led.update_worker_state("t2", "PENDING", actor="harvest")
        raise AssertionError("actor=harvest 不应能 WAITING→PENDING")
    except PermissionError:
        pass
    assert led.get_contract("t2")["harvest_status"] == "WAITING"
    led.close()


# ── C2a/C2b: resume_task 幂等 + WAITING 回拨 ────────────────────

def test_c2a_resume_on_pending_card_idempotent(tmp_path):
    """PENDING 无自环边, 旧代码必炸 ValueError; C-2 跳过迁移, 合法完成。"""
    from mw.pipeline import resume_task
    led = _led(tmp_path)
    led.put_contract("t3", {"required_files": []})
    led.upsert_session("t3", "sess-3", "coder", "api",
                      workspace=str(tmp_path))
    with patch("urllib.request.urlopen", return_value=FakeResp("sess-3")):
        r = resume_task("t3", ledger=led, instruction="继续", timeout=5)
    assert r["ok"] is True
    assert r["state_reset"] == "skipped:already_pending"
    assert led.get_contract("t3")["harvest_status"] == "PENDING"
    led.close()


def test_c2b_resume_on_waiting_card_resets(tmp_path):
    """C-1+C-2 组合: WAITING 卡 resume 正常回拨 PENDING。"""
    from mw.pipeline import resume_task
    led = _led(tmp_path)
    led.put_contract("t4", {"required_files": []})
    led.update_worker_state("t4", "WAITING", actor="reaper")
    led.upsert_session("t4", "sess-4", "coder", "api",
                      workspace=str(tmp_path))
    with patch("urllib.request.urlopen", return_value=FakeResp("sess-4")):
        r = resume_task("t4", ledger=led, instruction="继续", timeout=5)
    assert r["ok"] is True
    assert r["state_reset"] == "done"
    assert led.get_contract("t4")["harvest_status"] == "PENDING"
    led.close()


def test_c2c_resume_on_working_card_not_pinned(tmp_path):
    """WORKING→PENDING resume 不在白名单 (handoff 形状, 本轮不扩):
    回拨失败必须不中断函数 (防钉死), 返回附 skipped, 卡状态不变。"""
    from mw.pipeline import resume_task
    led = _led(tmp_path)
    led.put_contract("t5", {"required_files": []})
    led.update_worker_state("t5", "WORKING", actor="harvest")
    led.upsert_session("t5", "sess-5", "coder", "api",
                      workspace=str(tmp_path))
    with patch("urllib.request.urlopen", return_value=FakeResp("sess-5")):
        r = resume_task("t5", ledger=led, instruction="继续", timeout=5)
    assert r["ok"] is True  # 旧代码这里直接炸, resume 整体失败
    assert r["state_reset"].startswith("skipped:")
    assert led.get_contract("t5")["harvest_status"] == "WORKING"
    led.close()


# ── C3: M3 扩扫 WAITING 兜底结算 ────────────────────────────────

def _setup_ws(tmp_path, task_id="t_w"):
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    ledger = Ledger(str(tmp_path / "ledger.db"))
    ledger.put_contract(task_id, {"required_files": []})
    ledger.upsert_session(task_id, f"sess-{task_id}", "coder", "api",
                         None, workspace=str(ws))
    return task_id, ws, ledger


def _seed_waiting(ledger, task_id):
    ledger.update_worker_state(task_id, "WAITING", actor="reaper")


def test_c3a_waiting_todo_completed_settles(tmp_path):
    """WAITING 卡 + TODO.md 已 completed → M3 补结算 HARVESTED。"""
    from mw.reaper import _reap_late_completions
    task_id, ws, ledger = _setup_ws(tmp_path)
    _seed_waiting(ledger, task_id)
    _make_todo(ws, STATUS_COMPLETED)
    results = []
    _reap_late_completions(ledger, str(tmp_path), str(tmp_path), None,
                          results, None)
    assert results and results[0]["trigger"] == "late_settlement", results
    assert results[0]["verdict"] == "COMPLETE"
    assert ledger.get_contract(task_id)["harvest_status"] == "HARVESTED"
    ledger.close()


def test_c3b_waiting_todo_working_skipped(tmp_path):
    """worker 被唤醒仍在跑 (TODO=working) → 不误结算。"""
    from mw.reaper import _reap_late_completions
    task_id, ws, ledger = _setup_ws(tmp_path)
    _seed_waiting(ledger, task_id)
    _make_todo(ws, STATUS_WORKING)
    results = []
    _reap_late_completions(ledger, str(tmp_path), str(tmp_path), None,
                          results, None)
    assert results == []
    assert ledger.get_contract(task_id)["harvest_status"] == "WAITING"
    ledger.close()


def test_c3c_waiting_settlement_idempotent(tmp_path):
    """补结算后离开扫描集, 重复调用无副作用。"""
    from mw.reaper import _reap_late_completions
    task_id, ws, ledger = _setup_ws(tmp_path)
    _seed_waiting(ledger, task_id)
    _make_todo(ws, STATUS_COMPLETED)
    r1 = []
    _reap_late_completions(ledger, str(tmp_path), str(tmp_path), None,
                          r1, None)
    assert len(r1) == 1
    r2 = []
    _reap_late_completions(ledger, str(tmp_path), str(tmp_path), None,
                          r2, None)
    assert r2 == []
    assert ledger.get_contract(task_id)["harvest_status"] == "HARVESTED"
    ledger.close()
