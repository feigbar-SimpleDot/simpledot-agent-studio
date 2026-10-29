"""issue #147 守护: orchestration skill 的「建卡前零研判」冲突措辞不得回潮。

背景 (PLAN-I §4): #147 的 SOUL 侧 (docs/main-soul.{zh-cn,en}.md) 已落地
「建卡前窗口只允许秒级取证」新纪律, 但 skill 文本里残留的许可性旧条款
(「允许且鼓励预定位」等) 更具体、更可操作, LLM 会跟随 skill 而非 SOUL ⇒
新 run 的「需求→首卡」窗口实测仍超 6.3 倍。本文件把冲突措辞从文本层移除后
留下机械可判的守护, 防后人把旧口径写回来。

守护形态 (PLAN-I §4.3):
  G1  冲突措辞清单: 10 个关键词在 deploy/skills/ 全部 SKILL 文件里逐个 0 命中
      (扫全 skills 目录而非只扫 orchestration 两份 ⇒ 「不误伤其它 skill」
      与「冲突词不得回潮」一条断言同时钉住; 逐词参数化, 变异时精确指认哪个词)。
  G2  正向口径在位: 改写后的新口径必须存在 (防「删词了事」留悬空段落)。
  G3  中英成对存在: 五处改写, 每处的 zh/en 语义锚必须成对出现 (防单边改动)。
  G4  G6 白名单锚点保护: test_issue109 的 G6 有 3 条锚在这两份 SKILL 文件里,
      改写不得触碰其所在段落 ⇒ 断言锚文本仍在 (O-5)。
  G5  变异反证: 往内存副本塞回冲突措辞 / 删掉正向口径 ⇒ 守护转红 (自证有牙齿)。

⚠ 为什么不用「git diff 相对基线 SHA 断言改动面恰为 2 文件」的形态 (PLAN-I
§4.3 断言 4 的原案): 该形态作为**常驻**测试会在后续任何合法 commit (s5/s6
及以后) 上转红, 属自毁式断言; 批级改动面核对由 s7/s8 卡承担。常驻守护改用
G1 的全 skills 目录扫描 —— 语义等价 (冲突词在任何 skill 里出现都拦), 且
不随时间失效。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = ROOT / "deploy" / "skills"
ZH = SKILL_DIR / "team-work-chain-orchestration" / "SKILL.zh-cn.md"
EN = SKILL_DIR / "team-work-chain-orchestration" / "SKILL.en.md"

# ── G1: 冲突措辞清单 (改前基线各 1 次, 改后必须 0) ─────────────────────
# (文件侧, 关键词) —— zh 区分大小写无意义 (中文), en 按 issue 口径大小写不敏感。
CONFLICT_KEYWORDS: tuple[tuple[str, str, bool], ...] = (
    ("zh", "允许且鼓励", False),
    ("zh", "有界的现场实测基线", False),
    ("zh", "代码预定位", False),
    ("zh", "预定位的根因", False),
    ("zh", "实测基线要有预算", False),
    ("en", "allowed and encouraged", True),
    ("en", "bounded measured baseline", True),
    ("en", "pre-locating code", True),
    ("en", "pre-located", True),
    ("en", "baseline has a budget", True),
)
_SIDE_FILE = {"zh": ZH, "en": EN}


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _norm(text: str) -> str:
    """折叠空白: en 文件硬换行, 语义锚跨行 ⇒ 比对前把连续空白压成单空格。"""
    return re.sub(r"\s+", " ", text)


def _count(text: str, kw: str, ignore_case: bool) -> int:
    flags = re.IGNORECASE if ignore_case else 0
    return len(re.findall(re.escape(kw), text, flags))


def _assert_no_conflict(text: str, where: str, side: str | None = None) -> None:
    """核心断言: 该文本里 10 个冲突关键词逐个 0 命中 (按各自语言侧)。"""
    side = side or ("zh" if where.endswith("zh-cn.md") else "en")
    for kw_side, kw, ic in CONFLICT_KEYWORDS:
        if kw_side != side:
            continue
        n = _count(text, kw, ic)
        assert n == 0, f"{where} 出现冲突措辞 {kw!r} ({n} 次), 与 SOUL 新纪律冲突"


# ── G2: 正向口径在位 (防删词了事) ──────────────────────────────────────
POSITIVE_PHRASES: tuple[tuple[str, str], ...] = (
    # (语言侧, 语义锚) —— zh 直接子串匹配; en 经 _norm 后匹配。
    ("zh", "由首张调研/研判卡产出"),
    ("zh", "秒级取证要有预算"),
    ("en", "produced by the first research/analysis card"),
    ("en", "second-scale intake has a budget"),
)


def _assert_positive_in_place(text: str, where: str) -> None:
    side = "zh" if where.endswith("zh-cn.md") else "en"
    norm = _norm(text)
    for pside, phrase in POSITIVE_PHRASES:
        if pside != side:
            continue
        pat = re.compile(re.escape(phrase), re.IGNORECASE if side == "en" else 0)
        assert pat.search(norm), f"{where} 缺少正向口径 {phrase!r} (疑似只删词未换口径)"


# ── G3: 中英成对语义锚 (五处改写, 逐对) ────────────────────────────────
BILINGUAL_PAIRS: tuple[tuple[str, str, str], ...] = (
    # (对名, zh 锚, en 锚)
    ("C1/E1 秒级取证", "秒级取证", "second-scale intake"),
    ("C2/E3 main 不自行定位", "main 不自行定位代码",
     "main does not locate code itself"),
    ("C3/E2 取证预算", "秒级取证要有预算", "second-scale intake has a budget"),
    ("C4/E4 下发前资料准备", "拉 issue 原文与评论/下载附件/读 PROJECT-MEMORY",
     "pulling issue text and comments, downloading attachments, "
     "reading PROJECT-MEMORY"),
    ("C5/E5 结论转述", "随该卡结论转述", "relayed with that card's conclusion"),
)


# ── G4: G6 白名单锚点 (test_issue109 的 3 条, 改写不得触碰) ────────────
# ⚠ 锚串含三字符星号, 但本守护文件自身会被 G6 全仓扫描 ⇒ 源码里不得出现
# 字面三星串 (G6 只豁免 test_issue109 自己), 故用 `"*" * 3` 运行时拼接。
_STARS = "*" * 3
G6_ANCHORS: tuple[tuple[Path, str], ...] = (
    (EN, f"weird emoji / {_STARS} not matching source"),
    (ZH, f"`{_STARS}`（qq-login 实测"),
    (ZH, "先用 `grep -o '<title>…' file | od -c`"),
)


# ═══ 用例 ════════════════════════════════════════════════════════════

@pytest.mark.parametrize("side,keyword,icase", CONFLICT_KEYWORDS,
                         ids=[f"{s}:{k}" for s, k, _ in CONFLICT_KEYWORDS])
def test_conflict_keyword_absent(side, keyword, icase):
    """G1: 冲突关键词在对应 SKILL 文件里 0 命中。"""
    text = _read(_SIDE_FILE[side])
    assert _count(text, keyword, icase) == 0, (
        f"{_SIDE_FILE[side].name} 回潮冲突措辞 {keyword!r}")


def test_conflict_keywords_absent_across_all_skills():
    """G1 扩面: deploy/skills/ 下任何 SKILL 文件都不得出现冲突措辞
    (同时钉住「不误伤其它 skill」—— 冲突词出现在别的 skill 里同样拦)。"""
    offenders = []
    for p in sorted(SKILL_DIR.rglob("SKILL.*.md")):
        text = _read(p)
        for side, kw, ic in CONFLICT_KEYWORDS:
            if _count(text, kw, ic):
                offenders.append(f"{p.relative_to(ROOT)}: {kw!r}")
    assert not offenders, "冲突措辞出现在 skill 目录: " + "; ".join(offenders)


@pytest.mark.parametrize("side,phrase", POSITIVE_PHRASES)
def test_positive_wording_in_place(side, phrase):
    """G2: 改写后的正向口径必须在位 (防只删词不留新纪律)。"""
    p = _SIDE_FILE[side]
    pat = re.compile(re.escape(phrase), re.IGNORECASE if side == "en" else 0)
    assert pat.search(_norm(_read(p))), (
        f"{p.name} 缺少正向口径 {phrase!r}")


@pytest.mark.parametrize("pair,zh_anchor,en_anchor", BILINGUAL_PAIRS,
                         ids=[p for p, _, _ in BILINGUAL_PAIRS])
def test_bilingual_pair_present(pair, zh_anchor, en_anchor):
    """G3: 每处改写的中英语义锚成对存在 (防单边改动)。"""
    zh_text = _norm(_read(ZH))
    en_text = _norm(_read(EN))
    assert zh_anchor in zh_text, f"{pair}: zh 侧缺锚 {zh_anchor!r}"
    assert en_anchor.lower() in en_text.lower(), f"{pair}: en 侧缺锚 {en_anchor!r}"


@pytest.mark.parametrize("path,anchor", G6_ANCHORS,
                         ids=[f"{p.name}:{a[:16]}" for p, a in G6_ANCHORS])
def test_g6_whitelist_anchors_untouched(path, anchor):
    """G4: test_issue109 G6 白名单的 3 条锚必须原样在位 (O-5)。"""
    assert anchor in _read(path), (
        f"{path.name} 的 G6 白名单锚 {anchor!r} 被改动 ⇒ 会连带 G6 转红")


# ── G5: 变异反证 (常驻自证: 断言对真实缺陷会转红) ──────────────────────

def test_mutation_reintroducing_zh_conflict_breaks_guard():
    """变异: 往 zh 内存副本塞回「允许且鼓励」⇒ 守护转红。"""
    mutated = _read(ZH).replace(
        "main 在建卡前只做**秒级取证**",
        "main 在建卡前只做**秒级取证**（允许且鼓励）")
    assert mutated != _read(ZH), "变异体未生效 (原文形态与预期不符)"
    with pytest.raises(AssertionError):
        _assert_no_conflict(mutated, "SKILL.zh-cn.md (mutated)", side="zh")


def test_mutation_reintroducing_en_conflict_breaks_guard():
    """变异: 往 en 内存副本塞回 `allowed and encouraged` ⇒ 守护转红。"""
    mutated = _read(EN).replace(
        "That is build positioning",
        "Writing hints is allowed and encouraged. That is build positioning")
    assert mutated != _read(EN), "变异体未生效 (原文形态与预期不符)"
    with pytest.raises(AssertionError):
        _assert_no_conflict(mutated, "SKILL.en.md (mutated)", side="en")


def test_mutation_dropping_positive_wording_breaks_guard():
    """变异: 删掉 zh 正向口径 (只删词了事) ⇒ G2 守护转红。"""
    mutated = _read(ZH).replace("由首张调研/研判卡产出", "另行产出")
    assert mutated != _read(ZH), "变异体未生效 (原文形态与预期不符)"
    with pytest.raises(AssertionError):
        _assert_positive_in_place(mutated, "SKILL.zh-cn.md (mutated)")
