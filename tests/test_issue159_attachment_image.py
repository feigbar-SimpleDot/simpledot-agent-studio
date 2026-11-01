"""issue #159 图片附件服务端压缩端点独立单测 (T-159-A..H + 质量下限 + 路由守护)。

依据: docs/PLAN-L-20261029.md §1.8 用例组 + §7.1 MV-159-* 变异反证的牙齿。

设计 (方案 A, 与 tests/test_fs_api.py 同形态):
  pytest 入口用 subprocess 把本文件当脚本再跑一次, 真测试在子进程里完成。
  ★ 理由: 真测试需要 PIL, 而 pytest 宿主解释器不保证有 PIL (方案 B 的
  importorskip 会让 AC-159-4 在缺依赖机器上静默跳过 = "看起来测了其实没测",
  违背本仓 fail-loud 纪律)。子进程用 sys.executable —— 与 pytest 同解释器,
  mw 运行时 (venv) 下 PIL 可 import。

跑法:
  - 直接: python3 tests/test_issue159_attachment_image.py
  - pytest: pytest tests/test_issue159_attachment_image.py -q
"""
import base64
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve()
IS_PYTEST = __name__ != '__main__'


def _pick_python_with_pil():
    """挑一个能 import PIL 的解释器跑真测试 (方案 A 的落地)。

    优先级: PYTHON_BIN env (与 mw/console.py `_cron_bridge` 同口径, 缺省
    /opt/hermes/.venv/bin/python3) > sys.executable。都不可用 ⇒ fail-loud
    (不静默 skip: AC-159-4 的"有独立单测"不能变成"看起来测了其实没测")。
    """
    import subprocess as _sp
    cands = [os.environ.get("PYTHON_BIN", "/opt/hermes/.venv/bin/python3"),
             sys.executable]
    for py in cands:
        if not py:
            continue
        try:
            r = _sp.run([py, "-c", "import PIL"],
                        capture_output=True, timeout=30)
            if r.returncode == 0:
                return py
        except Exception:
            continue
    raise RuntimeError(
        "issue159 测试需要可 import PIL 的解释器: 设 PYTHON_BIN 指向 "
        "mw 运行时 python (实测 /opt/hermes/.venv/bin/python3 可用)")


def _run_subprocess():
    """把当前文件当脚本再跑一次: __main__ 段是真测试 (在带 PIL 的解释器里)。"""
    py = _pick_python_with_pil()
    r = subprocess.run(
        [py, str(HERE)],
        capture_output=True, text=True, cwd=str(ROOT),
        env={**os.environ, 'LANG': 'en_US.UTF-8'},
        timeout=300,
    )
    print(r.stdout)
    if r.returncode != 0:
        print('--- STDERR ---', file=sys.stderr)
        print(r.stderr, file=sys.stderr)
    return r.returncode


if IS_PYTEST:
    # pytest 入口: 注册一个 fake test 函数, 内部跑 subprocess。
    # 避免模块顶层 sys.exit (会触发 pytest INTERNALERROR)。
    def test_issue159_subprocess():
        rc = _run_subprocess()
        assert rc == 0, f"issue159 子进程退出码 {rc} (非 0)"


# ──────────────────────────────────────────────────────────────────
# 真测试段 (script-style, 直接跑 `python3 tests/test_issue159_attachment_image.py`)
# ──────────────────────────────────────────────────────────────────
if __name__ == '__main__':  # noqa: F811 — 再被脚本入口执行
    sys.path.insert(0, str(ROOT))
    import tempfile

    from PIL import Image

    from mw import console as C

    PASS = []
    FAIL = []

    def check(name, cond, detail=""):
        (PASS if cond else FAIL).append(name)
        print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))

    def png_bytes(im, fmt="PNG"):
        b = io.BytesIO()
        im.save(b, fmt)
        return b.getvalue()

    def call(raw_or_b64, name="t.png", body_bytes=None, mime=None):
        if isinstance(raw_or_b64, (bytes, bytearray)):
            b64 = base64.b64encode(raw_or_b64).decode()
        else:
            b64 = raw_or_b64
        data = f"data:{mime};base64,{b64}" if mime else b64
        if body_bytes is None:
            body_bytes = len(data)
        return C.api_attachments_image({"name": name, "data": data},
                                       body_bytes=body_bytes)

    def noisy_jpeg(w, h):
        """高熵 JPEG (不可再压), 用于体积边界与超限用例。"""
        return png_bytes(Image.frombytes("RGB", (w, h), os.urandom(w * h * 3)),
                         "JPEG")

    def gradient_png(w, h):
        """结构化渐变 PNG: 大尺寸但 JPEG 可压。"""
        im = Image.new("RGB", (w, h))
        px = im.load()
        for y in range(h):
            for x in range(0, w, 7):
                v = (x * 3 + y * 5) % 256
                for dx in range(min(7, w - x)):
                    px[x + dx, y] = (v, (v * 2) % 256, (v * 3 + x) % 256)
        return png_bytes(im)

    # ══ T-159-A 尺寸边界 ══════════════════════════════════════════
    raw = gradient_png(4000, 3000)  # 330KB < 1MB ⇒ 直通 (尺寸大但体积未超阈值)
    r = call(raw, "big.png", mime="image/png")
    check("A1 大尺寸小体积直通: compressed=False + originalWidth=4000",
          r["code"] == 200 and r["compressed"] is False
          and r["originalWidth"] == 4000 and r["width"] == 4000,
          json.dumps({k: r.get(k) for k in ("code", "compressed", "width", "originalWidth")}))

    raw = noisy_jpeg(2000, 1500)  # 高熵 >1MB ⇒ 压缩路径, 长边未超 2048 不缩放
    r = call(raw, "m.jpg", mime="image/jpeg")
    check("A2 长边<=2048 不缩放: width==2000 且 compressed=True",
          r["code"] == 200 and r["compressed"] is True and r["width"] == 2000,
          json.dumps({k: r.get(k) for k in ("code", "compressed", "width", "error")}))

    raw = noisy_jpeg(3000, 3000)
    r = call(raw, "sq.jpg", mime="image/jpeg")
    check("A3 正方形 3000x3000 → 2048x2048",
          r["code"] == 200 and r["width"] == 2048 and r["height"] == 2048,
          json.dumps({k: r.get(k) for k in ("code", "width", "height", "error")}))

    tiny = png_bytes(Image.new("RGB", (1, 1), (255, 0, 0)))
    r = call(tiny, "px.png", mime="image/png")
    check("A4 极小图直通: compressed=False + bytes==原字节数",
          r["code"] == 200 and r["compressed"] is False and r["bytes"] == len(tiny))

    # ══ T-159-B 体积边界 ══════════════════════════════════════════
    raw = noisy_jpeg(2000, 1500)
    r = call(raw, "b.jpg", mime="image/jpeg")
    check("B1 压缩后 <= 2MB", r["code"] == 200 and r["bytes"] <= 2_097_152,
          f"bytes={r.get('bytes')}")
    # 边界含端点: 判定是 `> ATTACH_IMG_MAX_OUT` (等号放行)。静态锁判定形态,
    # 防止改成 >= 后边界图被误拒 (MV 语义: 端点行为有牙齿)。
    src = (ROOT / "mw" / "console.py").read_text(encoding="utf-8")
    check("B2 2MB 边界含端点 (判定为 > 而非 >=)",
          "if len(out) > ATTACH_IMG_MAX_OUT:" in src)

    raw = noisy_jpeg(4000, 4000)  # 高熵大图: 走完整降质降尺寸阶梯
    r = call(raw, "huge.jpg", mime="image/jpeg")
    check("B3a 高熵大图阶梯收敛: 最终 <=2MB 或 413 (绝不回退原图)",
          (r["code"] == 200 and r["bytes"] <= 2_097_152)
          or (r["code"] == 413 and r.get("error") == "still_too_large"
              and "dataUrl" not in r),
          json.dumps({k: r.get(k) for k in ("code", "error", "bytes")}))

    # B3b 阶梯仍超限的确定性构造: monkeypatch 编码函数返回恒定大字节,
    # 模拟"怎么压都 >2MB"的极端载荷 ⇒ 必须 413 且响应不含 dataUrl。
    # (诚实标注: 真实图片经 1600/q75 兜底档几乎总能压进 2MB, 该分支靠
    #  构造触发; MV-159-B "回退返回原图" 变异正是被这条钉住。)
    _orig_jpeg = C._attach_img_jpeg
    try:
        C._attach_img_jpeg = lambda im, quality: b"\xff" * (3 * 1024 * 1024)
        r = call(noisy_jpeg(2000, 1500), "stub.jpg", mime="image/jpeg")
        check("B3b 压缩后仍>2MB → 413 still_too_large (绝不回退原图)",
              r["code"] == 413 and r.get("error") == "still_too_large"
              and "dataUrl" not in r,
              json.dumps({k: r.get(k) for k in ("code", "error")}))
    finally:
        C._attach_img_jpeg = _orig_jpeg

    raw = os.urandom(24 * 1024 * 1024 + 10)  # 解码后 >24MB
    # body_bytes=0: 模拟 Content-Length 未知 (chunked) 时端点仍须卡解码后
    # 上限 —— 纵深防御: 32MB body 与 24MB decoded 两道闸各自独立生效。
    r = call(raw, "over.png", mime="image/png", body_bytes=0)
    check("B4 解码后 >24MB → 413 image_too_large",
          r["code"] == 413 and r.get("error") == "image_too_large",
          json.dumps({k: r.get(k) for k in ("code", "error")}))

    r = C.api_attachments_image({"name": "x", "data": "x"},
                                body_bytes=32 * 1024 * 1024 + 1)
    check("B5 请求体 >32MB → 413 payload_too_large",
          r["code"] == 413 and r.get("error") == "payload_too_large")

    # ══ T-159-C 非图片 ════════════════════════════════════════════
    r = call(b"hello world this is plain text, not an image")
    check("C1 纯文本 base64 → 400 not_an_image/decode_failed",
          r["code"] == 400 and r.get("error") in ("not_an_image", "decode_failed"))

    pdf = (b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
           b"trailer\n<< /Root 1 0 R >>\n%%EOF")
    r = call(pdf, "doc.pdf")
    check("C2 PDF base64 → 400 not_an_image/decode_failed",
          r["code"] == 400 and r.get("error") in ("not_an_image", "decode_failed"))

    r = C.api_attachments_image({"name": "a"}, body_bytes=10)
    check("C3 缺 data → 400 missing_data",
          r["code"] == 400 and r.get("error") == "missing_data")

    r = C.api_attachments_image({"data": "!!!not base64!!!"}, body_bytes=20)
    check("C4 非法 base64 → 400 bad_base64",
          r["code"] == 400 and r.get("error") == "bad_base64")

    # ══ T-159-D 损坏图片 ══════════════════════════════════════════
    r = call(os.urandom(200), "rnd.png", mime="image/png")
    check("D1 随机字节 → 400 decode_failed/not_an_image",
          r["code"] == 400 and r.get("error") in ("decode_failed", "not_an_image"))

    r = call(b"\x89PNG\r\n\x1a\n" + os.urandom(64), "trunc.png", mime="image/png")
    check("D2 截断 PNG (合法头+随机) → 400 decode_failed",
          r["code"] == 400 and r.get("error") == "decode_failed")

    # ══ T-159-E 特殊图 ════════════════════════════════════════════
    # 高熵 RGBA >1MB 保证走压缩路径 (纯色 RGBA PNG 体积很小会直通, 属设计内)
    raw = png_bytes(Image.frombytes("RGBA", (2000, 1500),
                                    os.urandom(2000 * 1500 * 4)))
    r = call(raw, "alpha.png", mime="image/png")
    check("E1 RGBA 透明 PNG → 成功输出 JPEG (不抛 cannot write mode RGBA)",
          r["code"] == 200 and r.get("mime") == "image/jpeg",
          json.dumps({k: r.get(k) for k in ("code", "error", "mime")}))

    # 高熵 >1MB 保证走压缩路径 (直通路径零重编码, EXIF 标签原样保留由
    # 模型侧解析, 属设计内行为)
    im = Image.frombytes("RGB", (2000, 1000), os.urandom(2000 * 1000 * 3))
    exif = im.getexif()
    exif[0x0112] = 6  # Orientation=6: 声明应顺时针转 90° (宽高互换)
    b = io.BytesIO(); im.save(b, "JPEG", exif=exif.tobytes()); raw = b.getvalue()
    r = call(raw, "rot.jpg", mime="image/jpeg")
    check("E2 EXIF orientation=6 → 输出宽高已按 EXIF 校正 (1000x2000)",
          r["code"] == 200 and r["width"] == 1000 and r["height"] == 2000
          and r["originalWidth"] == 2000 and r["originalHeight"] == 1000,
          json.dumps({k: r.get(k) for k in ("code", "width", "height",
                                            "originalWidth", "originalHeight")}))

    # 注意: 必须 RGB→P 转换构造, 直接 Image.new("P") 同色索引帧会被 Pillow
    # 去重成单帧 (实测 n_frames==1)
    g1 = Image.new("RGB", (100, 100), (255, 0, 0)).convert("P")
    g2 = Image.new("RGB", (100, 100), (0, 255, 0)).convert("P")
    b = io.BytesIO()
    g1.save(b, "GIF", save_all=True, append_images=[g2], duration=100, loop=0)
    r = call(b.getvalue(), "anim.gif", mime="image/gif")
    check("E3 动画 GIF → 首帧 + originalFormat 含 animated + animatedFrames==2",
          r["code"] == 200 and "animated" in (r.get("originalFormat") or "")
          and r.get("meta", {}).get("animatedFrames") == 2,
          json.dumps({k: r.get(k) for k in ("code", "originalFormat", "meta")}))

    # ══ T-159-F Pillow 缺失降级 ═══════════════════════════════════
    # 子进程里用 meta_path finder 屏蔽 PIL, 断言 503 且响应体不含原图字节
    # (钉死 "不回退裸内联" —— MV-159-C 的牙齿)。
    with tempfile.TemporaryDirectory() as td:
        script = Path(td) / "no_pil_probe.py"
        probe_img = png_bytes(Image.new("RGB", (10, 10), (9, 9, 9)))
        script.write_text(
            "import base64, json, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "class _NoPIL:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'PIL' or name.startswith('PIL.'):\n"
            "            raise ImportError('PIL blocked (issue159 T-F)')\n"
            "        return None\n"
            "sys.meta_path.insert(0, _NoPIL())\n"
            "try:\n"
            "    import PIL  # noqa: F401\n"
            "    print('PIL-STILL-IMPORTABLE')\n"
            "    sys.exit(2)\n"
            "except ImportError:\n"
            "    pass\n"
            "from mw import console as C\n"
            f"img = base64.b64decode({base64.b64encode(probe_img).decode()!r})\n"
            "r = C.api_attachments_image({'name': 'p.png', 'data': 'data:image/png;base64,'"
            " + base64.b64encode(img).decode()}, body_bytes=100)\n"
            "body = json.dumps(r)\n"
            "orig_b64 = base64.b64encode(img).decode()\n"
            "print(json.dumps({'code': r.get('code'), 'error': r.get('error'),"
            " 'leak': orig_b64 in body or 'dataUrl' in r}))\n",
            encoding="utf-8")
        p = subprocess.run([sys.executable, str(script)],
                           capture_output=True, text=True, timeout=60)
        try:
            out = json.loads(p.stdout.strip().splitlines()[-1])
        except Exception:
            out = {}
        check("F1 Pillow 缺失 → 503 pillow_unavailable 且响应体不含原图字节",
              out.get("code") == 503 and out.get("error") == "pillow_unavailable"
              and out.get("leak") is False,
              p.stdout.strip() + p.stderr[-200:])

    # ══ T-159-G 零回显 ════════════════════════════════════════════
    bodies = [
        call(b"hello world text", "g.txt"),
        call(os.urandom(200), "g.png", mime="image/png"),
        call(b"\x89PNG\r\n\x1a\n" + os.urandom(64), "g2.png", mime="image/png"),
        C.api_attachments_image({"data": "!!!"}, body_bytes=10),
        C.api_attachments_image({"name": "a"}, body_bytes=10),
    ]
    joined = json.dumps(bodies)
    _FIXED_DETAILS = {
        "attachment data is required",
        "attachment data is not valid base64",
        "request body exceeds 32MB limit",
        "image exceeds 24MB limit",
        "image compression service unavailable",
        "unsupported or corrupted image",
        "payload is not a supported image",
        "image dimensions exceed limit",
        "compressed image still exceeds 2MB limit",
    }
    check("G1 错误 detail 全部是固定英文短语 (白名单, 不回显 str(e))",
          all(x.get("code") == 400 for x in bodies)
          and all(x.get("detail") in _FIXED_DETAILS for x in bodies),
          json.dumps([x.get("detail") for x in bodies])[:300])
    check("G2 错误响应体不含绝对路径/Traceback/share_workspace",
          "/share_workspace" not in joined and "/opt/" not in joined
          and "Traceback" not in joined)

    # ══ T-159-H 小图不劣化 (AC-159-2) ═════════════════════════════
    r = call(tiny, "px.png", mime="image/png")
    out_raw = base64.b64decode(r["dataUrl"].split(",", 1)[1])
    check("H1 1x1 PNG 直通: dataUrl 解码与原图逐字节相等 (零重编码)",
          out_raw == tiny)
    check("H2 直通响应 meta 完整 (AC-159-1 直通路径同样满足)",
          r.get("meta", {}).get("originalResolution") == "1x1"
          and r["meta"].get("compressedBytes") == len(tiny))
    # issue #169 第3条 (P3-1 / REQ-169-AC5): originalFormat 并入 meta ——
    #   模型可见元信息的 format 字段取自 meta 子对象 (同源派生防漂移);
    #   顶层同名字段保留 (向后兼容)。1x1 PNG 直通 ⇒ meta.originalFormat == PNG。
    check("H3 meta.originalFormat 在位且与顶层同源 (issue #169 第3条)",
          r.get("meta", {}).get("originalFormat") == "PNG"
          and r.get("originalFormat") == r["meta"].get("originalFormat"),
          json.dumps({"meta": r.get("meta"),
                      "top": r.get("originalFormat")})[:200])

    # ══ 质量下限断言 (X-5: MV-159-J 的牙齿) ═══════════════════════
    m = re.search(r"ATTACH_IMG_JPEG_QUALITY\s*=\s*(\d+)", src)
    q = int(m.group(1)) if m else -1
    check("Q1 主压缩质量常量存在且 >= 70 (质量下限锁)",
          70 <= q <= 100, f"quality={q}")
    check("Q2 JPEG 保存走 optimize=True (压缩参数在位)",
          'im.save(buf, "JPEG", quality=quality, optimize=True)' in src)

    # ══ 路由注册守护 (X-1: 插在 /api/upload 之后) ══════════════════
    i_upload = src.find('if p == "/api/upload":')
    i_attach = src.find('if p == "/api/attachments/image":')
    check("R1 路由分支已注册且紧邻 /api/upload 之后 (X-1)",
          i_upload > 0 and i_attach > i_upload)

    # ══ 汇总 ══════════════════════════════════════════════════════
    print(f"\n== issue159 单测: {len(PASS)} passed, {len(FAIL)} failed ==")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)
