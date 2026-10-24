"""Hermes Gateway SSE bridge (v9, 2026-10-07).

Model X: main-side long-lived SSE connections to each worker's Hermes gateway.

For every dispatched task card, mw.open_bridge() opens one
GET /v1/runs/{run_id}/events connection in a daemon thread. Events update
the module-global `_worker_live[(profile, session_id)]` dict, which is read
synchronously by `mw.reaper._notify_main_of_stuck` to suppress stuck alerts
when the worker is provably alive. The connection closes itself on any
terminal event (run.completed / run.failed / run.cancelled, FIX-1) and
removes itself from the active subscription set. Non-terminal disconnects
reconnect with exponential backoff (FIX-5); an HTTP 404 on reconnect is
treated as terminal (run gone).

Single authority for LIVENESS = this in-memory dict.
Single authority for SETTLEMENT = fsnotify on TODO.md (mw.realtime).

Stdlib only (sqlite3 / urllib / threading / json). No third-party deps.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger("mw.sse_bridge")

# --------------------------------------------------------------------------
# Module-global state
# --------------------------------------------------------------------------

#: open SSE subscriptions indexed by run_id
_subscriptions: dict[str, "Subscription"] = {}
#: worker liveness snapshot indexed by (profile, session_id)
_worker_live: dict[tuple[str, str], dict] = {}
_lock = threading.Lock()

#: 终态事件集合 (FIX-1): 网关 run 生命周期以这三个事件收尾。
#: 规则「非终态即活动」—— 对未来新增事件名天然免疫, 不再靠枚举活动事件。
_TERMINAL_EVENTS = frozenset({"run.completed", "run.failed", "run.cancelled"})

#: 断流重连退避参数 (FIX-5): 2s 起指数翻倍, 封顶 60s, 直到终态或 cancel。
_RECONNECT_BASE_S = 2.0
_RECONNECT_MAX_S = 60.0


# --------------------------------------------------------------------------
# Subscription handle returned to callers
# --------------------------------------------------------------------------

@dataclass
class Subscription:
    run_id: str
    profile: str
    session_id: str
    base_url: str
    api_key: str
    on_event: Optional[Callable[[dict], None]] = None
    _thread: Optional[threading.Thread] = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)

    last_event_ts: float = 0.0
    last_event_type: str = ""
    event_count: int = 0

    def cancel(self) -> None:
        """Stop the daemon thread and remove from registry. Idempotent."""
        self._stop.set()
        with _lock:
            _subscriptions.pop(self.run_id, None)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def open_bridge(
    *,
    run_id: str,
    profile: str,
    session_id: str,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    on_event: Optional[Callable[[dict], None]] = None,
) -> Subscription:
    """Open one SSE long connection. Returns the Subscription immediately;
    the daemon thread continues to live in the background. Caller may
    ignore the handle but should keep a reference until run.completed
    so it isn't garbage-collected mid-stream.
    """
    base_url = (base_url or os.environ.get("MW_GATEWAY_URL", "http://127.0.0.1:8642")).rstrip("/")
    api_key = api_key or os.environ.get("API_SERVER_KEY", "")
    sub = Subscription(
        run_id=run_id,
        profile=profile,
        session_id=session_id,
        base_url=base_url,
        api_key=api_key,
        on_event=on_event,
    )
    sub._thread = threading.Thread(
        target=_run_subscription,
        args=(sub,),
        name=f"sse-bridge/{run_id[:12]}",
        daemon=True,
    )
    with _lock:
        _subscriptions[run_id] = sub
    sub._thread.start()
    log.info("opened SSE bridge run_id=%s profile=%s", run_id, profile)
    return sub


def is_thinking(profile: str, session_id: str) -> bool:
    """Whether the (profile, session_id) currently has an active SSE event
    stream we believe represents in-flight reasoning.

    Returns False if there is no live entry or thinking flag is False.
    """
    with _lock:
        live = _worker_live.get((profile, session_id))
    if not live:
        return False
    return bool(live.get("thinking"))


def live_snapshot() -> dict[tuple[str, str], dict]:
    """Return a copy of the liveness dict (for tests / debug / operator view)."""
    with _lock:
        return {k: dict(v) for k, v in _worker_live.items()}


def active_subscriptions() -> list[str]:
    """List of currently-open run_ids."""
    with _lock:
        return list(_subscriptions.keys())


def close_all() -> None:
    """Stop all subscriptions and join daemon threads (call on subprocess shutdown)."""
    with _lock:
        subs = list(_subscriptions.values())
        _subscriptions.clear()
    for sub in subs:
        sub.cancel()
    for sub in subs:
        if sub._thread is not None and sub._thread.is_alive():
            sub._thread.join(timeout=2.0)


# --------------------------------------------------------------------------
# Internal: background thread body
# --------------------------------------------------------------------------

def _run_subscription(sub: Subscription) -> None:
    """Loop: connect SSE, parse events, update _worker_live, reconnect on error.
    Stops on sub._stop or after a terminal event (run.completed/failed/cancelled).

    FIX-5: 非终态断连不再「重试 3 次即永久放弃」, 改为指数退避持续重连
    (2s/4s/8s... 封顶 60s), 直到收到终态事件或 sub.cancel()。
    重连拿到 HTTP 404 (run 已结束, rollout 实测) 视为终态退出, 防悬挂。
    """
    backoff = _RECONNECT_BASE_S
    try:
        while not sub._stop.is_set():
            try:
                _stream_once(sub)
                # _stream_once returned cleanly: terminal event received, exit.
                return
            except _Terminal:
                return
            except urllib.error.HTTPError as e:
                # 404 = run 不存在/已结束: 视为终态, 不再重连。
                if e.code == 404:
                    log.info("SSE bridge run not found (404), terminal exit "
                             "run_id=%s", sub.run_id)
                    _mark_terminal(sub)
                    return
                # 其他 HTTP 错误按断流处理, 继续退避重连。
                log.info("SSE bridge HTTP %d, reconnect in %.0fs run_id=%s",
                         e.code, backoff, sub.run_id)
            except Exception as e:  # URLError, ConnectionError, json, OS
                log.info("SSE bridge reconnect in %.0fs run_id=%s: %s",
                         backoff, sub.run_id, e)
            # 指数退避; 用 stop.wait 让 cancel 能立即打断睡眠。
            if sub._stop.wait(backoff):
                return
            backoff = min(backoff * 2, _RECONNECT_MAX_S)
    finally:
        with _lock:
            _subscriptions.pop(sub.run_id, None)


def _mark_terminal(sub: Subscription) -> None:
    """404 终态兜底: 把该 worker 的 thinking 置 False, 防陈旧活体残留。"""
    with _lock:
        live = _worker_live.get((sub.profile, sub.session_id))
        if live is not None:
            live["thinking"] = False
            live["last_event_type"] = "run.gone(404)"


class _Terminal(Exception):
    """Raised when the subscription has terminated (run.completed or cancelled)."""


def _stream_once(sub: Subscription) -> None:
    """Open the GET one SSE and walk the stream until run.completed or error.

    Raises _Terminal on run.completed (so caller does not retry).
    Raises generic Exception on network errors (caller decides to retry).

    NOTE: Hermes Gateway 用 Transfer-Encoding: chunked, 但 Python 3.13 urllib
    在 aiohttp server 上自动 dechunk (实测 for raw in resp 返回真实 data,
    无 chunked framing). 我们直接 line-by-line 处理.
    """
    url = f"{sub.base_url}/v1/runs/{sub.run_id}/events"
    req = urllib.request.Request(url, headers={
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {sub.api_key}",
    })
    with urllib.request.urlopen(req, timeout=None) as resp:
        if resp.status != 200:
            raise RuntimeError(f"SSE connect HTTP {resp.status}")
        log.info("SSE connected run_id=%s transfer=%s", sub.run_id,
                 resp.headers.get("Transfer-Encoding", "identity"))
        for raw in resp:
            if sub._stop.is_set():
                raise _Terminal()
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            if line.startswith(":"):
                # keepalive ping (实测 10s)
                continue
            if not line.startswith("data:"):
                continue
            payload_str = line[5:].strip()
            if not payload_str or payload_str == "[DONE]":
                continue
            try:
                payload = json.loads(payload_str)
            except json.JSONDecodeError:
                log.debug("drop non-json SSE line: %r", payload_str[:120])
                continue
            _handle_event(sub, payload)
            # FIX-1: run.failed / run.cancelled 同样是终态, 退出不重连。
            if payload.get("event") in _TERMINAL_EVENTS:
                raise _Terminal()
    # for 循环自然走完 = 流被对端关闭但没收到终态事件: 属断流, 抛给
    # 上层按 FIX-5 退避重连 (不能当终态, 否则断流即永久失明)。
    raise ConnectionError("SSE stream closed without terminal event")


def _handle_event(sub: Subscription, payload: dict) -> None:
    """Update sub counters, _worker_live, and dispatch on_event callback."""
    now = time.time()
    ev = payload.get("event", "")
    sub.last_event_ts = now
    sub.last_event_type = ev
    sub.event_count += 1

    # FIX-1: 「非终态即活动」—— 任何非终态事件都算活体 (assistant.delta /
    # assistant.commentary / tool.progress 等真实网关事件全覆盖, 旧事件名
    # message.delta / reasoning.available 同样天然在内)。
    # keepalive 是 ':' 注释行, 不进本函数, 不会伪装活体。
    # thinking 从首个活动事件起为 True, 直到终态事件才 False —— 正好覆盖
    # 单个长工具调用中间无事件的空窗。
    is_terminal = ev in _TERMINAL_EVENTS

    with _lock:
        live = _worker_live.setdefault((sub.profile, sub.session_id), {
            "session_id": sub.session_id,
            "last_run_id": sub.run_id,
            "last_event_ts": now,
            "last_event_type": ev,
            "thinking": False,
            "event_count": 0,
        })
        live["last_run_id"] = sub.run_id
        live["last_event_ts"] = now
        live["last_event_type"] = ev
        live["event_count"] = int(live.get("event_count", 0)) + 1
        live["thinking"] = not is_terminal

    if sub.on_event is not None:
        try:
            sub.on_event(payload)
        except Exception:
            log.exception("SSE on_event callback raised run_id=%s", sub.run_id)