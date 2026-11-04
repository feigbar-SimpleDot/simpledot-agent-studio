"""tests/test_issue211_upload_contract.py — #211 弹窗附件上传 后端/契约守护。

依据: docs/PLAN-T-20261103.md §1.3 s4 段 + docs/AC-T-20261103.md §B。
本卡后端零生产代码改动 (PLAN 结论: 现成 POST /api/upload + detail 透传
已满足需求), 本文件把该契约锁死为守护测试:

  U1  api_upload: base64 入 → 落盘 UPLOAD_DIR → 返回绝对路径, 文件字节
      与输入逐字一致 (AC-211-BE-2)。
  U2  api_upload: 单文件 >20MB ⇒ ValueError 且文案含「20MB」(AC-211-BE-3,
      固定文案口径, 不回显异常原文之外的动态内容)。
  U3  api_upload: 缺数据 / 非法 base64 ⇒ ValueError 固定文案。
  U4  build_dispatch_message: detail 含【附件】清单 ⇒ 绝对路径逐字出现
      在首条派发消息中, 不裁剪不转义 (AC-211-BE-4; PLAN §1.4 协议格式)。
  U5  ScheduleStore.create/get: 多行含附件清单 detail 往返逐字相等,
      无截断无转义 (AC-211-BE-5)。
  U6  api_schedule_create: 多行 detail 完整入库, GET 列表读回一致
      (PLAN §1.3 ③; 既有 strip 语义 = 仅去首尾空白, 正文含换行原样保留)。

跑法: pytest tests/test_issue211_upload_contract.py -v
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mw import console as C  # noqa: E402
from mw import schedule as S  # noqa: E402

# PLAN §1.4 协议级格式 (双语环境安全): 用户任务原文 + 空行 + 【附件】
# + 每行一个绝对路径。该格式在此锁死, 前端 s5 必须逐字对齐。
ATTACH_DETAIL = (
    "帮我分析这份数据\n"
    "\n"
    "【附件】\n"
    "- /share_workspace/uploads/20261103_120000_ab12cd_data.csv\n"
    "- /share_workspace/uploads/20261103_120001_ef34gh_notes.md\n"
)


@pytest.fixture()
def upload_dir(tmp_path, monkeypatch):
    """api_upload 落点重定向到 tmp (不污染共享卷 /share_workspace/uploads)。"""
    d = tmp_path / "uploads"
    monkeypatch.setattr(C, "UPLOAD_DIR", str(d))
    return d


# ── U1: api_upload 落盘 + 绝对路径 (AC-211-BE-2) ─────────────────────
def test_upload_writes_file_and_returns_absolute_path(upload_dir):
    payload = {"name": "a.txt",
               "data": "data:text/plain;base64,"
                       + base64.b64encode(b"hello").decode()}
    d = C.api_upload(payload)
    assert d["ok"] is True
    assert d["name"] == "a.txt"
    assert d["bytes"] == len(b"hello")
    p = Path(d["path"])
    assert p.is_absolute(), f"path 必须是绝对路径, got {d['path']!r}"
    assert p.read_bytes() == b"hello"
    # 落点必须在 UPLOAD_DIR 之下 (worker 容器经共享卷按该绝对路径可读)
    assert p.parent == upload_dir.resolve() or p.parent == upload_dir


def test_upload_bare_base64_without_dataurl_prefix(upload_dir):
    """裸 base64 (无 data: 前缀) 同样接受 —— 与聊天附件调用面兼容。"""
    d = C.api_upload({"name": "b.bin",
                      "data": base64.b64encode(b"\x00\x01\x02").decode()})
    assert d["ok"] is True
    assert Path(d["path"]).read_bytes() == b"\x00\x01\x02"


# ── U2: 20MB 超限拒绝 (AC-211-BE-3) ─────────────────────────────────
def test_upload_rejects_over_20mb(upload_dir):
    raw = b"x" * (C.UPLOAD_MAX_BYTES + 1)   # 20MB + 1 字节
    with pytest.raises(ValueError) as ei:
        C.api_upload({"name": "big.bin",
                      "data": base64.b64encode(raw).decode()})
    assert "20MB" in str(ei.value)
    # 超限文件不得落盘
    assert not list(upload_dir.glob("*"))


def test_upload_allows_exactly_20mb_boundary(upload_dir):
    """边界不误伤: 恰好 20MB 合法 (闸是严格大于)。"""
    raw = b"x" * C.UPLOAD_MAX_BYTES
    d = C.api_upload({"name": "edge.bin",
                      "data": base64.b64encode(raw).decode()})
    assert d["ok"] is True and d["bytes"] == C.UPLOAD_MAX_BYTES


# ── U3: 入参错误固定文案 (不回显动态内容) ───────────────────────────
def test_upload_missing_data_fixed_error(upload_dir):
    with pytest.raises(ValueError) as ei:
        C.api_upload({"name": "a.txt", "data": ""})
    assert str(ei.value) == "缺少附件数据"


def test_upload_bad_base64_fixed_error(upload_dir):
    with pytest.raises(ValueError) as ei:
        C.api_upload({"name": "a.txt", "data": "data:text/plain;base64,a"})
    assert str(ei.value) == "附件 base64 解码失败"


# ── U4: 派发消息对附件清单原文透传 (AC-211-BE-4) ────────────────────
def test_dispatch_message_passes_attachment_paths_verbatim():
    msg = S.build_dispatch_message(
        {"detail": ATTACH_DETAIL, "workflow": "auto", "workspace": ""},
        "sid1")
    # 两条附件绝对路径逐字出现在首条消息中 (worker 据此 read_file 直读)
    assert "/share_workspace/uploads/20261103_120000_ab12cd_data.csv" in msg
    assert "/share_workspace/uploads/20261103_120001_ef34gh_notes.md" in msg
    # 【附件】协议段与任务原文同样不裁剪
    assert "【附件】" in msg
    assert "帮我分析这份数据" in msg
    # detail 整体作为连续子串嵌入 (无中间插入/转义/截断)
    assert ATTACH_DETAIL in msg


def test_dispatch_message_multiline_detail_not_truncated():
    """多行 detail 全量透传: 首行与末行都在, 行数不减。"""
    lines = [f"line{i}" for i in range(200)]
    detail = "\n".join(lines)
    msg = S.build_dispatch_message(
        {"detail": detail, "workflow": "auto", "workspace": ""}, "s")
    for probe in ("line0", "line100", "line199"):
        assert probe in msg
    assert msg.count("\nline") >= 199


# ── U5: store 往返逐字相等 (AC-211-BE-5) ────────────────────────────
def test_store_roundtrip_multiline_attachment_detail(tmp_path):
    store = S.ScheduleStore(str(tmp_path / "schedule.db"))
    created = store.create(ATTACH_DETAIL)
    fetched = store.get(created["id"])
    assert fetched is not None
    assert fetched["detail"] == ATTACH_DETAIL   # 逐字相等, 无截断/转义
    # 标题取首条非空行 (附件段不得混入标题)
    assert fetched["title"] == "帮我分析这份数据"


# ── U6: api_schedule_create 完整入库 (PLAN §1.3 ③) ──────────────────
@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    """独立 schedule.db + 空 ledger, 重置模块级单例 (同 test_schedule_api 口径)。"""
    monkeypatch.setenv("MW_SCHEDULE_DB", str(tmp_path / "sched.db"))
    monkeypatch.setenv("MW_LEDGER_DB", str(tmp_path / "none.db"))
    monkeypatch.setattr(S, "_engine", None)
    monkeypatch.setattr(S, "_engine_path", None)
    eng = S.get_engine()
    eng._gateway_free = lambda: (True, None)
    eng._spawn_fire = lambda task: None
    yield
    monkeypatch.setattr(S, "_engine", None)
    monkeypatch.setattr(S, "_engine_path", None)


def test_api_create_roundtrip_attachment_detail(isolated_env):
    code, body = S.api_schedule_create({"detail": ATTACH_DETAIL,
                                        "workflow": "auto", "workspace": ""})
    assert code == 201, body
    task = body["task"]
    # 既有语义: api_schedule_create 对 detail 做 strip() (仅去首尾空白,
    # 正文换行与附件清单原样保留) —— 前端 s5 拼装的 detail 无尾随换行。
    assert task["detail"] == ATTACH_DETAIL.strip()
    code, listing = S.api_schedule_tasks()
    assert code == 200
    assert listing["tasks"][0]["detail"] == ATTACH_DETAIL.strip()
