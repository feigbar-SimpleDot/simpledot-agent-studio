"""s7b P1 守护 (#235 回归): active 段 current_card_result 服务端截断 + 载荷上界。

背景 (DOC-AUDIT §4.1 探针证据): s3 实现把当前卡 result **全文**放进
`GET /api/tasks?fields=active` 的 current_card_result —— 该端点是 dashboard/
看板 5s 轮询面, docstring 与 design.md §4.7 自述 ~1KB 轻量契约。审计实测
result=200,000 字符 ⇒ 响应体 200,643 B (基线 11001bc 为 583 B, ≈345×),
且与卡 result 长度线性相关。

修法守护 (design.md §4.7 字段内容纪律):
  G1 超长 result 服务端截断: 500 字符 + 省略号 (ACTIVE_RESULT_MAX_CHARS)。
  G2 载荷守护: 构造超长 result 后, active 段响应体尺寸有硬上界 (~1KB 量级),
     不再随 result 长度线性膨胀。
  G3 两端点逐字一致 (#178 纪律): 截断后键集一致且 current_card_result 同值。
  G4 缺省语义不变: result 缺省 None 保持 None; 短 result 原样透传。
  G5 边界: len==500 不截不加省略号; len==501 截为 500+省略号。

不依赖外部资源; sqlite3 临时建 mock kanban + ledger
(与 tests/test_issue178_delivery_badge.py 同口径)。
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mw.console as console

MAX = console.ACTIVE_RESULT_MAX_CHARS  # 500


def fresh_dual_db():
    """建一对临时 db: kanban + ledger, 都注入 console 顶层模块变量。"""
    kanban_f = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    kanban_f.close()
    ledger_f = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    ledger_f.close()
    kc = sqlite3.connect(kanban_f.name)
    kc.executescript("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            assignee TEXT,
            status TEXT,
            block_kind TEXT,
            result TEXT,
            created_at REAL,
            started_at REAL,
            completed_at REAL
        );
    """)
    kc.commit()
    kc.close()
    console.KANBAN_DB = kanban_f.name
    console.LEDGER_DB = ledger_f.name
    return kanban_f.name, ledger_f.name


def cleanup(kanban_path, ledger_path):
    for p in [kanban_path, ledger_path]:
        try:
            os.unlink(p)
        except OSError:
            pass


def seed_run_with_result(kanban_path, result):
    """建 ACTIVE run + 当前卡 (kanban 行带指定 result), 返回 task_id。"""
    from mw.ledger import Ledger
    led = Ledger(console.LEDGER_DB)
    try:
        led.create_team_work_run("r_p1", "sess_x", "需求")
        led.put_contract("t_p1", {"required_files": []})
        led.register_team_work_card("r_p1", "t_p1")
        led.set_team_work_current_card("r_p1", "t_p1")
    finally:
        try:
            led.close()
        except Exception:
            pass
    kc = sqlite3.connect(kanban_path)
    kc.execute(
        "INSERT INTO tasks(id, title, assignee, status, result, created_at,"
        " started_at) VALUES(?,?,?,?,?,?,?)",
        ("t_p1", "卡P1", "coder", "running", result, 1.0, 2.0))
    kc.commit()
    kc.close()
    return "t_p1"


def active_body_bytes(resp):
    """active 段响应体字节数 (与 HTTP 面同口径的 JSON 序列化)。"""
    return len(json.dumps(resp, ensure_ascii=False).encode("utf-8"))


def test_g1_long_result_truncated_server_side():
    """G1: 200,000 字符 result ⇒ current_card_result 截为 500 字符 + 省略号。"""
    k, l = fresh_dual_db()
    try:
        seed_run_with_result(k, "x" * 200000)
        a = console.api_tasks(fields="active")["team_work_active"]
        got = a["current_card_result"]
        assert got is not None
        assert len(got) == MAX + 1, f"期望 {MAX}+省略号, got len={len(got)}"
        assert got.endswith("…"), "截断必须以省略号结尾"
        assert got == "x" * MAX + "…", "截断内容必须为前 500 字符 + 省略号"
    finally:
        cleanup(k, l)


def test_g2_payload_size_bounded():
    """G2 载荷守护: 超长 result 下 active 段响应体仍 ~1KB 量级 (硬上界 4KB)。

    修前实测 200,643 B (DOC-AUDIT §4.1); 基线 583 B。修后 result 被截到
    501 字符, 响应体应回到 1KB 量级, 与 result 原始长度解耦。
    """
    k, l = fresh_dual_db()
    try:
        seed_run_with_result(k, "y" * 200000)
        body = active_body_bytes(console.api_tasks(fields="active"))
        assert body < 4096, f"active 段响应体 {body} B 超出 ~1KB 量级上界"
        # 解耦性: 再放大 10 倍, 尺寸不变
        kc = sqlite3.connect(k)
        kc.execute("UPDATE tasks SET result=? WHERE id='t_p1'",
                   ("z" * 2000000,))
        kc.commit()
        kc.close()
        body2 = active_body_bytes(console.api_tasks(fields="active"))
        assert body2 == body, (
            f"载荷随 result 长度膨胀: {body} -> {body2}")
    finally:
        cleanup(k, l)


def test_g3_two_endpoints_identical():
    """G3: 截断后两端点键集逐字一致且 current_card_result 同值 (#178)。"""
    k, l = fresh_dual_db()
    try:
        seed_run_with_result(k, "w" * 100000)
        a_light = console.api_tasks(fields="active")["team_work_active"]
        a_full = console.api_tasks()["team_work_active"]
        assert set(a_light) == set(a_full), (
            f"light-only={set(a_light)-set(a_full)} "
            f"full-only={set(a_full)-set(a_light)}")
        assert a_light["current_card_result"] == a_full["current_card_result"]
        assert len(a_light["current_card_result"]) == MAX + 1
    finally:
        cleanup(k, l)


def test_g4_defaults_and_short_passthrough():
    """G4: 缺省 None 不变; 短 result 原样透传 (不加省略号)。"""
    k, l = fresh_dual_db()
    try:
        # 无 result (NULL) ⇒ None
        seed_run_with_result(k, None)
        a = console.api_tasks(fields="active")["team_work_active"]
        assert a["current_card_result"] is None, "缺省 None 语义必须保持"
        # 短 result 原样
        kc = sqlite3.connect(k)
        kc.execute("UPDATE tasks SET result=? WHERE id='t_p1'",
                   ("简短结果",))
        kc.commit()
        kc.close()
        a = console.api_tasks(fields="active")["team_work_active"]
        assert a["current_card_result"] == "简短结果"
    finally:
        cleanup(k, l)


def test_g5_boundary_500_vs_501():
    """G5: len==500 原样; len==501 截为 500+省略号 (len 仍 501, 内容不同)。"""
    k, l = fresh_dual_db()
    try:
        seed_run_with_result(k, "a" * MAX)
        a = console.api_tasks(fields="active")["team_work_active"]
        assert a["current_card_result"] == "a" * MAX, "500 字符边界不得截断"
        kc = sqlite3.connect(k)
        kc.execute("UPDATE tasks SET result=? WHERE id='t_p1'",
                   ("a" * (MAX + 1),))
        kc.commit()
        kc.close()
        a = console.api_tasks(fields="active")["team_work_active"]
        assert a["current_card_result"] == "a" * MAX + "…"
    finally:
        cleanup(k, l)
