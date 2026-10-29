"""T3 reaper/harvest/ledger 串行护栏单元测试 (2026-10-03)。

覆盖:
  1. reap_once 无 ACTIVE run → SKIP, 不动孤儿卡
  2. reap_once 有 ACTIVE run → 只扫该 run 的 PENDING 卡
  3. reap_once._notify_main_of_stuck 加 active_run_id 过滤
  4. harvest 落 COMPLETE → clear team_work_runs.current_card_id
  5. harvest 落 NEED_CLARIFICATION / FAILED → 也清 current_card_id
     (边界减法 2026-10-08: PHYSICAL_ERROR / PASS_BUMPED 已删, 任何终态都清槽交球 main)
  7. is_task_running 加 team work 串行校验 (current_card_id 匹配)
  8. is_task_running run 非 ACTIVE → running=False + violation
"""
import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results = []


def check(name, fn):
    try:
        fn()
        print(f"  {PASS} {name}")
        results.append(True)
    except AssertionError as e:
        print(f"  {FAIL} {name}: {e}")
        results.append(False)
    except Exception as e:
        print(f"  {FAIL} {name}: {type(e).__name__}: {e}")
        results.append(False)


def fresh(base_dir):
    """在 base_dir 下建 ledger.db, 返回 (led, base_path)。

    issue #130: 目录生命周期交给调用方 (pytest 路径 = tmp_path fixture;
    直跑路径 = TemporaryDirectory)。旧写法在本函数里自己
    tempfile.mkdtemp(), 各用例 finally 再裸 shutil.rmtree(base) ——
    那条清理窗口会与 reap_once 起的后台 daemon 线程抢同一个目录 ⇒
    OSError: [Errno 39] Directory not empty 假红 (产品断言早已 PASS)。
    本函数只负责「在给定目录里建 ledger.db」, 不再自造目录。
    """
    base = str(base_dir)
    db = os.path.join(base, "ledger.db")
    from mw.ledger import Ledger
    led = Ledger(db)
    # is_task_running 通过 MW_HOME 环境变量找 marker dir; 设上
    os.environ["MW_HOME"] = base
    return led, base


# ============================================================
# T3.1: reap_once 扫描源收窄
# ============================================================

def test_reap_skips_when_no_active(tmp_path):
    """无 ACTIVE run → reap_once 不动孤儿卡。"""
    from mw import reaper
    led, base = fresh(tmp_path)
    try:
        # 写一个孤儿 PENDING 卡 + 不带 team_work_run_id
        led.put_contract("t_orphan", {"required_files": [], "timeout_seconds": 60})
        # reap
        results_list = reaper.reap_once(ledger=led, hermes_home=base,
                                       base=base, grace_seconds=10, registry={})
        scope_skips = [r for r in results_list
                      if r.get("trigger") == "reap_scope" and r.get("verdict") == "SKIP"]
        assert len(scope_skips) == 1, \
            f"expected 1 reap_scope SKIP, got {results_list}"
        # 孤儿卡不应被 reap
        assert led.get_contract("t_orphan")["harvest_status"] == "PENDING"
    finally:
        # issue #130: 不再 rmtree(base) —— 目录归 tmp_path 生命周期管
        # (裸 rmtree 与后台 daemon 线程抢目录 ⇒ Errno 39 假红)。
        led.close()


def test_reap_only_scans_active_run(tmp_path):
    """有 ACTIVE run → 只扫该 run 下的 PENDING。"""
    from mw import reaper
    led, base = fresh(tmp_path)
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.put_contract("t_in", {"required_files": [], "timeout_seconds": 60})
        led.register_team_work_card("r1", "t_in")
        led.put_contract("t_orphan", {"required_files": [], "timeout_seconds": 60})
        # 不写 TODO.md → reaper 的 reap_scope 应只 log in_run card,
        # 不会触碰孤儿
        results_list = reaper.reap_once(ledger=led, hermes_home=base,
                                       base=base, grace_seconds=10, registry={})
        scope = [r for r in results_list if r.get("trigger") == "reap_scope"]
        assert len(scope) == 1
        assert "r1" in scope[0]["note"]
        # 孤儿卡未被 settle
        assert led.get_contract("t_orphan")["harvest_status"] == "PENDING"
    finally:
        # issue #130: 不再 rmtree(base) —— 目录归 tmp_path 生命周期管
        # (裸 rmtree 与后台 daemon 线程抢目录 ⇒ Errno 39 假红)。
        led.close()


# ============================================================
# T3.2: _notify_main_of_stuck 过滤
# ============================================================

def test_stuck_notify_skips_without_active_run_id(tmp_path):
    """active_run_id=None → _notify_main_of_stuck 不扫任何卡。"""
    from mw import reaper
    led, base = fresh(tmp_path)
    try:
        # 即使有孤儿卡 stuck 也不该敲门
        led.put_contract("t_orphan", {"required_files": []})
        results_list = []
        reaper._notify_main_of_stuck(led, base, results_list, registry={},
                                    stuck_after_min=1, active_run_id=None)
        assert results_list == [], \
            f"active_run_id=None 应 SKIP 敲门, got {results_list}"
    finally:
        # issue #130: 不再 rmtree(base) —— 目录归 tmp_path 生命周期管
        # (裸 rmtree 与后台 daemon 线程抢目录 ⇒ Errno 39 假红)。
        led.close()


def test_stuck_notify_filters_by_active_run_id(tmp_path):
    """active_run_id 给定时, 只扫该 run 的 PENDING。"""
    from mw import reaper
    led, base = fresh(tmp_path)
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.put_contract("t_in", {})
        led.register_team_work_card("r1", "t_in")
        led.put_contract("t_orphan", {})
        results_list = []
        reaper._notify_main_of_stuck(led, base, results_list, registry={},
                                    stuck_after_min=0, active_run_id="r1")
        # t_orphan 没 last_progress_at / 不会被敲门 (因为 last_progress_at 是 NULL
        # → 兜底 updated_at, 我们刚 PUT, 距 now < threshold) — 验证只走了
        # pending_in_team_work("r1") 而没有 t_orphan
        for r in results_list:
            assert r.get("task_id") != "t_orphan", \
                f"孤儿不应被敲门: {r}"
    finally:
        # issue #130: 不再 rmtree(base) —— 目录归 tmp_path 生命周期管
        # (裸 rmtree 与后台 daemon 线程抢目录 ⇒ Errno 39 假红)。
        led.close()


# ============================================================
# T3.3: harvest 落终态清 current_card_id
# ============================================================

def test_harvest_complete_clears_current_card(tmp_path):
    """COMPLETE 落账后, run.current_card_id 应清 NULL。"""
    from mw import harvest as harvest_mod
    led, base = fresh(tmp_path)
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.put_contract("t_a", {"required_files": ["out.txt"]})
        led.register_team_work_card("r1", "t_a")
        led.set_team_work_current_card("r1", "t_a")
        assert led.get_team_work("r1")["current_card_id"] == "t_a"
        # 模拟 workspace + 产物
        ws = os.path.join(base, "ws")
        os.makedirs(ws, exist_ok=True)
        # 没产物 → 边界减法后仍 COMPLETE (物理缺失只作证据), 但这里写产物
        # 走正常 COMPLETE 路径
        (Path(ws) / "out.txt").write_text("ok")
        # harvest
        r = harvest_mod.harvest("t_a", ledger=led, hermes_home=base,
                              workspace=ws, registry={})
        # verdict 应该是 COMPLETE
        assert r["verdict"] == "COMPLETE", f"got {r['verdict']}: {r.get('log')}"
        # ★ current_card_id 应清 NULL
        assert led.get_team_work("r1")["current_card_id"] is None, \
            f"current_card_id 应清 NULL, got {led.get_team_work('r1')['current_card_id']}"
    finally:
        # issue #130: 不再 rmtree(base) —— 目录归 tmp_path 生命周期管
        # (裸 rmtree 与后台 daemon 线程抢目录 ⇒ Errno 39 假红)。
        led.close()


def test_harvest_complete_includes_products_evidence(tmp_path):
    """COMPLETE 报告应含 products 字段 (结构化产物证据)。

    减法 (2026-10-05): worker_report 加 products 让 main 读到结构化
    证据 (file 列表 + 静态扫描) 就不必独立冒烟。
    """
    from mw import harvest as harvest_mod
    led, base = fresh(tmp_path)
    try:
        led.create_team_work_run("r1", "s1", "需求")
        led.put_contract("t_a", {"required_files": ["out.html"]})
        led.register_team_work_card("r1", "t_a")
        led.set_team_work_current_card("r1", "t_a")
        ws = os.path.join(base, "ws")
        os.makedirs(ws, exist_ok=True)
        # 写个干净的 HTML 产物
        (Path(ws) / "out.html").write_text(
            "<!doctype html><html><body>"
            "<script>function ok(){return 1;}</script>"
            "</body></html>")
        r = harvest_mod.harvest("t_a", ledger=led, hermes_home=base,
                              workspace=ws, registry={})
        assert r["verdict"] == "COMPLETE"
        wr = r["worker_report"]
        assert "products" in wr, f"应含 products 字段: {wr.keys()}"
        products = wr["products"]
        assert "files" in products and len(products["files"]) == 1
        f = products["files"][0]
        assert f["name"] == "out.html"
        assert f["exists"] is True
        assert f["size_bytes"] > 0
        # sha256 (小文件 <2MB 必出)
        assert "sha256" in f and len(f["sha256"]) == 64
        # static_check 应 passed (干净 HTML)
        assert f["static_check"]["passed"] is True, \
            f"应 passed, got issues: {f['static_check']['issues']}"
    finally:
        # issue #130: 不再 rmtree(base) —— 目录归 tmp_path 生命周期管
        # (裸 rmtree 与后台 daemon 线程抢目录 ⇒ Errno 39 假红)。
        led.close()


def test_harvest_complete_products_flags_undefined_identifier(tmp_path):
    """HTML 含 GLYPH_X 未定义标识符 → static_check 应捕获并 passed=false。"""
    from mw import harvest as harvest_mod
    led, base = fresh(tmp_path)
    try:
        led.create_team_work_run("r1", "s1", "需求")
        led.put_contract("t_a", {"required_files": ["buggy.html"]})
        led.register_team_work_card("r1", "t_a")
        led.set_team_work_current_card("r1", "t_a")
        ws = os.path.join(base, "ws")
        os.makedirs(ws, exist_ok=True)
        # 模拟 BUG-2 场景 (transcript 70100fba85ad t_2fdd9bf3): think 动作
        # 引用 GLYPH_EX 但全文件无声明
        (Path(ws) / "buggy.html").write_text(
            "<!doctype html><html><body>"
            "<script>function think(){drawChar(GLYPH_EX);}</script>"
            "</body></html>")
        r = harvest_mod.harvest("t_a", ledger=led, hermes_home=base,
                              workspace=ws, registry={})
        assert r["verdict"] == "COMPLETE", \
            "静态扫描失败不阻断 harvest, 仍 COMPLETE (main 看 products 决定)"
        products = r["worker_report"]["products"]
        assert products["static_check"]["passed"] is False, \
            f"应 fail: {products['static_check']}"
        assert any("GLYPH" in i.get("rule", "") or "未定义" in i.get("message", "")
                  for i in products["static_check"]["issues"]), \
            f"应捕获 GLYPH_* issue: {products['static_check']['issues']}"
    finally:
        # issue #130: 不再 rmtree(base) —— 目录归 tmp_path 生命周期管
        # (裸 rmtree 与后台 daemon 线程抢目录 ⇒ Errno 39 假红)。
        led.close()


def test_harvest_missing_files_complete_with_evidence_clears_current_card(tmp_path):
    """边界减法 (2026-10-08): required_files 缺失不再落 PHYSICAL_ERROR,
    而是随 COMPLETE 上报 physical_check.passed=False 证据 (worker 自报
    驱动 verdict, mw 不推翻); 落账后仍清 current_card_id。"""
    from mw import harvest as harvest_mod
    led, base = fresh(tmp_path)
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.put_contract("t_a", {"required_files": ["missing.txt"]})
        led.register_team_work_card("r1", "t_a")
        led.set_team_work_current_card("r1", "t_a")
        ws = os.path.join(base, "ws")
        os.makedirs(ws, exist_ok=True)
        r = harvest_mod.harvest("t_a", ledger=led, hermes_home=base,
                              workspace=ws, registry={})
        assert r["verdict"] == "COMPLETE"
        # 物理缺失以证据形式随报告上报, 供 main 判断
        rep = r.get("worker_report") or {}
        pc = rep.get("physical_check") or {}
        assert pc.get("passed") is False, f"应带 physical_check 证据: {rep}"
        assert led.get_team_work("r1")["current_card_id"] is None
    finally:
        # issue #130: 不再 rmtree(base) —— 目录归 tmp_path 生命周期管
        # (裸 rmtree 与后台 daemon 线程抢目录 ⇒ Errno 39 假红)。
        led.close()


def test_harvest_need_clarification_clears_current_card(tmp_path):
    """NEED_CLARIFICATION 落账后也清 (卡 .clarify.json 模拟)。"""
    from mw import harvest as harvest_mod
    led, base = fresh(tmp_path)
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.put_contract("t_a", {"required_files": []})
        led.register_team_work_card("r1", "t_a")
        led.set_team_work_current_card("r1", "t_a")
        ws = os.path.join(base, "ws")
        os.makedirs(ws, exist_ok=True)
        # 模拟 .clarify.json
        (Path(ws) / ".clarify.json").write_text('{"question": "需要 X?"}')
        r = harvest_mod.harvest("t_a", ledger=led, hermes_home=base,
                              workspace=ws, registry={})
        assert r["verdict"] == "NEED_CLARIFICATION"
        assert led.get_team_work("r1")["current_card_id"] is None
    finally:
        # issue #130: 不再 rmtree(base) —— 目录归 tmp_path 生命周期管
        # (裸 rmtree 与后台 daemon 线程抢目录 ⇒ Errno 39 假红)。
        led.close()


def test_harvest_failed_hands_ball_to_main(tmp_path):
    """边界减法 (2026-10-08) 串行不变量: worker 自报 failed → 球交 main。

    验证三件事缺一不可:
      1. verdict=FAILED (worker 决策如实上报, 不被 mw 推翻也不被吞)
      2. harvest_status=BLOCKED → 落在 pixel_bridge._BLOCK_STATUS,
         办公室显示 blocked (不是假装还在干活)
      3. current_card_id 清空 → worker_active=False → main_owns_ball=True
         → main 场景 editing (永远有人在跟进)
    """
    from mw import harvest as harvest_mod
    led, base = fresh(tmp_path)
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.put_contract("t_a", {"required_files": []})
        led.register_team_work_card("r1", "t_a")
        led.set_team_work_current_card("r1", "t_a")
        ws = os.path.join(base, "ws")
        os.makedirs(ws, exist_ok=True)
        r = harvest_mod.harvest("t_a", ledger=led, hermes_home=base,
                              workspace=ws, registry={},
                              worker_status="failed")
        assert r["verdict"] == "FAILED", r
        assert r["harvest_status"] == "BLOCKED", r
        # 球交 main: current_card_id 已清
        assert led.get_team_work("r1")["current_card_id"] is None
        # BLOCKED 必须在 pixel 的阻塞集合里, 否则办公室看不出阻塞
        from mw.pixel_bridge import _BLOCK_STATUS, _WORKER_ACTIVE_STATUS
        assert "BLOCKED" in _BLOCK_STATUS
        assert "BLOCKED" not in _WORKER_ACTIVE_STATUS
    finally:
        # issue #130: 不再 rmtree(base) —— 目录归 tmp_path 生命周期管
        # (裸 rmtree 与后台 daemon 线程抢目录 ⇒ Errno 39 假红)。
        led.close()


def test_harvest_timeout_hands_ball_to_main(tmp_path):
    """平台收尸 (timeout) 同样归 FAILED + BLOCKED, 球交 main。

    与 worker 自报 failed 的区别只在 why 字段, 不新增 verdict 状态
    (减法原则)。
    """
    from mw import harvest as harvest_mod
    led, base = fresh(tmp_path)
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.put_contract("t_a", {"required_files": []})
        led.register_team_work_card("r1", "t_a")
        led.set_team_work_current_card("r1", "t_a")
        ws = os.path.join(base, "ws")
        os.makedirs(ws, exist_ok=True)
        r = harvest_mod.harvest("t_a", ledger=led, hermes_home=base,
                              workspace=ws, registry={},
                              worker_status="timeout")
        assert r["verdict"] == "FAILED", r
        assert r["harvest_status"] == "BLOCKED", r
        assert "超时" in r["worker_report"]["why"], r["worker_report"]
        assert led.get_team_work("r1")["current_card_id"] is None
    finally:
        # issue #130: 不再 rmtree(base) —— 目录归 tmp_path 生命周期管
        # (裸 rmtree 与后台 daemon 线程抢目录 ⇒ Errno 39 假红)。
        led.close()


def test_s1_timeout_not_in_alias_table(tmp_path):
    """issue#127 S-1 陷阱锁: "timeout" **永不**进 normalize_status 别名表。

    "timeout" 是平台内部收尸值 (reaper.py:317 超时收尸 / :382 TODO 缺失
    收尸显式传入), **不是** worker 可写状态。把它塞进 STATUS_ALIASES 会
    让 worker 字面写出的 "timeout" 被当成合法 failed, 破坏"worker 不可写
    timeout"的边界 —— 故此处显式锁死。

    本锁同时是 S-1 前置短路的**前提**: 只有 normalize_status("timeout")
    恒为 None, harvest 才必须自己前置处理 timeout, 而不能依赖归一化。
    """
    from mw.todo import (STATUS_ALIASES, STATUS_BLOCKED, STATUS_FAILED,
                          _STATUS_ALIAS_INDEX, normalize_status)
    # 陷阱本体: 归一化不认识 timeout
    assert normalize_status("timeout") is None, \
        "normalize_status('timeout') 必须为 None (timeout 非 worker 可写状态)"
    # 别名表任何一格都不得含 timeout
    for canon, aliases in STATUS_ALIASES.items():
        assert "timeout" not in aliases, \
            f"STATUS_ALIASES[{canon!r}] 不得含 'timeout' (issue#127 S-1)"
    assert "timeout" not in _STATUS_ALIAS_INDEX, \
        "归一化索引不得含 'timeout' (issue#127 S-1)"
    # failed/blocked 别名表本身未受影响 (收敛到它, 不是改它)
    assert "failed" in STATUS_ALIASES[STATUS_FAILED]
    assert "blocked" in STATUS_ALIASES[STATUS_BLOCKED]


def test_s1_harvest_timeout_still_FAILED_with_timeout_reason(tmp_path):
    """issue#127 S-1 回归锁: harvest(worker_status="timeout") 仍 FAILED +
    why=harvest.reason.failed_timeout。

    封死未来"把 timeout 交给 normalize_status 统一处理"的改动 ——
    那样 normalize_status("timeout")→None 会掉进 1c 未知状态兜底,
    **超时收尸被静默改判 COMPLETE/clarify, 收尸路径断裂** (P0 级静默
    行为变更, 非注释级清理)。

    本锁是 S-1 要求的定向回归 (R-127-S1-c): 与改前逐字一致 ——
    verdict=FAILED + harvest_status=BLOCKED + why 取平台收尸专用文案
    (非 worker 自报失败的文案)。
    """
    from mw import harvest as harvest_mod
    from mw.i18n import t as _t
    led, base = fresh(tmp_path)
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.put_contract("t_a", {"required_files": []})
        led.register_team_work_card("r1", "t_a")
        led.set_team_work_current_card("r1", "t_a")
        ws = os.path.join(base, "ws")
        os.makedirs(ws, exist_ok=True)
        r = harvest_mod.harvest("t_a", ledger=led, hermes_home=base,
                               workspace=ws, registry={},
                               worker_status="timeout")
        # 1) verdict / harvest_status 与改前逐字一致
        assert r["verdict"] == "FAILED", r
        assert r["harvest_status"] == "BLOCKED", r
        # 2) why 必须是**平台收尸**专用文案 (而非 worker 自报失败文案)
        why = r["worker_report"]["why"]
        assert why == _t("harvest.reason.failed_timeout"), \
            f"timeout 的 why 必须是 failed_timeout, 实际 {why!r}"
        assert why != _t("harvest.reason.failed_worker_self"), \
            "timeout 不得被当成 worker 自报失败"
        # 3) 不能掉进 1c 未知兜底 (那会静默改判 COMPLETE/clarify)
        assert r["verdict"] != "COMPLETE", r
        assert r["verdict"] != "NEED_CLARIFICATION", r
        # 4) 球交 main (current_card_id 已清)
        assert led.get_team_work("r1")["current_card_id"] is None
    finally:
        # issue #130: 不再 rmtree(base) —— 目录归 tmp_path 生命周期管
        # (裸 rmtree 与后台 daemon 线程抢目录 ⇒ Errno 39 假红)。
        led.close()


def test_harvest_no_run_id_does_not_explode(tmp_path):
    """孤儿卡 (无 team_work_run_id) harvest 不报错, 也不清任何 run。"""
    from mw import harvest as harvest_mod
    led, base = fresh(tmp_path)
    try:
        led.put_contract("t_orphan", {"required_files": []})
        ws = os.path.join(base, "ws")
        os.makedirs(ws, exist_ok=True)
        r = harvest_mod.harvest("t_orphan", ledger=led, hermes_home=base,
                              workspace=ws, registry={})
        assert r["verdict"] == "COMPLETE"  # 没产物要求, 物理 OK
    finally:
        # issue #130: 不再 rmtree(base) —— 目录归 tmp_path 生命周期管
        # (裸 rmtree 与后台 daemon 线程抢目录 ⇒ Errno 39 假红)。
        led.close()


# ============================================================
# T3.4: is_task_running 升级校验
# ============================================================

def test_is_task_running_no_violation_for_legacy(tmp_path):
    """不带 run_id 的卡 (legacy / 单卡 dispatch) → running 由 marker 决定。"""
    led, base = fresh(tmp_path)
    try:
        led.put_contract("t_legacy", {})
        # 没 marker → running False
        r = led.is_task_running("t_legacy")
        assert r["running"] is False
        assert r["team_work_violation"] is None
        assert r["team_work_run_id"] is None
    finally:
        # issue #130: 不再 rmtree(base) —— 目录归 tmp_path 生命周期管
        # (裸 rmtree 与后台 daemon 线程抢目录 ⇒ Errno 39 假红)。
        led.close()


def test_is_task_running_violation_when_current_card_mismatch(tmp_path):
    """task 属于 ACTIVE run, 但 current_card_id 不是本 task → violation。"""
    led, base = fresh(tmp_path)
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.put_contract("t_a", {})
        led.register_team_work_card("r1", "t_a")
        led.put_contract("t_b", {})
        led.register_team_work_card("r1", "t_b")
        # 设 current_card_id = t_a, 查 t_b 应被识别为 violation
        led.set_team_work_current_card("r1", "t_a")
        # 写 marker 让它看起来"在跑" (is_task_running 需要 marker + harvest PENDING)
        from mw.reaper import write_marker
        import time
        write_marker(base, "t_b", started_at=time.time())
        r = led.is_task_running("t_b")
        assert r["running"] is False
        assert r["team_work_violation"] is not None
        assert "t_a" in r["team_work_violation"]
    finally:
        # issue #130: 不再 rmtree(base) —— 目录归 tmp_path 生命周期管
        # (裸 rmtree 与后台 daemon 线程抢目录 ⇒ Errno 39 假红)。
        led.close()


def test_is_task_running_passes_when_current_card_matches(tmp_path):
    """task 属于 ACTIVE run, current_card_id == task → running=True, 无 violation。"""
    led, base = fresh(tmp_path)
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.put_contract("t_a", {})
        led.register_team_work_card("r1", "t_a")
        led.set_team_work_current_card("r1", "t_a")
        from mw.reaper import write_marker
        import time
        write_marker(base, "t_a", started_at=time.time())
        r = led.is_task_running("t_a")
        assert r["running"] is True
        assert r["team_work_violation"] is None
    finally:
        # issue #130: 不再 rmtree(base) —— 目录归 tmp_path 生命周期管
        # (裸 rmtree 与后台 daemon 线程抢目录 ⇒ Errno 39 假红)。
        led.close()


def test_is_task_running_violation_when_run_closed(tmp_path):
    """run 已 CLOSED → 即使 task 是 current_card 也不再算 running。"""
    led, base = fresh(tmp_path)
    try:
        led.create_team_work_run("r1", "s1", "需求1")
        led.put_contract("t_a", {})
        led.register_team_work_card("r1", "t_a")
        led.set_team_work_current_card("r1", "t_a")
        from mw.reaper import write_marker
        import time
        write_marker(base, "t_a", started_at=time.time())
        led.close_team_work("r1", "test")
        r = led.is_task_running("t_a")
        assert r["running"] is False
        assert r["team_work_violation"] is not None
        assert "ABORTED" in r["team_work_violation"] or "非 ACTIVE" in r["team_work_violation"]
    finally:
        # issue #130: 不再 rmtree(base) —— 目录归 tmp_path 生命周期管
        # (裸 rmtree 与后台 daemon 线程抢目录 ⇒ Errno 39 假红)。
        led.close()


# ============================================================

def main():
    """直跑路径 (issue #130): 逐个用例包一层临时目录。

    用例签名里的 tmp_path 是 pytest fixture, 直跑时不存在 ⇒ 这里用
    tempfile.TemporaryDirectory(ignore_cleanup_errors=True) 兜底:
      - 退出即回收 (不泄漏临时目录; 68d5862 实测裸 mkdtemp 会 40/40 轮泄漏);
      - 清理期若撞上 reap_once 起的后台 daemon 线程落 WAL 文件, 只忽略
        清理错误, 不把直跑搞挂 (裸 rmtree 会抛 Errno 39 假红)。
    不用 rmtree(ignore_errors=True): 那只是把异常吞掉, 目录永久泄漏。
    """
    # 直跑时没有 tests/conftest.py 代劳, 这里把部署时钟钉回 zh-cn —
    # 与 conftest.py 的 setdefault 同款 (测试断言是中文字面量,
    # MW_LANG 缺省是 en ⇒ 直跑会误红)。
    os.environ.setdefault("MW_LANG", "zh-cn")
    tests = [
        test_reap_skips_when_no_active,
        test_reap_only_scans_active_run,
        test_stuck_notify_skips_without_active_run_id,
        test_stuck_notify_filters_by_active_run_id,
        test_harvest_complete_clears_current_card,
        test_harvest_complete_includes_products_evidence,
        test_harvest_complete_products_flags_undefined_identifier,
        test_harvest_missing_files_complete_with_evidence_clears_current_card,
        test_harvest_need_clarification_clears_current_card,
        test_harvest_failed_hands_ball_to_main,
        test_harvest_timeout_hands_ball_to_main,
        test_s1_timeout_not_in_alias_table,
        test_s1_harvest_timeout_still_FAILED_with_timeout_reason,
        test_harvest_no_run_id_does_not_explode,
        test_is_task_running_no_violation_for_legacy,
        test_is_task_running_violation_when_current_card_mismatch,
        test_is_task_running_passes_when_current_card_matches,
        test_is_task_running_violation_when_run_closed,
    ]
    for t in tests:
        # 直跑时 fixture 不存在 ⇒ 传一个手工临时目录兜底, 保证两种跑法都绿
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            check(t.__name__, lambda t=t, td=td: t(Path(td)))
    total = len(results)
    passed = sum(results)
    print()
    # 清理 MW_HOME
    os.environ.pop("MW_HOME", None)
    if passed == total:
        print(f"\033[92m=== ALL {total} T3 TESTS PASSED ===\033[0m")
        return 0
    else:
        print(f"\033[91m=== {total - passed}/{total} FAILED ===\033[0m")
        return 1


if __name__ == "__main__":
    sys.exit(main())