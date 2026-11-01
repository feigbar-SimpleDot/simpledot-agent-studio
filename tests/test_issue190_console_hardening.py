"""O 批 O-1 守护: console.py 收口 (#190 #166 #176 + #141 state 透传)。

覆盖 (依据 docs/FIX-PLAN-O.md §簇A 与 docs/ADJUDICATION-O.md §s3):

  #190 两端点键集在「当前卡无 harvest_contracts 行」边界分叉
    A1  轻量端点: 无契约行 ⇒ current_card_harvest_status is None
                  (键在, 值为 None) —— 修前是「键缺失」。
    A2  轻量端点: 无契约行 ⇒ current_card_delivery_failed is False
                  (键在, 值为 False) —— 修前是「键缺失」。
    A3  两端点键集在该边界仍逐字一致 (AC-141-4 同源纪律)。
    A4  正常边界 (有契约行 + notify_last_error) 键集与值仍一致 (回归)。

  #141 run 级派生态 (state / holder_state) 透传
    B1  两端点都带 state + holder_state 两个键 (AC-141-4)。
    B2  WORKING 承接 ⇒ state='ACTIVE' / holder_state='WORKING'。
    B3  WAITING_SUB 卡 ⇒ state='ACTIVE_BLOCKED' / holder_state='WAITING_SUB'
        (ADJUDICATION AC-141-1 的服务端前置形状)。
    B4  WAITING (reaper 标记) ⇒ state='ACTIVE_BLOCKED' / holder_state='WAITING'
        (AC-141-2 的服务端前置形状)。
    B5  无 ACTIVE run ⇒ team_work_active=None, 两端点不抛 (AC-141-6)。
    B6  派生异常 fail-open ⇒ 两键为 None 且键集不缺失 (不阻断摘要返回)。

  #166 do_POST 全局 body 预读无上限
    C1  Content-Length > POST_BODY_MAX_BYTES ⇒ 413 payload_too_large。
    C2  超限时**不读** rfile (body 未进内存) —— 守「闸在读之前」。
    C3  Content-Length == 上限 ⇒ 不被 413 拦 (闸是严格大于, 边界不误伤)。
    C4  POST_BODY_MAX_BYTES 常量存在且为 64MB (FIX-PLAN §#166 落点口径)。

  #176 do_POST 外层兜底 str(e) 回显泄漏内部信息
    D1  端点抛 RuntimeError ⇒ 500 且响应体只有固定文案 internal_error。
    D2  响应体不含异常原文 (路径/栈/内部标识均不回显)。
    D3  服务端日志保留全量异常 (含栈) —— 排障信息不丢。
    D4  紧邻的 ValueError 分支仍 422 且**保留** str(e) (用户可读校验文案
        不是泄漏面, 不得被 D1 的改法误伤)。

不依赖外部资源; 用 sqlite3 临时建 mock kanban + mock ledger
(与 tests/test_issue178_delivery_badge.py 同口径)。

双口径断言 (照抄 #178 守护 s8fix 的结构化口径):
  pytest 收集口径 —— check() 失败即抛 AssertionError (守护有牙齿, 变异即红);
  脚本直跑口径   —— main() 置 _COLLECT_ONLY=True, 只收集不抛, 逐条明细 + rc 汇总。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mw.console as console  # noqa: E402
from mw.ledger import Ledger  # noqa: E402

PASS, FAIL = [], []

# 口径开关 (照抄 #178 守护):
#   False = pytest 收集口径 —— check() 失败即 raise AssertionError;
#   True  = 脚本直跑口径 (main() 置位) —— check() 只收集不抛。
_COLLECT_ONLY = False


def check(name, cond, detail=""):
    """单条断言点: 收集 + 打印 + (pytest 口径) 失败即红。"""
    (PASS if cond else FAIL).append((name, detail))
    marker = "PASS" if cond else "FAIL"
    line = f"  {marker}  {name}"
    if detail:
        line += f"  {detail}"
    print(line)
    if not _COLLECT_ONLY:
        assert cond, f"{name} {detail}"


# ── 夹具: mock kanban + mock ledger (同 #178 守护口径) ─────────────────

def fresh_dual_db():
    """建一对临时 db: kanban + ledger, 都注入 console 顶层模块变量。"""
    kanban_f = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    kanban_f.close()
    ledger_f = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    ledger_f.close()
    kc = sqlite3.connect(kanban_f.name)
    kc.executescript("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            assignee TEXT,
            status TEXT,
            block_kind TEXT,
            result TEXT,
            created_at REAL,
            started_at REAL,
            completed_at REAL
        );
    """)
    kc.commit()
    kc.close()
    console.KANBAN_DB = kanban_f.name
    console.LEDGER_DB = ledger_f.name
    return kanban_f.name, ledger_f.name, Ledger(ledger_f.name)


def cleanup(kanban_path, ledger_path, led):
    try:
        led.close()
    except Exception:
        pass
    for p in [kanban_path, ledger_path]:
        try:
            os.unlink(p)
        except OSError:
            pass


def seed_kanban(path, rows):
    c = sqlite3.connect(path)
    for r in rows:
        c.execute("""
            INSERT INTO tasks(id, title, assignee, status, block_kind, result,
                            created_at, started_at, completed_at)
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (r.get("id"), r.get("title"), r.get("assignee"),
              r.get("status"), r.get("block_kind"), r.get("result"),
              r.get("created_at"), r.get("started_at"), r.get("completed_at")))
    c.commit()
    c.close()


def seed_active_run(led, k_path, run_id="r1", task_id="t_a"):
    """建 ACTIVE run + 当前卡 + kanban 行 (正常边界: 有契约行)。"""
    led.create_team_work_run(run_id, "sess_x", "需求")
    led.put_contract(task_id, {"required_files": []})
    led.register_team_work_card(run_id, task_id)
    led.set_team_work_current_card(run_id, task_id)
    seed_kanban(k_path, [{"id": task_id, "title": "卡A", "assignee": "coder",
                          "status": "running", "created_at": 1.0}])
    return task_id


def seed_run_without_contract(led, k_path, run_id="r_nc", task_id="t_nc"):
    """建 ACTIVE run, 但**当前卡不种 harvest_contracts 行** (issue #190 边界)。

    这是 #190 的真实边界形态: 卡刚被 claim, 契约行尚未落库。
    注意 set_team_work_current_card 只做 UPDATE, 不强制存在契约行。
    """
    led.create_team_work_run(run_id, "sess_x", "需求")
    led.set_team_work_current_card(run_id, task_id)
    seed_kanban(k_path, [{"id": task_id, "title": "卡B", "assignee": "coder",
                          "status": "running", "created_at": 1.0}])
    return task_id


# ── #190: 无契约行边界的键集与缺省值 ───────────────────────────────────

def test_a190_light_endpoint_has_default_keys_without_contract_row():
    """A1/A2: 无契约行 ⇒ 两键**存在**且取缺省 (None / False)。"""
    k_path, l_path, led = fresh_dual_db()
    try:
        seed_run_without_contract(led, k_path)
        a = console.api_tasks(fields="active")["team_work_active"]
        check("A1 无契约行 ⇒ current_card_harvest_status 键在且为 None",
              a is not None
              and "current_card_harvest_status" in a
              and a["current_card_harvest_status"] is None,
              f"got {a and a.get('current_card_harvest_status', 'MISSING')!r}")
        check("A2 无契约行 ⇒ current_card_delivery_failed 键在且为 False",
              a is not None
              and "current_card_delivery_failed" in a
              and a["current_card_delivery_failed"] is False,
              f"got {a and a.get('current_card_delivery_failed', 'MISSING')!r}")
    finally:
        cleanup(k_path, l_path, led)


def test_a190_two_endpoints_key_set_identical_without_contract_row():
    """A3: 无契约行边界上两端点键集仍逐字一致 (修前分叉)。"""
    k_path, l_path, led = fresh_dual_db()
    try:
        seed_run_without_contract(led, k_path)
        a_light = console.api_tasks(fields="active")["team_work_active"]
        a_full = console.api_tasks()["team_work_active"]
        check("A3 无契约行边界: 两端点键集逐字一致",
              set(a_light) == set(a_full),
              f"light-only={set(a_light)-set(a_full)} "
              f"full-only={set(a_full)-set(a_light)}")
    finally:
        cleanup(k_path, l_path, led)


def test_a190_two_endpoints_key_set_identical_normal_boundary():
    """A4: 正常边界 (有契约行) 键集与值仍一致 (回归)。"""
    k_path, l_path, led = fresh_dual_db()
    try:
        tid = seed_active_run(led, k_path)
        led.record_notify_failure(tid, "boom", attempts=1, dead_until=0.0)
        led.upsert_session(tid, "S1", profile="coder",
                           runner_kind="docker", workspace="/w")
        a_light = console.api_tasks(fields="active")["team_work_active"]
        a_full = console.api_tasks()["team_work_active"]
        check("A4 正常边界: 两端点键集逐字一致",
              set(a_light) == set(a_full),
              f"light-only={set(a_light)-set(a_full)} "
              f"full-only={set(a_full)-set(a_light)}")
        check("A4 正常边界: 两端点 current_card_delivery_failed 同为 True",
              a_light.get("current_card_delivery_failed") is True
              and a_full.get("current_card_delivery_failed") is True)
    finally:
        cleanup(k_path, l_path, led)


# ── #141: run 级派生态 (state / holder_state) 透传 ────────────────────

def test_b141_both_endpoints_carry_state_and_holder_state():
    """B1: 两端点都带 state + holder_state 两个键。"""
    k_path, l_path, led = fresh_dual_db()
    try:
        seed_active_run(led, k_path)
        a_light = console.api_tasks(fields="active")["team_work_active"]
        a_full = console.api_tasks()["team_work_active"]
        for tag, a in (("轻量", a_light), ("全量", a_full)):
            check(f"B1 {tag}端点含 state 键", "state" in a, f"got {sorted(a)}")
            check(f"B1 {tag}端点含 holder_state 键",
                  "holder_state" in a, f"got {sorted(a)}")
    finally:
        cleanup(k_path, l_path, led)


def test_b141_working_holder_yields_active_state():
    """B2: WORKING 承接 ⇒ state='ACTIVE' / holder_state='WORKING'。"""
    k_path, l_path, led = fresh_dual_db()
    try:
        seed_active_run(led, k_path)
        a = console.api_tasks(fields="active")["team_work_active"]
        check("B2 轻量端点 state=ACTIVE", a.get("state") == "ACTIVE",
              f"got {a.get('state')!r}")
        check("B2 轻量端点 holder_state=WORKING",
              a.get("holder_state") == "WORKING",
              f"got {a.get('holder_state')!r}")
        a2 = console.api_tasks()["team_work_active"]
        check("B2 全量端点同值 (同源)",
              a2.get("state") == "ACTIVE"
              and a2.get("holder_state") == "WORKING",
              f"got {a2.get('state')!r}/{a2.get('holder_state')!r}")
    finally:
        cleanup(k_path, l_path, led)


def test_b141_waiting_sub_holder_yields_active_blocked_plus_waiting_sub():
    """B3: WAITING_SUB 卡 ⇒ ACTIVE_BLOCKED + holder_state=WAITING_SUB。

    这是 ADJUDICATION-O AC-141-1 的**服务端前置形状**: 前端分支条件是
    runState==='ACTIVE_BLOCKED' && active.holder_state==='WAITING_SUB'。
    """
    k_path, l_path, led = fresh_dual_db()
    try:
        tid = seed_active_run(led, k_path)
        led.conn.execute(
            "UPDATE harvest_contracts SET subagent_wait_since=? "
            "WHERE task_id=?", (time.time() - 300, tid))
        led.conn.commit()
        a = console.api_tasks(fields="active")["team_work_active"]
        check("B3 轻量端点 state=ACTIVE_BLOCKED",
              a.get("state") == "ACTIVE_BLOCKED", f"got {a.get('state')!r}")
        check("B3 轻量端点 holder_state=WAITING_SUB",
              a.get("holder_state") == "WAITING_SUB",
              f"got {a.get('holder_state')!r}")
        a2 = console.api_tasks()["team_work_active"]
        check("B3 全量端点同值 (同源)",
              a2.get("state") == "ACTIVE_BLOCKED"
              and a2.get("holder_state") == "WAITING_SUB",
              f"got {a2.get('state')!r}/{a2.get('holder_state')!r}")
    finally:
        cleanup(k_path, l_path, led)


def test_b141_waiting_holder_yields_active_blocked_plus_waiting():
    """B4: WAITING (reaper 标记) ⇒ ACTIVE_BLOCKED + holder_state=WAITING。

    ADJUDICATION-O AC-141-2: 其余 ACTIVE_BLOCKED 形态必须**仍**是
    「承接者受阻」⇒ 服务端不得把 WAITING 误压成 WAITING_SUB。
    """
    k_path, l_path, led = fresh_dual_db()
    try:
        tid = seed_active_run(led, k_path)
        led.update_worker_state(tid, "WAITING", actor="reaper")
        a = console.api_tasks(fields="active")["team_work_active"]
        check("B4 轻量端点 state=ACTIVE_BLOCKED",
              a.get("state") == "ACTIVE_BLOCKED", f"got {a.get('state')!r}")
        check("B4 轻量端点 holder_state=WAITING (非 WAITING_SUB)",
              a.get("holder_state") == "WAITING",
              f"got {a.get('holder_state')!r}")
    finally:
        cleanup(k_path, l_path, led)


def test_b141_no_active_run_is_empty_state_safe():
    """B5: 无 ACTIVE run ⇒ team_work_active=None, 两端点不抛 (AC-141-6)。"""
    k_path, l_path, led = fresh_dual_db()
    try:
        r = console.api_tasks(fields="active")
        check("B5 轻量端点空态 team_work_active is None",
              r["team_work_active"] is None)
        r2 = console.api_tasks()
        check("B5 全量端点空态 team_work_active is None",
              r2["team_work_active"] is None)
    finally:
        cleanup(k_path, l_path, led)


def test_b141_derive_failure_fails_open_to_none():
    """B6: 派生异常 ⇒ 两键为 None 且键仍在 (fail-open, 不阻断摘要)。"""
    k_path, l_path, led = fresh_dual_db()
    try:
        seed_active_run(led, k_path)
        with mock.patch.object(
                Ledger, "derive_run_overview",
                side_effect=RuntimeError("derive boom")):
            a = console.api_tasks(fields="active")["team_work_active"]
            check("B6 派生异常 ⇒ 两端点仍返回摘要",
                  a is not None, f"got {a!r}")
            check("B6 派生异常 ⇒ state/holder_state 键在且为 None",
                  a.get("state") is None and a.get("holder_state") is None,
                  f"got {a.get('state')!r}/{a.get('holder_state')!r}")
            a2 = console.api_tasks()["team_work_active"]
            check("B6 派生异常 ⇒ 全量端点同样 fail-open",
                  a2.get("state") is None
                  and a2.get("holder_state") is None,
                  f"got {a2.get('state')!r}/{a2.get('holder_state')!r}")
    finally:
        cleanup(k_path, l_path, led)


# ── #166: do_POST body 预读上限 ───────────────────────────────────────

class _FakeRfile:
    """假 rfile: 记录 read 次数 (用于断言「闸在读之前」)。"""

    def __init__(self, body: bytes = b""):
        self.body = body
        self.reads = 0

    def read(self, n):
        self.reads += 1
        return self.body[:n]


class _FakeHandler(console.Handler):
    """绕开 BaseHTTPRequestHandler.__init__ (不联网), 只装路由所需属性。

    照抄 tests/test_issue75_messages_window.py 的 MockHandler 口径, 但额外
    让 write() 返回实际写入字节数 (console._write_all 的循环写需要 int 返回)。
    """

    def __init__(self, path, headers=None, body=b""):
        self.path = path
        self.headers = headers or {}
        self.status = None
        self.sent_headers = []
        self.body = b""
        self.wfile = self
        self.rfile = _FakeRfile(body)

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.sent_headers.append((k, v))

    def end_headers(self):
        pass

    def write(self, chunk):
        self.body += bytes(chunk)
        return len(chunk)


def test_c166_constant_is_64mb():
    """C4: POST_BODY_MAX_BYTES 常量存在且 = 64MB (FIX-PLAN §#166 落点)。"""
    check("C4 POST_BODY_MAX_BYTES 存在",
          hasattr(console, "POST_BODY_MAX_BYTES"))
    check("C4 POST_BODY_MAX_BYTES == 64MB",
          getattr(console, "POST_BODY_MAX_BYTES", 0) == 64 * 1024 * 1024,
          f"got {getattr(console, 'POST_BODY_MAX_BYTES', None)!r}")


def test_c166_oversize_body_returns_413():
    """C1: Content-Length 超上限 ⇒ 413 payload_too_large。"""
    h = _FakeHandler("/api/upload",
                     {"Content-Length": str(console.POST_BODY_MAX_BYTES + 1)})
    h.do_POST()
    check("C1 超限 body ⇒ 413", h.status == 413, f"got {h.status}")
    check("C1 超限 body ⇒ error=payload_too_large",
          json.loads(h.body or b"{}").get("error") == "payload_too_large",
          f"got {h.body!r}")


def test_c166_oversize_body_is_not_read_into_memory():
    """C2: 超限时不得读 rfile —— 闸必须落在「读」之前。"""
    h = _FakeHandler("/api/upload",
                     {"Content-Length": str(console.POST_BODY_MAX_BYTES + 1)},
                     body=b"x" * 1024)
    h.do_POST()
    check("C2 超限 ⇒ rfile 零次读 (body 未进内存)",
          h.rfile.reads == 0, f"reads={h.rfile.reads}")


def test_c166_at_limit_is_not_rejected():
    """C3: 恰好等于上限 ⇒ 不被 413 拦 (闸是严格大于)。"""
    h = _FakeHandler("/api/no-such-endpoint",
                     {"Content-Length": str(console.POST_BODY_MAX_BYTES)})
    h.do_POST()
    check("C3 等于上限 ⇒ 不被 413 拦 (走正常路由)",
          h.status != 413, f"got {h.status}")
    check("C3 等于上限 ⇒ 落到 404 (路由层正常处理)",
          h.status == 404, f"got {h.status}")


# ── #176: 外层兜底不再回显 str(e) ─────────────────────────────────────

class _LogCapture(logging.Handler):
    """捕获 mw.console logger 的记录 (该 logger propagate=False, caplog 抓不到)。"""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_d176_internal_error_does_not_echo_exception_text():
    """D1/D2: 端点抛异常 ⇒ 500 + 固定文案, 响应体不含异常原文。"""
    secret = "/opt/mwroot/mw/ledger.db internal-identifier-xyz"
    h = _FakeHandler("/api/resume", {"Content-Length": "2"}, b"{}")
    with mock.patch.object(console, "api_resume",
                           side_effect=RuntimeError(secret)):
        h.do_POST()
    check("D1 端点异常 ⇒ 500", h.status == 500, f"got {h.status}")
    payload = json.loads(h.body or b"{}")
    check("D1 响应体 error 字段 == internal_error",
          payload.get("error") == "internal_error", f"got {payload!r}")
    check("D2 响应体不含异常原文",
          secret.encode() not in h.body, f"got {h.body!r}")
    check("D2 响应体不含 'Traceback'",
          b"Traceback" not in h.body, f"got {h.body!r}")


def test_d176_server_log_keeps_full_exception():
    """D3: 服务端日志保留全量异常 (含栈) —— 排障信息不丢。"""
    secret = "internal-identifier-xyz"
    logger = logging.getLogger("mw.console")
    cap = _LogCapture()
    logger.addHandler(cap)
    try:
        h = _FakeHandler("/api/resume", {"Content-Length": "2"}, b"{}")
        with mock.patch.object(console, "api_resume",
                               side_effect=RuntimeError(secret)):
            h.do_POST()
    finally:
        logger.removeHandler(cap)
    msgs = [r.getMessage() for r in cap.records]
    check("D3 服务端日志含 do_POST 失败记录",
          any("do_POST" in m for m in msgs), f"got {msgs}")
    check("D3 服务端日志含异常原文 (排障不丢)",
          any(secret in m for m in msgs), f"got {msgs}")
    check("D3 记录带异常栈 (exc_info)",
          any(r.exc_info for r in cap.records), "no exc_info")


def test_d176_value_error_branch_still_returns_422_with_text():
    """D4: ValueError 分支仍 422 且保留 str(e) (用户可读校验文案)。"""
    msg = "非法工作目录名: 'Bad Name' (仅允许小写字母/数字/连字符)"
    h = _FakeHandler("/api/resume", {"Content-Length": "2"}, b"{}")
    with mock.patch.object(console, "api_resume", side_effect=ValueError(msg)):
        h.do_POST()
    check("D4 ValueError ⇒ 422", h.status == 422, f"got {h.status}")
    payload = json.loads(h.body or b"{}")
    check("D4 ValueError ⇒ 保留 str(e) 原文",
          payload.get("error") == msg, f"got {payload!r}")


def main():
    """脚本直跑口径: 置 _COLLECT_ONLY=True ⇒ check() 只收集不抛。"""
    global _COLLECT_ONLY
    _COLLECT_ONLY = True
    tests = [
        test_a190_light_endpoint_has_default_keys_without_contract_row,
        test_a190_two_endpoints_key_set_identical_without_contract_row,
        test_a190_two_endpoints_key_set_identical_normal_boundary,
        test_b141_both_endpoints_carry_state_and_holder_state,
        test_b141_working_holder_yields_active_state,
        test_b141_waiting_sub_holder_yields_active_blocked_plus_waiting_sub,
        test_b141_waiting_holder_yields_active_blocked_plus_waiting,
        test_b141_no_active_run_is_empty_state_safe,
        test_b141_derive_failure_fails_open_to_none,
        test_c166_constant_is_64mb,
        test_c166_oversize_body_returns_413,
        test_c166_oversize_body_is_not_read_into_memory,
        test_c166_at_limit_is_not_rejected,
        test_d176_internal_error_does_not_echo_exception_text,
        test_d176_server_log_keeps_full_exception,
        test_d176_value_error_branch_still_returns_422_with_text,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            check(t.__name__, False, f"EXC: {type(e).__name__}: {e}")
    total = len(PASS) + len(FAIL)
    print()
    if not FAIL:
        print(f"\033[92m=== ALL {total} O-1 TESTS PASSED ===\033[0m")
        return 0
    print(f"\033[91m=== {len(FAIL)}/{total} FAILED ===\033[0m")
    for n, d in FAIL:
        print(f"  - {n}: {d}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
