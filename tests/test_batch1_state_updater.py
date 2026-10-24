"""批次1 (#5) 统一状态更新器回归测试 — BATCH1-DESIGN §4 T11-T15.

覆盖:
  T11 update_worker_state 合法迁移 → 落库 + touch_progress + worker.state 事件
  T12 非法迁移 (ARCHIVED→PENDING) → raise ValueError, 不落库
  T13 resume_task 收编后行为 → PENDING 复位 + actor=resume 事件
  T14 settle_team_work_residue 收编后 → 返回语义不变 + actor=advance 事件
  T15 state-correct 端点 → set 走 main_correct / 非 initiator 403 /
      真终态 set 回退 422
  T16 收口断言: mw/ 里 UPDATE harvest_contracts 只允许出现在
      ledger 统一器实现内部 (set_harvest/锁/通知字段), 状态迁移零裸写
"""
import json
import os
import re
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
    STATUS_COMPLETED, STATUS_FAILED, STATUS_WORKING,
)


def _led(tmp_path):
    return Ledger(str(tmp_path / "ledger.db"))


def _events(led, task_id):
    return [dict(r) for r in led.conn.execute(
        "SELECT event_type, payload FROM mw_events WHERE task_id=?",
        (task_id,)).fetchall()]


# ── T11: 合法迁移三件套 ─────────────────────────────────────────

def test_t11_legal_transition(tmp_path):
    led = _led(tmp_path)
    led.put_contract("t1", {"required_files": []})
    old_progress = led.get_contract("t1")["last_progress_at"]
    time.sleep(0.01)
    out = led.update_worker_state("t1", "BLOCKED", actor="reaper",
                                 reason="超时收尸", exit_code=124)
    assert out == "BLOCKED"
    hc = led.get_contract("t1")
    assert hc["harvest_status"] == "BLOCKED"
    assert hc["exit_code"] == 124
    assert "[reaper] 超时收尸" in hc["harvest_log"]  # 审计轨迹
    assert hc["last_progress_at"] > (old_progress or 0)  # progress bump
    evs = _events(led, "t1")
    st = [e for e in evs if e["event_type"] == "worker.state"]
    assert st, evs
    p = json.loads(st[-1]["payload"])
    assert p == {"from": "PENDING", "to": "BLOCKED",
                "actor": "reaper", "reason": "超时收尸"}
    led.close()


# ── T12: 非法迁移拒绝 ───────────────────────────────────────────

def test_t12_illegal_transition_rejected(tmp_path):
    led = _led(tmp_path)
    led.put_contract("t2", {"required_files": []})
    led.update_worker_state("t2", "HARVESTED", actor="harvest")
    led.update_worker_state("t2", "ARCHIVED", actor="operator")
    before = led.get_contract("t2")["harvest_status"]
    try:
        led.update_worker_state("t2", "PENDING", actor="main_correct")
        raise AssertionError("ARCHIVED→PENDING 应 raise ValueError")
    except ValueError:
        pass
    assert led.get_contract("t2")["harvest_status"] == before
    # HARVESTED → BLOCKED 也非法 (真终态保护)
    led.put_contract("t2b", {"required_files": []})
    led.update_worker_state("t2b", "HARVESTED", actor="harvest")
    try:
        led.update_worker_state("t2b", "BLOCKED", actor="reaper")
        raise AssertionError("HARVESTED→BLOCKED 应 raise ValueError")
    except ValueError:
        pass
    assert led.get_contract("t2b")["harvest_status"] == "HARVESTED"
    led.close()


# ── T13: resume_task 收编 ───────────────────────────────────────

def test_t13_resume_uses_updater(tmp_path):
    from mw.pipeline import resume_task
    led = _led(tmp_path)
    led.put_contract("t3", {"required_files": []})
    led.update_worker_state("t3", "NEED_CLARIFICATION", actor="harvest")
    led.upsert_session("t3", "sess-3", "coder", "api",
                      workspace=str(tmp_path))
    # 假 resume API: 直接返回受理
    class FakeResp:
        headers = {"X-Hermes-Session-Id": "sess-3"}
        def read(self):
            return json.dumps({"choices": [{"message":
                       {"content": "ok"}}]}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    with patch("urllib.request.urlopen", return_value=FakeResp()):
        r = resume_task("t3", ledger=led, instruction="继续",
                       timeout=5)
    assert r["ok"] is True
    assert led.get_contract("t3")["harvest_status"] == "PENDING"
    evs = [json.loads(e["payload"]) for e in _events(led, "t3")
           if e["event_type"] == "worker.state"]
    assert any(p["to"] == "PENDING" and p["actor"] == "resume"
               for p in evs), evs
    led.close()


# ── T14: settle_team_work_residue 收编 ──────────────────────────

def test_t14_settle_residue_uses_updater(tmp_path):
    led = _led(tmp_path)
    led.create_team_work_run("r14", "s_main", "需求")
    for tid in ("t_a", "t_b"):
        led.put_contract(tid, {"required_files": []})
        led.register_team_work_card("r14", tid)
    led.update_worker_state("t_a", "BLOCKED", actor="reaper",
                           log="timeout: 收尸")
    led.update_worker_state("t_b", "NEED_CLARIFICATION", actor="harvest")
    settled = led.settle_team_work_residue("r14", exclude_task_id="t_a")
    # 返回语义不变: 只覆盖 BLOCKED/NEED_CLARIFICATION
    assert settled == ["t_b"]
    assert led.get_contract("t_b")["harvest_status"] == "HARVESTED"
    assert "[main_advance]" in led.get_contract("t_b")["harvest_log"]
    evs = [json.loads(e["payload"]) for e in _events(led, "t_b")
           if e["event_type"] == "worker.state"]
    assert any(p["actor"] == "advance" and p["to"] == "HARVESTED"
               for p in evs), evs
    # t_a 未被 exclude 之外误动
    assert led.get_contract("t_a")["harvest_status"] == "BLOCKED"
    led.close()


# ── T15: state-correct 端点 ─────────────────────────────────────

def test_t15_state_correct_endpoint(tmp_path):
    import mw.console as console
    led = _led(tmp_path)
    led.put_contract("t15", {"required_files": []})
    led.update_worker_state("t15", "BLOCKED", actor="reaper",
                           log="timeout: 误收尸")
    led.record_initiator("t15", "s_main")
    console.LEDGER_DB = led.path
    try:
        # 非 initiator → 403
        r = console.api_task_state_correct("t15", {
            "action": "set", "target_status": "HARVESTED",
            "reason": "x", "actor_session": "s_intruder"})
        assert r.get("code") == 403, r
        # initiator set → 走 main_correct
        r = console.api_task_state_correct("t15", {
            "action": "set", "target_status": "PENDING",
            "reason": "误收尸, 让 reaper 重看", "actor_session": "s_main"})
        assert r["ok"] is True and r["to"] == "PENDING", r
        assert led.get_contract("t15")["harvest_status"] == "PENDING"
        evs = [json.loads(e["payload"]) for e in _events(led, "t15")
               if e["event_type"] == "worker.state"]
        assert any(p["actor"] == "main_correct" for p in evs), evs
        # 真终态 set 回退 → ValueError (console 层转 422)
        led.update_worker_state("t15", "HARVESTED", actor="harvest")
        try:
            console.api_task_state_correct("t15", {
                "action": "set", "target_status": "PENDING",
                "reason": "想翻案", "actor_session": "s_main"})
            raise AssertionError("真终态 set 回退应 raise ValueError")
        except ValueError:
            pass
        # 缺 reason → ValueError
        try:
            console.api_task_state_correct("t15", {
                "action": "set", "target_status": "PENDING",
                "actor_session": "s_main"})
            raise AssertionError("缺 reason 应 raise ValueError")
        except ValueError:
            pass
    finally:
        led.close()


# ── T16: 收口断言 — 状态迁移零裸 UPDATE ────────────────────────

def test_t16_no_bare_status_update():
    """mw/ 里 UPDATE harvest_contracts 涉及 harvest_status 的写,
    只允许出现在 ledger.py 统一器实现 (set_harvest) 内部。
    其他文件出现即收编遗漏。"""
    mw_dir = ROOT / "mw"
    offenders = []
    for py in sorted(mw_dir.glob("*.py")):
        src = py.read_text(encoding="utf-8")
        # 剥离 docstring 与注释再匹配, 防文档提及造成假阳性
        code = re.sub(r'"""[\s\S]*?"""', "", src)
        code = re.sub(r"#[^\n]*", "", code)
        for m in re.finditer(
                r'UPDATE\s+harvest_contracts\s+SET\s+([^"\']+)', code,
                re.IGNORECASE):
            cols = m.group(1)
            if "harvest_status" in cols and py.name != "ledger.py":
                offenders.append((py.name, cols[:60]))
    assert not offenders, f"裸状态迁移 UPDATE 未收编: {offenders}"
    # ledger.py 内部: harvest_status 的 UPDATE 只应出现在
    # set_harvest / put_contract / register 占位 (INSERT OR IGNORE) 里
    src = (mw_dir / "ledger.py").read_text(encoding="utf-8")
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    code = re.sub(r"#[^\n]*", "", code)
    hits = re.findall(
        r'UPDATE harvest_contracts SET harvest_status', code)
    # set_harvest 一处 + put_contract INSERT OR REPLACE 不算 UPDATE
    assert len(hits) <= 1, f"ledger 内 harvest_status UPDATE 超一处: {hits}"


# ── T17: reassess 终态门槛 (P1-1 修复回归) ──────────────────────

def _reassess_setup(tmp_path, task_id="t17", todo_status=None):
    """建 workspace + TODO.md (可指定状态/不建) + ledger + initiator。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    if todo_status is not None:
        task = TodoTask(step_id="s", title="T", goal="g", assignee="coder",
                       product_workspace=str(ws), status=todo_status)
        init_todo(ws / "TODO.md", "T", task)
    led = _led(tmp_path)
    led.put_contract(task_id, {"required_files": []})
    led.upsert_session(task_id, f"sess-{task_id}", "coder", "api",
                       None, workspace=str(ws))
    led.record_initiator(task_id, "s_main")
    return ws, led


def _reassess(led, task_id):
    import mw.console as console
    console.LEDGER_DB = led.path
    with patch("mw.harvest.export_transcript", return_value=None), \
         patch("mw.harvest.fetch_transcript_api", return_value=None):
        return console.api_task_state_correct(task_id, {
            "action": "reassess", "reason": "P1-1 回归",
            "actor_session": "s_main"})


def test_t17_reassess_nonterminal_not_settled(tmp_path):
    """P1-1: TODO.md 缺失 / working / blocked 等非终态 → reassess 不结算,
    不再被 `or "completed"` 兜底成 COMPLETE。"""
    for status in (None, STATUS_WORKING, "blocked", "进行中"):
        sub = tmp_path / f"c_{status}"
        sub.mkdir()
        _, led = _reassess_setup(sub, todo_status=status)
        try:
            r = _reassess(led, "t17")
            assert r["ok"] is True, r
            assert r.get("settled") is False, (status, r)
            assert "非终态" in r.get("note", ""), (status, r)
            # 契约状态不得被推进到 HARVESTED
            assert led.get_contract("t17")["harvest_status"] != "HARVESTED", \
                (status, led.get_contract("t17")["harvest_status"])
        finally:
            led.close()


def test_t17b_reassess_completed_settles(tmp_path):
    """终态 completed → reassess 正常结算 HARVESTED + verdict=COMPLETE。"""
    _, led = _reassess_setup(tmp_path, todo_status=STATUS_COMPLETED)
    try:
        r = _reassess(led, "t17")
        assert r["ok"] is True and r.get("settled") is True, r
        assert r.get("verdict") == "COMPLETE", r
        assert led.get_contract("t17")["harvest_status"] == "HARVESTED"
    finally:
        led.close()


def test_t17c_reassess_failed_settles(tmp_path):
    """终态 failed → reassess 结算 verdict=FAILED (worker 自报权威)。"""
    _, led = _reassess_setup(tmp_path, todo_status=STATUS_FAILED)
    try:
        r = _reassess(led, "t17")
        assert r["ok"] is True and r.get("settled") is True, r
        assert r.get("verdict") == "FAILED", r
    finally:
        led.close()
