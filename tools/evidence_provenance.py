#!/usr/bin/env python3
"""tools/evidence_provenance.py — 证据文件 provenance 头采集器 (issue #107)

团队约定 (Gitea issue #107): 验收/测试产出的每件证据文件**首行**加一行
provenance 头, 格式固定为:

    # host=<主机名> head=<commit sha> cmd=<完整命令行> ts=<UTC ISO>

目的: 让下游独立复核能就地判定证据是「真机产出」还是「本机冒充」,
不再逐件回原机对撞 md5 (170 验收 58 件证据 25 件零标记 / 42 件缺 sha 的教训)。

用法 (详见 tools/README-evidence-provenance.md):
    # 给一批证据文件加头 (幂等: 已有头的文件跳过不重复加)
    python tools/evidence_provenance.py out/*.md out/*.log

    # 只打印头, 不落盘 (供 shell 组合: 头 + 命令输出 一次性生成证据文件)
    { python tools/evidence_provenance.py --print; my_verify_cmd > /dev/stdout; } > evidence.txt

    # 覆盖命令行字段 (默认取本脚本 argv; 组合用法里应显式声明真实命令)
    python tools/evidence_provenance.py evidence.txt --cmd "deploy/verify.sh --170"

采集口径:
    host  — socket.gethostname()
    head  — git rev-parse HEAD (仓库根 = 本脚本所在仓库; 取不到时降级 "-",
            非 git 环境不致中断采集)
    cmd   — 调用方 --cmd 传入; 缺省用 shlex.join(sys.argv)
    ts    — UTC ISO8601 (秒级, 形如 2026-10-25T03:04:05Z)

幂等判定 (2026-10-25 D-2 修, Gitea #112) —— 首行分四类, 绝不叠加双头:
    provenance  严格匹配 PROVENANCE_RE (本工具自产的头) ⇒ 跳过 (幂等);
    approx      '#' 开头且含字段标记 (host=/head=/cmd=/ts=) 的近似头
                ⇒ **替换**该行为新头 (旧头的 host/head/ts 已过期, 换成
                新采集值; 不保留旧行 ⇒ 不出现双头);
    comment     '#' 开头的手写注释 (标题/说明) ⇒ 视为「已有头」跳过,
                不叠加 (原行内容一字不动保留); stderr 提示该文件首行
                非合规 provenance 头 (要加头请把头写成首行, 或用 --print
                组合用法);
    other       非注释首行 (命令输出/正文) ⇒ 照常加头, 正文逐字节保留。
    旧判定 (只看严格正则) 会把 comment/approx 判成「无头」⇒ 再叠加一行
    ⇒ 文件首两行都是 '#' 注释 (双头) —— s5 移交项 D-2, 本版修掉。
退出码: 0 = 全部成功 (含幂等跳过); 1 = 有文件处理失败 (缺失/不可读写)。
"""
from __future__ import annotations

import argparse
import datetime
import re
import shlex
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# provenance 头格式: ts 固定在尾部 (\S+), cmd 贪婪匹配允许含空格/等号,
# 反向锚定保证 " ts=" 取最后一个, cmd 内的 " ts=" 字面量不会截错字段。
PROVENANCE_RE = re.compile(
    r"^# host=(?P<host>\S+) head=(?P<head>\S+) cmd=(?P<cmd>.*) ts=(?P<ts>\S+)$"
)

# 近似头判定 (D-2): '#' 开头且含 provenance 字段标记 —— 旧头/手抄头/
# 字段顺序不同的头, 都算「已有头」⇒ 替换该行而不是叠加第二行。
APPROX_RE = re.compile(r"^#.*\b(?:host|head|cmd|ts)=")


def collect_head(repo_root: Path = REPO_ROOT) -> str:
    """取当前仓库 HEAD sha; 非 git 环境/git 缺失时降级 '-' (不抛异常)。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root, capture_output=True, text=True, check=True,
        ).stdout.strip()
        return out if out else "-"
    except (OSError, subprocess.SubprocessError):
        return "-"


def collect_provenance(cmd: str | None = None,
                       repo_root: Path = REPO_ROOT) -> dict:
    """采集四项 provenance 字段, 返回 dict(host, head, cmd, ts)。"""
    return {
        "host": socket.gethostname(),
        "head": collect_head(repo_root),
        "cmd": cmd if cmd is not None else shlex.join(sys.argv),
        "ts": datetime.datetime.now(datetime.timezone.utc)
              .strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def build_header(info: dict) -> str:
    """按团队约定格式拼一行头 (不含换行)。"""
    return f"# host={info['host']} head={info['head']} cmd={info['cmd']} ts={info['ts']}"


def is_provenance_line(line: str) -> bool:
    """判断一行是否已是合规 provenance 头 (严格判定, 格式与幂等互洽)。"""
    return PROVENANCE_RE.match(line.rstrip("\r\n")) is not None


def classify_first_line(line: str) -> str:
    """给文件首行分类 (D-2 幂等判定的唯一依据)。

    返回四类之一:
      "provenance" — 严格合规头 (本工具自产) ⇒ 跳过, 不动文件;
      "approx"     — '#' 开头且含 provenance 字段标记的近似头 ⇒ 替换该行;
      "comment"    — '#' 开头的手写注释 ⇒ 视为「已有头」跳过, 不叠加;
      "other"      — 非注释首行 (命令输出/正文) ⇒ 照常加头。
    """
    stripped = line.rstrip("\r\n")
    if is_provenance_line(stripped):
        return "provenance"
    if not stripped.startswith("#"):
        return "other"
    if APPROX_RE.match(stripped):
        return "approx"
    return "comment"


def stamp_file(path: Path, header: str, force: bool = False) -> tuple[str, str]:
    """加头并返回 (状态, 首行分类)。

    状态 ∈ {'added', 'skipped', 'replaced'}; 分类 ∈ {'provenance', 'approx',
    'comment', 'other'}。force=True 时无视注释首行, 照常在首行之上加头
    (保留「首行是标题也要能加头」的能力, 由调用方显式选择)。
    """
    content = path.read_text(encoding="utf-8", errors="surrogateescape")
    first_line = content.split("\n", 1)[0] if content else ""
    kind = classify_first_line(first_line)
    if kind == "provenance":
        return "skipped", kind
    if kind == "comment" and not force:
        # 首行是手写注释 ⇒ 视为「已有头」, 原行一字不动, 不叠加。
        return "skipped", kind
    if kind == "approx":
        # 近似头 (旧头/手抄头) ⇒ 替换该行为新采集的头, 不留旧行。
        rest = content.split("\n", 1)[1] if "\n" in content else ""
        path.write_text(header + "\n" + rest,
                        encoding="utf-8", errors="surrogateescape")
        return "replaced", kind
    path.write_text(header + "\n" + content,
                    encoding="utf-8", errors="surrogateescape")
    return "added", kind


def add_header(path: Path, header: str) -> str:
    """给单个文件加头。返回 'added' / 'skipped'(幂等) / 'replaced'(近似头)。

    不可读写时抛异常。D-2 (Gitea #112): 首行已是注释/近似头时**绝不叠加**
    第二行头 —— 旧判定只认严格正则, 手写注释首行会被判「无头」⇒ 双头。
    """
    return stamp_file(path, header)[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("files", nargs="*", type=Path,
                        help="要加 provenance 头的证据文件 (原地修改)")
    parser.add_argument("--cmd", default=None,
                        help="写入头部的完整命令行 (缺省取本脚本 argv)")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="只向 stdout 打印一行头, 不处理文件")
    parser.add_argument("--force", action="store_true",
                        help="首行是手写注释时仍强制在首行之上加头 "
                             "(默认跳过, 避免双头)")
    args = parser.parse_args(argv)

    info = collect_provenance(cmd=args.cmd)
    header = build_header(info)

    if args.print_only:
        print(header)
        return 0

    if not args.files:
        parser.error("未给出证据文件; 若只想生成头请用 --print")

    failed = 0
    for f in args.files:
        try:
            result, kind = stamp_file(f, header, force=args.force)
        except OSError as e:
            print(f"FAILED {f}: {e}", file=sys.stderr)
            failed += 1
            continue
        print(f"{result:7s} {f}")
        if kind == "comment" and not args.force:
            # 提示而非静默: 文件首行不是合规 provenance 头, 本次未加头。
            print(f"NOTE {f}: 首行是手写注释 (非合规 provenance 头), 已跳过; "
                  f"要加头请用 --force, 或用 --print 组合用法", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
