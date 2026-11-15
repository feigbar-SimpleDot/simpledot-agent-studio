"""mw.audio_proxy — 语音转写转发 (issue #232, PROPOSAL-V2 推荐路径 a)。

职责 (UX-PROTOTYPE §6 / PROPOSAL-V2 §1.2): console.py 只做路由薄壳, 本模块
承载全部实现 —— 能力探测 `voice_status()` 与转写代理 `transcribe()`, 纯 stdlib。

上游 = 外部 ASR (OpenAI 兼容, 已实测契约 PROPOSAL-V2 §2.5):
  GET  {base}/models                → 200 {"data":[{"id":...},...]}
  POST {base}/audio/transcriptions  → 200 {"text":"..."}  (multipart file+model)
  仅收 WAV (PCM RIFF/WAVE); Authorization 任意值 (无真实鉴权)。
浏览器从不直连 ASR (无 CORS 问题), 全部经 mw 同源端点转发。

上游配置三层链 (ASR-SET: 设置页优先, env 为部署期兜底; 未来切 dashboard 备用
路径 b 只改配置不改代码):
  1. settings.json 的 asr_base_url / asr_model / asr_key —— 设置页「语音转写
     (ASR)」分组, 每次调用实时读(无缓存, 保存即热生效); 空串/缺失 = 未设置, 穿透。
  2. env MW_ASR_BASE_URL / MW_ASR_MODEL / MW_ASR_KEY —— 部署期兜底 (DEPLOY.md §3)。
  3. 内置默认 DEFAULT_ASR_BASE http://<your-lan-ip>:8731/v1 /
     DEFAULT_ASR_MODEL qwen3-asr-1.7b / key "none" (外部 ASR 无真实鉴权, 任意值即可)。

错误口径 (PROPOSAL-V2 §1.3 护栏 / UX-PROTOTYPE S6):
  空转写 = 200 + text:"" (静音正常返回, 前端按「未识别到语音」提示, 不报错);
  上游不可达/超时 = 503 + error:"asr_unreachable" (前端提示改用文本);
  上游 HTTP 非 200 = 502 + error:"asr_error";
  请求体非法 = 400 + error:"bad_payload"/"not_wav"。
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# ── 配置 (三层链: settings asr_* > env MW_ASR_* > 内置默认, 见模块 docstring) ──
# ⚠ 内置默认值同步面 (ASR-SET §8): 若 <your-lan-ip>:8731 / qwen3-asr-1.7b 变更,
#   须同批改: 本两常量 + ui i18n settings.row.asr_*_hint 文案 + design.md §11.2/§11.7
#   + DEPLOY.md §3 —— 本处是默认值唯一真源, SETTINGS_DEFAULTS 刻意留空串不放副本。
DEFAULT_ASR_BASE = "http://<your-lan-ip>:8731/v1"
DEFAULT_ASR_MODEL = "qwen3-asr-1.7b"

STATUS_TTL_S = 30          # 探测结果内存缓存 TTL (PROPOSAL-V2 §3.2: 30-60s)
STATUS_TIMEOUT_S = 2       # /models 探测超时 (§3.2: 超时 2s)
TRANSCRIBE_TIMEOUT_S = 60  # 转写超时 (实测单次含模型加载约 10s, 留足余量)
WAV_MAX_BYTES = 8 * 1024 * 1024  # 解码后音频上限 (60s WAV≈1.9MB, 4 倍余量)

_status_lock = threading.Lock()
_status_cache: dict | None = None  # {ok, available, reason, checked_at}


def _settings_path() -> str:
    # 与 mw/console.py:58 SETTINGS、mw/pixel_proxy.py:41 settings_path() 同口径
    return os.environ.get(
        "MW_SETTINGS",
        os.path.join(os.environ.get("MW_HOME", "/mw_data"), "settings.json"))


def _setting(key: str) -> str:
    """读 settings.json 单键 (strip); 文件缺失/坏 JSON/键缺失/非字符串 ⇒ ""(穿透)。"""
    try:
        raw = json.loads(Path(_settings_path()).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    v = raw.get(key) if isinstance(raw, dict) else None
    return v.strip() if isinstance(v, str) else ""


def _asr_base() -> str:
    # 优先级链: settings asr_base_url > env MW_ASR_BASE_URL > DEFAULT_ASR_BASE
    v = _setting("asr_base_url") or os.environ.get("MW_ASR_BASE_URL") \
        or DEFAULT_ASR_BASE
    return v.rstrip("/")


def _asr_model() -> str:
    return (_setting("asr_model") or os.environ.get("MW_ASR_MODEL")
            or DEFAULT_ASR_MODEL)


def _asr_key() -> str:
    # 外部 ASR 无真实鉴权 (PROPOSAL-V2 §2.5: key 任意字符串实测通过)
    return (_setting("asr_key") or os.environ.get("MW_ASR_KEY") or "none")


def _reset_status_cache() -> None:
    """清探测缓存 (单测隔离用; 前端强刷走 voice_status(force=True))。"""
    global _status_cache
    with _status_lock:
        _status_cache = None


# ── GET /api/audio/voice-status: 能力探测 (PROPOSAL-V2 §3.2) ──────────

def voice_status(force: bool = False) -> dict:
    """端点可达 + 模型在列 两级短路探测, 结果缓存 STATUS_TTL_S 秒。

    返回 {ok:true, available:bool, reason:str, checked_at:float}:
      reason: "ok" | "asr_unreachable" (连接失败) | "asr_model_missing" (200 但模型不在列)。
    不做真实转写探测 (§3.2 第 3 条: 太慢且有副作用); 残余风险由转写时 503 降级兜底。
    """
    global _status_cache
    now = time.time()
    if not force and _status_cache and now - _status_cache["checked_at"] < STATUS_TTL_S:
        return dict(_status_cache)
    with _status_lock:
        # 锁内双检: 并发线程可能已在等锁期间完成刷新
        now = time.time()
        if not force and _status_cache \
                and now - _status_cache["checked_at"] < STATUS_TTL_S:
            return dict(_status_cache)
        available, reason = False, "asr_unreachable"
        try:
            req = urllib.request.Request(
                _asr_base() + "/models",
                headers={"Authorization": f"Bearer {_asr_key()}"})
            with urllib.request.urlopen(req, timeout=STATUS_TIMEOUT_S) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            ids = [m.get("id") for m in (data.get("data") or [])
                   if isinstance(m, dict)]
            if _asr_model() in ids:
                available, reason = True, "ok"
            else:
                available, reason = False, "asr_model_missing"
        except Exception as e:  # 连接失败/超时/非 JSON 一律按不可达降级
            logging.getLogger("mw.console").info(
                "voice-status probe failed: %s", e)
            available, reason = False, "asr_unreachable"
        _status_cache = {"ok": True, "available": available, "reason": reason,
                         "checked_at": time.time()}
        return dict(_status_cache)


# ── POST /api/audio/transcribe: 转写代理 (PROPOSAL-V2 §1.1) ───────────

def _multipart_wav(audio: bytes, model: str, boundary: str) -> bytes:
    """stdlib 手拼 multipart/form-data (file=audio.wav + model), 零新依赖。"""
    crlf = b"\r\n"
    parts = [
        f"--{boundary}".encode() + crlf
        + b'Content-Disposition: form-data; name="model"' + crlf + crlf
        + model.encode("utf-8") + crlf,
        f"--{boundary}".encode() + crlf
        + b'Content-Disposition: form-data; name="file"; filename="audio.wav"'
        + crlf + b"Content-Type: audio/wav" + crlf + crlf + audio + crlf,
        f"--{boundary}--".encode() + crlf,
    ]
    return b"".join(parts)


def transcribe(payload: dict) -> tuple[int, dict]:
    """dataURL(WAV) → 外部 ASR multipart 转写。返回 (http_code, body)。

    成功 200 {ok:true, text}; 空转写同样 200 + text:"" (S6a 口径归前端);
    非法请求 400/413; 上游不可达 503; 上游 HTTP 错误 502。
    """
    data_url = str((payload or {}).get("data_url") or "")
    if not data_url.startswith("data:") or ";base64," not in data_url:
        return 400, {"ok": False, "error": "bad_payload",
                     "detail": "data_url must be a base64 data URL"}
    try:
        audio = base64.b64decode(data_url.split(";base64,", 1)[1])
    except (binascii.Error, ValueError):
        return 400, {"ok": False, "error": "bad_payload",
                     "detail": "base64 decode failed"}
    if len(audio) > WAV_MAX_BYTES:
        return 413, {"ok": False, "error": "audio_too_large"}
    # 外部 ASR 只收 WAV PCM (PROPOSAL-V2 §2.5 实测 webm/ogg 400 拒收);
    # 本地预检 RIFF/WAVE 魔数, 非法格式早退不浪费上游往返。
    if len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        return 400, {"ok": False, "error": "not_wav"}
    boundary = "----sasAudio" + uuid.uuid4().hex
    body = _multipart_wav(audio, _asr_model(), boundary)
    req = urllib.request.Request(
        _asr_base() + "/audio/transcriptions", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": f"Bearer {_asr_key()}"})
    try:
        with urllib.request.urlopen(req, timeout=TRANSCRIBE_TIMEOUT_S) as r:
            raw = json.loads(r.read().decode("utf-8", "replace"))
        return 200, {"ok": True, "text": str(raw.get("text") or "")}
    except urllib.error.HTTPError as e:
        logging.getLogger("mw.console").info(
            "asr upstream HTTP %s on transcribe", e.code)
        return 502, {"ok": False, "error": "asr_error",
                     "detail": f"upstream HTTP {e.code}"}
    except Exception as e:
        logging.getLogger("mw.console").info(
            "asr unreachable on transcribe: %s", e)
        return 503, {"ok": False, "error": "asr_unreachable"}
