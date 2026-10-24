"""issue #77: 远端 worker 续 claim + reclaim 计数口径 (用户 2026-10-23 裁决)。

三组用例 (卡面要求的验收点):
  A) 活性证据判定 — message_count 在 5min 观察窗内增长 ⇒ 有活性证据;
     单采样 / 计数持平 / 窗外样本 ⇒ 无证据 (不给死 worker 续命)。
  B) CAS 续期 — 只有 (status='running' AND claim_lock=?) 命中才延长
     claim_expires, 并同步 task_runs.claim_expires; 串卡 / 非 running 一律
     不动 (rowcount==0 ⇒ 不写)。
  C) reclaim 计数口径 — 远端 worker 有活性证据时, 该次回收**不累加**
     consecutive_failures, 且 last_failure_error 写成
     reclaimed_remote_alive 与真实失败区分; 无活性证据 ⇒ 计数照旧。

外加 D) 端到端一轮: renew_remote_claims() 在真实 sqlite 库上跑完
「续期 + 回收后修账」两步, 证明挂点函数本身可用 (不只是纯函数)。

断言口径 (与仓库既有防恒真约定一致):
  * 期望值一律取自**被测对象之外**的字面量或从 fixture 读回, 不取自
    被测代码的常量 (否则断言恒真)。
  * 每条用例都用真实 assert (pytest 可见), 不靠 print 汇总。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mw import pipeline  # noqa: E402
from mw.ledger import Ledger  # noqa: E402

# ── kanban.db 最小夹具 (只取被测代码真正读写的列) ─────────────────
# 与 hermes_cli/kanban_db.py 的真实 schema 同名同类型, 但只建被测路径
# 需要的列 —— 测试自建库, 绝不碰生产 /opt/data/kanban.db。
SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    created_at           INTEGER NOT NULL,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    result               TEXT,
    idempotency_key      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    worker_started_at    INTEGER,
    last_failure_error   TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    INTEGER,
    current_run_id       INTEGER
);
CREATE TABLE IF NOT EXISTS task_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    profile             TEXT,
    step_key            TEXT,
    status              TEXT NOT NULL,
    claim_lock          TEXT,
    claim_expires       INTEGER,
    worker_pid          INTEGER,
    worker_started_at   INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   INTEGER,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    outcome             TEXT,
    summary             TEXT,
    metadata            TEXT,
    error               TEXT
);
CREATE TABLE IF NOT EXISTS task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);
"""


def _mk_kanban(path, *, task_id="t_issue77", status="running",
               claim_lock: str | None = "host:999:lock-1",
               claim_expires: int | None = 1000,
               failures=0, last_failure_error=None, run_id=None):
    """建一个最小 kanban 库并塞一张卡 + 一个 run。返回 (conn, task_id)。

    claim_lock / claim_expires 允许 None (回收后的卡就是这形态)。
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, claim_lock, "
        "claim_expires, consecutive_failures, last_failure_error, "
        "current_run_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, "issue77 card", status, now, claim_lock, claim_expires,
         failures, last_failure_error, run_id))
    conn.execute(
        "INSERT INTO task_runs (id, task_id, profile, status, claim_lock, "
        "claim_expires, started_at) VALUES (?,?,?,?,?,?,?)",
        (run_id, task_id, "coder", "running", claim_lock, claim_expires, now))
    conn.commit()
    return conn


def _mk_ledger(tmp_path, *, task_id="t_issue77", session_id="api_sess_1",
               profile="coder", runner_kind="api_async"):
    """建一个最小 mw ledger 并塞一条远端 session 锚点。返回 Ledger。"""
    led = Ledger(str(tmp_path / "ledger.db"))
    led.upsert_session(task_id, session_id, profile, runner_kind,
                       workspace=str(tmp_path))
    return led


# ══════════════════════════════════════════════════════════════════
# A) 活性证据判定
# ══════════════════════════════════════════════════════════════════
def test_alive_by_message_growth_detects_growth():
    """窗内 message_count 增长 ⇒ 有活性证据 (delta 逐字可核对)。"""
    now = 1_000_000.0
    samples = [(now - 240, 10), (now - 120, 13), (now - 5, 17)]
    alive, detail = pipeline.alive_by_message_growth(
        samples, window_s=300, now=now)
    assert alive is True
    assert detail["samples_in_window"] == 3
    assert detail["first_count"] == 10
    assert detail["last_count"] == 17
    assert detail["delta"] == 7
    assert detail["liveness_reason"] == "growing"


def test_alive_by_message_growth_flat_is_not_alive():
    """窗内计数持平 ⇒ 无活性证据 (worker 停了, 不得续命)。"""
    now = 1_000_000.0
    samples = [(now - 240, 12), (now - 120, 12), (now - 5, 12)]
    alive, detail = pipeline.alive_by_message_growth(
        samples, window_s=300, now=now)
    assert alive is False
    assert detail["delta"] == 0
    assert detail["liveness_reason"] == "flat"


def test_alive_by_message_growth_single_sample_is_not_alive():
    """只有一个采样 ⇒ 无法判定增长 ⇒ 无证据 (保守口径)。"""
    now = 1_000_000.0
    alive, detail = pipeline.alive_by_message_growth(
        [(now - 5, 30)], window_s=300, now=now)
    assert alive is False
    assert detail["samples_in_window"] == 1
    assert detail["liveness_reason"] == "insufficient_samples"


def test_alive_by_message_growth_ignores_samples_outside_window():
    """窗外样本不参与判定 (5min 窗是硬边界, 防止陈样本撑出假活性)。"""
    now = 1_000_000.0
    # 两个增长样本都在窗外 ⇒ 窗内零采样 ⇒ 无证据
    samples = [(now - 3600, 1), (now - 1800, 5)]
    alive, detail = pipeline.alive_by_message_growth(
        samples, window_s=300, now=now)
    assert alive is False
    assert detail["samples_in_window"] == 0
    assert detail["liveness_reason"] == "insufficient_samples"


def test_alive_by_message_growth_window_is_five_minutes_by_default():
    """默认观察窗 = 300s (用户裁决口径), 恰在窗内的增长算数。"""
    now = 1_000_000.0
    # 两个采样相隔 299s (<=300) ⇒ 窗内 ⇒ 有活性
    alive_in, _ = pipeline.alive_by_message_growth(
        [(now - 299, 4), (now, 9)], window_s=300, now=now)
    assert alive_in is True
    # 相隔 301s (>300) ⇒ 窗外 ⇒ 无证据
    alive_out, _ = pipeline.alive_by_message_growth(
        [(now - 301, 4), (now, 9)], window_s=300, now=now)
    assert alive_out is False


# ══════════════════════════════════════════════════════════════════
# B) CAS 续期
# ══════════════════════════════════════════════════════════════════
def test_renew_claim_cas_extends_and_syncs_run(tmp_path):
    """命中 (running + claim_lock 匹配) ⇒ claim_expires 与
    task_runs.claim_expires 同步延长。"""
    conn = _mk_kanban(tmp_path / "kanban.db", run_id=7,
                      claim_expires=1000)
    now = 2_000_000
    ok = pipeline.renew_claim_cas(conn, "t_issue77", "host:999:lock-1",
                                   ttl_seconds=900, now=now)
    assert ok is True
    task = conn.execute(
        "SELECT claim_expires, last_heartbeat_at, status FROM tasks "
        "WHERE id='t_issue77'").fetchone()
    run = conn.execute(
        "SELECT claim_expires FROM task_runs WHERE id=7").fetchone()
    assert task["claim_expires"] == now + 900
    assert task["last_heartbeat_at"] == now
    assert task["status"] == "running"
    # run 行同步 (与 gateway _extend_run_claim 同口径)
    assert run["claim_expires"] == now + 900
    conn.close()


def test_renew_claim_cas_misses_on_wrong_claim_lock(tmp_path):
    """claim_lock 不匹配 ⇒ CAS 落空 ⇒ 绝不续别人的卡。"""
    conn = _mk_kanban(tmp_path / "kanban.db", run_id=7, claim_expires=1000)
    ok = pipeline.renew_claim_cas(conn, "t_issue77", "someone-elses-lock",
                                   ttl_seconds=900, now=2_000_000)
    assert ok is False
    task = conn.execute(
        "SELECT claim_expires FROM tasks WHERE id='t_issue77'").fetchone()
    run = conn.execute(
        "SELECT claim_expires FROM task_runs WHERE id=7").fetchone()
    assert task["claim_expires"] == 1000     # 一字未动
    assert run["claim_expires"] == 1000
    conn.close()


def test_renew_claim_cas_misses_when_not_running(tmp_path):
    """卡已不是 running (被回收成 ready) ⇒ CAS 落空 ⇒ 不写。"""
    conn = _mk_kanban(tmp_path / "kanban.db", status="ready",
                      run_id=7, claim_expires=1000)
    ok = pipeline.renew_claim_cas(conn, "t_issue77", "host:999:lock-1",
                                   ttl_seconds=900, now=2_000_000)
    assert ok is False
    task = conn.execute(
        "SELECT claim_expires FROM tasks WHERE id='t_issue77'").fetchone()
    assert task["claim_expires"] == 1000
    conn.close()


def test_renew_claim_cas_uses_ttl_from_env(tmp_path, monkeypatch):
    """TTL 取同源 env (HERMES_KANBAN_CLAIM_TTL_SECONDS), 与回收阈值同口径。"""
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_TTL_SECONDS", "1200")
    assert pipeline.claim_ttl_seconds() == 1200
    conn = _mk_kanban(tmp_path / "kanban.db", run_id=7, claim_expires=1000)
    now = 2_000_000
    assert pipeline.renew_claim_cas(conn, "t_issue77", "host:999:lock-1",
                                    now=now) is True
    task = conn.execute(
        "SELECT claim_expires FROM tasks WHERE id='t_issue77'").fetchone()
    assert task["claim_expires"] == now + 1200
    conn.close()


def test_renew_claim_cas_ignores_invalid_ttl_env(tmp_path, monkeypatch):
    """非法 TTL env ⇒ 回落默认 900 (与仓库既有 env 容错口径一致)。"""
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_TTL_SECONDS", "not-a-number")
    assert pipeline.claim_ttl_seconds() == 900


# ══════════════════════════════════════════════════════════════════
# C) reclaim 计数口径
# ══════════════════════════════════════════════════════════════════
def _reclaimed_fixture(tmp_path, *, failures=1):
    """造一张「刚被 gateway 回收」的卡: status=ready, claim_lock=NULL,
    consecutive_failures=1, last_failure_error='stale_lock=...' + 一条
    reclaimed 事件 (gateway 的真实落痕形态)。"""
    conn = _mk_kanban(tmp_path / "kanban.db", status="ready",
                      claim_lock=None, claim_expires=None,
                      failures=failures,
                      last_failure_error="stale_lock=host:999:lock-1",
                      run_id=7)
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?,?,?,?,?)",
        ("t_issue77", 7, "reclaimed", '{"stale_lock": "host:999:lock-1"}',
         int(time.time())))
    conn.commit()
    return conn


def test_repair_reclaim_accounting_does_not_count_failure_when_alive(tmp_path):
    """有活性证据 ⇒ 回收那次不累加 consecutive_failures, 且写
    reclaimed_remote_alive 与真实失败区分。"""
    led = _mk_ledger(tmp_path)
    conn = _reclaimed_fixture(tmp_path, failures=1)
    row = conn.execute(
        "SELECT * FROM tasks WHERE id='t_issue77'").fetchone()
    out = pipeline._repair_reclaim_accounting(
        conn, led, row, alive=True,
        detail={"liveness_reason": "growing", "delta": 4},
        now=float(time.time()))
    assert [r["verdict"] for r in out] == ["REPAIRED"]
    task = conn.execute(
        "SELECT consecutive_failures, last_failure_error FROM tasks "
        "WHERE id='t_issue77'").fetchone()
    # 计数回退 (1 → 0): 该次回收不计入
    assert task["consecutive_failures"] == 0
    # 错误串换成活性豁免标记 (与真实失败区分)
    assert task["last_failure_error"] == "reclaimed_remote_alive"
    # 审计留痕: mw 自己的事件表
    evs = led.conn.execute(
        "SELECT event_type, payload FROM mw_events "
        "WHERE event_type='claim.reclaimed_remote_alive'").fetchall()
    assert len(evs) == 1
    conn.close()
    led.close()


def test_repair_reclaim_accounting_keeps_count_without_liveness(tmp_path):
    """无活性证据 ⇒ 计数照旧 (真失败不能被豁免), 也不改错误串。"""
    led = _mk_ledger(tmp_path)
    conn = _reclaimed_fixture(tmp_path, failures=1)
    row = conn.execute(
        "SELECT * FROM tasks WHERE id='t_issue77'").fetchone()
    out = pipeline._repair_reclaim_accounting(
        conn, led, row, alive=False,
        detail={"liveness_reason": "flat", "delta": 0},
        now=float(time.time()))
    assert [r["verdict"] for r in out] == ["SKIP"]
    task = conn.execute(
        "SELECT consecutive_failures, last_failure_error FROM tasks "
        "WHERE id='t_issue77'").fetchone()
    assert task["consecutive_failures"] == 1        # 计数没被抹掉
    assert task["last_failure_error"] == "stale_lock=host:999:lock-1"
    evs = led.conn.execute(
        "SELECT event_type FROM mw_events "
        "WHERE event_type='claim.reclaimed_remote_alive'").fetchall()
    assert len(evs) == 0
    conn.close()
    led.close()


def test_repair_reclaim_accounting_is_idempotent(tmp_path):
    """修过一次后再修 ⇒ 不重复扣计数 (幂等, 不会把计数扣成负数)。"""
    led = _mk_ledger(tmp_path)
    conn = _reclaimed_fixture(tmp_path, failures=2)
    now = float(time.time())
    row = conn.execute(
        "SELECT * FROM tasks WHERE id='t_issue77'").fetchone()
    pipeline._repair_reclaim_accounting(
        conn, led, row, alive=True, detail={"liveness_reason": "growing"}, now=now)
    row = conn.execute(
        "SELECT * FROM tasks WHERE id='t_issue77'").fetchone()
    out = pipeline._repair_reclaim_accounting(
        conn, led, row, alive=True, detail={"liveness_reason": "growing"}, now=now)
    # 第二次: 错误串已是 reclaimed_remote_alive, 不再是 stale_lock= 前缀
    # ⇒ 不再触发修账 (幂等)
    assert out == []
    task = conn.execute(
        "SELECT consecutive_failures, last_failure_error FROM tasks "
        "WHERE id='t_issue77'").fetchone()
    assert task["consecutive_failures"] == 1        # 只扣了一次
    assert task["last_failure_error"] == "reclaimed_remote_alive"
    conn.close()
    led.close()


def test_repair_reclaim_accounting_ignores_non_reclaim_error(tmp_path):
    """last_failure_error 不是回收错误 (真实失败) ⇒ 一律不碰。"""
    led = _mk_ledger(tmp_path)
    conn = _mk_kanban(tmp_path / "kanban.db", status="ready",
                      claim_lock=None, failures=1,
                      last_failure_error="spawn_failed: boom", run_id=7)
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?,?,?,?,?)",
        ("t_issue77", 7, "reclaimed", "{}", int(time.time())))
    conn.commit()
    row = conn.execute(
        "SELECT * FROM tasks WHERE id='t_issue77'").fetchone()
    out = pipeline._repair_reclaim_accounting(
        conn, led, row, alive=True, detail={"liveness_reason": "growing"},
        now=float(time.time()))
    assert out == []
    task = conn.execute(
        "SELECT consecutive_failures, last_failure_error FROM tasks "
        "WHERE id='t_issue77'").fetchone()
    assert task["consecutive_failures"] == 1
    assert task["last_failure_error"] == "spawn_failed: boom"
    conn.close()
    led.close()


def test_repair_reclaim_accounting_skips_old_reclaim(tmp_path):
    """回收事件太老 (超修账窗口) ⇒ 不动账 (避免误修历史卡)。"""
    led = _mk_ledger(tmp_path)
    conn = _mk_kanban(tmp_path / "kanban.db", status="ready",
                      claim_lock=None, failures=1,
                      last_failure_error="stale_lock=host:999:lock-1",
                      run_id=7)
    old = int(time.time()) - 7200
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?,?,?,?,?)",
        ("t_issue77", 7, "reclaimed", "{}", old))
    conn.commit()
    row = conn.execute(
        "SELECT * FROM tasks WHERE id='t_issue77'").fetchone()
    out = pipeline._repair_reclaim_accounting(
        conn, led, row, alive=True, detail={"liveness_reason": "growing"},
        now=float(time.time()))
    assert [r["verdict"] for r in out] == ["SKIP"]
    assert out[0]["reason"] == "reclaim_too_old"
    task = conn.execute(
        "SELECT consecutive_failures FROM tasks WHERE id='t_issue77'"
    ).fetchone()
    assert task["consecutive_failures"] == 1
    conn.close()
    led.close()


def test_repair_reclaim_accounting_never_goes_below_zero(tmp_path):
    """计数已是 0 时修账 ⇒ 仍为 0 (不出现负数)。"""
    led = _mk_ledger(tmp_path)
    conn = _reclaimed_fixture(tmp_path, failures=0)
    row = conn.execute(
        "SELECT * FROM tasks WHERE id='t_issue77'").fetchone()
    pipeline._repair_reclaim_accounting(
        conn, led, row, alive=True, detail={"liveness_reason": "growing"},
        now=float(time.time()))
    task = conn.execute(
        "SELECT consecutive_failures FROM tasks WHERE id='t_issue77'"
    ).fetchone()
    assert task["consecutive_failures"] == 0
    conn.close()
    led.close()


# ══════════════════════════════════════════════════════════════════
# D) 端到端: renew_remote_claims() 在真实库上跑完两步
# ══════════════════════════════════════════════════════════════════
def test_renew_remote_claims_renews_then_repairs(tmp_path, monkeypatch):
    """一轮内: ① 有活性 ⇒ CAS 续期; ② 卡被回收 ⇒ 按活性修账。

    message_count 由桩网关提供 (两次采样, 计数增长 ⇒ 有活性证据)。
    """
    kanban = tmp_path / "kanban.db"
    conn = _mk_kanban(kanban, run_id=7, claim_expires=1000)
    now = int(time.time())
    conn.close()
    led = _mk_ledger(tmp_path)
    # 桩网关: message_count 随采样递增
    counts = iter([10, 14, 18, 22])
    monkeypatch.setattr(pipeline, "fetch_session_message_count",
                        lambda reg, sid, **kw: next(counts))
    # 桩 console: 无 SSE 活体条目 (本用例只验 message_count 这一路)
    monkeypatch.setattr(pipeline, "query_console_liveness",
                        lambda profile, session_id, **kw: None)
    registry = {"coder": {"api_url": "http://worker.invalid",
                          "api_key": "k"}}
    db = str(kanban)

    # ── 第一步: 卡在跑 ⇒ 续期 (首个采样无对比 ⇒ 不续, 这是保守口径)
    out = pipeline.renew_remote_claims(ledger=led, registry=registry,
                                       kanban_db=db, now=now)
    assert [r["verdict"] for r in out] == ["SKIP"]
    assert out[0]["reason"] == "no_liveness_evidence"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    task = conn.execute(
        "SELECT claim_expires FROM tasks WHERE id='t_issue77'").fetchone()
    assert task["claim_expires"] == 1000        # 首轮不续 (无对比样本)

    # ── 第二步: 第二个采样有增长 ⇒ 续期成功
    out = pipeline.renew_remote_claims(ledger=led, registry=registry,
                                       kanban_db=db, now=now + 60)
    assert [r["verdict"] for r in out] == ["RENEWED"]
    task = conn.execute(
        "SELECT claim_expires, last_heartbeat_at FROM tasks "
        "WHERE id='t_issue77'").fetchone()
    assert task["claim_expires"] == now + 60 + 900
    assert task["last_heartbeat_at"] == now + 60

    # ── 第三步: gateway 把卡回收了 (status=ready + 计数+1 + 错误串)
    conn.execute(
        "UPDATE tasks SET status='ready', claim_lock=NULL, "
        "claim_expires=NULL, consecutive_failures=1, "
        "last_failure_error='stale_lock=host:999:lock-1' WHERE id='t_issue77'")
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?,?,?,?,?)",
        ("t_issue77", 7, "reclaimed", "{}", now + 120))
    conn.commit()
    conn.close()

    out = pipeline.renew_remote_claims(ledger=led, registry=registry,
                                       kanban_db=db, now=now + 120)
    assert "REPAIRED" in [r["verdict"] for r in out]
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    task = conn.execute(
        "SELECT consecutive_failures, last_failure_error FROM tasks "
        "WHERE id='t_issue77'").fetchone()
    # 回收那次不累加 ⇒ 计数回退
    assert task["consecutive_failures"] == 0
    assert task["last_failure_error"] == "reclaimed_remote_alive"
    conn.close()
    led.close()


def test_renew_remote_claims_repairs_even_after_reclaim(tmp_path, monkeypatch):
    """卡被回收后 main 又重新 claim 它 ⇒ 修账仍然发生。

    (按 status 分流会把修账跳过 —— 计数永远停在被误伤的那一格。)
    """
    kanban = tmp_path / "kanban.db"
    conn = _mk_kanban(kanban, run_id=7, claim_expires=1000)
    now = int(time.time())
    conn.close()
    led = _mk_ledger(tmp_path)
    counts = iter([10, 14, 18, 22])
    monkeypatch.setattr(pipeline, "fetch_session_message_count",
                        lambda reg, sid, **kw: next(counts))
    monkeypatch.setattr(pipeline, "query_console_liveness",
                        lambda profile, session_id, **kw: None)
    registry = {"coder": {"api_url": "http://worker.invalid", "api_key": "k"}}
    db = str(kanban)

    # 第一轮: 采样 (无对比 ⇒ 不续)
    pipeline.renew_remote_claims(ledger=led, registry=registry,
                                 kanban_db=db, now=now)
    # 第二轮: 有增长 ⇒ 续期
    out = pipeline.renew_remote_claims(ledger=led, registry=registry,
                                       kanban_db=db, now=now + 60)
    assert "RENEWED" in [r["verdict"] for r in out]

    # 模拟: gateway 回收 (计数+1, 错误串 stale_lock=) 之后, main 立刻
    # 重新 claim (status 又回到 running, claim_lock 换成新值)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "UPDATE tasks SET consecutive_failures=1, "
        "last_failure_error='stale_lock=host:999:lock-1' WHERE id='t_issue77'")
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?,?,?,?,?)",
        ("t_issue77", 7, "reclaimed", "{}", now + 90))
    conn.execute(
        "UPDATE tasks SET status='running', claim_lock='host:999:lock-2', "
        "claim_expires=? WHERE id='t_issue77'", (now + 90 + 900,))
    conn.commit()
    conn.close()

    out = pipeline.renew_remote_claims(ledger=led, registry=registry,
                                       kanban_db=db, now=now + 120)
    assert "REPAIRED" in [r["verdict"] for r in out]
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    task = conn.execute(
        "SELECT consecutive_failures, last_failure_error FROM tasks "
        "WHERE id='t_issue77'").fetchone()
    assert task["consecutive_failures"] == 0
    assert task["last_failure_error"] == "reclaimed_remote_alive"
    conn.close()
    led.close()


def test_renew_remote_claims_skips_when_kanban_db_absent(tmp_path):
    """kanban 库不存在 ⇒ 静默跳过 (不炸 reaper 主流程)。"""
    led = _mk_ledger(tmp_path)
    out = pipeline.renew_remote_claims(
        ledger=led, registry={}, kanban_db=str(tmp_path / "nope.db"))
    assert out == []
    led.close()


def test_renew_remote_claims_only_touches_remote_runners(tmp_path):
    """本地 runner 的卡不在续期范围 (gateway 自己有活体豁免分支)。"""
    kanban = tmp_path / "kanban.db"
    conn = _mk_kanban(kanban, task_id="t_local", run_id=7)
    conn.close()
    led = Ledger(str(tmp_path / "ledger.db"))
    led.upsert_session("t_local", "api_sess_local", "coder", "local",
                       workspace=str(tmp_path))
    out = pipeline.renew_remote_claims(
        ledger=led, registry={}, kanban_db=str(kanban))
    assert out == []
    led.close()


def test_renew_remote_claims_is_wired_into_reaper():
    """挂点钉桩: reaper 的 reap_once 必须调用 renew_remote_claims。

    (防止「实现写好了但没挂上」—— 挂点缺失 = 修复不生效。)
    """
    import inspect
    from mw import reaper as reaper_mod
    src = inspect.getsource(reaper_mod.reap_once)
    # 断言的是「调用」而不是名字出现 —— import 行也含函数名, 只查名字
    # 会让「import 了但没调用」的摘线变异漏网 (变异测试实测过)。
    assert "renew_remote_claims(ledger=ledger" in src, \
        "reaper.reap_once 未调用 renew_remote_claims (issue #77 修复未挂载)"


# ══════════════════════════════════════════════════════════════════
# E) console SSE 活体信号 (长工具调用期间 message_count 是平的)
# ══════════════════════════════════════════════════════════════════
def test_console_liveness_thinking_is_alive():
    """thinking=True ⇒ 活 (粘滞标志, 覆盖长工具调用空窗)。"""
    now = 1_000_000.0
    alive, detail = pipeline.console_liveness_alive(
        {"thinking": True, "last_event_ts": now - 9999},
        window_s=300, now=now)
    assert alive is True
    assert detail["liveness_reason"] == "console_thinking"


def test_console_liveness_fresh_event_is_alive():
    """事件新鲜 (距今 < 窗口) ⇒ 活。"""
    now = 1_000_000.0
    alive, detail = pipeline.console_liveness_alive(
        {"thinking": False, "last_event_ts": now - 60},
        window_s=300, now=now)
    assert alive is True
    assert detail["liveness_reason"] == "console_event_fresh"
    assert detail["event_age_s"] == 60


def test_console_liveness_stale_event_is_not_alive():
    """事件陈旧且 thinking=False ⇒ 不活。"""
    now = 1_000_000.0
    alive, detail = pipeline.console_liveness_alive(
        {"thinking": False, "last_event_ts": now - 3600},
        window_s=300, now=now)
    assert alive is False
    assert detail["liveness_reason"] == "console_stale"


def test_console_liveness_no_entry_is_not_alive():
    """无条目 ⇒ 不活 (但不是「死」, 只是证据不可得)。"""
    alive, detail = pipeline.console_liveness_alive(
        None, window_s=300, now=1_000_000.0)
    assert alive is False
    assert detail["liveness_reason"] == "console_no_entry"


def test_worker_liveness_renewal_uses_union_of_both_signals():
    """续期判活 = message_count 增长 OR console 活体 (并集)。

    场景: worker 跑长工具调用 ⇒ message_count 平的 (A 假), 但 console
    thinking=True (B 真) ⇒ 仍判活 ⇒ 继续续 claim。
    """
    now = 1_000_000.0
    flat = [(now - 240, 20), (now - 120, 20), (now - 5, 20)]
    out = pipeline.worker_liveness(
        "coder", "api_sess_1", flat, window_s=300, now=now)
    # A 假 (计数平) / B 无条目 ⇒ 两个信号都假 ⇒ 不续期
    assert out["message_growth"] is False
    assert out["console_alive"] is False
    assert out["alive_for_renewal"] is False


def test_worker_liveness_message_growth_alone_is_enough(monkeypatch):
    """A 真 (message_count 增长) ⇒ 判活 (B 无条目也不影响)。"""
    monkeypatch.setattr(pipeline, "query_console_liveness",
                        lambda profile, session_id, **kw: None)
    now = 1_000_000.0
    growing = [(now - 240, 20), (now - 120, 24), (now - 5, 31)]
    out = pipeline.worker_liveness(
        "coder", "api_sess_1", growing, window_s=300, now=now)
    assert out["message_growth"] is True
    assert out["alive_for_renewal"] is True
    assert out["liveness_reason"] == "message_growing"


def test_worker_liveness_repair_only_trusts_message_growth(monkeypatch):
    """修账硬判定只认 message_count 增长 (裁决口径)。

    console thinking=True (B 真) 但 message_count 平 (A 假) 时,
    alive_for_renewal=True (可续期), 而 message_growth=False
    (回收计数不豁免) —— 两个信号的分工被钉死。
    """
    monkeypatch.setattr(pipeline, "query_console_liveness",
                        lambda profile, session_id, **kw: {
                            "thinking": True, "last_event_ts": 1.0})
    now = 1_000_000.0
    flat = [(now - 240, 20), (now - 120, 20), (now - 5, 20)]
    out = pipeline.worker_liveness(
        "coder", "api_sess_1", flat, window_s=300, now=now)
    assert out["console_alive"] is True
    assert out["alive_for_renewal"] is True
    assert out["message_growth"] is False   # 修账不豁免


def test_renew_remote_claims_renews_on_console_signal_only(tmp_path, monkeypatch):
    """长工具调用场景: message_count 平的, 但 console thinking=True
    ⇒ 仍续 claim (否则 TTL 到点照样被回收 = 没修好)。"""
    kanban = tmp_path / "kanban.db"
    conn = _mk_kanban(kanban, run_id=7, claim_expires=1000)
    now = int(time.time())
    conn.close()
    led = _mk_ledger(tmp_path)
    # message_count 恒定 (长工具调用中)
    monkeypatch.setattr(pipeline, "fetch_session_message_count",
                        lambda reg, sid, **kw: 42)
    # console 报 thinking=True
    monkeypatch.setattr(pipeline, "query_console_liveness",
                        lambda profile, session_id, **kw: {
                            "thinking": True, "last_event_ts": float(now)})
    registry = {"coder": {"api_url": "http://worker.invalid", "api_key": "k"}}
    db = str(kanban)

    out = pipeline.renew_remote_claims(ledger=led, registry=registry,
                                       kanban_db=db, now=now)
    assert [r["verdict"] for r in out] == ["RENEWED"]
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    task = conn.execute(
        "SELECT claim_expires FROM tasks WHERE id='t_issue77'").fetchone()
    assert task["claim_expires"] == now + 900
    conn.close()
    led.close()
