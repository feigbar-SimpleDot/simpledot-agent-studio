"""tests/test_issue58_tokens.py — issue #58 token 消耗量显示 (a3-impl)

覆盖 (API-SPEC-58 §1-§3 + main 裁决 1/2, AC 编号见 ACCEPTANCE-58):
  H 组  _capture_tokens 单元契约
    H1  正常拉取: 9 键齐, source=api, total_tokens=Σ五桶 (AC-P1/P2)
    H2  registry 缺 api_url/api_key / session_id 缺失 → unavailable 全 0, 不抛
    H3  gateway 4xx/5xx、坏 JSON、非 dict 响应 → unavailable, captured_at=None
    H4  字段缺失/脏类型 → 视为 0; 全字段缺 → source=api + 0 用量 (R1)
    H5  Bearer 头: 请求头必须 "Bearer <key>" (裸 key 会 401, AC-P2)
  C 组  结算路径合并 (AC-P3 四终态全覆盖)
    C1  completed / blocked / failed / ws 未知+clarify 四条路径的
        worker_report 均含 tokens 键
    C2  gateway 不可达 (fail-open): 结算照常落账, tokens.source=unavailable
    C3  多次结算: 后写覆盖前写 (AC-P6) — 白名单守卫允许未真结算态重写
  A 组  console 聚合 (AC-A1/A2/A3/A5)
    A1  _aggregate_run_tokens 纯函数五场景 (空/全 api/混合/全 unavailable/坏 JSON)
    A2  api_team_work_get: cards[].worker_report 注入 + team_work.token_total
    A3  api_team_works_list 与 api_team_work_get 同 run 的 token_total 相等
    A4  孤儿卡快照计入 run 求和 (AC-A5)
    A5  既有键零删除冒烟: 注入不吞原有 cards/team_work 键 (AC-A4 抽样)

隔离纪律: 全部测试用 monkeypatch 顶掉 mw.harvest 的 HTTP 层或喂空 registry,
**绝不发真实网络请求** (容器内 170 不可达, 且 5s 超时会拖慢全量 pytest)。
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mw.harvest as harvest
import mw.console as console
from mw.ledger import Ledger


# ─────────────────────────────────────────────────────────────────────
# 假 gateway: 一次性 GET /api/sessions/<sid>, 按脚本回 JSON / 状态码
# ─────────────────────────────────────────────────────────────────────
class _Gateway:
    """受控 HTTP stub; 记录请求 (path, Authorization 头), 按 handler 回包。"""

    def __init__(self, status=200, payload=None, raw=None):
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append(
                    (self.path, self.headers.get("Authorization")))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                body = raw if raw is not None else json.dumps(payload or {})
                self.wfile.write(body.encode())

            def log_message(self, format, *a):  # 静音
                pass

        self.requests = []
        self._srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self._srv.server_address[1]
        self._th = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._th.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()


def _row(d):
    """dict → 类 sqlite3.Row (支持 keys() 与 []), 只测消费面。"""
    class _R:
        def keys(self):
            return list(d.keys())

        def __getitem__(self, k):
            return d[k]
    return _R()


_FULL_SESSION = {"session": {
    "input_tokens": 1234, "output_tokens": 567, "cache_read_tokens": 11,
    "cache_write_tokens": 22, "reasoning_tokens": 33,
    "estimated_cost_usd": 0.0042}}

# ═════════════════════════ H 组: _capture_tokens ═════════════════════════

def test_H1_normal_capture_full_contract():
    gw = _Gateway(payload=_FULL_SESSION)
    try:
        t0 = time.time()
        snap = harvest._capture_tokens({"api_url": gw.url, "api_key": "***"},
                                       _row({"session_id": "sid-1"}))
        assert snap["source"] == "api"
        assert set(snap) == {"input_tokens", "output_tokens",
                             "cache_read_tokens", "cache_write_tokens",
                             "reasoning_tokens", "estimated_cost_usd",
                             "total_tokens", "captured_at", "source"}
        assert snap["input_tokens"] == 1234 and snap["output_tokens"] == 567
        assert snap["cache_read_tokens"] == 11 and snap["cache_write_tokens"] == 22
        assert snap["reasoning_tokens"] == 33
        assert abs(snap["estimated_cost_usd"] - 0.0042) < 1e-9
        assert snap["total_tokens"] == 1234 + 567 + 11 + 22 + 33
        assert snap["captured_at"] >= t0
    finally:
        gw.stop()


def test_H2_missing_inputs_returns_unavailable_never_raises():
    zero = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "reasoning_tokens": 0,
            "estimated_cost_usd": 0.0, "total_tokens": 0,
            "captured_at": None, "source": "unavailable"}
    # 三种缺输入形态: reg None / 缺 key / 缺 session
    assert harvest._capture_tokens(None, _row({"session_id": "s"})) == zero
    assert harvest._capture_tokens({"api_url": "http://x"},
                                   _row({"session_id": "s"})) == zero
    assert harvest._capture_tokens({"api_url": "http://x", "api_key": "***"},
                                   None) == zero
    # sess_row 存在但没有 session_id 键
    assert harvest._capture_tokens({"api_url": "http://x", "api_key": "***"},
                                   _row({"task_id": "t"})) == zero


def test_H3_http_and_json_failures_are_fail_open():
    zero_source = "unavailable"
    for kwargs in (
        {"status": 401, "payload": {}},          # 裸 key/失效 key 场景
        {"status": 503, "payload": {}},          # gateway 挂
        {"status": 200, "raw": "not-json{{"},     # 非 JSON
        {"status": 200, "raw": "[1,2,3]"},        # 顶层非 dict → .get 炸 → fail-open
    ):
        gw = _Gateway(**kwargs)
        try:
            snap = harvest._capture_tokens(
                {"api_url": gw.url, "api_key": "***"},
                _row({"session_id": "sid"}))
            assert snap["source"] == zero_source, kwargs
            assert snap["captured_at"] is None and snap["total_tokens"] == 0
        finally:
            gw.stop()


def test_H4_dirty_fields_default_to_zero_but_source_stays_api():
    # 旧版 gateway 六字段全缺 (风险 R1): 拉到响应即 api, 数值全 0
    gw = _Gateway(payload={"session": {}})
    try:
        snap = harvest._capture_tokens({"api_url": gw.url, "api_key": "***"},
                                       _row({"session_id": "sid"}))
        assert snap["source"] == "api"
        assert snap["total_tokens"] == 0 and snap["estimated_cost_usd"] == 0.0
    finally:
        gw.stop()
    # 脏类型 (字符串/bool) → 0, 合法字段照常
    gw = _Gateway(payload={"session": {
        "input_tokens": "abc", "output_tokens": True,
        "cache_read_tokens": 5, "estimated_cost_usd": "NaN-str"}})
    try:
        snap = harvest._capture_tokens({"api_url": gw.url, "api_key": "***"},
                                       _row({"session_id": "sid"}))
        assert snap["input_tokens"] == 0 and snap["output_tokens"] == 0
        assert snap["cache_read_tokens"] == 5
        assert snap["estimated_cost_usd"] == 0.0
        assert snap["total_tokens"] == 5
    finally:
        gw.stop()


def test_H5_bearer_header_format():
    gw = _Gateway(payload=_FULL_SESSION)
    # key 用运行时拼接: 平台脱敏器会改写源码里的凭据字面量, 断言值必须同源构造
    key = "ka" + "7x"
    try:
        harvest._capture_tokens({"api_url": gw.url + "/", "api_key": key},
                                _row({"session_id": "sid-9"}))
        assert len(gw.requests) == 1  # 单卡 1 次, 不重试 (AC-P4)
        path, auth = gw.requests[0]
        assert path == "/api/sessions/sid-9"
        assert auth == "Bearer " + key
    finally:
        gw.stop()


# ═════════════════════ C 组: 结算路径合并 (AC-P3) ════════════════════════

def _stub_no_cli_transcript(monkeypatch):
    """容器无 hermes CLI: export_transcript 桩 None + fetch_transcript_api 桩 None,
    结算测试只验 token 合并, 不依赖 transcript 三跳 fallback。"""
    monkeypatch.setattr(harvest, "export_transcript",
                        lambda *a, **k: None)
    monkeypatch.setattr(harvest, "fetch_transcript_api",
                        lambda *a, **k: None)


def _settle_setup(worker_status, clarify=None, gateway=None):
    """建 ledger+workspace; registry 指向假 gateway (或空)。

    返回 (led, ws, task_id, restore_env)。测试自己调 harvest 并清理。
    """
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    led = Ledger(db)
    task_id = "t_58_" + worker_status
    ws = tempfile.mkdtemp(prefix="ws58_")
    led.put_contract(task_id, {"required_files": [], "timeout_seconds": 60},
                     required_passes=None)
    led.upsert_session(task_id, "sid-58", "gw", runner_kind="api", workspace=ws)
    if clarify is not None:
        (Path(ws) / ".clarify.json").write_text(json.dumps(clarify))
    env = os.environ.get("MW_REGISTRY")
    regf = Path(ws) / "_registry.json"
    regf.write_text(json.dumps(
        {"gw": {"api_url": gateway.url, "api_key": "***"}}
        if gateway else {}))
    os.environ["MW_REGISTRY"] = str(regf)

    def restore():
        if env is None:
            os.environ.pop("MW_REGISTRY", None)
        else:
            os.environ["MW_REGISTRY"] = env
        led.close()
        os.unlink(db)
    return led, ws, task_id, restore


def _read_wr(led, task_id):
    row = led.conn.execute(
        "SELECT worker_report FROM harvest_contracts WHERE task_id=?",
        (task_id,)).fetchone()
    return json.loads(row["worker_report"])


def test_C1_all_four_verdict_paths_carry_tokens_key(monkeypatch):
    _stub_no_cli_transcript(monkeypatch)
    gw = _Gateway(payload=_FULL_SESSION)
    try:
        cases = [
            ("completed", None, "COMPLETE"),
            ("blocked", {"question": "?"}, "NEED_CLARIFICATION"),
            ("failed", None, "FAILED"),
        ]
        for status, clarify, verdict in cases:
            led, ws, tid, restore = _settle_setup(status, clarify, gw)
            try:
                r = harvest.harvest(tid, ledger=led, hermes_home="",
                                    workspace=ws, spawn_result=None,
                                    worker_status=status)
                assert r["settled"] and r["verdict"] == verdict, r
                wr = _read_wr(led, tid)
                assert "tokens" in wr, f"{status}: worker_report 缺 tokens 键"
                assert wr["tokens"]["source"] == "api"
                assert wr["tokens"]["total_tokens"] == 1867
            finally:
                restore()
        # 第 4 条: worker_status 未知 (CLI 手动结算) + .clarify.json 兜底
        led, ws, tid, restore = _settle_setup(
            "unknown", {"question": "fallback?"}, gw)
        try:
            r = harvest.harvest(tid, ledger=led, hermes_home="", workspace=ws,
                                spawn_result=None, worker_status=None)
            assert r["settled"] and r["verdict"] == "NEED_CLARIFICATION"
            wr = _read_wr(led, tid)
            assert "tokens" in wr and wr["tokens"]["source"] == "api"
        finally:
            restore()
    finally:
        gw.stop()


def test_C2_unreachable_gateway_settlement_unaffected(monkeypatch):
    _stub_no_cli_transcript(monkeypatch)
    """fail-open (AC-P4): registry 指向关闭端口 → 结算照常, tokens unavailable。"""
    led, ws, tid, restore = _settle_setup("completed")  # 空 registry
    # issue #126: 把"墙钟"代理指标换成"零 HTTP 调用"行为断言。
    # 旧断言 assert time.time() - t0 < 2.0 是拿绝对时间当判据 —— 高负载
    # (并发跑测 / 机器忙) 下 2.04s 就击穿, 属假红, 而产品行为是对的。
    # 该断言的真实语义 = "空 registry → 无 HTTP 输入 → 短路返回, 不卡在
    # 超时等待上"。mw.harvest._capture_tokens 的 HTTP 出口是
    # `import urllib.request` 后调 urllib.request.urlopen (函数内 import,
    # 每次取模块属性), 所以打在 sys.modules['urllib.request'] 上的探针
    # 必然拦截到 ⇒ 断言"零调用"才是"短路"语义本体, 比墙钟更严。
    #
    # 只统计**本线程**的调用: 同一 pytest 进程里, 前面测试文件 (reaper
    # 用例) 起的报告中继后台线程也会打 urlopen —— 那是别的卡的投递, 与
    # 本用例的结算路径无关, 计进来就成了新的假红源。按线程隔离后,
    # 断言精确等于"本次结算路径自己没发任何 HTTP"。
    _http_calls: list = []
    _real_urlopen = urllib.request.urlopen
    _self_ident = threading.get_ident()

    def _spy_urlopen(*a, **k):
        if threading.get_ident() == _self_ident:
            _http_calls.append(a)
        return _real_urlopen(*a, **k)

    monkeypatch.setattr(urllib.request, "urlopen", _spy_urlopen)
    try:
        t0 = time.time()
        r = harvest.harvest(tid, ledger=led, hermes_home="", workspace=ws,
                            spawn_result=None, worker_status="completed")
        assert r["settled"] and r["verdict"] == "COMPLETE"
        wr = _read_wr(led, tid)
        assert wr["tokens"]["source"] == "unavailable"
        assert wr["tokens"]["captured_at"] is None
        # 行为断言 (R-126-B 首选): 空 registry → 结算路径零 HTTP 调用。
        # 这才是"短路"语义本体; 墙钟降为宽松兜底代理指标。
        assert _http_calls == [], \
            f"空 registry 结算不应发起 HTTP, 实际 {len(_http_calls)} 次"
        # 墙钟兜底保留 (红线: 不得删除该断言), 阈值由 2.0s 放宽到 5.0s:
        # 5s 恰是 _capture_tokens 的单次 HTTP timeout —— 真卡在超时等待上
        # 必然超过它, 而高负载下的正常短路 (实测 ~0.2s) 远低于它。
        assert time.time() - t0 < 5.0, \
            "结算不应卡在超时等待上 (5s 兜底, 代理指标)"
    finally:
        restore()


def test_C3_resettle_latest_snapshot_wins(monkeypatch):
    _stub_no_cli_transcript(monkeypatch)
    """AC-P6: NEED_CLARIFICATION (未真结算态) 被再次结算 → tokens 后写覆盖。"""
    gw = _Gateway(payload=_FULL_SESSION)
    try:
        led, ws, tid, restore = _settle_setup("blocked", {"question": "?"}, gw)
        try:
            r1 = harvest.harvest(tid, ledger=led, hermes_home="", workspace=ws,
                                 spawn_result=None, worker_status="blocked")
            assert r1["settled"]
            wr1 = _read_wr(led, tid)
            # 同输入重结算 → captured_at 后移 (后写覆盖前写)
            time.sleep(0.01)
            r2 = harvest.harvest(tid, ledger=led, hermes_home="", workspace=ws,
                                 spawn_result=None, worker_status="blocked")
            assert r2["settled"]
            wr2 = _read_wr(led, tid)
            assert wr2["tokens"]["captured_at"] > wr1["tokens"]["captured_at"]
        finally:
            restore()
    finally:
        gw.stop()


# ═══════════════════ A 组: console 聚合 + 两出口注入 ═════════════════════

def _api_snap(n):
    return {"input_tokens": n, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "reasoning_tokens": 0,
            "estimated_cost_usd": 0.1, "total_tokens": n,
            "captured_at": 1760874000.0, "source": "api"}


def test_A1_aggregate_pure_function_scenarios():
    agg = console._aggregate_run_tokens
    # 空 run
    r = agg([])
    assert r == {"total_tokens": 0, "estimated_cost_usd": 0.0,
                 "captured": 0, "missing": 0, "source": "unavailable"}
    # 全 api
    cards = [{"worker_report": {"tokens": _api_snap(100)}},
             {"worker_report": {"tokens": _api_snap(200)}}]
    r = agg(cards)
    assert r["total_tokens"] == 300 and r["captured"] == 2 and r["missing"] == 0
    assert r["source"] == "api"
    assert abs(r["estimated_cost_usd"] - 0.2) < 1e-6
    # 混合: 1 有快照 + 1 历史 (无 tokens 键) + 1 unavailable
    mixed = cards[:1] + [{"worker_report": {"verdict": "COMPLETE"}},
                         {"worker_report": None},
                         {"worker_report": {"tokens": {
                             "input_tokens": 9, "output_tokens": 0,
                             "cache_read_tokens": 0, "cache_write_tokens": 0,
                             "reasoning_tokens": 0, "estimated_cost_usd": 0.0,
                             "total_tokens": 9, "captured_at": None,
                             "source": "unavailable"}}}]
    r = agg(mixed)
    assert r == {"total_tokens": 100, "estimated_cost_usd": 0.1,
                 "captured": 1, "missing": 3, "source": "partial"}
    # 全 unavailable
    r = agg([{"worker_report": {"tokens": {"source": "unavailable"}}}])
    assert r["source"] == "unavailable" and r["total_tokens"] == 0
    assert r["captured"] == 0 and r["missing"] == 1


def _console_env():
    """一对临时 db 注入 console 顶层变量 (同 test_s2_fix11 姿势)。"""
    kanban_f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    kanban_f.close()
    ledger_f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    ledger_f.close()
    kc = sqlite3.connect(kanban_f.name)
    kc.executescript("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
            block_kind TEXT, result TEXT, created_at REAL, started_at REAL,
            completed_at REAL);
    """)
    kc.commit()
    kc.close()
    pre = Ledger(ledger_f.name)
    pre.close()
    old_k, old_l = console.KANBAN_DB, console.LEDGER_DB
    console.KANBAN_DB, console.LEDGER_DB = kanban_f.name, ledger_f.name
    return kanban_f.name, ledger_f.name, (old_k, old_l)


def _seed_run(kpath, lpath, card_specs):
    """建 1 run + N 卡 (kanban + ledger), card_specs: [(task_id, wr_json)]。"""
    led = Ledger(lpath)
    led.create_team_work_run("run58", "s58", "issue58 demo", "/tmp/ws58")
    for tid, wr in card_specs:
        led.register_team_work_card("run58", tid)
        led.put_contract(tid, {"required_files": []}, required_passes=None)
        if wr is not None:
            led.conn.execute(
                "UPDATE harvest_contracts SET worker_report=? WHERE task_id=?",
                (json.dumps(wr, ensure_ascii=False), tid))
    led.conn.commit()
    led.close()
    kc = sqlite3.connect(kpath)
    for tid, _ in card_specs:
        kc.execute("INSERT INTO tasks(id,title,assignee,status,created_at)"
                   " VALUES (?,?,?,?,?)",
                   (tid, tid, "coder", "done", 100.0))
    kc.commit()
    kc.close()


def test_A2_A3_detail_injection_and_list_detail_consistency():
    kpath, lpath, (old_k, old_l) = _console_env()
    try:
        _seed_run(kpath, lpath, [
            ("t_a", {"verdict": "COMPLETE", "tokens": _api_snap(100)}),
            ("t_b", {"verdict": "COMPLETE"}),                       # 历史卡
            ("t_c", {"verdict": "COMPLETE", "tokens": _api_snap(50)}),
        ])
        detail = console.api_team_work_get("run58")
        tw = detail["team_work"]
        # detail 注入
        assert tw["token_total"]["total_tokens"] == 150
        assert tw["token_total"]["captured"] == 2
        assert tw["token_total"]["missing"] == 1
        assert tw["token_total"]["source"] == "partial"
        # cards[].worker_report 注入 (main 裁决 1): 完整 dict, 含 tokens 子键
        by_id = {c["id"]: c for c in detail["cards"]}
        assert by_id["t_a"]["worker_report"]["tokens"]["total_tokens"] == 100
        assert "tokens" not in by_id["t_b"]["worker_report"]  # 老卡键缺失
        # AC-A4 抽样: 原有键未吞
        for key in ("id", "title", "assignee", "status", "harvest_status",
                    "exit_code", "harvest_log", "session_id", "workspace",
                    "team_work_run_id", "exec_index", "appended", "state"):
            assert key in by_id["t_a"], f"cards[].{key} 被吞 (AC-A4)"
        for key in ("run_id", "status", "initial_task", "card_count",
                    "created_at", "state", "state_source", "state_card_states"):
            assert key in tw, f"team_work.{key} 被吞 (AC-A4)"
        # AC-A2: 两出口同值
        lst = console.api_team_works_list({"include_closed": True})
        row = next(r for r in lst["team_works"] if r["run_id"] == "run58")
        assert row["token_total"] == tw["token_total"]
    finally:
        console.KANBAN_DB, console.LEDGER_DB = old_k, old_l
        for p in (kpath, lpath):
            try:
                os.unlink(p)
            except OSError:
                pass


def test_A4_orphan_card_snapshot_counts():
    kpath, lpath, (old_k, old_l) = _console_env()
    try:
        # t_orphan 只登记 ledger (kanban 无行) → 孤儿, 但有快照
        _seed_run(kpath, lpath, [("t_live", {"tokens": _api_snap(10)})])
        led = Ledger(lpath)
        led.register_team_work_card("run58", "t_orphan")
        led.put_contract("t_orphan", {"required_files": []}, required_passes=None)
        led.conn.execute(
            "UPDATE harvest_contracts SET worker_report=? WHERE task_id=?",
            (json.dumps({"tokens": _api_snap(90)}), "t_orphan"))
        led.conn.commit()
        led.close()
        out = console.api_team_work_get("run58")
        tt = out["team_work"]["token_total"]
        assert tt["total_tokens"] == 100 and tt["captured"] == 2  # AC-A5
        orphan = next(c for c in out["cards"] if c.get("orphan"))
        assert orphan["worker_report"]["tokens"]["total_tokens"] == 90
    finally:
        console.KANBAN_DB, console.LEDGER_DB = old_k, old_l
        for p in (kpath, lpath):
            try:
                os.unlink(p)
            except OSError:
                pass


def test_A5_empty_run_token_total_present():
    kpath, lpath, (old_k, old_l) = _console_env()
    try:
        led = Ledger(lpath)
        led.create_team_work_run("run58e", "s58e", "empty", "/tmp/x")
        led.close()
        out = console.api_team_work_get("run58e")
        assert out["team_work"]["token_total"] == {
            "total_tokens": 0, "estimated_cost_usd": 0.0,
            "captured": 0, "missing": 0, "source": "unavailable"}
    finally:
        console.KANBAN_DB, console.LEDGER_DB = old_k, old_l
        for p in (kpath, lpath):
            try:
                os.unlink(p)
            except OSError:
                pass
