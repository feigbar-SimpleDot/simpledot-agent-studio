"""F-M-4 回归测试 (2026-10-30, 卡 t_2a7870f9) —— 复用条款 vs 新鲜度守卫。

背景 (docs/DOC-AUDIT-M-20261030.md F-M-4):
  issue #170 坑 1 给物理验收加了新鲜度维度 (产物 mtime 早于本卡派发
  基准 ⇒ physical_stale), 而 worker_contract_prompt 的「产物已存在即复用」
  条款教 worker 复用前轮产物 ⇒ 复用场景产物 mtime **必然**早于本卡
  session 创建时刻 ⇒ worker 照条款写 completed ⇒ reaper 必判陈旧 ⇒
  续作轮次 (blocked→resume / fix-up 卡复用前轮产物) 稳定触发假失败,
  worker 与守卫互相甩锅, 卡死等 main。

本卡处置 (main 裁决 = (a)+(b) 组合):
  (a) 复用前必须 touch 产物 (或声明复用来源), 让 mtime 新于本卡基准;
  (b) harvest 侧: worker 在『结果』里显式声明复用来源时, 新鲜度维度
      降级为告警不判死 (physical_check 证据里标注 stale 原因)。

守护形态:
  R1 复用声明提取: zh / en 标记都能识别; 无标记 / 空路径不误判。
  R2 复用声明 ⇒ verify_physical 判 passed=True (告警不判死), 且 stale
     事实仍写进 out_info (main 看得见)。
  R3 无复用声明 ⇒ 仍判 physical_stale (原守卫不被削弱)。
  R4 harvest 端到端: 复用场景 (产物 mtime 早于基准) 不再稳定假失败 ——
     physical_check.passed=True 且 reuse_declared=True / reuse_source 非空。
  R5 产物**缺失**时, 复用声明**不得**放行 (存在性是硬底线)。
  R6 模板条款: 中英两份 worker_contract_prompt 的复用条款必须与新鲜度
     守卫衔接口径一致 (复用来源标记字面在位), 且条款文本零花括号。
  R7 基线 fixture 同步: zh 模板渲染 = byte-equal baseline (改动模板必改
     fixture, 否则派发与基线漂移)。
  R8 变异反证: 去掉守卫里的 reuse 降级分支 ⇒ 守护转红 (自证有牙齿)。

纯函数/本地 ledger 测试, 不依赖 hermes CLI / 网络。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from mw.harvest import (  # noqa: E402
    extract_reuse_declaration, verify_physical,
)
from mw.ledger import Ledger  # noqa: E402
from mw.todo import TodoTask, init_todo, STATUS_COMPLETED  # noqa: E402


@pytest.fixture(autouse=True)
def _no_transcript_export():
    """容器无 hermes CLI: 屏蔽 transcript 导出路径 (与既有测试同法)。"""
    with patch("mw.harvest.export_transcript", return_value=None), \
         patch("mw.harvest.fetch_transcript_api", return_value=None):
        yield


def _mk_product(tmp_path, name="out.md", mtime=None):
    """建产物文件, 可选把 mtime 拨到过去。返回 (product_ws, rel)。"""
    pws = tmp_path / "proj"
    pws.mkdir(exist_ok=True)
    f = pws / name
    f.write_text("content", encoding="utf-8")
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return str(pws), name


# ════════════════════════════════════════════════════════════════
# R1: 复用声明提取 (纯函数)
# ════════════════════════════════════════════════════════════════

def test_r1_zh_reuse_declaration_extracted():
    """R1a: zh 标记「复用来源: <路径>」能取出路径。"""
    src = extract_reuse_declaration(
        "已复用前轮产物。复用来源: docs/FIX-NOTES-M-DOCSYNC.md")
    assert src == "docs/FIX-NOTES-M-DOCSYNC.md"


def test_r1_en_reuse_declaration_extracted():
    """R1b: en 标记「reuse source: <path>」能取出路径 (大小写不敏感)。"""
    src = extract_reuse_declaration(
        "Reused the previous round's product. reuse source: docs/OUT.md")
    assert src == "docs/OUT.md"


def test_r1_bare_reuse_word_is_not_a_declaration():
    """R1c: 裸 "reuse" 一词不算正式声明 (避免误判放行)。"""
    assert extract_reuse_declaration("we may reuse it later") is None
    assert extract_reuse_declaration("可以复用") is None


def test_r1_empty_or_missing_declaration_returns_none():
    """R1d: 空结果 / 空路径 / None ⇒ 无声明。"""
    assert extract_reuse_declaration(None) is None
    assert extract_reuse_declaration("") is None
    assert extract_reuse_declaration("复用来源:") is None
    assert extract_reuse_declaration("复用来源:   ") is None


# ════════════════════════════════════════════════════════════════
# R2 / R3: verify_physical 的复用降级语义
# ════════════════════════════════════════════════════════════════

def test_r2_reuse_declared_downgrades_stale_to_warning(tmp_path):
    """R2: 复用声明 ⇒ 陈旧降级为告警 (passed=True), stale 事实仍留痕。"""
    base = time.time()
    pws, rel = _mk_product(tmp_path, mtime=base - 100)  # 远早于基准
    contract = {"required_files": [rel]}
    info: dict = {}
    ok, why = verify_physical(contract, str(tmp_path), pws,
                              baseline_ts=base,
                              baseline_label="task_sessions.created_at",
                              out_info=info,
                              reuse_declared=True)
    assert ok is True, "复用声明后不得判物理失败 (假失败根因)"
    assert info.get("stale") is True, "陈旧事实必须仍留痕 (main 要看得见)"
    assert "stale_target" in info


def test_r3_without_reuse_declaration_still_fails(tmp_path):
    """R3: 无复用声明 ⇒ 仍判 physical_stale (原守卫不被削弱)。"""
    base = time.time()
    pws, rel = _mk_product(tmp_path, mtime=base - 100)
    contract = {"required_files": [rel]}
    info: dict = {}
    ok, why = verify_physical(contract, str(tmp_path), pws,
                              baseline_ts=base,
                              baseline_label="task_sessions.created_at",
                              out_info=info,
                              reuse_declared=False)
    assert ok is False, "无复用声明时守卫必须照旧生效"
    assert info.get("stale") is True


def test_r5_reuse_declaration_does_not_rescue_missing_file(tmp_path):
    """R5: 产物**缺失**时, 复用声明不得放行 (存在性是硬底线)。"""
    base = time.time()
    pws = str(tmp_path / "proj")
    (Path(pws)).mkdir(exist_ok=True)
    contract = {"required_files": ["missing.md"]}
    info: dict = {}
    ok, why = verify_physical(contract, str(tmp_path), pws,
                              baseline_ts=base, out_info=info,
                              reuse_declared=True)
    assert ok is False, "复用声明只降级新鲜度, 不能豁免存在性"
    assert not info.get("stale")


# ════════════════════════════════════════════════════════════════
# R4: harvest 端到端 (复用场景不再稳定假失败)
# ════════════════════════════════════════════════════════════════

def _setup_reuse_harvest(tmp_path, task_id, card_id, result_text,
                         product_mtime=None):
    """建 ledger + 契约 + session + 项目根 TODO.md (含『结果』复用声明)。"""
    ws = tmp_path / ".tasks" / task_id
    ws.mkdir(parents=True)
    proj = tmp_path                      # 项目根 = tmp_path
    pws = tmp_path / "out"
    pws.mkdir(exist_ok=True)
    prod = pws / "OUT.md"
    prod.write_text("deliverable", encoding="utf-8")
    if product_mtime is not None:
        os.utime(prod, (product_mtime, product_mtime))

    led = Ledger(str(tmp_path / "ledger.db"))
    led.put_contract(task_id, {"required_files": ["out/OUT.md"]})
    led.upsert_session(task_id, f"sess-{task_id}", "coder", "api",
                       None, workspace=str(ws))
    # 本卡 worker 开跑时刻 = now ⇒ 复用产物 mtime 早于它 ⇒ 陈旧
    task = TodoTask(step_id="s", title="T", goal="g", assignee="coder",
                    status=STATUS_COMPLETED, result=result_text,
                    kanban_task_id=card_id,
                    product_workspace=str(proj))
    init_todo(proj / "TODO.md", "T", task)
    # submit_log 提供 project_workspace (get_product_workspace 读它;
    # 列序: idempotency_key / task_id / manifest_json / created_at)
    led.record_submit("idem-1", task_id,
                       {"project_workspace": str(proj)})
    return led, ws, proj


def test_r4_reuse_scenario_no_longer_stable_false_failure(tmp_path):
    """R4: 复用场景 (产物 mtime 早于基准) 不再稳定假失败。

    这是 F-M-4 的核心验收: 复用前轮产物 + 在『结果』里声明复用来源
    ⇒ physical_check.passed=True (不再是假失败), 且 stale 与复用来源
    都留痕供 main 审计。
    """
    now = time.time()
    led, ws, proj = _setup_reuse_harvest(
        tmp_path, "t_reuse", "t_reuse",
        "复用前轮产物。复用来源: out/OUT.md",
        product_mtime=now - 600)
    from mw.harvest import harvest
    r = harvest("t_reuse", ledger=led, hermes_home=str(tmp_path),
                workspace=str(ws), spawn_result=None,
                worker_status="completed")
    assert r["settled"] is True
    assert r["verdict"] == "COMPLETE"
    pc = r["worker_report"]["physical_check"]
    assert pc["passed"] is True, \
        f"复用场景不得稳定假失败: {pc}"
    assert pc["stale"] is True, "陈旧事实仍须留痕"
    assert pc["reuse_declared"] is True
    assert pc["reuse_source"], "复用来源必须留痕 (main 审计线索)"
    led.close()


def test_r4b_no_declaration_keeps_false_failure_visible(tmp_path):
    """R4b: 未声明复用的陈旧产物 ⇒ 仍判 passed=False (对照臂)。"""
    now = time.time()
    led, ws, proj = _setup_reuse_harvest(
        tmp_path, "t_noreuse", "t_noreuse",
        "本轮正常产出, 走完自检。",
        product_mtime=now - 600)
    from mw.harvest import harvest
    r = harvest("t_noreuse", ledger=led, hermes_home=str(tmp_path),
                workspace=str(ws), spawn_result=None,
                worker_status="completed")
    assert r["settled"] is True
    pc = r["worker_report"]["physical_check"]
    assert pc["passed"] is False, \
        f"未声明复用的陈旧产物必须仍判不过: {pc}"
    assert pc["stale"] is True
    assert pc["reuse_declared"] is False
    led.close()


# ════════════════════════════════════════════════════════════════
# R6 / R7: 模板条款与基线 fixture (文档面与代码面必须同 commit)
# ════════════════════════════════════════════════════════════════

ZH_TEMPLATE = ROOT / "mw" / "worker_contract_prompt.zh-cn.md"
EN_TEMPLATE = ROOT / "mw" / "worker_contract_prompt.en.md"
FIXTURE = ROOT / "tests" / "fixtures" / "worker_prompt_baseline_zh.txt"


def test_r6_contract_prompt_reuse_clause_matches_guard():
    """R6: 中英模板的复用条款必须与新鲜度守卫衔接口径一致。"""
    zh = ZH_TEMPLATE.read_text(encoding="utf-8")
    en = EN_TEMPLATE.read_text(encoding="utf-8")
    # 复用声明标记字面在位 (与 harvest.extract_reuse_declaration 的口径一致)
    assert "复用来源" in zh, "zh 模板缺『复用来源』声明标记"
    assert "reuse source" in en, "en 模板缺 'reuse source' declaration marker"
    # 两条合法出路都在位 (touch 刷新 + 声明降级)
    assert "touch" in zh, "zh 模板缺 touch 刷新口径"
    assert "touch" in en, "en 模板缺 touch 刷新口径"
    # 陈旧守卫名在位 (条款与守卫口径对齐的锚)
    assert "physical_stale" in zh and "physical_stale" in en


def test_r6b_reuse_clause_zero_braces():
    """R6b: 复用条款文本零花括号 (worker_contract_prompt 走 str.format,
    花括号会炸派发; PLAN R-3 既有纪律)。"""
    for f, marker in ((ZH_TEMPLATE, "复用条款与新鲜度守卫"),
                      (EN_TEMPLATE, "Reuse clause vs the freshness guard")):
        text = f.read_text(encoding="utf-8")
        i = text.index(marker)
        clause = text[i:text.index("\n\n", i)]
        assert "{" not in clause and "}" not in clause, \
            f"{f.name} 复用条款含花括号, str.format 会炸"


def test_r7_zh_baseline_fixture_in_sync():
    """R7: zh 模板渲染 = byte-equal baseline (改模板必改 fixture)。"""
    os.environ["MW_LANG"] = "zh-cn"
    from mw.i18n import _cache, _LOADED
    _cache.clear()
    _LOADED.clear()
    import importlib
    import mw.pipeline as pipeline
    importlib.reload(pipeline)
    p = pipeline.worker_contract_prompt("/x/TODO.md", "/x", "/x")
    base = FIXTURE.read_text(encoding="utf-8")
    assert p == base, (
        f"zh prompt 偏离 baseline {len(base)} 字节, 实际 {len(p)} 字节")
    assert "复用来源" in p, "baseline 未含复用声明标记"


# ════════════════════════════════════════════════════════════════
# R8: 变异反证 (自证有牙齿)
# ════════════════════════════════════════════════════════════════

def test_r8_reuse_flag_is_load_bearing(tmp_path, monkeypatch):
    """R8: 复用降级分支是**承重**的 —— 去掉它, 复用场景立刻回到假失败。

    变异方式 (不改源码, 用 monkeypatch 模拟「分支被删」): 把 verify_physical
    包一层, 丢弃 reuse_declared 参数 ⇒ 等价于守卫里没有降级分支。
    断言: 端到端 harvest 恢复 passed=False (假失败回归) ⇒ 本守护有牙齿。
    """
    import mw.harvest as H
    orig = H.verify_physical

    def _mutant(*a, **kw):
        kw.pop("reuse_declared", None)
        return orig(*a, **kw)

    now = time.time()
    led, ws, proj = _setup_reuse_harvest(
        tmp_path, "t_mutant", "t_mutant",
        "复用前轮产物。复用来源: out/OUT.md",
        product_mtime=now - 600)
    monkeypatch.setattr(H, "verify_physical", _mutant)
    r = H.harvest("t_mutant", ledger=led, hermes_home=str(tmp_path),
                  workspace=str(ws), spawn_result=None,
                  worker_status="completed")
    pc = r["worker_report"]["physical_check"]
    assert pc["passed"] is False, \
        f"去掉降级分支后必须回到假失败 (变异反证失效): {pc}"
    assert pc["stale"] is True
    led.close()


def test_r8b_reuse_decl_marker_mutation_turns_guard_red():
    """R8b: 复用声明标记被改 ⇒ 提取必然失效 (守护对标记改动敏感)。"""
    import re
    from mw.harvest import _REUSE_DECL_RE
    assert _REUSE_DECL_RE.search("复用来源: a/b.md") is not None
    assert _REUSE_DECL_RE.search("reuse source: a/b.md") is not None
    # 变异: 把标记换成不可能命中的串 ⇒ 提取必然失效 (反向自证)
    mutant = _REUSE_DECL_RE.pattern.replace("复用来源", "绝不命中")
    assert re.search(mutant, "复用来源: a/b.md") is None


# ════════════════════════════════════════════════════════════════
# F-L-hist-1: 卡绑定告警的去重窗口不再隐式依赖 MW_REAPER_STUCK_MIN
# ════════════════════════════════════════════════════════════════

def test_r9_dedup_window_is_independent_of_stuck_min(monkeypatch):
    """R9: 去重窗口走独立 env, 不随 MW_REAPER_STUCK_MIN 漂移。

    F-L-hist-1: 旧实现直接读 MW_REAPER_STUCK_MIN, 而该阈值有三个互不相同
    的内置默认值 (部署 10 / cli.py 5 / reaper.py 30) ⇒ 告警节奏随 stuck
    阈值漂移。本卡把它独立成 MW_BINDING_EVENT_DEDUP_MIN (默认 30)。
    """
    from mw.reaper import _binding_event_dedup_min
    # 默认 30 (与旧注释口径一致)
    monkeypatch.delenv("MW_BINDING_EVENT_DEDUP_MIN", raising=False)
    monkeypatch.delenv("MW_REAPER_STUCK_MIN", raising=False)
    assert _binding_event_dedup_min() == 30
    # 改 stuck 阈值 ⇒ 去重窗口**不变** (独立性自证)
    monkeypatch.setenv("MW_REAPER_STUCK_MIN", "5")
    assert _binding_event_dedup_min() == 30
    monkeypatch.setenv("MW_REAPER_STUCK_MIN", "10")
    assert _binding_event_dedup_min() == 30
    # 独立 env 显式设置 ⇒ 生效
    monkeypatch.setenv("MW_BINDING_EVENT_DEDUP_MIN", "7")
    assert _binding_event_dedup_min() == 7
    # 非法值 ⇒ 回落 30 (不抛异常)
    monkeypatch.setenv("MW_BINDING_EVENT_DEDUP_MIN", "abc")
    assert _binding_event_dedup_min() == 30


def test_r9b_suppression_uses_dedup_env_not_stuck_min(tmp_path, monkeypatch):
    """R9b: 端到端: 事件抑制窗口跟独立 env 走, 跟 stuck 阈值无关。"""
    from mw.ledger import Ledger
    from mw.reaper import _binding_mismatch_event_suppressed
    led = Ledger(str(tmp_path / "ledger.db"))
    # stuck 阈值压到 1min (旧实现下窗口 = 60s); 独立 env 保持 30min
    monkeypatch.setenv("MW_REAPER_STUCK_MIN", "1")
    monkeypatch.setenv("MW_BINDING_EVENT_DEDUP_MIN", "30")
    led.emit_event("harvest.card_binding_mismatch", "t_x", {})
    # 事件发生在 5min 前: 独立窗口 (30min) ⇒ 仍静默
    led.conn.execute(
        "UPDATE mw_events SET created_at=? "
        "WHERE task_id=? AND event_type='harvest.card_binding_mismatch'",
        (time.time() - 5 * 60, "t_x"))
    led.conn.commit()
    assert _binding_mismatch_event_suppressed(led, "t_x") is True, \
        "去重窗口不得随 MW_REAPER_STUCK_MIN 缩到 1min (F-L-hist-1)"
    led.close()
