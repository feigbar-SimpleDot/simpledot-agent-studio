"""极简扫描版 pixel_bridge 单元测试 (2026-10-06 重写)。

三态: editing / blocked / idle。
- editing: 承接任务的 worker, 或 main 被敲门/harvest 唤醒 (mw_events 30s 内)
- blocked: 承接任务且 harvest_status ∈ {NEED_CLARIFICATION, BLOCKED, ...}
- idle:   其余所有角色

权威: ledger (team_work_runs + harvest_contracts + mw_events)
"""
from __future__ import annotations
import os, sys, sqlite3, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mw.pixel_bridge as pb
import mw.pixel_proxy as pp

events = []
def fake_post(token, ev):
    events.append((ev.get("session_id"), ev.get("hook_event_name"), ev))
    return True


class StubLedger:
    """最简 ledger 替身: 直接持有 sqlite 连接, 暴露 .conn。"""
    def __init__(self, conn):
        self.conn = conn


def setup_db(d):
    db = os.path.join(d, "ledger.db")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE team_work_runs(
        run_id TEXT PRIMARY KEY, status TEXT, current_card_id TEXT);
    CREATE TABLE harvest_contracts(
        card_id TEXT, assignee TEXT, title TEXT, harvest_status TEXT);
    CREATE TABLE mw_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT, task_id TEXT, payload TEXT, created_at REAL);
    """)
    con.commit()
    return con


def reset(con):
    global events
    events = []
    pb._last_scene.clear()
    pb._online.clear()
    pb._last_token = None
    con.execute("DELETE FROM team_work_runs")
    con.execute("DELETE FROM harvest_contracts")
    con.execute("DELETE FROM mw_events")
    con.commit()


def names(sid):
    return [n for (s, n, _ev) in events if s == sid]


def main():
    pp.pixel_office_token = lambda: "tok"
    pp.pixel_office_running = lambda: True
    pb._post_event = fake_post
    d = tempfile.mkdtemp()
    con = setup_db(d)
    ledger = StubLedger(con)
    REGISTRY = {"coder", "tester"}

    PASS = FAIL = 0
    def check(label, cond):
        nonlocal PASS, FAIL
        if cond: PASS += 1; print(f"  ✓ {label}")
        else:    FAIL += 1; print(f"  ✗ {label}")

    # ── 基础 worker 场景 ──
    print("== case1: editing — worker 承接任务 ==")
    reset(con)
    con.execute("INSERT INTO team_work_runs VALUES('r1','ACTIVE','tA')")
    con.execute("INSERT INTO harvest_contracts VALUES('tA','coder','实现登录','PENDING')")
    con.commit()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    cn = names("coder"); tn = names("tester")
    check("coder 上线", "SessionStart" in cn)
    check("coder 进入 editing(PreToolUse)", "PreToolUse" in cn)
    pt = [e for n,e in [(n,e) for s,n,e in events if s=="coder"] if n=="PreToolUse"][0]
    check("打字标题=卡标题", pt["tool_input"]["file_path"] == "实现登录")
    check("tester idle(Stop)", "Stop" in tn)
    check("tester 不打字", "PreToolUse" not in tn)

    print("== case2: blocked — harvest NEED_CLARIFICATION ==")
    reset(con)
    con.execute("INSERT INTO team_work_runs VALUES('r1','ACTIVE','tA')")
    con.execute("INSERT INTO harvest_contracts VALUES('tA','coder','实现登录','NEED_CLARIFICATION')")
    con.commit()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    cn = names("coder")
    check("coder 举手(PermissionRequest)", "PermissionRequest" in cn)
    check("coder 不打字", "PreToolUse" not in cn)

    print("== case3: 无 ACTIVE run → 全员 idle ==")
    reset(con)
    pb.sync_from_ledger(ledger, REGISTRY, "")
    check("coder idle", "Stop" in names("coder"))
    check("tester idle", "Stop" in names("tester"))

    print("== case4: current_card 为空 → 全员 idle ==")
    reset(con)
    con.execute("INSERT INTO team_work_runs VALUES('r1','ACTIVE',NULL)")
    con.commit()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    check("coder idle", "Stop" in names("coder"))

    # ── editing/blocked 转换 ──
    print("== case5: editing → blocked 收尾打字+举手 ==")
    reset(con)
    con.execute("INSERT INTO team_work_runs VALUES('r1','ACTIVE','tA')")
    con.execute("INSERT INTO harvest_contracts VALUES('tA','coder','X','PENDING')")
    con.commit()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    events.clear()
    con.execute("UPDATE harvest_contracts SET harvest_status='BLOCKED' WHERE card_id='tA'")
    con.commit()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    cn = names("coder")
    check("收尾 PostToolUse", "PostToolUse" in cn)
    check("举手 PermissionRequest", "PermissionRequest" in cn)
    check("收尾在举手之前", cn.index("PostToolUse") < cn.index("PermissionRequest"))

    print("== case6: editing → idle(current_card 清空) ==")
    reset(con)
    con.execute("INSERT INTO team_work_runs VALUES('r1','ACTIVE','tA')")
    con.execute("INSERT INTO harvest_contracts VALUES('tA','coder','X','PENDING')")
    con.commit()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    events.clear()
    con.execute("UPDATE team_work_runs SET current_card_id=NULL")
    con.commit()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    cn = names("coder")
    check("收尾 PostToolUse", "PostToolUse" in cn)
    check("Stop 离座", "Stop" in cn)

    # ── editing 重发 (持续打字动画) ──
    print("== case7: editing 持续期每轮重发 PreToolUse ==")
    reset(con)
    con.execute("INSERT INTO team_work_runs VALUES('r1','ACTIVE','tA')")
    con.execute("INSERT INTO harvest_contracts VALUES('tA','coder','X','PENDING')")
    con.commit()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    events.clear()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    pb.sync_from_ledger(ledger, REGISTRY, "")
    cn = names("coder")
    check("editing 重发 PreToolUse (2 轮 ≥2 次)",
          cn.count("PreToolUse") >= 2)

    print("== case8: idle 持续期零事件(不刷屏) ==")
    reset(con)
    pb.sync_from_ledger(ledger, REGISTRY, "")
    events.clear()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    pb.sync_from_ledger(ledger, REGISTRY, "")
    check("tester 持续 idle 零事件",
          len(names("tester")) == 0)

    # ── token 旋转 (侧卡重启) ──
    print("== case9: pixel-agents 重启(token 变) → 角色重新上线 ==")
    reset(con)
    con.execute("INSERT INTO team_work_runs VALUES('r1','ACTIVE','tA')")
    con.execute("INSERT INTO harvest_contracts VALUES('tA','coder','X','PENDING')")
    con.commit()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    events.clear()
    pp.pixel_office_token = lambda: "tok-rotated"
    pb.sync_from_ledger(ledger, REGISTRY, "")
    cn = names("coder")
    check("token 变触发重新上线(SessionStart)", "SessionStart" in cn)
    check("重新上线后恢复 editing(PreToolUse)", "PreToolUse" in cn)

    # ── main 唤醒路径 ──
    print("== case10: main 默认 idle_present (常驻工位, SessionStart + Notification) ==")
    reset(con)
    pb.sync_from_ledger(ledger, REGISTRY, "")
    mn = names("main")
    check("main 上线(SessionStart)", "SessionStart" in mn)
    # pixel-agents 要求 pending external session 等一个确认事件才创建。
    # main 首次进入 idle_present 时发 Notification(idle_prompt) 触发确认。
    check("main 补 Notification(idle_prompt) 触发确认",
          "Notification" in mn)
    check("main 默认不发 Stop", "Stop" not in mn)
    check("main 默认不发 PreToolUse", "PreToolUse" not in mn)
    # 多轮 sync, main 仍静默 (不再发任何事件)
    events.clear()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    pb.sync_from_ledger(ledger, REGISTRY, "")
    check("main 持续 idle_present 完全静默(零事件)",
          len(names("main")) == 0)

    print("== case11: main 被敲门唤醒 (task.stuck 30s 内) → editing ==")
    reset(con)
    con.execute("INSERT INTO mw_events(event_type,task_id,payload,created_at) "
                "VALUES('task.stuck','tA','{}',?)", (time.time(),))
    con.commit()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    mn = names("main")
    check("main 被敲门进入 editing", "PreToolUse" in mn)

    print("== case12: main 被 harvest 落账唤醒 (harvest.complete) → editing ==")
    reset(con)
    con.execute("INSERT INTO mw_events(event_type,task_id,payload,created_at) "
                "VALUES('harvest.complete','tA','{}',?)", (time.time(),))
    con.commit()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    check("main 被 harvest 唤醒进入 editing", "PreToolUse" in names("main"))

    print("== case13: main 唤醒事件超过 30s → 回 idle_present(不发 Stop) ==")
    reset(con)
    con.execute("INSERT INTO mw_events(event_type,task_id,payload,created_at) "
                "VALUES('task.stuck','tA','{}',?)", (time.time() - 60,))
    con.commit()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    events.clear()
    # 再跑一轮: main 应从 editing 回 idle_present, 但绝不发 Stop
    pb.sync_from_ledger(ledger, REGISTRY, "")
    # 上面: 第一轮 mw_events 60s 前还在 → main 进入 editing;
    #       第二轮同状态, editing 持续期重发 PreToolUse, 但已过 30s
    #       (因为 60s 前的事件已经在第一轮进入后立刻判定为不在窗口)
    # 实际: case13 第一轮会发 PreToolUse (60s 仍在窗口内 30s?) 不, 30s 窗口 60s 超,
    #       第一轮就不发 editing, 直接 idle_present
    check("main 唤醒窗口外不进 editing(60s > 30s 窗口)",
          "PreToolUse" not in names("main"))
    # 持续静默
    events.clear()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    check("main 持续 idle_present 零事件",
          len(names("main")) == 0)

    print("== case14: main 与 worker 同时 editing 不冲突 ==")
    reset(con)
    con.execute("INSERT INTO team_work_runs VALUES('r1','ACTIVE','tA')")
    con.execute("INSERT INTO harvest_contracts VALUES('tA','coder','X','PENDING')")
    con.execute("INSERT INTO mw_events(event_type,task_id,payload,created_at) "
                "VALUES('task.stuck','tA','{}',?)", (time.time(),))
    con.commit()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    check("coder 在 editing", "PreToolUse" in names("coder"))
    check("main 同时 editing", "PreToolUse" in names("main"))

    # ── 串行不变量 (2026-10-08 用户定调: 永远有人在跟进) ──
    print("== case15: worker 正在干活 (PENDING) → main 待命 idle_present ==")
    reset(con)
    con.execute("INSERT INTO team_work_runs VALUES('r1','ACTIVE','tA')")
    con.execute("INSERT INTO harvest_contracts VALUES('tA','coder','X','PENDING')")
    con.commit()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    mn = names("main")
    check("worker 持球时 main 不进 editing", "PreToolUse" not in mn)
    check("worker 持球时 main 是 idle_present (Notification)",
          "Notification" in mn)

    print("== case16: 有 ACTIVE run 但 worker 已交卷 (HARVESTED) → main 必须接手 ==")
    reset(con)
    con.execute("INSERT INTO team_work_runs VALUES('r1','ACTIVE','tA')")
    con.execute("INSERT INTO harvest_contracts VALUES('tA','coder','X','HARVESTED')")
    con.commit()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    check("worker 交卷后球回 main → main editing",
          "PreToolUse" in names("main"))

    print("== case17: 有 ACTIVE run 但 current_card 为空 (main 未 advance) → main 接手 ==")
    reset(con)
    con.execute("INSERT INTO team_work_runs VALUES('r1','ACTIVE',NULL)")
    con.commit()
    pb.sync_from_ledger(ledger, REGISTRY, "")
    check("无当前卡时 main editing (不出现无人跟进)",
          "PreToolUse" in names("main"))

    print("== case18: 无 ACTIVE run → main 回 idle_present (链已结束) ==")
    reset(con)
    pb.sync_from_ledger(ledger, REGISTRY, "")
    mn = names("main")
    check("无 run 时 main 不 editing", "PreToolUse" not in mn)
    check("无 run 时 main idle_present", "Notification" in mn)

    print(f"\n结果: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
