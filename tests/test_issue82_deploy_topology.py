"""issue #82 防回归护栏 —— 部署拓扑的三个硬不变量。

背景 (issue #82, P0):
    main 的 /opt/data 原本是宿主 bind mount (./data/main:/opt/data)。在
    macOS + Colima/lima 上宿主侧是 virtiofs。virtiofs 上一个 socket inode
    可能在容器 (guest) 侧变得不可达: os.path.exists() 因 E95 返回 False
    (Python 吞掉 OSError), 于是 startup 路径跳过 unlink, 随后的 bind 打在
    残留 inode 上再次 E95, gateway 直接死掉并无限重启循环。

    同一台机器上的 worker 从来没复现 E95 —— 它们的 /opt/data 用的是
    命名卷 (ext4)。这就是判据: 决定性因素是存储后端, 不是镜像, 不是版本。

    根因修复 = 把 main 的 /opt/data 也换成命名卷。

护栏覆盖三个不变量:
    I1  main 的 /opt/data 是命名卷, 不是宿主 bind mount。
    I2  compose 的 service 名 == container_name, 且网络 alias 唯一 ——
        共享 alias "hermes" 会让跨容器请求命中错误的服务。
    I3  worker 运行时 compose 模板 (mw/console.py COMPOSE_TEMPLATE) 同样用
        命名卷挂载 /opt/data, 接入 simple-dot, 且 alias 唯一。

这些是纯静态断言 (只读文件文本), 不碰真机。
"""

from __future__ import annotations

import pathlib
import re

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
COMPOSE_YAML = REPO / "deploy" / "main.compose.yaml"
CONSOLE_PY = REPO / "mw" / "console.py"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_YAML.read_text())


def _volume_specs(svc: dict) -> list:
    vols = svc.get("volumes") or []
    out = []
    for v in vols:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            out.append("%s:%s" % (v.get("source"), v.get("target")))
    return out


# ── I1: main 的 /opt/data 必须是命名卷 ─────────────────────────────────────

def test_i1_main_data_is_named_volume_not_bind() -> None:
    """main's /opt/data must be a named volume."""
    compose = _compose()
    svc = compose["services"]["hermes_main"]
    specs = _volume_specs(svc)

    targets = [s.rsplit(":", 1)[-1] for s in specs]
    assert "/opt/data" in targets, (
        "main has no /opt/data mount at all: %s" % (specs,)
    )

    data_spec = next(s for s in specs if s.rsplit(":", 1)[-1] == "/opt/data")
    source = data_spec.rsplit(":", 1)[0]

    assert "/" not in source, (
        "main's /opt/data must be a NAMED VOLUME, got a bind mount: %r. "
        "A host bind puts the control socket on virtiofs, where a stale "
        "socket inode becomes unreachable in the guest (E95) and the gateway "
        "can never rebind it (issue #82)." % (data_spec,)
    )
    assert source == "main_hermes_main", (
        "unexpected volume name for /opt/data: %r "
        "(expected 'main_hermes_main')" % (source,)
    )


def test_i1_main_volume_declared_at_top_level() -> None:
    """The named volume must be declared under the top-level volumes: key."""
    compose = _compose()
    vols = compose.get("volumes") or {}
    assert "main_hermes_main" in vols, (
        "main_hermes_main is not declared under top-level volumes: %s"
        % (sorted(vols),)
    )


def test_i1_skills_soul_deploy_mounts_survive() -> None:
    """The sub-mounts that carry the payload must still be there."""
    svc = _compose()["services"]["hermes_main"]
    specs = _volume_specs(svc)
    joined = "\n".join(specs)

    assert "/opt/data/skills/agent-matrix/" in joined, (
        "the agent-matrix skills sub-mount was lost: %s" % (specs,)
    )
    assert "/opt/soul-src" in joined, "the SOUL mount was lost: %s" % (specs,)
    assert "/opt/mwroot/deploy" in joined, "the ./deploy mount was lost: %s" % (specs,)


# ── I2: service 名与网络 alias 唯一 ─────────────────────────────────────────

def test_i2_main_service_name_matches_container_name() -> None:
    """The compose service name must equal container_name.

    Compose registers the SERVICE NAME as a network alias automatically.
    If the service is called "hermes" while the container is "hermes_main",
    every container on the shared network also answers to "hermes" and a
    cross-container request for "hermes" can hit the wrong service.
    """
    compose = _compose()
    svc = compose["services"]["hermes_main"]
    # container_name is templated (${MAIN_CONTAINER_NAME:-hermes_main}); what
    # matters is that its DEFAULT expansion equals the service name, because
    # compose registers the SERVICE name as the network alias.
    for field in ("container_name", "hostname"):
        raw = svc.get(field)
        assert raw is not None, "main must declare %s" % field
        assert raw == "${MAIN_CONTAINER_NAME:-hermes_main}", (
            "main %s must default to hermes_main, got %r" % (field, raw)
        )


def test_i2_main_declares_unique_network_alias() -> None:
    """main must declare an explicit, unique network alias."""
    compose = _compose()
    svc = compose["services"]["hermes_main"]
    networks = svc.get("networks") or {}
    assert networks, "main must join a network"

    aliases = []
    for net in networks.values():
        if isinstance(net, dict):
            aliases.extend(net.get("aliases") or [])
    assert aliases, (
        "main declares no explicit network alias — other containers could not "
        "address it deterministically over simple-dot"
    )
    assert "hermes" not in aliases, (
        'main must not answer to the shared alias "hermes": %s' % (aliases,)
    )
    assert "hermes_main" in aliases, (
        "main must answer to its own unique alias 'hermes_main': %s" % (aliases,)
    )


def test_i2_main_joins_simple_dot_network() -> None:
    """main must be on the shared simple-dot network."""
    compose = _compose()
    svc = compose["services"]["hermes_main"]
    networks = svc.get("networks") or {}
    assert "simple-dot" in networks, (
        "main is not on the simple-dot network: %s" % (sorted(networks),)
    )


def test_i2_simple_dot_is_external_in_compose() -> None:
    """simple-dot is created by install.sh, so compose must not own it."""
    compose = _compose()
    nets = compose.get("networks") or {}
    assert "simple-dot" in nets, "no simple-dot network declared: %s" % (sorted(nets),)
    assert nets["simple-dot"].get("external") is True, (
        "simple-dot must be external — install.sh creates it once, and if "
        "compose owned it, `docker compose down` would delete the network "
        "that every worker is attached to"
    )


# ── I3: worker 运行时模板必须同构 ───────────────────────────────────────────

def _worker_template() -> str:
    """Extract the COMPOSE_TEMPLATE source out of mw/console.py."""
    src = CONSOLE_PY.read_text()
    m = re.search(r'COMPOSE_TEMPLATE\s*=\s*"""(?P<body>.*?)"""', src, re.S)
    assert m, "COMPOSE_TEMPLATE not found in mw/console.py"
    return m.group("body")


def test_i3_worker_template_uses_named_volume_for_data() -> None:
    """Workers must mount /opt/data from a named volume too."""
    body = _worker_template()
    assert ":/opt/data" in body or ":${HERMES_DATA_VOLUME}:/opt/data" in body, (
        "worker template does not mount /opt/data"
    )
    assert "./data" not in body, (
        "worker template mounts /opt/data from a host path — that is the "
        "virtiofs topology that causes E95 (issue #82)"
    )


def test_i3_worker_template_joins_simple_dot_with_unique_alias() -> None:
    """The worker template must use simple-dot and a unique alias."""
    body = _worker_template()
    assert "simple-dot" in body, (
        "worker template does not join the simple-dot network"
    )
    # YAML block sequence form:  aliases:\n        - hermes_{name}
    aliases = re.findall(r"aliases:\s*\n((?:\s*-\s*\S+\s*\n)+)", body)
    assert aliases, "worker template declares no network aliases"
    for group in aliases:
        names = [n.strip().lstrip("-").strip().strip('"\'')
                 for n in group.strip().splitlines()]
        names = [n for n in names if n]
        assert names, "worker template has an empty alias list"
        for n in names:
            assert n != "hermes", (
                'worker template uses the shared alias "hermes": %s' % (names,)
            )
            assert n == "hermes_{name}", (
                "worker alias must be the templated unique name, got %r" % (n,)
            )


def test_i3_worker_template_service_name_is_placeholder_unique() -> None:
    """The worker service name must be templated per worker, not fixed."""
    body = _worker_template()
    assert re.search(r"^\s{2}\S*\{name\}:", body, re.M), (
        "worker template service name is not templated — every worker would "
        "register the same network alias"
    )


def test_i3_worker_template_service_mount_matches_declared_volume() -> None:
    """The service-level mount must name a declared top-level volume.

    Regression: on a live host the worker compose carried a top-level
    `volumes:` block but the SERVICE had no /opt/data mount at all. Docker
    then auto-created an ANONYMOUS volume (the image declares VOLUME
    /opt/data) -- back to the anonymous-storage topology, invisible to the
    operator, while the template's declared volume sat unused.
    """
    body = _worker_template()
    m = re.search(r"^volumes:\s*\n((?:[ \t]+\S.*\n)+)", body, re.M)
    assert m, "worker template declares no top-level volumes: block"
    declared = set()
    for line in m.group(1).splitlines():
        km = re.match(r"^[ \t]+([A-Za-z0-9_.\-{}\s]+):\s*$", line)
        if km:
            declared.add(km.group(1).strip())
    assert declared, "worker template declares an empty top-level volumes: block"

    # service-level mount of /opt/data
    mm = re.search(r"^[ \t]+-[ \t]+([A-Za-z0-9_.\-{}+]+):/opt/data\s*$", body, re.M)
    assert mm, (
        "worker template has no service-level mount for /opt/data -- docker "
        "would silently create an anonymous volume instead"
    )
    source = mm.group(1)
    assert source in declared, (
        "worker mounts /opt/data from %r, which is not declared under the "
        "top-level volumes: block (%s) -- compose would treat it as an "
        "unnamed volume" % (source, sorted(declared))
    )



if __name__ == "__main__":
    failures = []
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS %s" % name)
            except AssertionError as exc:
                failures.append(name)
                print("FAIL %s: %s" % (name, exc))
    raise SystemExit(1 if failures else 0)
