"""ASR-SET 守护: ASR 三键进设置页 (settings asr_* > env MW_ASR_* > 内置默认)。

依据: docs/asr-config-settings/DESIGN.md §7 守护测试清单 S1-S5 (唯一技术依据)。
用户拍板「进配置而非 env」: SETTINGS_DEFAULTS 新增 asr_base_url/asr_model/asr_key
三键(默认空串哨兵), audio_proxy 每次调用实时读 settings.json(无缓存), 保存 ASR
键联动清 voice-status 探测缓存 ⇒ 热生效。

覆盖:
  S1  键集守护: 三键在 SETTINGS_DEFAULTS 且默认值 == ""(防坑条款, 变异反证:
      默认值改硬编码 ⇒ 本测红)。
  S2  三层链: MW_SETTINGS 指向 tmp settings.json。S2a 文件缺失 ⇒ 内置默认;
      S2b 键空串 ⇒ env 值; S2c 键非空 ⇒ settings 压 env; S2d 坏 JSON ⇒ 穿透不炸。
  S3  校验: api_settings_save asr_base_url="javascript:alert(1)" 抛 ValueError;
      ""/"https://x" 通过且落盘值正确。
  S4  保存联动清缓存: 预置 _status_cache, save 含 asr 键 ⇒ 缓存被清;
      save 不含 asr 键 ⇒ 缓存保留。
  S5  回归: 既有 V5 env 覆盖语义 (env 有值 settings 空 ⇒ env 生效)。

双口径断言 (照抄 #232/#193/#190 守护):
  pytest 收集口径 —— check() 失败即抛 AssertionError;
  脚本直跑口径   —— main() 置 _COLLECT_ONLY=True, 只收集不抛, rc 汇总。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mw.console as console  # noqa: E402
import mw.audio_proxy as audio  # noqa: E402

PASS, FAIL = [], []
_COLLECT_ONLY = False


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    line = f"  {'PASS' if cond else 'FAIL'}  {name}"
    if detail:
        line += f"  {detail}"
    print(line)
    if not _COLLECT_ONLY:
        assert cond, f"{name} {detail}"


class _clean_asr_env:
    """with 块: 移除 MW_ASR_* 三 env, 设 MW_SETTINGS; 退出自动还原。"""

    def __init__(self, settings_path, **extra):
        self.settings_path = str(settings_path)
        self.extra = extra

    def __enter__(self):
        self._cm = mock.patch.dict(os.environ)
        self._cm.__enter__()
        for k in ("MW_ASR_BASE_URL", "MW_ASR_MODEL", "MW_ASR_KEY"):
            os.environ.pop(k, None)
        os.environ["MW_SETTINGS"] = self.settings_path
        os.environ.update(self.extra)
        return self

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


# ── S1: 键集守护 (§1/§2 防坑条款) ─────────────────────────────────────

def test_s1_settings_defaults_keyset():
    for k in ("asr_base_url", "asr_model", "asr_key"):
        check(f"S1 {k} 在 SETTINGS_DEFAULTS", k in console.SETTINGS_DEFAULTS)
        # 默认值必须为空串哨兵: 硬编码现值 ⇒ 任何一次保存钉死字面值永久压 env
        check(f"S1 {k} 默认值为空串", console.SETTINGS_DEFAULTS.get(k) == "",
              f"实际={console.SETTINGS_DEFAULTS.get(k)!r}")


# ── S2: 三层链 (settings > env > 代码默认) ────────────────────────────

def test_s2a_settings_file_missing_falls_to_code_default():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "settings.json"  # 不创建 ⇒ 文件缺失
        with _clean_asr_env(p):
            check("S2a 文件缺失 ⇒ 内置默认 base",
                  audio._asr_base() == audio.DEFAULT_ASR_BASE)
            check("S2a 文件缺失 ⇒ 内置默认 model",
                  audio._asr_model() == audio.DEFAULT_ASR_MODEL)
            check("S2a 文件缺失 ⇒ 默认 key none", audio._asr_key() == "none")


def test_s2b_empty_string_falls_to_env():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "settings.json"
        p.write_text(json.dumps({"asr_base_url": "", "asr_model": "",
                                 "asr_key": ""}), encoding="utf-8")
        with _clean_asr_env(p, MW_ASR_BASE_URL="http://env-base:1/v1",
                            MW_ASR_MODEL="env-model", MW_ASR_KEY="env-key"):
            check("S2b 空串穿透 ⇒ env base",
                  audio._asr_base() == "http://env-base:1/v1")
            check("S2b 空串穿透 ⇒ env model",
                  audio._asr_model() == "env-model")
            check("S2b 空串穿透 ⇒ env key", audio._asr_key() == "env-key")


def test_s2c_settings_beats_env():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "settings.json"
        p.write_text(json.dumps({"asr_base_url": "http://settings-base:2/v1",
                                 "asr_model": "settings-model",
                                 "asr_key": "settings-key"}), encoding="utf-8")
        with _clean_asr_env(p, MW_ASR_BASE_URL="http://env-base:1/v1",
                            MW_ASR_MODEL="env-model", MW_ASR_KEY="env-key"):
            check("S2c settings 压 env base",
                  audio._asr_base() == "http://settings-base:2/v1")
            check("S2c settings 压 env model",
                  audio._asr_model() == "settings-model")
            check("S2c settings 压 env key", audio._asr_key() == "settings-key")
        # strip 语义: 纯空白 = 未设置, 穿透
        p.write_text(json.dumps({"asr_base_url": "   ", "asr_model": "  "}),
                     encoding="utf-8")
        with _clean_asr_env(p, MW_ASR_MODEL="env-model"):
            check("S2c 纯空白 strip 后穿透 ⇒ 默认 base",
                  audio._asr_base() == audio.DEFAULT_ASR_BASE)
            check("S2c 纯空白 strip 后穿透 ⇒ env model",
                  audio._asr_model() == "env-model")


def test_s2d_bad_json_penetrates_without_crash():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "settings.json"
        p.write_text("{not-json!!", encoding="utf-8")
        with _clean_asr_env(p, MW_ASR_MODEL="env-model"):
            try:
                base, model, key = (audio._asr_base(), audio._asr_model(),
                                    audio._asr_key())
                ok = True
            except Exception as e:  # noqa: BLE001
                ok, base, model, key = False, repr(e), "", ""
            check("S2d 坏 JSON 不炸且穿透", ok
                  and base == audio.DEFAULT_ASR_BASE
                  and model == "env-model" and key == "none",
                  f"base={base}")


# ── S3: 保存校验 (§5) ─────────────────────────────────────────────────

def test_s3_save_validation():
    with tempfile.TemporaryDirectory() as d:
        sp = Path(d) / "settings.json"
        with mock.patch.object(console, "SETTINGS", str(sp)):
            try:
                console.api_settings_save({"asr_base_url": "javascript:alert(1)"})
                raised = False
            except ValueError:
                raised = True
            check("S3 javascript: 注入被拒 (ValueError⇒422 通道)", raised)
            try:
                console.api_settings_save({"asr_base_url": "ftp://x"})
                raised2 = False
            except ValueError:
                raised2 = True
            check("S3 ftp:// 非 http(s) 被拒", raised2)
            r1 = console.api_settings_save({"asr_base_url": ""})
            check("S3 空串通过", r1["ok"] and r1["settings"]["asr_base_url"] == "")
            r2 = console.api_settings_save({"asr_base_url": "https://x"})
            disk = json.loads(sp.read_text(encoding="utf-8"))
            check("S3 https://x 通过且落盘正确",
                  r2["settings"]["asr_base_url"] == "https://x"
                  and disk["asr_base_url"] == "https://x")
            # 未知键回归: 白名单仍拒
            try:
                console.api_settings_save({"asr_base_urls": "x"})
                unknown_rejected = False
            except ValueError:
                unknown_rejected = True
            check("S3 未知键 asr_base_urls 仍拒 (A9 面)", unknown_rejected)


# ── S4: 保存联动清缓存 (§3 热生效) ────────────────────────────────────

def test_s4_save_clears_status_cache():
    with tempfile.TemporaryDirectory() as d:
        sp = Path(d) / "settings.json"
        with mock.patch.object(console, "SETTINGS", str(sp)):
            audio._status_cache = {"ok": True, "available": True,
                                   "reason": "ok", "checked_at": 0}
            console.api_settings_save({"asr_model": "m2"})
            check("S4 save 含 asr 键 ⇒ 探测缓存被清",
                  audio._status_cache is None,
                  f"cache={audio._status_cache}")
            audio._status_cache = {"ok": True, "available": True,
                                   "reason": "ok", "checked_at": 0}
            console.api_settings_save({"default_timeout_seconds": 1234})
            check("S4 save 不含 asr 键 ⇒ 缓存保留",
                  audio._status_cache is not None)
            audio._status_cache = {"ok": True, "available": True,
                                   "reason": "ok", "checked_at": 0}
            console.api_settings_save({"asr_base_url": "https://y"})
            check("S4 save 含 asr_base_url ⇒ 缓存被清",
                  audio._status_cache is None)
        audio._reset_status_cache()


# ── S5: V5 回归语义 (env 有值 settings 空 ⇒ env) ─────────────────────

def test_s5_env_fallback_regression():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "settings.json"
        p.write_text(json.dumps({"asr_base_url": ""}), encoding="utf-8")
        with _clean_asr_env(p, MW_ASR_BASE_URL="http://127.0.0.1:9/",
                            MW_ASR_MODEL="alt-model"):
            check("S5 settings 空 + env 有值 ⇒ env 生效 (V5 语义)",
                  audio._asr_base() == "http://127.0.0.1:9"
                  and audio._asr_model() == "alt-model")


def main():
    global _COLLECT_ONLY
    _COLLECT_ONLY = True
    tests = [
        test_s1_settings_defaults_keyset,
        test_s2a_settings_file_missing_falls_to_code_default,
        test_s2b_empty_string_falls_to_env,
        test_s2c_settings_beats_env,
        test_s2d_bad_json_penetrates_without_crash,
        test_s3_save_validation,
        test_s4_save_clears_status_cache,
        test_s5_env_fallback_regression,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            check(t.__name__, False, f"EXC: {type(e).__name__}: {e}")
    total = len(PASS) + len(FAIL)
    print()
    if not FAIL:
        print(f"\033[92m=== ALL {total} ASR-SET TESTS PASSED ===\033[0m")
        return 0
    print(f"\033[91m=== {len(FAIL)}/{total} FAILED ===\033[0m")
    for n, d in FAIL:
        print(f"  - {n}: {d}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
