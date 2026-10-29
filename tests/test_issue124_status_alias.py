"""issue #124 守护: TODO 状态中英别名表 + 单一归一化入口 (含变异反证)。

覆盖 (PLAN-E-20261026 §1.5 / REQUIREMENTS R-124-E):
  1. 表完整性正证: STATUS_ALIASES 逐名覆盖 R-124-A 全部别名,
     大小写/首尾空白不敏感, 未识别 → None。
  2. 结算路径正证: 中文终态写法在主 reap / late_settlement / console
     reassess 三条路径分别触发 COMPLETE / FAILED / NEED_CLARIFICATION
     (AC-124-1)。
  3. 变异反证: 从反查索引删一项 → normalize 返回 None 且结算守护转红
     (证明守护依赖表而非恒绿, AC-124-3)。
  4. 全仓扫描守护: reaper.py / console.py 不再出现绕过入口的终态
     字面量集合, normalize_status 出现次数达标 (AC-124-2)。
  5. 展示映射: _TODO_STATUS_SEM 按归一化规范值命中正确徽章 (AC-124-4)。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mw.todo as todo
from mw.todo import (
    STATUS_ALIASES,
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_WAITING_SUB,
    STATUS_WORKING,
    IN_PROGRESS_SET,
    TERMINAL_STATUSES,
    TodoTask,
    init_todo,
    normalize_status,
)

# ──────────────────────────────────────────────────────────────────────
# 1. 表完整性正证 (R-124-A)
# ──────────────────────────────────────────────────────────────────────

EXPECTED_ALIASES = {
    STATUS_COMPLETED: {"completed", "complete", "done", "已完成", "完成"},
    STATUS_FAILED: {"failed", "fail", "已失败", "失败"},
    STATUS_BLOCKED: {"blocked", "block", "已阻塞", "阻塞"},
    STATUS_WORKING: {"working", "进行中", "in-progress", "inprogress",
                     "in_progress"},
    STATUS_WAITING_SUB: {STATUS_WAITING_SUB, "waiting-sub", "等待子任务"},
}


def test_alias_table_covers_all_required_names():
    """逐名断言 STATUS_ALIASES 覆盖 R-124-A 全部别名, 一项不缺。"""
    for canon, required in EXPECTED_ALIASES.items():
        actual = set(STATUS_ALIASES[canon])
        missing = required - actual
        assert not missing, f"{canon} 缺别名: {missing}"
        # 规范值本身必须在自己的别名集里
        assert canon in actual


def test_normalize_case_and_whitespace_insensitive():
    """大小写 / 首尾空白不敏感 (CJK 不受 lower 影响)。"""
    assert normalize_status("  Completed ") == "completed"
    assert normalize_status("COMPLETED") == "completed"
    assert normalize_status(" 已完成 ") == "completed"
    assert normalize_status("Failed\n") == "failed"
    assert normalize_status("已阻塞 ") == "blocked"
    assert normalize_status("In-Progress") == "working"
    assert normalize_status(" Waiting_Sub ") == "waiting_sub"


def test_normalize_unrecognized_returns_none():
    """未识别 → None, 不新增猜测 (与现状语义一致)。"""
    assert normalize_status(None) is None
    assert normalize_status("") is None
    assert normalize_status("   ") is None
    assert normalize_status("whatever") is None
    assert normalize_status("已完成x") is None
    assert normalize_status("排队中") is None


def test_set_membership_semantics():
    """IN_PROGRESS_SET / TERMINAL_STATUSES 语义钉: working+waiting_sub 在干,
    completed+failed 终态, blocked 不在终态 (求助态另走 ATTENTION)。"""
    assert IN_PROGRESS_SET == frozenset({STATUS_WORKING, STATUS_WAITING_SUB})
    assert TERMINAL_STATUSES == frozenset({STATUS_COMPLETED, STATUS_FAILED})
    assert STATUS_BLOCKED not in TERMINAL_STATUSES
    assert STATUS_WAITING_SUB not in TERMINAL_STATUSES


# ──────────────────────────────────────────────────────────────────────
# 2. 结算路径正证 (AC-124-1) — 三条路径 × 三个中文终态
# ──────────────────────────────────────────────────────────────────────

def _setup_ws_ledger(tmp_path, task_id="t124"):
    """workspace + ledger + contract + session (复用 issue65 harness 风格)。"""
    from mw.ledger import Ledger
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    ledger = Ledger(str(tmp_path / "ledger.db"))
    ledger.put_contract(task_id, {"required_files": []})
    ledger.upsert_session(task_id, f"sess-{task_id}", "coder", "api",
                         None, workspace=str(ws))
    return task_id, ws, ledger


def _make_todo(ws: Path, status: str):
    task = TodoTask(step_id="s", title="T", goal="g", assignee="coder",
                   product_workspace=str(ws), status=status)
    init_todo(ws / "TODO.md", "T", task)
    return ws / "TODO.md"


@pytest.mark.parametrize("zh_status,expect_verdict", [
    ("已完成", "COMPLETE"),
    ("已失败", "FAILED"),
    ("已阻塞", "NEED_CLARIFICATION"),
])
def test_main_reap_chinese_terminal_settles(tmp_path, zh_status, expect_verdict):
    """主 reap: 中文终态写法 → 正确 verdict (非 timeout 收尸)。"""
    from mw.reaper import _reap_one_task
    task_id, ws, ledger = _setup_ws_ledger(tmp_path)
    _make_todo(ws, zh_status)
    row = ledger.conn.execute(
        "SELECT * FROM harvest_contracts WHERE task_id=?", (task_id,)).fetchone()
    results = []
    with patch("mw.harvest.export_transcript", return_value=None), \
         patch("mw.harvest.fetch_transcript_api", return_value=None):
        _reap_one_task(dict(row), ledger, "/tmp", str(tmp_path), 30, None,
                       results)
    assert len(results) == 1, results
    assert results[0]["verdict"] == expect_verdict, results[0]
    ledger.close()


def test_late_settlement_chinese_completed(tmp_path):
    """补结算 (M3): WAITING 卡 + TODO 已完成 → late_settlement COMPLETE。"""
    from mw.reaper import _reap_late_completions
    task_id, ws, ledger = _setup_ws_ledger(tmp_path, "t124late")
    ledger.update_worker_state(task_id, "WAITING", actor="reaper")
    _make_todo(ws, "已完成")
    results = []
    with patch("mw.harvest.export_transcript", return_value=None), \
         patch("mw.harvest.fetch_transcript_api", return_value=None):
        _reap_late_completions(ledger, str(tmp_path), str(tmp_path), None,
                              results, None)
    assert results and results[0]["trigger"] == "late_settlement", results
    assert results[0]["verdict"] == "COMPLETE", results[0]
    assert ledger.get_contract(task_id)["harvest_status"] == "HARVESTED"
    ledger.close()


def test_console_reassess_chinese_terminal(tmp_path):
    """console reassess: TODO 已完成 → settled=True verdict=COMPLETE。"""
    from mw.ledger import Ledger
    import mw.console as console
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_todo(ws, "已完成")
    led = Ledger(str(tmp_path / "led.db"))
    led.put_contract("t124rs", {"required_files": []})
    led.upsert_session("t124rs", "sess-rs", "coder", "api", None,
                      workspace=str(ws))
    led.record_initiator("t124rs", "s_main")
    console.LEDGER_DB = led.path
    try:
        with patch("mw.harvest.export_transcript", return_value=None), \
             patch("mw.harvest.fetch_transcript_api", return_value=None):
            r = console.api_task_state_correct("t124rs", {
                "action": "reassess", "reason": "issue124 正证",
                "actor_session": "s_main"})
        assert r["ok"] is True and r.get("settled") is True, r
        assert r.get("verdict") == "COMPLETE", r
    finally:
        led.close()


# ──────────────────────────────────────────────────────────────────────
# 3. 变异反证 (AC-124-3) — 删表项 → 守护必须转红
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("removed_alias,canon", [
    ("已完成", STATUS_COMPLETED),
    ("已失败", STATUS_FAILED),
    ("已阻塞", STATUS_BLOCKED),
])
def test_mutation_removed_alias_breaks_normalize(monkeypatch, removed_alias,
                                               canon):
    """删反查索引一项 → normalize_status(该项) 变 None (表驱动证明)。"""
    mutated = {k: v for k, v in todo._STATUS_ALIAS_INDEX.items()
               if k != removed_alias}
    monkeypatch.setattr(todo, "_STATUS_ALIAS_INDEX", mutated)
    assert normalize_status(removed_alias) is None
    # 其余项不受影响 (只删了目标项)
    assert normalize_status(canon) == canon


@pytest.mark.parametrize("removed_alias", ["已完成", "已失败", "已阻塞"])
def test_mutation_removed_alias_breaks_reap(tmp_path, monkeypatch,
                                           removed_alias):
    """删表项后主 reap 不再把该中文终态当终态 → 守护转红 (非恒绿)。"""
    from mw.reaper import _reap_one_task
    mutated = {k: v for k, v in todo._STATUS_ALIAS_INDEX.items()
               if k != removed_alias}
    monkeypatch.setattr(todo, "_STATUS_ALIAS_INDEX", mutated)
    task_id, ws, ledger = _setup_ws_ledger(tmp_path, "t124mut")
    _make_todo(ws, removed_alias)
    row = ledger.conn.execute(
        "SELECT * FROM harvest_contracts WHERE task_id=?", (task_id,)).fetchone()
    results = []
    with patch("mw.harvest.export_transcript", return_value=None), \
         patch("mw.harvest.fetch_transcript_api", return_value=None):
        _reap_one_task(dict(row), ledger, "/tmp", str(tmp_path), 30, None,
                       results)
    triggers = [r["trigger"] for r in results]
    # 变异后不得再出现终态触发器 (未识别 → no_marker / todo_pending 兜底)
    assert "todo_completed" not in triggers, results
    assert "todo_failed" not in triggers, results
    assert "todo_blocked" not in triggers, results
    ledger.close()


# ──────────────────────────────────────────────────────────────────────
# 4. 全仓扫描守护 (AC-124-2)
# ──────────────────────────────────────────────────────────────────────

def test_no_bypass_literal_sets_in_reaper_and_console():
    """reaper/console 源码不再出现绕过入口的终态字面量 (含注释)。"""
    for rel in ("mw/reaper.py", "mw/console.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        hits = re.findall(r'"(complete|fail|block)"', src)
        assert not hits, f"{rel} 残留绕过 normalize_status 的字面量: {hits}"


def test_normalize_status_call_sites_present():
    """normalize_status 在两文件出现次数 ≥ 收敛调用点数 (防漏改回裸 lower)。"""
    reaper_src = (ROOT / "mw/reaper.py").read_text(encoding="utf-8")
    console_src = (ROOT / "mw/console.py").read_text(encoding="utf-8")
    # reaper: import 1 + 收敛调用点 5 (_reap_one_task / late_settlement /
    # stuck 换尺 / SSE 挂死检测)
    assert reaper_src.count("normalize_status") >= 6, \
        f"reaper normalize_status 出现 {reaper_src.count('normalize_status')} 次"
    # console: 展示映射 1 + reassess import 1 + reassess 调用 1
    assert console_src.count("normalize_status") >= 3, \
        f"console normalize_status 出现 {console_src.count('normalize_status')} 次"
    # 禁止残留旧的裸 lower 状态归一化模式
    assert '"inprogress"' not in reaper_src
    assert "'inprogress'" not in reaper_src


# ──────────────────────────────────────────────────────────────────────
# 5. 展示映射 (AC-124-4)
# ──────────────────────────────────────────────────────────────────────

def test_todo_status_sem_canonical_keys():
    """_TODO_STATUS_SEM 键 = 归一化规范值; 中文变异经归一化命中正确徽章。"""
    from mw.console import _TODO_STATUS_SEM
    assert set(_TODO_STATUS_SEM) == {
        "working", "blocked", "completed", "failed", "waiting_sub"}
    # 中文变异 "已完成" 归一化后命中绿色徽章 (非灰色排队中);
    # 归一化失败 → KeyError, 守护同样转红
    sem = _TODO_STATUS_SEM[normalize_status("已完成") or ""]
    assert sem[2] == "badge-green", sem
    sem_f = _TODO_STATUS_SEM[normalize_status("已失败") or ""]
    assert sem_f[2] == "badge-red", sem_f
    sem_b = _TODO_STATUS_SEM[normalize_status("已阻塞") or ""]
    assert sem_b[2] == "badge-yellow", sem_b
    sem_w = _TODO_STATUS_SEM[normalize_status("进行中") or ""]
    assert sem_w[2] == "badge-blue", sem_w


def test_todo_status_sem_second_slot_is_a_status_token_not_a_display_text():
    """issue #146: status_sem 第二位是**规范状态 token**, 不是展示文案。

    后端只下发机器值; 文案由前端按运行时语言渲染 (PLAN-H §6.2/§6.3)。
    """
    from mw.console import _TODO_STATUS_SEM
    canonical = {"working", "blocked", "completed", "failed", "waiting_sub"}
    assert set(_TODO_STATUS_SEM) == canonical
    for key, sem in _TODO_STATUS_SEM.items():
        assert len(sem) == 3, f"{key} 应仍是三元组 (family, token, badgeCls): {sem}"
        assert sem[1] == key, (
            f"{key} 的第二位应是规范 token 本身, 实得 {sem[1]!r}")
        # 旧形态是中文展示文案 —— 必须已被 token 取代
        assert not re.search(r"[一-鿿]", sem[1]), (
            f"{key} 的第二位仍含中文展示文案: {sem[1]!r}")
        assert isinstance(sem[2], str) and sem[2].startswith("badge-"), sem
