"""Issue #228 守护 (BB轮 s2, 2026-11-05) — /api/workers api_key 明文回显根治。

依据: docs/issue-convergence-BB/ADJUDICATION.md §2 + docs/dispatch/BB-DOWNSTREAM.md s2。
覆盖:
  T1  GET 出口剥离: api_workers() 每条目无 api_key 字段, 响应全文
      (json dump) 不含密钥明文; api_url/online 等其余字段不回归。
  T2  保存回环保守 (最大风险点): PUT 条目缺失 api_key → registry 旧值保留。
  T3  PUT 条目 api_key 为空串/纯空白 → 旧值保留 (视同「未改」)。
  T4  PUT 显式新值 → 正常覆盖。
  T5  新 worker 无旧值 + 空 api_key → 不凭空造 (保持缺失)。
  T6  全回环模拟: GET 剥离后的条目原样 PUT 回 (编辑弹窗路径),
      registry 密钥不被洗掉 (防断 relay 认证)。
  T7  回归红线: api_worker_detail 的 key 打码逻辑不受影响 (仍 '***')。

跑法: 仓库根 .venv 下 `pytest tests/test_issue228_api_key_strip.py -v`
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mw.console as console

SECRET = "sk-" + "a" * 61  # 64 位形态, 与生产实测长度一致
SECRET2 = "sk-" + "b" * 61


def setup_tmp(tmp_path, monkeypatch):
    """registry/ledger/kanban 全部指到 tmp, 关闭 2s /health 探测。"""
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps({
        "coder": {"runner": "api", "api_url": "http://stub-coder",
                  "api_key": SECRET},
        "reviewer": {"runner": "api", "api_url": "http://stub-rev",
                     "api_key": SECRET2, "description": "审代码"},
    }))
    monkeypatch.setattr(console, "REGISTRY", str(reg))
    monkeypatch.setattr(console, "LEDGER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(console, "KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setattr(console, "_health_probe", lambda entry: True)
    return reg


def read_reg(reg: Path) -> dict:
    return json.loads(reg.read_text())


# ── T1: GET 出口剥离 ────────────────────────────────────────────────
def test_t1_api_workers_strips_api_key(tmp_path, monkeypatch):
    reg = setup_tmp(tmp_path, monkeypatch)
    d = console.api_workers()
    dumped = json.dumps(d, ensure_ascii=False)
    assert SECRET not in dumped, "GET 响应仍含 api_key 明文 (#228 回归)"
    assert SECRET2 not in dumped, "GET 响应仍含 api_key 明文 (#228 回归)"
    for name, entry in d["workers"].items():
        assert "api_key" not in entry, f"{name} 条目仍带 api_key 字段"
        # 其余字段不回归 (avatar 剥离纪律之外的字段原样保留)
        assert entry["api_url"] == read_reg(reg)[name]["api_url"]
        assert entry["online"] is True
    assert d["workers"]["reviewer"].get("description") == "审代码"


# ── T2: PUT 缺失 api_key → 保留旧值 ────────────────────────────────
def test_t2_put_missing_key_keeps_old(tmp_path, monkeypatch):
    reg = setup_tmp(tmp_path, monkeypatch)
    r = console.api_registry_save({
        "coder": {"runner": "api", "api_url": "http://stub-coder"},
    })
    assert r.get("ok") is True
    assert read_reg(reg)["coder"]["api_key"] == SECRET, "旧密钥被洗掉 (#228 回环风险)"


# ── T3: PUT 空/空白 api_key → 保留旧值 ─────────────────────────────
def test_t3_put_empty_key_keeps_old(tmp_path, monkeypatch):
    reg = setup_tmp(tmp_path, monkeypatch)
    console.api_registry_save({
        "coder": {"runner": "api", "api_url": "http://stub-coder",
                  "api_key": ""},
        "reviewer": {"runner": "api", "api_url": "http://stub-rev",
                     "api_key": "   "},
    })
    regd = read_reg(reg)
    assert regd["coder"]["api_key"] == SECRET
    assert regd["reviewer"]["api_key"] == SECRET2


# ── T4: PUT 显式新值 → 覆盖 ────────────────────────────────────────
def test_t4_put_explicit_new_key_overwrites(tmp_path, monkeypatch):
    reg = setup_tmp(tmp_path, monkeypatch)
    new_key = "sk-" + "c" * 61
    console.api_registry_save({
        "coder": {"runner": "api", "api_url": "http://stub-coder",
                  "api_key": new_key},
    })
    assert read_reg(reg)["coder"]["api_key"] == new_key


# ── T5: 新 worker 空 api_key → 不凭空造 ────────────────────────────
def test_t5_new_worker_empty_key_not_fabricated(tmp_path, monkeypatch):
    reg = setup_tmp(tmp_path, monkeypatch)
    console.api_registry_save({
        "coder": {"runner": "api", "api_url": "http://stub-coder",
                  "api_key": SECRET},
        "newbie": {"runner": "api", "api_url": "http://stub-new",
                   "api_key": ""},
    })
    assert read_reg(reg)["newbie"].get("api_key", "") == ""


# ── T6: 全回环 (GET 剥离 → 原样 PUT 回) 密钥不丢 ───────────────────
def test_t6_full_roundtrip_keeps_keys(tmp_path, monkeypatch):
    reg = setup_tmp(tmp_path, monkeypatch)
    view = console.api_workers()["workers"]
    # 模拟编辑弹窗保存: 前端基于 GET 数据重建全量 map 后 PUT
    payload = {}
    for name, entry in view.items():
        e = {k: v for k, v in entry.items()
             if k in ("runner", "api_url", "api_key", "description",
                      "scope", "avatar")}
        payload[name] = e
    console.api_registry_save(payload)
    regd = read_reg(reg)
    assert regd["coder"]["api_key"] == SECRET
    assert regd["reviewer"]["api_key"] == SECRET2


# ── T7: 回归红线 — worker 详情端点打码不受影响 ─────────────────────
def test_t7_worker_detail_still_masked(tmp_path, monkeypatch):
    reg = setup_tmp(tmp_path, monkeypatch)

    class _NoNet:
        def __init__(self, *a, **k):
            raise OSError("no net in test")

    monkeypatch.setattr("urllib.request.urlopen", _NoNet)
    d = console.api_worker_detail("coder")
    assert d["registry"].get("api_key") == "***", "详情端点打码回归"
    assert SECRET not in json.dumps(d)
