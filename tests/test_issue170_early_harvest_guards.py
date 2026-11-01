"""issue #170 提前收割三连坑回归测试 (2026-10-30)。

覆盖三条缺陷 (PLAN-M-20261030 §2):
  坑 1  物理检查只看存在不看新鲜 → 迟到写/上轮残留产物被当本轮交付
        - F1 产物 mtime 早于基准 ⇒ physical_stale (与 missing 区分)
        - F2 新鲜产物 ⇒ passed=True, stale=False
        - F3 基准取不到 (0.0) ⇒ 不做新鲜度判定 (fail-open)
        - F4 epsilon 容差: 基准同一秒的产物不误判
        - F5 基准优先 task_sessions/team_work_runs, 不被
             harvest_contracts.created_at 重置拖偏 (PLAN F-4)
  坑 3  收割前卡绑定校验
        - B1 『卡:』指向他卡 ⇒ 拒收 (SKIP_CARD_BINDING_MISMATCH,
              不落 HARVESTED / 不写 worker_report)
        - B2 『卡:』== 本卡 ⇒ 放行 (本链自证: 不误杀)
        - B3 无『卡:』字段 ⇒ 放行 (todo_missing/no_binding 不误杀)
        - B4 reaper 侧: mismatch ⇒ 不收割 + 留痕事件 + 敲门
        - B5 reaper 侧: 匹配 ⇒ 正常收割

纯函数/本地 ledger 测试, 不依赖 hermes CLI / 网络: 敲门通道 patch 掉。
"""
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

import pytest  # noqa: E402

from mw.ledger import Ledger  # noqa: E402
from mw.todo import (  # noqa: E402
    init_todo, TodoTask, STATUS_COMPLETED, STATUS_FAILED, STATUS_BLOCKED,
)
from mw.harvest import (  # noqa: E402
    FRESH_EPSILON, verify_physical, verify_card_binding_for_harvest,
    _freshness_baseline,
)


@pytest.fixture(autouse=True)
def _no_transcript_export():
    """容器无 hermes CLI: 屏蔽 transcript 导出路径 (与既有测试同法)。"""
    with patch("mw.harvest.export_transcript", return_value=None), \
         patch("mw.harvest.fetch_transcript_api", return_value=None):
        yield


# ════════════════════════════════════════════════════════════════
# 坑 1: 新鲜度基准 + 陈旧判定
# ════════════════════════════════════════════════════════════════

def _mk_product(tmp_path, name="out.md", mtime=None):
    """建产物文件, 可选把 mtime 拨到过去。返回 (product_ws, rel)。"""
    pws = tmp_path / "proj"
    pws.mkdir(exist_ok=True)
    rel = f"{name}"
    f = pws / name
    f.write_text("content", encoding="utf-8")
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return str(pws), rel


def test_f1_stale_product_detected():
    """F1: 产物存在但 mtime 早于基准 ⇒ passed=False 且 stale=True。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        base = time.time()
        # 产物 mtime 拨到基准前 100s (远超 epsilon)
        pws, rel = _mk_product(tmp, mtime=base - 100)
        contract = {"required_files": [rel]}
        info: dict = {}
        ok, why = verify_physical(contract, str(tmp), pws,
                                  baseline_ts=base,
                                  baseline_label="task_sessions.created_at",
                                  out_info=info)
        assert ok is False
        assert info.get("stale") is True
        assert "stale_target" in info
        # 与 missing 严格区分: 文案不是"缺少产出文件"
        assert "缺少" not in why


def test_f2_fresh_product_passes():
    """F2: 产物 mtime 晚于基准 ⇒ passed=True, stale 不置位。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        pws, rel = _mk_product(tmp)  # mtime = now
        base = time.time() - 50      # 基准在过去
        contract = {"required_files": [rel]}
        info: dict = {}
        ok, why = verify_physical(contract, str(tmp), pws,
                                  baseline_ts=base, out_info=info)
        assert ok is True
        assert not info.get("stale")


def test_f3_no_baseline_skips_freshness():
    """F3: 基准取不到 (0.0) ⇒ 不做新鲜度判定 (fail-open 不误杀)。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        pws, rel = _mk_product(tmp, mtime=time.time() - 10_000)
        contract = {"required_files": [rel]}
        ok, why = verify_physical(contract, str(tmp), pws, baseline_ts=0.0)
        assert ok is True  # 无基准 ⇒ 只查存在性


def test_f4_epsilon_tolerates_same_second_write():
    """F4: 基准与产物 mtime 同秒级 (差 < epsilon) ⇒ 不误判陈旧。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        now = time.time()
        # 产物比基准"早" 1s —— 在 2.0s epsilon 内 ⇒ 放行
        pws, rel = _mk_product(tmp, mtime=now - 1.0)
        contract = {"required_files": [rel]}
        ok, why = verify_physical(contract, str(tmp), pws, baseline_ts=now)
        assert ok is True
        assert FRESH_EPSILON == 2.0


def test_f5_baseline_prefers_session_over_contract(tmp_path):
    """F5 (PLAN F-4): 基准取 task_sessions.created_at, 不被
    harvest_contracts.created_at (每次 dispatch 被重置) 拖偏。"""
    led = Ledger(str(tmp_path / "ledger.db"))
    tid = "t_f5"
    now = time.time()
    # 契约 created_at = now (模拟刚被 dispatch 重置)
    led.put_contract(tid, {"required_files": []})
    led.upsert_session(tid, "sess-f5", "coder", "api", None,
                       workspace=str(tmp_path))
    # session created_at 拨到 1 小时前 (worker 真正开跑时刻)
    led.conn.execute(
        "UPDATE task_sessions SET created_at=? WHERE task_id=?",
        (now - 3600, tid))
    led.conn.commit()
    ts, label = led.get_freshness_baseline(tid)
    assert label == "task_sessions.created_at"
    assert abs(ts - (now - 3600)) < 1.0
    led.close()


def test_f5b_baseline_falls_back_to_contract(tmp_path):
    """F5b: 无 session 行 ⇒ 兜底 harvest_contracts.created_at。"""
    led = Ledger(str(tmp_path / "ledger.db"))
    tid = "t_f5b"
    led.put_contract(tid, {"required_files": []})
    ts, label = led.get_freshness_baseline(tid)
    assert label == "harvest_contracts.created_at"
    assert ts > 0
    led.close()


def test_f5c_baseline_missing_returns_zero(tmp_path):
    """F5c: 账本无此卡 ⇒ (0.0, "") ⇒ 上层不做新鲜度判定。"""
    led = Ledger(str(tmp_path / "ledger.db"))
    assert led.get_freshness_baseline("t_none") == (0.0, "")
    # _freshness_baseline 对缺方法的替身也 fail-open
    class Dummy:
        pass
    assert _freshness_baseline(Dummy(), "x") == (0.0, "")
    led.close()


# ════════════════════════════════════════════════════════════════
# 坑 3: 收割前卡绑定校验 (纯函数层)
# ════════════════════════════════════════════════════════════════

def _todo_with_card(tmp_path, card_id, status=STATUS_COMPLETED):
    """建带『卡:』绑定的 TODO.md, 返回路径。"""
    task = TodoTask(step_id="s", title="T", goal="g", assignee="coder",
                    status=status, kanban_task_id=card_id)
    p = tmp_path / "TODO.md"
    init_todo(p, "T", task)
    return p


def test_b1_mismatch_rejected(tmp_path):
    """B1: 『卡:』指向他卡 ⇒ 拒收 (迟到写污染形状)。"""
    p = _todo_with_card(tmp_path, "t_other")
    ok, detail = verify_card_binding_for_harvest("t_mine", str(p))
    assert ok is False
    assert detail["card_binding"] == "mismatch"
    assert detail.get("reason") == "mismatch"


def test_b2_matching_card_passes(tmp_path):
    """B2: 『卡:』== 本卡 ⇒ 放行 (本链自证: 不误杀)。"""
    p = _todo_with_card(tmp_path, "t_mine")
    ok, detail = verify_card_binding_for_harvest("t_mine", str(p))
    assert ok is True
    assert detail["card_binding"] == "ok"


def test_b3_no_binding_passes(tmp_path):
    """B3: TODO.md 无『卡:』字段 ⇒ 放行 (历史卡不误杀)。"""
    task = TodoTask(step_id="s", title="T", goal="g", assignee="coder",
                    status=STATUS_COMPLETED)  # 无 kanban_task_id
    p = tmp_path / "TODO.md"
    init_todo(p, "T", task)
    ok, detail = verify_card_binding_for_harvest("t_mine", str(p))
    assert ok is True
    assert detail["card_binding"] == "unknown"


def test_b3b_missing_todo_and_none_path_pass(tmp_path):
    """B3b: TODO.md 不存在 / 路径为 None ⇒ 放行 (fail-open)。"""
    ok, d = verify_card_binding_for_harvest("t_x", str(tmp_path / "no.md"))
    assert ok is True and d["card_binding"] == "unknown"
    ok, d = verify_card_binding_for_harvest("t_x", None)
    assert ok is True and d["reason"] == "no_todo_path"


# ════════════════════════════════════════════════════════════════
# 坑 3: harvest / reaper 集成层
# ════════════════════════════════════════════════════════════════

def _setup_harvest(tmp_path, task_id, card_id, with_product=True,
                   status=STATUS_COMPLETED):
    """建 ledger + 契约 + session + 项目根 TODO.md (带卡绑定)。"""
    ws = tmp_path / ".tasks" / task_id
    ws.mkdir(parents=True)
    proj = tmp_path  # 项目根 = tmp_path (workspace 在 .tasks/ 下)
    led = Ledger(str(tmp_path / "ledger.db"))
    led.put_contract(task_id, {"required_files": []})
    led.upsert_session(task_id, f"sess-{task_id}", "coder", "api",
                       None, workspace=str(ws))
    task = TodoTask(step_id="s", title="T", goal="g", assignee="coder",
                    status=status, kanban_task_id=card_id,
                    product_workspace=str(proj) if with_product else "")
    init_todo(proj / "TODO.md", "T", task)
    return led, ws, proj


def test_b4_harvest_rejects_mismatch(tmp_path):
    """B4: harvest 入口遇卡绑定冲突 ⇒ 不收割、不落 HARVESTED、留痕。"""
    led, ws, proj = _setup_harvest(tmp_path, "t_cur", "t_other")
    from mw.harvest import harvest
    r = harvest("t_cur", ledger=led, hermes_home=str(tmp_path),
                workspace=str(ws), spawn_result=None,
                worker_status="completed")
    assert r["settled"] is False
    assert r["verdict"] == "SKIP_CARD_BINDING_MISMATCH"
    # 契约状态未被推进 (没写 HARVESTED)
    assert led.get_contract("t_cur")["harvest_status"] == "PENDING"
    # 账本留痕
    ev = led.conn.execute(
        "SELECT event_type FROM mw_events WHERE task_id=?",
        ("t_cur",)).fetchall()
    assert "harvest.card_binding_mismatch" in [e["event_type"] for e in ev]
    led.close()


def test_b5_harvest_passes_matching_card(tmp_path):
    """B5: 『卡:』匹配 ⇒ 正常收割 HARVESTED (本链自证不误杀)。"""
    led, ws, proj = _setup_harvest(tmp_path, "t_cur", "t_cur")
    from mw.harvest import harvest
    r = harvest("t_cur", ledger=led, hermes_home=str(tmp_path),
                workspace=str(ws), spawn_result=None,
                worker_status="completed")
    assert r["settled"] is True
    assert r["verdict"] == "COMPLETE"
    assert led.get_contract("t_cur")["harvest_status"] == "HARVESTED"
    # physical_check 新增字段存在且默认非陈旧 (required_files=[] ⇒ 无陈旧)
    pc = r["worker_report"]["physical_check"]
    assert pc["stale"] is False
    led.close()


def test_b6_reaper_mismatch_skips_and_knocks(tmp_path):
    """B6: reaper 遇 mismatch ⇒ 不收割 + 留痕 + 敲门 main。"""
    led, ws, proj = _setup_harvest(tmp_path, "t_cur", "t_other")
    # 补 initiator (敲门目标)
    led.conn.execute(
        "INSERT OR REPLACE INTO task_initiators VALUES (?,?,?)",
        ("t_cur", "sess-main", time.time()))
    led.conn.commit()
    # spawn marker (issue#9 守门要求)
    marker_dir = tmp_path / "spawn"
    marker_dir.mkdir(exist_ok=True)
    (marker_dir / "t_cur.json").write_text(json.dumps(
        {"started_at": time.time(), "workspace": str(ws),
         "task_id": "t_cur"}))
    row = dict(led.conn.execute(
        "SELECT * FROM harvest_contracts WHERE task_id=?",
        ("t_cur",)).fetchone())
    from mw.reaper import _reap_one_task
    results = []
    knocked = []
    with patch("mw.reaper._send_to_main",
               side_effect=lambda *a, **k: knocked.append(a)):
        _reap_one_task(row, led, str(tmp_path), str(tmp_path), 30,
                       None, results)
    # 未收割
    assert led.get_contract("t_cur")["harvest_status"] == "PENDING"
    verdicts = [r.get("verdict") for r in results]
    assert "SKIP_CARD_BINDING_MISMATCH" in verdicts, results
    # 敲了门
    assert knocked, "mismatch 必须敲门 main"
    # 留痕
    ev = [e["event_type"] for e in led.conn.execute(
        "SELECT event_type FROM mw_events WHERE task_id=?",
        ("t_cur",)).fetchall()]
    assert "harvest.card_binding_mismatch" in ev
    led.close()


def test_b7_reaper_matching_card_harvests(tmp_path):
    """B7: reaper 匹配卡 ⇒ 正常收割 (校验对正常链零干扰)。"""
    led, ws, proj = _setup_harvest(tmp_path, "t_cur", "t_cur")
    marker_dir = tmp_path / "spawn"
    marker_dir.mkdir(exist_ok=True)
    (marker_dir / "t_cur.json").write_text(json.dumps(
        {"started_at": time.time(), "workspace": str(ws),
         "task_id": "t_cur"}))
    row = dict(led.conn.execute(
        "SELECT * FROM harvest_contracts WHERE task_id=?",
        ("t_cur",)).fetchone())
    from mw.reaper import _reap_one_task
    results = []
    _reap_one_task(row, led, str(tmp_path), str(tmp_path), 30, None, results)
    assert led.get_contract("t_cur")["harvest_status"] == "HARVESTED"
    assert not [r for r in results
                if r.get("verdict") == "SKIP_CARD_BINDING_MISMATCH"]
    led.close()


# ════════════════════════════════════════════════════════════════
# s8 审查回流 P2-2: 绑定校验覆盖 failed / blocked / clarify 路径
# (REVIEW-M-20261030 §5 P2-2: 迟到写同样可伪造这三种终态)
# ════════════════════════════════════════════════════════════════

def _mismatch_events(led, task_id):
    return led.conn.execute(
        "SELECT event_type FROM mw_events WHERE task_id=? "
        "AND event_type='harvest.card_binding_mismatch'",
        (task_id,)).fetchall()


def test_p22_harvest_failed_mismatch_rejected(tmp_path):
    """P2-2: harvest 遇 failed + 卡绑定冲突 ⇒ 拒收 (不落 FAILED)。"""
    led, ws, proj = _setup_harvest(tmp_path, "t_cur", "t_other",
                                   status=STATUS_FAILED)
    from mw.harvest import harvest
    r = harvest("t_cur", ledger=led, hermes_home=str(tmp_path),
                workspace=str(ws), spawn_result=None,
                worker_status="failed")
    assert r["settled"] is False
    assert r["verdict"] == "SKIP_CARD_BINDING_MISMATCH"
    assert led.get_contract("t_cur")["harvest_status"] == "PENDING"
    assert len(_mismatch_events(led, "t_cur")) == 1
    led.close()


def test_p22_harvest_blocked_mismatch_rejected(tmp_path):
    """P2-2: harvest 遇 blocked + 卡绑定冲突 ⇒ 拒收 (不落 NEED_CLARIFICATION)。"""
    led, ws, proj = _setup_harvest(tmp_path, "t_cur", "t_other",
                                   status=STATUS_BLOCKED)
    (ws / ".clarify.json").write_text(
        json.dumps({"question": "q"}), encoding="utf-8")
    from mw.harvest import harvest
    r = harvest("t_cur", ledger=led, hermes_home=str(tmp_path),
                workspace=str(ws), spawn_result=None,
                worker_status="blocked")
    assert r["settled"] is False
    assert r["verdict"] == "SKIP_CARD_BINDING_MISMATCH"
    assert led.get_contract("t_cur")["harvest_status"] == "PENDING"
    assert len(_mismatch_events(led, "t_cur")) == 1
    led.close()


def test_p22_harvest_clarify_fallback_mismatch_rejected(tmp_path):
    """P2-2: worker_status 未知 + .clarify.json 兜底分支 (原校验盲区)
    遇卡绑定冲突 ⇒ 拒收。校验已移到 clarify 分支之前。"""
    led, ws, proj = _setup_harvest(tmp_path, "t_cur", "t_other")
    (ws / ".clarify.json").write_text(
        json.dumps({"question": "q"}), encoding="utf-8")
    from mw.harvest import harvest
    r = harvest("t_cur", ledger=led, hermes_home=str(tmp_path),
                workspace=str(ws), spawn_result=None,
                worker_status=None)  # 未知状态 → 走 1c clarify 兜底
    assert r["settled"] is False
    assert r["verdict"] == "SKIP_CARD_BINDING_MISMATCH"
    assert led.get_contract("t_cur")["harvest_status"] == "PENDING"
    led.close()


def test_p22_harvest_failed_no_binding_passes(tmp_path):
    """P2-2 反例守恒: failed 路径无『卡:』绑定 ⇒ 放行 (不误杀)。"""
    led, ws, proj = _setup_harvest(tmp_path, "t_cur", "",
                                   status=STATUS_FAILED)
    from mw.harvest import harvest
    r = harvest("t_cur", ledger=led, hermes_home=str(tmp_path),
                workspace=str(ws), spawn_result=None,
                worker_status="failed")
    assert r["settled"] is True
    assert r["verdict"] == "FAILED"
    led.close()


def _reap_with_marker(tmp_path, led, ws, task_id):
    """补 spawn marker 后跑一轮 _reap_one_task, 返回 results。"""
    marker_dir = tmp_path / "spawn"
    marker_dir.mkdir(exist_ok=True)
    (marker_dir / f"{task_id}.json").write_text(json.dumps(
        {"started_at": time.time(), "workspace": str(ws),
         "task_id": task_id}))
    row = dict(led.conn.execute(
        "SELECT * FROM harvest_contracts WHERE task_id=?",
        (task_id,)).fetchone())
    from mw.reaper import _reap_one_task
    results = []
    knocked = []
    with patch("mw.reaper._send_to_main",
               side_effect=lambda *a, **k: knocked.append(a)):
        _reap_one_task(row, led, str(tmp_path), str(tmp_path), 30,
                       None, results)
    return results


def test_p22_reaper_failed_mismatch_skips(tmp_path):
    """P2-2: reaper 遇 failed + mismatch ⇒ 不收割 (公共入口校验)。"""
    led, ws, proj = _setup_harvest(tmp_path, "t_cur", "t_other",
                                   status=STATUS_FAILED)
    results = _reap_with_marker(tmp_path, led, ws, "t_cur")
    assert led.get_contract("t_cur")["harvest_status"] == "PENDING"
    assert "SKIP_CARD_BINDING_MISMATCH" in [r.get("verdict")
                                            for r in results], results
    led.close()


def test_p22_reaper_blocked_mismatch_skips(tmp_path):
    """P2-2: reaper 遇 blocked(+clarify) + mismatch ⇒ 不收割。"""
    led, ws, proj = _setup_harvest(tmp_path, "t_cur", "t_other",
                                   status=STATUS_BLOCKED)
    (ws / ".clarify.json").write_text(
        json.dumps({"question": "q"}), encoding="utf-8")
    results = _reap_with_marker(tmp_path, led, ws, "t_cur")
    assert led.get_contract("t_cur")["harvest_status"] == "PENDING"
    assert "SKIP_CARD_BINDING_MISMATCH" in [r.get("verdict")
                                            for r in results], results
    led.close()


def test_p22_reaper_blocked_matching_harvests(tmp_path):
    """P2-2 反例守恒: blocked + 绑定匹配 ⇒ 正常结算 (不误杀本链)。"""
    led, ws, proj = _setup_harvest(tmp_path, "t_cur", "t_cur",
                                   status=STATUS_BLOCKED)
    (ws / ".clarify.json").write_text(
        json.dumps({"question": "q"}), encoding="utf-8")
    results = _reap_with_marker(tmp_path, led, ws, "t_cur")
    assert led.get_contract("t_cur")["harvest_status"] == \
        "NEED_CLARIFICATION"
    assert not [r for r in results
                if r.get("verdict") == "SKIP_CARD_BINDING_MISMATCH"]
    led.close()


# ════════════════════════════════════════════════════════════════
# s8 审查回流 P2-1: mismatch 事件去重 (持续污染不再每 15s 刷屏)
# (REVIEW-M-20261030 §5 P2-1)
# ════════════════════════════════════════════════════════════════

def test_p21_reaper_mismatch_event_dedup(tmp_path):
    """P2-1: mismatch 持续 ⇒ 首轮 emit 事件, 后续 reap 静默 (去重窗口内)。"""
    led, ws, proj = _setup_harvest(tmp_path, "t_cur", "t_other")
    led.conn.execute(
        "INSERT OR REPLACE INTO task_initiators VALUES (?,?,?)",
        ("t_cur", "sess-main", time.time()))
    led.conn.commit()
    for _ in range(3):  # 模拟连续 3 轮 reap
        _reap_with_marker(tmp_path, led, ws, "t_cur")
    assert len(_mismatch_events(led, "t_cur")) == 1, \
        "mismatch 事件必须去重: 窗口内只留首报"
    led.close()


def test_p21_dedup_window_expiry_reemits(tmp_path):
    """P2-1: 首报超出窗口后 ⇒ 再次 emit (降频而非一次性静默)。"""
    led, ws, proj = _setup_harvest(tmp_path, "t_cur", "t_other")
    from mw.reaper import _binding_mismatch_event_suppressed
    assert _binding_mismatch_event_suppressed(led, "t_cur") is False
    led.emit_event("harvest.card_binding_mismatch", "t_cur", {})
    assert _binding_mismatch_event_suppressed(led, "t_cur") is True
    # 把首报时间拨到窗口之外 (默认 30min) ⇒ 解除静默
    led.conn.execute(
        "UPDATE mw_events SET created_at=? "
        "WHERE task_id=? AND event_type='harvest.card_binding_mismatch'",
        (time.time() - 31 * 60, "t_cur"))
    led.conn.commit()
    assert _binding_mismatch_event_suppressed(led, "t_cur") is False
    led.close()
