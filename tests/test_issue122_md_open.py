"""issue #122 守护: 浏览器打开 .md 服务端渲染 + 预览四形态换行 CSS。

覆盖 (PLAN-E-20261026 §2.3 / REQUIREMENTS §2.2-2.4):
  1. 渲染器安全: <script> 被转义不存活; javascript:/data: 链接不生成 <a>;
     https 链接正常; 图片不生成 <img> 只留 alt。
  2. 渲染器功能: 标题/列表/围栏代码块/表格/hr/粗斜体 各一条断言。
  3. open 头: /api/fs/file?mode=open (.md) → text/html + sandbox CSP + nosniff;
     (.txt) → 仍 text/plain; (.md embed/download) → 仍 text/plain 零改动。
  4. 超限降级: >1 MiB .md → 回退 text/plain 流式。
  5. CSS 守护: index.css 含 .wrap-anywhere; .md-body pre 不再 overflow-auto;
     FilePreview.jsx 纯文本 <pre> 挂 wrap-anywhere; 四处无 truncate 红线。

跑法同 test_fs_api.py: pytest 入口 subprocess 跑 __main__ 真测试,
避免改 WS_PROJECTS 污染兄弟测试。
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve()
IS_PYTEST = __name__ != '__main__'


def _run_subprocess():
    r = subprocess.run(
        [sys.executable, str(HERE)],
        capture_output=True, text=True, cwd=str(ROOT),
        env={**os.environ, 'MW_LANG': 'zh-cn', 'LANG': 'en_US.UTF-8'},
        timeout=120,
    )
    print(r.stdout)
    if r.returncode != 0:
        print('--- STDERR ---', file=sys.stderr)
        print(r.stderr, file=sys.stderr)
    return r.returncode


if IS_PYTEST:
    def test_issue122_md_open_subprocess():
        rc = _run_subprocess()
        assert rc == 0, f"issue122 守护 subprocess 退出码 {rc} (非 0)"


# ──────────────────────────────────────────────────────────────────
# 真测试段 (script-style)
# ──────────────────────────────────────────────────────────────────
if __name__ == '__main__':  # noqa: F811
    import shutil as _shutil
    import tempfile as _tempfile
    from urllib.parse import parse_qs as _parse_qs

    sys.path.insert(0, str(ROOT))

    PASS, FAIL = [], []

    def check(name, cond, detail=""):
        (PASS if cond else FAIL).append((name, detail))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
              + (f"  {detail}" if not cond and detail else ""))

    # 隔离沙箱
    BASE = Path(_tempfile.mkdtemp(prefix="issue122_"))
    os.environ["MW_ALLOWED_WS_PREFIX"] = str(BASE)
    PROJ = BASE / "projects" / "simpledot-agent-studio"
    PROJ.mkdir(parents=True)

    import mw.console_files as cf
    cf.WS_PROJECTS = str(BASE / "projects")  # 双保险 (env 在 import 前已设)

    LONG = "A" * 5000  # 无空格超长 token
    MD_SAFE = (
        "# 标题一\n\n"
        "段落 **粗** 与 *斜* 与 `inline code` 与 "
        "[安全链接](https://ok.com/a) 与 "
        "[坏链](javascript:alert(1)) 与 [数据](data:text/html,<b>x</b>)\n\n"
        "- 列表甲\n- 列表乙\n\n"
        "1. 有序一\n2. 有序二\n\n"
        "```\ncode fence <script>alert(1)</script>\n" + LONG + "\n```\n\n"
        "| 列A | 列B |\n|---|---|\n| " + LONG[:80] + " | 短 |\n\n"
        "---\n\n"
        "![远程图](https://evil.example/x.png)\n"
    )
    (PROJ / "doc.md").write_text(MD_SAFE, encoding="utf-8")
    (PROJ / "plain.txt").write_text(LONG, encoding="utf-8")
    big = PROJ / "huge.md"
    with open(big, "w", encoding="utf-8") as f:
        f.write("# big\n\n")
        f.write(("x" * 1000 + "\n") * 1100)  # > 1 MiB

    print("\n[渲染器安全]")
    from mw.md_render import render_markdown_safe
    out = render_markdown_safe(MD_SAFE)
    check("script 转义存活", "&lt;script&gt;" in out)
    check("script 不存活", "<script" not in out)
    check("javascript: 不生成 a", 'href="javascript:' not in out.lower())
    check("data: 不生成 a", 'href="data:' not in out.lower())
    check("https 链接正常", '<a href="https://ok.com/a">' in out)
    check("图片不生成 img", "<img" not in out)
    check("图片留 alt", "远程图" in out)

    print("\n[渲染器功能]")
    check("h1", "<h1>标题一</h1>" in out)
    check("strong", "<strong>粗</strong>" in out)
    check("em", "<em>斜</em>" in out)
    check("inline code", "<code>inline code</code>" in out)
    check("ul", "<ul><li>列表甲</li><li>列表乙</li></ul>" in out)
    check("ol", "<ol><li>有序一</li><li>有序二</li></ol>" in out)
    check("fence pre", "<pre><code>" in out and LONG in out)
    check("table thead", "<thead><tr><th>列A</th><th>列B</th></tr></thead>" in out)
    check("table tbody td", "<td>" in out and "短" in out)
    check("hr", "<hr>" in out)
    check("包裹 div.md", out.startswith('<div class="md">'))
    # 变异反证: 空输入不炸
    check("空输入", render_markdown_safe("") == '<div class="md"></div>')

    print("\n[I-1 回归 — NUL 占位符越界]")
    # 对抗输入: 字面量 \x00<数字>\x00 曾被还原正则命中 → IndexError → 500
    try:
        evil = render_markdown_safe("a `x` b\x0042\x00 c")
        ok_evil = True
    except IndexError:
        evil = ""
        ok_evil = False
    check("I-1 对抗输入不抛 IndexError", ok_evil)
    check("I-1 合法 code 仍还原", "<code>x</code>" in evil)
    check("I-1 无 <code>42</code> 伪造", "<code>42</code>" not in evil)
    # 多段 code 正常还原 (防护不误伤合法索引)
    multi = render_markdown_safe("`a` and `b` and `c`")
    check("I-1 多段 code 全还原",
          multi.count("<code>") == 3 and "</code>" in multi)
    # 越界占位符原样保留 (NUL 字符仍在输出, 无标签注入)
    fake = render_markdown_safe("\x007\x00")
    check("I-1 伪造占位不生成 code", "<code>" not in fake)

    print("\n[open 头 — .md 渲染]")

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
            wfile.write(data)

    def get_file(rel, mode):
        h = MockHandler()
        h.wfile = h
        cf.handle_fs_get(h, "/api/fs/file",
                        _parse_qs(f"project=simpledot-agent-studio"
                                 f"&path={rel}&mode={mode}"))
        return h, dict(h.headers)

    h, hd = get_file("doc.md", "open")
    check("md open CT=text/html",
          hd.get("Content-Type") == "text/html; charset=utf-8",
          str(hd.get("Content-Type")))
    check("md open sandbox CSP", "sandbox" in hd.get("Content-Security-Policy", ""))
    check("md open nosniff", hd.get("X-Content-Type-Options") == "nosniff")
    check("md open inline", hd.get("Content-Disposition", "").startswith("inline"))
    body = h.body.decode("utf-8")
    check("md open 渲染标题", "<h1>标题一</h1>" in body)
    check("md open 无 script", "<script" not in body)
    check("md open 无 <script> 转义丢失", "&lt;script&gt;" in body)
    check("md open Content-Length 一致",
          int(hd["Content-Length"]) == len(h.body))

    print("\n[open 头 — 其余 text 类零改动]")
    h, hd = get_file("plain.txt", "open")
    check("txt open CT=text/plain",
          hd.get("Content-Type", "").startswith("text/plain"))
    check("txt open 无 CSP", "Content-Security-Policy" not in hd)
    check("txt open 原文完整", h.body.decode() == LONG)

    h, hd = get_file("doc.md", "embed")
    check("md embed 仍 text/plain",
          hd.get("Content-Type", "").startswith("text/plain"))
    check("md embed 无 CSP", "Content-Security-Policy" not in hd)

    h, hd = get_file("doc.md", "download")
    check("md download 仍 attachment",
          hd.get("Content-Disposition", "").startswith("attachment"))
    check("md download 仍 text/plain",
          hd.get("Content-Type", "").startswith("text/plain"))

    print("\n[超限降级]")
    h, hd = get_file("huge.md", "open")
    check("huge.md open 回退 text/plain",
          hd.get("Content-Type", "").startswith("text/plain"),
          str(hd.get("Content-Type")))
    check("huge.md 流式内容非空", len(h.body) > 1024 * 1024)

    print("\n[S-4 注入反证 — 模板占位符双向串位]")
    # issue#127 S-4: 改前是链式 .replace(__TITLE__).replace(__BODY__), 有两个
    # 方向的二次污染。修法改单趟 re.sub 回调 (替换进去的值不再被扫描)。
    # 本段锁死双向: 任一方向被"二次展开"都红。
    #
    # 方向1: 文件名含 "__BODY__" 字面量
    #   改前: TITLE 替换把 __BODY__ 带进 <title> 区 → BODY 替换二次展开
    #          → body 内容注入 <title>。
    name_a = "x__BODY__y.md"
    (PROJ / name_a).write_text("# 正文甲\n", encoding="utf-8")
    page_a = cf._render_md_open_page(str(PROJ / name_a), name_a)
    check("S-4 方向1 渲染成功", page_a is not None)
    if page_a:
        pa = page_a.decode("utf-8")
        title_a = pa.split("<title>")[1].split("</title>")[0] \
            if "<title>" in pa else ""
        check("S-4 方向1 title 保留字面 __BODY__",
              "__BODY__" in title_a, f"title={title_a!r}")
        check("S-4 方向1 title 未被 body 内容污染",
              "正文甲" not in title_a, f"title={title_a!r}")
        check("S-4 方向1 正文区含真实内容",
              "正文甲" in pa)
        check("S-4 方向1 无残留占位符",
              "__TITLE__" not in pa and pa.count("__BODY__") == 1, pa[:400])
    # 方向2: 正文含 "__TITLE__" 字面量
    name_b = "s4name.md"
    raw_b = "see __TITLE__ here"
    (PROJ / name_b).write_text(raw_b + "\n", encoding="utf-8")
    page_b = cf._render_md_open_page(str(PROJ / name_b), name_b)
    check("S-4 方向2 渲染成功", page_b is not None)
    if page_b:
        pb = page_b.decode("utf-8")
        title_b = pb.split("<title>")[1].split("</title>")[0] \
            if "<title>" in pb else ""
        check("S-4 方向2 title 是真实文件名",
              title_b == name_b, f"title={title_b!r}")
        check("S-4 方向2 正文保留字面 __TITLE__",
              "__TITLE__" in pb, pb[:400])
        # 只在 <body> 正文区判串位 (title 区本来就该含文件名)
        body_b = pb.split("<body>")[1].split("</body>")[0] \
            if "<body>" in pb and "</body>" in pb else ""
        check("S-4 方向2 正文区保留字面 __TITLE__",
              "__TITLE__" in body_b, body_b[:400])
        check("S-4 方向2 正文区未被文件名污染",
              name_b not in body_b, body_b[:400])
        check("S-4 方向2 无残留占位符",
              "__BODY__" not in pb and pb.count("__TITLE__") == 1, pb[:400])
    # 单趟替换的结构性保证: 模板本身只含两个占位符, 替换后均已消费
    check("S-4 模板占位符恰好两个",
          cf._MD_PAGE_TMPL.count("__TITLE__") == 1
          and cf._MD_PAGE_TMPL.count("__BODY__") == 1)

    print("\n[CSS 守护 — 四形态换行红线]")
    css = (ROOT / "ui" / "src" / "index.css").read_text(encoding="utf-8")
    check("F-a .wrap-anywhere 存在", ".wrap-anywhere" in css
          and "overflow-wrap: anywhere" in css)
    # .md-body pre 段不再含 overflow-auto
    import re as _re
    pre_block = _re.search(r"\.md-body pre \{[^}]*\}", css)
    check("F-b .md-body pre 存在", pre_block is not None)
    check("F-b pre 无 overflow-auto",
          pre_block and "overflow-auto" not in pre_block.group(0))
    check("F-b pre pre-wrap", pre_block and "white-space: pre-wrap"
          in pre_block.group(0))
    check("F-c code anywhere",
          ".md-body code" in css and "overflow-wrap: anywhere" in css)
    check("F-d table fixed", "table-layout: fixed" in css)
    # 红线: 预览相关规则里禁止 truncate/ellipsis
    check("红线 无 truncate/ellipsis",
          "text-overflow" not in css and "truncate" not in css)
    fp = (ROOT / "ui" / "src" / "components" / "filebrowser" / "FilePreview.jsx")
    fp_src = fp.read_text(encoding="utf-8")
    check("F-a FilePreview pre 挂 wrap-anywhere",
          "wrap-anywhere" in fp_src)

    _shutil.rmtree(BASE, ignore_errors=True)

    total = len(PASS) + len(FAIL)
    print(f"\n{'='*60}")
    print(f"  issue #122 守护: {len(PASS)}/{total} PASS, {len(FAIL)} FAIL")
    if FAIL:
        print("FAIL:")
        for n, d in FAIL:
            print(f"  - {n}  {d}")
        sys.exit(1)
