"""tests/test_schedule_store.py — 排期存储层状态机测试 (issue #164, ARCH-164 §8#10)。

覆盖:
  SM1  状态机全迁移矩阵 (合法迁移成功 / 非法迁移返回 False ⇒ 服务层 409 语义)
  SM2  仅取消态可删 (AC-3.5)
  SM3  修改仅 paused 且保存后保持 paused (A6/AC-3.7)
  SM4  标题自动截取 (A2: 首条非空行, >40 截 40 + …)
  SM5  队首取序 = added_at DESC (A8 裁决: 显示序=执行序)
  SM6  自动执行开关持久化 (A12 默认开; AC-4.1 重启保持)
  SM7  列表分组预排 (组0 running / 组1 pending+paused / 组2 done+cancelled)
  SM8  读写分离 (issue #183): 读路径零写语句 / 首次建库自动建表+播种 /
       新库写路径不因「表不存在」失败 / 并发首建库无异常

跑法: pytest tests/test_schedule_store.py -v
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mw import schedule as S  # noqa: E402


@pytest.fixture()
def store(tmp_path):
    return S.ScheduleStore(str(tmp_path / "schedule.db"))


def _mk(store, detail="任务", **kw):
    return store.create(detail, **kw)


# ── SM1: 状态机全迁移矩阵 ────────────────────────────────────────────
def test_legal_transitions(store):
    t = _mk(store)
    assert t["status"] == "pending"
    assert store.pause(t["id"]) is True            # pending→paused
    assert store.resume(t["id"]) is True           # paused→pending
    assert store.cancel(t["id"]) is True           # pending→cancelled


def test_cancel_from_paused(store):
    t = _mk(store)
    store.pause(t["id"])
    assert store.cancel(t["id"]) is True           # paused→cancelled


def test_illegal_transitions_return_false(store):
    t = _mk(store)
    # pending 态: resume/修改/删除 全非法 (0 行 ⇒ False ⇒ 409)
    assert store.resume(t["id"]) is False
    assert store.update_paused(t["id"], detail="x") is False
    assert store.delete_cancelled(t["id"]) is False
    # 重复 pause 非法
    assert store.pause(t["id"]) is True
    assert store.pause(t["id"]) is False
    # cancelled 终态: 一切迁移非法
    assert store.cancel(t["id"]) is True
    assert store.pause(t["id"]) is False
    assert store.resume(t["id"]) is False
    assert store.cancel(t["id"]) is False


def test_running_transitions(store):
    t = _mk(store)
    head = store.take_head_pending()
    assert head["id"] == t["id"] and head["status"] == "running"
    assert head["executed_at"] is not None
    # running 态: pause/cancel/update 全非法 (AC-3.8)
    assert store.pause(t["id"]) is False
    assert store.cancel(t["id"]) is False
    assert store.update_paused(t["id"], detail="x") is False
    # running→done 与 running→pending(失败回退)
    assert store.mark_done(t["id"]) is True
    assert store.mark_done(t["id"]) is False       # done 后再转非法
    t2 = _mk(store)
    store.take_head_pending()
    assert store.mark_running_failed(t2["id"], "boom") is True
    got = store.get(t2["id"])
    assert got["status"] == "pending" and got["last_error"] == "boom"


# ── SM2: 仅取消态可删 ────────────────────────────────────────────────
def test_only_cancelled_deletable(store):
    a = _mk(store, "A")
    b = _mk(store, "B")
    assert store.delete_cancelled(a["id"]) is False    # pending 不可删
    store.cancel(b["id"])
    assert store.delete_cancelled(b["id"]) is True
    assert store.get(b["id"]) is None
    assert store.delete_cancelled(b["id"]) is False    # 已删再删 False


# ── SM3: 修改仅 paused, 保持 paused ─────────────────────────────────
def test_update_only_paused_keeps_paused(store):
    t = _mk(store, "原标题")
    store.pause(t["id"])
    assert store.update_paused(t["id"], detail="新详情行", priority="urgent") is True
    got = store.get(t["id"])
    assert got["status"] == "paused"                   # A6: 保持暂停
    assert got["detail"] == "新详情行"
    assert got["title"] == "新详情行"                  # 标题随 detail 重生成
    assert got["priority"] == "urgent"


# ── SM4: 标题截取 ────────────────────────────────────────────────────
def test_title_generation(store):
    assert S.make_title("  首行  \n次行") == "首行"
    assert S.make_title("\n\n 缩进的行") == "缩进的行"
    assert S.make_title("x" * 60) == "x" * 40 + "…"
    assert len(S.make_title("x" * 60)) == 41
    assert S.make_title("") == ""
    t = store.create("带\n换行的任务详情")
    assert t["title"] == "带"


# ── SM5: 队首取序 ────────────────────────────────────────────────────
def test_head_pending_order(store):
    """队首 = 创建最早者 (added_at ASC) —— issue #197 取代 A8 的 DESC 裁决。

    MV-197-1: 把 head_pending/take_head_pending 的 ASC 改回 DESC ⇒ 本用例红。
    """
    a = _mk(store, "A")
    time.sleep(0.01)
    b = _mk(store, "B")
    time.sleep(0.01)
    c = _mk(store, "C")
    assert store.head_pending()["id"] == a["id"]       # added_at ASC
    store.pause(a["id"])                               # 暂停天然不在 pending 集
    assert store.head_pending()["id"] == b["id"]
    assert store.take_head_pending()["id"] == b["id"]
    assert store.take_head_pending()["id"] == c["id"]
    assert store.take_head_pending() is None


# ── SM5b: take_pending 精确取 (倒计时持有者) ────────────────────────
def test_take_pending_targets_exact_task(store):
    """take_pending(task_id) 按 id 精确取, 不按队首取 (AC-Q197-10③)。

    MV-197-2: 把 take_pending 换回 take_head_pending ⇒ 本用例红。
    """
    a = _mk(store, "A")
    time.sleep(0.01)
    b = _mk(store, "B")
    # 指定取 b (非队首 a) ⇒ b 变 running, a 仍 pending
    got = store.take_pending(b["id"])
    assert got is not None and got["id"] == b["id"]
    assert got["status"] == "running" and got["executed_at"] is not None
    assert store.get(a["id"])["status"] == "pending"
    # 非 pending (已 running) ⇒ 0 行 ⇒ 弃轮返回 None (双保险)
    assert store.take_pending(b["id"]) is None
    # 不存在的 id ⇒ None
    assert store.take_pending("nope") is None


# ── SM6: 开关持久化 ──────────────────────────────────────────────────
def test_auto_execute_persist(tmp_path):
    p = str(tmp_path / "s.db")
    st = S.ScheduleStore(p)
    assert st.get_auto_execute() is True               # A12 默认开
    st.set_auto_execute(False)
    # 新连接 (模拟重启) 读回持久值
    assert S.ScheduleStore(p).get_auto_execute() is False
    S.ScheduleStore(p).set_auto_execute(True)
    assert S.ScheduleStore(p).get_auto_execute() is True


# ── SM7: 列表分组预排 ────────────────────────────────────────────────
def test_list_grouping(store):
    """四分组 + 组内序 (issue #197)。

    组序: running / pending / paused / done+cancelled (paused 独立成组,
    OQ-1 裁决)。组内: 组0-2 按 added_at ASC, 组3 按完成时刻 DESC。

    MV-197-3: 把 paused 并回 pending 组 / 组内序改回 DESC ⇒ 本用例红。
    """
    a = _mk(store, "A")
    b = _mk(store, "B")
    c = _mk(store, "C")
    d = _mk(store, "D")
    store.take_head_pending()          # 队首 (a, #197 升序) → running
    time.sleep(0.01)
    assert store.take_pending(d["id"]) is not None   # d → running
    time.sleep(0.01)
    assert store.mark_done(d["id"]) is True           # d → done (写 finished_at)
    time.sleep(0.01)
    assert store.cancel(c["id"]) is True              # c → cancelled (写 finished_at)
    store.pause(b["id"])               # b → paused
    order = [t["id"] for t in store.list_all()]
    # 组0 running: a / 组1 pending: 无 / 组2 paused: b /
    # 组3 done+cancelled: 按完成时刻 DESC ⇒ c(后取消) 先, d(先完成) 后
    assert order == [a["id"], b["id"], c["id"], d["id"]]


def test_list_grouping_paused_is_separate_group(store):
    """AC-Q197-6: 暂停任务不出现在待执行分组, 而在独立「已暂停」组。"""
    a = _mk(store, "A")
    b = _mk(store, "B")
    store.pause(a["id"])
    rows = store.list_all()
    statuses = [(r["id"], r["status"]) for r in rows]
    assert statuses == [(b["id"], "pending"), (a["id"], "paused")]


# ── SM7b: 完成时刻 (finished_at) 排序 (AC-Q197-3/4) ──────────────────
def test_finished_at_written_on_done_and_cancel(store):
    """AC-Q197-4: done/cancelled 行必须写完成时刻 (非创建/开始执行时刻)。

    MV-197-4: 把 mark_done/cancel 的 finished_at 写入去掉 ⇒ 本用例红。
    """
    t = _mk(store, "A")
    assert store.get(t["id"])["finished_at"] is None
    store.take_head_pending()
    time.sleep(0.01)
    assert store.mark_done(t["id"]) is True
    done_row = store.get(t["id"])
    assert done_row["finished_at"] is not None
    assert done_row["finished_at"] >= done_row["added_at"]
    # 取消同样写 finished_at
    c = _mk(store, "C")
    time.sleep(0.01)
    assert store.cancel(c["id"]) is True
    assert store.get(c["id"])["finished_at"] is not None


def test_done_group_sorted_by_finished_at_desc(store):
    """AC-Q197-3: 已完成组按**完成时刻**降序 (最近完成的在最上)。

    后创建的任务先完成 ⇒ 必须排在更早创建但更晚完成的任务之前 ——
    这正是「按创建时刻排」会错的场景 (变异反证点 M2)。

    MV-197-5: 把 list_all 的组3 排序键改回 added_at ⇒ 本用例红。
    """
    early = _mk(store, "早期创建")           # 先创建
    time.sleep(0.02)
    late = _mk(store, "晚期创建")            # 后创建
    # 晚期创建的任务先完成
    assert store.take_pending(late["id"]) is not None
    time.sleep(0.02)
    assert store.mark_done(late["id"]) is True
    # 早期创建的任务后完成
    assert store.take_pending(early["id"]) is not None
    time.sleep(0.02)
    assert store.mark_done(early["id"]) is True
    done_rows = [r for r in store.list_all() if r["status"] == "done"]
    assert [r["id"] for r in done_rows] == [early["id"], late["id"]]
    # 完成时刻确实降序 (按创建时刻排会得到相反次序 ⇒ 断言有牙齿)
    assert done_rows[0]["finished_at"] > done_rows[1]["finished_at"]


# ── SM7c: 存量库迁移 (AC-Q197-4 的存量口径) ─────────────────────────
def _make_legacy_db(path):
    """造一个 v1 存量库 (无 finished_at 列, 无 schema_version 行)。"""
    c = sqlite3.connect(str(path))
    c.executescript(
        "CREATE TABLE schedule_tasks ("
        " id TEXT PRIMARY KEY, title TEXT NOT NULL, detail TEXT NOT NULL,"
        " priority TEXT NOT NULL DEFAULT 'normal',"
        " status TEXT NOT NULL DEFAULT 'pending',"
        " added_at REAL NOT NULL, executed_at REAL,"
        " added_by TEXT NOT NULL DEFAULT '',"
        " workflow TEXT NOT NULL DEFAULT 'auto',"
        " workspace TEXT NOT NULL DEFAULT '',"
        " session_id TEXT, run_id TEXT, last_error TEXT,"
        " updated_at REAL NOT NULL);"
        "CREATE TABLE schedule_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    c.execute("INSERT INTO schedule_settings(key, value) "
              "VALUES ('auto_execute_enabled', '1')")
    c.execute("INSERT INTO schedule_tasks(id, title, detail, status, added_at, "
              "updated_at) VALUES ('old-done','旧已完成','x','done',100.0,500.0)")
    c.execute("INSERT INTO schedule_tasks(id, title, detail, status, added_at, "
              "updated_at) VALUES ('old-pend','旧待执行','x','pending',200.0,200.0)")
    c.commit()
    c.close()


def test_legacy_db_migrated_adds_finished_at(tmp_path):
    """存量 v1 库 ⇒ 首次写连接自动迁移 (加列 + 回填 + 版本行)。

    MV-197-6: 把迁移去掉 ⇒ 本用例红 (列缺失 / 存量 done 行 finished_at 为空)。
    """
    p = tmp_path / "legacy.db"
    _make_legacy_db(p)
    st = S.ScheduleStore(str(p))
    # 迁移发生在写连接 ⇒ 一次写即触发
    st.ensure_schema()
    c = sqlite3.connect(str(p))
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(schedule_tasks)")}
        ver = c.execute("SELECT value FROM schedule_settings "
                        "WHERE key='schema_version'").fetchone()
        old_done = c.execute("SELECT finished_at FROM schedule_tasks "
                             "WHERE id='old-done'").fetchone()
    finally:
        c.close()
    assert "finished_at" in cols, "存量库未加 finished_at 列"
    assert ver is not None and int(ver[0]) >= S.SCHEMA_VERSION
    # 存量 done 行以 updated_at 回填 (最后一次可用近似)
    assert old_done is not None and old_done[0] is not None
    assert abs(old_done[0] - 500.0) < 1e-6


def test_migration_is_idempotent(tmp_path):
    """迁移幂等: 重复 ensure_schema 不炸, 数据不丢, 版本行稳定。"""
    p = tmp_path / "legacy.db"
    _make_legacy_db(p)
    st = S.ScheduleStore(str(p))
    st.ensure_schema()
    st.ensure_schema()
    st.ensure_schema()
    c = sqlite3.connect(str(p))
    try:
        ver = c.execute("SELECT value FROM schedule_settings "
                        "WHERE key='schema_version'").fetchone()
        n = c.execute("SELECT COUNT(*) FROM schedule_tasks").fetchone()[0]
    finally:
        c.close()
    assert int(ver[0]) == S.SCHEMA_VERSION
    assert n == 2


def test_migrated_db_reads_do_not_crash(tmp_path):
    """迁移后读路径正常 (排序表达式引用 finished_at 列不得报错)。"""
    p = tmp_path / "legacy.db"
    _make_legacy_db(p)
    st = S.ScheduleStore(str(p))
    st.ensure_schema()
    rows = st.list_all()
    assert len(rows) == 2
    head = st.head_pending()
    assert head is not None and head["id"] == "old-pend"


# ── SM8: 读写分离 (issue #183) ────────────────────────────────────────
#
# 旧行为: _conn() 每次连接都 executescript(SCHEMA) + 播种 INSERT +
# commit() + PRAGMA journal_mode=WAL ⇒ 纯读 list_all() 也跑 4 条写语句
# + 1 次写事务 (实测 9 条语句); snapshot() 每 2s 轮询开 2~3 个连接 ⇒
# 常态每秒数次写事务, 与「独立库零锁竞争」初衷有出入。
#
# 修复后: 读连接只发 SELECT + 只读 PRAGMA(busy_timeout); DDL/播种收敛到
# ensure_schema() (仅未就绪时真正写一次); 写连接先确保 schema 就绪。

# 读路径不允许出现的语句形态 (MV-183-a/b/c/d 的判据)
_WRITE_STMT_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE)\b"
    r"|\bCOMMIT\b|\bBEGIN\b|journal_mode",
    re.IGNORECASE)


class _TraceConn:
    """给 sqlite3.connect 装 trace 回调, 捕获该连接执行的全部语句。

    必须挂在 **连接** 上 (而不是全局打桩 sqlite3.connect): trace 回调能
    捕到 executescript 内部的语句, 全局桩会漏掉它们 —— 那样「读路径零
    写」就成了假绿。
    """

    def __init__(self):
        self.statements: list[str] = []

    def install(self, monkeypatch):
        real_connect = sqlite3.connect

        def spy(path, *args, **kwargs):
            c = real_connect(path, *args, **kwargs)
            c.set_trace_callback(self.statements.append)
            return c

        monkeypatch.setattr(sqlite3, "connect", spy)
        return self


def _read_methods(store):
    """返回全部读方法名 (读路径必须零写)。"""
    return ["get", "list_all", "list_by_status", "head_pending",
            "has_pending", "get_auto_execute"]


def test_read_path_has_zero_write_statements(tmp_path, monkeypatch):
    """AC-183-1: 已建好的库上, 纯读操作零写语句 (无 DDL/INSERT/COMMIT/
    journal_mode)。

    判据同时用两把尺子: (a) trace 回调捕获的全部语句里没有任何写语句
    形态; (b) 读操作前后 sqlite3.Connection.total_changes 不变。
    """
    p = str(tmp_path / "schedule.db")
    st = S.ScheduleStore(p)
    st.create("预置任务")                 # 先建库 + 建表 + 播种

    trace = _TraceConn().install(monkeypatch)
    st.list_all()
    st.head_pending()
    st.has_pending()
    st.get_auto_execute()
    st.list_by_status("pending")
    st.get("nope")

    offenders = [s for s in trace.statements if _WRITE_STMT_RE.search(s)]
    assert offenders == [], f"读路径出现写语句: {offenders}"
    # 读路径确实发了语句 (不是「压根没查」的空转假绿)
    assert any("SELECT" in s for s in trace.statements)


def test_read_path_total_changes_unchanged(tmp_path, monkeypatch):
    """AC-183-1 判据 (b): 读路径连接上 total_changes 不变 (恒为 0)。

    ⚠ 必须断言**存储层自己开的那条连接**的 total_changes: 另开一条
    连接去查 total_changes 是空断言 (total_changes 是连接级计数器,
    别的连接改了行它也看不见) ⇒ 那样「假绿」。

    存储层每次操作都 close 连接 ⇒ 用 sqlite3 的 factory 钩子造一条
    「close 之前记账」的连接子类来采样。

    ⚠ 口径说明 (诚实边界): total_changes 统计的是**实际生效的行变更**,
    不是语句条数 ⇒ 对已存在行的 `INSERT OR IGNORE` 不可见 (实测: 读
    连接加一条播种 INSERT 时本用例仍绿)。因此本断言只作为「读路径
    没改数据行」的**辅证**, 语句级判据由
    test_read_path_has_zero_write_statements (trace 回调) 承担 ——
    那一组才是能抓住「假修复」的主断言。
    """
    p = str(tmp_path / "schedule.db")
    st = S.ScheduleStore(p)
    st.create("任务")

    # 存储层每次操作都会 close 连接 ⇒ total_changes 必须在 close 之前
    # 采样 (关掉后访问抛 ProgrammingError)。用 sqlite3 的 factory 钩子
    # 造一条「会记账」的连接子类。
    changes: list[int] = []
    real_connect = sqlite3.connect

    class _TrackedConn(sqlite3.Connection):
        """close() 之前把 total_changes 记到外部列表。"""

        def close(self):
            try:
                changes.append(self.total_changes)
            except sqlite3.Error:
                pass
            super().close()

    def spy(path, *args, **kwargs):
        kwargs["factory"] = _TrackedConn
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    st.list_all()
    st.has_pending()
    st.get_auto_execute()
    st.head_pending()
    monkeypatch.undo()

    assert changes, "存储层未开任何连接 (用例失效)"
    assert all(n == 0 for n in changes), \
        f"读路径改了数据行 (total_changes != 0): {changes}"
    assert all(n == 0 for n in changes), \
        f"读路径改了数据行 (total_changes != 0): {changes}"


def test_read_path_zero_writes_on_warm_db(tmp_path, monkeypatch):
    """AC-183-1 变体: 热库 (非首次) 上的读路径同样零写。

    冷库那次会因「首次建库」产生 DDL + 播种 (AC-183-2 要求), 所以本
    用例先建库, 再在**同一路径**上断言读路径零写。
    """
    p = str(tmp_path / "schedule.db")
    st = S.ScheduleStore(p)
    st.create("任务")

    trace = _TraceConn().install(monkeypatch)
    for _ in range(3):
        st.list_all()
    offenders = [s for s in trace.statements if _WRITE_STMT_RE.search(s)]
    assert offenders == [], f"热库读路径出现写语句: {offenders}"


def test_first_use_creates_schema_and_seed(tmp_path):
    """AC-183-2: 全新路径下 list_all() 返回空列表且 schedule_settings 含
    种子行 (auto_execute_enabled='1'), 无需显式初始化调用。

    「首次建库仍自动建表+播种」不得回归。
    """
    p = tmp_path / "fresh" / "schedule.db"
    st = S.ScheduleStore(str(p))

    assert st.list_all() == []                     # 读即建库
    assert st.get_auto_execute() is True           # 种子已就绪

    c = sqlite3.connect(str(p))
    try:
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        seed = c.execute("SELECT value FROM schedule_settings "
                         "WHERE key='auto_execute_enabled'").fetchone()
    finally:
        c.close()
    assert "schedule_tasks" in tables
    assert "schedule_settings" in tables
    assert seed is not None and seed[0] == "1"


def test_write_path_on_fresh_db_succeeds(tmp_path):
    """X-10 / AC-183-2: 新库上写路径不因「表不存在」失败。

    写方法必须先确保 schema 已建, 否则 INSERT 会报 no such table。
    """
    st = S.ScheduleStore(str(tmp_path / "fresh.db"))
    t = st.create("直接写")
    assert t["id"]
    assert st.get(t["id"])["detail"] == "直接写"
    assert st.has_pending() is True


def test_write_path_creates_schema_when_missing(tmp_path):
    """写路径自愈: 库文件存在但表被删 (漂移) ⇒ 写路径重建 schema 后成功。

    ensure_schema 的探针判「表 + 种子行是否就绪」, 表缺失 ⇒ 重建。
    """
    p = str(tmp_path / "drift.db")
    st = S.ScheduleStore(p)
    st.create("任务")
    c = sqlite3.connect(p)
    try:
        c.execute("DROP TABLE schedule_tasks")
        c.commit()
    finally:
        c.close()

    t = st.create("漂移后重建")
    assert t["id"]
    assert st.get(t["id"])["detail"] == "漂移后重建"


def test_ensure_schema_is_idempotent(tmp_path):
    """ensure_schema 幂等: 重复调用不炸, 种子值不被覆盖。"""
    p = str(tmp_path / "idem.db")
    st = S.ScheduleStore(p)
    st.ensure_schema()
    st.ensure_schema()
    st.set_auto_execute(False)
    st.ensure_schema()                          # 不得把开关重置回 '1'
    assert st.get_auto_execute() is False


def test_concurrent_first_use_creates_schema_once(tmp_path):
    """并发首建库 (ticker 线程与 API 线程同进程) 不抛异常, 库结构完好。

    ensure_schema 用进程内锁 + 双重检查串行化 ⇒ 只有一个连接跑 DDL,
    其余等待后直接返回。
    """
    p = str(tmp_path / "concurrent.db")
    st = S.ScheduleStore(p)
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def worker():
        try:
            barrier.wait()
            st.list_all()
            st.has_pending()
        except BaseException as e:                # noqa: BLE001 (断言用)
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == [], f"并发首建库抛异常: {errors}"

    c = sqlite3.connect(p)
    try:
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        seed = c.execute("SELECT value FROM schedule_settings "
                         "WHERE key='auto_execute_enabled'").fetchone()
    finally:
        c.close()
    assert {"schedule_tasks", "schedule_settings"} <= tables
    assert seed is not None and seed[0] == "1"


def test_write_path_still_writes(tmp_path, monkeypatch):
    """回归锁: 写路径**仍要**真写 (读写分离不得把写也砍掉)。

    断言写方法确实发出 INSERT/UPDATE 语句 —— 防止「假分离」: 只把写
    挪到别处却让写路径也不落库。
    """
    p = str(tmp_path / "w.db")
    st = S.ScheduleStore(p)
    trace = _TraceConn().install(monkeypatch)
    t = st.create("要写")
    assert t["id"]
    joined = " ".join(trace.statements).upper()
    assert "INSERT INTO SCHEDULE_TASKS" in joined


# ── SM9: snapshot() 一个周期零写 (issue #183, AC-183-4) ───────────────
#
# snapshot() 是前端 2s 轮询的数据源 (/api/schedule/state), 它一次周期
# 走 head_pending + list_by_status('running') + get_auto_execute ⇒ 旧实现
# 是 3 个连接 = 3 次 DDL + 3 次写事务 ⇒ 常态每秒数次写事务。

def test_snapshot_cycle_has_zero_write_statements(tmp_path, monkeypatch):
    """AC-183-4: snapshot() 一个周期内不再产生常态写事务。

    与 AC-183-1 同判据 (trace 回调零写语句); 覆盖 snapshot 路径。
    """
    from mw import schedule as S  # noqa: F811 (模块内引用)
    p = str(tmp_path / "snap.db")
    st = S.ScheduleStore(p)
    st.create("任务")

    eng = S.ScheduleEngine(st, ledger_db=str(tmp_path / "ledger.db"))
    eng._gateway_free = lambda: (True, None)

    trace = _TraceConn().install(monkeypatch)
    eng.snapshot()
    offenders = [s for s in trace.statements if _WRITE_STMT_RE.search(s)]
    assert offenders == [], f"snapshot 周期出现写语句: {offenders}"
    # 快照确实查了库 (不是空转假绿)
    assert any("SELECT" in s for s in trace.statements)


# ── F-1 回流修复: 存量 v1 库读路径零写降级 (issue #197) ──────────────
#
# 背景 (docs/REVIEW-Q-20261102.md F-1, P1):
#   存量 v1 库 (无 schema_version 行 / 无 finished_at 列) 上, 任何一次
#   读 (list_all) 经 _read_conn → _schema_ready 判未就绪 → ensure_schema()
#   在**写连接**上跑迁移 (ALTER + UPDATE + INSERT + COMMIT + journal_mode)
#   ⇒ 读路径真写库; 只读库 (chmod 0444) 上直接抛 OperationalError。
#
# 本组守护的夹具是**未经任何写连接触碰**的 v1 库 —— 既有三个零写守护
# (test_read_path_has_zero_write_statements 等) 的夹具都是 st.create(),
# 走当前代码建库 ⇒ 建出来就是 v2 ⇒ 永远命中「已就绪」分支 ⇒ 对本路径
# **零覆盖**。这正是 F-1 逃过既有守护的原因。

def _make_v1_db(path):
    """造一个**未经写连接触碰**的 v1 存量库。

    刻意不用 ScheduleStore.create()/ensure_schema() —— 那两个调用走当前
    代码, 会把库直接升到 v2 (带 finished_at 列 + schema_version 行) ⇒
    构造不出「迁移尚未发生」的存量库场景。
    """
    c = sqlite3.connect(str(path))
    c.executescript(
        "CREATE TABLE schedule_tasks ("
        " id TEXT PRIMARY KEY, title TEXT NOT NULL, detail TEXT NOT NULL,"
        " priority TEXT NOT NULL DEFAULT 'normal',"
        " status TEXT NOT NULL DEFAULT 'pending',"
        " added_at REAL NOT NULL, executed_at REAL,"
        " added_by TEXT NOT NULL DEFAULT '',"
        " workflow TEXT NOT NULL DEFAULT 'auto',"
        " workspace TEXT NOT NULL DEFAULT '',"
        " session_id TEXT, run_id TEXT, last_error TEXT,"
        " updated_at REAL NOT NULL);"
        "CREATE TABLE schedule_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    c.execute("INSERT INTO schedule_settings(key, value) "
              "VALUES ('auto_execute_enabled', '1')")
    c.execute("INSERT INTO schedule_tasks(id, title, detail, status, added_at, "
              "updated_at) VALUES ('old-done','旧已完成','x','done',100.0,500.0)")
    c.execute("INSERT INTO schedule_tasks(id, title, detail, status, added_at, "
              "updated_at) VALUES ('old-pend','旧待执行','x','pending',200.0,200.0)")
    c.commit()
    c.close()


def test_v1_legacy_db_read_path_has_zero_write_statements(tmp_path, monkeypatch):
    """F-1 主守护: 存量 v1 库上 list_all() 发**零**写语句。

    MV-F-1: 把 _read_conn 的懒建分支改回「未就绪就 ensure_schema()」
    ⇒ 本用例红 (实测 6 条写语句: journal_mode=WAL / ALTER / BEGIN /
    UPDATE / INSERT / COMMIT)。
    """
    p = str(tmp_path / "v1.db")
    _make_v1_db(p)
    st = S.ScheduleStore(p)

    trace = _TraceConn().install(monkeypatch)
    st.list_all()

    offenders = [s for s in trace.statements if _WRITE_STMT_RE.search(s)]
    assert offenders == [], f"存量 v1 库读路径出现写语句: {offenders}"
    # 读路径确实查了库 (不是「压根没查」的空转假绿)
    assert any("SELECT" in s for s in trace.statements)


def test_v1_legacy_db_read_path_does_not_migrate(tmp_path):
    """F-1: 读路径**不触发迁移** —— 读完库结构与版本行保持原样。

    断言口径: 读完之后 finished_at 列仍不存在、schema_version 行仍不存在
    ⇒ 迁移确实没跑 (而不是「跑了但被回滚」)。
    """
    p = str(tmp_path / "v1.db")
    _make_v1_db(p)
    st = S.ScheduleStore(p)
    st.list_all()

    c = sqlite3.connect(p)
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(schedule_tasks)")}
        ver = c.execute("SELECT value FROM schedule_settings "
                        "WHERE key='schema_version'").fetchone()
    finally:
        c.close()
    assert "finished_at" not in cols, "读路径触发了迁移 (finished_at 列已加)"
    assert ver is None, "读路径触发了迁移 (schema_version 行已写)"


def test_v1_legacy_db_read_only_file_does_not_raise(tmp_path):
    """F-1: 只读库 (chmod 0444) 上 list_all() **不抛异常**。

    修复前: OperationalError attempt to write a readonly database
    (读路径上的迁移要写库)。修复后: 纯读 ⇒ 正常返回行。
    """
    p = tmp_path / "ro.db"
    _make_v1_db(p)
    os.chmod(str(p), 0o444)
    try:
        st = S.ScheduleStore(str(p))
        rows = st.list_all()
        assert [r["id"] for r in rows] == ["old-pend", "old-done"]
        # 其余读路径方法同样不得抛 (都走 _read_conn)
        head = st.head_pending()
        assert head is not None and head["id"] == "old-pend"
        assert st.has_pending() is True
        assert st.list_by_status("done")[0]["id"] == "old-done"
    finally:
        os.chmod(str(p), 0o600)      # 复原权限, 免得 tmp 清理失败


def test_v1_legacy_db_read_order_matches_updated_at_desc(tmp_path):
    """F-1 降级排序口径: 无 finished_at 列时终态组按 updated_at 降序。

    存量 v1 库里没有完成时刻列 ⇒ 只能退回 updated_at (最后一次可用近似
    完成时刻)。断言降级后终态组仍然「最近更新的在最上」, 且不崩。
    """
    p = tmp_path / "v1.db"
    c = sqlite3.connect(str(p))
    c.executescript(
        "CREATE TABLE schedule_tasks ("
        " id TEXT PRIMARY KEY, title TEXT NOT NULL, detail TEXT NOT NULL,"
        " priority TEXT NOT NULL DEFAULT 'normal',"
        " status TEXT NOT NULL DEFAULT 'pending',"
        " added_at REAL NOT NULL, executed_at REAL,"
        " added_by TEXT NOT NULL DEFAULT '',"
        " workflow TEXT NOT NULL DEFAULT 'auto',"
        " workspace TEXT NOT NULL DEFAULT '',"
        " session_id TEXT, run_id TEXT, last_error TEXT,"
        " updated_at REAL NOT NULL);"
        "CREATE TABLE schedule_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    c.execute("INSERT INTO schedule_settings(key, value) "
              "VALUES ('auto_execute_enabled', '1')")
    # 两个终态行: updated_at 分别是 500 与 900 ⇒ 降序应是 900 在前
    c.execute("INSERT INTO schedule_tasks(id, title, detail, status, added_at, "
              "updated_at) VALUES ('done-old','旧完成','x','done',100.0,500.0)")
    c.execute("INSERT INTO schedule_tasks(id, title, detail, status, added_at, "
              "updated_at) VALUES ('done-new','新完成','x','done',100.0,900.0)")
    c.execute("INSERT INTO schedule_tasks(id, title, detail, status, added_at, "
              "updated_at) VALUES ('pend','待执行','x','pending',200.0,200.0)")
    c.commit()
    c.close()

    st = S.ScheduleStore(str(p))
    rows = st.list_all()
    assert [r["id"] for r in rows] == ["pend", "done-new", "done-old"]


def test_write_path_still_migrates_legacy_db(tmp_path):
    """F-1 回归锁: **写路径**仍要迁移存量 v1 库 (读降级不等于放弃迁移)。

    MV-F-1: 把 _migrate_schema_on 去掉 ⇒ 本用例红 (列缺失 / 版本行缺失)。
    """
    p = tmp_path / "v1.db"
    _make_v1_db(p)
    st = S.ScheduleStore(str(p))
    st.ensure_schema()                    # 写连接 ⇒ 迁移

    c = sqlite3.connect(str(p))
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(schedule_tasks)")}
        ver = c.execute("SELECT value FROM schedule_settings "
                        "WHERE key='schema_version'").fetchone()
    finally:
        c.close()
    assert "finished_at" in cols, "写路径未迁移存量库"
    assert ver is not None and int(ver[0]) >= S.SCHEMA_VERSION


def test_migrated_db_read_uses_finished_at_desc(tmp_path):
    """F-1: 迁移到 v2 后, 终态组回到 finished_at 降序 (完成时刻口径)。

    与降级口径 (updated_at) 对照 ⇒ 证明排序键是**动态**选的, 不是写死。
    """
    p = tmp_path / "v1.db"
    _make_v1_db(p)
    st = S.ScheduleStore(str(p))
    st.ensure_schema()                    # 迁移到 v2

    c = sqlite3.connect(str(p))
    try:
        # 两个终态行, updated_at 顺序与 finished_at 顺序**相反** ⇒
        # 若排序键写死 updated_at, 断言必红。
        c.execute("UPDATE schedule_tasks SET finished_at=900.0, updated_at=500.0 "
                  "WHERE id='old-done'")
        c.execute("INSERT INTO schedule_tasks(id, title, detail, status, "
                  "added_at, finished_at, updated_at) "
                  "VALUES('old-done2','旧完成2','x','done',100.0,300.0,800.0)")
        c.commit()
    finally:
        c.close()

    rows = st.list_all()
    order = [r["id"] for r in rows]
    assert order.index("old-done") < order.index("old-done2"), \
        f"v2 库未按 finished_at 降序: {order}"


def test_zero_byte_db_file_still_auto_creates_schema(tmp_path):
    """F-1 边界: 0 字节空库文件仍走懒建 (AC-183-2 不得回归)。

    为什么要这一条: 判据若按「文件是否存在」, 0 字节文件 (首次建库被
    中断 / 部署侧 touch 出来的空文件) 会被误判成存量库 ⇒ 读路径查一张
    不存在的表 ⇒ OperationalError: no such table。
    """
    p = tmp_path / "empty.db"
    p.touch()
    st = S.ScheduleStore(str(p))
    assert st.list_all() == []
    assert st.get_auto_execute() is True      # 种子已播种


def test_drift_db_with_settings_table_dropped_still_self_heals(tmp_path):
    """F-1 边界: 漂移库 (schedule_settings 被删) 读路径仍自愈, 不抛异常。

    这是本卡在实现中实测到并修掉的一个**自造回归**: 懒建判据若只看
    「schedule_tasks 表在不在」, 则「settings 表被删」的漂移库会被当成
    存量库走只读降级 ⇒ get_auto_execute() 直接抛
    `OperationalError: no such table: schedule_settings`
    (基线行为是自愈: 懒建分支把 schema 补回来)。

    ⇒ 判据必须查**两个**核心表都在, 才认定「库是建好的」。
    """
    p = tmp_path / "drift.db"
    st = S.ScheduleStore(str(p))
    st.create("任务")
    c = sqlite3.connect(str(p))
    try:
        c.execute("DROP TABLE schedule_settings")
        c.commit()
    finally:
        c.close()

    st2 = S.ScheduleStore(str(p))               # 新实例 (重启视角)
    assert st2.get_auto_execute() is True      # 漂移自愈, 不抛
    assert len(st2.list_all()) == 1            # 数据没丢
