"""tests/test_schedule_api.py — 排期 HTTP 层测试 (issue #164, ARCH-164 §8#12)。

api_schedule_* 函数直接调用 (返回 (code, body) 元组, 不起 HTTP server)。
覆盖: 各端点 happy / 400 / 404 / 409; id 白名单; 错误体 {error, code} 形状。

跑法: pytest tests/test_schedule_api.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mw import schedule as S  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """每个用例独立 schedule.db + 空 ledger, 重置模块级单例。"""
    monkeypatch.setenv("MW_SCHEDULE_DB", str(tmp_path / "sched.db"))
    monkeypatch.setenv("MW_LEDGER_DB", str(tmp_path / "none.db"))
    monkeypatch.setattr(S, "_engine", None)
    monkeypatch.setattr(S, "_engine_path", None)
    # 单例引擎的探活 stub 掉 (API 测试不碰网络)
    eng = S.get_engine()
    eng._gateway_free = lambda: (True, None)
    eng._spawn_fire = lambda task: None
    yield


def _create(detail="任务详情", **kw):
    code, body = S.api_schedule_create({"detail": detail, **kw})
    assert code == 201, body
    return body["task"]


# ── 创建 ─────────────────────────────────────────────────────────────
def test_create_happy():
    t = _create("第一行\n第二行", priority="high", workflow="wf1", workspace="/ws")
    assert t["title"] == "第一行" and t["status"] == "pending"
    assert t["priority"] == "high" and t["workflow"] == "wf1"
    code, body = S.api_schedule_tasks()
    assert code == 200
    assert [x["id"] for x in body["tasks"]] == [t["id"]]
    assert body["next_task_id"] == t["id"]


def test_create_detail_required():
    code, body = S.api_schedule_create({"detail": "   "})
    assert code == 400 and body["code"] == 400 and body["error"]
    code, body = S.api_schedule_create({})
    assert code == 400


def test_create_bad_priority():
    code, body = S.api_schedule_create({"detail": "x", "priority": "asap"})
    assert code == 400


# ── 动作端点: 404 / 409 ──────────────────────────────────────────────
def test_action_404_unknown_id():
    code, body = S.api_schedule_action("does-not-exist", "pause")
    assert code == 404 and body["code"] == 404


def test_action_id_whitelist():
    # id 白名单: 非法字符 (路径注入面) 直接 404, 不落库查询
    for bad in ("../x", "a b", "", "x" * 65, "id;drop"):
        code, _ = S.api_schedule_action(bad, "pause")
        assert code == 404, bad


def test_pause_resume_cancel_flow():
    t = _create()
    code, body = S.api_schedule_action(t["id"], "pause")
    assert code == 200 and body["task"]["status"] == "paused"
    # 重复 pause ⇒ 非法迁移 409
    code, body = S.api_schedule_action(t["id"], "pause")
    assert code == 409 and body["code"] == 409
    code, body = S.api_schedule_action(t["id"], "resume")
    assert code == 200 and body["task"]["status"] == "pending"
    code, _ = S.api_schedule_action(t["id"], "cancel")
    assert code == 200


def test_delete_only_cancelled():
    t = _create()
    code, body = S.api_schedule_action(t["id"], "delete")
    assert code == 409                                   # pending 不可删
    S.api_schedule_action(t["id"], "cancel")
    code, body = S.api_schedule_action(t["id"], "delete")
    assert code == 200 and body.get("deleted") == t["id"]
    code, _ = S.api_schedule_action(t["id"], "delete")
    assert code == 404                                   # 已删 ⇒ 不存在


# ── update: 仅 paused ────────────────────────────────────────────────
def test_update_only_paused():
    t = _create("原详情")
    code, _ = S.api_schedule_update(t["id"], {"detail": "x"})
    assert code == 409                                   # pending 不可改
    S.api_schedule_action(t["id"], "pause")
    code, body = S.api_schedule_update(
        t["id"], {"detail": "新详情", "priority": "low"})
    assert code == 200
    assert body["task"]["status"] == "paused"           # A6 保持暂停
    assert body["task"]["detail"] == "新详情"
    # 空 detail ⇒ 400
    code, _ = S.api_schedule_update(t["id"], {"detail": "  "})
    assert code == 400
    code, _ = S.api_schedule_update("no-such-id", {"detail": "x"})
    assert code == 404


# ── settings ─────────────────────────────────────────────────────────
def test_settings_toggle():
    code, body = S.api_schedule_settings({"auto_execute": False})
    assert code == 200 and body["auto_execute"] is False
    code, body = S.api_schedule_state()
    assert body["auto_execute"] is False
    code, body = S.api_schedule_settings({"auto_execute": True})
    assert body["auto_execute"] is True


# ── F2: 开关 OFF→ON 重开完整 5 分钟 (REVIEW-164 P1-2, AC-4.3) ──────────
def test_settings_off_on_restarts_full_countdown(monkeypatch):
    """关→开后倒计时**重新开始完整 5 分钟** (不续用关闭前剩余) (AC-4.3)。

    旧行为: api_schedule_settings 只调 evaluate_now() (唤醒 ticker), 不清
    countdown_ends_at ⇒ 若 ticker 未观察到中间的"关"态 (同一 ≤1s 间隔内
    关→开), 旧倒计时原样保留并走完剩余时间 (AC-4.3 违反)。"""
    class FakeClock:
        def __init__(self):
            self.now = 1000.0

        def __call__(self):
            return self.now

    clock = FakeClock()
    eng = S.get_engine()
    monkeypatch.setattr(eng, "clock", clock)
    _create()
    eng._tick()                                    # 起表 ends_at = 1300.0
    assert eng.countdown_ends_at == 1300.0
    clock.now += 200                              # 已消耗 200s, 剩 100s
    assert eng.snapshot()["countdown"]["remaining_s"] == 100

    # 关→开 (期间 ticker 未跑 ⇒ 从未观察到"关"态)
    code, _ = S.api_schedule_settings({"auto_execute": False})
    assert code == 200
    code, _ = S.api_schedule_settings({"auto_execute": True})
    assert code == 200
    # 旧倒计时必须已作废 (重开的起点是 0)
    assert eng.countdown_ends_at is None

    eng._tick()                                   # 下一轮 ticker ⇒ 重新起表
    assert eng.countdown_ends_at == clock.now + 300
    assert eng.snapshot()["countdown"]["remaining_s"] == 300


# ── state / run-now ──────────────────────────────────────────────────
def test_state_shape():
    _create()
    code, body = S.api_schedule_state()
    assert code == 200
    for k in ("auto_execute", "countdown", "busy_reason", "running_task_id"):
        assert k in body


def test_run_now_requires_countdown():
    _create()
    # 未起表 (ticker 没跑) ⇒ 409 countdown_inactive (A11 双保险)
    code, body = S.api_schedule_run_now()
    assert code == 409 and body["code"] == 409 and body["error"]


def test_run_now_with_countdown_fires():
    eng = S.get_engine()
    fired = []
    eng._spawn_fire = lambda task: fired.append(task)
    t = _create()
    eng._tick()                                   # 起表
    assert eng.countdown_ends_at is not None
    code, body = S.api_schedule_run_now()
    assert code == 200 and body["ok"] is True
    assert [x["id"] for x in fired] == [t["id"]]


# ── dispatch 消息等价性 (R1 关键断言) ────────────────────────────────
def test_dispatch_message_equivalence(monkeypatch):
    """首条消息与旧 submitTeamWork firstMsg 逐字段等价 (ARCH §2.4):
    结构行序 + marker 字面 + 两分支拼装。"""
    monkeypatch.setenv("MW_LANG", "zh-cn")
    import mw.i18n
    mw.i18n._cache.clear()
    mw.i18n._LOADED.clear()
    task = {"detail": "做个功能", "workflow": "auto", "workspace": ""}
    msg = S.build_dispatch_message(task, "sid-abc")
    lines = msg.split("\n")
    assert lines[0] == "【Team Work 多智能体任务】"          # marker 双语同字面
    assert lines[1] == ""
    assert lines[2] == "需求: 做个功能"
    assert lines[3] == ("参考 workflow: 请你自行从 GET /api/workflows 的清单中"
                        "选择最合适的一个")
    assert lines[4].startswith("工作目录: 未指定")
    assert lines[5] == ""
    assert "mw-task-dispatch" in lines[6]
    assert "sid-abc" in msg
    # workspace 设定分支
    msg2 = S.build_dispatch_message(
        {"detail": "x", "workflow": "wf", "workspace": "/ws"}, "s2")
    assert "参考 workflow: wf" in msg2
    assert "工作目录: /ws" in msg2
    assert 'project_workspace' in msg2
