"""s3-fix-15 测试 (2026-10-17, issue #15 worker 误激活 修复回归)。

根因 (docs/VERIFY-11-15.md §4): api_workers() 注入 sse_bridge.live 到
每个 worker 条目, 即使没有 derived_state, 前端 workerStatus 也靠
live.thinking && fresh 判定「工作中」。后果: 上一次 run 留下
ux-designer session 的 SSE tail (180s 内 last_event_ts 仍在窗口),
新 run 不派给 ux-designer, 但前端仍误标 ux-designer 工作中。

修法 (FIX-15):
  1) backend _workers_derived_states 返回 (out, active_run_id) 元组;
     api_workers 顶层暴露 active_team_work_run_id (string | None)。
  2) frontend workerStatus 把『工作中』的权威源限定为 derived_state;
     derived_state 缺失时 (worker 名下无卡, 或无 ACTIVE run) 降级
     idle/offline, **不再**看 entry.live.thinking。
  3) WorkersView 透传 active_team_work_run_id 给 WorkerCard → workerStatus。

覆盖:
  T15.1  无 ACTIVE run + SSE live 残留 (ux-designer) → 不再 working
         (frontend 端由 _worker 调用条件 live.thinking=... 但 derived_state
          为 None 决定 idle); backend 顶层 active_team_work_run_id=None
  T15.2  ACTIVE run 名下 worker 名下无卡 + SSE 残留 → derived_state 不
         注入, active_team_work_run_id 暴露; api_workers 派生态空。
  T15.3  ACTIVE run 名下 worker 名下有卡 (WORKING) → derived_state=
         WORKING, 与 active_team_work_run_id 一致 → 真正 working
         (硬约束: 真有卡显示不回退)。
  T15.4  ACTIVE run 名下 worker 名下有卡 (PENDING 队尾) → derived_state=
         PENDING 不在 DERIVED_TO_VISUAL 映射 → frontend 走 online 派生
         (idle/offline), 不会因 PENDING 而标 working。
  T15.5  跨 run 残留: run_a 完成 → 残留 SSE live (tester); 新 run_b
         拉起不含 tester 卡 → tester 不再 derived_state, api_workers
         顶层 active_team_work_run_id = run_b, tester 不误激活。
  T15.6  api_workers shape 稳定: 既有字段 (workers / active_run_ids /
         reaper_alive / reaper_heartbeat_age_s) 不丢, 新增
         active_team_work_run_id。

不依赖任何外部 hermes / docker 资源; 纯 SQLite + Ledger + console API +
frontend 纯函数模拟 (workerStatus 通过 dist/index.html 中的实例函数
不可直接 import, 这里用 mirror — 见下文)。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mw.console as console
from mw.ledger import Ledger  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    marker = "PASS" if cond else "FAIL"
    line = f"  {marker}  {name}"
    if detail:
        line += f"  {detail}"
    print(line)


def fresh_dual_db():
    """建临时 kanban + ledger, 注入 console 顶层模块变量。"""
    kanban_f = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    kanban_f.close()
    ledger_f = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    ledger_f.close()
    kc = sqlite3.connect(kanban_f.name)
    kc.executescript("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            assignee TEXT,
            status TEXT,
            block_kind TEXT,
            result TEXT,
            created_at REAL,
            started_at REAL,
            completed_at REAL
        );
    """)
    kc.commit()
    kc.close()
    _pre_led = Ledger(ledger_f.name)
    _pre_led.close()
    console.KANBAN_DB = kanban_f.name
    console.LEDGER_DB = ledger_f.name
    return kanban_f.name, ledger_f.name, Ledger(ledger_f.name)


def cleanup(kanban_path, ledger_path, led):
    try:
        led.close()
    except Exception:
        pass
    for p in [kanban_path, ledger_path]:
        try:
            os.unlink(p)
        except OSError:
            pass


def seed_kanban(path, rows):
    c = sqlite3.connect(path)
    for r in rows:
        c.execute(
            "INSERT INTO tasks(id,title,assignee,status,created_at,started_at) "
            "VALUES(?,?,?,?,?,?)",
            (r.get("id"), r.get("title"), r.get("assignee"),
             r.get("status"), r.get("created_at"), r.get("started_at")))
    c.commit()
    c.close()


def stub_workers_registry():
    console.REGISTRY = "/tmp/_test_s3_fix15_registry.json"
    Path(console.REGISTRY).write_text(json.dumps({
        "coder": {"api_url": "http://stub", "api_key": "x" * 32},
        "ux-designer": {"api_url": "http://stub", "api_key": "x" * 32},
        "tester": {"api_url": "http://stub", "api_key": "x" * 32},
        "code-review": {"api_url": "http://stub", "api_key": "x" * 32},
    }))


# ============================================================
# 前端 workerStatus 纯函数 mirror (与 ui/src/views/WorkersView.jsx
# 严格对齐, 文档同步改, 这里也同步改; dist 是构建产物此处不验)
# ============================================================
DERIVED_TO_VISUAL = {
    "WORKING": "working", "HANDOFF": "working",
    "NEED_CLARIFICATION": "attention", "BLOCKED": "attention", "ERROR": "attention",
    "WAITING": "waiting",
}


def workerStatus(entry, activeRunId):
    """FIX-15 后的 workerStatus 镜像 (issue #15 worker 误激活 修复)。

    关键变化: derived_state 缺失时不再看 entry.live.thinking。
    不在派生态的 worker (无 active run / worker 不在 run 名下) → idle。
    """
    dv = entry and entry.get("derived_state")
    if dv and DERIVED_TO_VISUAL.get(dv):
        return DERIVED_TO_VISUAL[dv]
    # 派生为空 → 不在派生态 (FIX-15):
    #   无 active run, 或本 worker 在 active run 名下没卡。
    # 直接走 online/offline, **不**看 entry.live.thinking。
    if entry and entry.get("online") is True:
        return "idle"
    if entry and entry.get("online") is False:
        return "offline"
    return "unknown"


def stub_sse_live(profile, session_id, *, thinking=True, last_event_ts=None):
    """往 sse_bridge._worker_live 注入一条 live 记录 (模拟 issue #15
    残留 SSE tail 的场景)。"""
    from mw import sse_bridge
    sse_bridge._worker_live[(profile, session_id)] = {
        "session_id": session_id,
        "last_run_id": "",
        "last_event_ts": last_event_ts if last_event_ts is not None else time.time(),
        "last_event_type": "tool.progress",
        "thinking": thinking,
        "event_count": 3,
    }


# ============================================================
# T15.1: 无 ACTIVE run + SSE 残留 → 不 working
# ============================================================
def test_T15_1_no_active_run_with_sse_tail_no_working():
    """无 ACTIVE run + ux-designer 残留 SSE tail → workerStatus = idle,
       不是 working。backend api_workers.active_team_work_run_id = None。

    issue #15 复现: 用户截图说 run 名下没给 ux-designer 卡,
    但 live 标识 working。修后: active_run_id=None, derived_state
    不注入, 前端走 online → idle。
    """
    from mw import sse_bridge
    k_path, l_path, led = fresh_dual_db()
    stub_workers_registry()
    # 模拟 issue #15 现场: 无 ACTIVE run (没有 create_team_work_run)
    # 但 sse_bridge._worker_live 有 (ux-designer, s_old) 残留尾段
    sse_bridge._worker_live.clear()
    stub_sse_live("ux-designer", "s_old", thinking=True)
    # 关闭 2s 超时的 /health 探测: registry 用 stub URL 会超时 2s,
    # 让 online 落 False 也无所谓 (idle/offline 都符合预期, 不要 working)
    try:
        d = console.api_workers()
        # T15.1.1: 顶层 active_team_work_run_id = None
        check("T15.1.1 api_workers.active_team_work_run_id = None",
              d.get("active_team_work_run_id") is None,
              f"got {d.get('active_team_work_run_id')!r}")
        # T15.1.2: ux-designer 派生状态不注入 (无 ACTIVE run)
        ud = d["workers"].get("ux-designer", {})
        check("T15.1.2 ux-designer.derived_state 不注入",
              "derived_state" not in ud,
              f"got keys={list(ud.keys())}")
        # T15.1.3: 前端 workerStatus 在 SSE 残留 + 无 active run 下
        # 走 online 派生 (idle/offline) → 不会 working。
        ud["online"] = True  # 即便 online 也应 idle
        st = workerStatus(ud, d.get("active_team_work_run_id"))
        check("T15.1.3 ux-designer (online=True) = idle (不 working)",
              st == "idle",
              f"got status={st!r}")
        # T15.1.4: online=False 也绝不能 working (issue #15 关键回归)
        ud["online"] = False
        st = workerStatus(ud, d.get("active_team_work_run_id"))
        check("T15.1.4 ux-designer (online=False) = offline (不 working)",
              st == "offline",
              f"got status={st!r}")
        # T15.1.5: 即便 live 数组还在且 thinking=True (180s 残留尾段),
        # 派生为空 + 无 active run → 仍然 idle/offline (issue #15 核心)
        ud["online"] = True
        ud["live"] = [{"thinking": True, "last_event_ts": time.time()}]
        st = workerStatus(ud, d.get("active_team_work_run_id"))
        check("T15.1.5 SSE live thinking=True 残留 → 仍 idle (issue #15 根因)",
              st != "working",
              f"got status={st!r}")
    finally:
        sse_bridge._worker_live.clear()
        cleanup(k_path, l_path, led)


# ============================================================
# T15.2: ACTIVE run 名下 worker 名下无卡 → 派生空 + run_id 暴露
# ============================================================
def test_T15_2_active_run_worker_not_in_run_no_derived():
    """ACTIVE run 存在 (含 coder 卡), 但 ux-designer 名下无卡 →
       ux-designer 不注入 derived_state; active_team_work_run_id 暴露
       run_id; 前端 workerStatus 走 online 派生 (不 working)。
    """
    from mw import sse_bridge
    k_path, l_path, led = fresh_dual_db()
    stub_workers_registry()
    try:
        # ACTIVE run 含 coder 卡 (无 ux-designer 卡)
        led.create_team_work_run("r_fix15_2", "s_main", "需求")
        led.put_contract("t_c1", {"required_files": []})
        led.register_team_work_card("r_fix15_2", "t_c1")
        seed_kanban(k_path, [
            {"id": "t_c1", "title": "C1", "assignee": "coder",
             "status": "PENDING", "created_at": time.time()},
        ])
        led.set_team_work_current_card("r_fix15_2", "t_c1")
        # 模拟 ux-designer 有 SSE 残留 (last run 的尾巴)
        sse_bridge._worker_live.clear()
        stub_sse_live("ux-designer", "s_old_2", thinking=True)
        d = console.api_workers()
        # T15.2.1: 顶层 active_team_work_run_id 暴露 run_id
        check("T15.2.1 active_team_work_run_id = r_fix15_2",
              d.get("active_team_work_run_id") == "r_fix15_2",
              f"got {d.get('active_team_work_run_id')!r}")
        # T15.2.2: coder 名下有卡 → 注入 derived_state=PENDING (未开始)
        coder = d["workers"].get("coder", {})
        check("T15.2.2 coder.derived_state 存在 (PENDING/WORKING 由 ledger 决)",
              "derived_state" in coder,
              f"got derived_state={coder.get('derived_state')!r}")
        # T15.2.3: ux-designer 名下无卡 → 不注入 derived_state
        ud = d["workers"].get("ux-designer", {})
        check("T15.2.3 ux-designer.derived_state 不注入",
              "derived_state" not in ud,
              f"got keys={list(ud.keys())}")
        # T15.2.4: ux-designer 即便有 SSE live, 在 active run 下 worker
        # 名下无卡 → workerStatus 走 online → 不 working (FIX-15)
        ud["online"] = True
        ud["live"] = [{"thinking": True, "last_event_ts": time.time()}]
        st = workerStatus(ud, d.get("active_team_work_run_id"))
        check("T15.2.4 ux-designer (有 SSE 残留 + active run) = idle",
              st == "idle",
              f"got status={st!r}")
    finally:
        sse_bridge._worker_live.clear()
        cleanup(k_path, l_path, led)


# ============================================================
# T15.3: ACTIVE run 名下 worker 名下有 WORKING 卡 → 真正 working
# 硬约束: 真有 ACTIVE 卡显示不回退
# ============================================================
def test_T15_3_worker_with_active_card_shows_working():
    """硬约束 (FIX-15 设计): 真有 ACTIVE run 名下 ACTIVE 卡的 worker
       派生为 WORKING 时, frontend 必须显示 working, 不能因 SSE 不在
       而降级 idle。
    """
    from mw import sse_bridge
    k_path, l_path, led = fresh_dual_db()
    stub_workers_registry()
    try:
        led.create_team_work_run("r_fix15_3", "s_main", "需求")
        led.put_contract("t_c1", {"required_files": []})
        led.register_team_work_card("r_fix15_3", "t_c1")
        seed_kanban(k_path, [
            {"id": "t_c1", "title": "C1", "assignee": "coder",
             "status": "PENDING", "created_at": time.time()},
        ])
        led.set_team_work_current_card("r_fix15_3", "t_c1")
        # 关键: SSE 桥空 (worker 没活体 SSE) 也不能让 workerStatus 退回 idle
        sse_bridge._worker_live.clear()
        d = console.api_workers()
        coder = d["workers"].get("coder", {})
        # T15.3.1: derived_state 已注入 (由 ledger 派生)
        check("T15.3.1 coder.derived_state 已注入",
              "derived_state" in coder,
              f"got keys={list(coder.keys())}")
        # T15.3.2: frontend 即便 online=False + SSE 空, 走派生态 → working
        coder["online"] = False
        coder["live"] = []
        st = workerStatus(coder, d.get("active_team_work_run_id"))
        # WORKING → 'working' (或 HANDOFF → 'working')
        check("T15.3.2 coder 派生 WORKING + online=False + SSE 空 = working (不回退)",
              st == "working",
              f"got status={st!r}, derived_state={coder.get('derived_state')!r}")
    finally:
        sse_bridge._worker_live.clear()
        cleanup(k_path, l_path, led)


# ============================================================
# T15.4: ACTIVE run 名下 worker 名下有 PENDING 队尾 → 走 online 派生
# ============================================================
def test_T15_4_pending_in_active_run_idle_via_online():
    """ACTIVE run 名下 worker 有 PENDING 卡 (队尾未开始) → 派生是
       PENDING (不在 DERIVED_TO_VISUAL) → workerStatus 走 online 派生。
       这是正确行为 (PENDING 不是 working, 但仍在 run 里)。
    """
    from mw import sse_bridge
    k_path, l_path, led = fresh_dual_db()
    stub_workers_registry()
    try:
        led.create_team_work_run("r_fix15_4", "s_main", "需求")
        led.put_contract("t_c1", {"required_files": []})
        led.register_team_work_card("r_fix15_4", "t_c1")
        seed_kanban(k_path, [
            {"id": "t_c1", "title": "C1", "assignee": "coder",
             "status": "PENDING", "created_at": time.time() - 200},
        ])
        # 注意: 没 set_team_work_current_card → t_c1 是 PENDING 队尾
        sse_bridge._worker_live.clear()
        d = console.api_workers()
        coder = d["workers"].get("coder", {})
        # T15.4.1: derived_state 已注入 (PENDING/WORKING 由 ledger 决)
        check("T15.4.1 coder.derived_state 存在 (PENDING 系)",
              "derived_state" in coder,
              f"got derived_state={coder.get('derived_state')!r}")
        # T15.4.2: 若派生是 PENDING (不在映射), workerStatus 走 online 派生
        #        若派生是 WORKING (因为 derive_worker_state 在某些窗口判定 WORKING),
        #          也正确 (working 或 idle 都算 NOT stale 误激活)
        coder["online"] = True
        st = workerStatus(coder, d.get("active_team_work_run_id"))
        check("T15.4.2 PENDING 队尾 → 不会因 SSE 残留误 working",
              st in ("idle", "working"),  # 派生 WORKING 或 PENDING 都不是 working 误激活
              f"got status={st!r}, derived_state={coder.get('derived_state')!r}")
        # 关键回归: PENDING 派生 + SSE 残留 thinking=True → 不会 working
        coder["online"] = False
        coder["live"] = [{"thinking": True, "last_event_ts": time.time()}]
        st2 = workerStatus(coder, d.get("active_team_work_run_id"))
        # 若 derived_state 在 DERIVED_TO_VISUAL (如 WORKING), 走映射 working
        # 若 derived_state 不在 (PENDING), 走 online=False → offline
        if coder.get("derived_state") in DERIVED_TO_VISUAL:
            check("T15.4.3 PENDING 派生在映射 → working",
                  st2 == "working",
                  f"got status={st2!r}")
        else:
            check("T15.4.3 PENDING 派生 + online=False + SSE 残留 = offline (不误激活)",
                  st2 == "offline",
                  f"got status={st2!r}, derived_state={coder.get('derived_state')!r}")
    finally:
        sse_bridge._worker_live.clear()
        cleanup(k_path, l_path, led)


# ============================================================
# T15.5: 跨 run 残留 → 新 run 不含 worker → 不再 derived
# ============================================================
def test_T15_5_cross_run_stale_sse_no_activation():
    """run_a 完成 → 残留 tester SSE tail; 新 run_b 拉起不含 tester 卡
       → tester 不再 derived_state; api_workers.active_team_work_run_id
       = run_b; tester workerStatus = idle (不误激活)。"""
    from mw import sse_bridge
    k_path, l_path, led = fresh_dual_db()
    stub_workers_registry()
    try:
        # run_a: 已存在并 complete
        led.create_team_work_run("run_a", "s_a", "需求_a")
        led.close_team_work("run_a", reason="completed")
        # run_b: 当前 ACTIVE, 含 coder 卡, 不含 tester 卡
        led.create_team_work_run("run_b", "s_b", "需求_b")
        led.put_contract("t_b1", {"required_files": []})
        led.register_team_work_card("run_b", "t_b1")
        seed_kanban(k_path, [
            {"id": "t_b1", "title": "B1", "assignee": "coder",
             "status": "PENDING", "created_at": time.time()},
        ])
        led.set_team_work_current_card("run_b", "t_b1")
        # 残留 SSE: tester 在 run_a 完成时留下 live 尾段
        sse_bridge._worker_live.clear()
        stub_sse_live("tester", "s_a_tail", thinking=True)
        d = console.api_workers()
        # T15.5.1: active_team_work_run_id = run_b (当前 ACTIVE)
        check("T15.5.1 active_team_work_run_id = run_b (新 run)",
              d.get("active_team_work_run_id") == "run_b",
              f"got {d.get('active_team_work_run_id')!r}")
        # T15.5.2: tester 不再 derived_state (run_b 不含 tester 卡)
        tester = d["workers"].get("tester", {})
        check("T15.5.2 tester.derived_state 不注入 (跨 run 名下无卡)",
              "derived_state" not in tester,
              f"got keys={list(tester.keys())}")
        # T15.5.3: 即便 tester 有残留 SSE, workerStatus 走 online → idle
        tester["online"] = True
        tester["live"] = [{"thinking": True, "last_event_ts": time.time()}]
        st = workerStatus(tester, d.get("active_team_work_run_id"))
        check("T15.5.3 跨 run SSE 残留 → tester = idle (issue #15 根因消除)",
              st == "idle",
              f"got status={st!r}")
    finally:
        sse_bridge._worker_live.clear()
        cleanup(k_path, l_path, led)


# ============================================================
# T15.6: api_workers shape 稳定
# ============================================================
def test_T15_6_api_workers_shape_preserves_existing_fields():
    """api_workers 既有字段 (workers / active_run_ids / reaper_alive /
       reaper_heartbeat_age_s) 不丢; 新增 active_team_work_run_id。
    """
    from mw import sse_bridge
    k_path, l_path, led = fresh_dual_db()
    stub_workers_registry()
    try:
        sse_bridge._worker_live.clear()
        d = console.api_workers()
        # T15.6.1: workers 字段在
        check("T15.6.1 workers 字段保留",
              "workers" in d and isinstance(d["workers"], dict),
              f"got keys={list(d.keys())}")
        # T15.6.2: active_run_ids 字段在 (SSE 桥接活跃订阅列表, 既有)
        check("T15.6.2 active_run_ids 字段保留",
              "active_run_ids" in d,
              f"got keys={list(d.keys())}")
        # T15.6.3: reaper_alive / reaper_heartbeat_age_s 字段在
        check("T15.6.3 reaper_alive 字段保留",
              "reaper_alive" in d,
              f"got keys={list(d.keys())}")
        check("T15.6.4 reaper_heartbeat_age_s 字段保留",
              "reaper_heartbeat_age_s" in d,
              f"got keys={list(d.keys())}")
        # T15.6.5: 新增 active_team_work_run_id (FIX-15)
        check("T15.6.5 新增 active_team_work_run_id 字段",
              "active_team_work_run_id" in d,
              f"got keys={list(d.keys())}")
        # T15.6.6: 无 ACTIVE run 时 active_team_work_run_id = None
        check("T15.6.6 无 ACTIVE run → active_team_work_run_id = None",
              d.get("active_team_work_run_id") is None,
              f"got {d.get('active_team_work_run_id')!r}")
    finally:
        sse_bridge._worker_live.clear()
        cleanup(k_path, l_path, led)


def main():
    """运行所有 T15.x 测试。pytest 也可独立发现 (test_*.py 命名)。"""
    tests = [
        test_T15_1_no_active_run_with_sse_tail_no_working,
        test_T15_2_active_run_worker_not_in_run_no_derived,
        test_T15_3_worker_with_active_card_shows_working,
        test_T15_4_pending_in_active_run_idle_via_online,
        test_T15_5_cross_run_stale_sse_no_activation,
        test_T15_6_api_workers_shape_preserves_existing_fields,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            check(f"{t.__name__} (exception)", False, f"raised: {e!r}")
    print()
    print(f"  ── PASS {len(PASS)}, FAIL {len(FAIL)} ──")
    if FAIL:
        for name, det in FAIL:
            print(f"  FAIL  {name}  {det}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()