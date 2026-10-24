"""Dry-run 测试: workflow 步骤跳过 (路径 A 最小实现)。

验证:
  1. steps[].include=false 的项被裁掉, ledger 完全不建对应卡
  2. card_count 只算 include=true 的
  3. 跳过信息进 skipped_steps 返回字段 (审计)
  4. 全跳过 → 400 拒
  6. 旧 payload (不带 include) 仍正常 (默认 include=true, 向后兼容)

参考: dryrun/workflow-step-skip (2026-10-06)
"""
import sys, os
import json
import tempfile
import sqlite3
from pathlib import Path

# 把 mw 包目录加进 path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mw.console as console
from mw.ledger import Ledger


def _make_fake_registry(monkeypatch, names=("coder", "tester", "system_engineer")):
    """塞一份假 registry, 让 api_team_work_launch 的 assignee 校验过。"""
    fake = {n: {"name": n, "scope": {"excludes": []}, "api_url": "http://x",
                "api_key": "k", "model": "m", "hermes_home": "/h"} for n in names}
    monkeypatch.setattr(console, "api_workers", lambda: {"workers": fake})


def _make_fake_dispatch(monkeypatch, captured):
    """替换 api_dispatch: 记录收到的 payload, 返回 ok。"""
    def _f(payload):
        captured["payload"] = payload
        return {"ok": True, "accepted": True, "task_id": payload["task_id"],
                "session_id": "s-fake", "workspace": "/tmp/ws"}
    monkeypatch.setattr(console, "api_dispatch", _f)


def _launch_with(tmp_path, monkeypatch, steps, initial="test"):
    _make_fake_registry(monkeypatch)
    captured = {}
    _make_fake_dispatch(monkeypatch, captured)
    # 用临时 db
    db = str(tmp_path / "ledger.db")
    monkeypatch.setattr(console, "LEDGER_DB", db)
    payload = {"initial_task": initial, "session_id": "sess-x",
               "steps": steps}
    r = console.api_team_work_launch(payload)
    return r, captured, db


def _step(step_id, assignee="coder", title=None, task_id=None, include=None,
          skip_reason=None):
    s = {"step_id": step_id, "title": title or step_id,
       "task_id": task_id or f"t_{step_id}",
       "assignee": assignee}
    if include is not None:
        s["include"] = include
    if skip_reason is not None:
        s["skip_reason"] = skip_reason
    return s


def test_skip_single_step(tmp_path, monkeypatch):
    """5 步跳过中间 1 步 → launch 应该只看到 4 张卡。"""
    steps = [
        _step("req", "coder"),
        _step("design", "system_engineer"),
        _step("ui", "system_engineer", include=False, skip_reason="纯后端,无需 UI"),
        _step("impl", "coder"),
        _step("test", "tester"),
    ]
    r, captured, db = _launch_with(tmp_path, monkeypatch, steps)

    assert r["ok"] is True, r
    # cards 只含 4 项
    assert len(r["cards"]) == 4
    assert [c["step_id"] for c in r["cards"]] == ["req", "design", "impl", "test"]
    # card_count 是 4
    assert r["card_count"] == 4
    # skipped_steps 字段,审计信息齐
    assert len(r["skipped_steps"]) == 1
    assert r["skipped_steps"][0]["step_id"] == "ui"
    assert r["skipped_steps"][0]["skip_reason"] == "纯后端,无需 UI"
    # 第一张被 dispatch 的是 req
    assert r["dispatched"] == "t_req"
    assert captured["payload"]["task_id"] == "t_req"
    # ledger 没建 ui 那张卡的 contract
    led = Ledger(db)
    rows = led.conn.execute(
        "SELECT task_id FROM harvest_contracts").fetchall()
    task_ids = {r[0] for r in rows}
    assert "t_ui" not in task_ids, f"被跳过的卡竟然入了 ledger: {task_ids}"
    assert "t_req" in task_ids and "t_design" in task_ids
    # team_work_runs.card_count = 4
    run = led.get_team_work(r["run_id"])
    assert run["card_count"] == 4


def test_skip_multiple_steps(tmp_path, monkeypatch):
    """3 步跳过 2 步 → 只剩 1 张卡。"""
    steps = [
        _step("a", "coder"),
        _step("b", "system_engineer", include=False),
        _step("c", "tester", include=False, skip_reason="产品直接验收"),
    ]
    r, _, db = _launch_with(tmp_path, monkeypatch, steps)
    assert r["ok"]
    assert r["card_count"] == 1
    assert [c["step_id"] for c in r["cards"]] == ["a"]
    assert len(r["skipped_steps"]) == 2
    reasons = {x["step_id"]: x["skip_reason"] for x in r["skipped_steps"]}
    assert reasons["b"] == "no reason given"
    assert reasons["c"] == "产品直接验收"


def test_all_skipped_rejected(tmp_path, monkeypatch):
    """全跳 → 400 拒, 不能创建空 run。"""
    steps = [
        _step("x", "coder", include=False),
        _step("y", "tester", include=False),
    ]
    r, _, _ = _launch_with(tmp_path, monkeypatch, steps)
    assert r["code"] == 400
    assert "全被跳过" in r["error"]
    assert len(r["skipped"]) == 2


def test_no_include_field_is_compatible(tmp_path, monkeypatch):
    """不带 include 字段的旧 payload 必须正常工作 (默认 include=true)。"""
    steps = [
        _step("a", "coder"),
        _step("b", "tester"),
    ]
    r, _, _ = _launch_with(tmp_path, monkeypatch, steps)
    assert r["ok"]
    assert r["card_count"] == 2
    assert r["skipped_steps"] == []


def test_skip_does_not_break_first_dispatch(tmp_path, monkeypatch):
    """跳过前面的步, 第一张被 dispatch 的应该是 include=true 的第一张。"""
    steps = [
        _step("design", "system_engineer", include=False),
        _step("req", "coder"),
        _step("test", "tester"),
    ]
    r, captured, _ = _launch_with(tmp_path, monkeypatch, steps)
    assert r["dispatched"] == "t_req"
    assert captured["payload"]["task_id"] == "t_req"
    assert captured["payload"]["idempotency_key"] == f"tw-{r['run_id']}-req"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


# ── 路径 2 测试 (TODO.md 协议层跳过, 留痕) ──────────────────────

def test_worker_prompt_includes_skip_protocol():
    """worker_contract_prompt 必须告诉 worker 怎么识别 - 跳过: 是 字段。"""
    from mw.pipeline import worker_contract_prompt
    p = worker_contract_prompt("/tmp/TODO.md", "/tmp/ws")
    assert "- 跳过: 是" in p, "prompt 没识别跳过协议"
    assert "跳过快速通道" in p or "跳过" in p


def test_skip_prompt_examples_have_correct_sed():
    """跳过模式给的 sed 必须保留 `- ` 前缀 (否则 reaper 读不到状态)。"""
    from mw.pipeline import worker_contract_prompt
    p = worker_contract_prompt("/tmp/TODO.md", "/tmp/ws")
    assert "sed -i 's/^- 状态:" in p


def test_workflow_yaml_declares_optional():
    """workflows.yaml 必须给 dev-workflow 各步骤标 optional 字段。"""
    import yaml
    with open(ROOT / "mw" / "workflows.yaml") as f:
        wf = yaml.safe_load(f)
    dev = wf["workflows"]["dev-workflow"]["steps"]
    optional_flags = {s["id"]: s.get("optional", False) for s in dev}
    # 校验关键步骤的 optional 取值
    assert optional_flags["req-analysis"] is False   # 需求永远不能跳
    assert optional_flags["implementation"] is False # 实现不能跳
    assert optional_flags["code-review"] is False    # 审查不能跳
    assert optional_flags["design"] is True          # 设计可跳 (纯 hotfix)
    assert optional_flags["testing"] is True         # 测试可跳 (typo)
    # skip_when 必须是 list[str]
    design = next(s for s in dev if s["id"] == "design")
    assert isinstance(design.get("skip_when"), list)