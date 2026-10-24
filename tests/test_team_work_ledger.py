"""Team Work ledger 不变量测试 (2026-10-03 T1)。

铁律:
  1. team_work 必须是 ledger 一条记录(team_work_runs 一行 = 一个 run)
  2. 一次只能一个 ACTIVE run (应用层 if + DB partial UNIQUE INDEX 双层防护)
  3. current_card_id 单槽,set 前必须 NULL,clear 后才能 advance
  4. close 时未结算卡统一 ARCHIVED, 避免 reaper 误激活遗留 PENDING
  5. list_team_works 默认只看 ACTIVE

不依赖任何外部 hermes / docker 资源;纯 SQLite + Ledger 类。
"""
import sqlite3
import tempfile
import os
import sys
from pathlib import Path

# 让 mw.ledger 可直接 import
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mw.ledger import Ledger


PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"

results = []


def check(name, fn):
    """跑 fn(), 捕获断言 / 异常, 打印 + 累计。"""
    try:
        fn()
        print(f"  {PASS} {name}")
        results.append(True)
    except AssertionError as e:
        print(f"  {FAIL} {name}: {e}")
        results.append(False)
    except Exception as e:
        print(f"  {FAIL} {name}: {type(e).__name__}: {e}")
        results.append(False)


def make_led():
    """新建临时 ledger.db, 返回 Ledger 实例。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Ledger(path), path


# ============================================================
# T1.1: create_team_work_run + 双层防护
# ============================================================

def test_create_basic():
    led, path = make_led()
    try:
        r = led.create_team_work_run("r1", "s1", "需求1")
        assert r["run_id"] == "r1"
        assert r["status"] == "ACTIVE"
        assert r["card_count"] == 0
        assert r["initiator_session"] == "s1"
        assert r["initial_task"] == "需求1"
        assert r["created_at"] > 0
    finally:
        os.unlink(path)


def test_app_guard_blocks_second_active():
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        try:
            led.create_team_work_run("r2", "s2", "需求2")
            assert False, "app guard should have raised IntegrityError"
        except sqlite3.IntegrityError as e:
            assert "r1" in str(e)
    finally:
        os.unlink(path)


def test_db_partial_unique_blocks_race():
    """绕过 app 层,直接两次 INSERT ACTIVE → DB partial UNIQUE INDEX 兜底。"""
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.close_team_work("r1", "test")  # 关掉 r1 让 active 空位
        # 直插两行 ACTIVE
        try:
            led.conn.execute(
                "INSERT INTO team_work_runs (run_id, initiator_session, "
                "initial_task, status, created_at) "
                "VALUES ('rA','s','t','ACTIVE',1)")
            led.conn.execute(
                "INSERT INTO team_work_runs (run_id, initiator_session, "
                "initial_task, status, created_at) "
                "VALUES ('rB','s','t','ACTIVE',2)")
            led.conn.commit()
            assert False, "DB-level guard should have raised"
        except sqlite3.IntegrityError as e:
            assert "UNIQUE" in str(e) or "constraint" in str(e).lower()
    finally:
        os.unlink(path)


def test_close_then_recreate():
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.close_team_work("r1", "complete_all")
        r2 = led.create_team_work_run("r2", "s2", "需求2")
        assert r2["status"] == "ACTIVE"
        # 旧 r1 仍可查: 空 run 无遗留 → 自动判定 CLOSED (AC3-1 语义,
        # A-4 更新: 旧 ABORTED 断言编码了「终态统一写 ABORTED」旧不变量)
        old = led.get_team_work("r1")
        assert old["status"] == "CLOSED"
        assert old["closed_reason"] == "complete_all"
    finally:
        os.unlink(path)


def test_double_close_idempotent():
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.close_team_work("r1", "first")
        led.close_team_work("r1", "second")  # 幂等, 不抛
        r1 = led.get_team_work("r1")
        assert r1["closed_reason"] == "first", f"got {r1['closed_reason']}"
    finally:
        os.unlink(path)


def test_auto_close_runs_last_card_complete():
    """链尾自动 close (减法, 2026-10-05): harvest 落 COMPLETE 时,若 run 名下
    无 PENDING/ERROR/BLOCKED/NEED_CLARIFICATION 残留,run 应自动 close 并
    修正 status 为 CLOSED (无遗留 = 正常完成, 与 user_close 区分开)。"""
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求")
        led.register_team_work_card("r1", "t_last")
        led.set_team_work_current_card("r1", "t_last")
        led.set_harvest("t_last", "HARVESTED", 0, "ok")
        # 模拟 harvest._harvest_inner 的 auto-close 分支
        _remaining = led.conn.execute(
            "SELECT COUNT(*) AS n FROM harvest_contracts "
            "WHERE team_work_run_id=? AND harvest_status IN "
            "('PENDING','ERROR','BLOCKED','NEED_CLARIFICATION')",
            ("r1",)).fetchone()
        assert _remaining["n"] == 0, f"残留: {_remaining['n']}"
        # A-4: 手工 status UPDATE (旧 close→ABORTED 再改 CLOSED) 已被
        # close_team_work(final_status='CLOSED') 一步取代 —— 删手工 UPDATE
        # 直接得到 CLOSED (AC3-1 语义: 无遗留 = 正常完成)
        led.close_team_work("r1", reason="complete_all",
                            closed_by="auto", final_status="CLOSED")
        r1 = led.get_team_work("r1")
        assert r1["status"] == "CLOSED", f"got {r1['status']}"
        assert r1["closed_reason"] == "complete_all"
        assert r1["closed_at"] is not None
    finally:
        os.unlink(path)


def test_auto_close_skipped_when_residual_pending():
    """链中段卡 COMPLETE 时,run 名下仍有 PENDING 卡 → 不应 auto-close。"""
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求")
        led.register_team_work_card("r1", "t_mid")
        led.register_team_work_card("r1", "t_next")  # 这张还没派
        led.set_team_work_current_card("r1", "t_mid")
        led.set_harvest("t_mid", "HARVESTED", 0, "ok")
        # 模拟 harvest: 查残留
        _remaining = led.conn.execute(
            "SELECT COUNT(*) AS n FROM harvest_contracts "
            "WHERE team_work_run_id=? AND harvest_status IN "
            "('PENDING','ERROR','BLOCKED','NEED_CLARIFICATION')",
            ("r1",)).fetchone()
        assert _remaining["n"] == 1, "还有 PENDING 卡, 不该 close"
        # 不调 close, run 仍 ACTIVE
        r1 = led.get_team_work("r1")
        assert r1["status"] == "ACTIVE"
    finally:
        os.unlink(path)


# ============================================================
# T1.1b: close_team_work 自动判定并入 UPDATE (#7-3, TOCTOU 消除)
# ============================================================

def test_close_auto_returns_actual_status_string():
    """#7-3: 自动判定分支返回值必须是实际终态字符串 (非 CASE 表达式串)。
    空 run 无遗留 → 'CLOSED'; 落库 status 与返回值一致。"""
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求")
        got = led.close_team_work("r1", "auto_done")
        assert got == "CLOSED", f"返回值应为 'CLOSED', got {got!r}"
        assert led.get_team_work("r1")["status"] == "CLOSED"
    finally:
        os.unlink(path)


def test_close_auto_aborted_with_residue():
    """#7-3: run 名下有未结算遗留 (PENDING) → 自动判定 'ABORTED',
    且 current_card_id 仍被清 NULL (原子副作用不变)。"""
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求")
        led.register_team_work_card("r1", "t_a")
        led.set_team_work_current_card("r1", "t_a")  # 槽位非空 + 卡 PENDING
        got = led.close_team_work("r1", "user_close")
        assert got == "ABORTED", f"有遗留应判 'ABORTED', got {got!r}"
        r1 = led.get_team_work("r1")
        assert r1["status"] == "ABORTED"
        assert r1["current_card_id"] is None, "close 必须清槽"
    finally:
        os.unlink(path)


def test_close_rowcount_guard_returns_none_when_racing():
    """#7-3: 幂等下沉到 UPDATE rowcount —— 他人(另一连接)先关闭后,
    本方 close 必须 0 行命中返回 None, 不覆盖已落终态。"""
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求")
        # 模拟并发对手: 独立连接抢先 close
        other = sqlite3.connect(path, timeout=30)
        try:
            other.execute(
                "UPDATE team_work_runs SET status='ABORTED', closed_at=1, "
                "closed_reason='racer', current_card_id=NULL "
                "WHERE run_id='r1' AND status='ACTIVE'")
            other.commit()
        finally:
            other.close()
        # 本方 close: SELECT 守卫与 rowcount 双层都会拦下 (此夹具里守卫先
        # 见 racer 终态即返 None; 真并发下守卫漏过则由 UPDATE rowcount=0
        # 兜底)。两层语义一致: 不写、不覆盖 racer 的 reason。
        got = led.close_team_work("r1", "late_close")
        assert got is None, f"已被抢先关闭应返回 None, got {got!r}"
        assert led.get_team_work("r1")["closed_reason"] == "racer"
    finally:
        os.unlink(path)


def test_close_rowcount_guard_when_guard_races_through():
    """#7-3: 直测 rowcount 兜底 —— 守卫 SELECT 通过后、UPDATE 前被抢先
    关闭 (连接代理在 UPDATE 前一刻改行), 本方必须 0 行命中返 None,
    不覆盖 racer 终态。"""
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求")
        real = led.conn

        class RacingConn:
            """转发全部属性; execute 命中 close 的 UPDATE 前先抢写终态。"""
            def __getattr__(self, name):
                return getattr(real, name)

            def execute(self, sql, *a, **kw):
                if sql.lstrip().upper().startswith("UPDATE TEAM_WORK_RUNS"):
                    # 模拟并发对手抢先落终态 (同连接, 未提交, 本方可读)
                    real.execute(
                        "UPDATE team_work_runs SET status='ABORTED', "
                        "closed_reason='racer' WHERE run_id='r1'")
                return real.execute(sql, *a, **kw)

        led.conn = RacingConn()
        try:
            got = led.close_team_work("r1", "late_close")
        finally:
            led.conn = real
        assert got is None, f"UPDATE 0 行命中应返 None, got {got!r}"
        # racer 的 reason 未被覆盖 (racer 未提交, 读同连接可见)
        row = real.execute(
            "SELECT status, closed_reason FROM team_work_runs "
            "WHERE run_id='r1'").fetchone()
        assert row["status"] == "ABORTED" and row["closed_reason"] == "racer", \
            f"racer 终态被覆盖: {dict(row)}"
    finally:
        os.unlink(path)


# ============================================================
# T1.2: register_team_work_card + current_card_id 单槽
# ============================================================

def test_register_card_increments_count():
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.register_team_work_card("r1", "t_a")
        led.register_team_work_card("r1", "t_b")
        led.register_team_work_card("r1", "t_c")
        assert led.get_team_work("r1")["card_count"] == 3
    finally:
        os.unlink(path)


def test_register_card_syncs_to_harvest_contracts():
    """register_team_work_card 应同步反写 harvest_contracts.team_work_run_id。"""
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.put_contract("t_a", {"required_files": []})
        led.register_team_work_card("r1", "t_a")
        hc = led.get_contract("t_a")
        assert hc["team_work_run_id"] == "r1", f"got {hc['team_work_run_id']}"
    finally:
        os.unlink(path)


def test_register_card_creates_placeholder_when_no_contract():
    """race fix: 即使 put_contract 没建 contract 行 (被 except 吞掉的场景),
    register_team_work_card 也应兜底建占位 PENDING 行并回写 team_work_run_id,
    否则卡会留下 NULL run_id → reaper SKIP 孤儿 → 30min stuck-notify 兜底。"""
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        # 注意: 这里不调 put_contract,模拟 console.py put_contract 因 race/异常被吞
        assert led.get_contract("t_a") is None
        led.register_team_work_card("r1", "t_a")
        # 占位 PENDING 行被建出来, team_work_run_id 也回填
        hc = led.get_contract("t_a")
        assert hc is not None, "register 应建占位行, 但 harvest_contracts 没行"
        assert hc["team_work_run_id"] == "r1"
        assert hc["harvest_status"] == "PENDING"
    finally:
        os.unlink(path)


def test_current_card_id_set_clear():
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        assert led.get_team_work("r1")["current_card_id"] is None
        led.set_team_work_current_card("r1", "t_a")
        assert led.get_team_work("r1")["current_card_id"] == "t_a"
        led.clear_team_work_current_card("r1")
        assert led.get_team_work("r1")["current_card_id"] is None
    finally:
        os.unlink(path)


# ============================================================
# T1.3: pending_in_team_work 过滤
# ============================================================

def test_pending_in_team_work_filters_orphans():
    """pending_in_team_work 只返回属于该 run 的 PENDING/ERROR/BLOCKED;
    孤儿(无 run_id)不出现在结果里。"""
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.put_contract("t_in_1", {})
        led.put_contract("t_in_2", {})
        led.register_team_work_card("r1", "t_in_1")
        led.register_team_work_card("r1", "t_in_2")
        # 孤儿:有 contract 但没 register
        led.put_contract("t_orphan", {})
        rows = led.pending_in_team_work("r1")
        ids = {r["task_id"] for r in rows}
        assert "t_orphan" not in ids, f"孤儿不应在结果中: {ids}"
        assert ids == {"t_in_1", "t_in_2"}, f"got {ids}"
    finally:
        os.unlink(path)


def test_pending_in_team_work_excludes_terminal():
    """归档/COMPLETE 卡不应在 pending_in_team_work 里。"""
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.put_contract("t_done", {})
        led.set_harvest("t_done", "HARVESTED")  # 终态
        led.register_team_work_card("r1", "t_done")
        rows = led.pending_in_team_work("r1")
        assert len(rows) == 0, f"expected empty, got {[r['task_id'] for r in rows]}"
    finally:
        os.unlink(path)


# ============================================================
# T1.4: archive_team_work_cards + close
# ============================================================

def test_archive_marks_cards_archived():
    """archive_team_work_cards 把 run 下未结算卡 harvest_status='ARCHIVED'。"""
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.put_contract("t_a", {})
        led.put_contract("t_b", {})
        led.register_team_work_card("r1", "t_a")
        led.register_team_work_card("r1", "t_b")
        archived = led.archive_team_work_cards("r1")
        assert sorted(archived) == ["t_a", "t_b"]
        assert led.get_contract("t_a")["harvest_status"] == "ARCHIVED"
        assert led.get_contract("t_b")["harvest_status"] == "ARCHIVED"
    finally:
        os.unlink(path)


def test_archive_excludes_terminal_cards():
    """archive 不动已 COMPLETE / 已 ARCHIVED 的卡。"""
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.put_contract("t_done", {})
        led.set_harvest("t_done", "HARVESTED")
        led.put_contract("t_pending", {})
        led.register_team_work_card("r1", "t_done")
        led.register_team_work_card("r1", "t_pending")
        archived = led.archive_team_work_cards("r1")
        assert archived == ["t_pending"]
        assert led.get_contract("t_done")["harvest_status"] == "HARVESTED"
    finally:
        os.unlink(path)


def test_settle_team_work_residue():
    """T1.6 (2026-10-05, 修订 2026-10-08): advance 串行铁律 —
    settle_team_work_residue 只覆盖 BLOCKED / NEED_CLARIFICATION 残留,
    PENDING 不动 (它是排队等 main advance 的未来卡)。

    旧注释「PENDING (残留 → 覆盖)」是 10-05 的设想, 实际 SQL 自始只
    覆盖三类状态, PENDING 因 exclude_task_id 而保持原状。2026-10-08
    把这条明确写进 docstring, 杜绝后人误以为 advance 把未来卡隐式标完。
    """
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        for tid in ["t_a", "t_b", "t_c", "t_d"]:
            led.put_contract(tid, {})
            led.register_team_work_card("r1", tid)
        # t_a 已 HARVESTED (不动)
        led.set_harvest("t_a", "HARVESTED")
        # t_b 是 PENDING (排除 — 是 exclude_task_id)
        # t_c 是 BLOCKED (残留 → 覆盖)
        led.set_harvest("t_c", "BLOCKED", log="reaper fallback")
        # t_d 是 NEED_CLARIFICATION (残留 → 覆盖)
        led.set_harvest("t_d", "NEED_CLARIFICATION")

        settled = led.settle_team_work_residue("r1", exclude_task_id="t_b")
        assert sorted(settled) == ["t_c", "t_d"], f"got {sorted(settled)}"

        assert led.get_contract("t_a")["harvest_status"] == "HARVESTED"
        assert led.get_contract("t_b")["harvest_status"] == "PENDING"  # 排除
        assert led.get_contract("t_c")["harvest_status"] == "HARVESTED"
        assert led.get_contract("t_d")["harvest_status"] == "HARVESTED"
        # log 含标记
        log_c = led.get_contract("t_c")["harvest_log"]
        assert "[main_advance]" in log_c, f"got {log_c!r}"
    finally:
        os.unlink(path)


def test_settle_team_work_residue_no_cross_run():
    """T1.6b: settle 只动本 run 名下卡, 不跨 run (2026-10-05 用户明示:
    一次只允许一个 ACTIVE run, 不存在老 run 残留场景)。

    直接绕开 create_team_work_run 的应用层守卫 — 用 conn.execute
    强制 INSERT 一个 ACTIVE run, 验证 settle_team_work_residue 的 SQL
    范围严格按 team_work_run_id 过滤。"""
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        # 直接 SQL INSERT 第二个 run, 绕过应用层单 ACTIVE 守卫
        import time as _t
        led.conn.execute(
            "INSERT INTO team_work_runs (run_id, initiator_session, "
            "initial_task, status, card_count, created_at) "
            "VALUES (?, ?, ?, 'CLOSED', 0, ?)",
            ("r2", "s2", "需求2", _t.time()))
        led.conn.commit()
        led.put_contract("t_r1", {})
        led.register_team_work_card("r1", "t_r1")
        led.put_contract("t_r2", {})
        led.register_team_work_card("r2", "t_r2")
        led.set_harvest("t_r1", "BLOCKED")
        led.set_harvest("t_r2", "BLOCKED")

        settled = led.settle_team_work_residue("r1", exclude_task_id="")
        assert settled == ["t_r1"], f"got {settled}"
        assert led.get_contract("t_r2")["harvest_status"] == "BLOCKED"  # 不动
    finally:
        os.unlink(path)


# ============================================================
# T1.5: list_team_works
# ============================================================

def test_list_active_only_by_default():
    led, path = make_led()
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.close_team_work("r1", "done")
        led.create_team_work_run("r2", "s2", "需求2")
        active = led.list_team_works(include_closed=False)
        all_ = led.list_team_works(include_closed=True)
        assert len(active) == 1
        assert active[0]["run_id"] == "r2"
        assert len(all_) == 2
    finally:
        os.unlink(path)


# ============================================================
# T1.6: get_active_team_work 单值性
# ============================================================

def test_get_active_team_work_returns_one():
    led, path = make_led()
    try:
        # 空库
        assert led.get_active_team_work() is None
        # 一条
        led.create_team_work_run("r1", "s1", "需求1")
        a = led.get_active_team_work()
        assert a and a["run_id"] == "r1"
        # partial unique 兜底下不可能同时有两条;验证 partial index 存在
        idx = led.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_team_work_one_active'").fetchone()
        assert idx is not None, "partial unique index 应已建好"
    finally:
        os.unlink(path)


# ============================================================
# T1.7: schema 迁移幂等(开两次 Ledger,旧 db 走 ALTER 也能跑)
# ============================================================

def test_reopen_persists_schema():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        l1 = Ledger(path)
        l1.create_team_work_run("r1", "s1", "需求1")
        l1.put_contract("t_a", {})
        l1.register_team_work_card("r1", "t_a")
        l1.close()
        # 重开:schema 应自动迁移完成
        l2 = Ledger(path)
        r = l2.get_team_work("r1")
        assert r is not None
        hc = l2.get_contract("t_a")
        assert hc["team_work_run_id"] == "r1"
        # harvest_contracts 应有 team_work_run_id 列
        cols = [r["name"] for r in l2.conn.execute(
            "PRAGMA table_info(harvest_contracts)").fetchall()]
        assert "team_work_run_id" in cols
    finally:
        os.unlink(path)


# ============================================================
# 主入口
# ============================================================

def main():
    tests = [
        test_create_basic,
        test_app_guard_blocks_second_active,
        test_db_partial_unique_blocks_race,
        test_close_then_recreate,
        test_double_close_idempotent,
        test_register_card_increments_count,
        test_register_card_syncs_to_harvest_contracts,
        test_register_card_creates_placeholder_when_no_contract,
        test_current_card_id_set_clear,
        test_pending_in_team_work_filters_orphans,
        test_pending_in_team_work_excludes_terminal,
        test_archive_marks_cards_archived,
        test_archive_excludes_terminal_cards,
        test_list_active_only_by_default,
        test_get_active_team_work_returns_one,
        test_reopen_persists_schema,
        test_auto_close_runs_last_card_complete,
        test_auto_close_skipped_when_residual_pending,
    ]
    for t in tests:
        check(t.__name__, t)

    total = len(results)
    passed = sum(results)
    print()
    if passed == total:
        print(f"\033[92m=== ALL {total} TESTS PASSED ===\033[0m")
        return 0
    else:
        print(f"\033[91m=== {total - passed}/{total} FAILED ===\033[0m")
        return 1


if __name__ == "__main__":
    sys.exit(main())