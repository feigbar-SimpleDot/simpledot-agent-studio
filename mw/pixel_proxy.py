"""像素办公室(pixel-agents)同源代理。

让控制台(:8700)把 pixel-agents(容器内 127.0.0.1:3199)内嵌进同一个源,
浏览器全程只见控制台一个端口,pixel-agents 不对外暴露。

成立前提(pixel-agents 侧, 已在 spike 中验证):
  - WS 同源门禁是 `new URL(origin).host === host` —— 只比 host 不比端口。
    代理把浏览器带来的 Host 头原样转发, 两边一致, 门禁通过。
  - SPA 资源是相对路径(./assets/...), 挂在 /pixel/ 前缀下可正常加载。
  - tokenless WS 客户端可正常观看办公室(只有装 hooks 到 ~/.claude 才要 token),
    所以 iframe 的 src 不需要带 token。

两个代理面:
  proxy_http(handler)  —— /pixel/* 的普通 HTTP(含 SPA/资源/health)。
  tunnel_ws(handler)   —— /ws 的 WebSocket 升级隧道(裸字节双向中继)。

关键坑(spike 实测): 转发 101 握手必须把上游的【原始字节】直接 sendall 到
self.connection, 不能用 BaseHTTPRequestHandler.send_response —— 后者写进缓冲
wfile, 而中继写裸 socket, 两条写路径打架会把 101 握手破坏, 页面卡在 "Loading..."
且 WS 反复重连(日志见 101 但下游 0 帧)。
"""
from __future__ import annotations

import glob
import json
import os
import select
import socket
import urllib.error
import urllib.request

# 容器内 pixel-agents 监听地址(entry.sh 固定 127.0.0.1:3199)。
# 可用 MW_PIXEL_OFFICE_URL 覆盖(本地 dev 直连别处)。
DEFAULT_UPSTREAM = "http://127.0.0.1:3199"


def upstream_base() -> str:
    return os.environ.get("MW_PIXEL_OFFICE_URL", DEFAULT_UPSTREAM).rstrip("/")


def settings_path() -> str:
    return os.environ.get(
        "MW_SETTINGS",
        os.path.join(os.environ.get("MW_HOME", "/mw_data"), "settings.json"))


def pixel_enabled() -> bool:
    """像素办公室是否启用: 设置页 pixel_office_enabled 优先, 否则看 env。

    设置页开关是闭环的最终控制(用户可在 UI 里开关而不必改 .env);
    env PIXEL_OFFICE_ENABLED 是部署期默认。两者任一为真即启用。
    """
    try:
        cur = json.loads(open(settings_path(), "r", encoding="utf-8").read())
        v = cur.get("pixel_office_enabled")
        if v is not None:
            return bool(v)
    except (OSError, json.JSONDecodeError):
        pass
    return os.environ.get("PIXEL_OFFICE_ENABLED", "0") == "1"


def _upstream_hostport() -> tuple[str, int]:
    base = upstream_base()
    # http://host:port
    rest = base.split("://", 1)[-1]
    host, _, port = rest.partition(":")
    return host or "127.0.0.1", int(port or "3199")


def pixel_office_running() -> bool:
    """探测 pixel-agents 是否活着(供设置页/前端显示状态)。"""
    try:
        with urllib.request.urlopen(upstream_base() + "/api/health",
                                  timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def pixel_office_token() -> str:
    """读 pixel-agents 启动时登记的实时 token(每次启动轮换, 不能写死)。

    注册文件: <HOME>/.pixel-agents/servers/<pid>-<port>.json 的 token 字段。
    HOME 因进程上下文而异(entry/reaper 是 /opt/data, docker exec 可能是
    /root), 故按候选目录依次找: $HOME → /opt/data → /home/*。
    bridge 注入 hook 事件时用它做 Bearer。读不到返回空串(调用方据此降级)。
    """
    homes = [os.environ.get("HOME", os.path.expanduser("~")), "/opt/data"]
    try:
        homes += [os.path.join("/home", d) for d in os.listdir("/home")]
    except OSError:
        pass
    want_port = str(_upstream_hostport()[1])
    best = ""
    best_ts = -1.0
    seen = set()
    for h in homes:
        if not h or h in seen:
            continue
        seen.add(h)
        pat = os.path.join(h, ".pixel-agents", "servers", "*.json")
        for f in glob.glob(pat):
            try:
                d = json.loads(open(f, "r", encoding="utf-8").read())
            except (OSError, json.JSONDecodeError):
                continue
            if str(d.get("port", "")) != want_port:
                continue
            ts = float(d.get("startedAt", 0) or 0)
            if ts >= best_ts and d.get("token"):
                best, best_ts = d["token"], ts
    return best


def proxy_http(handler, upstream_path: str):
    """把 handler 当前请求转发到 pixel-agents 的 upstream_path, 原样回传。

    保留浏览器 Host 头(同源门禁的关键)。upstream_path 已剥掉 /pixel 前缀,
    含 query string。
    """
    url = upstream_base() + (upstream_path or "/")
    n = int(handler.headers.get("Content-Length", 0) or 0)
    body = handler.rfile.read(n) if n else None
    req = urllib.request.Request(url, data=body, method=handler.command)
    # ★ 保留 Host —— 同源门禁只比 host, 转发浏览器原 Host 即可通过。
    req.add_header("Host", handler.headers.get("Host", ""))
    ctype = handler.headers.get("Content-Type")
    if body is not None and ctype:
        req.add_header("Content-Type", ctype)
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        data = e.read()
        handler._send(e.code, data,
                     e.headers.get("Content-Type", "application/json"))
        return
    except Exception as e:  # noqa: BLE001
        handler._send(502, {"error": f"pixel-agents unreachable: {e}"})
        return
    with resp:
        data = resp.read()
        ct = resp.headers.get("Content-Type", "application/octet-stream")
        handler.send_response(resp.status)
        for k, v in resp.headers.items():
            if k.lower() in ("transfer-encoding", "connection",
                            "content-encoding", "content-length"):
                continue
            handler.send_header(k, v)
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        # issue #62/64: 大响应需经 pacing 循环写全(Apple container 代理截断适配)
        from .console import Handler
        Handler._write_paced(handler.wfile, data)


def _relay(client_sock: socket.socket, up_sock: socket.socket):
    """双向裸字节中继, 任一侧关闭即止。"""
    socks = [client_sock, up_sock]
    try:
        while True:
            r, _, _ = select.select(socks, [], [], 300)
            if not r:
                break
            for s in r:
                try:
                    data = s.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                other = up_sock if s is client_sock else client_sock
                try:
                    other.sendall(data)
                except OSError:
                    return
    except OSError:
        pass


def tunnel_ws(handler):
    """把 handler 连接升级为到 pixel-agents /ws 的隧道, 裸字节双向中继。"""
    host, port = _upstream_hostport()
    try:
        up = socket.create_connection((host, port), timeout=10)
    except OSError as e:
        handler._send(502, {"error": f"pixel-agents ws connect failed: {e}"})
        return

    # 用浏览器原始请求头重建升级请求(Host 保留)。
    lines = ["GET %s HTTP/1.1" % handler.path]
    for k, v in handler.headers.items():
        if k.lower() in ("proxy-connection",):
            continue
        lines.append("%s: %s" % (k, v))
    try:
        up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    except OSError:
        up.close()
        return

    # 读上游 101 响应头。
    buf = b""
    try:
        while b"\r\n\r\n" not in buf:
            chunk = up.recv(4096)
            if not chunk:
                handler._send(502, {"error": "upstream closed during upgrade"})
                up.close()
                return
            buf += chunk
    except OSError:
        up.close()
        return
    head, rest = buf.split(b"\r\n\r\n", 1)
    status_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
    if "101" not in status_line:
        handler._send(502, {"error": "upstream refused ws: %s" % status_line})
        up.close()
        return

    # ★ 劫持: 把上游原始 101 字节直接写裸 socket(绕开 wfile 缓冲),
    #   否则与中继的裸写打架破坏握手。
    handler.close_connection = True
    try:
        handler.connection.sendall(head + b"\r\n\r\n")
        if rest:
            handler.connection.sendall(rest)
    except OSError:
        up.close()
        return
    _relay(handler.connection, up)
    try:
        up.close()
    except OSError:
        pass
