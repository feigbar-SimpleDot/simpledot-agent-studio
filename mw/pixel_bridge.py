"""像素办公室事件桥 — 极简扫描版 (2026-10-06 用户定调)。

「不要加太多逻辑和约束, 持续扫描状态机, 持续更新最新状态即可。」

唯一权威 = ledger:
  - team_work_runs.current_card_id → 谁在承接 (editing)
  - harvest_contracts.harvest_status 阻塞标记 → blocked
  - mw_events 流 → main 的唤醒节点 (task.stuck / harvest.*)

三态:
  editing = 有人承接任务 (worker assignee), 或 main 被敲门/harvest 唤醒
  blocked = 当前承接卡 harvest_status 落在 _BLOCK_STATUS
  idle    = worker 队列里非承接者

main 永远常驻工位 (idle_present 状态: 仅 SessionStart 上线, 不发 Stop
让角色「离座走动」), 被敲门/harvest 唤醒时进入 editing。

无 ACTIVE run, 或 current_card 为空 → worker 全 idle, main 仅在
刚刚被唤醒 (mw_events 30s 窗口内) 时进入 editing, 否则 idle。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request
from pathlib import Path

from . import pixel_proxy

_last_scene: dict[str, str] = {}    # 角色名 → 上轮场景
_online: set[str] = set()           # 已 SessionStart 的 session
_warned = False
_last_token: str | None = None     # 上轮 token (检测侧卡重启)

HOOK_URL_PATH = "/api/hooks/claude"

# 三场景 (worker):
#   editing    - 承接任务中
#   blocked    - 承接任务但 harvest_status 阻塞
#   idle       - 不在承接
# main 第四场景:
#   idle_present - 常驻工位 (SessionStart 一次, 不发 Stop, 永不刷屏)
_SCENE_EDITING = "editing"
_SCENE_BLOCKED = "blocked"
_SCENE_IDLE = "idle"
_SCENE_IDLE_PRESENT = "idle_present"   # main 专属

# harvest 阻塞标记 — 单线权威来源 (不是 kanban status)
# 边界减法 (2026-10-08): PERMISSION_REQUEST 全代码库无人写入, 死枚举已删。
_BLOCK_STATUS = {"NEED_CLARIFICATION", "BLOCKED", "ERROR"}

# worker「正在干活」的 harvest 状态 —— 只有 PENDING 意味着 worker 持有
# 当前卡且未交卷。其余状态 (HARVESTED/BLOCKED/NEED_CLARIFICATION/
# ARCHIVED) 都意味着球不在 worker 手上。
_WORKER_ACTIVE_STATUS = {"PENDING"}


def _overview(ledger):
    """D2 (issue #5, VERIFY-20261013): run 级单一派生优先。

    ledger 提供 derive_run_overview (真 Ledger) 时返回其结果, pixel 场景
    与 run 页 / worker 面板同源 (derive_worker_state/derive_run_overview);
    替身缺方法 / 查询异常返回 None = 回落本模块旧扫描逻辑 (fail-open,
    与 D1 同款 hasattr 前置, 防御不过宽)。
    """
    if not hasattr(ledger, "derive_run_overview"):
        return None
    try:
        run = ledger.conn.execute(
            "SELECT run_id FROM team_work_runs WHERE status='ACTIVE' LIMIT 1"
        ).fetchone()
        if not run:
            return None
        ov = ledger.derive_run_overview(run["run_id"])
        return ov or None
    except Exception:
        return None

# mw_events 中唤醒 main 的事件类型
# task.stuck   → reaper 敲门 (sleep 太久, 没人应答, 叫 main)
# task.session_hang → F9 挂死检测 (worker 流断了没交卷, main 决定 resume/reroute)
# harvest.*    → worker 落账 (COMPLETE / FAILED / clarify)
# resume.enqueued → resume 队列变化 (有人接回)
_MAIN_WAKE_EVENTS = {"task.stuck", "task.session_hang",
                    "harvest.COMPLETE", "harvest.complete",
                    "harvest.failed", "harvest.clarify",
                    "resume.enqueued"}


def _post_event(token: str, event: dict) -> bool:
    url = pixel_proxy.upstream_base() + HOOK_URL_PATH
    body = json.dumps(event).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def _ensure_online(token: str, sid: str) -> None:
    if sid in _online:
        return
    _post_event(token, {"hook_event_name": "SessionStart",
                        "session_id": sid, "source": "startup",
                        "cwd": f"/mw/workers/{sid}"})
    _online.add(sid)


def _emit(token: str, sid: str, prev: str | None, scene: str,
          title: str = "") -> None:
    """纯 diff + editing 重发。

    worker 三场景: editing / blocked / idle
    main 一场景: idle_present (首次 SessionStart, 之后不发任何事件,
              除非被唤醒进入 editing, 此时改走 editing 路径)
    """
    is_editing = scene == _SCENE_EDITING
    was_editing = prev == _SCENE_EDITING

    # 离开 editing 时先 PostToolUse 收尾打字
    if was_editing and not is_editing:
        _post_event(token, {"hook_event_name": "PostToolUse",
                            "session_id": sid, "tool_name": "Edit"})

    # editing 重发 PreToolUse, 保持实时观众打字动画
    if is_editing:
        _post_event(token, {"hook_event_name": "PreToolUse",
                            "session_id": sid, "tool_name": "Edit",
                            "tool_input": {"file_path": title or "task"}})
        return

    # main idle_present: 仅首次 SessionStart, 之后完全静默。
    # 注意: pixel-agents 的 "pending external session" 必须等一个
    # 确认事件 (Stop / Notification / PermissionRequest) 才会被
    # 创建为可见 agent。所以首次进入 idle_present 时, 在 SessionStart
    # 之后必须补一个 Notification(idle_prompt) 让 main 真正出现。
    # idle_prompt 不会触发离座走动 — Stop 才会。
    if scene == _SCENE_IDLE_PRESENT and prev != _SCENE_IDLE_PRESENT:
        _post_event(token, {"hook_event_name": "Notification",
                            "session_id": sid,
                            "notification_type": "idle_prompt"})
        return
    if scene == _SCENE_IDLE_PRESENT:
        return  # 已在 idle_present, 静默

    # blocked / idle 只在变化时发
    if prev == scene:
        return
    if scene == _SCENE_BLOCKED:
        _post_event(token, {"hook_event_name": "PermissionRequest",
                            "session_id": sid})
    elif scene == _SCENE_IDLE:
        _post_event(token, {"hook_event_name": "Stop",
                            "session_id": sid})


def _active_card(ledger) -> tuple[str | None, str, str]:
    """返回 (assignee, title, harvest_status) 或 (None, '', '')。"""
    try:
        run = ledger.conn.execute(
            "SELECT run_id, current_card_id FROM team_work_runs "
            "WHERE status='ACTIVE' LIMIT 1"
        ).fetchone()
    except Exception:
        return None, "", ""
    if not run or not run["current_card_id"]:
        return None, "", ""
    cur_id = run["current_card_id"]
    try:
        row = ledger.conn.execute(
            "SELECT assignee, title, harvest_status FROM harvest_contracts "
            "WHERE card_id=?", (cur_id,)
        ).fetchone()
    except Exception:
        return None, "", ""
    if not row:
        return None, "", ""
    return row["assignee"], (row["title"] or ""), (row["harvest_status"] or "")


def _kanban_card(hermes_home: str, task_id: str) -> tuple[str | None, str]:
    """兜底: harvest_contracts 没有 assignee/title 时读 kanban。"""
    if not task_id:
        return None, ""
    db = Path(hermes_home) / "kanban.db"
    if not db.exists():
        return None, ""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT assignee,title FROM tasks WHERE id=?",
                (task_id,)).fetchone()
        finally:
            con.close()
    except Exception:
        return None, ""
    if not row:
        return None, ""
    return row["assignee"], (row["title"] or "")


def _has_active_run(ledger) -> bool:
    """是否存在 ACTIVE team work run。

    串行不变量的判据之一: 有 ACTIVE run 就意味着这条链还活着,
    必须有人跟进。与「有没有当前卡」「worker 在不在干活」组合才能
    定出球在谁手上。
    """
    try:
        row = ledger.conn.execute(
            "SELECT 1 FROM team_work_runs WHERE status='ACTIVE' LIMIT 1"
        ).fetchone()
    except Exception:
        return False
    return row is not None


def _main_woken_recently(ledger, window_s: float = 30.0) -> bool:
    """main 刚被敲门/harvest 唤醒? 扫 mw_events 最近 30s 内的唤醒事件。"""
    cutoff = time.time() - window_s
    placeholders = ",".join("?" * len(_MAIN_WAKE_EVENTS))
    try:
        cur = ledger.conn.execute(
            f"SELECT 1 FROM mw_events WHERE event_type IN ({placeholders}) "
            f"AND created_at > ? LIMIT 1",
            (*_MAIN_WAKE_EVENTS, cutoff),
        ).fetchone()
    except Exception:
        return False
    return cur is not None


def sync_from_ledger(ledger, registry: dict,
                    hermes_home: str = "") -> int:
    """reaper 每轮调用。极简: 算 editing/blocked/idle, diff 注入。"""
    global _warned, _last_token
    if not registry:
        return 0
    token = pixel_proxy.pixel_office_token()
    if not token:
        if not _warned:
            print("[pixel_bridge] pixel-agents token 未找到, 跳过注入"
                  " (sidecar 未启动?)")
            _warned = True
        return 0
    _warned = False

    # token 变 = 侧卡重启, 清状态让角色重新上线
    if _last_token is not None and token != _last_token:
        _online.clear()
        _last_scene.clear()
    _last_token = token

    # ── 权威输入 ──
    assignee, title, hs = _active_card(ledger)
    if not assignee:
        # harvest_contracts 可能没存 assignee/title, 兜底读 kanban
        try:
            run = ledger.conn.execute(
                "SELECT current_card_id FROM team_work_runs "
                "WHERE status='ACTIVE' LIMIT 1"
            ).fetchone()
            cur_id = run["current_card_id"] if run else None
        except Exception:
            cur_id = None
        if cur_id:
            assignee, title = _kanban_card(hermes_home, cur_id)

    blocked = bool(assignee) and hs in _BLOCK_STATUS
    # 串行不变量 (2026-10-08, 用户定调): 「永远有人在跟进」。
    # 有 ACTIVE run 但 worker 没在干活 (harvest_status 不是 PENDING,
    # 或根本没有 assignee) → 球在 main 手上 → main 必须在处理。
    # 旧逻辑只看「30s 内有没有唤醒事件」, 会造出「run 活着但没人
    # 跟进」的假 idle —— 那正是流程中断, 而中断是 main 的职责。
    has_active_run = _has_active_run(ledger)
    worker_active = bool(assignee) and hs in _WORKER_ACTIVE_STATUS
    # D2 单一派生覆盖 (issue #5): 真 Ledger 场景下 worker 是否「在干活」/
    # 「受阻」/ run 是否活着, 改读 derive_run_overview —— 与 run 页
    # (api_team_work_get 注入 state) 逐字同一派生。典型分歧: 承接卡被
    # reaper 标 WAITING 后 hs 仍在 _WORKER_ACTIVE_STATUS 之外的口径差异,
    # 以及 handoff 窗口 (holder_state=HANDOFF → 球不在 worker)。
    ov = _overview(ledger)
    if ov is not None:
        has_active_run = bool(ov.get("has_active_run"))
        holder_state = ov.get("holder_state") or ""
        # WAITING = reaper 判闲置等 main 介入, 语义等同「受阻举手」
        # (PermissionRequest 场景), 与 run 页「等介入」同口径。
        # issue#13 (2026-10-15): WAITING_SUB 也属 worker 忙碌族 (球
        # 仍在 worker 不在 main), 不应被当「受阻举手」. 仅 WAITING/
        # NEED_CLARIFICATION/BLOCKED/ERROR 走 blocked 路径.
        blocked = (holder_state in _BLOCK_STATUS
                   and holder_state != "WAITING_SUB") \
                   or holder_state == "WAITING"
        # WAITING_SUB 与 WORKING 同样: worker 持球, 像素动画 editing.
        worker_active = holder_state in ("WORKING", "WAITING_SUB")
    main_woken = _main_woken_recently(ledger)
    main_owns_ball = has_active_run and not worker_active

    # ── 角色场景 ──
    # worker 三态: 承接者 = editing (或 blocked), 其余 idle
    # main: 球在手上 (有 run 无 worker 活动) 或被唤醒 = editing,
    #       worker 正在干活时 = idle_present 待命
    scenes: dict[str, str] = {}
    for name in registry:
        if name == assignee:
            scenes[name] = _SCENE_BLOCKED if blocked else _SCENE_EDITING
        else:
            scenes[name] = _SCENE_IDLE
    scenes["main"] = (_SCENE_EDITING if (main_owns_ball or main_woken)
                      else _SCENE_IDLE_PRESENT)

    # ── 注入 ──
    # session_id 直接用角色名 (coder / main / ...) — 我们的 session
    # 都是 hooks-only 注入, 不会撞真实 Claude session; pixel-agents
    # 会把这个 id 显示为角色头顶的标签, 加 mw- 前缀会污染 UI。
    changes = 0
    for name, scene in scenes.items():
        sid = name
        prev = _last_scene.get(name)
        _ensure_online(token, sid)
        if scene != prev or scene == _SCENE_EDITING:
            t = title if name == assignee else ""
            _emit(token, sid, prev, scene, t)
            if scene != prev:
                changes += 1
        _last_scene[name] = scene

    if changes and os.environ.get("MW_PIXEL_BRIDGE_DEBUG"):
        print(f"[pixel_bridge] diag changes={changes} assignee={assignee} "
              f"hs={hs or '-'} blocked={blocked} main_woken={main_woken}")
    return changes


def tick(ledger, registry_path: str | None = None,
         hermes_home: str = "") -> int:
    reg_path = registry_path or os.environ.get(
        "MW_REGISTRY", "/mw_data/registry.json")
    try:
        registry = json.loads(Path(reg_path).read_text())
    except (OSError, json.JSONDecodeError):
        return 0
    return sync_from_ledger(ledger, registry, hermes_home)
