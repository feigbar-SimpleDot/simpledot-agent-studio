#!/bin/sh
# main container entry: gateway (foreground) + reaper (resident task reaper) + console (web console)
#                 + hermes-webui (optional, auto-started when /webui is mounted)
# Any background process that exits unexpectedly is restarted by its watchdog loop; gateway exit = container exit.
# Note (m1-impl macOS, 2026-10-18): the main SOUL arrives as a DIRECTORY bind mount
#     at /opt/soul-src (Apple `container` requires directory bind sources; the old
#     file-level :ro mount of docs/main-soul.<LANG>.md onto /opt/data/SOUL.md only
#     worked on docker). install.sh stages the active-LANG source into the host-owned
#     deploy/.runtime-soul/ before compose up; this entry publishes it at the
#     canonical /opt/data/SOUL.md below via idempotent `ln -sfn`. Upgrade =
#     `git pull + ./deploy/install.sh` (recreate via stop+rm).
set -u

# ── m1-impl: publish the read-only SOUL source at the canonical path ──
# /opt/soul-src is the :ro directory bind of deploy/.runtime-soul (contains SOUL.md).
# ln -sfn is idempotent; if a pre-m1-impl deploy left a stale REGULAR /opt/data/SOUL.md
# here (bind-mounted file), unlink it first so the symlink replaces it cleanly.
# issue #59 N-c: the guard must also clear a pre-existing DIRECTORY at the
# canonical path — `rm -f` cannot remove a directory, and `ln -sfn` would then
# land the link INSIDE it (/opt/data/SOUL.md/SOUL.md), silently publishing
# nothing. Hence: existing (-e) and not a symlink (-L) => rm -rf (file OR dir).
if [ -f /opt/soul-src/SOUL.md ] && [ -e /opt/data/SOUL.md ] && [ ! -L /opt/data/SOUL.md ]; then
    rm -rf /opt/data/SOUL.md 2>/dev/null || true
fi
if [ -f /opt/soul-src/SOUL.md ]; then
    ln -sfn /opt/soul-src/SOUL.md /opt/data/SOUL.md
fi

# ── Critical fix 2026-10-04 ────────────────────────────────
# s6-supervise (/proc/1) does not inherit the docker compose `environment:` block,
# and entry.sh runs in the cont-init context (not PID 1 init). As a result, processes
# forked by the reaper watchdog cannot see MW_HOME/MW_REGISTRY etc. and fall
# back to Path.cwd()/mw_data = /opt/hermes/mw_data (empty dir; sqlite silently
# creates an empty db), so the reaper never sees ACTIVE team work.
# The explicit exports below make sure every forked process inherits them.
export MW_HOME=/mw_data
export MW_REGISTRY=/mw_data/registry.json
export MW_HERMES_HOME=/opt/data
export MW_KANBAN_DB=/opt/data/kanban.db
export MW_TASK_DIR_BASE=/share_workspace/tasks
export MW_ALLOWED_WS_PREFIX=/share_workspace
export PYTHONPATH=/opt/mwroot

# i18n v1.0 (arch §3.3): read the deploy language from the deploy_lang file written by install.sh,
# then export MW_LANG (single source for mw/i18n). The known s6-not-inheriting-compose-env trap
# (measured 2026-10-04) is bypassed via the proven host-write -> container-read channel.
# Second gate inside the container: missing file / invalid value -> default en (arch §3.3 end note).
# Load deploy/lib/i18n.sh (arch §4.4 / AR6): shared POSIX-sh i18n helpers from the same source
# (i18n_say/i18n_fail/i18n_validate). The container-side deploy/lib arrives via bind mount.
if [ -f /opt/mwroot/deploy/lib/i18n.sh ]; then
    . /opt/mwroot/deploy/lib/i18n.sh
fi
MW_LANG_FILE="/mw_data/deploy_lang"
if [ -f "${MW_LANG_FILE}" ]; then
    MW_LANG_VAL="$(head -n1 "${MW_LANG_FILE}" 2>/dev/null || echo '')"
else
    MW_LANG_VAL=""
fi
case "${MW_LANG_VAL}" in
    en|zh-cn) MW_LANG="${MW_LANG_VAL}" ;;
    *)        MW_LANG="en" ;;
esac
export MW_LANG
# Also export SAS_LANG (for shells / sub-scripts inside the container, consistent with the host)
export SAS_LANG="${MW_LANG}"

# ── mw runtime deps: watchdog + pyyaml ─────────────────────────────────────
# watchdog = fsnotify primary path (design.md §5.0); pyyaml = 读写两侧共同
# 依赖 (issue #97): mw/workflows.yaml 与 issue #70 的 mw/decision_paths.yaml
# 都要 import yaml, 而镜像裸 `python3` 实测无 PyYAML (evidence-issues51-53
# 全套 pytest 的 ModuleNotFoundError: yaml ×2 + docs/TEST53-DEPS.md 环境
# 探测), 部署面此前无任何声明 ⇒ /api/decision-paths 会静默恒返 {"paths":[]},
# main 误以为「没有决策路径表」。修法 = 与 watchdog 完全同一机制: 探测缺失
# 包集合 → uv/pip --target ${MW_DEPS_DIR} (bind volume) → 失败 warn 降级,
# 绝不阻塞容器启动。
# 2026-10-18 (issue #53 r5b-deps-target): e69d5b4 installed into
# /opt/hermes/.venv — but the venv is root:root 755 while entry runs as the
# hermes service uid (10000): ensurepip/pip hit EPERM and a fresh deploy
# ALWAYS degrades (170 measured P1, docs/TEST53-DEPS.md §4). Fix = plan A:
# install with pip --target into /opt/data/mw-deps — /opt/data is the bind
# volume from compose (./data/main), so it is uid-writable AND persists across
# `docker rm` + recreate; we then prepend it to PYTHONPATH so every forked
# process (reaper/console/gateway) sees it, whichever python3 PATH resolves to
# (measured: bare `python3` = /usr/bin/python3, NOT the venv — the old block's
# venv target could not have helped the reaper anyway). Red lines kept: no
# image rebuild, install.sh untouched, install failure = warn + degrade to the
# reaper's own 15s polling (mw/cli.py:142-148), never block container start.
#
# Installer chain (first available wins, short timeouts so offline starts
# don't hang): uv (baked in image, no pip needed) → existing pip → stdlib
# urllib pip-wheel bootstrap into MW_DEPS_DIR (wheels are plain zips), then
# retry pip. MW_DEPS_DIR is env-overridable purely for stub tests.
MW_DEPS_DIR="${MW_DEPS_DIR:-/opt/data/mw-deps}"
export PYTHONPATH="${MW_DEPS_DIR}:${PYTHONPATH:-}"
# 缺失包集合 (探测式, 幂等): 全齐 => 整段短路; 有缺 => 一次补齐。
MW_DEPS_MISSING=""
python3 -c "import watchdog.observers, watchdog.events" >/dev/null 2>&1 \
    || MW_DEPS_MISSING="${MW_DEPS_MISSING:+$MW_DEPS_MISSING }watchdog"
python3 -c "import yaml" >/dev/null 2>&1 \
    || MW_DEPS_MISSING="${MW_DEPS_MISSING:+$MW_DEPS_MISSING }pyyaml"
if [ -z "${MW_DEPS_MISSING}" ]; then
    echo "[entry] ✓ watchdog + pyyaml OK (fsnotify + yaml parsing available)" >&2
else
    echo "[entry] missing mw deps: ${MW_DEPS_MISSING} — installing into ${MW_DEPS_DIR} (bind volume) ..." >&2
    MW_DEPS_OK=0
    if mkdir -p "${MW_DEPS_DIR}" 2>/dev/null; then
        # 1) uv: single binary, resolves the SAME python3 the reaper will run.
        #    Timeout via UV_HTTP_TIMEOUT (uv pip install has no --timeout flag).
        if command -v uv >/dev/null 2>&1; then
            UV_HTTP_TIMEOUT=15 uv pip install --quiet \
                --python "$(command -v python3)" --target "${MW_DEPS_DIR}" \
                ${MW_DEPS_MISSING} >/dev/null 2>&1 && MW_DEPS_OK=1
        fi
        # 2) pip if the image already has one
        if [ "${MW_DEPS_OK}" -eq 0 ] && python3 -m pip --version >/dev/null 2>&1; then
            python3 -m pip install --disable-pip-version-check --timeout 15 --retries 1 \
                --target "${MW_DEPS_DIR}" ${MW_DEPS_MISSING} >/dev/null 2>&1 && MW_DEPS_OK=1
        fi
        # 3) no pip in image (measured v2026.9.24): stdlib urllib pulls the pip
        #    wheel and unzips it into MW_DEPS_DIR, then step 2 gets its chance
        if [ "${MW_DEPS_OK}" -eq 0 ]; then
            python3 - "${MW_DEPS_DIR}" <<'PYBOOT' >/dev/null 2>&1 || true
import io, re, sys, time, urllib.request, zipfile
target = sys.argv[1]
def fetch(url, tries=4):
    for i in range(tries):
        try:
            return urllib.request.urlopen(url, timeout=15).read()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1 + i)
try:
    html = fetch("https://pypi.org/simple/pip/").decode()
    links = re.findall(r'href="([^"]*?pip-[\d.]+-py3-none-any\.whl[^"]*)"', html)
    if links:
        zipfile.ZipFile(io.BytesIO(fetch(links[-1].split("#", 1)[0]))).extractall(target)
except Exception:
    sys.exit(1)
PYBOOT
            python3 -m pip install --disable-pip-version-check --timeout 15 --retries 1 \
                --target "${MW_DEPS_DIR}" ${MW_DEPS_MISSING} >/dev/null 2>&1 && MW_DEPS_OK=1
        fi
    fi
    # 逐项复核 (安装结果可能只补齐一部分, 逐项报告 + 逐项降级口径):
    #   watchdog 缺 => reaper 退 15s 轮询 (mw/cli.py try/except);
    #   pyyaml  缺 => /api/workflows·/api/decision-paths 降级空表 (两端点
    #                try/except 不抛)。两者都只 warn, 不阻塞容器启动。
    if python3 -c "import watchdog.observers, watchdog.events" >/dev/null 2>&1; then
        case " ${MW_DEPS_MISSING} " in
            *watchdog*) echo "[entry] ✓ watchdog installed (fsnotify primary path available)" >&2 ;;
        esac
    else
        echo "[entry] ⚠ watchdog install failed — reaper will degrade to 15s polling" >&2
    fi
    if python3 -c "import yaml" >/dev/null 2>&1; then
        case " ${MW_DEPS_MISSING} " in
            *pyyaml*) echo "[entry] ✓ pyyaml installed (workflows/decision-paths yaml parsing available)" >&2 ;;
        esac
    else
        echo "[entry] ⚠ pyyaml install failed — /api/workflows and /api/decision-paths degrade to empty tables" >&2
    fi
fi

# reaper watchdog (background)
# 2026-10-04: added MW_REAPER_STUCK_MIN=5 (commit 4 / design.md §5.0:
# replaces the old 30-min fallback threshold)
# 2026-10-05: measured reasoning-model max turn ~13min; the 5-min threshold still
# fired during long halogen/qwen turns (measured 12.5% false-trigger rate).
# Raised to 10min to cover 98.5% of reasoning durations; real hangs are still caught within 10min.
( while true; do
    MW_REAPER_STUCK_MIN="${MW_REAPER_STUCK_MIN:-10}" \
    python3 -m mw.cli reaper --interval "${MW_REAPER_INTERVAL:-15}" || sleep 5
    echo "[entry] reaper exited, restarting in 5s" >&2
    sleep 5
  done ) &
REAPER_PID=$!

# console watchdog (background)
( while true; do
    python3 -m mw.console || sleep 5
    echo "[entry] console exited, restarting in 5s" >&2
    sleep 5
  done ) &
CONSOLE_PID=$!

# hermes-webui watchdog (optional: only when /webui is mounted and contains start.sh)
# Repo: https://github.com/nesquena/hermes-webui
# FOREGROUND=1 stops start.sh from daemonizing; this watchdog owns its lifecycle.
WEBUI_PID=""
if [ -d /webui ] && [ -f /webui/start.sh ]; then
  ( while true; do
      cd /webui && HERMES_WEBUI_FOREGROUND=1 ./start.sh || sleep 5
      echo "[entry] hermes-webui exited, restarting in 5s" >&2
      sleep 5
    done ) &
  WEBUI_PID=$!
  echo "[entry] hermes-webui watchdog started (/webui, port ${HERMES_WEBUI_PORT:-8787})" >&2
fi

# pixel-agents pixel-office watchdog (optional: PIXEL_OFFICE_ENABLED=1 and /pixel-office mounted)
# Binds to container-internal 127.0.0.1:3199 only, never exposed; the console reaches it via the same-origin proxy /pixel/ + /ws.
# The mounted directory supports two layouts:
#   A. npm project root (recommended): /pixel-office/node_modules/pixel-agents/dist/cli.js
#      (@fastify/* deps live in the sibling node_modules, so the whole dir must be mounted)
#   B. bare package dir:      /pixel-office/dist/cli.js (only when deps are bundled; the current package is not)
# Critical seed: ~/.pixel-agents/config.json with standalone.watchAllSessions=true —
# without it, bridge-injected external worker sessions are rejected as "project untracked".
# The config lives in the container HOME (not a mounted volume), rebuilt on every start; idempotent and harmless.
PIXEL_PID=""
PIXEL_CLI=""
if [ "${PIXEL_OFFICE_ENABLED:-0}" = "1" ]; then
  if [ -f /pixel-office/node_modules/pixel-agents/dist/cli.js ]; then
    PIXEL_CLI=/pixel-office/node_modules/pixel-agents/dist/cli.js
  elif [ -f /pixel-office/dist/cli.js ]; then
    PIXEL_CLI=/pixel-office/dist/cli.js
  fi
fi
if [ -n "$PIXEL_CLI" ]; then
  mkdir -p "$HOME/.pixel-agents"
  if [ ! -f "$HOME/.pixel-agents/config.json" ]; then
    printf '{"standalone":{"watchAllSessions":true}}' > "$HOME/.pixel-agents/config.json"
  else
    node -e '
      const fs = require("fs"), p = process.env.HOME + "/.pixel-agents/config.json";
      let d = {}; try { d = JSON.parse(fs.readFileSync(p, "utf8")); } catch {}
      d.standalone = Object.assign({}, d.standalone, { watchAllSessions: true });
      fs.writeFileSync(p, JSON.stringify(d));
    ' 2>/dev/null || printf '{"standalone":{"watchAllSessions":true}}' > "$HOME/.pixel-agents/config.json"
  fi
  ( while true; do
      node "$PIXEL_CLI" --port 3199 --host 127.0.0.1 || sleep 5
      echo "[entry] pixel-agents exited, restarting in 5s" >&2
      sleep 5
    done ) &
  PIXEL_PID=$!
  echo "[entry] pixel-agents watchdog started ($PIXEL_CLI, 127.0.0.1:3199)" >&2
elif [ "${PIXEL_OFFICE_ENABLED:-0}" = "1" ]; then
  echo "[entry] ⚠ PIXEL_OFFICE_ENABLED=1 但 /pixel-office 下找不到 pixel-agents cli.js — 跳过(PIXEL_OFFICE_DIR 需指向 npm install pixel-agents 的项目根, 含 node_modules)" >&2
fi

trap 'kill $REAPER_PID $CONSOLE_PID ${WEBUI_PID:-} ${PIXEL_PID:-} 2>/dev/null' TERM INT

# gateway bootstrap: since image v2026.9.24 the image's own _maybe_redirect_run_to_s6_supervision rewrites this line into `s6-svc -u gateway-default` + `sleep infinity` — the gateway is hosted by the s6 slot, not by the container's foreground process
exec hermes gateway run
