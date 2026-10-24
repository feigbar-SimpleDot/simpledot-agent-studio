"""实时事件层 (fsnotify 主路径, 2026-10-04 引入).

设计动机 (design.md §5.0):
  原 reaper 是 15s 轮询 + 30min stuck 兑底, 不可能实时。worker 完成通知延迟
  0-15s (轮询窗口), 卡死发现要等 30min。LLM 自己开 watchdog 反而更快这件事
  暴露了根本缺陷: 实时通知不能靠轮询。

架构:
  - fsnotify 订阅 <project_workspace>/TODO.md mtime (ms 级)
  - on_modified → 调和 ledger → 调 _reap_one_task → 调 _notify_main_of_reports
  - 去重靠 fsnotify 事件本身 (每次 mtime 变化唯一一次)
  - 旧 15s run_loop 保留作兑底 (fsnotify 漏事件 / 文件重建修复)
  - stuck 兑底阈值 30min → 5min

不动 reaper.py 的 reap_once 逻辑, 只是新增实时事件源; harvest/relay 复用
reaper.py 的现有函数 (_reap_one_task / _notify_main_of_reports), 不重复
实现。

依赖:
  - watchdog >= 4.0 (Linux inotify / macOS FSEvents / Windows ReadDirectoryChangesW)
  - 在 deploy/install.sh 镜像构建阶段安装: pip install watchdog

边界 (1A 2026-10-03):
  - mw 不改 kanban, 不决策, 不写项目文件 (除 TODO.md 状态字段 worker 自己写)
  - relay 通知 main: 真收到 HTTP 200 才 mark_notified (修 commit 2 同步上)
  - fsnotify 事件 = 唯一性去重, should_notify 不再做 120s 闸门
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("mw.realtime")
# 2026-10-04: 配 stdout handler, 让 [fsnotify] / [event-processor] 实时事件
# 进 docker logs。之前 log.info 因无 handler + 默认 WARNING 级被静默丢弃,
# 导致实时路径不可观测 (无法从日志证明 fsnotify 是否触发、延迟多少)。
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

# 事件队列: fsnotify Observer 线程 → 后台 harvest/relay 处理线程。
# 用 queue.Queue 而不是直接调 harvest (避免 fsnotify 线程被 harvest 同步 IO
# 拖慢, fsnotify 事件必须尽快入队, 处理可以异步)。
_event_queue: "queue.Queue[FsEvent]" = queue.Queue(maxsize=1024)


@dataclass
class FsEvent:
    """fsnotify 事件载荷."""
    task_id: str              # 这条事件归属的 task
    run_id: str               # 归属的 team work run
    project_workspace: str    # 事件源 (TODO.md 完整路径)
    mtime: float              # 文件 mtime (epoch sec)
    detected_at: float = field(default_factory=time.time)


# 当前 ACTIVE run 的 fsnotify 订阅状态 (单例, run 切换时换 watcher)
_state_lock = threading.Lock()
_current_watcher: Optional["TodoMdWatcher"] = None
_current_run_id: Optional[str] = None


class TodoMdWatcher:
    """watchdog FileSystemEventHandler 包装: 订阅 <project_workspace>/TODO.md
    mtime, 把事件入 _event_queue。"""

    def __init__(self, project_workspace: str, run_id: str,
                 ledger_db: Optional[str] = None):
        from watchdog.events import FileSystemEventHandler  # type: ignore
        self._project_workspace = project_workspace
        self._run_id = run_id
        self._ledger_db = ledger_db
        self._todo_path = Path(project_workspace) / "TODO.md"
        # mtime 去重: watchdog 在某些 fs (macOS FSEvents) 会触发两次
        # coalesced 事件, 同一 mtime 第二次直接跳过
        self._last_seen_mtime: dict[str, float] = {}
        self._handler = self._make_handler()

    def _make_handler(self):
        # 注意: FileSystemEventHandler 必须在 _make_handler 作用域内 import。
        # 它只在 __init__ 里 import 过是局部名, 嵌套类定义引用它会 NameError
        # (2026-10-04 170 实测: "name 'FileSystemEventHandler' is not defined")。
        from watchdog.events import FileSystemEventHandler  # type: ignore
        outer = self

        class _Handler(FileSystemEventHandler):
            # 三路事件全接: worker 的 write_file 是原子写 (write temp +
            # os.replace), 内核发 IN_MOVED_TO → watchdog 报 FileMovedEvent,
            # 不是 FileModifiedEvent。只实现 on_modified 会漏掉 worker 的
            # 全部写入 (2026-10-04 170 实测: watcher 挂上、TODO.md 被改,
            # 但 on_modified 一次没触发)。on_created 兜首次落盘。
            def on_modified(self, event):
                if not event.is_directory:
                    outer._process_path(event.src_path)

            def on_created(self, event):
                if not event.is_directory:
                    outer._process_path(event.src_path)

            def on_moved(self, event):
                # 原子替换: src_path 是 temp, dest_path 才是 TODO.md
                if not event.is_directory:
                    outer._process_path(getattr(event, "dest_path", None))

        return _Handler()

    def start(self):
        """启动 watchdog Observer 订阅 project_workspace."""
        from watchdog.observers import Observer  # type: ignore
        # 目录可能尚未创建 (worker 还没写 TODO.md)。watchdog 订阅不存在的目录
        # 会 OSError: No such file or directory (2026-10-04 170 实测)。
        # 先 mkdir -p: 这是 worker 马上要写入的项目工作区, 建空目录无害且必需。
        try:
            Path(self._project_workspace).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("[fsnotify] mkdir %s failed: %s", self._project_workspace, e)
            return None
        obs = Observer()
        obs.schedule(self._handler, self._project_workspace, recursive=False)
        obs.daemon = True
        obs.start()
        self._observer = obs
        log.info("[fsnotify] watching %s for run %s",
                 self._project_workspace, self._run_id)
        return obs

    def stop(self):
        if hasattr(self, "_observer"):
            self._observer.stop()
            self._observer.join(timeout=2)

    def _process_path(self, path_str):
        """三路 fsnotify 事件 (modified/created/moved) 的统一处理入口。

        过滤到 TODO.md → mtime 去重 → 解析路由 task_id → 入队。
        """
        if not path_str:
            return
        src = Path(path_str)
        if src.name != "TODO.md":
            return
        if not src.exists():
            return
        try:
            mtime = src.stat().st_mtime
        except OSError:
            return
        # 同 mtime 去重 (FSEvents coalesce / 同一原子写的多路事件)
        last = self._last_seen_mtime.get(str(src))
        if last is not None and abs(last - mtime) < 0.001:
            return
        self._last_seen_mtime[str(src)] = mtime
        # 解析要结算的 task_id: 优先 ledger.current_card_id (确定性映射),
        # 兜底 TODO.md 的 kanban_task 字段。旧版只读 kanban_task 魔法字段,
        # worker 不写 → 事件全丢 → 退回 15s 轮询。
        task_id = self._resolve_current_card() or _read_current_kanban_task(src)
        if not task_id:
            log.info("[fsnotify] %s changed but no current card resolvable, skip",
                     src)
            return
        # F8 (2026-10-08): worker 改 TODO.md = 真实进展信号, 必须在事件
        # 入队前 touch_progress。否则 reaper 仍按 marker.started_at 兜底
        # 「挂 N 分钟」=「spawn 后 N 分钟」, 哪怕 worker 几秒前才改过 TODO。
        if self._ledger_db:
            try:
                from .ledger import Ledger
                _led = Ledger(self._ledger_db)
                try:
                    _led.touch_progress(task_id)
                finally:
                    _led.close()
            except Exception:
                pass  # best-effort, 不影响事件入队
        ev = FsEvent(task_id=task_id, run_id=self._run_id,
                     project_workspace=str(src), mtime=mtime)
        try:
            _event_queue.put_nowait(ev)
            log.info("[fsnotify] enqueued task_id=%s mtime=%.3f", task_id, mtime)
        except queue.Full:
            log.warning("[fsnotify] queue full, dropping event for %s", task_id)

    def _resolve_current_card(self) -> Optional[str]:
        """从 ledger 读「当前唯一 ACTIVE run 的 current_card_id」—— fsnotify 路由锚点。

        注意: 不按 watcher 绑定的 self._run_id 过滤。因为 run 切换时 watcher
        重挂有最多 15s 延迟 (sync_watcher 每轮跑), 期间 self._run_id 是 stale
        的旧 run, 用它过滤会查不到 → 事件被误 skip (2026-10-04 170 实测:
        新 run launch 后 touch TODO.md, fsnotify 捕获到但路由到旧 run skip)。
        ledger 有 partial UNIQUE INDEX 保证同时只有一个 ACTIVE run, 所以直接
        查 status='ACTIVE' 的 current_card_id 就是此刻该结算的卡, 无歧义。

        用短连接 (每次事件新建), 避免跨线程共享 sqlite 连接的并发问题;
        WAL 模式下读是 ms 级 (实测 fsnotify 捕获延迟 ~8ms), 不影响实时性。
        """
        if not self._ledger_db:
            return None
        try:
            import sqlite3
            c = sqlite3.connect(self._ledger_db, timeout=5)
            try:
                row = c.execute(
                    "SELECT current_card_id FROM team_work_runs "
                    "WHERE status='ACTIVE' LIMIT 1").fetchone()
                return row[0] if row else None
            finally:
                c.close()
        except Exception as e:
            log.debug("[fsnotify] resolve current card failed: %s", e)
            return None


def _read_current_kanban_task(todo_md: Path) -> Optional[str]:
    """从 TODO.md『当前任务』段读 kanban_task 字段 (worker → fsnotify 路由)."""
    try:
        text = todo_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    in_current = False
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("## "):
            in_current = (s[3:].strip() == "当前任务")
            continue
        if not in_current:
            continue
        if ln.lstrip().startswith("- kanban_task") or ln.lstrip().startswith("- kanban_task"):
            kv = ln.split(":", 1)
            if len(kv) == 2:
                return kv[1].strip() or None
    return None


def sync_watcher(ledger, hermes_home: str, base: str,
                 registry: Optional[dict] = None) -> Optional[TodoMdWatcher]:
    """把 fsnotify 订阅同步到当前 ACTIVE run。

    调用时机:
      - run_loop 启动时 (fsnotify 接管前的初始状态)
      - 每轮 reap_once 后 (run 切换 / 关闭 → 重挂 watcher)

    行为:
      - 取 ledger.get_active_team_work()
      - 若 run_id / project_workspace 与当前 watcher 一致 → 不动
      - 否则: 停旧 watcher, 起新 watcher (新 run / 新 workspace)
      - 若无 ACTIVE run → 停旧 watcher, 返回 None (T3: 防误激活)

    不抛异常: watcher 启动失败时 log + 返回 None, run_loop 不挂。
    """
    global _current_watcher, _current_run_id
    try:
        active = ledger.get_active_team_work()
    except Exception as e:
        log.warning("[fsnotify] ledger.get_active_team_work failed: %s", e)
        return _current_watcher

    target_run = active["run_id"] if active else None
    target_ws = (active or {}).get("project_workspace")

    with _state_lock:
        if target_run == _current_run_id and _current_watcher is not None:
            return _current_watcher
        # 切换: 停旧
        if _current_watcher is not None:
            try:
                _current_watcher.stop()
            except Exception as e:
                log.warning("[fsnotify] stop old watcher failed: %s", e)
            _current_watcher = None
            _current_run_id = None
        # 无 ACTIVE run → 不挂新 watcher
        if not target_run or not target_ws:
            log.info("[fsnotify] no active run, watcher cleared")
            return None
        # 起新
        try:
            w = TodoMdWatcher(target_ws, target_run,
                             ledger_db=getattr(ledger, "path", None))
            w.start()
            _current_watcher = w
            _current_run_id = target_run
            return w
        except Exception as e:
            log.warning("[fsnotify] start watcher for %s failed: %s",
                        target_ws, e)
            return None


def event_processor_loop(*, ledger, hermes_home: str, base: str,
                        grace_seconds: int = 120,
                        registry: Optional[dict] = None,
                        stop_event: Optional[threading.Event] = None):
    """守护线程: 从 _event_queue 取 fsnotify 事件 → 调 harvest → 调 relay。

    这是 ms 级实时路径, 与 reaper.py 的 15s 轮询 reap_once 并行存在。
    两者各跑各的: fsnotify 路径吃绝大多数事件, 轮询只兑底 fsnotify 漏触发。
    """
    from .reaper import _reap_one_task, _notify_main_of_reports
    if stop_event is None:
        stop_event = threading.Event()
    log.info("[event-processor] started")
    while not stop_event.is_set():
        try:
            ev = _event_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            _handle_event(ev, ledger, hermes_home, base, grace_seconds, registry)
        except Exception as e:
            log.exception("[event-processor] handle %s failed: %s",
                          ev.task_id, e)
    log.info("[event-processor] stopped")


def _handle_event(ev: FsEvent, ledger, hermes_home: str, base: str,
                 grace_seconds: int, registry: Optional[dict]):
    """处理单条 fsnotify 事件: 找对应 harvest_contracts 行 → reap → relay.

    ★ 2026-10-04 根因修复: 本函数跑在 event_processor 独立线程, 而传入的
    `ledger` 是主线程创建的。SQLite 连接不能跨线程用 (ProgrammingError:
    SQLite objects created in a thread can only be used in that same thread),
    导致**每个 fsnotify 事件都崩**, 全部退回 15s 轮询 —— 这才是「reaper
    不实时」在生产里一直没真正跑通的根因 (170 全管道隔离测试实测复现)。
    修法: 在本线程内用 ledger.path 开独立连接, 传给下游 reap/relay,
    用完关闭。WAL 模式下多线程各自连接是安全的。
    """
    from .reaper import _reap_one_task, _notify_main_of_reports
    from .ledger import Ledger
    db_path = getattr(ledger, "path", None)
    if not db_path:
        log.warning("[event-processor] ledger 无 path, 无法开线程内连接")
        return
    led = Ledger(db_path)
    try:
        # 只按 task_id 查, 不按 run_id 过滤 (ev.run_id 可能 stale)。
        row = led.conn.execute(
            "SELECT * FROM harvest_contracts WHERE task_id=?",
            (ev.task_id,)).fetchone()
        if row is None:
            log.debug("[event-processor] %s not in ledger, skip", ev.task_id)
            return
        if row["harvest_status"] not in ("PENDING", "ERROR", "BLOCKED"):
            # 已结算完 (HARVESTED/NEED_CLARIFICATION/...), fsnotify 只是回放
            log.debug("[event-processor] %s already %s, skip",
                      ev.task_id, row["harvest_status"])
            return
        results: list = []
        _reap_one_task(row, led, hermes_home, base, grace_seconds, registry,
                       results)
        for r in results:
            log.info("[event-processor] %s → %s %s",
                     ev.task_id, r.get("trigger"), r.get("verdict"))
        # 立即中继 (不依赖 15s 轮询)
        _notify_main_of_reports(led, results, registry=registry)
        for r in results:
            if r.get("trigger") == "relay":
                log.info("[event-processor] %s relay %s",
                         ev.task_id, r.get("verdict"))
    finally:
        led.close()


def start_realtime(*, ledger, hermes_home: str, base: str,
                   grace_seconds: int = 120,
                   registry: Optional[dict] = None,
                   stop_event: Optional[threading.Event] = None
                   ) -> tuple[threading.Thread, Optional[TodoMdWatcher]]:
    """一键启动 fsnotify 主路径: 同步 watcher + 启动 event_processor 线程。

    返回 (processor_thread, watcher_or_None)。
    caller 持有 stop_event, 触发 set() 时两线程都干净退出。
    """
    if stop_event is None:
        stop_event = threading.Event()
    watcher = sync_watcher(ledger, hermes_home, base, registry)
    proc = threading.Thread(
        target=event_processor_loop,
        kwargs=dict(ledger=ledger, hermes_home=hermes_home, base=base,
                    grace_seconds=grace_seconds, registry=registry,
                    stop_event=stop_event),
        name="mw-event-processor", daemon=True)
    proc.start()
    return proc, watcher
