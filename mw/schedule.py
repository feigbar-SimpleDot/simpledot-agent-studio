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

VALID_PRIORITIES = ("urgent", "high", "normal", "low")

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedule_tasks (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    detail       TEXT NOT NULL,
    priority     TEXT NOT NULL DEFAULT 'normal'
                 CHECK(priority IN ('urgent','high','normal','low')),
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','paused','running','done','cancelled')),
    added_at     REAL NOT NULL,
    executed_at  REAL,
    added_by     TEXT NOT NULL DEFAULT '',
    workflow     TEXT NOT NULL DEFAULT 'auto',
    workspace    TEXT NOT NULL DEFAULT '',
    session_id   TEXT,
    run_id       TEXT,
    last_error   TEXT,
    updated_at   REAL NOT NULL
);
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
        """
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        if not self._schema_ready(c):
            # 全新路径: 建库 + 播种 (AC-183-2 不得回归 —— 无需显式初始化)
            c.close()
            self.ensure_schema()
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout=30000")
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

    def _schema_ready(self, c: sqlite3.Connection) -> bool:
        """只读探针: schema 表与种子行是否已就绪 (零写语句)。"""
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

    def _ensure_schema_on(self, c: sqlite3.Connection) -> None:
        """在给定连接上幂等建库 + 播种 (仅在未就绪时真正写)。"""
        if self._schema_ready(c):
            return
        c.executescript(SCHEMA)
        # 种子: 自动执行默认开启 (A12, main 已裁决)
        c.execute("INSERT OR IGNORE INTO schedule_settings(key, value) "
                  "VALUES ('auto_execute_enabled', '1')")
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
        """按 (status 组序, added_at DESC) 服务端预排 (ARCH-164 §2.3):
        组0 执行中 / 组1 未执行(pending+paused) / 组2 已完成+取消。"""
        c = self._read_conn()
        try:
            rows = c.execute(
                "SELECT * FROM schedule_tasks ORDER BY "
                "CASE status WHEN 'running' THEN 0 "
                "          WHEN 'pending' THEN 1 WHEN 'paused' THEN 1 "
                "          ELSE 2 END, added_at DESC").fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    def list_by_status(self, status: str) -> list[dict]:
        c = self._read_conn()
        try:
            rows = c.execute(
                "SELECT * FROM schedule_tasks WHERE status=? "
                "ORDER BY added_at DESC", (status,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    def head_pending(self) -> dict | None:
        """队首待执行任务: 取序 = 显示序 = added_at DESC (A8 裁决)。"""
        c = self._read_conn()
        try:
            r = c.execute(
                "SELECT * FROM schedule_tasks WHERE status='pending' "
                "ORDER BY added_at DESC LIMIT 1").fetchone()
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
        return self._cond_update(task_id, "status='cancelled'",
                                 "status IN ('pending','paused')", ())

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
        0 行 ⇒ 放弃)。返回置为 running 后的任务或 None。"""
        c = self._write_conn()
        try:
            r = c.execute("SELECT * FROM schedule_tasks WHERE status='pending' "
                          "ORDER BY added_at DESC LIMIT 1").fetchone()
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

    def mark_running_failed(self, task_id: str, err: str) -> bool:
        """触发失败回退 running→pending + last_error (AC-10.1)。"""
        return self._cond_update(task_id, "status='pending', last_error=?",
                                 "status='running'", (err,))

    def mark_done(self, task_id: str) -> bool:  # running→done
        return self._cond_update(task_id, "status='done'",
                                 "status='running'", ())

    def backfill_session(self, task_id: str, session_id: str) -> bool:
        return self._cond_update(task_id, "session_id=?",
                                 "status='running'", (session_id,))

    def backfill_run(self, task_id: str, run_id: str) -> bool:
        return self._cond_update(task_id, "run_id=?",
                                 "status='running'", (run_id,))


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
        self._probe_cache: tuple[float, tuple[bool, str | None] | None] = (0.0, None)
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

        清零后由下一轮 _tick() 重新起完整 countdown_s 秒。"""
        with self._lock:
            self.countdown_ends_at = None

    def snapshot(self) -> dict:
        """GET /api/schedule/state 数据源 (ARCH-164 §2.3)。"""
        with self._lock:
            ends = self.countdown_ends_at
        cd = None
        if ends is not None:
            head = self.store.head_pending()
            cd = {"active": True,
                  "ends_at": ends,
                  "remaining_s": max(0, int(ends - self.clock())),
                  "next_task_id": head["id"] if head else None,
                  "next_task_title": head["title"] if head else None}
        running = self.store.list_by_status("running")
        return {"auto_execute": self.store.get_auto_execute(),
                "countdown": cd,
                "busy_reason": self._busy_reason(),
                "running_task_id": running[0]["id"] if running else None}

    def run_now(self) -> tuple[bool, str]:
        """立即执行 (AC-6.x/A11): 锁内复查全部条件 (含倒计时必须 active),
        不满足 ⇒ (False, reason) ⇒ 409; 满足 ⇒ 锁内取队首转 running。

        F-1 修复: 探活 + 取队首 + 清倒计时整段都在 _fire_lock 内, 与
        _fire_due 共用同一把锁 ⇒ 两个触发源同时发生只发起一个 run
        (AC-10.2); 锁序固定 _fire_lock → _lock (与 _fire_due 一致)。"""
        with self._fire_lock:
            ok, reason = self._condition_ok()
            if not ok:
                return False, reason or "not_ready"
            with self._lock:
                if self.countdown_ends_at is None:
                    return False, "countdown_inactive"
            task = self.store.take_head_pending()
            if task is None:
                return False, "no_pending"
            with self._lock:
                self.countdown_ends_at = None
        self._spawn_fire(task)
        return True, ""

    # ── 执行条件 C(t) (ARCH-164 §2.2) ──
    def _busy_reason(self) -> str | None:
        """占用原因: Team Work ACTIVE run / gateway 忙 / 探活不可用
        (fail-closed: 探活失败按"有 run"处理, 宁可等待不可抢跑)。"""
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
        # 2/3. 起表 / 撤表
        ok, _reason = self._condition_ok()
        due = False
        now = self.clock()
        with self._lock:
            if self.countdown_ends_at is not None:
                if not ok:
                    self.countdown_ends_at = None       # AC-5.6 撤销
                elif now >= self.countdown_ends_at:
                    due = True
            elif ok:
                self.countdown_ends_at = now + self.countdown_s  # AC-5.5 起表
        # 4. 归零: 重新全量复查后触发 (AC-5.3)
        if due:
            self._fire_due()

    def _fire_due(self) -> None:
        """归零触发 (AC-5.3/AC-10.2): 锁内认领倒计时 → 全量复查 → 取队首。

        F-1 修复 (AC-10.2): 原实现把**可阻塞的探活**放在 _fire_lock 之外,
        且顺序是「先清 countdown_ends_at, 后取 _fire_lock」(倒序) ⇒ ticker
        卡在探活时倒计时仍标着到期, 前端「立即执行」仍可点 ⇒ run_now 抢先
        取走一个队首并发起 run, ticker 恢复后再取走第二个队首 ⇒ 两个 run
        并发发起 (条件 UPDATE 只防"同一任务双发", 防不住"两个任务同时
        发起")。

        现在锁序统一为 _fire_lock → _lock (与 run_now 一致, 无嵌套反转):
          1. 取 _fire_lock —— 两个触发源 (归零 / 立即执行) 互斥;
          2. 认领本轮倒计时 (清 countdown_ends_at; 已被他人认领/撤销/重启
             ⇒ 直接放弃, 不触发);
          3. 锁内探活 + 全量复查 (AC-5.3);
          4. 锁内取队首 (条件 UPDATE 双保险)。"""
        with self._fire_lock:
            if not self._claim_due_countdown():
                return
            ok, _ = self._condition_ok()   # 归零瞬间全量复查 (锁内探活)
            if not ok:
                return                       # 复查失败 ⇒ 倒计时已清, 下轮重数
            task = self.store.take_head_pending()
        if task is not None:
            self._spawn_fire(task)

    def _claim_due_countdown(self) -> bool:
        """认领"本轮倒计时归零"这个触发权 (调用方必须持 _fire_lock)。

        返回 True ⇒ 本调用方独占本轮触发权 (倒计时已被清空);
        返回 False ⇒ 本轮已被他人认领 (run_now 抢先触发)、倒计时已被撤销
        (条件破坏) 或已重启 (开关 OFF→ON) ⇒ 放弃本轮, 不触发。"""
        now = self.clock()
        with self._lock:
            ends = self.countdown_ends_at
            if ends is None or now < ends:
                return False
            # 认领: 清表 (复查失败也清, 下一轮 ticker 重新起表重数)
            self.countdown_ends_at = None
            return True

    def _observe_running(self) -> None:
        """run↔排期观察 (只读 ledger): 未回填 run_id 先按 session_id 查
        team_work_runs.initiator_session 回填; run 行存在且 status!='ACTIVE'
        ⇒ 转 done。run 行尚未出现 (main 还在拆解) ⇒ 保持 running。"""
        for row in self.store.list_by_status("running"):
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
    "pause": ("pause", False),
    "resume": ("resume", True),     # 恢复 ⇒ 重新起表 (A7)
    "cancel": ("cancel", False),
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
