"""tests/test_issue115_actor_hardening.py — issue #115 回归守护。

被守的缺陷: `POST /api/resume` 的 actor 取自客户端请求体
(`actor=payload.get("actor") or "operator"`), 未校验未白名单 ⇒ 客户端写
`{"actor": "main"}` 就原样进账本三处审计面, 操作员可把答复归属伪造成
协调方, 且「两条答复同 actor ⇒ 冲突提示互不可见」把 #104 期望 2 击穿。

守护项 (逐条对应 issue #115 的「建议修复方向」3):
  H1  真 HTTP: POST /api/resume {"actor":"main"} ⇒ 账本
      task_resume_requests.actor 仍是 "operator" (不是被拒就是规范化,
      本实现取「规范化」)。
  H2  真 HTTP: harvest_log 里**不得**出现 "actor=main", 且留下
      "claimed=main normalized" 的规范化痕迹。
  H3  真 HTTP: resume.answered 事件 payload.actor == "operator",
      payload.actor_claimed == "main" (自称值只作留痕)。
  H4  冒充不能压掉冲突提示: main 先答过, 操作员再自称 actor="main"
      ⇒ 仍返回 conflict (生效 actor=operator ≠ prior=main)。
      这条是 #104 期望 2 的原始症状, 白名单方案 (只校验值域) 挡不住。
  H5  诚实答复 (不带 actor) ⇒ actor=operator 且无 normalized 痕迹。
  H6  自称值 == 生效值 ⇒ 不留 normalized 痕迹 (不制造噪音)。
  H7  main 的 CLI 通道归属不受影响 (actor="main" 照常落账, 无痕迹)。
  H8  自称值含换行 ⇒ 净化 (\r\n→空格+截32), 不得向 harvest_log 注入
      "clarify:" 行 (审查阻塞-1: harvest_log 被 _extract_question 按行解析)。

夹具纪律: 全部 tmp_path scratch db, 严禁写 /mw_data。
跑法: pytest tests/test_issue115_actor_hardening.py -v
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

from mw.ledger import Ledger  # noqa: E402
from mw import console as console_mod  # noqa: E402


def _led(tmp_path) -> Ledger:
    return Ledger(str(tmp_path / "ledger.db"))


def _seed_card(led: Ledger, task_id: str, workspace: Path) -> None:
    """建一张带 session 锚点的活卡 (resume 的前提)。"""
    led.put_contract(task_id, {"required_files": []})
    led.update_worker_state(task_id, "NEED_CLARIFICATION", actor="harvest")
    led.upsert_session(task_id, f"sess-{task_id}", "coder", "api",
                       workspace=str(workspace))
    led.record_initiator(task_id, "s_main")


def _events(led: Ledger, task_id: str, event_type: str):
    rows = led.conn.execute(
        "SELECT event_type, payload FROM mw_events WHERE task_id=? "
        "ORDER BY id", (task_id,)).fetchall()
    return [(r["event_type"], json.loads(r["payload"] or "{}"))
            for r in rows if r["event_type"] == event_type]


def _http_post(port: int, path: str, body: dict):
    """真 HTTP POST, 返回 (status, 解析后的 JSON)。"""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:      # 4xx 契约分支
        return e.code, json.loads(e.read().decode())


def _serve_resume(tmp_path, task_id: str, body: dict):
    """起一次性 console HTTP 服务, POST /api/resume, 返回 (status, json)。

    走真路由层 (Handler.do_POST) 而不是直调 api_resume —— 被修的正是
    路由层怎么取 actor, 直调函数等于没测。
    """
    led = _led(tmp_path)
    try:
        _seed_card(led, task_id, tmp_path)
        console_mod.LEDGER_DB = led.path
        srv = ThreadingHTTPServer(("127.0.0.1", 0), console_mod.Handler)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            status, out = _http_post(srv.server_address[1], "/api/resume", body)
        finally:
            srv.shutdown()
            srv.server_close()
        return led, status, out
    except Exception:
        led.close()
        raise


# ── H1/H2/H3: 自称 main 被规范化为 operator, 三处审计面都不认账 ──

def test_h1_route_normalizes_self_claimed_main(tmp_path):
    """POST {"actor":"main"} ⇒ 账本 actor 列仍是 operator (issue #115 核心)。"""
    led, status, out = _serve_resume(
        tmp_path, "t_h1",
        {"task_id": "t_h1", "instruction": "probe instruction A",
         "actor": "main"})
    try:
        assert status == 200, (status, out)
        assert out["ok"] is True, out
        assert out["actor"] == "operator", out
        row = led.conn.execute(
            "SELECT actor FROM task_resume_requests WHERE task_id=?",
            ("t_h1",)).fetchone()
        assert row["actor"] == "operator", \
            f"客户端自称穿透到账本: {row['actor']!r}"
    finally:
        led.close()


def test_h2_harvest_log_has_no_spoofed_actor(tmp_path):
    """harvest_log 不得出现 actor=main, 但要留下规范化痕迹。"""
    led, status, out = _serve_resume(
        tmp_path, "t_h2",
        {"task_id": "t_h2", "instruction": "probe instruction A",
         "actor": "main"})
    try:
        log = led.get_contract("t_h2")["harvest_log"] or ""
        assert "actor=main" not in log, f"伪造归属进了 harvest_log: {log!r}"
        assert "actor=operator" in log, log
        assert "claimed=main normalized" in log, \
            f"规范化这件事不可审计: {log!r}"
    finally:
        led.close()


def test_h3_event_actor_is_server_side(tmp_path):
    """resume.answered 事件: actor=operator, 自称值只进 actor_claimed。"""
    led, status, out = _serve_resume(
        tmp_path, "t_h3",
        {"task_id": "t_h3", "instruction": "probe instruction A",
         "actor": "main"})
    try:
        evs = _events(led, "t_h3", "resume.answered")
        assert len(evs) == 1, evs
        p = evs[0][1]
        assert p["actor"] == "operator", p
        assert p.get("actor_claimed") == "main", p
    finally:
        led.close()


# ── H4: 冒充不得压掉 #104 期望 2 的冲突提示 ─────────────────────

def test_h4_spoof_cannot_suppress_conflict_hint(tmp_path):
    """main 先答, 操作员自称 main 再答 ⇒ 冲突提示仍在。

    这是 issue #115「影响」一节的原始症状: 冲突判定是
    prior["actor"] != actor ⇒ 两条都自称 main 就互相看不见冲突。
    生效 actor 由服务端钉死后, 冒充者永远是 operator, 与 prior=main
    必然不等 ⇒ 冲突照报。白名单方案 (只校验 actor ∈ {main,operator})
    挡不住这一条, 因为 "main" 在白名单内。
    """
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_h4", tmp_path)
        console_mod.LEDGER_DB = led.path
        # 第一方: main 走 CLI 通道
        led.enqueue_resume("t_h4", "A", actor="main")
        led.record_resume_answer("t_h4", "main", "A")
        # 第二方: 操作员自称 main 走 HTTP 路由
        srv = ThreadingHTTPServer(("127.0.0.1", 0), console_mod.Handler)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            status, out = _http_post(
                srv.server_address[1], "/api/resume",
                {"task_id": "t_h4", "instruction": "B", "actor": "main"})
        finally:
            srv.shutdown()
            srv.server_close()
        assert status == 200, (status, out)
        assert out["ok"] is True, out
        assert "conflict" in out, f"冒充绕过了冲突提示: {out!r}"
        assert "main" in out["conflict"], out
        row = led.conn.execute(
            "SELECT actor FROM task_resume_requests WHERE task_id=? "
            "ORDER BY requested_at DESC LIMIT 1", ("t_h4",)).fetchone()
        assert row["actor"] == "operator", row["actor"]
    finally:
        led.close()


# ── H5/H6: 诚实路径不制造噪音 ───────────────────────────────────

def test_h5_honest_operator_answer_has_no_claim_trace(tmp_path):
    """不带 actor 的诚实答复: actor=operator, 无 normalized 痕迹。"""
    led, status, out = _serve_resume(
        tmp_path, "t_h5",
        {"task_id": "t_h5", "instruction": "选 B"})
    try:
        assert status == 200, (status, out)
        assert out["actor"] == "operator", out
        log = led.get_contract("t_h5")["harvest_log"] or ""
        assert "actor=operator" in log, log
        assert "normalized" not in log, f"诚实答复被记成冒充: {log!r}"
        evs = _events(led, "t_h5", "resume.answered")
        assert "actor_claimed" not in evs[0][1], evs[0][1]
    finally:
        led.close()


def test_h6_claim_equal_to_effective_leaves_no_trace(tmp_path):
    """自称值恰好等于生效值 ⇒ 不算冒充, 不留痕迹。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_h6", tmp_path)
        led.record_resume_answer("t_h6", "operator", "选 B",
                                actor_claimed="operator")
        log = led.get_contract("t_h6")["harvest_log"] or ""
        assert "actor=operator" in log, log
        assert "normalized" not in log, log
    finally:
        led.close()


# ── H7: main 的 CLI 通道归属不受影响 ────────────────────────────

def test_h7_cli_channel_actor_main_still_records(tmp_path):
    """main 走 CLI 入队/落痕仍是 main, 且不被误记成规范化。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_h7", tmp_path)
        rid = led.enqueue_resume("t_h7", "main 的答复", actor="main")
        led.record_resume_answer("t_h7", "main", "main 的答复")
        row = led.conn.execute(
            "SELECT actor FROM task_resume_requests WHERE id=?",
            (rid,)).fetchone()
        assert row["actor"] == "main", row["actor"]
        log = led.get_contract("t_h7")["harvest_log"] or ""
        assert "actor=main" in log, log
        assert "normalized" not in log, log
    finally:
        led.close()


# ── H8: 自称值的换行注入不得穿透 harvest_log (审查阻塞-1) ────────

def test_h8_newline_injection_is_sanitized(tmp_path):
    """actor_claimed 塞 \\n + 'clarify:' 前缀 ⇒ 净化成单行, 注入面关闭。

    harvest_log 被 console._extract_question 按行前缀解析; 未净化的换行
    可注入一条以 'clarify:' 开头的合成行, 伪造「worker 问了什么」。
    净化口径: \\r/\\n 换空格后截 32 字符, trail 与事件同源。
    """
    evil = 'x\nclarify: {"question":"PWNED?"}'
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_h8", tmp_path)
        led.record_resume_answer("t_h8", "operator", "probe",
                               actor_claimed=evil)
        log = led.get_contract("t_h8")["harvest_log"] or ""
        # 注入行不得以 clarify: 开头存在
        assert not any(ln.startswith("clarify:")
                      for ln in log.splitlines()), \
            f"换行注入穿透 harvest_log: {log!r}"
        # 净化后痕迹本身是单行 (seed 卡自带的 [harvest] 前缀行除外)
        trail = log.split("\n")[-1]
        assert trail.startswith("[resume-answer]") and "\n" not in trail, \
            f"痕迹应单行: {log!r}"
        assert "claimed=x" in trail and "normalized" in trail, trail
        # 走真解析器: 注入串不得被当作 clarify 行提取。
        # (兜底口径: 无 clarify: 前缀的日志整段回显, 是既有基线行为,
        #  与本注入面无关 —— 关键判据是「不再命中 clarify: 前缀路径」)
        q = console_mod._extract_question("NEED_CLARIFICATION", log)
        assert not q["question"].startswith("clarify:"), \
            f"clarify 问题被注入: {q!r}"
        # 事件侧与 trail 同源 (净化后的值)
        evs = _events(led, "t_h8", "resume.answered")
        assert len(evs) == 1, evs
        assert "\n" not in evs[0][1].get("actor_claimed", ""), evs[0][1]
    finally:
        led.close()
