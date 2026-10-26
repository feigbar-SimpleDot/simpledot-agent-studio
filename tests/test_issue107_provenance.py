"""tests/test_issue107_provenance.py — issue #107 provenance 头采集器回归。

守护点 (对应卡面自测要求):
  C1  头格式: build_header 产出与团队约定格式逐字段一致, 且被 PROVENANCE_RE
      识别 (格式 = 幂等判定的唯一依据, 两者必须互洽)。
  C2  sha 一致性: collect_head() == `git rev-parse HEAD` 实际值;
      --head 缺省采集链路 (collect_provenance) 同样取到真实 sha。
  C3  幂等: CLI 对同一文件跑两次只加一次头, 正文逐字节不变。
  C4  加头正确性: 无头文件加头后首行合规, 原内容完整保留在第二行起。
  C5  --print 只出一行头且格式合规, 不落盘 (组合用法链路)。
  C6  --cmd 覆盖生效 (组合用法里声明真实命令); 含空格命令往返可解析。
  C7  幂等判定不误伤: 非注释首行 (命令输出) 照常加头。

2026-10-25 D-2 修 (Gitea #112) 新增守护:
  D2-1  首行是手写注释 ⇒ **不叠加**第二行头 (原行一字不动保留);
  D2-2  工具自产的头仍严格幂等 (跑两次只一行头, 正文逐字节不变);
  D2-3  首行是近似头 (含 host=/head= 标记但格式不合规) ⇒ 替换该行,
        不产生双头, 且新头携带本次采集值;
  D2-4  --force 显式覆盖: 首行是手写注释时仍可加头 (保留原能力);
  D2-5  注释首行时 stderr 有 NOTE 提示 (不静默跳过)。

跑法: pytest tests/test_issue107_provenance.py -v
"""
from __future__ import annotations

import importlib.util
import re
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "evidence_provenance.py"

# 按文件路径加载 tools/evidence_provenance (tools/ 非包, 与 ast_diff 同风格)
_spec = importlib.util.spec_from_file_location("evidence_provenance", SCRIPT)
assert _spec and _spec.loader
ep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ep)


def _git_head() -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _run_cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *argv],
                          capture_output=True, text=True)


# ──────────────────────────────────────────────────────────────────
# C1: 头格式与约定逐字段一致, 且能被幂等判定正则识别
# ──────────────────────────────────────────────────────────────────
def test_header_format_matches_convention():
    info = ep.collect_provenance(cmd="deploy/verify.sh --170")
    header = ep.build_header(info)
    # 约定字面格式: "# host=… head=… cmd=… ts=…" 单行
    assert header.startswith("# host=")
    m = re.fullmatch(
        r"# host=(\S+) head=(\S+) cmd=(.+) ts=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)",
        header)
    assert m, f"头格式不符约定: {header!r}"
    assert m.group(1) == socket.gethostname()
    assert m.group(2) == info["head"]
    assert m.group(3) == "deploy/verify.sh --170"
    assert ep.is_provenance_line(header)          # 格式与幂等判定互洽
    assert ep.is_provenance_line(header + "\n")   # 容忍行尾换行


# ──────────────────────────────────────────────────────────────────
# C2: sha 与实际 HEAD 一致
# ──────────────────────────────────────────────────────────────────
def test_head_matches_actual_git_head():
    assert ep.collect_head() == _git_head()
    assert ep.collect_provenance()["head"] == _git_head()


# ──────────────────────────────────────────────────────────────────
# C4: 无头文件加头 — 首行合规, 正文完整保留
# ──────────────────────────────────────────────────────────────────
def test_add_header_to_plain_file(tmp_path):
    f = tmp_path / "evidence.txt"
    body = "PASS line1\nPASS line2\n"
    f.write_text(body, encoding="utf-8")
    r = _run_cli(str(f), "--cmd", "pytest tests/ -q")
    assert r.returncode == 0, r.stderr
    lines = f.read_text(encoding="utf-8").splitlines()
    assert ep.is_provenance_line(lines[0])
    assert f"cmd=pytest tests/ -q " in lines[0] + " "
    assert "\n".join(lines[1:]) + "\n" == body


# ──────────────────────────────────────────────────────────────────
# C3: 幂等 — 跑两次只加一次头, 正文不变
# ──────────────────────────────────────────────────────────────────
def test_idempotent_run_twice_adds_once(tmp_path):
    f = tmp_path / "evidence.log"
    body = "md5 a1b2c3 /evidence/on-170\n"
    f.write_text(body, encoding="utf-8")
    assert _run_cli(str(f)).returncode == 0
    after_first = f.read_text(encoding="utf-8")
    assert _run_cli(str(f)).returncode == 0       # 第二次必须静默幂等
    after_second = f.read_text(encoding="utf-8")
    assert after_second == after_first, "重复运行重复加了头"
    # 只有一行头, 且正文恰好保留在头之后
    assert sum(ep.is_provenance_line(l) for l in after_second.splitlines()) == 1
    assert after_second.endswith(body)


# ──────────────────────────────────────────────────────────────────
# C5: --print 一行头、格式合规、不落盘
# ──────────────────────────────────────────────────────────────────
def test_print_mode(tmp_path):
    f = tmp_path / "should-not-exist.txt"
    r = _run_cli("--print", "--cmd", "my_verify_cmd")
    assert r.returncode == 0, r.stderr
    out = r.stdout.splitlines()
    assert len(out) == 1
    assert ep.is_provenance_line(out[0])
    assert f" head={_git_head()} " in out[0]
    assert not f.exists()


# ──────────────────────────────────────────────────────────────────
# C6: --cmd 含空格命令, 头部字段可被正则完整取回
# ──────────────────────────────────────────────────────────────────
def test_cmd_with_spaces_roundtrip():
    cmd = 'bash -c "pytest tests/ -q 2>&1 | tee out.log"'
    header = ep.build_header(ep.collect_provenance(cmd=cmd))
    m = ep.PROVENANCE_RE.match(header)
    assert m and m.group("cmd") == cmd, f"cmd 字段截错: {header!r}"


# ──────────────────────────────────────────────────────────────────
# C7: 非注释首行 (命令输出) 不误伤, 照常加头
# ──────────────────────────────────────────────────────────────────
def test_plain_output_line_not_treated_as_header(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("PASS line1\n内容\n", encoding="utf-8")
    assert ep.classify_first_line("PASS line1") == "other"
    assert _run_cli(str(f)).returncode == 0
    lines = f.read_text(encoding="utf-8").splitlines()
    assert ep.is_provenance_line(lines[0]) and lines[1] == "PASS line1"


# ──────────────────────────────────────────────────────────────────
# D-2 (Gitea #112): 首行是手写注释 ⇒ 不叠加双头
# ──────────────────────────────────────────────────────────────────
def test_handwritten_comment_first_line_not_duplicated(tmp_path):
    """首行是手写注释时, 工具**不得**再叠加第二行 provenance 头。

    旧判定 (只认严格正则) 把 `# already a header` 判成「无头」⇒ 加头后
    文件首两行都是 '#' 注释 (双头) —— s5 移交项 D-2。
    """
    f = tmp_path / "ev2.txt"
    body = "# already a header\nbody line\n"
    f.write_text(body, encoding="utf-8")
    assert ep.classify_first_line("# already a header") == "comment"
    r = _run_cli(str(f))
    assert r.returncode == 0, r.stderr
    after = f.read_text(encoding="utf-8")
    assert after == body, f"手写注释首行被叠加/改动:\n{after!r}"
    # 关键断言: 全文件里不得出现第二行头
    assert sum(ep.is_provenance_line(l) for l in after.splitlines()) == 0
    assert after.splitlines()[0] == "# already a header"


# ──────────────────────────────────────────────────────────────────
# D-2 (Gitea #112): 工具自产的头仍严格幂等
# ──────────────────────────────────────────────────────────────────
def test_tool_generated_header_still_idempotent(tmp_path):
    """D-2 修完不能把「已有合规头」判成别的 ⇒ 跑两次仍只一行头。"""
    f = tmp_path / "ev3.log"
    body = "md5 a1b2c3 /evidence/on-170\n"
    f.write_text(body, encoding="utf-8")
    assert _run_cli(str(f)).returncode == 0
    first = f.read_text(encoding="utf-8")
    assert ep.is_provenance_line(first.splitlines()[0])
    assert _run_cli(str(f)).returncode == 0          # 第二次必须幂等跳过
    second = f.read_text(encoding="utf-8")
    assert second == first, "跑两次内容变了 (幂等被破坏)"
    assert sum(ep.is_provenance_line(l) for l in second.splitlines()) == 1
    assert second.endswith(body)


# ──────────────────────────────────────────────────────────────────
# D-2 (Gitea #112): 近似头 ⇒ 替换该行, 不产生双头
# ──────────────────────────────────────────────────────────────────
def test_approx_header_is_replaced_not_duplicated(tmp_path):
    """近似头 (含 host=/head= 标记但字段格式不合规) ⇒ 替换, 不叠第二行。"""
    f = tmp_path / "ev4.txt"
    f.write_text("# host=old ts=2020-01-01T00:00:00Z\nbody\n", encoding="utf-8")
    assert ep.classify_first_line("# host=old ts=2020-01-01T00:00:00Z") == "approx"
    assert _run_cli(str(f), "--cmd", "pytest tests/ -q").returncode == 0
    lines = f.read_text(encoding="utf-8").splitlines()
    assert ep.is_provenance_line(lines[0]), lines[0]
    assert "cmd=pytest tests/ -q" in lines[0], lines[0]
    # 旧头被替换掉, 不残留 ⇒ 全文只有一行头
    assert sum(ep.is_provenance_line(l) for l in lines) == 1
    assert "host=old" not in f.read_text(encoding="utf-8")
    assert lines[1] == "body"


# ──────────────────────────────────────────────────────────────────
# D-2 (Gitea #112): --force 显式覆盖 (首行注释也要能加头)
# ──────────────────────────────────────────────────────────────────
def test_force_overrides_comment_first_line(tmp_path):
    """--force: 首行是手写注释时仍在首行之上加头 (保留原能力)。"""
    f = tmp_path / "ev5.md"
    f.write_text("# 验收报告\n内容\n", encoding="utf-8")
    r = _run_cli(str(f), "--force")
    assert r.returncode == 0, r.stderr
    lines = f.read_text(encoding="utf-8").splitlines()
    assert ep.is_provenance_line(lines[0]), lines[0]
    assert lines[1] == "# 验收报告"


# ──────────────────────────────────────────────────────────────────
# D-2 (Gitea #112): 注释首行时 stderr 有 NOTE (不静默跳过)
# ──────────────────────────────────────────────────────────────────
def test_comment_first_line_emits_note(tmp_path):
    f = tmp_path / "ev6.md"
    f.write_text("# 验收报告\n内容\n", encoding="utf-8")
    r = _run_cli(str(f))
    assert r.returncode == 0, r.stderr
    assert "NOTE" in r.stderr, r.stderr
    assert "skipped" in r.stdout, r.stdout
