"""文件浏览 API 测试 (issue #46, s4-impl).

依据: docs/ARCH-FILEBROWSER.md §9.1 测试清单 + mw/console_files.py 实现。
覆盖: 穿越 / 越权 / symlink / list / file / 只读 / 404 / 一致性 / 回归。

设计: 纯函数测试。构造临时沙箱项目目录, 调用 console_files 纯函数, 不依赖
console.HTTP 路由层。后端 _fs_resolve / api_fs_list / api_fs_file / handle_fs_get
均是无 HTTP 副作用的纯函数 (返回 dict / 抛 FsPathError); 路由接线由
console.py do_GET 的 <15 行插入实现, 单测覆盖在 console 端 (S5/S6 域)。

pytest 兼容: pytest 入口跑 subprocess, 真测试在隔离子进程里完成,
避免污染兄弟测试模块的状态 (mw.console 在 pytest 收集阶段已被 import,
改其 WS_PROJECTS 会破坏后续测试)。

跑法:
  - 直接: python3 tests/test_fs_api.py          (跑真测试)
  - pytest: pytest tests/test_fs_api.py -q      (pytest 包装: 子进程跑真测试)
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve()
# 标记: pytest 把本文件当模块 import 时, __name__ == 'tests.test_fs_api',
# 不是 '__main__'; 真测试段只在 __main__ 跑, 避免双跑。
IS_PYTEST = __name__ != '__main__'


def _run_subprocess():
    """把当前文件当脚本再跑一次: __main__ 段是真测试。"""
    r = subprocess.run(
        [sys.executable, str(HERE)],
        capture_output=True, text=True, cwd=str(ROOT),
        env={**os.environ,
             'MW_LANG': 'zh-cn',
             'LANG': 'en_US.UTF-8'},
        timeout=120,
    )
    print(r.stdout)
    if r.returncode != 0:
        print('--- STDERR ---', file=sys.stderr)
        print(r.stderr, file=sys.stderr)
    return r.returncode


if IS_PYTEST:
    # pytest 入口: 注册一个 fake test 函数, 内部跑 subprocess。
    # 避免模块顶层 sys.exit (会触发 pytest INTERNALERROR)。
    def test_fs_api_subprocess():
        rc = _run_subprocess()
        assert rc == 0, f"fs API subprocess 退出码 {rc} (非 0)"


# ──────────────────────────────────────────────────────────────────
# 真测试段 (script-style, 直接跑 `python3 tests/test_fs_api.py` 时执行)
# ──────────────────────────────────────────────────────────────────
if __name__ == '__main__':  # noqa: F811 — 再被脚本入口执行
    import shutil as _shutil
    import tempfile as _tempfile
    from urllib.parse import parse_qs as _parse_qs

    _ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(_ROOT))

    # 隔离: 测试用临时沙箱目录 (与生产 /share_workspace 解耦)
    BASE = Path(_tempfile.mkdtemp(prefix="fs_api_"))
    PROJ_DIR = BASE / "projects" / "simpledot-agent-studio"
    PROJ_DIR.mkdir(parents=True)
    (PROJ_DIR / "docs").mkdir()
    (PROJ_DIR / "docs" / "REQ.md").write_text("# hello\nfoo\n", encoding="utf-8")
    (PROJ_DIR / "docs" / "run.log").write_text("log line\n", encoding="utf-8")
    (PROJ_DIR / "src").mkdir()
    (PROJ_DIR / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (PROJ_DIR / "data.bin").write_bytes(b"\x00\x01\x02")
    (PROJ_DIR / "subdir").mkdir()
    (PROJ_DIR / "subdir" / "sub.txt").write_text("sub\n", encoding="utf-8")
    os.symlink("/etc/passwd", PROJ_DIR / "link.txt")
    os.symlink("/etc", PROJ_DIR / "sub_link")

    OTHER_DIR = BASE / "projects" / "other-project"
    OTHER_DIR.mkdir(parents=True)
    (OTHER_DIR / "secret.md").write_text("secret\n", encoding="utf-8")

    os.environ["MW_ALLOWED_WS_PREFIX"] = str(BASE)

    import mw.console as console
    import mw.console_files as cf

    assert cf.WS_PROJECTS == console.WS_PROJECTS, (
        f"WS_PROJECTS 漂移: console={console.WS_PROJECTS} "
        f"console_files={cf.WS_PROJECTS}")
    assert cf.WS_PROJECTS == str(BASE / "projects")

    PASS, FAIL = [], []

    def check(name, cond, detail=""):
        (PASS if cond else FAIL).append((name, detail))
        print(f"  {'PASS' if cond else 'FAIL'}  {name}"
              + (f"  {detail}" if detail else ""))


    print("\n[穿越 — 各种 .. 编码变体]")
    import urllib.parse as _up
    for raw in ["..", "..%2f", "....//", "%2e%2e%2f", "a%2f..%2f..%2fetc",
                "..\\", "a..b", "%00etc", "//etc/passwd"]:
        decoded = _up.unquote(raw)
        try:
            cf._fs_resolve("simpledot-agent-studio", decoded)
            check(f"reject[{raw!r}]", False, "应拒")
        except cf.FsPathError as e:
            check(f"reject [{raw!r}] -> {e.code}",
                  e.code in (400, 403) and "error" not in e.msg.lower()
                  and "/etc" not in e.msg, f"code={e.code} msg={e.msg!r}")

    try:
        root = cf._fs_resolve("simpledot-agent-studio", "")
        check("empty rel → 项目根", root == str(PROJ_DIR))
    except cf.FsPathError as e:
        check("empty rel 应允许", False, f"code={e.code}")

    print("\n[越权]")
    try:
        cf._fs_resolve("simpledot-agent-studio", "/etc/passwd")
        check("rel=/etc/passwd", False, "应拒")
    except cf.FsPathError as e:
        check("rel=/etc/passwd → 403", e.code == 403)

    try:
        cf._fs_resolve("simpledot-agent-studio", "../uploads/x")
        check("rel=../uploads/x", False, "应拒")
    except cf.FsPathError as e:
        check("rel=../uploads/x → 400", e.code == 400)

    try:
        cf._fs_resolve("simpledot-agent-studio", "../other-project/secret.md")
        check("rel=../other-project/secret.md", False, "应拒")
    except cf.FsPathError as e:
        check("rel=../other-project/secret.md → 400 (normpath 层)",
              e.code == 400)

    for proj in ["", "..", "../etc"]:
        try:
            cf._fs_resolve(proj, "")
            check(f"project={proj!r}", False, "应拒")
        except cf.FsPathError as e:
            check(f"project={proj!r} → 403", e.code == 403)

    print("\n[symlink]")
    try:
        cf._fs_resolve("simpledot-agent-studio", "link.txt")
        check("symlink file", False, "应拒")
    except cf.FsPathError as e:
        check("symlink file → 403", e.code == 403)

    result = cf.api_fs_list("simpledot-agent-studio", "")
    items = {it["name"]: it for it in result["items"]}
    check("list 含 link.txt", "link.txt" in items)
    check("list 含 sub_link (symlink dir)", "sub_link" in items)
    if "link.txt" in items:
        check("link.txt.link=true", items["link.txt"]["link"] is True)
        check("link.txt.dir=false", items["link.txt"]["dir"] is False)
    if "sub_link" in items:
        check("sub_link.link=true", items["sub_link"]["link"] is True)
        check("sub_link.dir=false (symlink 目录算 link)",
              items["sub_link"]["dir"] is False)

    try:
        cf._fs_resolve("simpledot-agent-studio", "link.txt")
        check("file→symlink", False, "应拒")
    except cf.FsPathError as e:
        check("file→symlink 403", e.code == 403)

    print("\n[list]")
    result = cf.api_fs_list("simpledot-agent-studio", "")
    items = result["items"]
    names = [it["name"] for it in items]
    check("目录在前", all(d in names for d in ["docs", "src", "subdir"]),
          f"dirs={[i['name'] for i in items if i['dir']]}")
    for it in items:
        check(f"item[{it['name']}].size is int", isinstance(it["size"], int))
        check(f"item[{it['name']}].mtime is int", isinstance(it["mtime"], int))

    (PROJ_DIR / ".hidden").write_text("x", encoding="utf-8")
    result = cf.api_fs_list("simpledot-agent-studio", "")
    names = [it["name"] for it in result["items"]]
    check("隐藏 .hidden", ".hidden" not in names)

    many_dir = PROJ_DIR / "many"
    many_dir.mkdir()
    for i in range(1005):
        (many_dir / f"f{i:04d}.txt").write_text("x", encoding="utf-8")
    result = cf.api_fs_list("simpledot-agent-studio", "many")
    check("truncated=true", result["truncated"] is True)
    check("items=1000", len(result["items"]) == 1000)
    check("total=1005", result["total"] == 1005)
    _shutil.rmtree(many_dir)

    try:
        cf.api_fs_list("simpledot-agent-studio", "data.bin")
        check("list→file", False, "应拒")
    except cf.FsPathError as e:
        check("list→file → 400", e.code == 400)

    print("\n[file — 三 mode 响应头]")

    class MockHandler:
        def __init__(self):
            self.headers = []
            self.status = None
            self.body = b""
            self.wfile = self

        def send_response(self, code):
            self.status = code

        def send_header(self, k, v):
            self.headers.append((k, v))

        def end_headers(self):
            pass

        def write(self, chunk):
            self.body += chunk

        def _write_paced(self, wfile, data):
            """Mock: 直接写入, 模拟 console.py Handler._write_paced"""
            wfile.write(data)


    def file_headers_for(rel, mode):
        h = MockHandler()
        h.wfile = h
        abs_path = cf._fs_resolve("simpledot-agent-studio", rel)
        cf._stream_file(h, abs_path, mode)
        return h


    h = file_headers_for("docs/REQ.md", "embed")
    hd = dict(h.headers)
    check("md embed CT", hd.get("Content-Type", "").startswith("text/plain"))
    check("md embed nosniff", hd.get("X-Content-Type-Options") == "nosniff")
    check("md embed 无 CSP", "Content-Security-Policy" not in hd)

    h = file_headers_for("docs/REQ.md", "open")
    hd = dict(h.headers)
    check("md open CD=inline",
          hd.get("Content-Disposition", "").startswith("inline"))
    check("md open nosniff", hd.get("X-Content-Type-Options") == "nosniff")

    h = file_headers_for("docs/REQ.md", "download")
    hd = dict(h.headers)
    check("md download CD=attachment",
          hd.get("Content-Disposition", "").startswith("attachment"))
    check("md download RFC 5987",
          "filename*=UTF-8''" in hd.get("Content-Disposition", ""))
    check("md download nosniff", hd.get("X-Content-Type-Options") == "nosniff")

    h = file_headers_for("src/main.py", "embed")
    hd = dict(h.headers)
    check("py embed CT=text/plain",
          hd.get("Content-Type", "").startswith("text/plain"))
    check("py embed nosniff", hd.get("X-Content-Type-Options") == "nosniff")

    h = file_headers_for("data.bin", "embed")
    hd = dict(h.headers)
    check("data.bin embed CT=octet-stream",
          hd.get("Content-Type") == "application/octet-stream")

    try:
        h = MockHandler()
        cf.handle_fs_get(h, "/api/fs/file",
                         _parse_qs("project=simpledot-agent-studio&path=docs"))
        check("file→dir → 400",
              h.status == 400 and b"bad request" in h.body,
              f"status={h.status} body={h.body!r}")
    except cf.FsPathError as e:
        check("file→dir → 400", e.code == 400)

    try:
        cf._fs_resolve("simpledot-agent-studio", "link.txt")
        check("file→symlink", False, "应拒")
    except cf.FsPathError as e:
        check("file→symlink → 403", e.code == 403)

    print("\n[只读]")

    class MockReqHandler(MockHandler):
        pass


    h = MockReqHandler()
    cf.handle_fs_get(h, "/api/fs/list",
                     _parse_qs("project=simpledot-agent-studio&path="))
    check("GET /api/fs/list → 200", h.status == 200)

    h = MockReqHandler()
    cf.handle_fs_get(h, "/api/fs/file",
                     _parse_qs("project=simpledot-agent-studio"
                              + "&path=docs/REQ.md&mode=embed"))
    check("GET /api/fs/file → 200", h.status == 200)

    print("\n[404]")
    try:
        cf._fs_resolve("simpledot-agent-studio", "no-such-file.md")
        check("missing file", False, "应 404")
    except cf.FsPathError as e:
        check("missing file → 404", e.code == 404)

    try:
        cf._fs_resolve("simpledot-agent-studio", "no-such-dir/file")
        check("missing dir", False, "应 404")
    except cf.FsPathError as e:
        check("missing dir → 404", e.code == 404)

    try:
        cf._fs_resolve("no-such-project", "")
        check("missing project", False, "应 404")
    except cf.FsPathError as e:
        check("missing project → 404", e.code == 404)

    print("\n[零回显]")
    err = cf.FsPathError(403, "forbidden")
    check("FsPathError msg 固定", err.msg == "forbidden")
    check("FsPathError 不含路径", "/etc" not in err.msg)
    check("FsPathError 不含栈", "Traceback" not in err.msg)

    _shutil.rmtree(BASE, ignore_errors=True)

    total = len(PASS) + len(FAIL)
    print(f"\n{'='*60}")
    print(f"  fs API 测试: {len(PASS)}/{total} PASS, {len(FAIL)} FAIL")
    if FAIL:
        print("FAIL:")
        for n, d in FAIL:
            print(f"  - {n}  {d}")
    sys.exit(0 if not FAIL else 1)