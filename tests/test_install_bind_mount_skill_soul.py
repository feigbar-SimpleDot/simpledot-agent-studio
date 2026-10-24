#!/usr/bin/env python3
# coding: utf-8
"""Directory bind-mount skills/SOUL flow regression test (m1-impl macOS compat, 2026-10-18).

Background chain: s4d-fix2 (BUG-53-A) replaced host-side cp with FILE-level
:ro bind mounts pointing straight at the repo source. That only ever worked on
docker: Apple `container`'s bind parser hard-requires the source to be a
directory (`Parser.swift` `guard isDirectory`), and the docker-for-apple-container
shim passes volume specs through without conversion — so every file-level mount
line crashes a macOS deploy. `--force-recreate` is likewise outside the shim's
subcommand set.

m1-impl contract (what this file pins):

  T1  main.compose.yaml bind sources are ALL directories:
        ./deploy/.runtime-skills/<sk> -> /opt/data/skills/agent-matrix/<sk>:ro  (×4)
        ./deploy/.runtime-soul        -> /opt/soul-src:ro
        ./deploy                      -> /opt/mwroot/deploy:ro
      (no file-level mounts of repo source files left).
  T2  compose command points at the entry via the directory mount:
        command: ["/opt/mwroot/deploy/main_entry.sh"]
  T3  install.sh §6 stages the active-LANG sources into the host-owned
      staging dirs (rm -rf + mkdir -p + cp per skill + SOUL), fail-fast on
      missing sources — the compose bind paths are language-neutral.
  T4  The LANG-sentinel sed is fully removed: neither install.sh nor
      main.compose.yaml may mention `SKILL.en.md` / `main-soul.en.md` at all.
  T5  install.sh has NO `--force-recreate` anywhere; recreate semantics are
      carried by the unconditional stop+rm before compose up.
  T6  BSD-portability red lines still hold in executable code: no `sed -i`,
      no `mktemp -t`; mktemp uses the POSIX TMPDIR+XXXXXX form; the EXIT
      trap cleans the temp compose.
  T7  AC-53-04 red line preserved: no docker cp / write docker exec in
      install.sh.
  T8  staging e2e: run install.sh §6's exact staging logic against the real
      repo sources for LANG=en and LANG=zh-cn; assert every staged
      .runtime-skills/<sk>/SKILL.md + .runtime-soul/SOUL.md is byte-identical
      to its active-LANG source (the semantic equivalent of the old
      sed-substitution contract: content, not path, carries the language).
  T9  main_entry.sh publishes the SOUL via idempotent `ln -sfn
      /opt/soul-src/SOUL.md /opt/data/SOUL.md` (guards against mounting the
      staging dir directly over /opt/data, which would shadow kanban.db).
  T10 absolute-path compose sed pipeline preserves the directory mounts.
  T10b unwritable-TMPDIR counter-proof: the pre-fix bare idiom really fails
      there (red side, anti false-green) while the hermetic snippet succeeds
      in the very same environment (green side); the subprocess env pins
      `LC_ALL=C` so the `Permission denied` stderr text is locale-independent.
  T11 entry SOUL publish guard clears a pre-existing DIRECTORY at the
      canonical path (rm -rf + ln -sfn), so the link never lands inside it.

Real on-host macOS verification (Apple container actually accepting the
directory topology) is the responsibility of the downstream m3-test card;
this file only pins the deploy-script / compose contract statically + the
staging transform in a subprocess.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"
COMPOSE_YAML = REPO_ROOT / "deploy" / "main.compose.yaml"
MAIN_ENTRY_SH = REPO_ROOT / "deploy" / "main_entry.sh"

SKILLS = ["mw-task-dispatch", "team-work-chain-advance",
          "team-work-chain-orchestration", "worker-message-relay"]


def _read_install() -> str:
    return INSTALL_SH.read_text()


def _read_compose() -> str:
    return COMPOSE_YAML.read_text()


def _code_only(src: str) -> str:
    """Drop whole-line comments; keep inline code (same口径 as the old file)."""
    return "\n".join(
        ln for ln in src.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    )


# ── T1: compose bind sources are all directories ──────────────────────────

def test_t1_compose_declares_directory_bind_mounts() -> None:
    """main.compose.yaml must declare the 6 m1-impl DIRECTORY bind mounts.

    Apple container requires directory sources; the 4 skill staging dirs and
    the soul staging dir each mount :ro onto the same in-container paths the
    verify.sh fingerprint / agent already expect. ./deploy mounts read-only so
    the entry + lib/i18n.sh arrive without a single-file bind.
    """
    src = _read_compose()
    for sk in SKILLS:
        line = (f"./deploy/.runtime-skills/{sk}:"
                f"/opt/data/skills/agent-matrix/{sk}:ro")
        assert line in src, (
            f"main.compose.yaml missing skill directory mount: expected `{line}`."
        )
    assert "./deploy/.runtime-soul:/opt/soul-src:ro" in src, (
        "main.compose.yaml missing the SOUL directory mount "
        "(expected ./deploy/.runtime-soul:/opt/soul-src:ro)."
    )
    assert "./deploy:/opt/mwroot/deploy:ro" in src, (
        "main.compose.yaml missing the ./deploy directory mount "
        "(entry + i18n lib must arrive via a directory bind, never a file bind)."
    )
    # No file-level :ro mounts of repo source files may survive — that is the
    # exact Apple-container failure mode m1-impl removes.
    assert not re.search(r"\./(?:docs|deploy/skills)/\S+:\S+:ro", src), (
        "main.compose.yaml still carries a file-level :ro bind of a repo source "
        "file (./docs/... or ./deploy/skills/...) — Apple container rejects "
        "non-directory bind sources."
    )
    assert "docker cp" not in src
    assert "docker exec" not in src


# ── T2: entry command resolves through the directory mount ────────────────

def test_t2_entry_command_via_directory_mount() -> None:
    """compose command must be /opt/mwroot/deploy/main_entry.sh.

    The old /opt/mwroot/entry.sh came from a FILE bind (./deploy/main_entry.sh
    -> /opt/mwroot/entry.sh:ro), which Apple container rejects. The directory
    mount ./deploy:/opt/mwroot/deploy:ro delivers the same script at its
    in-repo-relative path.
    """
    src = _read_compose()
    assert 'command: ["/opt/mwroot/deploy/main_entry.sh"]' in src, (
        'main.compose.yaml command must be ["/opt/mwroot/deploy/main_entry.sh"] '
        "(path under the ./deploy directory mount)."
    )
    assert "/opt/mwroot/entry.sh" not in src, (
        "stale single-file entry mount path /opt/mwroot/entry.sh still present."
    )


# ── T3: install.sh §6 staging generation + pre-flight ─────────────────────

def test_t3_staging_generation_and_preflight() -> None:
    """install.sh must regenerate deploy/.runtime-{skills,soul}/ per install.

    Contract: rm -rf both staging dirs (full rebuild, never stale), per-skill
    fail-fast on the missing source then mkdir -p + cp into
    .runtime-skills/<sk>/SKILL.md, same for SOUL into .runtime-soul/SOUL.md.
    """
    code = _code_only(_read_install())
    assert 'RUNTIME_SKILLS="deploy/.runtime-skills"' in code
    assert 'RUNTIME_SOUL="deploy/.runtime-soul"' in code
    assert re.search(r"rm\s+-rf\s+\"\$\{RUNTIME_SKILLS\}\"\s+\"\$\{RUNTIME_SOUL\}\"", code), (
        "install.sh must rm -rf both staging dirs before regenerating "
        "(full rebuild = never serve a stale previous-LANG file)."
    )
    # per-skill pre-flight + staging
    assert 'SKILL_SRC="deploy/skills/${sk}/SKILL.${SAS_LANG}.md"' in code, (
        "install.sh §6 must resolve the active-LANG skill source "
        "deploy/skills/${sk}/SKILL.${SAS_LANG}.md (LANG-aware fail-fast kept)."
    )
    assert re.search(r"mkdir\s+-p\s+\"\$\{RUNTIME_SKILLS\}/\$\{sk\}\"", code), (
        "install.sh §6 must mkdir -p each per-skill staging dir."
    )
    assert re.search(
        r"cp\s+\"\$\{SKILL_SRC\}\"\s+\"\$\{RUNTIME_SKILLS\}/\$\{sk\}/SKILL\.md\"", code), (
        "install.sh §6 must cp the active-LANG SKILL source into the staging dir."
    )
    # SOUL pre-flight + staging
    assert 'SOUL_SRC="docs/main-soul.${SAS_LANG}.md"' in code, (
        "install.sh §6 must resolve docs/main-soul.${SAS_LANG}.md."
    )
    assert re.search(
        r"cp\s+\"\$\{SOUL_SRC\}\"\s+\"\$\{RUNTIME_SOUL\}/SOUL\.md\"", code), (
        "install.sh §6 must cp the active-LANG SOUL source into deploy/.runtime-soul/."
    )


# ── T4: LANG sentinel sed fully removed ───────────────────────────────────

def test_t4_lang_sentinel_sed_removed() -> None:
    """The compose-side .en.md sentinel is dead: neither install.sh nor
    main.compose.yaml may mention `SKILL.en.md` / `main-soul.en.md` at all.

    m1-impl stages language-neutral bind paths; the sentinel pattern's very
    presence would mean someone re-introduced the rewrite step (a macOS sed
    portability surface) or a file-level mount. grep-zero over both files is
    the acceptance口径.
    """
    for path in (INSTALL_SH, COMPOSE_YAML):
        src = path.read_text()
        assert "SKILL.en.md" not in src, (
            f"{path.name} still references the SKILL.en.md sentinel — "
            "language now travels via the staging dirs, not a sed rewrite."
        )
        assert "main-soul.en.md" not in src, (
            f"{path.name} still references the main-soul.en.md sentinel."
        )


# ── T5: --force-recreate gone; recreate via stop+rm ───────────────────────

def test_t5_no_force_recreate_stop_rm_is_recreate() -> None:
    """install.sh must not pass --force-recreate (outside the Apple docker
    shim's subcommand set) and must stop+rm any existing container before
    compose up (that IS the recreate semantic on both engines).
    """
    src = _read_install()
    assert "--force-recreate" not in src, (
        "install.sh still passes/mentions --force-recreate — the Apple "
        "container docker shim does not support it; recreate is stop+rm."
    )
    code = _code_only(src)
    assert re.search(r"docker stop \"\$\{MAIN_CONTAINER_NAME\}\"", code), (
        "install.sh must docker stop the existing container before up."
    )
    assert re.search(r"docker rm \"\$\{MAIN_CONTAINER_NAME\}\"", code), (
        "install.sh must docker rm the existing container before up."
    )
    # stop/rm must sit BEFORE the compose up line (else up would keep the
    # same-project container running and the new mount topology never applies).
    assert code.index("docker rm \"${MAIN_CONTAINER_NAME}\"") < code.index(
        "docker compose --project-name"
    ), "stop+rm must precede compose up — up alone never applies a new mount topology."


# ── T6: BSD-portability red lines ─────────────────────────────────────────

def test_t6_bsd_red_lines_zero() -> None:
    """install.sh executable code: no `sed -i`, no `mktemp -t`; mktemp keeps
    the POSIX TMPDIR+XXXXXX form; the EXIT trap cleans the temp compose.

    (r3b-fixbsd heritage. The sentinel sed itself is gone in m1-impl, but the
    absolute-path rewrite is still a sed pipeline and future edits must not
    regress to GNU-only forms.)
    """
    code = _code_only(_read_install())
    sed_i_hits = re.findall(r"\bsed\b[^\n]*\s-i(?:\s|\")", code)
    assert not sed_i_hits, (
        f"install.sh executable code still uses `sed -i` (macOS BSD sed "
        f"breaks). Hits: {sed_i_hits}"
    )
    mktemp_t_hits = re.findall(r"\bmktemp\b[^\n]*\s-t\b", code)
    assert not mktemp_t_hits, (
        f"install.sh executable code still uses `mktemp -t` (BSD ignores the "
        f"template suffix). Hits: {mktemp_t_hits}"
    )
    assert re.search(r"mktemp\s+\"\$\{TMPDIR:-/tmp\}/[^\\\"]*XXXXXX\"", code), (
        "install.sh must keep the POSIX mktemp TMPDIR+XXXXXX form."
    )
    assert re.search(r"trap\s+'rm\s+-f\s+\"\$\{ABS_COMPOSE\}\"'\s+EXIT", _read_install()), (
        "install.sh EXIT trap must clean ${ABS_COMPOSE}."
    )


# ── T7: AC-53-04 red lines preserved ──────────────────────────────────────

# 写操作标记 (沿用既有判定口径: chown / chmod / rm -rf / tee / 重定向)
WRITE_OP_MARKERS = ("chown", "chmod", "rm -rf", "tee", "> /", ">> /")

# 只读探测白名单 —— issue #59 N-b: 逐条显式列举, 不使用能匹配一整类命令的
# 前缀通配。旧的 'exec "${MAIN_CONTAINER_NAME}"' 整行豁免已删除 (它把整类
# 主容器 exec 行的写操作检测全部绕过)。
# 形如 docker exec / docker compose exec / docker-compose exec, 后接若干
# 选项, 再接容器名, 再接一个只读子命令 —— 每条只放行一个具体子命令。
# (${DOCKER_CMD} 是历史上的 CLI 抽象变量, 当前 install.sh 已无它, 但保留
#  可选前缀, 防止将来它被重新引入时成为绕过口。)
_CLI = r'(?:docker|docker-compose|\$\{DOCKER_CMD\})'
# 容器名变量 (install.sh 自有变量, 带引号/不带引号两种写法都认)
_CNAME = r'(?:"\$\{MAIN_CONTAINER_NAME\}"|\$\{MAIN_CONTAINER_NAME\})'
_READONLY_EXEC_PREFIX = (
    _CLI + r'\s+(?:compose\s+)?exec\s+'
    r'(?:-{1,2}[^\s]+\s+)*' + _CNAME + r'\s+'
)
READONLY_EXEC_ALLOWLIST = (
    ("cat", re.compile(_READONLY_EXEC_PREFIX + r"cat(?:\s|$)")),
    ("ls", re.compile(_READONLY_EXEC_PREFIX + r"ls(?:\s|$)")),
    ("test", re.compile(_READONLY_EXEC_PREFIX + r"test(?:\s|$)")),
    ("stat", re.compile(_READONLY_EXEC_PREFIX + r"stat(?:\s|$)")),
    ("sha256sum", re.compile(_READONLY_EXEC_PREFIX + r"sha256sum(?:\s|$)")),
    ("md5sum", re.compile(_READONLY_EXEC_PREFIX + r"md5sum(?:\s|$)")),
)

# shell 控制字符 —— 出现在 docker exec 行里就不能按"只读探测"放行
SHELL_CONTROL_CHARS = (";", "&", "|", ">", "<", "`", "$(")

# 任意 exec 形态 (docker exec / docker compose exec / docker-compose exec)
_EXEC_FORM = re.compile(_CLI + r'\s+(?:compose\s+)?exec\b')

# docker cp 的任意 CLI 前缀形态
_CP_FORM = re.compile(_CLI + r'\s+cp\b')


def _strip_shell_comment(line: str) -> str:
    """去掉行尾 shell 注释 (引号内的 # 不算注释), 供红线判定使用。"""
    out: list[str] = []
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (i == 0 or line[i - 1].isspace()):
            break
        out.append(ch)
    return "".join(out)


def _logical_lines(src: str) -> list[str]:
    """把反斜杠续行拼成逻辑行 (shell 的一条命令可跨多行)。

    不拼的话, `docker exec ... cat \\` + 下一行 `> /f` 这种把写操作藏到
    续行里的形态会绕过逐行判定 —— 续行首行命中"只读探测"白名单后, 真正
    的重定向落在不含 docker 词的下一行上, 逐行扫描看不见。
    """
    out: list[str] = []
    buf = ""
    for raw in src.splitlines():
        stripped = raw.rstrip()
        buf = f"{buf} {stripped}".strip() if buf else stripped
        if buf.endswith("\\"):
            buf = buf[:-1].rstrip()
            continue
        out.append(buf)
        buf = ""
    if buf:
        out.append(buf)
    return out


def _find_red_lines(src: str) -> list[tuple[str, str]]:
    """扫 install.sh 源码找 AC-53-04 红线命中, 返回 [(类别, 行内容)]。

    判定口径 (issue #59 N-b):
      1. 含 docker cp 的行 = 红线;
      2. exec 行先判写操作标记 (chown/chmod/rm -rf/tee/重定向),
         命中即红线 —— 判定先于白名单, 防止"白名单命令词 + 追加写操作"绕过;
      3. 含 shell 控制字符 (; & | > < ` $() 的 exec 行 = 红线;
      4. 只放行 READONLY_EXEC_ALLOWLIST 里逐条列举的只读探测;
      5. 其余 exec 行 (未列举) = 红线 —— 白名单是"逐条许可", 不是默认放行。
    """
    red: list[tuple[str, str]] = []
    for raw in _logical_lines(src):
        code = _strip_shell_comment(raw).strip()
        if not code or not re.search(r"(?:docker|docker-compose|DOCKER_CMD)", code):
            continue
        if _CP_FORM.search(code):
            red.append(("docker cp", code))
            continue
        if not _EXEC_FORM.search(code):
            continue
        marker = next((w for w in WRITE_OP_MARKERS if w in code), None)
        if marker is not None:
            red.append((f"docker exec write ({marker})", code))
            continue
        if any(ch in code for ch in SHELL_CONTROL_CHARS):
            red.append(("docker exec shell metachar", code))
            continue
        if not any(rx.search(code) for _, rx in READONLY_EXEC_ALLOWLIST):
            red.append(("docker exec not in read-only allowlist", code))
    return red


def test_t7_ac_53_04_red_line_preserved() -> None:
    """install.sh must NOT contain docker cp / write docker exec.

    Staging writes only into host-owned deploy/.runtime-* dirs — the
    BUG-53-A ownership trap stays eradicated (container never writes there,
    mounts are :ro).

    issue #59 N-b: 白名单已从"整行豁免主容器 exec"收窄到逐条列举的只读探测
    子命令; 未列举的 exec 行一律判红线。
    """
    bad = _find_red_lines(_read_install())
    assert not bad, (
        f"AC-53-04 red line violated: {bad}. The directory-mount approach "
        "should reduce Docker calls, not add write paths."
    )


@pytest.mark.parametrize("fragment", [
    # 写操作 docker exec —— 每种写法都必须被判红线
    "docker exec \"${MAIN_CONTAINER_NAME}\" sh -c 'echo x > /f'",
    "docker exec \"${MAIN_CONTAINER_NAME}\" sh -c 'echo x >/f'",
    "docker exec -u root \"${MAIN_CONTAINER_NAME}\" sh -c 'echo x > /f'",
    "docker exec \"${MAIN_CONTAINER_NAME}\" chown 10000:10000 /opt/data/SOUL.md",
    "docker exec \"${MAIN_CONTAINER_NAME}\" chmod 644 /opt/data/SOUL.md",
    "docker exec \"${MAIN_CONTAINER_NAME}\" rm -rf /opt/data/skills",
    "docker exec \"${MAIN_CONTAINER_NAME}\" tee /opt/data/SOUL.md",
    "docker cp /tmp/x /opt/data/SOUL.md",
    # 等价后门形态 (换一种写法不得绕过检测)
    "docker exec \"${MAIN_CONTAINER_NAME}\" bash -c \"echo x > /f\"",
    "docker exec \"${MAIN_CONTAINER_NAME}\" sh -c 'cat /f > /g'",
    "docker exec \"${MAIN_CONTAINER_NAME}\" cat /f > /g",
    "docker exec \"${MAIN_CONTAINER_NAME}\" mv /a /b",
    "docker exec \"${MAIN_CONTAINER_NAME}\" sed -i s/a/b/ /f",
    "docker exec \"${MAIN_CONTAINER_NAME}\" python3 -c \"open('/f','w').write('x')\"",
    "docker compose exec \"${MAIN_CONTAINER_NAME}\" sh -c 'echo x > /f'",
    "docker-compose exec \"${MAIN_CONTAINER_NAME}\" sh -c 'echo x > /f'",
    # 续行藏写操作 (只读词在首行, 重定向在续行)
    "docker exec \"${MAIN_CONTAINER_NAME}\" cat \\\n  > /f",
    "docker exec \"${MAIN_CONTAINER_NAME}\" sh -c \\\n  'echo x > /f'",
    # 历史 CLI 抽象变量形态 (当前 install.sh 已无 DOCKER_CMD, 防它回来当后门)
    "${DOCKER_CMD} exec \"${MAIN_CONTAINER_NAME}\" sh -c 'echo x > /f'",
    # 不带引号的容器名写法
    "docker exec ${MAIN_CONTAINER_NAME} sh -c 'echo x > /f'",
])
def test_t7_red_line_detects_write_exec(fragment: str) -> None:
    """反向用例 (issue #59 N-b): 写操作 docker exec 必须被判红线命中。

    合成片段, 不改真实 install.sh。
    """
    assert _find_red_lines(fragment), (
        f"写操作 docker exec 未被检出 —— 收窄后的白名单仍有后门: {fragment}"
    )


@pytest.mark.parametrize("fragment", [
    # 只读探测 docker exec —— 必须放行, 不得误报
    "docker exec \"${MAIN_CONTAINER_NAME}\" cat /mw_data/settings.json",
    "docker exec \"${MAIN_CONTAINER_NAME}\" ls /opt/data",
    "docker exec \"${MAIN_CONTAINER_NAME}\" test -f /opt/data/SOUL.md",
    "docker exec \"${MAIN_CONTAINER_NAME}\" sha256sum /opt/data/SOUL.md",
])
def test_t7_red_line_allows_readonly_exec(fragment: str) -> None:
    """反向用例 (issue #59 N-b): 只读探测 docker exec 不得误报。"""
    assert not _find_red_lines(fragment), (
        f"只读 docker exec 探测被误报: {_find_red_lines(fragment)}"
    )


# ── T8: staging e2e — content carries the language ────────────────────────

# install.sh §6 段的稳定标记 (issue #59 N-a: T8 不再手抄 §6 的 staging 逻辑,
# 改为运行时从真实源文件切段执行 —— 改 §6 忘改测试就会真的变红, 不再假绿漂移)
INSTALL_S6_START_MARKER = "# ── 6. Skills + SOUL staging"
INSTALL_S6_END_MARKER = "# ── 6.5 --preserve-soul"


def _extract_install_section(src: str, start_marker: str, end_marker: str) -> str:
    """按稳定标记切出 install.sh 的一段 (含起始标记行, 不含结束标记行)。

    标记找不到即 AssertionError —— 绝不静默回退到任何手抄副本。
    """
    start = src.find(start_marker)
    assert start != -1, (
        f"install.sh 找不到起始标记 {start_marker!r} —— §6 结构变了, "
        "T8 的抽取锚点需同步更新。"
    )
    end = src.find(end_marker, start + len(start_marker))
    assert end != -1, (
        f"install.sh 找不到结束标记 {end_marker!r} —— §6 结构变了, "
        "T8 的抽取锚点需同步更新。"
    )
    return src[start:end]


def _run_install_section(section: str, workdir: Path, lang: str) -> subprocess.CompletedProcess:
    """在临时仓库副本里执行 install.sh 抽出的 §6 段 (原样执行, 只 stub 外部依赖)。

    stub: fail (打印到 stderr 并 exit 1) / say (透传打印); SAS_LANG 由用例设定
    (install.sh 生产里它来自 .env 的 LANG)。
    """
    script = "\n".join([
        "set -euo pipefail",
        f"cd {shlex.quote(str(workdir))}",
        f"SAS_LANG={shlex.quote(lang)}",
        # fail: 打印到 stderr 并 exit 1 (与 install.sh 的 fail 同语义)
        "fail() { printf '[install] %s\\n' \"$*\" >&2; exit 1; }",
        # say: install.sh 的 say 是双参 (en, zh-cn), stub 只做透传打印
        "say() { printf '[install] %s\\n' \"$*\"; }",
        section,
    ])
    return subprocess.run(["bash", "-c", script], cwd=str(workdir),
                          capture_output=True, text=True)


def _stage_repo_copy(d: Path, lang: str) -> None:
    """搭出 install.sh §6 触及的最小仓库形态 (deploy/skills/<sk> + docs/main-soul.<lang>.md)。"""
    (d / "deploy" / "skills").mkdir(parents=True)
    (d / "docs").mkdir(parents=True)
    for sk in SKILLS:
        shutil.copytree(REPO_ROOT / "deploy" / "skills" / sk,
                        d / "deploy" / "skills" / sk)
    shutil.copy(REPO_ROOT / "docs" / f"main-soul.{lang}.md",
                d / "docs" / f"main-soul.{lang}.md")


def _assert_staged_matches_sources(d: Path, lang: str) -> None:
    """T8 断言口径: 暂存产物与源文件 byte-identical + 挂载源是目录。

    抽成独立函数, 好让"弄坏 install.sh 副本 → 同一断言必须变红"的
    自证用例复用同一套判定 (issue #59 N-a)。
    """
    for sk in SKILLS:
        staged_path = d / "deploy" / ".runtime-skills" / sk / "SKILL.md"
        assert staged_path.is_file(), (
            f"LANG={lang}: staged SKILL.md for {sk} missing "
            f"({staged_path}) — install.sh §6 没有生成该暂存产物。"
        )
        staged = staged_path.read_bytes()
        source = (REPO_ROOT / "deploy" / "skills" / sk / f"SKILL.{lang}.md").read_bytes()
        assert staged == source, (
            f"LANG={lang}: staged SKILL.md for {sk} differs from source "
            "(byte compare)."
        )
    staged_soul_path = d / "deploy" / ".runtime-soul" / "SOUL.md"
    assert staged_soul_path.is_file(), (
        f"LANG={lang}: staged SOUL.md missing ({staged_soul_path}) — "
        "install.sh §6 没有生成该暂存产物。"
    )
    staged_soul = staged_soul_path.read_bytes()
    assert staged_soul == (REPO_ROOT / "docs" / f"main-soul.{lang}.md").read_bytes(), (
        f"LANG={lang}: staged SOUL.md differs from docs/main-soul.{lang}.md."
    )
    # mount source dirs exist (Apple container's isDirectory guard)
    for sk in SKILLS:
        assert (d / "deploy" / ".runtime-skills" / sk).is_dir()
    assert (d / "deploy" / ".runtime-soul").is_dir()


def test_t8_staging_e2e_lang_content() -> None:
    """Run install.sh §6's staging logic for LANG=en and zh-cn in a scratch copy
    of the repo; assert every staged file is byte-identical to its active-LANG
    source, and that the mount sources are directories (Apple container's
    isDirectory guard).

    issue #59 N-a: §6 逻辑从真实 install.sh 抽取执行, 不再是测试里的手抄副本。
    """
    section = _extract_install_section(
        _read_install(), INSTALL_S6_START_MARKER, INSTALL_S6_END_MARKER)
    # §6 覆盖的技能集不得与本用例断言的技能集脱节
    for sk in SKILLS:
        assert sk in section, (
            f"install.sh §6 段里找不到技能 {sk} —— 技能集变了, T8 的断言口径需同步。"
        )
    for lang in ("en", "zh-cn"):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            _stage_repo_copy(d, lang)
            r = _run_install_section(section, d, lang)
            assert r.returncode == 0, (
                f"LANG={lang}: staging pipeline failed rc={r.returncode}\n"
                f"stderr: {r.stderr}"
            )
            _assert_staged_matches_sources(d, lang)


def test_t8_section_break_makes_t8_red() -> None:
    """自证 (issue #59 N-a): T8 真的在读源文件, 不是又一份手抄。

    把 install.sh §6 弄坏 (临时副本, 真实仓库不动) —— 暂存产物随之缺失,
    T8 的同一套断言必须变红。若 T8 仍在跑手抄副本, 本用例会假绿。
    """
    original = _read_install()
    broken = original.replace(
        'cp "${SOUL_SRC}" "${RUNTIME_SOUL}/SOUL.md"',
        'cp "${SOUL_SRC}" "${RUNTIME_SOUL}/SOUL.md.broken"',
    )
    assert broken != original, (
        "自证用例的破坏点没命中 install.sh (§6 的 cp 行已变) —— "
        "破坏点需与真实源码同步。"
    )
    section = _extract_install_section(
        broken, INSTALL_S6_START_MARKER, INSTALL_S6_END_MARKER)
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _stage_repo_copy(d, "en")
        r = _run_install_section(section, d, "en")
        # 段本身仍跑通 (退出码 0), 但暂存产物缺失 → 同一套断言必须判红
        assert r.returncode == 0, f"破坏后的 §6 段应仍退出 0, 实际 rc={r.returncode}"
        with pytest.raises(AssertionError):
            _assert_staged_matches_sources(d, "en")


# ── T9: entry publishes SOUL via idempotent ln -sfn ───────────────────────

def test_t9_entry_publishes_soul_symlink() -> None:
    """main_entry.sh must symlink /opt/soul-src/SOUL.md to the canonical
    /opt/data/SOUL.md with ln -sfn (idempotent), and must NOT mount the
    staging dir directly over /opt/data (would shadow kanban.db — guarded by
    the compose check in T1; here we pin the entry side).
    """
    entry = MAIN_ENTRY_SH.read_text()
    assert re.search(r"ln\s+-sfn\s+/opt/soul-src/SOUL\.md\s+/opt/data/SOUL\.md", entry), (
        "main_entry.sh must publish the SOUL with "
        "`ln -sfn /opt/soul-src/SOUL.md /opt/data/SOUL.md`."
    )
    # i18n guard G11 compatibility: entry still loads the lib from the deploy dir mount
    assert "/opt/mwroot/deploy/lib/i18n.sh" in entry, (
        "main_entry.sh must source i18n helpers from /opt/mwroot/deploy/lib/ "
        "(the ./deploy directory mount — same path m1-impl keeps)."
    )


# ── T11: entry guard clears a pre-existing DIRECTORY at the canonical path ──

# main_entry.sh 的 SOUL 发布守卫段 —— 按稳定标记切出来在临时目录里跑
# (真机 /opt/data 绝不碰)。守卫段只依赖 sh 内建, 无外部依赖。
ENTRY_GUARD_START_MARKER = "# ── m1-impl: publish the read-only SOUL source at the canonical path ──"
ENTRY_GUARD_END_MARKER = "# ── Critical fix 2026-10-04 ────────────────────────────────"


def _extract_entry_guard(entry: str) -> str:
    """切出 main_entry.sh 的 SOUL 发布守卫段 (含起始标记, 不含结束标记)。"""
    start = entry.find(ENTRY_GUARD_START_MARKER)
    assert start != -1, (
        f"main_entry.sh 找不到守卫段起始标记 {ENTRY_GUARD_START_MARKER!r} —— "
        "守卫段结构变了, T11 的抽取锚点需同步更新。"
    )
    end = entry.find(ENTRY_GUARD_END_MARKER, start + len(ENTRY_GUARD_START_MARKER))
    assert end != -1, (
        f"main_entry.sh 找不到守卫段结束标记 {ENTRY_GUARD_END_MARKER!r} —— "
        "守卫段结构变了, T11 的抽取锚点需同步更新。"
    )
    return entry[start:end]


def _run_entry_guard(guard: str, soul_src: Path, soul_dst: Path) -> subprocess.CompletedProcess:
    """在临时目录里跑守卫段 (路径参数化到临时目录, 不碰真机 /opt/*)。"""
    script = "\n".join([
        "set -u",
        f"SOUL_SRC={shlex.quote(str(soul_src))}",
        f"SOUL_DST={shlex.quote(str(soul_dst))}",
        # 守卫段里的绝对路径 /opt/soul-src/SOUL.md 与 /opt/data/SOUL.md
        # 参数化到临时目录 (真机路径绝不碰)。
        guard.replace("/opt/soul-src/SOUL.md", '"${SOUL_SRC}"')
             .replace("/opt/data/SOUL.md", '"${SOUL_DST}"'),
    ])
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True)


def test_t11_entry_guard_clears_preexisting_directory() -> None:
    """issue #59 N-c: 目标既存为目录时, 守卫必须清掉它再建链接。

    旧守卫只判 -f + rm -f: rm -f 删不掉目录, 随后的 ln -sfn 会把链接建进
    目录内部 (/opt/data/SOUL.md/SOUL.md) —— 静默错位。

    断言: 链接建成了 <dst> 这个符号链接本身, 而不是建进目录内部
    (目录内没有多出 SOUL.md 文件)。在临时目录里跑, 不碰真机 /opt/data。
    """
    guard = _extract_entry_guard(MAIN_ENTRY_SH.read_text())
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        soul_src = d / "soul-src"
        soul_src.mkdir()
        (soul_src / "SOUL.md").write_text("SOUL-BODY\n", encoding="utf-8")
        soul_dst = d / "data" / "SOUL.md"
        soul_dst.parent.mkdir()
        soul_dst.mkdir()          # 目标既存为目录 (触发面)
        # 目录内预置一个无关文件 —— 守卫清目录时应一并清掉
        (soul_dst / "stale.txt").write_text("stale\n", encoding="utf-8")

        r = _run_entry_guard(guard, soul_src / "SOUL.md", soul_dst)
        assert r.returncode == 0, (
            f"守卫段执行失败 rc={r.returncode}\nstderr: {r.stderr}"
        )
        assert soul_dst.is_symlink(), (
            f"{soul_dst} 不是符号链接 —— 守卫没把既存目录换成链接"
        )
        assert os.readlink(soul_dst) == str(soul_src / "SOUL.md"), (
            f"{soul_dst} 指向了 {os.readlink(soul_dst)}, 期望 {soul_src / 'SOUL.md'}"
        )
        # 链接建成了 dst 本身 => 目录内部不可能再有同名 SOUL.md
        assert not (soul_dst / "SOUL.md").exists(), (
            "链接被建进了既存目录内部 (/opt/data/SOUL.md/SOUL.md 形态) —— "
            "N-c 未闭环"
        )
        # 目录形态的旧目标已被清掉 (stale.txt 随目录一起没了)
        assert not (soul_dst / "stale.txt").exists(), (
            "既存目录里的内容没被清掉 —— 守卫没 rm -rf 掉目录"
        )


def test_t11_entry_guard_keeps_regular_file_behavior() -> None:
    """生产默认行为不变: 目标不存在 => 直接建链接; 目标既存为普通文件 => 换掉它。

    这两条是 N-c 之前就有的行为, 收窄改动不得把它们改坏。
    """
    guard = _extract_entry_guard(MAIN_ENTRY_SH.read_text())
    # 情形 1: 目标不存在
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        soul_src = d / "soul-src"
        soul_src.mkdir()
        (soul_src / "SOUL.md").write_text("SOUL-BODY\n", encoding="utf-8")
        soul_dst = d / "data" / "SOUL.md"
        soul_dst.parent.mkdir()
        r = _run_entry_guard(guard, soul_src / "SOUL.md", soul_dst)
        assert r.returncode == 0, f"rc={r.returncode} stderr={r.stderr}"
        assert soul_dst.is_symlink(), "目标不存在时应直接建链接"
        assert soul_dst.read_text(encoding="utf-8") == "SOUL-BODY\n"

    # 情形 2: 目标既存为普通文件 (旧 bind-mounted file 形态)
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        soul_src = d / "soul-src"
        soul_src.mkdir()
        (soul_src / "SOUL.md").write_text("SOUL-BODY\n", encoding="utf-8")
        soul_dst = d / "data" / "SOUL.md"
        soul_dst.parent.mkdir()
        soul_dst.write_text("OLD-REGULAR\n", encoding="utf-8")
        r = _run_entry_guard(guard, soul_src / "SOUL.md", soul_dst)
        assert r.returncode == 0, f"rc={r.returncode} stderr={r.stderr}"
        assert soul_dst.is_symlink(), "既存普通文件应被换成链接"
        assert soul_dst.read_text(encoding="utf-8") == "SOUL-BODY\n"


# ── issue #76 helpers: install.sh §8 abs-compose snippet (verbatim anchor) ──

# mktemp 行字面 (C-1 防漂移锚): 必须与 deploy/install.sh:274 逐字一致。
# 源码即渲染文本 (非 f-string), 任何改写都会同时破坏 AC-04 的同型比对。
_ABS_COMPOSE_MKTEMP_LINE = 'ABS_COMPOSE="$(mktemp "${TMPDIR:-/tmp}/main.compose.abs.XXXXXX")"'


def _abs_compose_snippet(d: Path, hermetic: bool) -> str:
    """渲染 install.sh §8 绝对路径改写片段的 bash 脚本。

    hermetic=True (裁-2): mktemp 行之前先 `TMPDIR='<用例目录>'`, 片段不再取用
    外部 TMPDIR; mktemp 行本身与 install.sh:274 逐字同型, 不改一字。
    hermetic=False: 修复前裸 idiom, 仅供反证用例红侧断言在内存里复现原红
    (外部 TMPDIR 直接取用 -> 不可写环境 mktemp Permission denied)。
    """
    tmpdir_line = f"TMPDIR='{d}'\n" if hermetic else ""
    sed_line = (
        f'sed -E "s|\\\\./([a-zA-Z0-9_./-]+)|{d}/\\\\1|g" '
        'deploy/main.compose.yaml > "${ABS_COMPOSE}"'
    )
    return (
        "set -euo pipefail\n"
        f"cd '{d}'\n"
        f"{tmpdir_line}"
        f"{_ABS_COMPOSE_MKTEMP_LINE}\n"
        f"{sed_line}\n"
        'echo "${ABS_COMPOSE}"\n'
    )


def _compose_volume_lines(content: str) -> list[str]:
    """提取 compose 文件 volumes 段内的 `- ` 挂载行 (行级 strip)。

    t10 主体与反证用例的绿侧共用本解析逻辑 —— 绿侧不能比它所验证的对象更宽松,
    否则"验证"失去意义 (issue #76 审查建议项 S-3)。
    """
    volume_lines: list[str] = []
    in_volumes = False
    for ln in content.splitlines():
        if ln.strip().startswith("volumes:"):
            in_volumes = True
            continue
        if in_volumes and ln.strip() and not ln.lstrip().startswith("#") \
                and ln.lstrip().startswith("- "):
            volume_lines.append(ln.strip())
    return volume_lines


# ── T10: absolute-path compose pipeline still generates valid mounts ──────

def test_t10_abs_compose_pipeline_directory_mounts() -> None:
    """End-to-end: run install.sh §8 step (relative -> absolute sed) over the
    REAL compose file; assert the directory bind mounts survive the rewrite
    with the repo-root prefix intact (the mount-line counterpart of the
    removed sentinel test — language is NOT part of the path anymore).
    """
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        deploy_dir = d / "deploy"
        deploy_dir.mkdir()
        shutil.copy(COMPOSE_YAML, deploy_dir / "main.compose.yaml")
        snippet = _abs_compose_snippet(d, hermetic=True)
        r = subprocess.run(["bash", "-c", snippet], cwd=str(d),
                           capture_output=True, text=True)
        assert r.returncode == 0, f"pipeline failed: {r.stderr}"
        content = Path(r.stdout.strip().splitlines()[-1]).read_text()
        volume_lines = _compose_volume_lines(content)
        joined = "\n".join(volume_lines)
        for sk in SKILLS:
            expected = (f"{d}/deploy/.runtime-skills/{sk}:"
                        f"/opt/data/skills/agent-matrix/{sk}:ro")
            assert expected in joined, f"abs-rewrite lost skill mount: {expected}"
        expected_soul = f"{d}/deploy/.runtime-soul:/opt/soul-src:ro"
        assert expected_soul in joined, f"abs-rewrite lost SOUL mount: {expected_soul}"
        expected_deploy = f"{d}/deploy:/opt/mwroot/deploy:ro"
        assert expected_deploy in joined, f"abs-rewrite lost ./deploy mount: {expected_deploy}"
        # 2026-10-22 issue #82: /opt/data is a NAMED VOLUME, not a host bind.
        # The abs-rewrite must not turn it back into a path — a bind mount puts
        # the control socket on the virtiofs host mount, where a socket inode
        # can become unreachable in the guest (E95) and the gateway can never
        # rebind it. Assert the volume form survives the rewrite verbatim.
        assert "main_hermes_main:/opt/data" in joined, (
            "abs-rewrite lost the named volume for /opt/data: "
            "main_hermes_main:/opt/data"
        )
        assert f"{d}/data/main:/opt/data" not in joined, (
            "abs-rewrite turned /opt/data back into a host bind mount — that is "
            "the virtiofs topology that causes E95 (issue #82)"
        )


# ── T10-unwritable-tmpdir: 反证自证 (issue #76 裁-3, 防假绿) ────────────────

@pytest.mark.skipif(os.geteuid() == 0,
                    reason="chmod 555 对 root 无效, 红侧断言天然不成立 (REQ C-4)")
def test_t10_unwritable_tmpdir_counter_proof() -> None:
    """反证自证 (裁-3): 外部 TMPDIR 不可写时 ——
    红侧: 修复前裸 idiom (片段直接取用外部 TMPDIR) 必 Permission denied 失败,
          证明原红真实存在、本用例断言方向正确 (防假绿);
    绿侧: 修复后片段 (片段内先设 TMPDIR, mktemp 行字面不变) 在同一环境 rc=0
          且产物 mounts 正确。
    不可写目录用例自建自清 (chmod 555 / finally 恢复 755 再删, REQ C-2)。
    子进程 env 钉 `LC_ALL=C`: `Permission denied` 是 glibc `strerror(EACCES)` 的
    运行期输出, 受 `LC_MESSAGES` locale 控制 —— 不钉住就是一条新的环境耦合
    (审查建议项 M-1: 本修复自身会引入 locale 依赖假红)。
    """
    outer = tempfile.TemporaryDirectory()
    ro_dir = Path(outer.name) / "unwritable-tmpdir"
    ro_dir.mkdir()
    ro_dir.chmod(0o555)
    try:
        # 钉 locale 到 C: 只作用于本用例的两个 subprocess, 影响面为零。
        env = {**os.environ, "TMPDIR": str(ro_dir), "LC_ALL": "C"}

        # 红侧: 裸 idiom 在不可写 TMPDIR 下必须失败 (Permission denied)。
        # 断言对象是「修复前 idiom」(hermetic=False, 内存渲染, 不落被测文件)。
        red = subprocess.run(["bash", "-c", _abs_compose_snippet(Path(outer.name), hermetic=False)],
                             capture_output=True, text=True, env=env)
        assert red.returncode != 0, (
            "红侧断言失败: 裸 idiom 在不可写 TMPDIR 下竟然成功了 —— "
            "环境权限模型与 REQ §2 根因不符, 本反证失去意义 (假绿警报)"
        )
        assert "Permission denied" in red.stderr, (
            f"红侧失败原因不是 mktemp Permission denied, 而是其他错误: {red.stderr!r}"
        )

        # 绿侧: 修复后片段在同一个不可写 TMPDIR 环境下成功, 且 compose 产物正确。
        work = Path(outer.name) / "work"
        deploy_dir = work / "deploy"
        deploy_dir.mkdir(parents=True)
        shutil.copy(COMPOSE_YAML, deploy_dir / "main.compose.yaml")
        green = subprocess.run(["bash", "-c", _abs_compose_snippet(work, hermetic=True)],
                               capture_output=True, text=True, env=env)
        assert green.returncode == 0, f"修复后片段仍失败: rc={green.returncode} {green.stderr}"
        produced = Path(green.stdout.strip().splitlines()[-1])
        # 落点正向断言: mktemp 必须落在片段自设的 TMPDIR (<work>) 下 —— 纯否定式
        # 断言在 "TMPDIR 行被误改成别的可写目录" 时仍会成立 (审查建议项 S-4)。
        assert str(work) in str(produced), (
            f"mktemp 落点不在片段自设 TMPDIR ({work}) 下: {produced}"
        )
        assert str(ro_dir) not in str(produced), (
            f"mktemp 落点泄漏到不可写 TMPDIR: {produced}"
        )
        # 绿侧复用 t10 主体的 volumes 段解析逻辑, 不比被验证对象更宽松 (S-3)。
        joined = "\n".join(_compose_volume_lines(produced.read_text()))
        expected_soul = f"{work}/deploy/.runtime-soul:/opt/soul-src:ro"
        assert expected_soul in joined, f"abs-rewrite lost SOUL mount: {expected_soul}"
        # 2026-10-22 issue #82: /opt/data is a named volume (see the sibling
        # test) — the rewrite must keep the volume form, not a host path.
        assert "main_hermes_main:/opt/data" in joined, (
            "abs-rewrite lost the named volume for /opt/data: "
            "main_hermes_main:/opt/data"
        )
    finally:
        ro_dir.chmod(0o755)  # 恢复权限让 TemporaryDirectory 能清理 (REQ C-2 不留残余)
        outer.cleanup()


if __name__ == "__main__":
    # standalone 双跑口径 (pytest 收集对某些源码字符串处理不可靠时的兜底)
    for fn in [test_t1_compose_declares_directory_bind_mounts,
               test_t2_entry_command_via_directory_mount,
               test_t3_staging_generation_and_preflight,
               test_t4_lang_sentinel_sed_removed,
               test_t5_no_force_recreate_stop_rm_is_recreate,
               test_t6_bsd_red_lines_zero,
               test_t7_ac_53_04_red_line_preserved,
               test_t8_staging_e2e_lang_content,
               test_t8_section_break_makes_t8_red,
               test_t9_entry_publishes_soul_symlink,
               test_t11_entry_guard_clears_preexisting_directory,
               test_t11_entry_guard_keeps_regular_file_behavior,
               test_t10_abs_compose_pipeline_directory_mounts,
               test_t10_unwritable_tmpdir_counter_proof]:
        fn()
        print(f"PASS {fn.__name__}")
    # 参数化用例 (pytest 专属) —— standalone 口径下逐条跑
    for frag in [
        "docker exec \"${MAIN_CONTAINER_NAME}\" sh -c 'echo x > /f'",
        "docker exec -u root \"${MAIN_CONTAINER_NAME}\" sh -c 'echo x > /f'",
        "docker exec \"${MAIN_CONTAINER_NAME}\" chown 10000:10000 /opt/data/SOUL.md",
        "docker exec \"${MAIN_CONTAINER_NAME}\" chmod 644 /opt/data/SOUL.md",
        "docker exec \"${MAIN_CONTAINER_NAME}\" rm -rf /opt/data/skills",
        "docker exec \"${MAIN_CONTAINER_NAME}\" tee /opt/data/SOUL.md",
        "docker cp /tmp/x /opt/data/SOUL.md",
        "docker exec \"${MAIN_CONTAINER_NAME}\" bash -c \"echo x > /f\"",
        "docker exec \"${MAIN_CONTAINER_NAME}\" sh -c 'cat /f > /g'",
        "docker exec \"${MAIN_CONTAINER_NAME}\" cat /f > /g",
        "docker exec \"${MAIN_CONTAINER_NAME}\" mv /a /b",
        "docker exec \"${MAIN_CONTAINER_NAME}\" sed -i s/a/b/ /f",
        "docker exec \"${MAIN_CONTAINER_NAME}\" python3 -c \"open('/f','w').write('x')\"",
        "docker compose exec \"${MAIN_CONTAINER_NAME}\" sh -c 'echo x > /f'",
        "docker-compose exec \"${MAIN_CONTAINER_NAME}\" sh -c 'echo x > /f'",
        "docker exec \"${MAIN_CONTAINER_NAME}\" cat \\\n  > /f",
        "docker exec \"${MAIN_CONTAINER_NAME}\" sh -c \\\n  'echo x > /f'",
        "${DOCKER_CMD} exec \"${MAIN_CONTAINER_NAME}\" sh -c 'echo x > /f'",
        "docker exec ${MAIN_CONTAINER_NAME} sh -c 'echo x > /f'",
    ]:
        test_t7_red_line_detects_write_exec(frag)
    print("PASS test_t7_red_line_detects_write_exec (17 fragments)")
    for frag in [
        "docker exec \"${MAIN_CONTAINER_NAME}\" cat /mw_data/settings.json",
        "docker exec \"${MAIN_CONTAINER_NAME}\" ls /opt/data",
        "docker exec \"${MAIN_CONTAINER_NAME}\" test -f /opt/data/SOUL.md",
        "docker exec \"${MAIN_CONTAINER_NAME}\" sha256sum /opt/data/SOUL.md",
    ]:
        test_t7_red_line_allows_readonly_exec(frag)
    print("PASS test_t7_red_line_allows_readonly_exec (4 fragments)")
    print("ALL PASS")
