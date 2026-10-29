# mw/md_render.py — escape-first markdown 子集渲染器 (issue #122 open 模式专用)
#
# stdlib-only (PLAN-E-20261026 §2.2 决策 D3: 镜像内无 Python markdown 库,
# 不新增运行时依赖)。
#
# 安全不变量 (审查重点):
#   1. 输入先整体 html.escape(quote=True), 原始 HTML/脚本不可能存活;
#   2. 之后仅按行级规则把 markdown 语法构造为白名单标签
#      (h1-h6 / p / ul / ol / li / pre / code / strong / em / a / table / hr);
#   3. 链接仅允许 http/https/mailto/# 前缀 (大小写不敏感), 其余
#      (javascript:/data:/vbscript:) 不生成 <a>, 保留转义后原文;
#   4. 图片 ![alt](url) 不生成 <img> (CSP img-src 仅 data:, 远程图必挂),
#      渲染为转义后的 alt 文本。
#
# 支持子集: ATX 标题 h1-h6 / 围栏代码块 ``` / 行内 `code` /
# 粗体 **x** 斜体 *x* / 无序-有序列表 / 链接 / 管道表格 / hr / 段落。
# 逐行状态机: fence 内不解析任何行内语法; 表格按分隔行 |---| 识别。
# 渲染产物为 <div class="md">…</div> 片段, 由调用方包完整 HTML 文档。
import html
import re

# 链接协议白名单: 仅 http/https/mailto/锚点 (大小写不敏感)
_SAFE_URL_RE = re.compile(r"^(?:https?://|mailto:|#)", re.IGNORECASE)

# 行内 code: `x` (单行内, 不含反引号)
_CODE_RE = re.compile(r"`([^`\n]+)`")
# 图片: ![alt](url) — 只保留 alt
_IMG_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
# 链接: [text](url)
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
# 粗体 / 斜体 (粗体先于斜体处理, 避免 * 互相吞)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
# ATX 标题
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# 分隔线 (独立成行的三连字符: 连字符 / 星号 / 下划线)
_HR_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
# 无序 / 有序列表项
_UL_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
# 表格分隔行: 仅由 | : - 空格组成, 且同时含 | 与 - (如 |---|:--:|)
_TSEP_RE = re.compile(r"^\|?[\s:|-]+\|?$")


def _is_table_sep(s: str) -> bool:
    """管道表格的分隔行判定 (必须含 | 与 -, 排除裸 --- 误判成表格)。"""
    return bool(_TSEP_RE.match(s)) and "|" in s and "-" in s


def _cells(row: str) -> list:
    """拆一行管道表格: 去首尾 |, 按 | 切, 逐格 strip。"""
    s = row.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _inline(s: str) -> str:
    """行内语法渲染 (输入必须是已转义文本; 输出只含白名单标签)。

    顺序: code 占位保护 → 图片降级 alt → 链接白名单 → 粗体 → 斜体 → 还原 code。
    code 段内容不参与后续 emphasis/链接 解析, 防语义串扰。
    """
    codes = []

    def _stash(m):
        codes.append(m.group(1))
        return "\x00%d\x00" % (len(codes) - 1)

    s = _CODE_RE.sub(_stash, s)
    # 图片 → 转义 alt 文本 (不生成 <img>, 见模块头不变量 4)
    s = _IMG_RE.sub(lambda m: m.group(1), s)

    def _link(m):
        text, url = m.group(1), m.group(2)
        if _SAFE_URL_RE.match(url.strip()):
            # url 已整体转义 (quote=True), " 与 < 无法逃逸出 href 属性
            return '<a href="%s">%s</a>' % (url, text)
        return m.group(0)  # 危险协议: 原样保留转义文本, 不生成 <a>

    s = _LINK_RE.sub(_link, s)
    s = _BOLD_RE.sub(r"<strong>\1</strong>", s)
    s = _ITALIC_RE.sub(r"<em>\1</em>", s)
    def _restore(m):
        # I-1 防护: 输入可含字面量 \x00<数字>\x00 (NUL 不被 html.escape 转义),
        # 越界索引 codes 会抛 IndexError → 渲染 500。索引须在 [0, len(codes))
        # 内才还原 <code>; 伪造占位符原样保留 (转义后文本, 无逃逸风险)。
        idx = int(m.group(1))
        if 0 <= idx < len(codes):
            return "<code>%s</code>" % codes[idx]
        return m.group(0)

    s = re.sub(r"\x00(\d+)\x00", _restore, s)
    return s


def _table(header: list, rows: list) -> str:
    """管道表格 → thead/tbody (单元格内容走行内渲染)。"""
    out = ["<table><thead><tr>"]
    for c in header:
        out.append("<th>%s</th>" % _inline(c))
    out.append("</tr></thead><tbody>")
    for r in rows:
        out.append("<tr>")
        for c in r:
            out.append("<td>%s</td>" % _inline(c))
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def render_markdown_safe(text: str) -> str:
    """escape-first 的 markdown 子集渲染器 (issue #122 open 模式专用)。

    安全不变量见模块头。返回 <div class="md">…</div> 片段。
    """
    esc = html.escape(text or "", quote=True)  # 不变量 1: 先全量转义
    lines = esc.split("\n")
    out = []
    para = []

    def flush_para():
        # 段落: 连续普通行以空格拼接 (与 markdown 软换行口径一致)
        if para:
            out.append("<p>%s</p>" % _inline(" ".join(para)))
            para.clear()

    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        # 围栏代码块: fence 内零解析 (不变量 2)
        if stripped.startswith("```"):
            flush_para()
            buf = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # 跳过闭合围栏 (未闭合则到 EOF, 宽容处理)
            out.append("<pre><code>%s</code></pre>" % "\n".join(buf))
            continue
        if not stripped:
            flush_para()
            i += 1
            continue
        # 表格: 当前行 | 开头 且 下一行是分隔行
        if (stripped.startswith("|") and i + 1 < n
                and _is_table_sep(lines[i + 1].strip())):
            flush_para()
            header = _cells(stripped)
            rows = []
            j = i + 2
            while j < n and lines[j].strip().startswith("|"):
                rows.append(_cells(lines[j].strip()))
                j += 1
            out.append(_table(header, rows))
            i = j
            continue
        m = _HEAD_RE.match(stripped)
        if m:
            flush_para()
            lvl = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (lvl, _inline(m.group(2)), lvl))
            i += 1
            continue
        if _HR_RE.match(stripped):
            flush_para()
            out.append("<hr>")
            i += 1
            continue
        lm = _UL_RE.match(stripped)
        if lm:
            flush_para()
            items = [lm.group(1)]
            i += 1
            while i < n:
                m2 = _UL_RE.match(lines[i])
                if not m2:
                    break
                items.append(m2.group(1))
                i += 1
            out.append("<ul>%s</ul>"
                      % "".join("<li>%s</li>" % _inline(x) for x in items))
            continue
        lm = _OL_RE.match(stripped)
        if lm:
            flush_para()
            items = [lm.group(1)]
            i += 1
            while i < n:
                m2 = _OL_RE.match(lines[i])
                if not m2:
                    break
                items.append(m2.group(1))
                i += 1
            out.append("<ol>%s</ol>"
                      % "".join("<li>%s</li>" % _inline(x) for x in items))
            continue
        para.append(stripped)
        i += 1
    flush_para()
    return '<div class="md">%s</div>' % "".join(out)
