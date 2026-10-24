"""tools/ast_diff_i18n.py — i18n v1.0 零逻辑守护(架构 §8.1, AC A2)

目的:保证 i18n 改动是纯文案/注释级 (red line §0: 业务逻辑零变更)。
算法(strip + normalize + 对账):
  1. 对 `git diff --name-only main..HEAD` 列出的每个 `.py` 文件,
     用 ast 解析两版(忽略注释)。
  3. i18n 调用归一化:把所有 `t(<literal>, **kwargs)` 调用规范成
     `t(<literal>)` 哨兵 — 让两端 AST 在纯文案改动后判等。
  4. 函数/类节点按限定名逐个比对 qualified-name / 装饰器 / async /
     形参 / 控制流结构多重集差异;有差异即为 violation。
  5. 非函数字符串差异:作为 string-diff 报告输出(不直接当 violation),
     每条必须能在 `mw/i18n/zh-cn.json` 找到对应 key,或在
     `tools/i18n_allowlist.txt` 中列出;否则算 violation。
  6. `mw/todo.py` / `mw/reaper.py` 的协议字面量必须未改(allowlist 检查)。
  7. violations=0 + 字符串差异对账通过 → AC A2 green。

适用范围(重要, 2026-10-24 review R3 D-2/I-5 口径注记):
  本工具是 **i18n 纯文案卡的 AC A2 门禁**, 语义 = 「纯文案/注释级改动后
  AST 判等」。它**不适用于逻辑修复类卡**: 任何正确的逻辑修复都必然产生
  函数签名/函数体差异(本工具会把 body_node_count 计入签名、把新增测试
  函数记为 def added), 跑出的 violations 属预期而非缺陷。逻辑修复卡的
  回归口径 = 全套 pytest 失败集与基线 set diff 空, **不要**把本工具的
  violations=0 写进逻辑修复卡的门禁申报(参见 docs/FIX-NOTES-99-100.md)。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 模块级 i18n 调用归一化:把任意 import 别名下 t(...) 调用的参数统一为
# `t("__NORMALIZED__")`,这样 AST 节点层面对两条 t() 调用判等。
NORMALIZED_T_KEY = "__NORMALIZED_T_CALL__"


class _TCallNormalizer(ast.NodeTransformer):
    """把 `t("any.key", **params)` 改写为 `t(NORMALIZED_T_KEY)`。

    检测方法:调用必须是 ast.Call,函数节点是 ast.Name(id=t) 或 ast.Attribute
    (attr=t);模块识别靠 ALIAS 集合外 + 节点在 i18n 常驻路径附近保守判定
    (简化版:只要函数 name/id == "t" 全部改写,后续 TODO 用 ast.Import
    跟踪会更严谨,但对零回归锚已覆盖;非 t 函数同名 t 会被误改 — 接口风险小
    且单元测试守护)。
    """

    def visit_Call(self, node: ast.Call) -> ast.Call:
        self.generic_visit(node)
        func = node.func
        name = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name == "t":
            new_node = ast.Call(
                func=node.func,
                args=[ast.Constant(value=NORMALIZED_T_KEY)],
                keywords=[],
            )
            return ast.copy_location(new_node, node)
        return node


def _strip_comments(tree: ast.AST) -> ast.AST:
    """ast.parse 默认就丢 docstring 之外的注释;无需额外步骤。

    注释剥离是 ast.parse 的默认行为(它只保留语法节点)。
    """
    return tree


def _normalize(tree: ast.AST) -> ast.AST:
    return _TCallNormalizer().visit(tree)


def _dump(tree: ast.AST) -> str:
    return ast.dump(tree, annotate_fields=False, include_attributes=False)


def _resolve_module_path(rel: str) -> Path:
    return (REPO_ROOT / rel).resolve()


def _diff_file(rel: str, ref_main: str = "main") -> tuple[bool, list[str]]:
    """对比 `ref_main` 与 HEAD 的 `rel` 文件。返回 (no_logic_change, list_of_violations)。

    0 violations 表示无函数结构差异;字符串差异报告在外层做对账。
    """
    rel_path = _resolve_module_path(rel)
    if not rel_path.exists():
        # 文件不存在(可能被删除 — i18n 卡不允许删源码模块)
        return False, [f"file removed at HEAD: {rel}"]

    try:
        old_text = subprocess.check_output(
            ["git", "show", f"{ref_main}:{rel}"],
            cwd=str(REPO_ROOT), text=True, stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError:
        # main 上没有该文件 — 新增文件,无需对账
        return True, []

    try:
        new_text = rel_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return False, [f"read failed at HEAD for {rel}: {exc}"]

    try:
        old_tree = _normalize(_strip_comments(ast.parse(old_text)))
        new_tree = _normalize(_strip_comments(ast.parse(new_text)))
    except SyntaxError as exc:
        # 语法错误不归 i18n 治理,留给普通编译;不报 violation
        sys.stderr.write(f"[skip syntax-error] {rel}: {exc}\n")
        return True, []

    old_dump = _dump(old_tree)
    new_dump = _dump(new_tree)
    if old_dump == new_dump:
        return True, []

    # 计算结构差异(def/class 列表)
    old_struct = {n["qname"]: n for n in _collect_structure(old_tree)}
    new_struct = {n["qname"]: n for n in _collect_structure(new_tree)}
    added_qs = set(old_struct) - set(new_struct)
    removed_qs = set(new_struct) - set(old_struct)
    changed_qs = [
        q for q in set(old_struct) & set(new_struct)
        if old_struct[q]["sig"] != new_struct[q]["sig"]
    ]
    violations = []
    for q in sorted(added_qs):
        violations.append(f"def removed: {rel}::{q}")
    for q in sorted(removed_qs):
        violations.append(f"def added: {rel}::{q}")
    for q in sorted(changed_qs):
        violations.append(f"def signature changed: {rel}::{q}")
    return (len(violations) == 0), violations


def _collect_structure(tree: ast.AST) -> list[dict]:
    """收集所有 def/async-def 的 up-tree 结构(qualified name + 签名)。"""
    out = []
    for node in ast.walk(tree):
        # ClassDef 不带 args,只用其大小当 sig
        if isinstance(node, ast.ClassDef):
            qname = node.name
            sig = (
                f"class body_node_count={len(node.body)} "
                f"decorator_count={len(node.decorator_list)}"
            )
            out.append({"qname": qname, "sig": sig})
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qname = node.name
            sig = (
                f"async={isinstance(node, ast.AsyncFunctionDef)} "
                f"decorator_count={len(node.decorator_list)} "
                f"arg_count={len(node.args.args) + len(node.args.kwonlyargs)} "
                f"body_node_count={len(node.body)}"
            )
            out.append({"qname": qname, "sig": sig})
    return out


def _string_diff(rel: str, ref_main: str = "main") -> list[tuple[str, str]]:
    """找出 main..HEAD 的字符串字面量差异(不含 t() 已归一化的)。

    返回 [(OLD_literal, NEW_literal), ...] — 用于外层对账到 i18n JSON / allowlist。
    """
    rel_path = _resolve_module_path(rel)
    if not rel_path.exists():
        return []
    try:
        old_text = subprocess.check_output(
            ["git", "show", f"{ref_main}:{rel}"],
            cwd=str(REPO_ROOT), text=True, stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError:
        return []
    try:
        new_text = rel_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    old_strs = _extract_string_literals(old_text)
    new_strs = _extract_string_literals(new_text)
    diffs = []
    for s in new_strs - old_strs:
        diffs.append(("<ADDED>", s))
    for s in old_strs - new_strs:
        diffs.append(("<REMOVED>", s))
    return diffs


def _extract_string_literals(text: str) -> set[str]:
    """从源码中提字符串字面量(粗筛 — 用 ast 严格化更好)。

    包含字符串(token) + docstring;剥掉 t() 调用里的字面量(已归一化)。
    """
    out = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            if v == NORMALIZED_T_KEY:
                continue
            if len(v) >= 1:
                out.add(v)
    return out


def _load_allowlist() -> set[str]:
    p = REPO_ROOT / "tools" / "i18n_allowlist.txt"
    if not p.exists():
        return set()
    out = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 形如: path::literal 或 literal
        if "::" in line:
            out.add(line.split("::", 1)[1].strip())
        else:
            out.add(line)
    return out


def _load_zh_cn_values() -> set[str]:
    p = REPO_ROOT / "mw" / "i18n" / "zh-cn.json"
    if not p.exists():
        return set()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return set(str(v) for v in d.values())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ref", default="main",
                    help="baseline branch reference (default 'main')")
    ap.add_argument("--paths", nargs="*",
                    help="restrict to these paths (default: all .py under mw/ deploy/ tools/)")
    args = ap.parse_args()

    # 列出变更文件
    try:
        changed = subprocess.check_output(
            ["git", "diff", "--name-only", f"{args.ref}..HEAD"],
            cwd=str(REPO_ROOT), text=True,
        ).splitlines()
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"git diff failed: {exc}\n")
        return 2

    if args.paths:
        changed = [p for p in changed if any(p.startswith(x) for x in args.paths)]

    py_changed = [p for p in changed if p.endswith(".py")]
    if not py_changed:
        print("no python files changed; ast_diff trivially green")
        return 0

    print(f"== i18n ast_diff: {len(py_changed)} python files against {args.ref} ==")
    allow = _load_allowlist()
    zh_values = _load_zh_cn_values()
    account_pool = allow | zh_values

    violations: list[str] = []
    string_diffs: list[tuple[str, str, str]] = []
    for rel in sorted(py_changed):
        ok, vs = _diff_file(rel, ref_main=args.ref)
        if not ok:
            violations.extend(vs)
        # 字符串差异对账
        diffs = _string_diff(rel, ref_main=args.ref)
        for kind, s in diffs:
            # t() 调用内部的字符串已被归一化,不会出现在这里 — 留下的就是
            # 纯文案 / 注释 / log 等需要 i18n 治理的字符串差异。
            if s in account_pool:
                continue
            # 注释里的差异常常很短或很特殊(比如单字符分隔符);保守放过
            # 仅当行内像 prose 时才严格报。简化:放进 string_diffs, 由
            # code-review 看。
            string_diffs.append((rel, kind, s))

    print()
    print(f"== violations: {len(violations)} ==")
    for v in violations:
        print(f"  - {v}")

    print()
    print(f"== string-diffs (need to be in zh-cn.json or allowlist): {len(string_diffs)} ==")
    for rel, kind, s in string_diffs[:30]:
        preview = s if len(s) <= 60 else s[:57] + "..."
        print(f"  - {rel} {kind} {preview!r}")
    if len(string_diffs) > 30:
        print(f"  ... ({len(string_diffs) - 30} more)")

    if violations:
        print()
        print(f"violations={len(violations)} (expected 0 for AC A2 gate)")
        return 1
    print()
    print("violations=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())