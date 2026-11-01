"""文件浏览器写面测试 (issue #158: 上传 / 删除).

依据: docs/PLAN-L-20261029.md §2.9 测试矩阵 T-158-A..K + mw/console_files.py 实现。
覆盖: 路径穿越 / 越界 project / symlink / 目录删除拒绝 / 超限 / 二次确认 /
同名冲突 / 正常上传 / 零回显 / 只读面零回归 / 上限常量防漂移 / 静态守护。

设计: 纯函数测试 (api_fs_upload / api_fs_delete / handle_fs_post 均无 HTTP
副作用; handle_fs_post 用 fake handler 收响应)。pytest 兼容形态与
tests/test_fs_api.py 完全一致: pytest 入口跑 subprocess, 真测试在隔离子进程
完成 —— mw.console_files.WS_PROJECTS 是模块级常量, 改它会污染同进程兄弟测试。

跑法:
  - 直接: python3 tests/test_issue158_fs_write.py
  - pytest: pytest tests/test_issue158_fs_write.py -q
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve()
# pytest 把本文件当模块 import 时 __name__ != '__main__'; 真测试段只在
# __main__ 跑, 避免双跑 (同 test_fs_api.py 口径)。
IS_PYTEST = __name__ != '__main__'


def _run_subprocess():
    """把当前文件当脚本再跑一次: __main__ 段是真测试。"""
    r = subprocess.run(
        [sys.executable, str(HERE)],
        capture_output=True, text=True, cwd=str(ROOT),
        env={**os.environ, 'MW_LANG': 'zh-cn', 'LANG': 'en_US.UTF-8'},
        timeout=180,
    )
    print(r.stdout)
    if r.returncode != 0:
        print('--- STDERR ---', file=sys.stderr)
        print(r.stderr, file=sys.stderr)
    return r.returncode


if IS_PYTEST:
    def test_issue158_subprocess():
        rc = _run_subprocess()
        assert rc == 0, f"issue158 fs-write subprocess 退出码 {rc} (非 0)"


# ──────────────────────────────────────────────────────────────────
# 真测试段 (script-style, 直接跑 `python3 tests/test_issue158_fs_write.py`)
# ──────────────────────────────────────────────────────────────────
if __name__ == '__main__':  # noqa: F811
    import ast
    import base64
    import io
    import json
    import tempfile as _tempfile
    import urllib.parse as _up

    _ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(_ROOT))

    # 隔离沙箱 (env 必须在 import mw.console_files 之前设置 —— 模块级常量)
    BASE = Path(_tempfile.mkdtemp(prefix="fs_write_"))
    PROJ = BASE / "projects" / "p1"
    PROJ.mkdir(parents=True)
    (PROJ / "docs").mkdir()
    (PROJ / "docs" / "REQ.md").write_text("# hello\n", encoding="utf-8")
    (PROJ / "keep.txt").write_text("keep me\n", encoding="utf-8")
    (PROJ / "subdir").mkdir()
    (PROJ / "subdir" / "in.txt").write_text("in\n", encoding="utf-8")
    # 外部 symlink (层 4 realpath 拦) 与内部 symlink (层 5 islink 拦)
    os.symlink("/etc/passwd", PROJ / "link_out.txt")
    os.symlink("docs/REQ.md", PROJ / "link_in.md")
    OTHER = BASE / "projects" / "other"
    OTHER.mkdir(parents=True)
    (OTHER / "secret.md").write_text("secret\n", encoding="utf-8")

    os.environ["MW_ALLOWED_WS_PREFIX"] = str(BASE)

    import mw.console as console
    import mw.console_files as cf

    PASS, FAIL = [], []

    def check(name, cond, detail=""):
        (PASS if cond else FAIL).append((name, detail))
        print(f"  {'PASS' if cond else 'FAIL'}  {name}"
              + (f"  {detail}" if detail else ""))

    def expect_err(fn, codes=(400, 403, 404, 409, 413), tag=""):
        """调用应抛 FsPathError 且 code 在预期集; 返回 (code, msg)。"""
        try:
            fn()
            check(f"reject {tag}", False, "应拒但成功")
            return (None, "")
        except cf.FsPathError as e:
            check(f"reject {tag} -> {e.code}", e.code in codes,
                  f"msg={e.msg!r}")
            return (e.code, e.msg)

    def no_outside(tag):
        """★ 硬断言: 项目外 (BASE 之外 或 other 项目) 无任何新建文件。"""
        leaked = [str(p) for p in OTHER.iterdir()
                  if p.name != "secret.md"]
        check(f"项目外无泄漏 [{tag}]", not leaked, str(leaked))

    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode()

    # ══ T-158-A 路径穿越 (AC-158-3) ══════════════════════════════
    print("\n[T-158-A 路径穿越 — 与 test_fs_api.py 同一组向量]")
    TRAVERSALS = ["..", "../uploads/x", "..%2f", "%2e%2e%2f", "....//",
                  "a%2f..%2f..%2fetc", "..\\", "a..b", "%00etc",
                  "//etc/passwd"]
    for raw in TRAVERSALS:
        decoded = _up.unquote(raw)
        expect_err(lambda: cf.api_fs_upload("p1", decoded, "x.txt", b"hi"),
                   tag=f"upload path[{raw!r}]")
        expect_err(lambda: cf.api_fs_delete("p1", decoded, True),
                   tag=f"delete path[{raw!r}]")
    no_outside("穿越向量组")
    # 删除被拒后目标文件仍在
    check("拒绝后 keep.txt 仍在", (PROJ / "keep.txt").exists())

    # name 含 ../escape.txt → 4xx 或消毒后落盘项目内 (两种都接受), 项目外零创建
    try:
        r = cf.api_fs_upload("p1", "", "../escape.txt", b"evil")
        check("name ../escape.txt 落盘为消毒名", r["name"] == "escape.txt"
              and (PROJ / "escape.txt").exists(), f"got {r['name']!r}")
    except cf.FsPathError as e:
        check("name ../escape.txt 被拒", e.code in (400, 403), f"code={e.code}")
    no_outside("name ../escape.txt")
    # name 消毒后仍含 .. 子串 (a..b) → 第二道 _fs_resolve 层 2 拒 (MV-158-B 牙齿)
    expect_err(lambda: cf.api_fs_upload("p1", "", "a..b.txt", b"x"),
               codes=(400,), tag="name a..b.txt")
    check("a..b.txt 未落盘", not (PROJ / "a..b.txt").exists())
    # ★ MV-158-H 牙齿: 落盘名必须等于消毒后的名字 (不消毒则含 / 的名字漏过)
    r = cf.api_fs_upload("p1", "", "bad name/with spaces!x.txt", b"z")
    check("消毒名落盘", r["name"] == "bad_name_with_spaces_x.txt"
          and (PROJ / r["name"]).exists(), f"got {r['name']!r}")
    no_outside("消毒用例")
    # 超长名截断 200
    r = cf.api_fs_upload("p1", "", "长" * 300 + ".txt", b"t")
    check("超长名截断 ≤200", len(r["name"]) <= 200, f"len={len(r['name'])}")

    # ══ T-158-B 越界 project ═════════════════════════════════════
    print("\n[T-158-B 越界 project]")
    for bad in ["..", "../etc", "no-such-project", "", "a/b"]:
        expect_err(lambda: cf.api_fs_upload(bad, "", "x.txt", b"hi"),
                   codes=(400, 403, 404), tag=f"upload project[{bad!r}]")
        expect_err(lambda: cf.api_fs_delete(bad, "keep.txt", True),
                   codes=(400, 403, 404), tag=f"delete project[{bad!r}]")
    no_outside("越界 project")

    # ══ T-158-C symlink ══════════════════════════════════════════
    print("\n[T-158-C symlink]")
    expect_err(lambda: cf.api_fs_delete("p1", "link_out.txt", True),
               codes=(403,), tag="删外部 symlink")
    check("symlink 目标 /etc/passwd 仍在", os.path.exists("/etc/passwd"))
    check("link_out.txt 未被删", (PROJ / "link_out.txt").is_symlink())
    expect_err(lambda: cf.api_fs_delete("p1", "link_in.md", True),
               codes=(403,), tag="删内部 symlink (MV-158-I 牙齿)")
    check("link_in.md 未被删", (PROJ / "link_in.md").is_symlink())
    expect_err(lambda: cf.api_fs_upload("p1", "", "link_out.txt", b"x"),
               codes=(403,), tag="上传覆盖外部 symlink 名")
    expect_err(lambda: cf.api_fs_upload("p1", "", "link_in.md", b"x"),
               codes=(403,), tag="上传覆盖内部 symlink 名")
    expect_err(lambda: cf.api_fs_upload("p1", "sub_link", "x.txt", b"x"),
               codes=(400, 403, 404), tag="上传进 symlink 目录")

    # ══ T-158-D 目录删除拒绝 ═════════════════════════════════════
    print("\n[T-158-D 目录删除]")
    expect_err(lambda: cf.api_fs_delete("p1", "subdir", True),
               codes=(400,), tag="删目录")
    check("目录及内容完好", (PROJ / "subdir" / "in.txt").exists())
    expect_err(lambda: cf.api_fs_delete("p1", "", True),
               codes=(400,), tag="删项目根")
    check("项目根完好", PROJ.exists())

    # ══ T-158-E 超限 ═════════════════════════════════════════════
    print("\n[T-158-E 体积上限]")
    expect_err(lambda: cf.api_fs_upload("p1", "", "big.bin",
                                        os.urandom(21 * 1024 * 1024)),
               codes=(413,), tag="21MB 上传")
    check("超限无残留文件", not (PROJ / "big.bin").exists())
    r = cf.api_fs_upload("p1", "", "exact20.bin",
                         os.urandom(20 * 1024 * 1024))
    check("恰好 20MB 成功 (边界含端点)", r["ok"] and r["size"] == 20 * 1024 * 1024)
    (PROJ / "exact20.bin").unlink()

    # ══ T-158-F 二次确认 (AC-158-2) ══════════════════════════════
    print("\n[T-158-F confirm 闸门]")
    (PROJ / "del1.txt").write_text("d1\n", encoding="utf-8")
    expect_err(lambda: cf.api_fs_delete("p1", "del1.txt", None),
               codes=(400,), tag="confirm 缺失")
    expect_err(lambda: cf.api_fs_delete("p1", "del1.txt", False),
               codes=(400,), tag="confirm false")
    expect_err(lambda: cf.api_fs_delete("p1", "del1.txt", "true"),
               codes=(400,), tag="confirm 'true' 字符串")
    expect_err(lambda: cf.api_fs_delete("p1", "del1.txt", 1),
               codes=(400,), tag="confirm 1 (truthy 非布尔)")
    check("confirm 被拒后文件仍在", (PROJ / "del1.txt").exists())
    r = cf.api_fs_delete("p1", "del1.txt", True)
    check("confirm true → 删除成功", r["ok"] and not (PROJ / "del1.txt").exists())

    # ══ T-158-G 同名冲突 (X-6: 原内容逐字节未变) ═════════════════
    print("\n[T-158-G 同名冲突]")
    orig = (PROJ / "docs" / "REQ.md").read_bytes()
    expect_err(lambda: cf.api_fs_upload("p1", "docs", "REQ.md", b"OVERWRITE"),
               codes=(409,), tag="同名上传")
    check("原文件内容逐字节未变",
          (PROJ / "docs" / "REQ.md").read_bytes() == orig)
    expect_err(lambda: cf.api_fs_upload("p1", "", "subdir", b"x"),
               codes=(409,), tag="上传名撞目录")

    # ══ T-158-H 正常上传 (AC-158-1) ══════════════════════════════
    print("\n[T-158-H 正常上传]")
    payload = os.urandom(1234)
    r = cf.api_fs_upload("p1", "", "h1.bin", payload)
    check("根目录上传内容逐字节相等",
          r["ok"] and (PROJ / "h1.bin").read_bytes() == payload
          and r["path"] == "h1.bin" and r["size"] == 1234)
    r = cf.api_fs_upload("p1", "subdir", "h2.txt", b"nested\n")
    check("子目录上传", r["path"] == "subdir/h2.txt"
          and (PROJ / "subdir" / "h2.txt").read_bytes() == b"nested\n")
    r = cf.api_fs_upload("p1", "", "empty.txt", b"")
    check("空文件上传成功", r["ok"] and (PROJ / "empty.txt").stat().st_size == 0)
    # 上传后列表即时可见 (与只读面联动)
    names = [it["name"] for it in cf.api_fs_list("p1", "")["items"]]
    check("列表即时可见", "h1.bin" in names and "empty.txt" in names)

    # ══ T-158-I 零回显 (AC-G2/F1 口径) ═══════════════════════════
    print("\n[T-158-I 零回显]")
    echo_cases = []
    for fn, tag in [
        (lambda: cf.api_fs_upload("p1", "..", "x", b""), "upload 穿越"),
        (lambda: cf.api_fs_delete("p1", "subdir", True), "delete 目录"),
        (lambda: cf.api_fs_delete("p1", "keep.txt", None), "delete 无 confirm"),
        (lambda: cf.api_fs_upload("p1", "", "REQ.md", b"x"), "upload 409"),
    ]:
        try:
            fn()
            echo_cases.append((tag, ""))
        except cf.FsPathError as e:
            echo_cases.append((tag, e.msg))
    for tag, msg in echo_cases:
        bad = (str(BASE) in msg) or ("/etc" in msg) or ("Traceback" in msg) \
            or (str(PROJ) in msg) or ("/" in msg)
        check(f"零回显 [{tag}]", not bad, f"msg={msg!r}")

    # ══ T-158-J 只读面零回归 (AC-158-5) ══════════════════════════
    print("\n[T-158-J 只读面回归]")
    d = cf.api_fs_list("p1", "docs")
    check("api_fs_list 正常", any(i["name"] == "REQ.md" for i in d["items"]))
    try:
        cf._fs_resolve("p1", "docs/REQ.md")
        check("_fs_resolve 正常", True)
    except cf.FsPathError as e:
        check("_fs_resolve 正常", False, f"code={e.code}")

    # ══ T-158-K 上限常量防漂移 (MV-158-J 牙齿) ════════════════════
    print("\n[T-158-K 防漂移]")
    check("FS_UPLOAD_MAX_BYTES == console.UPLOAD_MAX_BYTES",
          cf.FS_UPLOAD_MAX_BYTES == console.UPLOAD_MAX_BYTES,
          f"{cf.FS_UPLOAD_MAX_BYTES} vs {console.UPLOAD_MAX_BYTES}")

    # ══ handle_fs_post 路由面 (fake handler) ═════════════════════
    print("\n[handle_fs_post 路由映射]")

    class FakeH:
        def __init__(self):
            self.code = None
            self.headers = {}
            self.wfile = io.BytesIO()

        def send_response(self, code):
            self.code = code

        def send_header(self, k, v):
            self.headers[k] = v

        def end_headers(self):
            pass

        def _write_paced(self, wf, body):
            wf.write(body)

    def post(p, payload, body_bytes=0):
        h = FakeH()
        cf.handle_fs_post(h, p, payload, body_bytes)
        return h.code, json.loads(h.wfile.getvalue() or b"{}")

    code, body = post("/api/fs/upload",
                      {"project": "p1", "path": "", "name": "rt.txt",
                       "data": "data:text/plain;base64," + b64(b"route")})
    check("路由 upload 200", code == 200 and body.get("ok") is True)
    code, body = post("/api/fs/upload",
                      {"project": "p1", "path": "", "name": "rt.txt",
                       "data": b64(b"dup")})
    check("路由 upload 409", code == 409 and body.get("error") == "conflict")
    code, body = post("/api/fs/upload", {"project": "p1", "data": ""})
    check("路由 upload 缺 data 400", code == 400)
    code, body = post("/api/fs/upload", {"project": "p1", "data": "!!!"},
                      body_bytes=29 * 1024 * 1024)
    check("路由 upload 请求体超限 413", code == 413)
    code, body = post("/api/fs/delete", {"project": "p1", "path": "rt.txt"})
    check("路由 delete 缺 confirm 400", code == 400
          and body.get("error") == "confirm required")
    check("缺 confirm 时文件仍在", (PROJ / "rt.txt").exists())
    code, body = post("/api/fs/delete", {"project": "p1", "path": "rt.txt",
                                         "confirm": True})
    check("路由 delete 200", code == 200 and body.get("ok") is True
          and not (PROJ / "rt.txt").exists())
    code, body = post("/api/fs/other", {})
    check("未知 fs 写路径 404", code == 404)
    code, body = post("/api/fs/upload", "not-a-dict")
    check("payload 非 dict 400", code == 400)

    # ══ 静态守护: 五层链复用零旁路 (红线证明) ════════════════════
    print("\n[静态守护 — _fs_resolve 唯一取路径入口]")
    src = Path("mw/console_files.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef,))}

    def calls_fs_resolve(fn):
        return sum(1 for n in ast.walk(fn)
                   if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Name)
                   and n.func.id == "_fs_resolve")

    check("api_fs_upload 调 _fs_resolve ≥2 次 (目录+最终路径双查)",
          calls_fs_resolve(funcs["api_fs_upload"]) >= 2,
          f"calls={calls_fs_resolve(funcs['api_fs_upload'])}")
    check("api_fs_delete 调 _fs_resolve ≥1 次",
          calls_fs_resolve(funcs["api_fs_delete"]) >= 1)

    def touches_ws(fn):
        return any((isinstance(n, ast.Name) and n.id == "WS_PROJECTS")
                   or (isinstance(n, ast.Attribute)
                       and n.attr == "WS_PROJECTS")
                   for n in ast.walk(fn))

    for fn_name in ("api_fs_upload", "api_fs_delete", "handle_fs_post",
                    "_sanitize_fs_name", "_decode_upload_b64", "_rel_out"):
        check(f"{fn_name} 不触碰 WS_PROJECTS (零新增拼接通道)",
              not touches_ws(funcs[fn_name]))
    # 写面不落第二条取路径通道: 写面函数体内不得出现 normpath+join 之外的
    # 独立根拼接 —— join 只允许基于已锚定的 dir_abs (审查口径: grep 命中数)
    check("WS_PROJECTS grep 命中数 = 5 (基线不增加)",
          src.count("WS_PROJECTS") == 5, f"count={src.count('WS_PROJECTS')}")
    # confirm 严格布尔 (is True) 静态钉住
    check("delete 用 `is not True` 严格判定",
          "confirm is not True" in ast.get_source_segment(src, funcs["api_fs_delete"]))
    # O_EXCL 独占落盘静态钉住 (MV-158-C 语义)
    check("落盘用 O_EXCL", "os.O_EXCL" in ast.get_source_segment(src, funcs["api_fs_upload"]))
    # 只删文件不删目录静态钉住 (MV-158-E 语义)
    check("delete 有 isfile 判定",
          "os.path.isfile" in ast.get_source_segment(src, funcs["api_fs_delete"]))
    # console.py do_POST: /api/fs/ 分支在 /api/upload 之前 (X-2 锚点)
    csrc = Path("mw/console.py").read_text(encoding="utf-8")
    i_fs = csrc.find('if p.startswith("/api/fs/"):')
    i_up = csrc.find('if p == "/api/upload":')
    i_img = csrc.find('if p == "/api/attachments/image":')
    check("do_POST: fs 分支在 upload 之前 (X-2)", -1 < i_fs < i_up)
    check("do_POST: fs 分支在 attachments/image 之前 (合并顺序 fs→upload→image)",
          -1 < i_fs < i_img)
    check("do_POST fs 分支走 handle_fs_post",
          "handle_fs_post" in csrc[i_fs:i_up])

    # ── 汇总 ──
    print(f"\n== issue158 fs-write 单测: {len(PASS)} passed, {len(FAIL)} failed ==")
    if FAIL:
        for name, detail in FAIL:
            print(f"  FAILED: {name}  {detail}")
        sys.exit(1)
