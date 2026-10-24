"""F4b (2026-10-09): null_slot 兜底敲门误报修复的永久回归测试。

移植自 .tasks/t_dc277ea3/testrun/test_f4b_full.py (testing 步独立夹具),
按 DESIGN §5/§6 承诺入库, 覆盖 T1-T8 + relay env 补充共 12 条语义。

覆盖 (对应 docs/DESIGN.md §6 / docs/REQ.md 验收标准):
  T1  current_card_cleared_at 迁移幂等, 旧行 NULL (AC-1)。
  T2  ledger 维护点: clear 落戳 / set 不重置 cleared_at (AC-2, B-2 不变式)。
  T3  核心回归: 清槽 6s 前的原误报场景必须零敲 (AC-3)。
  T4a relay confirmed 在窗内 → SUPPRESSED_RELAY + trigger/note (AC-4/AC-9)。
  T4b 在途 claim (COALESCE last_notified_at) 同样抑制。
  T4c relay 超窗 → 不抑制, 继续走层3 照敲。
  T5a initiator 刚活跃 → SUPPRESSED_INITIATOR (AC-5)。
  T5b MW_REAPER_INITIATOR_LIVENESS=0 → 层3 跳过 → 敲。
  T6  真死 main 仍敲, 文案「等待 main 派发」且 mins≈60, 去重戳落库 (AC-4/G4)。
  T7  已派发卡 + console 报死 → 照敲, 不扰动 dispatched 路径 (AC-7)。
  T8  initiator 查询抛异常 → fail-open 照敲 (AC-8)。
  补充 MW_REAPER_RELAY_SUPPRESS_WINDOW_S env 收紧窗口生效性 (DESIGN §4)。

夹具: 全部 tmp_path scratch db, 严禁写 /mw_data。
跑法: pytest tests/test_f4b_null_slot_suppression.py
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

# reaper 敲门路径要求 API_SERVER_KEY 非空
os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")  # 不会真连

from mw.ledger import Ledger  # noqa: E402
from mw import reaper as reaper_mod  # noqa: E402

STUCK_MIN = 5  # threshold = 300s


class Harness:
    """scratch db 夹具: 每次新建 tmpdir + Ledger, 收尾 close。"""

    def __init__(self, tmpdir):
        self.tmpdir = str(tmpdir)
        self.db = os.path.join(self.tmpdir, "ledger.db")
        self.led = Ledger(self.db)
        self.now = time.time()

    def close(self):
        self.led.close()

    def new_run(self, rid, *, run_age_s, cleared_at, status="ACTIVE"):
        """建 run 行: 显式控制 run 年龄与清槽时刻。"""
        self.led.conn.execute(
            "UPDATE team_work_runs SET status='ARCHIVED' WHERE status='ACTIVE'")
        self.led.conn.execute(
            "INSERT INTO team_work_runs (run_id, initiator_session, initial_task,"
            " project_workspace, current_card_id, status, card_count, created_at,"
            " current_card_cleared_at) VALUES (?,?,'task',?,NULL,?,1,?,?)",
            (rid, "s_init", self.tmpdir, status,
             self.now - run_age_s, cleared_at))
        self.led.conn.commit()

    def seed_card(self, run_id, task_id, *, shape="undispatched", relay=None):
        """relay=(confirmed_offset, notified_offset): 距 now 的秒数, None=NULL。"""
        self.led.conn.execute(
            "INSERT INTO harvest_contracts (task_id, contract_json,"
            " harvest_status, pass_count, created_at, updated_at,"
            " team_work_run_id, last_progress_at) VALUES (?,?,?,?,?,?,?,?)",
            (task_id, json.dumps({"required_files": []}), "PENDING", 0,
             self.now - 5700, self.now - 5700, run_id,
             (self.now - 3600) if shape == "dispatched" else None))
        self.led.conn.execute(
            "INSERT INTO task_sessions (task_id, session_id, profile,"
            " runner_kind, workspace, created_at) VALUES (?,?,?,?,?,?)",
            (task_id, f"sess_{task_id}", "coder", "api", self.tmpdir,
             self.now))
        self.led.conn.execute(
            "INSERT OR REPLACE INTO task_initiators (task_id, session_id,"
            " created_at) VALUES (?,?,?)", (task_id, "init_sess", self.now))
        if relay:
            conf = (self.now - relay[0]) if relay[0] is not None else None
            notif = (self.now - relay[1]) if relay[1] is not None else None
            self.led.conn.execute(
                "UPDATE harvest_contracts SET notify_confirmed_at=?,"
                " last_notified_at=? WHERE task_id=?", (conf, notif, task_id))
        self.led.conn.commit()
        if shape == "dispatched":
            reaper_mod.write_marker(self.tmpdir, task_id,
                                   started_at=self.now - 3600)

    def knock(self, run_id, *, initiator_la=None, initiator_raises=False,
             liveness_entry=None, env=None):
        """跑一次 _notify_null_slot, 返回 (sent_msgs, results)。"""
        sent = []

        def fake_la(sid):
            if initiator_raises:
                raise RuntimeError("boom (T8 fixture)")
            return initiator_la

        def fake_console(*a, **k):
            return liveness_entry

        patches = [
            mock.patch.object(reaper_mod, "_send_to_main",
                            lambda *a, **k: sent.append(a) or True),
            mock.patch.object(reaper_mod, "_query_console_liveness",
                            fake_console),
            mock.patch.object(reaper_mod, "_query_initiator_last_active",
                            fake_la),
        ]
        env_over = dict(env or {})
        with patches[0], patches[1], patches[2], \
             mock.patch.dict(os.environ, env_over, clear=False):
            ar = self.led.get_team_work(run_id)
            results = []
            reaper_mod._notify_null_slot(self.led, ar, results,
                                        stuck_after_min=STUCK_MIN,
                                        base=self.tmpdir)
        msgs = [a[1] for a in sent]
        return msgs, results


def verdict_of(results):
    return [r.get("verdict") for r in results]


# ── T1 迁移幂等 (AC-1) ────────────────────────────────────────
def test_T1_migration_idempotent(tmp_path):
    db = str(tmp_path / "ledger.db")
    led1 = Ledger(db)
    cols1 = [r["name"] for r in
             led1.conn.execute("PRAGMA table_info(team_work_runs)")]
    led1.conn.execute(
        "INSERT INTO team_work_runs (run_id, initiator_session,"
        " initial_task, project_workspace, current_card_id, status,"
        " card_count, created_at) VALUES ('r_old','s','t',?,NULL,"
        "'ACTIVE',1,?)", (str(tmp_path), time.time() - 100))
    led1.conn.commit()
    led1.close()
    led2 = Ledger(db)  # 第二次打开不得报错
    cols2 = [r["name"] for r in
             led2.conn.execute("PRAGMA table_info(team_work_runs)")]
    led3 = Ledger(db)  # 第三次, 彻底钉死幂等
    cols3 = [r["name"] for r in
             led3.conn.execute("PRAGMA table_info(team_work_runs)")]
    assert "current_card_cleared_at" in cols2
    assert cols1 == cols2 == cols3
    row = led2.conn.execute(
        "SELECT current_card_cleared_at FROM team_work_runs "
        "WHERE run_id='r_old'").fetchone()
    assert row["current_card_cleared_at"] is None  # 旧行 NULL
    led2.close()
    led3.close()


# ── T2 维护点 (AC-2) ──────────────────────────────────────────
def test_T2_maintenance_points(tmp_path):
    h = Harness(tmp_path)
    try:
        h.new_run("run_t2", run_age_s=100, cleared_at=None)
        h.led.set_team_work_current_card("run_t2", "t1")
        assert h.led.get_team_work("run_t2")["current_card_cleared_at"] is None
        t_clear = time.time()
        h.led.clear_team_work_current_card("run_t2")
        c = h.led.get_team_work("run_t2")["current_card_cleared_at"]
        assert c and abs(c - t_clear) <= 2, f"clear 后 cleared_at 应≈now, got {c}"
        h.led.set_team_work_current_card("run_t2", "t2")
        assert h.led.get_team_work("run_t2")["current_card_cleared_at"] == c, \
            "set 不得重置 cleared_at (B-2 不变式)"
        h.led.set_team_work_current_card("run_t2", "t3")
        assert h.led.get_team_work("run_t2")["current_card_cleared_at"] == c
    finally:
        h.close()


# ── T3 核心回归: 原误报场景必须零敲 (AC-3) ────────────────────
def test_T3_core_regression_zero_knock(tmp_path):
    h = Harness(tmp_path)
    try:
        h.new_run("run_t3", run_age_s=5700,
                 cleared_at=h.now - 6)  # 事故形状: 清槽 6s 前
        h.seed_card("run_t3", "t_ns", shape="undispatched")
        msgs, results = h.knock("run_t3")
        assert len(msgs) == 0, f"T3 必须零敲, got {msgs}"
        assert "NOTIFIED" not in verdict_of(results)
    finally:
        h.close()


# ── T4a relay confirmed 在窗内 → SUPPRESSED_RELAY (AC-4) ──────
def test_T4a_relay_confirmed_suppressed(tmp_path):
    h = Harness(tmp_path)
    try:
        h.new_run("run_t4a", run_age_s=5700, cleared_at=h.now - 600)
        h.seed_card("run_t4a", "t_ns", relay=(30, 40))
        msgs, results = h.knock("run_t4a")
        assert len(msgs) == 0
        assert "SUPPRESSED_RELAY" in verdict_of(results)
        r = [r for r in results if r.get("verdict") == "SUPPRESSED_RELAY"][0]
        assert r["trigger"] == "null_slot_knock" and r.get("note")  # AC-9
    finally:
        h.close()


# ── T4b 在途 claim (COALESCE last_notified_at) ────────────────
def test_T4b_relay_inflight_suppressed(tmp_path):
    h = Harness(tmp_path)
    try:
        h.new_run("run_t4b", run_age_s=5700, cleared_at=h.now - 600)
        h.seed_card("run_t4b", "t_ns", relay=(None, 30))  # confirmed NULL
        msgs, results = h.knock("run_t4b")
        assert len(msgs) == 0
        assert "SUPPRESSED_RELAY" in verdict_of(results)
    finally:
        h.close()


# ── T4c relay 超窗 → 不抑制, 继续走层3 ───────────────────────
def test_T4c_relay_out_of_window_not_suppressed(tmp_path):
    h = Harness(tmp_path)
    try:
        h.new_run("run_t4c", run_age_s=5700, cleared_at=h.now - 600)
        h.seed_card("run_t4c", "t_ns", relay=(700, 700))  # >300s 窗口
        msgs, results = h.knock("run_t4c")  # initiator 静默
        assert "SUPPRESSED_RELAY" not in verdict_of(results)
        assert "NOTIFIED" in verdict_of(results) and len(msgs) == 1
    finally:
        h.close()


# ── T5a initiator 刚活跃 → SUPPRESSED_INITIATOR (AC-5) ───────
def test_T5a_initiator_liveness_suppressed(tmp_path):
    h = Harness(tmp_path)
    try:
        h.new_run("run_t5a", run_age_s=5700, cleared_at=h.now - 600)
        h.seed_card("run_t5a", "t_ns")
        msgs, results = h.knock("run_t5a", initiator_la=h.now - 10)
        assert len(msgs) == 0
        assert "SUPPRESSED_INITIATOR" in verdict_of(results)
        r = [r for r in results
             if r.get("verdict") == "SUPPRESSED_INITIATOR"][0]
        assert r["trigger"] == "null_slot_knock" and r.get("note")  # AC-9
    finally:
        h.close()


# ── T5b 开关关闭 → 层3 跳过 → 敲 (AC-5 开关) ─────────────────
def test_T5b_initiator_liveness_switch_off(tmp_path):
    h = Harness(tmp_path)
    try:
        h.new_run("run_t5b", run_age_s=5700, cleared_at=h.now - 600)
        h.seed_card("run_t5b", "t_ns")
        msgs, results = h.knock("run_t5b", initiator_la=h.now - 10,
                               env={"MW_REAPER_INITIATOR_LIVENESS": "0"})
        assert len(msgs) == 1 and "NOTIFIED" in verdict_of(results)
    finally:
        h.close()


# ── T6 真死 main 仍敲 (AC-4/G4) ──────────────────────────────
def test_T6_truly_dead_main_still_knocks(tmp_path):
    h = Harness(tmp_path)
    try:
        h.new_run("run_t6", run_age_s=5700, cleared_at=h.now - 3600)
        h.seed_card("run_t6", "t_ns")
        msgs, results = h.knock("run_t6", initiator_la=h.now - 7200)
        assert len(msgs) == 1 and "NOTIFIED" in verdict_of(results)
        assert "等待 main 派发" in msgs[0]
        import re
        m = re.search(r"已 ([\d.]+) 分钟", msgs[0])
        assert m and abs(float(m.group(1)) - 60.0) < 0.2, f"mins 应≈60: {msgs[0]}"
        # 敲门去重时刻已落库
        kk = h.led.get_team_work("run_t6")["last_null_slot_knock_at"]
        assert kk and abs(kk - h.now) <= 5
    finally:
        h.close()


# ── T7 dispatched 卡 + console 报死 → 仍敲 (AC-7 不回归) ─────
def test_T7_dispatched_dead_worker_still_knocks(tmp_path):
    h = Harness(tmp_path)
    try:
        h.new_run("run_t7", run_age_s=5700, cleared_at=h.now - 3600)
        h.seed_card("run_t7", "t_d", shape="dispatched")
        dead = {"thinking": False, "last_event_ts": h.now - 7200,
               "last_event_type": "tool.end"}
        msgs, results = h.knock("run_t7", liveness_entry=dead)
        assert len(msgs) == 1 and "NOTIFIED" in verdict_of(results)
        assert "SUPPRESSED_ALIVE" not in verdict_of(results)
    finally:
        h.close()


# ── T8 initiator 查询抛异常 → fail-open 照敲 (AC-8) ───────────
def test_T8_initiator_query_raises_fail_open(tmp_path):
    h = Harness(tmp_path)
    try:
        h.new_run("run_t8", run_age_s=5700, cleared_at=h.now - 600)
        h.seed_card("run_t8", "t_ns")  # relay 列 NULL, 查询正常无 relay
        msgs, results = h.knock("run_t8", initiator_raises=True)
        assert len(msgs) == 1 and "NOTIFIED" in verdict_of(results)
    finally:
        h.close()


# ── 补充: relay 抑制窗口 env 生效性 (DESIGN §4) ───────────────
def test_extra_relay_window_env_override(tmp_path):
    h = Harness(tmp_path)
    try:
        h.new_run("run_x", run_age_s=5700, cleared_at=h.now - 600)
        h.seed_card("run_x", "t_ns", relay=(100, 100))
        # 默认窗口=threshold=300 → 100s 前 relay 抑制
        msgs, results = h.knock("run_x")
        assert "SUPPRESSED_RELAY" in verdict_of(results)
        # env 收紧到 50s → 100s 前不抑制 → 敲
        msgs2, results2 = h.knock("run_x",
                                 env={"MW_REAPER_RELAY_SUPPRESS_WINDOW_S": "50"})
        assert "SUPPRESSED_RELAY" not in verdict_of(results2)
        assert "NOTIFIED" in verdict_of(results2) and len(msgs2) == 1
    finally:
        h.close()
