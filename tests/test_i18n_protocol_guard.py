"""tests/test_i18n_protocol_guard.py — i18n v1.0 协议禁区守护(架构 §8.3 / AC A3 / D1 / AC D4)

测试以下机器可检项:
  G1. en.json / zh-cn.json key 集合完全相等 (AC D1)
  G2. resolve_lang() 缺省 = 'en';非法值 → 'en' (AC §5.2)
  G3. `mw/todo.py` 不 import i18n,不调用 t() (协议字段不被翻译,架构 §2.3)
  G4. `mw/reaper.py` 协议字面量未改 (架构 §8.1 step 5)
  G5. mw/*.py 内 t("...") 字面 key 都在 en.json 里存在 (typos gate)
  G6. tools/i18n_allowlist.txt 存在且非空
  G7. GET /api/lang 返回 {"lang": "<en|zh-cn>"},Cache-Control: no-store,
      无鉴权 (design.md §10)

跑法: 在仓库根 .venv 下 `pytest tests/test_i18n_protocol_guard.py -v`
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

# 让 `import mw.*` 走仓库包(与 conftest 同处理)
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mw.i18n  # noqa: E402


# ──────────────────────────────────────────────────────────────────
# G1: en.json / zh-cn.json key 集合完全相等
# ──────────────────────────────────────────────────────────────────
def test_en_zh_cn_key_set_equal():
    en_p = ROOT / "mw" / "i18n" / "en.json"
    zh_p = ROOT / "mw" / "i18n" / "zh-cn.json"
    assert en_p.exists(), f"missing {en_p}"
    assert zh_p.exists(), f"missing {zh_p}"
    en_keys = set(json.loads(en_p.read_text(encoding="utf-8")).keys())
    zh_keys = set(json.loads(zh_p.read_text(encoding="utf-8")).keys())
    assert en_keys == zh_keys, (
        f"key-set mismatch: only in en={en_keys - zh_keys}, "
        f"only in zh={zh_keys - en_keys}")


# ──────────────────────────────────────────────────────────────────
# G1b: JSON 与 console_ui._LANG_TABLES 交集内 **值** 一致 (issue #121 F-1)
#      原守护只查键集, 值漂移 (ph_url 双表不一致) 无人拦截; 本条堵口。
# ──────────────────────────────────────────────────────────────────
def test_json_vs_lang_tables_value_consistency():
    import mw.console_ui as cui
    for lang, json_name in (("en", "en.json"), ("zh-cn", "zh-cn.json")):
        tbl = cui._LANG_TABLES[lang]
        js = json.loads(
            (ROOT / "mw" / "i18n" / json_name).read_text(encoding="utf-8"))
        shared = set(tbl) & set(js)
        assert shared, f"{lang}: 两表无交集, 守护失效"
        diff = {k: (tbl[k], js[k]) for k in shared if tbl[k] != js[k]}
        assert not diff, f"{lang} 表值漂移: {diff}"


# ──────────────────────────────────────────────────────────────────
# G2: resolve_lang() 缺省/非法值 → en
# ──────────────────────────────────────────────────────────────────
def test_resolve_lang_defaults(monkeypatch):
    monkeypatch.delenv("MW_LANG", raising=False)
    assert mw.i18n.resolve_lang() == "en"
    monkeypatch.setenv("MW_LANG", "")
    # 之 lang 也按 'en'(空字符串不在 SUPPORTED 内)
    assert mw.i18n.resolve_lang() == "en"
    monkeypatch.setenv("MW_LANG", "fr")
    assert mw.i18n.resolve_lang() == "en"
    monkeypatch.setenv("MW_LANG", "zh-cn")
    assert mw.i18n.resolve_lang() == "zh-cn"


# ──────────────────────────────────────────────────────────────────
# G3: mw/todo.py 不 import i18n,不调 t() (协议字段不被翻译)
# ──────────────────────────────────────────────────────────────────
def test_todo_does_not_import_i18n():
    todo_src = (ROOT / "mw" / "todo.py").read_text(encoding="utf-8")
    # 静态 import 守护:
    assert "from .i18n" not in todo_src and "import i18n" not in todo_src, \
        "mw/todo.py 不应 import i18n(协议字段不是文案)"
    # t() 调用(任意 name)也不该出现:
    assert not re.search(r"\bt\s*\(", todo_src), \
        "mw/todo.py 不应调用 t()(协议字段不是文案)"


# ──────────────────────────────────────────────────────────────────
# G4: mw/harvest.py / mw/reaper.py 协议字面量必须保留
# ──────────────────────────────────────────────────────────────────
def test_protocol_literals_intact():
    """架构 §8.1 step 5 + §2.3:协议字面量在 harvest.py / reaper.py 必须未改。
    (harvest.py 才是实际 emit 这些事件的位置;reaper.py 也有 task.stuck)。
    """
    harv = (ROOT / "mw" / "harvest.py").read_text(encoding="utf-8")
    reap = (ROOT / "mw" / "reaper.py").read_text(encoding="utf-8")
    for literal in ("team_work.run_idle", "harvest.complete"):
        assert literal in harv, \
            f"mw/harvest.py 丢失协议字面量 {literal!r} (架构 §8.1 step 5)"
    assert "task.stuck" in reap, \
        "mw/reaper.py 丢失 task.stuck 协议字面量 (架构 §8.1 step 5)"


# ──────────────────────────────────────────────────────────────────
# G5: mw/*.py 内 t("...") 字面 key 都在 en.json 里存在
# ──────────────────────────────────────────────────────────────────
def test_t_call_keys_exist_in_en_json():
    en_keys = set(json.loads(
        (ROOT / "mw" / "i18n" / "en.json").read_text(encoding="utf-8")
    ).keys())
    mw_dir = ROOT / "mw"
    bad: list[str] = []
    for py in mw_dir.glob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        # 抓 `t("...key...")` 或 `t('...key...')` 形式:
        for m in re.finditer(r'\bt\s*\(\s*["\']([^"\']+)["\']\s*', text):
            key = m.group(1)
            if key in en_keys:
                continue
            # 模板占位 / 测试 fixture 不算
            if key.startswith("{") or key.startswith("__"):
                continue
            bad.append(f"{py.name}: t({key!r}) not in en.json")
    assert not bad, "missing i18n keys:\n  " + "\n  ".join(bad)


# ──────────────────────────────────────────────────────────────────
# G6: tools/i18n_allowlist.txt 存在
# ──────────────────────────────────────────────────────────────────
def test_allowlist_exists():
    p = ROOT / "tools" / "i18n_allowlist.txt"
    assert p.exists(), f"missing {p}"
    content = p.read_text(encoding="utf-8")
    assert content.strip(), "allowlist empty"
    # 至少含 TODO 协议字面量(架构 §8.2 必须登记)
    assert "## 当前任务" in content, \
        "allowlist 缺 TODO.md 协议字面量(架构 §8.2)"


# ──────────────────────────────────────────────────────────────────
# G7: GET /api/lang 返回 {"lang": "<en|zh-cn>"},Cache-Control: no-store,
#    无鉴权 (design.md §10)
# ──────────────────────────────────────────────────────────────────
def test_api_lang_endpoint_contract(monkeypatch):
    """直接读 console.py 的请求处理代码片段,无容器也能验契约。"""
    console_text = (ROOT / "mw" / "console.py").read_text(encoding="utf-8")
    # 1) 路由存在(do_GET 内有 `if p == "/api/lang"` 分支)
    assert '"/api/lang"' in console_text, \
        "/api/lang 路由未在 console.py do_GET 注册"
    # 2) Cache-Control: no-store(api-spec §2.1)
    assert '"Cache-Control"' in console_text and "no-store" in console_text, \
        "/api/lang 缺 Cache-Control: no-store 头(api-spec §2.1)"
    # 3) 调 mw.i18n.resolve_lang()(单源)
    assert "resolve_lang" in console_text, \
        "/api-lang 应读 mw.i18n.resolve_lang()(架构 §5.2 单源)"
    # 4) 返回 {"lang": <value>}
    assert re.search(r'json\.dumps\(\s*\{\s*"lang"\s*:', console_text), \
        "/api/lang 响应 body 必须形如 {\"lang\": ...}"


# ──────────────────────────────────────────────────────────────────
# G8: SOUL_TEMPLATE 默认按 resolve_lang() 选模板族(架构 §4.3)
# ──────────────────────────────────────────────────────────────────
def test_soul_template_default_is_language_aware(monkeypatch):
    """console.SOUL_TEMPLATE 默认路径包含语言后缀。"""
    import mw.console as console
    assert ".en.md" in console.SOUL_TEMPLATE or ".zh-cn.md" in console.SOUL_TEMPLATE, \
        f"console.SOUL_TEMPLATE 缺语言后缀: {console.SOUL_TEMPLATE}"


# ──────────────────────────────────────────────────────────────────
# G9: conftest.py 钉 MW_LANG=zh-cn (架构 §6.1, AC D3 零回归锚)
# ──────────────────────────────────────────────────────────────────
def test_conftest_pins_zh_cn():
    p = ROOT / "tests" / "conftest.py"
    assert p.exists(), "missing tests/conftest.py (架构 §6.1 / AC D3)"
    src = p.read_text(encoding="utf-8")
    assert 'MW_LANG' in src and 'zh-cn' in src, \
        "conftest.py 必须把 MW_LANG 钉为 zh-cn (D3 零回归锚)"


# ──────────────────────────────────────────────────────────────────
# G10: ast_diff_i18n.py 与 i18n_allowlist.txt 存在 + 路径契约
# ──────────────────────────────────────────────────────────────────
def test_ast_diff_script_exists():
    p = ROOT / "tools" / "ast_diff_i18n.py"
    assert p.exists(), "missing tools/ast_diff_i18n.py (架构 §8.1)"
    src = p.read_text(encoding="utf-8")
    # 关键算法标记:
    for marker in ("TCallNormalizer", "NORMALIZED_T_KEY",
                   "violations=0", "git", "ast.parse"):
        assert marker in src, f"ast_diff_i18n.py 缺关键片段 {marker!r}"


# ──────────────────────────────────────────────────────────────────
# G11: install.sh / main_entry.sh / verify.sh 装载 i18n.sh (架构 §4.4)
# ──────────────────────────────────────────────────────────────────
def test_deploy_scripts_use_i18n_lib():
    lib = ROOT / "deploy" / "lib" / "i18n.sh"
    assert lib.exists(), "missing deploy/lib/i18n.sh (架构 §4.4 / AR6)"
    for script in ("install.sh", "verify.sh", "main_entry.sh"):
        p = ROOT / "deploy" / script
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8")
        assert "i18n.sh" in src, f"deploy/{script} 未装载 i18n.sh"
    # install.sh 必须有 LANG 校验 + deploy_lang 写盘(架构 §3.2/§3.3)
    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "i18n_validate" in install, "install.sh 缺 i18n_validate (架构 §3.2)"
    assert "deploy_lang" in install, "install.sh 缺 deploy_lang 写盘 (架构 §3.3)"
    # main_entry.sh 必须 export MW_LANG
    main_entry = (ROOT / "deploy" / "main_entry.sh").read_text(encoding="utf-8")
    assert "export MW_LANG" in main_entry, "main_entry.sh 缺 MW_LANG export (架构 §3.3)"


# ──────────────────────────────────────────────────────────────────
# G12: SOUL 模板族已 split(架构 §4.1)
# ──────────────────────────────────────────────────────────────────
def test_soul_template_family_split():
    """main-soul / soul_template / deploy skills 都按语言后缀切分。"""
    must_exist = [
        ("docs/main-soul.en.md", "docs/main-soul.zh-cn.md"),
        ("mw/soul_template.en.md", "mw/soul_template.zh-cn.md"),
        # deploy skills (架构 §4.1 列出 4 个)
        ("deploy/skills/mw-task-dispatch/SKILL.en.md",
         "deploy/skills/mw-task-dispatch/SKILL.zh-cn.md"),
        ("deploy/skills/team-work-chain-advance/SKILL.en.md",
         "deploy/skills/team-work-chain-advance/SKILL.zh-cn.md"),
        ("deploy/skills/team-work-chain-orchestration/SKILL.en.md",
         "deploy/skills/team-work-chain-orchestration/SKILL.zh-cn.md"),
        ("deploy/skills/worker-message-relay/SKILL.en.md",
         "deploy/skills/worker-message-relay/SKILL.zh-cn.md"),
    ]
    missing = []
    for a, b in must_exist:
        if not (ROOT / a).exists():
            missing.append(a)
        if not (ROOT / b).exists():
            missing.append(b)
    assert not missing, "missing split-family files:\n  " + "\n  ".join(missing)


# ──────────────────────────────────────────────────────────────────
# G13: B2 机检裁决(main-soul.en.md 代码块外 CJK=0)
# ──────────────────────────────────────────────────────────────────
def test_main_soul_en_b2_check():
    """架构 §11.1 裁决: B2 = CJK outside fenced code blocks = 0。
    inline code (`x`) 与 HTML 注释 (<!-- ... -->) 内的 CJK 也允许
    (协议字面量可放在 inline code 里引用)。
    """
    import re
    p = ROOT / "docs" / "main-soul.en.md"
    if not p.exists():
        pytest.skip("main-soul.en.md missing")
    text = p.read_text(encoding="utf-8")
    lines = text.split("\n")
    in_fence = False
    violations = []
    for i, line in enumerate(lines, 1):
        if re.match(r"^```", line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # 剥 HTML 注释
        no_comment = re.sub(r"<!--.*?-->", "", line)
        # 剥 inline code
        no_inline = re.sub(r"`[^`]+`", "", no_comment)
        cjk = re.findall(r"[\u4e00-\u9fff]+", no_inline)
        if cjk:
            violations.append(f"line {i}: {line.strip()[:100]}")
    assert not violations, (
        "B2 violations (CJK outside fence+inline+html-comment):\n  "
        + "\n  ".join(violations))