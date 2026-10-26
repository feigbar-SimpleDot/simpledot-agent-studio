"""tests/test_issue114_post_terminal_output.py — issue #114 终态卡新产出告警。

覆盖 (#104 期望 3 后半句: reaper 检测到终态卡新产出时告警 main):
  A. 触发面
    A1  终态卡 (HARVESTED) 结算后工作区出现新文件 ⇒ 告警
        (worker.state reason=post_terminal_output + _send_to_main + NOTIFIED)。
    A2  CANCELLED / ARCHIVED 同样在检测面内。
    B. 去重
    B1  同一 task 第二轮巡检 ⇒ SKIP, 不再发第二个事件 / 第二次敲门。
  C. 不触发面 (误报控制)
    C1  工作区无结算后新产出 ⇒ 不告警。
    C2  新文件落在「结算+宽限」内 (结算自身写盘) ⇒ 不告警。
    C3  非终态卡 (PENDING) 有新产出 ⇒ 不告警。
    C4  结算时刻落在检测窗口之外 (老账) ⇒ 不扫。
    C5  无 task_sessions.workspace 记录 ⇒ 静默跳过。
  D. 开关与配置
    D1  MW_REAPER_POST_TERMINAL_ALERT=0 ⇒ 整段关闭。
    D2  窗口/宽限 env 解析: 默认值与非法值回退。
  E. 健壮性
    E1  扫描异常 (workspace 指向不存在路径) ⇒ 静默跳过不抛。
    E2  reap_once 集成: 3.7 挂点跑通, 不影响其余步骤。
    E3  扫描上限 cap 生效 (命中数不超过 cap)。

夹具纪律: 全部 tmp_path scratch db, 严禁写 /mw_data。
跑法: pytest tests/test_issue114_post_terminal_output.py -v
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
from mw import reaper as reaper_mod  # noqa: E402


def _led(tmp_path) -> Ledger:
    return Ledger(str(tmp_path / "ledger.db"))


def _seed_terminal(led: Ledger, task_id: str, ws: str,
                  status: str = "HARVESTED",
                  settled_ago: float = 600.0) -> float:
    """建一张已结算终态卡: 契约 + session(workspace) + initiator。

    updated_at 拨到 settled_ago 秒前 (结算时刻), 返回该时刻。
    """
    now = time.time()
    settled = now - settled_ago
    led.put_contract(task_id, {"required_files": []})
    led.conn.execute(
        "UPDATE harvest_contracts SET harvest_status=?, updated_at=? "
        "WHERE task_id=?", (status, settled, task_id))
    led.upsert_session(task_id, f"sess-{task_id}", "coder",
                      runner_kind="api", workspace=ws)
    led.conn.execute(
        "INSERT OR REPLACE INTO task_initiators (task_id, session_id, "
        "created_at) VALUES (?,?,?)", (task_id, "s_main", settled))
    led.conn.commit()
    return settled


def _touch(path: Path, mtime: float) -> None:
    """写文件并把 mtime 钉到指定时刻 (消除时钟抖动)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("post-terminal write\n")
    os.utime(path, (mtime, mtime))


def _run_alert(led: Ledger, **kw):
    """跑一次检测, 返回 (敲门消息列表, results)。_send_to_main 被 mock。"""
    sent = []
    with mock.patch.object(reaper_mod, "_send_to_main",
                          lambda *a, **k: sent.append(a) or True):
        results = []
        reaper_mod._notify_post_terminal_output(led, None, results, **kw)
    return [a[1] for a in sent], results


def _alerts(led: Ledger, task_id: str) -> list[dict]:
    rows = led.conn.execute(
        "SELECT payload FROM mw_events WHERE event_type='worker.state' "
        "AND task_id=? AND payload LIKE '%post_terminal_output%' "
        "ORDER BY id", (task_id,)).fetchall()
    return [json.loads(r["payload"]) for r in rows]


# ── A: 触发面 ──────────────────────────────────────────────────────

def test_a1_new_file_after_settlement_alerts(tmp_path):
    """终态卡结算后工作区出现新文件 ⇒ 告警 main + 事件留痕。"""
    led = _led(tmp_path)
    try:
        ws = tmp_path / "ws_a1"
        settled = _seed_terminal(led, "t_a1", str(ws))
        # 结算 5 分钟后出现新产出 (宽限 120s 之外)
        _touch(ws / "src" / "late_change.py", settled + 300)
        msgs, results = _run_alert(led)
        assert len(msgs) == 1, f"未告警: {msgs} {results}"
        assert "t_a1" in msgs[0] and "新产出" in msgs[0], msgs[0]
        assert any(r.get("verdict") == "NOTIFIED" for r in results), results
        evs = _alerts(led, "t_a1")
        assert len(evs) == 1, evs
        assert evs[0]["reason"] == "post_terminal_output", evs[0]
        assert "late_change.py" in evs[0]["post_terminal_file"], evs[0]
        assert evs[0]["actor"] == "reaper", evs[0]
    finally:
        led.close()


def test_a2_cancelled_archived_also_covered(tmp_path):
    """CANCELLED / ARCHIVED 与 HARVESTED 同在终态检测面。"""
    led = _led(tmp_path)
    try:
        for tid, st in (("t_cancel", "CANCELLED"), ("t_arch", "ARCHIVED")):
            ws = tmp_path / f"ws_{tid}"
            settled = _seed_terminal(led, tid, str(ws), status=st)
            _touch(ws / "f.py", settled + 300)
        msgs, results = _run_alert(led)
        notified = {r["task_id"] for r in results
                    if r.get("verdict") == "NOTIFIED"}
        assert notified == {"t_cancel", "t_arch"}, results
    finally:
        led.close()


# ── B: 去重 (每 task 只告警一次) ─────────────────────────────────

def test_b1_second_round_skips(tmp_path):
    """第二轮巡检同一 task ⇒ SKIP, 不重复事件 / 不重复敲门。"""
    led = _led(tmp_path)
    try:
        ws = tmp_path / "ws_b1"
        settled = _seed_terminal(led, "t_b1", str(ws))
        _touch(ws / "f.py", settled + 300)
        msgs1, _ = _run_alert(led)
        assert len(msgs1) == 1, msgs1
        # 再来一轮 (新产出仍在)
        msgs2, results2 = _run_alert(led)
        assert not msgs2, f"第二轮重复敲门: {msgs2}"
        assert any(r.get("verdict") == "SKIP" for r in results2), results2
        assert len(_alerts(led, "t_b1")) == 1, "第二轮重复落了事件"
    finally:
        led.close()


# ── C: 不触发面 (误报控制) ────────────────────────────────────────

def test_c1_no_new_output_no_alert(tmp_path):
    """工作区文件全部早于结算时刻 ⇒ 不告警。"""
    led = _led(tmp_path)
    try:
        ws = tmp_path / "ws_c1"
        settled = _seed_terminal(led, "t_c1", str(ws))
        _touch(ws / "old.py", settled - 100)
        msgs, _ = _run_alert(led)
        assert not msgs, f"无新产出仍告警: {msgs}"
    finally:
        led.close()


def test_c2_within_grace_not_alerted(tmp_path):
    """新文件落在「结算+宽限 120s」内 = 结算自身写盘 ⇒ 不告警。"""
    led = _led(tmp_path)
    try:
        ws = tmp_path / "ws_c2"
        settled = _seed_terminal(led, "t_c2", str(ws))
        _touch(ws / "harvest_own_write.log", settled + 60)  # 宽限内
        msgs, _ = _run_alert(led)
        assert not msgs, f"宽限内写盘被误报: {msgs}"
    finally:
        led.close()


def test_c3_non_terminal_not_scanned(tmp_path):
    """PENDING 卡有新产出 ⇒ 不在检测面 (那是正常施工)。"""
    led = _led(tmp_path)
    try:
        ws = tmp_path / "ws_c3"
        settled = _seed_terminal(led, "t_c3", str(ws), status="PENDING")
        _touch(ws / "working.py", settled + 300)
        msgs, _ = _run_alert(led)
        assert not msgs, f"非终态卡被告警: {msgs}"
    finally:
        led.close()


def test_c4_outside_window_not_scanned(tmp_path):
    """结算时刻在检测窗口 (默认 30min) 之外 ⇒ 老账不翻。"""
    led = _led(tmp_path)
    try:
        ws = tmp_path / "ws_c4"
        settled = _seed_terminal(led, "t_c4", str(ws), settled_ago=7200)
        _touch(ws / "f.py", settled + 300)
        msgs, _ = _run_alert(led)  # 默认窗口 30min
        assert not msgs, f"窗口外老账被翻出: {msgs}"
        # 把窗口放大到 3h ⇒ 同一数据应命中 (证窗口真的在起作用)
        msgs2, _ = _run_alert(led, window_s=3 * 3600)
        assert len(msgs2) == 1, f"放大窗口后应命中: {msgs2}"
    finally:
        led.close()


def test_c5_no_workspace_silent(tmp_path):
    """无 task_sessions.workspace 记录 ⇒ 无从检测, 静默跳过。"""
    led = _led(tmp_path)
    try:
        settled = time.time() - 600
        led.put_contract("t_c5", {"required_files": []})
        led.conn.execute(
            "UPDATE harvest_contracts SET harvest_status='HARVESTED', "
            "updated_at=? WHERE task_id=?", (settled, "t_c5"))
        led.conn.commit()
        msgs, results = _run_alert(led)
        assert not msgs, msgs
        assert not any(r.get("verdict") == "NOTIFIED" for r in results), results
    finally:
        led.close()


# ── D: 开关与配置 ─────────────────────────────────────────────────

def test_d1_switch_off_disables(monkeypatch, tmp_path):
    """MW_REAPER_POST_TERMINAL_ALERT=0 ⇒ 整段关闭。"""
    monkeypatch.setenv("MW_REAPER_POST_TERMINAL_ALERT", "0")
    led = _led(tmp_path)
    try:
        ws = tmp_path / "ws_d1"
        settled = _seed_terminal(led, "t_d1", str(ws))
        _touch(ws / "f.py", settled + 300)
        msgs, results = _run_alert(led)
        assert not msgs, f"开关关闭仍告警: {msgs}"
        assert not results, results
    finally:
        led.close()


def test_d2_env_defaults_and_fallback(monkeypatch):
    """窗口/宽限: 默认 30min / 120s; 非法值回退默认。"""
    monkeypatch.delenv("MW_REAPER_POST_TERMINAL_WINDOW_MIN", raising=False)
    monkeypatch.delenv("MW_REAPER_POST_TERMINAL_GRACE_S", raising=False)
    assert reaper_mod._post_terminal_window_s() == 1800.0
    assert reaper_mod._post_terminal_grace_s() == 120.0
    monkeypatch.setenv("MW_REAPER_POST_TERMINAL_WINDOW_MIN", "5")
    monkeypatch.setenv("MW_REAPER_POST_TERMINAL_GRACE_S", "30")
    assert reaper_mod._post_terminal_window_s() == 300.0
    assert reaper_mod._post_terminal_grace_s() == 30.0
    monkeypatch.setenv("MW_REAPER_POST_TERMINAL_WINDOW_MIN", "abc")
    monkeypatch.setenv("MW_REAPER_POST_TERMINAL_GRACE_S", "")
    assert reaper_mod._post_terminal_window_s() == 1800.0
    assert reaper_mod._post_terminal_grace_s() == 120.0


# ── E: 健壮性 ─────────────────────────────────────────────────────

def test_e1_missing_workspace_dir_silent(tmp_path):
    """workspace 指向不存在的路径 ⇒ 静默跳过, 不抛异常。"""
    led = _led(tmp_path)
    try:
        _seed_terminal(led, "t_e1", str(tmp_path / "gone_dir"))
        msgs, results = _run_alert(led)  # 不应抛
        assert not msgs, msgs
    finally:
        led.close()


def test_e2_reap_once_integration(tmp_path):
    """reap_once 集成: 3.7 挂点跑通, 告警照常, 其余步骤不受影响。"""
    led = _led(tmp_path)
    try:
        # 一个 ACTIVE run (让 reap_once 走完整流程)
        led.conn.execute(
            "INSERT INTO team_work_runs (run_id, initiator_session, "
            "initial_task, project_workspace, current_card_id, status, "
            "card_count, created_at) VALUES ('r_e2','s_main','t','/tmp',"
            "NULL,'ACTIVE',1,?)", (time.time() - 600,))
        led.conn.commit()
        ws = tmp_path / "ws_e2"
        settled = _seed_terminal(led, "t_e2", str(ws))
        _touch(ws / "ghost_write.py", settled + 300)
        with mock.patch.object(reaper_mod, "_send_to_main",
                              lambda *a, **k: True):
            rs = reaper_mod.reap_once(ledger=led, hermes_home=str(tmp_path),
                                     base=str(tmp_path))
        hits = [r for r in rs if r["trigger"] == "post_terminal_output"]
        assert any(h["verdict"] == "NOTIFIED" for h in hits), rs
        # 再跑一轮: 去重戳生效, 不再 NOTIFIED
        with mock.patch.object(reaper_mod, "_send_to_main",
                              lambda *a, **k: True):
            rs2 = reaper_mod.reap_once(ledger=led, hermes_home=str(tmp_path),
                                      base=str(tmp_path))
        hits2 = [r for r in rs2 if r["trigger"] == "post_terminal_output"]
        assert all(h["verdict"] != "NOTIFIED" for h in hits2), rs2
    finally:
        led.close()


def test_e3_scan_cap_bounds_work(tmp_path):
    """cap 生效: 新产出在 cap 之后 ⇒ 扫不到 (有界遍历, 不无限扫大仓)。"""
    ws = tmp_path / "ws_e3"
    ws.mkdir()
    base_ts = time.time() - 1000
    # 前 10 个文件是旧的, 第 11 个是新产出
    for i in range(10):
        _touch(ws / f"f{i:02d}.py", base_ts - 100)
    _touch(ws / "f10_new.py", base_ts + 500)
    # cap=5: 只扫前 5 个 (全旧) ⇒ 无命中
    assert reaper_mod._find_new_output(ws, base_ts, cap=5) is None
    # cap=50: 扫到第 11 个 ⇒ 命中
    hit = reaper_mod._find_new_output(ws, base_ts, cap=50)
    assert hit is not None and "f10_new" in hit[0], hit
