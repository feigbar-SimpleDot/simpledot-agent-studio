#!/usr/bin/env python3
# coding: utf-8
"""issue #82 guard: main entry must stay the single gateway-start bootstrap (static).

Background (docs/ANALYSIS-82.md, s1 verdict — accepted by main):

  * On image v2026.9.24, `deploy/main_entry.sh`'s last line
    `exec hermes gateway run` does NOT spawn a second gateway: the image's
    own `_maybe_redirect_run_to_s6_supervision` (hermes_cli/gateway.py:4428)
    rewrites it into `s6-svc -u gateway-default` + `execvp("sleep","infinity")`.
  * That line is nevertheless the ONLY thing that pulls the s6
    `gateway-default` slot up on a FRESH deploy (no `gateway_state.json`):
    the image reconciler registers the slot with a `down` marker unless the
    desired state says `running`, and the legacy CMD seed does not match
    our CMD (`/opt/mwroot/deploy/main_entry.sh`). Replacing the line with
    `exec sleep infinity` (issue #82's proposed fix) therefore silently
    kills gateway startup on every fresh install — vetoed in ANALYSIS-82 §2.

  So the invariant "the entry's last line can bring the gateway up" is what
  today holds only by luck and had zero test coverage. This file pins it.
  G1 is deliberately OPPOSITE to what issue #82 asked to change: the line
  the issue wanted to edit is exactly the line this test nails down
  (ANALYSIS-82 §5 A-2 note).

What this file pins (issue #82, card t_5f6acc24 / ANALYSIS-82 §5 A-2):

  G1  main_entry.sh's last executable line is exactly `exec hermes gateway
      run` — the sole bootstrap that starts the s6 gateway-default slot.
      Reverse variants that MUST turn red: `exec sleep infinity`,
      `exec tail -f /dev/null`, deleting the last line, commenting it out.
  G2  Outside that last line, main_entry.sh's executable code contains NO
      second statement that starts a gateway (matches `gateway run` /
      `hermes gateway`). A second gateway starter anywhere else = red.
  G3  main.compose.yaml `command:` still points at
      ["/opt/mwroot/deploy/main_entry.sh"] (entry not executing = reaper /
      console / SOUL publish / watchdog all disappear).
  G4  main.compose.yaml does NOT mention HERMES_GATEWAY_NO_SUPERVISE /
      --no-supervise / gateway-default / s6-svc — i.e. we never took over
      the image's s6 orchestration and re-implemented it ourselves.
  G5  main_entry.sh executable code contains no `s6-svc -d` / `s6-svc -t`
      (entry never stops/restarts the s6 slot by hand).

Self-check discipline (same口径 as test_install_bind_mount_skill_soul.py's
test_t8_section_break_makes_t8_red): every *_selfcheck case mutates an
IN-MEMORY copy only (real repo files untouched), first asserts the mutation
actually changed the text (break-point hit — guards against a no-op replace
faking a green), then asserts the SAME assertion function turns red.

Real on-host runtime verification (process counts, fresh-volume boot,
gateway-starts.log behaviour) is the responsibility of the downstream
tester card (evidence per ANALYSIS-82 §4); this file only pins the
entry / compose contract statically.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_ENTRY_SH = REPO_ROOT / "deploy" / "main_entry.sh"
COMPOSE_YAML = REPO_ROOT / "deploy" / "main.compose.yaml"

# entry 末行的唯一合法写法 (G1 断言目标; 与 ANALYSIS-82 §5 A-2 G1 逐字一致)
GATEWAY_START_LINE = "exec hermes gateway run"

# G2 的"会启动 gateway 的语句"匹配口径: `gateway run` / `hermes gateway`
GATEWAY_START_PATTERN = re.compile(r"\bgateway\s+run\b|\bhermes\s+gateway\b")

# G3 的 compose command 合法写法 (行尾允许尾随注释)
COMPOSE_COMMAND_RE = re.compile(
    r'^\s*command:\s*\["/opt/mwroot/deploy/main_entry.sh"\]', re.M)

# G4 红线: compose 里不得出现的 s6 自编排关键字 (ANALYSIS-82 §5 A-2 G4)
COMPOSE_S6_BANNED = (
    "HERMES_GATEWAY_NO_SUPERVISE",
    "--no-supervise",
    "gateway-default",
    "s6-svc",
)


def _read_entry() -> str:
    return MAIN_ENTRY_SH.read_text(encoding="utf-8")


def _read_compose() -> str:
    return COMPOSE_YAML.read_text(encoding="utf-8")


def _code_only(src: str) -> str:
    """去掉整行注释, 保留行内代码 (与 test_install_bind_mount_skill_soul.py 同口径)。"""
    return "\n".join(
        ln for ln in src.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    )


# ── 断言函数 (参数化文本, 便于对内存破坏副本复用同一套判定) ──────────────

def _assert_entry_starts_gateway(entry: str) -> None:
    """G1 判定: entry 的最后一条可执行语句必须逐字是 `exec hermes gateway run`。

    这是全新部署 (无 gateway_state.json) 时拉起 s6 gateway-default 槽的
    唯一兜底 (ANALYSIS-82 §2.1 实测); sleep infinity / tail -f /dev/null /
    删行 / 注释行都会让它消失。
    """
    code_lines = _code_only(entry).splitlines()
    assert code_lines, "main_entry.sh 的可执行代码为空"
    last = code_lines[-1].strip()
    assert last == GATEWAY_START_LINE, (
        "main_entry.sh 末行可执行语句必须是 "
        f"`{GATEWAY_START_LINE}` —— 它是把 s6 gateway-default 槽拉起的唯一兜底 "
        f"(ANALYSIS-82 §2.1: 改成 sleep infinity 会让全新部署静默无 gateway)。"
        f" 实际末行: {last!r}"
    )


def _find_extra_gateway_start(entry: str) -> list[str]:
    """G2 判定辅助: 返回末行之外所有'会启动 gateway'的可执行语句。"""
    code_lines = _code_only(entry).splitlines()
    assert code_lines, "main_entry.sh 的可执行代码为空"
    extra = []
    for ln in code_lines[:-1]:
        if GATEWAY_START_PATTERN.search(ln):
            extra.append(ln.strip())
    return extra


def _assert_single_gateway_start(entry: str) -> None:
    """G2 判定: 除末行那一条外, 全文可执行代码不得有第二条启动 gateway 的语句。"""
    extra = _find_extra_gateway_start(entry)
    assert not extra, (
        "main_entry.sh 的可执行代码在末行之外出现了第二条启动 gateway 的语句 "
        "(两条拉起请求 = issue #82 症状的温床): " + repr(extra)
    )


def _assert_compose_entry_command(compose: str) -> None:
    """G3 判定: compose command 必须仍指向 /opt/mwroot/deploy/main_entry.sh。"""
    assert COMPOSE_COMMAND_RE.search(compose), (
        'main.compose.yaml command must remain '
        '["/opt/mwroot/deploy/main_entry.sh"] — if the entry stops running, '
        "reaper/console/SOUL publish/watchdog all vanish."
    )


def _assert_compose_no_s6_takeover(compose: str) -> None:
    """G4 判定: compose 不得出现镜像 s6 编排被我们接管的关键字。"""
    bad = [tok for tok in COMPOSE_S6_BANNED if tok in compose]
    assert not bad, (
        "main.compose.yaml 出现了 "
        + repr(bad) + " —— 镜像自身的 s6 重定向/reconciler 负责 gateway 编排, "
        "compose 抢编排 = 第二套生命周期实现 (ANALYSIS-82 §3.1 否决理由)。"
    )


def _find_s6_slot_stop(entry: str) -> list[str]:
    """G5 判定辅助: 返回可执行代码里所有 `s6-svc -d` / `s6-svc -t` 语句。"""
    return [
        ln.strip() for ln in _code_only(entry).splitlines()
        if re.search(r"\bs6-svc\s+-[dt]\b", ln)
    ]


# ── 正向用例: 真实仓库文件必须全部通过 ──────────────────────────────────

def test_g1_entry_last_line_starts_gateway() -> None:
    """G1 正向: main_entry.sh 末行可执行语句 = exec hermes gateway run。"""
    _assert_entry_starts_gateway(_read_entry())


def test_g2_no_second_gateway_start_in_entry() -> None:
    """G2 正向: 除末行外, entry 可执行代码不含第二条 gateway 启动语句。"""
    _assert_single_gateway_start(_read_entry())
    # 交叉自保: 末行本身必须命中匹配口径, 否则"零命中"是匹配器失效的假绿
    assert GATEWAY_START_PATTERN.search(_code_only(_read_entry()).splitlines()[-1]), (
        "G2 匹配器对末行本身不命中 —— 判定口径已失效, 必须同步更新。"
    )


def test_g3_compose_command_points_at_entry() -> None:
    """G3 正向: compose command 仍指向 main_entry.sh (经 ./deploy 目录挂载)。"""
    _assert_compose_entry_command(_read_compose())


def test_g4_compose_has_no_s6_takeover_knobs() -> None:
    """G4 正向: compose 不含 HERMES_GATEWAY_NO_SUPERVISE / --no-supervise /
    gateway-default / s6-svc (未把镜像的 s6 编排抢过来自己实现)。"""
    _assert_compose_no_s6_takeover(_read_compose())


def test_g5_entry_does_not_stop_s6_slot() -> None:
    """G5 正向: entry 可执行代码不含 s6-svc -d / s6-svc -t (不手动停/重启槽)。"""
    bad = _find_s6_slot_stop(_read_entry())
    assert not bad, (
        f"main_entry.sh 可执行代码手动停/重启了 s6 槽: {bad!r} —— "
        "ANALYSIS-82 §3.1: 手动停槽会被 entry 自身的重定向再拉起来, 属无效改动。"
    )


# ── 自证用例: 内存副本破坏 → 同一套断言必须变红 (真实文件不动) ──────────

# (变体名, 破坏函数, 该变体应打红的判定)
# issue = (被替换原文, 替换文本); delete_last / comment_last 是结构破坏
_ENTRY_G1_MUTATIONS = {
    "sleep_infinity": lambda s: s.replace(GATEWAY_START_LINE, "exec sleep infinity"),
    "tail_f_devnull": lambda s: s.replace(GATEWAY_START_LINE, "exec tail -f /dev/null"),
    "delete_last_line": lambda s: "\n".join(s.splitlines()[:-1]) + "\n",
    "comment_out_last": lambda s: s.replace(
        GATEWAY_START_LINE, "# " + GATEWAY_START_LINE),
}


@pytest.mark.parametrize("variant", sorted(_ENTRY_G1_MUTATIONS))
def test_g1_selfcheck_break_makes_red(variant: str) -> None:
    """G1 自证: 四种末行破坏 (sleep infinity / tail -f /dev/null / 删行 / 注释)
    都必须让同一套 G1 断言变红。破坏仅在内存副本, 真实文件不动。"""
    original = _read_entry()
    # 正向基线必须先绿, 且破坏点必须真实命中, 否则"变红"是自证假绿
    _assert_entry_starts_gateway(original)
    broken = _ENTRY_G1_MUTATIONS[variant](original)
    assert broken != original, (
        f"自证用例的破坏点没命中 main_entry.sh (变体 {variant}) —— "
        "破坏逻辑需与真实源码同步。"
    )
    with pytest.raises(AssertionError):
        _assert_entry_starts_gateway(broken)


def test_g2_selfcheck_second_gateway_start_makes_red() -> None:
    """G2 自证: 在末行之前插入第二条 gateway 启动语句, 同一套 G2 断言必须变红。"""
    original = _read_entry()
    _assert_single_gateway_start(original)   # 正向基线先绿
    broken = original.replace(
        GATEWAY_START_LINE,
        "hermes gateway run --replace &\n" + GATEWAY_START_LINE,
        1,
    )
    assert broken != original, (
        "自证用例的破坏点没命中 main_entry.sh —— 插入锚已失效, 需与源码同步。"
    )
    with pytest.raises(AssertionError):
        _assert_single_gateway_start(broken)
    # 且 G1 视角必须仍绿 —— 破坏只打在 G2 面上, 两个判定不得互相掩盖
    _assert_entry_starts_gateway(broken)


def test_g3_selfcheck_command_break_makes_red() -> None:
    """G3 自证: compose command 改掉 (entry 不再执行), 同一套 G3 断言必须变红。"""
    original = _read_compose()
    _assert_compose_entry_command(original)  # 正向基线先绿
    broken = original.replace(
        'command: ["/opt/mwroot/deploy/main_entry.sh"]',
        'command: ["sleep", "infinity"]',
        1,
    )
    assert broken != original, (
        "自证用例的破坏点没命中 main.compose.yaml —— command 行写法已变, 需同步。"
    )
    with pytest.raises(AssertionError):
        _assert_compose_entry_command(broken)


@pytest.mark.parametrize("token", COMPOSE_S6_BANNED)
def test_g4_selfcheck_s6_takeover_token_makes_red(token: str) -> None:
    """G4 自证: 往 compose 注入每个 s6 接管关键字, 同一套 G4 断言都必须变红。"""
    original = _read_compose()
    _assert_compose_no_s6_takeover(original)  # 正向基线先绿
    broken = original.replace(
        "services:", f"services:\n  # selfcheck-injected {token}", 1)
    assert broken != original, (
        "自证注入锚 (services:) 没命中 main.compose.yaml —— 结构已变, 需同步。"
    )
    assert token in broken
    with pytest.raises(AssertionError):
        _assert_compose_no_s6_takeover(broken)


def test_g5_selfcheck_s6_slot_stop_makes_red() -> None:
    """G5 自证: 在 entry 末行前插入 `s6-svc -d`, 同一套 G5 判定必须命中变红。"""
    original = _read_entry()
    assert not _find_s6_slot_stop(original)   # 正向基线先绿
    broken = original.replace(
        GATEWAY_START_LINE,
        "s6-svc -d /run/service/gateway-default\n" + GATEWAY_START_LINE,
        1,
    )
    assert broken != original, (
        "自证用例的破坏点没命中 main_entry.sh —— 插入锚已失效, 需与源码同步。"
    )
    hits = _find_s6_slot_stop(broken)
    assert hits, "注入了 s6-svc -d 却没被 G5 检出 —— 判定口径失效"


if __name__ == "__main__":
    # standalone 双跑口径 (pytest 收集不可靠时的兜底, 同既有先例文件)
    test_g1_entry_last_line_starts_gateway()
    test_g2_no_second_gateway_start_in_entry()
    test_g3_compose_command_points_at_entry()
    test_g4_compose_has_no_s6_takeover_knobs()
    test_g5_entry_does_not_stop_s6_slot()
    for v in sorted(_ENTRY_G1_MUTATIONS):
        test_g1_selfcheck_break_makes_red(v)
    test_g2_selfcheck_second_gateway_start_makes_red()
    test_g3_selfcheck_command_break_makes_red()
    for t in COMPOSE_S6_BANNED:
        test_g4_selfcheck_s6_takeover_token_makes_red(t)
    test_g5_selfcheck_s6_slot_stop_makes_red()
    print("ALL PASS")
