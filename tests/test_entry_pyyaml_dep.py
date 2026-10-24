"""deploy/main_entry.sh 的 pyyaml 部署依赖钉桩 (issue #97)。

背景 (REVIEW-78-70 §2.6): mw/workflows.yaml 与 issue #70 的
mw/decision_paths.yaml 都要 `import yaml`, 但镜像裸 python3 实测无
PyYAML, 而 entry 的依赖安装链只装 watchdog ⇒ /api/decision-paths
静默恒返 {"paths": []}。修法 = pyyaml 与 watchdog 同机制: 探测缺失
包集合 → 一次 uv/pip --target ${MW_DEPS_DIR} → 失败逐项 warn 降级。

本文件只做**静态钉桩** (worker SOUL 边界: 容器实跑归 tester):
锚定「探测 pyyaml」「安装目标含 pyyaml」「pyyaml 缺失 warn」「齐备短路」
四个语义点, 任何一处回退即红。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = (ROOT / "deploy" / "main_entry.sh").read_text(encoding="utf-8")


def test_entry_probes_pyyaml():
    """依赖段必须对 pyyaml 做 import 探测 (与 watchdog 同款探测式)。"""
    assert re.search(r"python3 -c \"import yaml\"", ENTRY), \
        "main_entry.sh 缺 pyyaml import 探测 (issue #97)"


def test_entry_installs_missing_deps_into_mw_deps_dir():
    """安装命令的目标包必须是缺失集合变量 (watchdog/pyyaml 共用一条链),
    不得再出现只写死 watchdog 的旧写法。"""
    # 反斜杠续行拼平后再匹配
    flat = ENTRY.replace("\\\n", " ")
    install_cmds = re.findall(
        r"(?:uv pip install|pip install)[^\n]*?--target \"?\$\{MW_DEPS_DIR\}\"?\s+([^\n>&]+)",
        flat)
    assert install_cmds, "找不到 --target ${MW_DEPS_DIR} 安装命令"
    for cmd in install_cmds:
        assert "${MW_DEPS_MISSING}" in cmd, \
            f"安装命令未纳入缺失集合变量: {cmd.strip()!r}"


def test_entry_warns_on_pyyaml_degradation():
    """pyyaml 装不上 ⇒ 恰一行 warn 指向两端点降级空表 (不阻塞启动)。"""
    assert "pyyaml install failed" in ENTRY
    assert "/api/decision-paths degrade to empty tables" in ENTRY


def test_entry_short_circuit_covers_both_deps():
    """两包齐备 ⇒ 一行 OK 短路, 零安装动作 (幂等, recreate 秒起)。"""
    assert "watchdog + pyyaml OK" in ENTRY


def test_watchdog_semantics_preserved():
    """红线: watchdog 原口径不得丢失 —— fsnotify 主路径文案与 15s 轮询
    降级 warn 仍在, PYBOOT pip-wheel 引导段仍在。"""
    assert "fsnotify primary path available" in ENTRY
    assert "reaper will degrade to 15s polling" in ENTRY
    assert "PYBOOT" in ENTRY
    # export PYTHONPATH 前置 mw-deps 的机制不变
    assert 'export PYTHONPATH="${MW_DEPS_DIR}:${PYTHONPATH:-}"' in ENTRY
