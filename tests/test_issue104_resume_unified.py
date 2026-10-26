"""tests/test_issue104_resume_unified.py — issue #104 回归测试。

覆盖 issue #104 的 4 处根因 (逐条对应「期望修复」):

  根因 1 (console resume 不进账本、零簿记):
    A1  console resume 确实入队 task_resume_requests (不再直接调
        pipeline.resume_task) —— CLI 与 console 共用同一张表。
    A2  task_resume_requests 表有 actor 列, 区分 main / operator。
    A3  旧库 (无 actor 列) 自动迁移 (ADD COLUMN 幂等)。

  根因 2 (resume 注入后状态机不动作且静默):
    B1  state_reset='done' 落 harvest_log + emit worker.state 事件。
    B2  state_reset='skipped:...' 也落账 (不只塞返回 dict)。

  根因 3 (双答复通道互相不可见):
    C1  答复后落一条带 actor 的 resume.answered 事件 + harvest_log 痕迹。
    C2  后到的答复方看到冲突提示 (不是盲注)。
    C3  卡片详情端点带出「已被 X 于 HH:MM 答复」。

  根因 4 (归档不停 worker + resume 不校验终态):
    D1  api_resume 对终态卡 (ARCHIVED/CANCELLED/HARVESTED) 拒绝 (409)。
    D2  api_task_archive 联动 _kill_spawn_bg 停掉 spawn wrapper。

夹具纪律: 全部 tmp_path scratch db, 严禁写 /mw_data。
跑法: pytest tests/test_issue104_resume_unified.py -v
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

from mw.ledger import Ledger  # noqa: E402
from mw import console as console_mod  # noqa: E402
from mw import pipeline as pipeline_mod  # noqa: E402


def _led(tmp_path) -> Ledger:
    return Ledger(str(tmp_path / "ledger.db"))


def _seed_card(led: Ledger, task_id: str, workspace: Path,
               status: str = "NEED_CLARIFICATION") -> None:
    """建一张带 session 锚点的卡 (session 锚点是 resume 的前提)。"""
    led.put_contract(task_id, {"required_files": []})
    if status != "PENDING":
        led.update_worker_state(task_id, status, actor="harvest")
    led.upsert_session(task_id, f"sess-{task_id}", "coder", "api",
                       workspace=str(workspace))
    led.record_initiator(task_id, "s_main")


def _events(led: Ledger, task_id: str, event_type: str | None = None):
    rows = led.conn.execute(
        "SELECT event_type, payload FROM mw_events WHERE task_id=? "
        "ORDER BY id", (task_id,)).fetchall()
    out = []
    for r in rows:
        if event_type and r["event_type"] != event_type:
            continue
        out.append((r["event_type"], json.loads(r["payload"] or "{}")))
    return out


# ── A: console resume 入队 (根因 1) ──────────────────────────────

def test_a1_console_resume_enqueues_request(tmp_path):
    """console 人工答复必须写进 task_resume_requests (reaper 消费同一队列)。

    旧版 api_resume 直接起后台线程调 pipeline.resume_task —— 账本上
    这张卡 0 行 (issue #104 实案 t_a8052713 实测), reaper 完全看不见。
    """
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_a1", tmp_path)
        console_mod.LEDGER_DB = led.path
        # 守门: console 路径不得直接调 pipeline.resume_task (那是旧版
        # 「不入队」的形状); 一旦回退到后台线程直调, 本断言立刻转红。
        with mock.patch.object(pipeline_mod, "resume_task",
                               side_effect=AssertionError(
                                   "console 路径不得直接调 resume_task "
                                   "(必须入队由 reaper 消费)")) as _direct:
            out = console_mod.api_resume("t_a1", "选 B", actor="operator")
        assert _direct.call_count == 0, "console resume 走了旧的直调路径"
        assert out["ok"] is True, out
        assert out["actor"] == "operator", out
        assert "request_id" in out, out
        # 队列里必须有这一行, 且带 actor=operator
        rows = led.conn.execute(
            "SELECT task_id, instruction, status, actor "
            "FROM task_resume_requests WHERE task_id=?", ("t_a1",)).fetchall()
        assert len(rows) == 1, f"console resume 未入队: {rows}"
        assert rows[0]["status"] == "PENDING", dict(rows[0])
        assert rows[0]["actor"] == "operator", dict(rows[0])
        assert rows[0]["instruction"] == "选 B", dict(rows[0])
    finally:
        led.close()


def test_a2_cli_and_console_share_one_queue(tmp_path):
    """CLI (main) 与 console (operator) 两条通道共用同一张表 + actor 列。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_a2", tmp_path)
        # CLI 路径 (main)
        rid1 = led.enqueue_resume("t_a2", "main 的答复", actor="main")
        # console 路径 (operator)
        rid2 = led.enqueue_resume("t_a2", "operator 的答复", actor="operator")
        assert rid1 != rid2
        rows = led.conn.execute(
            "SELECT id, actor FROM task_resume_requests WHERE task_id=? "
            "ORDER BY id", ("t_a2",)).fetchall()
        # 同 task 的 PENDING 被后一条覆盖 (enqueue_resume 既有语义)
        assert len(rows) == 1, [dict(r) for r in rows]
        assert rows[0]["actor"] == "operator", dict(rows[0])
    finally:
        led.close()


def test_a3_actor_column_migration_is_idempotent(tmp_path):
    """旧库 (无 actor 列) 自动迁移, 且重复打开不报错 (幂等)。"""
    import sqlite3
    db = str(tmp_path / "ledger.db")
    # 手工造一个「旧版」task_resume_requests 表 (无 actor 列)
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE task_resume_requests ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT NOT NULL, instruction TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'PENDING',"
        " requested_at REAL NOT NULL, finished_at REAL,"
        " result_json TEXT, error TEXT);")
    con.commit()
    con.close()
    # 第一次打开 → 迁移加列
    led = Ledger(db)
    cols = [r["name"] for r in led.conn.execute(
        "PRAGMA table_info(task_resume_requests)")]
    assert "actor" in cols, cols
    led.close()
    # 第二次打开 → 幂等, 列集合不变
    led2 = Ledger(db)
    cols2 = [r["name"] for r in led2.conn.execute(
        "PRAGMA table_info(task_resume_requests)")]
    assert cols == cols2, (cols, cols2)
    # 迁移后的表仍能入队
    rid = led2.enqueue_resume("t_old", "老库答复", actor="operator")
    assert rid > 0
    row = led2.conn.execute(
        "SELECT actor FROM task_resume_requests WHERE id=?", (rid,)).fetchone()
    assert row["actor"] == "operator", dict(row)
    led2.close()


# ── B: state_reset 落账 (根因 2) ────────────────────────────────

class _FakeResp:
    """假 resume API: 受理并回同 session id。"""

    def __init__(self, sid):
        self.headers = {"X-Hermes-Session-Id": sid}

    def read(self):
        return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_b1_state_reset_done_lands_in_ledger(tmp_path):
    """成功回拨时 state_reset 落 harvest_log + emit worker.state。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_b1", tmp_path, status="NEED_CLARIFICATION")
        with mock.patch("urllib.request.urlopen",
                        return_value=_FakeResp("sess-t_b1")):
            r = pipeline_mod.resume_task("t_b1", ledger=led,
                                         instruction="继续", timeout=5)
        assert r["state_reset"] == "done", r
        log = led.get_contract("t_b1")["harvest_log"] or ""
        assert "state_reset=done" in log, log
        evs = _events(led, "t_b1", "worker.state")
        assert any("state_reset=done" in (p.get("reason") or "")
                   for _, p in evs), evs
    finally:
        led.close()


def test_b2_state_reset_skipped_lands_in_ledger(tmp_path):
    """跳过路径 (already_pending) 同样落账 —— 旧版只塞返回 dict, 无人知晓。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_b2", tmp_path, status="PENDING")
        with mock.patch("urllib.request.urlopen",
                        return_value=_FakeResp("sess-t_b2")):
            r = pipeline_mod.resume_task("t_b2", ledger=led,
                                         instruction="继续", timeout=5)
        assert r["state_reset"] == "skipped:already_pending", r
        log = led.get_contract("t_b2")["harvest_log"] or ""
        assert "state_reset=skipped:already_pending" in log, log
        evs = _events(led, "t_b2", "worker.state")
        assert any("state_reset=skipped" in (p.get("reason") or "")
                   for _, p in evs), evs
    finally:
        led.close()


def test_b3_state_reset_trace_records_actor(tmp_path):
    """痕迹上带答复来源 (answered_by) —— 双通道互读的前提。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_b3", tmp_path, status="NEED_CLARIFICATION")
        with mock.patch("urllib.request.urlopen",
                        return_value=_FakeResp("sess-t_b3")):
            pipeline_mod.resume_task("t_b3", ledger=led, instruction="继续",
                                     timeout=5, resume_actor="operator")
        log = led.get_contract("t_b3")["harvest_log"] or ""
        assert "answered_by=operator" in log, log
    finally:
        led.close()


# ── C: 双答复通道互相可见 (根因 3) ──────────────────────────────

def test_c1_answer_records_actor_trace(tmp_path):
    """答复后落 resume.answered 事件 (带 actor) + harvest_log 痕迹。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_c1", tmp_path)
        stamp = led.record_resume_answer("t_c1", "operator", "选 B")
        assert stamp["actor"] == "operator"
        assert stamp["answered_at_hhmm"], stamp
        log = led.get_contract("t_c1")["harvest_log"] or ""
        assert "actor=operator" in log, log
        evs = _events(led, "t_c1", "resume.answered")
        assert len(evs) == 1, evs
        assert evs[0][1]["actor"] == "operator", evs
        assert evs[0][1]["answered_at_hhmm"], evs
    finally:
        led.close()


def test_c2_second_answer_gets_conflict_hint(tmp_path):
    """后到的答复方看到冲突提示 (已被 X 于 HH:MM 答复), 不是盲注。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_c2", tmp_path)
        console_mod.LEDGER_DB = led.path
        # 第一方: main 走 CLI
        led.enqueue_resume("t_c2", "A", actor="main")
        led.record_resume_answer("t_c2", "main", "A")
        # 第二方: operator 走 console → 应带 conflict
        out = console_mod.api_resume("t_c2", "B", actor="operator")
        assert out["ok"] is True, out
        assert "conflict" in out, out
        assert "main" in out["conflict"], out
        assert "答复" in out["conflict"], out
        # 同一方的重复答复不报冲突 (自己看自己不算冲突)
        out2 = console_mod.api_resume("t_c2", "C", actor="operator")
        assert "conflict" not in out2, out2
    finally:
        led.close()


def test_c3_task_detail_exposes_answered_by(tmp_path):
    """卡片详情端点带出「已被 X 于 HH:MM 答复」。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_c3", tmp_path)
        console_mod.LEDGER_DB = led.path
        # 起一张 kanban 行 (api_task_detail 从 kanban 表取主体)
        console_mod.KANBAN_DB = str(tmp_path / "kanban.db")
        _mk_kanban(tmp_path, "t_c3")
        console_mod.api_resume("t_c3", "选 B", actor="operator")
        d = console_mod.api_task_detail("t_c3")
        t = d["task"]
        assert t["answered_by"] == "operator", t
        assert t["answered_at_hhmm"], t
        assert t["answered_at"], t
    finally:
        led.close()


def _mk_kanban(tmp_path: Path, task_id: str) -> None:
    """在 scratch kanban db 里造一张卡 (api_task_detail 依赖它)。"""
    import sqlite3
    db = str(tmp_path / "kanban.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE IF NOT EXISTS tasks ("
        " id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,"
        " block_kind TEXT, result TEXT, created_at REAL,"
        " started_at REAL, completed_at REAL);")
    con.execute(
        "INSERT OR REPLACE INTO tasks (id,title,assignee,status,result,"
        "created_at) VALUES (?,?,?,?,?,?)",
        (task_id, "标题", "coder", "running", "", time.time()))
    con.commit()
    con.close()


# ── D: 终态守卫 + archive 联动杀进程 (根因 4) ───────────────────

def test_d1_resume_rejects_terminal_cards(tmp_path):
    """已终态的卡 (ARCHIVED/CANCELLED/HARVESTED) resume 直接拒 (409)。"""
    led = _led(tmp_path)
    try:
        console_mod.LEDGER_DB = led.path
        for term in ("ARCHIVED", "CANCELLED", "HARVESTED"):
            tid = f"t_d1_{term.lower()}"
            _seed_card(led, tid, tmp_path, status="PENDING")
            led.update_worker_state(tid, term, actor="operator"
                                    if term != "HARVESTED" else "harvest")
            out = console_mod.api_resume(tid, "还想继续", actor="operator")
            assert out.get("code") == 409, (term, out)
            assert out.get("ok") is False, (term, out)
            # 队列里不得出现这行
            n = led.conn.execute(
                "SELECT COUNT(*) FROM task_resume_requests WHERE task_id=?",
                (tid,)).fetchone()[0]
            assert n == 0, f"{term} 终态卡仍被入队: {n}"
    finally:
        led.close()


def test_d2_archive_kills_spawn_wrapper(tmp_path):
    """archive 路径必须联动 _kill_spawn_bg 停掉 spawn wrapper。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_d2", tmp_path, status="PENDING")
        console_mod.LEDGER_DB = led.path
        with mock.patch.object(console_mod, "_kill_spawn_bg",
                               return_value=2) as _kill:
            out = console_mod.api_task_archive("t_d2")
        assert _kill.call_count == 1, "archive 未联动 _kill_spawn_bg"
        assert _kill.call_args[0][0] == "t_d2", _kill.call_args
        assert out["ok"] is True, out
        assert out.get("killed_procs") == 2, out
        assert led.get_contract("t_d2")["harvest_status"] == "ARCHIVED"
    finally:
        led.close()


def test_d3_cancel_still_kills_spawn_wrapper(tmp_path):
    """cancel 路径的既有语义不得回归 (仍杀 spawn wrapper + 标终态)。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_d3", tmp_path, status="PENDING")
        console_mod.LEDGER_DB = led.path
        with mock.patch.object(console_mod, "_kill_spawn_bg",
                               return_value=1) as _kill:
            out = console_mod.api_task_cancel("t_d3")
        assert _kill.call_count == 1
        assert out["ok"] is True, out
        assert led.get_contract("t_d3")["harvest_status"] == "CANCELLED"
    finally:
        led.close()


# ── E: s6fix P1-1 执行点终态守卫 (根因 4 收口补全) ───────────────
#
# D1 只挡了 console API 入口; 本节的三条执行点守卫各自独立成防:
#   E1  reaper 消费循环: 入队后卡被归档 → 消费点拒绝, 不调 resume_task,
#       队列行走 finish_resume 收口 (ERROR, 不留 PROCESSING 孤儿)。
#   E2  pipeline.resume_task 直调: POST 之前拒绝, 零注入, 落痕。
#   E3  cli cmd_resume: 终态卡不入队, 退出码非 0, 落痕。
# 变异自证: 摘掉任一守卫 → 对应用例转红 (E1 见 resume_task 被调,
# E2 见 urlopen POST 发生, E3 见队列入队)。

def test_e1_reaper_consumer_refuses_terminal_card(tmp_path):
    """已入队的答复在卡进终态后被 reaper 消费点拒绝, 不注入唤醒。"""
    from mw.reaper import reap_once
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_e1", tmp_path)
        # 先入队 (答复时卡还活着), 再把卡归档 —— 复现实案时间差。
        led.enqueue_resume("t_e1", "选 B", actor="operator")
        led.update_worker_state("t_e1", "ARCHIVED", actor="operator")
        # 注入入口全程 mock 成计数器: 守卫在位时 resume_task 必须零调用。
        with mock.patch.object(pipeline_mod, "resume_task",
                               return_value={"ok": True}) as _inj:
            reap_once(ledger=led, hermes_home=str(tmp_path),
                      base=str(tmp_path))
        assert _inj.call_count == 0, "终态卡的排队答复仍被 reaper 注入了"
        # 队列行走正规 finish_resume 收口: ERROR + 'terminal card'。
        r = led.conn.execute(
            "SELECT status, error, finished_at FROM task_resume_requests "
            "WHERE task_id='t_e1'").fetchone()
        assert r["status"] == "ERROR", dict(r)
        assert "terminal card" in (r["error"] or ""), dict(r)
        assert r["finished_at"] is not None, "未走 finish_resume, 留了孤儿行"
        # 账本痕迹: worker.state 事件带 resume_rejected。
        evs = _events(led, "t_e1", "worker.state")
        assert any(p.get("resume_rejected") == "terminal" for _, p in evs), evs
    finally:
        led.close()


def test_e2_resume_task_refuses_terminal_before_post(tmp_path):
    """resume_task 直调在注入 POST 之前拒绝终态卡, 并落可追溯痕迹。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_e2", tmp_path, status="PENDING")
        led.update_worker_state("t_e2", "HARVESTED", actor="harvest")
        with mock.patch("urllib.request.urlopen",
                        return_value=_FakeResp("sess-t_e2")) as _post:
            out = pipeline_mod.resume_task("t_e2", ledger=led,
                                           instruction="继续", timeout=5)
        assert out["ok"] is False, out
        assert "terminal" in out["reason"], out
        assert _post.call_count == 0, "守卫摘除: 注入 POST 已发出"
        log = led.get_contract("t_e2")["harvest_log"] or ""
        assert "[resume-terminal]" in log, log
        evs = _events(led, "t_e2", "worker.state")
        assert any(p.get("resume_rejected") == "terminal" for _, p in evs), evs
    finally:
        led.close()


def test_e3_cli_resume_refuses_terminal_no_enqueue(tmp_path, monkeypatch):
    """CLI resume 对终态卡拒绝入队 (退出码非 0), 队列不留待引爆的行。"""
    import argparse
    from mw import cli as cli_mod
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_e3", tmp_path, status="PENDING")
        led.update_worker_state("t_e3", "CANCELLED", actor="operator")
        monkeypatch.setattr(cli_mod, "_ledger", lambda: led)
        args = argparse.Namespace(task="t_e3", instruction="继续",
                                  instruction_file="")
        rc = cli_mod.cmd_resume(args)
        assert rc != 0, "终态卡 CLI resume 竟然成功"
        n = led.conn.execute(
            "SELECT COUNT(*) FROM task_resume_requests "
            "WHERE task_id='t_e3'").fetchone()[0]
        assert n == 0, "终态卡答复仍被入队"
        log = led.get_contract("t_e3")["harvest_log"] or ""
        assert "[resume-terminal]" in log, log
    finally:
        led.close()
