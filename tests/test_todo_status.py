"""验证 TODO.md 状态字段读写 (parse_status / write_status_atomic)。

统一格式 (2026-10-03 减法): 只认『## 当前任务』段内的 `- 状态: <值>` 行。
旧的"全文件一行式状态字段" fallback 已删 —— 它掩盖过 worker 写错格式
(丢 `- ` 前缀) 的静默失败。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mw.todo import (
    init_todo, parse_status, write_status_atomic,
    TodoTask, RESULT_PLACEHOLDER,
    STATUS_COMPLETED, STATUS_FAILED, STATUS_BLOCKED,
    STATUS_WORKING, STATUS_INPROGRESS,
)


def _make_todo(tmpdir: Path, status: str = "进行中") -> Path:
    """在 tmpdir 写一份标准 TODO.md, 返回路径。"""
    p = tmpdir / "TODO.md"
    task = TodoTask(
        step_id="probe",
        title="PROBE 任务",
        goal="测试状态字段",
        assignee="coder",
        product_workspace=str(tmpdir),
        status=status,
    )
    init_todo(p, "PROBE", task)
    return p


def test_parse_status_completed(tmp_path):
    p = _make_todo(tmp_path, status="completed")
    assert parse_status(p) == "completed"


def test_parse_status_working(tmp_path):
    p = _make_todo(tmp_path, status="working")
    assert parse_status(p) == "working"


def test_parse_status_failed(tmp_path):
    p = _make_todo(tmp_path, status="failed")
    assert parse_status(p) == "failed"


def test_parse_status_missing_file(tmp_path):
    p = tmp_path / "no_such.md"
    assert parse_status(p) is None


def test_parse_status_no_status_field(tmp_path):
    """如果文件没『状态』字段, 返回 None (而不是抛错)。"""
    p = tmp_path / "TODO.md"
    p.write_text("# Team Work: test\n\n## 当前任务\n- 步骤: x\n- 目标: y\n", encoding="utf-8")
    assert parse_status(p) is None


def test_parse_status_english_colon(tmp_path):
    """冒号中英文都认 (: 与 ：), 但必须在『当前任务』段内且带 `- ` 前缀。"""
    p = tmp_path / "TODO.md"
    p.write_text("## 当前任务\n- 状态：completed\n", encoding="utf-8")
    assert parse_status(p) == "completed"


def test_parse_status_requires_section(tmp_path):
    """统一格式: 一行式 `当前任务状态: completed` (无 section) 不再被认 → None。

    旧 fallback 已删。worker 必须写 `## 当前任务` + `- 状态: x`。
    """
    p = tmp_path / "TODO.md"
    p.write_text(
        "# Task: t_xxx\n"
        "承接方: coder\n"
        "当前任务状态: completed\n"
        "结果: done\n",
        encoding="utf-8",
    )
    assert parse_status(p) is None


def test_parse_status_bare_status_line_not_recognized(tmp_path):
    """统一格式: 单独一行 `状态: working` (无 section) 不再被认 → None。

    这防止 worker 丢了 `- ` 前缀 / 丢了 section 时, reaper 误读到状态。
    """
    p = tmp_path / "TODO.md"
    p.write_text("状态: working\n", encoding="utf-8")
    assert parse_status(p) is None


def test_parse_status_only_current_section(tmp_path):
    """只有『当前任务』段的状态行被读; 其他段的状态行忽略。"""
    p = tmp_path / "TODO.md"
    p.write_text(
        "## 当前任务\n"
        "- 状态: working\n"
        "\n"
        "## 已完成\n"
        "- 状态: completed\n",  # 历史段, 不是当前
        encoding="utf-8",
    )
    assert parse_status(p) == "working"


def test_write_status_atomic_changes_field(tmp_path):
    p = _make_todo(tmp_path, status="working")
    assert parse_status(p) == "working"
    ok = write_status_atomic(p, "completed")
    assert ok is True
    assert parse_status(p) == "completed"


def test_write_status_atomic_preserves_other_fields(tmp_path):
    """写状态时不能动其它字段 (所有权: worker 只改状态/结果)。"""
    p = _make_todo(tmp_path, status="working")
    before = p.read_text()
    write_status_atomic(p, "completed")
    after = p.read_text()
    assert len(before.splitlines()) == len(after.splitlines())
    for line in before.splitlines():
        if line.strip().startswith(("- 步骤:", "- 目标:", "- 承接方:",
                                     "- 产物目录:")):
            assert line in after, f"行被改了: {line}"


def test_write_status_atomic_no_status_field(tmp_path):
    """没有『状态』行 → 返回 False, 不写。"""
    p = tmp_path / "TODO.md"
    p.write_text("# Team Work: x\n\n## 当前任务\n- 步骤: x\n", encoding="utf-8")
    ok = write_status_atomic(p, "completed")
    assert ok is False


def test_write_status_atomic_creates_tmp_then_rename(tmp_path):
    """验证 atomic rename: 写完不应该残留 .tmp 文件。"""
    p = _make_todo(tmp_path, status="working")
    write_status_atomic(p, "completed")
    assert not (tmp_path / "TODO.md.tmp").exists()
    assert p.exists()


if __name__ == "__main__":
    import tempfile as _tf
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    fail = 0
    for name, fn in tests:
        with _tf.TemporaryDirectory() as td:
            try:
                fn(Path(td))
                print(f"  PASS  {name}")
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}")
                fail += 1
            except Exception as e:
                print(f"  ERROR {name}: {e!r}")
                fail += 1
    if fail:
        print(f"\n{fail} failures")
        sys.exit(1)
    print(f"\nall {len(tests)} tests passed")
