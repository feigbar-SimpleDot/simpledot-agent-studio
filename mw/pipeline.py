"""派发管道 (1A 边界, 2026-10-03 减法重构)。

mw = 通讯中间件。本模块只做三件事:
  1. 校验 (registry / 工作区白名单 / 重复派发护栏)
  2. 记账 (ledger: contract / session)
  3. 起 worker (HTTP POST 到 worker gateway) + 写 spawn marker

**mw 不做的事 (1A 铁律)**:
  - ❌ 不调 hermes kanban create/claim/archive —— 建卡/claim/complete 是 main 的事
  - ❌ 不写项目文件 (TODO.md 由 main 播种, worker 只改『状态』『结果』)
  - ❌ 不执行外部命令 (docker/local runner 已删; 只走 worker gateway HTTP API)
  - ❌ 不决策 (结算/推进/重派全归 main)

任务内容契约在共享卷的 TODO.md 里 (main 播种), worker 直接读文件,
不穿过代码链 —— 根治 "continue task" 占位符。

TODO.md 路径约定 (统一后, 2026-10-03):
  - 项目任务: <product_workspace>/TODO.md   (main 播种, reaper 读这里)
  - 非项目任务: <workspace>/TODO.md          (worker 起手自建)
  - .clarify.json 永远在 task workspace (<workspace>/.clarify.json),
    不落项目根, 避免下游 reviewer 把协议信号当产物。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote

from .schemas import TaskManifest
from .ledger import Ledger

SESSION_ID_RE = re.compile(r"session_id:\s*([0-9A-Za-z_\-]+)")


class SubmitError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def load_registry(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def _map_path(p: str) -> str:
    """容器路径 → 宿主机路径 (MW_PATH_MAP='/share_workspace=/host/path')。"""
    m = os.environ.get("MW_PATH_MAP", "")
    if m:
        c, _, h = m.partition("=")
        if p.startswith(c):
            return h + p[len(c):]
    return p


def validate_dispatch(payload: dict, registry: dict) -> TaskManifest:
    """闸一 schema; 闸二 registry (堵幽灵 profile); 闸三 工作区白名单。零决策。"""
    try:
        m = TaskManifest.model_validate(payload)
    except Exception as e:
        raise SubmitError(422, f"schema 拒绝: {e}")
    if m.target.profile not in registry:
        raise SubmitError(
            422, f"幽灵 profile 拒绝: '{m.target.profile}' 不在注册表 "
                 f"{sorted(registry)}")
    if m.project_workspace:
        allowed = os.environ.get("MW_ALLOWED_WS_PREFIX", "/share_workspace")
        norm = os.path.normpath(m.project_workspace)
        if not (norm == allowed or norm.startswith(allowed.rstrip("/") + "/")):
            raise SubmitError(
                422, f"project_workspace 越权拒绝: {m.project_workspace} "
                     f"不在 {allowed} 之下")
    return m


# ── worker prompt (统一 TODO.md 契约格式, 2026-10-03) ─────────────
# 只有一种契约格式, 一种状态写法:
#   ## 当前任务 / ## Current Task
#   - 状态 / status: working|blocked|completed|failed
# worker 只改『状态』『结果』两行; 起手必写 working, 否则 reaper 永不结算。
#
# 2026-10-06 扩展: 步骤可跳过 (路径 2 — 留痕路径)。
# 协议加 - 跳过: 是 [原因: ...] 字段。
# main / 产品经理决策该步跳过时,在播种的 TODO.md 写此字段;
# worker 起手读到立即 completed, 不调 LLM (或极简一句 ack)。
# 语义: 卡在 ledger 还在, 但产物为空, 留 audit 痕迹。
#
# 2026-10-17 扩展 (Issue #41, s4-prompt-i18n): 双语模板族。
# 原内联 f-string 拆为 mw/worker_contract_prompt.<lang>.md 两个模板文件,
# launch 时按 mw/i18n.resolve_lang() 选其一; zh 模板逐字节回退到原内联
# 字符串的输出(测试钉住),en 模板同协议语义但语种切换。
# 模板用 str.format 替换 {todo_path} / {out_dir} / {workspace} 占位符;
# 模板内的字面 JSON 示例 {{"question": ...}} 已转义为 `{{ ... }}`,
# 防止被 format 误吃。解析端保持双键别名向后兼容(zh/en 段头 + 字段名,
# 详见 mw/todo.py BILINGUAL_*_ALIASES)。

# 默认模板族路径:按部署语言(架构 §4.3 + §3.2, 与 mw/console.py
# SOUL_TEMPLATE 同模式)选模板;env override 优先,行为兼容测试。
_PROMPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROMPT_TEMPLATES = {
    "zh-cn": "worker_contract_prompt.zh-cn.md",
    "en":    "worker_contract_prompt.en.md",
}


def _resolve_prompt_template_path() -> str:
    """按 mw/i18n.resolve_lang() 选模板文件路径,env override 优先。

    优先级: MW_WORKER_PROMPT_TEMPLATE env > resolve_lang() 选模板。
    解析失败 / 文件不存在 → 退化到 zh 模板(逐字节与原内联字符串等价,
    守 zh 部署零回归锚 §6.1)。
    """
    override = os.environ.get("MW_WORKER_PROMPT_TEMPLATE", "").strip()
    if override:
        return override
    try:
        from .i18n import resolve_lang
        lang = resolve_lang()
    except Exception:
        lang = "en"  # 解析失败时退化到 en 模板
    name = _PROMPT_TEMPLATES.get(lang) or _PROMPT_TEMPLATES["zh-cn"]
    return os.path.join(_PROMPT_DIR, name)


def worker_contract_prompt(todo_path: str, workspace: str,
                          product_workspace: str | None = None) -> str:
    """worker 提示词: 任务契约在 TODO.md, worker 读它干活。

    双语模板 (Issue #41, 2026-10-17): 从 `mw/worker_contract_prompt.<lang>.md`
    读模板,按 mw/i18n.resolve_lang() 选语种,zh 行为逐字节回退到原内联
    f-string 输出(详见 tests/fixtures/worker_prompt_baseline_zh.txt 钉锚)。

    所有权边界 (硬约定):
    - worker【只能】改『当前任务』的『状态』『结果』两行。
    - 【禁止】改目标/承接方/产物目录/产出文件, 禁止动『待执行/已完成/已放弃』。
    - 【只做这一步】, 做完写 completed 就退出, 禁止顺手承接下一步。
    """
    out_dir = product_workspace or workspace
    tpl_path = _resolve_prompt_template_path()
    # 读失败时退化到 zh 模板(零回归锚 §6.1):zh 部署是测试基线,
    # en 部署读到 en 模板挂掉也只是退化,永远不出空串。
    try:
        with open(tpl_path, encoding="utf-8") as f:
            tpl = f.read()
    except OSError:
        tpl_path = os.path.join(_PROMPT_DIR, _PROMPT_TEMPLATES["zh-cn"])
        with open(tpl_path, encoding="utf-8") as f:
            tpl = f.read()
    return tpl.format(todo_path=todo_path, out_dir=out_dir,
                      workspace=workspace)


def build_worker_prompt(m: TaskManifest, workspace: str = "/workspace",
                      product_workspace: str | None = None) -> str:
    """按 manifest 生成 worker prompt。统一走 TODO.md 契约。"""
    pw = product_workspace or m.project_workspace
    if not pw and workspace and "/.tasks/" in workspace:
        # 兜底: task ws 形如 <project>/.tasks/<id> → 推出项目根
        pw = workspace.rsplit("/.tasks/", 1)[0]
    if pw:
        return worker_contract_prompt(f"{pw.rstrip('/')}/TODO.md",
                                    workspace, pw)
    return worker_contract_prompt(f"{workspace.rstrip('/')}/TODO.md",
                                workspace, None)


# ── worker 拉起 (只走 worker gateway HTTP API) ────────────────────

def create_session(reg_entry: dict, timeout: int = 15) -> str | None:
    """预创建 worker session: POST /api/sessions。

    锚点在派发 HTTP 发生**之前**确立并落库 —— 之后无论超时/断连/进程死,
    session 都丢不掉, resume 永远有锚 (UI-4 根治)。
    """
    import urllib.request
    base = reg_entry["api_url"].rstrip("/")
    try:
        req = urllib.request.Request(
            base + "/api/sessions", data=b"{}", method="POST",
            headers={"Authorization": "Bearer " + reg_entry["api_key"],
                    "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        return d.get("session", {}).get("id") or d.get("id") or None
    except Exception:
        return None


def spawn_api(m: TaskManifest, reg_entry: dict, prompt: str,
             timeout: int, session_id: str = None,
             mode: str = "chat_completions") -> dict:
    """长驻 worker 容器 (gateway API server) 派发。

    mode="chat_completions"（旧路径）:
      首选: 预创建 session + POST /api/sessions/{sid}/chat —
      锚点先于 HTTP 存在, 超时不丢。
      回退: /v1/chat/completions + 响应头 X-Hermes-Session-Id。
      阻塞: 等 LLM turn 完整返回 (timeout 期间).

    mode="runs" (v9 路径, 2026-10-07):
      异步: POST /v1/runs -> 立即 202 + run_id + session_id.
      不等 LLM turn 结束, 由 mw.sse_bridge 订阅 run events.
      Returns {ok: True, run_id, session_id, runner_kind: "api_async", exit_code: 0}.
      caller 负责: open_bridge(...) 启 SSE + 写 ledger session.
    """
    import urllib.request
    headers = {"Authorization": "Bearer " + reg_entry["api_key"],
               "Content-Type": "application/json"}

    # v9 async 路径: POST /v1/runs 立即拿 run_id
    if mode == "runs":
        url = reg_entry["api_url"].rstrip("/") + "/v1/runs"
        body = json.dumps({
            "model": reg_entry.get("model", "hermes-agent"),
            "input": prompt,
            "session_id": session_id,
            "stream": False,
        }).encode()
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, data=body, method="POST",
                                          headers=headers),
                    timeout=15) as r:
                payload = json.loads(r.read())
            run_id = payload.get("run_id") or payload.get("id")
            # 注: v1/runs 返回的 session_id 不在响应里, 需用 caller 传入的预创建 sid
            return {"runner_kind": "api_async", "exit_code": 0,
                    "stdout": "", "stderr": "",
                    "session_id": session_id, "run_id": run_id,
                    "container_id": None}
        except Exception as e:
            return {"runner_kind": "api_async", "exit_code": 1,
                    "stdout": "", "stderr": f"/v1/runs POST failed: {e}",
                    "session_id": session_id, "run_id": None,
                    "container_id": None}

    # 旧路径 (默认, 保留兼容): /api/sessions/{sid}/chat or /v1/chat/completions
    if session_id:
        url = reg_entry["api_url"].rstrip("/") + f"/api/sessions/{session_id}/chat"
        body = json.dumps({"model": reg_entry.get("model", "hermes-agent"),
                          "message": prompt}).encode()
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, data=body, method="POST",
                                          headers=headers),
                    timeout=timeout) as r:
                payload = json.loads(r.read())
            content = (payload.get("message") or {}).get("content", "")
            return {"runner_kind": "api", "exit_code": 0, "stdout": content,
                    "stderr": "", "session_id": session_id,
                    "container_id": None}
        except Exception as e:
            # 超时/断连: 锚点仍在 (worker 可能还在跑), 原样带回 session
            return {"runner_kind": "api", "exit_code": 1, "stdout": "",
                    "stderr": f"API call failed: {e}",
                    "session_id": session_id, "container_id": None}
    url = reg_entry["api_url"].rstrip("/") + "/v1/chat/completions"
    body = json.dumps({
        "model": reg_entry.get("model", "hermes-agent"),
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode()
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, data=body, method="POST",
                                      headers=headers),
                timeout=timeout) as r:
            sid = r.headers.get("X-Hermes-Session-Id")
            payload = json.loads(r.read())
            content = payload.get("choices", [{}])[0].get(
                "message", {}).get("content", "")
        return {"runner_kind": "api", "exit_code": 0,
                "stdout": content, "stderr": "", "session_id": sid,
                "container_id": None}
    except Exception as e:
        return {"runner_kind": "api", "exit_code": 1,
                "stdout": "", "stderr": f"API call failed: {e}",
                "session_id": None, "container_id": None}


def spawn_worker(m: TaskManifest, reg_entry: dict, workspace: str,
                timeout: int, session_id: str = None,
                mode: str = "chat_completions") -> dict:
    """拉起 worker。runner 可注入以便测试; 生产只支持 api runner。

    v9 hotfix (2026-10-07): 透传 mode 给 spawn_api。原实现漏了该参数,
    __spawn-bg --mode runs 会 TypeError 崩掉
    (spawn_worker() got an unexpected keyword argument 'mode')。
    """
    runner = reg_entry.get("runner", "api")
    if runner != "api":
        raise SubmitError(500, f"未知 runner: {runner} (1A 边界只支持 api)")
    prompt = build_worker_prompt(m, workspace,
                               product_workspace=m.project_workspace)
    return spawn_api(m, reg_entry, prompt, timeout, session_id=session_id,
                     mode=mode)


# ── 派发主入口 ────────────────────────────────────────────────────

def dispatch(payload: dict, *, registry_path: str, ledger: Ledger,
            spawn=None, async_mode: bool = True) -> dict:
    """为一张 main 已建好 (并已 claim) 的 kanban 卡起 worker。

    前置 (main 的责任, mw 不代劳):
      - kanban 卡已创建, payload["task_id"] = kanban task_id
      - main 已 `hermes kanban claim <task_id>`
      - 项目任务: main 已播种 <project>/TODO.md (当前任务=本卡)

    mw 只做: 校验 → 记账 → marker → spawn。
    """
    registry = load_registry(registry_path)
    m = validate_dispatch(payload, registry)
    task_id = payload.get("task_id")
    if not task_id:
        raise SubmitError(422, "task_id 必填 (main 建好的 kanban 卡 id)")

    # 重复派发护栏: 同 task 已有活跃 session → 拒, 返回现有 session 让 resume。
    state = ledger.is_task_running(task_id)
    if state["running"]:
        return {
            "ok": False, "task_id": task_id,
            "rejected": "already_running",
            "session_id": state["session_id"],
            "started_at": state["started_at"],
            "note": "task 已有活跃 session。改用 POST /api/resume 续作, "
                    "不要起第二个 worker 重复做工。",
        }

    ledger.record_submit(m.idempotency_key, task_id, payload)
    # required_passes 管道已废弃 (2026-10-08 边界减法): 不再传给 put_contract,
    # mw 不按通过次数门控结算。重复执行由 main 建多张卡实现。
    ledger.put_contract(task_id, m.harvest.model_dump())

    # 任务工作区 (worker 的 scratch + .clarify.json 落点)。
    # mw 不建项目文件; task workspace 目录不存在时由 worker 自己创建
    # (prompt 已告知), mw 不代建。
    ws = payload.get("workspace")
    if not ws:
        if m.project_workspace:
            ws = str(Path(m.project_workspace) / ".tasks" / task_id)
        else:
            base_dir = os.environ.get("MW_TASK_DIR_BASE")
            ws = (str(Path(base_dir) / task_id) if base_dir
                  else f"/share_workspace/tasks/{task_id}")

    reg_entry = registry[m.target.profile]

    # 预创建 session: 锚点先于派发 HTTP 存在。
    # 注入 spawn (测试) 时跳过 —— 测试不连真实 worker gateway。
    pre_sid = None if spawn else create_session(reg_entry)

    # marker 必须先于 spawn 落盘 (started_at 先行, reaper 才能超时兜底;
    # 若进程中途死掉也留有痕迹, 不会静默失踪)。
    from .reaper import write_marker
    base = os.environ.get("MW_HOME", ".")
    write_marker(base, task_id, started_at=time.time(), workspace=ws,
                session_id=pre_sid)
    if pre_sid:
        ledger.upsert_session(task_id, pre_sid, m.target.profile, "api",
                             None, workspace=ws,
                             worker_hermes_home=reg_entry.get("hermes_home"))

    if async_mode:
        # CLI 进程立即退出, daemon 线程活不到 spawn 完成 —— 派生
        # detached 子进程跑阻塞 spawn 并回填 marker。
        import subprocess as sp
        mf = Path(base) / f"manifest_{task_id}.json"
        mf.write_text(json.dumps(payload, ensure_ascii=False))
        argv = [sys.executable, "-m", "mw.cli", "__spawn-bg",
               "--manifest", str(mf), "--task", task_id,
               "--workspace", ws,
               "--mode", "runs"]  # v9: 异步 /v1/runs + SSE bridge
        if pre_sid:
            argv += ["--session", pre_sid]
        # v9: spawn-bg 完成后会调 console.py:8700 启 SSE bridge
        env = {**os.environ,
               "PYTHONPATH": str(Path(__file__).parent.parent),
               "MW_CONSOLE_URL": os.environ.get("MW_CONSOLE_URL", "http://127.0.0.1:8700"),
               "API_SERVER_KEY": os.environ.get("API_SERVER_KEY", "")}
        sp.Popen(argv, stdout=sp.DEVNULL, stderr=sp.DEVNULL,
                 env=env, start_new_session=True)
        # F8 (2026-10-08): 派发成功必须 touch_progress, 否则
        # reaper 敲门时 last_progress_at=NULL, 静默回退到
        # marker.started_at,「挂 N 分钟」=「spawn 后 N 分钟」, 与
        # worker 是否真进展无关 (memory 已记录的坑)。dispatch 是
        # 真实进展信号, 必须落账。
        try:
            ledger.touch_progress(task_id)
        except Exception:
            pass
        return {"ok": True, "accepted": True, "task_id": task_id,
                "workspace": ws, "session_id": pre_sid,
                "note": "async: detached spawn + reaper 按 TODO.md 状态结算"}

    kwargs = {"session_id": pre_sid} if pre_sid else {}
    res = (spawn or spawn_worker)(m, reg_entry, ws,
                                m.harvest.timeout_seconds, **kwargs)
    write_marker(base, task_id, exit_code=res.get("exit_code"),
                stdout=(res.get("stdout") or "")[-4000:],
                stderr=(res.get("stderr") or "")[-2000:])
    session_id = res.get("session_id")
    if not session_id:
        combined = (res.get("stdout") or "") + "\n" + (res.get("stderr") or "")
        sid_m = SESSION_ID_RE.search(combined)
        session_id = sid_m.group(1) if sid_m else None
    ledger.upsert_session(task_id, session_id, m.target.profile,
                         res["runner_kind"], res.get("container_id"),
                         workspace=ws,
                         worker_hermes_home=reg_entry.get("hermes_home"))
    # F8 (2026-10-08): sync 路径同样 touch_progress。详见 async 路径注释。
    try:
        ledger.touch_progress(task_id)
    except Exception:
        pass
    return {"ok": True, "task_id": task_id, "session_id": session_id,
            "workspace": ws}


# ── resume (main → worker 续作通道, 纯转发) ───────────────────────

def resume_task(task_id: str, *, ledger: Ledger,
               instruction: str, timeout: int = 300,
               hermes_home: str = None) -> dict:
    """凭 session 锚点原地恢复 worker 会话续作 (header 续接实测成立)。

    新回复写回同一 session (上下文完整)。这是"只差收尾 / 澄清已答"场景的
    恢复路径, 避免重派重复劳动。

    resume 后把 harvest_status 拨回 PENDING, 让 reaper 按 worker 续写后的
    TODO.md 状态重新结算。
    """
    sess = ledger.get_session(task_id)
    if not sess or not sess["session_id"]:
        raise SubmitError(404, f"task {task_id} 无 session 锚点, 无法 resume")
    reg = load_registry(os.environ.get("MW_REGISTRY",
                     str(Path(__file__).parent / "registry.json")))
    entry = reg[sess["profile"]]
    import urllib.request
    body = json.dumps({"model": entry.get("model", "hermes-agent"),
                      "messages": [{"role": "user", "content": instruction}],
                      "stream": False}).encode()
    req = urllib.request.Request(
        entry["api_url"].rstrip("/") + "/v1/chat/completions", data=body,
        headers={"Authorization": "Bearer " + entry["api_key"],
                "Content-Type": "application/json",
                "X-Hermes-Session-Id": sess["session_id"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            sid = r.headers.get("X-Hermes-Session-Id")
            content = json.loads(r.read())["choices"][0]["message"]["content"]
    except Exception as e:
        # F3 (2026-10-04): resume 超时 ≠ 失败。worker 的回合在服务端继续跑
        # (hermes session 长驻), 完成信号是 TODO.md 状态, 不是 resume 的
        # HTTP 返回。超时按「已受理」处理: 照样拨回 PENDING 让 reaper 按
        # worker 续写的 TODO.md 结算。只有非超时错误(拒连/401/无锚点)才是
        # 真失败。(201 实测 t_321617ab: resume 60s 超时标 ERROR, 但 coder
        # 正在同会话真正修复 —— 假失败误导 main。)
        if not (isinstance(e, TimeoutError) or "timed out" in str(e).lower()):
            raise SubmitError(500, f"resume API 失败: {e}")
        sid = sess["session_id"]
        content = None
    resumed_same = (sid == sess["session_id"])
    # 清除澄清请求: 答案已注入, 不清 .clarify.json 会让下轮 harvest
    # 再次命中旧澄清 → 永远 BLOCK 的死循环 (设计必堵)。
    try:
        cpath = Path(_map_path(sess["workspace"])) / ".clarify.json"
        if cpath.exists():
            cpath.unlink()
    except OSError:
        pass
    # 状态拨回 PENDING 让 reaper 重新按 TODO.md 结算
    # M4 收编 (BATCH1-DESIGN): 裸 UPDATE 改走统一状态更新器,
    # 迁移带 actor=resume 的 worker.state 事件 + progress bump。
    # issue #65 C-2 (幂等 + 防钉死):
    #   ① 卡已是 PENDING 时跳过迁移 —— _STATE_TRANSITIONS["PENDING"] 无自环,
    #      裸调 update_worker_state 必炸 ValueError, resume 对 PENDING 卡
    #      永远失败 (resume 在此场景仅唤醒语义, 跳过即合法完成)。
    #   ② 其余迁移失败 (如 WORKING→PENDING 属 handoff 形状, 白名单不收) 不再
    #      中断函数 —— 唤醒已成功, 必须继续走下方 F2 重占槽, 否则卡掉进
    #      「唤醒成功但状态未回拨」的解耦死区 (issue #65 核实点 3)。失败信息
    #      随返回 dict 透传 (reaper 暂未落账, 见 REVIEW-61-65 S-2)。
    state_reset = "done"
    try:
        cur = ledger.conn.execute(
            "SELECT harvest_status FROM harvest_contracts WHERE task_id=?",
            (task_id,)).fetchone()
        if cur and cur["harvest_status"] == "PENDING":
            state_reset = "skipped:already_pending"
        else:
            ledger.update_worker_state(task_id, "PENDING", actor="resume",
                                      reason="resume 续作, 等待重新结算")
    except (ValueError, PermissionError) as e:
        state_reset = f"skipped:{e}"
    # F2 (2026-10-04): resume = main 明确要继续盯这张卡 → 把卡重占回
    # run 的 current_card_id 槽位。否则: 结算已把槽清成 NULL, resume 只
    # 把卡拨回 PENDING, 而 reaper 只结算/敲门 current_card → 卡掉进
    # PENDING+NULL 无人区, 永不结算也无人敲门
    # (201 实测 t_321617ab 整条死锁的最后一环)。
    # 只在「槽为 NULL 且卡确属本 run」时占槽 —— 槽被别的卡占着说明
    # 串行链在别处, 不越权抢槽 (advance 的串行铁律同样适用)。
    try:
        row = ledger.conn.execute(
            "SELECT team_work_run_id FROM harvest_contracts WHERE task_id=?",
            (task_id,)).fetchone()
        run_id = row["team_work_run_id"] if row else None
        if run_id:
            run = ledger.get_team_work(run_id)
            if run and run["status"] == "ACTIVE" \
                    and run["current_card_id"] is None:
                ledger.set_team_work_current_card(run_id, task_id)
                # 占槽后清掉这张卡的死信/退避, 让后续结算报告能送达
                ledger.conn.execute(
                    "UPDATE harvest_contracts SET notify_dead_until=NULL, "
                    "notify_attempts=0 WHERE task_id=?", (task_id,))
                ledger.conn.commit()
    except Exception:
        pass  # 占槽失败不阻断 resume 本身 (reaper 兜底敲门仍会救)
    return {"ok": True, "task_id": task_id,
            "resumed_same_session": resumed_same,
            # issue #65 C-2: 回拨结果 (done / skipped:*) 透传给 reaper 记录
            "state_reset": state_reset,
            "reply": (content or "")[:2000]}


# ════════════════════════════════════════════════════════════════════
# issue #77: 远端 worker 续 claim + reclaim 计数口径
# ────────────────────────────────────────────────────────────────────
# 根因 (源码 + 实测双证, 见 docs-source/issue77/TRIAGE-20261022.md):
#   gateway 的 kanban dispatcher 每 60s 一 tick, 按 claim TTL (默认 15min)
#   回收 status='running' 且 claim_expires 过期的卡。回收的活体豁免要求
#   (host_local AND worker_pid) —— mw 起的远端 worker 两个都不满足
#   (host_local=False, worker_pid=NULL), 所以**没有任何逃生口**: 跑过
#   TTL 的卡必被 running→ready, 而 worker 其实还活着。
#   回收还走 _record_task_failure ⇒ consecutive_failures +1 (断路器
#   阈值 2) ⇒ 第二次回收即 auto-block, 长链必停。
#
# mw 侧对策 (两段, 缺一不可):
#   ① 续 claim: CAS UPDATE tasks SET claim_expires=? WHERE id=?
#      AND status='running' AND claim_lock=? (claimer = 该卡自己的
#      claim_lock), 并同步 task_runs.claim_expires。
#      ★ 禁用 `hermes kanban heartbeat` CLI —— 实测它只动
#      last_heartbeat_at, 不动 claim_expires (照 issue 正文方向#1 原样
#      做 = 回收照旧发生)。
#   ② 修账: 回收事件照落 task_runs (outcome='reclaimed') 供审计, 不隐藏;
#      但远端 worker 有活性证据 (session message_count 在 5min 观察窗内
#      仍在增长) 时, 该次回收**不累加** consecutive_failures, 且把
#      last_failure_error 写成 reclaimed_remote_alive 与真实失败区分。
#      (口径 = 用户 2026-10-23 裁决)
#
# 挂点: reaper 每轮调一次 renew_remote_claims() (单步, 不起常驻线程);
# reaper 是唯一既常驻、又握有 ledger 上下文与 registry 的 mw 进程。

# gateway DEFAULT_CLAIM_TTL_SECONDS = 15 * 60 (hermes_cli/kanban_db.py)
CLAIM_TTL_DEFAULT = 15 * 60
# 活性观察窗 = 5min (用户裁决口径: message_count 在 5min 窗内仍在增长)
ALIVE_WINDOW_DEFAULT = 300
# 修账窗口 = 1h (回收事件的判龄上限; 越老越不碰, 避免误修历史卡)
REPAIR_WINDOW_DEFAULT = 3600
# 活性豁免时写入 last_failure_error 的固定值 (与真实失败区分)
RECLAIM_ALIVE_ERROR = "reclaimed_remote_alive"
# gateway 回收时写的 last_failure_error 前缀 (stale_lock=<claim_lock>)
RECLAIM_ERR_PREFIX = "stale_lock="
# mw 管的远端 runner 类型 (本地 worker 走 gateway 自己的豁免分支)
REMOTE_RUNNER_KINDS = ("api_async", "api")

# 每张被观察卡的采样历史: task_id -> {session_id, samples:[(ts, count)]}
# 进程内状态 (reaper 常驻, 重启后重新采样即可, 无需持久化)。
_CLAIM_WATCH: dict[str, dict] = {}


def claim_ttl_seconds() -> int:
    """claim TTL (秒)。与 gateway 同源 env, 保证续期长度与回收阈值同口径。"""
    v = os.environ.get("HERMES_KANBAN_CLAIM_TTL_SECONDS", "").strip()
    try:
        return max(1, int(v)) if v else CLAIM_TTL_DEFAULT
    except ValueError:
        return CLAIM_TTL_DEFAULT


def _alive_window_s() -> float:
    """活性观察窗 (秒), env MW_CLAIM_ALIVE_WINDOW_S, 非法值回落 300。"""
    v = os.environ.get("MW_CLAIM_ALIVE_WINDOW_S", "").strip()
    try:
        return float(v) if v else float(ALIVE_WINDOW_DEFAULT)
    except ValueError:
        return float(ALIVE_WINDOW_DEFAULT)


def _repair_window_s() -> float:
    """修账判龄窗口 (秒), env MW_CLAIM_REPAIR_WINDOW_S, 非法值回落 3600。"""
    v = os.environ.get("MW_CLAIM_REPAIR_WINDOW_S", "").strip()
    try:
        return float(v) if v else float(REPAIR_WINDOW_DEFAULT)
    except ValueError:
        return float(REPAIR_WINDOW_DEFAULT)


def _console_window_s() -> float:
    """console SSE 活体窗口 (秒), env MW_CLAIM_CONSOLE_WINDOW_S。

    默认 = 活性观察窗 (300s)。thinking 粘滞标志不受窗口限制 (它由终态
    事件才置 False), 窗口只管 "事件新鲜" 这条兜底判定。
    """
    v = os.environ.get("MW_CLAIM_CONSOLE_WINDOW_S", "").strip()
    try:
        return float(v) if v else _alive_window_s()
    except ValueError:
        return _alive_window_s()


def open_kanban_conn(db_path: str):
    """打开 main 的 kanban.db (读写)。

    ★ 这是 mw 首次**写** kanban.db (现状只读)。1A 边界「LLM 不碰
    kanban.db」仍然成立 —— 写的是代码, 不是 LLM 直接改账本; 且写入
    严格限定在「CAS 续自己的 claim_expires」与「回收误伤修账」两处。
    """
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def renew_claim_cas(conn, task_id: str, claim_lock: str, *,
                    ttl_seconds: int | None = None,
                    now: int | None = None) -> bool:
    """CAS 续 claim: 只在 (status='running' AND claim_lock=?) 命中时延长。

    claimer = 该卡自己的 claim_lock (不是我们的身份) —— 语义 = "把
    本来就属于这张卡的 claim 往后延", 绝不越界续别人的卡。命中后同步
    task_runs.claim_expires (与 gateway _extend_run_claim 同口径)。
    返回 True = 续期成功。
    """
    now = int(now if now is not None else time.time())
    ttl = int(ttl_seconds if ttl_seconds is not None else claim_ttl_seconds())
    expires = now + ttl
    cur = conn.execute(
        "UPDATE tasks SET claim_expires=?, last_heartbeat_at=? "
        "WHERE id=? AND status='running' AND claim_lock=?",
        (expires, now, task_id, claim_lock))
    if cur.rowcount != 1:
        conn.rollback()
        return False
    conn.execute(
        "UPDATE task_runs SET claim_expires=? "
        "WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)",
        (expires, task_id))
    conn.commit()
    return True


def fetch_session_message_count(reg_entry: dict, session_id: str,
                                 *, timeout: float = 3.0) -> int | None:
    """取远端 worker session 的 message_count (活性证据的唯一来源)。

    走 worker gateway 的 GET /api/sessions/<sid>。真实返回形状是
    {"object": ..., "session": {..., "message_count": N}} —— message_count
    嵌在 session 里, 顶层没有 (同 mw/console.py 的窗口化代理层口径)。
    两种形态都兼容 (嵌套优先, 顶层兜底); 取不到 / 网关不可达 → None
    (证据不可得 ≠ 死, 调用方按 fail-closed 处理)。
    """
    if not reg_entry or not reg_entry.get("api_url") or not session_id:
        return None
    import urllib.request
    url = (reg_entry["api_url"].rstrip("/") + "/api/sessions/"
           + quote(session_id, safe=""))
    req = urllib.request.Request(url, method="GET")
    key = reg_entry.get("api_key") or ""
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for cand in (data.get("session"), data):
        if not isinstance(cand, dict):
            continue
        mc = cand.get("message_count")
        # bool 是 int 的子类, 显式排除 (True 不可能是消息条数)
        if isinstance(mc, int) and not isinstance(mc, bool) and mc >= 0:
            return mc
    return None


def alive_by_message_growth(samples, *, window_s: float, now: float) -> tuple[bool, dict]:
    """活性证据判定 (裁决口径): message_count 在观察窗内出现过增长。

    samples = [(ts, count), ...] (按时间升序)。窗内至少两个采样且最大
    计数严格大于最小计数 (max > min, 即窗内曾出现增长, 末值可以回落)
    ⇒ 有活性证据; 单采样 / 计数全部相同 ⇒ 无证据 (保守: 宁可漏判
    活性, 也不给死 worker 续命)。

    返回的 detail 用 "liveness_reason" 命名判定依据 —— 不叫 "reason",
    避免与调用方结果行里的动作级 reason (为什么跳过) 撞键互相覆盖。
    """
    win = [(ts, c) for ts, c in samples
           if ts >= now - window_s and c is not None]
    detail: dict = {"samples_in_window": len(win)}
    if len(win) < 2:
        detail["liveness_reason"] = "insufficient_samples"
        return False, detail
    counts = [c for _, c in win]
    grew = max(counts) > min(counts)
    detail.update({"first_count": counts[0], "last_count": counts[-1],
                   "delta": counts[-1] - counts[0],
                   "liveness_reason": "growing" if grew else "flat"})
    return grew, detail


def query_console_liveness(profile: str, session_id: str,
                            *, timeout: float = 2.0) -> dict | None:
    """向 console 查 (profile, session_id) 的 SSE 活体条目。

    与 mw/reaper.py 的同名 helper 同口径 (GET /api/workers/live, 只读
    sse_bridge 内存态)。返回条目 dict (thinking / last_event_ts /
    event_count); 不可达 / 无条目 / 异常 → None (fail-open: 证据不可得
    绝不被当成 "死")。
    """
    if not profile or not session_id:
        return None
    import urllib.request
    url = (os.environ.get("MW_CONSOLE_URL", "http://127.0.0.1:8700")
           .rstrip("/") + "/api/workers/live")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read())
        for e in data.get("live", []):
            if e.get("profile") == profile and e.get("session_id") == session_id:
                return e
    except Exception:
        return None
    return None


def console_liveness_alive(entry: dict | None, *, window_s: float,
                           now: float) -> tuple[bool, dict]:
    """console 活体条目的存活判定 (与 reaper._is_alive_entry 同口径)。

    thinking==True (从首个活动事件粘滞到终态, 覆盖单个长工具调用中间
    无事件的空窗) 或 last_event_ts 距今 < 窗口 ⇒ 活。
    """
    detail: dict = {"console_entry": bool(entry)}
    if entry is None:
        detail["liveness_reason"] = "console_no_entry"
        return False, detail
    if entry.get("thinking") is True:
        detail["liveness_reason"] = "console_thinking"
        return True, detail
    ts = entry.get("last_event_ts") or 0
    if ts > 0 and (now - ts) < window_s:
        detail["liveness_reason"] = "console_event_fresh"
        detail["event_age_s"] = int(now - ts)
        return True, detail
    detail["liveness_reason"] = "console_stale"
    return False, detail


def worker_liveness(profile: str, session_id: str,
                     samples, *, window_s: float, now: float,
                     console_window_s: float | None = None) -> dict:
    """汇总远端 worker 的活性证据 (两个独立信号)。

    信号 A —— message_count 增长 (裁决口径, 5min 窗内曾出现增长, max>min):
      用于「回收计数是否豁免」这一硬判定 (宁可漏判, 也不给死 worker
      免罪)。
    信号 B —— console SSE 活体 (thinking 粘滞标志 / 事件新鲜):
      用于「是否续 claim」。加它的理由: 一个跑长工具调用的 worker 在
      工具返回前 message_count 是平的 (可 >5min), 只看 A 会误判成死
      worker 而停止续期 ⇒ TTL 到点照样被回收 (= 没修好)。thinking 从
      首个活动事件粘滞到终态, 正好覆盖这段空窗 (与开发机侧已验证的
      keeper 同一信号源)。

    返回 {alive_for_renewal, message_growth, console_alive, ...detail}。
    """
    grew, grow_detail = alive_by_message_growth(
        samples, window_s=window_s, now=now)
    entry = query_console_liveness(profile, session_id)
    c_alive, console_detail = console_liveness_alive(
        entry, window_s=console_window_s if console_window_s is not None
        else window_s, now=now)
    out: dict = {
        "message_growth": grew,
        "console_alive": c_alive,
        # 续期用并集 (任一信号即活) —— 漏判活性的代价是回收误伤。
        "alive_for_renewal": bool(grew or c_alive),
    }
    out.update(grow_detail)
    out.update(console_detail)
    # liveness_reason 合并: A 优先 (裁决口径), 否则用 B 的判定
    if grew:
        out["liveness_reason"] = "message_growing"
    elif c_alive:
        out["liveness_reason"] = console_detail.get("liveness_reason")
    else:
        out["liveness_reason"] = grow_detail.get(
            "liveness_reason", console_detail.get("liveness_reason"))
    return out


def _remote_task_rows(ledger) -> list[dict]:
    """mw 管的远端卡清单 (ledger.task_sessions 里 runner_kind=远端的那批)。"""
    try:
        rows = ledger.conn.execute(
            "SELECT task_id, session_id, profile, runner_kind FROM task_sessions "
            "WHERE runner_kind IN (?, ?) AND session_id IS NOT NULL",
            REMOTE_RUNNER_KINDS).fetchall()
    except Exception:
        return []
    return [dict(r) for r in rows]


def _kanban_task_row(conn, task_id: str):
    return conn.execute(
        "SELECT id, status, claim_lock, claim_expires, consecutive_failures, "
        "       last_failure_error, current_run_id FROM tasks WHERE id=?",
        (task_id,)).fetchone()


def _latest_reclaim_event(conn, task_id: str):
    """该卡最近一次回收相关事件 (reclaimed / gave_up), 供修账判龄。"""
    return conn.execute(
        "SELECT id, kind, payload, created_at FROM task_events "
        "WHERE task_id=? AND kind IN ('reclaimed','gave_up') "
        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()


def _repair_reclaim_accounting(conn, ledger, row, *, alive: bool,
                                detail: dict, now: float) -> list[dict]:
    """回收误伤修账: 远端 worker 仍活 ⇒ 不累加 consecutive_failures。

    触发条件 (全部满足才动账):
      1. last_failure_error 以 'stale_lock=' 开头 = gateway 回收时写的
         原始错误 (我们修过就换成 reclaimed_remote_alive, 天然幂等);
      2. 该卡最近一次 reclaimed/gave_up 事件在修账窗口内;
      3. 远端 worker 有活性证据 (message_count 5min 窗内增长)。
    动作: consecutive_failures 减 1 (回收那次本不该计入) + 写
    last_failure_error=reclaimed_remote_alive。
    ★ 不改卡状态 (blocked/gave_up 归 main 处置 —— 1A 边界), 不动
    task_runs (回收审计痕照落, 不隐藏)。
    """
    tid = row["id"]
    lfe = row["last_failure_error"] or ""
    if not lfe.startswith(RECLAIM_ERR_PREFIX):
        return []
    ev = _latest_reclaim_event(conn, tid)
    if ev is None:
        return []
    age = now - float(ev["created_at"] or 0)
    if age > _repair_window_s():
        return [{"task_id": tid, "trigger": "claim_renewal",
                 "verdict": "SKIP", "reason": "reclaim_too_old",
                 "age_s": int(age)}]
    if not alive:
        return [{"task_id": tid, "trigger": "claim_renewal",
                 "verdict": "SKIP", "reason": "reclaim_without_liveness",
                 **detail}]
    before = int(row["consecutive_failures"] or 0)
    after = max(0, before - 1)
    # CAS 落在 last_failure_error 上: 并发下只有一个执行者能修成功
    cur = conn.execute(
        "UPDATE tasks SET consecutive_failures=?, last_failure_error=? "
        "WHERE id=? AND last_failure_error=?",
        (after, RECLAIM_ALIVE_ERROR, tid, lfe))
    conn.commit()
    if cur.rowcount != 1:
        return []
    try:
        ledger.emit_event("claim.reclaimed_remote_alive", tid, {
            "consecutive_failures_before": before,
            "consecutive_failures_after": after,
            "reclaim_event_id": ev["id"],
            "reclaim_event_kind": ev["kind"],
            "reclaim_age_s": int(age),
            "liveness": detail,
            "last_failure_error_before": lfe,
        })
    except Exception:
        pass
    return [{"task_id": tid, "trigger": "claim_renewal",
             "verdict": "REPAIRED",
             "consecutive_failures_before": before,
             "consecutive_failures_after": after,
             "reclaim_event_id": ev["id"], **detail}]


def _handle_remote_task(conn, ledger, reg_entry, r: dict, *, now: float) -> list[dict]:
    """单卡一轮: 有活性 ⇒ 续 claim; 被回收过 ⇒ 修账。

    两个活性信号分工 (见 worker_liveness 的说明):
      续期用并集 (message_count 增长 OR console SSE 活体);
      修账 (回收计数豁免) 只认 message_count 增长 —— 裁决口径, 且
      宁可漏判也不给死 worker 免罪。

    ★ 续期与修账是**两条独立分支**, 不是 if/else:
      卡被回收后 main 可能在下一轮之前就重新 claim 它 (status 又变回
      running) —— 此时若按 status 分流走续期分支, 修账就被跳过了,
      计数永远停在被误伤的那一格。所以这里先按「last_failure_error
      是否仍是回收错误」独立判修账, 再按「是否 running」判续期。
    """
    tid = r["task_id"]
    row = _kanban_task_row(conn, tid)
    if row is None:
        return []
    lfe = row["last_failure_error"] or ""
    # 只在「还在跑」或「疑似刚被回收」时才去问 worker 网关要 message_count
    need_sample = (row["status"] == "running" and row["claim_lock"]) or \
                  lfe.startswith(RECLAIM_ERR_PREFIX)
    count = None
    if need_sample:
        count = fetch_session_message_count(reg_entry, r["session_id"])
    watch = _CLAIM_WATCH.setdefault(tid, {"session_id": r["session_id"],
                                           "profile": r["profile"], "samples": []})
    watch["session_id"] = r["session_id"]
    watch["profile"] = r["profile"]
    if count is not None:
        watch["samples"].append((now, count))
    # 采样历史裁剪 (留窗长的两倍, 防时钟回拨把陈样本带回来)
    horizon = now - max(_alive_window_s(), 60) * 2
    watch["samples"] = [(ts, c) for ts, c in watch["samples"] if ts >= horizon][-60:]
    live = worker_liveness(r["profile"], r["session_id"],
                            watch["samples"], window_s=_alive_window_s(),
                            now=now, console_window_s=_console_window_s())
    out: list[dict] = []
    # ── 分支一: 修账 (回收误伤豁免) —— 只认 message_count 增长这一条硬证据
    if lfe.startswith(RECLAIM_ERR_PREFIX):
        out.extend(_repair_reclaim_accounting(
            conn, ledger, row, alive=bool(live["message_growth"]),
            detail=live, now=now))
    # ── 分支二: 续期 (卡还在跑 + 我们持有 claim) —— 并集判活
    if row["status"] == "running" and row["claim_lock"]:
        if not live["alive_for_renewal"]:
            out.append({"task_id": tid, "trigger": "claim_renewal",
                        "verdict": "SKIP", "reason": "no_liveness_evidence",
                        **live})
        else:
            ok = renew_claim_cas(conn, tid, row["claim_lock"], now=int(now))
            out.append({"task_id": tid, "trigger": "claim_renewal",
                        "verdict": "RENEWED" if ok else "SKIP",
                        "reason": None if ok else "cas_miss", **live})
    return out


def renew_remote_claims(*, ledger, registry: dict | None = None,
                         kanban_db: str | None = None,
                         now: float | None = None) -> list[dict]:
    """远端 worker 续 claim + reclaim 计数口径 —— reaper 每轮驱动一次。

    返回 reaper 风格的结果列表 (task_id / trigger / verdict), 便于在
    reaper 日志里直接看见续期与修账。任何异常都收敛在本函数内, 绝不
    影响 reaper 主流程。
    """
    out: list[dict] = []
    db = kanban_db or os.environ.get("MW_KANBAN_DB", "/opt/data/kanban.db")
    if not db or not os.path.exists(db):
        return out
    reg = registry if registry is not None else {}
    rows = _remote_task_rows(ledger)
    if not rows:
        return out
    now = float(now if now is not None else time.time())
    try:
        conn = open_kanban_conn(db)
    except Exception as e:
        return [{"task_id": "_meta", "trigger": "claim_renewal",
                 "verdict": "ERROR", "error": str(e)[:200]}]
    try:
        for r in rows:
            try:
                out.extend(_handle_remote_task(
                    conn, ledger, reg.get(r["profile"]), r, now=now))
            except Exception as e:
                out.append({"task_id": r["task_id"], "trigger": "claim_renewal",
                            "verdict": "ERROR", "error": str(e)[:200]})
    finally:
        conn.close()
    # 采样历史裁剪: ledger 里已不存在的卡 (结算/归档) 从观察表里清掉,
    # 否则进程常驻 ⇒ 观察表只增不减 (慢泄漏)。
    live_ids = {r["task_id"] for r in rows}
    for tid in [t for t in _CLAIM_WATCH if t not in live_ids]:
        _CLAIM_WATCH.pop(tid, None)
    return out
