"""issue #238 守护: 字符串设置键传非字符串值 500 而非 422。

依据: docs/issue-II/TRIAGE.md §2.2 验收判据 1/2。
根因: _clean_webui_url/_clean_asr_base 首行 (v or "").strip() 对 int 抛
AttributeError → do_PUT 兜底 500; 其余 5 个无清洗字符串键非字符串值原样
落盘 200 (类型污染)。
修法: api_settings_save 未知键校验后、清洗调用前统一类型闸 ——
SETTINGS_DEFAULTS[k] 为 str 而 v 非 str ⇒ ValueError (走既有 422 通道)。

覆盖:
  G1  全部 7 个字符串键传 123 ⇒ ValueError (含 issue 点名的 5 键)。
  G2  合法字符串不回归: asr_base_url="https://x" 落盘成功。
  G3  int/bool 键不扩面: pixel_office_enabled=true / default_timeout_seconds=1800
      照常 200 落盘。
  G4  类型闸在未知键校验之后: 未知键仍报未知配置项 (顺序守护)。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mw.console as console  # noqa: E402

STR_KEYS = [k for k, v in console.SETTINGS_DEFAULTS.items()
            if isinstance(v, str)]


def _save(sp: Path, payload: dict) -> dict:
    with mock.patch.object(console, "SETTINGS", str(sp)):
        return console.api_settings_save(payload)


def test_g1_all_string_keys_reject_int(tmp_path):
    """§2.2 判据 1: 7 个字符串键传 123 ⇒ ValueError (HTTP 面即 422)。"""
    assert len(STR_KEYS) == 7
    sp = tmp_path / "settings.json"
    for k in STR_KEYS:
        try:
            _save(sp, {k: 123})
        except ValueError:
            continue
        raise AssertionError(f"{k}=123 应抛 ValueError 而未抛")
    # 非 str 非 int 的其它类型同样拒
    for k in ("asr_base_url", "hermes_webui_url", "default_image",
              "asr_model", "asr_key"):
        for bad in ([], {}, None, True, 1.5):
            try:
                _save(sp, {k: bad})
            except ValueError:
                continue
            raise AssertionError(f"{k}={bad!r} 应抛 ValueError 而未抛")


def test_g2_valid_string_not_regressed(tmp_path):
    """§2.2 判据 2: 合法字符串照常落盘。"""
    sp = tmp_path / "settings.json"
    r = _save(sp, {"asr_base_url": "https://x"})
    assert r["ok"] is True
    data = json.loads(sp.read_text())
    assert data["asr_base_url"] == "https://x"
    r = _save(sp, {"default_image": "img:tag", "asr_key": "sk-1",
                   "asr_model": "m1", "hermes_webui_url": "https://w"})
    assert r["ok"] is True


def test_g3_int_bool_keys_not_widened(tmp_path):
    """§2.2 判据 2: int/bool 键不在类型闸范围, 不扩面。"""
    sp = tmp_path / "settings.json"
    r = _save(sp, {"pixel_office_enabled": True})
    assert r["ok"] is True
    r = _save(sp, {"default_timeout_seconds": 1800})
    assert r["ok"] is True
    data = json.loads(sp.read_text())
    assert data["pixel_office_enabled"] is True
    assert data["default_timeout_seconds"] == 1800


def test_g4_unknown_key_still_unknown_error(tmp_path):
    """顺序守护: 类型闸在未知键白名单校验之后, 未知键报错语义不变。"""
    sp = tmp_path / "settings.json"
    try:
        _save(sp, {"no_such_key": 123})
        raise AssertionError("未知键应抛 ValueError")
    except ValueError as e:
        assert "未知配置项" in str(e)
