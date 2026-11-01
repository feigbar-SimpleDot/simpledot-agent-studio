"""tests/test_issue167_delivery_observability.py — issue #167 回归测试。

派生 session_id 场景下 reaper 中继投递 404 导致「任务卡死且两边都不知道」。
本文件钉住修复后的三件事 (PLAN-M-20261030 §5.5):

  T1 (AC4 + AC1): 404 = 永久性失败 ⇒ 首次失败即死信 (notify_dead_until ≈
      now + 1 年) + emit relay.notify_permanent_failure (reason=
      session_not_found / http_status=404 / session_id / attempts /
      next_action); 且死信后再投一律入口闸门挡下 ⇒ 投递尝试次数有界。
  T2 (零回归): 临时失败 (5xx/网络异常) 仍走指数退避 (dead_until ≈ now+30s,
      不是 1 年); 429 仍不计死信计数 (attempts 不增)。
  T3 (AC2 主判据面): GET /api/tasks/<id> 详情端点返回 delivery 字典,
      delivery_failed / delivery_dead 一眼可读 (不依赖列表端点)。
  T4 (AC3): task.stuck 事件 payload 含 waiting_criteria (阈值/闲置时长/
      依据字段); WAITING 的 harvest_log reason 带判据参数。
  T5 (变体反证锚): 详情端点 delivery 缺 delivery_failed 注入 ⇒ 转红
      (键集合硬锚, 摘掉注入即断言失败)。

夹具纪律: 全部 tmp_path scratch db, 严禁写 /mw_data。
跑法: pytest tests/test_issue167_delivery_observability.py -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

from mw.ledger import Ledger  # noqa: E402
from mw import reaper as reaper_mod  # noqa: E402
from mw import console as console_mod  # noqa: E402

GW = "http://127.0.0.1:9"  # 不会有人接的端口


def _led(tmp_path) -> Ledger:
    return Ledger(str(tmp_path / "ledger.db"))


def _seed_card(led: Ledger, task_id: str) -> None:
    """建一张有中继目标的卡 (契约 + initiator)。"""
    led.put_contract(task_id, {"required_files": []})
    led.record_initiator(task_id, "s_main")


def _http_error(code: int) -> urllib.error.HTTPError:
    from email.message import Message
    return urllib.error.HTTPError(
        f"{GW}/api/sessions/s_main/chat", code, "Not Found" if code == 404
        else "Error", Message(), None)


def _send_with_error(led: Ledger, task_id: str, code: int) -> None:
    """让一次投递以指定 HTTP 状态码失败 (发送线程异步, 轮询等落账)。

    issue #171 (2026-10-31): 404 死信前新增网关路由探测 (GET /api/sessions,
    同走 urlopen)。本用例模拟的场景是「目标会话不存在」(网关活着、路由
    健康, 只有 chat POST 撞 404) ⇒ mock 按方法区分: GET 探测返回 200,
    POST 抛指定状态码。「网关整体 404」的退避分支由
    tests/test_issue_cluster_b_20261031.py K5 单独覆盖。
    """
    def boom(rq, timeout=None):
        if getattr(rq, "get_method", lambda: "POST")() == "GET":
            return _OkResp()  # 路由健康 ⇒ 探测判定为会话级 404
        raise _http_error(code)
    with mock.patch("urllib.request.urlopen", side_effect=boom):
        reaper_mod._send_to_main("s_main", "报告", GW, "k", led.path,
                                 notify_task=task_id)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            row = led.conn.execute(
                "SELECT notify_dead_until FROM harvest_contracts "
                "WHERE task_id=?", (task_id,)).fetchone()
            if row and row["notify_dead_until"]:
                break
            time.sleep(0.05)
        time.sleep(0.2)  # 等事件 emit 收尾


def _row(led: Ledger, task_id: str) -> dict:
    return dict(led.conn.execute(
        "SELECT * FROM harvest_contracts WHERE task_id=?",
        (task_id,)).fetchone())


# ── T1: 404 永久失败 ⇒ 立即死信 + 语义化事件 + 尝试次数有界 ─────────

def test_t1_404_immediate_dead_letter_and_semantic_event(tmp_path):
    """404 首败即死信 (dead_until ≈ now+1 年) + relay.notify_permanent_failure。

    旧行为: 404 与网络抖动同走指数退避, 8 次 (~2.7h) 才死信, 期间 main
    零信号 —— 派生 sid 投递 404 静默卡死的机制之一。
    """
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_167_404")
        before = time.time()
        _send_with_error(led, "t_167_404", 404)
        r = _row(led, "t_167_404")
        assert r["notify_attempts"] == 1, r
        assert r["notify_dead_until"] and \
            r["notify_dead_until"] > before + 86400 * 300, \
            f"404 未立即死信 (dead_until={r['notify_dead_until']}), " \
            f"退避路径被误改?"
        assert r["notify_last_error"] and "404" in r["notify_last_error"], r
        # 语义化事件: 可机读的「会话不存在」语义 + 人工处置指引
        evs = led.conn.execute(
            "SELECT payload FROM mw_events WHERE task_id=? "
            "AND event_type='relay.notify_permanent_failure'",
            ("t_167_404",)).fetchall()
        assert evs, "404 未落 relay.notify_permanent_failure 事件"
        p = json.loads(evs[0]["payload"])
        assert p["reason"] == "session_not_found", p
        assert p["http_status"] == 404, p
        assert p["session_id"] == "s_main", p
        assert p["attempts"] >= 1, p
        assert p["next_action"], p
        # 既有 relay.notify_failed 留痕同步在 (issue #103 通道不动)
        assert led.conn.execute(
            "SELECT 1 FROM mw_events WHERE task_id=? "
            "AND event_type='relay.notify_failed'",
            ("t_167_404",)).fetchone(), "既有失败留痕事件丢失"
    finally:
        led.close()


def test_t1b_404_dead_letter_bounds_further_attempts(tmp_path):
    """死信后再投 ⇒ 入口闸门直接挡下 (不再 POST) ⇒ 尝试次数有界 (AC4)。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_167_bound")
        _send_with_error(led, "t_167_bound", 404)
        posts: list = []
        # 模拟后续 reap 周期反复重投: 全部应被 notify_dead_until 闸门挡下
        with mock.patch("urllib.request.urlopen",
                        side_effect=lambda rq, timeout=None: posts.append(rq)
                        or _OkResp()):
            for _ in range(3):
                assert reaper_mod._send_to_main(
                    "s_main", "报告", GW, "k", led.path,
                    notify_task="t_167_bound") is False
        time.sleep(0.3)
        assert not posts, f"死信后仍发出 {len(posts)} 次 POST (次数无界)"
        r = _row(led, "t_167_bound")
        assert r["notify_attempts"] == 1, f"死信后 attempts 仍在涨: {r}"
    finally:
        led.close()


class _OkResp:
    status = 200

    def read(self):
        return b"{}"

    def getcode(self):
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ── T2: 临时失败 / 429 的既有语义逐字保留 ──────────────────────────

def test_t2_temporary_failure_keeps_exponential_backoff(tmp_path):
    """500 ⇒ 退避窗口 (≈ now+30s), **不是**立即死信 (MV-167-B 反证)。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_167_500")
        before = time.time()
        _send_with_error(led, "t_167_500", 500)
        r = _row(led, "t_167_500")
        assert r["notify_attempts"] == 1, r
        assert r["notify_dead_until"], r
        # 首次退避 = 30s; 允许宽松上界 (封顶被 #83 抬到 timeout+60) 但
        # 必须远小于 1 年 —— 把临时失败当永久失败的实现会在这里转红。
        assert r["notify_dead_until"] < before + 3600, \
            f"临时失败被立即死信 (dead_until={r['notify_dead_until']})"
        # 临时失败不得 emit 永久失败事件
        assert not led.conn.execute(
            "SELECT 1 FROM mw_events WHERE task_id=? "
            "AND event_type='relay.notify_permanent_failure'",
            ("t_167_500",)).fetchone()
    finally:
        led.close()


def test_t2b_429_backpressure_semantics_unchanged(tmp_path):
    """429 ⇒ dead_until ≈ now+30 且 attempts 不增 (MV-167-C 反证)。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_167_429")
        before = time.time()
        _send_with_error(led, "t_167_429", 429)
        r = _row(led, "t_167_429")
        assert (r["notify_attempts"] or 0) == 0, \
            f"429 被计入死信计数 (attempts={r['notify_attempts']})"
        assert r["notify_dead_until"] and \
            before < r["notify_dead_until"] < before + 120, \
            f"429 退避窗口异常: {r['notify_dead_until']}"
        assert not r["notify_last_error"], \
            "429 不应写 notify_last_error (背压非投递失败)"
    finally:
        led.close()


# ── T3: 详情端点 delivery 字段 (AC2 主判据面) ──────────────────────

def _mk_kanban(tmp_path: Path, task_id: str) -> None:
    """在 scratch kanban db 里造一张卡 (api_task_detail 依赖它)。"""
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


def test_t3_task_detail_exposes_delivery(tmp_path):
    """投递失败后 GET /api/tasks/<id> 一次操作直接读出 delivery_failed。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_167_detail")
        console_mod.LEDGER_DB = led.path
        console_mod.KANBAN_DB = str(tmp_path / "kanban.db")
        _mk_kanban(tmp_path, "t_167_detail")
        # 干净卡: delivery 字段在位且未失败
        t = console_mod.api_task_detail("t_167_detail")["task"]
        assert "delivery" in t, "详情端点缺 delivery 字段"
        assert t["delivery"]["delivery_failed"] is False, t["delivery"]
        assert t["delivery"]["delivery_dead"] is False, t["delivery"]
        # 模拟 404 永久失败后: 组合状态一眼可读
        _send_with_error(led, "t_167_detail", 404)
        led.conn.execute(
            "UPDATE harvest_contracts SET harvest_status='WAITING' "
            "WHERE task_id='t_167_detail'")
        led.conn.commit()
        t = console_mod.api_task_detail("t_167_detail")["task"]
        d = t["delivery"]
        assert t["harvest_status"] == "WAITING", t
        assert d["delivery_failed"] is True, d
        assert d["delivery_dead"] is True, d
        assert d["notify_attempts"] >= 1, d
        assert "404" in (d["notify_last_error"] or ""), d
        assert d["notify_last_failed_at"], d
    finally:
        led.close()


def test_t5_delivery_keys_hard_anchor(tmp_path):
    """变体反证锚 (MV-167-D): delivery 字典键集合硬锚。

    摘掉 delivery_failed / delivery_dead 任一注入 ⇒ 本用例转红 ——
    防止「只加了原始列没加派生组合状态」的半吊子实现蒙混过关。
    """
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_167_anchor")
        console_mod.LEDGER_DB = led.path
        console_mod.KANBAN_DB = str(tmp_path / "kanban.db")
        _mk_kanban(tmp_path, "t_167_anchor")
        d = console_mod.api_task_detail("t_167_anchor")["task"]["delivery"]
        assert set(d.keys()) == {
            "notify_attempts", "notify_confirmed_at", "notify_last_failed_at",
            "notify_last_error", "notify_dead_until", "last_notified_at",
            "delivery_failed", "delivery_dead",
        }, sorted(d.keys())
    finally:
        led.close()


# ── T4: WAITING 判据可查询 (AC3) ───────────────────────────────────

def _setup_stuck(tmp_path: Path):
    """stuck 敲门夹具: run + 卡 + session + initiator + 占槽 (同 issue#13)。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    led = Ledger(str(tmp_path / "ledger.db"))
    led.create_team_work_run("r167", "sid_main", "需求")
    led.put_contract("t167", {"required_files": []})
    led.register_team_work_card("r167", "t167")
    led.upsert_session("t167", "sid_w", "coder", "api", None,
                       workspace=str(ws))
    led.record_initiator("t167", "sid_main")
    led.set_team_work_current_card("r167", "t167")
    return led, ws


def test_t4_stuck_event_carries_waiting_criteria(tmp_path):
    """task.stuck payload 含 waiting_criteria 三要素 + WAITING reason 带判据。"""
    led, ws = _setup_stuck(tmp_path)
    base = Path(led.path).parent
    try:
        # 静默 40 分钟 (> 默认 30min 阈值)
        lp = time.time() - 40 * 60
        led.conn.execute(
            "UPDATE harvest_contracts SET last_progress_at=? "
            "WHERE task_id='t167'", (lp,))
        led.conn.commit()
        results: list[dict] = []
        with mock.patch("mw.reaper._send_to_main"), \
             mock.patch("mw.reaper._query_console_liveness",
                        return_value=None):
            reaper_mod._notify_main_of_stuck(
                led, base, results, registry=None,
                stuck_after_min=30, active_run_id="r167",
                current_card_id="t167")
        assert [r for r in results if r.get("verdict") == "NOTIFIED"], results
        ev = led.conn.execute(
            "SELECT payload FROM mw_events WHERE task_id='t167' "
            "AND event_type='task.stuck' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert ev, "未落 task.stuck 事件"
        p = json.loads(ev["payload"])
        wc = p.get("waiting_criteria")
        assert isinstance(wc, dict), f"task.stuck 缺 waiting_criteria: {p}"
        assert wc["threshold_minutes"] == 30, wc
        assert wc["idle_minutes"] >= 30, wc
        assert wc["last_progress_at"] == lp, wc
        assert wc["basis"], wc
        # harvest_log 的 WAITING reason 带判据参数 (阈值/闲置/依据字段)
        log = led.conn.execute(
            "SELECT harvest_log FROM harvest_contracts "
            "WHERE task_id='t167'").fetchone()["harvest_log"] or ""
        assert "SSE 静默超阈" in log, log
        assert "阈值 30 分钟" in log, log
        assert "last_progress_at=" in log, log
    finally:
        led.close()
