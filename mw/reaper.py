"""reaper (TODO.md 状态字段版) — 边界 (2026-10-03):

reaper 的工作是 **"摆事实 + 敲门"**:
  1. 扫 PENDING 任务的 TODO.md『状态』字段 → 调 harvest 写报告
     (harvest 是被动函数, reaper 是主动触发点)
  2. 给挂太久的任务发"通知 main"的信号 (main 决定怎么办)
  3. 消费 resume 队列 (main→worker 续作通道)
  4. 心跳 (供 dashboard 判断存活)

不做的事:
  - ❌ 不调 cmd_claim_and_spawn 推链 (链推进是 main 的事)
  - ❌ 不调 hermes kanban complete / block / unblock (改 kanban 是 main 的事)
  - ❌ 不替 main 决策 ("这张卡要不要换 worker" 由 main 决定)
  - ❌ 不调 hermes gateway HTTP API 反查 session 状态 (2026-10-03 移除)
       原因: HTTP /chat 返回成功 ≠ worker 真干完; HTTP 返回超时 ≠
       worker 没干活。session 是 LLM turn 模型, 不是进程退出模型,
       用 HTTP 信号做结算会误判。改读 fs 上的 TODO.md 状态字段,
       是物理证据。

物理证据路径 (2026-10-03):
  - worker 在 TODO.md『当前任务』→『状态』字段写 working / blocked /
    completed / failed, atomic rename 避免半截写入
  - reaper 每 15s 扫 PENDING 卡的 workspace/TODO.md, 按状态决策:
      completed  → 立即 harvest (verdict=COMPLETE)
      failed     → 立即 harvest (verdict=FAILED, worker 自己的决策)
      blocked    → 立即 harvest (verdict=NEED_CLARIFICATION)
                   (写序铁律: worker 先落 .clarify.json 再翻 blocked,
                    两者原子产生; clarify 缺失也兜底结算, 不干等)
      working    → 不动 (在干)
      进行中 / None → 不动 (worker 还没起手 / TODO.md 还没建好)
  - 兜底: 状态一直是 working 但 marker.started_at 超过 timeout+grace →
          平台收尸 (verdict=FAILED, reason=TIMEOUT, 非 worker 决策)
  - 边界减法 (2026-10-08): PHYSICAL_ERROR / PASS_BUMPED 已删。物理验收
    只作 worker_report.physical_check 证据, 不推翻 worker 的 completed;
    loop 与否由 main 编排, mw 不自启同卡重跑。

不变式:
  - marker.exit_code 字段保留 (审计), 但 reaper 决策不依赖它
  - reaper 不调 hermes gateway HTTP API (session_msg_count /
    _lookup_session_via_api / _cross_check_session 全删)
  - F6 (2026-10-05 重写, SSE-FIX-F3 2026-10-08 再改): 敲门活体抑制
    改为向同容器 console 的 GET /api/workers/live 查一次 (timeout 2s),
    thinking==True 或事件新鲜 → 抑制; console 不可达/无条目 → 照敲
    (fail-open)。无 ledger 写入 / 无状态机。旧的跨进程 sse_bridge
    内存表直读 (F6 v9) 已删 —— reaper 与 SSE 桥不同进程, 内存不共享。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .harvest import harvest
from .i18n import t as _t
from .ledger import Ledger
from .todo import (
    parse_status, normalize_status, IN_PROGRESS_SET, TERMINAL_STATUSES,
    STATUS_COMPLETED, STATUS_FAILED, STATUS_BLOCKED,
    STATUS_WORKING, STATUS_WAITING_SUB,
)


def marker_path(base: str, task_id: str) -> Path:
    return Path(base) / "spawn" / f"{task_id}.json"


def write_marker(base: str, task_id: str, **fields):
    p = marker_path(base, task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    old = {}
    if p.exists():
        try:
            old = json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    old.update(fields)
    p.write_text(json.dumps(old))


def read_marker(base: str, task_id: str):
    p = marker_path(base, task_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def _todo_path_for_task(workspace: str | None, ledger_row=None,
                        contract: dict | None = None) -> str | None:
    """解析任务对应的 TODO.md 路径。

    布局约定 (1A, 2026-10-03):
      - 项目任务: TODO.md 在 **项目根** <project>/TODO.md;
        task workspace 是 <project>/.tasks/<task_id> (worker scratch)。
        → 从 /.tasks/ 切出项目根, TODO.md 在项目根。
      - 非项目任务: workspace 即任务目录, TODO.md 在 workspace 根。
      - 兜底: contract.product_workspace/TODO.md。
    """
    if not workspace:
        return None
    # 项目任务: workspace 形如 <project>/.tasks/<task_id>
    # TODO.md 在项目根 (worker 改的就是项目根那份), 不在 .tasks 子目录
    if "/.tasks/" in workspace:
        project_root = workspace.split("/.tasks/")[0]
        return str(Path(project_root) / "TODO.md")
    if workspace.rstrip("/").endswith("/tasks"):
        # /share_workspace/tasks/<id> 平铺: TODO.md 在 workspace 根
        return str(Path(workspace) / "TODO.md")
    # project workspace 直接传入 (无 .tasks 段): workspace 根即项目根
    p = Path(workspace) / "TODO.md"
    if p.exists():
        return str(p)
    # 兜底: contract 里的 product_workspace
    if contract:
        pw = contract.get("product_workspace")
        if pw:
            return str(Path(pw) / "TODO.md")
    return str(p)


def _read_todo_status(workspace: str | None, ledger_row=None,
                     contract: dict | None = None) -> str | None:
    """读 TODO.md 当前任务的状态字段。

    返回:
      str  - 找到的状态值 (working / completed / blocked / failed / 进行中 / 自定义)
      None - TODO.md 不存在 / 没有状态字段 / 解析失败
    """
    p = _todo_path_for_task(workspace, ledger_row, contract)
    if not p:
        return None
    return parse_status(p)


def _reap_one_task(row, ledger, hermes_home, base, grace_seconds, registry, results):
    """结算单卡: 读 TODO.md 状态字段 → 决策 → 触发 harvest。

    决策表 (2026-10-03 重写):
      TODO.md 状态 = completed  → 立即 harvest, spawn_result=None
                                  (不依赖 HTTP exit_code)
      TODO.md 状态 = failed     → 立即 harvest, spawn_result=None
      TODO.md 状态 = blocked
        + .clarify.json 存在     → 立即 harvest, verdict=NEED_CLARIFICATION
      TODO.md 状态 = blocked
        + .clarify.json 不在     → 仍结算 NEED_CLARIFICATION (兜底占位)
      TODO.md 状态 = working /
                    进行中       → 检查 marker.started_at
                                    超 timeout → 平台收尸 (FAILED/TIMEOUT)
                                    未超 → 不动
      TODO.md 不存在 / 状态 None → 不动 (worker 还没起手或不是 mw 模板)

    Issue #9 (#9 假警报型 c 0-tool-calls 陈旧 COMPLETE, s2 守门):
      completed / failed 路径在落 harvest 之前追加两道前置验证:
        1) 必须存在本卡 spawn marker (write_marker 写过 started_at);
           没有 marker 的卡按「未真正派发」对待, 不替 worker 决策。
           排除: 1) 上轮卡 TODO.md 残留被当前 harvest 当成已完工;
                2) 0 工具调用的 false COMPLETE 落账回路。
        2) marker 记录的 task_id 必须 == 本 task_id (防 marker 串扰,
           旧 marker 复用跨 task_id 误判)。
      任一不满足 → SKIP_STALE_TODO, 不调 harvest, 不敲 main。
    """
    task_id = row["task_id"]
    contract = json.loads(row["contract_json"])
    sess = ledger.get_session(task_id)
    marker = read_marker(base, task_id)
    ws = (sess["workspace"] if sess and sess["workspace"]
          else (marker or {}).get("workspace"))
    if not ws:
        return

    # 1) 读 TODO.md 状态 (物理证据)
    todo_status = _read_todo_status(ws, ledger_row=row, contract=contract)
    # issue #124: 状态语义统一经 normalize_status 归一化, 不再本地 lower()
    # + 字面量集合。未识别 → None, 走下方"未写状态"兜底路径 (语义不变)。
    norm = normalize_status(todo_status)
    # waiting_sub (等子任务) 单独识别: 独立阈值 + 专属敲门文案 (issue#13)。
    is_waiting_sub = norm == STATUS_WAITING_SUB
    sub_min = _subagent_wait_min()
    sub_enabled = sub_min > 0

    # 2) 状态驱动的结算决策
    spawn_result = None  # 默认 None, 物理收尸 / 状态驱动都不依赖 exit_code

    # ★ s2 (issue #9): 假警报型 (c) 「0-tool-calls 陈旧 COMPLETE」的守门。
    # 强保护: marker 存在但记录的 task_id 与本卡不一致 → 拒绝落 harvest,
    # 防陈旧快照让 main 收到虚假 COMPLETE。
    # 弱判定 (marker 完全缺失) 不在此拦截 —— 「未 spawn 的卡」下方 L340+ 已有
    # 独立的 no_marker SKIP 分支, 把判断移到这里会与既有回归锁串, 误拦测试。
    # 单纯 marker 缺失但 TODO.md 已 completed: 由现有 completed 分支直接判
    # 走 harvest, 与既有行为保持一致; 陈旧问题由下方 marker.task_id 兜底。
    def _stale_marker_guard() -> tuple:
        """返回 (ok, reason)。ok=False 时 completed/failed 路径应 SKIP。"""
        if marker is not None:
            mid = marker.get("task_id")
            if mid and mid != task_id:
                return False, f"marker_task_mismatch:{mid}"
        return True, ""

    def _clear_subagent_wait_stamp() -> None:
        """issue#13: worker 离开 waiting_sub (改 working/completed/failed/
        blocked) → 清 subagent_wait_since 戳, 避免 derive_worker_state
        滞留展示 WAITING_SUB. 与 _reap_one_task 状态分支解耦, 一律幂等
        UPDATE. 失败不抛 (现状 root cause 不因此失败)。
    """
        try:
            ledger.conn.execute(
                "UPDATE harvest_contracts SET subagent_wait_since=NULL "
                "WHERE task_id=? AND subagent_wait_since IS NOT NULL",
                (task_id,))
            ledger.conn.commit()
        except Exception:
            pass

    if norm == STATUS_COMPLETED:
        ok, why = _stale_marker_guard()
        if not ok:
            results.append({"task_id": task_id,
                           "trigger": "todo_completed",
                           "verdict": "SKIP_STALE_TODO",
                           "note": (f"TODO.md 状态={todo_status} 但 {why}, "
                                   "拒绝假 COMPLETE 落账")})
            return
        trigger = "todo_completed"
        # 状态明确, 不需要 exit_code。worker 自报 completed 是权威,
        # harvest 只把它转成 verdict=COMPLETE + 附产物证据。
        _clear_subagent_wait_stamp()  # issue#13: 离开 waiting_sub 清戳
        r = harvest(task_id, ledger=ledger, hermes_home=hermes_home,
                   workspace=ws, registry=registry, spawn_result=None,
                   worker_status="completed")
        results.append({"task_id": task_id, "trigger": trigger,
                       "verdict": r.get("verdict"),
                       "note": f"TODO.md 状态={todo_status}"})
        return

    if norm == STATUS_FAILED:
        # s2 (issue #9): failed 同 completed 一样有陈旧 TODO 假信号风险,
        # 共享 _stale_marker_guard。
        ok, why = _stale_marker_guard()
        if not ok:
            results.append({"task_id": task_id,
                           "trigger": "todo_failed",
                           "verdict": "SKIP_STALE_TODO",
                           "note": (f"TODO.md 状态={todo_status} 但 {why}, "
                                   "拒绝假 FAILED 落账")})
            return
        trigger = "todo_failed"
        # worker 自报 failed 是它自己的决策 → harvest 转 verdict=FAILED
        _clear_subagent_wait_stamp()  # issue#13: 离开 waiting_sub 清戳
        r = harvest(task_id, ledger=ledger, hermes_home=hermes_home,
                   workspace=ws, registry=registry, spawn_result=None,
                   worker_status="failed")
        results.append({"task_id": task_id, "trigger": trigger,
                       "verdict": r.get("verdict"),
                       "note": f"TODO.md 状态={todo_status}"})
        return

    if norm == STATUS_BLOCKED:
        # 边界减法 (2026-10-08): worker prompt 已强制「先落 .clarify.json
        # 再翻 blocked」(pipeline.py 写序铁律), blocked 与 clarify 原子
        # 产生 —— 不再存在「blocked 但 clarify 未落盘」的复合态, 旧的
        # WAIT-等下轮分支已删。blocked 直接结算为 NEED_CLARIFICATION,
        # 球交 main (串行不变量: 永远有人跟进)。
        # clarify 文件万一缺失 (worker 违约), 兜底占位问题, 不再干等。
        trigger = "todo_blocked"
        _clear_subagent_wait_stamp()  # issue#13: 离开 waiting_sub 清戳
        r = harvest(task_id, ledger=ledger, hermes_home=hermes_home,
                   workspace=ws, registry=registry, spawn_result=None,
                   worker_status="blocked")
        results.append({"task_id": task_id, "trigger": trigger,
                       "verdict": r.get("verdict"),
                       "note": f"TODO.md 状态={todo_status}"})
        return

    if norm in IN_PROGRESS_SET:
        # 在干活. 检查超时.
        started = (marker or {}).get("started_at", 0) if marker else 0
        now = time.time()
        timeout = contract.get("timeout_seconds", 1800)
        # M2 (BATCH1-DESIGN): 超时锚点 = max(started_at, last_progress_at)
        # —— worker 任何可见进展 (改 TODO / touch_progress) 都顺延超时
        # 基准, 消除「挂 N 分钟 = spawn 后 N 分钟」的旧口径 (F8 遗留)。
        lpa = _row_get(ledger.get_contract(task_id), "last_progress_at") or 0
        eff_started = max(started, lpa)
        # issue#13: waiting_sub 卡独立等待阈值 —— 超阈才落平台收尸,
        # 默认 60min 给 main resume 留窗口; 收尸线 = max(原 timeout,
        # 2×SUB_MIN), 即超阈敲门先于收尸发生 (§5).
        if is_waiting_sub and sub_enabled:
            timeout_eff = max(timeout, 2 * sub_min * 60)
        else:
            timeout_eff = timeout
        if eff_started and (now - eff_started) > timeout_eff + grace_seconds:
            # M1 (BATCH1-DESIGN, #6 期望1): 收尸前先查活体。
            # 活体在 → 不收, 滑宽限 (touch_progress 顺延下一轮判定);
            # 超硬顶 hard_cap 仍收 —— 防「僵尸但偶尔冒泡」永占链。
            total_elapsed = now - (started or eff_started)
            hard_cap = 3 * (timeout_eff + grace_seconds)
            alive, why = _worker_alive_for_reap(
                ledger, sess, base, task_id, now)
            if alive and total_elapsed < hard_cap:
                ledger.touch_progress(task_id)
                results.append({"task_id": task_id,
                              "trigger": "timeout_suppressed_alive",
                              "verdict": "WAIT",
                              "note": _t("reaper.why.suppressed_alive_extend_grace", why=why)})
                return
            # 不活 或 超硬顶 → 照收尸 (why 附审计)
            r = harvest(task_id, ledger=ledger, hermes_home=hermes_home,
                       workspace=ws, registry=registry, spawn_result={
                          "exit_code": 124, "stdout": "",
                          "stderr": f"TIMEOUT(TODO.md 状态={todo_status} "
                                   f"但 {int(now - eff_started)}s 未变, "
                                   f"活体={why}"
                                   + (", hard_cap" if alive else "") + ")"},
                       worker_status="timeout")
            results.append({"task_id": task_id, "trigger": "timeout",
                           "verdict": r.get("verdict"),
                           "note": (f"TODO.md 状态={todo_status} 超时"
                                   + (" (hard_cap)" if alive else ""))})
            return
        # 没超时, worker 在干, 不动
        # issue#13: waiting_sub 卡 < SUB_MIN 时落 verdict=WAIT 抑制类结果,
        # 不敲门 / 不改 TODO / 不写 worker_report (AC-A1); 同时盖数据戳
        # (COALESCE 不覆盖), 供 derive_worker_state 派生 WAITING_SUB.
        if is_waiting_sub and sub_enabled:
            # 盖戳: 首遇此 waiting_sub 状态写 subagent_wait_since=now,
            # 已写过 (COALESCE) 不覆盖 —— 等待时长锚点取首遇时刻.
            try:
                ledger.conn.execute(
                    "UPDATE harvest_contracts "
                    "SET subagent_wait_since=COALESCE(subagent_wait_since, ?) "
                    "WHERE task_id=?",
                    (now, task_id))
                ledger.conn.commit()
            except Exception:
                pass
            silence = now - eff_started
            mins = round(silence / 60, 1)
            results.append({"task_id": task_id,
                          "trigger": "todo_waiting_sub",
                          "verdict": "WAIT",
                          "note": _t("reaper.why.waiting_sub_wait",
                                     mins=mins, sub_min=sub_min)})
            return
        results.append({"task_id": task_id, "trigger": "todo_working",
                       "verdict": "WAIT",
                       "note": _t("reaper.why.not_suppressed_in_progress", status=todo_status)})
        # issue#13: worker 从 waiting_sub 切回普通 working → 清等待戳
        # (防后续 reap 误判仍 WAITING_SUB). is_waiting_sub 为 False 即
        # 此分支; _reap_one_task 顶部已读 norm, 这里再检查
        # 一次仅做保护性清戳, 不影响常规 working 卡 (NULL → NULL 无害).
        _clear_subagent_wait_stamp()
        return

    # 状态字段缺失 / 未识别 (统一格式后: worker 没写 TODO.md 状态 = 没起手)
    # 1A 减法 (2026-10-03): 旧的 "marker.exit_code 兜底" 分支已删 ——
    # exit_code 是 POST /chat 的 HTTP 返回值, 不是 worker 完成信号, 用它
    # 结算会误判。worker 起手必写 TODO.md 状态; 没写 = 没干活, 交给
    # started_at 超时兜底。
    if marker:
        # 有 marker 但 TODO.md 状态还没出现
        started = marker.get("started_at", 0)
        now = time.time()
        if started and (now - started) > 300:
            # M1 (BATCH1-DESIGN): 5min 兜底收尸同样先查活体 ——
            # worker 活着但还没写 TODO.md 状态 (起手慢/长思考) 不该收。
            # fail-open: 活体证据不可得 → 维持照收 (现行为不回退)。
            alive, why = _worker_alive_for_reap(
                ledger, sess, base, task_id, now)
            if alive:
                ledger.touch_progress(task_id)
                results.append({"task_id": task_id,
                              "trigger": "timeout_suppressed_alive",
                              "verdict": "WAIT",
                              "note": f"TODO 缺失但活体:{why}, 不收"})
                return
            # worker 5min 没写 TODO.md 状态, 平台收尸 (非 worker 决策)
            r = harvest(task_id, ledger=ledger, hermes_home=hermes_home,
                       workspace=ws, registry=registry, spawn_result=None,
                       worker_status="timeout")
            results.append({"task_id": task_id, "trigger": "todo_missing_5min",
                           "verdict": r.get("verdict"),
                           "note": _t("reaper.why.no_todo_5min_fallback")})
            return
        results.append({"task_id": task_id, "trigger": "todo_pending",
                       "verdict": "WAIT",
                       "note": _t("reaper.why.no_todo_yet_wait")})
        return

    # 没 marker = 这张卡从没被 spawn (launch 的 pending_parent 卡 /
    # main 还没 claim 的卡)。reaper 不推链也不替 main 决策, 收尸
    # 会对"还没开工"的卡误报 FAILED。跳过, 交给 stuck 通知。
    results.append({"task_id": task_id, "trigger": "no_marker",
                   "verdict": "SKIP", "note": _t("reaper.why.skip_unspawned")})


def _reap_late_completions(ledger, hermes_home, base, registry, results,
                          active_run):
    """M3 幂等补结算通道 (BATCH1-DESIGN §M3, #6 期望2) — 本批核心。

    症状 (#6 根因B): 超时收尸把契约钉成 BLOCKED 后, _teardown_current_card
    清了 run.current_card_id, 方案2 的结算面 (只看当前卡) 让这张卡后到的
    completed 永远不被二次评估 → 孤儿, main 永不被唤醒。

    修法: 每轮扫「被超时收尸钉成 BLOCKED」的契约, 重读 TODO.md **此刻**
    的盘 (无缓存, 消竞态), 若已转 completed/failed 就带着新状态再调一次
    harvest —— B 白名单 (harvest.py) 允许 BLOCKED→HARVESTED 重写,
    set_worker_report 内容变化自动清 notify_confirmed_at → 三态中继把新
    报告投给 main。本函数零新增中继逻辑。

    扫描集 (零 DB 迁移, 用既有 harvest_log 前缀区分收尸来源):
      (harvest_status = 'BLOCKED'
       AND harvest_log LIKE 'timeout:%')   # harvest 1a 分支的 ws_norm 前缀
      OR harvest_status = 'WAITING'        # issue #65 C-3 (2026-10-19):
      #   WAITING 卡不在 current_card_contract 结算面 (F5-4 语义), resume 又
      #   可能只唤醒成功而未回拨 —— 兜底面按「此刻的盘」补结算。worker 被
      #   唤醒仍在跑时 TODO=working 自动跳过; 结算后离开扫描集, 幂等;
      #   WAITING→HARVESTED actor=harvest 白名单本就合法 (ledger.py L731)。
      AND (team_work_run_id IS NULL OR run.status='ACTIVE')  # F7 陈旧保护
    worker 自报 failed 的 BLOCKED 前缀是 'failed:', 不进扫描集 —— 那是
    worker 自己的决策, 后续由 resume 走 PENDING 正规路径。

    幂等: 重写后 harvest_status 离开扫描集, 重复调用无副作用;
    并发安全: 复用 harvest 内部 try_claim_harvest 行锁;
    不碰 current_card: 补结算只改该卡自身契约与报告, 链可能已 advance,
    main 收到迟到 COMPLETE 自行消化 (结算作用域铁律不破)。
    """
    if os.environ.get("MW_REAPER_LATE_SETTLE", "1") == "0":
        return  # env 开关: 整体停用补结算 (回滚保险)
    rows = ledger.conn.execute(
        "SELECT hc.task_id, hc.contract_json FROM harvest_contracts hc "
        "LEFT JOIN team_work_runs twr ON hc.team_work_run_id = twr.run_id "
        "WHERE ((hc.harvest_status = 'BLOCKED' "
        "AND hc.harvest_log LIKE 'timeout:%') "
        "OR hc.harvest_status = 'WAITING') "  # issue #65 C-3: WAITING 兜底面
        "AND (hc.team_work_run_id IS NULL OR twr.status = 'ACTIVE')"
    ).fetchall()
    for row in rows:
        task_id = row["task_id"]
        try:
            contract = json.loads(row["contract_json"] or "{}")
            sess = ledger.get_session(task_id)
            marker = read_marker(base, task_id)
            ws = (sess["workspace"] if sess and sess["workspace"]
                  else (marker or {}).get("workspace"))
            if not ws:
                continue
            # ★ 重读 TODO.md 当前状态 (此刻的盘, 无缓存)
            todo_status = _read_todo_status(ws, ledger_row=row,
                                          contract=contract)
            # issue #124: 补结算门槛经统一归一化入口, 中文变异终态同样补
            norm = normalize_status(todo_status)
            if norm not in TERMINAL_STATUSES:
                # blocked 等 main 答澄清 / working 还在跑 / None 没动静
                # —— 都不补, 下轮再看
                continue
            # 保护 current_card: harvest 落终态会 teardown 清槽, 但补
            # 结算的卡可能早已不是当前卡 (链已 advance) —— 若清掉的是
            # 别的卡的槽位就是越权破坏串行铁律。先记后补, 事后还原。
            _pre_cur = None
            if active_run:
                _pre_cur = active_run.get("current_card_id")
            r = harvest(task_id, ledger=ledger, hermes_home=hermes_home,
                       workspace=ws, registry=registry, spawn_result=None,
                       worker_status=norm)
            if _pre_cur and _pre_cur != task_id:
                try:
                    _run_now = ledger.get_team_work(active_run["run_id"])
                    if _run_now and _run_now["status"] == "ACTIVE" \
                            and _run_now["current_card_id"] is None:
                        ledger.set_team_work_current_card(
                            active_run["run_id"], _pre_cur)
                except Exception:
                    pass
            if r.get("settled"):
                results.append({"task_id": task_id,
                               "trigger": "late_settlement",
                               "verdict": r.get("verdict"),
                               "note": (f"收尸后补结算: "
                                       f"BLOCKED(timeout)→"
                                       f"{r.get('harvest_status')}")})
        except Exception as e:
            results.append({"task_id": task_id,
                           "trigger": "late_settlement",
                           "verdict": "ERROR", "error": str(e)[:200]})


def _should_deliver(notify_task: str | None, db_path: str,
                     registry: dict | None) -> bool:
    """网关投递前再校验 (2026-10-03 简化)。

    反向校验只查 ledger, 不再走 hermes gateway API (那部分逻辑已删)。

    只有一道闸门: ledger.should_notify (静音/退避/死信期)。
    1A 修 (2026-10-03 演练实测): 旧的"contract 已 HARVESTED 就 drop"闸门
    与报告中继互斥 —— 中继的触发条件正是 harvest_status=HARVESTED, 该闸门
    会把**第一次**投递也挡掉 (实测 t_9062fb58 报告永不送达 main)。
    去重完全交给 should_notify + mark_notified (发送前乐观标记)。
    """
    if not notify_task:
        return True  # 无 task_id 锚定的消息 (initiator 之外) 仍透传
    try:
        led = Ledger(db_path)
    except Exception:
        return True  # ledger 拿不到就别挡 — 兜底放行, 避免误杀
    try:
        return led.should_notify(notify_task)
    except Exception:
        return True  # 闸门失败不放行反而风险更大 — 让旧路径兜底
    finally:
        try:
            led.close()
        except Exception:
            pass


def _relay_send_timeout_s() -> float:
    """relay 发送线程的单次 HTTP 超时 (秒)。

    2026-10-25 issue #83: 这是「退避封顶必须 >= 发送 timeout」的锚 ——
    退避到期时若上一个线程仍阻塞在这个 timeout 里, claim_for_relay 会
    放行 ⇒ 同一份报告多线程并发在途。默认 2400s (与 _send_to_main 里的
    urlopen timeout 一致); env MW_RELAY_SEND_TIMEOUT_S 可覆写。

    2026-10-25 默认 600 → 2400 (170 E2E 实测): gateway 的 POST /chat 全同步
    (等 session turn lease + 跑完整回合才返回), 而 lease 等待硬编码 1800s
    (agent/turn_facade_lease.py LEASE_WAIT_SECONDS; 生产 agent.log 实测该
    POST 阻塞 p50=1801s / p90=1859s / max=2040s)。旧默认 600s 几乎必然先
    超时 ⇒ 本模块误判「投递失败」走退避重投, 而服务端那份仍在排队、lease
    释放后照常投递 (客户端超时不取消投递, 170 探针实证) ⇒ 重投撞上「第一份
    已处理完」时 main 被同一报告叫起两遍 (#83 风暴的残余通道)。抬到
    2400s (1800 lease + 600 turn 余量) 让首投拿到确定性 200 →
    confirm_delivered, 误判→重投路径失去触发条件。送达延迟由 lease 决定
    (mw 无法单方面消除), 本修复消灭的是误判失败 + 重复处理。
    """
    try:
        return float(os.environ.get("MW_RELAY_SEND_TIMEOUT_S", "2400"))
    except ValueError:
        return 2400.0


def _relay_backoff_cap_s() -> float:
    """relay 失败退避的封顶 (秒) —— issue #83 B 项。

    旧版封顶 300s < 发送 timeout 600s ⇒ 退避到期时上一个发送线程可能
    仍阻塞在 600s 里, claim_for_relay 放行 ⇒ 同一份报告多个线程并发
    在途 (实测 20 分钟内 6 条重复转发, 每条消耗 main 一个完整 turn)。

    修法: 封顶默认 = 发送 timeout + 60s 余量, 保证「退避到期」永远晚于
    「上一个发送线程返回」。env MW_RELAY_BACKOFF_CAP_S 可覆写, 但显式
    设得比发送 timeout 小时会被抬到 timeout+1 (只允许更保守)。
    """
    timeout_s = _relay_send_timeout_s()
    try:
        cap = float(os.environ.get("MW_RELAY_BACKOFF_CAP_S", ""))
    except ValueError:
        cap = 0.0
    if cap <= 0:
        cap = timeout_s + 60.0
    # 只允许比发送 timeout 更保守 (封顶 >= timeout+1), 否则并发窗口回来
    return max(cap, timeout_s + 1.0)


def _send_to_main(sid: str, msg: str, gw: str, key: str, db_path: str,
                 registry: dict | None = None,
                 notify_task: str | None = None):
    """把消息以 chat POST 到 main session, 后台线程发送不阻塞调用方。

    2026-10-04 修订 (commit 2 / design.md §5.0.1):
      原"乐观标记 mark_notified 在派线程前"是项目崩塌的真凶之一 ——
      发送失败 (timeout / 401 / session end) 时 ledger 说"发过了"但消息
      实际丢了, 后续 reap 看到 last_notified_at 推进过, 永远 SKIP。
      修法: **真收到 main gateway HTTP 200 才 mark_notified**。
      发送异常 → 不标 → 下次 fsnotify 触发 / 下轮 reap 重发。

    二次 edge check (2026-10-04): 线程内收到响应后再查 should_notify ——
    若其间被 kill reaper / archive 操作静音, 主动回滚 last_notified_at
    让下一次事件重新派发。这条边检修"标后被静音"这条 race window。

    退避逻辑 (2026-10-02 qq-login 实测, 封顶口径 2026-10-25 issue #83 B 项更新):
      1) 失败不立刻清 NULL, 而是 +1 notify_attempts 并设 notify_dead_until
         = now + 30s * 2^attempts (指数退避, 封顶 RELAY_BACKOFF_CAP_S,
         默认 660s —— 旧版 300s 已被 #83 抬升, 见下方 B 项)
      2) 超过 8 次还失败 → 通知"死信", 永远不再尝试
      3) 退避期内 should_notify 直接返回 False

    2026-10-25 issue #83 A 案 + issue #103 (投递层去重 + 失败留痕):
      1) **POST 前二次核状态** (A 案核心): 发送线程真正 POST 之前重读
         该卡 —— confirmed_at 已非 NULL (并发的另一个线程赢了) 或 run
         已非 ACTIVE ⇒ 直接放弃不 POST。旧版只靠 claim_for_relay 的
         原子占位, 但失败退避封顶 300s < 发送 timeout 600s ⇒ 退避到期
         时上一个线程可能仍阻塞在 600s 里, claim_for_relay 会放行 ⇒
         同一份报告多个线程并发在途 (实测: 20 分钟内 6 条重复转发)。
      2) **退避封顶 >= 发送 timeout** (B 项): 300s → RELAY_BACKOFF_CAP_S
         (默认 660s > timeout 600s), 保证「退避到期」永远晚于「上一个
         发送线程返回」。
      3) **失败不再清 last_notified_at=NULL** (B 项): 失败路径保持
         last_notified_at 置位 (在途态覆盖整个在途窗口), 让 claim_for_relay
         在 inflight_s 内不再放行第二个线程。旧版清 NULL 是「重试并发
         窗口」的直接成因。
      4) **失败留痕** (issue #103): notify_last_failed_at / notify_last_error
         两列 + relay.notify_failed 事件, 让账本看得出「失败过、什么时候
         失败、为什么失败」。
    """
    import urllib.request
    # 入口闸门: 只查 notify_dead_until (死信退避), 不再查 last_notified_at
    # (120s 闸门已废, relay 去重由 notify_confirmed_at 承担 — confirmed_at
    #  非 NULL 表示已确认送达, relay 扫描 WHERE confirmed_at IS NULL)
    #
    # 2026-10-25 issue #83: 发送权 (单飞) 的 claim 下沉到**发送层** ——
    # 在起线程之前原子抢 relay 发送权 (claim_for_relay)。旧版 claim 在
    # 扫描层 (relay 扫描 → claim → 起线程), 扫描层与发送层之间隔着一次
    # 函数调用, 且失败路径会清 last_notified_at ⇒ 退避到期时第二个线程
    # 抢到发送权, 而第一个线程可能仍阻塞在 600s 的 POST 里 ⇒ 同一份报告
    # 多个线程并发在途。发送层 claim 把「同一 task 至多一个在途投递」
    # 收成一条原子 UPDATE。
    if notify_task:
        try:
            led = Ledger(db_path)
            try:
                dead_row = led.conn.execute(
                    "SELECT notify_dead_until FROM harvest_contracts "
                    "WHERE task_id=?", (notify_task,)).fetchone()
                if dead_row and dead_row["notify_dead_until"] and \
                   time.time() < dead_row["notify_dead_until"]:
                    led.close()
                    return False  # 死信退避期, 不投递
                # 原子抢发送权 (单飞): 抢不到 = 已有在途投递, 不重复发。
                # 2026-10-25: inflight 窗口与发送 timeout 同源 (+60)。旧默认
                # 660s < 发送 timeout 2400s ⇒ 第一个线程还阻塞在 POST 里
                # claim 就放行第二线程, 正是 #83 并发在途的复发窗口。
                claimed = led.claim_for_relay(
                    notify_task, inflight_s=_relay_send_timeout_s() + 60.0)
            finally:
                try:
                    led.close()
                except Exception:
                    pass
            if not claimed:
                return False  # 已有在途投递, 不重复发
        except Exception:
            pass  # 闸门失败兜底放行, 让实际 POST 自验

    body = json.dumps({"model": "hermes-agent", "message": msg}).encode()
    req = urllib.request.Request(
        f"{gw}/api/sessions/{sid}/chat", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})

    def _send(rq=req, t=notify_task):
        sent_ok = False
        # ── issue #83 A 案: POST 前二次核状态 ──────────────────────
        # 真正 POST 之前重读该卡: confirmed_at 非 NULL (并发的另一个
        # 线程赢了) / 卡已终态 / run 已非 ACTIVE ⇒ 直接放弃不 POST。
        # 这一步是「同一份报告多个线程并发在途」的直接止血点。
        if t:
            try:
                led = Ledger(db_path)
                try:
                    _chk = led.relay_precheck(t)
                finally:
                    led.close()
                if not _chk.get("go"):
                    try:
                        led2 = Ledger(db_path)
                        try:
                            led2.emit_event("relay.skipped", t, {
                                "reason": _chk.get("reason"),
                                "phase": "pre_post",
                            })
                        finally:
                            led2.close()
                    except Exception:
                        pass
                    return  # 放弃投递, 不 POST
            except Exception:
                pass  # 闸门失败兜底放行, 让实际 POST 自验
        try:
            # s6fix P2-1: 发送 timeout 与退避封顶同源 (_relay_send_timeout_s),
            # 消除「env 调小 MW_RELAY_SEND_TIMEOUT_S 但 socket 仍 600s」对
            # 「封顶 >= 发送 timeout」不变量的静默击穿。
            with urllib.request.urlopen(
                    rq, timeout=_relay_send_timeout_s()) as r:
                # 二次 edge check (2026-10-04): 收到响应后再核 should_notify
                # 若其间被静音 (kill reaper / archive / close_team_work),
                # 回滚 last_notified_at 让下一次 fsnotify 重派
                if t:
                    try:
                        led = Ledger(db_path)
                        try:
                            dead_row = led.conn.execute(
                                "SELECT notify_dead_until FROM harvest_contracts "
                                "WHERE task_id=?", (t,)).fetchone()
                            still_alive = (not dead_row
                                           or not dead_row["notify_dead_until"]
                                           or time.time() >= dead_row["notify_dead_until"])
                        finally:
                            led.close()
                        if not still_alive:
                            # 期间被静音, 主动回滚 (清 last_notified_at)
                            try:
                                led2 = Ledger(db_path)
                                try:
                                    led2.conn.execute(
                                        "UPDATE harvest_contracts "
                                        "SET last_notified_at=NULL "
                                        "WHERE task_id=?", (t,))
                                    led2.conn.commit()
                                finally:
                                    led2.close()
                            except Exception:
                                pass
                            return  # 不标, 让下次事件重派
                    except Exception:
                        pass
                # F5 (2026-10-04): HTTP 200 = 网关已受理并起 main 的 turn,
                # 即视为送达 —— body 解析失败不再落入失败分支。
                # (201 实测: 报告 main 明明收到, 但 body 解析抖动把成功
                # 记成失败, attempts 堆到 8 → 死信 → 合法通知永久静音。)
                status = getattr(r, "status", None) or r.getcode()
                raw = r.read()
                if status == 200:
                    sent_ok = True
                    try:
                        json.loads(raw)  # 仅记录, 失败不影响送达判定
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
        except Exception as e:
            # F5: 429 = main 并发 turn 满 (背压), 不是投递失败 ——
            # 短退避重试, 不计入死信计数。main 的慢 halogen turn 期间
            # 堆 429 是常态, 计死信会把合法报告误封。
            code = getattr(e, "code", None)
            if code == 429:
                if t:
                    try:
                        led = Ledger(db_path)
                        try:
                            led.conn.execute(
                                "UPDATE harvest_contracts SET "
                                "notify_dead_until=?, last_notified_at=NULL "
                                "WHERE task_id=?",
                                (time.time() + 30, t))
                            led.conn.commit()
                        finally:
                            led.close()
                    except Exception:
                        pass
                return
            # 失败分支: 不调 mark_notified (修项目崩塌的真凶之一)
            if t:
                try:
                    led = Ledger(db_path)
                    try:
                        cur = led.conn.execute(
                            "SELECT notify_attempts FROM harvest_contracts "
                            "WHERE task_id=?", (t,)).fetchone()
                        attempts = (cur["notify_attempts"] if cur else 0) + 1
                        if attempts >= 8:
                            # 死信: 保留 last_notified_at 置位 (永不再投)。
                            # issue #103: 失败留痕 —— 失败时刻 + 失败原因
                            # 落两列, 账本看得出「失败过、什么时候、为什么」。
                            led.record_notify_failure(
                                t, str(e), attempts,
                                time.time() + 86400 * 365,
                                last_notified_at=time.time())
                        else:
                            # issue #83 B 项: 退避封顶提到 >= 发送 timeout
                            # (默认 660s > 600s), 保证「退避到期」永远晚于
                            # 「上一个发送线程返回」, 不给并发在途留窗口。
                            backoff_s = min(30 * (2 ** (attempts - 1)),
                                            _relay_backoff_cap_s())
                            # issue #83 B 项: 失败路径**不再**清
                            # last_notified_at —— 保持置位, 让 claim_for_relay
                            # 在 inflight_s 窗口内不再放行第二个线程。
                            # 旧版清 NULL 是「退避到期时上一个线程仍在
                            # 600s 阻塞 → claim 放行 → 多线程并发在途」的
                            # 直接成因 (实测 20 分钟 6 条重复转发)。
                            # 报告重投由 notify_dead_until 退避 + inflight_s
                            # 超时兜底保证, 不靠清 NULL。
                            led.record_notify_failure(
                                t, str(e), attempts,
                                time.time() + backoff_s,
                                last_notified_at=time.time())
                    finally:
                        led.close()
                except Exception:
                    pass
            return
        # 成功路径: 真收到 main gateway HTTP 200 → confirm_delivered
        # (三态: 等待确认 → 已确认送达)。confirm_delivered 内部已清
        # notify_attempts / notify_dead_until, 无需再单独清退避计数。
        if sent_ok and t:
            # issue #61: 旧写法 Ledger(db_path).confirm_delivered(t) 是 inline
            # 构造, 实例永不 close —— 每次成功投递泄漏一个 sqlite fd。
            # 其余六处 Ledger(db_path) 均已有 try/finally 闭环, 唯此漏。
            try:
                led = Ledger(db_path)
                try:
                    led.confirm_delivered(t)
                finally:
                    led.close()
            except Exception:
                pass

    import threading
    threading.Thread(target=_send, daemon=True).start()
    return True


def _notify_main_of_reports(ledger, results, registry: dict | None = None):
    """报告中继 (mw 的本职): 把 worker 报告以 chat 消息转发回发起 session。

    新边界下 harvest 只落账不改 kanban, main(LLM) turn 结束后没人知道
    报告到了。mw 作为通讯中间件, 把报告 POST 到 main 的 session chat,
    触发 main 起一个 turn 读报告 → 自己 complete/派下一张/resume。
    去重: 同卡 120s 内只叫醒一次 (last_notified_at)。

    2026-10-03 简化: 不再走 _cross_check_session, 直接读 ledger。
    """
    gw = os.environ.get("MW_GATEWAY_URL", "http://127.0.0.1:8642")
    key = os.environ.get("API_SERVER_KEY", "")
    if not key:
        results.append({"task_id": "_meta", "trigger": "relay",
                       "verdict": "SKIP", "note": "无 API_SERVER_KEY, 中继不可用"})
        return
    # ★ 2026-10-04 修雪崩 (三态模型): 只扫「未确认送达」的报告。
    # 旧版每轮全表扫 HARVESTED 行 + should_notify 默认 min_interval_s=0 恒真
    # → 同一份报告每 15s 重投一次 → main (halogen reasoning 慢) agent turn
    # 堆到 gateway 上限 10 → 429 Too many concurrent runs → 退避重试 →
    # 实测 14 分钟延迟 (hello.py 04:55 写完, 05:09 才 notified)。
    # 三态: 待发送(confirmed_at NULL 且无在途) / 等待确认(在途未超时) /
    # 已确认送达(confirmed_at 非空)。扫描只认 confirmed_at IS NULL;
    # claim_for_relay 原子把 待发送→等待确认; 真 200 confirm_delivered 置位。
    #
    # F7 (2026-10-07): 加 JOIN team_work_runs.status='ACTIVE'。原因 ——
    # close_team_work 后,即便 confirm_delivered 还没置位(还在网关 429/
    # 排队的路上),后续 reap 轮次也不能再投。`run=ABORTED` 且
    # `closed_reason='complete_all'` 的语义是「链走完,run 已关」,
    # 此时任何报告都是陈旧落账(实测 simple-agents-website run 630cf5a
    # 关闭后仍有 2 条 phantom【mw 兜底敲门】+【mw 报告转发】送达 main,
    # main 必须为每条 phantom 消耗一个 turn 消歧,等于把消歧责任推给 LLM)。
    # harvest_contracts.team_work_run_id 已存在,直接 JOIN 即可,无需新字段。
    # 孤儿卡(team_work_run_id IS NULL)=非 Team Work 启动的卡,这里不挡
    # (主流程不依赖 Team Work 的卡仍走原路径),语义 = 仅挡「run 已关」的卡。
    rows = ledger.conn.execute(
        "SELECT hc.task_id, hc.harvest_status, hc.worker_report "
        "FROM harvest_contracts hc "
        "LEFT JOIN team_work_runs twr ON hc.team_work_run_id = twr.run_id "
        "WHERE hc.harvest_status IN ('HARVESTED','NEED_CLARIFICATION','BLOCKED') "
        "AND hc.worker_report IS NOT NULL AND hc.notify_confirmed_at IS NULL "
        "AND (hc.team_work_run_id IS NULL OR twr.status = 'ACTIVE')"
    ).fetchall()
    for row in rows:
        tid = row["task_id"]
        if not ledger.should_notify(tid):
            continue
        sid = ledger.get_initiator(tid)
        if not sid:
            continue  # 非 Team Work 发起的卡, 无中继目标
        try:
            report = json.loads(row["worker_report"])
        except (json.JSONDecodeError, TypeError):
            continue
        # 发送权 (单飞) 已下沉到发送层 _send_to_main (issue #83, 2026-10-25):
        # 起线程前原子 claim_for_relay。扫描层不再抢 claim —— 双层 claim
        # 会让发送层永远抢不到 (扫描刚置 last_notified_at, 发送层再 claim
        # 必失败), 报告永远投不出去。
        verdict = report.get("verdict", row["harvest_status"])
        msg = (f"【mw 报告转发】任务 {tid} 结算落账: verdict={verdict}。"
               f" 产物目录: {report.get('product_workspace') or '(见 ledger)'}。"
               f" 请读 GET /api/tasks 或 ledger worker_report 后决策: "
               f"COMPLETE→你调 hermes kanban complete 并推进下一张; "
               f"NEED_CLARIFICATION→回答后 POST /api/resume; "
               f"FAILED→改方向/换 worker/建后续卡/放弃。")
        try:
            sent = _send_to_main(sid, msg, gw, key, ledger.path,
                                registry=registry, notify_task=tid)
            if sent is False:
                results.append({"task_id": tid, "trigger": "relay",
                               "verdict": "SUPPRESSED",
                               "note": "去重/退避期, 未投递"})
            else:
                results.append({"task_id": tid, "trigger": "relay",
                               "verdict": f"DISPATCHED:{verdict}"})
        except Exception as e:
            results.append({"task_id": tid, "trigger": "relay",
                           "verdict": "RELAY_FAIL", "error": str(e)[:150]})


# ── SSE-FIX-F3 (2026-10-08, F3b 移植): console 活体查询公共函数 ──
# 背景: sse_bridge 的活体表由 **console 进程** 持有, reaper 是独立
# 进程, 跨进程 import 只能拿到空字典 —— 旧的
# `from .sse_bridge import _worker_live` 直读是死代码, 已删。
# 修法: 敲门前向同容器 console 的 GET /api/workers/live (FIX-2 已上线)
# 查一次现成状态。fail-open 铁律: console 不可达 / 无条目 / 任何异常
# → 返回 None, 调用方不抑制、照敲 (宁可多敲, 不漏挂死)。

def _console_base_url() -> str:
    """console 活体端点基址, MW_CONSOLE_URL 可覆写。"""
    return os.environ.get("MW_CONSOLE_URL", "http://127.0.0.1:8700").rstrip("/")


def _query_console_liveness(profile, session_id) -> dict | None:
    """向 console 查 (profile, session_id) 的 SSE 活体条目。

    返回匹配条目 dict (thinking/last_event_ts/last_event_type/...);
    无条目 / console 不可达 / 任何异常 → None (fail-open, 调用方照敲)。
    """
    if not profile or not session_id:
        return None
    import urllib.request
    url = f"{_console_base_url()}/api/workers/live"
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            data = json.loads(r.read())
        for e in data.get("live", []):
            if e.get("profile") == profile and e.get("session_id") == session_id:
                return e
    except Exception:
        return None  # fail-open: 查询失败绝不吞成「抑制」
    return None


def _is_alive_entry(entry: dict | None, now: float,
                    suppress_window_s: float) -> bool:
    """活体判定 (FIX-3 语义): thinking==True (从首个活动事件粘滞到
    终态, 正好覆盖单个长工具调用中间无事件的空窗) 或 last_event_ts
    距今 < 抑制窗口。任一命中即活。"""
    if entry is None:
        return False
    if entry.get("thinking") is True:
        return True
    ts = entry.get("last_event_ts") or 0
    return ts > 0 and (now - ts) < suppress_window_s


def _live_window_s() -> float:
    """M1 (BATCH1-DESIGN): 收尸活体判定窗口 (秒), 默认 120。

    env MW_REAPER_LIVE_WINDOW_S。语义: 事件流/TODO 进展距现在 < 窗口
    = worker 仍在动。与 stuck 敲门的 MW_REAPER_SUPPRESS_WINDOW_S 独立
    (收尸是重决策, 窗口比敲门宽)。
    """
    v = os.environ.get("MW_REAPER_LIVE_WINDOW_S", "")
    try:
        return float(v) if v else 120.0
    except ValueError:
        return 120.0


def _subagent_wait_min() -> int:
    """issue#13 (2026-10-15): waiting_sub 卡独立等待阈值 (分钟)。

    env MW_REAPER_SUBAGENT_MIN, 默认 60. 设 0 = 整体停用 waiting_sub
    新行为 (回滚开关, AC-A10) —— waiting_sub 仍被 IN_PROGRESS_SET
    接纳 (防 5min 误收尸), 但不盖戳 / 不派生 WAITING_SUB / 不走专属
    阈值与文案; 超阈判断退回旧 stuck 30min 路径, fail-open 形态保留.
    非法值回落 60 (与既有 env 容错一致).
    """
    v = os.environ.get("MW_REAPER_SUBAGENT_MIN", "")
    try:
        n = int(v) if v else 60
        return n if n >= 0 else 60
    except ValueError:
        return 60


def _worker_alive_for_reap(ledger, sess, base, task_id, now) -> tuple:
    """M1 收尸前活体判定。返回 (alive, reason)。

    判定 (任一命中即活):
      1. console 活体条目 thinking == True (粘滞标志, 覆盖长工具空窗)
      2. 条目 last_event_ts 距今 < live_window —— 事件流仍在增长
      3. ledger last_progress_at 距今 < live_window —— worker 刚改过
         TODO.md (fsnotify F8 已 touch)
    fail-open 铁律 (与 FIX-3 敲门方向相反, 注意!):
      console 不可达 / 无条目 / 异常 → 返回 (False, "liveness_unknown")
      —— 收尸是最后安全网, 活体证据不可得时维持现行为 (照收),
      宁可误收不误漏挂死; 误收由 M3 补结算兜底, 不再是死局。
    """
    window = _live_window_s()
    try:
        prof = _row_get(sess, "profile")
        sid = _row_get(sess, "session_id")
        if prof and sid:
            entry = _query_console_liveness(prof, sid)
            if entry is not None:
                if entry.get("thinking") is True:
                    return True, "thinking"
                ts = entry.get("last_event_ts") or 0
                if ts > 0 and (now - ts) < window:
                    return True, f"event_stream_fresh_{int(now - ts)}s"
    except Exception:
        pass  # fail-open: 证据不可得 → 不判活
    try:
        row = ledger.get_contract(task_id)
        lpa = _row_get(row, "last_progress_at")
        if lpa and (now - lpa) < window:
            return True, f"todo_progress_{int(now - lpa)}s"
    except Exception:
        pass
    return False, "liveness_unknown"


def _relay_suppress_window(threshold_s: float) -> float:
    """F4b (2026-10-09): relay 送达抑制窗口 (秒)。

    env MW_REAPER_RELAY_SUPPRESS_WINDOW_S; 未设/非法 → 默认 =
    threshold (stuck_after_min*60)。语义: 距本 run 最近一次 relay
    送达不足一个 stuck 窗口 → main 刚被唤醒, 不该再被兜底敲门打扰。
    """
    v = os.environ.get("MW_REAPER_RELAY_SUPPRESS_WINDOW_S", "")
    try:
        return float(v) if v else threshold_s
    except ValueError:
        return threshold_s


def _query_initiator_last_active(sid: str) -> float | None:
    """F4b (2026-10-09): 经 gateway 读 initiator session 的 last_active (epoch)。

    GET {MW_GATEWAY_URL}/api/sessions/<sid> (hermes
    web_routers/sessions.py get_session_detail, 返回 session 行,
    last_active 每轮活动 bump)。
    不可达 / 无 key / 无字段 / 任何异常 → None (fail-open, 照敲)。
    """
    if not sid:
        return None
    gw = os.environ.get("MW_GATEWAY_URL", "http://127.0.0.1:8642")
    key = os.environ.get("API_SERVER_KEY", "")
    if not key:
        return None
    import urllib.request
    req = urllib.request.Request(
        f"{gw}/api/sessions/{sid}", method="GET",
        headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read())
        # 兼容顶层 session 字段与直接返回 session dict 两种形态
        sess = data.get("session") if isinstance(data.get("session"), dict) \
            else data
        la = sess.get("last_active")
        return float(la) if la else None
    except Exception:
        return None


def _notify_main_of_stuck(ledger, base, results, registry: dict | None = None,
                          stuck_after_min=30, active_run_id: str | None = None,
                          current_card_id: str | None = None):
    """敲门: PENDING 任务挂 > stuck_after_min 分钟, 真发消息叫醒 main。

    去重: 同卡 stuck_after_min 内只敲一次 (last_notified_at)。

    T3 限流 (2026-10-03): 仅对 active_run_id 名下的 PENDING 卡生效。
    无 ACTIVE run (active_run_id=None) → 全 SKIP, 不主动敲门, 防止给
    历史/孤儿卡打搅 main。

    方案2 收窄 (2026-10-04): 只敲 current_card_id 这一张。非当前卡在
    ledger 语义里是「排队等 main advance」, 不是「卡死」—— 对它敲门会
    误导 main 去救一张本就不该动的卡, 与「main focus 当前任务」冲突。

    issue#13 (2026-10-15) waiting_sub 阈值换尺: 当 current_card
    TODO.md 状态 ∈ WAITING_SUB_ALIASES 且 MW_REAPER_SUBAGENT_MIN>0
    时, threshold 切到 sub_min*60 (默认 60min), 文案换成「子任务
    等待超阈, 建议 resume」专用版 (AC-A2). 去重与 should_notify 共用
    "同卡一个 SUB_MIN 窗口只敲一次" 语义.
    """
    threshold = stuck_after_min * 60
    now = time.time()
    gw = os.environ.get("MW_GATEWAY_URL", "http://127.0.0.1:8642")
    key = os.environ.get("API_SERVER_KEY", "")
    if not key:
        return
    if not active_run_id:
        # T3: 无 active run → 跳过敲门 (旧行为会全扫 ledger.pending())
        return
    row = ledger.current_card_contract(active_run_id, current_card_id)
    rows = [row] if row else []
    for row in rows:
        task_id = row["task_id"]
        keys = row.keys()
        # s2 (issue #9 / AC9-2): 已 confirmed 送达给 main 的卡绝不敲门。
        # 报告已成功发到 initiator session (notify_confirmed_at 非空),
        # 重复敲门只会让 main 再消费一个 turn 消歧 phantom 报告
        # (实测 630cf5a run 关后 phantom 敲门的根因之一)。
        if "notify_confirmed_at" in keys and row["notify_confirmed_at"]:
            results.append({"task_id": task_id,
                           "trigger": "stuck_notify",
                           "verdict": "SUPPRESSED_DELIVERED",
                           "note": _t("reaper.why.stuck_already_confirmed")})
            continue
        # issue#13: 当前卡若处于 waiting_sub 态, 换尺 (threshold + 敲
        # 门文案). 此判定基于 TODO.md 物理状态, 与 ledger 派生解耦:
        # 不必等 derive_worker_state 落 WAITING_SUB (那是展示层). 开关
        # 关 (MW_REAPER_SUBAGENT_MIN=0) 或状态非 waiting_sub → 维持旧
        # 阈值旧文案 (与第三版 AC-A2 兼容矩阵同款).
        sess = None
        try:
            sess = ledger.get_session(task_id)
        except Exception:
            sess = None
        contract = {}
        try:
            contract = json.loads(row["contract_json"]) if row["contract_json"] else {}
        except Exception:
            contract = {}
        ws = _row_get(sess, "workspace") if sess else None
        cur_todo_status = _read_todo_status(
            ws, ledger_row=row, contract=contract)
        cur_todo_norm = normalize_status(cur_todo_status)
        cur_is_waiting_sub = cur_todo_norm == STATUS_WAITING_SUB
        sub_min = _subagent_wait_min()
        if cur_is_waiting_sub and sub_min > 0:
            threshold = sub_min * 60
        # 优先 ledger.last_progress_at (每次报告落账都 touch),
        # 兜底 marker.started_at / updated_at (旧行迁移前为 NULL)
        last_progress = ((row["last_progress_at"] if "last_progress_at" in keys
                         and row["last_progress_at"] else None)
                        or (read_marker(base, task_id) or {}).get("started_at")
                        or row["updated_at"])
        if now - last_progress <= threshold:
            continue
        if not ledger.should_notify(task_id, min_interval_s=threshold):
            continue
        # SSE-FIX-F3 (2026-10-08, F3b 移植): 敲门前活体抑制 —— 已过
        # stuck 阈值、should_notify 通过、即将敲门的这一刻, 向同容器
        # console 的 GET /api/workers/live 查一次 (timeout 2s), 按
        # (profile, session_id) 匹配条目。thinking==True 或事件新鲜 →
        # 抑制。旧的 `from .sse_bridge import _worker_live` 直读是跨进程
        # 死代码 (reaper 与 SSE 桥不同进程, 内存不共享), 已删。
        # 窗口保留基线 F8 语义: 默认 60s (MW_REAPER_SUPPRESS_WINDOW_S
        # 可覆写), 任一 SSE event 在窗口内 = 活体即抑制。
        # fail-open 不变: console 不可达 / 无条目 / 任何异常 → 照敲。
        suppress_window_s = int(os.environ.get(
            "MW_REAPER_SUPPRESS_WINDOW_S", "60"))
        try:
            sess = ledger.get_session(task_id)
            _prof = _row_get(sess, "profile")
            _sid = _row_get(sess, "session_id")
            if _sid and _prof:
                entry = _query_console_liveness(_prof, _sid)
                if _is_alive_entry(entry, now, suppress_window_s):
                    age = now - (entry.get("last_event_ts") or 0)
                    results.append({"task_id": task_id,
                                  "trigger": "stuck_notify",
                                  "verdict": "SUPPRESSED_ALIVE",
                                  "note": (f"worker 活跃 "
                                           f"(console live: "
                                           f"{entry.get('last_event_type','?')} "
                                           f"{int(age)}s 前, "
                                           f"thinking={entry.get('thinking')})")})
                    continue
        except Exception:
            pass  # 活体查询失败不抑制 —— 宁可多敲一次, 不漏真挂死
        # F5-4 (2026-10-13, issue #5 状态机统一): 在敲门前先把 task
        # 标 WAITING (actor=reaper, 用户原始要求 2.3 「reaper 确认
        # 闲置」)。 这是单一派生态的真理来源 —— derive_worker_state
        # 看到 WAITING 时返 WAITING (UI 显「等介入」而非「执行中」),
        # 不再被三处展示各自拍脑袋折回 PENDING。 已 WAITING 不重复标。
        # D1 防御 (2026-10-13, VERIFY-20261013 §2.3): 「真挂死 fail-open
        # 不倒退」必须对任意 ledger 替身成立 —— mock/替身缺
        # update_worker_state 方法时 AttributeError 会穿透下面的
        # (ValueError, PermissionError) 兜底、打断敲门路径。 先 hasattr
        # 前置判定 (只防「方法缺失」这一种结构性缺位, 不扩宽到业务异常,
        # 防御不过宽), 业务异常仍按原样只吞 ValueError/PermissionError。
        try:
            if (row["harvest_status"] in ("PENDING", "WORKING", "BLOCKED")
                    and hasattr(ledger, "update_worker_state")):
                ledger.update_worker_state(
                    task_id, "WAITING", actor="reaper",
                    reason="[stuck] SSE 静默超阈, 待 main 介入",
                    log="stuck_notify_arming")
                # 重新读 row (state 已变)
                row = ledger.current_card_contract(
                    active_run_id, current_card_id) or row
        except (ValueError, PermissionError):
            pass  # 状态机拒绝 = 别的路径已过渡, 跳过; 仍敲门
        sid = ledger.get_initiator(task_id)
        if not sid:
            continue
        mins = round((now - last_progress) / 60, 1)
        # issue#13: waiting_sub 卡超阈敲门用专属文案 (AC-A2). 文案硬
        # 编码中文, 与既有【mw 敲门】系列同口径, 不走 i18n.
        if cur_is_waiting_sub and sub_min > 0:
            msg = (f"【mw 子任务等待超阈】任务 {task_id} 的 worker 等待子任务"
                   f" 已 {mins} 分钟无进展 (阈值 {sub_min} 分钟),"
                   f" worker 可能已失联或子任务异常。子任务结果可能已落在"
                   f" worker session 记录里 —— 建议优先 resume 续作"
                   f" (POST /api/resume), 或 re-route / cancel。")
        else:
            msg = (f"【mw 敲门】任务 {task_id} 已挂 {mins} 分钟无人推进 "
                   f"(harvest_status={row['harvest_status']})。")
            msg += (" 请检查: worker 是否还在跑 / 要不要 resume / "
                    "re-route / cancel。")
        try:
            _send_to_main(sid, msg, gw, key, ledger.path,
                         registry=registry, notify_task=task_id)
            # bug fix (2026-10-05): stuck 路径必须写 last_notified_at,
            # 否则 should_notify (L490) 永远看陈旧值/NULL → 返回 True →
            # 每 15s 都敲门。relay 路径用 claim_for_relay 写此字段,
            # stuck 路径走 _send_to_main 直接发送,从未触发 mark_notified。
            # confirm_delivered 只改 notify_confirmed_at,不动 last_notified_at。
            # 修法: 起发送线程前主动 mark_notified 一次,与 L304 入口闸门
            # 的逻辑对齐 — 应发之前就置「在途」,失败时 _send_to_main 已
            # 自动 rollback,本调用幂等。
            try:
                ledger.mark_notified(task_id)
            except Exception:
                pass
            ledger.emit_event("task.stuck", task_id, {
                "stuck_minutes": mins,
                "harvest_status": row["harvest_status"],
                "notified_session": sid,
            })
            results.append({"task_id": task_id, "trigger": "stuck_notify",
                           "verdict": "NOTIFIED"})
        except Exception as e:
            results.append({"task_id": task_id, "trigger": "stuck_notify",
                           "verdict": "ERROR", "error": str(e)[:150]})


def _row_get(row, key, default=None):
    """sqlite3.Row 没有 .get() —— 直接 sess.get('profile') 会抛
    AttributeError。代码库既有约定是 `row[k] if k in row.keys() else None`
    (见 harvest.py:453, reaper.py:805)。统一走这个 helper。

    背景 (2026-10-08 F9): F8 活体抑制在 reaper.py:586 用了
    `sess.get("profile")`, 在 sqlite3.Row 上抛 AttributeError 被外层
    try/except 静默吞掉 → 抑制逻辑从未真正执行过。
    """
    if row is None:
        return default
    try:
        if key in row.keys():
            v = row[key]
            return default if v is None else v
    except Exception:
        pass
    return default


def _notify_main_of_session_hang(ledger, base, results, registry=None,
                                hang_after_s=300,
                                active_run_id=None, current_card_id=None):
    """F9 (2026-10-08): worker session 挂死检测 —— 补 stuck/timeout 的盲区。

    症状 (170 实测 run 2bf49f44757f4d9e / t_642b4be6):
      worker 的 SSE 流中途静默 —— thinking=False, 最后事件不是
      'run.completed', 但 TODO.md 仍停在 'working'。LLM turn 死在中间,
      既没交卷 (completed) 也没报错 (failed), gateway 进程还活着。

    为什么现有路径漏:
      - _reap_one_task: TODO.md=working → 只查 marker.started_at 超
        timeout+grace (默认 1800+120=32min) 才收尸。挂死要等 32min。
      - _notify_main_of_stuck: 只看 last_progress_at (报告落账才 touch),
        stuck_after_min=30min。挂死时 last_progress_at 停在最后一次
        tool 完成, 30min 内不敲。
      两者都对"流断了但状态没落"反应太慢, 且无法区分"真挂死"与
      "worker 在跑长任务"。

    信号源: console GET /api/workers/live 的活体条目 (SSE-FIX-F3b 移植:
    原基线直读 sse_bridge._worker_live 是跨进程死代码, 已改经端点)。

    判定 (全部满足 = 挂死):
      1) 有 session 且 SSE 有 live entry (没 bridge 不判, 交回 stuck)
      2) last_event_type != 'run.completed'  (没干净收尾)
      3) thinking == False                    (不在推理/动作)
      4) now - last_event_ts > hang_after_s   (流静默超阈值, 默认 300s)
      5) TODO.md 状态仍是 working/in_progress  (没交卷)

    动作: 敲 main, verdict=SESSION_HUNG, 明确告诉 main "worker 流断了
    没交卷", 让 main 决定 resume / re-route / cancel。
    ★ 绝不 auto-close run / 绝不 auto-harvest COMPLETE —— main 是决策者
      (用户铁律: 非用户手动, 任何进程不得越权关 run)。

    去重: 复用 ledger.should_notify (min_interval = hang_after_s),
    同 session 挂死窗口内只敲一次, 不刷屏。
    """
    if not active_run_id or not current_card_id:
        return
    row = ledger.current_card_contract(active_run_id, current_card_id)
    if not row:
        return
    task_id = row["task_id"]
    contract = json.loads(row["contract_json"]) if row["contract_json"] else {}
    sess = ledger.get_session(task_id)
    profile = _row_get(sess, "profile")
    session_id = _row_get(sess, "session_id")
    if not (session_id and profile):
        return  # 没 session/没 profile → 无 SSE 信号, 交回 stuck 路径

    # 只在 TODO.md 仍是"在干"时判挂死; 已 completed/failed/blocked 走正常结算
    ws = _row_get(sess, "workspace")
    todo_status = _read_todo_status(ws, ledger_row=row, contract=contract)
    # issue#13 (2026-10-15) 显式排除 waiting_sub: waiting_sub 卡 SSE
    # 静默是 worker 等子任务的预期形态, 不是流断了; 此路径只盯
    # 普通 working 卡. 注意不要把 waiting_sub 纳入 —— 纳入等价于
    # 把「等子任务」误判为「流断了」, 与 waiting_sub 设计语义相反.
    # issue #124: 归一化后只认规范值 working, 各中英语义别名
    # (进行中 / in-progress 等) 同样被盯, 未识别值不管 (语义不变)。
    todo_norm = normalize_status(todo_status)
    if todo_norm != STATUS_WORKING:
        return  # 没在"working" 状态, 不是本检测的目标

    # SSE-FIX-F3b 移植 (2026-10-08): 活体信号改经 console 端点获取。
    # 基线的 `from .sse_bridge import _worker_live` 直读在 reaper 进程
    # 永远为空 (跨进程内存不共享), 本检测实际从未生效 —— 死代码。
    # 语义保持: 无条目 / console 不可达 → 不判 (宁可漏, 不误报)。
    live = _query_console_liveness(profile, session_id)
    if not live:
        return  # 没 SSE live entry, 无信号
    last_ev_ts = live.get("last_event_ts", 0)
    last_ev_type = live.get("last_event_type", "")
    thinking = bool(live.get("thinking"))
    if last_ev_ts <= 0:
        return
    now = time.time()
    silence = now - last_ev_ts
    if last_ev_type == "run.completed":
        return  # 已收尾, 正常结算路径处理
    if thinking:
        return  # 还在推理/动作, 没挂
    if silence <= hang_after_s:
        return  # 静默未超阈值, 可能是正常 turn 间空档
    # 判定为挂死
    if not ledger.should_notify(task_id, min_interval_s=hang_after_s):
        return
    gw = os.environ.get("MW_GATEWAY_URL", "http://127.0.0.1:8642")
    key = os.environ.get("API_SERVER_KEY", "")
    if not key:
        return
    sid = ledger.get_initiator(task_id)
    if not sid:
        return
    mins = round(silence / 60, 1)
    msg = (f"【mw 挂死检测】任务 {task_id} 的 worker session "
           f"{sess['session_id']} 已静默 {mins} 分钟: "
           f"最后事件={last_ev_type or '(无)'}, 非 run.completed, "
           f"未在推理, 但 TODO.md 仍是 '{todo_status}' (没交卷)。"
           f" worker 进程可能还活着但 LLM turn 已断。"
           f" 请决定: resume 续作 / re-route 换 worker / cancel。")
    try:
        _send_to_main(sid, msg, gw, key, ledger.path,
                     registry=registry, notify_task=task_id)
        try:
            ledger.mark_notified(task_id)
        except Exception:
            pass
        ledger.emit_event("task.session_hang", task_id, {
            "silence_seconds": int(silence),
            "last_event_type": last_ev_type,
            "todo_status": todo_status,
            "session_id": sess["session_id"],
            "notified_session": sid,
        })
        results.append({"task_id": task_id, "trigger": "session_hang",
                       "verdict": "SESSION_HUNG",
                       "note": f"SSE 静默 {mins}min, 最后={last_ev_type}, "
                               f"TODO={todo_status}"})
    except Exception as e:
        results.append({"task_id": task_id, "trigger": "session_hang",
                       "verdict": "ERROR", "error": str(e)[:150]})


def _notify_null_slot(ledger, active_run, results, registry=None,
                     stuck_after_min=30, base="."):
    """F4 (2026-10-04): NULL 槽兜底敲门。

    形状: run ACTIVE + current_card_id=NULL + 名下还有未结算卡
    (PENDING/ERROR/BLOCKED)。语义 = main 把上一张结算后没有 advance
    下一张 (忘派 / turn 死在中间 / resume 没接住), 整条链掉进
    「reaper 只看 current_card → NULL → 什么都不做」的盲区。
    本次 201 演练 t_321617ab 卡死 26 分钟零敲门就是这个形状。

    方案2 把结算面收窄到 current_card 是对的, 但收窄后 NULL 槽成了
    静默黑洞 —— 兜底敲门补上这个洞: 闲置超过 stuck 窗口敲一次,
    run 级去重 (last_null_slot_knock_at), 不刷屏。
    """
    if not active_run:
        return
    if active_run.get("current_card_id") is not None:
        return  # 有当前卡, 走正常 stuck 路径
    run_id = active_run["run_id"]
    pending = ledger.pending_in_team_work(run_id)
    if not pending:
        return  # 名下没有未结算卡 = 链真的走完了, 不是卡死
    threshold = stuck_after_min * 60
    now = time.time()
    last_knock = active_run.get("last_null_slot_knock_at")
    if last_knock and (now - last_knock) < threshold:
        return  # 本窗口已敲过

    # SSE-FIX-F4 (2026-10-08, F3b 移植): 闲置基准只算**真正派发过**的卡
    # (存在 spawn marker 或 last_progress_at 非空)。从未派发的排队卡
    # last_progress_at 为 NULL, 旧兜底落到 updated_at = launch 时刻 →
    # 槽位一空闲置度瞬间 = 距 launch 分钟数, 拿排队卡出生时间当心跳,
    # 必然误报。剔除后:
    #   - 有已派发卡 → 以它们最近的 progress/notified 时刻为基准;
    #   - 名下全部未派发 (链刚 launch、main 还没派第一张) → 以 run 行
    #     created_at/updated_at 为基准, 文案改「等待 main 派发」。
    dispatched = [r for r in pending
                  if read_marker(base, r["task_id"])
                  or _row_get(r, "last_progress_at")]
    all_undispatched = not dispatched
    if all_undispatched:
        # F4b (2026-10-09): 闲置基准 = NULL 槽起点。
        # current_card_cleared_at = 上次被清成 NULL 的时刻;
        # NULL (run 出生即 NULL / 迁移前旧行) 兜底 created_at,
        # 行为退化为「距 launch 闲置」但不劣于现状 (REQ §5 兼容)。
        # 旧代码 updated_at 列在 team_work_runs 根本不存在,
        # 永远落 created_at → 「等待 main 派发 95.9 分钟」误报。
        #
        # s2 (issue #9 / AC9-3 (a) 创建时间基准误敲): 严控 created_at
        # 兜底必须满足至少一个真实信号:
        #   (i)  current_card_cleared_at 存在 → 走它, 不回到 created_at,
        #        排除「距 launch N 分钟」误敲;
        #   (ii) 仅 created_at 兜底且距 launch < 软静默阈值
        #        (默认 600s = 10min, 远大于 stuck_after_min 默认 30min
        #         折合 1800s 已超, 但若 main 设了很短 stuck_after 也兜底)
        #        → run 刚出生一拍即落, 不敲。
        cleared_at = active_run.get("current_card_cleared_at")
        created_at = active_run.get("created_at")
        base_ts = cleared_at or created_at
        if not base_ts:
            return
        stamps = [base_ts]
        # 软静默期: 若只能用 created_at (即刚出生跑/迁移前旧行没 cleared_at),
        # 距离 now 不足 MW_REAPER_LAUNCH_GRACE_S (默认 600s) 时, 直接
        # 视为「run 刚 birth, main 还没派第一张」, 不敲 —— 旧版的「挂 95min
        # 等待派发」误报就是这条路径。clear 戳存在的卡不受此限。
        if not cleared_at and created_at:
            grace_s = int(os.environ.get(
                "MW_REAPER_LAUNCH_GRACE_S", "600"))
            if (now - created_at) < grace_s:
                results.append({"task_id": run_id,
                               "trigger": "null_slot_knock",
                               "verdict": "SUPPRESSED_LAUNCH_GRACE",
                               "note": (f"run 出生 {int(now - created_at)}s "
                                       f"< grace {grace_s}s, main 可能"
                                       "还在派第一张, 不敲")})
                return
    else:
        stamps = []
        for r in dispatched:
            for col in ("last_progress_at", "last_notified_at", "updated_at"):
                v = _row_get(r, col)
                if v:
                    stamps.append(v)
        if not stamps:
            return
    idle = now - max(stamps)
    if idle <= threshold:
        return

    # SSE-FIX-F3/F4: 敲前对名下已派发卡的 worker 做活体查询 ——
    # 任一 worker 活着 (thinking 或事件新鲜) 即抑制, 不敲。
    suppress_window_s = int(os.environ.get(
        "MW_REAPER_SUPPRESS_WINDOW_S", "60"))
    for r in dispatched:
        try:
            sess = ledger.get_session(r["task_id"])
            profile = _row_get(sess, "profile")
            session_id = _row_get(sess, "session_id")
            if sess and session_id and profile:
                entry = _query_console_liveness(profile, session_id)
                if _is_alive_entry(entry, now, suppress_window_s):
                    results.append({"task_id": run_id,
                                  "trigger": "null_slot_knock",
                                  "verdict": "SUPPRESSED_ALIVE",
                                  "note": (f"{r['task_id']} worker 活跃 "
                                          f"(console live: "
                                          f"{entry.get('last_event_type','?')} "
                                          f"{int(now - (entry.get('last_event_ts') or 0))}s 前)")})
                    return
        except Exception:
            pass  # fail-open: 查询异常不抑制, 继续走敲门判定

    # F4b 层2 (2026-10-09): relay 送达抑制 —— 本 run 名下任一卡
    # (含刚结算的当前卡) 的 relay 报告在窗口内已确认/在途送达
    # initiator → main 刚被唤醒正在处理, 兜底敲门是重复打扰, 不敲。
    relay_window_s = _relay_suppress_window(threshold)
    try:
        _rr = ledger.conn.execute(
            "SELECT MAX(COALESCE(notify_confirmed_at, last_notified_at)) "
            "AS last_relay FROM harvest_contracts "
            "WHERE team_work_run_id=?", (run_id,)).fetchone()
        _last_relay = _rr["last_relay"] if _rr else None
        if _last_relay and (now - _last_relay) < relay_window_s:
            results.append({"task_id": run_id,
                           "trigger": "null_slot_knock",
                           "verdict": "SUPPRESSED_RELAY",
                           "note": (f"relay {int(now - _last_relay)}s 前已"
                                    f"送达 initiator, main 刚被唤醒")})
            return
    except Exception:
        pass  # fail-open: 查询异常不抑制, 继续走后续判定

    # F4b 层3 (2026-10-09): initiator 活体抑制 (默认开,
    # MW_REAPER_INITIATOR_LIVENESS="0" 关闭)。main session 在抑制
    # 窗口内有活动 → 正在 turn 里推进, 不敲。每 run 至多 1 次
    # gateway HTTP (只查 initiator 一个 session)。
    # s2 (issue #9 / AC9-2) 不再加「all delivered 也抑制」层 —— 与
    # F4b RELAY 抑制层 (relay_window_s 默认 = threshold_s) 重叠, 只会
    # 把「已 delivered 但 main 已转入新一轮沉睡」的合理敲门场景误拦;
    # stuck_notify 那条路径的 delivered 守门才是唯一刚需 (那里没
    # F4b RELAY 抑制覆盖)。
    sid = ledger.get_initiator(pending[0]["task_id"])
    if os.environ.get("MW_REAPER_INITIATOR_LIVENESS", "1") != "0":
        try:  # F4b fail-open: 活体查询异常不抑制, 继续走敲门
            if sid:
                _la = _query_initiator_last_active(sid)
                if _la and (now - _la) < suppress_window_s:
                    results.append({"task_id": run_id,
                                   "trigger": "null_slot_knock",
                                   "verdict": "SUPPRESSED_INITIATOR",
                                   "note": (f"initiator {sid} "
                                           f"{int(now - _la)}s 前有活动")})
                    return
        except Exception:
            pass  # fail-open: 异常不抑制, 照敲 (AC-8)

    if not sid:
        return
    mins = round(idle / 60, 1)
    ids = ", ".join(r["task_id"] for r in pending[:5])
    if all_undispatched:
        # 全部未派发 = main 侧还没 advance, 不是 worker 的锅
        msg = (f"【mw 兜底敲门】run {run_id[:12]} 已 {mins} 分钟"
               f"等待 main 派发。名下 {len(pending)} 张未结算: {ids}。"
               f" 请 advance 下一张 (POST /api/team-work/{run_id}/advance) "
               f"或 close run (POST /api/team-work/{run_id}/close)。")
    else:
        msg = (f"【mw 兜底敲门】run {run_id[:12]} 闲置 {mins} 分钟。"
               f"名下 {len(pending)} 张未结算: {ids}。"
               f" worker 活跃可忽略, 否则 close run "
               f"(POST /api/team-work/{run_id}/close)。")
    gw = os.environ.get("MW_GATEWAY_URL", "http://127.0.0.1:8642")
    key = os.environ.get("API_SERVER_KEY", "")
    if not key:
        return
    try:
        ledger.conn.execute(
            "UPDATE team_work_runs SET last_null_slot_knock_at=? "
            "WHERE run_id=?", (now, run_id))
        ledger.conn.commit()
        _send_to_main(sid, msg, gw, key, ledger.path,
                     registry=registry, notify_task=None)
        ledger.emit_event("team_work.null_slot_knock", run_id,
                         {"idle_minutes": mins, "pending": ids})
        results.append({"task_id": run_id, "trigger": "null_slot_knock",
                       "verdict": "NOTIFIED"})
    except Exception as e:
        results.append({"task_id": run_id, "trigger": "null_slot_knock",
                       "verdict": "ERROR", "error": str(e)[:150]})


def _notify_undelivered_harvest(ledger, active_run, results, registry=None,
                                stuck_after_min=30, base="."):
    """F4 补档 (2026-10-25 issue #103): 「已收割未送达 main」空窗敲门。

    形状: run ACTIVE + current_card_id=NULL (槽位空) + 名下存在
    HARVESTED 且 notify_confirmed_at IS NULL 的卡 + 闲置 > stuck 窗口。

    为什么原 F4 覆盖不到 (issue #103 实案 t_dc11c891):
      原 F4 的锚点是「run 派发/等待」—— 名下有未结算卡 (PENDING/ERROR/
      BLOCKED) 才敲。而「已收割未送达」的卡既不未结算 (HARVESTED), 槽位
      又已清空 (结算时清槽), 原 F4 直接 return ⇒ 8 次投递全失败后 main
      侧 23+ 分钟零信号, 靠人工追问才发现交付已落袋。

    去重: 复用 run 级 last_null_slot_knock_at (同一窗口至多敲一次),
    避免与原 F4 互相刷屏。
    """
    if not active_run:
        return
    if active_run.get("current_card_id") is not None:
        return  # 有当前卡, 走正常结算/stuck 路径
    run_id = active_run["run_id"]
    # 「已收割未送达」候选卡
    try:
        undelivered = ledger.harvested_undelivered(run_id)
    except Exception:
        return
    if not undelivered:
        return  # 名下没有已收割未送达卡 = 不是这个空窗
    threshold = stuck_after_min * 60
    now = time.time()
    last_knock = active_run.get("last_null_slot_knock_at")
    if last_knock and (now - last_knock) < threshold:
        return  # 本窗口已敲过 (与原 F4 共用去重戳)
    # 闲置基准: 这些卡最后一次有动静的时刻 (结算/投递尝试) —— 用最早
    # 的那个, 保证「最久没送达的那张」决定闲置时长。
    stamps = []
    for r in undelivered:
        for col in ("notify_last_failed_at", "last_notified_at",
                    "last_progress_at", "updated_at"):
            v = _row_get(r, col)
            if v:
                stamps.append(float(v))
    if not stamps:
        return
    idle = now - min(stamps)
    if idle <= threshold:
        return  # 还在 stuck 窗口内, 不敲
    ids = ", ".join(r["task_id"] for r in undelivered[:5])
    n = len(undelivered)
    # 投递失败留痕信息 (issue #103): 让敲门文案带上失败次数/原因, main
    # 一眼看出「不是没收到, 是投递一直失败」。
    detail = ""
    try:
        r0 = undelivered[0]
        att = _row_get(r0, "notify_attempts") or 0
        err = (_row_get(r0, "notify_last_error") or "")[:80]
        detail = f" (relay 投递已失败 {att} 次" + (f": {err}" if err else "") + ")"
    except Exception:
        pass
    sid = ledger.get_initiator(undelivered[0]["task_id"])
    if not sid:
        return
    gw = os.environ.get("MW_GATEWAY_URL", "http://127.0.0.1:8642")
    key = os.environ.get("API_SERVER_KEY", "")
    if not key:
        return
    mins = round(idle / 60, 1)
    msg = (f"【mw 兜底敲门】run {run_id[:12]} 有 {n} 张已结算卡未送达 main"
           f"{detail}。闲置 {mins} 分钟。卡: {ids}。"
           f" 请 GET /api/tasks 读报告后裁决, 或确认已处理。")
    try:
        ledger.conn.execute(
            "UPDATE team_work_runs SET last_null_slot_knock_at=? "
            "WHERE run_id=?", (now, run_id))
        ledger.conn.commit()
        _send_to_main(sid, msg, gw, key, ledger.path,
                     registry=registry, notify_task=None)
        ledger.emit_event("team_work.undelivered_knock", run_id,
                         {"idle_minutes": mins, "cards": ids, "count": n})
        results.append({"task_id": run_id, "trigger": "undelivered_knock",
                        "verdict": "NOTIFIED"})
    except Exception as e:
        results.append({"task_id": run_id, "trigger": "undelivered_knock",
                        "verdict": "ERROR", "error": str(e)[:150]})


# ── issue #114: 终态卡新产出告警 (post-terminal output alert) ──────
# #104 期望 3 后半句: 「reaper 检测到终态卡新产出 (commit/文件写) 时
# 告警 main」。终态卡 (HARVESTED/CANCELLED/ARCHIVED) 结算后本应静默,
# 若其工作区在结算之后仍出现新写入 (含 git commit/checkout —— 它们都会
# 刷新工作树文件与 .git/index 的 mtime), 说明存在「无主施工」残留:
# 已交卷的 worker 还在动盘, 或有人拿终态卡的身份继续改代码。P1-1
# 执行点守卫已掐断主要注入通道, 本检测是纵深防御的补哨。
#
# 口径 (issue #114 卡面):
#   - 窗口: MW_REAPER_POST_TERMINAL_WINDOW_MIN, 默认 30 分钟; 只盯
#     结算(updated_at)落在窗口内的终态卡, 老账不翻。
#   - 宽限: MW_REAPER_POST_TERMINAL_GRACE_S, 默认 120s; 结算过程自身
#     会写 transcript/报告, mtime 落在结算+宽限内不算新产出 (防误报)。
#   - 每 task 只告警一次: 以 mw_events 里已落的 worker.state
#     (reason=post_terminal_output) 事件为去重戳, 不新增列。
#   - 开关: MW_REAPER_POST_TERMINAL_ALERT, 默认开; 显式 0/false/off/no 关。
#   - 检测失败静默跳过: 整段 try/except, 任何异常只落一条 SKIP 结果,
#     绝不影响 reap_once 其余步骤。
#   - 扫描有界: 跳过 .git/node_modules/venv 等重目录, 最多扫
#     MW_REAPER_POST_TERMINAL_SCAN_MAX (默认 5000) 个文件, 命中即停。


def _post_terminal_alert_enabled() -> bool:
    """开关: 默认开, 显式 0/false/off/no 才关 (与 MW_* 惯例一致)。"""
    return os.environ.get("MW_REAPER_POST_TERMINAL_ALERT", "1").lower() \
        not in ("0", "false", "off", "no")


def _post_terminal_window_s() -> float:
    """检测窗口 (分钟→秒), 默认 30; 非法值回退默认。"""
    try:
        return float(os.environ.get("MW_REAPER_POST_TERMINAL_WINDOW_MIN", "30")) * 60.0
    except (TypeError, ValueError):
        return 30.0 * 60.0


def _post_terminal_grace_s() -> float:
    """结算自身写盘的宽限期 (秒), 默认 120; 非法值回退默认。"""
    try:
        return float(os.environ.get("MW_REAPER_POST_TERMINAL_GRACE_S", "120"))
    except (TypeError, ValueError):
        return 120.0


def _find_new_output(root: Path, after_ts: float, cap: int) -> "tuple[str, float] | None":
    """有界扫描: 返回 root 树下 mtime > after_ts 的第一个 (路径, mtime)。

    跳过重目录 (.git 自身不扫, 但其 mtime 变化经父目录可见; 工作树
    文件被 git checkout/commit 触碰时 mtime 必然刷新, 故一次遍历同时
    覆盖「文件写」与「git 活动」两种新产出形态)。cap 限制 stat 次数,
    命中即停 —— 巡检路径绝不允许在大仓上无界遍历。
    """
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                      if d not in (".git", "node_modules", "__pycache__",
                                  ".venv", "venv")]
        for fn in filenames:
            n += 1
            if n > cap:
                return None
            try:
                m = os.stat(os.path.join(dirpath, fn)).st_mtime
            except OSError:
                continue  # 竞态删除/权限: 跳过该文件, 不算失败
            if m > after_ts:
                return (os.path.join(dirpath, fn), m)
    return None


def _notify_post_terminal_output(ledger, results, registry=None,
                               window_s=None):
    """issue #114: 终态卡结算后工作区仍有新产出 ⇒ 告警 main, 每 task 一次。

    形状: harvest_status ∈ 真终态三态 + updated_at 落在检测窗口内 +
    task_sessions.workspace 存在 mtime > updated_at+宽限 的文件。
    告警 = emit worker.state(reason=post_terminal_output) + _send_to_main;
    事件本身即去重戳, 下轮扫到同一 task 直接 SKIP。
    (issue #121 F-3: 删去函数体从未引用的 active_run / base 死参数。)
    """
    if not _post_terminal_alert_enabled():
        return
    if window_s is None:
        window_s = _post_terminal_window_s()
    grace = _post_terminal_grace_s()
    now = time.time()
    try:
        rows = ledger.conn.execute(
            "SELECT task_id, harvest_status, updated_at FROM harvest_contracts "
            "WHERE harvest_status IN ('HARVESTED','CANCELLED','ARCHIVED') "
            "AND updated_at >= ? ORDER BY updated_at DESC LIMIT 20",
            (now - window_s,)).fetchall()
    except Exception as e:
        results.append({"task_id": "_meta", "trigger": "post_terminal_output",
                       "verdict": "SKIP", "note": f"query failed: {str(e)[:120]}"})
        return
    for r in rows:
        tid = r["task_id"]
        settled = float(r["updated_at"])
        try:
            # 去重: 本 task 已告警过 (worker.state 事件带 post_terminal_output)
            dup = ledger.conn.execute(
                "SELECT 1 FROM mw_events WHERE event_type='worker.state' "
                "AND task_id=? AND payload LIKE '%post_terminal_output%' LIMIT 1",
                (tid,)).fetchone()
            if dup:
                results.append({"task_id": tid, "trigger": "post_terminal_output",
                              "verdict": "SKIP",
                              "note": "已告警过 (worker.state 去重戳), 不重复"})
                continue
            sess = ledger.get_session(tid)
            ws = _row_get(sess, "workspace")
            if not ws:
                continue  # 无工作区记录, 无从检测
            root = Path(ws)
            if not root.is_dir():
                continue
            hit = _find_new_output(root, settled + grace,
                                  int(os.environ.get(
                                      "MW_REAPER_POST_TERMINAL_SCAN_MAX", "5000")))
            if not hit:
                continue
            path, m = hit
            mins = round((m - settled) / 60, 1)
            # 先落事件 (去重戳), 再敲门 —— 顺序保证: 即便投递失败,
            # 也不会每 15s 重复刷屏; 漏报风险由事件留痕兜底可查。
            ledger.emit_event("worker.state", tid, {
                "from": r["harvest_status"], "to": r["harvest_status"],
                "actor": "reaper",
                "reason": "post_terminal_output",
                "post_terminal_file": path,
                "post_terminal_mtime": m,
                "settled_at": settled,
                "minutes_after_settled": mins,
            })
            sid = ledger.get_initiator(tid)
            gw = os.environ.get("MW_GATEWAY_URL", "http://127.0.0.1:8642")
            key = os.environ.get("API_SERVER_KEY", "")
            if sid and key:
                msg = (f"【mw 终态卡新产出告警】task {tid} 已结算"
                       f"({r['harvest_status']}), 但结算后 {mins} 分钟其工作区"
                       f"仍有新产出: {path}。可能是已交卷 worker 仍在动盘 "
                       f"(无主施工残留), 请核查该卡是否被继续施工。")
                _send_to_main(sid, msg, gw, key, ledger.path,
                             registry=registry, notify_task=None)
                results.append({"task_id": tid, "trigger": "post_terminal_output",
                              "verdict": "NOTIFIED",
                              "note": f"{path} mtime=+{mins}min"})
            else:
                # issue #121 F-4: 无发起人/无 key ⇒ 实际未投递,
                # 不得记 NOTIFIED (事件已落但消息没出门, 口径要分开)。
                results.append({"task_id": tid,
                              "trigger": "post_terminal_output",
                              "verdict": "NO_INITIATOR",
                              "note": "no initiator/key, 未投递"})
        except Exception as e:
            # 检测失败静默跳过 (卡面铁律): 单卡异常不阻断其余卡与主流程
            results.append({"task_id": tid, "trigger": "post_terminal_output",
                          "verdict": "SKIP", "note": str(e)[:120]})


def reap_once(*, ledger: Ledger, hermes_home: str, base: str,
             grace_seconds: int = 120, registry: dict | None = None) -> list[dict]:
    """扫一轮 ACTIVE team work 下的 PENDING 契约, 对可结算的触发 harvest。

    2026-10-03 重写 (T3): 不再扫 ledger.pending() (会扫到孤儿 / 历史卡,
    reaper 会误激活历史 task);改成先取 ACTIVE run, 再扫该 run 下的 PENDING。
    无 ACTIVE run → 全部 SKIP, 防止灾难性误激活。
    """
    results = []
    # ★ 方案2 (2026-10-04 用户定调): 结算面收敛到 current_card_id 这一张。
    # ledger 的 current_card_id 是「现在该看谁」的唯一权威, TODO.md 只回答
    # 「它完成了没」。旧版扫 pending_in_team_work(run_id) 全部 pending 卡,
    # 等于绕过 ledger 自行决定结算范围 —— 那 ledger 就没有存在意义, 且会
    # 误结算非当前卡 (170 实测: 首卡未完, 第二张因 workspace 残留旧
    # TODO.md 的 completed 状态被提前结算)。
    # 现在: 只结算 current_card_id; current_card 为 NULL (main 尚未 advance)
    # 时本轮不结算任何卡, 也不敲门 —— 非当前卡是「排队等派」, 不是「卡死」。
    active_run = ledger.get_active_team_work()
    if active_run:
        run_id = active_run["run_id"]
        cur_card = active_run.get("current_card_id")
        row = ledger.current_card_contract(run_id, cur_card)
        pending_rows = [row] if row else []
        results.append({"task_id": "_meta", "trigger": "reap_scope",
                       "note": (f"active run {run_id[:16]}... "
                               f"current_card={cur_card} "
                               f"(settle scope = current card only)")})
    else:
        pending_rows = []
        results.append({"task_id": "_meta", "trigger": "reap_scope",
                       "verdict": "SKIP",
                       "note": "无 ACTIVE team work, reaper 跳过 "
                               "(防误激活历史/孤儿卡, T3 铁律 1)"})

    # 1) harvest 结算 (仅当前活动卡)
    for row in pending_rows:
        try:
            _reap_one_task(row, ledger, hermes_home, base, grace_seconds, registry, results)
        except Exception as e:
            results.append({"task_id": row["task_id"], "trigger": "harvest",
                           "verdict": "ERROR", "error": str(e)[:200]})
    # 1.5) M3 (BATCH1-DESIGN, #6 期望2): 已收尸 BLOCKED 契约的幂等补
    # 结算 —— 扫 timeout 收尸遗留的 BLOCKED 行, 重读 TODO.md 已转终态
    # 则重写结算并让 relay 把新报告投给 main。失败不影响主流程。
    try:
        _reap_late_completions(ledger, hermes_home, base, registry,
                              results, active_run)
    except Exception as e:
        results.append({"task_id": "_meta", "trigger": "late_settlement",
                       "verdict": "ERROR", "error": str(e)[:200]})
    # 1.6) issue #77: 远端 worker 续 claim + reclaim 计数口径。
    # gateway dispatcher 按 15min TTL 回收 running 卡, 而它的活体豁免
    # 要求 (host_local AND worker_pid) —— mw 起的远端 worker 两个都不
    # 满足, 于是 TTL 一到必被误回收 (worker 其实还活着), 且每次回收
    # 吃掉一次 consecutive_failures (阈值 2) ⇒ 第二次即 auto-block。
    # 这里是 reaper 每 15s 一轮的唯一常驻挂点: 有活性证据就 CAS 续
    # claim_expires; 已发生的回收则按活性证据修账 (不累加计数)。
    # ★ 逻辑全在 mw/pipeline.py, reaper 侧只做一次调用 (minimized
    #   touch: mw/reaper.py 是下一批 #83 的改动面, 避免撞车)。
    try:
        from .pipeline import renew_remote_claims
        results.extend(renew_remote_claims(ledger=ledger, registry=registry))
    except Exception as e:
        results.append({"task_id": "_meta", "trigger": "claim_renewal",
                       "verdict": "ERROR", "error": str(e)[:200]})
    # 2) 报告中继: 把落账的 worker 报告转发回发起 session, 叫醒 main
    try:
        _notify_main_of_reports(ledger, results, registry=registry)
    except Exception as e:
        results.append({"task_id": "_meta", "trigger": "relay",
                       "verdict": "ERROR", "error": str(e)[:200]})
    # 3) 敲门: 仅对当前活动卡生效 (方案2: 非当前卡是排队, 不是卡死)
    # commit 4: stuck_after_min 通过 env 注入 (默认 30, 配 MW_REAPER_STUCK_MIN=5)
    # F4: 提到 try 外 —— 3.5 的兜底敲门也依赖它, 不能让 step3 的异常
    # 把它变成未定义变量。
    stuck_after_min = int(os.environ.get("MW_REAPER_STUCK_MIN", "30"))
    try:
        _notify_main_of_stuck(ledger, base, results, registry=registry,
                              stuck_after_min=stuck_after_min,
                              active_run_id=(active_run["run_id"] if active_run else None),
                              current_card_id=(active_run.get("current_card_id")
                                             if active_run else None))
    except Exception as e:
        results.append({"task_id": "_meta", "trigger": "stuck_notify",
                       "verdict": "ERROR", "error": str(e)[:200]})
    # 3.3) F9: worker session 挂死检测 —— SSE 流静默 + 非 run.completed
    # + TODO.md 仍 working。补 stuck(30min)/timeout(32min) 对"流断了
    # 没交卷"反应太慢的盲区。只敲 main 报 SESSION_HUNG, 不 auto-close。
    hang_after_s = int(os.environ.get("MW_REAPER_HANG_AFTER_S", "300"))
    try:
        _notify_main_of_session_hang(
            ledger, base, results, registry=registry,
            hang_after_s=hang_after_s,
            active_run_id=(active_run["run_id"] if active_run else None),
            current_card_id=(active_run.get("current_card_id")
                            if active_run else None))
    except Exception as e:
        results.append({"task_id": "_meta", "trigger": "session_hang",
                       "verdict": "ERROR", "error": str(e)[:200]})
    # 3.5) F4: NULL 槽兜底敲门 —— run ACTIVE 但 current_card_id=NULL
    # 且名下有未结算卡时, 正常 stuck 路径 (只看 current_card) 完全静默,
    # 这里补敲一次。放在 stuck 之后: 有当前卡时本函数直接 return, 不重复。
    try:
        _notify_null_slot(ledger, active_run, results, registry=registry,
                         stuck_after_min=stuck_after_min, base=base)
    except Exception as e:
        results.append({"task_id": "_meta", "trigger": "null_slot_knock",
                       "verdict": "ERROR", "error": str(e)[:200]})
    # 3.6) F4 补档 (2026-10-25 issue #103): 「已收割未送达 main」空窗
    # 敲门。原 F4 锚在「名下有未结算卡」, 收割后槽清空的卡不在其中 ⇒
    # 投递全失败时 main 侧零信号 (issue #103 实案 23+ 分钟)。放在原 F4
    # 之后: 两者共用 last_null_slot_knock_at 去重戳, 同窗口至多敲一次。
    try:
        _notify_undelivered_harvest(ledger, active_run, results,
                                    registry=registry,
                                    stuck_after_min=stuck_after_min, base=base)
    except Exception as e:
        results.append({"task_id": "_meta", "trigger": "undelivered_knock",
                       "verdict": "ERROR", "error": str(e)[:200]})
    # 3.7) issue #114: 终态卡新产出告警 —— 已结算 (终态) 的卡在工作区
    # 仍有结算后的新文件写入/git 活动时, 敲一次 main (每 task 只一次,
    # 事件即去重戳)。开关 MW_REAPER_POST_TERMINAL_ALERT 默认开, 检测
    # 失败静默跳过, 不影响后续步骤。
    try:
        _notify_post_terminal_output(ledger, results, registry=registry)
    except Exception as e:
        results.append({"task_id": "_meta", "trigger": "post_terminal_output",
                        "verdict": "ERROR", "error": str(e)[:200]})
    # 4) resume 请求消费 (main→worker 续作通道)
    from .pipeline import resume_task
    max_resumes_per_round = int(os.environ.get("MW_REAPER_MAX_RESUMES", 10))
    resume_timeout = int(os.environ.get("MW_RESUME_TIMEOUT", 60))
    for _ in range(max_resumes_per_round):
        req = ledger.claim_pending_resume()
        if not req:
            break
        # s6fix P1-1 (issue #104 根因 4 执行点收口): 消费前终态守卫 ——
        # 「答复入队 → 卡被归档/收割 → reaper 消费」的时间差里, 队列里
        # 的旧答复不得再注入 worker session 唤醒 (无主施工注入形态)。
        # 命中走正规 finish_resume 收口 (不留 PROCESSING 孤儿行), 并落
        # worker.state 痕迹让「为什么没注入」可追溯。
        if ledger.is_terminal(req["task_id"]):
            try:
                ledger.emit_event("worker.state", req["task_id"], {
                    "from": "", "to": "", "actor": "resume",
                    "reason": "resume 拒绝执行: card terminal (消费点守卫)",
                    "resume_rejected": "terminal",
                })
            except Exception:
                pass  # 痕迹失败不阻断拒绝
            ledger.finish_resume(req["id"], ok=False,
                                 error="terminal card")
            results.append({"task_id": req["task_id"], "trigger": "resume",
                            "verdict": "RESUME_REFUSED",
                            "note": "terminal card"})
            continue
        try:
            r = resume_task(req["task_id"], ledger=ledger,
                           hermes_home=hermes_home,
                           instruction=req["instruction"],
                           timeout=resume_timeout,
                           # issue #104: 答复来源随请求行带下来 (main /
                           # operator), 让状态回拨痕迹带上「谁答的」。
                           resume_actor=(req["actor"] if "actor" in req.keys()
                                         else "main"))
            ledger.finish_resume(req["id"], ok=bool(r.get("ok")),
                                result={"ok": r.get("ok"),
                                        "task_id": req["task_id"],
                                        "actor": (req["actor"]
                                                  if "actor" in req.keys()
                                                  else None),
                                        "state_reset": r.get("state_reset"),
                                        "resumed_same_session": r.get("resumed_same_session"),
                                        "harvest_verdict": (r.get("harvest") or {}).get("verdict")})
            results.append({"task_id": req["task_id"], "trigger": "resume",
                           "verdict": "RESUMED" if r.get("ok") else "RESUME_FAILED"})
        except Exception as e:
            try:
                ledger.finish_resume(req["id"], ok=False, error=str(e)[:500])
            except Exception:
                pass
            results.append({"task_id": req["task_id"], "trigger": "resume",
                           "verdict": "RESUME_ERROR", "error": str(e)[:200]})
    return results


def run_loop(*, interval: int = 15, **kw):
    print(f"[reaper] started, interval={interval}s, base={kw.get('base')}")
    hb = Path(kw.get("base", ".")) / "reaper_heartbeat"
    while True:
        try:
            rs = reap_once(**kw)
            for r in rs:
                print(f"[reaper] {r['task_id']} {r['trigger']} → {r.get('verdict', '?')}")
        except Exception as e:
            print(f"[reaper] round error: {e}")
        # 每轮同步 fsnotify watcher: 新 ACTIVE run 出现 / run 切换 / run 关闭
        # 时重挂订阅。run 没变时 sync_watcher 是 no-op (O(1) 比较)。
        # 没有这一步, reaper 启动后才 launch 的 run 永远没有 watcher 挂上
        # (2026-10-04 170 实测: launch 在 reaper 启动之后, watcher 停在
        # "NO ACTIVE RUN" 状态, 实时路径完全失效)。
        try:
            from .realtime import sync_watcher
            w = sync_watcher(kw.get("ledger"), kw.get("hermes_home", ""),
                            kw.get("base", "."), kw.get("registry"))
            if w is not None:
                cur = getattr(run_loop, "_last_watched_run", None)
                if cur != w._run_id:
                    print(f"[reaper] fsnotify watcher attached → run {w._run_id[:16]}")
                    run_loop._last_watched_run = w._run_id
        except Exception as e:
            print(f"[reaper] sync_watcher failed: {e}")
        try:
            hb.write_text(str(time.time()))
        except OSError:
            pass
        # 像素办公室事件桥: 按 ledger 单线权威(current_card_id)同步像素场景。
        # 开关关 / sidecar 没起时是零成本 no-op(内部探测失败即返回)。
        try:
            from .pixel_bridge import tick as pixel_tick
            from .pixel_proxy import pixel_enabled
            if pixel_enabled():
                n = pixel_tick(kw.get("ledger"),
                              hermes_home=kw.get("hermes_home", ""))
                if n:
                    print(f"[reaper] pixel_bridge: {n} worker 场景变化")
        except Exception as e:
            print(f"[reaper] pixel_bridge failed: {e}")
        time.sleep(interval)
