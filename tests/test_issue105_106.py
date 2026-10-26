"""tests/test_issue105_106.py — issue #105 + #106 回归测试 (B 批 s2, 2026-10-25)。

#105 (install.sh §2c 播种判空被 data/main 0700 权限击穿):
  A1  静态护栏: 判空不再出现宿主 `ls -A data/main`; 判空/播种都走
      root helper (`_seed_helper` → docker run --user 0:0)。
  A2  行为仿真: 源目录宿主不可读 (真 chmod 000, 本容器用户即非 root) 时,
      假 docker 桩以「root 视角」返回非空列表 ⇒ 不再误报 empty, 而是
      走播种 (cp -a + chown + chmod u+rwX)。
  A3  卷有内容但缺 kanban.db ⇒ 走补种路径 (topping up), 已有内容不覆盖。
  A4  卷有内容且关键文件齐 ⇒ 维持 keep 语义, 不播种。
  A5  源真空 ⇒ nothing to seed, 零播种调用。
  仿真纪律: 假 docker 是 PATH 前置桩, 不碰真 daemon; 000 目录只建在
  tmp_path 里, 不 chmod 任何生产目录。

#106 (archived 卡 harvest_contracts 行滞留 PENDING):
  B1  _reconcile_archived_orphans: kanban archived 孤儿卡契约行推
      ARCHIVED, 走正规 update_worker_state (worker.state 事件 + actor
      审计轨迹), 巡检口径不再算它孤儿。
  B2  launch 护栏: 直建直归档的测试卡不再 409 (假阳性阻塞源消除)。
  B3  launch 护栏: 未归档的真孤儿 PENDING 卡照旧 409 (拦截语义不回归)。
  B4  api_tasks 巡检读面同样联动对账。
  B5  sync 幂等: 二次调用零变更; CANCELLED/无契约行跳过不动。
  B6  kanban.db 不可读时联动降级 no-op, 不炸消费方。

跑法: pytest tests/test_issue105_106.py -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

from mw.ledger import Ledger  # noqa: E402
from mw import console as console_mod  # noqa: E402

INSTALL_SH = ROOT / "deploy" / "install.sh"


# ══════════════════════════ #105: install.sh 播种判空 ══════════════════════

def _extract_seed_fragment() -> str:
    """从 install.sh 抠出 _seed_helper + _seed_main_volume 的函数定义。

    测试直接 source 真实函数体 (而非复制实现), 保证测的就是要部署的代码。
    锚点: 起于 `^_seed_helper() {`, 止于「Seeded volume」say 后的首个 `^}`。
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    start = text.index("_seed_helper() {")
    tail_anchor = 'say "Seeded volume ${vol} from data/main"'
    end = text.index(tail_anchor, start)
    end = text.index("\n}\n", end) + len("\n}\n")
    return text[start:end]


def _run_seed_case(tmp_path: Path, *, src_mode: int, src_files: list[str],
                   vol_files: list[str], vol_missing: list[str],
                   seed_fail: bool = False) -> dict:
    """跑一次 _seed_main_volume 仿真, 返回 {rc, out, calls}。

    假 docker 桩: PATH 前置的可执行脚本, 按 -c 命令串分派场景数据 —
      ls -A /src   → src_listing 文件 (root 视角的源目录列表)
      ls -A /seed  → vol_listing 文件 (卷列表)
      [ -e /seed/X ] → X 在 vol_missing 里 ⇒ exit 1 (缺文件)
      其余 (cp/chown/top-up) → 记入 calls.log
    源目录真 chmod 到 src_mode (0700 + 属主非 root 时宿主 ls 必失败),
    桩的 root 视角列表独立给出 —— 两者的分歧正是 issue #105 的根因形状。
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "src_listing").write_text("\n".join(src_files) + ("\n" if src_files else ""))
    (state / "vol_listing").write_text("\n".join(vol_files) + ("\n" if vol_files else ""))
    (state / "vol_missing").write_text("\n".join(vol_missing) + ("\n" if vol_missing else ""))
    calls_log = state / "calls.log"
    calls_log.touch()

    docker_stub = bin_dir / "docker"
    docker_stub.write_text(
        "#!/usr/bin/env bash\n"
        'echo "docker $*" >> "$MW105_CALLS"\n'
        'cmd=""; prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "-c" ]; then cmd="$a"; fi\n'
        '  prev="$a"\n'
        'done\n'
        'case "$cmd" in\n'
        '  *"ls -A /src"*)  cat "$MW105_STATE/src_listing"; ;;\n'
        '  *"ls -A /seed"*) cat "$MW105_STATE/vol_listing"; ;;\n'
        '  *"-e /seed/"*)\n'
        '    kf=${cmd##*/seed/}; kf=${kf% ]*}; kf=${kf%]}\n'
        '    if [ -s "$MW105_STATE/vol_missing" ] && grep -Fxq "$kf" "$MW105_STATE/vol_missing"; then exit 1; fi\n'
        '    ;;\n'
        '  *)\n'
        '    if [ "${MW105_SEED_FAIL:-0}" = "1" ]; then exit 1; fi\n'
        '    ;;\n'
        'esac\n'
        'exit 0\n', encoding="utf-8")
    docker_stub.chmod(0o755)

    # 真实源目录 (data/main), 按 mode 落权限: 0700 场景宿主 ls 必失败
    (tmp_path / "data" / "main").mkdir(parents=True)
    for f in src_files:
        (tmp_path / "data" / "main" / f).write_text("x")
    os.chmod(tmp_path / "data" / "main", src_mode)

    frag = tmp_path / "seed_frag.sh"
    frag.write_text(_extract_seed_fragment(), encoding="utf-8")

    runner = tmp_path / "run_case.sh"
    runner.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'export PATH="{bin_dir}:$PATH"\n'
        f'export MW105_STATE="{state}"\n'
        f'export MW105_CALLS="{calls_log}"\n'
        'export MW105_SEED_FAIL=' + ("1" if seed_fail else "0") + "\n"
        'say()  { printf "[install] %s\\n" "$1"; }\n'
        'fail() { printf "FAIL: %s\\n" "$1" >&2; exit 1; }\n'
        'HERMES_IMAGE=fake/hermes:test\n'
        f'source "{frag}"\n'
        f'cd "{tmp_path}"\n'
        "_seed_main_volume testvol\n", encoding="utf-8")
    runner.chmod(0o755)
    p = subprocess.run(["bash", str(runner)], capture_output=True, text=True, timeout=30)
    calls = calls_log.read_text()
    # 收尾: 恢复权限让 tmp_path 可删 (只动测试自建目录)
    for d in (tmp_path / "data" / "main",):
        try:
            os.chmod(d, 0o755)
        except OSError:
            pass
    return {"rc": p.returncode, "out": p.stdout + p.stderr, "calls": calls}


# ── A1: 静态护栏 ────────────────────────────────────────────────────────────

def test_a1_no_host_ls_empty_check() -> None:
    """§2c 判空绝不允许再出现宿主 `ls -A data/main` (issue #105 根因)。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "ls -A data/main" not in text, (
        "install.sh 又用宿主 ls 判空了 —— 0700/uid-10000 源目录会输出空, "
        "静默跳过播种 (issue #105 实案根因)")


def test_a1_seed_goes_through_root_helper() -> None:
    """判空/播种/卷检查统一走 `_seed_helper` (docker run --user 0:0)。"""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "_seed_helper()" in text, "缺 _seed_helper 统一 helper 前缀"
    helper_def = text[text.index("_seed_helper() {"):text.index("}", text.index("_seed_helper() {"))]
    assert "--entrypoint sh" in helper_def and "--user 0:0" in helper_def, (
        "helper 必须带 --entrypoint sh (绕 s6) 与 --user 0:0 (root 读 0700 源)")
    # 播种与补种产物都要对齐 worker uid 可写 (附带观察 #4)
    assert text.count("chown -R 10000:10000") >= 3, (
        "播种 / 补种 / §7b 属主修正三处都要 chown 10000:10000")
    assert "chmod -R u+rwX" in text, (
        "播种产物要 chmod u+rwX, 保证 worker uid (10000) 可写")


# ── A2: 0700 不可读源不再假空 (行为仿真) ────────────────────────────────────

def test_a2_unreadable_source_no_longer_false_empty(tmp_path) -> None:
    """源目录宿主侧不可读 (000: 等价 170 现场「宿主用户读不了 0700/uid-10000
    源」的形状) + root helper 见非空 ⇒ 走播种而非跳过。

    这正是 170 事故现场: 宿主 ls 视角为空, root helper 视角有 kanban.db。
    旧实现 (宿主 ls -A) 会打印 empty 静默 skip; 新实现必须真播种。
    (本容器测试进程即属主, 0700 对自己仍可 —— 用 000 构造「宿主视角为空,
    容器 root 视角非空」的最小等价形状; 只动 tmp_path, 不碰生产目录。)
    """
    r = _run_seed_case(tmp_path, src_mode=0o000,
                       src_files=["kanban.db", "config.yaml"],
                       vol_files=[], vol_missing=[])
    assert r["rc"] == 0, r["out"]
    assert "data/main is empty" not in r["out"], (
        f"0700 源目录仍被判空 — 判空没走 root helper: {r['out']}")
    assert "Seeded volume testvol" in r["out"], r["out"]
    # 播种命令以 root helper 执行且带 chown/chmod
    assert "cp -a /src/. /seed/" in r["calls"]
    assert "--user 0:0" in r["calls"]
    assert "chmod -R u+rwX" in r["calls"]


def test_a2_truly_empty_source_still_skips(tmp_path) -> None:
    """helper 视角也真空 ⇒ 正常 nothing to seed, 零播种 (语义不扩大)。"""
    r = _run_seed_case(tmp_path, src_mode=0o700, src_files=[],
                       vol_files=[], vol_missing=[])
    assert r["rc"] == 0, r["out"]
    assert "nothing to seed" in r["out"], r["out"]
    assert "cp -a" not in r["calls"], "真空源不应触发播种"


# ── A3/A4: §7b 补种路径 ────────────────────────────────────────────────────

def test_a3_volume_with_content_missing_db_gets_topped_up(tmp_path) -> None:
    """卷有内容但缺 kanban.db (170 事故卷形态) ⇒ 补种而非 keep 原样。"""
    r = _run_seed_case(tmp_path, src_mode=0o755,
                       src_files=["kanban.db", "skills"],
                       vol_files=["skills"], vol_missing=["kanban.db"])
    assert r["rc"] == 0, r["out"]
    assert "has content but misses: kanban.db" in r["out"], r["out"]
    assert "keeping it" not in r["out"], "缺关键文件不得再走 keep"
    assert "cp -a" in r["calls"], "补种必须真的执行 cp"
    assert "chown -R 10000:10000" in r["calls"], "补种后同样对齐 10000:10000"


def test_a4_volume_complete_still_keeps(tmp_path) -> None:
    """卷有内容且 kanban.db 在位 ⇒ 维持原 keep 语义, 零 cp。"""
    r = _run_seed_case(tmp_path, src_mode=0o755,
                       src_files=["kanban.db", "skills"],
                       vol_files=["kanban.db", "skills"], vol_missing=[])
    assert r["rc"] == 0, r["out"]
    assert "keeping it" in r["out"], r["out"]
    assert "cp -a" not in r["calls"], "完整卷不得重复播种/补种"


# ══════════════════════════ #106: archived 契约联动 ══════════════════════════

_KANBAN_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS tasks ("
    " id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,"
    " block_kind TEXT, result TEXT, created_at REAL,"
    " started_at REAL, completed_at REAL);")


def _make_kanban(path: str, rows: list[tuple[str, str]]) -> None:
    """建测试 kanban.db; rows = [(task_id, status)]。"""
    con = sqlite3.connect(path)
    con.executescript(_KANBAN_SCHEMA)
    for tid, st in rows:
        con.execute(
            "INSERT OR REPLACE INTO tasks (id,title,assignee,status,"
            "created_at) VALUES (?,?,?,?,?)",
            (tid, "测试卡", "coder", st, time.time()))
    con.commit()
    con.close()


def _seed_orphan(led: Ledger, task_id: str, status: str = "PENDING") -> None:
    """直建卡: 有契约行、无 team_work_run (孤儿面), 可选推进到指定状态。"""
    led.put_contract(task_id, {"required_files": []})
    if status != "PENDING":
        # 终态迁移 (CANCELLED/ARCHIVED) 的 actor 白名单只有 operator
        actor = "operator" if status in ("CANCELLED", "ARCHIVED") else "harvest"
        led.update_worker_state(task_id, status, actor=actor)


def _pending_orphan_ids(led: Ledger) -> set:
    return {o["task_id"] for o in led.list_pending_orphan_cards(limit=50)}


# ── B1: 联动对账主路径 ──────────────────────────────────────────────────────

def test_b1_reconcile_archives_contract_and_leaves_events(tmp_path, monkeypatch) -> None:
    """kanban archived 孤儿卡: 契约行 → ARCHIVED, 且走正规状态机留痕。"""
    ldb = str(tmp_path / "ledger.db")
    led = Ledger(ldb)
    try:
        monkeypatch.setattr(console_mod, "LEDGER_DB", ldb)
        monkeypatch.setattr(console_mod, "KANBAN_DB", str(tmp_path / "kanban.db"))
        _seed_orphan(led, "t_arch")
        _make_kanban(console_mod.KANBAN_DB, [("t_arch", "archived"),
                                             ("t_other", "running")])
        synced = console_mod._reconcile_archived_orphans(led)
        assert synced == ["t_arch"], synced
        assert led.get_contract("t_arch")["harvest_status"] == "ARCHIVED"
        # 巡检口径: 不再算 PENDING 孤儿
        assert "t_arch" not in _pending_orphan_ids(led)
        # 正规入口留痕: worker.state 事件 (from/to/actor) + harvest_log 审计
        evs = [json.loads(r["payload"]) for r in led.conn.execute(
            "SELECT payload FROM mw_events WHERE event_type='worker.state' "
            "AND task_id='t_arch'").fetchall()]
        assert any(e.get("from") == "PENDING" and e.get("to") == "ARCHIVED"
                   and e.get("actor") == "operator" for e in evs), evs
        log = led.get_contract("t_arch")["harvest_log"] or ""
        assert "[operator]" in log, log
    finally:
        led.close()


# ── B2/B3: launch 护栏 ──────────────────────────────────────────────────────

def _stub_launch_env(monkeypatch, tmp_path: Path) -> list:
    """最小 launch 依赖: registry 桩 + api_dispatch 记录桩。"""
    reg_path = tmp_path / "registry.json"
    reg_path.write_text(json.dumps(
        {"coder": {"api_url": "http://stub", "api_key": "x" * 32}}))
    monkeypatch.setattr(console_mod, "REGISTRY", str(reg_path))
    calls: list = []

    def _stub(payload):
        calls.append(dict(payload))
        return {"ok": True, "accepted": True, "task_id": payload.get("task_id")}

    monkeypatch.setattr(console_mod, "api_dispatch", _stub)
    return calls


def test_b2_launch_not_blocked_by_archived_orphan(tmp_path, monkeypatch) -> None:
    """直建直归档的测试卡 (旧版会 409 orphan_pending_cards) 不再拦 launch。"""
    ldb = str(tmp_path / "ledger.db")
    led = Ledger(ldb)
    try:
        monkeypatch.setattr(console_mod, "LEDGER_DB", ldb)
        monkeypatch.setattr(console_mod, "KANBAN_DB", str(tmp_path / "kanban.db"))
        _seed_orphan(led, "t_stale_arch")
        _make_kanban(console_mod.KANBAN_DB, [("t_stale_arch", "archived")])
        # 前置确认: 联动前它确实会命中孤儿面 (证明场景真实存在)
        assert "t_stale_arch" in _pending_orphan_ids(led)
        calls = _stub_launch_env(monkeypatch, tmp_path)
        r = console_mod.api_team_work_launch({
            "initial_task": "r", "session_id": "s_main",
            "steps": [{"step_id": "a", "title": "A", "task_id": "t_new",
                       "assignee": "coder"}],
        })
        assert r.get("ok") is True, r
        assert led.get_contract("t_stale_arch")["harvest_status"] == "ARCHIVED"
    finally:
        led.close()


def test_b3_launch_still_blocks_real_orphan(tmp_path, monkeypatch) -> None:
    """未归档的真孤儿 PENDING 卡 (kanban running) 照旧 409 — 拦截语义不回归。"""
    ldb = str(tmp_path / "ledger.db")
    led = Ledger(ldb)
    try:
        monkeypatch.setattr(console_mod, "LEDGER_DB", ldb)
        monkeypatch.setattr(console_mod, "KANBAN_DB", str(tmp_path / "kanban.db"))
        _seed_orphan(led, "t_running")
        _make_kanban(console_mod.KANBAN_DB, [("t_running", "running")])
        _stub_launch_env(monkeypatch, tmp_path)
        r = console_mod.api_team_work_launch({
            "initial_task": "r", "session_id": "s_x",
            "steps": [{"step_id": "a", "title": "A", "task_id": "t_new",
                       "assignee": "coder"}],
        })
        assert r.get("code") == 409, r
        kinds = [b.get("kind") for b in r.get("blockers", [])]
        assert "orphan_pending_cards" in kinds, r
        # 联动不得误伤未归档卡
        assert led.get_contract("t_running")["harvest_status"] == "PENDING"
    finally:
        led.close()


# ── B4: 巡检读面 (api_tasks) 联动 ───────────────────────────────────────────

def test_b4_api_tasks_list_reconciles_archived_orphan(tmp_path, monkeypatch) -> None:
    """/api/tasks (部署健康度巡检读面) 先对账再出列表。"""
    ldb = str(tmp_path / "ledger.db")
    kdb = str(tmp_path / "kanban.db")
    led = Ledger(ldb)
    try:
        monkeypatch.setattr(console_mod, "LEDGER_DB", ldb)
        monkeypatch.setattr(console_mod, "KANBAN_DB", kdb)
        _seed_orphan(led, "t_arch2")
        _make_kanban(kdb, [("t_arch2", "archived")])
        out = console_mod.api_tasks()
        assert "tasks" in out
        assert led.get_contract("t_arch2")["harvest_status"] == "ARCHIVED"
        assert "t_arch2" not in _pending_orphan_ids(led)
    finally:
        led.close()


# ── B5: sync 幂等与边界 ─────────────────────────────────────────────────────

def test_b5_sync_idempotent_and_skips_terminal_or_missing(tmp_path) -> None:
    """二次 sync 零变更; CANCELLED 卡 (禁 →ARCHIVED 边) 与无契约卡跳过。"""
    led = Ledger(str(tmp_path / "ledger.db"))
    try:
        _seed_orphan(led, "t_ok")
        assert led.sync_archived_contract_states(["t_ok"]) == ["t_ok"]
        # 第二次: 已 ARCHIVED, 无新变更 (幂等)
        assert led.sync_archived_contract_states(["t_ok"]) == []
        _seed_orphan(led, "t_cancel", status="CANCELLED")
        assert led.sync_archived_contract_states(
            ["t_cancel", "t_no_such_row"]) == []
        assert led.get_contract("t_cancel")["harvest_status"] == "CANCELLED"
    finally:
        led.close()


# ── B6: kanban 不可读 ⇒ 安全降级 ────────────────────────────────────────────

def test_b6_missing_kanban_db_degrades_to_noop(tmp_path, monkeypatch) -> None:
    """kanban.db 缺失/不可读时联动 no-op 返回 [], 消费方不被拖崩。"""
    ldb = str(tmp_path / "ledger.db")
    led = Ledger(ldb)
    try:
        monkeypatch.setattr(console_mod, "LEDGER_DB", ldb)
        monkeypatch.setattr(console_mod, "KANBAN_DB",
                            str(tmp_path / "nope" / "kanban.db"))
        _seed_orphan(led, "t_any")
        assert console_mod._reconcile_archived_orphans(led) == []
        # 卡保持原状 (无对账依据时绝不乱推终态)
        assert led.get_contract("t_any")["harvest_status"] == "PENDING"
    finally:
        led.close()


# ── B7: s6fix P2-7 批内单行失败不中断整批 ───────────────────────────────────

def test_b7_sync_continues_batch_when_one_row_raises(tmp_path, monkeypatch) -> None:
    """并发竞对同一卡时 update_worker_state 抛 ValueError, 批内其余卡照常推。"""
    led = Ledger(str(tmp_path / "ledger.db"))
    try:
        _seed_orphan(led, "t_b7_1")
        _seed_orphan(led, "t_b7_2")
        _seed_orphan(led, "t_b7_3")
        real = led.update_worker_state

        def flaky(task_id, new_status, **kw):
            # 模拟并发对手抢先: 第二行迁移必炸, 其余走真实现
            if task_id == "t_b7_2":
                raise ValueError("非法状态迁移 ARCHIVED → ARCHIVED")
            return real(task_id, new_status, **kw)

        monkeypatch.setattr(led, "update_worker_state", flaky)
        synced = led.sync_archived_contract_states(
            ["t_b7_1", "t_b7_2", "t_b7_3"])
        # 旧实现: t_b7_2 抛 ValueError 直接中断, t_b7_3 永远推不到
        assert synced == ["t_b7_1", "t_b7_3"], synced
        assert led.get_contract("t_b7_3")["harvest_status"] == "ARCHIVED"
    finally:
        led.close()
