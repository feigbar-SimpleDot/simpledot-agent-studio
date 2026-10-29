"""issue #140 守护: current_card_contract 结算面红线的测试牙齿。

背景 (G 批 OBS-1 / 变异反证 MV-132-D): 把 'WAITING' 塞进
`Ledger.current_card_contract` 的结算 IN 子句后, 既有 W1-W9 +
test_team_work_reaper 全绿 (60 用例) ⇒「WAITING 卡不可被结算」这条红线
**只靠审查纪律守护, 没有机器拦**。

本文件用**真实 Ledger 实例** (非桩) 逐 harvest_status 实测, 把 IN 集合
= {PENDING, ERROR, BLOCKED} 钉死 (口径 = docs/PLAN-I-20261029.md §2.2):
  * 正向对照先立 (防「查不到任何行」导致测试恒绿);
  * 状态一律经 update_worker_state 的**合法迁移 + actor 白名单**设置,
    顺带证明该状态在真实状态机里可达;
  * 跨 run 隔离 + 空参守卫, 排除「碰巧查不到」。

★ 变异反证 MV-140-A: 把 mw/ledger.py 的 IN 子句塞回 'WAITING' ⇒
  test_waiting_card_is_not_settleable 必红; 复原后转绿。
  ⚠ 该变异**只动被测代码、不动本文件** —— 这是「断言真会红」的唯一判据。

⚠ 命名易混: 账本侧 harvest_status 的 `WAITING` (大写, reaper 判闲置) 与
  TODO.md 侧的 `waiting_sub` (小写, worker 等子任务) 是两回事, 本文件只涉前者。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mw.ledger import Ledger  # noqa: E402

RUN_ID = "r140"
TASK_ID = "t140"

# harvest_status → 从 PENDING 直达该态的合法 actor (取自 _ACTOR_POLICY)。
# 走真实状态机而非裸 UPDATE: 测试同时验证「该态可达」(PLAN-I §2.2 断言 7)。
REACH_FROM_PENDING = {
    "PENDING": None,                      # put_contract 落库即 PENDING
    "ERROR": "harvest",
    "BLOCKED": "harvest",
    "WAITING": "reaper",
    "NEED_CLARIFICATION": "harvest",
    "HARVESTED": "harvest",
    "CANCELLED": "operator",
    "ARCHIVED": "operator",
}
# 结算面 IN 集合的期望: 命中三态返回行, 其余五态返回 None
SETTLEABLE = ("PENDING", "ERROR", "BLOCKED")
NOT_SETTLEABLE = ("WAITING", "NEED_CLARIFICATION", "HARVESTED",
                  "CANCELLED", "ARCHIVED")


def _make_ledger(tmp_path: Path) -> Ledger:
    """建 run + 契约 + 登记到 run 名下 (reaper 结算入口的前置三件套)。"""
    led = Ledger(str(tmp_path / "ledger.db"))
    led.create_team_work_run(RUN_ID, "sid_main_140", "需求")
    led.put_contract(TASK_ID, {"required_files": [], "timeout_seconds": 60})
    led.register_team_work_card(RUN_ID, TASK_ID)
    led.set_team_work_current_card(RUN_ID, TASK_ID)
    return led


def _status_of(led: Ledger) -> str:
    """直读 harvest_status 列 (只读断言用, 不引入生产 API)。"""
    row = led.conn.execute(
        "SELECT harvest_status FROM harvest_contracts WHERE task_id=?",
        (TASK_ID,)).fetchone()
    return row["harvest_status"] if row else ""


def _to_status(led: Ledger, status: str) -> None:
    """经合法迁移把卡推到目标 harvest_status (非法迁移会 raise, 测试即红)。"""
    actor = REACH_FROM_PENDING[status]
    if actor is None:
        assert _status_of(led) == "PENDING"
        return
    # 前置声明: 目标态必须在状态机里存在, 且 PENDING→目标 是条合法边
    assert status in Ledger._STATE_TRANSITIONS, f"未知状态 {status}"
    assert status in Ledger._STATE_TRANSITIONS["PENDING"], \
        f"PENDING → {status} 不是合法迁移边"
    led.update_worker_state(TASK_ID, status, actor=actor, reason="issue140 测试")
    assert _status_of(led) == status


# ---------------------------------------------------------------- 断言 1
def test_pending_card_is_settleable(tmp_path):
    """正向对照 (防恒绿): PENDING ⇒ 返回行, 且行的 task_id / run_id 正确。

    没有这条, 后面所有「返回 None」的断言都可能只是「根本没查到任何行」。
    """
    led = _make_ledger(tmp_path)
    row = led.current_card_contract(RUN_ID, TASK_ID)
    assert row is not None, "PENDING 卡必须落在结算面上"
    assert row["task_id"] == TASK_ID
    assert row["team_work_run_id"] == RUN_ID
    assert row["harvest_status"] == "PENDING"


# ---------------------------------------------------------------- 断言 2 (红线主体)
def test_waiting_card_is_not_settleable(tmp_path):
    """红线主体: harvest_status='WAITING' ⇒ current_card_contract 返回 None。

    MV-140-A 的靶心 —— 把 'WAITING' 塞进 IN 子句, 本断言必红。
    """
    led = _make_ledger(tmp_path)
    _to_status(led, "WAITING")
    assert led.current_card_contract(RUN_ID, TASK_ID) is None


# ---------------------------------------------------------------- 断言 3
@pytest.mark.parametrize("status", SETTLEABLE)
def test_settleable_statuses_return_row(tmp_path, status):
    """IN 集合的正面: PENDING / ERROR / BLOCKED 三态可被结算。"""
    led = _make_ledger(tmp_path)
    _to_status(led, status)
    row = led.current_card_contract(RUN_ID, TASK_ID)
    assert row is not None, f"{status} 应在结算面内"
    assert row["harvest_status"] == status


# ---------------------------------------------------------------- 断言 4
@pytest.mark.parametrize("status", NOT_SETTLEABLE)
def test_non_settleable_statuses_return_none(tmp_path, status):
    """IN 集合的反面: 其余五态 (含 WAITING) 一律不可被结算。"""
    led = _make_ledger(tmp_path)
    _to_status(led, status)
    assert led.current_card_contract(RUN_ID, TASK_ID) is None, \
        f"{status} 不该出现在结算 IN 子句里"


def test_in_clause_is_exactly_the_expected_set(tmp_path):
    """把 IN 集合整体钉死: 8 个可达态里**恰好**三态返回行。

    逐态断言的汇总面 —— 若有人往 IN 里多塞一个态, 本用例给出精确差集。
    """
    led = _make_ledger(tmp_path)
    actual_settleable = []
    for status in (*SETTLEABLE, *NOT_SETTLEABLE):
        # 每态一个独立库, 避免终态后无法回退
        one = _make_ledger(Path(led.path).parent / f"db-{status}.db")
        _to_status(one, status)
        if one.current_card_contract(RUN_ID, TASK_ID) is not None:
            actual_settleable.append(status)
    assert sorted(actual_settleable) == sorted(SETTLEABLE), \
        f"结算面集合漂移: 实际={actual_settleable} 期望={list(SETTLEABLE)}"


# ---------------------------------------------------------------- 断言 5
def test_cross_run_isolation(tmp_path):
    """跨 run 隔离: 同 task_id 但 run_id 不匹配 ⇒ None (排除「碰巧查不到」)。"""
    led = _make_ledger(tmp_path)
    assert led.current_card_contract(RUN_ID, TASK_ID) is not None
    assert led.current_card_contract("r_other_140", TASK_ID) is None


# ---------------------------------------------------------------- 断言 6
def test_empty_args_guard_returns_none(tmp_path):
    """空参守卫 (既有 L1924 语义, 顺带钉住): 任一参数为空 ⇒ None。"""
    led = _make_ledger(tmp_path)
    assert led.current_card_contract("", TASK_ID) is None
    assert led.current_card_contract(RUN_ID, "") is None
    assert led.current_card_contract("", "") is None


# ---------------------------------------------------------------- 断言 7
def test_waiting_state_is_reachable_via_real_state_machine(tmp_path):
    """WAITING 在真实状态机里可达且 actor 白名单生效 (排除「测了个不存在的态」)。

    PENDING→WAITING 只允许 actor=reaper; 换 actor 必须被 PermissionError 拦下,
    否则本文件的 WAITING 断言可能建立在非法构造的数据上。
    """
    led = _make_ledger(tmp_path)
    with pytest.raises(PermissionError):
        led.update_worker_state(TASK_ID, "WAITING", actor="harvest",
                                reason="不该被允许")
    assert led.current_card_contract(RUN_ID, TASK_ID) is not None  # 仍 PENDING
    _to_status(led, "WAITING")
    assert led.current_card_contract(RUN_ID, TASK_ID) is None
