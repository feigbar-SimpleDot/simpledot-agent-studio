"""issue #139 守护: TODO.md 契约文件解析层健壮性 (回归锁)。

实案背景: 纯文本契约文件被 worker 写坏 ⇒ parse_status 读 None ⇒ reaper
走「状态未识别 → 保守等待」⇒ 链静默停摆 (G 批 s3 实际停 3h19m)。I 批
#152 只容错了 `N|` 行号前缀, 本文件钉死 K 批补齐的 4 种残余形态:
  tab (`N\\t`, cat -n 风格) / star bullet (`* 状态:`) /
  缺 `- ` 前缀 (`状态:`) / 缩进 (`  - 状态:`)

口径 = docs/PLAN-K-20261029.md §1.4 断言组 T-A..T-G:
  T-A 四形态 × 状态别名 ⇒ parse_status 逐字相等
  T-B 四形态 × 四入口判定一致
  T-C 合法含数字内容不误伤 (与基线逐字一致)
  T-D 段限定: 段外状态行读不到 (全文件扫描 fallback 不得复活)
  T-E diagnose 五 reason 各钉一组
  T-F 快路径回归牙: 无竖线 tab 文档必须可读 (专拦 MV-139-B 掩蔽)
  T-G 剥离函数加宽后幂等 + `12:` / `12)` 形态不被剥
  T-H 代价上界: 深前导空白+无冒号行 / 键区长空白 run 的耗时断言
      (issue #139-F1, s10 回流卡: 旧 _FIELD_LINE pattern 的相邻量词致
       超线性回溯, 400 空格单行 ≈ 16 s, 落在 reaper 结算热路径)

★ 变异反证 MV-139-A..E 见 PLAN-K §1.6; 本文件对每条变异都有转红用例。
★ F-1 变异反证 MV-F1-A/B/C 见 docs/FIX-NOTES-K-F1.md §5。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mw.todo import (  # noqa: E402
    parse, parse_status, current_binding, verify_current_task_binding,
    normalize_status, diagnose, _strip_line_num_prefix, _match_field_line,
    _LINE_NUM_PREFIX,
)

# 一份结构完整的 TODO.md (与 152 守护文件同风格)
CLEAN_DOC = """# Team Work: 批处理示例

## 当前任务
- 步骤: s1
- 标题: 需求分析
- 目标: 分析需求并出报告
- 承接方: product-manager
- 产物目录: /share_workspace/projects/demo
- 产出文件: docs/REQ.md
- 状态: completed
- 结果: verdict=COMPLETE; 产物在 docs/REQ.md
- 卡: t_abc123

---

## 待执行
- [s2] 技术方案 → system-engineer
  目标: 出文件:行级方案
  卡: t_def456

## 已完成
- [s0] 立项 → 已建单

## 已放弃
- (无)
"""


# ---- 四种残余破坏形态的生成函数 (与 s1 探针 probe_s1_forms.py 同口径) ----

def form_tab(text: str) -> str:
    """cat -n / nl -ba 风格: 每行前缀 `数字+制表符`。"""
    return "\n".join(f"{i}\t{ln}" for i, ln in enumerate(text.splitlines(), 1))


def form_star(text: str) -> str:
    """bullet 风格: `- ` 换成 `* `。"""
    return "\n".join(("* " + ln[2:]) if ln.startswith("- ") else ln
                     for ln in text.splitlines())


def form_nodash(text: str) -> str:
    """缺 `- ` 前缀: 字段行去掉行首 `- `。"""
    return "\n".join(ln[2:] if ln.startswith("- ") else ln
                     for ln in text.splitlines())


def form_indent(text: str) -> str:
    """缩进: 字段行前加两个空格。"""
    return "\n".join(("  " + ln) if ln.startswith("- ") else ln
                     for ln in text.splitlines())


DAMAGED_FORMS = {
    "tab": form_tab,
    "star": form_star,
    "no-dash": form_nodash,
    "indented": form_indent,
}


def _write(tmp_path: Path, text: str, name: str = "TODO.md") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _snapshot(path: Path) -> dict:
    """四入口的返回值快照 —— 用于「坏文件 == 干净文件」的逐字比对。"""
    doc = parse(path)
    cur = None
    if doc and doc.current:
        cur = {
            "step_id": doc.current.step_id,
            "title": doc.current.title,
            "goal": doc.current.goal,
            "assignee": doc.current.assignee,
            "status": doc.current.status,
            "result": doc.current.result,
            "product_workspace": doc.current.product_workspace,
            "required_files": list(doc.current.required_files),
            "kanban_task_id": doc.current.kanban_task_id,
        }
    return {
        "title": doc.title if doc else None,
        "parse_status": parse_status(path),
        "current": cur,
        "pending": [
            {"step_id": t.step_id, "title": t.title, "assignee": t.assignee,
             "goal": t.goal, "product_workspace": t.product_workspace,
             "required_files": list(t.required_files),
             "kanban_task_id": t.kanban_task_id}
            for t in (doc.pending if doc else [])
        ],
        "done": list(doc.done) if doc else [],
        "dropped": list(doc.dropped) if doc else [],
        "binding": current_binding(path),
        "verify": verify_current_task_binding(path, "t_abc123", "s1"),
    }


# ------------------------------------------------------------------ T-A
@pytest.mark.parametrize("form", sorted(DAMAGED_FORMS))
@pytest.mark.parametrize("status", ["completed", "working"])
def test_damaged_forms_parse_status_exact(tmp_path, form, status):
    """T-A (AC-139-1): 四形态 × 状态别名 ⇒ parse_status 逐字相等。

    钉**值本身**而非「非 None」: 停摆根因是 None 后保守等待。
    """
    text = DAMAGED_FORMS[form](
        CLEAN_DOC.replace("- 状态: completed", f"- 状态: {status}"))
    assert parse_status(_write(tmp_path, text)) == status


# ------------------------------------------------------------------ T-B
@pytest.mark.parametrize("form", sorted(DAMAGED_FORMS))
def test_four_entry_points_agree(tmp_path, form):
    """T-B (AC-139-3): 四形态下四入口归一化后逐字相等, verify 与 binding 一致。

    旧实现四入口各持一份字段行正则 ⇒ indented 形态入口间不一致
    (parse_status=None 而 parse/current_binding=completed); 收敛到
    _match_field_line 单一识别点后必须齐步。
    """
    text = DAMAGED_FORMS[form](CLEAN_DOC)
    p = _write(tmp_path, text)
    ps = parse_status(p)
    doc = parse(p)
    cur_status = doc.current.status if (doc and doc.current) else None
    cb = current_binding(p)
    cb_status = cb["status"] if cb else None
    assert normalize_status(ps) == "completed"
    assert normalize_status(cur_status) == normalize_status(ps)
    assert normalize_status(cb_status) == normalize_status(ps)
    ok_v, detail = verify_current_task_binding(p, "t_abc123", "s1")
    assert ok_v is True and detail == {}


@pytest.mark.parametrize("form", sorted(DAMAGED_FORMS))
def test_damaged_form_current_fields_equal_clean(tmp_path, form):
    """T-B 强化: 四形态下『当前任务』全部字段 + verify 结论与干净文件逐字相等。

    范围口径: 容错钉的是 worker 拥有的『当前任务』段字段与结算面 (PLAN-K
    T-B 口径); pending/done 段是 main 拥有的归档区, 不在形态容错范围内。
    """
    clean = _write(tmp_path, CLEAN_DOC, "TODO-clean.md")
    damaged = _write(tmp_path, DAMAGED_FORMS[form](CLEAN_DOC), "TODO-bad.md")
    c, d = _snapshot(clean), _snapshot(damaged)
    for key in ("title", "parse_status", "current", "binding", "verify"):
        assert d[key] == c[key], f"{form}: {key} 不一致"


# ------------------------------------------------------------------ T-C
def test_legal_digit_result_not_broken(tmp_path):
    """T-C (AC-139-2): 合法文件『结果』行含数字开头内容 ⇒ 四入口与基线一致。

    容错不是「越宽松越好」: `- 结果: 2026-10-29 完成了 X` 是合法内容,
    解析结果必须与 s1 基线 (全 completed/True) 逐字一致。
    """
    text = CLEAN_DOC.replace("- 结果: verdict=COMPLETE; 产物在 docs/REQ.md",
                             "- 结果: 2026-10-29 完成了 X")
    p = _write(tmp_path, text)
    assert parse_status(p) == "completed"
    doc = parse(p)
    assert doc.current.status == "completed"
    assert doc.current.result == "2026-10-29 完成了 X"
    assert current_binding(p) == {
        "step_id": "s1", "kanban_task": "t_abc123",
        "status": "completed", "title": "需求分析",
    }
    assert verify_current_task_binding(p, "t_abc123", "s1") == (True, {})


@pytest.mark.parametrize("noise", ["123 普通文本", "12: 普通文本"])
def test_plain_text_lines_in_current_section_keep_status(tmp_path, noise):
    """T-C 补充: 『当前任务』段内插入数字开头普通文本行 ⇒ 状态仍读原值。

    `12: 普通文本` 会被字段行识别取键 `12` —— 非协议键, 无消费方
    (PLAN-K §1.3 已知行为边界 1), 状态值不受影响。
    """
    text = CLEAN_DOC.replace("- 卡: t_abc123", f"- 卡: t_abc123\n{noise}")
    p = _write(tmp_path, text)
    assert parse_status(p) == "completed"
    assert current_binding(p)["status"] == "completed"


# ------------------------------------------------------------------ T-D
def test_status_line_outside_current_section_not_read(tmp_path):
    """T-D (AC-139-4b): 段外状态行 (no-dash 写法) 读不到 ⇒ 全文件扫描 fallback 未复活。

    2026-10-03 减法删掉「全文件扫一行式状态字段」fallback 的理由必须在
    本用例上持续成立: 缺 `- ` 前缀的容错**只在『当前任务』段内**发生。
    """
    # 状态行只出现在『已完成』段 (no-dash 写法)
    text = (CLEAN_DOC.replace("- 状态: completed\n", "")
            .replace("## 已完成\n- [s0] 立项 → 已建单",
                     "## 已完成\n- [s0] 立项 → 已建单\n状态: completed"))
    p = _write(tmp_path, text)
    assert parse_status(p) is None
    assert diagnose(p)["reason"] == "no_status_line"
    # 同样内容挪进『当前任务』段 ⇒ 可读 (段内/段外行为分界)
    text_in = CLEAN_DOC.replace("- 卡: t_abc123",
                                "- 卡: t_abc123\n状态: completed")
    assert parse_status(_write(tmp_path, text_in, "IN.md")) == "completed"


# ------------------------------------------------------------------ T-E
def test_diagnose_file_missing(tmp_path):
    """T-E: 缺文件 / 非法 UTF-8 ⇒ file_missing (与 current_binding fail-closed 口径对齐)。"""
    assert diagnose(tmp_path / "nope.md") == {"status": None,
                                              "reason": "file_missing"}
    bad = tmp_path / "BAD.md"
    bad.write_bytes(b"\xff\xfe\x00 broken utf8")
    assert diagnose(bad) == {"status": None, "reason": "file_missing"}


def test_diagnose_no_current_section(tmp_path):
    """T-E: 无『当前任务』段 (含中英别名) ⇒ no_current_section。"""
    text = "# Team Work: T\n\n## 待执行\n- [s1] x → coder\n"
    assert diagnose(_write(tmp_path, text))["reason"] == "no_current_section"


def test_diagnose_no_status_line(tmp_path):
    """T-E: 段内无状态字段行 ⇒ no_status_line。"""
    text = CLEAN_DOC.replace("- 状态: completed\n", "")
    assert diagnose(_write(tmp_path, text))["reason"] == "no_status_line"


@pytest.mark.parametrize("bad_value", ["乱码xyz", ""])
def test_diagnose_unrecognized_value(tmp_path, bad_value):
    """T-E: 状态值为空或 normalize_status 归一化不了 ⇒ unrecognized_value。"""
    text = CLEAN_DOC.replace("- 状态: completed", f"- 状态: {bad_value}")
    assert diagnose(_write(tmp_path, text))["reason"] == "unrecognized_value"


def test_diagnose_ok_and_damaged_forms(tmp_path):
    """T-E: clean ⇒ ok; 四残余形态经 diagnose 也是 ok (可观测入口与四入口同口径)。"""
    assert diagnose(_write(tmp_path, CLEAN_DOC)) == {
        "status": "completed", "reason": "ok"}
    for form, fn in DAMAGED_FORMS.items():
        d = diagnose(_write(tmp_path, fn(CLEAN_DOC), f"{form}.md"))
        assert d == {"status": "completed", "reason": "ok"}, form


# ------------------------------------------------------------------ T-F
def test_tab_form_without_any_pipe_is_readable(tmp_path):
    """T-F (AC-139-4c) 快路径回归牙: 全文无任何竖线的 tab 形态必须可读。

    专拦「恢复 `if \"|\" not in text: return text` 快路径」的行为等价变异
    (MV-139-B, I 批 #153 实坑): 快路径使加宽后的 pattern 对无竖线输入
    静默失效 ⇒ 只跑含竖线的正常批证明不了容错真的生效。
    """
    text = form_tab(CLEAN_DOC)
    assert "|" not in text                 # 前提: 不含竖线字符
    assert parse_status(_write(tmp_path, text)) == "completed"


# ------------------------------------------------------------------ T-G
def test_strip_helper_widened_idempotent_and_narrow():
    """T-G: 剥离函数加宽后幂等; `12:` / `12)` 形态不被剥 (下界不放宽)。"""
    tabbed = form_tab(CLEAN_DOC)
    once = _strip_line_num_prefix(tabbed)
    assert _strip_line_num_prefix(once) == once          # 幂等
    assert once == "\n".join(CLEAN_DOC.splitlines())     # 回到无前缀形态
    # 竖线形态仍剥 (I 批 #152 语义保持)
    assert _strip_line_num_prefix("a|b\n12|c") == "a|b\nc"
    # 冒号 / 括号形态不在剥离范围
    assert _strip_line_num_prefix("12: x") == "12: x"
    assert _strip_line_num_prefix("12) x") == "12) x"
    # 无行号前缀的原文逐字返回 (含尾部换行)
    assert _strip_line_num_prefix(CLEAN_DOC) == CLEAN_DOC


# ------------------------------------------------------------------ T-H
# issue #139-F1 (s10): _FIELD_LINE 的**代价上界**守护。
#
# 背景: #139 把四入口的字段行正则收敛为单一识别点 `_FIELD_LINE`
# (mw/todo.py), 但旧 pattern `^\s*[-*]?\s*([^:：]+?)\s*[:：]\s*(.*)$`
# 把三个相邻量词作用在同一段前导空白 run 上 ⇒ 行内**无冒号**时引擎须对
# 「空白 run 的每一种切分」逐一回溯, 成本约 n^3.6 (400 空格单行 ≈ 16 s)。
# 该识别点被 parse_status (结算面 reaper 每 15 s 串行轮询) / current_binding
# (派发面) / _parse_kv (parse 面) / diagnose 四处共用 ⇒ 单个长行即可把
# 一轮结算拖到分钟级 —— 而「静默停摆」正是 #139 要消灭的失效模式。
#
# 本组钉两件事:
#   T-H1 深前导空白 + 无冒号行 ⇒ 读不出 (语义) 且**耗时**有上界 (代价);
#   T-H2 含冒号但键区含长空白 run ⇒ 同样有上界 (键名组惰性→贪婪的代价面);
#   T-H3 修复**不得**放宽语义: 合法内容行 (含数字+制表符开头) 不被剥。
#
# 耗时断言用标准库 time.perf_counter 实测 (仓库**无** pytest-timeout
# 依赖 ⇒ 不引入新依赖)。上界取 10 ms: 修复后实测 0.002~0.03 ms, 余量
# 三个数量级; 修复前 400 空格即 > 8 s (实测超时), 必然转红。

# 耗时上界 (毫秒)。修复后实测 ≤ 0.03 ms, 余量三个数量级。
TIME_BUDGET_MS = 10.0


def _elapsed_ms(fn, *args):
    """实测单次调用耗时 (毫秒), 标准库 perf_counter。"""
    t0 = time.perf_counter()
    fn(*args)
    return (time.perf_counter() - t0) * 1000.0


def test_deep_leading_whitespace_without_colon_is_cheap_and_unreadable(tmp_path):
    """T-H1 (AC-F1-1): 深前导空白 + 无冒号行 ⇒ 读不出, 且解析耗时 < 10 ms。

    该形态是 F-1 的触发输入: 旧 pattern 在此超线性回溯 (400 空格 ≈ 16 s)。
    断言两件事 —— 语义 (读不出, 不误判成字段行) 与代价 (耗时上界)。
    语义断言不可省: 只钉耗时的话, 「短路后恒返回 None」也能过, 但那会
    把**含冒号的合法行**一起误杀 (由 T-H2 单独钉)。
    """
    deep = " " * 400                      # 400 空格一行 (无冒号)
    assert ":" not in deep and "：" not in deep
    # 语义: 无冒号 ⇒ 不是字段行
    assert _match_field_line(deep) is None
    # 代价: 单行识别耗时必须有上界
    elapsed = _elapsed_ms(_match_field_line, deep)
    assert elapsed < TIME_BUDGET_MS, (
        f"深前导空白无冒号行耗时 {elapsed:.3f} ms 超过上界 "
        f"{TIME_BUDGET_MS} ms (F-1 超线性回溯回归)")
    # 同形态经真实入口 (parse_status) 也必须在上界内
    text = CLEAN_DOC.replace("- 卡: t_abc123", f"- 卡: t_abc123\n{deep}")
    p = _write(tmp_path, text, "DEEP.md")
    elapsed_ep = _elapsed_ms(parse_status, p)
    assert elapsed_ep < TIME_BUDGET_MS, (
        f"parse_status 端到端耗时 {elapsed_ep:.3f} ms 超过上界 "
        f"{TIME_BUDGET_MS} ms")


def test_long_whitespace_run_inside_key_is_cheap():
    r"""T-H2: 键区含长空白 run (行内**有**冒号) ⇒ 耗时同样有上界。

    这一族**不触发**无冒号短路 ⇒ 专拦「只加短路就收工」的半吊子修法:
    键名组惰性 `([^:：]+?)` 与其后 `\s*` 相邻, 键区含长空白 run 时仍会
    二次回溯 (修复前 'x'+6400 空格+'y: v' ≈ 431 ms)。修复后键名组改贪婪
    ⇒ 线性 (实测 ≈ 0.27 ms)。
    """
    for n in (2000, 6400):
        ln = "x" + " " * n + "y: v"
        elapsed = _elapsed_ms(_match_field_line, ln)
        assert elapsed < TIME_BUDGET_MS, (
            f"键区空白 {n} 的行耗时 {elapsed:.3f} ms 超过上界 "
            f"{TIME_BUDGET_MS} ms")


def test_field_line_semantics_unchanged_for_legal_content(tmp_path):
    r"""T-H3 (AC-F1-3 回归锁): 合法内容行不被剥、不被改写。

    修复只动**代价特性**, 不得改语义:
      - 行号前缀剥离的覆盖面**逐字未变** (_LINE_NUM_PREFIX pattern 钉死)
        ⇒ 本卡未波及 _strip_line_num_prefix;
      - 契约行 (以 `- ` 开头) 内的「数字+制表符」内容不被剥
        (剥离是行首锚定, 行首不是数字 ⇒ 不触发);
      - 键名/键值的 strip 与「值可含冒号 (只切第一个)」语义不变;
      - 含数字开头的『结果』行内容不被剥。

    ★ 已知边界 (F-4, 审查卡 s7 判 P3 follow-up, **本卡不修**):
      裸行 `2026<TAB>完成` 会被剥离 ⇒ 变 `完成`。该行为由 #139 有意加宽
      _LINE_NUM_PREFIX 引入 (cat -n 形态容错), 属计划内变更; 本卡只钉
      「覆盖面未被本卡改动」, 不在此处改变该语义 (改它 = 越出本卡红线)。
    """
    # (a) 行号前缀剥离覆盖面逐字未变 (证明本卡未波及 _strip_line_num_prefix)
    assert _LINE_NUM_PREFIX.pattern == r"^[ \t]*\d+[|\t]"
    # (b) 契约行内的「数字+制表符」内容不被剥 (剥离行首锚定)
    assert _strip_line_num_prefix("- 结果: 2026\t完成了 X") == "- 结果: 2026\t完成了 X"
    # (c) 键名/键值 strip 语义不变
    assert _match_field_line("-   状态   :   completed   ") == ("状态", "completed")
    # (d) 值可含冒号 (只切第一个)
    assert _match_field_line("- 标题: 含冒号的标题: 继续") == ("标题", "含冒号的标题: 继续")
    # (e) 含数字开头的『结果』行内容不被剥 (AC-F1-3 合法文件不误伤)
    text = CLEAN_DOC.replace("- 结果: verdict=COMPLETE; 产物在 docs/REQ.md",
                             "- 结果: 2026-10-29 完成了 X")
    p = _write(tmp_path, text, "DIGIT.md")
    assert parse_status(p) == "completed"
    doc = parse(p)
    assert doc is not None and doc.current is not None
    assert doc.current.result == "2026-10-29 完成了 X"
    assert current_binding(p)["status"] == "completed"


# ------------------------------------------------------------------ T-I
@pytest.mark.parametrize("form", sorted(DAMAGED_FORMS))
def test_four_forms_still_readable_after_f1_fix(tmp_path, form):
    """T-I (AC-F1-2): F-1 修复**不得**回退 #139 的四形态容错。

    逐形态**单列**断言 (卡面 AC-F1-2 明令: 不许用「四种一起转绿」一个
    断言概括) ⇒ 本用例按形态参数化, 每个形态是 pytest 收集到的**独立
    用例**; 每个形态内部再对四个入口**逐个**断言。

    四个入口 = parse_status / parse.current.status / current_binding.status
    / verify_current_task_binding。断言值是**状态字面**本身 (不是
    「非 None」) —— 停摆根因是 None 后 reaper 走保守等待。
    """
    text = DAMAGED_FORMS[form](CLEAN_DOC)
    p = _write(tmp_path, text, f"{form}.md")

    # 入口 1: parse_status
    assert parse_status(p) == "completed", f"{form}: parse_status 未读出状态"

    # 入口 2: parse → current.status
    doc = parse(p)
    assert doc is not None and doc.current is not None, f"{form}: parse 未造出 current"
    assert doc.current.status == "completed", f"{form}: parse.current.status 不对"

    # 入口 3: current_binding.status
    cb = current_binding(p)
    assert cb is not None, f"{form}: current_binding 为 None"
    assert cb["status"] == "completed", f"{form}: current_binding.status 不对"

    # 入口 4: verify_current_task_binding
    ok_v, detail = verify_current_task_binding(p, "t_abc123", "s1")
    assert ok_v is True, f"{form}: verify 未放行 ({detail})"
    assert detail == {}, f"{form}: verify 详情非空 ({detail})"
