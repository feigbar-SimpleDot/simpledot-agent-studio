"""F5-R2 (2026-10-13, issue #5 回流修复, VERIFY-20261013 §4 D1/D2/D3)。

覆盖 (对齐 F5-R2 回流修复卡 AC 映射, 见 git log):
  R1-1 (D1) ledger 替身缺 update_worker_state → stuck 敲门路径不炸且照敲
             (hasattr 前置防御, 「真挂死 fail-open 不倒退」对任意替身成立)
  R1-2 (D1) 替身带 update_worker_state → 敲门前先标 WAITING (actor=reaper)
  R2-1 (D2) _workers_derived_states: ACTIVE run 下 worker→derive_worker_state
             映射; WAITING holder 显著度高于 PENDING
  R2-2 (D2) api_workers 给在册 worker 注入 derived_state (单一派生源)
  R2-3 (D2) 无 ACTIVE run → _workers_derived_states 空 dict (前端回落 liveness)
  R2-4 (D2) pixel_bridge 用 derive_run_overview: WAITING holder → blocked;
             真 Ledger WORKING holder → editing; StubLedger 缺方法 → 回落旧扫描
  R3-1 (D3) api_team_work_get 注入 card.appended: launch 基线卡 False,
             mid-run add_cards 追加卡 True; 全未开跑 → 全 False
前端消费同源性 (WorkersView workerStatus / TasksBoardView isAppended) 为
纯函数渲染层, 由源码 grep + dist 标记 + 浏览器截图实证 (F5-R2 卡 §5),
不在本文件重复起浏览器用例。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from unittest import mock

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mw.ledger import Ledger  # noqa: E402


# ── D1: reaper stuck 敲门对 ledger 替身的防御 ────────────────────

class _NoMethodLedger:
    """刻意缺 update_worker_state 的替身 (复现 VERIFY §2.3 崩溃现场)。"""

    def __init__(self, lpa):
        self._lpa = lpa
        self.path = ":memory:"

    def current_card_contract(self, run_id, card_id):
        return {"task_id": "T-1", "harvest_status": "PENDING",
                "last_progress_at": self._lpa, "updated_at": self._lpa}

    def should_notify(self, task_id, *, min_interval_s):
        return True

    def get_session(self, task_id):
        return None

    def get_initiator(self, task_id):
        return "sid_main"


class _WithMethodLedger(_NoMethodLedger):
    """带桩的替身: 记录 WAITING-arming 调用。"""

    def __init__(self, lpa):
        super().__init__(lpa)
        self.calls = []

    def update_worker_state(self, task_id, new_status, *, actor,
                            reason="", log=""):
        self.calls.append((task_id, new_status, actor))


def _run_stuck(ledger):
    from mw import reaper as reaper_mod
    from mw import sse_bridge
    sse_bridge._subscriptions.clear()
    sse_bridge._worker_live.clear()
    results = []
    with mock.patch.object(reaper_mod, "_query_console_liveness",
                           side_effect=ConnectionRefusedError("down")), \
         mock.patch.object(reaper_mod, "_send_to_main") as sent:
        reaper_mod._notify_main_of_stuck(
            ledger, "/nonexistent-base", results, registry=None,
            stuck_after_min=1, active_run_id="run_a",
            current_card_id="card_c")
    return results, sent


def test_r1_1_missing_update_worker_state_still_knocks():
    """D1: 替身缺方法不得 AttributeError 穿透打断敲门 (基线绿回归)。"""
    ledger = _NoMethodLedger(time.time() - 3600)
    _results, sent = _run_stuck(ledger)
    assert sent.call_count == 1, "缺方法替身仍必须完成敲门 (fail-open)"


def test_r1_2_with_method_marks_waiting_before_knock():
    """D1 正面: 有方法的 ledger 敲门前先标 WAITING (actor=reaper)。"""
    ledger = _WithMethodLedger(time.time() - 3600)
    _results, sent = _run_stuck(ledger)
    assert sent.call_count == 1
    assert ledger.calls and ledger.calls[0] == ("T-1", "WAITING", "reaper"), \
        ledger.calls


# ── D2: worker 面板 / pixel_bridge 单一派生源 ────────────────────

def _dual_db(tmp_path):
    kanban = tmp_path / "kanban.db"
    kc = sqlite3.connect(kanban)
    kc.executescript("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
            block_kind TEXT, result TEXT,
            created_at REAL, started_at REAL, completed_at REAL);
    """)
    kc.commit()
    kc.close()
    import mw.console as console
    old = (console.KANBAN_DB, console.LEDGER_DB)
    console.KANBAN_DB = str(kanban)
    led = Ledger(str(tmp_path / "ledger.db"))
    console.LEDGER_DB = led.path
    return led, console, old


def _seed(kanban_path, tid, assignee, created_at, started_at=None):
    c = sqlite3.connect(kanban_path)
    c.execute("INSERT INTO tasks(id,title,assignee,status,created_at,"
              "started_at) VALUES(?,?,?,?,?,?)",
              (tid, tid, assignee, "ready", created_at, started_at))
    c.commit()
    c.close()


def test_r2_1_workers_derived_states_mapping(tmp_path):
    """D2: worker→derive_worker_state 映射; WAITING 显著度覆盖 PENDING。

    FIX-15 升级: 返回 (out, active_run_id) 元组; out 形状不变,
    active_run_id 是当前 ACTIVE run_id 字符串 (无 ACTIVE run 时 None)。
    """
    led, console, old = _dual_db(tmp_path)
    try:
        now = time.time()
        led.create_team_work_run("r_d2", "s_main", "需求")
        led.put_contract("t_a", {"required_files": []})
        led.register_team_work_card("r_d2", "t_a")
        led.put_contract("t_b", {"required_files": []})
        led.register_team_work_card("r_d2", "t_b")
        _seed(console.KANBAN_DB, "t_a", "coder", now - 100)
        _seed(console.KANBAN_DB, "t_b", "coder", now - 100)
        led.set_team_work_current_card("r_d2", "t_a")
        # 未标 WAITING: 承接者 = WORKING
        out, run_id = console._workers_derived_states(led)
        assert out == {"coder": "WORKING"}
        assert run_id == "r_d2"
        # 标 WAITING 后: 显著度优先级取 WAITING (t_b PENDING 不冲掉)
        led.update_worker_state("t_a", "WAITING", actor="reaper")
        out, run_id = console._workers_derived_states(led)
        assert out == {"coder": "WAITING"}
        assert run_id == "r_d2"
    finally:
        console.KANBAN_DB, console.LEDGER_DB = old
        led.close()


def test_r2_2_api_workers_injects_derived_state(tmp_path):
    """D2: /api/workers 每条目附 derived_state (与 run 页同一派生源)。

    FIX-15 增项: 顶层 active_team_work_run_id 暴露 (FIX-15 让 UI 不再
    依赖 SSE live 决定 working); worker 名下无卡 → 不注入 derived_state
    (语义: 不在派生态, 由前端按 active_team_work_run_id 上下文降级 idle)。
    """
    led, console, old = _dual_db(tmp_path)
    reg_f = tmp_path / "registry.json"
    reg_f.write_text(json.dumps(
        {"coder": {"api_url": "http://127.0.0.1:1", "api_key": "k"},
         "tester": {"api_url": "http://127.0.0.1:1", "api_key": "k"}}))
    old_reg = console.REGISTRY
    console.REGISTRY = str(reg_f)
    try:
        now = time.time()
        led.create_team_work_run("r_d2b", "s_main", "需求")
        led.put_contract("t_a", {"required_files": []})
        led.register_team_work_card("r_d2b", "t_a")
        _seed(console.KANBAN_DB, "t_a", "coder", now - 50)
        led.set_team_work_current_card("r_d2b", "t_a")
        d = console.api_workers()
        assert d["workers"]["coder"].get("derived_state") == "WORKING"
        # tester 名下无卡 → 不注入, 前端回落 active_team_work_run_id 上下文
        assert "derived_state" not in d["workers"]["tester"]
        # FIX-15: 顶层 active_team_work_run_id 暴露 run_id (非 None)
        assert d.get("active_team_work_run_id") == "r_d2b"
        # 原有字段保持
        assert "active_run_ids" in d
    finally:
        console.REGISTRY = old_reg
        console.KANBAN_DB, console.LEDGER_DB = old
        led.close()


def test_r2_3_no_active_run_no_injection(tmp_path):
    """D2: 无 ACTIVE run → 空映射 (worker 面板按 active_team_work_run_id=None
    全部降级 idle, 不再依赖 SSE live 决定 working; FIX-15)。

    旧契约: 返回 {} (dict)。新契约: 返回 ({}, None), 同时 api_workers
    顶层 active_team_work_run_id = None。
    """
    led, console, old = _dual_db(tmp_path)
    try:
        out, run_id = console._workers_derived_states(led)
        assert out == {}
        assert run_id is None
    finally:
        console.KANBAN_DB, console.LEDGER_DB = old
        led.close()


def _pixel_sync(ledger, tmp_path, hermes_home=""):
    import mw.pixel_bridge as pb
    import mw.pixel_proxy as pp
    events = []
    pp.pixel_office_token = lambda: "tok"
    pp.pixel_office_running = lambda: True
    old_post = pb._post_event
    pb._post_event = lambda token, ev: (
        events.append((ev.get("session_id"), ev.get("hook_event_name"))),
        True)[-1]
    pb._last_scene.clear()
    pb._online.clear()
    pb._last_token = None
    try:
        pb.sync_from_ledger(ledger, {"coder"}, hermes_home)
    finally:
        pb._post_event = old_post
    return [n for s, n in events if s == "coder"]


def test_r2_4_pixel_bridge_uses_derive_run_overview(tmp_path):
    """D2: pixel_bridge 场景判定走 derive_run_overview 单一源。"""
    led, console, old = _dual_db(tmp_path)
    try:
        now = time.time()
        # kanban 提供 assignee (真 harvest_contracts 无该列,
        # _active_card 回落 _kanban_card 同源兜底)
        hh = tmp_path / "home"
        hh.mkdir()
        kc = sqlite3.connect(hh / "kanban.db")
        kc.executescript("CREATE TABLE tasks (id TEXT PRIMARY KEY,"
                         "assignee TEXT, title TEXT);")
        kc.execute("INSERT INTO tasks VALUES('t_a','coder','干活')")
        kc.commit()
        kc.close()
        led.create_team_work_run("r_px", "s_main", "需求")
        led.put_contract("t_a", {"required_files": []})
        led.register_team_work_card("r_px", "t_a")
        led.set_team_work_current_card("r_px", "t_a")
        # WORKING holder → editing (PreToolUse)
        names = _pixel_sync(led, tmp_path, str(hh))
        assert "PreToolUse" in names, names
        # WAITING holder → 举手 blocked (PermissionRequest, 非 editing)
        led.update_worker_state("t_a", "WAITING", actor="reaper")
        names = _pixel_sync(led, tmp_path, str(hh))
        assert "PermissionRequest" in names, names
    finally:
        console.KANBAN_DB, console.LEDGER_DB = old
        led.close()


def test_r2_5_pixel_bridge_stub_ledger_falls_back(tmp_path):
    """D2 防御: 替身缺 derive_run_overview → 回落旧扫描 (不过宽防御)。"""
    class Stub:
        def __init__(self, conn):
            self.conn = conn
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE team_work_runs(
            run_id TEXT PRIMARY KEY, status TEXT, current_card_id TEXT);
        CREATE TABLE harvest_contracts(
            card_id TEXT, assignee TEXT, title TEXT, harvest_status TEXT);
        CREATE TABLE mw_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT, task_id TEXT, payload TEXT, created_at REAL);
    """)
    con.execute("INSERT INTO team_work_runs VALUES('r1','ACTIVE','tA')")
    con.execute("INSERT INTO harvest_contracts "
                "VALUES('tA','coder','X','PENDING')")
    con.commit()
    names = _pixel_sync(Stub(con), None, "")
    # 旧路径: assignee=coder 承接 → editing
    assert "PreToolUse" in names, names


# ── D3: 「补」角标 server 侧权威判定 ─────────────────────────────

def test_r3_1_appended_flag(tmp_path):
    """D3: launch 基线卡 appended=False; mid-run 追加卡 appended=True。"""
    led, console, old = _dual_db(tmp_path)
    try:
        t0 = time.time()
        led.create_team_work_run("r_d3", "s_main", "需求")
        for tid, started in (("t1", t0 + 10), ("t2", None)):
            led.put_contract(tid, {"required_files": []})
            led.register_team_work_card("r_d3", tid)
            _seed(console.KANBAN_DB, tid, "coder", t0, started)
        # t3 = launch 之后 add_cards 追加 (created_at 晚于最早 started_at)
        led.put_contract("t3", {"required_files": []})
        led.register_team_work_card("r_d3", "t3")
        _seed(console.KANBAN_DB, "t3", "coder", t0 + 20, t0 + 30)
        out = console.api_team_work_get("r_d3")
        flags = {c["id"]: c.get("appended") for c in out["cards"]}
        assert flags == {"t1": False, "t2": False, "t3": True}, flags
        # 执行序: t1 (started 最早) → t3 (started 次之) → t2 (未执行, seq 尾)
        assert [c["id"] for c in out["cards"]] == ["t1", "t3", "t2"]
        assert [c["exec_index"] for c in out["cards"]] == [1, 2, 3]
    finally:
        console.KANBAN_DB, console.LEDGER_DB = old
        led.close()


def test_r3_2_no_execution_no_appended(tmp_path):
    """D3: run 全未开跑 → 无追加判定 (min_started 不存在全 False)。"""
    led, console, old = _dual_db(tmp_path)
    try:
        t0 = time.time()
        led.create_team_work_run("r_d3b", "s_main", "需求")
        led.put_contract("t1", {"required_files": []})
        led.register_team_work_card("r_d3b", "t1")
        _seed(console.KANBAN_DB, "t1", "coder", t0)
        out = console.api_team_work_get("r_d3b")
        assert all(c.get("appended") is False for c in out["cards"])
    finally:
        console.KANBAN_DB, console.LEDGER_DB = old
        led.close()
