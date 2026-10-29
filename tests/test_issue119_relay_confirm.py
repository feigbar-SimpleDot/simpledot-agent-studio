"""issue #119 回归锁: main 侧显式回执通道 POST /api/relay/confirm。

覆盖 AC-119-1..6 (docs/REQUIREMENTS-J-20261029.md §2.4):
  T1 = AC-119-1 回执通道可调用: 200+ok:true; 不存在⇒404; 缺参⇒400
  T2 = AC-119-2 确认位置位: 回执前 NULL ⇒ 回执后正数时间戳
  T3 = AC-119-3 死信恢复: attempts=8/dead_until=+1年 形态, 回执后恰为 0|-1
  T4 = AC-119-5 幂等: 连续两次均 200, 第二次后确认位仍非空
  T5 = AC-119-6 隔离: 回执 A ⇒ 同库卡 B 三字段逐字不变
  T6 = AC-119-4③ 回归锁: 未回执卡在 relay 扫描/precheck 口径下仍命中, 字段原样

测试环境隔离 (AC-119-8): 全部走 Ledger(tmp 临时 sqlite), 不碰生产账本;
断言落在**行为** (sqlite 字段实效) 上, 不是落在「端点返回 200」上 ——
恒绿桩 (端点回 ok 但不写账本) 会让 T2/T3/T4/T5 全红。

跑法: pytest tests/test_issue119_relay_confirm.py
"""
import json
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mw.console as console
from mw.ledger import Ledger

# reaper.py:852 的 relay 扫描口径 (回归锁用, 逐字对齐其过滤条件)
RELAY_SCAN_SQL = (
    "SELECT hc.task_id FROM harvest_contracts hc "
    "LEFT JOIN team_work_runs twr ON hc.team_work_run_id = twr.run_id "
    "WHERE hc.harvest_status IN ('HARVESTED','NEED_CLARIFICATION','BLOCKED') "
    "AND hc.worker_report IS NOT NULL AND hc.notify_confirmed_at IS NULL "
    "AND (hc.team_work_run_id IS NULL OR twr.status = 'ACTIVE')"
)


def make_ledger(tmp_path, name="ledger.db"):
    """临时账本 (AC-119-8: 不写生产账本)。"""
    return Ledger(str(tmp_path / name))


def seed_card(led, task_id):
    """造一张「已结算待投递」卡: HARVESTED + 有报告 + 未确认。

    坑 (ledger.py:556 注释): put_contract 是 INSERT OR REPLACE 整行替换,
    任何预置的 notify_* 字段必须在 put_contract **之后** UPDATE, 否则被抹平。
    """
    led.put_contract(task_id, {"required_files": ["x.md"]})
    led.set_worker_report(task_id, {"verdict": "COMPLETE", "evidence": ["a"]})
    led.set_harvest(task_id, "HARVESTED", 0, "ok")


def seed_dead_letter(led, task_id):
    """预置死信形态 (TRIAGE §3): attempts=8, dead_until=now+1年, confirmed=NULL。"""
    seed_card(led, task_id)
    led.conn.execute(
        "UPDATE harvest_contracts SET notify_attempts=8, notify_dead_until=?, "
        "notify_last_failed_at=?, notify_last_error='boom', "
        "notify_confirmed_at=NULL WHERE task_id=?",
        (1600000000.0 + 31536000, 1600000000.0, task_id))
    led.conn.commit()


def field(led, task_id, col):
    return led.conn.execute(
        f"SELECT {col} FROM harvest_contracts WHERE task_id=?",
        (task_id,)).fetchone()[0]


def sqlite3_cli_shape(db_path, task_id):
    """按 AC 验收口径用独立 sqlite 连接读字段 (模拟 sqlite3 CLI 查询)。"""
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT notify_attempts, COALESCE(notify_dead_until,-1) "
            "FROM harvest_contracts WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()


# ── 端点层 (console.LEDGER_DB 注入临时账本, 既有测试惯例) ────────────

def test_T1_endpoint_shape(tmp_path, monkeypatch):
    """AC-119-1: 命中⇒ok:true; 不存在⇒404; 缺参⇒400, 错误形态可区分。"""
    led = make_ledger(tmp_path)
    monkeypatch.setattr(console, "LEDGER_DB", led.path)
    seed_card(led, "t_119a")
    r = console.api_relay_confirm({"task_id": "t_119a"})
    assert r["ok"] is True and r["task_id"] == "t_119a"
    assert isinstance(r["confirmed_at"], float) and r["confirmed_at"] > 0
    # 不存在的卡 ⇒ 404 (与内部错误 500 可区分)
    r404 = console.api_relay_confirm({"task_id": "t_119_missing"})
    assert r404["code"] == 404 and r404["ok"] is False
    assert r404["error"] == "task not found"
    # 缺 task_id ⇒ 400
    r400 = console.api_relay_confirm({})
    assert r400["code"] == 400 and r400["ok"] is False
    assert r400["error"] == "task_id required"
    led.close()


def test_T2_confirm_flag(tmp_path, monkeypatch):
    """AC-119-2: 回执前 confirmed_at IS NULL ⇒ 回执后为正数时间戳。"""
    led = make_ledger(tmp_path)
    monkeypatch.setattr(console, "LEDGER_DB", led.path)
    seed_card(led, "t_119b")
    assert field(led, "t_119b", "notify_confirmed_at") is None
    r = console.api_relay_confirm({"task_id": "t_119b"})
    assert r["ok"] is True
    after = field(led, "t_119b", "notify_confirmed_at")
    assert after is not None and after > 0
    assert r["confirmed_at"] == after
    led.close()


def test_T3_dead_letter_recovery(tmp_path, monkeypatch):
    """AC-119-3: 死信形态回执后 sqlite 查询输出恰为 (0, -1)。"""
    led = make_ledger(tmp_path)
    monkeypatch.setattr(console, "LEDGER_DB", led.path)
    seed_dead_letter(led, "t_119dead")
    # 预置形态自检: attempts=8, dead_until 非空, confirmed NULL
    assert sqlite3_cli_shape(led.path, "t_119dead") == (
        8, 1600000000.0 + 31536000)
    assert field(led, "t_119dead", "notify_confirmed_at") is None
    r = console.api_relay_confirm({"task_id": "t_119dead"})
    assert r["ok"] is True
    # 验收口径: notify_attempts=0 且 notify_dead_until 清空 (COALESCE ⇒ -1)
    assert sqlite3_cli_shape(led.path, "t_119dead") == (0, -1)
    assert field(led, "t_119dead", "notify_last_failed_at") is None
    assert field(led, "t_119dead", "notify_last_error") is None
    led.close()


def test_T4_idempotent(tmp_path, monkeypatch):
    """AC-119-5 + AC-157-1: 连续回执两次均成功不抛错, 第二次后确认位仍非空;
    且回执路径零事件写入 —— mw_events 行数回执前后不变 (#157 观察 1)。"""
    led = make_ledger(tmp_path)
    monkeypatch.setattr(console, "LEDGER_DB", led.path)
    seed_card(led, "t_119c")
    # AC-157-1: 把「回执不产生事件」从静态面背书钉到实效。静态背书保留作依据:
    # confirm_delivered (ledger.py:984) 函数体无 emit_event; 全库唯一
    # INSERT INTO mw_events 在 ledger.py:1395 (emit_event 本体, resume 路径),
    # 不在回执路径上 ⇒ 回执前后行数必须逐字相等。
    n0 = led.conn.execute("SELECT COUNT(*) FROM mw_events").fetchone()[0]
    r1 = console.api_relay_confirm({"task_id": "t_119c"})
    r2 = console.api_relay_confirm({"task_id": "t_119c"})
    assert r1.get("ok") is True and r2.get("ok") is True
    assert "code" not in r1 and "code" not in r2  # 两次都是成功形态
    assert field(led, "t_119c", "notify_confirmed_at") is not None
    n1 = led.conn.execute("SELECT COUNT(*) FROM mw_events").fetchone()[0]
    assert n1 == n0, "回执是幂等状态位写入, 不得向 mw_events 写事件"
    led.close()


def test_race_window_pinned_current_semantics(tmp_path, monkeypatch):
    """AC-157-2 (#157 观察 2, 按 s2 方案采纳「登记」不收紧): 钉住竞态窗口
    命中时的现行语义, 未来任何收紧改动都会让本用例转红、强制重读注释。

    模拟窗口命中: api_relay_confirm 内 get_contract 被调两次 —— 预检(第 1 次)
    走原函数返回行, 回执后重查(第 2 次)返回 None (等价于卡恰被并发删除)。
    现行语义 = ok:true + confirmed_at:null (confirm_delivered 对不存在
    task_id 静默 no-op, 幂等无破坏面); 收紧方案见 console.py 注释块。
    """
    led = make_ledger(tmp_path)
    monkeypatch.setattr(console, "LEDGER_DB", led.path)
    seed_card(led, "t_119race")
    calls = {"n": 0}
    orig_get_contract = Ledger.get_contract

    def fake_get_contract(self, task_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return orig_get_contract(self, task_id)  # 预检: 卡还在
        return None  # 回执后重查: 模拟窗口内卡被并发删除

    monkeypatch.setattr(Ledger, "get_contract", fake_get_contract)
    r = console.api_relay_confirm({"task_id": "t_119race"})
    assert calls["n"] == 2, "预检 + 回执后重查, get_contract 应恰被调两次"
    assert r["ok"] is True and r["confirmed_at"] is None, (
        "窗口命中时现行语义为 ok:true + confirmed_at:null;"
        "若本断言转红说明已收紧, 请回读 console.py 竞态注释块再定返回语义")
    led.close()


def test_T5_isolation(tmp_path, monkeypatch):
    """AC-119-6: 回执 A ⇒ 同库卡 B 的三字段逐字不变 (WHERE task_id=? 隔离)。"""
    led = make_ledger(tmp_path)
    monkeypatch.setattr(console, "LEDGER_DB", led.path)
    seed_dead_letter(led, "t_119B")  # B 也预置死信形态, 变没变一眼可见
    seed_card(led, "t_119A")
    cols = ("notify_confirmed_at", "notify_attempts", "notify_dead_until")
    before = tuple(field(led, "t_119B", c) for c in cols)
    assert before[0] is None and before[1] == 8  # B 回执前确为未确认死信
    r = console.api_relay_confirm({"task_id": "t_119A"})
    assert r["ok"] is True
    after = tuple(field(led, "t_119B", c) for c in cols)
    assert after == before, "回执 A 不得动卡 B 的任何字段"
    led.close()


def test_T6_regression_lock_unconfirmed_still_scanned(tmp_path, monkeypatch):
    """AC-119-4③ 回归锁: 未回执卡在 relay 扫描/precheck 口径下仍命中, 字段原样。"""
    led = make_ledger(tmp_path)
    monkeypatch.setattr(console, "LEDGER_DB", led.path)
    seed_card(led, "t_119hit")
    seed_card(led, "t_119conf")
    console.api_relay_confirm({"task_id": "t_119conf"})
    hits = {r[0] for r in led.conn.execute(RELAY_SCAN_SQL).fetchall()}
    assert "t_119hit" in hits, "未回执卡必须仍被 relay 扫描命中 (零回归)"
    assert "t_119conf" not in hits, "已回执卡不再被扫描命中"
    # precheck 三态逻辑对未回执卡原样放行
    assert led.relay_precheck("t_119hit")["go"] is True
    cols = ("notify_confirmed_at", "notify_attempts", "notify_dead_until")
    assert tuple(field(led, "t_119hit", c) for c in cols) == (None, 0, None)
    led.close()


# ── HTTP 层 (真路由: ThreadingHTTPServer + Handler, 既有测试形态) ────

def test_http_route_real_server(tmp_path, monkeypatch):
    """真 HTTP 路由: POST /api/relay/confirm ⇒ 200+ok:true 且账本实效落库;
    不存在卡 ⇒ 404; 缺参 ⇒ 400。"""
    led = make_ledger(tmp_path)
    monkeypatch.setattr(console, "LEDGER_DB", led.path)
    seed_card(led, "t_119http")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    def post(body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/relay/confirm",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        code, body = post({"task_id": "t_119http"})
        assert code == 200 and body["ok"] is True
        # 断言落在账本实效: 独立连接读回确认位
        assert sqlite3_cli_shape(led.path, "t_119http") == (0, -1)
        conn2 = sqlite3.connect(led.path)
        ts = conn2.execute("SELECT notify_confirmed_at FROM harvest_contracts "
                           "WHERE task_id='t_119http'").fetchone()[0]
        conn2.close()
        assert ts is not None and ts > 0 and body["confirmed_at"] == ts
        code404, body404 = post({"task_id": "t_119_nope"})
        assert code404 == 404 and body404["ok"] is False
        code400, body400 = post({})
        assert code400 == 400 and body400["ok"] is False
    finally:
        srv.shutdown()
        srv.server_close()
