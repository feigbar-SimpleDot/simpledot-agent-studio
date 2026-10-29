"""issue #152 守护: mw/todo.py 状态解析对「行号前缀」的容错 (回归锁)。

实案 (2026-10-27 G 批 s3): worker 用带行号的读法 (read_file / nl -ba) 抓
TODO.md 全文后原样写回 ⇒ 每行变成 `12|- 状态: completed` ⇒ parse_status
返回 None ⇒ reaper 走「状态未识别 → 保守等待」⇒ 永不收割, 全程零报错,
该卡实际停摆 3h19m。

本文件钉死三件事 (口径 = docs/PLAN-I-20261029.md §2.1, K 批 #139 更新):
  1. 带前缀文件能解析出**正确的状态值** (逐字相等, 不是「不抛异常」);
  2. 四入口 (parse_status / parse / current_binding /
     verify_current_task_binding) 行为一致 —— 与同内容干净文件逐字相同;
  3. 容错范围 = `^\\s*\\d+[|\\t]` 行号前缀 + 『当前任务』段内形态容错
     (issue #139: 缩进 / `*` bullet / 缺 `- ` 前缀); 段外形态 (12: / 12))
     仍读不到 ⇒ 证明没有把段外规则顺手放宽。

★ 变异反证 MV-152-A: 去掉 _strip_line_num_prefix 的调用 (或把
  _LINE_NUM_PREFIX 改成永不匹配的 pattern) ⇒ 本文件所有带前缀用例转红。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mw.todo import (  # noqa: E402
    parse, parse_status, current_binding, verify_current_task_binding,
    _strip_line_num_prefix,
)

# 一份结构完整的 TODO.md: 当前任务 + 待执行 2 项 + 已完成 1 项 + 分隔线
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
  产物目录: /share_workspace/projects/demo
  产出文件: docs/PLAN.md
  卡: t_def456
- [s3] 编码 → coder
  目标: 按方案实施
  卡: t_ghi789

## 已完成
- [s0] 立项 → 已建单

## 已放弃
- (无)
"""


def _add_line_numbers(text: str) -> str:
    """复刻 read_file / nl -ba 的产出: 每行前缀 `N|` (issue #152 实案形态)。"""
    return "\n".join(
        f"{i}|{ln}" for i, ln in enumerate(text.splitlines(), 1)) + "\n"


def _write(tmp_path: Path, text: str, name: str = "TODO.md") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _snapshot(path: Path) -> dict:
    """四入口的返回值快照 —— 用于「带前缀 == 干净」的逐字比对。"""
    doc = parse(path)
    cur = None
    if doc and doc.current:
        cur = {
            "step_id": doc.current.step_id,
            "title": doc.current.title,
            "goal": doc.current.goal,
            "assignee": doc.current.assignee,
            "status": doc.current.status,
            "result": doc.current.result,
            "product_workspace": doc.current.product_workspace,
            "required_files": list(doc.current.required_files),
            "kanban_task_id": doc.current.kanban_task_id,
        }
    return {
        "title": doc.title if doc else None,
        "parse_status": parse_status(path),
        "current": cur,
        "pending": [
            {"step_id": t.step_id, "title": t.title, "assignee": t.assignee,
             "goal": t.goal, "product_workspace": t.product_workspace,
             "required_files": list(t.required_files),
             "kanban_task_id": t.kanban_task_id}
            for t in (doc.pending if doc else [])
        ],
        "done": list(doc.done) if doc else [],
        "dropped": list(doc.dropped) if doc else [],
        "binding": current_binding(path),
        "verify": verify_current_task_binding(path, "t_abc123", "s1"),
    }


# ---------------------------------------------------------------- 断言 1
def test_prefixed_parse_status_returns_exact_value(tmp_path):
    """红线主体: 带行号前缀 ⇒ parse_status 返回字符串 'completed' (逐字)。

    不是「不抛异常」, 也不是「非 None」—— 停摆的根因是返回 None 后 reaper
    保守等待, 所以必须钉住**值本身**。
    """
    p = _write(tmp_path, _add_line_numbers(CLEAN_DOC))
    assert parse_status(p) == "completed"


@pytest.mark.parametrize("status", ["working", "blocked", "failed",
                                    "waiting_sub", "进行中", "in-progress"])
def test_prefixed_parse_status_each_status(tmp_path, status):
    """每种状态值 (含中文别名) 带前缀都能原样读出, 不只是 completed。"""
    text = CLEAN_DOC.replace("- 状态: completed", f"- 状态: {status}")
    assert parse_status(_write(tmp_path, _add_line_numbers(text))) == status


# ---------------------------------------------------------------- 断言 2
def test_four_entry_points_agree_with_clean_file(tmp_path):
    """四入口一致性: 带前缀文件的返回值与同内容干净文件**逐字相等**。

    覆盖 issue §建议修法 1 的反面要求 —— 不许出现「有的入口能读有的不能读」
    的半吊子状态。verify_current_task_binding 经 current_binding 继承容错。
    """
    clean = _write(tmp_path, CLEAN_DOC, "TODO-clean.md")
    prefixed = _write(tmp_path, _add_line_numbers(CLEAN_DOC), "TODO-pref.md")
    assert _snapshot(prefixed) == _snapshot(clean)


def test_prefixed_current_binding_fields(tmp_path):
    """current_binding 的四个键带前缀全部可读 (派发前机器校验依赖它)。"""
    p = _write(tmp_path, _add_line_numbers(CLEAN_DOC))
    assert current_binding(p) == {
        "step_id": "s1",
        "kanban_task": "t_abc123",
        "status": "completed",
        "title": "需求分析",
    }


def test_prefixed_verify_binding_passes(tmp_path):
    """verify_current_task_binding 带前缀放行 (True, {}), 不再误报 todo_missing。"""
    p = _write(tmp_path, _add_line_numbers(CLEAN_DOC))
    assert verify_current_task_binding(p, "t_abc123", "s1") == (True, {})


# ---------------------------------------------------------------- 断言 3
def test_prefixed_section_split_and_pending_count_unchanged(tmp_path):
    """段划分与 pending/done 计数语义未放宽: 带前缀与干净文件读出同一结构。"""
    clean = parse(_write(tmp_path, CLEAN_DOC, "TODO-clean.md"))
    pref = parse(_write(tmp_path, _add_line_numbers(CLEAN_DOC), "TODO-pref.md"))
    assert pref is not None and clean is not None
    assert [(t.step_id, t.title, t.assignee) for t in pref.pending] == \
           [(t.step_id, t.title, t.assignee) for t in clean.pending]
    assert [t.step_id for t in pref.pending] == ["s2", "s3"]
    assert pref.pending[0].product_workspace == "/share_workspace/projects/demo"
    assert pref.pending[0].required_files == ["docs/PLAN.md"]
    assert pref.pending[1].kanban_task_id == "t_ghi789"
    assert pref.done == clean.done == ["- [s0] 立项 → 已建单"]
    assert pref.dropped == clean.dropped == []
    assert pref.title == clean.title == "批处理示例"


# ---------------------------------------------------------------- 断言 4
def test_clean_file_behaviour_unchanged(tmp_path):
    """不误伤: 干净文件 (无前缀) 的四入口返回值与容错前语义一致。

    容错是「加法」不是「改写」: 无竖线的文件走快路径, 零改动。
    """
    p = _write(tmp_path, CLEAN_DOC)
    snap = _snapshot(p)
    assert snap["parse_status"] == "completed"
    assert snap["current"]["kanban_task_id"] == "t_abc123"
    assert snap["current"]["step_id"] == "s1"
    assert snap["binding"]["status"] == "completed"
    assert snap["verify"] == (True, {})
    assert len(snap["pending"]) == 2
    # 快路径: 无竖线字符时逐字返回原串
    assert _strip_line_num_prefix(CLEAN_DOC) == CLEAN_DOC


# ---------------------------------------------------------------- 断言 5
def test_tolerance_does_not_loosen_other_rules(tmp_path):
    """容错不外溢: 无状态行 / 无当前任务段 / 文件不存在 ⇒ 仍返回 None。"""
    # 无『状态』字段
    no_status = CLEAN_DOC.replace("- 状态: completed\n", "")
    assert parse_status(_write(tmp_path, _add_line_numbers(no_status))) is None
    # 无『当前任务』段
    no_current = "# Team Work: T\n\n## 待执行\n- [s1] x → coder\n"
    assert parse_status(_write(tmp_path, _add_line_numbers(no_current))) is None
    assert current_binding(_write(tmp_path, _add_line_numbers(no_current),
                                  "B.md")) is None
    # 文件不存在
    assert parse_status(tmp_path / "nope.md") is None
    assert current_binding(tmp_path / "nope.md") is None
    # 空值状态行仍归 None (与容错前一致)
    empty = CLEAN_DOC.replace("- 状态: completed", "- 状态:")
    assert parse_status(_write(tmp_path, _add_line_numbers(empty), "E.md")) is None


# ---------------------------------------------------------------- 断言 6
@pytest.mark.parametrize("shape, expect", [
    ("12|- 状态: completed", "completed"),        # 行号前缀容错 (竖线)
    ("  12|- 状态: completed", "completed"),      # 行首允许空白
    ("12: - 状态: completed", None),              # 冒号形态不容错
    ("12) - 状态: completed", None),              # 括号形态不容错
    # K 批 #139 计划内翻转: I 批设计内边界 (tab / 缺前缀读不到) 被有意推翻
    ("     12\t- 状态: completed", "completed"),  # cat -n 形态纳入容错
    ("状态: completed", "completed"),             # 缺 `- ` 前缀 (段内) 纳入容错
    ("- 状态: completed", "completed"),           # 干净形态
])
def test_prefix_boundary_is_exactly_digits_pipe(tmp_path, shape, expect):
    """钉死容错边界: 行号前缀 `^\\s*\\d+[|\\t]` + 段内形态容错 (#139 更新)。

    没有这条断言, 把剥离范围误改成更宽的 `^\\s*\\d+[|:\\t]` 也不会转红
    (PLAN-I §2.1 MV-152-A 注意事项); `12:` / `12)` 两行仍钉住「冒号/括号
    形态不剥」的下界。
    """
    text = "\n".join(
        (shape if ln.startswith("- 状态:") else ln)
        for ln in CLEAN_DOC.splitlines())
    assert parse_status(_write(tmp_path, text)) == expect


# ------------------------------------------------------ 断言 6b (#153 补牙)
def test_widened_pattern_caught_when_pipe_elsewhere(tmp_path):
    """快路径掩蔽补牙 (#153): 含竖线的文档里 `12:` 形态前缀也不得被剥。

    断言 6 的全部参数形态均无竖线 ⇒ 走 `_strip_line_num_prefix` 快路径
    短路, 「只加宽 pattern、保留快路径」的变异在该文件恒绿 (掩蔽)。本
    用例把竖线注入**非状态行** ⇒ 快路径不短路、pattern 真正被应用:
    现状 pattern (`^[ \\t]*\\d+\\|`) 不剥 `12:` ⇒ 状态读不到 (None);
    若 pattern 被改宽 (如 `^\\s*\\d+[|:\\t]`) 则 `12:` 被剥 ⇒ 状态读成
    completed ⇒ 本用例转红 —— 正是 MV-153 要拦的变异。

    形态细节: 用 `12:- 状态:` (冒号后无空格) 而非 `12: - 状态:` ——
    后者即使被变异剥掉 `12:` 也剩 ` - 状态:` (带前导空格), `parse_status`
    的 `^-\\s*` 依旧不匹配 ⇒ 行为差异不可观测、变异照样漏网; 前者剥离后
    恰为 `- 状态:` 可观测 ⇒ 本用例对变异真有拦截力。

    判据口径: 「对 `cat -n` 制表符形态免疫」是设计内的行为等价 (见
    _strip_line_num_prefix docstring), 本用例不把 `12\\t` 纳入容错范围,
    只钉「含竖线输入」这条 pattern 真正生效的路径。
    """
    text = (CLEAN_DOC
            .replace("- 结果: verdict=COMPLETE; 产物在 docs/REQ.md",
                     "- 结果: verdict=COMPLETE | 产物在 docs/REQ.md")
            .replace("- 状态: completed", "12:- 状态: completed"))
    assert "|" in text                        # 前提: 快路径不短路
    assert parse_status(_write(tmp_path, text)) is None


# ---------------------------------------------------------------- 断言 7
def test_prefixed_english_form_still_readable(tmp_path):
    """en 部署形态 (`## Current Task` + `- status:`) 带前缀同样可读。

    双键识别语义未放宽: 容错只剥前缀, 键名折算仍走既有规则。
    """
    en = (CLEAN_DOC
          .replace("## 当前任务", "## Current Task")
          .replace("## 待执行", "## Pending")
          .replace("## 已完成", "## Done")
          .replace("## 已放弃", "## Dropped")
          .replace("- 状态: completed", "- status: completed")
          .replace("- 步骤: s1", "- step: s1")
          .replace("- 卡: t_abc123", "- kanban_task: t_abc123"))
    p = _write(tmp_path, _add_line_numbers(en))
    assert parse_status(p) == "completed"
    assert current_binding(p) == {
        "step_id": "s1", "kanban_task": "t_abc123",
        "status": "completed", "title": "需求分析",
    }
    doc = parse(p)
    assert doc is not None
    assert len(doc.pending) == 2


# ---------------------------------------------------------- 剥离函数单元
def test_strip_helper_is_idempotent_and_narrow():
    """_strip_line_num_prefix: 幂等 + 只剥一种形态 + 无竖线零改动。"""
    once = _strip_line_num_prefix(_add_line_numbers(CLEAN_DOC))
    assert _strip_line_num_prefix(once) == once          # 幂等
    assert once == "\n".join(CLEAN_DOC.splitlines())     # 回到无前缀形态
    assert _strip_line_num_prefix("a|b\n12|c") == "a|b\nc"
    assert _strip_line_num_prefix("无竖线原文") == "无竖线原文"
