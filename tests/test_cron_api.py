"""cron 定时任务端点测试 (s4 / 卡 t_da1c3a64)。

依据: docs/PLAN-CRON-20261027.md (R2 修订版) §6.1 测试要点
       + docs/PLAN-CRON-R2-20261027.md §6 (两层测试: 端点层 mock `_cron_bridge`
       + 桥层真 subprocess + 隔离 HERMES_HOME 假 store)。
       (以上两份是**过程文档**, 位于任务 workspace docs/, 不入仓 —— 干净克隆
        里无此路径; 本文件的用例注释自含覆盖点, 不依赖它们才能读懂)

覆盖:
  CT1   列表端点形状: jobs 字段齐全 + state 来自 effective_job_state
        (造 enabled=true + state=paused 的矛盾记录 → 必须显示 scheduled)
  CT2   pause → 列表 state=paused → resume → state=scheduled 往返 (桥层真跑)
  CT3   trigger 对 scheduled 作业 → 200 且 next_run_at 被 stamp;
        对终态作业 → 409 (桥 ValueError → code:409 映射)
  CT4   不存在的 job_id 三写操作 → 404 {"error": ...} (桥层 + 端点层各断一次)
  CT5   runs: 无执行记录 → runs: []; 有记录 → 字段含 status/started_at/error
  CT6   cron 数据源不可用时端点 503 而非崩进程, 四种触发各一条:
        ① TimeoutExpired ② FileNotFoundError ③ rc=1 / stdout 非 JSON
        ④ {"ok":false,"code":503}
  CT6b  错误映射表逐行 (PLAN §2.0): 桥 code 404/409/500 → 端点同码 + error 透传
  MV1   坏形状: 桥返回 {"jobs": null} / {"jobs": [{"state": 123}]} → 端点不崩,
        前端落错误态 / 未知徽标 (前端断言在 ui/tests/test_cron_view.mjs)
  MV3   桥协议回归: TimeoutExpired → 503 映射必须存在且落在 `_cron_bridge`
        函数体内 (D-1 修复: 锚限定作用域 + 行为断言 detail 可区分, 删分支必红)

安全红线 (BRIEF-s4 §4):
  * 测试**绝不对生产作业执行 pause/resume/trigger**;
  * 桥层夹具一律在**隔离 HERMES_HOME**(tmp_path)下跑, 不碰 /opt/data/cron。

跑法:
  - 直接: python3 tests/test_cron_api.py
  - pytest: pytest tests/test_cron_api.py -v

issue #137 (用例卫生): 桥层用例在本容器**缺 cron venv** 时一律
`pytest.skip(原因写明缺哪个解释器)`, 不再用裸 `return` —— 裸 return 会让
pytest 把「用例根本没执行」记成 passed, 报告面覆盖数虚高。
`pytest tests/test_cron_api.py -rs` 可直接看到每条 skip 的原因。
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mw.console as console  # noqa: E402


# ──────────────────────────────────────────────────────────────────
# 夹具工具
# ──────────────────────────────────────────────────────────────────

def _bridge_src() -> str:
    """桥脚本源码 (console 内嵌常量) —— 桥层测试直接复用, 不复制一份。"""
    return console._CRON_BRIDGE_SRC


def _run_bridge(req: dict, home: str, timeout: float = 30.0) -> dict:
    """在隔离 home 下真跑一次桥子进程, 返回解析后的 JSON。"""
    env = {**os.environ, "HERMES_HOME": home}
    py = os.environ.get("PYTHON_BIN", "/opt/hermes/.venv/bin/python3")
    proc = subprocess.run(
        [py, "-c", _bridge_src()],
        input=json.dumps(req, ensure_ascii=False),
        capture_output=True, text=True, timeout=timeout, env=env)
    out = (proc.stdout or "").strip()
    assert proc.returncode == 0, f"bridge rc={proc.returncode} stderr={proc.stderr[-500:]}"
    assert out, "bridge stdout 为空"
    # 协议: stdout 恰好一行 JSON
    assert len(out.splitlines()) == 1, f"bridge stdout 不是单行: {out[:200]!r}"
    return json.loads(out)


def _make_job(home: str, name: str = "cron-test", schedule: str = "0 3 * * *") -> str:
    """在隔离 home 下造一个作业, 返回 job_id。

    走 cron 公开 API create_job(schedule 收**字符串** cron 表达式)。
    """
    env = {**os.environ, "HERMES_HOME": home}
    py = os.environ.get("PYTHON_BIN", "/opt/hermes/.venv/bin/python3")
    code = (
        "import json,sys\n"
        "from cron.jobs import create_job\n"
        "j = create_job(prompt='cron test prompt', schedule=%r, name=%r)\n"
        "print(json.dumps({'id': j.get('id'), 'state': j.get('state')}, ensure_ascii=False))\n"
    ) % (schedule, name)
    proc = subprocess.run([py, "-c", code], capture_output=True, text=True,
                          timeout=60, env=env)
    assert proc.returncode == 0, f"create_job 失败: {proc.stderr[-500:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])["id"]


def _make_terminal_job(home: str) -> str:
    """造一个终态 (state=completed) 作业 —— trigger 必抛 ValueError → 409。"""
    env = {**os.environ, "HERMES_HOME": home}
    py = os.environ.get("PYTHON_BIN", "/opt/hermes/.venv/bin/python3")
    code = (
        "import json\n"
        "from cron.jobs import create_job, update_job\n"
        "j = create_job(prompt='done job', schedule='0 3 * * *', name='cron-done')\n"
        "update_job(j['id'], {'state': 'completed', 'enabled': False})\n"
        "print(json.dumps({'id': j['id']}))\n"
    )
    proc = subprocess.run([py, "-c", code], capture_output=True, text=True,
                          timeout=60, env=env)
    assert proc.returncode == 0, f"造终态作业失败: {proc.stderr[-500:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])["id"]


def _seed_execution(home: str, job_id: str, status: str = "failed",
                    error: str = "boom") -> None:
    """往隔离 home 的 executions 表插一行 (runs 取数面 = executions.db)。

    用 cron 公开 API: create_execution (写 claimed 行) + finish_execution
    (收口成 failed + error)。不手写 sqlite, 避免与库内 schema 漂移。
    """
    env = {**os.environ, "HERMES_HOME": home}
    py = os.environ.get("PYTHON_BIN", "/opt/hermes/.venv/bin/python3")
    code = (
        "from cron.executions import create_execution, finish_execution\n"
        "rec = create_execution(%r, source='cron')\n"
        "finish_execution(rec['id'], success=%s, error=%r)\n"
        "print('ok')\n"
    ) % (job_id, "False" if status == "failed" else "True", error)
    proc = subprocess.run([py, "-c", code], capture_output=True, text=True,
                          timeout=60, env=env)
    assert proc.returncode == 0, f"写执行记录失败: {proc.stderr[-500:]}"


def _isolated_home(tmp_path: Path) -> str:
    """隔离 HERMES_HOME —— cron 的 JOBS_FILE/executions.db 都锚在它下面。"""
    home = tmp_path / "cronhome"
    (home / "cron").mkdir(parents=True, exist_ok=True)
    return str(home)


def _cron_python() -> str:
    """桥层测试所用的解释器路径 (与 mw/console.py `_cron_bridge` 同口径)。

    运行时读取 env (不缓存): 用例可能 monkeypatch PYTHON_BIN 造场景。
    """
    return os.environ.get("PYTHON_BIN", "/opt/hermes/.venv/bin/python3")


def _no_venv_reason() -> str:
    """pytest.skip 的原因文案 —— 写明缺的是哪个解释器 (issue #137)。

    报告面 (`pytest -rs`) 直接展示这条文案, 读者据此知道是环境缺依赖
    (非 cron venv) 而非用例失败, 也知道该去装/指哪个解释器。
    """
    py = _cron_python()
    if not Path(py).exists():
        return (f"no cron venv: interpreter {py} 不存在 "
                f"(设 PYTHON_BIN 指向可 import cron 的 Hermes venv 解释器)")
    return (f"no cron venv: interpreter {py} 存在但 import cron.jobs 失败 "
            f"(该解释器不是 Hermes venv —— 设 PYTHON_BIN 指向可 import cron 的解释器)")


def _have_venv_cron() -> bool:
    """本容器是否有可 import cron 的 venv 解释器 (桥层测试的前提)。"""
    py = _cron_python()
    if not Path(py).exists():
        return False
    try:
        r = subprocess.run([py, "-c", "import cron.jobs"], capture_output=True,
                           text=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────
# CT6b / CT6 / MV3: 错误映射表 + 503 四触发面 (mock 边界 = _cron_bridge)
# ──────────────────────────────────────────────────────────────────

def _patch_bridge(monkeypatch, payload):
    """把 `_cron_bridge` 替换成返回固定 payload 的桩 (端点层 mock 边界)。"""
    monkeypatch.setattr(console, "_cron_bridge",
                        lambda req, timeout: dict(payload))


def test_ct6b_error_mapping_table(monkeypatch):
    """CT6b: 桥 code 404/409/500 → 端点同码 + error 透传 (PLAN §2.0 逐行)。"""
    cases = [
        (404, "任务已不存在"),
        (409, "状态冲突"),
        (500, "内部错误"),
    ]
    for code, msg in cases:
        _patch_bridge(monkeypatch, {"ok": False, "code": code, "error": msg})
        rcode, body = console.api_cron_jobs()
        assert rcode == code, f"桥 code={code} → 端点 {rcode}"
        assert body.get("error") == msg, f"error 未透传: {body}"


def test_ct6b_503_maps_to_fixed_message(monkeypatch):
    """CT6b: 桥 code:503 → 端点 503 + 固定文案「cron 数据源不可用」。"""
    _patch_bridge(monkeypatch, {"ok": False, "code": 503, "error": "随便什么"})
    rcode, body = console.api_cron_jobs()
    assert rcode == 503
    assert body.get("error") == "cron 数据源不可用"


def test_ct6b_unknown_code_falls_back_to_503(monkeypatch):
    """CT6b: 未知 code → 归 503 (不裸奔成 200 + 空体)。"""
    _patch_bridge(monkeypatch, {"ok": False, "code": 418, "error": "teapot"})
    rcode, body = console.api_cron_jobs()
    assert rcode == 503
    assert body.get("error") == "cron 数据源不可用"


def test_ct6_timeout_maps_503(monkeypatch):
    """CT6①: 子进程超时 (TimeoutExpired) → 503, 不裸抛。

    console._cron_bridge 内部 `import subprocess as _sp` → 落到模块属性,
    故 monkeypatch subprocess.run 即命中。
    """
    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="py", timeout=3)
    monkeypatch.setattr(subprocess, "run", _timeout)
    r = console._cron_bridge({"action": "list"}, 1.0)
    assert r["ok"] is False and r["code"] == 503, r
    assert r["error"] == "cron 数据源不可用"


def test_ct6_missing_interpreter_maps_503(monkeypatch):
    """CT6②: 解释器不存在 (FileNotFoundError) → 503。"""
    monkeypatch.setenv("PYTHON_BIN", "/nonexistent/python3")
    r = console._cron_bridge({"action": "list"}, 5.0)
    assert r["ok"] is False and r["code"] == 503, r
    assert r["error"] == "cron 数据源不可用"


def test_ct6_bad_output_maps_503(monkeypatch):
    """CT6③: rc≠0 / stdout 非 JSON → 503。"""
    class _Proc:
        def __init__(self, rc, out, err=""):
            self.returncode = rc
            self.stdout = out
            self.stderr = err
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Proc(1, "", "boom"))
    r = console._cron_bridge({"action": "list"}, 5.0)
    assert r["ok"] is False and r["code"] == 503, r
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _Proc(0, "not json at all"))
    r = console._cron_bridge({"action": "list"}, 5.0)
    assert r["ok"] is False and r["code"] == 503, r


def test_ct6_bridge_503_maps_503(monkeypatch):
    """CT6④: 桥返回 code:503 (venv 内 import cron 失败) → 端点 503。"""
    _patch_bridge(monkeypatch, {"ok": False, "code": 503,
                                 "error": "cron 数据源不可用"})
    rcode, body = console.api_cron_jobs()
    assert rcode == 503
    assert body.get("error") == "cron 数据源不可用"


def _cron_bridge_body() -> str:
    """`_cron_bridge` 函数体源码 (D-1: 锚必须限定作用域, 不许扫全文件)。

    console.py 里 reaper 等先例同样含 `_sp.TimeoutExpired` 字样, 全文件
    字符串锚会被无关命中 —— 变异(删掉桥的超时分支)后锚仍绿即回归锁失效。
    """
    import inspect
    return inspect.getsource(console._cron_bridge)


def test_mv3_timeout_mapping_present():
    """MV3 (变异反证): 超时 → 503 的映射必须真实存在于 `_cron_bridge` 内。

    D-1 修复 (卡 t_59874c29): 旧版锚扫 console.py 全文, 被 reaper 先例
    (L4982/L5046 的 `except _sp.TimeoutExpired`) 无关命中, 删掉桥的超时
    分支后 25 例仍全绿 —— 锚无牙齿。现改为:
      ① 作用域锚: `_sp.TimeoutExpired` 必须出现在 inspect.getsource
         (`_cron_bridge`) 的函数体内, 无关代码救不回来;
      ② 行为断言 (见 test_mv3_timeout_branch_behavior): 真构造超时,
         断言 detail 来自超时分支的独有文案 "bridge timeout"。
    两条自证均在隔离 clone 实测: 删 `except _sp.TimeoutExpired:` 分支
    → 本用例与行为用例双双转红; `git checkout --` 复原后全绿。
    """
    body = _cron_bridge_body()
    assert "_sp.TimeoutExpired" in body, \
        "_cron_bridge 函数体内缺 TimeoutExpired 捕获 (MV3 回归)"
    assert "bridge timeout" in body, "超时分支缺可区分归因标记 (detail)"


def test_mv3_timeout_branch_behavior(monkeypatch):
    """MV3 行为面: 桥超时归因必须来自**超时分支**, 而非泛化兜底 except。

    兜底 `except Exception` 的 error 同为「cron 数据源不可用」, 仅靠
    error 文案删分支不会红; detail 是超时分支独有的 "bridge timeout",
    兜底分支的 detail 是 str(e) —— 两者可区分, 删分支后本用例必红。
    """
    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="py", timeout=3)
    monkeypatch.setattr(subprocess, "run", _timeout)
    r = console._cron_bridge({"action": "list"}, 1.0)
    assert r["ok"] is False and r["code"] == 503, r
    assert r.get("detail") == "bridge timeout", \
        f"detail 应来自超时分支, 实得 {r.get('detail')!r} (超时捕获可能被删)"


def _reset_console_log_handlers():
    """摘掉 mw.console logger 的全部 handler (reload 前后各一次)。

    logging.getLogger 是进程级单例: 模块 reload 后 `if not handlers` 守卫
    会跳过重新挂 handler, 旧 handler 仍指向旧落点 —— 必须显式清空才能让
    「落点由 MW_HOME 派生」这条断言真正生效。
    """
    import logging
    lg = logging.getLogger("mw.console")
    for h in list(lg.handlers):
        lg.removeHandler(h)
        h.close()


def test_mv143_cron_bridge_error_tail_is_singleline_and_bounded(monkeypatch,
                                                                tmp_path):
    """issue #143: rc≠0 分支的 stderr 尾巴仍单行化 + ≤500 字符 (#135 不回退)。

    MV-143-B (去掉 [-500:] 截断) / MV-143-C (去掉 split() 单行化) 必红。
    落点在 import 期由 MW_HOME 派生 ⇒ 把 MW_HOME 指到 tmp_path 后 reload
    模块, 同时验证「落点路径由 MW_HOME 派生」(一个测试两条 AC); finally
    恢复原 env 并再 reload, 不污染同会话后续用例。
    """
    orig_mw_home = os.environ.get("MW_HOME")
    monkeypatch.setenv("MW_HOME", str(tmp_path))
    try:
        _reset_console_log_handlers()
        importlib.reload(console)

        class _Proc:
            returncode = 7
            stdout = ""
            # 尾部刻意留 "\nY"*200: 若去掉 split() (MV-143-C), [-500:] 截出的
            # 尾巴会含换行 ⇒ 日志变多行, 下面的单行断言必红; 折叠串 618 字符
            # > 500 ⇒ 若去掉 [-500:] (MV-143-B), 长度断言必红。
            stderr = "boom\nsecond line\n" + "X" * 400 + "\n" + "Y" * 200

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
        r = console._cron_bridge({"action": "list"}, 5.0)
        assert r["ok"] is False and r["code"] == 503, r
        # 响应体不含 stderr 原文 (#135: 只进日志面, 不进 HTTP 响应)
        dumped = json.dumps(r, ensure_ascii=False)
        assert "boom" not in dumped, r
        assert "second line" not in dumped, r
        assert "XXXX" not in dumped, r
        assert "YYYY" not in dumped, r
        # 落点文件存在 ⇒ 路径确由 MW_HOME 派生 (AC-143-1 的单元层投影)
        log_path = tmp_path / "logs" / "console.log"
        assert log_path.exists(), \
            f"落点未随 MW_HOME 落到 {log_path} (handler 可能未挂文件)"
        raw = log_path.read_text(encoding="utf-8")
        assert raw.count("[cron-bridge] rc=7") == 1, raw
        tail = raw.split("[cron-bridge] rc=7 stderr_tail: ", 1)[1].rstrip("\n")
        # 单行化 (MV-143-C 牙齿): 整条日志记录只占一行 (formatter 换行之外
        # 不得再有换行); 原文多行 stderr 已被 split() 折叠成一行。
        assert len(raw.rstrip("\n").splitlines()) == 1, \
            f"日志记录未单行化 ({len(raw.splitlines())} 行)"
        assert "\n" not in tail, f"尾巴未单行化: {tail[:80]!r}"
        # ≤500 字符 (MV-143-B 牙齿): 折叠串 618 字符, 只准留尾部 500
        assert len(tail) <= 500, f"尾巴 {len(tail)} 字符, 超 500 上限"
        assert tail.endswith("Y" * 200), "截断未保留尾部 (应留最后一段原文)"
    finally:
        if orig_mw_home is None:
            os.environ.pop("MW_HOME", None)
        else:
            os.environ["MW_HOME"] = orig_mw_home
        _reset_console_log_handlers()
        importlib.reload(console)


def test_issue148_obs2_stderr_tail_must_fold_via_split_join():
    """issue #148 OBS-2: stderr 尾巴**必须经 join 折叠 (单行化)**, 源码形态钉死。

    既有行为断言 (上一条用例) 只查「无换行 + ≤500 + 保留尾部」—— 任何能把
    多行压成单行的写法都能通过 (如 `.replace("\\n", " ")` / `re.sub(r"\\s+",
    " ", ...)`), 折叠**语义**被换掉而行为断言看不见 (MV-148-C1/C4 实测漏检)。
    本用例是显式**源码形态断言**: 锚在 `_cron_bridge` 函数体内 `_tail` 赋值
    语句上, 钉死「切分 → join 折叠 → 截断」完整管线三特征并存:
      B1 `" ".join(`   —— join 折叠必须在场
      B2 `.split()`    —— 折叠输入必须是**无参切分**的 token 序列
                         (切掉所有空白控制符; `.split(" ")` 只切空格不切换行,
                          特征串带空参正是为了缩小误匹配面, PLAN-I Q-C)
      B3 `[-500:]`     —— 截断锚 (既有行为一并钉住)
    变异反证: MV-148-C1 (join→replace) / C4 (join→re.sub) 本用例红而行为断言
    不红 (形态断言的核心价值); C2 (删折叠) / C3 (splitlines[0]) 两者都红。
    """
    import inspect
    import re as _re
    src = inspect.getsource(console._cron_bridge)
    # 定位 `_tail = ` 赋值行 (函数体内唯一; 找不到 ⇒ 重构后测试显式失效而非静默)
    tail_lines = [ln for ln in src.splitlines()
                  if _re.search(r"^\s*_tail\s*=", ln)]
    assert len(tail_lines) == 1, \
        f"`_tail =` 赋值应恰 1 处, 实得 {len(tail_lines)}: {tail_lines}"
    line = tail_lines[0]
    assert '" ".join(' in line, \
        f"B1 缺失: 尾巴未经 `\" \".join(` 折叠 (单行化管线破): {line.strip()}"
    assert ".split()" in line, \
        f"B2 缺失: join 的输入未经无参 `.split()` 切分 (折叠语义破): {line.strip()}"
    assert "[-500:]" in line, \
        f"B3 缺失: 尾巴未经 `[-500:]` 截断 (≤500 契约破): {line.strip()}"


def test_ct6b_endpoint_never_crashes_on_bridge_failure(monkeypatch):
    """端点层: 桥各种失败面都归一为 (code, body), 绝不抛异常。"""
    for payload in ({"ok": False, "code": 404, "error": "x"},
                    {"ok": False, "code": 409, "error": "y"},
                    {"ok": False, "code": 500, "error": "z"},
                    {"ok": False, "code": 503, "error": "w"}):
        _patch_bridge(monkeypatch, payload)
        rcode, body = console.api_cron_jobs()
        assert rcode in (404, 409, 500, 503)
        assert isinstance(body, dict) and body.get("error")


# ──────────────────────────────────────────────────────────────────
# CT1: 列表端点形状 (端点层 mock + 桥层真跑)
# ──────────────────────────────────────────────────────────────────

def test_ct1_endpoint_shape(monkeypatch):
    """CT1 端点层: GET /api/cron/jobs 响应形状符合 PLAN §2.1。"""
    job = {
        "id": "abc123",
        "name": "每日自检",
        "prompt": "x" * 300,
        "schedule_display": "每天 08:00",
        "schedule_expr": "0 8 * * *",
        "state": "scheduled",
        "next_run_at": "2026-10-28T08:00:00+08:00",
        "last_run_at": "2026-10-27T08:00:12+08:00",
        "paused_reason": None,
        "latest_execution": {"status": "completed", "started_at": "2026-10-27T08:00:12+08:00",
                              "finished_at": "2026-10-27T08:00:40+08:00", "error": None},
    }
    _patch_bridge(monkeypatch, {"ok": True, "home": console._cron_home(),
                                 "count": 1, "jobs": [job]})
    rcode, body = console.api_cron_jobs()
    assert rcode == 200
    assert "jobs" in body and "ticker" in body
    j = body["jobs"][0]
    for key in ("id", "name", "prompt", "schedule_display", "schedule_expr",
                "state", "next_run_at", "last_run_at", "paused_reason",
                "latest_execution"):
        assert key in j, f"缺字段 {key}"
    assert j["latest_execution"]["status"] == "completed"


def test_ct1_bad_shape_jobs_null(monkeypatch):
    """MV1: 桥返回 {"jobs": null} → 端点 500 (不是 200 + 空体)。"""
    _patch_bridge(monkeypatch, {"ok": True, "home": console._cron_home(),
                                 "jobs": None})
    rcode, body = console.api_cron_jobs()
    assert rcode == 500
    assert body.get("error")


def test_ct1_home_mismatch_is_500(monkeypatch):
    """同源双保险: 桥回带 home 与 console 期望不等 → 500。"""
    _patch_bridge(monkeypatch, {"ok": True, "home": "/somewhere/else",
                                 "jobs": []})
    rcode, body = console.api_cron_jobs()
    assert rcode == 500
    assert "home" in str(body.get("error", "")).lower() or \
        "不一致" in str(body.get("error", ""))


def test_ct1_invalid_job_id_rejected_before_bridge(monkeypatch):
    """job_id 白名单: 非法 id 直接 422, 不落子进程 (无注入面)。"""
    called = {"n": 0}

    def _spy(req, timeout):
        called["n"] += 1
        return {"ok": True, "home": console._cron_home(), "jobs": []}
    monkeypatch.setattr(console, "_cron_bridge", _spy)
    rcode, body = console.api_cron_runs("bad id; rm -rf /", 3)
    assert rcode == 422
    assert called["n"] == 0, "非法 job_id 不该落子进程"


def test_ct4_unknown_action_rejected(monkeypatch):
    """action 白名单: 未知 action → 422, 不落子进程。"""
    called = {"n": 0}

    def _spy(req, timeout):
        called["n"] += 1
        return {"ok": True, "home": console._cron_home(), "jobs": []}
    monkeypatch.setattr(console, "_cron_bridge", _spy)
    rcode, _ = console.api_cron_action("abc", "delete")
    assert rcode == 422
    assert called["n"] == 0


# ──────────────────────────────────────────────────────────────────
# 桥层测试 (真 subprocess + 隔离 HERMES_HOME) —— 需本容器有 cron venv
#
# issue #137: 缺 venv 时一律 pytest.skip(原因写明缺哪个解释器), 不用裸 return。
# 裸 return 会让 pytest 把「用例根本没执行」记成 passed ⇒ 报告面覆盖数虚高,
# 掩盖「桥层实际未被执行」的事实。`pytest -rs` 可直接看到 skip 原因。
# ──────────────────────────────────────────────────────────────────

def test_bridge_layer_ct1_state_derived_from_effective_state(tmp_path):
    """CT1 桥层: state 取自 effective_job_state —— enabled=true + state=paused
    的矛盾记录必须显示 scheduled (不信 record['state'])。"""
    if not _have_venv_cron():
        # issue #137: 裸 return 会把「桥层未执行」记成 passed —— 报告面覆盖数虚高。
        # 改 pytest.skip, 原因写明缺哪个解释器, 让报告面如实计 skipped。
        pytest.skip(_no_venv_reason())
    home = _isolated_home(tmp_path)
    job_id = _make_job(home)
    # 直接改 jobs.json 造矛盾记录: enabled=true 但 state=paused
    jobs_file = Path(home) / "cron" / "jobs.json"
    data = json.loads(jobs_file.read_text(encoding="utf-8"))
    for rec in data["jobs"]:
        if rec["id"] == job_id:
            rec["state"] = "paused"
            rec["paused_at"] = "2026-10-27T00:00:00+00:00"
    jobs_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    res = _run_bridge({"action": "list"}, home)
    assert res["ok"] is True
    assert res["home"] == str(Path(home).resolve()) or res["home"] == home
    job = res["jobs"][0]
    assert job["state"] == "scheduled", f"矛盾记录应派生态为 scheduled, 实得 {job['state']}"


def test_bridge_layer_ct2_pause_resume_roundtrip(tmp_path):
    """CT2: pause → state=paused → resume → state=scheduled (桥层真跑)。"""
    if not _have_venv_cron():
        # issue #137: 裸 return 会把「桥层未执行」记成 passed —— 报告面覆盖数虚高。
        # 改 pytest.skip, 原因写明缺哪个解释器, 让报告面如实计 skipped。
        pytest.skip(_no_venv_reason())
    home = _isolated_home(tmp_path)
    job_id = _make_job(home)
    paused = _run_bridge({"action": "pause", "job_id": job_id}, home)
    assert paused["ok"] is True
    assert paused["job"]["state"] == "paused", paused["job"]["state"]
    listed = _run_bridge({"action": "list"}, home)
    assert listed["jobs"][0]["state"] == "paused"
    resumed = _run_bridge({"action": "resume", "job_id": job_id}, home)
    assert resumed["ok"] is True
    assert resumed["job"]["state"] == "scheduled", resumed["job"]["state"]


def test_bridge_layer_ct3_trigger_stamps_next_run(tmp_path):
    """CT3: trigger 对 scheduled 作业 → ok 且 next_run_at 被 stamp。"""
    if not _have_venv_cron():
        # issue #137: 裸 return 会把「桥层未执行」记成 passed —— 报告面覆盖数虚高。
        # 改 pytest.skip, 原因写明缺哪个解释器, 让报告面如实计 skipped。
        pytest.skip(_no_venv_reason())
    home = _isolated_home(tmp_path)
    job_id = _make_job(home)
    before = _run_bridge({"action": "list"}, home)["jobs"][0]["next_run_at"]
    res = _run_bridge({"action": "trigger", "job_id": job_id}, home)
    assert res["ok"] is True
    after = res["job"]["next_run_at"]
    assert after != before, "trigger 未 stamp next_run_at"


def test_bridge_layer_ct3_terminal_trigger_is_409(tmp_path):
    """CT3: 终态作业 trigger → 409 (桥 ValueError → code:409 映射)。"""
    if not _have_venv_cron():
        # issue #137: 裸 return 会把「桥层未执行」记成 passed —— 报告面覆盖数虚高。
        # 改 pytest.skip, 原因写明缺哪个解释器, 让报告面如实计 skipped。
        pytest.skip(_no_venv_reason())
    home = _isolated_home(tmp_path)
    job_id = _make_terminal_job(home)
    res = _run_bridge({"action": "trigger", "job_id": job_id}, home)
    assert res["ok"] is False
    assert res["code"] == 409, f"期望 409, 实得 {res}"
    assert res.get("error")


def test_bridge_layer_ct4_unknown_id_is_404(tmp_path):
    """CT4: 不存在的 job_id 三写操作 → 404。"""
    if not _have_venv_cron():
        # issue #137: 裸 return 会把「桥层未执行」记成 passed —— 报告面覆盖数虚高。
        # 改 pytest.skip, 原因写明缺哪个解释器, 让报告面如实计 skipped。
        pytest.skip(_no_venv_reason())
    home = _isolated_home(tmp_path)
    for action in ("pause", "resume", "trigger"):
        res = _run_bridge({"action": action, "job_id": "nosuchjob"}, home)
        assert res["ok"] is False, f"{action} 应对不存在 id 返回失败"
        assert res["code"] == 404, f"{action} 期望 404, 实得 {res}"


def test_bridge_layer_ct5_runs(tmp_path):
    """CT5: runs 无记录 → []; 有记录 → 含 status/started_at/error。"""
    if not _have_venv_cron():
        # issue #137: 裸 return 会把「桥层未执行」记成 passed —— 报告面覆盖数虚高。
        # 改 pytest.skip, 原因写明缺哪个解释器, 让报告面如实计 skipped。
        pytest.skip(_no_venv_reason())
    home = _isolated_home(tmp_path)
    job_id = _make_job(home)
    empty = _run_bridge({"action": "runs", "job_id": job_id, "limit": 3}, home)
    assert empty["ok"] is True
    assert empty["runs"] == []
    assert empty["limit"] == 3
    _seed_execution(home, job_id, status="failed", error="boom")
    rows = _run_bridge({"action": "runs", "job_id": job_id, "limit": 3}, home)
    assert len(rows["runs"]) >= 1
    r = rows["runs"][0]
    for key in ("id", "status", "started_at", "finished_at", "error",
                "scheduled_instant"):
        assert key in r, f"缺字段 {key}"
    assert r["status"] == "failed"
    assert r["error"] == "boom"


def test_bridge_layer_runs_unknown_job_is_404(tmp_path):
    """CT4 (runs 侧): 不存在的 job_id → 404。"""
    if not _have_venv_cron():
        # issue #137: 裸 return 会把「桥层未执行」记成 passed —— 报告面覆盖数虚高。
        # 改 pytest.skip, 原因写明缺哪个解释器, 让报告面如实计 skipped。
        pytest.skip(_no_venv_reason())
    home = _isolated_home(tmp_path)
    res = _run_bridge({"action": "runs", "job_id": "nosuchjob", "limit": 3}, home)
    assert res["ok"] is False and res["code"] == 404, res


def test_bridge_layer_never_touches_real_store(tmp_path):
    """红线自检: 桥层夹具全程在隔离 home 下, 生产 /opt/data/cron 不被改。"""
    if not _have_venv_cron():
        # issue #137: 裸 return 会把「桥层未执行」记成 passed —— 报告面覆盖数虚高。
        # 改 pytest.skip, 原因写明缺哪个解释器, 让报告面如实计 skipped。
        pytest.skip(_no_venv_reason())
    home = _isolated_home(tmp_path)
    _make_job(home)
    # 隔离 home 下的 jobs.json 存在
    assert (Path(home) / "cron" / "jobs.json").exists()
    # 且 home 值确实不同于生产 home (兜底断言)
    assert home != "/opt/data"


def test_endpoint_limit_clamp(monkeypatch):
    """CT5 端点层: limit 非数字 → 422; 超界 → clamp 到 [1,10]。"""
    seen = {}

    def _spy(req, timeout):
        seen.update(req)
        return {"ok": True, "home": console._cron_home(), "runs": [], "limit": 3}
    monkeypatch.setattr(console, "_cron_bridge", _spy)
    rcode, _ = console.api_cron_runs("abc", "abc")
    assert rcode == 422
    rcode, body = console.api_cron_runs("abc", 999)
    assert rcode == 200 and body["limit"] == 10
    rcode, body = console.api_cron_runs("abc", 0)
    assert rcode == 200 and body["limit"] == 1
    rcode, body = console.api_cron_runs("abc", None)
    assert rcode == 200 and body["limit"] == 3


def test_bridge_source_has_no_injection_surface():
    """注入面自检: 桥脚本不得有 shell=True / 字符串拼接进 python 源码。"""
    src = console._CRON_BRIDGE_SRC
    assert "shell=True" not in src
    assert "os.system" not in src
    assert "eval(" not in src
    assert "exec(" not in src
    # 桥只准用 cron 公开 API: 不得出现 cron 私有符号调用
    for priv in ("_normalize_job_record", "_jobs_lock", "_claim_is_live",
                 "_parse_aware", "_hermes_now", "load_jobs"):
        assert priv not in src, f"桥脚本引用了私有/未授权符号 {priv}"


def test_console_registers_cron_routes():
    """路由自检: 5 个端点在 console.py 注册, 且 POST 分支在 body 预读之前。"""
    src = Path(console.__file__).read_text(encoding="utf-8")
    assert '"/api/cron/jobs"' in src
    assert 'pause|resume|trigger' in src
    # POST 分支的 cron 路由必须排在 body 预读 (Content-Length 读取) 之前
    post_idx = src.index("def do_POST")
    post_block = src[post_idx:post_idx + 4000]
    cron_idx = post_block.index("/api/cron/jobs/")
    body_idx = post_block.index('int(self.headers.get("Content-Length", 0))')
    assert cron_idx < body_idx, "cron POST 路由必须先于 body 预读 (否则挂死)"


if __name__ == "__main__":
    # pytest 已是模块级 import (issue #137 的 pytest.skip 需要), 此处直接用。
    sys.exit(pytest.main([__file__, "-v"]))
