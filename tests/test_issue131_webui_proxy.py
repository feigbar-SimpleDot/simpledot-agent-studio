"""tests/test_issue131_webui_proxy.py — issue #131 方案B: /webui 同源代理守护。

背景 (docs/FIX-PLAN-O.md §簇D): hermes-webui 原以独立宿主端口 8787 暴露;
方案B 改为 console(:8700) 的 /webui 路径同源反代 —— webui 容器内只绑
127.0.0.1, 宿主端口映射删除, 浏览器只见 :8700 单入口, 鉴权保留 webui
原口令 (HERMES_WEBUI_PASSWORD), 代理不干预。

覆盖:
  W1  开关三级口径: settings.json webui_proxy_enabled > env
      WEBUI_PROXY_ENABLED > /webui/start.sh 自动探测 (与 entry 启动条件同源)
  W2  do_GET /webui 未启用 ⇒ 404 {"error": "webui proxy disabled"}
  W3  do_GET /webui/* 启用 ⇒ proxy_http 收到剥前缀路径 (含 query)
  W4  do_POST /webui 先于 body 预读 (代理自读 rfile; 预读会饿死上游读)
  W5  do_PUT / do_DELETE / do_PATCH 的 /webui 路由; 非 /webui 的 PATCH ⇒ 501
  W6  proxy_http: Host 保留 + Cookie/CSRF/Origin 透传; 302 不跟随原样透传;
      HTTPError(401) 原样透传不塌 502; 上游不可达 ⇒ 502; SSE 逐行流式;
      非 SSE 逐跳头过滤 + Content-Length 重算
  W7  键集决策守护: webui_proxy_enabled 刻意不进 SETTINGS_DEFAULTS
      (设置页保存会钉死 False 压掉自动探测); api_settings_save 拒收该键
  W8  compose 守护: 8787 宿主端口映射已删; HERMES_WEBUI_HOST=127.0.0.1
  W9  verify.sh 守护: webui 探活走 docker exec 容器内探测 (宿主 curl 已删)

跑法: 仓库根 .venv 下 `pytest tests/test_issue131_webui_proxy.py -v`
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mw.console as console  # noqa: E402
import mw.webui_proxy as wp  # noqa: E402
from mw.console import Handler  # noqa: E402


# ──────────────────────────────────────────────────────────────────
# 脚手架: 不绑 socket 的 Handler 函数级直测 (test_issue63 同口径)
# ──────────────────────────────────────────────────────────────────

class FakeWfile:
    def __init__(self):
        self.buf = bytearray()

    def write(self, b) -> int:
        self.buf += bytes(b)
        return len(b)

    def flush(self):
        pass


def make_handler(command="GET", path="/webui/", headers=None, body=b""):
    h = Handler.__new__(Handler)
    h.command = command
    h.path = path
    h.headers = dict(headers or {})
    h.rfile = io.BytesIO(body)
    h.wfile = FakeWfile()
    h.close_connection = False
    h.sent = []          # _send(code, obj) 记录
    h.headers_sent = []  # send_response/send_header/end_headers 记录
    h.send_response = lambda code: h.headers_sent.append(("status", code))
    h.send_header = lambda k, v: h.headers_sent.append((k, v))
    h.end_headers = lambda: h.headers_sent.append(("end", None))
    h._send = lambda code, obj, ctype="application/json": h.sent.append(
        (code, obj))
    h.log_message = lambda *a, **k: None
    return h


class FakeResp:
    """假上游响应: 与 urlopen 返回值同形态 (status/headers/read/迭代/上下文)。"""

    def __init__(self, status=200, headers=None, body=b"", lines=None):
        self.status = status
        self.headers = dict(headers or {})
        self._body = body
        self._lines = lines

    def read(self):
        return self._body

    def __iter__(self):
        return iter(self._lines or [])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """假 opener: 记录 Request, 按脚本返回 FakeResp 或抛异常。"""

    def __init__(self, result):
        self.result = result
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


# ──────────────────────────────────────────────────────────────────
# W1: 开关三级口径
# ──────────────────────────────────────────────────────────────────

def _settings_file(tmp_path, payload):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


def test_w1a_settings_page_wins_over_env(tmp_path):
    """settings.json 显式 False 压过 env=1 (设置页是闭环最终控制)。"""
    sp = _settings_file(tmp_path, {"webui_proxy_enabled": False})
    with mock.patch.object(wp, "settings_path", lambda: sp), \
         mock.patch.dict(os.environ, {"WEBUI_PROXY_ENABLED": "1"}):
        assert wp.webui_proxy_enabled() is False
    sp2 = _settings_file(tmp_path, {"webui_proxy_enabled": True})
    with mock.patch.object(wp, "settings_path", lambda: sp2), \
         mock.patch.dict(os.environ, {"WEBUI_PROXY_ENABLED": "0"}):
        assert wp.webui_proxy_enabled() is True


def test_w1b_env_overrides_autodetect(tmp_path):
    """settings 无键时 env 生效: 1 开 / 0 关 (即便 start.sh 探测为真)。"""
    sp = _settings_file(tmp_path, {})
    with mock.patch.object(wp, "settings_path", lambda: sp), \
         mock.patch.dict(os.environ, {"WEBUI_PROXY_ENABLED": "1"}), \
         mock.patch.object(os.path, "isfile", lambda p: True):
        assert wp.webui_proxy_enabled() is True
    with mock.patch.object(wp, "settings_path", lambda: sp), \
         mock.patch.dict(os.environ, {"WEBUI_PROXY_ENABLED": "0"}), \
         mock.patch.object(os.path, "isfile", lambda p: True):
        assert wp.webui_proxy_enabled() is False


def test_w1c_autodetect_mirrors_entry_condition(tmp_path):
    """settings/env 都缺省时: /webui/start.sh 存在 ⇔ 启用 (与 main_entry.sh 同条件)。"""
    sp = _settings_file(tmp_path, {})
    env = {k: v for k, v in os.environ.items() if k != "WEBUI_PROXY_ENABLED"}
    with mock.patch.object(wp, "settings_path", lambda: sp), \
         mock.patch.dict(os.environ, env, clear=True), \
         mock.patch.object(os.path, "isfile",
                           lambda p: p == "/webui/start.sh"):
        assert wp.webui_proxy_enabled() is True
    with mock.patch.object(wp, "settings_path", lambda: sp), \
         mock.patch.dict(os.environ, env, clear=True), \
         mock.patch.object(os.path, "isfile", lambda p: False):
        assert wp.webui_proxy_enabled() is False


def test_w1d_settings_unreadable_falls_through(tmp_path):
    """settings.json 不存在/坏 JSON 不炸, 落入 env/探测层。"""
    with mock.patch.object(wp, "settings_path",
                           lambda: str(tmp_path / "missing.json")), \
         mock.patch.dict(os.environ, {"WEBUI_PROXY_ENABLED": "1"}):
        assert wp.webui_proxy_enabled() is True


def test_w1e_upstream_base_follows_port_env():
    """上游地址跟随 HERMES_WEBUI_PORT (与 compose 注入同端口)。"""
    with mock.patch.dict(os.environ, {"HERMES_WEBUI_PORT": "18787"}):
        assert wp.upstream_base() == "http://127.0.0.1:18787"
    env = {k: v for k, v in os.environ.items() if k != "HERMES_WEBUI_PORT"}
    with mock.patch.dict(os.environ, env, clear=True):
        assert wp.upstream_base() == "http://127.0.0.1:8787"


# ──────────────────────────────────────────────────────────────────
# W2/W3: do_GET 路由
# ──────────────────────────────────────────────────────────────────

def test_w2_get_disabled_returns_404():
    h = make_handler(path="/webui/anything")
    with mock.patch.object(wp, "webui_proxy_enabled", lambda: False):
        h.do_GET()
    assert h.sent and h.sent[0][0] == 404
    assert "webui proxy disabled" in json.dumps(h.sent[0][1])


def test_w3_get_enabled_strips_prefix_keeps_query():
    calls = []
    h = make_handler(path="/webui/api/sessions?limit=5")
    with mock.patch.object(wp, "webui_proxy_enabled", lambda: True), \
         mock.patch.object(wp, "proxy_http",
                           lambda handler, up: calls.append(up)):
        h.do_GET()
    assert calls == ["/api/sessions?limit=5"]


def test_w3b_get_bare_webui_prefix():
    """/webui (无尾斜杠) 也进代理, 剥前缀后为空串 ⇒ 代理侧补 '/'。"""
    calls = []
    h = make_handler(path="/webui")
    with mock.patch.object(wp, "webui_proxy_enabled", lambda: True), \
         mock.patch.object(wp, "proxy_http",
                           lambda handler, up: calls.append(up)):
        h.do_GET()
    assert calls == [""]


# ──────────────────────────────────────────────────────────────────
# W4: do_POST 先于 body 预读
# ──────────────────────────────────────────────────────────────────

def test_w4_post_routes_before_body_preread():
    """登录 POST 带 body: 代理分支必须在 json.loads 预读之前,
    rfile 未被 do_POST 消费 (proxy_http 自读转发)。"""
    calls = []
    body = b'{"password":"x"}'
    h = make_handler(command="POST", path="/webui/api/auth/login",
                     headers={"Content-Length": str(len(body))}, body=body)
    with mock.patch.object(wp, "webui_proxy_enabled", lambda: True), \
         mock.patch.object(wp, "proxy_http",
                           lambda handler, up: calls.append(up)):
        h.do_POST()
    assert calls == ["/api/auth/login"]
    # 关键: do_POST 未预读 body (rfile 位置仍在 0)
    assert h.rfile.tell() == 0


def test_w4b_post_disabled_returns_404():
    h = make_handler(command="POST", path="/webui/api/auth/login",
                     headers={"Content-Length": "2"}, body=b"{}")
    with mock.patch.object(wp, "webui_proxy_enabled", lambda: False):
        h.do_POST()
    assert h.sent and h.sent[0][0] == 404


# ──────────────────────────────────────────────────────────────────
# W5: do_PUT / do_DELETE / do_PATCH
# ──────────────────────────────────────────────────────────────────

def test_w5_put_delete_route_webui():
    calls = []
    ctx = [mock.patch.object(wp, "webui_proxy_enabled", lambda: True),
           mock.patch.object(wp, "proxy_http",
                             lambda handler, up: calls.append(up))]
    h1 = make_handler(command="PUT", path="/webui/api/x")
    h2 = make_handler(command="DELETE", path="/webui/api/y")
    for ctxm in ctx:
        ctxm.start()
    try:
        h1.do_PUT()
        h2.do_DELETE()
    finally:
        for ctxm in ctx:
            ctxm.stop()
    assert calls == ["/api/x", "/api/y"]


def test_w5b_patch_webui_routes_non_webui_501():
    calls = []
    h1 = make_handler(command="PATCH", path="/webui/api/kanban/tasks/1")
    h2 = make_handler(command="PATCH", path="/api/anything")
    with mock.patch.object(wp, "webui_proxy_enabled", lambda: True), \
         mock.patch.object(wp, "proxy_http",
                           lambda handler, up: calls.append(up)):
        h1.do_PATCH()
        h2.do_PATCH()
    assert calls == ["/api/kanban/tasks/1"]
    assert h2.sent and h2.sent[0][0] == 501


def test_w5c_patch_disabled_404():
    h = make_handler(command="PATCH", path="/webui/api/kanban/tasks/1")
    with mock.patch.object(wp, "webui_proxy_enabled", lambda: False):
        h.do_PATCH()
    assert h.sent and h.sent[0][0] == 404


# ──────────────────────────────────────────────────────────────────
# W6: proxy_http 行为
# ──────────────────────────────────────────────────────────────────

def test_w6a_forwards_host_cookie_csrf_origin():
    """同源门禁关键: Host 原样保留; Cookie/CSRF/Origin 透传。"""
    resp = FakeResp(200, {"Content-Type": "application/json"}, b'{"ok":1}')
    op = FakeOpener(resp)
    h = make_handler(
        path="/webui/api/sessions",
        headers={"Host": "127.0.0.1:8700",
                 "Cookie": "hermes_session=abc",
                 "X-Hermes-CSRF-Token": "tok",
                 "Origin": "http://127.0.0.1:8700"})
    with mock.patch.object(wp, "_opener", op):
        wp.proxy_http(h, "/api/sessions")
    req = op.requests[0]
    # urllib.Request 会把头名 capitalize() 存储 (X-Hermes-CSRF-Token →
    # X-hermes-csrf-token); get_header 同规则归一, 断言按归一后键取。
    assert req.get_header("Host") == "127.0.0.1:8700"
    assert req.get_header("Cookie") == "hermes_session=abc"
    assert req.get_header("X-hermes-csrf-token") == "tok"
    assert req.get_header("Origin") == "http://127.0.0.1:8700"
    assert req.full_url.endswith("/api/sessions")


def test_w6b_redirect_not_followed():
    """302 原样透传 (相对 Location 由浏览器按 /webui/ 前缀解析)。"""
    resp = FakeResp(302, {"Location": "login?next=/",
                          "Content-Type": "text/html"}, b"")
    op = FakeOpener(resp)
    h = make_handler(path="/webui/")
    with mock.patch.object(wp, "_opener", op):
        wp.proxy_http(h, "/")
    assert ("status", 302) in h.headers_sent
    assert ("Location", "login?next=/") in h.headers_sent
    # 结构性守护: 模块 opener 的 redirect handler 永不跟随
    rh = [x for x in wp._opener.handlers
          if isinstance(x, urllib.request.HTTPRedirectHandler)]
    assert rh and rh[0].redirect_request(None, None, None, None, None) is None


def test_w6c_http_error_passthrough_not_502():
    """上游 401/403/404 是合法响应语义, 必须原样透传 (不塌 502)。
    透传走 send_response 正常回传路径 (非 _send 兜底路径)。"""
    err = urllib.error.HTTPError(
        "http://127.0.0.1:8787/api/x", 401, "Unauthorized",
        {"Content-Type": "application/json"}, io.BytesIO(b'{"error":"nope"}'))
    op = FakeOpener(err)
    h = make_handler(path="/webui/api/x")
    with mock.patch.object(wp, "_opener", op):
        wp.proxy_http(h, "/api/x")
    assert not h.sent  # 未走 _send 兜底
    assert ("status", 401) in h.headers_sent
    assert bytes(h.wfile.buf) == b'{"error":"nope"}'


def test_w6d_upstream_unreachable_502():
    op = FakeOpener(OSError("connection refused"))
    h = make_handler(path="/webui/health")
    with mock.patch.object(wp, "_opener", op):
        wp.proxy_http(h, "/health")
    assert h.sent and h.sent[0][0] == 502


def test_w6e_sse_streams_line_by_line():
    """SSE 响应逐行透传 (整体 read 会把事件卡在代理缓冲)。"""
    frames = [b"event: x\ndata: 1\n\n", b"data: 2\n\n"]
    resp = FakeResp(200, {"Content-Type": "text/event-stream"}, lines=frames)
    op = FakeOpener(resp)
    h = make_handler(path="/webui/api/session/stream")
    with mock.patch.object(wp, "_opener", op):
        wp.proxy_http(h, "/api/session/stream")
    assert ("status", 200) in h.headers_sent
    assert bytes(h.wfile.buf) == b"".join(frames)
    # SSE 分支不发 Content-Length (流式无总长)
    assert not any(k == "Content-Length" for k, _ in h.headers_sent)


def test_w6f_non_sse_filters_hop_headers():
    resp = FakeResp(200, {"Content-Type": "text/html",
                          "Transfer-Encoding": "chunked",
                          "Connection": "keep-alive",
                          "X-Custom": "keep"}, body=b"<html>hi</html>")
    op = FakeOpener(resp)
    h = make_handler(path="/webui/")
    with mock.patch.object(wp, "_opener", op):
        wp.proxy_http(h, "/")
    keys = [k for k, _ in h.headers_sent]
    assert "Transfer-Encoding" not in keys and "Connection" not in keys
    assert ("X-Custom", "keep") in h.headers_sent
    assert ("Content-Length", str(len(b"<html>hi</html>"))) in h.headers_sent
    assert bytes(h.wfile.buf) == b"<html>hi</html>"


def test_w6g_post_body_forwarded():
    """POST body 由代理自读 rfile 并转发 (与 W4 路由顺序配套)。"""
    body = b'{"password":"x"}'
    resp = FakeResp(200, {"Content-Type": "application/json"}, b'{"ok":true}')
    op = FakeOpener(resp)
    h = make_handler(command="POST", path="/webui/api/auth/login",
                     headers={"Content-Length": str(len(body)),
                              "Content-Type": "application/json"},
                     body=body)
    with mock.patch.object(wp, "_opener", op):
        wp.proxy_http(h, "/api/auth/login")
    assert op.requests[0].data == body


# ──────────────────────────────────────────────────────────────────
# W7: 键集决策守护
# ──────────────────────────────────────────────────────────────────

def test_w7_settings_keyset_decision():
    """webui_proxy_enabled 刻意不进 SETTINGS_DEFAULTS: 若进键集, 任何一次
    设置页保存都会把 False 钉进 settings.json, 永久压掉自动探测兜底。
    同时 api_settings_save 必须拒收该键 (未知配置项)。"""
    assert "webui_proxy_enabled" not in console.SETTINGS_DEFAULTS
    try:
        console.api_settings_save({"webui_proxy_enabled": True})
        raise AssertionError("api_settings_save 应拒收未登记键")
    except ValueError as e:
        assert "webui_proxy_enabled" in str(e)


# ──────────────────────────────────────────────────────────────────
# W8/W9: 部署面守护
# ──────────────────────────────────────────────────────────────────

def test_w8_compose_no_host_port_and_loopback_bind():
    text = (ROOT / "deploy" / "main.compose.yaml").read_text(encoding="utf-8")
    # 8787 宿主端口映射已删 (不允许再出现 "…:8787" 形式的 ports 条目)
    assert '"${HERMES_WEBUI_PORT:-8787}:${HERMES_WEBUI_PORT:-8787}"' not in text
    # webui 容器内只绑回环
    assert "HERMES_WEBUI_HOST=127.0.0.1" in text
    assert "HERMES_WEBUI_HOST=0.0.0.0" not in text


def test_w9_verify_probes_inside_container():
    text = (ROOT / "deploy" / "verify.sh").read_text(encoding="utf-8")
    # 旧的宿主侧 curl 探活必须删除
    assert 'curl -sf --max-time 5 "http://127.0.0.1:${WPORT}/health"' not in text
    # 新探活走 docker exec + 容器内 python3 urllib (容器内无 curl 保证)
    assert "docker exec -e WPORT" in text


# ──────────────────────────────────────────────────────────────────
# W10: issue #196 裸 /webui 尾斜杠规范化 302 (proxy_http 直测)
# ──────────────────────────────────────────────────────────────────

def test_w10a_bare_webui_get_redirects_to_slash():
    """GET + upstream_path="" ⇒ 302 /webui/, 上游 opener 零调用。"""
    op = FakeOpener(FakeResp(200, {}, b"should-not-be-used"))
    h = make_handler(path="/webui")
    with mock.patch.object(wp, "_opener", op):
        wp.proxy_http(h, "")
    assert ("status", 302) in h.headers_sent
    assert ("Location", "/webui/") in h.headers_sent
    assert ("Content-Length", "0") in h.headers_sent
    assert op.requests == []          # 未透传上游
    assert bytes(h.wfile.buf) == b""  # 302 无 body


def test_w10b_bare_webui_get_keeps_query():
    """GET + "?x=1" ⇒ 302 /webui/?x=1 (query 保留)。"""
    op = FakeOpener(FakeResp(200, {}, b""))
    h = make_handler(path="/webui?x=1")
    with mock.patch.object(wp, "_opener", op):
        wp.proxy_http(h, "?x=1")
    assert ("status", 302) in h.headers_sent
    assert ("Location", "/webui/?x=1") in h.headers_sent
    assert op.requests == []


def test_w10c_slash_and_post_not_redirected():
    """"/" (来自 /webui/) 与 POST 裸 /webui 均不重定向, 照常透传上游。"""
    resp = FakeResp(200, {"Content-Type": "text/html"}, b"<html></html>")
    op = FakeOpener(resp)
    h1 = make_handler(path="/webui/")
    h2 = make_handler(command="POST", path="/webui")
    with mock.patch.object(wp, "_opener", op):
        wp.proxy_http(h1, "/")
        wp.proxy_http(h2, "")
    assert len(op.requests) == 2      # 两者都走了上游
    assert not any(s == 302 for k, s in h1.headers_sent if k == "status")
    assert not any(s == 302 for k, s in h2.headers_sent if k == "status")


# ──────────────────────────────────────────────────────────────────
# W11: issue #196 GET /api/webui/status 只读端点
# ──────────────────────────────────────────────────────────────────

def _webui_status_via_env(tmp_path, env_val):
    """env 驱动 webui_proxy_enabled 后直调 do_GET 取 /api/webui/status 响应。"""
    missing = str(tmp_path / "no-settings.json")
    h = make_handler(path="/api/webui/status")
    with mock.patch.object(wp, "settings_path", lambda: missing), \
         mock.patch.dict(os.environ, {"WEBUI_PROXY_ENABLED": env_val}):
        h.do_GET()
    assert h.sent and h.sent[0][0] == 200
    return h.sent[0][1]


def test_w11a_webui_status_endpoint(tmp_path):
    """WEBUI_PROXY_ENABLED=1/0 ⇒ {"enabled": true/false}。"""
    assert _webui_status_via_env(tmp_path, "1") == {"enabled": True}
    assert _webui_status_via_env(tmp_path, "0") == {"enabled": False}


def test_w11b_webui_status_key_not_in_defaults():
    """端点只读探测, 其依赖键 webui_proxy_enabled 刻意不进 SETTINGS_DEFAULTS
    (与 W7 同法守护: 进键集会被设置页保存钉死, 压掉自动探测)。"""
    assert "webui_proxy_enabled" not in console.SETTINGS_DEFAULTS
