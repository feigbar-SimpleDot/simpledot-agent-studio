"""O 批簇B 回归测试 (2026-10-31): #177 #175 #171 #173 #174 #172。

依据 docs/FIX-PLAN-O.md §簇B 各条「验证要点」:
  #177  兜底敲门有界化
        - K1 连敲 N 次后第 N+1 轮不敲, exhausted 事件恰 1 条
        - K2 exhausted 后新报告 (set_worker_report 清 confirmed_at) ⇒ 重新武装
  #175  notify_task=None 敲门撞 404 留痕
        - K3 mock 404 + knock_ctx ⇒ 事件落账且 harvest_contracts 无变化
  #171  404 死信前网关路由探测
        - K4 探测 200 + 目标 404 ⇒ 死信 (旧语义不变)
        - K5 探测 404 ⇒ 退避 600s + relay.notify_gateway_misrouted
        - K6 探测连接异常 ⇒ 保守走旧死信
  #173  死信期卡 mismatch 敲门解耦
        - K7 死信卡 (dead_until=now+1年) + mismatch ⇒ 仍敲一次
        - K8 窗口内二次触发 ⇒ 不敲
  #174  TODO.md 路径定位同源
        - K9 pw/workspace 分叉 + _map_path 生效 ⇒ reaper 与 harvest 同路径
  #172  新鲜度容差 env 化
        - K10 MW_FRESH_EPSILON_S 覆盖生效 + 非法值回落

纯本地 ledger 测试, 不依赖 hermes CLI / 网络: 敲门通道与 urlopen 全 patch。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest import mock

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:9")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from mw import reaper as reaper_mod  # noqa: E402
from mw.ledger import Ledger  # noqa: E402
from mw.harvest import (  # noqa: E402
    FRESH_EPSILON, verify_physical, todo_path_for, _fresh_epsilon_s,
    _todo_path_for_harvest,
)

GW = "http://127.0.0.1:9"  # 不会有人接的端口


def _led(tmp_path) -> Ledger:
    return Ledger(str(tmp_path / "ledger.db"))


def _http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "Not Found", Message(), None)


class _OkResp:
    """最小 200 响应替身 (_send_to_main 只读 status + read)。"""

    status = 200

    def getcode(self):
        return 200

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _events(led, task_id, etype):
    return led.conn.execute(
        "SELECT payload FROM mw_events WHERE task_id=? AND event_type=?",
        (task_id, etype)).fetchall()


# ════════════════════════════════════════════════════════════════
# #177 兜底敲门有界化
# ════════════════════════════════════════════════════════════════

def _seed_undelivered_run(led: Ledger, run_id="r_k", task_id="t_k"):
    """种一个「run ACTIVE + 槽位空 + 名下 HARVESTED 未确认 + 闲置超窗」形状。"""
    led.create_team_work_run(run_id, "s_main", "需求")
    led.put_contract(task_id, {"required_files": []})
    led.register_team_work_card(run_id, task_id)
    # 拨成「已收割未送达 + 闲置 2h」
    old = time.time() - 7200
    led.conn.execute(
        "UPDATE harvest_contracts SET harvest_status='HARVESTED', "
        "notify_confirmed_at=NULL, updated_at=?, last_progress_at=? "
        "WHERE task_id=?", (old, old, task_id))
    led.conn.commit()


def _active_run(led, run_id="r_k") -> dict:
    run = dict(led.conn.execute(
        "SELECT * FROM team_work_runs WHERE run_id=?", (run_id,)).fetchone())
    run["current_card_id"] = None  # 槽位空 (兜底敲门形状前提)
    return run


def _knock_once(led, results, run_id="r_k"):
    """跑一轮兜底敲门; 每轮前清 last_null_slot_knock_at (模拟窗口已过)。"""
    led.conn.execute(
        "UPDATE team_work_runs SET last_null_slot_knock_at=NULL "
        "WHERE run_id=?", (run_id,))
    led.conn.commit()
    with mock.patch("mw.reaper._send_to_main", return_value=True) as sent:
        reaper_mod._notify_undelivered_harvest(
            led, _active_run(led, run_id), results,
            stuck_after_min=30, base=".")
    return sent


def test_k1_knock_bounded_and_exhausted_once(tmp_path):
    """K1: 默认上限 3 ⇒ 连敲 3 次, 第 4 轮不敲且 exhausted 事件恰 1 条。"""
    led = _led(tmp_path)
    try:
        _seed_undelivered_run(led)
        results: list = []
        for i in range(3):
            sent = _knock_once(led, results)
            assert sent.call_count == 1, f"第 {i+1} 轮应敲门"
        assert len(_events(led, "r_k", "team_work.undelivered_knock")) == 3
        # 第 4 轮: 达限 ⇒ 不敲
        sent = _knock_once(led, results)
        assert sent.call_count == 0, "达限后仍在敲"
        assert len(_events(led, "r_k", "team_work.undelivered_knock")) == 3
        evs = _events(led, "r_k", "team_work.undelivered_knock_exhausted")
        assert len(evs) == 1, f"exhausted 事件应恰 1 条, 实得 {len(evs)}"
        p = json.loads(evs[0]["payload"])
        assert p["max"] == 3 and p["next_action"], p
        # 第 5 轮: 不敲且 exhausted 不重复
        sent = _knock_once(led, results)
        assert sent.call_count == 0
        assert len(_events(led, "r_k",
                           "team_work.undelivered_knock_exhausted")) == 1
    finally:
        led.close()


def test_k2_new_report_rearms_knock(tmp_path):
    """K2: exhausted 后新报告 (set_worker_report 清 confirmed_at) ⇒ 重新武装。

    时间线仿真: 3 次敲门落在 2h 前 → 新报告 1h 前 (updated_at 刷新,
    晚于全部旧敲门) → 本轮闲置 1h (> stuck 30min) ⇒ 计数窗口 (created_at >
    最新报告) 归零 ⇒ 重新开敲。
    """
    led = _led(tmp_path)
    try:
        _seed_undelivered_run(led)
        results: list = []
        for _ in range(4):
            _knock_once(led, results)
        assert len(_events(led, "r_k",
                           "team_work.undelivered_knock_exhausted")) == 1
        # 把敲门事件拨回 2h 前 (模拟敲门发生在旧报告时代)
        led.conn.execute(
            "UPDATE mw_events SET created_at=created_at-7200 "
            "WHERE task_id='r_k'")
        # 新报告 = 新事件: 内容变化 → updated_at 刷新 + confirmed_at 清
        led.set_worker_report("t_k", {"verdict": "NEED_CLARIFICATION",
                                      "round": 2})
        # 新报告也已闲置 1h (仍无人确认送达)
        aged = time.time() - 3600
        led.conn.execute(
            "UPDATE harvest_contracts SET updated_at=?, last_progress_at=? "
            "WHERE task_id='t_k'", (aged, aged))
        led.conn.commit()
        sent = _knock_once(led, results)
        assert sent.call_count == 1, "新报告后未重新武装"
        assert len(_events(led, "r_k", "team_work.undelivered_knock")) == 4
    finally:
        led.close()


def test_k1b_knock_max_env(tmp_path):
    """上限 env 可配: MW_REAPER_UNDELIVERED_KNOCK_MAX=1 ⇒ 敲 1 次即达限。"""
    led = _led(tmp_path)
    try:
        _seed_undelivered_run(led)
        results: list = []
        with mock.patch.dict(os.environ,
                             {"MW_REAPER_UNDELIVERED_KNOCK_MAX": "1"}):
            sent = _knock_once(led, results)
            assert sent.call_count == 1
            sent = _knock_once(led, results)
            assert sent.call_count == 0
        assert len(_events(led, "r_k",
                           "team_work.undelivered_knock_exhausted")) == 1
        # 非法值回落 3
        with mock.patch.dict(os.environ,
                             {"MW_REAPER_UNDELIVERED_KNOCK_MAX": "abc"}):
            assert reaper_mod._undelivered_knock_max() == 3
        with mock.patch.dict(os.environ,
                             {"MW_REAPER_UNDELIVERED_KNOCK_MAX": "0"}):
            assert reaper_mod._undelivered_knock_max() == 3
    finally:
        led.close()


# ════════════════════════════════════════════════════════════════
# #175 notify_task=None 敲门撞 404 留痕
# ════════════════════════════════════════════════════════════════

def _wait_event(led, task_id, etype, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        evs = _events(led, task_id, etype)
        if evs:
            return evs
        time.sleep(0.05)
    return []


def test_k3_knock_404_leaves_event_no_contract_write(tmp_path):
    """K3: notify_task=None + knock_ctx + 404 ⇒ 事件落账, 契约零变化。"""
    led = _led(tmp_path)
    try:
        led.put_contract("t_175", {"required_files": []})
        before = dict(led.conn.execute(
            "SELECT * FROM harvest_contracts WHERE task_id='t_175'"
        ).fetchone())

        def boom(rq, timeout=None):
            raise _http_error(f"{GW}/api/sessions/s_x/chat", 404)

        with mock.patch("urllib.request.urlopen", side_effect=boom):
            reaper_mod._send_to_main("s_x", "敲门", GW, "k", led.path,
                                      notify_task=None,
                                      knock_ctx={"task_id": "t_175"})
            evs = _wait_event(led, "t_175", "relay.notify_permanent_failure")
        assert evs, "knock 404 未留痕"
        p = json.loads(evs[0]["payload"])
        assert p["reason"] == "session_not_found" and p["http_status"] == 404
        assert p["channel"] == "card_binding_knock", p
        after = dict(led.conn.execute(
            "SELECT * FROM harvest_contracts WHERE task_id='t_175'"
        ).fetchone())
        assert after == before, "knock 留痕污染了 harvest_contracts"
    finally:
        led.close()


# ════════════════════════════════════════════════════════════════
# #171 404 死信前网关路由探测 (三分支)
# ════════════════════════════════════════════════════════════════

def _seed_card(led: Ledger, task_id: str):
    led.put_contract(task_id, {"required_files": []})
    led.record_initiator(task_id, "s_main")


def _send_404_with_probe(led, task_id, probe):
    """目标 chat 恒 404; 探测 GET /api/sessions 行为由 probe 决定。

    probe: "ok" ⇒ 200; "404" ⇒ HTTPError 404; "err" ⇒ 连接异常。
    """
    def boom(rq, timeout=None):
        if rq.full_url.endswith("/api/sessions") and rq.get_method() == "GET":
            if probe == "ok":
                return _OkResp()
            if probe == "404":
                raise _http_error(rq.full_url, 404)
            raise urllib.error.URLError("connection refused")
        raise _http_error(f"{GW}/api/sessions/s_main/chat", 404)

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
        time.sleep(0.2)  # 等事件收尾


def test_k4_probe_healthy_keeps_dead_letter(tmp_path):
    """K4: 探测 200 (路由健康) + 目标 404 ⇒ 首败即死信 (旧语义逐字不变)。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_171a")
        before = time.time()
        _send_404_with_probe(led, "t_171a", "ok")
        r = dict(led.conn.execute(
            "SELECT * FROM harvest_contracts WHERE task_id='t_171a'"
        ).fetchone())
        assert r["notify_dead_until"] and \
            r["notify_dead_until"] > before + 86400 * 300, \
            "路由健康时 404 应仍首败即死信"
        assert _events(led, "t_171a", "relay.notify_permanent_failure")
        assert not _events(led, "t_171a", "relay.notify_gateway_misrouted")
    finally:
        led.close()


def test_k5_probe_404_backoff_not_dead_letter(tmp_path):
    """K5: 探测也 404 (路由错配) ⇒ 不死信, 退避 ~600s + misrouted 事件。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_171b")
        before = time.time()
        _send_404_with_probe(led, "t_171b", "404")
        r = dict(led.conn.execute(
            "SELECT * FROM harvest_contracts WHERE task_id='t_171b'"
        ).fetchone())
        assert r["notify_dead_until"], "路由错配应设临时退避"
        assert r["notify_dead_until"] < before + 3600, \
            f"退避过长 (dead_until={r['notify_dead_until']}), 误走死信?"
        evs = _events(led, "t_171b", "relay.notify_gateway_misrouted")
        assert evs, "路由错配未落 relay.notify_gateway_misrouted"
        assert not _events(led, "t_171b", "relay.notify_permanent_failure")
    finally:
        led.close()


def test_k6_probe_undecidable_falls_back_to_dead_letter(tmp_path):
    """K6: 探测连接异常 (不可判定) ⇒ 保守走旧死信 (不引入新静默)。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_171c")
        before = time.time()
        _send_404_with_probe(led, "t_171c", "err")
        r = dict(led.conn.execute(
            "SELECT * FROM harvest_contracts WHERE task_id='t_171c'"
        ).fetchone())
        assert r["notify_dead_until"] and \
            r["notify_dead_until"] > before + 86400 * 300, \
            "不可判定应保守走旧死信"
    finally:
        led.close()


# ════════════════════════════════════════════════════════════════
# #173 死信期卡 mismatch 敲门解耦
# ════════════════════════════════════════════════════════════════

def _seed_dead_mismatch_card(led: Ledger, task_id="t_173"):
    led.put_contract(task_id, {"required_files": []})
    led.record_initiator(task_id, "s_main")
    # 死信: dead_until = now + 1 年 ⇒ should_notify 恒 False (旧版吞敲门)
    led.conn.execute(
        "UPDATE harvest_contracts SET notify_dead_until=? WHERE task_id=?",
        (time.time() + 86400 * 365, task_id))
    led.conn.commit()


def test_k7_dead_letter_card_still_knocks_once(tmp_path):
    """K7: 死信卡 + mismatch ⇒ 仍敲一次 (去重与 relay 死信解耦)。"""
    led = _led(tmp_path)
    try:
        _seed_dead_mismatch_card(led)
        threshold = int(os.environ.get("MW_REAPER_STUCK_MIN", "30")) * 60
        # 前提自证: 旧去重源 (should_notify) 在死信期恒 False
        assert not led.should_notify("t_173", min_interval_s=threshold)
        results: list = []
        detail = {"actual_kanban_task": "t_other"}
        with mock.patch("mw.reaper._send_to_main", return_value=True) as sent:
            reaper_mod._knock_main_card_binding_mismatch(
                "t_173", led, results, detail)
            assert sent.call_count == 1, "死信期 mismatch 敲门被吞 (#173)"
            # 窗口内二次触发 ⇒ 不敲 (事件窗口兜刷屏)
            reaper_mod._knock_main_card_binding_mismatch(
                "t_173", led, results, detail)
        assert sent.call_count == 1, "窗口内重复敲门 (刷屏回归)"
        assert len(_events(led, "t_173",
                           "harvest.card_binding_mismatch_notified")) == 1
    finally:
        led.close()


# ════════════════════════════════════════════════════════════════
# #174 TODO.md 路径定位同源
# ════════════════════════════════════════════════════════════════

def test_k9_reaper_harvest_same_resolution_under_map(tmp_path):
    """K9: pw 与 workspace 分叉 + _map_path 生效 ⇒ 两侧解析同一路径。"""
    base = str(tmp_path)
    # 容器路径 /ctr/... 映射到宿主 /host/...
    with mock.patch.dict(os.environ, {"MW_PATH_MAP": f"{base}/ctr={base}/host"}):
        # pw 形态: 映射后的 pw/TODO.md 存在
        host_proj = tmp_path / "host" / "proj"
        host_proj.mkdir(parents=True)
        todo = host_proj / "TODO.md"
        todo.write_text("# TODO\n", encoding="utf-8")
        pw = f"{base}/ctr/proj"
        ws = f"{base}/ctr/proj/.tasks/t_x"  # 分叉的 task workspace
        contract = {"product_workspace": pw}
        from mw import reaper as rp
        p_reaper = rp._todo_path_for_task(ws, None, contract)
        p_harvest = _todo_path_for_harvest(pw, ws)
        assert p_reaper == p_harvest == str(todo), (p_reaper, p_harvest)

        # pw 不存在 ⇒ 两侧同走 workspace /.tasks/ 切分 (项目根)
        root2 = tmp_path / "host" / "proj2"
        root2.mkdir()
        todo2 = root2 / "TODO.md"
        todo2.write_text("# TODO\n", encoding="utf-8")
        ws2 = f"{base}/ctr/proj2/.tasks/t_y"
        p_reaper = rp._todo_path_for_task(ws2, None, None)
        p_harvest = _todo_path_for_harvest(None, ws2)
        assert p_reaper == p_harvest == str(todo2), (p_reaper, p_harvest)

        # 全判不出 ⇒ 两侧同为 None (harvest 语义: 校验放行)
        p_reaper = rp._todo_path_for_task(None, None, None)
        p_harvest = _todo_path_for_harvest(None, None)
        assert p_reaper is None and p_harvest is None


# ════════════════════════════════════════════════════════════════
# #172 新鲜度容差 env 化
# ════════════════════════════════════════════════════════════════

def test_k10_fresh_epsilon_env_override_and_fallback():
    """K10: MW_FRESH_EPSILON_S 覆盖生效; 非法/负值回落 2.0; 默认 2.0。"""
    assert FRESH_EPSILON == 2.0  # 既有锚 (test_issue170) 不动
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MW_FRESH_EPSILON_S", None)
        assert _fresh_epsilon_s() == 2.0
    with mock.patch.dict(os.environ, {"MW_FRESH_EPSILON_S": "10"}):
        assert _fresh_epsilon_s() == 10.0
    with mock.patch.dict(os.environ, {"MW_FRESH_EPSILON_S": "abc"}):
        assert _fresh_epsilon_s() == 2.0
    with mock.patch.dict(os.environ, {"MW_FRESH_EPSILON_S": "-1"}):
        assert _fresh_epsilon_s() == 2.0


def test_k10b_epsilon_env_changes_stale_verdict(tmp_path):
    """容差 env 直接影响判定: 偏斜 5s 的产物在 eps=10 下不误标陈旧。"""
    pws = tmp_path / "proj"
    pws.mkdir()
    f = pws / "out.md"
    f.write_text("x", encoding="utf-8")
    base = time.time()
    old = base - 5  # 模拟 NFS 时钟偏斜 5s (> 默认 2.0)
    os.utime(f, (old, old))
    contract = {"required_files": ["out.md"]}
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MW_FRESH_EPSILON_S", None)
        ok_default, _ = verify_physical(contract, str(tmp_path), str(pws),
                                        baseline_ts=base)
        assert ok_default is False, "默认容差 2.0 下应判陈旧 (基线行为)"
    with mock.patch.dict(os.environ, {"MW_FRESH_EPSILON_S": "10"}):
        ok_wide, _ = verify_physical(contract, str(tmp_path), str(pws),
                                     baseline_ts=base)
        assert ok_wide is True, "上调容差到 10 后不应误标陈旧"
