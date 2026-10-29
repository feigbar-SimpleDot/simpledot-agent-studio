"""issue #145 守护: MW_REAPER_SUBAGENT_MIN 默认值的真相源收敛 + 文档引用完整。

口径 (PLAN-H §5.2):
  * 语义真相源 = mw/reaper.py::_subagent_wait_min (函数体兜底值)。
  * 文档侧 (DEPLOY.md / design.md / docs/main-soul.{zh-cn,en}.md) 一律
    **引用式**表述 —— 不复述数字 ⇒ 改默认值不需要同步改文档。
  * 本文件守护「引用存在 + 中英语义等价」, 防后人把数字写回文档。

数字复述的判定范围 (PLAN-H §5.5 静态口径): 只看提到 subagent 的行
(grep '60' file | grep -i subagent 的测试化), 不扫全文 —— design.md
L192 的 `ge=60` 是 Pydantic 字段约束, 与本议题无关, 全文扫会假红。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mw.reaper import _subagent_wait_min  # noqa: E402

# 引用式表述必须指向的语义真相源 (逐字)
REF = "mw/reaper.py::_subagent_wait_min"
# 数字复述模式: 命中即视为「把默认值写回了文档」
NUMBER_PATTERNS = (r"60\s*min", r"=\s*60\b", r"default\s+60\b")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _code_default_min() -> int:
    """从 reaper.py 源码读出兜底默认值 (不硬编码, 免得测试与代码脱钩)。"""
    src = _read("mw/reaper.py")
    m = re.search(r"def _subagent_wait_min\(\).*?return\s+(\d+)\s*$", src, re.S | re.M)
    assert m, "无法从 mw/reaper.py 解析 _subagent_wait_min 的兜底默认值"
    return int(m.group(1))


def _assert_reference_style(text: str, where: str) -> None:
    """核心断言: 引用存在 + 提到 subagent 的行不复述默认值数字。"""
    assert REF in text, f"{where} 未引用语义真相源 {REF}"
    for line in text.splitlines():
        # 只审与 subagent 相关的行 (PLAN-H §5.5 静态口径的测试化)
        if "subagent" not in line.lower():
            continue
        for pat in NUMBER_PATTERNS:
            hits = re.findall(pat, line, flags=re.IGNORECASE)
            assert not hits, (
                f"{where} 仍在复述默认值数字 (模式 {pat!r}): {line.strip()!r}")


def test_code_is_the_single_semantic_source():
    """语义真相源 = 代码; env 缺省时返回值与源码兜底字面量一致。"""
    old = os.environ.pop("MW_REAPER_SUBAGENT_MIN", None)
    try:
        assert _subagent_wait_min() == _code_default_min()
    finally:
        if old is not None:
            os.environ["MW_REAPER_SUBAGENT_MIN"] = old


@pytest.mark.parametrize("rel", [
    "DEPLOY.md",
    "design.md",
    "docs/main-soul.zh-cn.md",
    "docs/main-soul.en.md",
])
def test_docs_reference_the_code_not_the_number(rel):
    """文档必须**引用** reaper.py, 且 subagent 相关行不得再复述默认值数字。"""
    _assert_reference_style(_read(rel), rel)


def test_main_soul_bilingual_equivalence():
    """main-soul 中英必须语义等价: 都引用同一真相源, 都不含数字复述。"""
    for rel in ("docs/main-soul.zh-cn.md", "docs/main-soul.en.md"):
        _assert_reference_style(_read(rel), rel)


def test_mutation_restoring_a_number_in_docs_breaks_the_guard():
    """变异反证: 把引用式改回数字 ⇒ 守护转红 (证明断言有牙齿)。"""
    original = _read("DEPLOY.md")
    mutated = original.replace(REF, f"{REF} (default 60 min)")
    assert mutated != original, "变异体未生效 (原文形态与预期不符)"
    with pytest.raises(AssertionError):
        _assert_reference_style(mutated, "DEPLOY.md (mutated)")
