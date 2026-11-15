"""issue #237 守护: 并发 PUT /api/settings 读-改-写非原子 ⇒ 500。

依据: docs/issue-II/TRIAGE.md §2.1 验收判据 1。
根因: api_settings_save 读盘→merge→写固定名 tmp→os.replace 整段非原子,
ThreadingHTTPServer 多线程并发互相截断 tmp / os.replace 竞态 FileNotFoundError。
修法: 模块级 _SETTINGS_SAVE_LOCK 覆盖读-改-写整段 + tmp 改 mkstemp 唯一名。

覆盖:
  T1  20 线程并发保存 ⇒ 0 异常、settings.json 可 json.loads、终值合法(无撕裂)。
  T2  无丢写: 每轮 7 线程各写一个不同字符串键唯一值, 多轮重复 ⇒ 每轮结束后
      全部键值都在盘上(读-改-写竞态下会整键回退丢失)。
  T3  无残留固定名 tmp 互踩面: 保存后目录无 settings.json.tmp。

pytest 口径: 直接 test_* 函数断言。
"""
from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mw.console as console  # noqa: E402

# SETTINGS_DEFAULTS 里全部 7 个字符串键 (类型闸/并发写共用)
STR_KEYS = [k for k, v in console.SETTINGS_DEFAULTS.items()
            if isinstance(v, str)]


def _val(rnd_or_i, k: str) -> str:
    # hermes_webui_url / asr_base_url 有 http(s) 清洗校验, 值须合法
    if k in ("hermes_webui_url", "asr_base_url"):
        return f"https://example.test/{rnd_or_i}-{k}"
    return f"{rnd_or_i}-{k}"


def _save(i: int, settings_path: Path) -> None:
    console.api_settings_save({
        "default_image": f"img-{i}",
        "asr_model": f"model-{i}",
    })


def test_t1_20_threads_no_exception_valid_json(tmp_path):
    """TRIAGE §2.1 判据 1: 20 线程并发 ⇒ 0 异常、可 json.loads、无撕裂终值。"""
    sp = tmp_path / "settings.json"
    # 注意: patch 必须在并发块外层 —— mock.patch 非线程安全, 线程内进出会互相还原
    with mock.patch.object(console, "SETTINGS", str(sp)):
        with ThreadPoolExecutor(max_workers=20) as ex:
            errs = list(ex.map(_save, range(20), [sp] * 20))
    assert errs == [None] * 20  # map 重抛异常, 走到这里即 0 异常
    data = json.loads(sp.read_text())  # 不可撕裂: 必须可解析
    assert data["default_image"] == f"img-{int(data['default_image'].split('-')[1])}"
    assert data["default_image"].startswith("img-")
    assert data["asr_model"].startswith("model-")


def test_t2_no_lost_writes_multi_rounds(tmp_path):
    """无丢写: 每轮 7 线程各写不同字符串键, 轮末全部键值在盘。

    读-改-写竞态下, 后写者用旧快照覆盖会整键回退 ⇒ 本测红。
    """
    assert len(STR_KEYS) == 7, f"字符串键应为 7 个, 实测 {STR_KEYS}"
    sp = tmp_path / "settings.json"
    with mock.patch.object(console, "SETTINGS", str(sp)):
        for rnd in range(8):
            def one(k, _rnd=rnd):
                console.api_settings_save({k: _val(f"r{_rnd}", k)})
            with ThreadPoolExecutor(max_workers=len(STR_KEYS)) as ex:
                list(ex.map(one, STR_KEYS))
            data = json.loads(sp.read_text())
            for k in STR_KEYS:
                assert data[k] == _val(f"r{rnd}", k), f"round {rnd}: 键 {k} 丢写 ({data[k]})"


def test_t3_no_fixed_tmp_leftover(tmp_path):
    """mkstemp 唯一 tmp: 保存后目录无固定名 settings.json.tmp 残留。"""
    sp = tmp_path / "settings.json"
    with mock.patch.object(console, "SETTINGS", str(sp)):
        console.api_settings_save({"default_image": "img-x"})
    assert sp.exists()
    assert not (tmp_path / "settings.json.tmp").exists()
    assert not list(tmp_path.glob("*.tmp"))
