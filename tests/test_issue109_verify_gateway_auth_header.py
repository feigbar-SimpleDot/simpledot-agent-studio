#!/usr/bin/env python3
# coding: utf-8
"""issue #109 回归守护: verify.sh 的 gateway 探测必须发真的 Bearer 头 (纯静态断言)。

背景 (s5 部署回流, 2026-10-26):

    issue #109 (commit 9d0e4c8) 把 gateway 健康检查从「宿主 curl 宿主
    端口 :8642」改写成「`docker exec` 进容器 + 容器内 python3 urllib POST
    :8642/api/sessions」(宿主不再发布 8642 端口, 宿主 curl 必然 refused)。
    改写时把 Authorization 头的前缀从 `Bearer ${KEY}` 抄成了占位符 `***`。
    ⇒ 线上探测头变成字面 `***<API_SERVER_KEY>`, gateway 一律回
    `gateway_auth_failed` 401, verify.sh 的 gateway 项恒红 —— 部署明明
    成功, 自检却报「gateway 不可达」。

    真机实测 (170, evidence-batch-d-s5/08_auth_variants.txt) 三变体对照:

        bearer      ("Bearer " + key) → 201 Created  ✅
        raw         (key)             → 401 Unauthorized
        bearer_lower("bearer " + key) → 401 Unauthorized

    ⇒ 头前缀必须是 `Bearer` + **一个半角空格**, 大小写敏感。

    这是同一坑的第二次回归: b732423 (2026-10-03) 已经踩过一次
    (硬编码 `Bearer ***` ⇒ gateway 检查永远 fail, 哪怕部署成功),
    commit message 里写明教训「verify.sh 的 self-test 不能用占位符」。
    #109 改写探测时没把这条教训继承下来 ⇒ 本文件把不变量钉死。

守护的不变量:

    G1  verify.sh 的 gateway 探测段里, Authorization 头的前缀字面量
        必须逐字节等于 `Bearer ` (6 字母 + 1 个半角空格, 共 7 字节),
        且 sha256 与锚点一致 (落盘自证, 防止"看着像 Bearer"就放过)。
    G2  该字面量里零个 `*` —— 占位符绝不能进到运行时头里。
    G3  前缀之后仍必须拼接容器内的 key (不能退化成写死某个 key),
        否则等于把真 key 写进仓库。
    G4  头部大小写敏感: `bearer ` / `BEARER ` 一律判红 (真机 401 实测)。
    G5  变异反证: 把字面量改回 `***` / 去掉空格 / 改小写 / 不拼 key,
        本文件的判据必须转红 —— 证明断言不是恒真。
    G6  全仓 tracked 文件零 `***` 残留 (白名单只放正当用法:
        注册表 key 掩码 / 示例密码占位符 / 回显打码伪影的经验文字)。
    G7  任何含 Authorization 的行都不得携带 `***` (运行时头绝不打码)。

本文件是**纯静态断言**: 只读 deploy/verify.sh 文本 + 算 sha256,
不碰真机、不发网络请求、不起 docker 容器。
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERIFY_SH = ROOT / "deploy" / "verify.sh"

# 期望的前缀字面量: `Bearer` + 一个半角空格 (7 字节)
EXPECTED_PREFIX = "Bearer "
# sha256("Bearer ") —— 落盘自证锚点
EXPECTED_SHA256 = (
    "35e5a9632e18bc9adad684a3eafdd297139c82b5693b7fa2a8d0efcaf024cdcf"
)

# 抓 gateway 探测段里的 Authorization 头构造:
#   headers={"Authorization": "<PREFIX>" + os.environ.get("GWKEY", ""),
# 允许 f-string 变体 (f"...")。
_AUTH_RE = re.compile(
    r'"Authorization"\s*:\s*'
    r'(?:f)?"(?P<prefix>[^"]*)"\s*\+\s*'
    # key 表达式优先匹配 os.environ.get(...) 整体 (内含逗号, 不能用 [^,)]+ 截断)
    r'(?P<keyexpr>os\.environ\.get\([^)]*\)|[^,)]+)'
)


def _auth_match() -> re.Match:
    """在 verify.sh 全文里定位 gateway 探测的 Authorization 头构造。"""
    src = VERIFY_SH.read_text(encoding="utf-8")
    matches = list(_AUTH_RE.finditer(src))
    assert matches, (
        "verify.sh 里找不到 gateway 探测的 Authorization 头构造 "
        '(期望形如 headers={"Authorization": "<前缀>" + <key 表达式>})'
    )
    # 本仓只有 gateway 探测一处构造该头; 多处说明发生了分叉, 需人工确认
    assert len(matches) == 1, (
        f"verify.sh 里有 {len(matches)} 处 Authorization 头构造, 期望 1 处"
    )
    return matches[0]


def _prefix() -> str:
    """取出当前源码里的 Authorization 前缀字面量。"""
    return _auth_match().group("prefix")


def _key_expr() -> str:
    """取出当前源码里前缀之后拼接的 key 表达式。"""
    return _auth_match().group("keyexpr")


def _prefix_ok(header_expr: str) -> bool:
    """按 G1/G2/G4 口径判定一个 Authorization 头构造表达式是否合规。"""
    m = re.search(
        r'"Authorization"\s*:\s*(?:f)?"(?P<prefix>[^"]*)"', header_expr
    )
    if not m:
        return False
    prefix = m.group("prefix")
    if "*" in prefix:
        return False
    if prefix != EXPECTED_PREFIX:
        return False
    return hashlib.sha256(prefix.encode("utf-8")).hexdigest() == EXPECTED_SHA256


def _key_expr_ok(header_expr: str) -> bool:
    """按 G3 口径判定一个 Authorization 头构造表达式是否仍拼容器内 key。"""
    m = re.search(
        r'"Authorization"\s*:\s*(?:f)?"[^"]*"\s*\+\s*'
        r'(?P<keyexpr>os\.environ\.get\([^)]*\)|[^,)]+)',
        header_expr,
    )
    if not m:
        return False
    key_expr = m.group("keyexpr")
    return "GWKEY" in key_expr and "os.environ.get" in key_expr


# ── G1: 前缀逐字节等于 `Bearer ` + sha256 自证 ──────────────────────────
def test_g1_prefix_is_exactly_bearer_space() -> None:
    """前缀必须逐字节等于 `Bearer ` (7 字节), sha256 与锚点一致。"""
    prefix = _prefix()
    assert prefix == EXPECTED_PREFIX, (
        f"verify.sh gateway 探测的 Authorization 前缀应为 {EXPECTED_PREFIX!r}, "
        f"实际 {prefix!r} —— 占位符/拼写错误会让 gateway 一律 401"
    )
    got = hashlib.sha256(prefix.encode("utf-8")).hexdigest()
    assert got == EXPECTED_SHA256, (
        f"前缀 sha256 自证失败: 期望 {EXPECTED_SHA256}, 实际 {got} "
        f"(前缀={prefix!r}, len={len(prefix.encode('utf-8'))} 字节)"
    )
    assert len(prefix.encode("utf-8")) == 7, (
        f"前缀应为 7 字节 (6 字母 + 1 空格), 实际 {len(prefix.encode('utf-8'))}"
    )


# ── G2: 前缀里零星号 ────────────────────────────────────────────────────
def test_g2_prefix_has_no_asterisk() -> None:
    """占位符 `***` 绝不能进到运行时头里。"""
    prefix = _prefix()
    assert "*" not in prefix, (
        f"Authorization 前缀含占位符星号: {prefix!r} —— "
        "线上会发出字面 `***<key>`, gateway 回 401, 自检恒红"
    )


# ── G3: 前缀之后仍须拼容器内 key (不得写死真 key) ───────────────────────
def test_g3_prefix_is_concatenated_with_container_key() -> None:
    """头 = 前缀 + 容器内 API_SERVER_KEY; 不能退化成写死某个 key。"""
    key_expr = _key_expr()
    assert "GWKEY" in key_expr, (
        f"Authorization 头未拼接容器内 key, 实际拼接表达式 {key_expr!r} —— "
        "必须取容器内 API_SERVER_KEY (经 docker exec -e 传入)"
    )
    assert "os.environ.get" in key_expr, (
        f"Authorization 头未从环境读取 key, 实际 {key_expr!r}"
    )


# ── G4: 大小写敏感 (真机 401 实测) ─────────────────────────────────────
def test_g4_prefix_is_case_sensitive_bearer() -> None:
    """`bearer ` / `BEARER ` 一律判红 —— gateway 大小写敏感。"""
    prefix = _prefix()
    assert prefix == "Bearer ", (
        f"Authorization 前缀大小写不符: {prefix!r} —— "
        "真机实测 `bearer <key>` 回 401 (evidence-batch-d-s5/08_auth_variants.txt)"
    )


# ── G5: 变异反证 (证明断言不是恒真) ───────────────────────────────────
def test_g5_mutation_counterproof() -> None:
    """把前缀改回占位符/去空格/改小写/不拼 key, 判据必须转红。"""
    src = VERIFY_SH.read_text(encoding="utf-8")
    original = _auth_match().group(0)

    # 变异 1: 前缀退回占位符 `***`
    mutant1 = original.replace('"Bearer "', '"***"')
    assert mutant1 != original
    assert not _prefix_ok(mutant1), "变异 1 (前缀退回占位符) 未被判红"

    # 变异 2: 丢掉 Bearer 后的半角空格
    mutant2 = original.replace('"Bearer "', '"Bearer"')
    assert mutant2 != original
    assert not _prefix_ok(mutant2), "变异 2 (缺半角空格) 未被判红"

    # 变异 3: 大小写改成小写 bearer
    mutant3 = original.replace('"Bearer "', '"bearer "')
    assert mutant3 != original
    assert not _prefix_ok(mutant3), "变异 3 (小写 bearer) 未被判红"

    # 变异 4: 不再拼接容器内 key (退化成写死前缀)
    mutant4 = re.sub(
        r'\s*\+\s*os\.environ\.get\("GWKEY",\s*""\)', "", original
    )
    assert mutant4 != original
    assert "GWKEY" not in mutant4
    assert not _key_expr_ok(mutant4), "变异 4 (不拼容器内 key) 未被判红"

    # 变异 5: 前缀整个去掉 (退化成裸 key, 无 Bearer 前缀) —— 真机 401
    mutant5 = original.replace('"Bearer "', '""')
    assert mutant5 != original
    assert not _prefix_ok(mutant5), "变异 5 (裸 key 无 Bearer 前缀) 未被判红"

    # 还原确认: 源文件未被变异 (本文件只读, 不写盘)
    assert VERIFY_SH.read_text(encoding="utf-8") == src


# ── G6: 全仓 tracked 文件零星号残留 (卡面「全仓无星号残留」口径) ───────
# 白名单: 四类**正当**的 `***` 用法, 均非运行时 Authorization 头:
#   1. mw/console.py 的注册表 key 掩码 (运行时故意打码, 是功能不是缺陷);
#   2. DEPLOY.md 的 HERMES_WEBUI_PASSWORD 示例占位符 (2026-09-28 起即如此);
#   3. skill / references 里「浏览器回显打码伪影」的经验说明文字;
#   4. tests/test_issue58_tokens.py 里的假 api_key 夹具 (测试用的哑值,
#      断言只看头形状 "Bearer <key>", 不参与真实鉴权)。
# 白名单以「文件 + 行内语义锚点」双重约束, 防止新引入的星号蒙混过关。
_STAR_ALLOWLIST: tuple[tuple[str, str], ...] = (
    ("mw/console.py", '"***" if "key" in k.lower() else v'),
    ("DEPLOY.md", "HERMES_WEBUI_PASSWORD=***"),
    ("deploy/skills/team-work-chain-orchestration/SKILL.en.md",
     "weird emoji / *** not matching source"),
    ("deploy/skills/team-work-chain-orchestration/SKILL.zh-cn.md",
     "`***`（qq-login 实测"),
    ("deploy/skills/team-work-chain-orchestration/SKILL.zh-cn.md",
     "先用 `grep -o '<title>…' file | od -c`"),
    ("deploy/skills/team-work-chain-orchestration/references/"
     "browser-smoke-verify.en.md",
     "weird emoji or `***` that are not in the source"),
    ("deploy/skills/team-work-chain-orchestration/references/"
     "browser-smoke-verify.zh-cn.md",
     "怪 emoji 或 `***`"),
    ("tests/test_issue58_tokens.py", '"api_key": "***"'),
)


def _tracked_files() -> list[str]:
    """列出 git 追踪的文件 (跳过 node_modules / .venv 等未追踪目录)。

    同时跳过**本守护文件自身**: 它必然含有 `***` (文档里描述占位符、
    变异反证里构造占位符、白名单里登记其它文件的占位符), 那是测试夹具
    而不是仓库残留。本文件若不排除自己, 提交入库后 G6/G7 会把自己判红。
    """
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    self_rel = pathlib.Path(__file__).resolve().relative_to(ROOT).as_posix()
    return [
        f for f in out.stdout.splitlines()
        if f and f != self_rel
    ]


def test_g6_no_asterisk_residue_in_tracked_files() -> None:
    """全仓 tracked 文件里除白名单外, 不得再有 `***` 残留。

    卡面口径「全仓无星号残留」的守门: 任何新引入的星号 (尤其又落进
    Authorization 头这类运行时位置) 都会在这里转红。
    """
    offenders: list[str] = []
    for rel in _tracked_files():
        p = ROOT / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # 二进制/非 UTF-8 文件不参与文本断言
        for lineno, line in enumerate(text.splitlines(), 1):
            if "***" not in line:
                continue
            allowed = any(
                rel == path and anchor in line
                for path, anchor in _STAR_ALLOWLIST
            )
            if not allowed:
                offenders.append(f"{rel}:{lineno}: {line.strip()[:110]}")
    assert not offenders, (
        "仓库里出现未登记的 `***` 残留 (占位符/打码字面量):\n  "
        + "\n  ".join(offenders)
        + "\n若是正当用法, 请登记进 _STAR_ALLOWLIST 并说明理由。"
    )


def test_g7_no_authorization_header_carries_asterisk() -> None:
    """任何含 Authorization 的行都不得携带 `***` (运行时头绝不打码)。"""
    offenders: list[str] = []
    for rel in _tracked_files():
        p = ROOT / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if "Authorization" in line and "***" in line:
                offenders.append(f"{rel}:{lineno}: {line.strip()[:110]}")
    assert not offenders, (
        "Authorization 头里出现 `***` 占位符 —— 线上会发出打码字面量, "
        "gateway 回 401:\n  " + "\n  ".join(offenders)
    )
