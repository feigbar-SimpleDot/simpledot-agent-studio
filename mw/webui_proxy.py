"""hermes-webui 同源代理 (issue #131 方案B)。

让控制台(:8700)把 hermes-webui(容器内 127.0.0.1:${HERMES_WEBUI_PORT:-8787})
内嵌进同一个源: 浏览器全程只见控制台一个端口, webui 不再对外暴露宿主端口。
入口形态 = 弱嵌入: 入口位置不变、点击弹独立窗口、URL 以 /webui 路径区分、
鉴权保留 webui 原口令 (HERMES_WEBUI_PASSWORD), 代理不干预。

成立前提 (spike 已在 hermes-webui master 上逐条实测, 见 docs/O4-NOTES.md):
  - SPA 资源全部相对路径: index.html 动态 <base href> 按当前路径推导
    (在 /webui/ 下自动落到 /webui/), 静态引用均为相对 (static/...)。
  - 无 WebSocket: webui 实时面是 SSE (EventSource), 全部经 new URL(rel,
    document.baseURI) 相对解析 —— 剥前缀代理天然覆盖, 无需 WS 隧道
    (与 /pixel 不同, pixel 的 WS 硬编码根路径 /ws)。
  - 口令流程 Cookie/重定向兼容剥前缀代理:
      * 会话 Cookie path='/' + SameSite=Lax, 浏览器按 host 存储 ⇒ 与
        控制台共享 host 时仍随请求携带 (口令鉴权独立于控制台, 互不干扰)。
      * 未登录 302 的 Location 是相对路径 (login?next=...), 代理必须
        原样透传不跟随 ⇒ 浏览器按 /webui/ 前缀解析落回 /webui/login。
      * webui 自带子路径挂载支持 (login.js _safeNextPath 按 appRoot 重映射
        next; api() 剥前导 / 走 baseURI 相对解析), 即官方支持本代理形态。
      * CSRF/同源门禁只比 Host 不比端口 ⇒ 代理保留浏览器原 Host 头即通过。

代理面: proxy_http(handler, upstream_path) —— /webui/* 全方法 HTTP
  (含 SPA/静态/业务 API/SSE 流式/PATCH/DELETE/OPTIONS)。
SSE 特判: 上游 Content-Type 为 text/event-stream 时逐行透传+flush
  (与 console._proxy_stream 同语义; 整体 read() 会把事件全卡在代理缓冲)。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

# 上游 Host 不跟随重定向的 opener: 302 必须原样透传给浏览器 (相对
# Location 由浏览器按 /webui/ 前缀解析; 代理若跟随会把登录跳转吞掉)。
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)

# 透传的上游响应头黑名单 (逐跳头/长度头由代理侧重算; 与 pixel_proxy 同口径)。
_HOP_BY_HOP = ("transfer-encoding", "connection", "content-encoding",
               "content-length")

# 从浏览器转发到上游的请求头白名单: Host 单独处理 (保留原值 = 同源门禁关键);
# Cookie/CSRF/Origin/Referer 是 webui 鉴权与 CSRF 门禁的全部依赖;
# Accept 决定 SSE 协商; Content-Type 在下方随 body 单独转发。
_FORWARD_REQ_HEADERS = ("Cookie", "Origin", "Referer",
                        "X-Hermes-CSRF-Token", "Sec-Fetch-Site", "Accept")


def upstream_base() -> str:
    """webui 上游地址: 容器内回环, 端口跟随 HERMES_WEBUI_PORT。

    部署侧 compose 把 HERMES_WEBUI_HOST 钉成 127.0.0.1 后, webui 仅
    console 可达; 这里同样只走回环, 不读该变量 (代理与 webui 同容器)。
    """
    port = os.environ.get("HERMES_WEBUI_PORT", "8787").strip() or "8787"
    return f"http://127.0.0.1:{port}"


def settings_path() -> str:
    return os.environ.get(
        "MW_SETTINGS",
        os.path.join(os.environ.get("MW_HOME", "/mw_data"), "settings.json"))


def webui_proxy_enabled() -> bool:
    """webui 同源代理开关: 设置页 webui_proxy_enabled 优先, env 兜底, 再兜底自动探测。

    三级口径 (与 pixel_enabled 同模式, 追加自动探测一级):
      1. 设置页开关 (settings.json webui_proxy_enabled) 是闭环最终控制;
      2. env WEBUI_PROXY_ENABLED 是部署期显式覆盖 (1 开 / 0 关);
      3. 都未设置时自动探测 /webui/start.sh 是否存在 —— 与 main_entry.sh
         启动 webui watchdog 的条件逐字相同, 即 "entry 起了 webui 则代理
         自动可用", 标准部署零配置。测试环境无 /webui ⇒ 缺省关闭, 行为确定。
    """
    try:
        cur = json.loads(open(settings_path(), "r", encoding="utf-8").read())
        v = cur.get("webui_proxy_enabled")
        if v is not None:
            return bool(v)
    except (OSError, json.JSONDecodeError):
        pass
    env = os.environ.get("WEBUI_PROXY_ENABLED", "").strip()
    if env:
        return env == "1"
    # 自动探测: 与 main_entry.sh 的 webui 启动条件同源 ([ -f /webui/start.sh ])
    return os.path.isfile("/webui/start.sh")


def proxy_http(handler, upstream_path: str):
    """把 handler 当前请求转发到 webui 的 upstream_path, 原样回传。

    保留浏览器 Host 头 (webui CSRF 门禁只比 Host)。upstream_path 已剥掉
    /webui 前缀, 含 query string。全方法通用 (GET/POST/PUT/PATCH/DELETE/
    OPTIONS); SSE 响应逐行流式透传。
    """
    # issue #196: 裸 /webui (无尾斜杠) 规范化 302 → /webui/。
    # webui index.html 的动态 <base href> 按 location.pathname 推导
    # (无尾斜杠路径算出 base='/'), 相对静态资源与相对 302 Location
    # 都会落出 /webui 前缀 → 控制台 404。带尾斜杠是唯一正确形态。
    # 仅 GET: POST 等打到裸 /webui 属畸形调用, 维持透传不改语义;
    # query 保留 (/webui?x=1 → /webui/?x=1); "/" 来自 /webui/ 不误伤。
    if handler.command == "GET" and (
            upstream_path == "" or upstream_path.startswith("?")):
        handler.send_response(302)
        handler.send_header("Location", "/webui/" + upstream_path)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    url = upstream_base() + (upstream_path or "/")
    n = int(handler.headers.get("Content-Length", 0) or 0)
    body = handler.rfile.read(n) if n else None
    req = urllib.request.Request(url, data=body, method=handler.command)
    # ★ 保留 Host —— webui 同源/CSRF 门禁只比 Host, 转发浏览器原 Host 即通过。
    req.add_header("Host", handler.headers.get("Host", ""))
    for k in _FORWARD_REQ_HEADERS:
        v = handler.headers.get(k)
        if v:
            req.add_header(k, v)
    ctype = handler.headers.get("Content-Type")
    if body is not None and ctype:
        req.add_header("Content-Type", ctype)
    try:
        resp = _opener.open(req, timeout=1800)
    except urllib.error.HTTPError as e:
        # 4xx/5xx 也是合法响应 (401/403/404 语义必须原样透传, 不能塌成 502)
        resp = e
    except Exception as e:  # noqa: BLE001
        handler._send(502, {"error": f"hermes-webui unreachable: {e}"})
        return
    with resp:
        ct = resp.headers.get("Content-Type", "")
        if "text/event-stream" in ct:
            # SSE 逐行透传: 整体 read() 会阻塞到上游关闭, 事件全卡代理缓冲
            # (与 console._proxy_stream 的 SSE 纪律同源)。
            handler.send_response(resp.status)
            handler.send_header("Content-Type", ct)
            handler.send_header("Cache-Control", "no-cache")
            handler.send_header("X-Accel-Buffering", "no")
            handler.end_headers()
            try:
                for line in resp:
                    handler.wfile.write(line)
                    handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                # 下游(浏览器)断开: 吞掉异常, 不外穿 do_* 外层 (同 #63 纪律)
                handler.close_connection = True
            return
        data = resp.read()
        handler.send_response(resp.status)
        for k, v in resp.headers.items():
            if k.lower() in _HOP_BY_HOP:
                continue
            handler.send_header(k, v)
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        # 大响应 (会话列表/工作区文件) 走 pacing 循环, Apple container 代理适配
        from .console import Handler
        Handler._write_paced(handler.wfile, data)
