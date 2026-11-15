"""#232 守护: 语音创建任务 mw 端点 (audio_proxy 转写代理 + 能力探测)。

依据: docs/voice-eval/PROPOSAL-V2.md §1.1/§1.3/§3 (推荐路径 a: mw 代理直连
外部 ASR) + docs/issue-convergence-HH/ADJUDICATION.md §2.3。外部 ASR 契约
(§2.5 实测): GET {base}/models 200 含模型; POST {base}/audio/transcriptions
multipart file+model → 200 {"text":...}; 仅收 WAV PCM。

覆盖:
  V 组  voice_status: 可用 / 不可达 / 模型缺列 / TTL 缓存 / force 强探 / env 上游可配
  T 组  transcribe: 成功 / 空转写 / 上游不可达 503 / 上游 HTTP 错 502 /
                    非法 payload 400 / 非 WAV 400 / 超大 413 / multipart 形态
  R 组  console 路由: GET /api/audio/voice-status (?force=1 透传) /
                      POST /api/audio/transcribe (code+body 透传)

双口径断言 (照抄 #193/#190 守护):
  pytest 收集口径 —— check() 失败即抛 AssertionError;
  脚本直跑口径   —— main() 置 _COLLECT_ONLY=True, 只收集不抛, rc 汇总。
"""
from __future__ import annotations

import base64
import io
import json
import os
import struct
import sys
import urllib.error
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


# ── 夹具 ────────────────────────────────────────────────────────────────

class _FakeResp(io.BytesIO):
    """urlopen 返回值替身: 可 with, .read() 剩余字节。"""

    def __init__(self, payload: bytes, status: int = 200):
        super().__init__(payload)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def _models_resp(model_id: str = audio.DEFAULT_ASR_MODEL) -> _FakeResp:
    return _FakeResp(json.dumps(
        {"data": [{"id": model_id, "task": "transcribe"}]}).encode())


def _wav_bytes(n: int = 100) -> bytes:
    """最小合法 WAV: RIFF/WAVE 头 + n 字节 data (满足本地魔数预检)。"""
    return (b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE"
            + b"fmt " + struct.pack("<I", 16) + b"\x00" * 16
            + b"data" + struct.pack("<I", n) + b"\x00" * n)


def _wav_data_url(n: int = 100) -> str:
    return ("data:audio/wav;base64,"
            + base64.b64encode(_wav_bytes(n)).decode())


class _FakeRfile:
    def __init__(self, body: bytes = b""):
        self.body = body

    def read(self, n):
        return self.body[:n]


class _FakeHandler(console.Handler):
    """绕开 BaseHTTPRequestHandler.__init__ (不联网), 照抄 #193 守护夹具。"""

    def __init__(self, path, headers=None, body=b""):
        self.path = path
        self.headers = headers or {}
        self.status = None
        self.sent_headers = []
        self.body = b""
        self.wfile = self
        self.rfile = _FakeRfile(body)

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.sent_headers.append((k, v))

    def end_headers(self):
        pass

    def write(self, chunk):
        self.body += bytes(chunk)
        return len(chunk)


# ── V 组: voice_status ─────────────────────────────────────────────────

def test_v1_available():
    audio._reset_status_cache()
    with mock.patch("mw.audio_proxy.urllib.request.urlopen",
                    return_value=_models_resp()) as m:
        d = audio.voice_status()
    check("V1 available=true reason=ok",
          d["available"] is True and d["reason"] == "ok" and d["ok"] is True,
          repr(d))
    check("V1 checked_at 为数值", isinstance(d["checked_at"], (int, float)))
    check("V1 打了 /models 端点", "/models" in m.call_args[0][0].full_url,
          m.call_args[0][0].full_url)


def test_v2_unreachable():
    audio._reset_status_cache()
    with mock.patch("mw.audio_proxy.urllib.request.urlopen",
                    side_effect=OSError("connection refused")):
        d = audio.voice_status()
    check("V2 连接失败 ⇒ asr_unreachable",
          d["available"] is False and d["reason"] == "asr_unreachable", repr(d))


def test_v3_model_missing():
    audio._reset_status_cache()
    with mock.patch("mw.audio_proxy.urllib.request.urlopen",
                    return_value=_models_resp("some-other-model")):
        d = audio.voice_status()
    check("V3 200 但模型不在列 ⇒ asr_model_missing",
          d["available"] is False and d["reason"] == "asr_model_missing", repr(d))


def test_v4_ttl_cache_and_force():
    audio._reset_status_cache()
    with mock.patch("mw.audio_proxy.urllib.request.urlopen",
                    return_value=_models_resp()) as m:
        audio.voice_status()
        n1 = m.call_count
        audio.voice_status()          # TTL 内 ⇒ 吃缓存
        n2 = m.call_count
        audio.voice_status(force=True)  # force ⇒ 重探
        n3 = m.call_count
    check("V4 首探 1 次", n1 == 1, f"n1={n1}")
    check("V4 TTL 内不重探", n2 == n1, f"n2={n2}")
    check("V4 force 重探", n3 == n1 + 1, f"n3={n3}")


def test_v5_env_upstream_configurable():
    audio._reset_status_cache()
    with mock.patch.dict(os.environ, {"MW_ASR_BASE_URL": "http://127.0.0.1:9/",
                                      "MW_ASR_MODEL": "alt-model"}), \
         mock.patch("mw.audio_proxy.urllib.request.urlopen",
                    return_value=_models_resp("alt-model")) as m:
        d = audio.voice_status()
    check("V5 env 覆盖上游 base+model 生效",
          d["available"] is True
          and m.call_args[0][0].full_url == "http://127.0.0.1:9/models",
          f"{d} url={m.call_args[0][0].full_url}")
    audio._reset_status_cache()


# ── T 组: transcribe ───────────────────────────────────────────────────

def test_t1_transcribe_success():
    with mock.patch("mw.audio_proxy.urllib.request.urlopen",
                    return_value=_FakeResp(json.dumps(
                        {"text": "帮我建个任务", "usage": {}}).encode())) as m:
        code, body = audio.transcribe({"data_url": _wav_data_url()})
    check("T1 200 + ok + text",
          code == 200 and body == {"ok": True, "text": "帮我建个任务"}, repr(body))
    req = m.call_args[0][0]
    ctype = req.get_header("Content-type") or ""
    check("T1 打 /audio/transcriptions",
          req.full_url.endswith("/audio/transcriptions"), req.full_url)
    check("T1 multipart 形态 (model 字段 + file=audio.wav + WAV 字节在体内)",
          b'name="model"' in req.data and b'filename="audio.wav"' in req.data
          and _wav_bytes() in req.data
          and ctype.startswith("multipart/form-data; boundary="),
          f"ctype={ctype}")


def test_t2_empty_transcript():
    with mock.patch("mw.audio_proxy.urllib.request.urlopen",
                    return_value=_FakeResp(b'{"text":"","usage":{"seconds":1}}')):
        code, body = audio.transcribe({"data_url": _wav_data_url()})
    check("T2 空转写 = 200 ok text='' (S6a 归前端提示, 不报错)",
          code == 200 and body == {"ok": True, "text": ""}, repr(body))


def test_t3_upstream_unreachable_503():
    with mock.patch("mw.audio_proxy.urllib.request.urlopen",
                    side_effect=OSError("connect timeout")):
        code, body = audio.transcribe({"data_url": _wav_data_url()})
    check("T3 不可达 ⇒ 503 asr_unreachable (机器可读 reason)",
          code == 503 and body.get("error") == "asr_unreachable", repr(body))


def test_t4_upstream_http_error_502():
    err = urllib.error.HTTPError("http://x", 400, "Bad Request", None, None)
    with mock.patch("mw.audio_proxy.urllib.request.urlopen", side_effect=err):
        code, body = audio.transcribe({"data_url": _wav_data_url()})
    check("T4 上游 HTTP 错 ⇒ 502 asr_error",
          code == 502 and body.get("error") == "asr_error", repr(body))


def test_t5_bad_payload():
    code, body = audio.transcribe({})
    check("T5a 缺 data_url ⇒ 400 bad_payload",
          code == 400 and body.get("error") == "bad_payload", repr(body))
    code, body = audio.transcribe({"data_url": "not-a-data-url"})
    check("T5b 非 data URL ⇒ 400 bad_payload",
          code == 400 and body.get("error") == "bad_payload", repr(body))


def test_t6_not_wav_rejected():
    du = "data:audio/webm;base64," + base64.b64encode(b"\x1a\x45\xdf\xa3" * 20).decode()
    code, body = audio.transcribe({"data_url": du})
    check("T6 非 WAV (RIFF/WAVE 魔数不符) ⇒ 400 not_wav 本地预检",
          code == 400 and body.get("error") == "not_wav", repr(body))


def test_t7_oversize_413():
    big = _wav_bytes(audio.WAV_MAX_BYTES + 1)
    du = "data:audio/wav;base64," + base64.b64encode(big).decode()
    with mock.patch("mw.audio_proxy.urllib.request.urlopen") as m:
        code, body = audio.transcribe({"data_url": du})
    check("T7 超 WAV_MAX_BYTES ⇒ 413 且不打上游",
          code == 413 and body.get("error") == "audio_too_large"
          and m.call_count == 0, repr(body))


# ── R 组: console 路由 ─────────────────────────────────────────────────

def test_r1_get_voice_status_route():
    h = _FakeHandler("/api/audio/voice-status")
    with mock.patch.object(console._audio, "voice_status",
                           return_value={"ok": True, "available": True,
                                         "reason": "ok", "checked_at": 1.0}) as m:
        h.do_GET()
    d = json.loads(h.body)
    check("R1 GET voice-status ⇒ 200 + 探测体",
          h.status == 200 and d.get("available") is True, f"{h.status} {d}")
    check("R1 默认 force=False", m.call_args.kwargs.get("force") is False)


def test_r2_get_voice_status_force_param():
    h = _FakeHandler("/api/audio/voice-status?force=1")
    with mock.patch.object(console._audio, "voice_status",
                           return_value={"ok": True, "available": False,
                                         "reason": "asr_unreachable",
                                         "checked_at": 1.0}) as m:
        h.do_GET()
    check("R2 ?force=1 ⇒ voice_status(force=True)",
          h.status == 200 and m.call_args.kwargs.get("force") is True)


def test_r3_post_transcribe_route_passthrough():
    payload = json.dumps({"data_url": _wav_data_url()}).encode()
    h = _FakeHandler("/api/audio/transcribe",
                     {"Content-Length": str(len(payload))}, payload)
    with mock.patch.object(console._audio, "transcribe",
                           return_value=(200, {"ok": True, "text": "hi"})) as m:
        h.do_POST()
    d = json.loads(h.body)
    check("R3 POST transcribe code+body 透传",
          h.status == 200 and d == {"ok": True, "text": "hi"}, f"{h.status} {d}")
    check("R3 payload 透传 data_url",
          m.call_args[0][0].get("data_url", "").startswith("data:audio/wav;base64,"))


def test_r4_post_transcribe_error_code_passthrough():
    payload = json.dumps({"data_url": "x"}).encode()
    h = _FakeHandler("/api/audio/transcribe",
                     {"Content-Length": str(len(payload))}, payload)
    with mock.patch.object(console._audio, "transcribe",
                           return_value=(503, {"ok": False,
                                               "error": "asr_unreachable"})):
        h.do_POST()
    check("R4 503 降级码经路由透传",
          h.status == 503 and json.loads(h.body).get("error") == "asr_unreachable")


def main():
    global _COLLECT_ONLY
    _COLLECT_ONLY = True
    tests = [
        test_v1_available,
        test_v2_unreachable,
        test_v3_model_missing,
        test_v4_ttl_cache_and_force,
        test_v5_env_upstream_configurable,
        test_t1_transcribe_success,
        test_t2_empty_transcript,
        test_t3_upstream_unreachable_503,
        test_t4_upstream_http_error_502,
        test_t5_bad_payload,
        test_t6_not_wav_rejected,
        test_t7_oversize_413,
        test_r1_get_voice_status_route,
        test_r2_get_voice_status_force_param,
        test_r3_post_transcribe_route_passthrough,
        test_r4_post_transcribe_error_code_passthrough,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            check(t.__name__, False, f"EXC: {type(e).__name__}: {e}")
    total = len(PASS) + len(FAIL)
    print()
    if not FAIL:
        print(f"\033[92m=== ALL {total} #232 TESTS PASSED ===\033[0m")
        return 0
    print(f"\033[91m=== {len(FAIL)}/{total} FAILED ===\033[0m")
    for n, d in FAIL:
        print(f"  - {n}: {d}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
