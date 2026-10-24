#!/usr/bin/env python3
"""问题1B: assignee 语义匹配是 main 的职责, mw 只做确定性校验。

1A 边界 (2026-10-03): launch 每步新增 task_id 必填 (mw 不建卡)。
核心断言不变:
- mw 绝不挑人(assignee 缺失 → 400, 不自动路由)
- mw 绝不调 LLM(校验逻辑里无网络调用)
- mw 不做 tag 字符串交集拒绝(main 语义选了 tag 字面不匹配的 worker, mw 放行)
- mw 只守确定性硬边界: assignee 存在性 + worker.excludes 红线 + must_split

跑法 (script-style, #67 整改后):
  - 直接: python3 tests/test_assignee_semantic.py   (跑真测试, 行为与改造前一致)
  - pytest: pytest tests/                            (import 即跳过, 不再 INTERNALERROR)
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mw.console as C

FAKE_REGISTRY = {
    "coder": {
        "runner": "api", "api_url": "http://x:1",
        "scope": {"tags": ["code-write", "debug", "test"],
                  "excludes": ["security-audit", "prod-deploy"]},
    },
    "code-reviewer": {
        "runner": "api", "api_url": "http://x:2",
        "scope": {"tags": ["review", "code-quality"],
                  "excludes": ["code-write"]},
    },
    "architect": {
        "runner": "api", "api_url": "http://x:3",
        "description": "架构设计 / 系统设计 / 技术方案",
        "scope": {"tags": ["架构设计", "技术方案"], "excludes": []},
    },
}

# 被派发的 body 记录 (stub_dispatch 写入)
DISPATCHED = []

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def base_step(**over):
    s = {"step_id": "s1", "title": "T", "task_id": "t_main_1",
         "task_description": "d"}
    s.update(over)
    return s


def launch(steps, **extra):
    DISPATCHED.clear()
    p = {"initial_task": "需求", "session_id": "sess1", "steps": steps}
    p.update(extra)
    return C.api_team_work_launch(p)


def main():
    global FAIL
    # 让 api_workers 返回假 registry, 不碰真实文件
    C.api_workers = lambda: {"workers": FAKE_REGISTRY}

    # stub 掉真实派发: 校验通过的用例不应连 worker gateway。
    # 记录被派发的 body, 返回 accepted。
    def _stub_dispatch(payload):
        DISPATCHED.append(payload)
        return {"ok": True, "accepted": True, "task_id": payload.get("task_id"),
                "session_id": "sess_stub", "workspace": "/tmp/stub"}
    C.api_dispatch = _stub_dispatch

    # record_initiator 落 ledger 会碰真实 DB, stub 掉
    # 2026-10-03 T2: launch 现在还会建 team_work_runs 行 (get_active_team_work /
    # create_team_work_run / register_team_work_card / set_team_work_current_card),
    # 派发失败回滚还会 archive_team_work_cards + close_team_work。一并 stub。
    class _StubLedger:
        _active = None  # 模拟「无 ACTIVE run」, 让第二个 launch 测试走 happy path
        def get_active_team_work(self):
            return None
        def create_team_work_run(self, run_id, session_id, initial_task,
                                project_workspace=None):
            _StubLedger._active = run_id
            return {"run_id": run_id, "status": "ACTIVE", "card_count": 0}
        def put_contract(self, task_id, contract, required_passes=None):
            pass
        def register_team_work_card(self, run_id, task_id):
            pass
        def set_team_work_current_card(self, run_id, task_id):
            pass
        def archive_team_work_cards(self, run_id):
            return []
        def close_team_work(self, run_id, reason):
            pass
        def record_initiator(self, task_id, session_id):
            pass
        def close(self):
            pass
    C.Ledger = lambda *a, **k: _StubLedger()

    print("== T1B-1: assignee 缺失 → 400, mw 不自动挑人 ==")
    r = launch([base_step(required_role_tags=["arch", "design"])])
    check("缺 assignee 返 400", r.get("code") == 400, json.dumps(r, ensure_ascii=False)[:120])
    check("错误信息说明是 main 的语义决策", "main" in (r.get("error") or ""),
          r.get("error", ""))

    print("== T1B-2: assignee 不在 registry → 422 ==")
    r = launch([base_step(assignee="ghost")])
    check("未知 assignee 返 422", r.get("code") == 422, json.dumps(r, ensure_ascii=False)[:120])

    print("== T1B-2b: 缺 task_id → 400 (mw 不建卡) ==")
    s = base_step(assignee="coder")
    del s["task_id"]
    r = launch([s])
    check("缺 task_id 返 400", r.get("code") == 400, json.dumps(r, ensure_ascii=False)[:120])
    check("错误说明 mw 不建卡", "不建卡" in (r.get("error") or ""), r.get("error", ""))

    print("== T1B-3: 语义匹配跨中英文 — main 选了 architect(中文tags)做 design 步, mw 放行 ==")
    # 关键: required_role_tags=['arch','design'] 与 architect 的中文 tags ['架构设计','技术方案']
    # 字面零交集, 但 main 语义判断 architect 合适 → mw 不得用字符串交集拒绝
    r = launch([base_step(assignee="architect", required_role_tags=["arch", "design"])])
    rejected_by_tags = (r.get("code") == 422 and "能力不匹配" in (r.get("error") or ""))
    check("不再用 tag 字符串交集拒绝语义匹配", not rejected_by_tags,
          json.dumps(r, ensure_ascii=False)[:160])
    check("校验通过后派发了第一张", r.get("ok") is True and len(DISPATCHED) == 1,
          json.dumps(r, ensure_ascii=False)[:160])

    print("== T1B-4: worker.excludes 红线 — coder 拒绝 security-audit 角色 ==")
    r = launch([base_step(assignee="coder", required_role_tags=["security-audit"])])
    check("excludes 命中返 422", r.get("code") == 422, json.dumps(r, ensure_ascii=False)[:120])
    check("错误提到 excludes", "excludes" in (r.get("error") or ""), r.get("error", ""))

    print("== T1B-5: must_split 同 assignee 复用 → 422 ==")
    r = launch([
        base_step(step_id="impl", task_id="t_a", assignee="coder"),
        base_step(step_id="review", task_id="t_b", assignee="coder", split_policy="must_split"),
    ])
    check("must_split 复用同 worker 返 422", r.get("code") == 422,
          json.dumps(r, ensure_ascii=False)[:120])
    check("标记 must_split_violation", r.get("must_split_violation") is True,
          json.dumps(r, ensure_ascii=False)[:120])

    print("== T1B-6: must_split 换 worker 通过校验(coder→code-reviewer) ==")
    r = launch([
        base_step(step_id="impl", task_id="t_a", assignee="coder"),
        base_step(step_id="review", task_id="t_b", assignee="code-reviewer", split_policy="must_split"),
    ])
    check("换 worker 不触发 must_split_violation",
          not r.get("must_split_violation"), json.dumps(r, ensure_ascii=False)[:120])

    print("== T1B-7: 严格串行 — launch 只派第一张, 不派全部 ==")
    r = launch([
        base_step(step_id="s1", task_id="t_1", assignee="coder"),
        base_step(step_id="s2", task_id="t_2", assignee="code-reviewer"),
        base_step(step_id="s3", task_id="t_3", assignee="architect"),
    ])
    check("只派发 1 张 (严格串行)", len(DISPATCHED) == 1,
          f"dispatched={len(DISPATCHED)}")
    check("派的是第一张 t_1", DISPATCHED and DISPATCHED[0]["task_id"] == "t_1",
          json.dumps(DISPATCHED[:1]))
    check("返回 dispatched=第一张 且 cards=3", r.get("dispatched") == "t_1"
          and r.get("card_count") == 3, json.dumps(r, ensure_ascii=False)[:160])

    print("== T1B-8: 校验逻辑无 LLM 网络调用(源码静态检查) ==")
    src = open(os.path.join(os.path.dirname(C.__file__), "console.py"), encoding="utf-8").read()
    # launch 校验段(从 def api_team_work_launch 到 def api_dispatch)不应含 chat/completions 或 _llm_pick
    seg = src[src.index("def api_team_work_launch"):src.index("def api_dispatch")]
    check("launch 校验段无 chat/completions 调用", "chat/completions" not in seg)
    check("launch 校验段无 _llm_pick_worker", "_llm_pick_worker" not in seg)
    check("全文件无 _resolve_assignee_by_tags 死代码", "_resolve_assignee_by_tags" not in src)

    print(f"\n结果: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
