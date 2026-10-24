"""Workflow step v2 字段测试: description / input / output (+ depends_on 透传修复)。

覆盖:
  1. _validate_workflow_body 接受新字段
  2. description 超 500 字 → 拒
  3. input/output 非字符串数组 → 拒
  4. depends_on 自指 / 引用未定义 step → 拒
  5. api_workflow_save 把新字段写回 yaml (round-trip 不丢)
  6. launch 把 step.input → dispatch.input_spec.context_files,
     step.description → task_description 兜底
  7. advance 同样透传 input / description
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mw.console as console

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))


def stub_workers_registry():
    console.REGISTRY = "/tmp/_test_wf_io_registry.json"
    Path(console.REGISTRY).write_text(
        __import__("json").dumps(
            {"coder": {"api_url": "http://stub", "api_key": "x" * 32}}))


def stub_dispatch_recorder():
    calls = []
    original = console.api_dispatch

    def _stub(payload):
        calls.append(dict(payload))
        return {"ok": True, "accepted": True, "task_id": payload.get("task_id")}

    console.api_dispatch = _stub
    return calls, lambda: setattr(console, "api_dispatch", original)


# ── 1. 校验: 接受新字段 ──────────────────────────────────────
def test_validate_accepts_new_fields():
    ok, err = console._validate_workflow_body({
        "name": "W",
        "steps": [{
            "id": "a", "name": "A",
            "description": "做一件小事",
            "input": ["docs/req.md"],
            "output": ["docs/design.md"],
            "required_role_tags": ["code-write"],
        }],
    })
    check("validate accepts description/input/output", ok, err)


# ── 2. description 超长拒收 ─────────────────────────────────
def test_validate_rejects_long_description():
    ok, err = console._validate_workflow_body({
        "name": "W",
        "steps": [{
            "id": "a", "name": "A", "required_role_tags": [],
            "description": "超" * 501,
        }],
    })
    check("validate rejects description > 500", not ok and "超长" in err, err)
    # 边界: 恰好 500 收 (Issue #52 放宽后, ASCII 与中文都按 len 字符数计)
    ok500, err500 = console._validate_workflow_body({
        "name": "W",
        "steps": [{
            "id": "a", "name": "A", "required_role_tags": [],
            "description": "x" * 500,
        }],
    })
    check("validate accepts description == 500", ok500 and not err500, err500)
    # 中文字符边界 (Issue #52 同步 § AC-52-03)
    ok_zh, err_zh = console._validate_workflow_body({
        "name": "W",
        "steps": [{
            "id": "a", "name": "A", "required_role_tags": [],
            "description": "步" * 500,
        }],
    })
    check("validate accepts 500 CJK chars", ok_zh and not err_zh, err_zh)


# ── 3. input/output 类型 ────────────────────────────────────
def test_validate_rejects_bad_input_output():
    ok, _ = console._validate_workflow_body({
        "name": "W",
        "steps": [{"id": "a", "name": "A", "required_role_tags": [],
                   "input": "not-a-list"}],
    })
    check("validate rejects input=string", not ok)
    ok2, _ = console._validate_workflow_body({
        "name": "W",
        "steps": [{"id": "a", "name": "A", "required_role_tags": [],
                   "output": [1, 2]}],
    })
    check("validate rejects output=non-str-list", not ok2)


# ── 4. depends_on 引用校验 ──────────────────────────────────
def test_validate_depends_on_refs():
    ok, err = console._validate_workflow_body({
        "name": "W",
        "steps": [
            {"id": "a", "name": "A", "required_role_tags": [], "depends_on": ["a"]},
        ],
    })
    check("validate rejects depends_on self-ref", not ok and "自指" in err, err)
    ok2, err2 = console._validate_workflow_body({
        "name": "W",
        "steps": [
            {"id": "a", "name": "A", "required_role_tags": []},
            {"id": "b", "name": "B", "required_role_tags": [], "depends_on": ["zzz"]},
        ],
    })
    check("validate rejects depends_on undefined", not ok2 and "未定义" in err2, err2)


# ── 5. save round-trip 不丢新字段 ───────────────────────────
def test_save_roundtrip_preserves_fields():
    src = tempfile.NamedTemporaryFile(suffix=".yaml", delete=False)
    src.close()
    cache = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    cache.close()
    os.environ["MW_WORKFLOWS_PATH"] = src.name
    os.environ["MW_WORKFLOWS_CACHE"] = cache.name
    try:
        r = console.api_workflow_save("io-test", {
            "name": "IO 流程",
            "description": "d",
            "steps": [{
                "id": "a", "name": "A",
                "description": "步骤简述",
                "input": ["in1.md", "in2.md"],
                "output": ["out.md"],
                "default_assignee": "coder",
                "required_role_tags": ["code-write"],
                "depends_on": [],
            }],
        })
        check("save ok", r.get("ok"), str(r))
        import yaml
        saved = yaml.safe_load(open(src.name))["workflows"]["io-test"]["steps"][0]
        check("description persisted", saved.get("description") == "步骤简述", str(saved))
        check("input persisted", saved.get("input") == ["in1.md", "in2.md"], str(saved))
        check("output persisted", saved.get("output") == ["out.md"], str(saved))
        check("default_assignee persisted", saved.get("default_assignee") == "coder", str(saved))
    finally:
        for p in (src.name, cache.name):
            try:
                os.unlink(p)
            except OSError:
                pass
        os.environ.pop("MW_WORKFLOWS_PATH", None)
        os.environ.pop("MW_WORKFLOWS_CACHE", None)


# ── 6. launch 透传 input → context_files ────────────────────
def test_launch_passes_input_to_context_files():
    led_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    led_path.close()
    console.LEDGER_DB = led_path.name
    stub_workers_registry()
    calls, restore = stub_dispatch_recorder()
    try:
        r = console.api_team_work_launch({
            "initial_task": "T", "session_id": "s",
            "steps": [{
                "step_id": "a", "title": "A", "task_id": "t_a", "assignee": "coder",
                "description": "步骤简述",
                "input": ["docs/req.md"],
            }],
        })
        check("launch ok", r.get("ok"), str(r))
        spec = calls[0]["input_spec"]
        check("context_files from input", spec.get("context_files") == ["docs/req.md"], str(spec))
        check("task_description falls back to description",
              spec.get("task_description") == "步骤简述", str(spec))
    finally:
        restore()
        os.unlink(led_path.name)


# ── 7. advance 透传 input / description ─────────────────────
def test_advance_passes_input_and_description():
    led_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    led_path.close()
    console.LEDGER_DB = led_path.name
    led = console.Ledger(led_path.name)
    # REOPEN A-1: advance 机器校验 TODO 绑定, 补临时 TODO 夹具指向 t_b
    ws = tempfile.mkdtemp()
    Path(ws, "TODO.md").write_text(
        "# Team Work: T\n\n## 当前任务\n- 步骤: b\n- 卡: t_b\n"
        "- 状态: 进行中\n\n---\n\n## 待执行\n(无)\n", encoding="utf-8")
    led.create_team_work_run("run1", "s", "T", project_workspace=ws)
    led.put_contract("t_b", {"required_files": []})
    led.register_team_work_card("run1", "t_b")
    led.close()
    calls, restore = stub_dispatch_recorder()
    try:
        r = console.api_team_work_advance("run1", {
            "task_id": "t_b", "target_profile": "coder",
            "title": "B", "description": "推进简述", "input": ["docs/design.md"],
        })
        check("advance dispatched", calls and calls[0]["task_id"] == "t_b", str(r))
        spec = calls[0]["input_spec"]
        check("advance context_files", spec.get("context_files") == ["docs/design.md"], str(spec))
        check("advance task_description from description",
              spec.get("task_description") == "推进简述", str(spec))
    finally:
        restore()
        os.unlink(led_path.name)


if __name__ == "__main__":
    for fn in [test_validate_accepts_new_fields,
               test_validate_rejects_long_description,
               test_validate_rejects_bad_input_output,
               test_validate_depends_on_refs,
               test_save_roundtrip_preserves_fields,
               test_launch_passes_input_to_context_files,
               test_advance_passes_input_and_description]:
        fn()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
