"""tests/test_issue116_ui_conflict_consumption.py — issue #116 守护用例。

issue #104 期望 2 的「操作员对话框回显」这一半: 后端 conflict /
answered_by 早已产出, 但两个控制台 UI 面 (React ui/ 与 fallback
mw/console_ui.py) 均 0 消费 —— 后到答复在界面上与盲注无异。

本卡 (#116) 的守护分两层:

  S1  静态断言 (issue 建议 4 的「UI 源码含 conflict 消费点」):
      S1a React 面 submitAnswer 接住返回值并消费 r.conflict
      S1b React 面步骤行渲染 answered_by / answered_at_hhmm
      S1c fallback 面 submitAnswer 判 d.conflict 写提示位
      S1d fallback 面弹窗渲染 answered_by 行
  S2  后端注入断言:
      S2a run 详情端点 (api_team_work_get) 每张卡带 answered_by/hhmm
  S3  i18n 断言:
      S3a 新键在 mw/i18n en/zh-cn 双侧齐全 (G1 同构)
      S3b React locales en/zh-cn tboard 双侧齐全
      S3c fallback 模板哨兵全部可被两语表替换 (无残留 ⟪⟫)

—— issue #117 升级 (S1b / S1d) ——————————————————————————————————

C 批 s6 (TEST-REPORT-BATCH-20261025C, D-2) 实证: S1b/S1d 原本只做
**字符串存在性**断言 (`"step.answered_by" in src`), 守得住「整块被删」,
守不住「渲染条件被改坏」:

    {step.answered_by && (   →   {false && (      # 角标永不渲染, 但
                                                    # 字符串还在 ⇒ 假绿

本卡把 S1b/S1d 升级为**剥注释后的渲染条件结构断言**: 定位角标锚点,
再沿花括号配对找到**结构上包住它**的渲染守卫, 断言守卫条件表达式
引用的正是 answered_by 字段 (而不是注释里的一句、也不是别的字段)。

并配**变异反证** (S1m): 把渲染条件改成 false 常量 ⇒ 同一套结构断言
必须转红。变异只作用于内存中的源码副本, **不碰产品代码**。

跑法: pytest tests/test_issue116_ui_conflict_consumption.py -v
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ──────────────────────────────────────────────────────────────────
# issue #117: 轻量 JS/JSX 掩码器 + 渲染守卫结构提取
# ──────────────────────────────────────────────────────────────────
#
# 为什么不用正则直接在源码里搜:
#   注释里写一句 `step.answered_by && (` 就能骗过字符串断言, 而渲染
#   条件真正被改成 `{false && (` 时字符串又还在 —— 两头都假绿。
#
# 为什么不用完整 JS 解析器: 仓库不引 babel/esbuild 运行时依赖, 守护
#   用例必须纯 stdlib + 仓库既有依赖才能在任意 CI 环境跑。
#
# 做法: 词法级扫描, 把 注释 / 字符串 / 模板文本 / 正则字面量 / JSX
#   文本 全部掩成空格(换行保留), **只保留 JS 表达式里的花括号** ——
#   花括号配对正是「渲染条件在结构上包住角标」的定位依据。掩码结果与
#   原文严格 1:1 等长, 故所有 span 都能直接回原文取真实文本。

# 正则字面量只能出现在这些关键字之后 (否则 `/` 是除号)
_REGEX_PRE_KEYWORDS = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "do", "else", "case", "yield", "await", "throw",
}
# 出现在其后 ⇒ `/` 是除号而非正则开头
_VALUE_END_CHARS = set("_$)]}\"'")
# 渲染守卫形态: `条件 &&` —— 条件是一个 (可带空白的) 点分成员表达式
_GUARD_RE = re.compile(
    r"^(?P<cond>[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)\s*&&")
# 角标锚点: t('tboard.answered_badge', …)
_BADGE_CALL_RE = re.compile(r"t\(\s*(['\"])tboard\.answered_badge\1\s*,")


def _skip_string(src: str, i: int, n: int) -> int | None:
    """跳过一个引号字符串 (含转义); 同行找不到配对引号 → None。

    None 表示这其实是 JSX 文本里的撇号 (如 `don't`), 按普通字符处理 ——
    否则会把它误当字符串开头, 把后面整段代码都抹平。
    """
    quote = src[i]
    j = i + 1
    while j < n:
        ch = src[j]
        if ch == "\\":
            j += 2
            continue
        if ch == quote:
            return j + 1
        if ch == "\n":
            return None
        j += 1
    return None


def _skip_regex(src: str, i: int, n: int) -> int:
    """跳过一个正则字面量 (含字符类与转义)。"""
    j = i + 1
    in_class = False
    while j < n:
        ch = src[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "\n":
            return j
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            j += 1
            while j < n and src[j].isalpha():
                j += 1
            return j
        j += 1
    return n


def _mask_js(src: str, mode: str = "all") -> str:
    """轻量 JS/JSX 掩码器。

    mode="all":     注释 + 字符串 + 模板文本 + 正则字面量 → 空格
    mode="comments": 只掩注释 (字符串等保留, 用于定位字面量锚点)

    返回值与 src 严格等长 (逐字符 1:1), 换行一律保留。
    代码区的花括号一律保留 —— 它们是结构帧的锚点。
    """
    n = len(src)
    out = list(src)
    mask_literals = (mode == "all")

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    # 栈帧: [kind, depth, is_interp]; kind ∈ code/tmpl
    stack: list[list] = [["code", 0, False]]
    i = 0
    prev_sig = ""   # 上一个有效字符
    prev_word = ""
    while i < n:
        frame = stack[-1]
        kind = frame[0]
        c = src[i]

        if kind == "code":
            # 注释
            if c == "/" and i + 1 < n and src[i + 1] == "/":
                j = i
                while j < n and src[j] != "\n":
                    j += 1
                blank(i, j)
                i = j
                continue
            if c == "/" and i + 1 < n and src[i + 1] == "*":
                j = i + 2
                while j < n and not (src[j] == "*" and j + 1 < n and src[j + 1] == "/"):
                    j += 1
                j = min(n, j + 2)
                blank(i, j)
                i = j
                continue
            # JSX 自闭合 `/>` 与闭合标签 `</div>`: 斜杠不是正则开头
            if c == "/" and i + 1 < n and src[i + 1] == ">":
                i += 2
                prev_sig = ">"
                prev_word = ""
                continue
            if c == "<" and i + 1 < n and src[i + 1] == "/":
                j = src.find(">", i)
                j = n if j == -1 else j + 1
                i = j
                prev_sig = ">"
                prev_word = ""
                continue
            # 字符串
            if c in "\"'":
                j = _skip_string(src, i, n)
                if j is not None:
                    if mask_literals:
                        blank(i, j)
                    i = j
                else:
                    i += 1  # JSX 文本里的撇号 → 普通字符
                prev_sig = c
                prev_word = ""
                continue
            # 模板字面量
            if c == "`":
                if mask_literals:
                    blank(i, i + 1)
                stack.append(["tmpl", 0, False])
                i += 1
                prev_sig = "`"
                prev_word = ""
                continue
            # 正则字面量
            if c == "/":
                allowed = ((not prev_sig)
                           or (prev_word in _REGEX_PRE_KEYWORDS)
                           or (not (prev_sig.isalnum() or prev_sig in _VALUE_END_CHARS)))
                if allowed:
                    j = _skip_regex(src, i, n)
                    if mask_literals:
                        blank(i, j)
                    i = j
                    prev_sig = "/"
                    prev_word = ""
                    continue
                prev_sig = "/"
                prev_word = ""
                i += 1
                continue
            # 花括号: 代码区一律保留 (结构帧锚点)
            if c == "{":
                frame[1] += 1
                prev_sig = c
                prev_word = ""
                i += 1
                continue
            if c == "}":
                if frame[1] > 0:
                    frame[1] -= 1
                elif frame[2] and len(stack) > 1:
                    stack.pop()  # 模板插值结束 → 回到模板文本
                prev_sig = c
                prev_word = ""
                i += 1
                continue
            if not c.isspace():
                if c.isalnum() or c in "_$":
                    prev_word = (prev_word + c
                                 if (prev_sig.isalnum() or prev_sig in "_$") else c)
                else:
                    prev_word = ""
                prev_sig = c
            i += 1
            continue

        # kind == "tmpl": 模板文本
        if c == "\\":
            if mask_literals:
                blank(i, min(i + 2, n))
            i += 2
            continue
        if c == "`":
            if mask_literals:
                blank(i, i + 1)
            stack.pop()
            i += 1
            prev_sig = "`"
            prev_word = ""
            continue
        if c == "$" and i + 1 < n and src[i + 1] == "{":
            # 插值的 `{` 与其配对 `}` 一起构成结构帧 ⇒ 两者都保留
            stack.append(["code", 0, True])
            i += 2
            prev_sig = "{"
            prev_word = ""
            continue
        if mask_literals:
            blank(i, i + 1)
        i += 1
    return "".join(out)


def _brace_pairs(masked: str) -> list[tuple[int, int]]:
    """掩码视图里的花括号配对 (open_idx, close_idx)。"""
    stack: list[int] = []
    pairs: list[tuple[int, int]] = []
    for idx, ch in enumerate(masked):
        if ch == "{":
            stack.append(idx)
        elif ch == "}" and stack:
            pairs.append((stack.pop(), idx))
    return pairs


def _enclosing_frames(masked: str, idx: int) -> list[tuple[int, int]]:
    """包含 idx 的全部花括号帧, 由内到外排序。"""
    frames = [p for p in _brace_pairs(masked) if p[0] < idx < p[1]]
    frames.sort(key=lambda p: p[1] - p[0])
    return frames


def _match_paren(masked: str, open_idx: int) -> int | None:
    """从 `(` 处找配对的 `)` (只看掩码视图)。"""
    depth = 0
    for i in range(open_idx, len(masked)):
        ch = masked[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def _react_badge_guards(src: str) -> list[dict]:
    """React 面: 抽出每个 answered_badge 角标锚点所挂的渲染守卫。

    返回 [{anchor, condition, guard_span, args}], 其中
      anchor      锚点起点 (t( 调用处)
      condition   守卫条件表达式 (归一化去空白), None = 未找到守卫
      guard_span  守卫花括号帧 span
      args        t() 调用的实参文本 (取自原文)
    """
    masked_comments = _mask_js(src, mode="comments")
    masked = _mask_js(src, mode="all")
    out = []
    for m in _BADGE_CALL_RE.finditer(masked_comments):
        anchor = m.start()
        # 锚点必须落在代码区: 掩码视图里该字符未被抹平 (即不在字符串里)
        if masked[anchor] != "t":
            continue
        # `t` 不能是标识符的一部分 (排除 foo_t(...) 之类的误命中)
        if anchor and (src[anchor - 1].isalnum() or src[anchor - 1] in "_$"):
            continue
        paren_open = masked.find("(", anchor)
        paren_close = _match_paren(masked, paren_open) if paren_open != -1 else None
        args = src[paren_open + 1:paren_close] if paren_close else ""
        condition = None
        guard_span = None
        cond_span = None
        for (o, c) in _enclosing_frames(masked, anchor):
            # 守卫形态在掩码视图上匹配: 注释/字符串已抹平, 不会误命中
            gm = _GUARD_RE.match(masked[o + 1:c])
            if gm:
                condition = re.sub(r"\s+", "", gm.group("cond"))
                guard_span = (o, c)
                # 条件表达式在原文中的 span (掩码视图与原文 1:1 对齐)
                cond_span = (o + 1 + gm.start("cond"), o + 1 + gm.end("cond"))
                break
        out.append({
            "anchor": anchor,
            "condition": condition,
            "guard_span": guard_span,
            "cond_span": cond_span,
            "args": args,
        })
    return out


def _fallback_rendered_js(template: str, tables: dict) -> str:
    """把 i18n 哨兵整页替换后, 取出 fallback 页面的 <script> 块。

    必须**先替换再分析**: 模板原文里部分对象字面量的闭合 `}` 藏在哨兵
    值里 (如 `⟪CONSOLE_FALLBACK_SEM_DONE⟫` ⇒ 值以 `}` 结尾), 替换后
    花括号才收支平衡, 结构帧定位才可信。
    """
    body = template
    for key, value in tables.items():
        body = body.replace("⟪" + key.replace(".", "_").upper() + "⟫", value)
    blocks = re.findall(r"<script>(.*?)</script>", body, re.S)
    assert len(blocks) == 1, f"fallback 页面应有且仅有一个 <script> 块, 实得 {len(blocks)}"
    return blocks[0]


def _fallback_answered_guards(js: str) -> list[dict]:
    """fallback 面: 抽出弹窗 answered 行的三元守卫。

    定位方式: 模板字面量插值 `${…}` → 花括号配对取出插值表达式 → 在
    掩码视图上找三元的 `?` 与 `:` (字符串内容已抹平, 不会误判)。
    返回 [{condition, then_branch, else_branch, span}]。
    """
    masked = _mask_js(js, mode="all")
    out = []
    for m in re.finditer(r"\$\{", masked):
        open_idx = m.start() + 1
        depth = 0
        close = None
        for idx in range(open_idx, len(masked)):
            ch = masked[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    close = idx
                    break
        if close is None:
            continue
        expr_start = m.start() + 2
        expr = js[expr_start:close]
        if "answered_by" not in expr:
            continue
        # 三元的 ? 与 : 在掩码视图上定位 (字符串里的 ?/: 已被抹平)
        q = masked.find("?", open_idx, close)
        colon = masked.rfind(":", open_idx, close)
        if q == -1 or colon == -1 or colon < q:
            out.append({"condition": None, "then_branch": expr,
                        "else_branch": None, "span": (expr_start, close)})
            continue
        out.append({
            "condition": re.sub(r"\s+", "", expr[:q - expr_start]),
            "then_branch": expr[q - expr_start + 1:colon - expr_start],
            "else_branch": expr[colon - expr_start + 1:],
            "span": (expr_start, close),
        })
    return out


# ── S1b / S1d 结构断言的同一套判据 (与变异反证 S1m 共用) ────────────

def _assert_react_badge_guards(src: str) -> None:
    """React 面角标的渲染条件结构断言 (S1b / S1m 共用判据)。"""
    guards = _react_badge_guards(src)
    assert guards, "React 面找不到 tboard.answered_badge 角标锚点"
    for g in guards:
        assert g["condition"] is not None, (
            "React 面 answered_badge 角标未挂在任何 `条件 && (` 渲染守卫之下 "
            "(锚点 offset=%d)" % g["anchor"])
        assert g["condition"] == "step.answered_by", (
            "React 面角标渲染守卫条件应为 step.answered_by, 实得 %r "
            "(锚点 offset=%d)" % (g["condition"], g["anchor"]))
        # 角标实参必须取到答复人与答复时刻 (守卫成立 ≠ 文案取值正确)
        assert "step.answered_by" in g["args"], (
            "React 面角标实参未取 step.answered_by: %r" % g["args"])
        assert "step.answered_at_hhmm" in g["args"], (
            "React 面角标实参未取 step.answered_at_hhmm: %r" % g["args"])
    # 守卫必须是真值判断, 不能退化成常量
    for g in guards:
        assert g["condition"] not in ("false", "true"), (
            "React 面角标渲染守卫退化为常量 %r" % g["condition"])


def _assert_fallback_answered_guards(js: str) -> None:
    """fallback 面弹窗 answered 行的渲染条件结构断言 (S1d / S1m 共用判据)。"""
    guards = _fallback_answered_guards(js)
    assert guards, "fallback 弹窗找不到 answered_by 的渲染三元守卫"
    for g in guards:
        assert g["condition"] is not None, (
            "fallback 弹窗 answered 行不是三元渲染守卫: %r" % g["then_branch"])
        assert g["condition"] == "t.answered_by", (
            "fallback 弹窗 answered 行守卫条件应为 t.answered_by, 实得 %r"
            % g["condition"])
        # 真分支必须真的渲染答复人与答复时刻 (守卫成立 ≠ 文案取值正确)
        assert "esc(t.answered_by)" in g["then_branch"], (
            "fallback 弹窗 answered 行真分支未渲染答复人: %r" % g["then_branch"])
        assert "esc(t.answered_at_hhmm)" in g["then_branch"], (
            "fallback 弹窗 answered 行真分支未渲染答复时刻: %r" % g["then_branch"])
        assert g["else_branch"].strip() == "''", (
            "fallback 弹窗 answered 行假分支应为空串, 实得 %r" % g["else_branch"])


# ── S1: 静态断言 —— 两个 UI 面必须消费 conflict / answered_by ──────

def test_s1a_react_submit_answer_consumes_conflict():
    src = (ROOT / "ui" / "src" / "views" / "TasksBoardView.jsx").read_text(
        encoding="utf-8")
    # submitAnswer 必须接住 api.resume 的返回值 (旧版 `await api.resume(...)`
    # 直接丢弃), 且消费 .conflict —— 字面匹配, 不用正则元字符 (r?.conflict
    # 里 ? 与 . 会被当量词, 变异检验假绿, 故转义)
    assert re.search(r"const\s+r\s*=\s*await\s+api\.resume\(", src)
    assert "r?.conflict" in src or "r.conflict" in src, \
        "React 面未消费 resume 返回的 conflict 字段"
    # 冲突走警示文案, 不再一律成功 toast
    assert "reply_conflict" in src, \
        "React 面冲突未走 tboard.reply_conflict 警示文案"
    # 消费点必须挂在接住的返回值上 (裸 conflict / 别的对象不算)
    assert re.search(r"r\s*\?\.\s*conflict", src), \
        "React 面 conflict 消费点未挂在 resume 返回值 r 上"


def test_s1b_react_step_row_renders_answered_by():
    """React 面步骤行: 角标必须**结构性地**挂在 step.answered_by 真值守卫下。

    issue #117: 原实现只做 `"step.answered_by" in src` 字符串存在性断言,
    把渲染条件改成 `{false && (` 也不转红 (C 批 s6 D-2 实证)。本实现
    剥注释/剥字面量后沿花括号配对定位角标的渲染守卫, 断言条件表达式
    引用的正是 answered_by。
    """
    src = (ROOT / "ui" / "src" / "views" / "TasksBoardView.jsx").read_text(
        encoding="utf-8")
    _assert_react_badge_guards(src)
    # 掩码器自检: 与原文等长 + 花括号收支平衡 (否则结构定位不可信)
    masked = _mask_js(src, mode="all")
    assert len(masked) == len(src), "掩码器破坏了源码偏移对齐"
    assert masked.count("{") == masked.count("}"), (
        "掩码器花括号不配对 (open=%d close=%d), 结构断言不可信"
        % (masked.count("{"), masked.count("}")))


def test_s1c_fallback_submit_answer_consumes_conflict():
    src = (ROOT / "mw" / "console_ui.py").read_text(encoding="utf-8")
    # 必须在 close() 之前判 d.conflict 并写进 dlgErr 提示位
    m = re.search(r"if\(d\.conflict\)\{", src)
    assert m, "fallback 面 submitAnswer 未判 d.conflict"
    seg = src[m.end(): m.end() + 300]
    assert "dlgErr" in seg, "fallback 面冲突未写进 dlgErr 提示位"
    # 冲突分支必须在 dlg.close() 之前 (否则提示还没看见就被关掉)
    close_at = src.find("document.getElementById('dlg').close()", m.end())
    dlgerr_at = seg.find("dlgErr")
    assert dlgerr_at != -1 and (close_at == -1 or m.end() + dlgerr_at < close_at), \
        "fallback 面冲突提示写在了 close() 之后"


def test_s1d_fallback_dialog_renders_answered_line():
    """fallback 弹窗: answered 行必须**结构性地**挂在 t.answered_by 三元守卫下。

    issue #117: 原实现只做 `"t.answered_by" in src` 字符串存在性断言,
    把渲染条件改成 `${false?…}` 也不转红 (C 批 s6 D-2 实证)。
    """
    from mw.console_ui import HTML_TEMPLATE, _LANG_TABLES
    js = _fallback_rendered_js(HTML_TEMPLATE, _LANG_TABLES["zh-cn"])
    _assert_fallback_answered_guards(js)
    # 掩码器自检: 与原文等长 + 花括号收支平衡
    masked = _mask_js(js, mode="all")
    assert len(masked) == len(js), "掩码器破坏了源码偏移对齐"
    assert masked.count("{") == masked.count("}"), (
        "掩码器花括号不配对 (open=%d close=%d), 结构断言不可信"
        % (masked.count("{"), masked.count("}")))
    # 详情合并白名单必须带上 answered 字段 (数据侧通路, 不受渲染条件影响)
    src = (ROOT / "mw" / "console_ui.py").read_text(encoding="utf-8")
    assert "'answered_by','answered_at_hhmm'" in src, \
        "fallback 弹窗详情字段合并未包含 answered_by/answered_at_hhmm"


# ── S1m: 变异反证 (issue #117 强制项) ─────────────────────────────
#
# 断言「结构判据本身有效」: 把渲染条件改成 false 常量后, 同一套判据
# 必须判红。变异点从**结构定位结果**反推 (不写死源码字面量), 源码
# 改版时用例仍能对准真正的守卫; 变异只作用在内存中的源码副本上,
# 不写产品代码。

def _react_guard_mutants(src: str) -> list[tuple[str, str]]:
    """从 React 源码里定位到守卫, 依位置生成 (变异后源码, 变异说明)。"""
    guards = _react_badge_guards(src)
    assert guards, "React 面找不到 tboard.answered_badge 角标锚点"
    mutants = []
    for g in guards:
        assert g["cond_span"] is not None, "React 面角标未定位到守卫条件 span"
        a, b = g["cond_span"]
        original = src[a:b]
        # 变异 1: 渲染条件改 false 常量 (issue #117 原文探针)
        mutants.append((
            src[:a] + "false" + src[b:],
            "渲染条件改 false 常量 (原条件 %r)" % original,
        ))
        # 变异 2: 守卫改读错字段 (结构还在, 但读的不是 answered_by)
        mutants.append((
            src[:a] + "step.answered_at_hhmm" + src[b:],
            "守卫改读错字段 (原条件 %r)" % original,
        ))
    return mutants


def test_s1m_react_badge_guard_mutation_counterproof():
    """变异反证 (React 面): 渲染条件改 false 常量 ⇒ S1b 判据必须转红。"""
    src = (ROOT / "ui" / "src" / "views" / "TasksBoardView.jsx").read_text(
        encoding="utf-8")
    mutants = _react_guard_mutants(src)
    assert mutants, "React 面未生成任何变异体 (源码结构已变?)"
    for mutated, why in mutants:
        assert mutated != src, "变异体与原源码相同: %s" % why
        with pytest.raises(AssertionError):
            _assert_react_badge_guards(mutated)


def _fallback_guard_mutants(template: str, tables: dict) -> list[tuple[str, str]]:
    """从 fallback 模板里定位到三元守卫, 依位置生成 (变异后模板, 说明)。

    注意: 守卫定位走**渲染后**的 JS (模板原文花括号不配平, 见
    `_fallback_rendered_js`), 但变异必须落在**模板**坐标系里 ——
    所以这里用「已知条件 + `${` 前缀」在模板里回定位, 不复用渲染视图
    的偏移 (两个坐标系不一致)。
    """
    js = _fallback_rendered_js(template, tables)
    guards = _fallback_answered_guards(js)
    assert guards, "fallback 弹窗找不到 answered_by 的渲染三元守卫"
    mutants = []
    for g in guards:
        assert g["condition"] is not None, "fallback 弹窗 answered 行不是三元守卫"
        # 在模板里回定位 `${条件?` (允许空白差异)
        pat = re.compile(r"\$\{\s*" + re.escape(g["condition"]) + r"\s*\?")
        m = pat.search(template)
        assert m, (
            "fallback 模板里回定位守卫条件 %r 失败 (源码结构已变?)"
            % g["condition"])
        a = m.start()
        # 条件表达式起点 = `${` 之后
        cond_start = template.index(g["condition"], a)
        mutants.append((
            template[:cond_start] + "false" + template[cond_start + len(g["condition"]):],
            "渲染条件改 false 常量 (原条件 %r)" % g["condition"],
        ))
        mutants.append((
            template[:cond_start] + "t.answered_at_hhmm"
            + template[cond_start + len(g["condition"]):],
            "守卫改读错字段 (原条件 %r)" % g["condition"],
        ))
    return mutants


def test_s1m_fallback_answered_guard_mutation_counterproof():
    """变异反证 (fallback 面): 渲染条件改 false 常量 ⇒ S1d 判据必须转红。"""
    from mw.console_ui import HTML_TEMPLATE, _LANG_TABLES
    tables = dict(_LANG_TABLES["zh-cn"])
    mutants = _fallback_guard_mutants(HTML_TEMPLATE, tables)
    assert mutants, "fallback 面未生成任何变异体 (源码结构已变?)"
    for mutated, why in mutants:
        assert mutated != HTML_TEMPLATE, "变异体与原模板相同: %s" % why
        js = _fallback_rendered_js(mutated, tables)
        with pytest.raises(AssertionError):
            _assert_fallback_answered_guards(js)
    # 变异 i18n 表: 真分支不再渲染答复人 ⇒ 判据同样必须转红
    bad_tables = dict(tables)
    bad_tables["console.fallback.answered_line_expr"] = (
        bad_tables["console.fallback.answered_line_expr"]
        .replace("esc(t.answered_by)", "esc(t.assignee)"))
    js = _fallback_rendered_js(HTML_TEMPLATE, bad_tables)
    with pytest.raises(AssertionError):
        _assert_fallback_answered_guards(js)


# ── S2: 后端注入 —— run 详情端点每卡带 answered_by ─────────────────

def test_s2a_team_work_get_injects_answered_by(tmp_path):
    import os
    import sqlite3
    import time as _t
    os.environ.setdefault("API_SERVER_KEY", "test-key")
    from mw.ledger import Ledger
    from mw import console as console_mod

    # api_team_work_get 的卡主体来自 kanban 表, 造两张 scratch 卡
    kdb = str(tmp_path / "kanban.db")
    con = sqlite3.connect(kdb)
    con.executescript(
        "CREATE TABLE IF NOT EXISTS tasks ("
        " id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,"
        " block_kind TEXT, result TEXT, created_at REAL,"
        " started_at REAL, completed_at REAL);")
    for tid in ("t_x", "t_y"):
        con.execute(
            "INSERT OR REPLACE INTO tasks (id,title,assignee,status,result,"
            "created_at) VALUES (?,?,?,?,?,?)",
            (tid, "标题", "coder", "running", "", _t.time()))
    con.commit()
    con.close()
    console_mod.KANBAN_DB = kdb

    led = Ledger(str(tmp_path / "ledger.db"))
    try:
        console_mod.LEDGER_DB = led.path
        # 两张卡: t_x 答过 (main), t_y 没答过
        led.put_contract("t_x", {"required_files": []})
        led.put_contract("t_y", {"required_files": []})
        led.create_team_work_run("run_116", "s_main", "需求")
        led.register_team_work_card("run_116", "t_x")
        led.register_team_work_card("run_116", "t_y")
        led.enqueue_resume("t_x", "main 的答复", actor="main")

        d = console_mod.api_team_work_get("run_116")
        assert d.get("ok") is not False, d
        cards = {c["id"]: c for c in d["cards"]}
        x = cards["t_x"]
        assert x.get("answered_by") == "main", x
        assert x.get("answered_at_hhmm"), x
        assert x.get("answered_at"), x
        # 没答过的卡不得凭空出现 answered_by
        assert "answered_by" not in cards["t_y"], cards["t_y"]
    finally:
        led.close()


# ── S3: i18n 双侧齐全 ─────────────────────────────────────────────

def test_s3a_mw_i18n_new_keys_parity():
    en = json.loads((ROOT / "mw" / "i18n" / "en.json").read_text("utf-8"))
    zh = json.loads((ROOT / "mw" / "i18n" / "zh-cn.json").read_text("utf-8"))
    for k in ("console.fallback.answered_line_expr",
              "console.fallback.conflict_notice_expr"):
        assert k in en, f"en.json 缺 {k}"
        assert k in zh, f"zh-cn.json 缺 {k}"
    assert set(en) == set(zh), "mw/i18n en/zh key 集合不等 (G1 守护)"


def test_s3b_react_locales_new_keys_parity():
    en = json.loads((ROOT / "ui" / "src" / "i18n" / "locales" / "en.json")
                    .read_text("utf-8"))
    zh = json.loads((ROOT / "ui" / "src" / "i18n" / "locales" / "zh-cn.json")
                    .read_text("utf-8"))
    for k in ("reply_conflict", "answered_badge"):
        assert k in en["tboard"], f"en tboard 缺 {k}"
        assert k in zh["tboard"], f"zh-cn tboard 缺 {k}"
    assert set(en["tboard"]) == set(zh["tboard"]), \
        "React locales tboard en/zh key 集合不等"


def test_s3c_fallback_template_fully_substitutable():
    from mw.console_ui import HTML_TEMPLATE, _LANG_TABLES
    tmpl_keys = set(re.findall(r"⟪([A-Z0-9_]+)⟫", HTML_TEMPLATE))
    for lang in ("en", "zh-cn"):
        table = _LANG_TABLES[lang]
        mapped = {k.replace(".", "_").upper() for k in table}
        missing = tmpl_keys - mapped
        assert not missing, f"{lang} 表缺哨兵键: {missing}"
        # 整页替换后不得残留哨兵
        body = HTML_TEMPLATE
        for sk, sv in table.items():
            body = body.replace("⟪" + sk.replace(".", "_").upper() + "⟫", sv)
        leftover = re.findall(r"⟪[A-Z0-9_]+⟫", body)
        assert not leftover, f"{lang} 渲染后残留哨兵: {leftover}"
