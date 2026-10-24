"""mw.i18n — 后端部署时钟文案表(i18n v1.0, AR4/AR5, 架构 §5)。

设计原则:
- 部署时钟: `.env LANG` (en|zh-cn, install.sh 校验) → 写盘
  `data/mw_data/deploy_lang` → main_entry.sh `export MW_LANG`。运行时
  容器内 POSIX `LANG` 不被覆盖(避免破坏 hermes gateway 编码)。
- 运行时单源: `MW_LANG` env。`os.environ.get("MW_LANG","en")` 调用时读。
- 词条表: `mw/i18n/{en,zh-cn}.json` 扁平 key (e.g. `console.upload.too_large`)。
- 回退: 目标语言 → en → key 字面量(永抛不出)。
- 插值: `str.format` 用 `{name}`,缺失的占位符保留不抛(i18next
  returnEmptyString:false 的对称实现)。
- 空串视为缺失(避免 zh-cn.json 留空键时返回空字符串)。

★零回归锚(架构 §6, 测试 D3/D4): `zh-cn.json` 的值必须逐字照抄改造前原文。
`tests/conftest.py` 钉 `MW_LANG=zh-cn`,36 个 pytest 文件零改断言即可全绿。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SUPPORTED = ("en", "zh-cn")

# 调用时缓存(语言键 → dict),语言切换进程内立即生效(测试可 monkeypatch env)。
_cache: dict[str, dict] = {}
_LOADED: dict[str, bool] = {}

# 解析失败保护(坏 JSON 不能阻塞服务启动 — 退化到只有 en 的子集)。
_BAD_LOAD: dict[str, str] = {}

_I18N_DIR = Path(__file__).resolve().parent


def _load(lang: str) -> dict:
    """读 `mw/i18n/<lang>.json`,缓存到进程级 dict。"""
    if lang in _LOADED:
        return _cache.get(lang, {})
    _LOADED[lang] = True
    p = _I18N_DIR / f"{lang}.json"
    if not p.exists():
        _BAD_LOAD[lang] = f"missing: {p}"
        _cache[lang] = {}
        return _cache[lang]
    try:
        _cache[lang] = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _BAD_LOAD[lang] = f"parse error: {exc}"
        _cache[lang] = {}
    return _cache[lang]


def resolve_lang() -> str:
    """部署语言: `MW_LANG` env 调用时读取,非法/缺省 → `en`。

    单源语言: 后端所有 user-visible 文案经此函数。永不抛。
    """
    v = os.environ.get("MW_LANG", "en")
    return v if v in SUPPORTED else "en"


def _normalize_params(params: dict) -> dict:
    """str.format 准备:`**params` 已经是 dict,这里只做空串保护。

    缺失参数保留占位符(架构 §5.2 明示);空串值视作 missing。
    """
    out = {}
    for k, v in params.items():
        if v == "" or v is None:
            continue
        out[k] = v
    return out


def t(key: str, **params) -> str:
    """查表: 目标语言 → en → key 字面量。

    插值: `str.format(**{name: value})`,缺失占位符保留为字面 `{name}`。
    空串视为缺失(与 i18next returnEmptyString:false 对称)。
    找不到任何语言的 key,返回 key 字面量(架构 §5.2 末端)。
    """
    lang = resolve_lang()
    tables = _cache.get(lang)
    if tables is None:
        tables = _load(lang)
    val = tables.get(key)
    if not val and lang != "en":
        en_tbl = _cache.get("en")
        if en_tbl is None:
            en_tbl = _load("en")
        val = en_tbl.get(key)
    if not val:
        return key
    if not params:
        return val
    try:
        return val.format(**_normalize_params(params))
    except (KeyError, IndexError, ValueError):
        return val


# 调试辅助(测试 / 守护工具会用到)
def _bad_loads() -> dict:
    """返回本进程对各语言 JSON 加载失败记录(守护测试 B2/§8.3 复用)。"""
    return dict(_BAD_LOAD)


def _loaded_keys(lang: str) -> set:
    """返回某语言已加载 keys(未加载就忽略,空集合)。"""
    if lang not in _LOADED:
        _load(lang)
    return set(_cache.get(lang, {}).keys())