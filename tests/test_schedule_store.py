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
    a = _mk(store, "A")
    time.sleep(0.01)
    b = _mk(store, "B")
    time.sleep(0.01)
    c = _mk(store, "C")
    assert store.head_pending()["id"] == c["id"]       # added_at DESC
    store.pause(c["id"])                               # 暂停天然不在 pending 集
    assert store.head_pending()["id"] == b["id"]
    assert store.take_head_pending()["id"] == b["id"]
    assert store.take_head_pending()["id"] == a["id"]
    assert store.take_head_pending() is None


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
    a = _mk(store, "A")
    b = _mk(store, "B")
    c = _mk(store, "C")
    d = _mk(store, "D")
    store.take_head_pending()          # 队首 (d) → running
    store.mark_done(d["id"])           # d → done
    store.cancel(c["id"])              # c → cancelled
    store.pause(b["id"])               # b → paused
    order = [t["id"] for t in store.list_all()]
    # 组0 running(无) / 组1 pending+paused 组内 added_at DESC: b(新),a /
    # 组2 done+cancelled 组内 added_at DESC: d(最新,done),c(cancelled)
    assert order == [b["id"], a["id"], d["id"], c["id"]]


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