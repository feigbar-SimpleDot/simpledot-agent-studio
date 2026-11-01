"""O 批 fix-up 回归测试 (2026-10-31, 卡 t_ec0c8bce / s4f)。

依据 docs/REVIEW-O.md:
  F-1 (P2) #174 的 todo_path_for 口径: 「pw 无条件最优先」使**非项目卡**
         (workspace 无 /.tasks/ 段) 且 contract 带 product_workspace 的形态
         (下称形态 D) 的状态来源从「workspace 根」改判为「product_workspace」
         ⇒ worker 在自己 workspace 根写 completed, reaper 却读到 pw 那份的
         working ⇒ 该卡静默不结算 (或反向误收割)。
         本卡采纳审查推荐**选项 2** (钉成分歧口径): 按 workspace 形态分流 ——
           - 项目卡 (workspace 含 /.tasks/) ⇒ pw/项目根优先 (保住 #174 同源收益)
           - 非项目卡 ⇒ workspace 根为权威源, pw 只作兜底 (与改前 reaper 序一致)
  F-2 (P3) TAB 改名残留: mw/i18n/en.json 的 schedule.err_bad_transition 英文值
         仍回显「schedule task」⇒ 改为「inbox task」(键名不动, 键集零增删)。

形态矩阵 (对照 REVIEW-O §F-1 证据表, 逐条锁定):
  A 项目卡 (ws=<pw>/.tasks/t_1)            ⇒ <pw>/TODO.md      (改前=改后)
  B 平铺卡, ws 根有 TODO.md, pw 无 TODO.md   ⇒ <ws>/TODO.md      (改前=改后)
  C 平铺卡, ws 根有 TODO.md, 无 pw          ⇒ <ws>/TODO.md      (改前=改后)
  D 平铺卡, ws 根有 TODO.md, pw 另有 TODO.md ⇒ <ws>/TODO.md  ★F-1 修复点
  E ws=None, pw 有效                       ⇒ <pw>/TODO.md      (改前 None)

变异反证锚 (MV):
  MV-F1-A 把 pw 分支改回「无条件最优先」            ⇒ K-D 红 (形态 D 改判)
  MV-F1-B 去掉非项目卡的 workspace 根分支             ⇒ K-D 红
  MV-F1-C 把 F-2 英文值改回「schedule task」         ⇒ K-F2 红

纯本地文件/纯函数测试: 不依赖 hermes CLI / 网络 / 容器。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mw.harvest import todo_path_for  # noqa: E402
from mw.reaper import _read_todo_status, _todo_path_for_task  # noqa: E402

TODO_TEXT = """# Team Work: probe

## 当前任务
- 步骤: s1
- 标题: T
- 卡: t_x
- 状态: {status}
- 结果: (probe)
"""


def _write_todo(path: Path, status: str) -> str:
    """写一份带『状态』字段的 TODO.md (段落格式与 mw/todo 协议一致)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TODO_TEXT.format(status=status), encoding="utf-8")
    return str(path)


# ════════════════════════════════════════════════════════════════
# F-1: todo_path_for 形态矩阵 (项目卡 pw 优先 / 非项目卡 ws 根优先)
# ════════════════════════════════════════════════════════════════

def test_f1_shape_a_project_card_pw_first(tmp_path):
    """形态 A (项目卡): workspace 含 /.tasks/ ⇒ pw/TODO.md 优先。"""
    pw = tmp_path / "proj"
    ws = tmp_path / "proj" / ".tasks" / "t_1"
    todo = _write_todo(pw / "TODO.md", "working")
    p = todo_path_for(str(ws), str(pw))
    assert p == todo, (p, todo)
    # reaper 侧薄委托同源
    assert _todo_path_for_task(str(ws), None, {"product_workspace": str(pw)}) == todo


def test_f1_shape_b_flat_card_pw_without_todo(tmp_path):
    """形态 B (平铺卡, pw 指向无 TODO.md 的目录): 落到 workspace 根。"""
    ws = tmp_path / "ws"
    todo = _write_todo(ws / "TODO.md", "completed")
    pw = tmp_path / "other"          # 存在但没有 TODO.md
    pw.mkdir(parents=True)
    p = todo_path_for(str(ws), str(pw))
    assert p == todo, (p, todo)


def test_f1_shape_c_flat_card_no_pw(tmp_path):
    """形态 C (平铺卡, 无 pw): 落到 workspace 根。"""
    ws = tmp_path / "ws"
    todo = _write_todo(ws / "TODO.md", "completed")
    p = todo_path_for(str(ws), None)
    assert p == todo, (p, todo)


def test_f1_shape_d_flat_card_ws_root_wins_over_pw(tmp_path):
    """形态 D ★F-1 修复点: 非项目卡 ⇒ workspace 根为权威源 (pw 只兜底)。

    这是 F-1 的核心: 改前 (pw 无条件最优先) 会读到 pw 那份的
    「working」, 与改前 reaper 的「workspace 根」不一致 ⇒ 状态改判。
    """
    ws = tmp_path / "ws"                       # 非项目卡 (无 /.tasks/ 段)
    ws_todo = _write_todo(ws / "TODO.md", "completed")
    pw = tmp_path / "pw"                       # 另一份内容不同的 TODO.md
    pw_todo = _write_todo(pw / "TODO.md", "working")

    p = todo_path_for(str(ws), str(pw))
    assert p == ws_todo, f"非项目卡须读 workspace 根, 实得 {p} (pw 那份={pw_todo})"
    # 状态来源断言: reaper 读到的状态必须与 worker 写在 workspace 根的一致
    assert _read_todo_status(str(ws), None,
                             {"product_workspace": str(pw)}) == "completed"


def test_f1_shape_e_ws_none_pw_used(tmp_path):
    """形态 E (ws=None, pw 有效): pw/TODO.md (与改后口径一致)。"""
    pw = tmp_path / "pw"
    todo = _write_todo(pw / "TODO.md", "working")
    p = todo_path_for(None, str(pw))
    assert p == todo, (p, todo)


def test_f1_shape_d_workspace_root_missing_falls_back_to_pw(tmp_path):
    """形态 D 变体: workspace 根**没有** TODO.md ⇒ 落 pw (兜底仍可达)。"""
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    pw = tmp_path / "pw"
    todo = _write_todo(pw / "TODO.md", "working")
    p = todo_path_for(str(ws), str(pw))
    assert p == todo, (p, todo)


def test_f1_contract_pw_fallback_still_reachable(tmp_path):
    """contract.product_workspace 兜底分支仍可达 (第 4 级不被改掉)。"""
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    pw = tmp_path / "pw"
    pw.mkdir(parents=True)
    # pw/TODO.md **不存在** ⇒ 前三级全判不出 ⇒ 落第 4 级 (不校验存在性)
    p = todo_path_for(str(ws), None, {"product_workspace": str(pw)})
    assert p == str(pw / "TODO.md"), p


def test_f1_project_card_keeps_harvest_same_source(tmp_path):
    """项目卡的 #174 同源收益不得被 F-1 修正误伤 (reaper/harvest 同一路径)。"""
    from mw.harvest import _todo_path_for_harvest
    pw = tmp_path / "proj"
    ws = tmp_path / "proj" / ".tasks" / "t_1"
    todo = _write_todo(pw / "TODO.md", "working")
    p_reaper = _todo_path_for_task(str(ws), None, {"product_workspace": str(pw)})
    p_harvest = _todo_path_for_harvest(str(pw), str(ws))
    assert p_reaper == p_harvest == todo, (p_reaper, p_harvest, todo)


def test_f1_shape_d_under_path_map(tmp_path):
    """形态 D 在容器路径映射 (MW_PATH_MAP) 下同样读 workspace 根。

    形态判定用的是**映射后**路径 (与切分同一份), 避免映射前后形态不一致。
    """
    base = str(tmp_path)
    with mock.patch.dict(os.environ, {"MW_PATH_MAP": f"{base}/ctr={base}/host"}):
        ws_host = tmp_path / "host" / "ws"
        ws_todo = _write_todo(ws_host / "TODO.md", "completed")
        pw_host = tmp_path / "host" / "pw"
        _write_todo(pw_host / "TODO.md", "working")
        ws_ctr = f"{base}/ctr/ws"
        pw_ctr = f"{base}/ctr/pw"
        p = todo_path_for(ws_ctr, pw_ctr)
        assert p == ws_todo, f"映射后仍须读 workspace 根, 实得 {p}"


def test_f1_mapped_project_card_still_splits_tasks_segment(tmp_path):
    """映射后的项目卡仍走 /.tasks/ 切分 (项目根) 分支。"""
    base = str(tmp_path)
    with mock.patch.dict(os.environ, {"MW_PATH_MAP": f"{base}/ctr={base}/host"}):
        proj_host = tmp_path / "host" / "proj"
        todo = _write_todo(proj_host / "TODO.md", "working")
        ws_ctr = f"{base}/ctr/proj/.tasks/t_1"
        pw_ctr = f"{base}/ctr/proj"
        p = todo_path_for(ws_ctr, pw_ctr)
        assert p == todo, (p, todo)


# ════════════════════════════════════════════════════════════════
# F-2: TAB 改名残留 (mw/i18n schedule.err_bad_transition 英文值)
# ════════════════════════════════════════════════════════════════

def test_f2_err_bad_transition_en_uses_inbox_wording():
    """F-2: 英文值改为「inbox task」, 键名零改 (键集不变)。"""
    en = json.loads((ROOT / "mw" / "i18n" / "en.json").read_text(encoding="utf-8"))
    assert en["schedule.err_bad_transition"] == \
        "Illegal state transition for this inbox task", \
        en["schedule.err_bad_transition"]
    # 旧措辞零残留 (全表扫描, 防同型遗漏)
    stale = {k: v for k, v in en.items()
             if isinstance(v, str) and "schedule task" in v.lower()}
    assert not stale, f"mw/i18n/en.json 仍残留「schedule task」: {stale}"


def test_f2_zh_side_unchanged_and_key_set_equal():
    """F-2 改动只碰英文值: 中文侧逐字不变 + 双表键集仍相等 (G1 同口径)。"""
    en = json.loads((ROOT / "mw" / "i18n" / "en.json").read_text(encoding="utf-8"))
    zh = json.loads((ROOT / "mw" / "i18n" / "zh-cn.json").read_text(encoding="utf-8"))
    assert zh["schedule.err_bad_transition"] == "当前状态不允许该操作", \
        zh["schedule.err_bad_transition"]
    assert set(en) == set(zh), "F-2 不得增删键"
    assert len(en) == 232, f"mw/i18n 顶层键数变了: {len(en)} (锚 232)"


def test_f2_no_schedule_wording_in_mw_locale_values():
    """mw/i18n 双表: 用户可见值内不得再出现「schedule task」措辞。"""
    for name in ("en.json", "zh-cn.json"):
        d = json.loads((ROOT / "mw" / "i18n" / name).read_text(encoding="utf-8"))
        hits = {k: v for k, v in d.items()
                if isinstance(v, str) and "schedule task" in v.lower()}
        assert not hits, f"{name} 残留「schedule task」: {hits}"
