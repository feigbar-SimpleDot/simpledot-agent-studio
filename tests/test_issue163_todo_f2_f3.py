"""issue #163 守护: K 批审查五条 follow-up F-2..F-6 (mw/todo.py 解析层)。

依据: docs/PLAN-L-20261029.md §3 (裁决与断言组) + ISSUE-163 原文。
  T-163-A (F-2) 段内非协议键冒号行不再凭空造 current task
  T-163-B (F-3) 缩进段头 ⇒ 四入口全齐 (含缩进 × 四残余形态 16 组)
  T-163-C (F-3 根因) 静态断言: _split_sections 不含内联段头字面量比较
  T-163-D (F-4) 已知边界钉桩: 数字+制表符前缀在内容行上被剥 (维持现状)
  T-163-E (F-5) 同段多条状态行优先级: 规范 bullet 行 > no-dash 行, 同类取首行
  T-163-G 等价性对拍: 族 2 (正常文档) / 族 3 (退化文档) 与 K 批基线
        aeea49e 的 mw/todo.py 四入口输出逐字相等 (★ 刻意变更只允许出现在族 1)
  T-163-H 耗时上界复跑 (K 批 T-H 同口径, 上界 10 ms 不得放宽)

★ 等价性对照基线 (PLAN-L §3.1 裁决): 基线 = git 提交 aeea49e 的 mw/todo.py
  (md5 5f17c507d6da14d1804cf4c7c9572414)。测试内用 `git show` 动态取源并
  importlib 加载 —— **不把基线副本复制进仓库** (那是 F-3 要消灭的"规则
  副本各自演化"的同款缺陷)。仓库无 git 环境时相关用例 skip (不误红)。

★ 变异反证 MV-163-A..J 见 PLAN-L §7.3; 本文件每条转红型 MV 都有对应用例。
跑法: pytest tests/test_issue163_todo_f2_f3.py
"""
from __future__ import annotations

import ast
import hashlib
import importlib.machinery
import importlib.util
import inspect
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mw.todo import (  # noqa: E402
    parse, parse_status, current_binding, verify_current_task_binding,
    diagnose, _strip_line_num_prefix, _find_status_line, _split_sections,
)

# K 批 F-1 修复后的合并提交 (等价性对照基线, PLAN-L §3.1)
BASELINE_COMMIT = "aeea49e"
BASELINE_MD5 = "5f17c507d6da14d1804cf4c7c9572414"

# 耗时上界 (ms) —— K 批钉死, 不得放宽
TIME_BUDGET_MS = 10.0

# 一份结构完整的合法 TODO.md (与 139/152 守护文件同风格)
CLEAN_DOC = """# Team Work: 批处理示例

## 当前任务
- 步骤: s1
- 标题: 需求分析
- 目标: 分析需求并出报告
- 承接方: product-manager
- 产物目录: /share_workspace/projects/demo
- 产出文件: docs/REQ.md
- 状态: completed
- 结果: verdict=COMPLETE; 产物在 docs/REQ.md
- 卡: t_abc123

---

## 待执行
- [s2] 技术方案 → system-engineer
  目标: 出文件:行级方案
  卡: t_def456

## 已完成
- [s0] 立项 → 已建单

## 已放弃
- (无)
"""


def _write(tmp_path: Path, text: str, name: str = "TODO.md") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _elapsed_ms(fn, *args) -> float:
    """实测单次调用耗时 (毫秒), 标准库 perf_counter (与 139 守护同口径)。"""
    t0 = time.perf_counter()
    fn(*args)
    return (time.perf_counter() - t0) * 1000.0


# ── 四残余形态生成器 (与 139 守护文件同口径) ─────────────────────────

def form_tab(text: str) -> str:
    return "\n".join(f"{i}\t{ln}" for i, ln in enumerate(text.splitlines(), 1))


def form_star(text: str) -> str:
    return "\n".join(("* " + ln[2:]) if ln.startswith("- ") else ln
                     for ln in text.splitlines())


def form_nodash(text: str) -> str:
    return "\n".join(ln[2:] if ln.startswith("- ") else ln
                     for ln in text.splitlines())


def form_indent(text: str) -> str:
    return "\n".join(("  " + ln) if ln.startswith("- ") else ln
                     for ln in text.splitlines())


DAMAGED_FORMS = {
    "tab": form_tab,
    "star": form_star,
    "no-dash": form_nodash,
    "indented": form_indent,
}


def _indent_header(text: str, spaces: str = "  ") -> str:
    """『## 当前任务』段头前加缩进 (F-3 的触发形态)。"""
    return text.replace("## 当前任务", spaces + "## 当前任务", 1)


# ── T-163-A (F-2): 非协议键冒号行不再凭空造 current task ─────────────

NON_PROTOCOL_LINES = [
    "12: 普通文本",
    "备注: 一些说明",
    "https://example.com/x",
    "时间: 12:30",
]


@pytest.mark.parametrize("junk", NON_PROTOCOL_LINES)
def test_f2_non_protocol_colon_lines_do_not_create_current(tmp_path, junk):
    """F-2: 段内只有非协议键冒号行 ⇒ 四入口一致地"没有当前任务"。"""
    text = CLEAN_DOC.replace("- 状态: completed", junk)
    text = text.replace("- 结果: verdict=COMPLETE; 产物在 docs/REQ.md", "")
    # 清掉其余协议键, 只留 junk 一条冒号行
    for proto in ("- 步骤: s1", "- 标题: 需求分析", "- 目标: 分析需求并出报告",
                  "- 承接方: product-manager", "- 产物目录: /share_workspace/projects/demo",
                  "- 产出文件: docs/REQ.md", "- 卡: t_abc123"):
        text = text.replace(proto + "\n", "")
    p = _write(tmp_path, text, "F2.md")
    # 入口 1/2: parse 不再凭空造 current
    doc = parse(p)
    assert doc is not None and doc.current is None, f"{junk!r}: parse 凭空造了 current"
    # 入口 3: current_binding 回到 None (修复前是全空串 dict)
    assert current_binding(p) is None, f"{junk!r}: current_binding 非 None"
    # 入口 4: verify reason 回到 todo_missing (修复前是 no_binding)
    ok_v, detail = verify_current_task_binding(p, "t_abc123", "s1")
    assert ok_v is False and detail["reason"] == "todo_missing", \
        f"{junk!r}: verify reason = {detail.get('reason')}"
    # parse_status 同步: 无状态行 ⇒ None
    assert parse_status(p) is None


def test_f2_protocol_keys_not_collateral(tmp_path):
    """F-2 不误伤: 段内含协议键 + 若干非协议键 ⇒ current 正常构造。"""
    text = CLEAN_DOC.replace("- 卡: t_abc123",
                             "- 卡: t_abc123\n备注: 一些说明\n时间: 12:30")
    p = _write(tmp_path, text, "F2B.md")
    doc = parse(p)
    assert doc is not None and doc.current is not None
    assert doc.current.status == "completed"
    assert doc.current.step_id == "s1"
    cb = current_binding(p)
    assert cb is not None and cb["status"] == "completed"
    ok_v, detail = verify_current_task_binding(p, "t_abc123", "s1")
    assert ok_v is True and detail == {}


# ── T-163-B (F-3): 缩进段头 ⇒ 四入口全齐 ─────────────────────────────

def test_f3_indented_header_four_entries_agree(tmp_path):
    """F-3: `  ## 当前任务` (段头前两空格) ⇒ 四入口一致读出 completed。"""
    p = _write(tmp_path, _indent_header(CLEAN_DOC), "F3.md")
    assert parse_status(p) == "completed"
    doc = parse(p)
    assert doc is not None and doc.current is not None, "parse 未识别缩进段头"
    assert doc.current.status == "completed"
    cb = current_binding(p)
    assert cb is not None and cb["status"] == "completed"
    ok_v, detail = verify_current_task_binding(p, "t_abc123", "s1")
    assert ok_v is True and detail == {}


@pytest.mark.parametrize("form", sorted(DAMAGED_FORMS))
def test_f3_indented_header_x_four_forms_four_entries(tmp_path, form):
    """F-3: 缩进段头 × 四残余形态 (tab/star/no-dash/indented) ⇒ 四入口全齐。"""
    text = DAMAGED_FORMS[form](_indent_header(CLEAN_DOC))
    p = _write(tmp_path, text, f"{form}_hdr.md")
    assert parse_status(p) == "completed", f"{form}: parse_status"
    doc = parse(p)
    assert (doc and doc.current and doc.current.status == "completed"), f"{form}: parse"
    cb = current_binding(p)
    assert cb and cb["status"] == "completed", f"{form}: current_binding"
    ok_v, detail = verify_current_task_binding(p, "t_abc123", "s1")
    assert ok_v is True, f"{form}: verify ({detail})"


def test_f3_indented_english_header(tmp_path):
    """F-3: 缩进的英文段头 `  ## Current Task` ⇒ 四入口全齐。"""
    p = _write(tmp_path, _indent_header(
        CLEAN_DOC.replace("## 当前任务", "## Current Task")), "F3EN.md")
    assert parse_status(p) == "completed"
    doc = parse(p)
    assert doc and doc.current and doc.current.status == "completed"
    cb = current_binding(p)
    assert cb and cb["status"] == "completed"
    ok_v, _ = verify_current_task_binding(p, "t_abc123", "s1")
    assert ok_v is True


# ── T-163-C (F-3 根因): 静态断言, 防"第三份副本" ─────────────────────

def test_f3_split_sections_has_no_inline_header_literals():
    """F-3 根因闭环: _split_sections 体内不得内联『当前任务』字面量比较,
    必须调用 _is_current_header (MV-163-D 的转红用例)。"""
    src = inspect.getsource(_split_sections)
    tree = ast.parse(src)
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_is_current_header" in called, \
        "_split_sections 未走 _is_current_header (段头判定副本复活)"
    # 体内不得出现 '当前任务' / 'Current Task' 字面量 (副本的形态学特征)
    literals = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "当前任务" not in literals and "Current Task" not in literals, \
        f"_split_sections 含内联段头字面量: {sorted(literals)}"


# ── T-163-D (F-4): 已知边界钉桩 (维持现状不收紧) ─────────────────────

def test_f4_known_boundary_pinned(tmp_path):
    """F-4 已知边界钉桩: 数字+制表符前缀在**内容行**上被剥 (维持现状)。

    ★ 钉的是"现状语义", 不是"应该修好的样子" —— 将来任何收紧改动都会让
    本用例转红, 强制走裁决流程 (PLAN-L §3.4 裁决: 维持现状 + 登记边界)。
    """
    # 现状: 内容行的 `数字+TAB` 前缀被剥
    assert _strip_line_num_prefix("2026\t完成") == "完成"
    # 状态读数不受影响
    text = CLEAN_DOC.replace("- 结果: verdict=COMPLETE; 产物在 docs/REQ.md",
                             "- 结果: 2026\t完成了 X")
    p = _write(tmp_path, text, "F4.md")
    assert parse_status(p) == "completed"


# ── T-163-E (F-5): 同段多条状态行的优先级口径 ────────────────────────

def test_f5_canonical_beats_stale_nodash(tmp_path):
    """F-5: 陈旧 no-dash 行在前 + 规范行在后 ⇒ 四入口一致读规范行。"""
    text = CLEAN_DOC.replace("- 状态: completed",
                             "状态: stale\n- 状态: completed")
    p = _write(tmp_path, text, "F5A.md")
    assert parse_status(p) == "completed", "规范行未压过陈旧 no-dash 行"
    doc = parse(p)
    assert doc and doc.current and doc.current.status == "completed"
    cb = current_binding(p)
    assert cb and cb["status"] == "completed"
    d = diagnose(p)
    assert d == {"status": "completed", "reason": "ok"}, f"diagnose 失真: {d}"


def test_f5_canonical_beats_later_nodash(tmp_path):
    """F-5: 规范行在前 + 陈旧 no-dash 行在后 ⇒ 四入口一致读规范行。

    ★ 这条是 MV-163-F 的牙齿所在: 若 current_binding/parse 仍走 dict
    后写覆盖 (未接 _find_status_line), 后写的 no-dash 陈旧行会胜出 ⇒
    与 parse_status/diagnose 相反 ⇒ 转红。
    """
    text = CLEAN_DOC.replace("- 状态: completed",
                             "- 状态: completed\n状态: stale")
    p = _write(tmp_path, text, "F5D.md")
    assert parse_status(p) == "completed"
    doc = parse(p)
    assert doc and doc.current and doc.current.status == "completed"
    cb = current_binding(p)
    assert cb and cb["status"] == "completed", \
        "current_binding 未走优先级口径 (dict 后写覆盖让陈旧行胜出)"
    assert diagnose(p) == {"status": "completed", "reason": "ok"}


def test_f5_two_canonical_lines_first_wins(tmp_path):
    """F-5: 段内两条规范状态行 ⇒ 取首行 (与写面「替换第一处」同源)。"""
    text = CLEAN_DOC.replace("- 状态: completed",
                             "- 状态: working\n- 状态: completed")
    p = _write(tmp_path, text, "F5B.md")
    assert parse_status(p) == "working"
    doc = parse(p)
    assert doc and doc.current and doc.current.status == "working"
    cb = current_binding(p)
    assert cb and cb["status"] == "working"


def test_f5_nodash_only_still_readable(tmp_path):
    """F-5: 段内只有 no-dash 行 ⇒ 仍能读到 (容错不回归)。"""
    text = CLEAN_DOC.replace("- 状态: completed", "状态: completed")
    p = _write(tmp_path, text, "F5C.md")
    assert parse_status(p) == "completed"
    doc = parse(p)
    assert doc and doc.current and doc.current.status == "completed"
    cb = current_binding(p)
    assert cb and cb["status"] == "completed"
    assert diagnose(p)["reason"] == "ok"


def test_f5_find_status_line_unit():
    """_find_status_line 单元: 优先级 + 同类首行 + 非状态键忽略。"""
    assert _find_status_line(["- 状态: working", "状态: stale"]) == ("状态", "working")
    assert _find_status_line(["状态: stale", "- 状态: ok2"]) == ("状态", "ok2")
    assert _find_status_line(["状态: blocked"]) == ("状态", "blocked")
    assert _find_status_line(["* 状态: done"]) == ("状态", "done")
    assert _find_status_line(["  - 状态: x"]) == ("状态", "x")   # 缩进 bullet
    assert _find_status_line(["- 结果: r"]) is None
    assert _find_status_line([]) is None


# ── T-163-G: 等价性对拍 (族 2 / 族 3 与基线逐字相等) ─────────────────

def _load_baseline_module():
    """从 git 取基线 mw/todo.py 并动态加载 (副本不落盘、不入库)。"""
    try:
        src = subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{BASELINE_COMMIT}:mw/todo.py"],
            capture_output=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("基线提交不可取 (无 git 环境), 对拍用例跳过")
    assert hashlib.md5(src).hexdigest() == BASELINE_MD5, \
        "基线 aeea49e 的 mw/todo.py md5 漂移 ⇒ 先解释漂移再继续 (PLAN-L §3.1 S-1)"
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(
        "todo_baseline_163",
        importlib.machinery.SourceFileLoader(
            "todo_baseline_163", str(ROOT / "mw" / "todo.py"))))
    # dataclass 的字符串注解经 sys.modules[cls.__module__] 解析 ⇒ 先注册
    sys.modules["todo_baseline_163"] = mod
    code = compile(src, "<todo_baseline_163>", "exec")
    exec(code, mod.__dict__)
    return mod


def _snapshot(mod, path: Path) -> dict:
    """四入口返回值快照 (可 json 比较的形态)。"""
    doc = mod.parse(path)
    cur = None
    if doc and doc.current:
        cur = {"step_id": doc.current.step_id, "title": doc.current.title,
               "goal": doc.current.goal, "assignee": doc.current.assignee,
               "product_workspace": doc.current.product_workspace,
               "required_files": list(doc.current.required_files),
               "status": doc.current.status, "result": doc.current.result,
               "kanban_task_id": doc.current.kanban_task_id}
    ok_v, detail = mod.verify_current_task_binding(path, "t_abc123", "s1")
    return {
        "title": (doc.title if doc else None),
        "current": cur,
        "pending": [[t.step_id, t.title, t.assignee] for t in (doc.pending if doc else [])],
        "done": list(doc.done if doc else []),
        "parse_status": mod.parse_status(path),
        "current_binding": mod.current_binding(path),
        "verify": [ok_v, detail],
    }


# 族 2: 正常文档 (含协议键的规范文档) —— 必须逐字相等
FAMILY2 = {
    "clean": CLEAN_DOC,
    "en_doc": CLEAN_DOC.replace("## 当前任务", "## Current Task")
               .replace("- 状态: completed", "- status: completed"),
    "result_digit": CLEAN_DOC.replace(
        "- 结果: verdict=COMPLETE; 产物在 docs/REQ.md",
        "- 结果: 2026-10-29 完成了 X"),
    "star_form": form_star(CLEAN_DOC),
    "nodash_form": form_nodash(CLEAN_DOC),
    "indent_form": form_indent(CLEAN_DOC),
    "tab_form": form_tab(CLEAN_DOC),
}

# 族 3: 边界/退化文档 —— fail-closed 语义不得松动, 必须逐字相等
FAMILY3 = {
    "no_current_section": "# Team Work: x\n\n## 待执行\n- [s1] a → b\n",
    "empty_status_value": CLEAN_DOC.replace("- 状态: completed", "- 状态:"),
    "status_outside_section": CLEAN_DOC.replace(
        "## 已完成", "- 状态: completed\n\n## 已完成"),
    "empty_file": "",
    "header_only": "# Team Work: x\n",
}


@pytest.mark.parametrize("name", sorted(FAMILY2))
def test_equiv_family2_identical_to_baseline(tmp_path, name):
    """族 2 (正常文档) × 四入口: 与基线 aeea49e 逐字相等 (未越界证明)。"""
    base = _load_baseline_module()
    p = _write(tmp_path, FAMILY2[name], f"{name}.md")
    assert _snapshot(__import__("mw.todo", fromlist=["x"]), p) == _snapshot(base, p), \
        f"族 2 [{name}] 与基线输出不一致 ⇒ 改动越界 (PLAN-L §3.1)"


@pytest.mark.parametrize("name", sorted(FAMILY3))
def test_equiv_family3_identical_to_baseline(tmp_path, name):
    """族 3 (退化文档) × 四入口: 与基线逐字相等 (fail-closed 不松动)。"""
    base = _load_baseline_module()
    p = _write(tmp_path, FAMILY3[name], f"{name}.md")
    assert _snapshot(__import__("mw.todo", fromlist=["x"]), p) == _snapshot(base, p), \
        f"族 3 [{name}] 与基线输出不一致 ⇒ fail-closed 语义松动"


def test_equiv_family1_changes_are_only_the_adjudicated_ones(tmp_path):
    """族 1 (F-2/F-3/F-5 目标态): 允许变更, 且**只**允许在裁决点上变。

    对每个族 1 输入: 基线 vs 工作树快照若不同, 差异必须落在裁决登记的字段
    (current / current_binding / verify) 上, title/pending/done/parse_status
    不得变 (F-5 的 parse_status 变化单独用 F5A 钉, 不在此族)。
    """
    base = _load_baseline_module()
    family1 = {
        "f2_junk_only": "# T\n\n## 当前任务\n备注: 说明\n\n## 待执行\n",
        "f3_indented_header": _indent_header(CLEAN_DOC),
    }
    for name, text in family1.items():
        p = _write(tmp_path, text, f"eq1_{name}.md")
        new = _snapshot(__import__("mw.todo", fromlist=["x"]), p)
        old = _snapshot(base, p)
        for key in ("title", "pending", "done", "parse_status"):
            assert new[key] == old[key], f"族 1 [{name}] 非裁决字段 {key} 变了 ⇒ 越界"


# ── T-163-H: 耗时上界复跑 (K 批 T-H 同口径, 上界不得放宽) ────────────

def test_h1_deep_whitespace_no_colon_cheap(tmp_path):
    """T-H1 复跑: 深前导空白 + 无冒号行 ⇒ 读不出且 < 10 ms。"""
    deep = " " * 400
    p = _write(tmp_path, CLEAN_DOC.replace(
        "- 卡: t_abc123", f"- 卡: t_abc123\n{deep}"), "H1.md")
    assert parse_status(p) == "completed"
    elapsed = _elapsed_ms(parse_status, p)
    assert elapsed < TIME_BUDGET_MS, f"parse_status 耗时 {elapsed:.3f} ms 超上界"


def test_h2_long_whitespace_run_in_key_cheap():
    """T-H2 复跑: 键区长空白 run (有冒号) ⇒ < 10 ms (F-1 贪婪化牙齿)。"""
    from mw.todo import _match_field_line
    for n in (2000, 6400):
        ln = "x" + " " * n + "y: v"
        elapsed = _elapsed_ms(_match_field_line, ln)
        assert elapsed < TIME_BUDGET_MS, f"键区空白 {n} 耗时 {elapsed:.3f} ms 超上界"


def test_h3_find_status_line_on_deep_whitespace_doc(tmp_path):
    """MV-163-G 的守护面: 含深空白行的文档走 _find_status_line 路径仍在上界内
    (证明新 helper 没有绕过 F-1 短路)。

    ★ 深空白行必须排在状态行**之前** —— _find_status_line 命中规范状态行
    即早退, 深空白行若在其后就永远扫不到, 变异 (绕过短路) 将无牙齿。
    """
    deep = " " * 400
    text = CLEAN_DOC.replace("## 当前任务\n", f"## 当前任务\n{deep}\n")
    p = _write(tmp_path, text, "H3.md")
    assert parse_status(p) == "completed"   # 语义不受深空白行影响
    for label, fn in (("parse_status", lambda: parse_status(p)),
                      ("parse", lambda: parse(p)),
                      ("current_binding", lambda: current_binding(p)),
                      ("diagnose", lambda: diagnose(p))):
        elapsed = _elapsed_ms(fn)
        assert elapsed < TIME_BUDGET_MS, f"{label} 耗时 {elapsed:.3f} ms 超上界"
