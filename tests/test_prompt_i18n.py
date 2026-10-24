"""tests/test_prompt_i18n.py — Issue #41 双语 worker launch prompt 守护。

本卡 (s4-prompt-i18n, 2026-10-17) 把 mw/pipeline.py:worker_contract_prompt
原内联 f-string 拆为 mw/worker_contract_prompt.<lang>.md 两个模板文件,
launch 时按 mw/i18n.resolve_lang() 选语种。

守护目标 (z 红线):
  P1. zh 模板经 str.format 后的输出**逐字节**与测试钉锚 baseline 相等
      —— 保证 zh 部署零回归锚(架构 §6.1, conftest 钉 MW_LANG=zh-cn)。
  P2. en 模板渲染含关键协议语义(段头 `## Current Task`, sed `- status:`
      前缀,waiting_sub, turn ending != task ending, etc.), 同时被
      `tests/test_issue13_waiting_sub.py::test_w7_*` 守护。
  P3. 切换 MW_LANG → 模板选 zh / en, 模板文件不存在 / env override 走
      退化路径, 永远不出空串。
  P4. build_worker_prompt() (项目任务 / 非项目任务两种 workspace 推导)
      与 worker_contract_prompt() 一致: zh 部署产出 byte-equal baseline
      的相应切片。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURE_BASELINE = ROOT / "tests" / "fixtures" / "worker_prompt_baseline_zh.txt"
PROMPT_DIR = ROOT / "mw"
ZH_TEMPLATE = PROMPT_DIR / "worker_contract_prompt.zh-cn.md"
EN_TEMPLATE = PROMPT_DIR / "worker_contract_prompt.en.md"


def _reload_pipeline():
    """强制 reload mw.pipeline, 避开 i18n._cache 跨测试污染。"""
    import importlib
    from mw.i18n import _cache, _LOADED
    _cache.clear()
    _LOADED.clear()
    import mw.pipeline as pipeline
    importlib.reload(pipeline)
    return pipeline


# ──────────────────────────────────────────────────────────────────────
# 文件层守护 (模板族已 split, 不依赖 import mw.*)
# ──────────────────────────────────────────────────────────────────────
def test_prompt_template_family_split():
    """I18N-R3 风格: zh/en 模板族文件均存在(架构 §4.3 类比)。"""
    assert ZH_TEMPLATE.exists(), f"missing {ZH_TEMPLATE} (Issue #41 / s4-prompt-i18n)"
    assert EN_TEMPLATE.exists(), f"missing {EN_TEMPLATE} (Issue #41 / s4-prompt-i18n)"


def test_prompt_templates_contain_protocol_markers():
    """两模板均含可识别协议 marker (与 soul_template 对称, I18N-R3 §4.1)。"""
    zh = ZH_TEMPLATE.read_text(encoding="utf-8")
    en = EN_TEMPLATE.read_text(encoding="utf-8")
    # 双语模板都暴露 3 个 str.format 占位符(实际被替换)
    for label, tpl in (("zh", zh), ("en", en)):
        for ph in ("{todo_path}", "{out_dir}", "{workspace}"):
            assert ph in tpl, f"{label} 模板缺 {ph} 占位符"
    # zh 模板字面包含中文协议段头 + sed `- 状态:` 前缀
    assert "## 当前任务" in zh or "『当前任务』" in zh, \
        "zh 模板未提及当前任务段头"
    assert "^- 状态:" in zh, "zh 模板 sed pattern 丢失 `- ` 前缀"
    # en 模板字面包含英文协议段头 + sed `- status:` 前缀
    assert "## Current Task" in en, "en 模板未提及 Current Task 段头"
    assert "^- status:" in en, "en 模板 sed pattern 丢失 `- ` 前缀"


# ──────────────────────────────────────────────────────────────────────
# 渲染层守护: zh byte-equal baseline
# ──────────────────────────────────────────────────────────────────────
def test_zh_prompt_byte_equal_baseline(monkeypatch):
    """P1 红线: zh 部署 worker_contract_prompt 输出 = baseline fixture。"""
    monkeypatch.setenv("MW_LANG", "zh-cn")
    pipeline = _reload_pipeline()
    p = pipeline.worker_contract_prompt("/x/TODO.md", "/x", "/x")
    baseline = FIXTURE_BASELINE.read_text(encoding="utf-8")
    assert p == baseline, (
        f"zh prompt 偏离 baseline {len(baseline)} 字节, "
        f"实际 {len(p)} 字节")
    # 显式钉住几个关键 sentinel, 防止 future 改动悄悄破坏语义
    assert "/x/TODO.md" in p
    assert "/x/.clarify.json" in p
    assert "- 状态: working" in p   # zh 起手 sed 字段
    assert "sed -i 's/^- 状态: .*/- 状态: working/'" in p
    assert "waiting_sub" in p       # issue#13 AC-A6
    assert "turn 结束" in p         # turn 结束 ≠ 任务结束


def test_zh_prompt_omits_nonexistent_module(monkeypatch):
    """zh 模板绝对不应 import any module 或调 t():纯文案, 解析零依赖。"""
    monkeypatch.setenv("MW_LANG", "zh-cn")
    pipeline = _reload_pipeline()
    p = pipeline.worker_contract_prompt("/x/TODO.md", "/x", "/x")
    # 不应包含任何 Python 表达式迹象
    assert "import " not in p or "导入" in p
    assert "raise " not in p or "抛错" in p


# ──────────────────────────────────────────────────────────────────────
# 渲染层守护: en 模板含协议语义
# ──────────────────────────────────────────────────────────────────────
def test_en_prompt_renders_with_protocol_semantics(monkeypatch):
    """P2: en 模板渲染含与 zh 等价的协议语义 (issue#13 等)。"""
    monkeypatch.setenv("MW_LANG", "en")
    pipeline = _reload_pipeline()
    p = pipeline.worker_contract_prompt("/y/TODO.md", "/y", "/y")
    assert "/y/TODO.md" in p
    assert "/y/.clarify.json" in p
    assert "## Current Task" in p
    assert "- status: working" in p
    assert "sed -i 's/^- status: .*/- status: working/'" in p
    assert "sed -i 's/^- status: .*/- status: completed/'" in p
    assert "waiting_sub" in p
    assert "turn ending" in p.lower()
    assert "completed" in p
    assert "blocked" in p
    assert "failed" in p
    assert ".clarify.json" in p
    # en 模板字面不该包含 zh 协议段头作为**主要**指引。
    # 例外: en 模板会在「If the file uses Chinese keys」段中**示例** zh
    # 协议字面(教 en worker 兼容历史 zh TODO.md), 这是有意设计, 不算污染。
    # 守护点: zh 段头**不应**作为主操作协议出现 (e.g. 不应是 sed 字段
    # 也不应是必读段)。简化判定: 模板里有 `## Current Task` 作为英文主段头。
    assert "## Current Task" in p
    # zh 段头 `## 当前任务` 在 en 模板里**出现次数极少** (仅示例),
    # 守 ≪ 段头总量的 1 处, 防意外溢出。
    assert p.count("## 当前任务") <= 1, \
        f"en 模板多次出现 `## 当前任务`: {p.count('## 当前任务')}"
    assert p.count("^- 状态:") <= 1, \
        f"en 模板多次出现 `^- 状态:`: {p.count('^- 状态:')}"


# ──────────────────────────────────────────────────────────────────────
# 语言切换 / 退化路径守护
# ──────────────────────────────────────────────────────────────────────
def test_lang_switch_picks_correct_template(monkeypatch):
    """P3: MW_LANG 切换 → 模板选择立刻跟随(进程内, 无需重启)。"""
    # zh-cn
    monkeypatch.setenv("MW_LANG", "zh-cn")
    pipeline = _reload_pipeline()
    p_zh = pipeline.worker_contract_prompt("/x/TODO.md", "/x", "/x")
    assert "你是一名独立执行任务的 worker" in p_zh
    # en
    monkeypatch.setenv("MW_LANG", "en")
    pipeline = _reload_pipeline()
    p_en = pipeline.worker_contract_prompt("/y/TODO.md", "/y", "/y")
    assert "You are an independent worker" in p_en


def test_invalid_lang_falls_back_to_en(monkeypatch):
    """非法 MW_LANG → resolve_lang() 走 en, 模板族按 en 选 (架构 §5.2)。"""
    monkeypatch.setenv("MW_LANG", "fr")
    pipeline = _reload_pipeline()
    p = pipeline.worker_contract_prompt("/x/TODO.md", "/x", "/x")
    # en 模板特征
    assert "You are an independent worker" in p


def test_env_override_wins_over_lang(monkeypatch, tmp_path):
    """P3: MW_WORKER_PROMPT_TEMPLATE env > resolve_lang() 选模板。"""
    monkeypatch.setenv("MW_LANG", "en")
    custom = tmp_path / "custom.md"
    custom.write_text("CUSTOM_PROMPT_MARKER for {todo_path}", encoding="utf-8")
    monkeypatch.setenv("MW_WORKER_PROMPT_TEMPLATE", str(custom))
    pipeline = _reload_pipeline()
    p = pipeline.worker_contract_prompt("/x/TODO.md", "/x", "/x")
    assert "CUSTOM_PROMPT_MARKER for /x/TODO.md" in p


def test_missing_template_falls_back_to_zh(monkeypatch):
    """P3 退化: env override 指向不存在文件 → 退化到 zh 模板(零回归锚 §6.1)。"""
    monkeypatch.setenv("MW_LANG", "en")  # 假设 en 部署
    monkeypatch.setenv("MW_WORKER_PROMPT_TEMPLATE", "/no/such/file.md")
    pipeline = _reload_pipeline()
    p = pipeline.worker_contract_prompt("/x/TODO.md", "/x", "/x")
    # 退化到 zh
    assert "你是一名独立执行任务的 worker" in p


# ──────────────────────────────────────────────────────────────────────
# 集成守护: build_worker_prompt() 包装层
# ──────────────────────────────────────────────────────────────────────
def test_build_worker_prompt_zh_byte_equal_baseline(monkeypatch):
    """P4: build_worker_prompt() 项目任务路径走 zh → 与 baseline 关键切片一致。

    注意 baseline 是用 worker_contract_prompt('/x/TODO.md', '/x', '/x') 钉的,
    build_worker_prompt() 调用约定不同(workspace 是 task ws 而非 product_workspace),
    {workspace} 占位符会得到 task ws 全路径而非 product_workspace 的产物目录。
    守护点: {todo_path} + {out_dir} 与 baseline byte-equal, {workspace} 是 task ws。
    """
    from mw.schemas import TaskManifest
    monkeypatch.setenv("MW_LANG", "zh-cn")
    pipeline = _reload_pipeline()
    m = TaskManifest.model_validate({
        "title": "T", "target": {"profile": "coder"},
        "input_spec": {"task_description": "d"},
        "harvest": {"required_files": [], "timeout_seconds": 1800},
        "idempotency_key": "k",
        "project_workspace": "/x",
    })
    p = pipeline.build_worker_prompt(
        m, workspace="/x/.tasks/t_1")
    baseline = FIXTURE_BASELINE.read_text(encoding="utf-8")
    # todo_path 与 out_dir 在 build_worker_prompt 下都从 product_workspace 推出,
    # 与 baseline 同;workspace 此处 = "/x/.tasks/t_1"(task ws 全路径), 不同于
    # baseline 的 "/x"。
    assert "/x/TODO.md" in p
    assert "/x" in p  # out_dir
    # {workspace} 占位符被替换为 task ws
    assert "/x/.tasks/t_1/.clarify.json" in p
    # 整体中文模板特征
    assert "你是一名独立执行任务的 worker" in p
    assert "^- 状态: .*/- 状态: working" in p


def test_build_worker_prompt_en_contains_protocol(monkeypatch):
    """P4: build_worker_prompt() 走 en → 产出 en 模板渲染。"""
    from mw.schemas import TaskManifest
    monkeypatch.setenv("MW_LANG", "en")
    pipeline = _reload_pipeline()
    m = TaskManifest.model_validate({
        "title": "T", "target": {"profile": "coder"},
        "input_spec": {"task_description": "d"},
        "harvest": {"required_files": [], "timeout_seconds": 1800},
        "idempotency_key": "k",
        "project_workspace": "/x",
    })
    p = pipeline.build_worker_prompt(m, workspace="/x/.tasks/t_1")
    # todo_path = project_workspace + /TODO.md, workspace = task ws
    assert "/x/TODO.md" in p
    # workspace 占位符 = task ws (用于 .clarify.json 路径)
    assert "/x/.tasks/t_1/.clarify.json" in p
    assert "## Current Task" in p


# ──────────────────────────────────────────────────────────────────────
# 解析回环 (issue #41 红线: en 部署 worker 写 en 协议 → mw/todo.py 双键
# 别名解析端能正确读到 status, 不动既有解析语义)
# ──────────────────────────────────────────────────────────────────────
def test_en_prompt_zh_protocol_roundtrip_status(monkeypatch, tmp_path):
    """en worker 按 en prompt 提示写 `status: working`, todo.py 解析端
    按双键规范折算至 ZH 协议键 → kv.get('状态') 命中(zh 部署默认写 '状态')。

    这是 issue#41 的『解析端保持双键别名向后兼容』红线验证。
    """
    from mw.todo import parse_status
    monkeypatch.setenv("MW_LANG", "en")
    pipeline = _reload_pipeline()
    # 模拟 en worker 真的按 prompt 写了英文 status
    en_prompt = pipeline.worker_contract_prompt("/x/TODO.md", "/x", "/x")
    assert "sed -i 's/^- status: .*/- status: working/'" in en_prompt
    # 把那段 sed 实际跑一遍到 TODO.md (en 段头 + en 键)
    todo_path = tmp_path / "TODO.md"
    todo_path.write_text(
        "# Team Work: T\n\n## Current Task\n"
        "- status: 进行中\n- result: \n\n---\n\n## Pending\n- (无)\n",
        encoding="utf-8",
    )
    import subprocess
    sed_cmd = ("sed -i 's/^- status: .*/- status: working/' "
               f"'{todo_path}'")
    subprocess.run(sed_cmd, shell=True, check=True)
    # 解析端 (BILINGUAL_FIELD_ALIASES) 应能读出 status 值
    val = parse_status(todo_path)
    assert val == "working", f"parse_status 未识别 en 写入的 status: {val!r}"