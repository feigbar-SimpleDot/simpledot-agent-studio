"""mw.schedule — 收件箱 TAB (原任务排期) 的任务队列服务 (issue #164, ARCH-164 §1/§2)。

三层结构 (ARCH-164 §2.1), 依赖单向:
    HTTP 层 (console.py 路由 → api_schedule_* 函数, 返回 (code, body))
      调用
    引擎层 ScheduleEngine (单例, 持 _fire_lock + ticker daemon 线程)
      调用
    存储层 ScheduleStore (schedule.db 全部读写, 每次操作短连接)

红线 (R1/R2, ARCH-164 §2.1 禁区):
  * 不 import reaper/harvest/pipeline/todo;
  * 不调 /api/team-work/launch (那是 main 的职责, 排期只经 gateway 公开
    端点向 main 发首条对话消息 —— 与浏览器弹窗提交在 gateway 视角不可区分);
  * 不写 ledger.db/kanban.db 任何表 (对 ledger 只读查询)。

独立库: ${MW_HOME}/schedule.db (env MW_SCHEDULE_DB 可覆写, 与 LEDGER_DB 同风格),
与 run 账本物理分离 (issue 硬口径 "单独建库用于管理排期任务")。
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .i18n import t as _t

log = logging.getLogger("mw.schedule")

# ── 库路径 (与 console.py LEDGER_DB 同 env 风格) ──────────────────────
def _schedule_db_path() -> str:
    return os.environ.get(
        "MW_SCHEDULE_DB",
        os.path.join(os.environ.get("MW_HOME", "/mw_data"), "schedule.db"))


def _ledger_db_path() -> str:
    return os.environ.get(
        "MW_LEDGER_DB",
        os.path.join(os.environ.get("MW_HOME", "/mw_data"), "ledger.db"))


# id 白名单 (照抄 _CRON_JOB_ID_RE 纪律, ARCH-164 §2.3)
_SCHED_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 倒计时时长 (issue 硬口径: 5 分钟)
COUNTDOWN_S = 300
# 探活缓存 TTL (秒) 与探活超时 (ARCH-164 §2.2)
_PROBE_TTL_S = 3.0
_PROBE_TIMEOUT_S = 5.0
# chat/stream 单连接硬上限 (ARCH-164 §2.2 触发流程 3)
_STREAM_HARD_CAP_S = 3600
# issue #206 僵尸 running 兜底阈值: running 行 updated_at 距今超过
# 2×硬上限仍未收敛 ⇒ 告警日志 (仅告警; 是否自动回退 pending 列 OQ-R1
# 待 main 裁决, 本卡不自动回退)
_ZOMBIE_RUNNING_WARN_S = 2 * _STREAM_HARD_CAP_S

VALID_PRIORITIES = ("urgent", "high", "normal", "low")

# schema 版本 (issue #197): v2 = schedule_tasks 增 finished_at 列
# (完成时刻; 已完成组按它降序, AC-Q197-3/4)。存量库由 _ensure_schema_on
# 按版本行一次性迁移 (版本行 + PRAGMA table_info 双检, 幂等)。
# issue #210: v3 = schedule_tasks.status CHECK 增 'hold' (run hold 联动:
# run 置 HOLD 时名下 running 的 inbox 任务同步置 hold, 回队顶走 resume)。
SCHEMA_VERSION = 3

# schedule_tasks 建表 DDL 单独成常量: v3 表重建迁移要按同一份定义重建,
# 与 SCHEMA 单一真相源 (两处定义漂移 = 迁移后 schema 与新建库不一致)。
_SCHEDULE_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS schedule_tasks (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    detail       TEXT NOT NULL,
    priority     TEXT NOT NULL DEFAULT 'normal'
                 CHECK(priority IN ('urgent','high','normal','low')),
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','paused','running','done',
                                  'cancelled','hold')),
    added_at     REAL NOT NULL,
    executed_at  REAL,
    finished_at  REAL,
    added_by     TEXT NOT NULL DEFAULT '',
    workflow     TEXT NOT NULL DEFAULT 'auto',
    workspace    TEXT NOT NULL DEFAULT '',
    session_id   TEXT,
    run_id       TEXT,
    last_error   TEXT,
    updated_at   REAL NOT NULL
);
"""

SCHEMA = _SCHEDULE_TASKS_DDL + """
CREATE INDEX IF NOT EXISTS idx_sched_status_added ON schedule_tasks(status, added_at DESC);
CREATE TABLE IF NOT EXISTS schedule_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _ro_query(db: str, sql: str, args=()) -> list[dict]:
    """对 ledger.db 等外部库的只读查询 (mode=ro, 绝不写)。

    与 console._ro_query 同纪律 (独立实现避免 schedule ← console 循环 import);
    库不存在/查询失败一律返回空列表 (排期侧按"无 run"或"未知"降级)。
    """
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(sql, args)]
        except sqlite3.Error:
            return []
        finally:
            c.close()
    except sqlite3.Error:
        return []


def make_title(detail: str) -> str:
    """标题自动生成 (A2): 首条非空行 strip; >40 字符截 40 + '…'。"""
    for line in (detail or "").splitlines():
        s = line.strip()
        if s:
            return s if len(s) <= 40 else s[:40] + "…"
    return ""


# ════════════════════════════════════════════════════════════════════
# 存储层
# ════════════════════════════════════════════════════════════════════
class ScheduleStore:
    """schedule.db 全部读写。每次操作短连接 (与 console 各 api_* 的
    Ledger(...)…finally: led.close() 模式一致)。

    读写分离 (issue #183): 读路径**零写语句** —— 读连接不建表 (DDL)、
    不播种 (INSERT)、不 commit()、也不设 `PRAGMA journal_mode=WAL`
    (后者本身是一次写操作)。建库 (DDL + 种子) 收敛到 ensure_schema(),
    只在库尚未就绪时真正写一次; 并发首建库 (ticker 线程与 API 线程
    同进程) 用进程内锁 + 双重检查串行化。

    「首次建库仍自动建表+播种」不得回归: 无需显式初始化调用, 任何
    一次读写都会先确保 schema 就绪 (AC-183-2)。

    存量 v1 库的读路径 (F-1 回流修复, issue #197): 核心表在、但版本过旧
    (无 schema_version 行 / 无 finished_at 列) ⇒ **只读降级**, 读路径
    **不触发迁移** (迁移含 ALTER/UPDATE/INSERT/COMMIT + journal_mode=WAL,
    在只读库 chmod 0444 上直接抛 OperationalError)。降级口径: 排序键按
    列是否存在动态选 (有 finished_at 列走 COALESCE(finished_at, updated_at),
    无列走 updated_at), 语义仍是「终态组按完成/最后更新时刻降序」。
    迁移只在**写连接**上发生。
    """

    def __init__(self, path: str | None = None):
        self.path = str(path or _schedule_db_path())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # 建库互斥 (issue #183): 进程内锁 —— ticker 与 API 线程同进程,
        # 覆盖真实的并发首连场景; 不引入跨进程状态, 无需回滚。
        self._schema_lock = threading.Lock()

    # ── 连接纪律 (issue #183: 读写分离) ──────────────────────────────
    def _read_conn(self) -> sqlite3.Connection:
        """只读连接: 只发 SELECT 与只读 PRAGMA (busy_timeout)。

        绝不执行 executescript(DDL) / 播种 INSERT / commit() /
        PRAGMA journal_mode=WAL —— 读路径零写语句 (AC-183-1)。

        库尚未建好时 (全新路径) 走一次写路径懒建, 之后读路径恒零写。

        F-1 回流修复: 读路径**只在「库结构压根没有」时**懒建。
        - 全新库 (表都没有) ⇒ 建库 + 播种 (AC-183-2 不得回归);
        - 存量 v1 库 (表在, 版本过旧, 无 finished_at 列) ⇒ **只读降级**,
          **不迁移** —— 迁移含 ALTER/UPDATE/INSERT/COMMIT + journal_mode=WAL,
          在只读库 chmod 0444 上会直接抛 OperationalError。降级后的排序键
          由 list_all 按列是否存在动态选 (见 _finished_sort_expr)。

        ⚠ 判据是「表在不在」而不是「文件在不在」: 文件判据会把「0 字节
        空库文件」(从没建过库的痕迹, 如首次建库被中断 / 部署侧 touch)
        误判成存量库 ⇒ 读路径直接查一张不存在的表 (no such table) ⇒
        AC-183-2「首次建库」语义回归。表判据覆盖得更准: 有表 ⇒ 有数据
        要保护 ⇒ 绝不写; 无表 ⇒ 建库。
        """
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        if not self._schema_ready(c):
            if not self._tables_ready(c):
                # 全新 / 漂移库 (结构缺失): 建库 + 播种
                # (AC-183-2 不得回归 —— 无需显式初始化)
                c.close()
                self.ensure_schema()
                c = sqlite3.connect(self.path, timeout=30)
                c.row_factory = sqlite3.Row
                c.execute("PRAGMA busy_timeout=30000")
            # else: 存量库 (表在, 版本过旧) ⇒ 只读降级, 读路径零写
        return c

    def _write_conn(self) -> sqlite3.Connection:
        """写连接: 先确保 schema 就绪 (新库上 INSERT 才不会因「表不存在」
        失败, X-10), 再设 busy_timeout + WAL。

        schema 保证与写连接同一条连接: 常规路径只开 1 个连接。
        """
        with self._schema_lock:
            c = self._write_conn_raw()
            self._ensure_schema_on(c)
            return c

    def _write_conn_raw(self) -> sqlite3.Connection:
        """写连接 (裸): busy_timeout + WAL。

        WAL 模式在库首次创建时固化, 之后读连接读到的是已持久化的 WAL
        模式, 无需再设 —— 故读连接不执行该 pragma (它是写操作)。
        """
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _base_schema_ready(self, c: sqlite3.Connection) -> bool:
        """只读探针: 基础结构就绪 (表存在 + 种子行存在) —— 不看版本。

        与 _schema_ready 的差别: 存量 v1 库「基础就绪但版本过旧」⇒
        迁移路径 (ALTER), 而非重建路径 (executescript 对已存在的表是
        no-op, 不会补列 ⇒ 走错分支会漏掉 finished_at 列)。
        """
        try:
            row = c.execute("SELECT name FROM sqlite_master "
                            "WHERE type='table' AND name='schedule_tasks'"
                            ).fetchone()
            if row is None:
                return False
            seed = c.execute("SELECT value FROM schedule_settings "
                             "WHERE key='auto_execute_enabled'").fetchone()
            return seed is not None
        except sqlite3.Error:
            return False

    def _tables_ready(self, c: sqlite3.Connection) -> bool:
        """只读探针: 核心表是否已就位 (只读, 不看版本、不看种子行)。

        F-1 回流修复的懒建判据 —— 「表在不在」而非「文件在不在」:
        - 两个核心表都在 ⇒ 库是**建好的** ⇒ 读路径**绝不写**, 版本过旧
          也只读降级 (迁移留给写连接); 有表 ⇒ 有数据要保护;
        - 任一核心表缺失 ⇒ 结构不完整 (全新库 / 0 字节空文件 / 漂移)
          ⇒ 读路径允许懒建 (AC-183-2)。

        ⚠ 判据不能只看「文件在不在」: sqlite3.connect() 自己就会把库
        文件建出来 (0 字节), 而「0 字节空文件」(首次建库被中断 / 部署侧
        touch) 按文件判据会被误当成存量库 ⇒ 读路径查一张不存在的表 ⇒
        OperationalError: no such table ⇒ AC-183-2 回归。

        ⚠ 两个表都要查: 只查 schedule_tasks 的话, 「schedule_settings
        被删」的漂移库会走只读降级 ⇒ get_auto_execute() 直接抛
        no such table (基线会自愈)。查两个表 ⇒ 漂移库照旧自愈。

        只发 SELECT (sqlite_master) ⇒ 读路径零写。
        """
        try:
            names = {r["name"] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('schedule_tasks','schedule_settings')")}
        except sqlite3.Error:
            return False
        return {"schedule_tasks", "schedule_settings"} <= names

    def _schema_ready(self, c: sqlite3.Connection) -> bool:
        """只读探针: schema 表、种子行、版本行是否已就绪 (零写语句)。

        issue #197: 版本行 (schema_version) 纳入探针 —— 存量 v1 库缺
        finished_at 列, 读路径的 list_all 排序表达式若引用到不存在的列
        会报错, 故版本过旧 ⇒ 判未就绪。

        F-1 回流修复: 「未就绪」现在**不等于**「读路径要迁移」——
        _read_conn 只在「核心表压根不存在」时懒建, 存量库走只读降级
        (排序键按列是否存在动态选)。迁移仍然只在写连接上发生。
        """
        try:
            if not self._base_schema_ready(c):
                return False
            return self._schema_version(c) >= SCHEMA_VERSION
        except sqlite3.Error:
            return False

    def _ensure_schema_on(self, c: sqlite3.Connection) -> None:
        """在给定连接上幂等建库 + 播种 + 版本迁移 (仅在未就绪时真正写)。

        issue #197: 存量库迁移 (schedule_tasks 增 finished_at 列)。
        迁移只发生在**写连接**上 (读路径零写纪律 #183 不受影响):
          - 版本行 schedule_settings.schema_version 门控 (缺版本行 ⇒ 视为 v1);
          - PRAGMA table_info 双检 (列已在 ⇒ 跳过 ALTER, 幂等);
          - 存量 done/cancelled 行以 updated_at 回填 finished_at
            (最后一次可用近似; 文档口径注明)。

        三条路径:
          1. 全新库 (表都没有) ⇒ executescript 建表 + 播种;
          2. 存量库 (基础结构在, 版本过旧) ⇒ 迁移 (ALTER + 回填 + 版本行);
          3. 版本已达标 ⇒ 什么都不做。

        建表与迁移统一收口到 _migrate_schema_on —— 「建表后仍要过一遍
        迁移」才不漏: 存在 schedule_tasks 但缺 schedule_settings 的漂移库
        走 executescript 分支, 而 executescript 对已存在的表是 no-op
        (不会补列) ⇒ 若不统一过迁移, finished_at 列会缺失。
        """
        if not self._base_schema_ready(c):
            c.executescript(SCHEMA)
            # 种子: 自动执行默认开启 (A12, main 已裁决)
            c.execute("INSERT OR IGNORE INTO schedule_settings(key, value) "
                      "VALUES ('auto_execute_enabled', '1')")
            c.commit()
        self._migrate_schema_on(c)

    def _schema_version(self, c: sqlite3.Connection) -> int:
        """读 schema 版本号 (无版本行 ⇒ 0 = v1 存量库)。只读 SELECT。"""
        try:
            r = c.execute("SELECT value FROM schedule_settings "
                          "WHERE key='schema_version'").fetchone()
            if r is None:
                return 0
            return int(r["value"])
        except (sqlite3.Error, TypeError, ValueError):
            return 0

    def _has_finished_at_column(self, c: sqlite3.Connection) -> bool:
        """PRAGMA table_info 双检: schedule_tasks 是否已有 finished_at 列。"""
        try:
            rows = c.execute("PRAGMA table_info(schedule_tasks)").fetchall()
        except sqlite3.Error:
            return False
        return any(r["name"] == "finished_at" for r in rows)

    def _finished_sort_expr(self, c: sqlite3.Connection) -> str:
        """终态组排序键表达式 (F-1 回流修复: 存量 v1 库只读降级)。

        v2 库 (有 finished_at 列) ⇒ COALESCE(finished_at, updated_at)
        —— 完成时刻优先, 缺失时回退 updated_at (与迁移回填口径一致)。

        v1 存量库 (无 finished_at 列, 迁移未跑) ⇒ 只能取 updated_at
        (最后一次可用近似完成时刻)。**不能**引用不存在的列, 否则
        排序表达式直接报 OperationalError。

        ⚠ PRAGMA table_info 是只读探针, 不产生写语句 ⇒ 读路径仍零写。
        """
        if self._has_finished_at_column(c):
            return "COALESCE(finished_at, updated_at)"
        return "updated_at"

    def _migrate_schema_on(self, c: sqlite3.Connection) -> None:
        """按版本号执行一次性 schema 迁移 (幂等, 只在写连接上跑)。

        v1 → v2 (issue #197): schedule_tasks 增 finished_at REAL 列。
        v2 → v3 (issue #210): status CHECK 增 'hold' —— SQLite 的 ALTER
          TABLE 不能改 CHECK ⇒ 表重建 (新表→拷数→drop→rename, 显式事务
          包裹 + 行数校验, 照 ledger ARCHIVED/#210 迁移先例)。重建块排在
          finished_at ALTER 与回填 UPDATE **之前**: 回填 UPDATE 会开隐式
          事务, 与重建块的显式 BEGIN 冲突 (cannot start a transaction
          within a transaction); 重建在前还顺带让 v1 库的新表自带
          finished_at 列, 回填 UPDATE 照常补数, 语义不变。
        """
        if self._schema_version(c) >= SCHEMA_VERSION:
            return
        # ── v3 (#210): status CHECK 增 'hold' 表重建 (内容门控, 幂等) ──
        # 门控用建表 SQL 是否已含 'hold' 而非仅版本号: 版本行缺失/漂移时
        # 也能自愈, 重跑无害。
        _ddl = c.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                         "AND name='schedule_tasks'").fetchone()
        if _ddl and _ddl[0] and "'hold'" not in _ddl[0]:
            # 防御性 commit: 显式 BEGIN 前确保无隐式事务 (同 ledger #210
            # 迁移块的 backup 死锁教训; 此处当前调用点无未提交写, 双保险)
            c.commit()
            c.execute("BEGIN")
            try:
                n_before = c.execute(
                    "SELECT COUNT(*) FROM schedule_tasks").fetchone()[0]
                old_cols = [r["name"] for r in c.execute(
                    "PRAGMA table_info(schedule_tasks)").fetchall()]
                c.execute("ALTER TABLE schedule_tasks "
                          "RENAME TO schedule_tasks__old")
                c.execute(_SCHEDULE_TASKS_DDL)
                new_cols = [r["name"] for r in c.execute(
                    "PRAGMA table_info(schedule_tasks)").fetchall()]
                copy_cols = [col for col in new_cols if col in old_cols]
                col_sql = ", ".join(copy_cols)
                c.execute(f"INSERT INTO schedule_tasks ({col_sql}) "
                          f"SELECT {col_sql} FROM schedule_tasks__old")
                n_after = c.execute(
                    "SELECT COUNT(*) FROM schedule_tasks").fetchone()[0]
                if n_after != n_before:
                    raise RuntimeError(
                        f"#210 schedule 迁移行数校验失败: 迁移前 {n_before} "
                        f"行, 迁移后 {n_after} 行 — 回滚, 拒绝半迁移")
                c.execute("DROP TABLE schedule_tasks__old")
                c.execute("CREATE INDEX IF NOT EXISTS idx_sched_status_added "
                          "ON schedule_tasks(status, added_at DESC)")
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        # ── v2 (#197): finished_at 列 + 存量终态行回填 ──
        if not self._has_finished_at_column(c):
            c.execute("ALTER TABLE schedule_tasks ADD COLUMN finished_at REAL")
        # 存量终态行回填: updated_at 是最后一次可用近似完成时刻
        c.execute("UPDATE schedule_tasks SET finished_at=updated_at "
                  "WHERE status IN ('done','cancelled') AND finished_at IS NULL")
        c.execute("INSERT INTO schedule_settings(key, value) "
                  "VALUES ('schema_version', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (str(SCHEMA_VERSION),))
        c.commit()

    def ensure_schema(self) -> None:
        """幂等建库 + 播种 (issue #183)。

        - 只在库尚未就绪时真正写 (探针先判一次);
        - 并发首建库: 进程内锁 + 双重检查 ⇒ 只有一个连接跑 DDL + 播种,
          其余等待后直接返回 (不重复 DDL, 不重复播种);
        - 「首次建库仍自动建表+播种」不得回归 (AC-183-2)。
        """
        with self._schema_lock:
            c = self._write_conn_raw()
            try:
                self._ensure_schema_on(c)
            finally:
                c.close()

    # ── 查询 ──
    def get(self, task_id: str) -> dict | None:
        c = self._read_conn()
        try:
            r = c.execute("SELECT * FROM schedule_tasks WHERE id=?",
                          (task_id,)).fetchone()
            return dict(r) if r else None
        finally:
            c.close()

    def list_all(self) -> list[dict]:
        """按 (status 组序, 组内序) 服务端预排 (ARCH-164 §2.3; issue #197)。

        issue #197 取代 A8 裁决 (显示序=执行序=added_at DESC):
          组0 running   / 组1 pending / 组2 paused / 组3 done+cancelled
        组内序:
          - 组0/1/2: added_at **ASC** (创建最早在最上 = 先执行, AC-Q197-1/2);
          - 组3: 完成时刻 **DESC** (最近完成的在最上, AC-Q197-3/4)
            —— 完成时刻取 finished_at, 缺失时回退 updated_at
            (存量库迁移前 / 迁移未回填行的近似口径)。

        取负技巧: 单表达式统一按 ASC 排 —— 完成时刻取负后升序即降序。

        F-1 回流修复: 终态组排序键按列是否存在动态选 (存量 v1 库无
        finished_at 列 ⇒ 退回 updated_at), 排序表达式不引用不存在的列
        ⇒ 存量库上只读降级也能正常排序, 且读路径零写。
        """
        c = self._read_conn()
        try:
            finished_expr = self._finished_sort_expr(c)
            rows = c.execute(
                "SELECT * FROM schedule_tasks ORDER BY "
                "CASE status WHEN 'running' THEN 0 "
                "          WHEN 'pending' THEN 1 "
                "          WHEN 'paused' THEN 2 "
                "          WHEN 'hold' THEN 2 ELSE 3 END, "
                "CASE WHEN status IN ('done','cancelled') "
                f"     THEN -{finished_expr} "
                "     ELSE added_at END ASC").fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    def list_by_status(self, status: str) -> list[dict]:
        """按状态取列表, 组内 added_at ASC (#197: 与执行序同源)。"""
        c = self._read_conn()
        try:
            rows = c.execute(
                "SELECT * FROM schedule_tasks WHERE status=? "
                "ORDER BY added_at ASC", (status,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    def head_pending(self) -> dict | None:
        """队首待执行任务: 取序 = 显示序 = added_at **ASC** (#197 取代 A8
        的 added_at DESC 裁决 —— 创建最早者先执行)。"""
        c = self._read_conn()
        try:
            r = c.execute(
                "SELECT * FROM schedule_tasks WHERE status='pending' "
                "ORDER BY added_at ASC LIMIT 1").fetchone()
            return dict(r) if r else None
        finally:
            c.close()

    def has_pending(self) -> bool:
        c = self._read_conn()
        try:
            return c.execute("SELECT 1 FROM schedule_tasks "
                             "WHERE status='pending' LIMIT 1").fetchone() is not None
        finally:
            c.close()

    # ── 设置 ──
    def get_auto_execute(self) -> bool:
        c = self._read_conn()
        try:
            r = c.execute("SELECT value FROM schedule_settings "
                          "WHERE key='auto_execute_enabled'").fetchone()
            return bool(r) and r["value"] == "1"
        finally:
            c.close()

    def set_auto_execute(self, enabled: bool) -> None:
        c = self._write_conn()
        try:
            c.execute("INSERT INTO schedule_settings(key, value) "
                      "VALUES ('auto_execute_enabled', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      ("1" if enabled else "0",))
            c.commit()
        finally:
            c.close()

    # ── 创建 ──
    def create(self, detail: str, priority: str = "normal",
               workflow: str = "auto", workspace: str = "") -> dict:
        now = time.time()
        tid = uuid.uuid4().hex[:16]
        c = self._write_conn()
        try:
            c.execute(
                "INSERT INTO schedule_tasks(id, title, detail, priority, "
                "status, added_at, added_by, workflow, workspace, updated_at) "
                "VALUES(?,?,?,?, 'pending', ?, '', ?, ?, ?)",
                (tid, make_title(detail), detail, priority,
                 now, workflow or "auto", workspace or "", now))
            c.commit()
        finally:
            c.close()
        created = self.get(tid)
        return created if created is not None else {}

    # ── 状态机迁移 (全部条件 UPDATE, 非法迁移 0 行 ⇒ False ⇒ 服务层 409) ──
    def _cond_update(self, task_id: str, sets: str, where: str,
                     params: tuple) -> bool:
        c = self._write_conn()
        try:
            cur = c.execute(
                f"UPDATE schedule_tasks SET {sets}, updated_at=? "
                f"WHERE id=? AND {where}",
                params + (time.time(), task_id))
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()

    def pause(self, task_id: str) -> bool:      # pending→paused
        return self._cond_update(task_id, "status='paused'",
                                 "status='pending'", ())

    def resume(self, task_id: str) -> bool:     # paused→pending
        return self._cond_update(task_id, "status='pending'",
                                 "status='paused'", ())

    def cancel(self, task_id: str) -> bool:     # pending/paused→cancelled
        # 取消属终态 ⇒ 同步写 finished_at (AC-Q197-4: 终态行按完成时刻排序)
        return self._cond_update(task_id, "status='cancelled', finished_at=?",
                                 "status IN ('pending','paused')", (time.time(),))

    def delete_cancelled(self, task_id: str) -> bool:
        """仅取消态可删 (AC-3.5)。"""
        c = self._write_conn()
        try:
            cur = c.execute("DELETE FROM schedule_tasks "
                            "WHERE id=? AND status='cancelled'", (task_id,))
            c.commit()
            return cur.rowcount > 0
        finally:
            c.close()

    def update_paused(self, task_id: str, *, detail: str | None = None,
                      priority: str | None = None, workflow: str | None = None,
                      workspace: str | None = None) -> bool:
        """仅 paused 可改, 保存后保持 paused (A6/AC-3.7)。"""
        sets, params = [], []
        if detail is not None:
            sets.append("detail=?")
            params.append(detail)
            sets.append("title=?")
            params.append(make_title(detail))
        if priority is not None:
            sets.append("priority=?")
            params.append(priority)
        if workflow is not None:
            sets.append("workflow=?")
            params.append(workflow)
        if workspace is not None:
            sets.append("workspace=?")
            params.append(workspace)
        if not sets:
            return self.get(task_id) is not None
        return self._cond_update(task_id, ", ".join(sets),
                                 "status='paused'", tuple(params))

    # ── 引擎专用迁移 ──
    def take_head_pending(self) -> dict | None:
        """锁内取队首并条件 UPDATE pending→running (AC-10.2 双保险:
        0 行 ⇒ 放弃)。返回置为 running 后的任务或 None。

        取序 = added_at ASC (#197 取代 A8: 创建最早者先执行)。

        【DEAD CODE / 死代码】#197 起引擎不再走队首取序 (倒计时持有者
        模型: _fire_due / run_now 均改用 take_pending(holder_id)), 生产
        代码零调用点。保留给存储层直调用例与将来可能的批量取用; 勿在
        引擎路径重新引用 (会绕过持有者语义, 见 design.md §4.5)。"""
        c = self._write_conn()
        try:
            r = c.execute("SELECT * FROM schedule_tasks WHERE status='pending' "
                          "ORDER BY added_at ASC LIMIT 1").fetchone()
            if not r:
                return None
            now = time.time()
            cur = c.execute(
                "UPDATE schedule_tasks SET status='running', executed_at=?, "
                "last_error=NULL, updated_at=? WHERE id=? AND status='pending'",
                (now, now, r["id"]))
            c.commit()
            if cur.rowcount == 0:
                return None
            out = dict(r)
            out.update(status="running", executed_at=now, last_error=None)
            return out
        finally:
            c.close()

    def take_pending(self, task_id: str) -> dict | None:
        """锁内取**指定** pending 任务并条件 UPDATE pending→running。

        issue #197: 倒计时绑定持有者 ⇒ 归零触发/立即执行取的是「持有者」
        而不是「归零瞬间的队首」。双保险纪律与 take_head_pending 同构:
        0 行 (已被暂停/取消/他人取走) ⇒ 弃轮, 返回 None。

        与 take_head_pending 的差别: 不做 ORDER BY 取序, 按 id 精确取
        (持有者是内存态倒计时的绑定对象, 引擎已持有其 id)。
        """
        c = self._write_conn()
        try:
            r = c.execute("SELECT * FROM schedule_tasks WHERE id=? "
                          "AND status='pending'", (task_id,)).fetchone()
            if not r:
                return None
            now = time.time()
            cur = c.execute(
                "UPDATE schedule_tasks SET status='running', executed_at=?, "
                "last_error=NULL, updated_at=? WHERE id=? AND status='pending'",
                (now, now, task_id))
            c.commit()
            if cur.rowcount == 0:
                return None
            out = dict(r)
            out.update(status="running", executed_at=now, last_error=None)
            return out
        finally:
            c.close()

    def mark_running_failed(self, task_id: str, err: str) -> bool:
        """触发失败回退 running→pending + last_error (AC-10.1)。"""
        return self._cond_update(task_id, "status='pending', last_error=?",
                                 "status='running'", (err,))

    def mark_done(self, task_id: str) -> bool:  # running→done
        # 完成时刻写 finished_at (AC-Q197-4: 已完成组按**完成时刻**排序,
        # 而非创建时刻或开始执行时刻)
        return self._cond_update(task_id, "status='done', finished_at=?",
                                 "status='running'", (time.time(),))

    def backfill_session(self, task_id: str, session_id: str) -> bool:
        return self._cond_update(task_id, "session_id=?",
                                 "status='running'", (session_id,))

    def backfill_run(self, task_id: str, run_id: str) -> bool:
        return self._cond_update(task_id, "run_id=?",
                                 "status='running'", (run_id,))

    # ── issue #210: run hold / resume 联动 (console 端点调用) ──
    def hold_running_for_run(self, run_id: str) -> list[str]:
        """run 置 HOLD 联动: 该 run 名下 running 的 inbox 任务置 'hold'。

        关联键 = schedule_tasks.run_id (_observe_running 回填的 run↔排期
        映射)。hold ∉ running ⇒ 「唯一执行中」第一判据
        (list_by_status('running')) 天然不含它, 下一任务按常规流程起
        倒计时 (PLAN-U §1.2.4 / issue 正文「开始准备启动下一个任务」)。
        返回被置 hold 的 task_id 列表 (空 = 该 run 无关联 running 任务,
        例如 run 非 inbox 发起 / run_id 尚未回填)。
        """
        c = self._write_conn()
        try:
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM schedule_tasks "
                "WHERE run_id=? AND status='running'", (run_id,)).fetchall()]
            if ids:
                c.execute(
                    "UPDATE schedule_tasks SET status='hold', updated_at=? "
                    "WHERE run_id=? AND status='running'",
                    (time.time(), run_id))
                c.commit()
            return ids
        finally:
            c.close()

    def resume_held_for_run(self, run_id: str) -> list[str]:
        """run resume 联动: 'hold'→'pending' 且 added_at 置队顶。

        队顶实现 (PLAN-U §1.2.4): added_at = min(当前 pending.added_at) - 1
        —— 取序 added_at ASC ⇒ 回队任务恒排最前 (issue 正文「回到待执行
        队列顶部」)。无其他 pending 时以 now 为基准。返回被回队的
        task_id 列表。
        """
        c = self._write_conn()
        try:
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM schedule_tasks "
                "WHERE run_id=? AND status='hold'", (run_id,)).fetchall()]
            if not ids:
                return []
            head = c.execute("SELECT MIN(added_at) FROM schedule_tasks "
                             "WHERE status='pending'").fetchone()[0]
            new_added = (head - 1.0) if head is not None else time.time()
            c.execute(
                "UPDATE schedule_tasks SET status='pending', added_at=?, "
                "updated_at=? WHERE run_id=? AND status='hold'",
                (new_added, time.time(), run_id))
            c.commit()
            return ids
        finally:
            c.close()


# ════════════════════════════════════════════════════════════════════
# 触发消息组装 (ARCH-164 §2.4: 与 submitTeamWork firstMsg 逐字段等价;
# 文案迁入 mw/i18n schedule.dispatch_*, 值逐字照抄 ui locale 现值)
# ════════════════════════════════════════════════════════════════════
def build_dispatch_message(task: dict, sid: str) -> str:
    """组装首条消息, 与 ui/src/views/ChatView.jsx submitTeamWork 的
    firstMsg 结构逐行等价 (marker/需求/workflow 两分支/workspace 两分支/
    skill 指令/sid 绑定提示)。"""
    wf = task.get("workflow") or "auto"
    wf_hint = (_t("schedule.dispatch_field_workflow_explicit", wf=wf)
               if wf and wf != "auto"
               else _t("schedule.dispatch_field_workflow_auto"))
    ws = task.get("workspace") or ""
    ws_hint = (_t("schedule.dispatch_field_workspace_set", ws=ws)
               + _t("schedule.dispatch_field_workspace_set_launch", ws=ws)
               if ws else
               _t("schedule.dispatch_field_workspace_unset")
               + _t("schedule.dispatch_field_workspace_unset_suffix"))
    sid_hint = (_t("schedule.dispatch_sid_hint_prefix", sid=sid)
                + _t("schedule.dispatch_sid_hint_body"))
    return "\n".join([
        _t("schedule.dispatch_marker"),
        "",
        _t("schedule.dispatch_field_requirement", task=task["detail"]),
        f"{wf_hint}",
        f"{ws_hint}",
        "",
        _t("schedule.dispatch_instruction_skill"),
        _t("schedule.dispatch_instruction_no_repeat"),
        f"{sid_hint}",
    ])


# ════════════════════════════════════════════════════════════════════
# 引擎层
# ════════════════════════════════════════════════════════════════════
class ScheduleEngine:
    """ticker daemon 线程 + 触发互斥锁。依赖白名单 (ARCH-164 §2.1):
    只读 ledger (ACTIVE run 判定 / run 状态观察), 只经 HTTP 调 gateway
    公开端点 (与浏览器经 _proxy_to_gateway 完全同路径)。

    可测性: store / ledger_db / clock / countdown_s 均可注入;
    _gateway_free / _has_active_run / _create_session / _stream_chat /
    _spawn_fire 为测试 monkeypatch 点。
    """

    def __init__(self, store: ScheduleStore | None = None,
                 ledger_db: str | None = None,
                 clock=time.time, countdown_s: int = COUNTDOWN_S):
        self.store = store or ScheduleStore()
        self.ledger_db = ledger_db or _ledger_db_path()
        self.clock = clock
        self.countdown_s = countdown_s
        self._fire_lock = threading.Lock()   # 触发互斥 (AC-10.2)
        self._lock = threading.Lock()       # 倒计时内存态
        self.countdown_ends_at: float | None = None  # A10: 不落库, 重启重数
        # issue #197: 倒计时**绑定持有者** (内存态, 与 countdown_ends_at
        # 同生同灭, 同一把锁读写) —— 归零触发的是持有者而非归零瞬间的队首
        # (OQ-3/AC-Q197-10③)。至多一个倒计时 ⇒ 不变式 I2 天然成立
        # (单槽结构, AC-Q197-12)。
        self.countdown_task_id: str | None = None
        self._probe_cache: tuple[float, tuple[bool, str | None] | None] = (0.0, None)
        # issue #206: 僵尸 running 告警去重 (键 = (task_id, updated_at),
        # 同一行同一时刻只告警一次, 不刷屏)
        self._zombie_warned: set = set()
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()      # evaluate_now 提前唤醒 ticker

    # ── 对外接口 (ARCH-164 §2.1) ──
    def start(self) -> None:
        """起 ticker daemon 线程 (console main() 在 serve_forever 前调用)。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="mw-schedule-ticker")
        self._thread.start()

    def evaluate_now(self) -> None:
        """API 写后请求立即重评 (AC-5.5/AC-7.4): 唤醒 ticker, 不阻塞请求。"""
        self._wake.set()

    def restart_countdown(self) -> None:
        """开关 OFF→ON 时显式清零倒计时 (AC-4.3 / REVIEW-164 F-2)。

        为什么必须显式清: 倒计时只在 _tick() 里因 _condition_ok() 返回
        False (开关关) 才被撤销 —— 若 OFF→ON 发生在同一个 ticker 间隔
        (≤1s) 内, ticker 从未观察到"关"态 ⇒ 旧倒计时原样保留并继续走完
        剩余时间 (AC-4.3 要求"重新开始完整 5 分钟, 不续用剩余")。

        清零后由下一轮 _tick() 重新起完整 countdown_s 秒。

        issue #197: 持有者与结束时刻同生同灭 ⇒ 一并清空持有者。"""
        with self._lock:
            self.countdown_ends_at = None
            self.countdown_task_id = None

    def snapshot(self) -> dict:
        """GET /api/schedule/state 数据源 (ARCH-164 §2.3)。

        issue #197: 倒计时条显示的 next_task_* 取**倒计时持有者**
        (countdown_task_id), 不再取「当前队首」—— 否则归零触发的是持有者
        而界面显示的是队首, 两套真相 (OQ-3)。持有者失效 (被暂停/取消)
        时 ticker ≤1s 内收敛, 此刻读到的可能是陈旧持有者 ⇒ 读一次库
        校准 (holder 存在且仍 pending 才显示标题)。
        """
        with self._lock:
            ends = self.countdown_ends_at
            holder_id = self.countdown_task_id
        cd = None
        if ends is not None:
            holder = self.store.get(holder_id) if holder_id else None
            if holder is None or holder.get("status") != "pending":
                # 持有者已非 pending (暂停/取消/完成): 倒计时本轮会被
                # ticker 清掉并给顺位继承者起新表 ⇒ 此刻不宣称有效持有者
                holder = None
            cd = {"active": True,
                  "ends_at": ends,
                  "remaining_s": max(0, int(ends - self.clock())),
                  # dashboard-ui-20261105 R5 (ANALYSIS 裁决 2 可选项): 倒计时
                  # 总时长只增键, 供驾驶舱进度条分母 (免前端硬编码 300)
                  "total_s": COUNTDOWN_S,
                  "next_task_id": holder["id"] if holder else None,
                  "next_task_title": holder["title"] if holder else None}
        running = self.store.list_by_status("running")
        return {"auto_execute": self.store.get_auto_execute(),
                "countdown": cd,
                "busy_reason": self._busy_reason(),
                "running_task_id": running[0]["id"] if running else None}

    def run_now(self) -> tuple[bool, str]:
        """立即执行 (AC-6.x/A11): 锁内复查全部条件 (含倒计时必须 active),
        不满足 ⇒ (False, reason) ⇒ 409; 满足 ⇒ 锁内取**持有者**转 running。

        F-1 修复: 探活 + 取持有者 + 清倒计时整段都在 _fire_lock 内, 与
        _fire_due 共用同一把锁 ⇒ 两个触发源同时发生只发起一个 run
        (AC-10.2); 锁序固定 _fire_lock → _lock (与 _fire_due 一致)。

        issue #197: 取持有者而非队首 —— 「立即执行」与倒计时归零触发
        同一目标 (AC-Q197-14 语义不变: 仍要求倒计时 active + 全量复查)。"""
        with self._fire_lock:
            ok, reason = self._condition_ok()
            if not ok:
                return False, reason or "not_ready"
            with self._lock:
                if self.countdown_ends_at is None:
                    return False, "countdown_inactive"
                holder_id = self.countdown_task_id
            if not holder_id:
                return False, "countdown_inactive"
            # 持有者已非 pending (暂停/取消后 ticker 尚未重评的窗口内):
            # 倒计时已失效 ⇒ 撤表并按「倒计时未激活」拒绝 (不误取队首,
            # 也不误报「无待执行任务」)
            holder = self.store.get(holder_id)
            if holder is None or holder.get("status") != "pending":
                with self._lock:
                    self.countdown_ends_at = None
                    self.countdown_task_id = None
                return False, "countdown_inactive"
            task = self.store.take_pending(holder_id)
            if task is None:
                return False, "no_pending"
            with self._lock:
                self.countdown_ends_at = None
                self.countdown_task_id = None
        self._spawn_fire(task)
        return True, ""

    # ── 执行条件 C(t) (ARCH-164 §2.2) ──
    def _busy_reason(self) -> str | None:
        """占用原因: inbox 进行中任务 / Team Work ACTIVE run / gateway 忙 /
        探活不可用 (fail-closed: 探活失败按"有 run"处理, 宁可等待不可抢跑)。

        issue #206: 第一判据 = inbox 有进行中任务
        (schedule_tasks.status='running' 非空 ⇒ 'inbox_running')。
        任务刚交付 main、卡片未生成 ⇒ ledger 无 run 行, _has_active_run
        盲区; inbox 行 running 覆盖「已交付未建卡」全程 (take_pending 置位
        → _observe_running 收敛)。用户裁决: 判据以「有进行中任务」为准,
        不再以是否有 run 为判断依据; _has_active_run 保留作第二判据
        (「或」叠加, 覆盖非 inbox 发起的 run, 防双跑)。"""
        if self.store.list_by_status("running"):
            return "inbox_running"
        if self._has_active_run():
            return "active_run"
        free, reason = self._gateway_free()
        if not free:
            return reason or "gateway_probe_unavailable"
        return None

    def _condition_ok(self) -> tuple[bool, str | None]:
        """C(t) = 自动执行开 ∧ 有 pending ∧ 无进行中 run (含探活)。
        返回 (是否满足, busy_reason)。

        issue #179: 无 pending 时返回 'no_pending' (而非 None) ⇒ 该拒绝码
        经 _RUN_NOW_ERRORS 映射到 schedule.err_no_pending_task, 不再落
        泛化文案 '当前状态不允许该操作'。

        ⚠ 消费方口径不变: _tick / _fire_due 只取 ok (丢弃 reason) ⇒ 倒计时
        起表/撤表与归零触发语义零影响 (AC-179-3: 409 状态码不变)。
        """
        if not self.store.get_auto_execute():
            return False, "auto_execute_off"
        if not self.store.has_pending():
            return False, "no_pending"
        reason = self._busy_reason()
        if reason is not None:
            return False, reason
        return True, None

    def _has_active_run(self) -> bool:
        rows = _ro_query(self.ledger_db,
                         "SELECT 1 AS x FROM team_work_runs "
                         "WHERE status='ACTIVE' LIMIT 1")
        return bool(rows)

    def _gateway_free(self) -> tuple[bool, str | None]:
        """gateway 探活 (A9): GET /health/detailed 的 gateway_busy==False
        且 active_agents==0。结果缓存 3s, 探活失败按忙处理。

        issue #180: 缓存时间戳取在探测**结束之后** —— 旧实现记的是探测
        **开始**时刻, 探活耗时 ≥ TTL(3s) 时缓存永不命中 (gateway 故障
        场景每次吃满 5s 超时 ⇒ /api/schedule/state 端到端 3.5~5s, 前端
        2s 轮询请求串行堆积)。

        ⚠ 缓存寿命语义随之变为「距上次探测**结束**不超过 TTL」: 探活耗时
        越长, 缓存越早过期 —— 这是正确语义 (TTL 本就该从探测结束起算)。

        issue #181: reason 三态化 —— busy=True ⇒ 'gateway_busy';
        busy=False 但 active_agents>0 ⇒ **也是** 'gateway_busy' (该组合是
        合法忙态, 旧实现 reason 恒 None ⇒ _busy_reason 回退成
        'gateway_probe_unavailable' ⇒ UI 把「gateway 忙」误报成「探活
        不可用」); busy=False 且 active==0 ⇒ None。探活异常由 except
        分支单独给 'gateway_probe_unavailable' ⇒ 三态互不覆盖。
        """
        now = time.monotonic()
        ts, cached = self._probe_cache
        if cached is not None and now - ts < _PROBE_TTL_S:
            return cached
        base = os.environ.get("MW_GATEWAY_URL", "http://127.0.0.1:8642").rstrip("/")
        key = os.environ.get("API_SERVER_KEY", "")
        try:
            req = urllib.request.Request(
                base + "/health/detailed",
                headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_S) as r:
                data = json.loads(r.read())
            busy = bool(data.get("gateway_busy"))
            active = int(data.get("active_agents") or 0)
            # 三态归因 (issue #181): busy 与「不忙但有活跃 agent」都归
            # gateway_busy (PM 裁决, 不新增 gateway_active_agents 码)
            if busy or active > 0:
                val = (False, "gateway_busy")
            else:
                val = (True, None)
        except Exception:
            val = (False, "gateway_probe_unavailable")
        # 时间戳取在探测**结束之后** (issue #180): 成功与失败两条路径共用
        # 这一行 ⇒ 探活耗时 ≥ TTL 时缓存仍能命中
        self._probe_cache = (time.monotonic(), val)
        return val

    # ── ticker 循环 ──
    def _loop(self) -> None:
        while True:
            try:
                self._tick()
            except Exception:
                # 循环体整体 try/except: daemon 线程永不因异常死亡
                log.exception("schedule ticker tick failed")
            self._wake.wait(1.0)
            self._wake.clear()

    def _tick(self) -> None:
        # 1. 观察 running 任务: 回填 run_id; run 关闭 ⇒ 转 done (AC-5.4/8.5)
        self._observe_running()
        # 2/3. 起表 / 撤表 (issue #197: 倒计时绑定持有者)
        ok, _reason = self._condition_ok()
        due = False
        now = self.clock()
        with self._lock:
            if self.countdown_ends_at is not None:
                # 持有者校验 (AC-Q197-7/8/9): 持有者已非 pending
                # (被暂停/取消/完成) ⇒ 清双字段, **同轮**重评起表分支
                # ⇒ 顺位继承者拿完整新表 (不继承剩余秒数)
                holder = self.store.get(self.countdown_task_id) \
                    if self.countdown_task_id else None
                holder_ok = holder is not None and holder.get("status") == "pending"
                if not ok or not holder_ok:
                    self.countdown_ends_at = None      # AC-5.6 撤销 / 持有者失效
                    self.countdown_task_id = None
                    # 同轮重评: 条件仍满足 ⇒ 顺位继承者起完整新表
                    if ok:
                        head = self.store.head_pending()
                        if head is not None:
                            self.countdown_ends_at = now + self.countdown_s
                            self.countdown_task_id = head["id"]
                elif now >= self.countdown_ends_at:
                    due = True
            elif ok:
                # 起表 (AC-5.5): 持有者 = 队首 (创建最早者)
                head = self.store.head_pending()
                if head is not None:
                    self.countdown_ends_at = now + self.countdown_s
                    self.countdown_task_id = head["id"]
        # 4. 归零: 重新全量复查后触发 (AC-5.3)
        if due:
            self._fire_due()

    def _fire_due(self) -> None:
        """归零触发 (AC-5.3/AC-10.2): 锁内认领倒计时 → 全量复查 → 取持有者。

        F-1 修复 (AC-10.2): 原实现把**可阻塞的探活**放在 _fire_lock 之外,
        且顺序是「先清 countdown_ends_at, 后取 _fire_lock」(倒序) ⇒ ticker
        卡在探活时倒计时仍标着到期, 前端「立即执行」仍可点 ⇒ run_now 抢先
        取走一个队首并发起 run, ticker 恢复后再取走第二个队首 ⇒ 两个 run
        并发发起 (条件 UPDATE 只防"同一任务双发", 防不住"两个任务同时
        发起")。

        现在锁序统一为 _fire_lock → _lock (与 run_now 一致, 无嵌套反转):
          1. 取 _fire_lock —— 两个触发源 (归零 / 立即执行) 互斥;
          2. 认领本轮倒计时 (清 countdown_ends_at + 持有者; 已被他人
             认领/撤销/重启 ⇒ 直接放弃, 不触发);
          3. 锁内探活 + 全量复查 (AC-5.3);
          4. 锁内取**持有者** (条件 UPDATE 双保险) —— issue #197: 归零
             触发的是持有者, 不是归零瞬间的队首 (OQ-3/AC-Q197-10③)。"""
        with self._fire_lock:
            holder_id = self._claim_due_countdown()
            if not holder_id:
                return
            ok, _ = self._condition_ok()   # 归零瞬间全量复查 (锁内探活)
            if not ok:
                return                       # 复查失败 ⇒ 倒计时已清, 下轮重数
            task = self.store.take_pending(holder_id)
        if task is not None:
            self._spawn_fire(task)

    def _claim_due_countdown(self) -> str | None:
        """认领"本轮倒计时归零"这个触发权 (调用方必须持 _fire_lock)。

        返回持有者任务 id ⇒ 本调用方独占本轮触发权 (倒计时已被清空);
        返回 None ⇒ 本轮已被他人认领 (run_now 抢先触发)、倒计时已被撤销
        (条件破坏) 或已重启 (开关 OFF→ON) ⇒ 放弃本轮, 不触发。

        issue #197: 认领时一并交出持有者 id —— 归零触发的是持有者。"""
        now = self.clock()
        with self._lock:
            ends = self.countdown_ends_at
            if ends is None or now < ends:
                return None
            holder_id = self.countdown_task_id
            # 认领: 清表 (复查失败也清, 下一轮 ticker 重新起表重数)
            self.countdown_ends_at = None
            self.countdown_task_id = None
            return holder_id

    def _observe_running(self) -> None:
        """run↔排期观察 (只读 ledger): 未回填 run_id 先按 session_id 查
        team_work_runs.initiator_session 回填; run 行存在且 status!='ACTIVE'
        ⇒ 转 done。run 行尚未出现 (main 还在拆解) ⇒ 保持 running。"""
        for row in self.store.list_by_status("running"):
            # issue #206 兜底: running 行超阈值未收敛 (run 行始终未出现,
            # 如进程重启残留) ⇒ 告警一次。自动回退 pending 列 OQ-R1, 未裁决。
            upd = row.get("updated_at") or 0.0
            if self.clock() - upd > _ZOMBIE_RUNNING_WARN_S:
                key = (row["id"], upd)
                if key not in self._zombie_warned:
                    self._zombie_warned.add(key)
                    log.warning(
                        "schedule: running task %s 已 %.0fs 未收敛 "
                        "(run 行未出现? 疑似僵尸, 倒计时被 inbox_running "
                        "阻塞, issue #206)", row["id"], self.clock() - upd)
            run_id = row.get("run_id")
            if not run_id and row.get("session_id"):
                r = _ro_query(self.ledger_db,
                              "SELECT run_id, status FROM team_work_runs "
                              "WHERE initiator_session=? LIMIT 1",
                              (row["session_id"],))
                if not r:
                    continue
                run_id = r[0]["run_id"]
                self.store.backfill_run(row["id"], run_id)
                if r[0]["status"] != "ACTIVE":
                    self.store.mark_done(row["id"])
            elif run_id:
                r = _ro_query(self.ledger_db,
                              "SELECT status FROM team_work_runs "
                              "WHERE run_id=? LIMIT 1", (run_id,))
                if r and r[0]["status"] != "ACTIVE":
                    self.store.mark_done(row["id"])

    # ── 触发流程 (专用 fire 线程, 不阻塞 ticker; ARCH-164 §2.2) ──
    def _spawn_fire(self, task: dict) -> None:
        threading.Thread(target=self._fire_worker, args=(task,),
                         daemon=True, name=f"mw-sched-fire-{task['id']}").start()

    def _fire_worker(self, task: dict) -> None:
        try:
            sid = self._create_session()
            self.store.backfill_session(task["id"], sid)
            msg = build_dispatch_message(task, sid)
            self._stream_chat(sid, msg)
        except Exception as e:
            # AC-10.1: 发起失败 ⇒ running→pending 回退 + last_error, 下轮可再触发
            log.exception("schedule fire failed for task %s", task["id"])
            self.store.mark_running_failed(task["id"], str(e)[:500])
            self.evaluate_now()

    def _gateway_base_key(self) -> tuple[str, str]:
        base = os.environ.get("MW_GATEWAY_URL", "http://127.0.0.1:8642").rstrip("/")
        return base, os.environ.get("API_SERVER_KEY", "")

    def _create_session(self) -> str:
        """POST {gateway}/api/sessions 取新 sid (与 api_worker_soul_init 同法)。"""
        base, key = self._gateway_base_key()
        req = urllib.request.Request(
            base + "/api/sessions", data=b"{}", method="POST",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            sess = json.loads(r.read())
        sid = (sess.get("session") or {}).get("id") or sess.get("id")
        if not sid:
            raise RuntimeError(_t("schedule.err_session_create_failed"))
        return str(sid)

    def _stream_chat(self, sid: str, message: str) -> None:
        """POST chat/stream 并读干到 EOF (保持 main 回合存活, 语义与浏览器
        挂着 SSE 一致); 单连接硬上限 3600s。"""
        base, key = self._gateway_base_key()
        body = json.dumps({"message": message}).encode()
        req = urllib.request.Request(
            base + f"/api/sessions/{sid}/chat/stream", data=body,
            method="POST",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            deadline = time.monotonic() + _STREAM_HARD_CAP_S
            while time.monotonic() < deadline:
                try:
                    line = r.readline()
                except socket.timeout:
                    continue   # 静默期不算断连, 连接保持
                if not line:
                    break


# 模块级单例 (懒建: 避免 import 期建库; env 变更时重建, 测试可 monkeypatch env)
_engine: ScheduleEngine | None = None
_engine_path: str | None = None


def get_engine() -> ScheduleEngine:
    global _engine, _engine_path
    path = _schedule_db_path()
    if _engine is None or _engine_path != path:
        _engine = ScheduleEngine(ScheduleStore(path))
        _engine_path = path
    return _engine


# ── issue #210: run hold/resume 的排期侧入口 (console 端点调用) ──
# 独立于 get_engine 单例: 短连接存储层直调, 不碰 ticker 内存态
# (倒计时只认 pending/running, hold 行天然不被观察, 无需打断)。
def hold_running_tasks_for_run(run_id: str) -> list[str]:
    """run 置 HOLD 时联动: 名下 running 的 inbox 任务置 hold (默认库)。"""
    return ScheduleStore().hold_running_for_run(run_id)


def resume_held_tasks_for_run(run_id: str) -> list[str]:
    """run resume 时联动: 名下 hold 的 inbox 任务回 pending 队顶 (默认库)。"""
    return ScheduleStore().resume_held_for_run(run_id)


# ════════════════════════════════════════════════════════════════════
# HTTP 层 (console.py 路由转发到这里, 返回 (code, body))
# ════════════════════════════════════════════════════════════════════
def _bad_id(task_id: str) -> tuple[int, dict] | None:
    """id 白名单前置 (照抄 _CRON_JOB_ID_RE 纪律): 非法 id 404, 不落库查询。"""
    if not _SCHED_ID_RE.match(str(task_id or "")):
        return 404, {"error": _t("schedule.err_task_not_found"), "code": 404}
    return None


def api_schedule_tasks() -> tuple[int, dict]:
    """GET /api/schedule/tasks (ARCH-164 §2.3)。"""
    eng = get_engine()
    tasks = eng.store.list_all()
    head = eng.store.head_pending()
    return 200, {"tasks": tasks,
                 "next_task_id": head["id"] if head else None}


def api_schedule_state() -> tuple[int, dict]:
    """GET /api/schedule/state (ARCH-164 §2.3)。"""
    return 200, get_engine().snapshot()


def api_schedule_create(payload: dict) -> tuple[int, dict]:
    """POST /api/schedule/tasks: 201 创建 (标题自动生成); detail 空 ⇒ 400;
    创建后 evaluate_now() (AC-5.5/AC-7.4)。"""
    detail = (payload or {}).get("detail")
    if not isinstance(detail, str) or not detail.strip():
        return 400, {"error": _t("schedule.err_detail_required"), "code": 400}
    priority = (payload or {}).get("priority") or "normal"
    if priority not in VALID_PRIORITIES:
        return 400, {"error": _t("schedule.err_bad_priority"), "code": 400}
    workflow = str((payload or {}).get("workflow") or "auto")
    workspace = str((payload or {}).get("workspace") or "")
    eng = get_engine()
    task = eng.store.create(detail.strip(), priority, workflow, workspace)
    eng.evaluate_now()
    return 201, {"task": task}


def api_schedule_settings(payload: dict) -> tuple[int, dict]:
    """POST /api/schedule/settings: 持久化开关; 关→开重新起完整 5 分钟
    (AC-4.1/AC-4.3)。

    F-2 修复 (AC-4.3): 关→开时**先显式清零倒计时**再 evaluate_now()
    —— 否则 OFF→ON 若发生在同一个 ticker 间隔 (≤1s) 内, ticker 从未
    观察到"关"态, 旧倒计时原样保留 ⇒ 续用关闭前剩余时间而非重新开始
    完整 5 分钟。"""
    enabled = bool((payload or {}).get("auto_execute"))
    eng = get_engine()
    was_on = eng.store.get_auto_execute()
    eng.store.set_auto_execute(enabled)
    if enabled and not was_on:
        eng.restart_countdown()   # AC-4.3: 重开 ⇒ 旧倒计时作废, 重新起完整 5 分钟
        eng.evaluate_now()
    return 200, {"auto_execute": enabled}


# 动作 → (store 方法, 成功后是否 evaluate_now)
_ACTIONS = {
    # issue #197: 暂停唤醒 ticker —— 持有者被暂停时必须即时重评
    # (停表 + 顺位继承者起完整新表, AC-Q197-7), 不等下一个 1s 轮询。
    "pause": ("pause", True),
    "resume": ("resume", True),     # 恢复 ⇒ 重新起表 (A7)
    "cancel": ("cancel", True),     # 取消持有者 ⇒ 同理即时重评 (AC-Q197-9)
    "delete": ("delete_cancelled", False),
}


def api_schedule_action(task_id: str, action: str) -> tuple[int, dict]:
    """POST /api/schedule/tasks/<id>/pause|resume|cancel|delete。
    任务不存在 ⇒ 404; 非法迁移 ⇒ 409 (条件 UPDATE 0 行)。"""
    bad = _bad_id(task_id)
    if bad:
        return bad
    if action not in _ACTIONS:
        return 404, {"error": _t("schedule.err_task_not_found"), "code": 404}
    eng = get_engine()
    method, wake = _ACTIONS[action]
    fn = getattr(eng.store, method)
    if eng.store.get(task_id) is None:
        return 404, {"error": _t("schedule.err_task_not_found"), "code": 404}
    if not fn(task_id):
        # delete 的 409 语义 = 非取消态不可删 (AC-3.5)
        msg = (_t("schedule.err_only_cancelled_delete") if action == "delete"
               else _t("schedule.err_bad_transition"))
        return 409, {"error": msg, "code": 409}
    if wake:
        eng.evaluate_now()
    task = eng.store.get(task_id)
    if task is None:      # delete 成功 ⇒ 无任务体返回
        return 200, {"deleted": task_id}
    return 200, {"task": task}


def api_schedule_update(task_id: str, payload: dict) -> tuple[int, dict]:
    """POST /api/schedule/tasks/<id>/update: 仅 paused (AC-3.6);
    保存后保持 paused (A6/AC-3.7)。"""
    bad = _bad_id(task_id)
    if bad:
        return bad
    eng = get_engine()
    task = eng.store.get(task_id)
    if task is None:
        return 404, {"error": _t("schedule.err_task_not_found"), "code": 404}
    if task["status"] != "paused":
        return 409, {"error": _t("schedule.err_only_paused_edit"), "code": 409}
    payload = payload or {}
    detail = payload.get("detail")
    if detail is not None and (not isinstance(detail, str) or not detail.strip()):
        return 400, {"error": _t("schedule.err_detail_required"), "code": 400}
    priority = payload.get("priority")
    if priority is not None and priority not in VALID_PRIORITIES:
        return 400, {"error": _t("schedule.err_bad_priority"), "code": 400}
    ok = eng.store.update_paused(
        task_id,
        detail=detail.strip() if isinstance(detail, str) else None,
        priority=priority,
        workflow=payload.get("workflow"),
        workspace=payload.get("workspace"))
    if not ok:
        # 并发下状态已变 (如恰好被触发) ⇒ 迁移守卫拒绝
        return 409, {"error": _t("schedule.err_only_paused_edit"), "code": 409}
    return 200, {"task": eng.store.get(task_id)}


# run-now 拒绝原因 → i18n 短句
_RUN_NOW_ERRORS = {
    "auto_execute_off": "schedule.err_auto_exec_off",
    # issue #206: inbox 有进行中任务 ⇒ 复用「A run is in progress」文案
    "inbox_running": "schedule.err_run_busy",
    "active_run": "schedule.err_run_busy",
    "gateway_busy": "schedule.err_run_busy",
    "gateway_probe_unavailable": "schedule.err_gateway_probe",
    "countdown_inactive": "schedule.err_countdown_inactive",
    "no_pending": "schedule.err_no_pending_task",
}


def api_schedule_run_now() -> tuple[int, dict]:
    """POST /api/schedule/run-now: 服务端再复查 (双保险, AC-6.x)。"""
    ok, reason = get_engine().run_now()
    if not ok:
        key = _RUN_NOW_ERRORS.get(reason, "schedule.err_bad_transition")
        return 409, {"error": _t(key), "code": 409}
    return 200, {"ok": True}
