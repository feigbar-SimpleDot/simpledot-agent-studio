"""I18N-R3 (2026-10-16) 双语 TODO.md 测试 (REQ §4.2 B1 兼容矩阵)。

覆盖:
  AC-5: 解析器 (parse / parse_status / write_status_atomic /
        current_binding) 同时识别中英段头 + 中英字段名, 双语 fixture
        全部正确读取状态; 既有 todo/reaper 测试零失败 (其他测试文件)。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mw.todo import (
    parse, TodoTask, TodoDoc, init_todo, write, current_binding,
    parse_status, write_status_atomic,
    BILINGUAL_HEADER_ALIASES, BILINGUAL_FIELD_ALIASES,
)


# ── en 部署: 渲染端输出英文键, 解析端双键通吃 ──────────────────

def test_en_render_uses_english_keys(monkeypatch):
    """en 部署 (MW_LANG=en) → render() 用英文段头 + 英文字段键。"""
    monkeypatch.setenv("MW_LANG", "en")
    # reload module to pick up env
    import importlib, mw.todo as _todo_mod
    importlib.reload(_todo_mod)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "TODO.md"
        _todo_mod.init_todo(p, "test", _todo_mod.TodoTask(
            step_id="step1", title="T1", goal="goal1",
            assignee="coder", product_workspace="/x"))
        text = p.read_text()
        assert "## Current Task" in text, text[:300]
        assert "- step: step1" in text, text
        assert "- title: T1" in text, text
        assert "- goal: goal1" in text, text
        assert "- assignee: coder" in text, text
        assert "- product_workspace: /x" in text, text
        assert "## Pending" in text
        assert "## Done" in text
        assert "## Dropped" in text


def test_en_render_status_literal(monkeypatch):
    """en 部署 → 状态值不翻译, reaper 兼容 (working/in-progress/进行中)。"""
    monkeypatch.setenv("MW_LANG", "en")
    import importlib, mw.todo as _todo_mod
    importlib.reload(_todo_mod)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "TODO.md"
        _todo_mod.init_todo(p, "test", _todo_mod.TodoTask(
            step_id="s", title="X", goal="g", assignee="coder"))
        # 状态值原样写
        assert "- status: -" in p.read_text() or "- status: 进行中" in p.read_text()


def test_zh_cn_render_byte_identical_baseline(monkeypatch):
    """zh-cn 部署 → 渲染逐字节保留基线 (af25139)。"""
    monkeypatch.setenv("MW_LANG", "zh-cn")
    import importlib, mw.todo as _todo_mod
    importlib.reload(_todo_mod)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "TODO.md"
        _todo_mod.init_todo(p, "test", _todo_mod.TodoTask(
            step_id="step1", title="T1", goal="goal1",
            assignee="coder", product_workspace="/x"))
        text = p.read_text()
        assert "## 当前任务" in text
        assert "- 步骤: step1" in text
        assert "- 标题: T1" in text
        assert "- 目标: goal1" in text
        assert "- 承接方: coder" in text
        assert "- 产物目录: /x" in text
        assert "## 待执行" in text
        assert "## 已完成" in text
        assert "## 已放弃" in text


# ── 解析端双键通吃 ──────────────────────────────────

def test_parse_english_todo():
    """en 部署写英文 TODO.md → 解析端正确读取所有字段。"""
    text_en = """# Team Work: test

## Current Task
- step: step1
- title: T1
- goal: goal1
- assignee: coder
- product_workspace: /x
- status: completed
- result: done
- kanban_task: t_x

---

## Pending
- []

## Done
(none)

## Dropped
(none)
"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "TODO.md"
        p.write_text(text_en)
        doc = parse(p)
        assert doc is not None
        assert doc.title == "test"
        assert doc.current is not None
        assert doc.current.step_id == "step1"
        assert doc.current.title == "T1"
        assert doc.current.goal == "goal1"
        assert doc.current.assignee == "coder"
        assert doc.current.product_workspace == "/x"
        assert doc.current.kanban_task_id == "t_x"


def test_parse_chinese_todo():
    """zh 部署写中文 TODO.md → 解析端正确读取。"""
    text_zh = """# Team Work: test

## 当前任务
- 步骤: step1
- 标题: T1
- 目标: goal1
- 承接方: coder
- 产物目录: /x
- 状态: 进行中
- 结果: (worker 完成后填写)
- kanban_task: t_y

---

## 待执行
- []

## 已完成
(none)

## 已放弃
(none)
"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "TODO.md"
        p.write_text(text_zh)
        doc = parse(p)
        assert doc is not None
        assert doc.current.step_id == "step1"
        assert doc.current.goal == "goal1"
        assert doc.current.assignee == "coder"
        assert doc.current.kanban_task_id == "t_y"


# ── parse_status / write_status_atomic ──────────────────

def test_parse_status_english_section():
    """parse_status 识别 `## Current Task` + `- status: <v>` 行。"""
    text_en = """# Team Work: t

## Current Task
- status: working
- result: ...

## Pending
"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "TODO.md"
        p.write_text(text_en)
        assert parse_status(p) == "working"


def test_parse_status_chinese_section():
    """parse_status 识别 `## 当前任务` + `- 状态: <v>` 行。"""
    text_zh = """# Team Work: t

## 当前任务
- 状态: 进行中
- 结果: ...

## 待执行
"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "TODO.md"
        p.write_text(text_zh)
        assert parse_status(p) == "进行中"


def test_write_status_atomic_english():
    """write_status_atomic 在英文 TODO.md 上工作。"""
    text_en = """# Team Work: t

## Current Task
- status: working
- result: ...

## Pending
"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "TODO.md"
        p.write_text(text_en)
        assert write_status_atomic(p, "completed")
        text = p.read_text()
        assert "- status: completed" in text
        assert "- result: ..." in text  # 其它字段未变


def test_write_status_atomic_chinese():
    """write_status_atomic 在中文 TODO.md 上工作。"""
    text_zh = """# Team Work: t

## 当前任务
- 状态: 进行中
- 结果: ...

## 待执行
"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "TODO.md"
        p.write_text(text_zh)
        assert write_status_atomic(p, "working")
        text = p.read_text()
        assert "- 状态: working" in text
        assert "- 结果: ..." in text


# ── current_binding ──────────────────

def test_current_binding_english_todo():
    """current_binding 读英文 TODO.md 返回规范 dict。"""
    text_en = """# Team Work: t

## Current Task
- step: s1
- title: T
- status: completed
- kanban_task: t_x

---

## Pending
"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "TODO.md"
        p.write_text(text_en)
        b = current_binding(p)
        assert b is not None
        assert b["step_id"] == "s1"
        assert b["title"] == "T"
        assert b["status"] == "completed"
        assert b["kanban_task"] == "t_x"


def test_current_binding_kanban_task_id_alias():
    """en 部署 main 写 `kanban_task_id:` 也被识别 (与 `kanban_task:` / `卡:` 同源)。"""
    text = """# Team Work: t

## Current Task
- step: s1
- kanban_task_id: t_alias

## Pending
"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "TODO.md"
        p.write_text(text)
        b = current_binding(p)
        assert b["kanban_task"] == "t_alias"


# ── 别名表完整性 ──────────────────

def test_aliases_dicts_have_full_coverage():
    """BILINGUAL_*_ALIASES 必备段头 / 字段名。"""
    for cn in ("当前任务", "待执行", "已完成", "已放弃"):
        assert cn in BILINGUAL_HEADER_ALIASES
    for cn in ("状态", "结果", "目标", "承接方", "产物目录", "产出文件",
              "标题", "步骤"):
        assert cn in BILINGUAL_FIELD_ALIASES
    for en in ("kanban_task", "kanban_task_id", "卡"):
        assert en in BILINGUAL_FIELD_ALIASES


def test_aliases_backward_compat():
    """解析端 `kanban_task:` 与 `kanban_task_id:` 与 `卡:` 等价。"""
    # 验证 BILINGUAL_FIELD_ALIASES['kanban_task'][0] == 'kanban_task' (自身)
    assert BILINGUAL_FIELD_ALIASES["kanban_task"][0] == "kanban_task"
    assert BILINGUAL_FIELD_ALIASES["kanban_task_id"][0] == "kanban_task"
    assert BILINGUAL_FIELD_ALIASES["卡"][0] == "kanban_task"


# ── I18N-R3-FIX (2026-10-16, CRITICAL/HIGH/M-1/M-3/M-4) ────────────
# 每条覆盖一项具体 review fix; fixture 真实写入 TODO.md → render →
# parse 回环 / 关键状态对账, 不只 grep 源码.

def test_parse_english_todo_pending_english_keys_roundtrip(monkeypatch):
    """H-3 修复: en 部署 render → parse 回环保留 pending 子字段.

    rotate_next 升 current 时若 en 部署丢了 goal/product_workspace/
    required_files 三个子字段, 下游卡会拿空工作目录. 实测基线
    (161b78c) 在英文 Pending 段写这些子字段后 rotate 即丢失.
    """
    # Step 1: 用 en 部署 (MW_LANG=en) render 出含 Pending + 英文子字段的 TODO.md
    # (monkeypatch 保证 env 用例内生效、用例后自动还原, 不污染后续测试)
    monkeypatch.setenv("MW_LANG", "en")
    import importlib, mw.todo as _todo_mod
    importlib.reload(_todo_mod)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "TODO.md"
        _todo_mod.init_from_steps(p, "t", [
            _todo_mod.TodoTask(step_id="step1", title="T1", goal="g1",
                              assignee="coder", product_workspace="/x",
                              required_files=["a.py"],
                              kanban_task_id="t_x"),
            _todo_mod.TodoTask(step_id="step2", title="T2", goal="g2",
                              assignee="tester", product_workspace="/y",
                              required_files=["b.py"],
                              kanban_task_id="t_y"),
        ])
        text = p.read_text()
        # pending 段以两空格缩进渲染 (与 current 段前缀 `- ` 不同)
        assert "  goal: g2" in text, f"Pending子字段未以en渲染: {text!r}"
        # Step 2: reparse —— pending 子字段应全部到位 (H-3 修复前会丢 goal/ws/files)
        doc = _todo_mod.parse(p)
        assert doc is not None
        assert len(doc.pending) == 1
        p2 = doc.pending[0]
        assert p2.step_id == "step2"
        assert p2.goal == "g2", f"goal lost after en roundtrip: {p2.goal!r}"
        assert p2.product_workspace == "/y"
        assert p2.required_files == ["b.py"]


# ── H-1 (reaper in-progress 别名) ──────────────────────────────
def test_reaper_in_progress_set_accepts_in_progress():
    """H-1 修复语义 (in-progress 别名被识别) 经 issue #124 统一入口保持。

    issue #124 前: reaper 两处本地 in_progress_set 字面量含
    STATUS_INPROGRESS_EN。issue #124 后: 本地集合已删, 别名识别收敛到
    todo.normalize_status —— 本测试改钉新机制, H-1 语义不变:
    'in-progress' 写法仍被 reaper 当"在干", 不误入收尸分支。
    """
    from mw.todo import (STATUS_INPROGRESS_EN, STATUS_INPROGRESS,
                        STATUS_WORKING, normalize_status)
    assert STATUS_INPROGRESS_EN == "in-progress"
    # 新机制: 归一化入口把 in-progress 收敛为规范值 working
    assert normalize_status("in-progress") == STATUS_WORKING
    assert normalize_status(STATUS_INPROGRESS) == STATUS_WORKING
    # reaper 不再构造本地字面量集合, 一律经 normalize_status
    import mw.reaper as _r
    src = (Path(__file__).resolve().parent.parent / "mw" / "reaper.py").read_text(
        encoding="utf-8"
    )
    assert "in_progress_set = {" not in src, \
        "reaper 不应再有本地 in_progress_set 构造 (issue #124)"
    assert "STATUS_INPROGRESS_EN" not in src, \
        "reaper 不应再直接引 STATUS_INPROGRESS_EN (别名走别名表)"
    assert src.count("normalize_status") >= 6, \
        f"reaper 收敛调用点缺失: normalize_status count={src.count('normalize_status')}"



if __name__ == "__main__":
    import tempfile as _tf
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    fail = 0
    for name, fn in tests:
        with _tf.TemporaryDirectory() as td:
            try:
                fn(Path(td))
                print(f"  PASS  {name}")
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}")
                fail += 1
            except Exception as e:
                print(f"  ERROR {name}: {e!r}")
                fail += 1
    if fail:
        print(f"\n{fail} failures")
        sys.exit(1)
    print(f"\nall {len(tests)} tests passed")

# ── § I18N-R3 M-1: parse_status 大小写不敏感 (§ M-1 polish) ────────────
def test_parse_status_case_insensitive():
    """parse_status 接受 STATUS / sTaTuS / Status 等大小写变体(对齐
    _canonical_field_key 的 case-insensitive 行为,见 I18N-R3 M-1)。"""
    from pathlib import Path
    from mw.todo import parse_status, CURRENT_HEADER
    from mw.todo import BILINGUAL_HEADER_ALIASES
    pair = BILINGUAL_HEADER_ALIASES[CURRENT_HEADER]
    en = pair[1]
    for marker in ("status:", "Status:", "STATUS:", "sTaTuS:", "StAtUs:"):
        p = Path("/tmp/_parse_status_case.py")
        p.write_text(f"# Team Work: probe\n\n## {en}\n- {marker} working\n", encoding="utf-8")
        assert parse_status(p) == "working", f"marker={marker!r} failed"


# ── § I18N-R3 M-4: 字段别名集补全 workspace / required files 变体 ───
def test_field_aliases_workspace_and_required_files_variants():
    """`product workspace` / `workspace` / `required files`(带空格) 与
    现有别名同义(§ M-4 polish,承诺对齐 REQ §4.2 B1 列举)。"""
    from pathlib import Path
    from mw.todo import parse
    import mw.todo as T
    en = T.BILINGUAL_HEADER_ALIASES[T.CURRENT_HEADER][1]

    # workspace variants
    for marker in ("product workspace:", "workspace:", "product_workspace:"):
        p = Path("/tmp/_ws.py")
        p.write_text(f"# T\n\n## {en}\n- {marker} /share_workspace/projects/p\n- kanban_task_id: t_x\n",
                     encoding="utf-8")
        c = parse(p).current
        assert c.product_workspace == "/share_workspace/projects/p", f"marker={marker!r} failed"

    # required files variants
    for marker in ("required files:", "required_files:", "产出文件:"):
        p = Path("/tmp/_rf.py")
        p.write_text(f"# T\n\n## {en}\n- {marker} a.md, b.md\n- kanban_task_id: t_x\n",
                     encoding="utf-8")
        c = parse(p).current
        assert c.required_files == ["a.md", "b.md"], f"marker={marker!r} failed"


# ── § I18N-R3 M-3: rotate 归档字面按 lang 分支(默认 zh-cn 兼容) ────
def test_rotate_no_result_marker_is_bilingual(monkeypatch):
    """rotate_to 在归档无结果卡时, 插入的字面应跟随 _render_lang()。
    zh-cn 部署写 `(无结果)`, en 部署写 `(no result)`。"""
    from pathlib import Path
    from mw import todo
    monkeypatch.setattr(todo, "_render_lang", lambda: "en")
    p = Path("/tmp/_rot.py")
    p.write_text(
        "# T\n\n"
        f"## {todo.BILINGUAL_HEADER_ALIASES[todo.CURRENT_HEADER][0]}\n"
        "- step: s1\n- title: no-result\n"
        "- kanban_task_id: t_x\n",
        encoding="utf-8")
    todo.rotate_to(p, "t_x")
    txt = p.read_text(encoding="utf-8")
    assert "→ (no result)" in txt
    assert "→ (无结果)" not in txt
