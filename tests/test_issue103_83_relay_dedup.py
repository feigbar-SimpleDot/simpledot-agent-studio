"""tests/test_issue103_83_relay_dedup.py — issue #83 + #103 回归测试。

覆盖 issue #83 (报告转发风暴) 与 issue #103 (投递失败不留痕 + F4 空窗):

  A (issue #83 验收判据: 并发投递只有一个线程 POST):
    A1  并发发起两个 _send_to_main, 只有第一个真正 POST;
        第二个在 POST 前二次核状态时被挡下。
        2026-10-25 D-1 修 (Gitea #111): 慢响应 mock + Barrier 真重叠,
        断言「发送窗口内实际 POST 数 = 1」(摘掉单飞 claim 即转红)。
    A2  confirmed_at 已置位时, 发送线程直接放弃不 POST。
    A3  run 已非 ACTIVE 时, 发送线程直接放弃不 POST (F7 语义)。

  B (issue #83 B 项: 退避封顶 >= 发送 timeout):
    B1  _relay_backoff_cap_s() >= 发送 timeout。
    B2  显式把封顶设得比 timeout 小时, 仍被抬到 timeout+1 (只允许更保守)。

  C (issue #83 B 项 + issue #103: 失败留痕):
    C1  投递失败后 last_notified_at **不被清空** (保持「在途」态)。
    C2  投递失败后 notify_last_failed_at / notify_last_error 落库 (留痕)。
    C3  投递失败落 relay.notify_failed 事件。
    C4  失败后 claim_for_relay 在 inflight 窗口内不再放行第二个线程。

  D (issue #103: F4 兜底敲门补一档):
    D1  「已收割未送达」形状会触发敲门 (含失败次数文案)。
    D2  已确认送达的卡不触发敲门。
    D3  闲置未超 stuck 窗口不触发敲门。

夹具纪律: 全部 tmp_path scratch db, 严禁写 /mw_data。
跑法: pytest tests/test_issue103_83_relay_dedup.py -v
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

from mw.ledger import Ledger  # noqa: E402
from mw import reaper as reaper_mod  # noqa: E402

GW = "http://127.0.0.1:9"  # 不会有人接的端口 (失败路径)


def _led(tmp_path) -> Ledger:
    return Ledger(str(tmp_path / "ledger.db"))


def _seed_reported(led: Ledger, task_id: str, run_id: str | None = None,
                   status: str = "HARVESTED") -> None:
    """建一张已落报告的卡 (relay 扫描面)。"""
    led.put_contract(task_id, {"required_files": []})
    if status != "PENDING":
        led.conn.execute(
            "UPDATE harvest_contracts SET harvest_status=?, worker_report=? "
            "WHERE task_id=?",
            (status, json.dumps({"verdict": "COMPLETE",
                                  "evidence": ["a"]}), task_id))
    if run_id:
        led.conn.execute(
            "UPDATE harvest_contracts SET team_work_run_id=? WHERE task_id=?",
            (run_id, task_id))
    led.conn.execute(
        "INSERT OR REPLACE INTO task_initiators (task_id, session_id, "
        "created_at) VALUES (?,?,?)", (task_id, "s_main", time.time()))
    led.conn.commit()


def _new_run(led: Ledger, run_id: str, *, current_card: str | None = None,
             age_s: float = 600.0) -> None:
    led.conn.execute(
        "UPDATE team_work_runs SET status='ARCHIVED' WHERE status='ACTIVE'")
    led.conn.execute(
        "INSERT INTO team_work_runs (run_id, initiator_session, initial_task,"
        " project_workspace, current_card_id, status, card_count, created_at)"
        " VALUES (?,?,'task',?,?, 'ACTIVE',1,?)",
        (run_id, "s_main", "/tmp", current_card, time.time() - age_s))
    led.conn.commit()


# ── A: 并发投递只有一个线程 POST (issue #83 验收) ───────────────

class _OkResp:
    """假 gateway 响应: HTTP 200。"""

    status = 200

    def read(self):
        return b"{}"

    def getcode(self):
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_a1_concurrent_sends_only_one_posts(tmp_path):
    """并发发起两个投递, 只有第一个真正 POST (第二个被单飞 claim 挡下)。

    旧版: 两个线程都 POST ⇒ main 收到同一份报告两次 (issue #83 实测
    20 分钟内 6 条重复转发, 每条消耗 main 一个完整 turn)。

    2026-10-25 D-1 修 (Gitea #111): 旧版 mock **立即返回** ⇒ 第一个线程
    在第二个线程起跑前就已跑完整个「claim → precheck → POST → confirm」
    链路并把 notify_confirmed_at 置位 ⇒ 第二个线程是被 **precheck** 挡下
    的, 摘掉发送层单飞 claim 本用例仍恒绿 (s5 变异 M13 实测 mut_exit=0,
    移交项 D-1)。现在改成真正的竞态窗口:
      - Barrier: 两个投递线程在同一瞬间进入发送窗口 (真重叠);
      - 阻塞式慢响应 mock: 第一个 POST 进入后由测试放行 ⇒ 窗口内第一个
        线程**尚未**收到 200 (notify_confirmed_at 仍未置位);
      - 断言「窗口内实际 POST 数 = 1」+「全程 POST 数 = 1」。
    ⇒ 摘掉发送层单飞 claim 时, 第二个线程的 precheck 会放行 ⇒ 窗口内
      POST 数 = 2 ⇒ 本用例转红 (变异自证), 复原转绿。
    """
    led = _led(tmp_path)
    try:
        _seed_reported(led, "t_a1")
        posts = []
        post_lock = threading.Lock()
        entered = threading.Semaphore(0)      # POST 进入窗口的信号
        release = threading.Event()            # 放行第一个 POST 的响应
        barrier = threading.Barrier(2, timeout=10)   # 两线程真重叠

        def fake_urlopen(rq, timeout=None):
            with post_lock:
                posts.append(rq)
            entered.release()
            release.wait(timeout=10)   # 慢响应: 阻塞在窗口里, 由测试放行
            return _OkResp()

        errors = []

        def sender():
            try:
                barrier.wait()      # 两线程同一瞬间进入发送窗口
                reaper_mod._send_to_main("s_main", "报告", GW, "k", led.path,
                                         notify_task="t_a1")
            except Exception as e:  # 记下异常, 断言时给上下文
                errors.append(e)

        try:
            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                threads = [threading.Thread(target=sender) for _ in range(2)]
                for t in threads:
                    t.start()
                # 等第一个 POST 进入 ⇒ 窗口打开
                assert entered.acquire(timeout=10), "第一个 POST 未进入发送窗口"
                # 给第二个线程留出 claim + precheck 的时间。
                # 等待上限 5s (正常毫秒级即到, 留足慢机器余量); 第二个 POST
                # 一旦出现立即结束 ⇒ 摘掉 claim 的变异会稳定转红。
                deadline = time.time() + 5.0
                while time.time() < deadline and len(posts) < 2:
                    time.sleep(0.02)
                posts_in_window = len(posts)      # 窗口内实际 POST 数
                release.set()                     # 放行第一个 POST
                for t in threads:
                    t.join(timeout=15)
                # 等发送线程收尾 (成功 ⇒ confirm_delivered 置位)
                deadline = time.time() + 5.0
                confirmed = None
                while time.time() < deadline:
                    confirmed = led.conn.execute(
                        "SELECT notify_confirmed_at FROM harvest_contracts "
                        "WHERE task_id='t_a1'").fetchone()["notify_confirmed_at"]
                    if confirmed is not None:
                        break
                    time.sleep(0.05)
        finally:
            release.set()

        assert not errors, errors
        assert posts_in_window == 1, (
            f"发送窗口内并发 POST 了 {posts_in_window} 次, 应为 1 "
            f"(发送层单飞 claim 被摘掉了?): {posts}")
        assert len(posts) == 1, f"并发投递发了 {len(posts)} 次 POST, 应为 1"
        # 第一次成功 → confirmed_at 置位
        assert confirmed is not None, "成功后未确认送达"
    finally:
        led.close()


def test_a2_precheck_blocks_when_already_confirmed(tmp_path):
    """confirmed_at 已置位 ⇒ 发送线程放弃不 POST。"""
    led = _led(tmp_path)
    try:
        _seed_reported(led, "t_a2")
        led.confirm_delivered("t_a2")
        posts = []
        with mock.patch("urllib.request.urlopen",
                        side_effect=lambda rq, timeout=None: posts.append(rq)
                        or _OkResp()):
            reaper_mod._send_to_main("s_main", "报告", GW, "k", led.path,
                                     notify_task="t_a2")
            time.sleep(1.0)
        assert not posts, f"已确认送达仍发了 POST: {len(posts)}"
    finally:
        led.close()


def test_a3_precheck_blocks_when_run_not_active(tmp_path):
    """run 已非 ACTIVE ⇒ 发送线程放弃不 POST (F7 语义保留)。"""
    led = _led(tmp_path)
    try:
        _new_run(led, "r_a3")
        _seed_reported(led, "t_a3", run_id="r_a3")
        led.conn.execute(
            "UPDATE team_work_runs SET status='CLOSED' WHERE run_id='r_a3'")
        led.conn.commit()
        posts = []
        with mock.patch("urllib.request.urlopen",
                        side_effect=lambda rq, timeout=None: posts.append(rq)
                        or _OkResp()):
            reaper_mod._send_to_main("s_main", "报告", GW, "k", led.path,
                                     notify_task="t_a3")
            time.sleep(1.0)
        assert not posts, f"run 已关仍发了 POST: {len(posts)}"
        # relay.skipped 事件留痕
        evs = [r["event_type"] for r in led.conn.execute(
            "SELECT event_type FROM mw_events WHERE task_id='t_a3'").fetchall()]
        assert "relay.skipped" in evs, evs
    finally:
        led.close()


# ── B: 退避封顶 >= 发送 timeout (issue #83 B 项) ────────────────

def test_b1_backoff_cap_ge_send_timeout():
    """退避封顶必须 >= 发送 timeout (否则退避到期时上个线程仍在途)。"""
    cap = reaper_mod._relay_backoff_cap_s()
    timeout_s = reaper_mod._relay_send_timeout_s()
    assert cap >= timeout_s, f"退避封顶 {cap} < 发送 timeout {timeout_s}"
    assert cap > 300, f"退避封顶仍是旧版的 300s: {cap}"


def test_b2_backoff_cap_cannot_be_set_below_timeout(monkeypatch):
    """显式把封顶设得比 timeout 小时, 仍被抬到 timeout+1 (只允许更保守)。"""
    monkeypatch.setenv("MW_RELAY_SEND_TIMEOUT_S", "600")
    monkeypatch.setenv("MW_RELAY_BACKOFF_CAP_S", "120")
    cap = reaper_mod._relay_backoff_cap_s()
    assert cap >= 601, f"显式小封顶未被抬升: {cap}"


# ── C: 失败留痕 + 不清 last_notified_at (issue #83 B + #103) ─────

def _fail_once(led: Ledger, task_id: str) -> None:
    """让一次投递失败 (连接被拒)。"""
    def boom(rq, timeout=None):
        raise OSError("connection refused (test fixture)")
    with mock.patch("urllib.request.urlopen", side_effect=boom):
        reaper_mod._send_to_main("s_main", "报告", GW, "k", led.path,
                                 notify_task=task_id)
        time.sleep(1.5)


def test_c1_failure_keeps_last_notified_at(tmp_path):
    """投递失败后 last_notified_at **不被清空**。

    旧版失败路径清 last_notified_at=NULL ⇒ 退避到期 (旧版封顶 300s <
    发送 timeout 600s) 时 claim_for_relay 放行, 上一个线程可能仍在
    600s 阻塞 ⇒ 同一份报告多个线程并发在途。
    """
    led = _led(tmp_path)
    try:
        _seed_reported(led, "t_c1")
        _fail_once(led, "t_c1")
        row = led.conn.execute(
            "SELECT last_notified_at, notify_attempts FROM harvest_contracts "
            "WHERE task_id='t_c1'").fetchone()
        assert row["last_notified_at"] is not None, \
            "失败后 last_notified_at 被清空了 (并发在途窗口回来了)"
        assert row["notify_attempts"] >= 1, dict(row)
    finally:
        led.close()


def test_c2_failure_records_trace_columns(tmp_path):
    """投递失败留痕: notify_last_failed_at / notify_last_error 落库。"""
    led = _led(tmp_path)
    try:
        _seed_reported(led, "t_c2")
        _fail_once(led, "t_c2")
        row = led.conn.execute(
            "SELECT notify_last_failed_at, notify_last_error, "
            "notify_dead_until FROM harvest_contracts WHERE task_id='t_c2'"
        ).fetchone()
        assert row["notify_last_failed_at"] is not None, \
            "失败时刻未落账 (issue #103: 过程无痕)"
        assert row["notify_last_error"], "失败原因未落账 (issue #103)"
        assert "connection refused" in row["notify_last_error"], \
            row["notify_last_error"]
        assert row["notify_dead_until"] is not None, "退避期未落账"
    finally:
        led.close()


def test_c3_failure_emits_event(tmp_path):
    """投递失败落 relay.notify_failed 事件 (可被 SSE/webhook 订阅)。"""
    led = _led(tmp_path)
    try:
        _seed_reported(led, "t_c3")
        _fail_once(led, "t_c3")
        rows = led.conn.execute(
            "SELECT payload FROM mw_events WHERE task_id='t_c3' "
            "AND event_type='relay.notify_failed'").fetchall()
        assert rows, "失败未落 relay.notify_failed 事件"
        p = json.loads(rows[0]["payload"])
        assert p["attempts"] >= 1, p
        assert p["error"], p
        assert p["failed_at"] > 0, p
    finally:
        led.close()


def test_c4_claim_blocked_after_failure_in_inflight_window(tmp_path):
    """失败后 inflight 窗口内不得再 claim (同一份报告不会多线程在途)。"""
    led = _led(tmp_path)
    try:
        _seed_reported(led, "t_c4")
        _fail_once(led, "t_c4")
        assert led.claim_for_relay("t_c4") is False, \
            "失败后在途窗口内仍可再 claim (并发在途窗口回来了)"
    finally:
        led.close()


def test_c5_confirm_clears_failure_trace(tmp_path):
    """确认送达后失败痕迹清干净 (新一轮报告重新计数)。"""
    led = _led(tmp_path)
    try:
        _seed_reported(led, "t_c5")
        _fail_once(led, "t_c5")
        led.confirm_delivered("t_c5")
        row = led.conn.execute(
            "SELECT notify_last_failed_at, notify_last_error, "
            "notify_attempts FROM harvest_contracts WHERE task_id='t_c5'"
        ).fetchone()
        assert row["notify_last_failed_at"] is None, dict(row)
        assert row["notify_last_error"] is None, dict(row)
        assert row["notify_attempts"] == 0, dict(row)
    finally:
        led.close()


# ── D: F4 兜底敲门补一档 (issue #103) ────────────────────────────

def _knock_undelivered(led: Ledger, run_id: str, *, stuck_after_min=5):
    """跑一次「已收割未送达」兜底敲门, 返回 (消息列表, results)。"""
    sent = []
    with mock.patch.object(reaper_mod, "_send_to_main",
                           lambda *a, **k: sent.append(a) or True):
        results = []
        ar = led.get_team_work(run_id)
        reaper_mod._notify_undelivered_harvest(
            led, ar, results, stuck_after_min=stuck_after_min, base=str(ROOT))
    return [a[1] for a in sent], results


def test_d1_knocks_when_harvested_but_undelivered(tmp_path):
    """run ACTIVE + 槽位 NULL + 存在 HARVESTED 且未确认送达的卡
    + 闲置超 stuck 窗口 ⇒ 敲门「有已结算卡未送达 main」。"""
    led = _led(tmp_path)
    try:
        _new_run(led, "r_d1", current_card=None, age_s=4000)
        _seed_reported(led, "t_d1", run_id="r_d1")
        # 把卡的动静时刻拨到很久以前 (制造「闲置超窗」)
        old = time.time() - 4000
        led.conn.execute(
            "UPDATE harvest_contracts SET updated_at=?, last_progress_at=?, "
            "last_notified_at=?, notify_last_failed_at=? WHERE task_id='t_d1'",
            (old, old, old, old))
        led.conn.commit()
        msgs, results = _knock_undelivered(led, "r_d1")
        assert len(msgs) == 1, f"「已收割未送达」未敲门: {msgs} {results}"
        assert "未送达" in msgs[0], msgs[0]
        assert "t_d1" in msgs[0], msgs[0]
        assert any(r.get("verdict") == "NOTIFIED" for r in results), results
    finally:
        led.close()


def test_d2_no_knock_when_confirmed(tmp_path):
    """已确认送达的卡不触发敲门 (别打扰已处理的 run)。"""
    led = _led(tmp_path)
    try:
        _new_run(led, "r_d2", current_card=None, age_s=4000)
        _seed_reported(led, "t_d2", run_id="r_d2")
        old = time.time() - 4000
        led.conn.execute(
            "UPDATE harvest_contracts SET updated_at=?, last_progress_at=? "
            "WHERE task_id='t_d2'", (old, old))
        led.confirm_delivered("t_d2")
        led.conn.commit()
        msgs, results = _knock_undelivered(led, "r_d2")
        assert not msgs, f"已确认送达仍敲门: {msgs}"
    finally:
        led.close()


def test_d3_no_knock_within_stuck_window(tmp_path):
    """闲置未超 stuck 窗口 ⇒ 不敲门。"""
    led = _led(tmp_path)
    try:
        _new_run(led, "r_d3", current_card=None, age_s=4000)
        _seed_reported(led, "t_d3", run_id="r_d3")
        now = time.time()
        led.conn.execute(
            "UPDATE harvest_contracts SET updated_at=?, last_progress_at=? "
            "WHERE task_id='t_d3'", (now, now))
        led.conn.commit()
        msgs, results = _knock_undelivered(led, "r_d3", stuck_after_min=30)
        assert not msgs, f"stuck 窗口内仍敲门: {msgs}"
    finally:
        led.close()


def test_d4_knock_message_carries_failure_count(tmp_path):
    """敲门文案带失败次数 —— main 一眼看出「不是没收到, 是投递一直失败」。"""
    led = _led(tmp_path)
    try:
        _new_run(led, "r_d4", current_card=None, age_s=4000)
        _seed_reported(led, "t_d4", run_id="r_d4")
        old = time.time() - 4000
        led.conn.execute(
            "UPDATE harvest_contracts SET updated_at=?, last_progress_at=?, "
            "notify_attempts=?, notify_last_error=? WHERE task_id='t_d4'",
            (old, old, 8, "connection refused"))
        led.conn.commit()
        msgs, _results = _knock_undelivered(led, "r_d4")
        assert len(msgs) == 1, msgs
        assert "失败" in msgs[0], msgs[0]
        assert "8" in msgs[0], msgs[0]
    finally:
        led.close()


def test_d5_no_knock_when_run_has_current_card(tmp_path):
    """run 槽位非空时不敲 (有当前卡 → 走正常结算/stuck 路径)。"""
    led = _led(tmp_path)
    try:
        _new_run(led, "r_d5", current_card="t_cur", age_s=4000)
        _seed_reported(led, "t_d5", run_id="r_d5")
        old = time.time() - 4000
        led.conn.execute(
            "UPDATE harvest_contracts SET updated_at=?, last_progress_at=? "
            "WHERE task_id='t_d5'", (old, old))
        led.conn.commit()
        msgs, _results = _knock_undelivered(led, "r_d5")
        assert not msgs, f"槽位非空仍敲门: {msgs}"
    finally:
        led.close()


def test_d6_harvested_undelivered_query_filters_correctly(tmp_path):
    """ledger.harvested_undelivered 只取「HARVESTED + 未确认送达」的卡。"""
    led = _led(tmp_path)
    try:
        _new_run(led, "r_d6")
        _seed_reported(led, "t_h", run_id="r_d6")            # HARVESTED 未确认
        _seed_reported(led, "t_c", run_id="r_d6")
        led.confirm_delivered("t_c")                         # 已确认
        _seed_reported(led, "t_p", run_id="r_d6", status="PENDING")  # 未结算
        rows = led.harvested_undelivered("r_d6")
        ids = [r["task_id"] for r in rows]
        assert ids == ["t_h"], ids
    finally:
        led.close()
