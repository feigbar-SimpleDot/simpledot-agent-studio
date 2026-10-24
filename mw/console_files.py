# mw/console_files.py — 只读文件浏览 API (issue #46)
#
# 提供 GET /api/fs/list 与 GET /api/fs/file 两个端点的纯函数实现:
#   - api_fs_list(project, path): 单层目录 + 排序 + 截断1000 + size/mtime/link 元数据
#   - api_fs_file(project, path, mode): 流式回源 (64KB chunk) + 按 mode 出响应头
#   - handle_fs_get(handler, p, qs): 路由分发 + 错误码映射
#
# 安全核心 _fs_resolve 五层链 (§3.2):
#   1. project 形状 (FS_PROJECT_RE)
#   2. rel 语法 (\x00 / \\ / 超长 / / 开头 / .. 子串)
#   3. normpath 锚本项目
#   4. realpath 锚 symlink 逃逸
#   5. symlink 终判 (OQ-3, 一律 403)
#
# 设计依据: docs/ARCH-FILEBROWSER.md §3 (issue #46, s3-arch)
# 不变量 (s5 必查):
#   - _fs_resolve 是 /api/fs/* 唯一取路径入口
#   - 它永不返回 WS_PROJECTS 根或兄弟项目路径下的位置
#   - 错误响应不回显 str(e)、绝对路径、请求 URL (AC-G2/F1)
import os
import re
import json
import urllib.parse as urlparse_mod
from pathlib import Path

# 与 mw/console.py 同源 env: 由 console.py do_GET lazy-import 本模块,
# 本模块顶层不 import console(避循环); 常量复制读取同一 env,
# 并在 tests/test_fs_api.py 断言两模块 WS_PROJECTS 相等, 防漂移。
# 注意: 运行时由 console.py Handler 类实例调用, 可直接用 handler._write_all。
WS_ROOT = os.environ.get("MW_ALLOWED_WS_PREFIX", "/share_workspace")
WS_PROJECTS = os.path.join(WS_ROOT, "projects")

# project 名正则: 比 console.WS_NAME_RE 宽 (允许 . 与 _, 但不含 .. 子串),
# 路径安全链 2/3/4/5 兜底足够拦截。
FS_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LIST_LIMIT = 1000          # R1.4 截断
MAX_REL_BYTES = 4096       # 路径长度护栏
CHUNK = 65536              # 64KB 流式读块

# 扩展名/类型单一口径表 (前后端各持一份, 必须逐行一致)。
# 与 kinds.js 的 KNOWN_EXTS + extOf 完全对齐。
# 文件类型 = (content_type, mode=embed 下加 sandbox 吗?, mode=open 的 CSP)
# 仅按 ext 区分; "目录"与"未知扩展名"在调用侧处理。
TEXT_EXT = {".md", ".markdown", ".txt", ".log", ".json", ".yaml", ".yml",
            ".csv", ".py", ".js", ".jsx", ".sh", ".ini", ".toml"}
HTML_EXT = {".html", ".htm"}
PDF_EXT = {".pdf"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
# svg 不在预览白名单 (ADR-4), 走兜底卡, open/download 仍可用。

# 默认扩展名 → MIME (text 类统一 text/plain;charset=utf-8)
_EXT_TO_CT = {}
for _ext in TEXT_EXT:
    _EXT_TO_CT[_ext] = "text/plain; charset=utf-8"
for _ext in HTML_EXT:
    _EXT_TO_CT[_ext] = "text/html; charset=utf-8"
_EXT_TO_CT[".pdf"] = "application/pdf"
for _ext in IMAGE_EXT:
    _EXT_TO_CT[_ext] = f"image/{'jpeg' if _ext in ('.jpg', '.jpeg') else _ext[1:]}"


class FsPathError(Exception):
    """fs 接口统一错误: code=HTTP 状态, msg=固定英文短消息 (不回显 e)。"""

    def __init__(self, code: int, msg: str):
        self.code, self.msg = code, msg
        super().__init__(msg)


def _is_text(ext: str) -> bool:
    return ext in TEXT_EXT


def _is_html(ext: str) -> bool:
    return ext in HTML_EXT


def _is_pdf(ext: str) -> bool:
    return ext in PDF_EXT


def _is_image(ext: str) -> bool:
    return ext in IMAGE_EXT


def _content_type_for(ext: str) -> str:
    return _EXT_TO_CT.get(ext, "application/octet-stream")


def _decode_rel(raw: str) -> str:
    """ARCH §3.2: 输入已是 URL-percent-decoded 一次 (parse_qs 已做)。
    这里保持原样透传, 不二次解码 — 双重编码变体 (如 ..%252f → 解码后 .. → 拒)
    由层 2-5 (子串检查 + normpath/realpath + islink) 兜底拦截。
    历史注: 旧版本曾再 unquote 一次, 会把合法含字面 %20 / %2e 的文件名误解码, 故修正。
    """
    if raw is None:
        return ""
    return raw


def _check_project(project: str) -> str:
    """层 1: project 形状白名单。失败 → 403。"""
    if not isinstance(project, str) or not FS_PROJECT_RE.match(project):
        raise FsPathError(403, "forbidden")
    return project


def _check_rel_syntax(rel: str) -> str:
    """层 2: rel 语法校验 (空字节/反斜杠/超长// 开头//.. 子串 → 拒)。
    返回归一化后 (decode) 的 rel; normpath 收敛空段、./ 等冗余。
    """
    if not isinstance(rel, str):
        raise FsPathError(400, "bad request")
    raw_bytes = rel.encode("utf-8", errors="replace")
    if len(raw_bytes) > MAX_REL_BYTES:
        raise FsPathError(400, "bad request")
    if "\x00" in rel or "\\" in rel:
        raise FsPathError(400, "bad request")
    if rel.startswith("/"):
        raise FsPathError(403, "forbidden")
    # 任一路径段含 ".." 子串 (宁拦错文件名, 不让编码变体钻空)。
    for seg in rel.split("/"):
        if ".." in seg:
            raise FsPathError(400, "bad request")
    return rel


def _fs_resolve(project: str, rel: str) -> str:
    """五层安全解析链 (§3.2)。
    输入 project + rel (URL-percent-decoded); 返回 abs 路径 (normpath, 非 realpath);
    失败抛 FsPathError。
    调用方拿到此 abs 后, 自己再 os.path.isdir / isfile / iterdir 做后续动作。
    """
    project = _check_project(project)
    rel = _check_rel_syntax(rel)
    # 空 rel = 项目根
    if rel == "":
        rel = "."

    P_root = os.path.normpath(os.path.join(WS_PROJECTS, project))
    # 层 3: normpath 锚本项目
    norm = os.path.normpath(os.path.join(P_root, rel))
    if norm != P_root and not norm.startswith(P_root + os.sep):
        raise FsPathError(403, "forbidden")
    # 层 4: realpath 锚 symlink 逃逸 (含中间段)
    rp = os.path.realpath(norm)
    rp_root = os.path.realpath(P_root)
    if rp != rp_root and not rp.startswith(rp_root + os.sep):
        raise FsPathError(403, "forbidden")
    # 层 5: symlink 终判 (OQ-3: 一律 403)
    if os.path.islink(norm):
        raise FsPathError(403, "forbidden")
    # 存在性: 不存在 → 404 (list/file 都查)
    if not os.path.exists(norm):
        raise FsPathError(404, "not found")
    return norm


def _ext_of(name: str) -> str:
    if not isinstance(name, str):
        return ""
    i = name.rfind(".")
    if i <= 0:
        return ""
    return name[i:].lower()


def api_fs_list(project: str, path: str = "") -> dict:
    """GET /api/fs/list?project=<p>&path=<rel>

    返回: {project, path, items[], truncated, total}
    排序: 目录在前、文件在后, 各按 name 字典序 (Python 码点序)。
    截断: 过滤后 >1000 → 取前 1000, truncated:true, total: 全量过滤后数。
    """
    norm = _fs_resolve(project, path)
    if not os.path.isdir(norm):
        raise FsPathError(400, "bad request")

    rel_norm = ""
    if path:
        # 归一化的 rel 用于响应 (展示用, 不回显绝对路径)
        try:
            rel_norm = os.path.relpath(norm,
                                       os.path.normpath(
                                           os.path.join(WS_PROJECTS, project)))
            if rel_norm == ".":
                rel_norm = ""
        except ValueError:
            rel_norm = path

    raw_items = []
    try:
        it = os.scandir(norm)
    except OSError:
        raise FsPathError(500, "internal error")
    for entry in it:
        try:
            name = entry.name
            if name.startswith("."):
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            is_link = entry.is_symlink()
            # lstat 元数据: link 不取 stat (follow_symlinks=False 已拒绝 symlink 进入,
            # 这里 list 仍允许 symlink 显示但记 link=true)
            st = entry.stat(follow_symlinks=False)
            raw_items.append({
                "name": name,
                "dir": bool(is_dir and not is_link),  # symlink 目录算 link, 不算 dir
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "link": bool(is_link),
            })
        except OSError:
            continue

    raw_items.sort(key=lambda x: (not x["dir"], x["name"]))
    total = len(raw_items)
    items = raw_items[:LIST_LIMIT]
    return {
        "project": project,
        "path": rel_norm,
        "items": items,
        "truncated": total > LIST_LIMIT,
        "total": total,
    }


def _parse_qs_list(qs) -> str:
    """从 parse_qs 字典里取 path 字符串 (单值)。"""
    if not qs:
        return ""
    v = qs.get("path", [""])
    if isinstance(v, list):
        return v[0] if v else ""
    return v


def _parse_qs_get(qs, key: str, default: str = "") -> str:
    if not qs:
        return default
    v = qs.get(key, [default])
    if isinstance(v, list):
        return v[0] if v else default
    return v


def _send_headers(handler, code: int, headers: dict, body: bytes) -> None:
    """统一写响应头 + body (do_GET 已 send_header Cache-Control 等通用头, 这里扩写)。"""
    handler.send_response(code)
    for k, v in headers.items():
        handler.send_header(k, v)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    if body:
        # issue #62 A-4: 复用 console.Handler._write_all 单点真源 (懒导入避循环,
        # 调用时 mw.console 必已加载); wbufsize=0 短写纪律与 console 侧一致。
        from .console import Handler
        Handler._write_all(handler.wfile, body)


def _csp_for_open_html() -> str:
    """open 模式 html: 强制 sandbox CSP, 禁脚本禁外链 (ADR-4)。"""
    return "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"


def _csp_for_open_pdf() -> str:
    return "default-src 'none'; sandbox"


def _csp_for_open_svg() -> str:
    return "default-src 'none'; sandbox"


def _stream_file(handler, abs_path: str, mode: str) -> None:
    """mode=embed|open|download 的响应头 + 流式回源 (§3.4 矩阵)。"""
    name = os.path.basename(abs_path) or "file"
    ext = _ext_of(name)
    ct = _content_type_for(ext)
    is_html = _is_html(ext)
    is_pdf = _is_pdf(ext)
    is_image = _is_image(ext)
    is_text = _is_text(ext)
    # disposition
    disposition = "attachment" if mode == "download" else "inline"
    # RFC 5987 filename* (UTF-8'' + quote)
    fname_ascii = "".join(c if (ord(c) < 128 and c.isalnum() or c in ".-_") else "_"
                          for c in name) or "file"
    fname_star = urlparse_mod.quote(name, safe="")
    headers = {
        "Content-Type": ct,
        "Content-Disposition":
            f'{disposition}; filename="{fname_ascii}"; '
            f"filename*=UTF-8''{fname_star}",
        "X-Content-Type-Options": "nosniff",
    }
    # 决定是否带 CSP: 仅 open 模式带 (embed 模式靠 iframe sandbox 隔离, 不带 CSP)
    if mode == "open":
        if is_html:
            headers["Content-Security-Policy"] = _csp_for_open_html()
        elif is_pdf:
            headers["Content-Security-Policy"] = _csp_for_open_pdf()
        elif ext == ".svg":
            headers["Content-Type"] = "image/svg+xml"
            headers["Content-Security-Policy"] = _csp_for_open_svg()
        elif is_image:
            headers["Content-Security-Policy"] = "sandbox"

    try:
        size = os.path.getsize(abs_path)
    except OSError:
        raise FsPathError(404, "not found")

    handler.send_response(200)
    for k, v in headers.items():
        handler.send_header(k, v)
    handler.send_header("Content-Length", str(size))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    try:
        # issue #62/64 A-4 → P-2: Apple container 代理截断适配; chunk 本身 64 KB
        # → 实际走 _write_paced 直通, 但为 fs/file 整文件一次性写出面与纪律统一,
        # 改调用即可, 行为不变; 流中断兜底原样保留。
        from .console import Handler
        with open(abs_path, "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                Handler._write_paced(handler.wfile, chunk)
    except (BrokenPipeError, ConnectionResetError):
        # 流中断: 客户端已走, 静默吞 (R-2)
        return
    except OSError:
        # 读取中途失败: 已发 200, 仅断流, 不抛 5xx (避免重复 send_response)
        return


def handle_fs_get(handler, path: str, qs) -> None:
    """console.py do_GET 唯一调用入口: 路由分发 + 错误码映射。"""
    try:
        if path == "/api/fs/list":
            project = _parse_qs_get(qs, "project", "")
            rel_raw = _parse_qs_list(qs)
            rel = _decode_rel(rel_raw)
            data = api_fs_list(project, rel)
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            _send_headers(handler, 200,
                          {"Content-Type": "application/json; charset=utf-8"},
                          body)
            return
        if path == "/api/fs/file":
            project = _parse_qs_get(qs, "project", "")
            rel_raw = _parse_qs_list(qs)
            rel = _decode_rel(rel_raw)
            mode = _parse_qs_get(qs, "mode", "embed")
            if mode not in ("embed", "open", "download"):
                mode = "embed"  # 宽松: 拼错不炸 400
            norm = _fs_resolve(project, rel)
            if not os.path.isfile(norm):
                # file 命中目录 → 400 (list 命中文件也是 400, 对称)
                raise FsPathError(400, "bad request")
            _stream_file(handler, norm, mode)
            return
        # 路径 /api/fs/<other> → 404
        handler.send_response(404)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        # issue #62/64: 统一使用 pacing 循环写全(Apple container 代理截断适配)
        handler._write_paced(handler.wfile, b'{"error":"not found"}')
    except FsPathError as e:
        body = json.dumps({"error": e.msg}, ensure_ascii=False).encode("utf-8")
        try:
            handler.send_response(e.code)
            handler.send_header("Content-Type",
                                "application/json; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            # issue #62/64: 统一使用 pacing 循环写全
            handler._write_paced(handler.wfile, body)
        except (BrokenPipeError, ConnectionResetError):
            return