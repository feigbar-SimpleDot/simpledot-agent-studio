"""派发管道测试 (1A 边界, 2026-10-03 减法重构)。

mw 不再建 kanban 卡 —— dispatch 只为 main 已建好/已 claim 的卡起 worker。
fake worker 注入, 验证确定性逻辑 (校验/记账/marker/护栏), 不依赖 LLM。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

def main():
    ROOT = Path(__file__).resolve().parent.parent
    BASE = Path("/tmp/mw_smoke")
    shutil.rmtree(BASE, ignore_errors=True)
    BASE.mkdir(parents=True)

    env = dict(os.environ)
    env.update({
        "MW_HOME": str(BASE),
        "MW_HERMES_HOME": str(BASE / "hermes"),
        "MW_REGISTRY": str(ROOT / "tests" / "registry_local.json"),
        "MW_TASK_DIR_BASE": str(BASE / "tasks"),
        "MW_ALLOWED_WS_PREFIX": str(BASE),
    })

    PASS, FAIL = [], []
    def check(name, cond, detail=""):
        (PASS if cond else FAIL).append(name)
        print(f"  {'✓' if cond else '✗'} {name} {detail}")

    VALID = {
        "task_id": "t_main_created_1",
        "title": "写 hello.py",
        "target": {"profile": "coder"},
        "input_spec": {"task_description": "在工作目录创建 hello.py 内容为 hello", "context_files": []},
        "harvest": {"required_files": ["hello.py"], "timeout_seconds": 60},
        "idempotency_key": "smoke-1",
    }

    print("== T1 schema 拒绝: 缺 harvest ==")
    bad = {k: v for k, v in VALID.items() if k != "harvest"}
    r = subprocess.run([sys.executable, "-m", "mw.cli", "dispatch", "--manifest", "-"],
                       env=env, capture_output=True, text=True, input=json.dumps(bad), cwd=ROOT)
    check("422 拒绝", "[mw] 422" in r.stderr and r.returncode != 0, f"(rc={r.returncode})")

    print("== T2 幽灵 profile 拒绝 ==")
    ghost = json.loads(json.dumps(VALID)); ghost["target"]["profile"] = "ghost_999"
    ghost["idempotency_key"] = "smoke-ghost"
    r = subprocess.run([sys.executable, "-m", "mw.cli", "dispatch", "--manifest", "-"],
                       env=env, capture_output=True, text=True, input=json.dumps(ghost), cwd=ROOT)
    check("422 拒绝", "幽灵 profile" in r.stderr and r.returncode != 0)

    print("== T3 缺 task_id 拒绝 (mw 不建卡) ==")
    notid = json.loads(json.dumps(VALID)); del notid["task_id"]
    notid["idempotency_key"] = "smoke-notid"
    r = subprocess.run([sys.executable, "-m", "mw.cli", "dispatch", "--manifest", "-"],
                       env=env, capture_output=True, text=True, input=json.dumps(notid), cwd=ROOT)
    check("422 拒绝", "task_id 必填" in r.stderr and r.returncode != 0)

    print("== T4 夹带 auto_retry 拒绝 ==")
    sneaky = json.loads(json.dumps(VALID))
    sneaky["harvest"]["auto_retry"] = True
    sneaky["idempotency_key"] = "smoke-sneak"
    r = subprocess.run([sys.executable, "-m", "mw.cli", "dispatch", "--manifest", "-"],
                       env=env, capture_output=True, text=True, input=json.dumps(sneaky), cwd=ROOT)
    check("422 拒绝", "[mw] 422" in r.stderr and "auto_retry" in r.stderr)

    print("== T5 同步派发成功路径: session 锚定 + marker + ledger ==")
    fake = '''
def fake_spawn(m, reg, ws, timeout, session_id=None):
    from pathlib import Path
    Path(ws).mkdir(parents=True, exist_ok=True)
    Path(ws, "hello.py").write_text("hello\\n")
    return {"runner_kind": "api", "exit_code": 0,
            "stdout": "session_id: 20261003_FAKE0001\\nDONE", "stderr": "",
            "session_id": session_id or "20261003_FAKE0001",
            "container_id": None}
'''
    code = fake + '''
import os, sys, json
sys.path.insert(0, ".")
from mw.pipeline import dispatch
from mw.ledger import Ledger
r = dispatch(json.loads(sys.argv[1]),
             registry_path="tests/registry_local.json",
             ledger=Ledger(os.environ["MW_HOME"] + "/ledger.db"),
             spawn=fake_spawn, async_mode=False)
print(json.dumps(r, ensure_ascii=False))
'''
    BASE.joinpath("fake_ok.py").write_text(code)
    r = subprocess.run([sys.executable, str(BASE / "fake_ok.py"), json.dumps(VALID)],
                       env=env, capture_output=True, text=True, cwd=ROOT)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        out = {}
        check("T5 运行", False, r.stderr[:300])
    if out:
        check("dispatch ok", out.get("ok") is True, json.dumps(out)[:200])
        check("session 锚定", out.get("session_id") == "20261003_FAKE0001")
        check("workspace 返回", bool(out.get("workspace")))
        # marker 已写 (started_at + exit_code)
        mk = BASE / "spawn" / f"{VALID['task_id']}.json"
        check("marker 落盘", mk.exists())
        if mk.exists():
            m = json.loads(mk.read_text())
            check("marker.started_at", m.get("started_at") is not None)
        # ledger 记账
        checker = BASE / "check_ledger.py"
        checker.write_text(
            "import os, json, sqlite3\n"
            "c = sqlite3.connect(os.environ['MW_HOME'] + '/ledger.db')\n"
            "c.row_factory = sqlite3.Row\n"
            "print(json.dumps({\n"
            "  'sess': [dict(x) for x in c.execute('SELECT task_id,session_id,profile FROM task_sessions')],\n"
            "  'contract': [dict(x) for x in c.execute('SELECT task_id,harvest_status FROM harvest_contracts')],\n"
            "  'submit': [dict(x) for x in c.execute('SELECT idempotency_key,task_id FROM submit_log')],\n"
            "}))\n")
        led = subprocess.run([sys.executable, str(checker)],
                            env=env, capture_output=True, text=True)
        try:
            lj = json.loads(led.stdout)
            check("ledger session 记录", any(s["task_id"] == VALID["task_id"] for s in lj["sess"]))
            check("ledger contract 记录", any(c["task_id"] == VALID["task_id"] for c in lj["contract"]))
            check("ledger submit_log 记录", any(s["task_id"] == VALID["task_id"] for s in lj["submit"]))
        except Exception:
            check("ledger 记录", False, led.stdout[:200] + led.stderr[:200])

    print("== T6 重复派发护栏: 同 task 活跃 session → 拒 ==")
    # 先把 T5 的 session 标成 running (marker 无 exit_code 视为活跃)
    mk = BASE / "spawn" / f"{VALID['task_id']}.json"
    if mk.exists():
        m = json.loads(mk.read_text())
        m.pop("exit_code", None)
        mk.write_text(json.dumps(m))
    r = subprocess.run([sys.executable, "-m", "mw.cli", "dispatch", "--manifest", "-"],
                       env=env, capture_output=True, text=True, input=json.dumps(VALID), cwd=ROOT)
    try:
        out2 = json.loads(r.stdout)
        check("rejected already_running",
              out2.get("rejected") == "already_running" and out2.get("ok") is False,
              json.dumps(out2)[:200])
    except Exception:
        check("rejected already_running", False, (r.stdout + r.stderr)[:300])

    print("== T7 工作区白名单: project_workspace 越权 → 422 ==")
    esc = json.loads(json.dumps(VALID))
    esc["task_id"] = "t_esc"
    esc["idempotency_key"] = "smoke-esc"
    esc["project_workspace"] = "/etc/passwd_dir"
    r = subprocess.run([sys.executable, "-m", "mw.cli", "dispatch", "--manifest", "-"],
                       env=env, capture_output=True, text=True, input=json.dumps(esc), cwd=ROOT)
    check("422 越权拒绝", "越权拒绝" in r.stderr and r.returncode != 0)

    print(f"\n最终结果: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)



if __name__ == "__main__":
    main()
