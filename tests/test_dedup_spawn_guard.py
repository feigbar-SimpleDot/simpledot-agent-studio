"""重复 spawn 护栏测试 (1A 边界, 2026-10-03 减法重构)。

场景: main agent 误重复 dispatch 同 task_id — mw 必须拒并返回现有 session,
让 main 去 resume 旧 session, 不预建第二个 session 把活做两遍。

bug 真实触发条件: 同 task 还在跑(harvest_status=PENDING, marker.started_at
存在但 exit_code=None)。这就是护栏要挡的情况。已结算的 task(HARVESTED)
不走这个护栏, 允许重新派发(新 worker 做新一轮)。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

def main():
    ROOT = Path(__file__).resolve().parent.parent
    BASE = Path("/tmp/mw_smoke_dedup")
    shutil.rmtree(BASE, ignore_errors=True)
    BASE.mkdir(parents=True)
    (BASE / "hermes").mkdir()

    env = dict(os.environ)
    env.update({
        "MW_HOME": str(BASE),
        "MW_HERMES_HOME": str(BASE / "hermes"),
        "MW_REGISTRY": str(ROOT / "tests" / "registry_local.json"),
        "MW_TASK_DIR_BASE": str(BASE / "tasks"),
        "MW_ALLOWED_WS_PREFIX": str(BASE),
        "PYTHONPATH": str(ROOT),
    })

    PASS, FAIL = [], []
    def check(name, cond, detail=""):
        (PASS if cond else FAIL).append(name)
        print(f"  {'✓' if cond else '✗'} {name} {detail}")

    VALID_MANIFEST = {
        "task_id": "t_dedup_1",
        "title": "写 hello.py",
        "target": {"profile": "coder"},
        "input_spec": {"task_description": "在工作目录创建 hello.py 内容为 hello", "context_files": []},
        "harvest": {"required_files": ["hello.py"], "timeout_seconds": 60},
        "idempotency_key": "dedup-test-1",
    }

    # fake spawn: 不连真实 worker gateway
    fake = '''
def fake_spawn(m, reg, ws, timeout, session_id=None):
    from pathlib import Path
    Path(ws).mkdir(parents=True, exist_ok=True)
    Path(ws, "hello.py").write_text("hello\\n")
    return {"runner_kind": "fake", "exit_code": 0,
            "stdout": "session_id: 20261003_FAKE0002\\nDONE", "stderr": "",
            "session_id": session_id or "20261003_FAKE0002",
            "container_id": None}
'''
    code = fake + '''
import os, sys, json
sys.path.insert(0, ".")
from mw.pipeline import dispatch
from mw.ledger import Ledger
r = dispatch(json.loads(sys.argv[1]), registry_path="tests/registry_local.json",
             ledger=Ledger(os.environ["MW_HOME"] + "/ledger.db"),
             spawn=fake_spawn, async_mode=False)
sys.stdout.write("__RESULT__" + json.dumps(r, ensure_ascii=False) + "\\n")
sys.stdout.flush()
'''
    BASE.joinpath("dedup_fake.py").write_text(code)


    def _set_state_local(task_id: str, harvest_status: str, *,
                         marker_exit_code=None):
        """手动拨回 ledger + marker 状态, 模拟 task 在跑 vs 已结算。

    marker_exit_code: None=清掉 exit_code(在跑); 显式值=覆盖(已结算)。
    bug 真实场景: task 还在跑, marker.exit_code 应为 None。
    """
        import time as _t
        sys.path.insert(0, str(ROOT))
        from mw.ledger import Ledger
        from mw.reaper import read_marker, write_marker
        base = str(BASE)
        led = Ledger(base + "/ledger.db")
        led.conn.execute(
            "UPDATE harvest_contracts SET harvest_status=? WHERE task_id=?",
            (harvest_status, task_id))
        old = read_marker(base, task_id) or {}
        old["started_at"] = _t.time() - 60
        if marker_exit_code is not None:
            old["exit_code"] = marker_exit_code
        else:
            old.pop("exit_code", None)
        marker_p = Path(base) / "spawn" / f"{task_id}.json"
        marker_p.parent.mkdir(parents=True, exist_ok=True)
        marker_p.write_text(json.dumps(old))
        led.conn.commit()
        led.close()


    # ── T1: 首次 dispatch 成功 + 拨回"在跑" ─────────────────
    print("== T1 首次 dispatch 成功 + 标 PENDING 模拟 task 在跑 ==")
    r = subprocess.run([sys.executable, str(BASE / "dedup_fake.py"),
                        json.dumps(VALID_MANIFEST)],
                       env=env, capture_output=True, text=True, cwd=ROOT)
    last = [l for l in r.stdout.splitlines() if l.startswith("__RESULT__")]
    if not last:
        check("首次 dispatch 成功", False, (r.stdout + r.stderr)[-300:])
        print(f"\n结果: {len(PASS)} passed, {len(FAIL)} failed (T1 挂, 后续无法跑)")
        sys.exit(1)
    first_out = json.loads(last[-1][len("__RESULT__"):])
    check("首次 dispatch 成功", first_out.get("ok") is True,
          json.dumps(first_out)[:150])
    first_tid = first_out.get("task_id")
    first_sid = first_out.get("session_id")

    # 关键步骤 — 把任务拨回"在跑"(harvest_status=PENDING, marker.exit_code=None)
    _set_state_local(first_tid, "PENDING", marker_exit_code=None)

    # ── T2: 同 task_id 重交 → 护栏挡 ────────────────────────
    print("== T2 同 task_id 重交被护栏挡 ==")
    r2 = subprocess.run([sys.executable, str(BASE / "dedup_fake.py"),
                         json.dumps(VALID_MANIFEST)],
                        env=env, capture_output=True, text=True, cwd=ROOT)
    last2 = [l for l in r2.stdout.splitlines() if l.startswith("__RESULT__")]
    if not last2:
        check("二次 dispatch 命中护栏", False, (r2.stdout + r2.stderr)[-300:])
    else:
        second_out = json.loads(last2[-1][len("__RESULT__"):])
        check("二次 dispatch 命中护栏 (rejected)",
              second_out.get("rejected") == "already_running",
              json.dumps(second_out)[:200])
        check("返回的 session_id 等于原 session",
              second_out.get("session_id") == first_sid,
              f"first={first_sid} second={second_out.get('session_id')}")
        check("ok=false", second_out.get("ok") is False)

    # ── T3: 不同 idempotency_key 但同 task_id → 护栏仍挡 ────
    # 1A: 护栏按 task_id 判 (不再依赖 kanban create), 不同 key 同 task 也挡。
    print("== T3 不同 idempotency_key 但同 task_id → 护栏仍挡 ==")
    DIFF_KEY = dict(VALID_MANIFEST)
    DIFF_KEY["idempotency_key"] = "dedup-test-1-DIFFERENT"
    r3 = subprocess.run([sys.executable, str(BASE / "dedup_fake.py"),
                         json.dumps(DIFF_KEY)],
                        env=env, capture_output=True, text=True, cwd=ROOT)
    last3 = [l for l in r3.stdout.splitlines() if l.startswith("__RESULT__")]
    if not last3:
        check("不同 key 同 task 被挡", False, (r3.stdout + r3.stderr)[-300:])
    else:
        third_out = json.loads(last3[-1][len("__RESULT__"):])
        check("不同 key 同 task_id 仍被护栏挡 (按 task_id 判)",
              third_out.get("rejected") == "already_running",
              json.dumps(third_out)[:200])

    # ── T4: 任务已结算后 (HARVESTED, marker.exit_code=0) 护栏不挡 ──
    print("== T4 已结算后护栏不挡 (正常推进) ==")
    _set_state_local(first_tid, "HARVESTED", marker_exit_code=0)
    r4 = subprocess.run([sys.executable, str(BASE / "dedup_fake.py"),
                         json.dumps(VALID_MANIFEST)],
                        env=env, capture_output=True, text=True, cwd=ROOT)
    last4 = [l for l in r4.stdout.splitlines() if l.startswith("__RESULT__")]
    if not last4:
        check("已结算后护栏不挡", False, (r4.stdout + r4.stderr)[-300:])
    else:
        out4 = json.loads(last4[-1][len("__RESULT__"):])
        check("已结算后护栏不挡 (正常路径)",
              "already_running" not in json.dumps(out4) and out4.get("ok") is True,
              json.dumps(out4)[:300])

    print(f"\n结果: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)



if __name__ == "__main__":
    main()
