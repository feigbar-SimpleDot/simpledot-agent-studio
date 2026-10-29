#!/usr/bin/env python3
# coding: utf-8
"""issue #156 回归守护: verify.sh 的 ok()/bad() 必须双语输出 + 单参调用点兜底。

背景 (K 批, 2026-10-29):

    verify.sh L37-38 的 ok()/bad() 此前只打印 $1 (英文), 第二参数 (中文)
    被静默丢弃 ⇒ 全文件双参调用点的中文文案是死代码, LANG=zh-cn 的运维
    看到的仍是英文门禁文案。修复: ok/bad 复用 deploy/lib/i18n.sh 的
    _i18n_pick (与 section_banner 同一 SAS_LANG 口径), 并带 "${2:-$1}"
    单参兜底 —— verify.sh 的 LANG= 报告行只传一参, 若无兜底, zh-cn 下
    该行输出空行。

守护的不变量:

    S1  ok()/bad() 定义各只有一处, 且都经 _i18n_pick 选语言,
        第二参用 "${2:-$1}" 兜底 (单参调用回退英文原文而非空串)。
    S2  双参调用点的英文第一参数字面逐字未动 (钉住关键几条, 防文案漂移)。
    R1  真实脚本 zh-cn 实测: bad 行输出中文 (容器未运行), 同行不含英文原文。
    R2  真实脚本 en 实测: bad 行输出英文 (Container not running),
        同行不含中文原文 (语言选择未反转)。
    R3  LANG= 报告行 (单参调用点) 在 zh-cn / en 两跑均非空且含 LANG=。
    R4  zh-cn 抽查: 控制台无响应 / 任务 API 失败 / SKILL 缺失行均为中文。
    R5  PASS/FAIL 计数逻辑未动: 汇总行数字与 ✓/✗ 行数一致。

★ 真实脚本实测纪律 (BRIEF-s4 硬约束): 运行时把仓库 deploy/ 整目录拷入
tmp 后以真实入口 `bash deploy/verify.sh` 跑 (等价于 git archive 展开,
且对工作区/HEAD 两种 checkout 都成立), 禁止照抄 ok/bad 定义的合成小脚本
—— 合成脚本证不了运行时真用哪个定义。

本文件不碰真机、不发真实网络请求: 容器名指向不存在容器、控制台端口指向
关闭端口, 所有门禁走"不可达"分支, 恰好覆盖 bad() 的中文路径。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERIFY_SH = ROOT / "deploy" / "verify.sh"

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

# zh-cn 下必须出现的中文锚 (对应各自门禁的英文第一参数)
ZH_ANCHORS = {
    "容器未运行": "Container not running",
    "控制台无响应": "Console not responding",
    "任务 API 失败": "Task API failed",
}


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _run_verify(tmp_path: Path, lang: str) -> list[str]:
    """真实脚本实测: 拷贝仓库 deploy/ 到 tmp, 以真实入口跑 verify.sh。

    容器名指向不存在容器 + 控制台端口指向关闭端口 ⇒ 全部门禁走 bad 分支,
    正好检验 bad() 的语言选择。返回去色后的输出行列表。
    """
    work = tmp_path / "deploy-root"
    shutil.copytree(ROOT / "deploy", work / "deploy", symlinks=True)
    env = dict(os.environ)
    env["LANG"] = lang
    env["MAIN_CONTAINER_NAME"] = "definitely-no-such-container"
    env["CONSOLE_PORT"] = "1"          # 必然 connection refused
    env.pop("SAS_LANG", None)          # 语言只由 LANG 推导 (verify.sh: SAS_LANG=${LANG:-en})
    env.pop("HERMES_WEBUI_DIR", None)  # 跳过可选 webui 段
    proc = subprocess.run(
        ["bash", "deploy/verify.sh"], cwd=work, env=env,
        capture_output=True, text=True, timeout=180)
    # verify.sh 有 FAIL 时 exit 1 属预期 (门禁红), 不代表脚本没跑
    assert proc.stdout, "verify.sh 无任何 stdout 输出, 脚本没跑起来"
    return _strip_ansi(proc.stdout).splitlines()


# ────────────────────────────────────────────────────────────────
# S1: ok/bad 定义唯一且经 _i18n_pick + 单参兜底
# ────────────────────────────────────────────────────────────────
def test_ok_bad_defined_once_via_i18n_pick_with_fallback():
    src = VERIFY_SH.read_text(encoding="utf-8")
    for fn in ("ok", "bad"):
        defs = re.findall(
            r'(?m)^' + fn + r'\(\)\s*\{.*\}$', src)
        assert len(defs) == 1, f"{fn}() 定义应恰好一处, 实得 {len(defs)} 处"
        body = defs[0]
        assert "_i18n_pick" in body, f"{fn}() 未经 _i18n_pick, zh-cn 下不会输出中文"
        assert '"${2:-$1}"' in body, (
            f'{fn}() 缺单参兜底 "${{2:-$1}}" ⇒ 单参调用点 (LANG= 报告行) '
            "在 zh-cn 下会输出空行")


def test_i18n_sh_not_repicked():
    """ok/bad 依赖 lib/i18n.sh 的 _i18n_pick: verify.sh 必须仍装载该库。"""
    src = VERIFY_SH.read_text(encoding="utf-8")
    assert re.search(r'\.\s+"\$\{LIB_DIR\}/i18n\.sh"', src), \
        "verify.sh 不再装载 lib/i18n.sh, _i18n_pick 将不存在"


# ────────────────────────────────────────────────────────────────
# S2: 英文第一参数字面逐字未动 (钉关键条, 防与既有文档漂移)
# ────────────────────────────────────────────────────────────────
def test_english_literals_unchanged():
    src = VERIFY_SH.read_text(encoding="utf-8")
    for lit in (
        'ok "LANG=${SAS_LANG} (allowed: en|zh-cn)"',
        'bad "Container not running (docker logs ${MAIN_CONTAINER_NAME})"',
        'bad "Console not responding"',
        'bad "Task API failed"',
        '"SOUL/SKILL content stale: host=',
    ):
        assert lit in src, f"英文文案字面漂移: 找不到 {lit!r}"


# ────────────────────────────────────────────────────────────────
# R1/R3/R4: zh-cn 真实跑
# ────────────────────────────────────────────────────────────────
def test_zh_cn_real_script_outputs_chinese(tmp_path):
    lines = _run_verify(tmp_path, "zh-cn")
    for zh, en in ZH_ANCHORS.items():
        hit = [l for l in lines if zh in l]
        assert hit, f"zh-cn 跑未出现中文门禁文案 {zh!r} (bad() 仍在丢第二参)"
        assert not any(en in l for l in hit), \
            f"zh-cn 下 {zh!r} 同行混入英文原文 {en!r}"
    # SKILL 缺失行 (docker exec 必失败 ⇒ 4 条 bad)
    skill_lines = [l for l in lines if "缺失" in l]
    assert skill_lines, "zh-cn 跑 SKILL 缺失行未输出中文"
    # R3: 单参调用点 LANG= 行非空 (MV-156-B 的拦截断言)
    lang_lines = [l for l in lines if "LANG=" in l and "✓" in l]
    assert lang_lines, "找不到 LANG= 报告行"
    for l in lang_lines:
        text = l.split("✓", 1)[1].strip()
        assert text and "LANG=" in text, \
            f"LANG= 单参调用点输出空/残缺 (单参兜底被去掉): {l!r}"


# ────────────────────────────────────────────────────────────────
# R2: en 真实跑 (语言选择未反转)
# ────────────────────────────────────────────────────────────────
def test_en_real_script_outputs_english(tmp_path):
    lines = _run_verify(tmp_path, "en")
    for zh, en in ZH_ANCHORS.items():
        hit = [l for l in lines if en in l]
        assert hit, f"en 跑未出现英文门禁文案 {en!r}"
        assert not any(zh in l for l in hit), \
            f"en 下 {en!r} 同行混入中文 {zh!r} (语言选择反转)"
    # R3: en 侧 LANG= 行同样非空
    lang_lines = [l for l in lines if "LANG=" in l and "✓" in l]
    assert lang_lines and all(l.split("✓", 1)[1].strip() for l in lang_lines)


# ────────────────────────────────────────────────────────────────
# R5: PASS/FAIL 计数逻辑未动 (汇总行与 ✓/✗ 行数一致)
# ────────────────────────────────────────────────────────────────
def test_pass_fail_counts_consistent(tmp_path):
    lines = _run_verify(tmp_path, "en")
    n_ok = sum(1 for l in lines if "✓" in l)
    n_bad = sum(1 for l in lines if "✗" in l)
    summary = [l for l in lines if re.search(r"\d+ failed / \d+ passed", l)]
    assert summary, "找不到英文汇总行 (尾部汇总被改动?)"
    m = re.search(r"(\d+) failed / (\d+) passed", summary[0])
    assert (int(m.group(1)), int(m.group(2))) == (n_bad, n_ok), \
        "汇总计数与 ✓/✗ 行数不一致 (PASS/FAIL 计数逻辑被改动)"
