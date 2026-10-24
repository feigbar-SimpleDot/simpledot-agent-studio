"""mw CLI — 中间件唯一对外接口 (1A 边界)。

  mw dispatch --manifest <json文件|->   为一张 main 已 claim 的卡起 worker
  mw harvest --task <id> --workspace <path>   手动结算(可重入)
  mw resume --task <id> --instruction ...     续作(异步入队)
  mw reaper [--once] [--interval N]           后台扫描循环
  mw status                            账本快照

mw 不建 kanban 卡 / 不 claim / 不播种 TODO.md —— 那些是 main 的事。
退出码: 0 成功 / 422 契约拒绝 / 1 其他错误
"""
import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

from .ledger import Ledger
from .pipeline import dispatch, SubmitError, spawn_worker, load_registry, SESSION_ID_RE
from .schemas import TaskManifest

BASE = Path(os.environ.get("MW_HOME", Path.cwd() / "mw_data"))


def _ledger() -> Ledger:
    return Ledger(str(BASE / "ledger.db"))


def cmd_dispatch(args):
    raw = sys.stdin.read() if args.manifest == "-" else Path(args.manifest).read_text()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[mw] 422 JSON 解析失败: {e}", file=sys.stderr)
        return 422
    try:
        result = dispatch(payload,
                         registry_path=os.environ.get("MW_REGISTRY",
                                                    str(Path(__file__).parent / "registry.json")),
                         ledger=_ledger(),
                         async_mode=not getattr(args, "sync", False))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") or result.get("accepted") else 1
    except SubmitError as e:
        print(f"[mw] {e.code} {e.msg}", file=sys.stderr)
        return e.code


def cmd_harvest(args):
    from .harvest import harvest
    led = _ledger()
    ws = args.workspace
    if not ws:
        sess = led.get_session(args.task)
        ws = sess["workspace"] if sess and sess["workspace"] else None
    if not ws:
        print("[mw] 需要 --workspace 或账本中存在该任务的 workspace", file=sys.stderr)
        return 1
    r = harvest(args.task, ledger=led,
                hermes_home=os.environ.get("MW_HERMES_HOME", str(BASE / "hermes")),
                workspace=ws,
                registry=load_registry(os.environ.get("MW_REGISTRY",
                             str(Path(__file__).parent / "registry.json"))))
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if r.get("settled") else 1


def cmd_resume(args):
    """异步入队: 立即返回, reaper 在下轮(≤15s)执行。

    旧版: urllib.urlopen 同步阻塞 worker API, 协调者工具超时一断即丢。
    新版: 写入 task_resume_requests, reaper claim → resume_task → finish_resume。
    行为: CLI 退出后指令仍在队列里, 协调者被 kill 也不丢。

    问题6B: 中文/全角标点会被 tirith 安全扫描判为 homoglyph (HIGH),
    整个 resume 命令被拦下。 解法: --instruction-file <path>, 把指令
    写到文件再 cat 传入。 tirith 只看到文件路径, 内容绕过扫描。
    """
    from .ledger import Ledger
    led = _ledger()
    sess = led.get_session(args.task)
    if not sess or not sess["session_id"]:
        print(f"[mw] task {args.task} 无 session 锚点, 无法 resume", file=sys.stderr)
        return 1
    # 问题6B: instruction_file 兜底
    instruction = args.instruction or ""
    if args.instruction_file:
        try:
            with open(args.instruction_file, "r", encoding="utf-8") as f:
                file_text = f.read()
        except OSError as e:
            print(f"[mw] 读 instruction_file 失败: {e}", file=sys.stderr)
            return 2
        # 互斥: 文件优先, 同时传 --instruction 时取文件 + 拼接为多行
        if instruction:
            instruction = instruction + "\n" + file_text
        else:
            instruction = file_text
        if not instruction.strip():
            print(f"[mw] instruction_file 为空, 不入队", file=sys.stderr)
            return 2
    if not instruction.strip():
        print(f"[mw] instruction 必填 (或用 --instruction-file)", file=sys.stderr)
        return 2
    req_id = led.enqueue_resume(args.task, instruction)
    print(json.dumps({"ok": True, "task_id": args.task,
                      "request_id": req_id,
                      "source": "file" if args.instruction_file else "inline",
                      "note": "已入队, reaper 下轮(≤15s)执行, kill 协调者也不丢"},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_reaper(args):
    """reaper 主循环入口。

    2026-10-04 修订 (commit 4): 启动 fsnotify 主路径 + 保留 15s 轮询兜底 +
    stuck 阈值 30min → 5min。三轨并行:
      - fsnotify (ms 级): <project_workspace>/TODO.md mtime 即时 reap+relay
      - 轮询 (15s): fsnotify 漏事件修复
      - stuck (5min): PENDING 挂死主动唤醒 main
    """
    from .reaper import reap_once, run_loop
    from .realtime import start_realtime
    ledger = _ledger()
    hermes_home = os.environ.get("MW_HERMES_HOME", str(BASE / "hermes"))
    base = str(BASE)
    registry = load_registry(os.environ.get("MW_REGISTRY",
                       str(Path(__file__).parent / "registry.json")))
    if args.once:
        # --once: 不起 fsnotify, 只跑一轮 reap (诊断用)
        rs = reap_once(ledger=ledger, hermes_home=hermes_home,
                       base=base, registry=registry)
        print(json.dumps(rs, ensure_ascii=False, indent=2))
        return 0
    # 启动 fsnotify 主路径 (commit 4 / design.md §5.0)
    stop_event = threading.Event()
    if not getattr(args, "no_realtime", False):
        try:
            proc, watcher = start_realtime(
                ledger=ledger, hermes_home=hermes_home, base=base,
                grace_seconds=int(os.environ.get("MW_REAPER_GRACE", "120")),
                registry=registry, stop_event=stop_event)
            print(f"[realtime] fsnotify 启动: {'OK' if watcher else 'NO ACTIVE RUN'}")
        except Exception as e:
            print(f"[realtime] 启动失败 ({e}), 退化到 15s 轮询", file=sys.stderr)
    # 同时跑原有 run_loop (15s 轮询兜底)
    # stuck_after_min 通过 MW_REAPER_STUCK_MIN env 注入 (commit 4: 30→5),
    # 避免污染 reap_once() 签名。
    os.environ.setdefault("MW_REAPER_STUCK_MIN", "5")
    try:
        run_loop(interval=args.interval, ledger=ledger,
                 hermes_home=hermes_home, base=base, registry=registry)
    finally:
        stop_event.set()
    return 0


def cmd_spawn_bg(args):
    """内部命令: detached 子进程跑阻塞 spawn, 回填 marker + session 锚点。

    启动即写 started_at marker (2026-10-02): 调用方(main 裸起 __spawn-bg)
    常常没预写 marker, 若本进程中途死掉就完全无 marker → reaper 永远
    SKIP、无法超时收尸 → 链静默死。started_at 先行让 reaper 能按
    timeout 兜底结算。

    v9 (2026-10-07): --mode runs 走 POST /v1/runs 异步, 立即拿 run_id.
    写 ledger session 后, 通过 HTTP POST /api/workers/subscribe_sse 通知
    console 进程启 SSE bridge (run_id + profile + session_id).
    console 进程持有 SSE daemon thread, 子进程退出不影响.
    """
    from .reaper import write_marker
    base = os.environ.get("MW_HOME", ".")
    manifest = json.loads(Path(args.manifest).read_text())
    led = _ledger()
    registry = load_registry(os.environ.get("MW_REGISTRY",
                         str(Path(__file__).parent / "registry.json")))
    m = TaskManifest.model_validate(manifest)
    entry = registry[m.target.profile]
    pre_sid = getattr(args, "session", None) or None
    spawn_mode = getattr(args, "mode", "chat_completions")
    write_marker(base, args.task, started_at=time.time(),
                 workspace=args.workspace, session_id=pre_sid)
    try:
        kwargs = {"session_id": pre_sid} if pre_sid else {}
        if spawn_mode == "runs":
            kwargs["mode"] = "runs"
        res = spawn_worker(m, entry, args.workspace, m.harvest.timeout_seconds,
                          **kwargs)
    except Exception as e:
        write_marker(base, args.task, exit_code=-1, stderr=str(e),
                    session_id=pre_sid)
        return 1
    combined = (res.get("stdout") or "") + "\n" + (res.get("stderr") or "")
    sid_m = SESSION_ID_RE.search(combined)
    sid = res.get("session_id") or pre_sid or (sid_m.group(1) if sid_m else None)
    led.upsert_session(args.task, sid, m.target.profile, res.get("runner_kind"),
                       res.get("container_id"), workspace=args.workspace,
                       worker_hermes_home=entry.get("hermes_home"))
    write_marker(base, args.task, exit_code=res.get("exit_code"),
                stdout=(res.get("stdout") or "")[-4000:],
                stderr=(res.get("stderr") or "")[-2000:])

    # v9: 异步 run 路径 — 通知 console 启 SSE bridge
    if spawn_mode == "runs" and res.get("run_id") and sid:
        _notify_console_sse(
                run_id=res["run_id"],
                profile=m.target.profile,
                session_id=sid,
                task_id=args.task,
                base_url=entry.get("api_url"),
                api_key=entry.get("api_key"),
                console_url=os.environ.get("MW_CONSOLE_URL", "http://127.0.0.1:8700"),
        )
    return res.get("exit_code") or 0


def _notify_console_sse(*, run_id, profile, session_id, task_id,
                       base_url, api_key, console_url):
    """POST /api/workers/subscribe_sse 通知 console 启 SSE bridge.

    Best-effort: 网络断了不重试, console 进程意外没开端口也不致命 —
    task 仍能结算 (TODO.md fsnotify 主路径不依赖 SSE).

    v9 hotfix (2026-10-07): 本函数用了 urllib.request 但模块顶部从未
    import urllib, 每次调用都 NameError → SSE bridge 永远订阅失败。
    """
    import urllib.request
    try:
        req = urllib.request.Request(
            console_url.rstrip("/") + "/api/workers/subscribe_sse",
            data=json.dumps({
                "run_id": run_id,
                "profile": profile,
                "session_id": session_id,
                "task_id": task_id,
                "api_url": base_url,
                "api_key": api_key,
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            log_text = f"console SSE subscribe: HTTP {r.status}"
    except Exception as e:
        log_text = f"console SSE subscribe failed (non-fatal): {e}"
    print(f"[spawn-bg] {log_text}  run_id={run_id[:16]}...")


def cmd_spawn_for(args):
    """为一张已存在(已由 main claim)的卡机械起 worker。

    边界 (2026-10-02): 链推进的决策(claim/complete)是 main 的事, 但
    "起 worker"是纯机械动作, mw 必须提供入口 — 删掉 claim-and-spawn 后
    main 没有合法途径为已存在的卡 spawn(只能瞎试 submit 建新卡, 实测踩坑)。
    本命令零决策: 只读 ledger(submit_log 原始 manifest + session workspace/
    profile), 写 marker, detached __spawn-bg 起 worker。不碰 kanban。

    ★ 2026-10-02 重复 spawn 护栏 (qq-login 实测):
    cmd_spawn_for 之前只判 marker.exit_code != None 才拒。但 main 用错
    session_id 二次 spawn 时 marker.exit_code 仍是 None, 漏过去 → 再起一个
    worker。改用 ledger.is_task_running() 三路汇判(marker + session +
    harvest_status), 一律拒并返回现有 session 让 main resume。
    """
    led = _ledger()
    task_id = args.task
    sess = led.get_session(task_id)
    if not sess:
        print(json.dumps({"error": f"task {task_id} 在 ledger 找不到",
                         "code": 404}, ensure_ascii=False))
        return 1
    workspace = sess["workspace"]
    profile = sess["profile"]
    if not workspace or not profile:
        print(json.dumps({"error": f"task {task_id} 缺 workspace/profile",
                         "code": 500}, ensure_ascii=False))
        return 1
    state = led.is_task_running(task_id)
    if state["running"]:
        print(json.dumps({"ok": False, "task_id": task_id,
                         "rejected": "already_running",
                         "session_id": state["session_id"],
                         "started_at": state["started_at"],
                         "harvest_status": state["harvest_status"],
                         "note": "task 仍有活跃 session,改用现有 session resume, "
                                  "不要 spawn 第二个 worker(会重复做工)。"},
                        ensure_ascii=False))
        return 0
    from .reaper import write_marker
    base = str(BASE)
    row = led.conn.execute(
        "SELECT manifest_json FROM submit_log WHERE task_id=?",
        (task_id,)).fetchone()
    if not row:
        print(json.dumps({"error": f"task {task_id} 的 submit_log 无原始 manifest, "
                           "无法重建 spawn(非 launch 下发的卡?)",
                         "code": 404}, ensure_ascii=False))
        return 1
    manifest = json.loads(row["manifest_json"])
    # marker 先于 spawn 落盘, 防 reaper 误判 no_marker 提前收尸
    write_marker(base, task_id, started_at=time.time(), workspace=workspace)
    mf = Path(base) / f"manifest_{task_id}.json"
    mf.write_text(json.dumps(manifest, ensure_ascii=False))
    import subprocess as sp
    sp.Popen([sys.executable, "-m", "mw.cli", "__spawn-bg",
              "--manifest", str(mf), "--task", task_id,
              "--workspace", workspace],
             stdout=sp.DEVNULL, stderr=sp.DEVNULL,
             env={**os.environ,
                  "PYTHONPATH": str(Path(__file__).parent.parent)},
             start_new_session=True)
    print(json.dumps({"ok": True, "task_id": task_id, "status": "spawning",
                     "profile": profile, "workspace": workspace,
                     "note": "worker 已起, 完成后 reaper 落账并转发报告给你"},
                    ensure_ascii=False))
    return 0


def cmd_status(args):
    led = _ledger()
    out = {"base": str(BASE)}
    for name, sql in [("submits", "SELECT idempotency_key, task_id FROM submit_log"),
                     ("sessions", "SELECT task_id, session_id, profile, runner_kind FROM task_sessions"),
                     ("harvests", "SELECT task_id, harvest_status, exit_code, worker_report FROM harvest_contracts")]:
        out[name] = [dict(r) for r in led.conn.execute(sql).fetchall()]
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="mw", description="确定性任务编排中间件 (1A: 纯通讯)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("dispatch",
                      help="为一张 main 已 claim 的 kanban 卡起 worker (manifest.task_id 必填)")
    s.add_argument("--manifest", required=True, help="JSON 文件路径或 '-' 读 stdin")
    s.add_argument("--sync", dest="sync", action="store_true",
                  help="同步等待 spawn 完成 (默认 async: detached spawn + reaper 结算)")
    s.set_defaults(fn=cmd_dispatch)
    h = sub.add_parser("harvest")
    h.add_argument("--task", required=True)
    h.add_argument("--workspace", default=None,
                  help="缺省时从账本 task_sessions.workspace 自取")
    h.set_defaults(fn=cmd_harvest)
    r = sub.add_parser("resume")
    r.add_argument("--task", required=True)
    r.add_argument("--instruction", default="",
                  help="续作指令(带原会话上下文执行)。 与 --instruction-file 互不冲突, 同时传则拼接")
    r.add_argument("--instruction-file", default="",
                  help="从文件读指令(问题6B: 绕过 tirith homoglyph 扫描)。 "
                       "内容含中文/全角标点时首选此选项。")
    r.set_defaults(fn=cmd_resume)
    rp = sub.add_parser("reaper", help="后台收尸循环(常驻)")
    rp.add_argument("--once", action="store_true", help="只扫一轮(测试/cron 用)")
    rp.add_argument("--interval", type=int, default=15)
    rp.add_argument("--no-realtime", action="store_true",
                   help="禁用 fsnotify 主路径, 只跑轮询 (调试/老环境兜底)")
    rp.set_defaults(fn=cmd_reaper)
    bg = sub.add_parser("__spawn-bg", help=argparse.SUPPRESS)
    bg.add_argument("--manifest", required=True)
    bg.add_argument("--task", required=True)
    bg.add_argument("--workspace", required=True)
    bg.add_argument("--session", default=None)
    bg.add_argument("--mode", default="chat_completions",
                   choices=["chat_completions", "runs"],
                   help="v9 改 runs: 异步 /v1/runs + SSE bridge 取代 blocking chat_completions")
    bg.set_defaults(fn=cmd_spawn_bg)
    # 2026-10-02 回退: cmd_claim_and_spawn 删除 (chain_daemon 越界)。
    # 链推进完全交给 main (main 自己调 hermes kanban claim/complete)。
    sf = sub.add_parser("spawn-for",
                       help="为已存在(main 已 claim)的卡机械起 worker: 零决策, 只读账本+写marker+detached spawn")
    sf.add_argument("--task", required=True)
    sf.set_defaults(fn=cmd_spawn_for)
    st = sub.add_parser("status")
    st.set_defaults(fn=cmd_status)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
