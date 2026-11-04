"""#191 守护: kanban 影子库回落 —— 404 排查提示 + HERMES_HOME 治本导出。

依据: docs/TRIAGE-S-20261102.md §2.6 / ISSUES-S.md #191 段。
根因: hermes CLI kanban_db_path() 解析序 HERMES_KANBAN_DB >
HERMES_KANBAN_HOME > get_default_hermes_root(); 裁剪 env 的 subprocess
(无 HERMES_HOME) 静默回落影子库 ~/.hermes/kanban.db, 与 mw 现读库
/opt/data/kanban.db 成为两个库, 全程零报错。

覆盖:
  F191-404   api_task_detail 查无卡 ⇒ 404 文案含影子库排查提示
             (HERMES_HOME / 影子库 ~/.hermes/kanban.db 关键词)。
  F191-404b  提示不破坏既有契约: code=404 不变, task_id 仍在文案中。
  F191-env   deploy/main_entry.sh 导出 HERMES_HOME=/opt/data (治本)。

双口径断言 (照抄 #190 守护)。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("API_SERVER_KEY", "test-key")
os.environ.setdefault("MW_GATEWAY_URL", "http://127.0.0.1:1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mw.console as console  # noqa: E402

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


def _empty_kanban():
    f = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    f.close()
    c = sqlite3.connect(f.name)
    c.executescript("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
            block_kind TEXT, result TEXT, created_at REAL, started_at REAL,
            completed_at REAL
        );
    """)
    c.commit()
    c.close()
    console.KANBAN_DB = f.name
    return f.name


def test_f191_task_404_carries_shadow_db_hint():
    path = _empty_kanban()
    try:
        d = console.api_task_detail("t_ghost")
        check("F191 查无卡 ⇒ code=404", d.get("code") == 404, f"got {d!r}")
        err = d.get("error", "")
        check("F191 404 文案含 HERMES_HOME 提示",
              "HERMES_HOME" in err, f"got {err!r}")
        check("F191 404 文案含影子库路径提示",
              "影子库" in err and "~/.hermes/kanban.db" in err,
              f"got {err!r}")
        check("F191 404 文案含 subprocess 排查方向",
              "subprocess" in err, f"got {err!r}")
    finally:
        os.unlink(path)


def test_f191_404_contract_preserved():
    """既有契约不破: code=404、task_id 回显、error 键非空。"""
    path = _empty_kanban()
    try:
        d = console.api_task_detail("t_needle_191")
        check("F191 文案仍含 task_id (可定位)",
              "t_needle_191" in d.get("error", ""), f"got {d!r}")
        check("F191 文案仍含『不存在』原语义",
              "不存在" in d.get("error", ""), f"got {d!r}")
    finally:
        os.unlink(path)


def test_f191_main_entry_exports_hermes_home():
    src = (ROOT / "deploy" / "main_entry.sh").read_text(encoding="utf-8")
    check("F191 main_entry.sh 导出 HERMES_HOME=/opt/data",
          "export HERMES_HOME=/opt/data" in src)
    # 治本语义: 导出落在 MW_KANBAN_DB 同组 (mw 进程树 spawn 的 subprocess 继承)
    lines = [ln.strip() for ln in src.splitlines()]
    i_env = next((i for i, ln in enumerate(lines)
                  if ln == "export HERMES_HOME=/opt/data"), -1)
    i_kanban = next((i for i, ln in enumerate(lines)
                     if ln.startswith("export MW_KANBAN_DB")), -1)
    check("F191 HERMES_HOME 导出与 MW_KANBAN_DB 同组 (entry 早期, 先于 exec)",
          i_env >= 0 and i_kanban >= 0 and abs(i_env - i_kanban) <= 10,
          f"env@{i_env} kanban@{i_kanban}")


def main():
    global _COLLECT_ONLY
    _COLLECT_ONLY = True
    tests = [
        test_f191_task_404_carries_shadow_db_hint,
        test_f191_404_contract_preserved,
        test_f191_main_entry_exports_hermes_home,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            check(t.__name__, False, f"EXC: {type(e).__name__}: {e}")
    total = len(PASS) + len(FAIL)
    print()
    if not FAIL:
        print(f"\033[92m=== ALL {total} #191 TESTS PASSED ===\033[0m")
        return 0
    print(f"\033[91m=== {len(FAIL)}/{total} FAILED ===\033[0m")
    for n, d in FAIL:
        print(f"  - {n}: {d}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
