"""tests/test_q197_inbox_order.py — #197 收件箱排序前端面守护 (s3, 卡 t_aa9a591c)。

依据: docs/PLAN-Q-20261102.md §1.2 (排序由后端出序, 前端只切组不重排)
      + docs/REQUIREMENTS-Q-20261102.md AC-Q197-1/6/16。

⚠ 为什么前端断言落在 Python 侧 (而不是 ui/tests/*.mjs):
  PLAN §3「改动面互斥论证」明确约定 —— 「s3 **不新增** ui/tests/*.mjs
  (其前端逻辑由 python 侧测试 + s6 真机回归覆盖), 使 ui/package.json
  test 链仅 s5 触碰, 消除第二处相交」。本文件即该约定的落点: 前端
  源码断言进 Python 测试, 不动 ui/tests/ 与 ui/package.json。

覆盖:
  Q1  schedGroups 四分组: paused 独立成组 (AC-Q197-6) —— 暂停任务
      必须排除出「待执行」分组, 但保留可见入口 (OQ-1)
  Q2  schedGroups 保持服务端预排 (不重排) —— 显示序 = 执行序
  Q3  mergePendingWithCron **升序** (AC-Q197-16) —— 时间早的在上;
      取代旧 A8 裁决的「倒序 = 新在前」(变异反证点 M9)
  Q4  视图确实渲染「已暂停」分组 + 空态判空并入 paused
  Q5  i18n 键集两侧一致 + schedule.group.paused 双语在位
  Q6  源码形态锚: mergePendingWithCron 不得回退到 .reverse()

跑法: pytest tests/test_q197_inbox_order.py -v
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHED_VIEW = ROOT / "ui" / "src" / "views" / "ScheduleView.jsx"
EN_JSON = ROOT / "ui" / "src" / "i18n" / "locales" / "en.json"
ZH_JSON = ROOT / "ui" / "src" / "i18n" / "locales" / "zh-cn.json"


@pytest.fixture(scope="module")
def sched_src() -> str:
    return SCHED_VIEW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def en_locale() -> dict:
    return json.loads(EN_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def zh_locale() -> dict:
    return json.loads(ZH_JSON.read_text(encoding="utf-8"))


def _extract_fn(src: str, name: str) -> str:
    """按函数名定位 + 大括号配平截取函数体 (不复制一份逻辑到本文件)。"""
    idx = src.find(f"function {name}(")
    if idx < 0:
        raise AssertionError(f"源码抽取失败: 找不到 {name}")
    braces = 0
    seen = False
    for i in range(idx, len(src)):
        ch = src[i]
        if ch == "{":
            braces += 1
            seen = True
        elif ch == "}":
            braces -= 1
        if seen and braces == 0:
            return src[idx:i + 1]
    raise AssertionError(f"源码抽取失败: {name} 大括号未配平")


# ══════════════════════════════════════════════════════════════════
# Q1: 四分组 (paused 独立成组)
# ══════════════════════════════════════════════════════════════════
def test_q1_groups_have_four_buckets(sched_src):
    """四桶齐备: running / pending / paused / done。"""
    fn = _extract_fn(sched_src, "schedGroups")
    for bucket in ("running", "pending", "paused", "done"):
        assert f"{bucket}:" in fn, f"缺 {bucket} 分组桶"
    # paused 必须是独立桶, 不得并回 pending
    assert "pending: [], paused: []" in fn or \
        re.search(r"running:\s*\[\],\s*pending:\s*\[\],\s*paused:\s*\[\],\s*done:\s*\[\]", fn), \
        "分组桶定义未含独立的 paused 桶"


def test_q1_paused_not_in_pending_group(sched_src):
    """AC-Q197-6: paused 任务不进 pending 组, 而进 paused 组。

    MV-197-3: 把 paused 并回 pending ⇒ 本用例红。
    """
    fn = _extract_fn(sched_src, "schedGroups")
    # 分派形态: paused 走独立分支进 g.paused
    assert re.search(r"status\s*===\s*'paused'\)", fn), \
        "schedGroups 未按 paused 状态分派"
    seg = fn[fn.find("'paused'"):]
    assert "g.paused.push" in seg, "paused 任务未进 g.paused"
    # 旧形态「pending || paused 进 g.pending」必须消失
    assert not re.search(r"'pending'\s*\|\|\s*t\.status\s*===\s*'paused'", fn), \
        "paused 仍并入 pending 组 (AC-Q197-6 违反)"


def test_q1_unknown_status_falls_into_done(sched_src):
    """坏形状防御: 未知状态归 done 组 (与旧行为一致)。"""
    fn = _extract_fn(sched_src, "schedGroups")
    assert "else g.done.push(t)" in fn, "未知状态未落 done 组"


def test_q1_done_bucket_still_truncated_to_50(sched_src):
    """R-5 缓解口径不变: done 组仍截 50 条。"""
    fn = _extract_fn(sched_src, "schedGroups")
    assert "g.done.slice(0, 50)" in fn, "done 组截 50 的口径被改动"


def test_q1_empty_input_does_not_crash(sched_src):
    """非数组输入不炸 (空四组)。"""
    fn = _extract_fn(sched_src, "schedGroups")
    assert "Array.isArray(tasks)" in fn, "缺非数组输入防御"


# ══════════════════════════════════════════════════════════════════
# Q2: 前端不重排 (服务端预排是唯一真相)
# ══════════════════════════════════════════════════════════════════
def test_q2_no_client_side_resort(sched_src):
    """前端只切组不重排 —— 排序由后端出序 (PLAN §1.2 裁决)。

    MV-197-4: 在 schedGroups 里加 .sort() ⇒ 本用例红。
    """
    fn = _extract_fn(sched_src, "schedGroups")
    assert ".sort(" not in fn, "schedGroups 自行排序 (排序须由后端出序, 前端只切组)"
    assert ".reverse(" not in fn, "schedGroups 出现 reverse (会破坏服务端预排)"


def test_q2_merge_keeps_server_order_for_tasks(sched_src):
    """mergePendingWithCron 的任务条目不得自行按 added_at 重排。"""
    fn = _extract_fn(sched_src, "mergePendingWithCron")
    seg = fn[:fn.find("items.sort")]
    assert ".sort(" not in seg, "任务条目映射阶段不得排序"


# ══════════════════════════════════════════════════════════════════
# Q3: mergePendingWithCron 升序 (AC-Q197-16)
# ══════════════════════════════════════════════════════════════════
def test_q3_ascending_sort_expression(sched_src):
    """排序表达式为升序 (a.at - b.at)。

    MV-197-5: 把 `a.at - b.at` 改成 `b.at - a.at` ⇒ 本用例红。
    """
    fn = _extract_fn(sched_src, "mergePendingWithCron")
    assert "a.at - b.at" in fn, "排序表达式缺失或反向 (须为升序 a.at - b.at)"


def test_q3_no_reverse_call(sched_src):
    """变异反证点 M9: mergePendingWithCron 不得回退到 .reverse()。

    旧实现 sort 后 reverse ⇒ 新在前 —— 与 #197 的升序口径相反。
    """
    fn = _extract_fn(sched_src, "mergePendingWithCron")
    assert ".reverse(" not in fn, \
        "mergePendingWithCron 不得再 reverse (那会把顺序改回「新在前」)"


def test_q3_a8_comment_updated(sched_src):
    """旧 A8 注释已更新 (不得再宣称「倒序 = 新在前」)。"""
    idx = sched_src.find("export function mergePendingWithCron")
    assert idx > 0, "找不到 mergePendingWithCron"
    seg = sched_src[idx:idx + 900]
    assert "时间倒序" not in seg, "注释仍宣称时间倒序 (与 #197 升序矛盾)"
    assert "#197" in seg, "注释未标注 #197 取代 A8 的裁决"


def test_q3_cron_filter_unchanged(sched_src):
    """cron 预览筛选口径不变 (scheduled 态 + 有 next_run_at)。"""
    fn = _extract_fn(sched_src, "mergePendingWithCron")
    assert "job.state !== 'scheduled'" in fn, "cron 状态筛选被改动"
    assert "Number.isNaN(at)" in fn, "cron 时间解析防御被改动"


# ══════════════════════════════════════════════════════════════════
# Q4: 视图渲染「已暂停」分组
# ══════════════════════════════════════════════════════════════════
def test_q4_view_renders_paused_group(sched_src):
    """视图渲染 paused 分组 (源码锚)。"""
    assert "schedule.group.paused" in sched_src, "视图未引用 schedule.group.paused 词条"
    assert "groups.paused.map(renderTaskRow)" in sched_src, "视图未渲染 paused 分组"


def test_q4_empty_state_includes_paused(sched_src):
    """空态判空并入 paused 组 (只有暂停任务时不该显示空态)。"""
    idx = sched_src.find("const empty =")
    assert idx > 0, "找不到 empty 判定"
    seg = sched_src[idx:idx + 300]
    assert "groups.paused.length === 0" in seg, \
        "空态判空未并入 paused 组 (只有暂停任务时会误显示空态)"


def test_q4_paused_actions_available(sched_src):
    """已暂停分组可恢复/取消/修改 (复用既有 schedActions('paused'))。"""
    assert "schedActions('paused')" in sched_src or \
        re.search(r"status\s*===\s*'paused'.*\['resume',\s*'cancel',\s*'edit'\]", sched_src), \
        "paused 任务的操作集缺失 (恢复入口不可达)"


# ══════════════════════════════════════════════════════════════════
# Q5: i18n 键集
# ══════════════════════════════════════════════════════════════════
def _flat(d: dict, p: str = "") -> list[str]:
    out: list[str] = []
    for k, v in d.items():
        key = f"{p}.{k}" if p else k
        if isinstance(v, dict):
            out.extend(_flat(v, key))
        else:
            out.append(key)
    return out


def test_q5_locale_key_sets_identical(en_locale, zh_locale):
    """locale 键集两侧完全一致。"""
    fz, fe = _flat(zh_locale), _flat(en_locale)
    assert [k for k in fe if k not in fz] == [], "only-in-en"
    assert [k for k in fz if k not in fe] == [], "only-in-zh"


def test_q5_group_paused_key_present_both_sides(en_locale, zh_locale):
    """schedule.group.paused 双语在位且非空。"""
    assert zh_locale["schedule"]["group"]["paused"] == "已暂停"
    assert en_locale["schedule"]["group"]["paused"] == "Paused"


def test_q5_view_referenced_schedule_keys_exist(sched_src, en_locale, zh_locale):
    """视图引用的 schedule.* 词条两侧都存在 (源码级反查)。"""

    def get(obj, key):
        for part in key.split("."):
            if not obj:
                return None
            obj = obj.get(part)
        return obj

    used = set(re.findall(r"schedule\.[a-zA-Z_]+\.[a-zA-Z_]+", sched_src))
    for k in used:
        assert get(zh_locale, k) is not None, f"zh-cn 缺源码引用的 {k}"
        assert get(en_locale, k) is not None, f"en 缺源码引用的 {k}"
