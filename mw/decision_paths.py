"""决策路径模块 (issue #70) — main 的粗分类路由表, 「暂不抽象」最小形态。

一个 yaml + 本纯函数模块 + 一个只读端点, 零框架 / 零插件 / 零 LLM /
零注册表依赖 (PLAN-78-70 §3.2 / §3.8)。后续换 jev 决策模型时只改
yaml 与 loader, 不动调用方。

判定算法 (PLAN §3.4, 顺序固定, 确定性可测):
  1. 每个 path: hits = [s for s in match.any_of if s.lower() in text.lower()]
  2. 每个 path: veto  = [s for s in match.none_of if s.lower() in text.lower()]
  3. 得分 = len(hits) - 2 * len(veto); 得分 > 0 里取最高分;
     并列 ⇒ 按 yaml 出现顺序取第一个 (不依赖 dict 序);
     全部 ≤ 0 ⇒ path_id=None + fallback="ask_user" (main 必须问用户,
     不许自行挑角色 —— 猜错 = 让错角色干 = 更慢)。

命中判定**大小写不敏感** (s6/F-1 修复): 两侧归一化后再比对, 裸栈回溯
`Traceback (most recent call last):` 因此也能命中 technical (词表小写
`traceback` 此前漏判 ⇒ 返回 None ⇒ main 每遇真实技术 issue 都要回头
问用户)。回传的 matched_signals / vetoed_signals 是**词表原文** (s),
调用方可按词表反查命中原因。

展示标签 (s13/P2-1 修复): 标签**只存 yaml 一份** (`label_en` / `label_zh`
两个语言键), 代码按**部署语言 ⇒ 语言键映射**取, 不做 `f"label_{lang}"`
式拼接 —— 部署语言是 locale 标识 (`zh-cn`), 语言键是 `zh`, 拼接必然 miss
并静默回退英文 (中文标签永不可达)。映射表见 _LANG_TO_LABEL_KEY。

结构非法**逐条丢弃** (s6/F-4 修复): id 非非空字符串 / match 不是 dict /
any_of、none_of 不是列表 / 缺端点契约字段 (route_to / role_tags /
forbidden_for_main) 的条目一律不进表 —— yaml 是仓库资产, main 可直接
编辑, 一条写错不该让整张路由表失效, 但坏条目绝不外泄给端点。

边界 (本批明确不做): 不引入规则引擎/决策图/插件注册; 不做 LLM 分类
(mw 是确定性中间件); 决策路径不是 workflow 的一种; 不在 UI 配路由。
"""
from __future__ import annotations

import os

DECISION_PATHS_PATH = os.environ.get(
    "MW_DECISION_PATHS_PATH",
    os.path.join(os.path.dirname(__file__) or ".", "decision_paths.yaml"))

# 端点契约 (PLAN §4.3 T-70-6): 每条 path 必须带的列表字段 ——
# 缺任一项的条目不得进表, 否则客户端按契约解析会 KeyError。
_REQUIRED_LIST_FIELDS = ("role_tags", "forbidden_for_main")

# 部署语言 (locale) ⇒ yaml 标签键后缀 (s13/P2-1 修复)。
#
# 根因: 部署语言是 locale 标识 (`zh-cn`), yaml 键后缀是语言键 (`zh`) ——
# 两个命名空间不同。此前 `_label()` 拼 `f"label_{lang}"` ⇒ `label_zh-cn`
# 恒 miss ⇒ `or` 链静默回退 `label_en` ⇒ **中文标签永不可达** (zh-cn 与 en
# 两种部署返回同值, 无告警)。
#
# 口径: **代码侧映射, 不改 yaml 键名**。理由:
#   1. yaml 自身已用 `_zh`/`_en` 后缀约定 (`signals_zh`/`signals_en`),
#      `label_zh`/`label_en` 与之同族; 改成 `label_zh-cn` 反而让同一文件
#      内两套后缀并存。
#   2. yaml 是「仓库资产, main 可直接编辑」的开放面 (见 load_paths 注释);
#      改键名 = 改开放面的公开契约, 所有已按 `label_zh`/`label_en` 写的
#      路由表条目全部失配 (静默回落 id, 不报错)。
#   3. 缺陷在代码的「locale 标识 == 键后缀」错误假设, 不在 yaml 命名;
#      代码侧修 = 零兼容面影响 (既有 yaml 条目与既有断言全不破)。
_LANG_TO_LABEL_KEY = {"en": "en", "zh-cn": "zh"}

# 目标语言缺标签时的回退顺序 (语言键后缀), 末位兜底取 path 的 id。
_LABEL_FALLBACK_KEYS = ("en", "zh")


def _is_valid_path(p: object) -> bool:
    """结构校验 (s6/F-4): 这条 path 能不能进路由表。

    通过条件:
      - 本身是 dict;
      - id 是非空字符串 (契约要求 path_id: str|None, 不放 int);
      - match 是 dict, 且 any_of / none_of 要么缺席要么是列表
        (字符串会被当成字符序列逐字命中, 语义全错);
      - 端点契约字段齐全: route_to 非空字符串 + role_tags /
        forbidden_for_main 是非空列表。
    """
    if not isinstance(p, dict):
        return False
    if not isinstance(p.get("id"), str) or not p["id"]:
        return False
    match = p.get("match")
    if not isinstance(match, dict):
        return False
    for key in ("any_of", "none_of"):
        v = match.get(key)
        if v is not None and not isinstance(v, list):
            return False
    if not isinstance(p.get("route_to"), str) or not p["route_to"]:
        return False
    for key in _REQUIRED_LIST_FIELDS:
        v = p.get(key)
        if not isinstance(v, list) or not v:
            return False
    return True


def load_paths(path: str | None = None) -> list[dict]:
    """读 decision_paths.yaml → list[dict]。

    文件缺失 / 解析失败 → [] (永不抛; 端点降级为 {'paths': []})。
    结构非法的条目**逐条丢弃** (不是整表作废): yaml 是仓库资产, main
    可直接编辑, 一条写错不该让整张路由表失效 —— 但坏条目绝不外泄。
    每次调用读盘: yaml 是仓库小文件, 无热更新缓存的必要 (改路由立即生效)。
    """
    src = (path or os.environ.get("MW_DECISION_PATHS_PATH")
           or DECISION_PATHS_PATH)
    try:
        import yaml
        with open(src, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        paths = data.get("paths")
        if not isinstance(paths, list):
            return []
        return [p for p in paths if _is_valid_path(p)]
    except Exception:
        return []


def classify(text: str, paths: list[dict] | None = None) -> dict:
    """按输入**文本形态**粗分类, 返回路由判定 (纯函数, 零副作用)。

    → {"path_id": str|None, "label": str, "matched_signals": [...],
       "vetoed_signals": [...], "score": int, "fallback": "ask_user"|None}

    命中判定大小写不敏感; 形状非法的 path 在此再兜一层 (调用方直接传
    paths 参数时 load_paths 的校验不生效), 保证**永不抛**。
    """
    if paths is None:
        paths = load_paths()
    elif not isinstance(paths, list):
        paths = []                    # s9/S-1: 非列表直传 ⇒ 视同空表, 兑现「永不抛」
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    text_n = text.lower()          # 归一化: 命中判定大小写不敏感 (F-1)
    best_id, best_score = None, 0
    best_hits: list[str] = []
    best_veto: list[str] = []
    best_path: dict = {}
    for p in paths or []:
        if not isinstance(p, dict):
            continue
        pid = p.get("id")
        if not isinstance(pid, str) or not pid:
            continue                  # id 非法 ⇒ 不参与判定 (契约 path_id: str)
        match = p.get("match")
        if not isinstance(match, dict):
            match = {}               # match 形状非法 ⇒ 当无信号, 不抛 (F-4)
        any_of = match.get("any_of")
        none_of = match.get("none_of")
        any_of = any_of if isinstance(any_of, list) else []
        none_of = none_of if isinstance(none_of, list) else []
        hits = [s for s in any_of if isinstance(s, str) and s.lower() in text_n]
        veto = [s for s in none_of if isinstance(s, str) and s.lower() in text_n]
        score = len(hits) - 2 * len(veto)
        # 严格 > 才替换 ⇒ 并列时保留先出现的 (yaml 顺序 = 确定性裁决)
        if score > best_score:
            best_id, best_score = pid, score
            best_hits, best_veto, best_path = hits, veto, p
    if best_id is None:
        return {"path_id": None, "label": "", "matched_signals": [],
                "vetoed_signals": [], "score": 0, "fallback": "ask_user"}
    return {"path_id": best_id, "label": _label(best_path),
            "matched_signals": best_hits, "vetoed_signals": best_veto,
            "score": best_score, "fallback": None}


def _label(p: dict) -> str:
    """按部署语言取 path 的展示标签 (双语随 yaml 分发, 不占 i18n 词条)。

    s13/P2-1 修复: 原实现拼 `f"label_{lang}"` ⇒ 部署语言 `zh-cn` 查
    `label_zh-cn` 恒 miss ⇒ 静默回退 `label_en` ⇒ 中文标签永不可达
    (zh-cn 与 en 两部署返回同值)。现按 _LANG_TO_LABEL_KEY 做**显式映射**
    (`zh-cn` ⇒ `label_zh`), 不再假设 locale 标识 == 键后缀。

    取值顺序: 目标语言标签 → 英文标签 → 中文标签 → path 的 id。
    永不抛; 末位兜底取 id (classify() 只对 id 非空字符串的条目调本函数,
    故经 classify() 的取值恒为非空串)。
    """
    try:
        from .i18n import resolve_lang
        lang = resolve_lang()
    except Exception:
        lang = "en"
    # 未知 locale (resolve_lang 已保证只返 SUPPORTED 内的值, 这里是双保险)
    suffix = _LANG_TO_LABEL_KEY.get(lang, "en")
    val = p.get(f"label_{suffix}")
    if val:
        return val
    for fb in _LABEL_FALLBACK_KEYS:
        if fb == suffix:
            continue
        val = p.get(f"label_{fb}")
        if val:
            return val
    return p.get("id", "")
