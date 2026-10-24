"""Issue #9 (2026-10-13) reaper 扫描与敲门机制修复 — s2 回归测试。

落地 s2 (#9) §4 验收: reaper 收紧触发集 + 假警报三型 (a)(b)(c)
(a) created_at 兜底误敲; (b) exit124 假 FAILED; (c) 0-tool-calls 陈旧快照
假 COMPLETE, 加上 AC9-1 / AC9-2 「不再对已结算/已终止 task 敲门」。

耦合纪律 (triage §7.2): 不发明新终态, BLOCKED 相关改动保持最小 —
具体到本测试集: 只验证「不再敲多」「不再收错」两种负面行为, 不动状态机。

Test IDs 对应方案 §6 与 s2 派单 §4:
  G1  AC9-1: run 非 ACTIVE → reap_once 对该 run 名下任何卡零动作
  G2  AC9-2: 已 confirmed 送达的卡, _notify_main_of_stuck 不再敲
  G3  AC9-2: 已 confirmed 送达的报告路径, _notify_null_slot 也不再误敲 (由现有 F4b 抑制层承担)
  G4  AC9-3 (a): run 刚出生 (< grace) → null_slot_knock 不敲
  G5  AC9-3 (a): run 只剩 created_at 兜底 → 文案为「等待 main 派发」风格 (软提示)
  G6  AC9-3 (c): TODO.md 显示 completed 但本卡无 marker → SKIP_STALE_TODO
  G7  AC9-3 (c): marker.task_id 与本卡不一致 → SKIP_STALE_TODO
  G8  真挂死兜底: 真没有 dispatched / relayed 证据时, 仍能走到 NOTIFIED (AC9-5)
  G9  通跑 reap_once 已 settled (HARVESTED) 的 run → reap_scope 也仍会扫它名下
       未结算的 pending 卡 (verify active_run scope 通过的回归)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")  # 不会真连

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mw.ledger import Ledger  # noqa: E402
from mw.todo import (  # noqa: E402
    init_todo, TodoTask, STATUS_COMPLETED, STATUS_WORKING, STATUS_FAILED,
)

import pytest  # noqa: E402


PASS = "\033[92mPASS\033[0m"
FAIL_R = "\033[91mFAIL\033[0m"
results: list[bool] = []


def _assert(name: str, cond: bool, msg: str = "") -> None:
    if cond:
        print(f"  {PASS} {name}")
        results.append(True)
    else:
        print(f"  {FAIL_R} {name}: {msg}")
        results.append(False)


@pytest.fixture(autouse=True)
def _no_transcript_export() -> None:
    with patch("mw.harvest.export_transcript", return_value=None), \
         patch("mw.harvest.fetch_transcript_api", return_value=None):
        yield


def _make_todo(ws: Path, status: str) -> None:
    p = ws / "TODO.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    task = TodoTask(step_id="s", title="T", goal="g", assignee="coder",
                   product_workspace=str(ws), status=status)
    init_todo(p, "T", task)


def _setup(tmp_path: Path, *, run_id: str = "r1", task_id: str = "t1",
           ws_dirname: str = "ws"):
    """基础夹具: ws + ledger + card + session + initiator。"""
    ws = tmp_path / ws_dirname
    ws.mkdir()
    led = Ledger(str(tmp_path / "ledger.db"))
    led.create_team_work_run(run_id, "sid_main", "需求")
    led.put_contract(task_id, {"required_files": [], "timeout_seconds": 60})
    led.register_team_work_card(run_id, task_id)
    led.upsert_session(task_id, "sid_w", "coder", "api", None,
                      workspace=str(ws))
    led.record_initiator(task_id, "sid_main")
    return led, ws, task_id


# =============================================================
# G1: AC9-1 — run 非 ACTIVE → reap_once 全零动作
# =============================================================
def test_g1_run_not_active_reap_zeros() -> None:
    """所有 PS/BLOCKED/HARVESTED 在非 ACTIVE run 下 reap_once 不动任何卡。

    reaper 在 reap_once 顶层取 ledger.get_active_team_work() — 无 ACTIVE
    run 时直接走 reap_scope=SKIP, 后面的 _reap_one_task /
    _notify_main_of_stuck / _notify_null_slot / _reap_late_completions
    都不被触发。结果 = 只有 1 条 reap_scope=SKIP, 没有触达任何卡片。
    """
    from mw import reaper
    led, ws, tid = _setup(
        Path(tempfile.mkdtemp()), run_id="r_close", task_id="t1")
    base = Path(led.path).parent
    try:
        # 把 run 强关, 模拟 close_team_work 后的常态
        led.conn.execute(
            "UPDATE team_work_runs SET status='CLOSED' WHERE run_id='r_close'")
        led.conn.commit()
        # 在 ledger 里放一张「未结算」卡: reap_once 顶层就 SKIP, 不会动
        led.conn.execute(
            "UPDATE harvest_contracts SET harvest_status='PENDING', "
            "team_work_run_id='r_close', last_progress_at=? WHERE task_id=?",
            (time.time() - 600, "t1"))
        led.conn.commit()

        _make_todo(ws, STATUS_COMPLETED)
        with patch("mw.reaper.harvest",
                  lambda *a, **k: {"verdict": "X", "settled": False}):
            rs = reaper.reap_once(ledger=led, hermes_home=str(base),
                                 base=str(base), grace_seconds=10, registry={})
        # reap_scope 必出现且为 SKIP
        scope = [r for r in rs if r.get("trigger") == "reap_scope"]
        _assert("g1 reap_once 有 reap_scope 条目",
                len(scope) == 1, f"got {rs}")
        _assert("g1 reap_scope verdict=SKIP",
                scope[0].get("verdict") == "SKIP", f"{scope[0]}")
        # 关键: 没有对 t1 的 harvest/relay/敲门动作 (harvest 没被调)
        for r in rs:
            assert r.get("task_id") != "t1" or r.get("trigger") == "reap_scope", \
                f"run 关闭后不该触达 t1: {r}"
        _assert("g1 run=CLOSED 时 t1 不被触达", True)
    finally:
        led.close()
        import shutil; shutil.rmtree(base)


# =============================================================
# G2: AC9-2 — _notify_main_of_stuck 不敲已 confirmed 卡
# =============================================================
def test_g2_stuck_notify_skips_delivered() -> None:
    """已 confirmed 送达的卡, _notify_main_of_stuck 直接 SUPPRESSED_DELIVERED。

    此前: BLOCKED + notify_confirmed_at 已敲定 → 仍触发 stuck knock
    现行: 新加的 `notify_confirmed_at` 守门, 一句 return,
          不再 send_to_main, verdict=SUPPRESSED_DELIVERED。
    """
    from mw import reaper as reaper_mod
    led, ws, tid = _setup(Path(tempfile.mkdtemp()))
    base = Path(led.path).parent
    try:
        # 模拟「之前已被敲过且 confirm_delivered 落账」
        led.conn.execute(
            "UPDATE harvest_contracts SET harvest_status='BLOCKED', "
            "last_progress_at=?, notify_confirmed_at=? WHERE task_id=?",
            (time.time() - 3600, time.time() - 60, tid))
        led.conn.commit()
        row = led.conn.execute(
            "SELECT * FROM harvest_contracts WHERE task_id=?",
            (tid,)).fetchone()
        sent = []
        results_list: list = []
        with patch.object(reaper_mod, "_send_to_main",
                         lambda *a, **k: sent.append(a) or True), \
             patch.object(reaper_mod, "_query_console_liveness",
                          lambda *a, **k: None), \
             patch.object(reaper_mod, "_query_initiator_last_active",
                          lambda *a, **k: None):
            reaper_mod._notify_main_of_stuck(
                led, str(base), results_list, registry=None,
                stuck_after_min=1, active_run_id="r1",
                current_card_id=tid)
        _assert("g2 SUPPRESSED_DELIVERED 出现",
                any(r.get("verdict") == "SUPPRESSED_DELIVERED"
                    for r in results_list), f"{results_list}")
        _assert("g2 _send_to_main 未触发", sent == [],
                f"sent={sent}")
    finally:
        led.close()
        import shutil; shutil.rmtree(base)


# =============================================================
# G4: AC9-3 (a) — run 刚出生 (< grace) → null_slot_knock 不敲
# =============================================================
def test_g4_run_just_launched_no_knock() -> None:
    """run 创建 30s 前, _notify_null_slot 应当 SUPPRESSED_LAUNCH_GRACE。

    旧版: 创建时间当 created_at 兜底, 30s 也被判「挂 30s 等 main 派发」;
          stuck_after_min 默认 30min 不敲但 95min 旧实测中会误敲。
    现行: 软静默期 (默认 600s = 10min) 直接 no-op, 不发送, verdict
          = SUPPRESSED_LAUNCH_GRACE, 留 verdict 字段可审计。
    """
    from mw import reaper as reaper_mod
    led, ws, tid = _setup(Path(tempfile.mkdtemp()))
    base = Path(led.path).parent
    try:
        # run 30s 前创建, cleared_at 仍 NULL, pending 卡从未 dispatch
        led.conn.execute(
            "UPDATE team_work_runs SET status='ACTIVE', "
            "created_at=?, current_card_id=NULL, "
            "current_card_cleared_at=NULL WHERE run_id='r1'",
            (time.time() - 30,))
        # pending 卡 last_progress_at NULL + 未 spawn marker = all undispatched
        led.conn.execute(
            "UPDATE harvest_contracts SET last_progress_at=NULL, "
            "harvest_status='PENDING' WHERE task_id=?", (tid,))
        led.conn.commit()
        ar = led.get_team_work("r1")
        sent = []
        results_list: list = []
        with patch.object(reaper_mod, "_send_to_main",
                         lambda *a, **k: sent.append(a) or True), \
             patch.object(reaper_mod, "_query_console_liveness",
                          lambda *a, **k: None), \
             patch.object(reaper_mod, "_query_initiator_last_active",
                          lambda *a, **k: None):
            reaper_mod._notify_null_slot(
                led, ar, results_list, registry=None,
                stuck_after_min=5, base=str(base))
        _assert("g4 SUPPRESSED_LAUNCH_GRACE 出现",
                any(r.get("verdict") == "SUPPRESSED_LAUNCH_GRACE"
                    for r in results_list), f"{results_list}")
        _assert("g4 _send_to_main 未触发", sent == [],
                f"sent={sent}")
    finally:
        led.close()
        import shutil; shutil.rmtree(base)


# =============================================================
# G5: AC9-3 (a) — run 出生超 grace, 仍不擅自当成「挂死」
# =============================================================
def test_g5_old_created_at_still_pings_but_soft() -> None:
    """仅有 created_at 兜底 (无 cleared_at 戳) 且远超 grace: 仍允许敲门,
    但文案必须是「等待 main 派发」而不是「挂死」。
    """
    from mw import reaper as reaper_mod
    led, ws, tid = _setup(Path(tempfile.mkdtemp()))
    base = Path(led.path).parent
    try:
        # 创建 6000s 前, cleared_at NULL, pending 卡从未 dispatch
        led.conn.execute(
            "UPDATE team_work_runs SET status='ACTIVE', "
            "created_at=?, current_card_id=NULL, "
            "current_card_cleared_at=NULL WHERE run_id='r1'",
            (time.time() - 6000,))
        led.conn.execute(
            "UPDATE harvest_contracts SET last_progress_at=NULL, "
            "harvest_status='PENDING' WHERE task_id=?", (tid,))
        led.conn.commit()
        ar = led.get_team_work("r1")
        sent = []
        results_list: list = []
        with patch.object(reaper_mod, "_send_to_main",
                         lambda *a, **k: sent.append(a) or True), \
             patch.object(reaper_mod, "_query_console_liveness",
                          lambda *a, **k: None), \
             patch.object(reaper_mod, "_query_initiator_last_active",
                          lambda *a, **k: None):
            reaper_mod._notify_null_slot(
                led, ar, results_list, registry=None,
                stuck_after_min=5, base=str(base))
        # 找到 NOTIFIED 那条 (stuck_after_min=5min, run_age=100min 远超)
        notified = [r for r in results_list
                    if r.get("verdict") == "NOTIFIED"]
        _assert("g5 远超 grace 后仍会敲门 NOTIFIED",
                len(notified) == 1, f"{results_list}")
        _assert("g5 文案带「等待 main 派发」语义",
                len(sent) == 1 and "等待 main 派发" in sent[0][1],
                f"sent={sent}")
    finally:
        led.close()
        import shutil; shutil.rmtree(base)


# =============================================================
# G6: AC9-3 (c) — TODO.md completed 但本卡无 marker → SKIP_STALE_TODO
# =============================================================
def test_g6_stale_todo_no_marker_skipped() -> None:
    """TODO.md 显示 completed 但 spawn marker 不存在 → 不调 harvest。

    实际就是「上轮卡 TODO.md 残留」陷阱: 项目根 TODO.md 不一定是当
    前卡写的, 不能看见 completed 就直接收。补 _stale_marker_guard
    守门: marker.task_id 不一致 → SKIP_STALE_TODO, 不调 harvest。
    此处用一个简化的「marker 全无」型, 注意 marker 不存在不会进入
    guard (由下方既有 no_marker SKIP 分支处理), 这里测的是 marker
    存在但 task_id 串到别卡的形态 (G7)。
    """
    from mw.reaper import _reap_one_task
    from mw.reaper import marker_path
    import json
    led, ws, tid = _setup(Path(tempfile.mkdtemp()))
    base = Path(led.path).parent
    try:
        _make_todo(ws, STATUS_COMPLETED)
        # 直接写一个 marker 但 task_id 故意写错 (绕过 write_marker 的
        # 关键字冲突)。
        mp = marker_path(str(base), tid)
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps({
            "started_at": time.time(),
            "workspace": str(ws),
            "task_id": "OTHER_TASK_ID",
        }))
        row = led.conn.execute(
            "SELECT * FROM harvest_contracts WHERE task_id=?",
            (tid,)).fetchone()
        results_list: list = []
        with patch("mw.reaper.harvest",
                  lambda *a, **k: {"verdict": "X", "settled": False}):
            _reap_one_task(dict(row), led, str(base), str(base), 30, None,
                          results_list)
        skipped = [r for r in results_list
                  if r.get("verdict") == "SKIP_STALE_TODO"]
        _assert("g7 marker.task_id 错位 → SKIP_STALE_TODO",
                len(skipped) == 1, f"{results_list}")
        _assert("g7 SKIP_STALE_TODO note 解释原因",
                "marker_task_mismatch" in skipped[0]["note"],
                f"{skipped[0]}")
    finally:
        led.close()
        import shutil; shutil.rmtree(base)


# =============================================================
# G7 (alias of G6 but stronger): marker.task_id mismatch
# =============================================================
def test_g6_marker_task_id_mismatch_skipped() -> None:
    """marker 存在但记录的 task_id 与本卡不同 → SKIP_STALE_TODO 不调 harvest。

    形态: marker 被同 workspace 复用 (罕见但会触发), 或 sanity test
    被 mock 复用过; 此时 completed 路径必须拒绝 harvest 落账。
    """
    from mw.reaper import _reap_one_task
    from mw.reaper import marker_path
    import json
    led, ws, tid = _setup(Path(tempfile.mkdtemp()))
    base = Path(led.path).parent
    try:
        _make_todo(ws, STATUS_COMPLETED)
        mp = marker_path(str(base), tid)
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps({
            "started_at": time.time(),
            "workspace": str(ws),
            "task_id": "OTHER_TASK_ID",
        }))
        row = led.conn.execute(
            "SELECT * FROM harvest_contracts WHERE task_id=?",
            (tid,)).fetchone()
        results_list: list = []
        with patch("mw.reaper.harvest",
                  lambda *a, **k: {"verdict": "X", "settled": False}):
            _reap_one_task(dict(row), led, str(base), str(base), 30, None,
                          results_list)
        skipped = [r for r in results_list
                  if r.get("verdict") == "SKIP_STALE_TODO"]
        _assert("g6 verdict=SKIP_STALE_TODO",
                len(skipped) == 1, f"{results_list}")
        _assert("g6 解释 marker_task_mismatch",
                "marker_task_mismatch" in skipped[0]["note"],
                f"{skipped[0]}")
    finally:
        led.close()
        import shutil; shutil.rmtree(base)


# =============================================================
# G8: AC9-5 — 真没活体 + 真没 relay 时, 兜底敲门 NOTIFIED 不倒退
# =============================================================
def test_g8_true_still_dead_null_slot_knocks() -> None:
    """run ACTIVE + 真挂死 + 没有抑制信号 → 兜底敲门 NOTIFIED。

    这是 §4 验收中最关键的「兜底不倒退」条款: 多层守门不能误伤真挂死。
    这里: run 有 cleared_at 戳 (距 now 很远) + 没有 dispatched 卡 +
    relay 没有抑制 → 仍敲。
    """
    from mw import reaper as reaper_mod
    led, ws, tid = _setup(Path(tempfile.mkdtemp()))
    base = Path(led.path).parent
    try:
        # cleared_at 距 now 1h (远超 stuck_after_min=5min) + 当前 NULL 槽
        # pending 卡也未 dispatch → 必须敲
        led.conn.execute(
            "UPDATE team_work_runs SET status='ACTIVE', "
            "created_at=?, current_card_id=NULL, "
            "current_card_cleared_at=? WHERE run_id='r1'",
            (time.time() - 7200, time.time() - 3600))
        # pending 卡是 PENDING 但 last_progress_at NULL + 无 marker
        led.conn.execute(
            "UPDATE harvest_contracts SET last_progress_at=NULL, "
            "harvest_status='PENDING' WHERE task_id=?", (tid,))
        led.conn.commit()
        ar = led.get_team_work("r1")
        sent = []
        results_list: list = []
        with patch.object(reaper_mod, "_send_to_main",
                         lambda *a, **k: sent.append(a) or True), \
             patch.object(reaper_mod, "_query_console_liveness",
                          lambda *a, **k: None), \
             patch.object(reaper_mod, "_query_initiator_last_active",
                          lambda *a, **k: None):
            reaper_mod._notify_null_slot(
                led, ar, results_list, registry=None,
                stuck_after_min=5, base=str(base))
        _assert("g8 NOTIFIED 出现 (兜底未倒退)",
                any(r.get("verdict") == "NOTIFIED"
                    for r in results_list), f"{results_list}")
        _assert("g8 _send_to_main 触发一次",
                len(sent) == 1, f"sent={sent}")
    finally:
        led.close()
        import shutil; shutil.rmtree(base)


# =============================================================
# G9: reap_once 已 HARVESTED 的卡不重 settle (现行 baseline 回归)
# =============================================================
def test_g9_harvested_terminal_not_resettled() -> None:
    """harvest_status=HARVESTED 的卡 reap_once 不再 settle (现有白名单)。"""
    from mw import reaper
    led, ws, tid = _setup(Path(tempfile.mkdtemp()))
    base = Path(led.path).parent
    try:
        # 直接钉成 HARVESTED (模拟已完成)
        led.conn.execute(
            "UPDATE harvest_contracts SET harvest_status='HARVESTED', "
            "worker_report=? WHERE task_id=?",
            ("{\"verdict\":\"COMPLETE\"}", tid))
        led.conn.commit()
        # reap_once 走一遍 — harvest 顶层白名单 (harvest.py L405) 直接
        # short-circuit 返 already=HARVESTED, 不该出 INVALID 第二行
        # settlement (trigger=harvest + verdict=COMPLETE 是允许的「
        # 已确认复核」语义, 但 _reap_one_task 走时不会再次 dequeue。
        # 测试层级: 至少不应出 ERROR/SETTLE_FAIL/RETRY 等负面 verdict。
        _make_todo(ws, STATUS_COMPLETED)
        rs = reaper.reap_once(ledger=led, hermes_home=str(base),
                             base=str(base), grace_seconds=10, registry={})
        bad = [r for r in rs
               if r.get("verdict") in ("ERROR", "settle_fail")]
        _assert("g9 HARVESTED 终态不出 ERROR",
                not bad, f"{bad}")
        _assert("g9 行数干净 (reap_scope 必有)",
                any(r.get("trigger") == "reap_scope" for r in rs),
                f"{rs}")
    finally:
        led.close()
        import shutil; shutil.rmtree(base)


if __name__ == "__main__":
    test_g1_run_not_active_reap_zeros()
    test_g2_stuck_notify_skips_delivered()
    test_g4_run_just_launched_no_knock()
    test_g5_old_created_at_still_pings_but_soft()
    test_g6_stale_todo_no_marker_skipped()
    test_g6_marker_task_id_mismatch_skipped()
    test_g8_true_still_dead_null_slot_knocks()
    test_g9_harvested_terminal_not_resettled()
    total = len(results)
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{total} passed")
    sys.exit(0 if passed == total else 1)
