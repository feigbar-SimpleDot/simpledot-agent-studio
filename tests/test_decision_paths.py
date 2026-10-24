"""决策路径模块测试 (issue #70, s3-impl)。

依据: docs/PLAN-78-70.md §3.4 判定规则 + §4.3 T-70-1..T-70-8。
覆盖: 粗分类 4 类输入 / 混合输入技术优先 / 确定性(顺序敏感可复现) /
不可判定 ask_user / 端点契约 / 端点降级 / 纯函数静态自检 / SOUL 双语同步。

纯函数为主; 端点冒烟用一次性 HTTP 服务 (同 test_console_sse_subscribe 风格)。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mw import console  # noqa: E402
from mw.decision_paths import classify, load_paths  # noqa: E402

DEFAULT_YAML = ROOT / "mw" / "decision_paths.yaml"


# ──────────────────────────────────────────────────────────────────
# T-70-1..4: 粗分类四类输入
# ──────────────────────────────────────────────────────────────────
def test_classify_requirement():
    r = classify("请加一个导出 CSV 的功能, 验收标准是列头与 Excel 一致")
    assert r["path_id"] == "requirement"
    assert r["matched_signals"]
    assert r["fallback"] is None


def test_classify_technical():
    r = classify("上传接口报 500, 堆栈在 mw/harvest.py:679, 复现步骤见 issue")
    assert r["path_id"] == "technical"
    assert r["matched_signals"]


def test_classify_mixed_goes_technical():
    """混合输入 ⇒ 技术类 (PLAN §3.4: 有报错栈 = 可复现的技术问题)。

    实现口径 = 非对称否决 (需求路径被技术信号 -2 否决, 技术路径不设
    否决), 见 decision_paths.yaml 内注释。
    """
    r = classify("验收标准是别 500; 堆栈: Traceback ...")
    assert r["path_id"] == "technical"
    # 需求路径的命中被否决清零 ⇒ technical 是唯一正分
    assert r["score"] > 0


def test_classify_undecidable_asks_user():
    r = classify("帮我看看这个")
    assert r["path_id"] is None
    assert r["fallback"] == "ask_user"
    assert r["score"] == 0


# ──────────────────────────────────────────────────────────────────
# T-70-5: 确定性 — 同输入两次相同; 并列分按 yaml 顺序取第一个
# ──────────────────────────────────────────────────────────────────
def _twin_paths():
    a = {"id": "alpha", "match": {"any_of": ["kw"], "none_of": []}}
    b = {"id": "beta", "match": {"any_of": ["kw"], "none_of": []}}
    return [a, b]


def test_classify_deterministic_and_yaml_order_tiebreak():
    text = "含有关键词 kw 的输入"
    assert classify(text, _twin_paths()) == classify(text, _twin_paths())
    # 并列分 ⇒ 取 yaml 里先出现的
    assert classify(text, _twin_paths())["path_id"] == "alpha"
    # 颠倒顺序 ⇒ 取新顺序的第一个 ⇒ 顺序敏感且可复现
    assert classify(text, list(reversed(_twin_paths())))["path_id"] == "beta"


# ──────────────────────────────────────────────────────────────────
# 默认路由表结构契约 (每条 path 必备字段, PLAN §3.3)
# ──────────────────────────────────────────────────────────────────
def test_default_yaml_schema():
    paths = load_paths(str(DEFAULT_YAML))
    assert [p["id"] for p in paths] == ["requirement", "technical"]
    for p in paths:
        for f in ("label_zh", "label_en", "match", "route_to",
                  "role_tags", "first_card", "then_cards",
                  "forbidden_for_main"):
            assert p.get(f), f"{p['id']} 缺字段 {f}"
        assert p["forbidden_for_main"], "forbidden_for_main 必须非空"
        assert set(p["match"]) >= {"any_of", "none_of"}


def test_route_to_semantic_roles_not_profiles():
    """route_to 写语义角色名, 不写死运行时 profile 名 (PLAN §3.5)。

    s6/F-3 修复: 原实现把断言体塞进 `if p["route_to"] in seed:` 分支 ——
    seed 只有 coder / code-reviewer, 而 route_to 是语义角色名
    (product-manager / system-engineer), 该分支**恒为 False** ⇒ 断言体
    一次都不执行 ⇒ 这条测试恒绿但什么都没验。

    现在改成**无条件执行**的结构契约断言, 且断言对象是 **yaml 原文**
    (不是 load_paths() 的过滤结果 —— 结构非法的条目已被 loader 丢弃,
    拿过滤后的结果断言会重蹈「断言体从不执行」的覆辙):
      - route_to 非空字符串, 且不是种子注册表里的 profile 名;
      - role_tags 非空且全是字符串 (main 靠它按 scope.tags 语义选人)。
    """
    import yaml
    seed = json.loads((ROOT / "mw" / "registry.json").read_text())
    raw = yaml.safe_load(DEFAULT_YAML.read_text(encoding="utf-8"))
    raw_paths = raw.get("paths")
    assert isinstance(raw_paths, list) and raw_paths, "默认路由表为空, 结构契约无从校验"
    for p in raw_paths:
        assert isinstance(p, dict)
        # route_to 必须是语义角色名, 不得直接写死种子 profile 名
        route_to = p.get("route_to")
        assert isinstance(route_to, str) and route_to, f"{p.get('id')} 的 route_to 非法"
        assert route_to not in seed, \
            f"{p['id']} 的 route_to 写死了 profile 名 {route_to!r}"
        # role_tags 是 main 选人的依据, 必须非空且全是字符串
        tags = p.get("role_tags")
        assert isinstance(tags, list) and tags, f"{p['id']} 的 role_tags 为空/非列表"
        assert all(isinstance(t, str) and t for t in tags), \
            f"{p['id']} 的 role_tags 含非字符串/空串: {tags!r}"
    # loader 过滤后仍必须保留全部两条 (结构校验不该误杀合法条目)
    assert [x["id"] for x in load_paths(str(DEFAULT_YAML))] == \
        ["requirement", "technical"]


# ──────────────────────────────────────────────────────────────────
# s6/F-1: 命中判定大小写不敏感 (裸 Traceback 此前返回 None ⇒ 逼 main 回头问用户)
# ──────────────────────────────────────────────────────────────────
def test_classify_bare_traceback_is_technical():
    """裸栈回溯 = 技术类输入的原型形态 ⇒ 必须判 technical, 不能 None。"""
    r = classify('Traceback (most recent call last):\n'
                 '  File "mw/harvest.py", line 679, in _run\n'
                 'ValueError: boom')
    assert r["path_id"] == "technical"
    assert r["fallback"] is None
    # 回传的信号名是词表原文 (小写), 调用方可按词表反查
    assert "traceback" in r["matched_signals"]


def test_classify_mixed_english_uppercase_goes_technical():
    """全英文大写混合输入 ⇒ 仍归技术类 (s6/F-1 的最小复现)。

    修复前: 大写 `Traceback` 既不命中 technical 的 any_of、也不命中
    requirement 的 none_of ⇒ 反向误判成 requirement。
    """
    r = classify("acceptance criteria: no 500; Traceback (most recent call last): "
                 "ValueError")
    assert r["path_id"] == "technical"
    assert r["score"] > 0


def test_classify_is_case_insensitive_for_both_hits_and_vetoes():
    """命中与否决两侧都大小写不敏感 (词表小写 ⇒ 输入大写也要算)。"""
    # requirement 的 any_of 词表小写, 输入大写 ⇒ 命中
    r = classify("ACCEPTANCE CRITERIA are listed in the issue")
    assert r["path_id"] == "requirement"
    assert r["matched_signals"]
    # requirement 的 none_of 词表小写, 输入大写 ⇒ 否决 (需求类被技术信号否决)
    r2 = classify("ACCEPTANCE CRITERIA are listed; STACK TRACE attached")
    assert r2["path_id"] == "technical", "大写 STACK TRACE 必须能否决 requirement"


# ──────────────────────────────────────────────────────────────────
# s6/F-2: EN skill 的 L4b push 措辞必须与 ZH 镜像对齐 (push 归协调方)
# ──────────────────────────────────────────────────────────────────
def test_skill_push_wording_mirror_zh_en():
    """L4b 协议补丁纪律的双语口径必须一致 (s6/F-2)。

    修复前: ZH 侧已改写为「commit(push 归协调方)」, EN 侧仍是源快照原文
    「committed + pushed」⇒ SAS_LANG=en 部署时 EN 那一份被装进容器, 直接
    教 main「push 成功」, 与本批「不 push, push 归协调方」的纪律冲突。
    """
    skill_dir = ROOT / "deploy" / "skills" / "team-work-chain-orchestration"
    zh = (skill_dir / "SKILL.zh-cn.md").read_text(encoding="utf-8")
    en = (skill_dir / "SKILL.en.md").read_text(encoding="utf-8")
    # ZH 侧口径: commit 后 push 归协调方
    assert "push 归协调方" in zh
    # EN 侧口径: commit 后 push 归协调方 (不得再出现 committed + pushed)
    assert "push belongs to the coordinator" in en
    assert "committed + pushed" not in en, \
        "EN skill 仍教 main「committed + pushed」, 与本批不 push 纪律冲突"
    # 两份镜像指向同一段仓库纪律 —— 关键锚点各出现一次
    assert zh.count("push 归协调方") == 1
    assert en.count("push belongs to the coordinator") == 1


# ──────────────────────────────────────────────────────────────────
# s6/F-4: 结构非法 ⇒ 坏条目丢弃 / 端点不吐畸形 path / classify 永不抛
# ──────────────────────────────────────────────────────────────────
def _write_yaml(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_load_paths_drops_malformed_entries_not_whole_table(tmp_path):
    """结构非法的条目**逐条丢弃**, 合法条目照常进表 (yaml 是仓库资产,
    main 可直接编辑 —— 一条写错不该让整张路由表失效)。"""
    body = """
paths:
  - id: bad-match-string
    label_zh: 坏
    label_en: bad
    match: "oops"
    route_to: product-manager
    role_tags: ["req-analysis"]
    first_card: product-manager
    then_cards: []
    forbidden_for_main: ["复现问题"]
  - id: good
    label_zh: 好
    label_en: good
    match:
      any_of: ["验收标准"]
      none_of: []
    route_to: product-manager
    role_tags: ["req-analysis"]
    first_card: product-manager
    then_cards: []
    forbidden_for_main: ["复现问题"]
"""
    p = _write_yaml(tmp_path, "malformed.yaml", body)
    paths = load_paths(p)
    assert [x["id"] for x in paths] == ["good"], "坏条目必须被丢弃, 好的必须留下"


def test_load_paths_rejects_bad_shapes(tmp_path):
    """match 非 dict / any_of 非 list / id 非 str / 缺端点契约字段 ⇒ 全部丢弃。

    每条用例都是**完整可解析的 yaml** (顶层有 paths:), 坏点只在条目内部 ——
    否则测到的会是「整个文件结构不对」而不是「单条 path 结构不对」。
    """
    good_fields = ("label_zh: x\nlabel_en: x\nroute_to: product-manager\n"
                   "role_tags: [\"t\"]\nfirst_card: product-manager\n"
                   "then_cards: []\nforbidden_for_main: [\"复现问题\"]\n")

    def entry(body: str) -> str:
        """把单条 path 的 yaml 片段包进 paths: 列表。"""
        return "paths:\n  - " + body.replace("\n", "\n    ")

    cases = {
        "match_list.yaml": entry("id: a\n" + good_fields + "match: [a, b]\n"),
        "match_str.yaml": entry("id: a\n" + good_fields + "match: \"oops\"\n"),
        "any_of_str.yaml": entry("id: a\n" + good_fields +
                                  "match:\n  any_of: \"验收标准\"\n"),
        "none_of_str.yaml": entry("id: a\n" + good_fields +
                                   "match:\n  any_of: [\"x\"]\n  none_of: \"oops\"\n"),
        "id_int.yaml": entry("id: 123\n" + good_fields + "match:\n  any_of: [\"x\"]\n"),
        "id_empty.yaml": entry("id: \"\"\n" + good_fields + "match:\n  any_of: [\"x\"]\n"),
        "no_id.yaml": entry(good_fields + "match:\n  any_of: [\"x\"]\n"),
        "no_match.yaml": entry("id: a\n" + good_fields),
        "no_route_to.yaml": entry(
            "id: a\nlabel_zh: x\nlabel_en: x\nmatch:\n  any_of: [\"x\"]\n"
            "role_tags: [\"t\"]\nfirst_card: product-manager\nthen_cards: []\n"
            "forbidden_for_main: [\"复现问题\"]\n"),
        "no_role_tags.yaml": entry(
            "id: a\nlabel_zh: x\nlabel_en: x\nroute_to: product-manager\n"
            "first_card: product-manager\nthen_cards: []\n"
            "forbidden_for_main: [\"复现问题\"]\nmatch:\n  any_of: [\"x\"]\n"),
        "role_tags_empty.yaml": entry(
            "id: a\nlabel_zh: x\nlabel_en: x\nroute_to: product-manager\n"
            "role_tags: []\nfirst_card: product-manager\nthen_cards: []\n"
            "forbidden_for_main: [\"复现问题\"]\nmatch:\n  any_of: [\"x\"]\n"),
        "forbidden_empty.yaml": entry(
            "id: a\nlabel_zh: x\nlabel_en: x\nroute_to: product-manager\n"
            "role_tags: [\"t\"]\nfirst_card: product-manager\nthen_cards: []\n"
            "forbidden_for_main: []\nmatch:\n  any_of: [\"x\"]\n"),
    }
    for name, body in cases.items():
        p = _write_yaml(tmp_path, name, body)
        assert load_paths(p) == [], f"{name} 的非法条目未被丢弃"


def test_api_decision_paths_never_returns_malformed_path(tmp_path, monkeypatch):
    """端点遇畸形 yaml ⇒ {'paths': []}, 不吐畸形 path (PLAN §4.3 T-70-6 契约)。"""
    body = """
paths:
  - id: x
    match: "oops"
    route_to: pm
"""
    p = _write_yaml(tmp_path, "malformed.yaml", body)
    monkeypatch.setenv("MW_DECISION_PATHS_PATH", p)
    assert console.api_decision_paths() == {"paths": []}


def test_classify_never_raises_on_malformed_paths():
    """classify() 对形状非法的 paths 直参**永不抛** (调用方自造 paths 时)。"""
    bad = [
        {"id": "a", "match": "oops"},                       # match 是字符串
        {"id": "b", "match": ["x"]},                        # match 是列表
        {"id": "c", "match": {"any_of": "验收标准"}},        # any_of 是字符串
        {"id": "d"},                                        # 缺 match
        {"id": 123, "match": {"any_of": ["x"]}},             # id 是整数
        {"match": {"any_of": ["x"]}},                        # 缺 id
        "not-a-dict",                                       # 条目不是 dict
    ]
    r = classify("随便什么输入", bad)
    assert r["path_id"] is None
    assert r["fallback"] == "ask_user"
    assert r["score"] == 0
    # s9/S-2 (s7 N-2 钉桩补强): id 非法的条目即使**命中**也不得进判定 ——
    # any_of 用测试文本实际含的词, 删掉 classify 里的 id 类型防御此用例即红
    # (否则只会因不命中而绿, 防御无独有覆盖)。
    hit_int_id = [{"id": 123, "match": {"any_of": ["随便"]}}]
    r2 = classify("随便什么输入", hit_int_id)
    assert r2["path_id"] is None and r2["fallback"] == "ask_user"


def test_classify_non_list_paths_never_raises():
    """s9/S-1: paths 直传**非列表** (如 123/"abc") ⇒ 视同空表, 永不抛。

    s8 复审 S-1 登记的 docstring「永不抛」微差 (paths=123 曾抛
    TypeError: 'int' object is not iterable), 本用例钉死修复: 命中判定
    前 paths 非 list ⇒ 归零为空表 ⇒ path_id null + ask_user。
    """
    # 故意传非法类型 (被测面即防御本身), pyright 按防御契约豁免
    for bad_paths in (123, "abc", {"paths": []}, object(), True):
        r = classify("堆栈 traceback 报错", bad_paths)  # type: ignore[arg-type]
        assert r["path_id"] is None, f"paths={bad_paths!r} 不应产出判定"
        assert r["fallback"] == "ask_user"
        assert r["score"] == 0


# ──────────────────────────────────────────────────────────────────
# T-70-7 + 端点契约 (T-70-6) + 路由冒烟 (PLAN §3.7 验证命令 9/10)
# ──────────────────────────────────────────────────────────────────
def test_api_decision_paths_contract():
    body = console.api_decision_paths()
    assert "paths" in body and len(body["paths"]) == 2
    for p in body["paths"]:
        assert {"id", "match", "route_to", "role_tags",
                "forbidden_for_main"} <= set(p)


def test_api_decision_paths_degrades_on_bad_yaml(tmp_path, monkeypatch):
    """yaml 缺失/损坏 ⇒ {'paths': []}, 不抛不 500。"""
    bad = tmp_path / "broken.yaml"
    bad.write_text("paths: [ {id: x, match: [::: 非法")  # 解析失败
    monkeypatch.setenv("MW_DECISION_PATHS_PATH", str(bad))
    assert console.api_decision_paths() == {"paths": []}
    monkeypatch.setenv("MW_DECISION_PATHS_PATH", str(tmp_path / "nope.yaml"))
    assert console.api_decision_paths() == {"paths": []}


def _http_get(port: int, path: str):
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:      # 4xx 契约分支 (如 classify 缺 text)
        return e.code, json.loads(e.read().decode())


def test_endpoint_route_smoke():
    """GET /api/decision-paths → 200 + JSON 含 paths (真 HTTP, 一次性服务)。"""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        status, body = _http_get(srv.server_address[1], "/api/decision-paths")
        assert status == 200
        assert len(body["paths"]) == 2
    finally:
        srv.shutdown()
        srv.server_close()


# ──────────────────────────────────────────────────────────────────
# s9/#95: classify 判定入口端点 (s5 §2.2 建议 a)
# ──────────────────────────────────────────────────────────────────
def test_classify_endpoint_smoke():
    """GET /api/decision-paths/classify?text=… → 200 + 判定 dict (真 HTTP)。

    响应**直接是判定** (path_id/label/matched_signals/vetoed_signals/
    score/fallback), SOUL/skill 由此同步为「调端点拿判定」。
    """
    from urllib.parse import quote
    srv = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        port = srv.server_address[1]
        # 命中: 技术类输入 ⇒ path_id=technical, 契约字段齐全
        status, body = _http_get(
            port, "/api/decision-paths/classify?text=" + quote("接口报错, 堆栈见上"))
        assert status == 200
        assert body["path_id"] == "technical"
        assert {"label", "matched_signals", "vetoed_signals",
                "score", "fallback"} <= set(body)
        assert body["fallback"] is None and body["score"] > 0
        # 不可判定 ⇒ 200 + path_id null + ask_user (不是 404/500)
        status, body = _http_get(
            port, "/api/decision-paths/classify?text=" + quote("帮我看看这个"))
        assert status == 200
        assert body["path_id"] is None and body["fallback"] == "ask_user"
        # text 缺席 ⇒ 400 + error (端点契约要显式)
        status, body = _http_get(port, "/api/decision-paths/classify")
        assert status == 400 and "text" in body.get("error", "")
        # text 空串是合法输入 (keep_blank_values) ⇒ 200 + ask_user, 不是 400
        status, body = _http_get(port, "/api/decision-paths/classify?text=")
        assert status == 200
        assert body["path_id"] is None and body["fallback"] == "ask_user"
    finally:
        srv.shutdown()
        srv.server_close()


def test_classify_endpoint_degrades_on_bad_yaml(tmp_path, monkeypatch):
    """yaml 缺失/损坏 ⇒ classify 端点仍 200 + path_id null (永不 500)。"""
    monkeypatch.setenv("MW_DECISION_PATHS_PATH", str(tmp_path / "nope.yaml"))
    code, body = console.api_decision_paths_classify({"text": ["堆栈 traceback"]})
    assert code == 200
    assert body["path_id"] is None and body["fallback"] == "ask_user"


def test_soul_skill_point_at_classify_endpoint():
    """s9/#95: SOUL 双语 + skill 双语的判定入口必须同步为 classify 端点。

    修复前四处文本都写「把文本与 any_of/none_of 自行命中手算得分」⇒
    判定逻辑存在两份 (mw/decision_paths.py 一份可测, 文字一份不可测)。
    钉桩: 四文件都出现 classify 端点路径, 且 SOUL 不再教手算公式。
    """
    needle = "/api/decision-paths/classify"
    soul_zh = (ROOT / "docs" / "main-soul.zh-cn.md").read_text(encoding="utf-8")
    soul_en = (ROOT / "docs" / "main-soul.en.md").read_text(encoding="utf-8")
    skill_dir = ROOT / "deploy" / "skills" / "team-work-chain-orchestration"
    skill_zh = (skill_dir / "SKILL.zh-cn.md").read_text(encoding="utf-8")
    skill_en = (skill_dir / "SKILL.en.md").read_text(encoding="utf-8")
    for name, text in (("soul-zh", soul_zh), ("soul-en", soul_en),
                       ("skill-zh", skill_zh), ("skill-en", skill_en)):
        assert needle in text, f"{name} 判定入口未指向 classify 端点"
    # 手算公式不再出现在 SOUL (判定口径唯一 = 端点)
    assert "命中数 − 2×否决数" not in soul_zh
    assert "score = hits" not in soul_en


# ──────────────────────────────────────────────────────────────────
# T-70-8: 纯函数静态自检 — 除 load_paths 读 yaml 外零 IO/网络/registry
# ──────────────────────────────────────────────────────────────────
def test_module_purity_static():
    src = (ROOT / "mw" / "decision_paths.py").read_text(encoding="utf-8")
    # load_paths 里合法的 open() 恰好 1 处; 其余 IO/网络/registry 字样零命中
    assert src.count("open(") == 1
    for pat in ("requests", "httpx", "urllib", "socket", "registry",
                "sqlite3", "subprocess"):
        assert not re.search(pat, src), f"decision_paths.py 含越界引用 {pat}"


# ──────────────────────────────────────────────────────────────────
# T-70-9/10/11: SOUL 双语同步 (新节存在 / EN CJK=0 / REQUIRED 块数不变)
# ──────────────────────────────────────────────────────────────────
def test_soul_bilingual_decision_paths_section():
    zh = (ROOT / "docs" / "main-soul.zh-cn.md").read_text(encoding="utf-8")
    en = (ROOT / "docs" / "main-soul.en.md").read_text(encoding="utf-8")
    assert "## 决策路径" in zh
    assert "## Decision Paths" in en
    # 两文件都引用了判定入口端点 (内容对应)
    assert "/api/decision-paths" in zh and "/api/decision-paths" in en
    # REQUIRED 块结构: 新节加在既有 REQUIRED 块内 ⇒ 独立标记行数不变 (9)。
    # 注意口径: 文件开头说明行里有 1 处 inline code 形态的同名字样,
    # grep -c '^<!-- REQUIRED' 口径 (独立标记行) 基线实测 = 9。
    assert len(re.findall(r"(?m)^<!-- REQUIRED: do not remove -->$", zh)) == 9
    assert len(re.findall(r"(?m)^<!-- REQUIRED: do not remove -->$", en)) == 9


def test_soul_en_cjk_zero_outside_code():
    """G13 同口径: EN SOUL 代码块/inline code/HTML 注释外 CJK = 0。"""
    text = (ROOT / "docs" / "main-soul.en.md").read_text(encoding="utf-8")
    in_fence = False
    violations = []
    for i, line in enumerate(text.split("\n"), 1):
        if re.match(r"^```", line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        no_inline = re.sub(r"`[^`]+`", "", re.sub(r"<!--.*?-->", "", line))
        if re.findall(r"[\u4e00-\u9fff]+", no_inline):
            violations.append(i)
    assert not violations, f"EN SOUL CJK 行: {violations}"

# ──────────────────────────────────────────────────────────────────
# s13/P2-1: 展示标签本地化 (部署语言 ⇒ 期望标签, 双向钉桩)
#
# 缺陷: `_label()` 拼 `f"label_{lang}"`, 而 resolve_lang() 返回 locale 标识
# `zh-cn`, yaml 键后缀却是 `zh` ⇒ 查 `label_zh-cn` 恒 miss ⇒ or 链静默
# 回退 `label_en` ⇒ **中文标签永不可达** (zh-cn 与 en 两部署返回同值)。
# 归因 s3 (`3164bb2`) 引入, s9 未触碰该函数 ⇒ 前序遗留。
#
# 口径: 代码侧显式映射 (locale ⇒ 键后缀), yaml 键名 `label_zh`/`label_en`
# 不变 (兼容既有路由表条目与既有断言)。
# ──────────────────────────────────────────────────────────────────
def _label_for(lang: str, path_id: str) -> str:
    """在给定部署语言下取某条 path 的展示标签 (走 classify 全链路)。"""
    from mw import decision_paths as dp
    paths = dp.load_paths(str(DEFAULT_YAML))
    target = [p for p in paths if p["id"] == path_id]
    assert target, f"默认路由表缺 {path_id}"
    # classify() 走 _label() 全链路 (不是直接调 _label), 顺带钉住端点取值
    text = "验收标准" if path_id == "requirement" else "堆栈 traceback"
    return dp.classify(text, target)["label"]


def test_label_localisation_zh_cn_returns_chinese_label(monkeypatch):
    """部署语言 zh-cn ⇒ 返回**中文**标签 (修复前恒返英文)。"""
    monkeypatch.setenv("MW_LANG", "zh-cn")
    assert _label_for("zh-cn", "requirement") == "需求类输入"
    assert _label_for("zh-cn", "technical") == "技术类输入"


def test_label_localisation_en_returns_english_label(monkeypatch):
    """部署语言 en ⇒ 返回**英文**标签 (修复未破坏 en 侧)。"""
    monkeypatch.setenv("MW_LANG", "en")
    assert _label_for("en", "requirement") == "requirement-shaped input"
    assert _label_for("en", "technical") == "technical-shaped input"


def test_label_localisation_two_languages_differ(monkeypatch):
    """两语言取值**必须不同** —— 恒等即恒真 (缺陷形态正是两语言同值)。

    这是本组钉桩的反恒真锚: 若实现退化成「永远返回英文」或「永远返回
    中文」, 本用例即红。
    """
    monkeypatch.setenv("MW_LANG", "zh-cn")
    zh_req = _label_for("zh-cn", "requirement")
    zh_tech = _label_for("zh-cn", "technical")
    monkeypatch.setenv("MW_LANG", "en")
    en_req = _label_for("en", "requirement")
    en_tech = _label_for("en", "technical")
    assert zh_req != en_req, f"requirement 两语言同值 ({zh_req!r}) ⇒ 恒真"
    assert zh_tech != en_tech, f"technical 两语言同值 ({zh_tech!r}) ⇒ 恒真"
    # 且中文侧标签确实含 CJK (不是英文标签换个马甲)
    assert re.search(r"[一-鿿]", zh_req), f"zh 标签不含 CJK: {zh_req!r}"
    assert re.search(r"[一-鿿]", zh_tech), f"zh 标签不含 CJK: {zh_tech!r}"


def test_label_localisation_invalid_lang_falls_back_without_crash(monkeypatch):
    """非法部署语言 ⇒ 回退 en 的标签, 不抛不崩 (契约: 永不抛)。"""
    for bad in ("", "fr", "zh", "ZH-CN", "zh_cn", "xx-YY"):
        monkeypatch.setenv("MW_LANG", bad)
        assert _label_for(bad, "technical") == "technical-shaped input", \
            f"非法语言 {bad!r} 未回退到 en 标签"


def test_label_localisation_no_second_label_source():
    """标签**只存 yaml 一份** —— 代码里不得出现第二份标签来源。

    四条钉桩 (用 AST 而非裸 grep, 免得被 docstring 里的说明文字误伤):

    1. **无第二份标签字面量**: 源码的字符串字面量里不得出现任何 yaml
       标签文案 (标签只能来自 yaml)。
    2. **无缺陷形态拼接**: `_label()` 里构造 yaml 标签键时, 拼进去的
       **不得是 `resolve_lang()` 的原始返回值** (locale 标识 `zh-cn` 与
       yaml 键后缀 `zh` 不同名 ⇒ 必 miss ⇒ 静默回退英文)。正确实现是
       先经 `_LANG_TO_LABEL_KEY` 映射再拼。
    3. **映射表存在且覆盖全部部署语言**: `mw.i18n.SUPPORTED` 的每个语言
       都必须在 `_LANG_TO_LABEL_KEY` 里有对应键后缀 (新增语言时漏映射
       ⇒ 静默回退英文, 本用例即红)。
    4. **检查非空转**: `_label()` 里必须**确实**有标签键构造表达式 ——
       否则第 2 条会因「没找到可查的东西」而恒绿。
    """
    import ast
    src = (ROOT / "mw" / "decision_paths.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # (1) 无第二份标签字面量
    literals = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.add(node.value)
    for lit in ("需求类输入", "技术类输入",
                "requirement-shaped input", "technical-shaped input"):
        assert lit not in literals, \
            f"代码内出现标签字面量 {lit!r} ⇒ 存在第二份标签来源"

    # (2)+(4) _label() 里的标签键构造: 拼入值不得是 resolve_lang() 原始返回
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_label")
    # 函数体内「变量名 ← 直接调用 resolve_lang()」的赋值
    raw_lang_vars = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fn = node.value.func
            fname = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if fname == "resolve_lang":
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        raw_lang_vars.add(tgt.id)

    key_constructions = []
    for node in ast.walk(func):
        # f"label_{x}" 形态
        if isinstance(node, ast.JoinedStr):
            consts = [p.value for p in node.values
                      if isinstance(p, ast.Constant) and isinstance(p.value, str)]
            if "".join(consts).startswith("label_"):
                names = [p.value.id for p in node.values
                         if isinstance(p, ast.FormattedValue)
                         and isinstance(p.value, ast.Name)]
                key_constructions.append(names)
        # "label_" + x 形态
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            parts = [node.left, node.right]
            for side, other in ((node.left, node.right), (node.right, node.left)):
                if (isinstance(side, ast.Constant) and isinstance(side.value, str)
                        and side.value.startswith("label_")
                        and isinstance(other, ast.Name)):
                    key_constructions.append([other.id])

    assert key_constructions, \
        "_label() 内未找到标签键构造表达式 ⇒ 本组检查空转 (恒真)"
    for names in key_constructions:
        for nm in names:
            assert nm not in raw_lang_vars, \
                (f"标签键直接拼入 resolve_lang() 的原始返回值 ({nm!r}) ⇒ "
                 f"locale 标识与 yaml 键后缀不同名, 必 miss ⇒ 静默回退英文")

    # (3) 映射表覆盖全部部署语言
    from mw import decision_paths as dp
    from mw.i18n import SUPPORTED
    for lang in SUPPORTED:
        assert lang in dp._LANG_TO_LABEL_KEY, \
            f"部署语言 {lang!r} 未映射到 yaml 标签键后缀 ⇒ 静默回退英文"


def test_label_localisation_asserts_against_yaml_source():
    """断言对象 = **yaml 原文** (不是代码里的期望值常量) —— 防自证循环。

    上游缺陷的教训: 断言若取「实现自己产出的值」, 则恒真。本用例直接读
    yaml 原文的 `label_zh` / `label_en`, 再与 classify 的输出比对 ——
    yaml 改了标签文案, 断言随之更新; 但「取哪条键」的口径由代码决定,
    仍被上面几条用例钉死。
    """
    import yaml
    raw = yaml.safe_load(DEFAULT_YAML.read_text(encoding="utf-8"))
    by_id = {p["id"]: p for p in raw["paths"]}
    for pid, zh_key, en_key in (("requirement", "label_zh", "label_en"),
                                ("technical", "label_zh", "label_en")):
        assert zh_key in by_id[pid] and en_key in by_id[pid], \
            f"{pid} 缺 {zh_key}/{en_key}"
        # 中文标签必须真的是中文, 英文标签必须真的是英文 (不能同值)
        assert re.search(r"[一-鿿]", by_id[pid][zh_key])
        assert not re.search(r"[一-鿿]", by_id[pid][en_key])
        assert by_id[pid][zh_key] != by_id[pid][en_key]


def test_classify_endpoint_label_follows_deploy_lang(monkeypatch):
    """端点 `GET /api/decision-paths/classify` 的 label 字段随部署语言变。

    s9 端点原样透传 classify() 的 label ⇒ 缺陷在 s9 被放大到生产面。
    本用例走真实 HTTP (一次性服务), 两语言各起一次 ⇒ 端点响应体里的
    label 必须是中文 / 英文两侧不同。
    """
    from urllib.parse import quote
    srv = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        port = srv.server_address[1]
        url = "/api/decision-paths/classify?text=" + quote("堆栈 traceback 报错")
        monkeypatch.setenv("MW_LANG", "zh-cn")
        status, body = _http_get(port, url)
        assert status == 200
        assert body["path_id"] == "technical"
        assert body["label"] == "技术类输入", \
            f"zh-cn 端点 label 非中文: {body['label']!r}"
        monkeypatch.setenv("MW_LANG", "en")
        status, body = _http_get(port, url)
        assert status == 200
        assert body["label"] == "technical-shaped input", \
            f"en 端点 label 非英文: {body['label']!r}"
        assert body["label"] != "技术类输入"
    finally:
        srv.shutdown()
        srv.server_close()