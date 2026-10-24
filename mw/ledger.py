"""中间件自有账本: task↔session 锚点 + harvest 契约。

与 kanban.db 物理分离。kanban 是账本(记录), 本库是执行台账(路由/结算状态)。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional


class SessionAlreadyBoundError(sqlite3.IntegrityError):
    """issue#12 R2: 专属异常,标识 session_id 已绑到既有 run,不能再发起新 run。

    设计动机 (2026-10-13): 三层守卫里 session 复用冲突若用 sqlite3.IntegrityError
    + 字符串嗅探判别,ledger 改文案就会静默退化为通用 409,前端
    session_reused 自动重拉链路失效 (issue#12 R2 实锤)。这里用专属异常类
    让 caller 用 isinstance 判别,不依赖 message 字符串契约。

    继承 sqlite3.IntegrityError 是为了: (a) 旧 caller 用 except
    sqlite3.IntegrityError 兜底仍然成立 (R2 行为不变,旧测试通过);
    (b) type hierarchy 兼容 DB 层兜底 race 抛出的 IntegrityError。
    """

    def __init__(self, session_id, conflicting_run_ids):
        self.session_id = session_id
        self.conflicting_run_ids = list(conflicting_run_ids or [])
        super().__init__(
            f"session {session_id} 已绑到 run {self.conflicting_run_ids}, "
            f"不能再发起新 run")

SCHEMA = """
CREATE TABLE IF NOT EXISTS task_sessions (
    task_id            TEXT PRIMARY KEY,
    session_id         TEXT,
    profile            TEXT NOT NULL,
    runner_kind        TEXT,
    container_id       TEXT,
    workspace          TEXT,
    worker_hermes_home TEXT,
    transcript_path    TEXT,
    created_at         REAL NOT NULL,
    -- 2026-10-03 反向校验通道: reaper 每轮从 state.db 读 session ground
    -- truth 后回灌的 4 列。alive=NULL 表示尚未校验过,不算"真死"。
    session_alive              INTEGER,
    session_last_activity_at   REAL,
    session_end_reason         TEXT,
    session_health_check_at    REAL
);
CREATE TABLE IF NOT EXISTS harvest_contracts (
    task_id         TEXT PRIMARY KEY,
    contract_json   TEXT NOT NULL,
    harvest_status  TEXT NOT NULL DEFAULT 'PENDING',
    exit_code       INTEGER,
    harvest_log     TEXT,
    pass_count      INTEGER NOT NULL DEFAULT 0,
    required_passes INTEGER,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    worker_report   TEXT,
    seq             INTEGER
);CREATE TABLE IF NOT EXISTS mw_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,
    task_id     TEXT,
    payload     TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mw_events_created ON mw_events(created_at);
CREATE TABLE IF NOT EXISTS mw_webhooks (
    id          TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    events      TEXT NOT NULL,
    secret      TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS submit_log (
    idempotency_key TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL,
    manifest_json   TEXT NOT NULL,
    created_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_initiators (
    task_id    TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_resume_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    instruction  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'PENDING',
    requested_at REAL NOT NULL,
    finished_at  REAL,
    result_json  TEXT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_resume_pending
    ON task_resume_requests(status, requested_at);
-- 2026-10-03 Team Work 顶层对象化:team work 必须包含一条记录(team_work_runs 一行
-- = 一个 run,run_id 在 launch 时生成,绝不从 manifest 派生)。一次只能一个 ACTIVE
-- team work:DB 层 partial UNIQUE INDEX 兜底 race,应用层 if 必须同步存在,缺一并发下
-- 会同时通过。current_card_id 是 ACTIVE run 内唯一可派卡,advance 前必须 NULL。
-- 孤儿 PENDING (无 team_work_run_id) reaper SKIP,操作员走 close 端点手动归档。
CREATE TABLE IF NOT EXISTS team_work_runs (
    run_id              TEXT PRIMARY KEY,
    initiator_session   TEXT NOT NULL,
    initial_task        TEXT NOT NULL,
    project_workspace   TEXT,
    current_card_id     TEXT,
    status              TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK(status IN ('ACTIVE','CLOSED','ABORTED','ARCHIVED')),
    card_count          INTEGER NOT NULL DEFAULT 0,
    created_at          REAL NOT NULL,
    closed_at           REAL,
    closed_reason       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_team_work_one_active
    ON team_work_runs(status) WHERE status='ACTIVE';
"""


class Ledger:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        # busy_timeout: reaper 15s 一轮, harvest/relay 跨进程抢连接时
        # 等 30s 而不是立刻 raise OperationalError (实测 201 上 "database
        # is locked" 把整轮 reap 废掉, 链路永远不闭环)。
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        # 问题3B: 兼容旧 db, 加 harvest_lock 字段(如果不存在)
        # 2026-09-30: 兼容旧 db, 加 pass_count/required_passes 字段(问题7C)
        # 否则代码侧 SCHEMA 期望的列在旧 db 上不存在, submit() 走 put_contract()
        # 时 SELECT pass_count 抛 sqlite3.OperationalError, launch 端点返 500。
        try:
            cols = [r["name"] for r in self.conn.execute(
                "PRAGMA table_info(harvest_contracts)").fetchall()]
            if "harvest_lock" not in cols:
                self.conn.execute(
                    "ALTER TABLE harvest_contracts "
                    "ADD COLUMN harvest_lock INTEGER NOT NULL DEFAULT 0")
            if "pass_count" not in cols:
                self.conn.execute(
                    "ALTER TABLE harvest_contracts "
                    "ADD COLUMN pass_count INTEGER NOT NULL DEFAULT 0")
            if "required_passes" not in cols:
                self.conn.execute(
                    "ALTER TABLE harvest_contracts "
                    "ADD COLUMN required_passes INTEGER")
            if "worker_report" not in cols:
                self.conn.execute(
                    "ALTER TABLE harvest_contracts "
                    "ADD COLUMN worker_report TEXT")
            if "last_progress_at" not in cols:
                self.conn.execute(
                    "ALTER TABLE harvest_contracts "
                    "ADD COLUMN last_progress_at REAL")
            if "last_notified_at" not in cols:
                self.conn.execute(
                    "ALTER TABLE harvest_contracts "
                    "ADD COLUMN last_notified_at REAL")
            if "notify_attempts" not in cols:
                self.conn.execute(
                    "ALTER TABLE harvest_contracts "
                    "ADD COLUMN notify_attempts INTEGER NOT NULL DEFAULT 0")
            if "notify_dead_until" not in cols:
                self.conn.execute(
                    "ALTER TABLE harvest_contracts "
                    "ADD COLUMN notify_dead_until REAL")
            # 2026-10-03 反向校验通道: task_sessions 加 4 列,reaper 每轮
            # 从 ~/.hermes/state.db 读 session 真实状态后回灌。纯加列,
            # 旧 db 迁移幂等 (PRAGMA table_info 检查后再 ADD)。
            ts_cols = [r["name"] for r in self.conn.execute(
                "PRAGMA table_info(task_sessions)").fetchall()]
            if "session_alive" not in ts_cols:
                self.conn.execute(
                    "ALTER TABLE task_sessions "
                    "ADD COLUMN session_alive INTEGER")
            if "session_last_activity_at" not in ts_cols:
                self.conn.execute(
                    "ALTER TABLE task_sessions "
                    "ADD COLUMN session_last_activity_at REAL")
            if "session_end_reason" not in ts_cols:
                self.conn.execute(
                    "ALTER TABLE task_sessions "
                    "ADD COLUMN session_end_reason TEXT")
            if "session_health_check_at" not in ts_cols:
                self.conn.execute(
                    "ALTER TABLE task_sessions "
                    "ADD COLUMN session_health_check_at REAL")
            # 2026-10-03 Team Work 顶层对象化:harvest_contracts 加
            # team_work_run_id 列(可选 NULL;legacy 旧数据不带 run_id,reaper
            # 见到 NULL 直接 SKIP)。迁移幂等:PRAGMA table_info 检查后再 ADD。
            hc_cols = [r["name"] for r in self.conn.execute(
                "PRAGMA table_info(harvest_contracts)").fetchall()]
            if "team_work_run_id" not in hc_cols:
                self.conn.execute(
                    "ALTER TABLE harvest_contracts "
                    "ADD COLUMN team_work_run_id TEXT")
            # 2026-10-04 relay 三态: notify_confirmed_at 记录「main 真收到
            # (HTTP 200)」的时刻。配合 last_notified_at(claim 时刻)构成:
            #   待发送     = confirmed_at IS NULL AND (last_notified_at IS NULL
            #                或 claim 已过在途超时)
            #   等待确认   = confirmed_at IS NULL AND last_notified_at 在途未超时
            #   已确认送达 = confirmed_at IS NOT NULL
            # relay 扫描只认 confirmed_at IS NULL; claim 原子把 待发送→等待确认;
            # 真 200 调 confirm_delivered 置 confirmed_at。
            if "notify_confirmed_at" not in hc_cols:
                self.conn.execute(
                    "ALTER TABLE harvest_contracts "
                    "ADD COLUMN notify_confirmed_at REAL")
            # F4 (2026-10-04): NULL 槽兜底敲门的去重时刻 —— run 级,
            # 记录上次「run ACTIVE 但 current_card_id=NULL 且有未结算卡」
            # 的敲门时间, 每 stuck 窗口最多敲一次。
            tw_cols = [r["name"] for r in self.conn.execute(
                "PRAGMA table_info(team_work_runs)").fetchall()]
            if "last_null_slot_knock_at" not in tw_cols:
                self.conn.execute(
                    "ALTER TABLE team_work_runs "
                    "ADD COLUMN last_null_slot_knock_at REAL")
            # F4b (2026-10-09): NULL 槽计时基准 —— current_card_id 上次
            # 被清成 NULL 的时刻 (epoch REAL)。事故 run 30625271a15e4a45:
            # 兜底敲门拿 created_at 当闲置基准, 「等待 main 派发 95.9 分钟」
            # 实为交接间隙 ~6s。NULL = 从未清过 (run 出生即 NULL / 迁移前
            # 旧行), 调用方兜底 created_at。迁移幂等: PRAGMA 检查后 ADD。
            if "current_card_cleared_at" not in tw_cols:
                self.conn.execute(
                    "ALTER TABLE team_work_runs "
                    "ADD COLUMN current_card_cleared_at REAL")
            # F5-2 (2026-10-13, issue #5 状态机统一):run 级 handoff 窗口
            # 起始时刻 —— clear_team_work_current_card 调用瞬间写的
            # current_card_handoff_at,与 current_card_cleared_at 同时落,
            # 但语义是「旧卡进入 HANDOFF」而非「槽清零」,供
            # derive_worker_state() 区分「清槽但还没出 handoff」与「
            # 清槽已久」两态(HANDOFF vs long-idle)。NULL = 从未进入
            # handoff 窗口 / 老迁移前 run。迁移幂等:PRAGMA 检查后 ADD。
            if "current_card_handoff_at" not in tw_cols:
                self.conn.execute(
                    "ALTER TABLE team_work_runs "
                    "ADD COLUMN current_card_handoff_at REAL")
            # F5-3 (2026-10-13, issue #5): run 上次进入 WAITING 时刻。
            # reaper 在「SSE 闲置 + thinking=False + todo=working」
            # 判定成立后调 update_worker_state(...,'WAITING'),
            # 同句写 last_waiting_at;notify_confirmed_at 不动(那是
            # 结算送达字段), 仅 run 级闲置诊断。 NULL = 未 WAITING 过。
            if "last_waiting_at" not in tw_cols:
                self.conn.execute(
                    "ALTER TABLE team_work_runs "
                    "ADD COLUMN last_waiting_at REAL")
            # issue#13 (2026-10-15): waiting_sub 数据戳列 —— worker
            # 在 TODO.md 写 waiting_sub 后, reaper 在本卡首遇此状态时
            # 盖戳 (COALESCE 不覆盖); 仅作 derive_worker_state 派生
            # WAITING_SUB 的数据源, 不参与 harvest_status 状态机 /
            # _STATE_TRANSITIONS / _ACTOR_POLICY. 离开 waiting_sub
            # (worker 改 working/completed/failed/blocked) 时清 NULL.
            # 迁移幂等: PRAGMA 检查后 ADD.
            if "subagent_wait_since" not in hc_cols:
                self.conn.execute(
                    "ALTER TABLE harvest_contracts "
                    "ADD COLUMN subagent_wait_since REAL")
            # 2026-10-09 Issue#2: harvest_contracts 不可变注册序列 seq。
            # updated_at 被 put_contract/set_harvest/settle 反复刷新,
            # 用它排序会让完成卡排后、未动卡排前 (#2 根因)。
            # seq 在 register 时一次性写入, 此后不再变; 迁移对存量行
            # 按 updated_at ASC 快照注册序 (REQ 已知风险#2 口径),
            # 只碰 seq IS NULL 行 → 幂等, 重跑无害。
            if "seq" not in hc_cols:
                self.conn.execute(
                    "ALTER TABLE harvest_contracts ADD COLUMN seq INTEGER")
                self.conn.execute(
                    "UPDATE harvest_contracts SET seq = ("
                    "  SELECT rn FROM ("
                    "    SELECT task_id, ROW_NUMBER() OVER ("
                    "      PARTITION BY team_work_run_id"
                    "      ORDER BY updated_at ASC, task_id ASC) AS rn"
                    "    FROM harvest_contracts"
                    "    WHERE team_work_run_id IS NOT NULL) r"
                    "  WHERE r.task_id = harvest_contracts.task_id)"
                    " WHERE seq IS NULL AND team_work_run_id IS NOT NULL")
            # 2026-10-17 Issue#11 (s2-fix-11): 可变『计划位』plan_seq。
            # 动机: seq 不可变(Issue#2 派发幂等保证)+ register 时按
            # MAX(seq)+1 自动取号 ⇒ add_cards 追加卡恒排 seq 尾。
            # 但用户的真实诉求是『排队列表预览应呈现真实队伍顺序』
            # (#11 reopen 截图 #7/#8 紧急追加却排尾, 与真实派发序矛盾)。
            # 修法: 与 seq 并列加一列 plan_seq REAL, 默认 NULL。
            #   - api_team_work_get 未执行段排序键由『seq ASC』改为
            #     『COALESCE(plan_seq, seq) ASC』, NULL 行为等同于 seq,
            #     对所有 launch 时入卡的现存数据零回归。
            #   - add_cards 端点接受可选 position(float), 写 plan_seq;
            #     不传 position 走 COALESCE 落 seq (向后兼容)。
            #   - launch 暂不传 plan_seq —— launch 时 seq=1..N 即为完整
            #     计划序, 不需要 plan_seq 重复。
            #   - plan_seq 不可作为主排序键以外的角色, 仅服务展示层
            #     排序; 不参与 dispatch 逻辑(派发由 main 显式 advance
            #     选 task_id, 不读 plan_seq)。
            # 迁移: 仅 ADD COLUMN, NULL = 沿用 seq (无历史破坏)。幂等。
            if "plan_seq" not in hc_cols:
                self.conn.execute(
                    "ALTER TABLE harvest_contracts "
                    "ADD COLUMN plan_seq REAL")
            # 2026-10-06 Team Work 归档 (run 列表收缩):CHECK constraint
            # 加 ARCHIVED 值。SQLite 的 ALTER TABLE 不能改 CHECK,只能
            # rebuild 表:drop → 重建(保留所有数据)。降级路径:db_version
            # 改 ≥6 才执行(其他版本 LIKE 重建不波及)。
            db_version = self.conn.execute(
                "PRAGMA user_version").fetchone()[0]
            if db_version < 6:
                self.conn.execute("PRAGMA foreign_keys=OFF")
                self.conn.execute("BEGIN")
                try:
                    # 重建 team_work_runs
                    self.conn.execute(
                        "ALTER TABLE team_work_runs "
                        "RENAME TO team_work_runs__old")
                    self.conn.execute(
                        "CREATE TABLE team_work_runs ("
                        "    run_id              TEXT PRIMARY KEY,"
                        "    initiator_session   TEXT NOT NULL,"
                        "    initial_task        TEXT NOT NULL,"
                        "    project_workspace   TEXT,"
                        "    current_card_id     TEXT,"
                        "    status              TEXT NOT NULL DEFAULT 'ACTIVE'"
                        "        CHECK(status IN ('ACTIVE','CLOSED',"
                        "                          'ABORTED','ARCHIVED')),"
                        "    card_count          INTEGER NOT NULL DEFAULT 0,"
                        "    created_at          REAL NOT NULL,"
                        "    closed_at           REAL,"
                        "    closed_reason       TEXT,"
                        "    last_null_slot_knock_at REAL,"
                        # F4b (2026-10-09): 与 A-1 迁移列同步, 防
                        # INSERT SELECT * 列数不匹配
                        "    current_card_cleared_at REAL"
                        ")")
                    self.conn.execute(
                        "INSERT INTO team_work_runs "
                        "SELECT * FROM team_work_runs__old")
                    self.conn.execute(
                        "DROP TABLE team_work_runs__old")
                    self.conn.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS "
                        "idx_team_work_one_active "
                        "ON team_work_runs(status) WHERE status='ACTIVE'")
                    self.conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_team_work_status "
                        "ON team_work_runs(status, created_at DESC)")
                    self.conn.execute("PRAGMA user_version = 6")
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
                finally:
                    self.conn.execute("PRAGMA foreign_keys=ON")
            # 索引 schema CREATE 在 SCHEMA 字符串里 CREATE TABLE IF NOT
            # EXISTS 已覆盖;但 SQLite 的 ALTER TABLE ADD COLUMN 没法自动加
            # 索引,这里补一个 idx_harvest_run 兜底(幂等)。
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_harvest_run "
                "ON harvest_contracts(team_work_run_id)")
            # 2026-10-09 Issue#2: 服务 run 内 seq 排序查询 (api_team_work_get)
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_harvest_run_seq "
                "ON harvest_contracts(team_work_run_id, seq)")
            # 2026-10-13 Issue#8: 一 run 一 session 护栏 — 按 initiator_session
            # 反查「这个 session 是否已绑过 run」。索引覆盖 launch 守卫的
            # 频繁查询路径 (AC8-1)。幂等重复 CREATE 不报错。
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_team_work_initiator "
                "ON team_work_runs(initiator_session)")
            self.conn.commit()
        except sqlite3.Error:
            pass
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # ---- submit 幂等 ----
    def find_submit(self, key: str):
        r = self.conn.execute(
            "SELECT task_id FROM submit_log WHERE idempotency_key=?", (key,)
        ).fetchone()
        return r["task_id"] if r else None

    def record_submit(self, key: str, task_id: str, manifest: dict):
        self.conn.execute(
            "INSERT OR REPLACE INTO submit_log VALUES (?,?,?,?)",
            (key, task_id, json.dumps(manifest, ensure_ascii=False), time.time()),
        )
        self.conn.commit()

    # ---- session 锚点 ----
    def upsert_session(self, task_id, session_id, profile, runner_kind=None,
                      container_id=None, workspace=None, worker_hermes_home=None):
        # 2026-10-01 bug 修复: ON CONFLICT 必须把 runner_kind 列入 UPDATE,
        # 否则 chain 卡 pipeline.py:330 预留的 "main-claimed" 行会被
        # 后续 spawn-bg upsert_session(..., runner_kind="api") 调覆盖时
        # 保留 runner_kind 不变 → ledger.task_sessions.runner_kind 永远卡死
        # "main-claimed" → harvest line 374 走 export_transcript 路径失败
        # → settled=False → harvest_status=ERROR → 卡永远 stuck running.
        # 修法: UPDATE SET 加上 runner_kind 字段,让真 runner 覆盖占位。
        self.conn.execute(
            """INSERT INTO task_sessions (task_id, session_id, profile, runner_kind,
                 container_id, workspace, worker_hermes_home, created_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(task_id) DO UPDATE SET
                 session_id=excluded.session_id,
                 container_id=excluded.container_id,
                 runner_kind=COALESCE(excluded.runner_kind, task_sessions.runner_kind),
                 workspace=COALESCE(excluded.workspace, task_sessions.workspace),
                 worker_hermes_home=COALESCE(excluded.worker_hermes_home,
                                            task_sessions.worker_hermes_home)""",
            (task_id, session_id, profile, runner_kind, container_id,
             workspace, worker_hermes_home, time.time()),
        )
        self.conn.commit()

    def get_session(self, task_id):
        return self.conn.execute(
            "SELECT * FROM task_sessions WHERE task_id=?", (task_id,)
        ).fetchone()

    # ---- 2026-10-03 反向校验通道: reaper 从 state.db 读 session ground
    # truth 后回灌到这里。每张卡的 session_alive / session_end_reason /
    # session_last_activity_at / session_health_check_at 是 reaper 的视野,
    # 不是 LLM 的视野 —— LLM 永远只读。
    def set_session_health(self, task_id, *, alive: Optional[bool],
                           last_activity_at: Optional[float],
                           end_reason: Optional[str],
                           checked_at: float):
        """回灌单卡的 session 健康快照。失败抛,reaper 自己 try/except。"""
        self.conn.execute(
            """UPDATE task_sessions
                  SET session_alive=?,
                      session_last_activity_at=?,
                      session_end_reason=?,
                      session_health_check_at=?
                WHERE task_id=?""",
            (1 if alive else 0 if alive is not None else None,
             last_activity_at, end_reason, checked_at, task_id),
        )
        self.conn.commit()

    def get_session_health(self, task_id):
        """读单卡的反向校验结果。None = 该 task_id 没锚过 session。"""
        return self.conn.execute(
            """SELECT session_alive, session_last_activity_at,
                      session_end_reason, session_health_check_at
                 FROM task_sessions WHERE task_id=?""",
            (task_id,),
        ).fetchone()

    def is_task_running(self, task_id: str) -> dict:
        """同 task 是否已有活跃 session(marker 未回填 exit_code)。

        2026-10-02 加 (重复 spawn 护栏):
          main agent 误重复 spawn 同 task 时,会拿到已存在但没结算的 session,
          不应再预建 session / 拉起第二个 worker。返回 dict:
            {running: bool, session_id, started_at, marker_exit_code,
             harvest_status} — 调用方按需决策 (resume / reuse / 拒 spawn)。

        来源: marker (权威 started_at + exit_code) + task_sessions (session_id)
              + harvest_contracts (harvest_status)。

        T3 升级 (2026-10-03): 若 task 属于某 ACTIVE team work, 校验:
          1) run.status='ACTIVE'
          2) run.current_card_id == task_id (advance 已 set)
        任一不满足 → running=False + 注明 team_work_violation。
        """
        from .reaper import read_marker
        base = os.environ.get("MW_HOME", ".")
        mk = read_marker(base, task_id)
        sess = self.get_session(task_id)
        hc = self.conn.execute(
            "SELECT harvest_status, exit_code, team_work_run_id "
            "FROM harvest_contracts WHERE task_id=?", (task_id,)).fetchone()
        marker_exit = mk.get("exit_code") if mk else None
        marker_started = mk.get("started_at") if mk else None
        basic_running = (marker_exit is None and marker_started is not None
                        and (not hc or hc["harvest_status"] == "PENDING"))
        # ★ T3: team work 串行校验 (防止绕过 advance 直接 dispatch)
        run_id = hc["team_work_run_id"] if hc else None
        team_work_violation = None
        if run_id and basic_running:
            run = self.get_team_work(run_id)
            if not run:
                team_work_violation = f"run {run_id} 不存在"
            elif run["status"] != "ACTIVE":
                team_work_violation = (f"run {run_id} 状态 {run['status']} "
                                       f"非 ACTIVE")
            elif run["current_card_id"] != task_id:
                team_work_violation = (f"run {run_id}.current_card_id="
                                       f"{run['current_card_id']!r} "
                                       f"!= task_id={task_id!r}")
        return {
            "running": bool(basic_running and not team_work_violation),
            "session_id": sess["session_id"] if sess else None,
            "started_at": marker_started,
            "marker_exit_code": marker_exit,
            "harvest_status": hc["harvest_status"] if hc else None,
            "team_work_run_id": run_id,
            "team_work_violation": team_work_violation,
        }

    # ---- harvest 契约 ----
    def put_contract(self, task_id: str, contract: dict,
                     required_passes: int | None = None):
        """写入契约。

        required_passes 参数已废弃 (2026-10-08 边界减法): 它来自 workflow 的
        pass_threshold, 原用于「通过 N 次才 COMPLETE」的 loop 门控 (PASS_BUMPED)。
        该门控已删 —— 重复执行由 main 建多张卡实现, mw 不再按次数门控。
        参数保留仅为向后兼容 (DB 列仍在, 写 NULL 无害); 新代码不要传。
        """
        now = time.time()
        # 保留既有 pass_count
        # 注意: INSERT OR REPLACE 是整行替换, 未出现在列清单里的列一律归 NULL。
        # team_work_run_id 由 launch 的 register_team_work_card 回填, 而 dispatch
        # 内部会再次 put_contract → 第一张卡的 run_id 被抹成 NULL,reaper 的
        # pending_in_team_work(run_id) 就再也扫不到它, harvest 永不触发
        # (2026-10-04 170 实测: 未派发的 T2 有 run_id, 被 dispatch 的 T1 是
        # NULL, 整条实时链路静默卡死)。修法: 读出既有 run_id 并写回, 让
        # put_contract 对归属字段幂等。
        # 2026-10-09 Issue#2: seq 同型问题 —— register 写入的不可变注册
        # 序列会被 put_contract 的整行替换抹成 NULL, 每次 dispatch 都把
        # 排序键归零, #2 原样复发 (R-2)。同样读出 seq 并写回。
        # 2026-10-17 issue#11 s2-fix-11: plan_seq 同型问题 —— register 写入
        # 的 plan_seq 也会被 put_contract 整行替换抹成 NULL; 同样读出
        # plan_seq 并写回, 让 put_contract 对 plan_seq 字段幂等。
        cur = self.conn.execute(
            "SELECT pass_count, team_work_run_id, seq, plan_seq "
            "FROM harvest_contracts WHERE task_id=?",
            (task_id,)).fetchone()
        existing_count = cur["pass_count"] if cur else 0
        existing_run = cur["team_work_run_id"] if cur else None
        existing_seq = cur["seq"] if cur else None
        existing_plan_seq = cur["plan_seq"] if cur else None
        self.conn.execute(
            "INSERT OR REPLACE INTO harvest_contracts "
            "(task_id, contract_json, harvest_status, exit_code, harvest_log, "
            "pass_count, required_passes, created_at, updated_at, "
            "team_work_run_id, seq, plan_seq) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, json.dumps(contract, ensure_ascii=False), "PENDING",
             None, None, existing_count, required_passes, now, now,
             existing_run, existing_seq, existing_plan_seq),
        )
        self.conn.commit()

    # bump_pass_count 已删除 (2026-10-08 边界减法): pass_threshold / PASS_BUMPED
    # 门控已废, 无人再需要累加通过次数。重复执行由 main 建多张卡实现。

    # ---- 问题3B: harvest 行级锁 (CAS 抢锁) ----
    def try_claim_harvest(self, task_id: str) -> bool:
        """原子 CAS 把 harvest_lock 0→1。返回 True=抢到, False=已被别的进程锁住。

        用法: reaper / resume / console /api/resume 三条入口在结算前都先调本方法,
        抢不到直接放弃本轮, 等下轮(下一次 reaper 扫描)重试。
        防止 resume 与 reaper 同一 task 并发 harvest 互踩 ledger。
        """
        cur = self.conn.execute(
            "UPDATE harvest_contracts SET harvest_lock=1 "
            "WHERE task_id=? AND harvest_lock=0",
            (task_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def release_harvest(self, task_id: str):
        """释放锁。harvest 完成后(无论成功失败)必须调, 否则永久占用。"""
        self.conn.execute(
            "UPDATE harvest_contracts SET harvest_lock=0 WHERE task_id=?",
            (task_id,))
        self.conn.commit()

    def harvest_is_locked(self, task_id: str) -> bool:
        """查询锁状态(调试用)。"""
        cur = self.conn.execute(
            "SELECT harvest_lock FROM harvest_contracts WHERE task_id=?",
            (task_id,)).fetchone()
        return bool(cur["harvest_lock"]) if cur else False

    def get_contract(self, task_id: str):
        return self.conn.execute(
            "SELECT * FROM harvest_contracts WHERE task_id=?", (task_id,)
        ).fetchone()

    def get_product_workspace(self, task_id: str) -> Optional[str]:
        """从 submit_log.manifest_json 提取 project_workspace。

        解决: project 任务 task workspace 已隔离到 .tasks/<id>/,
        但产物 required_files 仍按 project_workspace 解析(harvest 端需要)。
        """
        r = self.conn.execute(
            "SELECT manifest_json FROM submit_log WHERE task_id=?",
            (task_id,)).fetchone()
        if not r:
            return None
        try:
            return json.loads(r["manifest_json"]).get("project_workspace")
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def set_harvest(self, task_id, status, exit_code=None, log=None, transcript=None):
        cur = self.conn.execute(
            "SELECT * FROM harvest_contracts WHERE task_id=?", (task_id,)
        ).fetchone()
        if not cur:
            raise KeyError(task_id)
        self.conn.execute(
            """UPDATE harvest_contracts SET harvest_status=?, exit_code=?,
                 harvest_log=?, updated_at=? WHERE task_id=?""",
            (status, exit_code, (log or "")[:2000], time.time(), task_id),
        )
        if transcript:
            self.conn.execute(
                "UPDATE task_sessions SET transcript_path=? WHERE task_id=?",
                (transcript, task_id),
            )
        self.conn.commit()

    def touch_progress(self, task_id: str):
        """更新 last_progress_at — reaper 敲门判断『挂太久』的唯一依据。

        任何 worker 侧可见的进展(结算落账/pass bump)都应调用。
        """
        self.conn.execute(
            "UPDATE harvest_contracts SET last_progress_at=? WHERE task_id=?",
            (time.time(), task_id))
        self.conn.commit()

    # ---- M4 统一状态更新器 (BATCH1-DESIGN §M4, #5 状态单源收敛) ----
    # 所有 harvest_status 迁移的唯一入口。收口原则: 任何迁移都原子完成
    # 「合法性检查 + 落库 + touch_progress + worker.state 事件」, 消除
    # 旧裸 UPDATE 的口径漂移 (改了状态没 bump progress / 没发事件)。
    # set_harvest 保留为底层原语 (本方法内部调用, 也兼容旧测试直调),
    # 外部代码一律走本方法。
    #
    # F5 扩充 (2026-10-13, issue #5 状态机统一 by 2026-10-12 评论口径):
    # 状态集合新增:
    #   - WORKING  在前 _STATE_TRANSITIONS 已存在, 现在真正可达:
    #     advance 占槽即视(actor=harvest with handoff→working);
    #   - HANDOFF  worker 交卷瞬间, 旧卡进入; 视 run 正处切换中。
    #   - WAITING  reaper 确认闲置(SSE 静默 + thinking=False +
    #     todo=working), 等 main 干预。
    # 这三态不是真终态, 都可被 actor=harvest / main_correct 再推出。
    # 既有 PENDING/HARVESTED/NEED_CLARIFICATION/BLOCKED/ERROR 含义不变,
    # 仅迁移边集合按「run 推进 = 线性游标」语义收窄。
    _STATE_TRANSITIONS = {
        "PENDING": {"WORKING", "WAITING", "HARVESTED", "BLOCKED",
                    "NEED_CLARIFICATION", "ERROR", "CANCELLED", "ARCHIVED"},
        "WORKING": {"WAITING",
                    "HARVESTED", "BLOCKED", "NEED_CLARIFICATION", "ERROR",
                    "PENDING", "CANCELLED", "ARCHIVED"},
        "WAITING": {"WORKING",  # main 介入:resume / 派回 worker(2.4 出口)
                    "PENDING",  # main 撤回/重排(2.1)
                    "BLOCKED", "NEED_CLARIFICATION", "ERROR",  # main 强制判
                    "HARVESTED",  # reaper/harvest 兜底判 completed
                    "CANCELLED", "ARCHIVED"},
        "BLOCKED": {"PENDING", "WORKING",  # main_correct: 翻案
                    "HARVESTED", "BLOCKED",
                    "NEED_CLARIFICATION", "ERROR", "CANCELLED", "ARCHIVED"},
        "NEED_CLARIFICATION": {"PENDING", "WORKING",
                               "HARVESTED", "BLOCKED",
                               "NEED_CLARIFICATION", "ERROR",
                               "CANCELLED", "ARCHIVED"},
        "ERROR": {"PENDING", "WORKING",
                  "HARVESTED", "BLOCKED",
                  "NEED_CLARIFICATION", "CANCELLED", "ARCHIVED"},
        # 真终态只走操作员/归档路径, 不可回退
        "HARVESTED": {"ARCHIVED", "CANCELLED"},
        "ARCHIVED": set(),
        "CANCELLED": set(),
    }
    # 注:「HANDOFF」不是 storage 字段, 而是 run 级派生态 —— 由
    # ledger.current_card_handoff_at (F5-2) + card 是 ledger 最近更新
    # 一行, derive_worker_state 读出。 用户看到的「交接中」就是
    # 这个状态, 但 actor 白名单里没有 WORKING→HANDOFF (harvest
    # 直接写到 HARVESTED/BLOCKED/NEED_CLARIFICATION, 没有中间跳)。
    # 派生态的细节在 derive_worker_state() 注释里。

    # F5 actor 白名单 (2026-10-13): 状态迁移 (from, to) 二元组到合法 actor
    # 集合。不在白名单的 (actor, from, to) 组合 raise PermissionError,
    # 与 ValueError(非法状态迁移)区别开: 一个是「状态能不能跳」, 一个是
    # 「谁能下令跳」。 收口目的就是 2026-10-12 评论 2.1~2.5 五条路径的
    # 机器化兜底 (AC5-3)。
    #
    # 路径白名单对应:
    #   2.1 main 撤回/重排 → PENDING           : actor=main_correct
    #   2.2 worker 自判 → 终态/working/handoff  : actor=harvest
    #   2.3 reaper 确认闲置 → WAITING          : actor=reaper
    #   2.4 waiting 再激活仍闲置 → main 介入    : actor=main_correct(re-routing)
    #   2.5 人工取消/归档                       : actor=operator
    # + advance (settle_residue 隐式判终态)    : actor=advance
    # + resume (pipeline)                       : actor=resume
    # 这五条之外的 actor, 任何 (from, to) 都拒。
    #
    # R1 补齐 (2026-10-13, issue #12): 程序化比对 _STATE_TRANSITIONS 与
    # _ACTOR_POLICY 的差集, 收口遗漏的 11 条边——
    #   {BLOCKED/ERROR/NEED_CLARIFICATION} 互相间 + 各自自环 + 任意→HARVESTED。
    # actor 集合按 2.1~2.5 + advance + resume 原则按路径语义补齐:
    #   - 自环 (BLOCKED→BLOCKED / NEED_CLARIFICATION→NEED_CLARIFICATION):
    #     reaper/harvest 重打同状态戳, 仅原 tier 的报告者允许;
    #   - 升级到 ERROR/NEED_CLARIFICATION: actor=harvest (worker 自报);
    #   - 降级 (ERROR→BLOCKED): main_correct/harvest (main 翻案或 worker 自我纠正);
    #   - NEED_CLARIFICATION→BLOCKED (超时未答): reaper + main_correct;
    #   - 任意 → HARVESTED: advance (settle_team_work_residue 主路径) +
    #     harvest (worker 自报完成) + main_correct (翻案结算)。
    _ACTOR_POLICY = {
        # harvest: worker 自报路径 / run 切换路径
        ("PENDING", "WORKING"): {"harvest", "main_correct"},
        ("PENDING", "WAITING"): {"reaper"},
        ("WORKING", "WAITING"): {"reaper"},
        ("WORKING", "HARVESTED"): {"harvest"},
        ("WORKING", "BLOCKED"): {"harvest", "reaper"},  # reaper 可超时收尸
        ("WORKING", "NEED_CLARIFICATION"): {"harvest"},
        ("WORKING", "ERROR"): {"harvest"},
        ("WORKING", "PENDING"): {"main_correct"},  # 2.1 main 撤回
        # waiting 由 main / reaper 推出
        ("WAITING", "WORKING"): {"main_correct", "harvest"},  # 2.4 main 介入
        # issue #65 C-1: resume 亦可把 WAITING 卡拨回 PENDING —— resume 副作用
        # 链 (pipeline.resume_task) 对 WAITING 卡必炸 PermissionError 是本轮
        # 全盲区的直接根因; WAITING→PENDING 迁移边本就存在 (L665), 仅扩 actor。
        ("WAITING", "PENDING"): {"main_correct", "resume"},  # 2.1 撤回 / resume
        ("WAITING", "HARVESTED"): {"reaper", "harvest", "main_correct"},
        # 各种非真终态 → BLOCKED / NEED_CLARIFICATION / ERROR
        ("PENDING", "BLOCKED"): {"reaper", "harvest"},
        ("PENDING", "NEED_CLARIFICATION"): {"harvest"},
        ("PENDING", "ERROR"): {"harvest"},
        ("PENDING", "HARVESTED"): {"harvest"},
        ("WAITING", "BLOCKED"): {"reaper", "harvest"},
        ("WAITING", "NEED_CLARIFICATION"): {"harvest"},
        ("WAITING", "ERROR"): {"harvest"},
        # resume 路径:任意非真终态可被 resume 拨回 PENDING
        ("BLOCKED", "PENDING"): {"main_correct", "resume", "harvest"},
        ("BLOCKED", "WORKING"): {"main_correct", "harvest"},
        ("NEED_CLARIFICATION", "PENDING"): {"main_correct", "resume", "harvest"},
        ("NEED_CLARIFICATION", "WORKING"): {"main_correct", "harvest"},
        ("ERROR", "PENDING"): {"main_correct", "resume"},
        ("ERROR", "WORKING"): {"main_correct", "harvest"},
        # 任意非真终态 → 真终态(CANCELLED / ARCHIVED)
        ("PENDING", "CANCELLED"): {"operator"},
        ("PENDING", "ARCHIVED"): {"operator"},
        ("WORKING", "CANCELLED"): {"operator", "harvest"},
        ("WORKING", "ARCHIVED"): {"operator"},
        ("WAITING", "CANCELLED"): {"operator"},
        ("WAITING", "ARCHIVED"): {"operator"},
        ("BLOCKED", "CANCELLED"): {"operator"},
        ("BLOCKED", "ARCHIVED"): {"operator"},
        ("NEED_CLARIFICATION", "CANCELLED"): {"operator"},
        ("NEED_CLARIFICATION", "ARCHIVED"): {"operator"},
        ("ERROR", "CANCELLED"): {"operator"},
        ("ERROR", "ARCHIVED"): {"operator"},
        ("HARVESTED", "ARCHIVED"): {"operator"},
        ("HARVESTED", "CANCELLED"): {"operator"},
        # R1 补齐 (2026-10-13, issue #12): _STATE_TRANSITIONS 允许但
        # _ACTOR_POLICY 漏列的 11 条边——任何 (from, to) 都必须至少有一个
        # actor 落在白名单, 否则缺口回到「policy is None 放行任意 actor」
        # 旧语义 (R1 要害)。原则详见 _ACTOR_POLICY 上方注释。
        # 自环: 同 tier 重打状态戳
        ("BLOCKED", "BLOCKED"): {"reaper", "harvest"},
        ("NEED_CLARIFICATION", "NEED_CLARIFICATION"): {"harvest"},
        # 升级到 ERROR/NEED_CLARIFICATION: harvest 单边 (worker 自报)
        ("BLOCKED", "ERROR"): {"harvest"},
        ("BLOCKED", "NEED_CLARIFICATION"): {"harvest"},
        ("ERROR", "NEED_CLARIFICATION"): {"harvest"},
        # 降级 ERROR→BLOCKED: main 翻案或 worker 自我纠正
        ("ERROR", "BLOCKED"): {"main_correct", "harvest"},
        # NEED_CLARIFICATION→BLOCKED: 澄清超时未答
        ("NEED_CLARIFICATION", "BLOCKED"): {"reaper", "main_correct"},
        # NEED_CLARIFICATION→ERROR: 澄清失败升级
        ("NEED_CLARIFICATION", "ERROR"): {"harvest"},
        # 任意 → HARVESTED: advance (settle_residue) + harvest (worker 自报完成)
        # + main_correct (翻案结算)
        ("BLOCKED", "HARVESTED"): {"advance", "harvest", "main_correct"},
        ("ERROR", "HARVESTED"): {"advance", "harvest", "main_correct"},
        ("NEED_CLARIFICATION", "HARVESTED"): {"advance", "harvest", "main_correct"},
        # HARVESTED/ARCHIVED/CANCELLED 真终态之内的边在 _STATE_TRANSITIONS
        # 已禁, 此处不必再列; 列表只验「至少有一条合法路径时, actor
        # 是不是被许可」。缺路径时 update_worker_state 先抛 ValueError。
    }

    def update_worker_state(self, task_id, new_status, *, actor: str,
                          reason: str = "", exit_code=None, log=None,
                          create_missing: bool = False):
        """harvest_status 迁移的唯一入口 (收口)。

        原子完成:
          1. 迁移合法性检查 (_STATE_TRANSITIONS), 非法迁移 raise ValueError
          2. actor 白名单检查 (_ACTOR_POLICY), 非白名单 raise PermissionError
          3. UPDATE harvest_contracts SET harvest_status/exit_code/
             harvest_log(追加 actor/reason 轨迹)/updated_at
          4. touch_progress(task_id)
          5. emit_event("worker.state", {from, to, actor, reason})
        actor ∈ {reaper, harvest, resume, main_correct, operator, advance}
        create_missing=True: 无契约行时先插 {} 占位再迁移 (cancel/archive
        兜底语义, 对齐旧 set_terminal)。
        返回实际写入的新状态字符串。
        """
        cur = self.conn.execute(
            "SELECT harvest_status FROM harvest_contracts WHERE task_id=?",
            (task_id,)).fetchone()
        if cur is None:
            if not create_missing:
                raise KeyError(task_id)
            now0 = time.time()
            self.conn.execute(
                "INSERT INTO harvest_contracts (task_id, contract_json, "
                "harvest_status, pass_count, created_at, updated_at) "
                "VALUES (?,?, 'PENDING', 0, ?, ?)",
                (task_id, "{}", now0, now0))
            cur = self.conn.execute(
                "SELECT harvest_status FROM harvest_contracts WHERE task_id=?",
                (task_id,)).fetchone()
        old = cur["harvest_status"]
        allowed = self._STATE_TRANSITIONS.get(old)
        if allowed is None or new_status not in allowed:
            raise ValueError(
                f"非法状态迁移 {old} → {new_status} (task={task_id}, "
                f"actor={actor})")
        # F5 actor 白名单 (issue #5 统一状态机): 即使 (from, to) 合法,
        # 调用方 actor 也必须在该路径的白名单内。
        policy = self._ACTOR_POLICY.get((old, new_status))
        if policy is not None and actor not in policy:
            raise PermissionError(
                f"actor={actor!r} 不允许迁移 {old} → {new_status} "
                f"(task={task_id});合法 actor={sorted(policy)}")
        # 日志轨迹: 保留调用方 log 并追加 actor/reason, 纠正轨迹可审计
        merged_log = log
        if actor or reason:
            trail = f"[{actor}] {reason}" if reason else f"[{actor}]"
            merged_log = f"{log}\n{trail}" if log else trail
        self.set_harvest(task_id, new_status, exit_code, merged_log)
        self.touch_progress(task_id)
        # F5-3 (2026-10-13): WAITING 入口同步写 run 级 last_waiting_at,
        # 便于 run 级派生端 lookup「run 最近一次进入 WAITING 的时刻」做
        # 闲置归因。仅作用于「确实改变了 run 现状」的迁移 —— 旧值已
        # 是 WAITING 的 no-op 不覆盖。
        if new_status == "WAITING" and old != "WAITING":
            try:
                run_row = self.conn.execute(
                    "SELECT team_work_run_id FROM harvest_contracts "
                    "WHERE task_id=?", (task_id,)).fetchone()
                if run_row and run_row["team_work_run_id"]:
                    self.conn.execute(
                        "UPDATE team_work_runs SET last_waiting_at=? "
                        "WHERE run_id=?",
                        (time.time(), run_row["team_work_run_id"]))
            except Exception:
                pass  # run 关联缺失不阻塞状态落库
        try:
            self.emit_event("worker.state", task_id, {
                "from": old, "to": new_status,
                "actor": actor, "reason": reason,
            })
        except Exception:
            pass  # 事件失败不阻断状态落库
        return new_status


    def record_initiator(self, task_id: str, session_id: str):
        """记录发起这张卡的 main session — worker→main 报告中继的路由表。"""
        self.conn.execute(
            "INSERT OR REPLACE INTO task_initiators (task_id, session_id, created_at) "
            "VALUES (?,?,?)", (task_id, session_id, time.time()))
        self.conn.commit()

    def get_initiator(self, task_id: str) -> Optional[str]:
        cur = self.conn.execute(
            "SELECT session_id FROM task_initiators WHERE task_id=?",
            (task_id,)).fetchone()
        return cur["session_id"] if cur else None

    def should_notify(self, task_id: str, min_interval_s: float = 0.0) -> bool:
        """通知去重: 同卡 min_interval_s 内只叫醒 main 一次。

        2026-10-04 修订 (commit 3 / design.md §5.0.1):
          默认 min_interval_s 从 120s 降为 0 —— fsnotify 事件唯一性即去重,
          不需要 120s 闸门。旧 120s 默认在「乐观标记 + 失败不重发」组合下
          吞掉了首报 (实测 qq-login t_f6b539d1 重复转发就是这个形状)。

        保留的闸门:
          - notify_dead_until (死信退避期): now < dead_until → 不通知
          - 8 次失败后死信 (notify_dead_until=now+1年)

        min_interval_s 参数保留接口 (兼容旧调用方 stuck_notify 等传大值),
        但默认 0 让 relay 路径每次 fsnotify 事件都尝试派发。
        """
        cur = self.conn.execute(
            "SELECT last_notified_at, notify_dead_until FROM "
            "harvest_contracts WHERE task_id=?",
            (task_id,)).fetchone()
        if not cur:
            return False
        last = cur["last_notified_at"]
        dead = cur["notify_dead_until"]
        if dead and time.time() < dead:
            return False
        # min_interval_s=0 默认: 只看死信, 不看 last_notified_at
        # (fsnotify 事件唯一性即去重)
        if min_interval_s <= 0:
            return True
        return last is None or (time.time() - last) >= min_interval_s

    def mark_notified(self, task_id: str):
        self.conn.execute(
            "UPDATE harvest_contracts SET last_notified_at=? WHERE task_id=?",
            (time.time(), task_id))
        self.conn.commit()

    def claim_for_relay(self, task_id: str, inflight_s: float = 660.0) -> bool:
        """原子抢 relay 发送权: 待发送 → 等待确认。

        返回 True=抢到(本调用负责发送), False=已在途/已确认, 不要重复发。

        三态 (2026-10-04, 用户模型「发送成功确认收到=已通知, 发送中=等待确认」):
          待发送     = confirmed_at IS NULL AND (last_notified_at IS NULL
                       OR last_notified_at < now - inflight_s)
          等待确认   = confirmed_at IS NULL AND last_notified_at 在途未超时
          已确认送达 = confirmed_at IS NOT NULL

        为什么必需: relay 用后台线程发送, halogen reasoning turn 跑几十秒,
        而轮询 15s 一轮。发送还没返回、confirm 还没落时, 下一轮又扫到
        「未确认」又起一个 send 线程 → 堆满 gateway run 上限 10 → 429 →
        退避 → 实测 14 分钟延迟。claim 在起线程瞬间原子置 last_notified_at,
        后续轮次扫到「等待确认」直接跳过, 杜绝重复起线程。

        inflight_s 兜底进程崩溃: 若发送线程被 kill (reaper 重启) 没来得及
        confirm 也没 rollback, 超过 inflight_s (略大于发送 timeout=600s) 后
        视为失败, 允许重新 claim。发送失败由 _send_to_main rollback 立即清
        last_notified_at=NULL 回到待发送, 不等 inflight_s。
        """
        now = time.time()
        cur = self.conn.execute(
            "UPDATE harvest_contracts SET last_notified_at=? "
            "WHERE task_id=? AND notify_confirmed_at IS NULL "
            "AND (last_notified_at IS NULL OR last_notified_at < ?)",
            (now, task_id, now - inflight_s))
        self.conn.commit()
        return cur.rowcount > 0

    def confirm_delivered(self, task_id: str):
        """真收到 main gateway HTTP 200 → 已确认送达。

        置 notify_confirmed_at, 清退避计数。此后 relay 扫描
        (WHERE notify_confirmed_at IS NULL) 永不再扫到这行, 除非
        set_worker_report 因报告内容变化重新清 confirmed_at (回流/重结算)。
        """
        self.conn.execute(
            "UPDATE harvest_contracts SET notify_confirmed_at=?, "
            "notify_attempts=0, notify_dead_until=NULL WHERE task_id=?",
            (time.time(), task_id))
        self.conn.commit()

    def set_worker_report(self, task_id: str, report: dict, *, force: bool = False):
        """写入 worker 报告 — 给 main 看的结构化产物/状态摘要。

        取代旧设计中 harvest 调 kanban complete 改状态的越界做法。
        Main 读 ledger.worker_report 决定下一步 (complete / resume / re-route)。

        ★ 2026-10-02 修 (qq-login 实测): 重复落账 → 重复转发修复。
          旧版无条件 `last_notified_at=NULL`, 任何同 verdict 的重复
          harvest 调用都会清空标记, 导致 reaper 重复 dispatch 同份报告
          给 main (实测 3 次)。改: 只有报告内容(verdict + evidence 序列)
          变化时才清 NULL 触发新转发; 同 verdict 重复 harvest 视为幂等,
          保留 last_notified_at 不动, reaper 自然不重发。
          force=True 绕过内容比较(留 escape hatch)。
        """
        new_blob = json.dumps(report, ensure_ascii=False, sort_keys=True)
        cur = self.conn.execute(
            "SELECT worker_report FROM harvest_contracts WHERE task_id=?",
            (task_id,)).fetchone()
        # 旧报告 hash 比较 — 必须在判定里把"无变化"和"force"区分清楚
        same_content = False
        if cur and cur["worker_report"] and not force:
            try:
                old_blob = json.dumps(json.loads(cur["worker_report"]),
                                      sort_keys=True, ensure_ascii=False)
                same_content = (old_blob == new_blob)
            except (json.JSONDecodeError, TypeError):
                same_content = False
        if same_content:
            # 报告内容未变 — 保留 last_notified_at / notify_confirmed_at,
            # 只 touch updated_at/progress。重发链路被阻断。
            self.conn.execute(
                "UPDATE harvest_contracts SET updated_at=?, "
                "last_progress_at=? WHERE task_id=?",
                (time.time(), time.time(), task_id))
        else:
            # 内容变化 → 清 confirmed_at + last_notified_at: 回到「待发送」,
            # 让 relay 重新投一份新报告 (回流/重结算场景)。
            # F5 (2026-10-04): 同时清死信/退避 —— 新报告 = 新事件, 旧死信
            # 不该封杀它。(201 实测: 已结算卡 notify_attempts 堆到 10 →
            # dead_until=+1年, 之后这张卡任何合法后续通知被永久静音。)
            self.conn.execute(
                "UPDATE harvest_contracts SET worker_report=?, updated_at=?, "
                "last_progress_at=?, last_notified_at=NULL, "
                "notify_confirmed_at=NULL, notify_dead_until=NULL, "
                "notify_attempts=0 WHERE task_id=?",
                (new_blob, time.time(), time.time(), task_id))
        self.conn.commit()

    def get_worker_report(self, task_id: str) -> Optional[dict]:
        cur = self.conn.execute(
            "SELECT worker_report FROM harvest_contracts WHERE task_id=?",
            (task_id,)).fetchone()
        if not cur or not cur["worker_report"]:
            return None
        try:
            return json.loads(cur["worker_report"])
        except json.JSONDecodeError:
            return None

    def pending(self):
        return self.conn.execute(
            "SELECT * FROM harvest_contracts WHERE harvest_status='PENDING'"
        ).fetchall()

    def pending_or_errored(self):
        """2026-10-02: SE 卡实测根因 (201 reaper 永远扫不到 stuck 卡)。
        旧 pending() 只看 PENDING 卡, 一旦 harvest 写了 ERROR/BLOCKED 就再也进不来。
        reaper 周期里 harvest_status 永远 stuck, chain 永远断。
        新接口: 看 PENDING + ERROR + BLOCKED, 让 reaper/_todo_result_scan 有机会救回来。
        (harvest 内部有 PENDING 守卫, 同卡不会二次结算。)
        """
        return self.conn.execute(
            "SELECT * FROM harvest_contracts "
            "WHERE harvest_status IN ('PENDING','ERROR','BLOCKED')"
        ).fetchall()

    # ---- 终态标记 (取消/归档, 2026-10-01; M4 收编 2026-10-09) ----
    def set_terminal(self, task_id: str, status: str, log: str = ""):
        """把 task 标成终态 (CANCELLED / ARCHIVED), 让 reaper 与 chain_daemon 都跳过。

        即使该 task 从没写过 harvest 契约(例如 chain 卡只建了 ledger session 没
        contract), 这里也会 INSERT 一条占位契约, 保证终态一定落库、绝不会被
        后续 reap/chain_daemon 误激活。

        M4 收编 (BATCH1-DESIGN): 内部改走 update_worker_state 统一更新器,
        终态迁移同样带 worker.state 事件 + progress bump + 审计轨迹。
        """
        self.update_worker_state(task_id, status, actor="operator",
                                reason=log, create_missing=True)

    def is_terminal(self, task_id: str) -> bool:
        """task 是否已终态 (CANCELLED/ARCHIVED)。无契约视为非终态。"""
        r = self.conn.execute(
            "SELECT harvest_status FROM harvest_contracts WHERE task_id=?",
            (task_id,)).fetchone()
        return bool(r) and r["harvest_status"] in ("CANCELLED", "ARCHIVED")

    # ---- resume 请求队列 (问题6A: resume 异步化) ----
    def enqueue_resume(self, task_id: str, instruction: str) -> int:
        """入队一个 resume 请求。返回 request id。

        语义: reaper 在下轮扫描时执行 resume_task,本函数立即返回。
        同 task 已有 PENDING/PROCESSING 请求时,新指令直接覆盖:
        旧的那条如果 PROCESSING,reaper 完成时会 finish_resume 但 result 与新指令无关,
        但其 result_json 仍记录此次执行结果(供审计)。
        简化:此处只删 PENDING,不去抢 PROCESSING 行的所有权(reaper 自己 finish)。
        """
        # 删旧 PENDING(同 task 多个 PENDING 没意义,后者覆盖前者指令)
        self.conn.execute(
            "DELETE FROM task_resume_requests "
            "WHERE task_id=? AND status='PENDING'", (task_id,))
        cur = self.conn.execute(
            "INSERT INTO task_resume_requests "
            "(task_id, instruction, status, requested_at) "
            "VALUES (?, ?, 'PENDING', ?)",
            (task_id, instruction, time.time()))
        self.conn.commit()
        # 问题7D: 事件 emit(让 SSE/webhook 知道有 resume 请求入队)
        try:
            self.emit_event("resume.enqueued", task_id, {
                "request_id": cur.lastrowid,
                "instruction_preview": instruction[:80],
            })
        except Exception:
            pass
        # 问题7D: 入队时同步 dispatch webhook(不依赖 console 进程, 因为 enqueue 可能在
        # mw.cli / reaper 进程内被调, console webhook dispatcher 跑不到)
        try:
            from .console import dispatch_webhook
            dispatch_webhook({"event_type": "resume.enqueued", "task_id": task_id,
                              "payload": {"request_id": cur.lastrowid}})
        except Exception:
            pass
        return cur.lastrowid

    def claim_pending_resume(self):
        """原子获取一个 PENDING resume 请求并标记 PROCESSING。

        返回 row(单个)或 None。reaper 拿到后调 resume_task,完成时 finish_resume。
        status 用 PROCESSING 防止 reaper 多实例重复消费(实测踩坑);
        单实例时也安全——若 reaper crash, PENDING 行仍能下一轮重试。
        """
        cur = self.conn.execute(
            "SELECT * FROM task_resume_requests "
            "WHERE status='PENDING' ORDER BY requested_at LIMIT 1")
        row = cur.fetchone()
        if not row:
            return None
        self.conn.execute(
            "UPDATE task_resume_requests SET status='PROCESSING' "
            "WHERE id=? AND status='PENDING'", (row["id"],))
        self.conn.commit()
        # 重读返回(可能 race)
        cur = self.conn.execute(
            "SELECT * FROM task_resume_requests WHERE id=?", (row["id"],))
        r = cur.fetchone()
        return r if r and r["status"] == "PROCESSING" else None

    def finish_resume(self, request_id: int, *, ok: bool,
                     result: dict | None = None, error: str | None = None):
        """完成一个 resume 请求。ok=True 记 DONE + result_json; False 记 ERROR + error。"""
        status = "DONE" if ok else "ERROR"
        self.conn.execute(
            "UPDATE task_resume_requests SET status=?, finished_at=?, "
            "result_json=?, error=? WHERE id=?",
            (status, time.time(),
             json.dumps(result, ensure_ascii=False) if result else None,
             error, request_id))
        self.conn.commit()

    # ===== 问题7D: 事件总线 + webhook 订阅 =====
    def emit_event(self, event_type: str, task_id: str = "", payload: dict = None) -> int:
        """写入事件, 返回 id。 任何状态变更(harvest.complete/failed/clarify,
        resume 队列变化, kanban task done)都应调用。"""
        import time as _t
        self.conn.execute(
            "INSERT INTO mw_events(event_type, task_id, payload, created_at) VALUES (?,?,?,?)",
            (event_type, task_id, json.dumps(payload or {}), _t.time()))
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def list_events_since(self, since_id: int = 0, limit: int = 200):
        """SSE 用: 返回 id > since_id 的事件, 按 id 升序。"""
        return self.conn.execute(
            "SELECT id, event_type, task_id, payload, created_at FROM mw_events "
            "WHERE id > ? ORDER BY id ASC LIMIT ?", (since_id, limit)).fetchall()

    def register_webhook(self, url: str, events: list = None, secret: str = "") -> str:
        """注册 webhook 回调, 返回 webhook id (uuid)。 events=[] 监听全部。"""
        import uuid as _u, time as _t
        wid = _u.uuid4().hex[:16]
        ev = ",".join(events or [])
        self.conn.execute(
            "INSERT INTO mw_webhooks(id, url, events, secret, enabled, created_at) VALUES (?,?,?,?,1,?)",
            (wid, url, ev, secret, _t.time()))
        self.conn.commit()
        return wid

    def list_webhooks(self, enabled_only: bool = False):
        return self.conn.execute(
            f"SELECT id, url, events, secret, enabled, created_at FROM mw_webhooks "
            f"{'WHERE enabled=1' if enabled_only else ''} ORDER BY created_at DESC").fetchall()

    def delete_webhook(self, wid: str) -> bool:
        cur = self.conn.execute("DELETE FROM mw_webhooks WHERE id=?", (wid,))
        self.conn.commit()
        return cur.rowcount > 0

    def list_pending_resumes(self, limit: int = 50):
        """列出最近 PENDING/PROCESSING 的 resume 请求(前端调试用)。"""
        return self.conn.execute(
            "SELECT id, task_id, status, requested_at, finished_at, error "
            "FROM task_resume_requests "
            "WHERE status IN ('PENDING','PROCESSING') "
            "ORDER BY requested_at DESC LIMIT ?", (limit,)).fetchall()


    # ===== Team Work 顶层对象化 (2026-10-03) =====
    # Team work 是一次「前后串行」的多卡调度:launch 时建一行 team_work_runs
    # 记录决策,后续每张子卡的归属通过 harvest_contracts.team_work_run_id 反查。
    # 全局不变式:
    #   - 同一时刻全库最多一条 ACTIVE run(应用层 + DB 双重防护)
    #   - current_card_id 是 ACTIVE run 内唯一可派卡槽位,advance 前必须 NULL
    #   - card_count 由 register_team_work_card 单调递增,推进状态时不再减
    #   - close 时未结算卡统一 ARCHIVED,避免 reaper 误激活遗留 PENDING
    def create_team_work_run(self, run_id, session_id, initial_task,
                            project_workspace=None) -> dict:
        """新建一条 ACTIVE run。

        三层防护:
          1) 应用层 SELECT get_active_team_work() 先看,有则直接 raise
             IntegrityError —— 给出友好错误(UI 可提示操作员先 close 老 run)。
          2) DB 层 partial UNIQUE INDEX `idx_team_work_one_active` 兜底 race:
             哪怕两个进程同时通过应用层守卫并发 INSERT,第二个也会被 SQLite
             拒绝,不会产生双 ACTIVE。
          3) Issue#8 (2026-10-13) 一 run 一 session 护栏:本 session_id
             若已作为任何历史 run 的 initiator_session (任意状态), 必须
             raise IntegrityError 由 caller 转 409。报告路由按 initiator
             走, 共值会让运行中报告跨 run 灌进同一上游 session, 上下文
             污染 + 跨 run 误敲门 (生产数据 run f30ad9/e5c5c5 实锤)。
             调用方拿到该异常后应转换提示「请先新建对话再发起」。
        返回写入后的完整 row dict;若有冲突(双 ACTIVE 或 session 复用)
        则 raise IntegrityError,由 caller (T2 console) 决定是否转 409。
        """
        try:
            existing = self.get_active_team_work()
        except Exception:
            existing = None
        if existing:
            raise sqlite3.IntegrityError(
                f"已有 ACTIVE team work: {existing['run_id']}")
        # Issue#8 守卫 3: 一 run 一 session 护栏 (应用层先 SELECT,
        # DB 层靠 idx_team_work_initiator 索引;DB 不再加 UNIQUE,因为
        # 历史数据中允许同一个 session 跨多 run 我们的回滚更小,只拦
        # 新发起, 老行不动)。允许多 session 入同一 run 反向则不行。
        prior = self.find_runs_by_initiator_session(session_id)
        if prior:
            # issue#12 R2 (2026-10-13): 改用专属异常 SessionAlreadyBoundError,
            # 让 caller 用 isinstance 判别。旧字符串 "已绑到 run" 嗅探已弃用
            # —— ledger 改文案即静默退化为通用 409,前端 session_reused
            # 自动重拉链路失效 (issue 实锤)。仍继承 sqlite3.IntegrityError,
            # 旧 except IntegrityError 兜底保持兼容。
            raise SessionAlreadyBoundError(
                session_id,
                [r["run_id"] for r in prior])
        now = time.time()
        self.conn.execute(
            "INSERT INTO team_work_runs (run_id, initiator_session, "
            "initial_task, project_workspace, status, created_at) "
            "VALUES (?,?,?,?,'ACTIVE',?)",
            (run_id, session_id, initial_task, project_workspace, now))
        self.conn.commit()
        return self.get_team_work(run_id)

    def register_team_work_card(self, run_id, task_id,
                                plan_seq: Optional[float] = None) -> None:
        """把 task_id 登记到某 run 名下。

        副作用 (T1 + T3 补完):
          - task_initiators: INSERT (task_id, run.initiator_session) ← T3
            (让 get_initiator(task_id) 能反查回 main session, 否则 stuck/relay
            消息发不出去, 等价于没人会叫醒 main)
          - team_work_runs.card_count += 1
          - harvest_contracts.team_work_run_id 同步回写 (若已建 contract)
        launch 端点为每个 step 调一次;调用失败时 raise (让 console 报错)。

        参数 plan_seq (2026-10-17 issue#11 s2-fix-11):
          可选 float, 表示卡在排队预览中的『计划位』。语义:
            - None / 不传 → 不写 plan_seq 列 (NULL), 排序时与 seq 等价;
              兼容 launch (seq=1..N 即完整计划序, 不需要 plan_seq)。
            - 显式 float → 写 plan_seq; 排队预览用 COALESCE(plan_seq, seq)
              排, 故 add_cards 可『插到中间』而不依赖 seq 整数限制。
            - seq 不变 (Issue#2 不变式), plan_seq 是独立的展示层键。
            - 写 NULL 而非具体值时, 仍以 COALESCE 落 seq; 永远不让
              plan_seq 干扰 seq 的不可变性。
        """
        run = self.get_team_work(run_id)
        if not run:
            raise ValueError(f"run {run_id} 不存在")
        import time as _t
        self.conn.execute(
            "INSERT OR IGNORE INTO task_initiators(task_id, session_id, created_at) "
            "VALUES(?, ?, ?)",
            (task_id, run["initiator_session"], _t.time()))
        self.conn.execute(
            "UPDATE team_work_runs SET card_count=card_count+1 "
            "WHERE run_id=?", (run_id,))
        # 同步回写 harvest_contracts.team_work_run_id (兜底 race fix):
        # console.py 在 launch 时先 put_contract 再 register;若 put_contract
        # 因 SQLite lock/异常被 except: pass 吞掉, 本步 UPDATE 命中 0 行,
        # 卡留下 NULL team_work_run_id → reaper 按 SKILL §Core invariants 永远
        # SKIP 孤儿 → 30min 后 stuck-notify 才兜底叫醒 main。
        # 先 INSERT OR IGNORE 一个 PENDING 占位行 (对已存在行 no-op,
        # 不覆盖 put_contract 写入的 contract_json / pass_count),
        # 再 UPDATE 回填 team_work_run_id。
        now_ts = _t.time()
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO harvest_contracts "
                "(task_id, contract_json, harvest_status, exit_code, "
                "harvest_log, pass_count, required_passes, "
                "created_at, updated_at) "
                "VALUES (?, '{\"required_files\":[]}', 'PENDING', "
                "NULL, NULL, 0, NULL, ?, ?)",
                (task_id, now_ts, now_ts))
            # 2026-10-09 Issue#2: seq 不可变注册序列。重复登记 (INSERT OR
            # REPLACE 整行替换会把 seq 抹成 NULL) 与重复 register 都不得
            # 改变既有 seq —— COALESCE 保留; 新登记按 run 分区 MAX(seq)+1
            # 取号: launch 按 steps 顺序逐张 register → 1,2,3...;
            # add_cards 追加时 MAX 已含既有卡 → 新卡排尾部 (AC2-4)。
            # 2026-10-17 issue#11 s2-fix-11: 加 plan_seq 计划位 (REAL)。
            #   - launch (plan_seq=None) → plan_seq 列留 NULL, 排序键仍
            #     走 seq; 老行为零回归。
            #   - add_cards(position=N) → plan_seq=N, 排队预览按 plan_seq
            #     排; seq 仍 MAX+1 不变 (Issue#2 不变式)。
            #   - 重复 register 不覆盖既有 plan_seq (COALESCE), 与 seq 同口径。
            self.conn.execute(
                "UPDATE harvest_contracts SET team_work_run_id=?, "
                "  seq=COALESCE(seq, ("
                "    SELECT COALESCE(MAX(seq), 0) + 1 FROM harvest_contracts"
                "    WHERE team_work_run_id=? AND task_id != ?)), "
                "  plan_seq=COALESCE(plan_seq, ?), "
                "  updated_at=? WHERE task_id=?",
                (run_id, run_id, task_id, plan_seq, now_ts, task_id))
        except sqlite3.Error:
            pass
        self.conn.commit()

    def get_active_team_work(self) -> Optional[dict]:
        """取当前唯一一条 ACTIVE run;若没有则返回 None。

        关键: partial UNIQUE INDEX 保证最多一条 ACTIVE,所以 LIMIT 1 只是
        防御性写法,语义上等价于「第一条」=「唯一一条」。
        返回 dict(row) 或 None。
        """
        r = self.conn.execute(
            "SELECT * FROM team_work_runs WHERE status='ACTIVE' LIMIT 1"
        ).fetchone()
        return dict(r) if r else None

    def list_pending_orphan_cards(self, limit: int = 50) -> list[dict]:
        """列出「未结束 + 无 run 归属」的卡 (即孤儿 PENDING)。

        定义: harvest_status='PENDING' AND team_work_run_id IS NULL
        (运行中任务机 harvest 已经把它从 PENDING 切走,只会留 PENDING 是
        真没结算;legacy 数据未迁到顶层对象的也会带 NULL run_id)。

        用途: api_team_work_launch 启动前的护栏 — 若 ledger 里还有
        这种孤儿,意味着前一轮 launch 半途崩了或人工遗留,直接拦
        住新 run 避免两张 run 同时派同一个 task。limit 默认 50,
        launch 时若真实数 > 50 会带 in_more=True 提示。
        """
        rows = self.conn.execute(
            "SELECT task_id, harvest_status, updated_at, contract_json "
            "FROM harvest_contracts "
            "WHERE harvest_status='PENDING' AND team_work_run_id IS NULL "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_team_work(self, run_id) -> Optional[dict]:
        """按 run_id 取一条 run 记录(任意状态)。找不到返回 None。"""
        r = self.conn.execute(
            "SELECT * FROM team_work_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return dict(r) if r else None

    def set_team_work_current_card(self, run_id, task_id) -> None:
        """把 current_card_id 从 NULL 推进到 task_id(advance 端点调用)。

        硬守卫由 caller (T2 advance 端点) 保证:调用前必须 verify
        current_card_id 为 NULL,否则视为越权派活。Ledger 层只做赋值,
        不再二次检查 —— 否则会和 reaper 的 release lock 等场景冲突。

        F4b 不变式 (2026-10-09): 本方法**不得**重置
        current_card_cleared_at。它记录「上次清槽」事件时刻,
        reaper 用它度量 NULL 槽闲置; advance 重新占槽不影响该记录。
        """
        self.conn.execute(
            "UPDATE team_work_runs SET current_card_id=? WHERE run_id=?",
            (task_id, run_id))
        self.conn.commit()

    def clear_team_work_current_card(self, run_id) -> None:
        """结算完成后清 NULL。harvest 落任何终态 (COMPLETE / NEED_CLARIFICATION /
        FAILED) 时由 harvest 端调,清空后下一张卡才能 advance。
        边界减法 (2026-10-08): 不再有 PASS_BUMPED「不清槽继续跑」——
        worker 交卷即交球 main, loop 与否由 main 编排。
        调用方应保证 run 仍 ACTIVE;若已 CLOSED,本方法相当于 no-op。

        F4b (2026-10-09): 清槽即「NULL 槽开始计时」, 同语句写
        current_card_cleared_at = now, 作为 reaper 兜底敲门的闲置基准。

        F5-2 (2026-10-13): 同时写 current_card_handoff_at = now, 让
        derive_worker_state() 知道旧卡刚被 handoff; 与 cleared_at 同步
        触发但语义解耦 — 前者用于「run 切换中」窗口, 后者用于「槽
        清零多久」。两者由 harvest 与 settle 路径共存维护。
        """
        self.conn.execute(
            "UPDATE team_work_runs SET current_card_id=NULL, "
            "current_card_cleared_at=?, current_card_handoff_at=? "
            "WHERE run_id=?",
            (time.time(), time.time(), run_id))
        self.conn.commit()

    # ---- F5 (2026-10-13, issue #5): 派生状态单源 ----
    # 三处 UI (TasksBoardView.jsx 步骤列表 / WorkersView.jsx worker 面板 /
    # ChatView.jsx run 头部) 与 pixel_bridge 必须消费同一份派生结果。
    # 此函数是派生状态的唯一权威; 所有展示层不再自行分类。
    #
    # 派生规则 (run + card 视角):
    #   1. 任务存在性: ledger.harvest_contracts.harvest_status 是真权威
    #      (harvest 落账字段, 由 actor=harvest/advance/operator/main_correct
    #       写入, 任何路径都不能直接改它)。 当 plan B (kanban) 与 ledger 不
    #      一致时, 派生端优先 ledger (matches 当前 _serialize_todo_doc / UI
    #      show 的兜底逻辑)。
    #   2. 真终态 (HARVESTED / ARCHIVED / CANCELLED): 不被 run 状态覆盖,
    #      直接返 harvest_status。
    #   3. task_id 与 run.current_card_id 一致: 视为「正在承接」即 WORKING。
    #      包含「PENDING + 被占槽」与「HANDOFF」两类。
    #   4. run ACTIVE 但 current_card_id NULL:
    #         a. 派生端接收到的 task_id = 当前卡的最近一张 (以 ledger
    #            updated_at 与 run.current_card_handoff_at 兜底) — 把这张
    #            卡判 HANDOFF (run 切换间隙)。
    #         b. 非最近一张仍为 PENDING 但 run 整体在切换 — 不参与派生,
    #            派生端只对 caller 给的 task_id 返回「视角内最有意义」的态,
    #            实际就是 PENDING 当 run ACTIVE + 没有 handoff 锚点。
    #   5. run 非 ACTIVE: 派生端若 task_id 已真终态仍返真终态; 否则
    #      把 PENDING 当成「队尾闲置」 (UI 应显示「队列已冻结」)。
    #
    # 返回结构: (state, sources) — state 是单个字符串 (PENDING / WORKING /
    # HANDOFF / WAITING / BLOCKED / NEED_CLARIFICATION / HARVESTED /
    # ARCHIVED / CANCELLED / ERROR), sources 是字典, 含原始字段便于审计。
    def derive_worker_state(self, task_id: str, *, run_id: str = "") -> tuple[str, dict]:
        """单一权威派生 (issue #5, 2026-10-12 评论口径)。

        输入:
          task_id: 必填, 要派生哪张卡的状态
          run_id:  可选, 已知 run_id 时优先用; 否则从 harvest_contracts
                  反查 (logger 状态总能反查, 但显式传 run_id 性能更好)

        优先级 (从高到低):
          真终态 (HARVESTED/ARCHIVED/CANCELLED) → 一律保留
          WAITING/NEED_CLARIFICATION/BLOCKED/ERROR → worker 侧信号, 保留
          task_id == run.current_card_id → WORKING (承接中)
          task_id == 最近 handoff 卡 (current_card_handoff_at 在 run 上时
              且当前 task 是 ledger 上 updated_at 最近那张) → HANDOFF
          其他 → PENDING (排队中)
          run 非 ACTIVE + 任务非真终态 → CLOSED_RUN (UI 显示「冻结」)
        """
        sources: dict = {}
        if not task_id:
            return ("PENDING", {"reason": "no_task_id"})
        row = self.conn.execute(
            "SELECT team_work_run_id, harvest_status, subagent_wait_since "
            "FROM harvest_contracts "
            "WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            # 无契约 = 该 task 未登记(理论上不应该, launch/advance 都建契约)。
            # 兜底判 PENDING 不报错, UI 把它当排队中处理。
            return ("PENDING", {"reason": "no_contract"})
        if run_id and row["team_work_run_id"] != run_id:
            sources["note"] = "task 不属于 run"
        eff_run_id = run_id or row["team_work_run_id"] or ""
        hs = row["harvest_status"]
        sources["harvest_status"] = hs
        sources["run_id"] = eff_run_id
        # 1. 真终态优先, 不被 run 状态覆盖
        if hs in ("HARVESTED", "ARCHIVED", "CANCELLED"):
            sources["tier"] = "terminal"
            return (hs, sources)
        # 2. WAITING 是 reaper 主动判定的「等 main 干预」, 不被 run 覆盖
        #    (这是 F5 的关键: 旧版的「全员排队」失真形态就是因为把 WAITING
        #    折回了 PENDING)
        if hs == "WAITING":
            sources["tier"] = "waiting"
            return ("WAITING", sources)
        # 2.5 issue#13 (2026-10-15): worker 声明等待子任务 (TODO.md
        #    status=waiting_sub + reaper 已盖戳 subagent_wait_since).
        #    派生态层 token WAITING_SUB; 优先级低于 WAITING (超阈后
        #    reaper 会打 WAITING actor=reaper, 「等介入」盖过「在等
        #    子任务」, AC-A7 单一派生态真理源). 真终态已在 step 1 短路.
        #    仅当 run ACTIVE 且 cur_card==task_id 时才派生; 排队卡/
        #    run 关闭后残留卡/handoff 卡一律落前序步骤 PENDING
        #    (architecture.md §4.3 硬约束, s5b 修复 C1).
        if hs in ("PENDING", "WORKING"):
            sub_sw = row["subagent_wait_since"] \
                if "subagent_wait_since" in row.keys() else None
            if sub_sw and eff_run_id:
                run_row_ws = self.conn.execute(
                    "SELECT status, current_card_id "
                    "FROM team_work_runs WHERE run_id=?",
                    (eff_run_id,)).fetchone()
                if run_row_ws is not None \
                        and run_row_ws["status"] == "ACTIVE" \
                        and run_row_ws["current_card_id"] == task_id:
                    sources["tier"] = "subagent_wait"
                    return ("WAITING_SUB", sources)
        # 3. NEED_CLARIFICATION / BLOCKED / ERROR 也是 worker/reaper 给出
        #    的明确信号, 同样不被 run 状态覆盖
        if hs in ("NEED_CLARIFICATION", "BLOCKED", "ERROR"):
            sources["tier"] = "obstacle"
            return (hs, sources)
        # 4. 取 run 上下文判断「承接 / 排队 / handoff」
        if not eff_run_id:
            sources["tier"] = "pending_no_run"
            return ("PENDING", sources)
        run_row = self.conn.execute(
            "SELECT status, current_card_id, current_card_handoff_at "
            "FROM team_work_runs WHERE run_id=?",
            (eff_run_id,)).fetchone()
        if run_row is None:
            sources["tier"] = "pending_no_run_row"
            return ("PENDING", sources)
        run_status = run_row["status"]
        cur_card = run_row["current_card_id"]
        handoff_at = run_row["current_card_handoff_at"]
        sources["run_status"] = run_status
        sources["run_current_card_id"] = cur_card
        sources["run_handoff_at"] = handoff_at
        # 5. run 非 ACTIVE 时, 已经走到这一步的卡 (non-terminal + non-blocked)
        #    实际是「run 关闭后残留」; UI 显示「队列冻结」专用态。
        if run_status != "ACTIVE":
            sources["tier"] = "frozen_run"
            return ("PENDING", sources)
        # 6. 当前承接 = run.current_card_id 一致 → 承接中
        if cur_card == task_id:
            sources["tier"] = "working_holder"
            return ("WORKING", sources)
        # 7. handoff 窗口: run 上有 current_card_handoff_at (非空) + 当前
        #    card_id NULL → 旧卡刚交卷处于 handoff。 但 caller 给的
        #    task_id 必须就是「最近一张」才能判 handoff; 否则返 PENDING。
        if cur_card is None and handoff_at:
            # 找 run 上「最近被结算」的 task (即 ledger updated_at 最近的
            # 行 + 与 run 关联)。 这就是「刚交卷」的卡, 与 caller 期望的
            # task_id 比对, 一致 → HANDOFF; 不一致 → PENDING 等待 advance。
            latest = self.conn.execute(
                "SELECT task_id, harvest_status, updated_at "
                "FROM harvest_contracts "
                "WHERE team_work_run_id=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (eff_run_id,)).fetchone()
            if latest and latest["task_id"] == task_id:
                # 「最近一张」可以是任何 harvest_status — 真终态已被上面
                # 步骤 1 截走, 这里必然是「刚落账但非真终态」(只会是
                # ERROR / HARVESTED 等被 schema still 阻挡; 实务上
                # BLOCKED/NEED_CLARIFICATION 已不返了), 但理论上 verify
                # 不为真终态即可。 但实际:harvest 写终态后再 _teardown
                # 清槽, 终态在前一步就被判定返; 这个分支能进入时
                # 通常 harvest 路径变了 / 边缘 race, 兜底返 HANDOFF。
                if latest["harvest_status"] not in (
                        "HARVESTED", "ARCHIVED", "CANCELLED"):
                    sources["tier"] = "handoff"
                    sources["handoff_task_id"] = task_id
                    return ("HANDOFF", sources)
        sources["tier"] = "pending_queue"
        return ("PENDING", sources)

    def derive_run_overview(self, run_id: str) -> dict:
        """run 级派生: 当前承接者 / handoff / 等待态。

        返回字典含:
          state:           'ACTIVE'/'HANDOFF'/'WAITING_ACTIVE'/'IDLE'/'CLOSED'
          holder_state:    当前承接卡的 derive_worker_state (无则是 None)
          holder_task_id:  run.current_card_id (无则 None)
          waiting_active:  bool, run 名下有任一卡处于 WAITING
          handoff_active:  bool, run 名下 last_cleared 后还没 advance
          has_active_run:  bool, run.status=='ACTIVE'
        """
        if not run_id:
            return {"state": "IDLE", "holder_task_id": None,
                    "holder_state": None, "has_active_run": False,
                    "waiting_active": False, "handoff_active": False}
        r = self.conn.execute(
            "SELECT status, current_card_id, current_card_cleared_at, "
            "current_card_handoff_at FROM team_work_runs "
            "WHERE run_id=?", (run_id,)).fetchone()
        if r is None:
            return {"state": "IDLE", "holder_task_id": None,
                    "holder_state": None, "has_active_run": False,
                    "waiting_active": False, "handoff_active": False}
        run_status = r["status"]
        cur_card = r["current_card_id"]
        cleared_at = r["current_card_cleared_at"]
        waiting_active = bool(self.conn.execute(
            "SELECT 1 FROM harvest_contracts "
            "WHERE team_work_run_id=? AND harvest_status='WAITING' LIMIT 1",
            (run_id,)).fetchone())
        handoff_active = bool(cleared_at and cur_card is None)
        if run_status != "ACTIVE":
            state = "CLOSED"
            holder_state = None
        elif cur_card:
            holder_state_val, _ = self.derive_worker_state(
                cur_card, run_id=run_id)
            holder_state = holder_state_val
            if holder_state_val == "WORKING":
                state = "ACTIVE"
            else:
                # WAITING / NEED_CLARIFICATION / BLOCKED / ERROR /
                # HANDOFF (漏判兜底): run 仍有承接, 但承接者卡本身
                # 处于非正常态。 这给 UI「承接者受阻」专用视觉。
                state = "ACTIVE_BLOCKED"
        elif handoff_active:
            state = "HANDOFF"
            holder_state = None
        elif waiting_active:
            state = "WAITING_ACTIVE"
            holder_state = None
        else:
            state = "IDLE"
            holder_state = None
        return {"state": state, "run_status": run_status,
                "holder_task_id": cur_card, "holder_state": holder_state,
                "has_active_run": run_status == "ACTIVE",
                "waiting_active": waiting_active,
                "handoff_active": handoff_active}

    def settle_team_work_residue(self, run_id, *, exclude_task_id: str = "") -> list:
        """advance 串行铁律 (2026-10-08 修订): 只覆盖 reaper 兜底写的
        事实孤儿(BLOCKED / NEED_CLARIFICATION),不动 PENDING。

        旧版把三态一锅端标 HARVESTED,后果是 harvest 落账第 N 张时
        `count(PENDING) == 0` 提前成立 → 触发 auto-close → run 提前关闭
        → main 想 advance N+1 时 409「非 ACTIVE」,整条链靠人工救场。
        实证 run b9a9445419ab489e (2026-10-07/08 实测): 被 auto-close 强
        行关闭 4 次,每次都需要 kill reaper + UPDATE 恢复 run ACTIVE。

        新语义:
          - PENDING = 还在排队等 main advance,不动。main 没推就不算事实。
          - BLOCKED = reaper 兜底临时标记,事实未成立,advance 推下一张
            即隐式判定「旧卡不算 BLOCKED,算未结算 → 已结算」(主推进)
            必须覆盖,否则后续 harvest 看 ledger 仍以为旧卡没完。
          - NEED_CLARIFICATION = main 未答的澄清,advance 不应能跳过
            (main 应该回答后再 advance)。但旧版已经覆盖了它,保持原行为
            以避免双重语义 — 标记为「待 main 事后审计是否漏答」。
        """
        # M4 收编 (BATCH1-DESIGN): 裸 UPDATE 改走统一状态更新器,
        # 每条迁移带 actor=advance 的 worker.state 事件 + progress bump。
        # 日志保留 [main_advance] 前缀 (既有测试断言依赖)。
        targets = self.conn.execute(
            "SELECT task_id FROM harvest_contracts "
            "WHERE team_work_run_id=? "
            "  AND task_id != ? "
            "  AND harvest_status IN ('BLOCKED','NEED_CLARIFICATION')",
            (run_id, exclude_task_id)).fetchall()
        settled = []
        for t in targets:
            tid = t["task_id"]
            self.update_worker_state(
                tid, "HARVESTED", actor="advance",
                reason="[main_advance] implicit completion")
            settled.append(tid)
        return settled

    def pending_in_team_work(self, run_id) -> list:
        """取 run 名下所有未结算卡(PENDING / ERROR / BLOCKED 三种状态)。

        reaper 用本方法取代旧的 ledger.pending(),避免扫到「孤儿 PENDING」
        (legacy 旧数据,无 team_work_run_id);孤儿由 T3 的 reap_once 直接
        SKIP,绝不激活。
        返回 sqlite3.Row 列表(空 list 表示 run 名下没有未结算卡)。
        """
        return self.conn.execute(
            "SELECT hc.* FROM harvest_contracts hc "
            "JOIN team_work_runs r ON hc.team_work_run_id=r.run_id "
            "WHERE r.run_id=? AND hc.harvest_status IN "
            "('PENDING','ERROR','BLOCKED') ORDER BY hc.updated_at",
            (run_id,)).fetchall()

    def current_card_contract(self, run_id: str, task_id: str):
        """取「当前活动卡」的契约行 —— reaper 唯一的结算入口 (方案2)。

        设计依据 (2026-10-04 用户定调):
          - ledger 的 current_card_id 是「现在该看谁」的唯一权威;
            TODO.md 只回答「它完成了没」。
          - reaper 若扫 run 名下全部 pending 卡, 等于绕过 ledger 自行决定
            结算范围, 那 ledger 就没有存在意义。
          - 「一次只有一个活动 team work, 其中只有一个活动 task」是硬约束,
            所以结算面必须收敛到 current_card_id 这一张。非当前卡的
            TODO.md 一律不读, 从根上消除误结算。

        只在该卡属于本 run 且处于未结算态时返回行, 否则 None。
        """
        if not run_id or not task_id:
            return None
        return self.conn.execute(
            "SELECT * FROM harvest_contracts "
            "WHERE task_id=? AND team_work_run_id=? "
            "AND harvest_status IN ('PENDING','ERROR','BLOCKED')",
            (task_id, run_id)).fetchone()

    def close_team_work(self, run_id, reason: str,
                       closed_by: Optional[str] = None,
                       final_status: Optional[str] = None) -> Optional[str]:
        """关闭一条 ACTIVE run, 落终态 status / closed_at / closed_reason。

        final_status 语义:
          - 显式传入: 必须属于 {'CLOSED','ABORTED'}, 否则 raise ValueError。
          - None: 自动判定 —— 判定并入终 UPDATE 的 CASE 表达式 (#7-3):
            run 名下无未结算遗留卡 (harvest_status IN
            PENDING/ERROR/BLOCKED/NEED_CLARIFICATION 计数=0)
            且 current_card_id IS NULL → 'CLOSED'; 否则 → 'ABORTED'。
            判定与写入同一条语句原子完成, 消除「判定后、写入前」遗留
            数被并发改变导致终态与事实不符的 TOCTOU 窗口。
        原子副作用: status/closed_at/closed_reason 落库 +
        current_card_id 清 NULL (与 console close 端点旧裸 UPDATE 对齐,
        收编进 ledger; 清槽由本方法一步完成, 调用方不再拼 SQL)。
        只对 status='ACTIVE' 生效: UPDATE 带 status='ACTIVE' 条件,
        幂等保护下沉到语句级 —— 前置 SELECT 守卫只是快速路径与参数校验
        顺序, 真正裁决权在 UPDATE 的 rowcount (并发下他人先关则 0 行
        命中, 返回 None 不写)。返回实际写入的终态字符串 (ACTIVE→...
        才返回, 非 ACTIVE 返回 None 表示未变更)。

        调用时序警告: 自动判定读的是「当前」遗留数。若调用方在 close 之前
        先 archive 了卡 (console 端点就是此顺序), 遗留已被归档成 ARCHIVED,
        自动判定会误得 CLOSED —— 所以凡先归档再关闭的调用方, 必须显式传
        final_status; 自动判定只用于「未动过卡」的直接关闭场景。

        closed_reason 由 caller 拼接后传入, 常见格式:
            'complete_all' / 'user_close by <operator>' / 'first_dispatch_failed'
        """
        # 幂等保护: 非 ACTIVE 的 run 不动也不写, 返回 None。
        # (先于判定查询 —— 对已终态 run 跑判定 SELECT 无副作用, 但把
        #  守卫放最前面让意图与 A-5 用例语义一一对应)
        run_row = self.conn.execute(
            "SELECT status FROM team_work_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not run_row or run_row["status"] != "ACTIVE":
            return None
        if final_status is not None and final_status not in ("CLOSED", "ABORTED"):
            raise ValueError(
                f"final_status 必须为 'CLOSED' 或 'ABORTED', 收到 {final_status!r}")
        now = time.time()
        if final_status is not None:
            # 显式终态: 直接 UPDATE, status='ACTIVE' 条件兜并发幂等。
            cur = self.conn.execute(
                "UPDATE team_work_runs SET status=?, closed_at=?, "
                "closed_reason=?, current_card_id=NULL "
                "WHERE run_id=? AND status='ACTIVE'",
                (final_status, now, reason, run_id))
        else:
            # 自动判定 (#7-3): 判定并入 UPDATE 的 CASE —— 遗留数与
            # current_card_id 在写锁内读, 与写入原子, 无 TOCTOU 窗口。
            # 表达式与旧判定 SELECT 逐字同口径 (AC3-1/2)。
            final_status = (
                "CASE WHEN (SELECT current_card_id FROM team_work_runs "
                "          WHERE run_id=?) IS NULL "
                "       AND (SELECT COUNT(*) FROM harvest_contracts "
                "            WHERE team_work_run_id=? AND harvest_status IN "
                "            ('PENDING','ERROR','BLOCKED','NEED_CLARIFICATION'))"
                " = 0 THEN 'CLOSED' ELSE 'ABORTED' END")
            cur = self.conn.execute(
                f"UPDATE team_work_runs SET status={final_status}, "
                "closed_at=?, closed_reason=?, current_card_id=NULL "
                "WHERE run_id=? AND status='ACTIVE'",
                (run_id, run_id, now, reason, run_id))
        self.conn.commit()
        # rowcount 裁决: 0 行 = 并发下已被他人关闭 (非 ACTIVE), 未变更。
        if not cur.rowcount:
            return None
        if final_status in ("CLOSED", "ABORTED"):
            return final_status
        # 自动判定分支: 返回值须是「实际写入的终态」而非 CASE 表达式串,
        # 提交后回读一次 (报告用途, 非判定依据 —— 判定已在 UPDATE 内完成)。
        return self.conn.execute(
            "SELECT status FROM team_work_runs WHERE run_id=?",
            (run_id,)).fetchone()["status"]

    def archive_team_work_cards(self, run_id) -> list:
        """close 时调用:把 run 名下所有未结算卡标成 ARCHIVED,返回 task_id 列表。

        范围:PENDING / ERROR / BLOCKED / NEED_CLARIFICATION 四种「未终态」;
        已 COMPLETE / CANCELLED / ARCHIVED 的不动。
        返回被归档的 task_id 列表(空 list 表示无未结算遗留)。
        注:这里也包含 NEED_CLARIFICATION —— 这种卡理论上有 worker 正在等
        人类回复,但 run 一旦 close,等也白等,统一归档避免链路悬空。
        """
        # M4 收编 (BATCH1-DESIGN): 裸 UPDATE 改走统一状态更新器,
        # 每条迁移带 actor=operator 的 worker.state 事件 + progress bump;
        # 返回语义不变 (被归档的 task_id 列表)。
        targets = self.conn.execute(
            "SELECT task_id FROM harvest_contracts "
            "WHERE team_work_run_id=? AND harvest_status IN "
            "('PENDING','ERROR','BLOCKED','NEED_CLARIFICATION')",
            (run_id,)).fetchall()
        archived = []
        for t in targets:
            tid = t["task_id"]
            self.update_worker_state(tid, "ARCHIVED", actor="operator",
                                    reason="run close/archive 兜底归档")
            archived.append(tid)
        return archived

    def list_team_works(self, include_closed=False, include_archived=False) -> list:
        """UI 用:列出 team work。默认只 ACTIVE。

        include_closed=True  → 含 CLOSED/ABORTED(历史面板)
        include_archived=True → 还含 ARCHIVED(归档管理面板)
        2026-10-06 新增: ARCHIVED 状态(操作员手动归档, vs ABORTED 异常关闭,
        CLOSED 自然结束)。默认全部排除,避免列表过长。
        返回 list[dict],按 created_at DESC(最新在最前)。
        """
        # 显式列状态白名单,避免 WHERE 子句与 ARCHIVED 状态耦合搞错顺序
        if include_archived:
            statuses = ("ACTIVE", "CLOSED", "ABORTED", "ARCHIVED")
        elif include_closed:
            statuses = ("ACTIVE", "CLOSED", "ABORTED")
        else:
            statuses = ("ACTIVE",)
        placeholders = ",".join("?" * len(statuses))
        sql = (f"SELECT * FROM team_work_runs WHERE status IN ({placeholders}) "
               "ORDER BY created_at DESC")
        return [dict(r) for r in self.conn.execute(
            sql, tuple(statuses)).fetchall()]

    def find_runs_by_initiator_session(self, session_id: str) -> list[dict]:
        """按发起 session 反查「这个 session_id 是否已绑过 run」(任意状态)。

        用途 (issue #8, 2026-10-13): launch 守卫 — 新建 run 的 session_id 若
        已作为任何历史 run 的 initiator_session, 必须拒 409 (AC8-1)。一
        session 已绑一 run, 一 run 一 session 是不变式 —— 报告中继路由表
        (`get_initiator(task_id) → session_id`) 和运行中报告/敲门都按
        initiator 路由, 共享 initiator 会让两个 run 的实时消息互相灌进
        同一上游 session, 上下文污染 (生产 run f30ad9/e5c5c5 实锤:
        initiator_session 同 = api_1791794157_e0092598)。

        返回 list[dict] (按 created_at DESC), 0 长度 = 此 session 没用过。
        含 ACTIVE / CLOSED / ABORTED / ARCHIVED —— 不限制状态 (即使 run
        已 CLOSED, 这个 session 仍算「绑过」, 不允许它再发起新 run)。
        想专门查 ACTIVE 时调用方用 list_team_works() + 过滤。
        """
        cur = self.conn.execute(
            "SELECT * FROM team_work_runs WHERE initiator_session=? "
            "ORDER BY created_at DESC", (session_id,))
        return [dict(r) for r in cur.fetchall()]

    def archive_team_work(self, run_id) -> dict:
        """操作员归档一条非 ACTIVE run:ABORTED/CLOSED → ARCHIVED。

        不变量:
          - ACTIVE 拒绝(→ ValueError, console 转 409),避免误归档正在跑
            的链 (与 close 互补:close 终结 ACTIVE,archive 隐藏已结束的)。
          - 归档时调 archive_team_work_cards(run_id) 把旗下未终态卡标
            ARCHIVED, 与 close 走相同兜底 — 防止留 PENDING 卡在 ledger
            永久被 reaper SKIP。
          - 已 ARCHIVED 幂等:再调一次 ok=True, archived_cards=[]。
        返回 {ok, run_id, archived_cards:[task_id...], was_already}。
        """
        run = self.get_team_work(run_id)
        if not run:
            raise ValueError(f"team work {run_id} 不存在")
        if run["status"] == "ACTIVE":
            raise ValueError(
                f"team work {run_id} 状态 ACTIVE 不允许归档 — "
                "先关闭再归档")
        if run["status"] == "ARCHIVED":
            return {"ok": True, "run_id": run_id,
                    "archived_cards": [], "was_already": True}
        # 先归档旗下未终态卡,再改 run status; 一致性由 sqlite 单连接保证
        archived_cards = self.archive_team_work_cards(run_id)
        self.conn.execute(
            "UPDATE team_work_runs SET status='ARCHIVED', "
            "closed_reason=COALESCE(closed_reason, 'archived') "
            "WHERE run_id=? AND status IN ('ABORTED','CLOSED')",
            (run_id,))
        self.conn.commit()
        return {"ok": True, "run_id": run_id,
                "archived_cards": archived_cards,
                "was_already": False}

    # ---- task 关系(问题2b: reaper auto-unblock 下游) ----
    def list_children(self, task_id: str) -> list[str]:
        """返回 task_links 表中 task_id 的所有 child task_id 列表(直接子节点)。

        注意: task_links 是 kanban.db 表, 本方法假设 reaper 的 ledger 库与 kanban
        共用同一物理库(kanban.db) — 实际部署里 mw_home 和 kanban_home 可能分开,
        这种情况下方法返回空 list, harvest 端 fallback 到 recompute_ready 原生机制。
        """
        rows = self.conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id=? "
            "ORDER BY child_id", (task_id,)).fetchall()
        return [r["child_id"] for r in rows]
