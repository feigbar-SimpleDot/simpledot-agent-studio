"""#193 守护: console.py 500 兜底 str(e) 回显收口 + L-O1-1 PUT/PATCH body 闸。

依据: docs/TRIAGE-S-20261102.md §2.5 / docs-source/issue-batch-20261103-s/
ISSUES-S.md #193 段。先例: #176 do_POST (tests/test_issue190_console_hardening.py
D 组同款口径)。

覆盖:
  E193-outer  do_GET / do_PUT / do_DELETE 外层兜底:
              端点抛 RuntimeError ⇒ 500 + 固定文案 internal_error,
              响应体不含异常原文; 服务端日志保留全量异常 (含栈)。
  E193-cron   api_cron_jobs / api_cron_runs / api_cron_action 函数体内
              home 不一致 ValueError ⇒ 500 + 固定文案, 不回显 str(e)。
  E193-gate   do_PUT / do_PATCH 预读复用 POST_BODY_MAX_BYTES 闸:
              超限 ⇒ 413 payload_too_large 且 rfile 零次读;
              等于上限 ⇒ 不被 413 拦 (严格大于, 边界不误伤)。
  E193-src    源码级守卫: mw/console.py 不再存在 500 + str(e) 回显形态
              (外层兜底与 cron 函数体全部收口)。

双口径断言 (照抄 #190 守护):
  pytest 收集口径 —— check() 失败即抛 AssertionError;
  脚本直跑口径   —— main() 置 _COLLECT_ONLY=True, 只收集不抛, rc 汇总。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mw.console as console  # noqa: E402

PASS, FAIL = [], []
_COLLECT_ONLY = False


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    line = f"  {'PASS' if cond else 'FAIL'}  {name}"
    if detail:
        line += f"  {detail}"
    print(line)
    if not _COLLECT_ONLY:
        assert cond, f"{name} {detail}"


# ── 夹具 (照抄 #190 守护口径) ──────────────────────────────────────────

class _FakeRfile:
    def __init__(self, body: bytes = b""):
        self.body = body
        self.reads = 0

    def read(self, n):
        self.reads += 1
        return self.body[:n]


class _FakeHandler(console.Handler):
    """绕开 BaseHTTPRequestHandler.__init__ (不联网), 只装路由所需属性。"""

    def __init__(self, path, headers=None, body=b""):
        self.path = path
        self.headers = headers or {}
        self.status = None
        self.sent_headers = []
        self.body = b""
        self.wfile = self
        self.rfile = _FakeRfile(body)

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.sent_headers.append((k, v))

    def end_headers(self):
        pass

    def write(self, chunk):
        self.body += bytes(chunk)
        return len(chunk)


class _LogCapture(logging.Handler):
    """捕获 mw.console logger 的记录 (propagate=False, caplog 抓不到)。"""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


SECRET = "/opt/mwroot/mw/console.py internal-identifier-193"


def _assert_no_leak(tag, h):
    check(f"{tag} 端点异常 ⇒ 500", h.status == 500, f"got {h.status}")
    payload = json.loads(h.body or b"{}")
    check(f"{tag} 响应体 error == internal_error",
          payload.get("error") == "internal_error", f"got {payload!r}")
    check(f"{tag} 响应体不含异常原文",
          SECRET.encode() not in h.body, f"got {h.body!r}")
    check(f"{tag} 响应体不含 'Traceback'",
          b"Traceback" not in h.body, f"got {h.body!r}")


# ── E193-outer: do_GET / do_PUT / do_DELETE 外层兜底 ──────────────────

def test_e193_do_get_outer_fallback_no_leak():
    h = _FakeHandler("/api/team-work/r1/state")
    with mock.patch.object(console, "api_team_work_state",
                           side_effect=RuntimeError(SECRET)):
        h.do_GET()
    _assert_no_leak("E193 do_GET", h)


def test_e193_do_put_outer_fallback_no_leak():
    h = _FakeHandler("/api/workers", {"Content-Length": "2"}, b"{}")
    with mock.patch.object(console, "api_registry_save",
                           side_effect=RuntimeError(SECRET)):
        h.do_PUT()
    _assert_no_leak("E193 do_PUT", h)


def test_e193_do_delete_outer_fallback_no_leak():
    h = _FakeHandler("/api/workers/x")
    with mock.patch.object(console, "api_registry_delete",
                           side_effect=RuntimeError(SECRET)):
        h.do_DELETE()
    _assert_no_leak("E193 do_DELETE", h)


def test_e193_server_log_keeps_full_exception():
    logger = logging.getLogger("mw.console")
    cap = _LogCapture()
    logger.addHandler(cap)
    try:
        h = _FakeHandler("/api/workers", {"Content-Length": "2"}, b"{}")
        with mock.patch.object(console, "api_registry_save",
                               side_effect=RuntimeError(SECRET)):
            h.do_PUT()
    finally:
        logger.removeHandler(cap)
    msgs = [r.getMessage() for r in cap.records]
    check("E193 日志含 do_PUT 失败记录",
          any("do_PUT" in m for m in msgs), f"got {msgs}")
    check("E193 日志含异常原文 (排障不丢)",
          any(SECRET in m for m in msgs), f"got {msgs}")
    check("E193 记录带异常栈 (exc_info)",
          any(r.exc_info for r in cap.records), "no exc_info")


def test_e193_value_error_branches_still_422():
    """422 分支保留 str(e) (用户可读校验文案), 不得被收口误伤。"""
    msg = "非法工作目录名: 'Bad Name'"
    h = _FakeHandler("/api/workers", {"Content-Length": "2"}, b"{}")
    with mock.patch.object(console, "api_registry_save",
                           side_effect=ValueError(msg)):
        h.do_PUT()
    check("E193 do_PUT ValueError ⇒ 422", h.status == 422, f"got {h.status}")
    check("E193 do_PUT ValueError ⇒ 保留 str(e)",
          json.loads(h.body or b"{}").get("error") == msg,
          f"got {h.body!r}")
    h2 = _FakeHandler("/api/workers/x")
    with mock.patch.object(console, "api_registry_delete",
                           side_effect=ValueError(msg)):
        h2.do_DELETE()
    check("E193 do_DELETE ValueError ⇒ 422 保留 str(e)",
          h2.status == 422
          and json.loads(h2.body or b"{}").get("error") == msg,
          f"got {h2.status}/{h2.body!r}")


# ── E193-cron: 三个 cron 函数体 500 收口 ──────────────────────────────

def _cron_calls():
    """三个 cron API 在 _cron_assert_home 抛 ValueError 时的返回。"""
    out = []
    with mock.patch.object(console, "_cron_bridge",
                           return_value={"ok": True}), \
         mock.patch.object(console, "_cron_assert_home",
                           side_effect=ValueError(SECRET)):
        out.append(console.api_cron_jobs())
        out.append(console.api_cron_runs("j1", None))
        out.append(console.api_cron_action("j1", "pause"))
    return out


def test_e193_cron_functions_no_leak():
    for name, (code, body) in zip(
            ("api_cron_jobs", "api_cron_runs", "api_cron_action"),
            _cron_calls()):
        check(f"E193 {name} ⇒ 500", code == 500, f"got {code}")
        check(f"E193 {name} error == internal_error",
              body.get("error") == "internal_error", f"got {body!r}")
        check(f"E193 {name} 不回显异常原文",
              SECRET not in json.dumps(body, ensure_ascii=False),
              f"got {body!r}")


def test_e193_cron_server_log_keeps_exception():
    logger = logging.getLogger("mw.console")
    cap = _LogCapture()
    logger.addHandler(cap)
    try:
        _cron_calls()
    finally:
        logger.removeHandler(cap)
    msgs = [r.getMessage() for r in cap.records]
    for fn in ("api_cron_jobs", "api_cron_runs", "api_cron_action"):
        check(f"E193 日志含 {fn} 失败记录",
              any(fn in m for m in msgs), f"got {msgs}")
    check("E193 cron 记录带异常栈 (exc_info)",
          all(r.exc_info for r in cap.records) and len(cap.records) >= 3,
          f"records={len(cap.records)}")


# ── E193-gate: do_PUT / do_PATCH body 上限闸 ──────────────────────────

def test_e193_put_oversize_body_returns_413():
    h = _FakeHandler("/api/workers",
                     {"Content-Length": str(console.POST_BODY_MAX_BYTES + 1)},
                     body=b"x" * 1024)
    h.do_PUT()
    check("E193 do_PUT 超限 ⇒ 413", h.status == 413, f"got {h.status}")
    check("E193 do_PUT 超限 ⇒ payload_too_large",
          json.loads(h.body or b"{}").get("error") == "payload_too_large",
          f"got {h.body!r}")
    check("E193 do_PUT 超限 ⇒ rfile 零次读 (闸在读之前)",
          h.rfile.reads == 0, f"reads={h.rfile.reads}")


def test_e193_put_at_limit_not_rejected():
    h = _FakeHandler("/api/no-such-endpoint",
                     {"Content-Length": str(console.POST_BODY_MAX_BYTES)})
    h.do_PUT()
    check("E193 do_PUT 等于上限 ⇒ 不 413 (严格大于)",
          h.status != 413, f"got {h.status}")


def test_e193_patch_oversize_body_returns_413():
    for path in ("/api/whatever", "/webui/kanban"):
        h = _FakeHandler(path,
                         {"Content-Length":
                          str(console.POST_BODY_MAX_BYTES + 1)},
                         body=b"x" * 1024)
        h.do_PATCH()
        check(f"E193 do_PATCH {path} 超限 ⇒ 413",
              h.status == 413, f"got {h.status}")
        check(f"E193 do_PATCH {path} 超限 ⇒ rfile 零次读",
              h.rfile.reads == 0, f"reads={h.rfile.reads}")


def test_e193_patch_at_limit_not_rejected():
    h = _FakeHandler("/api/no-such-endpoint",
                     {"Content-Length": str(console.POST_BODY_MAX_BYTES)})
    h.do_PATCH()
    check("E193 do_PATCH 等于上限 ⇒ 不 413 (落 501 正常路由)",
          h.status == 501, f"got {h.status}")


# ── E193-src: 源码级守卫 ──────────────────────────────────────────────

def test_e193_source_no_500_str_e_pattern():
    src = (ROOT / "mw" / "console.py").read_text(encoding="utf-8")
    check("E193 源码无 _send(500, {\"error\": str(e)}) 残留",
          '_send(500, {"error": str(e)})' not in src)
    check("E193 源码无 return 500, {\"error\": str(e)} 残留",
          'return 500, {"error": str(e)}' not in src)


def main():
    global _COLLECT_ONLY
    _COLLECT_ONLY = True
    tests = [
        test_e193_do_get_outer_fallback_no_leak,
        test_e193_do_put_outer_fallback_no_leak,
        test_e193_do_delete_outer_fallback_no_leak,
        test_e193_server_log_keeps_full_exception,
        test_e193_value_error_branches_still_422,
        test_e193_cron_functions_no_leak,
        test_e193_cron_server_log_keeps_exception,
        test_e193_put_oversize_body_returns_413,
        test_e193_put_at_limit_not_rejected,
        test_e193_patch_oversize_body_returns_413,
        test_e193_patch_at_limit_not_rejected,
        test_e193_source_no_500_str_e_pattern,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            check(t.__name__, False, f"EXC: {type(e).__name__}: {e}")
    total = len(PASS) + len(FAIL)
    print()
    if not FAIL:
        print(f"\033[92m=== ALL {total} #193 TESTS PASSED ===\033[0m")
        return 0
    print(f"\033[91m=== {len(FAIL)}/{total} FAILED ===\033[0m")
    for n, d in FAIL:
        print(f"  - {n}: {d}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
