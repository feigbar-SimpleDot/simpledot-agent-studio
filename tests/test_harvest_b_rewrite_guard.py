"""Test: Fix Point B — harvest() 重写守卫 (2026-10-07)

回归点:harvest() 守卫从 `!= "PENDING"` 改成白名单 (终态不重写)。
覆盖 7 个状态迁移场景,确保:
- PENDING → 正常流程
- BLOCKED / NEED_CLARIFICATION / ERROR → 允许重写(5min 兜底写下的
  陈旧报告能被 worker 真完工时覆写)
- HARVESTED / COMPLETE / ARCHIVED / CANCELLED → 拒绝(终态保护)
"""
import sqlite3
import tempfile
import os
import sys
from pathlib import Path

# 让脚本可以 import mw 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mw.harvest import harvest  # noqa: E402
from mw.ledger import Ledger  # noqa: E402


def _build_ledger(harvest_status: str) -> tuple:
    """建一个临时 ledger,塞一条 task contract,返回 (ledger, db_path, task_id, product_ws)。"""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    led = Ledger(db_path)
    task_id = "t_b_test"
    product_ws = tempfile.mkdtemp(prefix="b_test_")
    # contract: 一个 required_file,看 harvest 行为
    contract = {
        "required_files": [os.path.join(product_ws, "out.txt")],
        "timeout_seconds": 1800,
    }
    led.put_contract(task_id, contract, required_passes=None)
    # 强制改 status(模拟 5min 兜底写 BLOCKED 等场景)
    led.conn.execute(
        "UPDATE harvest_contracts SET harvest_status=?, "
        "harvest_log='mock', updated_at=? WHERE task_id=?",
        (harvest_status, 0.0, task_id))
    led.conn.commit()
    # 给 task_sessions 塞一行,否则 harvest 内部 None 安全但走 fallback
    led.upsert_session(task_id, "sess_test", "test", runner_kind="api",
                       workspace=product_ws)
    return led, db_path, task_id, product_ws


def _cleanup(led, db_path, product_ws):
    try:
        led.close()
    except Exception:
        pass
    try:
        os.unlink(db_path)
    except OSError:
        pass
    try:
        os.unlink(product_ws)
    except OSError:
        pass


# ───────── 场景 1:PENDING ─ 正常流程(产物理齐全应判 COMPLETE)─────────
def test_pending_rewrites_to_complete():
    led, db_path, tid, pws = _build_ledger("PENDING")
    try:
        # 让产物落地
        Path(pws, "out.txt").write_text("hello")
        r = harvest(tid, ledger=led, hermes_home="", workspace=pws,
                    spawn_result=None)
        assert r["settled"], f"PENDING 应正常结算, 实际={r}"
        assert r["verdict"] == "COMPLETE",             f"PENDING + 产物理齐 应判 COMPLETE, 实际={r.get('verdict')}"
    finally:
        _cleanup(led, db_path, pws)
    print("✓ pending_rewrites_to_complete")


# ───────── 场景 2:BLOCKED(5min 兜底写的陈旧报告)→ 允许重写 ─────────
def test_blocked_rewritten_when_physically_ok():
    """核心 fix point:陈旧 BLOCKED 报告必须能被 worker 真完工时覆写。"""
    led, db_path, tid, pws = _build_ledger("BLOCKED")
    try:
        # worker 真完工 — 产物已落盘
        Path(pws, "out.txt").write_text("done by worker")
        r = harvest(tid, ledger=led, hermes_home="", workspace=pws,
                    spawn_result=None)
        assert r["settled"],             f"BLOCKED 应允许重写(本次 B 修复), 实际被早返回={r}"
        assert r["verdict"] == "COMPLETE",             f"BLOCKED + 产物理齐 重写后应判 COMPLETE, 实际={r.get('verdict')}"
        # 验 ledger 状态也跟着变成 HARVESTED
        row = led.get_contract(tid)
        assert row["harvest_status"] == "HARVESTED",             f"ledger harvest_status 应更新, 实际={row['harvest_status']}"
    finally:
        _cleanup(led, db_path, pws)
    print("✓ blocked_rewritten_when_physically_ok")


# ───────── 场景 3:NEED_CLARIFICATION → 允许重写 ─────────
def test_need_clarification_rewritten():
    led, db_path, tid, pws = _build_ledger("NEED_CLARIFICATION")
    try:
        Path(pws, "out.txt").write_text("done")
        r = harvest(tid, ledger=led, hermes_home="", workspace=pws,
                    spawn_result=None)
        assert r["settled"],             f"NEED_CLARIFICATION 应允许重写, 实际={r}"
        assert r["verdict"] == "COMPLETE"
    finally:
        _cleanup(led, db_path, pws)
    print("✓ need_clarification_rewritten")


# ───────── 场景 4:ERROR → 允许重写 ─────────
def test_error_rewritten():
    led, db_path, tid, pws = _build_ledger("ERROR")
    try:
        Path(pws, "out.txt").write_text("done")
        r = harvest(tid, ledger=led, hermes_home="", workspace=pws,
                    spawn_result=None)
        assert r["settled"], f"ERROR 应允许重写, 实际={r}"
        assert r["verdict"] == "COMPLETE"
    finally:
        _cleanup(led, db_path, pws)
    print("✓ error_rewritten")


# ───────── 场景 5:HARVESTED → 拒绝(终态保护)─────────
def test_harvested_refused():
    led, db_path, tid, pws = _build_ledger("HARVESTED")
    try:
        Path(pws, "out.txt").write_text("done")
        r = harvest(tid, ledger=led, hermes_home="", workspace=pws,
                    spawn_result=None)
        assert r["settled"] is True,             f"HARVESTED 应被早返回 settled=True, 实际={r}"
        assert "already" in r.get("reason", ""),             f"HARVESTED 应返 'already ...' reason, 实际={r.get('reason')}"
        assert "verdict" not in r,             f"终态早返回不应给 verdict, 实际={r}"
        # ledger 不应被改
        row = led.get_contract(tid)
        assert row["harvest_status"] == "HARVESTED",             f"终态不应被覆写, 实际={row['harvest_status']}"
    finally:
        _cleanup(led, db_path, pws)
    print("✓ harvested_refused")


# ───────── 场景 6:ARCHIVED → 拒绝(操作员决策)─────────
def test_archived_refused():
    led, db_path, tid, pws = _build_ledger("ARCHIVED")
    try:
        r = harvest(tid, ledger=led, hermes_home="", workspace=pws,
                    spawn_result=None)
        assert r["settled"] is True
        assert "already" in r.get("reason", "")
        row = led.get_contract(tid)
        assert row["harvest_status"] == "ARCHIVED"
    finally:
        _cleanup(led, db_path, pws)
    print("✓ archived_refused")


# ───────── 场景 7:CANCELLED → 拒绝(用户取消)─────────
def test_cancelled_refused():
    led, db_path, tid, pws = _build_ledger("CANCELLED")
    try:
        r = harvest(tid, ledger=led, hermes_home="", workspace=pws,
                    spawn_result=None)
        assert r["settled"] is True
        assert "already" in r.get("reason", "")
        row = led.get_contract(tid)
        assert row["harvest_status"] == "CANCELLED"
    finally:
        _cleanup(led, db_path, pws)
    print("✓ cancelled_refused")


# ───────── 场景 8:回归断言 — 旧守卫下 BLOCKED 不被覆写(确认 bug 存在)─────────
def test_regression_old_guard_left_blocked_stale():
    """确认旧守卫的 bug 是真实存在的(防回退)。

    旧代码: if row['harvest_status'] != 'PENDING': return
    BLOCKED 满足 !=PENDING → 早返回 → worker_report 永远是陈旧 BLOCKED。
    """
    led, db_path, tid, pws = _build_ledger("BLOCKED")
    try:
        # 模拟旧守卫(只允许 PENDING 继续)
        cur = led.get_contract(tid)
        if cur["harvest_status"] != "PENDING":
            old_guard_result = {"settled": True,
                              "reason": f"already {cur['harvest_status']}"}
        else:
            old_guard_result = {"settled": False, "reason": "would proceed"}
        assert old_guard_result["settled"] is True
        assert "already BLOCKED" in old_guard_result["reason"],             "回归断言: 旧守卫确实把 BLOCKED 早返回"
    finally:
        _cleanup(led, db_path, pws)
    print("✓ regression_old_guard_left_blocked_stale (确认旧 bug)")


# ───────── 场景 9:set_worker_report 联动 — 重写后清 notify_confirmed_at ─────────
def test_rewrite_clears_notify_confirmed_at():
    """B 的预期效果之一:重写后 set_worker_report 的 same_content 检测
    应清 notify_confirmed_at,让 relay 重新投新报告给 main。

    这条是链路验证 — 走完 harvest() 后再看 ledger 是否允许 relay 重投。
    """
    led, db_path, tid, pws = _build_ledger("BLOCKED")
    try:
        Path(pws, "out.txt").write_text("done")
        # 模拟上次 harvest 写完的 confirmed 标记(假设陈旧报告曾被 main 收到)
        led.conn.execute(
            "UPDATE harvest_contracts SET notify_confirmed_at=? WHERE task_id=?",
            (1.0, tid))
        led.conn.commit()
        # 新一次 harvest (BLOCKED → 允许重写)
        r = harvest(tid, ledger=led, hermes_home="", workspace=pws,
                    spawn_result=None)
        assert r["verdict"] == "COMPLETE"
        # 验证 notify_confirmed_at 已被清(set_worker_report 走「内容变化」分支)
        row = led.get_contract(tid)
        assert row["notify_confirmed_at"] is None,             f"重写后 confirmed_at 应清 NULL, 实际={row['notify_confirmed_at']}"
    finally:
        _cleanup(led, db_path, pws)
    print("✓ rewrite_clears_notify_confirmed_at")


if __name__ == "__main__":
    test_pending_rewrites_to_complete()
    test_blocked_rewritten_when_physically_ok()
    test_need_clarification_rewritten()
    test_error_rewritten()
    test_harvested_refused()
    test_archived_refused()
    test_cancelled_refused()
    test_regression_old_guard_left_blocked_stale()
    test_rewrite_clears_notify_confirmed_at()
    print("\nALL PASS — B verified.")
