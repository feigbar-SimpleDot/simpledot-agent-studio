"""收尸器 (回退版) — 边界 (2026-10-02):

harvest 不再改 kanban 状态, 只做"报告":
  1. 物理验收 (verify_physical) — 文件存在 + check_script 通过
  2. 澄清检测 (check_clarification) — .clarify.json
  3. 写 ledger 报告 — worker_report 字段 (产物路径 + 摘要 + 是否需要澄清)

kanban 状态的修改 (complete / block / unblock / unlink) 是 **main agent 的事**,
harvest 只把产物信息写到 ledger, main 读 ledger 决定下一步。

这消除了 BUG-A 死锁的根源: 旧 harvest 调 `kanban complete --force` 在
4 状态守卫 (running/ready/blocked/review) 外时失败, 留下 ERROR 卡 + 无
fallback 路径。 现在 harvest 不动 kanban, 死锁不存在。

reaper chain_daemon / _todo_result_scan / cmd_claim_and_spawn 也已删除:
reaper 的工作是 "main 通知 ping" 而非 "推进链 / 自己结算"。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Optional

# i18n v1.0 (架构 §5.2/§5.3): reason 文案写时 t(), 控制流零改动。
from .i18n import t as _t

# issue#127 S-1: 状态判定收敛到 mw.todo 的统一归一化入口 (issue #124 纪律:
# 全仓唯一状态语义入口, 禁止再写本地字面量集)。todo.py 不 import harvest,
# 无循环依赖。
from .todo import (STATUS_ALIASES, STATUS_BLOCKED, STATUS_FAILED,
                   normalize_status)

# issue#127 S-1 输入域守恒: 改前 harvest 只接受 ASCII 英文别名
# (failed/fail / blocked/block); 中文别名 (失败/阻塞) 由 reaper / console 在
# **进 harvest 之前**就已归一化成规范英文值 (两处都先 normalize_status 再
# 传参)。这里从同一张别名表派生 ASCII 子集, 保证 S-1 对**任何输入**都不
# 产生行为变更 (issue #127 §3.6 总红线)。
_FAILED_ALIASES = frozenset(
    a for a in STATUS_ALIASES[STATUS_FAILED] if a.isascii())
_BLOCKED_ALIASES = frozenset(
    a for a in STATUS_ALIASES[STATUS_BLOCKED] if a.isascii())

HERMES_BIN = shutil.which("hermes") or "hermes"


def _map_path(p: str) -> str:
    """容器路径 → 宿主机路径 (MW_PATH_MAP='/share_workspace=/host/path')。"""
    m = os.environ.get("MW_PATH_MAP", "")
    if m:
        c, _, h = m.partition("=")
        if p.startswith(c):
            return h + p[len(c):]
    return p


def _env(hermes_home: str) -> dict:
    return {"HERMES_HOME": hermes_home,
            "PATH": f"{Path(HERMES_BIN).parent}:" + os.environ.get("PATH", ""),
            "HOME": str(Path(hermes_home).parent)}


# ── transcript 读取 (数据收集, 不是决策) ─────────────────────────
def export_transcript(session_id: str, hermes_home: str, out_dir: str,
                     profile: str = None):
    """凭 session ID 导出 transcript(实测 E3)。返回解析后的 doc 或 None。"""
    out = Path(out_dir) / f"{session_id}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    prefix = os.environ.get("MW_EXPORT_PREFIX", "hermes").split()
    pre = ["-p", profile] if profile else []
    # issue #113 (2026-10-25): transcript 采集是「数据收集」, 不是决策。
    # CLI 二进制不在 PATH (容器装了 hermes 但没挂 PATH / 宿主裸跑 pytest /
    # MW_EXPORT_PREFIX 指向已卸载的镜像) 时, subprocess 抛 OSError
    # (FileNotFoundError 是其子类) 会把整条结算链路炸掉 —— 卡永远落不了账,
    # reaper 只能记一条 ERROR。既有降级口径 (doc=None → 「纯物理验收」)
    # 本就允许没有 transcript, 故这里把「命令不存在」也归入降级而非异常。
    if prefix and prefix[0] == "docker":
        container = prefix[prefix.index("exec") + 1]
        inner = f"/tmp/mw_{session_id}.jsonl"
        try:
            r = subprocess.run(
                prefix + ["sessions", "export", "--format", "jsonl",
                         "--session-id", session_id, "-y", inner],
                capture_output=True, text=True, env=_env(hermes_home))
            if r.returncode != 0:
                return None
            subprocess.run(["docker", "cp", f"{container}:{inner}", str(out)],
                           capture_output=True, text=True)
        except OSError:
            return None
        if not out.exists():
            return None
    else:
        try:
            r = subprocess.run(
                prefix + pre + ["sessions", "export", "--format", "jsonl",
                               "--session-id", session_id, "-y", str(out)],
                capture_output=True, text=True, env=_env(hermes_home))
        except OSError:
            return None
        if r.returncode != 0 or not out.exists():
            return None
    try:
        return json.loads(out.read_text())
    except json.JSONDecodeError:
        return None


def parse_tool_trace(doc: dict) -> list[dict]:
    """从 transcript 解析真实工具调用序列(零 LLM)。

    返回 [{name, arguments, result_exit_code}] — 这是原始事实, 不是叙述。
    """
    trace = []
    pending = None
    for msg in doc.get("messages", []):
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            pending = {"name": fn.get("name"),
                      "arguments": fn.get("arguments"),
                      "result_exit_code": None}
            trace.append(pending)
        if msg.get("role") == "tool" and pending is not None:
            content = msg.get("content")
            try:
                parsed = json.loads(content) if isinstance(content, str) else content
                if isinstance(parsed, dict) and "exit_code" in parsed:
                    pending["result_exit_code"] = parsed["exit_code"]
            except (json.JSONDecodeError, TypeError):
                pass
            pending = None
    return trace


# ── 物理验收 (只读检查, 不决策, 不执行命令) ──────────────────────
def verify_physical(contract: dict, workspace: str,
                   product_workspace: Optional[str] = None) -> tuple[bool, str]:
    """物理验收: 只检查 required_files 是否存在。

    1A 边界 (2026-10-03): check_script 执行已删除 —— mw 不执行任何外部
    命令。需要跑脚本验证的, 由 main 自己跑 (main 有终端)。mw 只做
    文件存在性这种零副作用的只读检查。

    workspace 是 task 隔离目录(.tasks/<id>/),product_workspace 是项目根。
    产物相对路径优先按 product_workspace 解析(project 任务产物共享)。

    返回 (ok, why) — 写 ledger, **不改 kanban**。
    """
    ws = _map_path(workspace)
    pws = _map_path(product_workspace) if product_workspace else None
    for f in contract.get("required_files", []):
        # 协议文件 .clarify.json 永远在 task workspace, 不参与产物验收
        if Path(f).name == ".clarify.json":
            continue
        p = Path(f)
        if p.is_absolute():
            target = p
        elif pws:
            target = Path(pws) / f
        else:
            target = Path(ws) / f
        if not target.exists():
            return False, _t("harvest.reason.physical_missing", target=target)
    return True, _t("harvest.reason.physical_ok")


def check_clarification(workspace: str) -> dict | None:
    """检测 worker 留下的澄清请求(.clarify.json)。

    澄清协议: worker 缺关键信息时写 .clarify.json 后退出 → 写 ledger 报告
    "need_clarification: <q>" → main 回答 → main 调 resume 带答案唤醒同会话。
    """
    p = Path(_map_path(workspace)) / ".clarify.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return {"question": "(.clarify.json 无法解析)"}
    return None


# ── 结构化产物证据 (2026-10-05) ─────────────────────────────────
# main 收 worker_report 时, 原本只有 verdict + log_parts 拼接字符串,
# 没法判断 worker 自测覆盖度, 普遍会再浏览器独立冒烟一遍。本模块把
# 「required_files 落盘事实」+ 「产物静态扫描」结构化产出, main 读到
# products.static_check.passed=true 时不必独立冒烟; 否则按 issues 列表
# 针对性复测。
#
# 边界: 这是「零副作用的只读检查」(同 verify_physical), mw 不执行命令、
# 不跑 headless browser、不 spawn 子进程。heuristic 有边界 (HTML 静态扫描
# 抓不到运行时异常), 但覆盖 transcript 里 main 独立冒烟的 80% 场景
# (文件存在 + JS 静态错误 + 大括号闭合 + required_files 全在)。
#
# 失败不阻断 harvest: 静态扫描失败仍落 verdict=COMPLETE, 仅在
# products.static_check 里标 passed=false + issues 列表 — main 决定。

# JS 静态扫描规则 — 启发式, 抓 worker 跑过但引用了未定义标识符的常见坑
_STATIC_JS_RULES = [
    (r"\bGLYPH_[A-Z_]+\b(?!\s*[:=])",
     "未定义字形常量: 引用了 GLYPH_* 但未在文件内声明 (典型未定义错误)"),
    (r"\bfunction\s+\w+\s*\([^)]*\)\s*\{(?![^}]*\})",
     "未闭合大括号: function 块缺少右花括号"),
    (r"ReferenceError|is not defined|undefined is not",
     "源码含 'ReferenceError' / 'is not defined' 字面量"),
    (r"throw\s+new\s+Error\s*\(\s*[\"'](TODO|placeholder|not implemented)",
     "显式 throw TODO/未实现"),
]


def _static_scan_html(html_text: str) -> dict:
    """对 HTML 文本做轻量静态扫描, 返回 {passed, issues}。

    issues: [{rule, message, snippet}] — snippet 是首处命中截取
    (前后各 60 字符, 便于定位)。
    """
    import re
    issues = []
    # 只扫 <script> 块
    script_blocks = re.findall(r"<script[^>]*>(.*?)</script>",
                              html_text, re.DOTALL | re.IGNORECASE)
    for block in script_blocks:
        for pattern, msg in _STATIC_JS_RULES:
            for m in re.finditer(pattern, block):
                start, end = m.span()
                s = max(0, start - 60)
                e = min(len(block), end + 60)
                snippet = block[s:e].replace("\n", " ")[:120]
                issues.append({"rule": pattern[:40], "message": msg,
                              "snippet": snippet})
                if len(issues) >= 10:
                    return {"passed": False, "issues": issues,
                           "scanned_blocks": len(script_blocks)}
    return {"passed": not issues, "issues": issues,
           "scanned_blocks": len(script_blocks)}


def _collect_product_evidence(contract: dict, workspace: str,
                                product_workspace: Optional[str]) -> dict:
    """收集产物证据 (零副作用只读): file 列表 + 静态扫描结果。

    返回:
      {
        "files": [{name, exists, size_bytes, sha256 (可选, 大文件跳过)}],
        "static_check": {"passed": bool, "issues": [...], "scanned_blocks": N}
      }
    """
    import hashlib
    ws = _map_path(workspace)
    pws = _map_path(product_workspace) if product_workspace else None
    files_info = []
    static_total = {"passed": True, "issues": [], "scanned_blocks": 0}
    for f in contract.get("required_files", []):
        # .clarify.json 不算产物
        if Path(f).name == ".clarify.json":
            continue
        p = Path(f)
        if p.is_absolute():
            target = p
        elif pws:
            target = Path(pws) / f
        else:
            target = Path(ws) / f
        target = Path(_map_path(str(target)))
        info = {"name": f, "exists": False, "size_bytes": 0}
        if target.exists():
            try:
                st = target.stat()
                info["exists"] = True
                info["size_bytes"] = st.st_size
                # 大文件 (>2MB) 跳过 sha256, 只给 size 即可 (产物 md5 通常 main 侧自己算)
                if st.st_size > 0 and st.st_size < 2 * 1024 * 1024:
                    try:
                        h = hashlib.sha256()
                        h.update(target.read_bytes())
                        info["sha256"] = h.hexdigest()
                    except (OSError, PermissionError):
                        pass
            except OSError:
                pass
        # HTML 文件做静态扫描 (产物通常是单文件 HTML)
        if info["exists"] and target.suffix.lower() in (".html", ".htm"):
            try:
                txt = target.read_text(errors="replace")
                result = _static_scan_html(txt)
                info["static_check"] = result
                # 累计到 overall (任一产物失败 → overall 失败)
                if not result["passed"]:
                    static_total["passed"] = False
                    static_total["issues"].extend(
                        [{"file": f, **iss} for iss in result["issues"]])
                static_total["scanned_blocks"] += result["scanned_blocks"]
            except (OSError, UnicodeDecodeError):
                pass
        files_info.append(info)
    return {"files": files_info, "static_check": static_total}


def fetch_transcript_api(api_url: str, api_key: str, session_id: str,
                        timeout: int = 30) -> dict | None:
    """经 worker 自己的 Hermes API 读 transcript。"""
    import urllib.request
    base = api_url.rstrip("/")
    try:
        req = urllib.request.Request(
            f"{base}/api/sessions/{session_id}",
            headers={"Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            meta = json.loads(r.read()).get("session", {})
        req = urllib.request.Request(
            f"{base}/api/sessions/{session_id}/messages",
            headers={"Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        msgs = data.get("data", [])
        if not msgs:
            return None
        return {"messages": msgs, "end_reason": meta.get("end_reason")}
    except Exception:
        return None


def read_worker_state_db(db_path: str, session_id: str) -> dict | None:
    """只读直查 worker 的 state.db(T13: 免 docker exec/cp)。"""
    tmp = f"/tmp/mw_state_{os.getpid()}.db"
    try:
        shutil.copy(db_path, tmp)
        for suffix in ("-wal", "-shm"):
            side = db_path + suffix
            if os.path.exists(side):
                shutil.copy(side, tmp + suffix)
    except OSError:
        return None
    try:
        c = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        rows = c.execute(
            "SELECT role, content, tool_calls FROM messages "
            "WHERE session_id=? AND active=1 ORDER BY id", (session_id,)).fetchall()
        end_reason = None
        for r in c.execute("SELECT end_reason FROM sessions WHERE id=?",
                          (session_id,)):
            end_reason = r[0]
        c.close()
        if not rows:
            return None
        return {"messages": [{"role": r[0], "content": r[1],
                             "tool_calls": json.loads(r[2]) if r[2] else None}
                            for r in rows],
                "end_reason": end_reason}
    except sqlite3.Error:
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def session_last_msg_ts(reg_entry: dict, session_id: str, timeout: int = 5):
    """查 worker session 最近一条消息的 timestamp (epoch sec)。

    用于 reaper 敲门活体抑制: now - last_msg_ts < 阈值 → worker 刚产出过
    turn, 抑制假警报。message_count 增量等价但需要 ledger 状态机 (上次基数
    落库), 这里时间戳直接读, 单 endpoint 无写入。
    返回 None = 探测失败 (session 不存在 / 网关 503 / 反序列化错),
    调用方按"探测失败不抑制"处理。
    """
    if not (reg_entry and reg_entry.get("api_url") and session_id):
        return None
    import urllib.request
    base = reg_entry["api_url"].rstrip("/")
    try:
        req = urllib.request.Request(
            f"{base}/api/sessions/{session_id}/messages?limit=1&order=latest",
            headers={"Authorization": "Bearer " + reg_entry.get("api_key", "")})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        data = d.get("data") or []
        if not data:
            return None
        return data[0].get("timestamp")
    except Exception:
        return None


# ── issue #58: 结算时刻 token 消耗快照 (API-SPEC-58 §1/§4) ──────────
_TOKEN_BUCKETS = ("input_tokens", "output_tokens", "cache_read_tokens",
                  "cache_write_tokens", "reasoning_tokens")


def _capture_tokens(reg_entry: dict | None, sess_row, *, timeout: int = 5) -> dict:
    """harvest 结算时拉一次 worker session 的 token 快照 (worker_report.tokens)。

    契约 (API-SPEC-58 §1.3/§1.6, AC-P1-P4):
      - 输入: reg_entry (registry.json 本卡 profile 项, 可 None) +
              sess_row (ledger.get_session 的 Row/dict, 需含 session_id)
      - 输出: 恒返回 9 键 dict (缺输入/任意失败 → source="unavailable" 全 0,
              fail-open 永不抛); 成功 source="api" (5 桶全 0 也算 api)。
      - total_tokens 五桶求和, 落库时一次算完 (单源, console 层不重算)。
      - 单卡 1 次 HTTP, 不重试 (防轰炸); Bearer 头硬约束 (裸 key 会 401)。
    """
    empty = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
        "reasoning_tokens": 0, "estimated_cost_usd": 0.0,
        "total_tokens": 0, "captured_at": None, "source": "unavailable",
    }
    api_url = (reg_entry or {}).get("api_url")
    api_key = (reg_entry or {}).get("api_key")
    # Row 与 dict 都支持 keys(); session_id 缺失 → 无数据源
    sid = (sess_row["session_id"]
           if sess_row is not None and "session_id" in sess_row.keys() else None)
    if not (api_url and api_key and sid):
        return empty
    import urllib.request
    base = api_url.rstrip("/")
    try:
        req = urllib.request.Request(
            f"{base}/api/sessions/{sid}",
            headers={"Authorization": "Bearer " + api_key})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            session = json.loads(r.read()).get("session") or {}

        def _i(key: str) -> int:
            v = session.get(key)
            # 非数值 (缺字段/脏数据) 一律视为 0 (REQUIREMENTS-58 §5.1)
            return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

        cost = session.get("estimated_cost_usd")
        snap: dict = {k: _i(k) for k in _TOKEN_BUCKETS}
        snap["estimated_cost_usd"] = (
            float(cost) if isinstance(cost, (int, float))
            and not isinstance(cost, bool) else 0.0)
        # 派生字段: 五桶求和 (API-SPEC-58 §1.4-5 求和单源不变量)
        snap["total_tokens"] = sum(snap[k] for k in _TOKEN_BUCKETS)
        snap["captured_at"] = time.time()
        snap["source"] = "api"
        return snap
    except Exception:
        # fail-open (AC-P4): 超时/4xx/5xx/坏 JSON 等一律 unavailable, 不抛
        return empty


# ── 结算主入口 (新边界: 不动 kanban, 写 ledger) ─────────────────
def harvest(task_id: str, *, ledger, hermes_home: str, workspace: str,
           spawn_result: dict | None = None,
           registry: dict | None = None,
           worker_status: str | None = None) -> dict:
    """结算一个任务, 写 ledger worker_report 报告, **不改 kanban**。

    边界减法 (2026-10-08, 用户定调): verdict 由 **worker 自报状态**
    (TODO.md『状态』字段) 驱动, mw 不再用自己的检查推翻 worker 的决策。
    worker_status 取值 → verdict / harvest_status:
        completed → COMPLETE            → HARVESTED
        blocked   → NEED_CLARIFICATION  → NEED_CLARIFICATION
        failed    → FAILED              → BLOCKED   (worker 自己说做不到)
        timeout   → FAILED              → BLOCKED   (平台收尸, **非** worker
                                 可写状态; 见 issue#127 S-1 前置短路)
        None/未知 → COMPLETE (CLI 手动结算走这条)
    物理检查 (verify_physical) 降级为 evidence 字段, 不再产生
    PHYSICAL_ERROR / BLOCKED —— 产物齐不齐是事实, 交 main 判断。

    返回值字段:
        - settled (bool): 报告是否成功落账
        - verdict (str): COMPLETE / NEED_CLARIFICATION / FAILED
        - worker_report (dict): 给 main 看的结构化报告
        - log (list[str]): 诊断日志
        - harvest_status (str): 写入 ledger 的新状态

    Main 读 ledger 看到 verdict=COMPLETE → 自己调 hermes kanban complete
    看到 verdict=NEED_CLARIFICATION → 回答后调 resume
    看到 verdict=FAILED → 自己决定: resume / 改方向 / 换 worker / 建后续卡
    """
    row = ledger.get_contract(task_id)
    if not row:
        return {"settled": False, "reason": "no contract"}
    # B (2026-10-07): 守卫改成白名单。允许重写 PENDING / BLOCKED /
    # NEED_CLARIFICATION / ERROR 四种「未真结算」状态;HARVESTED / COMPLETE /
    # ARCHIVED / CANCELLED 等真终态不重写。
    #
    # 根因:旧守卫 `!= "PENDING"` 会让 5min 兜底写下的 BLOCKED 报告永远
    # 无法被覆盖。worker 仍在跑时,reaper 5min 兜底触发 → verify_physical
    # 看不到产物 → 写 BLOCKED → worker 真完工后 TODO.md 状态变 completed
    # → fsnotify/15s 轮询 → _reap_one_task 调 harvest() → 早返回不重写
    # → 陈旧 BLOCKED 报告永久残留 → main 收到陈旧判词消耗 turn 消歧。
    # 改为白名单后:worker 真完工触发 harvest → 允许重写 → 写新报告
    # (verify_physical 通过) → set_worker_report same_content=false 清
    # notify_confirmed_at → relay 重投新报告给 main。
    #
    # 终态(HARVESTED/COMPLETE/ARCHIVED/CANCELLED)不重写:
    # - HARVESTED/COMPLETE 是「上次验证通过」,不应该是陈旧报告,不需要覆写
    # - ARCHIVED/CANCELLED 是操作员决策,不应被自动重结算推翻
    if row["harvest_status"] in ("HARVESTED", "COMPLETE", "ARCHIVED", "CANCELLED"):
        return {"settled": True, "reason": f"already {row['harvest_status']}"}

    # 行级锁: 防止 reaper/console 多入口并发写 ledger
    if not ledger.try_claim_harvest(task_id):
        return {"settled": False, "reason": "harvest_locked_by_other"}

    contract = json.loads(row["contract_json"])
    sess = ledger.get_session(task_id)
    exit_code = (spawn_result or {}).get("exit_code")
    log_parts = [f"exit_code={exit_code}"]

    try:
        return _harvest_inner(task_id, ledger, hermes_home, workspace,
                             contract, sess, exit_code, log_parts, registry,
                             worker_status=worker_status)
    finally:
        ledger.release_harvest(task_id)


def _harvest_inner(task_id, ledger, hermes_home, workspace,
                   contract, sess, exit_code, log_parts, registry,
                   worker_status=None):
    """内部: 收集产物证据 + 写 ledger 报告。**不动 kanban**。

    T3 (2026-10-03): 落终态后清 team_work_runs.current_card_id。
    用 _teardown_current_card 在每个 return 前执行清理, 让 advance 端点
    的 current_card_id 守卫立刻可通过。清理失败不阻断结算 (best-effort)。
    """
    product_workspace = ledger.get_product_workspace(task_id)

    # ★ T3: 取 team_work_run_id 用于结算后清理 current_card_id
    try:
        _tw_row = ledger.conn.execute(
            "SELECT team_work_run_id FROM harvest_contracts WHERE task_id=?",
            (task_id,)).fetchone()
        _tw_run_id = _tw_row["team_work_run_id"] if _tw_row else None
    except Exception:
        _tw_run_id = None

    def _teardown_current_card():
        """T3: 结算完成 = 释放本 run 的 current_card_id 槽位,
        让 advance 端点的下一张卡可以派发。

        F8 (2026-10-08): 任何落账路径都 touch_progress, 避免 reaper
        兜底回退到 marker.started_at 时「挂 N 分钟」=「spawn 后 N 分钟」。
        set_harvest 只 updated_at, last_progress_at 不会被自动写。

        F5-1 (2026-10-13, issue #5 状态机统一): 旧方案想把卡写
        HANDOFF, 但 harvest 路径已经把卡写到终态 (HARVESTED/
        BLOCKED/NEED_CLARIFICATION) _teardown 才清槽 — WORKING→
        HANDOFF 永远不可达, actor 白名单会 raise。 改用「run 级
        handoff 窗口」语义:
          1. clear_team_work_current_card 同时写 current_card_handoff_at
             (已在 ledger 层 F5-2 实现), 此即「run 处于 handoff」
             2. derive_worker_state 看到 task 与 run 上下文: 终态
                直接返; 非终态 + run.current_card_cleared_at 非空 +
                task 是 ledger updated_at 最新一张 → HANDOFF
        这免去了「活跃期 HANDOFF」跑一段窗口被 advance 半路抢走
        的麻烦; 也避免 actor 白名单需要 WORKING→HARVESTED→HANDOFF
        这种反直觉的两跳。
        """
        if _tw_run_id:
            try:
                ledger.clear_team_work_current_card(_tw_run_id)
            except Exception:
                pass
        try:
            ledger.touch_progress(task_id)
        except Exception:
            pass

    # transcript 收集 (可选, 只给 main 看)
    trace = []
    reg_entry: dict = {}
    if sess and sess["session_id"]:
        worker_home = (sess["worker_hermes_home"] if "worker_hermes_home" in sess.keys()
                      else None) or hermes_home
        doc = None
        reg = registry
        if reg is None:
            try:
                rp = os.environ.get("MW_REGISTRY",
                                   str(Path(__file__).parent / "registry.json"))
                reg = json.loads(Path(rp).read_text())
            except (OSError, json.JSONDecodeError):
                reg = {}
        reg_entry = (reg or {}).get(sess["profile"]) or {}
        if reg_entry.get("api_url") and reg_entry.get("api_key"):
            doc = fetch_transcript_api(reg_entry["api_url"], reg_entry["api_key"],
                                      sess["session_id"])
            if doc:
                log_parts.append("transcript来源: worker API")
        if doc is None:
            db_direct = os.environ.get("MW_WORKER_STATE_DB")
            if db_direct and Path(db_direct).exists():
                doc = read_worker_state_db(db_direct, sess["session_id"])
                if doc:
                    log_parts.append("transcript来源: state.db 直查")
        if doc is None:
            exp_profile = (None if (sess["runner_kind"]
                                   if "runner_kind" in sess.keys() else None)
                          == "api" else sess["profile"])
            doc = export_transcript(sess["session_id"], worker_home,
                                   str(Path(hermes_home) / "harvest"),
                                   profile=exp_profile)
        if doc:
            trace = parse_tool_trace(doc)
            ok_tools = sum(1 for t in trace if t["result_exit_code"] == 0)
            log_parts.append(f"transcript: {len(trace)} tool calls, "
                           f"{ok_tools} ok, end_reason={doc.get('end_reason')}")
        else:
            log_parts.append("transcript: 导出失败/不完整(降级为纯物理验收)")

    # issue #58 (D2): 结算时刻单次 token 快照 — 入口取一次, 下面 4 处
    # worker_report 共用 (AC-P4 防重试风暴)。fail-open, 永不抛。
    _tokens_snapshot = _capture_tokens(reg_entry, sess)

    # 1. worker 自报状态是权威 (边界减法 2026-10-08, 用户定调)
    # verdict 由 worker 的 TODO.md『状态』驱动, mw 不再用自己的检查推翻。
    # 判定顺序: worker_status 优先 → 缺失时回退 .clarify.json → 默认 COMPLETE。
    ws_norm = (worker_status or "").strip().lower()

    # 1a0. issue#127 S-1 — timeout 前置短路 (**必须**在 normalize 之前)。
    #
    # "timeout" 不是 worker 可写状态, 是平台内部收尸值: reaper 两条收尸
    # 路径显式传入 (reaper.py:317 超时收尸 / reaper.py:382 TODO 缺失 5min
    # 收尸)。STATUS_ALIASES 表**故意不含** timeout —— 它不该出现在 worker
    # 的 TODO.md 里, 也不该被 normalize_status 归一化。
    #
    # 若让它落入 normalize_status: normalize_status("timeout") → None
    # → 掉进 1c 未知状态兜底 (读 .clarify.json 否则按 COMPLETE)
    # → **超时收尸被静默改判 COMPLETE/clarify, 收尸路径断裂** (P0 级
    # 静默行为变更)。故此处前置显式处理, 并**禁止**把 "timeout" 塞进
    # STATUS_ALIASES (那会让 worker 字面写出的 "timeout" 被当成合法
    # failed, 破坏"worker 不可写 timeout"的边界)。
    if ws_norm == "timeout":
        why = _t("harvest.reason.failed_timeout")
        worker_report = {
            "verdict": "FAILED",
            "why": why,
            "evidence": log_parts,
            # issue #58: 快照键必有 (AC-P3, 失败终态也要有 tokens)
            "tokens": _tokens_snapshot,
        }
        ledger.update_worker_state(
            task_id, "BLOCKED", actor="harvest", reason=why,
            exit_code=exit_code,
            log=f"{ws_norm}: " + "\n".join(log_parts))
        ledger.set_worker_report(task_id, worker_report)
        _emit(ledger, "harvest.failed", task_id, worker_report)
        _teardown_current_card()
        return {"settled": True, "verdict": "FAILED",
                "worker_report": worker_report,
                "harvest_status": "BLOCKED",
                "log": log_parts}

    # 1a. failed = worker 明确宣告无法完成 (worker 自己的决策)
    #     (timeout 已在上方 1a0 前置短路处理, 不走本分支)
    #     归 FAILED + BLOCKED, 球交 main; 不新增 verdict 状态 (减法原则)。
    #
    # issue#127 S-1: 判定收敛到统一归一化入口 (issue #124 纪律), 不再写
    # 本地字面量集; 但**输入域守恒** —— 只接受改前就接受的 ASCII 英文
    # 别名 (failed/fail), 中文别名由上游 reaper/console 先归一化再传参,
    # harvest 这一层维持原样 (issue #127 §3.6 总红线: 零行为变更)。
    _norm = normalize_status(ws_norm)
    if _norm == STATUS_FAILED and ws_norm in _FAILED_ALIASES:
        why = _t("harvest.reason.failed_worker_self")
        worker_report = {
            "verdict": "FAILED",
            "why": why,
            "evidence": log_parts,
            # issue #58: 快照键必有 (AC-P3, 失败终态也要有 tokens)
            "tokens": _tokens_snapshot,
        }
        ledger.update_worker_state(
            task_id, "BLOCKED", actor="harvest", reason=why,
            exit_code=exit_code,
            log=f"{ws_norm}: " + "\n".join(log_parts))
        ledger.set_worker_report(task_id, worker_report)
        _emit(ledger, "harvest.failed", task_id, worker_report)
        _teardown_current_card()
        return {"settled": True, "verdict": "FAILED",
                "worker_report": worker_report,
                "harvest_status": "BLOCKED",
                "log": log_parts}

    # 1b. blocked = 需澄清。clarify 文件按写序铁律先于状态落盘,
    #     正常必能读到; 读不到 (worker 违约) 用兜底占位, 不干等。
    #     issue#127 S-1: 同 1a, 收敛到统一归一化入口 + 输入域守恒。
    if _norm == STATUS_BLOCKED and ws_norm in _BLOCKED_ALIASES:
        clarify = check_clarification(workspace) or {}
        payload = {"question": str(clarify.get(
            "question", _t("harvest.reason.clarify_fallback_no_question_recorded")))[:500]}
        if isinstance(clarify.get("options"), list):
            payload["options"] = [str(o) for o in clarify["options"]][:8]
        worker_report = {
            "verdict": "NEED_CLARIFICATION",
            "question": payload["question"],
            "options": payload.get("options", []),
            "evidence": log_parts,
            # issue #58: 快照键必有 (AC-P3, blocked 也要有 tokens)
            "tokens": _tokens_snapshot,
        }
        ledger.update_worker_state(
            task_id, "NEED_CLARIFICATION", actor="harvest",
            reason=_t("harvest.reason.blocked_needs_input"),
            exit_code=exit_code,
            log="clarify: " + json.dumps(payload, ensure_ascii=False))
        ledger.set_worker_report(task_id, worker_report)
        _emit(ledger, "harvest.clarify", task_id, worker_report)
        _teardown_current_card()
        return {"settled": True, "verdict": "NEED_CLARIFICATION",
                "worker_report": worker_report,
                "harvest_status": "NEED_CLARIFICATION",
                "log": log_parts}

    # 1c. worker_status 未知 (CLI 手动结算 / 旧调用方) → 回退读
    #     .clarify.json 兜底判 NEED_CLARIFICATION, 否则按 COMPLETE。
    if not ws_norm:
        clarify = check_clarification(workspace)
        if clarify:
            payload = {"question": str(clarify.get("question", str(clarify)))[:500]}
            if isinstance(clarify.get("options"), list):
                payload["options"] = [str(o) for o in clarify["options"]][:8]
            worker_report = {
                "verdict": "NEED_CLARIFICATION",
                "question": payload["question"],
                "options": payload.get("options", []),
                "evidence": log_parts,
                # issue #58: 快照键必有 (AC-P3, fallback 路径也要有 tokens)
                "tokens": _tokens_snapshot,
            }
            ledger.update_worker_state(
                task_id, "NEED_CLARIFICATION", actor="harvest",
                reason=_t("harvest.reason.clarify_fallback_unknown_worker_status"),
                exit_code=exit_code,
                log="clarify: " + json.dumps(payload, ensure_ascii=False))
            ledger.set_worker_report(task_id, worker_report)
            _emit(ledger, "harvest.clarify", task_id, worker_report)
            _teardown_current_card()
            return {"settled": True, "verdict": "NEED_CLARIFICATION",
                    "worker_report": worker_report,
                    "harvest_status": "NEED_CLARIFICATION",
                    "log": log_parts}

    # 2. 物理检查 — 只作证据, 不推翻 worker 的 completed 判定
    # 边界减法 (2026-10-08, 用户定调): worker LLM 是 worker 端决策者,
    # 写 completed 就是它自主判定完成。mw 用文件存在性推翻它并落
    # BLOCKED/PHYSICAL_ERROR 是越权 —— 产物齐不齐是「事实」, 作为
    # evidence 随报告给 main, 由 main 决定 resume/改方向/建后续卡。
    ok, why = verify_physical(contract, workspace, product_workspace)
    log_parts.append(why)

    # pass_count / required_passes 管道已废弃 (2026-10-08 边界减法):
    # 旧 PASS_BUMPED「同卡保持 PENDING 重跑」= mw 替 main 决定 loop, 已删。
    # worker 交卷即本轮结束, loop 与否是 main 读报告后的编排 —— 要「测 3 次」
    # 就建 3 张卡, 每张跑一轮。mw 不再计数, 也不再往报告里塞这两个字段。
    # (DB 列 pass_count/required_passes 保留不动: 已部署库有历史数据,
    #  删列需迁移, 留着无害且可供事后审计。)

    # worker 交卷 (completed) = 结算, 物理缺失只进 evidence
    # 结构化产物证据 (2026-10-05): 给 main 看的「产物落盘事实 + 静态扫描」,
    # 减少 main 独立冒烟的回合。零副作用只读,失败不阻断 harvest。
    try:
        products = _collect_product_evidence(contract, workspace, product_workspace)
    except Exception as _e:
        products = {"files": [], "static_check": {"passed": True, "issues": [],
                       "scanned_blocks": 0},
                    "collect_error": str(_e)[:200]}
    worker_report = {
        "verdict": "COMPLETE",
        "product_workspace": product_workspace,
        "evidence": log_parts,
        "products": products,
        # 物理检查降级为证据字段: main 看到 passed=False 自行决定补救,
        # mw 不再据此写 BLOCKED / 不再同卡重跑。
        "physical_check": {"passed": ok, "detail": why},
        # issue #58: 快照键必有 (AC-P3, complete 也要有 tokens)
        "tokens": _tokens_snapshot,
    }
    ledger.update_worker_state(task_id, "HARVESTED", actor="harvest",
                              reason="worker 交卷结算",
                              exit_code=exit_code,
                              log="\n".join(log_parts))
    ledger.set_worker_report(task_id, worker_report)
    _emit(ledger, "harvest.complete", task_id, worker_report)
    _teardown_current_card()
    # (2026-10-08) 边界重划:reaper/harvest 永不 auto-close run。
    # 「无未结算契约」是「run 名下契约层完成」的信号,但 run 是否关闭
    # 由 main(读完最后一张报告后调 close)或操作员手动决定。
    # 保留信号:emit `team_work.run_idle` 给 main,main 看到「无遗留 +
    # 自己已把最后一张 done」就调 /api/team-work/<run>/close。
    # settle_team_work_residue 的副作用会让 count==0 提前成立 (advance
    # 把 N+1..end 全标 HARVESTED), 所以这里只看「最后一张是 COMPLETE
    # 落账 + current_card_id 即将被清」, 由 main turn 决定。
    if _tw_run_id:
        try:
            _remaining = ledger.conn.execute(
                "SELECT COUNT(*) AS n FROM harvest_contracts "
                "WHERE team_work_run_id=? AND harvest_status IN "
                "('PENDING','ERROR','BLOCKED','NEED_CLARIFICATION')",
                (_tw_run_id,)).fetchone()
            if _remaining and _remaining["n"] == 0:
                _emit(ledger, "team_work.run_idle", "", {
                    "run_id": _tw_run_id,
                    "completed_card_id": task_id,
                    "reason": "no_residue_after_complete",
                    "hint": ("main 读完这份报告后,如确认链走完,"
                            "请主动调 POST /api/team-work/<run_id>/close。"
                            " mw 不再自动 close,避免越权关闭正在跑的链。"),
                })
        except Exception:
            pass  # 事件失败不影响结算
    return {"settled": True, "verdict": "COMPLETE",
            "worker_report": worker_report,
            "harvest_status": "HARVESTED",
            "log": log_parts}


def _emit(ledger, ev_type: str, task_id: str, payload: dict):
    """事件 emit (best-effort, 失败不影响结算)。"""
    try:
        ledger.emit_event(ev_type, task_id, payload)
        from .console import dispatch_webhook
        dispatch_webhook({"event_type": ev_type, "task_id": task_id,
                          "payload": payload})
    except Exception:
        pass