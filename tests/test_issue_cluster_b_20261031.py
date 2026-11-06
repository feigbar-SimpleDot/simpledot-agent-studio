"""O 批簇B 回归测试 (2026-10-31): #177 #175 #171 #173 #174 #172。

依据 docs/FIX-PLAN-O.md §簇B 各条「验证要点」:
  #177  兜底敲门有界化
        - K1 连敲 N 次后第 N+1 轮不敲, exhausted 事件恰 1 条
        - K2 exhausted 后新报告 (set_worker_report 清 confirmed_at) ⇒ 重新武装
  #175  notify_task=None 敲门撞 404 留痕
        - K3 mock 404 + knock_ctx ⇒ 事件落账且 harvest_contracts 无变化
  #171  404 死信前网关路由探测
        - K4 探测 200 + 目标 404 ⇒ 死信 (旧语义不变)
        - K5 探测 404 ⇒ 退避 600s + relay.notify_gateway_misrouted
        - K6 探测连接异常 ⇒ 保守走旧死信
  #173  死信期卡 mismatch 敲门解耦
        - K7 死信卡 (dead_until=now+1年) + mismatch ⇒ 仍敲一次
        - K8 窗口内二次触发 ⇒ 不敲
  #174  TODO.md 路径定位同源
        - K9 pw/workspace 分叉 + _map_path 生效 ⇒ reaper 与 harvest 同路径
  #172  新鲜度容差 env 化
        - K10 MW_FRESH_EPSILON_S 覆盖生效 + 非法值回落
  #194/#200  固定 sleep 收尾改轮询等待
        - K11 轮询等待口径 = 状态列 ∧ 事件行 (带超时上限) + 变异反证锚

纯本地 ledger 测试, 不依赖 hermes CLI / 网络: 敲门通道与 urlopen 全 patch。
"""
import ast
import json
import os
import sys
import time
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest import mock

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:9")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from mw import reaper as reaper_mod  # noqa: E402
from mw.ledger import Ledger  # noqa: E402
from mw.harvest import (  # noqa: E402
    FRESH_EPSILON, verify_physical, todo_path_for, _fresh_epsilon_s,
    _todo_path_for_harvest,
)

GW = "http://127.0.0.1:9"  # 不会有人接的端口


def _fixed_sleep_calls(path: Path) -> list:
    """列出本文件里所有真实的 time.sleep(<常量>) 调用 (AST 判据, 带所在函数)。

    #194/#200 的源码形态锚不能用文本 grep —— 判据自身的说明文字里也会
    出现那串字面量而误报。只认真实的调用节点, 且参数必须是常量
    (轮询步长 time.sleep(_RELAY_WAIT_STEP_S) 属变量, 不算硬 sleep)。

    返回 [(行号, 常量值, 所在函数名), ...]。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute)
                    and func.attr == "sleep"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "time"):
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                hits.append((node.lineno, node.args[0].value, fn.name))
    return sorted(hits)


# #194/#200 收尾纪律:
#   1) 本卡改动的投递 helper 内**不得**有任何常量 sleep (旧形态是
#      sleep(0.2) 等事件 emit 收尾 —— 必须换成带超时上限的轮询);
#   2) 本文件其余位置的常量 sleep 走显式白名单 —— 新增未登记的常量
#      sleep 即转红 (防止「换个写法又退回去」)。
# 白名单条目: (所在函数名, 常量值)
_ALLOWED_FIXED_SLEEPS = {
    # 既有形态: _wait_event 的轮询步长 (本身就是合规的轮询等待 helper)
    ("_wait_event", 0.05),
}

# 本卡改动的投递 helper (固定 sleep 的原宿主)
_RELAY_SEND_HELPERS = ("_send_404_with_probe",)


def _timeout_bound_ok(path: Path) -> bool:
    """AST 判据: 轮询等待 helper 的超时上限必须真的生效。

    变异反证 (PLAN §4 M12): 把 `deadline = time.time() + timeout` 换成
    更大的常量 ⇒ 轮询变成无上限 ⇒ 本判据转红 (而不是让用例挂死 ——
    挂死不是可诊断的失败形态)。

    判据 (两条都要满足, 缺一即判红):
      1) 轮询 helper 内存在 `time.time() + <名字>` 形态的 deadline
         赋值, 且该名字是 helper 的形参 (timeout);
      2) helper 内的循环 (While) 条件**真的引用了 deadline 变量名**。

    第 2 条是回流修复 F-2 (P2) 补的: 只验第 1 条时, 「保留 deadline
    赋值却把循环改成 `while True:`」这一同族形态仍判绿 —— 而那正是
    挂死形态 (轮询无上界 ⇒ 用例永不返回), 挂死不是可诊断的失败形态。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name != "_wait_relay_settled":
            continue
        params = {a.arg for a in fn.args.args}
        # 判据 1: 记下 deadline 变量名 (赋值目标), 供判据 2 比对
        deadline_names = set()
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.BinOp):
                continue
            if not isinstance(node.value.op, ast.Add):
                continue
            right = node.value.right
            if isinstance(right, ast.Name) and right.id in params:
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        deadline_names.add(tgt.id)
        if not deadline_names:
            continue
        # 判据 2: 循环条件必须以 deadline 为界 (否则 deadline 赋值是装饰)
        loops = [n for n in ast.walk(fn) if isinstance(n, ast.While)]
        if not loops:
            continue
        for loop in loops:
            refs = {n.id for n in ast.walk(loop.test)
                    if isinstance(n, ast.Name)}
            if not (refs & deadline_names):
                return False
        return True
    return False


def _led(tmp_path) -> Ledger:
    return Ledger(str(tmp_path / "ledger.db"))


def _http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "Not Found", Message(), None)


class _OkResp:
    """最小 200 响应替身 (_send_to_main 只读 status + read)。"""

    status = 200

    def getcode(self):
        return 200

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _events(led, task_id, etype):
    return led.conn.execute(
        "SELECT payload FROM mw_events WHERE task_id=? AND event_type=?",
        (task_id, etype)).fetchall()


# ════════════════════════════════════════════════════════════════
# #175 notify_task=None 敲门撞 404 留痕
# ════════════════════════════════════════════════════════════════

def _wait_event(led, task_id, etype, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        evs = _events(led, task_id, etype)
        if evs:
            return evs
        time.sleep(0.05)
    return []


def test_k3_knock_404_leaves_event_no_contract_write(tmp_path):
    """K3: notify_task=None + knock_ctx + 404 ⇒ 事件落账, 契约零变化。"""
    led = _led(tmp_path)
    try:
        led.put_contract("t_175", {"required_files": []})
        before = dict(led.conn.execute(
            "SELECT * FROM harvest_contracts WHERE task_id='t_175'"
        ).fetchone())

        def boom(rq, timeout=None):
            raise _http_error(f"{GW}/api/sessions/s_x/chat", 404)

        with mock.patch("urllib.request.urlopen", side_effect=boom):
            reaper_mod._send_to_main("s_x", "敲门", GW, "k", led.path,
                                      notify_task=None,
                                      knock_ctx={"task_id": "t_175"})
            evs = _wait_event(led, "t_175", "relay.notify_permanent_failure")
        assert evs, "knock 404 未留痕"
        p = json.loads(evs[0]["payload"])
        assert p["reason"] == "session_not_found" and p["http_status"] == 404
        assert p["channel"] == "card_binding_knock", p
        after = dict(led.conn.execute(
            "SELECT * FROM harvest_contracts WHERE task_id='t_175'"
        ).fetchone())
        assert after == before, "knock 留痕污染了 harvest_contracts"
    finally:
        led.close()


# ════════════════════════════════════════════════════════════════
# #171 404 死信前网关路由探测 (三分支)
# ════════════════════════════════════════════════════════════════

def _seed_card(led: Ledger, task_id: str):
    led.put_contract(task_id, {"required_files": []})
    led.record_initiator(task_id, "s_main")


# ── #194/#200: 轮询等待 (替代固定 sleep) ──────────────────────────
# 探测分支 ⇒ 该次投递期望落账的事件类型:
#   "ok"  路由健康 ⇒ 首败即死信 ⇒ relay.notify_permanent_failure
#   "404" 路由错配 ⇒ 退避 600s ⇒ relay.notify_gateway_misrouted
#   "err" 不可判定 ⇒ 保守走旧死信 ⇒ relay.notify_permanent_failure
_PROBE_EXPECT_EVENT = {
    "ok": "relay.notify_permanent_failure",
    "404": "relay.notify_gateway_misrouted",
    "err": "relay.notify_permanent_failure",
}

# 轮询参数: 超时上限 5s (与既有 deadline 同档) + 步长 0.05s。
_RELAY_WAIT_TIMEOUT_S = 5.0
_RELAY_WAIT_STEP_S = 0.05


def _wait_relay_settled(led: Ledger, task_id: str,
                        expect_event: str | None = None,
                        timeout: float = _RELAY_WAIT_TIMEOUT_S) -> bool:
    """轮询等待一次 relay 投递的结果落账 (issue #194/#200: 固定 sleep 改轮询)。

    落账口径 = 「状态列」(notify_dead_until 非空) ∧ 「事件行」(expect_event
    存在时)。旧写法只等状态列, 之后用固定 sleep(0.2) 等事件 emit 收尾 ——
    高负载 (89 文件逐文件并发) 下事件落账晚于 0.2s 窗口 ⇒ flaky。改轮询后
    与负载无关 (issue #194 正文 / #200 观察项)。

    超时后 break 退出 (不 fail-fast), 由调用方的既有断言以真实红信息失败,
    保留可诊断性 (PLAN-Q-20261102 §2.1-4)。返回是否在超时内落账。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = led.conn.execute(
            "SELECT notify_dead_until FROM harvest_contracts "
            "WHERE task_id=?", (task_id,)).fetchone()
        if row and row["notify_dead_until"]:
            if not expect_event:
                return True
            if _events(led, task_id, expect_event):
                return True
        time.sleep(_RELAY_WAIT_STEP_S)
    return False


def _send_404_with_probe(led: Ledger, task_id: str, probe):
    """目标 chat 恒 404; 探测 GET /api/sessions 行为由 probe 决定。

    probe: "ok" ⇒ 200; "404" ⇒ HTTPError 404; "err" ⇒ 连接异常。

    issue #194/#200 (2026-11-02): 收尾的固定 sleep(0.2) 改轮询等待 ——
    轮询目标从「状态列落账」扩为「状态列 ∧ 事件行落账」(带超时上限)。
    期望事件随探测分支而异 (PLAN-Q-20261102 §2.1-1):
      "ok"  路由健康 ⇒ 首败即死信 ⇒ relay.notify_permanent_failure
      "404" 路由错配 ⇒ 退避   ⇒ relay.notify_gateway_misrouted
      "err" 不可判定 ⇒ 保守死信 ⇒ relay.notify_permanent_failure
    """
    def boom(rq, timeout=None):
        if rq.full_url.endswith("/api/sessions") and rq.get_method() == "GET":
            if probe == "ok":
                return _OkResp()
            if probe == "404":
                raise _http_error(rq.full_url, 404)
            raise urllib.error.URLError("connection refused")
        raise _http_error(f"{GW}/api/sessions/s_main/chat", 404)

    with mock.patch("urllib.request.urlopen", side_effect=boom):
        reaper_mod._send_to_main("s_main", "报告", GW, "k", led.path,
                                 notify_task=task_id)
        _wait_relay_settled(led, task_id, _PROBE_EXPECT_EVENT.get(probe))


def test_k4_probe_healthy_keeps_dead_letter(tmp_path):
    """K4: 探测 200 (路由健康) + 目标 404 ⇒ 首败即死信 (旧语义逐字不变)。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_171a")
        before = time.time()
        _send_404_with_probe(led, "t_171a", "ok")
        r = dict(led.conn.execute(
            "SELECT * FROM harvest_contracts WHERE task_id='t_171a'"
        ).fetchone())
        assert r["notify_dead_until"] and \
            r["notify_dead_until"] > before + 86400 * 300, \
            "路由健康时 404 应仍首败即死信"
        assert _events(led, "t_171a", "relay.notify_permanent_failure")
        assert not _events(led, "t_171a", "relay.notify_gateway_misrouted")
    finally:
        led.close()


def test_k5_probe_404_backoff_not_dead_letter(tmp_path):
    """K5: 探测也 404 (路由错配) ⇒ 不死信, 退避 ~600s + misrouted 事件。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_171b")
        before = time.time()
        _send_404_with_probe(led, "t_171b", "404")
        r = dict(led.conn.execute(
            "SELECT * FROM harvest_contracts WHERE task_id='t_171b'"
        ).fetchone())
        assert r["notify_dead_until"], "路由错配应设临时退避"
        assert r["notify_dead_until"] < before + 3600, \
            f"退避过长 (dead_until={r['notify_dead_until']}), 误走死信?"
        evs = _events(led, "t_171b", "relay.notify_gateway_misrouted")
        assert evs, "路由错配未落 relay.notify_gateway_misrouted"
        assert not _events(led, "t_171b", "relay.notify_permanent_failure")
    finally:
        led.close()


def test_k6_probe_undecidable_falls_back_to_dead_letter(tmp_path):
    """K6: 探测连接异常 (不可判定) ⇒ 保守走旧死信 (不引入新静默)。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_171c")
        before = time.time()
        _send_404_with_probe(led, "t_171c", "err")
        r = dict(led.conn.execute(
            "SELECT * FROM harvest_contracts WHERE task_id='t_171c'"
        ).fetchone())
        assert r["notify_dead_until"] and \
            r["notify_dead_until"] > before + 86400 * 300, \
            "不可判定应保守走旧死信"
    finally:
        led.close()


# ════════════════════════════════════════════════════════════════
# #173 死信期卡 mismatch 敲门解耦
# ════════════════════════════════════════════════════════════════

def _seed_dead_mismatch_card(led: Ledger, task_id="t_173"):
    led.put_contract(task_id, {"required_files": []})
    led.record_initiator(task_id, "s_main")
    # 死信: dead_until = now + 1 年 ⇒ should_notify 恒 False (旧版吞敲门)
    led.conn.execute(
        "UPDATE harvest_contracts SET notify_dead_until=? WHERE task_id=?",
        (time.time() + 86400 * 365, task_id))
    led.conn.commit()


def test_k7_dead_letter_card_still_knocks_once(tmp_path):
    """K7: 死信卡 + mismatch ⇒ 仍敲一次 (去重与 relay 死信解耦)。"""
    led = _led(tmp_path)
    try:
        _seed_dead_mismatch_card(led)
        threshold = int(os.environ.get("MW_REAPER_STUCK_MIN", "30")) * 60
        # 前提自证: 旧去重源 (should_notify) 在死信期恒 False
        assert not led.should_notify("t_173", min_interval_s=threshold)
        results: list = []
        detail = {"actual_kanban_task": "t_other"}
        with mock.patch("mw.reaper._send_to_main", return_value=True) as sent:
            reaper_mod._knock_main_card_binding_mismatch(
                "t_173", led, results, detail)
            assert sent.call_count == 1, "死信期 mismatch 敲门被吞 (#173)"
            # 窗口内二次触发 ⇒ 不敲 (事件窗口兜刷屏)
            reaper_mod._knock_main_card_binding_mismatch(
                "t_173", led, results, detail)
        assert sent.call_count == 1, "窗口内重复敲门 (刷屏回归)"
        assert len(_events(led, "t_173",
                           "harvest.card_binding_mismatch_notified")) == 1
    finally:
        led.close()


# ════════════════════════════════════════════════════════════════
# #174 TODO.md 路径定位同源
# ════════════════════════════════════════════════════════════════

def test_k9_reaper_harvest_same_resolution_under_map(tmp_path):
    """K9: pw 与 workspace 分叉 + _map_path 生效 ⇒ 两侧解析同一路径。"""
    base = str(tmp_path)
    # 容器路径 /ctr/... 映射到宿主 /host/...
    with mock.patch.dict(os.environ, {"MW_PATH_MAP": f"{base}/ctr={base}/host"}):
        # pw 形态: 映射后的 pw/TODO.md 存在
        host_proj = tmp_path / "host" / "proj"
        host_proj.mkdir(parents=True)
        todo = host_proj / "TODO.md"
        todo.write_text("# TODO\n", encoding="utf-8")
        pw = f"{base}/ctr/proj"
        ws = f"{base}/ctr/proj/.tasks/t_x"  # 分叉的 task workspace
        contract = {"product_workspace": pw}
        from mw import reaper as rp
        p_reaper = rp._todo_path_for_task(ws, None, contract)
        p_harvest = _todo_path_for_harvest(pw, ws)
        assert p_reaper == p_harvest == str(todo), (p_reaper, p_harvest)

        # pw 不存在 ⇒ 两侧同走 workspace /.tasks/ 切分 (项目根)
        root2 = tmp_path / "host" / "proj2"
        root2.mkdir()
        todo2 = root2 / "TODO.md"
        todo2.write_text("# TODO\n", encoding="utf-8")
        ws2 = f"{base}/ctr/proj2/.tasks/t_y"
        p_reaper = rp._todo_path_for_task(ws2, None, None)
        p_harvest = _todo_path_for_harvest(None, ws2)
        assert p_reaper == p_harvest == str(todo2), (p_reaper, p_harvest)

        # 全判不出 ⇒ 两侧同为 None (harvest 语义: 校验放行)
        p_reaper = rp._todo_path_for_task(None, None, None)
        p_harvest = _todo_path_for_harvest(None, None)
        assert p_reaper is None and p_harvest is None


# ════════════════════════════════════════════════════════════════
# #172 新鲜度容差 env 化
# ════════════════════════════════════════════════════════════════

def test_k10_fresh_epsilon_env_override_and_fallback():
    """K10: MW_FRESH_EPSILON_S 覆盖生效; 非法/负值回落 2.0; 默认 2.0。"""
    assert FRESH_EPSILON == 2.0  # 既有锚 (test_issue170) 不动
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MW_FRESH_EPSILON_S", None)
        assert _fresh_epsilon_s() == 2.0
    with mock.patch.dict(os.environ, {"MW_FRESH_EPSILON_S": "10"}):
        assert _fresh_epsilon_s() == 10.0
    with mock.patch.dict(os.environ, {"MW_FRESH_EPSILON_S": "abc"}):
        assert _fresh_epsilon_s() == 2.0
    with mock.patch.dict(os.environ, {"MW_FRESH_EPSILON_S": "-1"}):
        assert _fresh_epsilon_s() == 2.0


def test_k10b_epsilon_env_changes_stale_verdict(tmp_path):
    """容差 env 直接影响判定: 偏斜 5s 的产物在 eps=10 下不误标陈旧。"""
    pws = tmp_path / "proj"
    pws.mkdir()
    f = pws / "out.md"
    f.write_text("x", encoding="utf-8")
    base = time.time()
    old = base - 5  # 模拟 NFS 时钟偏斜 5s (> 默认 2.0)
    os.utime(f, (old, old))
    contract = {"required_files": ["out.md"]}
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MW_FRESH_EPSILON_S", None)
        ok_default, _ = verify_physical(contract, str(tmp_path), str(pws),
                                        baseline_ts=base)
        assert ok_default is False, "默认容差 2.0 下应判陈旧 (基线行为)"
    with mock.patch.dict(os.environ, {"MW_FRESH_EPSILON_S": "10"}):
        ok_wide, _ = verify_physical(contract, str(tmp_path), str(pws),
                                     baseline_ts=base)
        assert ok_wide is True, "上调容差到 10 后不应误标陈旧"


# ════════════════════════════════════════════════════════════════
# #194/#200 轮询等待纪律 (变异反证锚)
# ════════════════════════════════════════════════════════════════

def test_k11a_wait_helper_has_timeout_bound():
    """源码形态锚: 轮询等待必须带超时上限 (PLAN §4 M12)。

    变异反证: 把 `deadline = time.time() + timeout` 换成常量 ⇒ 本用例
    **先**转红 (静态判据, 不等行为用例跑完) —— 否则行为用例会因轮询
    无上限而挂死, 挂死不是可诊断的失败形态。
    """
    assert _timeout_bound_ok(Path(__file__)), \
        "轮询等待缺超时上限 (PLAN §4 M12): deadline 未用 timeout 形参"


def test_k11_wait_helper_polls_event_and_state(tmp_path):
    """轮询等待的口径 = 状态列 ∧ 事件行, 且带超时上限。

    变异反证: 把 expect_event 忽略 (只等状态列) ⇒ 本用例转红
    —— 说明「轮询目标必须是事件行落账」这条纪律被真正钉住, 而不是
    换了个写法的固定 sleep。
    """
    # fail-fast 前提: 轮询必须带超时上限 —— 否则本用例会挂死而非转红
    assert _timeout_bound_ok(Path(__file__)), \
        "轮询等待缺超时上限 (PLAN §4 M12): deadline 未用 timeout 形参"
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_194_wait")
        led.conn.execute(
            "UPDATE harvest_contracts SET notify_dead_until=? "
            "WHERE task_id='t_194_wait'", (time.time() + 3600,))
        led.conn.commit()
        t0 = time.time()
        ok = _wait_relay_settled(led, "t_194_wait",
                                 "relay.notify_permanent_failure",
                                 timeout=0.6)
        elapsed = time.time() - t0
        assert ok is False, "只落状态列时轮询不应判成功"
        assert elapsed >= 0.5, f"轮询未等到超时上限就退出 (elapsed={elapsed:.3f})"
    finally:
        led.close()


def test_k11b_wait_helper_returns_true_when_event_lands(tmp_path):
    """状态列 + 事件行都落账 ⇒ 轮询立即返回 True (不必等满超时)。"""
    led = _led(tmp_path)
    try:
        _seed_card(led, "t_194_wait_ok")
        led.conn.execute(
            "UPDATE harvest_contracts SET notify_dead_until=? "
            "WHERE task_id='t_194_wait_ok'", (time.time() + 3600,))
        led.conn.commit()
        led.emit_event("relay.notify_permanent_failure", "t_194_wait_ok",
                       {"reason": "session_not_found"})
        t0 = time.time()
        ok = _wait_relay_settled(led, "t_194_wait_ok",
                                 "relay.notify_permanent_failure",
                                 timeout=5.0)
        elapsed = time.time() - t0
        assert ok is True, "事件行已落账时轮询应判成功"
        assert elapsed < 1.0, f"落账后未立即返回 (elapsed={elapsed:.3f})"
    finally:
        led.close()


def test_k11c_source_has_no_fixed_sleep_tail():
    """源码形态锚: 不得回退成「轮询 + 固定 sleep 收尾」。

    变异反证: 在 _send_404_with_probe 里重新加回 time.sleep(0.2) ⇒
    本用例转红 (issue #194 的原始形态)。

    判据走 AST (不是文本 grep) —— 文本 grep 会把本用例自己的说明文字
    也算进去而误报; AST 只认真实的 time.sleep(...) 调用节点。
    """
    hits = _fixed_sleep_calls(Path(__file__))
    # (1) 本卡改动的投递 helper 内零常量 sleep
    in_helper = [h for h in hits if h[2] in _RELAY_SEND_HELPERS]
    assert not in_helper, \
        f"投递 helper 内回退成固定 sleep 收尾 (issue #194): {in_helper}"
    # (2) 其余位置的常量 sleep 必须在白名单内
    unexpected = [h for h in hits
                  if h[2] not in _RELAY_SEND_HELPERS
                  and (h[2], h[1]) not in _ALLOWED_FIXED_SLEEPS]
    assert not unexpected, \
        f"新增未登记的固定 sleep (issue #194 纪律): {unexpected}"
    # (3) 轮询必须有超时上限 (PLAN §4 M12) —— 摘掉上限即转红, 不挂死
    assert _timeout_bound_ok(Path(__file__)), \
        "轮询等待缺超时上限 (PLAN §4 M12): deadline 未用 timeout 形参"
    src = Path(__file__).read_text(encoding="utf-8")
    assert "_wait_relay_settled" in src, "轮询等待 helper 缺失"
