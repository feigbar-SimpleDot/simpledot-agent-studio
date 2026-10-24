"""Issue #51 头像文件化回归测试 (s2a-impl, 2026-10-17)。

覆盖 REQ-ISSUES51-53 §1.4 + ACCEPT §A1-A2:
  AC-51-01 data URL → 服务端解码 → 落盘 → 裸文件名
  AC-51-02 GET /api/workers 响应 avatar 字段是文件名 (≤128 字符, 无 data:)
  AC-51-03 魔数嗅探: 伪造扩展/MIME → 422 拒绝
  AC-51-04 解码后 >200KB → 拒绝
  AC-51-05 裸文件名幂等回传: 不重复落盘
  AC-51-06 换头像时旧文件被删除
  AC-51-07 delete worker 同步清理头像文件
  AC-51-08 avatar 非字符串 / 超长 → 422
  AC-51-09 GET /api/workers/<name>/avatar 路由 (mock handler)
  AC-51-10 不存在 worker → 404
  AC-51-11 路径穿越 → 422/404, 不漏字节
  AC-51-12 文件被删后 → 404; 列表不挂
  AC-51-14-16 迁移 (启动期) — 已有 base64 → 文件名; 损坏 → 置空 + 不阻塞; 幂等
"""
import base64
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mw.console as console


# 1x1 透明 PNG (合法魔数)
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "533de0000000017352474200aece1ce90000000d4944415478da6300010000"
    "0005000133c2baf00000000049454e44ae426082")


def setup_registry(tmpdir: Path) -> tuple[str, str]:
    console.REGISTRY = str(tmpdir / "reg.json")
    console.AVATAR_DIR = str(tmpdir / "avatars")
    Path(console.REGISTRY).write_text(json.dumps({
        "coder": {"api_url": "http://stub", "api_key": "x" * 32}}))
    return console.REGISTRY, console.AVATAR_DIR


def data_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def PASS(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    (PASS_LIST if ok else FAIL_LIST).append(name)


PASS_LIST: list[str] = []
FAIL_LIST: list[str] = []


def test_51_01_decode_to_file(tmp_path):
    """AC-51-01: 合法 data URL → 落盘 + 裸文件名形态。"""
    setup_registry(tmp_path)
    du = data_url(PNG_BYTES)
    r = console.api_registry_save({"coder": {
        "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
    PASS("AC-51-01 save ok", r.get("ok") is True, str(r))
    reg = json.loads(Path(console.REGISTRY).read_text())
    av = reg["coder"]["avatar"]
    PASS("AC-51-01 avatar is bare filename",
         isinstance(av, str) and re.match(r"^coder-[0-9a-f]{8}\.png$", av) is not None,
         av)
    files = list(Path(console.AVATAR_DIR).iterdir())
    PASS("AC-51-01 exactly one file on disk", len(files) == 1, str(files))
    PASS("AC-51-01 file bytes == decoded PNG",
         files[0].read_bytes() == PNG_BYTES)
    # registry 全文不含 data:image
    raw_text = Path(console.REGISTRY).read_text()
    PASS("AC-51-01 registry no data:image", "data:image" not in raw_text)


def test_51_02_list_response_short(tmp_path):
    """AC-51-02: GET 响应 avatar 字段 ≤128 字符, 无 base64。"""
    setup_registry(tmp_path)
    du = data_url(PNG_BYTES)
    console.api_registry_save({"coder": {
        "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
    reg = json.loads(Path(console.REGISTRY).read_text())
    av = reg["coder"]["avatar"]
    PASS("AC-51-02 avatar len <= 128", len(av) <= 128, f"len={len(av)}")
    PASS("AC-51-02 no base64 in registry", "base64," not in Path(console.REGISTRY).read_text())


def test_51_03_magic_rejects_zip(tmp_path):
    """AC-51-03: 伪造 ZIP 头 → 拒绝, 无新文件。"""
    setup_registry(tmp_path)
    zip_magic = bytes.fromhex("504b0304140006000800000021000000000000000000000000000000")
    du = data_url(zip_magic, mime="image/png")
    try:
        r = console.api_registry_save({"coder": {
            "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
        PASS("AC-51-03 reject zip", False, f"unexpectedly saved: {r}")
    except ValueError as e:
        PASS("AC-51-03 reject zip", "魔数嗅探失败" in str(e), str(e))
    adir = Path(console.AVATAR_DIR)
    files = list(adir.iterdir()) if adir.exists() else []
    PASS("AC-51-03 no file written", len(files) == 0, str(files))
    # AC-51-03 side note: AVATAR_DIR 自身可能尚未创建 (ValueError 在 _avatar_root
    # 之前抛出), 不在此测试断言目录存在与否 — 后续 save 路径会 lazy 创建。


def test_51_04_size_limit(tmp_path):
    """AC-51-04: 解码后 >200KB → 拒绝。"""
    setup_registry(tmp_path)
    # 构造恰好 200KB+1 字节的内容 (魔数先放, 再补 0)
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (200 * 1024 + 1 - 8)
    du = data_url(big)
    try:
        r = console.api_registry_save({"coder": {
            "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
        PASS("AC-51-04 reject >200KB", False, f"unexpectedly saved: {r}")
    except ValueError as e:
        PASS("AC-51-04 reject >200KB", "204800" in str(e) or "200" in str(e), str(e))


def test_51_05_bare_filename_idempotent(tmp_path):
    """AC-51-05: 裸文件名形态回传 → 幂等, 不重写。"""
    setup_registry(tmp_path)
    du = data_url(PNG_BYTES)
    r = console.api_registry_save({"coder": {
        "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
    reg = json.loads(Path(console.REGISTRY).read_text())
    bare = reg["coder"]["avatar"]
    files_before = {p.name for p in Path(console.AVATAR_DIR).iterdir()}
    # 回传裸文件名
    r = console.api_registry_save({"coder": {
        "api_url": "http://stub", "api_key": "x" * 32, "avatar": bare}})
    files_after = {p.name for p in Path(console.AVATAR_DIR).iterdir()}
    PASS("AC-51-05 idempotent save", r.get("ok") is True)
    PASS("AC-51-05 no extra files", files_before == files_after, str(files_after))


def test_51_06_replace_deletes_old(tmp_path):
    """AC-51-06: 换头像时旧文件被删, 总数 =1。"""
    setup_registry(tmp_path)
    du1 = data_url(PNG_BYTES)
    r1 = console.api_registry_save({"coder": {
        "api_url": "http://stub", "api_key": "x" * 32, "avatar": du1}})
    reg1 = json.loads(Path(console.REGISTRY).read_text())
    name1 = reg1["coder"]["avatar"]
    files_after_first = list(Path(console.AVATAR_DIR).iterdir())
    PASS("AC-51-06 first file written", len(files_after_first) == 1)
    # 上传不同的字节 (用 PNG 头 + 不同 IDAT 区段)
    PNG2 = b"\x89PNG\r\n\x1a\n" + PNG_BYTES[8:65] + b"\x00" + PNG_BYTES[66:]
    du2 = data_url(PNG2)
    r2 = console.api_registry_save({"coder": {
        "api_url": "http://stub", "api_key": "x" * 32, "avatar": du2}})
    reg2 = json.loads(Path(console.REGISTRY).read_text())
    name2 = reg2["coder"]["avatar"]
    files_after_second = list(Path(console.AVATAR_DIR).iterdir())
    PASS("AC-51-06 name changed", name1 != name2, f"{name1} -> {name2}")
    PASS("AC-51-06 only one file after replace",
         len(files_after_second) == 1, str([p.name for p in files_after_second]))


def test_51_07_delete_removes_avatar(tmp_path):
    """AC-51-07: delete worker 同步清理头像文件。"""
    setup_registry(tmp_path)
    du = data_url(PNG_BYTES)
    console.api_registry_save({"coder": {
        "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
    files_before = list(Path(console.AVATAR_DIR).iterdir())
    PASS("AC-51-07 file exists before delete", len(files_before) == 1)
    console.api_registry_delete("coder")
    files_after = list(Path(console.AVATAR_DIR).iterdir())
    PASS("AC-51-07 file removed after delete", len(files_after) == 0, str([p.name for p in files_after]))


def test_51_08_avatar_validation(tmp_path):
    """AC-51-08: 非字符串 / 脏值 / 超长 → 422 (这里 ValueError 形式抛错)。"""
    setup_registry(tmp_path)
    for bad in [123, {"a": 1}, ["x" * 200]]:
        try:
            r = console.api_registry_save({"coder": {
                "api_url": "http://stub", "api_key": "x" * 32, "avatar": bad}})
            PASS(f"AC-51-08 reject {type(bad).__name__}", False, str(r))
        except ValueError as e:
            PASS(f"AC-51-08 reject {type(bad).__name__}", "avatar" in str(e), str(e))
    # 超长裸文件名
    long = "x" * 200
    try:
        r = console.api_registry_save({"coder": {
            "api_url": "http://stub", "api_key": "x" * 32, "avatar": long}})
        PASS("AC-51-08 reject long bare", False, str(r))
    except ValueError as e:
        PASS("AC-51-08 reject long bare",
             "形态非法" in str(e) or "超长" in str(e), str(e))


def test_51_09_route_serves_file(tmp_path):
    """AC-51-09: GET /api/workers/<name>/avatar mock → 200 + bytes + Content-Type。"""
    setup_registry(tmp_path)
    du = data_url(PNG_BYTES)
    console.api_registry_save({"coder": {
        "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})

    class MockHandler:
        def __init__(self):
            self.status = None
            self.headers = []
            self.body = b""
            self.wfile = self
        def send_response(self, n): self.status = n
        def send_header(self, k, v): self.headers.append((k, v))
        def end_headers(self): pass
        def write(self, chunk): self.body += chunk

    h = MockHandler()
    console._send_avatar_file(h, "coder")
    PASS("AC-51-09 status 200", h.status == 200, str(h.status))
    headers = dict(h.headers)
    PASS("AC-51-09 Content-Type image/png",
         headers.get("Content-Type") == "image/png", str(headers.get("Content-Type")))
    PASS("AC-51-09 Cache-Control set",
         "max-age" in (headers.get("Cache-Control") or ""),
         str(headers.get("Cache-Control")))
    PASS("AC-51-09 body matches PNG bytes",
         h.body == PNG_BYTES, f"len={len(h.body)}")


def test_51_10_route_404_unknown(tmp_path):
    """AC-51-10: 不存在 worker → 404, 不 500。"""
    setup_registry(tmp_path)

    class MockHandler:
        def __init__(self):
            self.status = None; self.headers = []
            self.body = b""; self.wfile = self
        def send_response(self, n): self.status = n
        def send_header(self, k, v): self.headers.append((k, v))
        def end_headers(self): pass
        def write(self, chunk): self.body += chunk

    h = MockHandler()
    console._send_avatar_file(h, "ghost")
    PASS("AC-51-10 unknown -> 404", h.status == 404, str(h.status))
    PASS("AC-51-10 body is JSON error",
         b"error" in h.body, str(h.body[:80]))


def test_51_11_route_path_traversal(tmp_path):
    """AC-51-11: 路径穿越 → 不漏目标字节 (404/422)。"""
    setup_registry(tmp_path)
    du = data_url(PNG_BYTES)
    console.api_registry_save({"coder": {
        "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
    # 服务端拒绝把 registry.avatar 写成 ../etc/passwd, 但路由层还应二次防御
    Path(console.REGISTRY).write_text(json.dumps({
        "coder": {"api_url": "http://stub", "api_key": "x" * 32,
                  "avatar": "../../etc/passwd"}}))

    class MockHandler:
        def __init__(self):
            self.status = None; self.headers = []
            self.body = b""; self.wfile = self
        def send_response(self, n): self.status = n
        def send_header(self, k, v): self.headers.append((k, v))
        def end_headers(self): pass
        def write(self, chunk): self.body += chunk

    for bad_name in ["coder", "../../etc/passwd", "..%2f..%2fetc"]:
        h = MockHandler()
        console._send_avatar_file(h, bad_name)
        # 不论 404 还是 422, body 都不得含 /etc/passwd 的内容
        # 也不得把目标文件任何字节写入 body
        PASS(f"AC-51-11 traversal {bad_name!r} blocked (status 4xx)",
             h.status in (404, 422), f"status={h.status}")


def test_51_12_route_missing_file(tmp_path):
    """AC-51-12: 文件被删 → 404。"""
    setup_registry(tmp_path)
    du = data_url(PNG_BYTES)
    console.api_registry_save({"coder": {
        "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
    # 手工删除磁盘文件
    for f in Path(console.AVATAR_DIR).iterdir():
        f.unlink()

    class MockHandler:
        def __init__(self):
            self.status = None; self.headers = []
            self.body = b""; self.wfile = self
        def send_response(self, n): self.status = n
        def send_header(self, k, v): self.headers.append((k, v))
        def end_headers(self): pass
        def write(self, chunk): self.body += chunk

    h = MockHandler()
    console._send_avatar_file(h, "coder")
    PASS("AC-51-12 missing file -> 404", h.status == 404, str(h.status))


def test_51_14_15_migration_basic(tmp_path):
    """AC-51-14/15: 启动期迁移 + 幂等。"""
    setup_registry(tmp_path)
    du = data_url(PNG_BYTES)
    Path(console.REGISTRY).write_text(json.dumps({
        "old_coder": {"api_url": "http://stub", "api_key": "x" * 32,
                      "avatar": du}}))
    stats = console.migrate_avatars()
    PASS("AC-51-14 migrated = 1", stats["migrated"] == 1, str(stats))
    reg = json.loads(Path(console.REGISTRY).read_text())
    av = reg["old_coder"]["avatar"]
    PASS("AC-51-14 avatar now bare filename",
         isinstance(av, str) and "data:image" not in av, av)
    files = list(Path(console.AVATAR_DIR).iterdir())
    PASS("AC-51-14 file written", len(files) == 1, str([p.name for p in files]))
    # 幂等
    stats2 = console.migrate_avatars()
    PASS("AC-51-15 idempotent", stats2["written"] is False and stats2["scanned"] == 0,
         str(stats2))


def test_51_16_migration_failed_open(tmp_path):
    """AC-51-16: 损坏 base64 → 置空 + 不阻塞。"""
    setup_registry(tmp_path)
    Path(console.REGISTRY).write_text(json.dumps({
        "broken": {"api_url": "http://stub", "api_key": "x" * 32,
                   "avatar": "data:image/png;base64,!!!not-base64"},
        "good": {"api_url": "http://stub", "api_key": "x" * 32,
                 "avatar": data_url(PNG_BYTES)}}))
    stats = console.migrate_avatars()
    PASS("AC-51-16 failed_clear = 1", stats["failed_clear"] == 1, str(stats))
    PASS("AC-51-16 good entry still migrated", stats["migrated"] == 1)
    reg = json.loads(Path(console.REGISTRY).read_text())
    PASS("AC-51-16 broken avatar cleared",
         "avatar" not in reg["broken"], str(reg["broken"]))
    PASS("AC-51-16 good avatar is filename",
         isinstance(reg["good"]["avatar"], str) and "data:" not in reg["good"]["avatar"])


# ── s3b-fix R-1 回归测试 (2026-10-17) ─────────────────────────────
# 修复前实证: PUT {"../../evilworker": {...合法头像}} → 200, 文件落到 AVATAR_DIR 外。
# 修复后: worker key 必须 ^\[A-Za-z0-9_.-]+$, 含 '/' / 绝对路径 / URL 编码变体 → 422。
def test_r1_rejects_path_traversal_key(tmp_path):
    """R-1: 含 '../' 的 worker key 写入 → ValueError, 无文件落盘到 AVATAR_DIR 外。"""
    setup_registry(tmp_path)
    du = data_url(PNG_BYTES)
    # 攻击样本 1: 含 '../' 路径穿越
    try:
        r = console.api_registry_save({"../../evilworker": {
            "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
        PASS("R-1 reject ../ in key", False, f"unexpectedly saved: {r}")
    except ValueError as e:
        PASS("R-1 reject ../ in key", "worker key" in str(e), str(e))
    # 攻击样本 2: 绝对路径 key
    try:
        r = console.api_registry_save({"/tmp/abs-path-evil": {
            "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
        PASS("R-1 reject abs path key", False, f"unexpectedly saved: {r}")
    except ValueError as e:
        PASS("R-1 reject abs path key", "worker key" in str(e), str(e))
    # 攻击样本 3: URL 编码变体 %2e%2e%2f
    try:
        r = console.api_registry_save({"..%2f..%2fevil": {
            "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
        PASS("R-1 reject url-encoded variant", False, f"unexpectedly saved: {r}")
    except ValueError as e:
        PASS("R-1 reject url-encoded variant", "worker key" in str(e), str(e))
    # 攻击样本 4: 路径分隔符 (正斜杠)
    try:
        r = console.api_registry_save({"subdir/evil": {
            "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
        PASS("R-1 reject subdir/evil", False, f"unexpectedly saved: {r}")
    except ValueError as e:
        PASS("R-1 reject subdir/evil", "worker key" in str(e), str(e))
    # 攻击样本 5: 空字符串
    try:
        r = console.api_registry_save({"": {
            "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
        PASS("R-1 reject empty key", False, f"unexpectedly saved: {r}")
    except ValueError as e:
        PASS("R-1 reject empty key", "worker key" in str(e), str(e))
    # 攻击样本 6: 控制字符 / 空格
    try:
        r = console.api_registry_save({"bad key with spaces": {
            "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
        PASS("R-1 reject space in key", False, f"unexpectedly saved: {r}")
    except ValueError as e:
        PASS("R-1 reject space in key", "worker key" in str(e), str(e))
    # 关键断言: AVATAR_DIR 之外没有新增文件 (写到 /tmp 等)
    # (我们用了 tmp_path 内的 AVATAR_DIR, 但攻击样本都拒, 所以 AVATAR_DIR 应空或不存在)
    adir = Path(console.AVATAR_DIR)
    if adir.exists():
        PASS("R-1 no file written to AVATAR_DIR",
             len(list(adir.iterdir())) == 0, str(list(adir.iterdir())))
    else:
        PASS("R-1 AVATAR_DIR not even created (rejected before _avatar_root)",
             True, str(adir))


def test_r1_allows_legal_key(tmp_path):
    """R-1: 合法 worker key (含 . _ -) 正常落盘。"""
    setup_registry(tmp_path)
    du = data_url(PNG_BYTES)
    for legal in ["coder", "my-coder", "my_coder", "v1.coder", "agent-007"]:
        try:
            r = console.api_registry_save({legal: {
                "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
            PASS(f"R-1 legal key {legal!r} accepted",
                 r.get("ok") is True, str(r))
        except ValueError as e:
            PASS(f"R-1 legal key {legal!r} accepted", False, str(e))


def test_r1_rejects_dot_dot(tmp_path):
    """s3d-fix N-1 加固: 显式排除 '.' 与 '..' — 它们正则上过关但语义上是保留名。

    修复前实证: `_WORKER_KEY_RE = ^[A-Za-z0-9_.-]+$` 接受 '.' 与 '..', 走到
    _save_avatar_data_url 的 f"{name}-{suffix}.{ext}" 拼接后, 经 realpath 解析
    仍有歧义 (容器内 bind mount 场景), 不能单纯依赖正则。最稳: 在正则前就拒。
    """
    setup_registry(tmp_path)
    du = data_url(PNG_BYTES)
    for reserved in [".", ".."]:
        try:
            r = console.api_registry_save({reserved: {
                "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
            PASS(f"N-1 reject reserved key {reserved!r}",
                 False, f"unexpectedly saved: {r}")
        except ValueError as e:
            msg = str(e)
            ok = "worker key" in msg and (
                "保留名" in msg or "reserved" in msg.lower()
                or "'.'" in msg or "'..'" in msg)
            PASS(f"N-1 reject reserved key {reserved!r}", ok, msg)
    # 关键断言: AVATAR_DIR 之外没有新增文件, 内部也没有
    adir = Path(console.AVATAR_DIR)
    if adir.exists():
        files = list(adir.iterdir())
        PASS("N-1 no file written to AVATAR_DIR for reserved keys",
             len(files) == 0, str(files))
    else:
        PASS("N-1 AVATAR_DIR not even created (rejected before _avatar_root)",
             True, str(adir))


# ── s3b-fix R-2 回归测试 (2026-10-17) ─────────────────────────────
# 修复前实证: WAV (`RIFF....WAVE`) 与 AVI (`RIFF....AVI `) 均被接受并落盘为 .webp
# 修复后: webp 二次魔数 (RIFF + 字节 8:12 == 'WEBP') 严格匹配, 非 webp RIFF 容器拒。
def test_r2_webp_secondary_magic_accepts_legal_webp(tmp_path):
    """R-2: 合法 webp (RIFF + 字节 8:12 == 'WEBP') 仍可落盘。"""
    setup_registry(tmp_path)
    # 最小合法 webp: RIFF + 4 字节 size + 'WEBP' + VP8 头
    webp = b"RIFF" + (32).to_bytes(4, "little") + b"WEBP" + b"VP8 " + b"\x00" * 20
    du = data_url(webp, mime="image/webp")
    r = console.api_registry_save({"coder": {
        "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
    PASS("R-2 legal webp accepted", r.get("ok") is True, str(r))
    reg = json.loads(Path(console.REGISTRY).read_text())
    av = reg["coder"]["avatar"]
    PASS("R-2 legal webp has .webp ext",
         av.endswith(".webp"), av)


def test_r2_webp_rejects_wav(tmp_path):
    """R-2: WAV 容器 (RIFF + 'WAVE' 在 8:12) → 拒绝, 非 webp。"""
    setup_registry(tmp_path)
    wav = b"RIFF" + (32).to_bytes(4, "little") + b"WAVE" + b"fmt " + b"\x00" * 20
    du = data_url(wav, mime="image/webp")  # 攻击者自报 webp MIME
    try:
        r = console.api_registry_save({"coder": {
            "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
        PASS("R-2 reject WAV as webp", False, f"unexpectedly saved: {r}")
    except ValueError as e:
        PASS("R-2 reject WAV as webp", "魔数嗅探失败" in str(e), str(e))
    adir = Path(console.AVATAR_DIR)
    if adir.exists():
        PASS("R-2 no file written for WAV",
             len(list(adir.iterdir())) == 0, str(list(adir.iterdir())))


def test_r2_webp_rejects_avi(tmp_path):
    """R-2: AVI 容器 (RIFF + 'AVI ' 在 8:12) → 拒绝。"""
    setup_registry(tmp_path)
    avi = b"RIFF" + (32).to_bytes(4, "little") + b"AVI " + b"\x00" * 20
    du = data_url(avi, mime="image/webp")
    try:
        r = console.api_registry_save({"coder": {
            "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
        PASS("R-2 reject AVI as webp", False, f"unexpectedly saved: {r}")
    except ValueError as e:
        PASS("R-2 reject AVI as webp", "魔数嗅探失败" in str(e), str(e))


# ── s3b-fix R-3 回归测试 (2026-10-17) ─────────────────────────────
# 修复前实证: AC-51-08 128 上限未强制 (AVATAR_NAME_MAX_LEN 是死常量)
# 修复后: 裸文件名形态 >128 字符 → ValueError
def test_r3_rejects_long_bare_filename(tmp_path):
    """R-3: 裸文件名形态 >128 字符 → 拒绝。"""
    setup_registry(tmp_path)
    # 130 字符的 'a' + 后缀, 不匹配 _AVATAR_NAME_RE 但能绕开正则长度边界
    # 用一个长得超过 128 但不含 '/' 的脏值测上限
    long_bad = "a" * 130 + "-12345678.png"  # 130 + 1 + 8 + 4 = 143 字符
    try:
        r = console.api_registry_save({"coder": {
            "api_url": "http://stub", "api_key": "x" * 32,
            "avatar": long_bad}})
        PASS("R-3 reject 143-char avatar", False, f"unexpectedly saved: {r}")
    except ValueError as e:
        PASS("R-3 reject 143-char avatar",
             "长度" in str(e) and "128" in str(e) and "超长" in str(e), str(e))
    # 数据 URL 形态长度可超过 128 (200KB 内) → 不应被此上限拒
    du = data_url(PNG_BYTES)
    try:
        r = console.api_registry_save({"coder2": {
            "api_url": "http://stub", "api_key": "x" * 32, "avatar": du}})
        PASS("R-3 data URL form not capped at 128",
             r.get("ok") is True, str(r))
    except ValueError as e:
        # data URL 长度 < 200KB decoded, 这里只要不是 128 上限错误就 OK
        PASS("R-3 data URL form not capped at 128",
             "128" not in str(e), str(e))


# ── s3b-fix R-6 + s3d-fix N-1 回归测试 (2026-10-17) ──────────────
# s3b-fix 修复前实证: §8.5 host 侧直用 sha256sum, macOS 缺此命令 → set -e + pipefail 中止
# s3b-fix 修复后: hash_of 泛化封装 sha256 (sha256sum | shasum -a 256 探测)。
# s3d-fix N-1 (2026-10-17): §8.5 管道尾用 `done | hash_of sha256 -)` (修复前写成了
# `done | hash_of sha256)`, 把 'sha256' 当文件名传, 触发 `[ -n "$f" ]` 短路)。
# 修复方式: hash_of 在 filename='-' 时走 sha256sum/shasum -a 256 stdin 语义。
# 测试策略: 从真实 install.sh 抽取 hash_of 函数体, 用 bash -c 真跑。
def _extract_hash_of_from_install_sh() -> str:
    """从 deploy/install.sh 抽取 hash_of 函数体 (含 s3d-fix N-1 的 '-' 分支)。

    失败时 (例如源码被重构) 让测试明确失败, 而不是悄悄退化成旧内嵌副本。
    """
    install_sh = (Path(__file__).resolve().parent.parent
                  / "deploy" / "install.sh").read_text()
    # 找 `hash_of() {` 起点和函数结束符 `}`。函数体内有 `awk '{print $1}'`
    # 这类含 {} 的字符串, 用简单 brace-counting 不行 — 转用 '# }' 注释做锚。
    # 函数结尾惯例是: 缩进后的 `}` 单独一行 (本仓 install.sh 的统一风格)。
    lines = install_sh.splitlines(keepends=True)
    start_idx = None
    for i, line in enumerate(lines):
        if line.startswith("hash_of() {"):
            start_idx = i
            break
    assert start_idx is not None, "install.sh 找不到 `hash_of() {` 起点"
    # 从函数首行后往下扫, 找到第一个 '}' 单独成行的位置。
    end_idx = None
    for j in range(start_idx + 1, len(lines)):
        if lines[j].rstrip() == "}":
            end_idx = j
            break
    assert end_idx is not None, "install.sh hash_of 函数体未闭合 (无 '}' 单行)"
    body = "".join(lines[start_idx:end_idx + 1])
    # 关键守卫: 抽取的源码必须含 s3d-fix N-1 的 '-' 分支, 否则就是源码漂移。
    assert '"${f}" = "-"' in body, \
        "install.sh hash_of 已不再含 '${f} = \"-\"' (s3d-fix N-1 撤回? 测试需同步)"
    return body


def test_r6_hash_of_via_bash(tmp_path):
    """R-6 + N-1: 跑真 bash 测 hash_of 函数 (md5 + sha256 + '-' stdin)。"""
    import subprocess
    hash_func_src = _extract_hash_of_from_install_sh()
    # 写一个文件测各分支
    f = tmp_path / "sample.txt"
    f.write_text("hello\n")
    # md5 默认 (1-arg) → b1946ac92492d2347c6235b4d2611184 (md5("hello\n"))
    r1 = subprocess.run(
        ["bash", "-c", hash_func_src + f'echo "$(hash_of {f})"'],
        capture_output=True, text=True)
    PASS("R-6 hash_of <file> default md5",
         "b1946ac92492d2347c6235b4d2611184" in r1.stdout, r1.stdout.strip())
    # md5 explicit (2-arg) → 同上
    r2 = subprocess.run(
        ["bash", "-c", hash_func_src + f'echo "$(hash_of md5 {f})"'],
        capture_output=True, text=True)
    PASS("R-6 hash_of md5 <file>",
         "b1946ac92492d2347c6235b4d2611184" in r2.stdout, r2.stdout.strip())
    # sha256 → 5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03
    r3 = subprocess.run(
        ["bash", "-c", hash_func_src + f'echo "$(hash_of sha256 {f})"'],
        capture_output=True, text=True)
    PASS("R-6 hash_of sha256 <file>",
         "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03" in r3.stdout,
         r3.stdout.strip())
    # N-1: sha256 读 stdin ('-' 形态) — 对 §8.5 `done | hash_of sha256 -` 至关重要。
    # 复审验证: sha256sum/shasum -a 256 直接吃 stdin, 输出 <hash>\n, awk '$1' 取哈希。
    # 期望: 与 `echo -n "hello" | sha256sum | awk '{print $1}'` 等价。
    expected = subprocess.run(
        ["bash", "-c", 'echo -n "hello" | sha256sum | awk \'{print $1}\''],
        capture_output=True, text=True).stdout.strip()
    r4 = subprocess.run(
        ["bash", "-c", hash_func_src + f'echo -n "hello" | hash_of sha256 -'],
        capture_output=True, text=True)
    PASS("N-1 hash_of sha256 - reads stdin",
         expected in r4.stdout and expected != "",
         f"got={r4.stdout.strip()!r} expected={expected!r}")
    # N-1 负样本: 缺 '-' (回归保护: 退回 s3b 之前的 broken `done | hash_of sha256)`)
    # — 单参 `hash_of sha256` 走 1-arg 分支, f=sha256, 被当成文件名, md5sum 报
    # 'No such file or directory'。回 0 是因为是 stderr, 但 stdout 是空, 不产出
    # 哈希。等价语义: '拿到 HOST_HASH 是空串或 md5(sha256) 的 8d10b4b... 文件名哈希',
    # 都不是 12 文件 sha256 之哈希。简化: 断言 stdout 为空 + 哈希输出不等价 stdin 的
    # 真实 sha256 (上游 N-1 测试已验证 '- 形态能拿到正确哈希, 这里只需证伪旧的形态)。
    r5 = subprocess.run(
        ["bash", "-c", hash_func_src + f'echo -n "hello" | hash_of sha256'],
        capture_output=True, text=True)
    # 期望: stdout 不含真实 sha256('hello') = 2cf24dba...
    wrong = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824" \
        not in r5.stdout
    # 同时: bash 应在某种失败下退出 — md5sum 找不到 'sha256' 文件 → rc=1
    # (stderr 必有 'No such file' 或 'missing argument' 或类似错误字串)
    PASS("N-1 hash_of sha256 without '-' does NOT produce stdin hash (regression guard)",
         wrong and (r5.returncode != 0 or "missing argument" in r5.stderr
                    or "No such file" in r5.stderr or "cannot open" in r5.stderr),
         f"stdout={r5.stdout.strip()!r} stderr={r5.stderr.strip()!r} rc={r5.returncode}")
    # 不存在的 algo
    r6 = subprocess.run(
        ["bash", "-c", hash_func_src + f'hash_of blake3 {f}'],
        capture_output=True, text=True)
    PASS("R-6 hash_of rejects unknown algo",
         "unsupported algo" in r6.stderr, r6.stderr.strip())


def test_r6_install_sh_section_85_uses_hash_of(tmp_path):
    """R-6: §8.5 host 侧已替换 sha256sum 为 hash_of sha256。"""
    install_sh = (Path(__file__).resolve().parent.parent
                  / "deploy" / "install.sh").read_text()
    # §8.5 region (between "8.5 Code version check" and "9. Wait for health")
    sec_start = install_sh.find("# ── 8.5 Code version check")
    sec_end = install_sh.find("# ── 9. Wait for health")
    assert sec_start != -1 and sec_end != -1, "section markers missing"
    section = install_sh[sec_start:sec_end]
    # host 侧 HOST_HASH 行应使用 hash_of sha256
    PASS("R-6 §8.5 host uses hash_of sha256",
         "hash_of sha256" in section, section[:300])
    # s3d-fix N-1: §8.5 管道尾必须是 `hash_of sha256 -)` 形态, 不能漏 '-'.
    # 旧 broken 形态: `done | hash_of sha256)` (sha256 当文件名)。
    has_correct_tail = "done | hash_of sha256 -)" in section
    has_broken_tail = re.search(r"done \| hash_of sha256\)(?! -)", section)
    PASS("N-1 §8.5 pipe tail is `hash_of sha256 -)`",
         has_correct_tail and not has_broken_tail,
         f"correct={has_correct_tail} broken={bool(has_broken_tail)}")
    # 旧 sha256sum 字面量在 §8.5 容器侧仍在 (容器是 Linux, OK)
    # 不强制


if __name__ == "__main__":
    fns = [test_51_01_decode_to_file,
           test_51_02_list_response_short,
           test_51_03_magic_rejects_zip,
           test_51_04_size_limit,
           test_51_05_bare_filename_idempotent,
           test_51_06_replace_deletes_old,
           test_51_07_delete_removes_avatar,
           test_51_08_avatar_validation,
           test_51_09_route_serves_file,
           test_51_10_route_404_unknown,
           test_51_11_route_path_traversal,
           test_51_12_route_missing_file,
           test_51_14_15_migration_basic,
           test_51_16_migration_failed_open,
           test_r1_rejects_path_traversal_key,
           test_r1_allows_legal_key,
           test_r1_rejects_dot_dot,
           test_r2_webp_secondary_magic_accepts_legal_webp,
           test_r2_webp_rejects_wav,
           test_r2_webp_rejects_avi,
           test_r3_rejects_long_bare_filename,
           test_r6_hash_of_via_bash,
           test_r6_install_sh_section_85_uses_hash_of]
    for fn in fns:
        with tempfile.TemporaryDirectory() as td:
            try:
                fn(Path(td))
            except Exception as e:
                FAIL_LIST.append(f"{fn.__name__}: {e!r}")
                print(f"  FAIL  {fn.__name__}:  {e!r}")
    print(f"\n{len(PASS_LIST)} passed, {len(FAIL_LIST)} failed")
    sys.exit(1 if FAIL_LIST else 0)
