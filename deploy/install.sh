#!/usr/bin/env bash
# Simple-Agents-Studio — one-shot main agent deploy
# Usage: run  ./deploy/install.sh  from the repo root
# Prereq: .env copied from .env.example with required fields filled; host has docker + compose plugin
# i18n (2026-10-14): loads deploy/lib/i18n.sh for bilingual messages and LANG validation
# i18n (2026-10-17, issue #53): host-side file pre-stage + bind mounts as source of truth.
# 2026-10-18 (s1-req #53 C): install.sh trimmed to the deploy essentials only —
#   bind-source pre-stage + compose up + one-line error reporting.
#   Operations scripts (hash compare / kanban init / pip deps / readiness loop /
#   failure dump) and the 3-layer container-CLI abstraction are removed. macOS users
#   keep calling `docker` directly (docker-for-apple-container ships a docker
#   shim) — see DEPLOY.md §0 for the macOS line note.
# m1-impl (2026-10-18, macOS compat): every bind source is now a DIRECTORY (Apple
#   container Parser.swift requires isDirectory); skills/SOUL are staged into
#   deploy/.runtime-{skills,soul}/ (gitignored) and mount as directory :ro binds.
#   The compose recreate flag (unsupported by the Apple docker shim) is gone —
#   recreate is done with stop+rm instead.
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"   # cd to the repo root

# ── i18n v1.0: load bilingual helpers + LANG validation (arch §3.2/§4.4) ──
LIB_DIR="$(cd "$(dirname "$0")" && pwd)/lib"
# shellcheck source=lib/i18n.sh
. "${LIB_DIR}/i18n.sh"

say()  {
  # After loading i18n, say/fail take two args: <en> <zh-cn>; one arg stays backward compatible (say "xxx" = plain English).
  if [ $# -ge 2 ]; then
    printf '\033[1;36m[install]\033[0m %s\n' "$(i18n_say "$1" "$2")"
  else
    printf '\033[1;36m[install]\033[0m %s\n' "$1"
  fi
}
fail() {
  if [ $# -ge 2 ]; then
    printf '\033[1;31m[install] %s\033[0m\n' "$(i18n_fail "$1" "$2")" >&2
  else
    printf '\033[1;31m[install] %s\033[0m\n' "$1" >&2
  fi
  exit 1
}

# ── 1. Config check + LANG sentinel ────────────────────────────
# LANG 哨兵: SAS_LANG 走 i18n_validate, 失败一行 fail, 不展开排障.
[ -f .env ] || fail "Missing .env — first run: cp .env.example .env and fill in the required fields (★)" "缺少 .env — 先执行: cp .env.example .env 并填写必填项(★)"
set -a; . ./.env; set +a
# i18n v1.0 (arch §3.2, AC C1/C2): hard-validate LANG=en|zh-cn; empty = en; invalid = fail fast
# Shared across i18n.sh / install.sh / verify.sh via SAS_LANG (POSIX LANG untouched, §3.1)
SAS_LANG="${LANG:-en}"
if ! i18n_validate "${SAS_LANG}" "LANG"; then
  fail "Invalid LANG='${SAS_LANG}' in .env — allowed: en (default) | zh-cn" \
       ".env 里的 LANG='${SAS_LANG}' 非法 — 允许值: en(默认) | zh-cn"
fi
# Persist the validated value to data/mw_data/deploy_lang (main_entry.sh reads it at startup to export MW_LANG).
# Overwritten on every install (idempotent); empty = empty file -> main_entry.sh still falls back to default en via its case gate.
mkdir -p data/mw_data
printf '%s\n' "${SAS_LANG}" > data/mw_data/deploy_lang \
  || fail "Failed to write data/mw_data/deploy_lang" "写 data/mw_data/deploy_lang 失败"
export SAS_LANG
: "${API_SERVER_KEY:?请在 .env 填写 API_SERVER_KEY}"
[ ${#API_SERVER_KEY} -ge 16 ] || fail "API_SERVER_KEY too short (>=16 chars); generate: openssl rand -hex 32" "API_SERVER_KEY 太短(≥16位), 生成: openssl rand -hex 32"
HERMES_IMAGE="${HERMES_IMAGE:-swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/nousresearch/hermes-agent:v2026.9.24}"
MAIN_CONTAINER_NAME="${MAIN_CONTAINER_NAME:-hermes_main}"
CONSOLE_PORT="${CONSOLE_PORT:-8700}"

# ── 2. Dirs & permissions (container hermes = uid 10000; host uses 777 to avoid sudo) ──
# Issue #53 AC-53-04: 一次性 chmod; bind mount 源在宿主侧写, 不再有运行期 docker exec chmod 兜底.
mkdir -p data/main data/share_workspace/tasks data/mw_data data/webui_disabled
chmod -R 777 data 2>/dev/null || say "⚠ some data files are owned by container UID, skipping chmod (no impact on running)" "⚠ 部分数据文件属主为容器 UID, 跳过 chmod(不影响运行)"

chmod +x deploy/main_entry.sh   # s6 uses this script as entry; missing exec bit = Permission denied
say "Data dirs ready: data/{main,share_workspace,tasks,mw_data}" "数据目录就绪: data/{main,share_workspace,tasks,mw_data}"

# ── 2b. Shared network "simple-dot" (2026-10-22, issue #82) ─────
# main and every worker join this network and address each other by their
# UNIQUE container alias (http://hermes_<name>:8642). Published host ports go
# through docker-proxy inside the VM, which drops connections under memory
# pressure — the symptom that made most workers read as "offline" in the web
# console even while their containers were up. The network is declared
# `external: true` in both main.compose.yaml and the generated worker compose,
# so compose never owns its lifecycle — install.sh creates it once, idempotently.
if docker network inspect simple-dot >/dev/null 2>&1; then
  say "Shared network simple-dot already exists" "共享网络 simple-dot 已存在"
else
  docker network create simple-dot >/dev/null 2>&1 \
    || fail "Could not create the shared network 'simple-dot'" "创建共享网络 'simple-dot' 失败"
  say "Created shared network simple-dot (main + workers talk over container-internal addresses)" \
      "已创建共享网络 simple-dot (main 与 worker 走容器内网地址通信)"
fi

# ── 2c. Main named volume — declaration + seeding helper (2026-10-22, issue #82) ──
# main's /opt/data is a NAMED VOLUME (ext4 on the VM disk), not a host bind
# mount. Reason: on a virtiofs host mount (lima/Colima on macOS) a socket
# inode can become unreachable *inside the container* — stat/unlink then fail
# with E95 (Operation not supported) while the host still sees a perfectly
# normal socket. The gateway's startup path is
#     if bind_path.exists():   -> False   (Python swallows the E95 OSError)
#     bind_path.unlink()       -> skipped, because exists() said "no"
#     start_unix_server(...)   -> bind on the stale inode -> E95 -> gateway dies
# so every restart inherits the poison and the container restart-loops forever.
# The worker volumes never reproduced E95, which is the proof that the storage
# backend — not the image, not the version — is the deciding factor.
#
# The volume must carry the pre-existing HERMES_HOME (kanban.db, config, skills
# symlinks...) or a fresh deploy would come up empty. Seeding is idempotent:
# a non-empty volume is never overwritten. The seeding itself runs in
# section 7b, right after the image is confirmed present — the seeding
# helper IS the deploy image, so it must be available first.
#
# The compose project name is "mw-${MAIN_CONTAINER_NAME}" (see section 7), and
# compose prefixes named volumes with it — so the real volume on disk is
# "mw-${MAIN_CONTAINER_NAME}_main_hermes_main", not the bare name. Section 7b
# resolves the real name from the project name instead of hard-coding it, so
# the two can never drift apart.
MAIN_VOLUME_KEY="main_hermes_main"
_seed_main_volume() {
  local vol="$1"
  if [ ! -d "data/main" ] || [ -z "$(ls -A data/main 2>/dev/null)" ]; then
    say "data/main is empty — nothing to seed into ${vol}" "data/main 为空 — 无需向 ${vol} 播种"
    return 0
  fi
  # Already seeded? A non-empty volume keeps its contents across redeploys.
  # The deploy image is used as the helper (not alpine): it is already cached
  # for this deploy, so seeding never needs a registry round-trip.
  if [ -n "$(docker run --rm --entrypoint sh -v "${vol}:/seed" "${HERMES_IMAGE}" -c 'ls -A /seed 2>/dev/null' 2>/dev/null)" ]; then
    say "Volume ${vol} already has content — keeping it (no re-seed)" \
        "卷 ${vol} 已有内容 — 保留现状(不重复播种)"
    return 0
  fi
  # NOTE: --entrypoint sh is mandatory. The hermes image ships an s6-overlay
  # ENTRYPOINT; without the override, `docker run <image> sh -c ...` starts
  # the whole service stack and NEVER runs the command — the seeding would
  # silently do nothing (and the volume would stay root-owned).
  docker run --rm --entrypoint sh --user 0:0 -v "${vol}:/seed" -v "${PWD}/data/main:/src:ro" "${HERMES_IMAGE}" \
    -c 'cp -a /src/. /seed/ && chown -R 10000:10000 /seed' >/dev/null 2>&1 \
    || fail "Could not seed the main volume ${vol} from data/main" \
            "无法把 data/main 播种进主卷 ${vol}"
  say "Seeded volume ${vol} from data/main" "已把 data/main 播种进卷 ${vol}"
}

# hermes-webui enable check (optional): if HERMES_WEBUI_DIR is set, validate the directory
if [ -n "${HERMES_WEBUI_DIR:-}" ]; then
  [ -f "${HERMES_WEBUI_DIR}/start.sh" ] \
    || fail "HERMES_WEBUI_DIR=${HERMES_WEBUI_DIR} has no start.sh — first clone: git clone https://github.com/nesquena/hermes-webui ${HERMES_WEBUI_DIR}" \
            "HERMES_WEBUI_DIR=${HERMES_WEBUI_DIR} 里没有 start.sh — 先 clone: git clone https://github.com/nesquena/hermes-webui ${HERMES_WEBUI_DIR}"
  say "hermes-webui enabled: ${HERMES_WEBUI_DIR} -> /webui in container (port ${HERMES_WEBUI_PORT:-8787})" "hermes-webui 已启用: ${HERMES_WEBUI_DIR} → 容器 /webui (端口 ${HERMES_WEBUI_PORT:-8787})"
fi

# Pixel office enable check (optional): when PIXEL_OFFICE_ENABLED=1, validate the mounted directory.
# Recommended: mount the npm project root of pixel-agents (includes node_modules/, deps travel with it);
# mounting the bare package dir also works (deps must be bundled). Either layout with cli.js is fine.
mkdir -p data/pixel_office_disabled   # empty mount placeholder when disabled (same semantics as webui_disabled)
if [ "${PIXEL_OFFICE_ENABLED:-0}" = "1" ]; then
  [ -n "${PIXEL_OFFICE_DIR:-}" ] \
    || fail "PIXEL_OFFICE_ENABLED=1 but PIXEL_OFFICE_DIR is empty — must point to the npm install pixel-agents project root (containing node_modules/pixel-agents/dist/cli.js)" \
            "PIXEL_OFFICE_ENABLED=1 但 PIXEL_OFFICE_DIR 为空 — 需指向 npm install pixel-agents 的项目根(含 node_modules/pixel-agents/dist/cli.js)"
  if [ -f "${PIXEL_OFFICE_DIR}/node_modules/pixel-agents/dist/cli.js" ] || [ -f "${PIXEL_OFFICE_DIR}/dist/cli.js" ]; then
    say "Pixel office enabled: ${PIXEL_OFFICE_DIR} -> /pixel-office in container (127.0.0.1:3199 inside, proxied same-origin via console /pixel/)" "像素办公室已启用: ${PIXEL_OFFICE_DIR} → 容器 /pixel-office (容器内 127.0.0.1:3199, 经控制台 /pixel/ 同源代理访问)"
  else
    fail "pixel-agents cli.js not found under PIXEL_OFFICE_DIR=${PIXEL_OFFICE_DIR} — this directory must be an npm-installed pixel-agents project root (containing node_modules/pixel-agents/dist/cli.js), not an uninstalled source checkout. Setup: mkdir -p ~/pixel_office && cd ~/pixel_office && npm install pixel-agents" \
         "PIXEL_OFFICE_DIR=${PIXEL_OFFICE_DIR} 里找不到 pixel-agents cli.js — 该目录应是【已 npm install 的 pixel-agents 项目根】(含 node_modules/pixel-agents/dist/cli.js), 不能是未安装的源码仓库。准备: mkdir -p ~/pixel_office && cd ~/pixel_office && npm install pixel-agents"
  fi
fi

# ── 3. Console frontend (React build artifacts ship with the repo, copied into mw/ui_dist) ──
# 2026-10-17: install never rebuilds — whoever edits ui/src must `cd ui && npm run build` and commit
# ui/dist/index.html in the same commit. install.sh only copies the committed artifact — no npm required.
# Issue #53 AC-53-04: host cp into the bind source dirs; no docker cp / docker exec.
# 2026-10-18 (s1-req #53 C): hash compare / version stamp dropped — install.sh reports
# what it copied, verify.sh is the place for byte-level checks.
UI_DIST_HTML="ui/dist/index.html"

# s2b-fix-stamp (2026-10-18, TEST60-62 §6.1): 重写 .stamp.json, 与复制过去的
# index.html 实测字节严格一致。console.py _ui_stamp() 按优先级读
#   <MW_CONSOLE_HTML>.stamp.json → /share_workspace/ui_dist/.stamp.json
#   → mw/ui_dist/.stamp.json
# 而 do_GET "/" 用该 stamp 的 build_md5 逐个校验候选 HTML —— stamp 陈旧或缺失
# 时所有候选被剔除, console 静默回退 console_ui.py 兜底页 (无报错)。
# 字段名与 _ui_stamp()/TEST60-62 §6.1 手修样例对齐: build_md5/build_size/
# installed_at/commit_short (build_size 为数字)。
_write_ui_stamp() {
  # $1 = 目标 ui_dist 目录 (stamp 写 <dir>/.stamp.json, md5/size 实测 <dir>/index.html)
  local _dir="$1" _md5="" _size=0
  # BSD/macOS: md5 -r; Linux coreutils: md5sum。两平台都要能跑, 取先命中的。
  if command -v md5 >/dev/null 2>&1; then
    _md5=$(md5 -r "${_dir}/index.html" | awk '{print $1}')
  elif command -v md5sum >/dev/null 2>&1; then
    _md5=$(md5sum "${_dir}/index.html" | awk '{print $1}')
  fi
  # 大小: BSD stat -f %z 先, GNU stat -c %s 次, wc -c 兜底 (tr 去 BSD wc 的前导空格)。
  # 探测必须走「捕获输出判成功」: set -e 下裸跑失败命令会直接终止脚本。
  if _sz=$(stat -f %z "${_dir}/index.html" 2>/dev/null); then :
  elif _sz=$(stat -c %s "${_dir}/index.html" 2>/dev/null); then :
  else _sz=$(wc -c < "${_dir}/index.html" | tr -d '[:space:]'); fi
  _size="${_sz}"
  # commit_short: 无 git 环境 / 非 git 目录时置空串, 不阻断安装。
  local _commit _now
  _commit=$(git rev-parse --short HEAD 2>/dev/null || true)
  # ISO8601: GNU date -Iseconds 先, BSD date 等价格式兜底。
  _now=$(date -Iseconds 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S%z')
  printf '{\n  "commit_short": "%s",\n  "build_md5": "%s",\n  "build_size": %s,\n  "installed_at": "%s"\n}\n' \
    "${_commit}" "${_md5}" "${_size}" "${_now}" > "${_dir}/.stamp.json"
}

if [ -f "${UI_DIST_HTML}" ]; then
  mkdir -p mw/ui_dist
  cp "${UI_DIST_HTML}" mw/ui_dist/index.html
  # stamp 必须紧跟 cp 重写, 否则 console serve "/" 时 md5 对不上 → 静默兜底页。
  _write_ui_stamp mw/ui_dist \
    || say "⚠ Failed to refresh mw/ui_dist/.stamp.json — console may fall back to the built-in page" \
           "⚠ mw/ui_dist/.stamp.json 刷新失败 — 控制台可能回退内置兜底页"
  say "Console frontend ready (mw/ui_dist/index.html + .stamp.json)" "控制台前端就绪 (mw/ui_dist/index.html + .stamp.json)"
  # 2026-10-10: console.py serves /share_workspace/ui_dist/index.html with HIGHER priority than
  # mw/ui_dist (worker nightly-build channel). The container bind-mounts
  # ./data/share_workspace -> /share_workspace (main.compose.yaml), so a host cp IS the container cp.
  if mkdir -p data/share_workspace/ui_dist 2>/dev/null && cp "${UI_DIST_HTML}" data/share_workspace/ui_dist/index.html 2>/dev/null; then
    # s2b-fix-stamp: 此侧 stamp 同样必须刷新 —— _ui_stamp() 优先读
    # /share_workspace/ui_dist/.stamp.json, 若残留 worker 夜间构建的旧 stamp,
    # do_GET "/" 会拿旧 md5 校验所有候选(含刚 cp 的两份新 HTML)全部剔除 → 兜底页。
    _write_ui_stamp data/share_workspace/ui_dist \
      || say "⚠ Failed to refresh data/share_workspace/ui_dist/.stamp.json — console may fall back to the built-in page" \
             "⚠ data/share_workspace/ui_dist/.stamp.json 刷新失败 — 控制台可能回退内置兜底页"
    say "synced data/share_workspace/ui_dist/index.html + .stamp.json (= container /share_workspace/ui_dist via bind mount)" \
        "已同步 data/share_workspace/ui_dist/index.html + .stamp.json (= 容器内 /share_workspace/ui_dist, 同一 bind mount)"
  else
    say "⚠ Could not refresh data/share_workspace/ui_dist/ (permission? run: sudo chown -R \$USER data/share_workspace) — console may serve a stale worker-built dist" \
        "⚠ data/share_workspace/ui_dist/ 刷新失败 (权限? 试: sudo chown -R \$USER data/share_workspace) — 控制台可能仍 serve worker 夜间构建的旧 dist"
  fi
else
  if [ -f mw/ui_dist/index.html ]; then
    say "⚠ ${UI_DIST_HTML} not in repo (looks like old baked-in dist) — falling back to mw/ui_dist/index.html" "⚠ 仓库无 ${UI_DIST_HTML}(像 baked-in 老 dist) — 退回 mw/ui_dist/index.html"
    say "    Hint: cd ui && npm run build, then re-run install.sh" "    提示: cd ui && npm run build, 再跑 install.sh"
  else
    say "⚠ No ${UI_DIST_HTML} and mw/ui_dist does not exist either — console will fall back to console_ui.py (HTML string constants, very old version)" "⚠ 缺 ${UI_DIST_HTML} 且 mw/ui_dist 也不存在 — 控制台回退 console_ui.py (HTML 字符串常量, 极老版)"
  fi
fi

# ── 4. Seed registry (console-writable copy lives on the ledger volume) ──────
# Issue #53 AC-53-04: host cp only — bind mount 把文件带进容器.
[ -f data/mw_data/registry.json ] || cp mw/registry.json data/mw_data/registry.json
chmod 666 data/mw_data/registry.json 2>/dev/null || \
  say "⚠ registry.json chmod failed — root inside the container can already write; no impact on operation" "⚠ registry.json 权限 chmod 失败 — 容器内 root 已可写, 不影响运行"
say "registry ready: data/mw_data/registry.json" "registry 就绪: data/mw_data/registry.json"

# ── 5. Generate console settings (per this host's deploy dirs, first deploy only; existing kept) ──
# Issue #53 AC-53-04: 启动前宿主侧直写; settings.json "existing kept" 语义保持.
if [ ! -f data/mw_data/settings.json ]; then
  ROOT="$(pwd)"
  cat > data/mw_data/settings.json <<EOF
{
  "shared_workspace_host": "${ROOT}/data/share_workspace",
  "docker_compose_dir": "${ROOT}/worker_compose",
  "default_timeout_seconds": 1800,
  "reaper_interval_seconds": 15,
  "default_image": "${HERMES_IMAGE}",
  "hermes_webui_url": ""
}
EOF
  chmod 666 data/mw_data/settings.json 2>/dev/null || \
    say "⚠ settings.json chmod failed — root inside the container can already write; no impact on operation" "⚠ settings.json 权限 chmod 失败 — 容器内 root 已可写, 不影响运行"
  say "Settings generated for this host: shared volume host path=${ROOT}/data/share_workspace" "设置已按本机生成: 共享卷宿主机路径=${ROOT}/data/share_workspace"
else
  say "settings.json already exists; keeping existing config" "settings.json 已存在, 保留现有配置"
fi

# ── 6. Skills + SOUL staging (m1-impl macOS: 目录挂载, 宿主生成 .runtime-* 暂存) ──
# Issue #53 R3 + s4d-fix2 + m1-impl: Apple `container` 的 bind 源必须是目录
# (Parser.swift `guard isDirectory`, docker shim 透传不改写), 所以 s4d-fix2 的
# 文件级 :ro 挂载只在 docker 上成立。新方案: 本段把当前 LANG 的源文件
# (deploy/skills/<sk>/SKILL.<LANG>.md / docs/main-soul.<LANG>.md) 重新生成到
# 宿主自有的暂存目录 deploy/.runtime-skills/<sk>/SKILL.md 与
# deploy/.runtime-soul/SOUL.md (gitignore, 每次 install 全量重建), compose 再做
# 目录级 :ro 挂载 — 语义与文件级完全等价, verify.sh 指纹路径不变。
# 暂存目录属宿主用户且容器侧 :ro 从不写 → BUG-53-A 属主陷阱保持根除。
# 本段同时做预检: 缺源文件先一行 fail, 比 compose up 的 volume 报错明确。
SKILL_NAMES="mw-task-dispatch team-work-chain-advance team-work-chain-orchestration worker-message-relay"
RUNTIME_SKILLS="deploy/.runtime-skills"
RUNTIME_SOUL="deploy/.runtime-soul"
# 全量重建暂存 (rm -rf 宿主自有目录无权限问题; 容器从不写这里)
rm -rf "${RUNTIME_SKILLS}" "${RUNTIME_SOUL}"
for sk in ${SKILL_NAMES}; do
  SKILL_SRC="deploy/skills/${sk}/SKILL.${SAS_LANG}.md"
  [ -f "${SKILL_SRC}" ] || fail "missing skill source ${SKILL_SRC} (LANG=${SAS_LANG})" "缺技能源文件 ${SKILL_SRC} (LANG=${SAS_LANG})"
  mkdir -p "${RUNTIME_SKILLS}/${sk}" \
    || fail "cannot create staging dir ${RUNTIME_SKILLS}/${sk}" "无法创建暂存目录 ${RUNTIME_SKILLS}/${sk}"
  cp "${SKILL_SRC}" "${RUNTIME_SKILLS}/${sk}/SKILL.md" \
    || fail "cannot stage ${SKILL_SRC} -> ${RUNTIME_SKILLS}/${sk}/SKILL.md" "无法生成暂存 ${SKILL_SRC} -> ${RUNTIME_SKILLS}/${sk}/SKILL.md"
done
SOUL_SRC="docs/main-soul.${SAS_LANG}.md"
[ -f "${SOUL_SRC}" ] || fail "missing soul source ${SOUL_SRC} (LANG=${SAS_LANG})" "缺 SOUL 源文件 ${SOUL_SRC} (LANG=${SAS_LANG})"
mkdir -p "${RUNTIME_SOUL}" \
  || fail "cannot create staging dir ${RUNTIME_SOUL}" "无法创建暂存目录 ${RUNTIME_SOUL}"
cp "${SOUL_SRC}" "${RUNTIME_SOUL}/SOUL.md" \
  || fail "cannot stage ${SOUL_SRC} -> ${RUNTIME_SOUL}/SOUL.md" "无法生成暂存 ${SOUL_SRC} -> ${RUNTIME_SOUL}/SOUL.md"
say "Skill + SOUL staged for LANG=${SAS_LANG}: ${SKILL_NAMES} + main SOUL -> deploy/.runtime-{skills,soul}/ (directory bind mounts, read-only in container)" \
    "技能 + SOUL 暂存已按 LANG=${SAS_LANG} 生成: ${SKILL_NAMES} + 主 SOUL -> deploy/.runtime-{skills,soul}/ (目录 bind 挂载, 容器内只读)"

# ── 6.5 --preserve-soul (kept for back-compat; behavior changed) ───────────
# s4d-fix2 起 SOUL.md 由仓库源只读 bind 挂载, 容器里没有用户可原地改的 data/main/SOUL.md;
# 用户要保留自定义 SOUL 应改 docs/main-soul.<LANG>.md 再跑 install.sh. --preserve-soul 仅吞掉,
# 不影响 bind 挂载结果.
if [ "${1:-}" = "--preserve-soul" ]; then
  say "--preserve-soul is a no-op after s4d-fix2 (skills/SOUL are bind-mounted read-only from the repo); ignoring" \
      "--preserve-soul 在 s4d-fix2 后无效果 (技能/SOUL 已只读 bind 挂载自仓库); 忽略"
fi

# ── 7. Start container (compose up) ────────────────────────────
# 2026-10-18 (s1-req #53 C): the 3-layer container-CLI probe is removed, macOS 走
# docker-for-apple-container 的 docker shim 直接敲 `docker`. compose v2 子命令同语义.
# Image readiness: `image inspect`, fall back to pull. 失败一行报错, 不展开排障.
if docker image inspect "${HERMES_IMAGE}" >/dev/null 2>&1; then
  say "Image already cached locally; skipping pull" "镜像已存在本地, 跳过拉取"
else
  say "Pulling image ${HERMES_IMAGE} ..." "拉取镜像 ${HERMES_IMAGE} ..."
  docker pull "${HERMES_IMAGE}" || fail "image pull failed and no local cache — check the network, or change HERMES_IMAGE in .env (China mirror: swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/nousresearch/hermes-agent:v2026.9.24)" "镜像拉取失败且本地无缓存 — 检查网络, 或在 .env 换 HERMES_IMAGE(国内镜像: swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/nousresearch/hermes-agent:v2026.9.24)"
fi
# ── 7b. Seed the main named volume (2026-10-22, issue #82) ─────────
# The image is confirmed present above, so the seeding helper (the deploy
# image itself) never needs a registry round-trip.
MAIN_PROJECT="mw-${MAIN_CONTAINER_NAME}"
MAIN_VOLUME="${MAIN_PROJECT}_${MAIN_VOLUME_KEY}"
if ! docker volume inspect "${MAIN_VOLUME}" >/dev/null 2>&1; then
  docker volume create "${MAIN_VOLUME}" >/dev/null 2>&1 \
    || fail "Could not create the main volume ${MAIN_VOLUME}" "无法创建主卷 ${MAIN_VOLUME}"
fi
_seed_main_volume "${MAIN_VOLUME}"

# The gateway runs as uid 10000 (hermes). A volume seeded from the host copy
# carries the host's ownership, and directories the image creates as root
# during bootstrap (e.g. pending_messages/, runtime/bot_live_delivery/) stay
# root-owned — the gateway then logs "Operation not permitted" for them and
# silently drops bot/mailbox deliveries. Fix the ownership inside the volume
# with the deploy image itself (already cached, no extra pull) and the same
# uid the container uses.
docker run --rm --entrypoint sh --user 0:0 -v "${MAIN_VOLUME}:/vol" "${HERMES_IMAGE}" \
  -c 'chown -R 10000:10000 /vol' >/dev/null 2>&1 \
  || say "⚠ could not fix ownership inside ${MAIN_VOLUME}" \
         "⚠ 无法修正卷 ${MAIN_VOLUME} 内的属主"

# 2026-10-01 (revised by m1-impl macOS): recreate is done via stop+rm instead of
# a compose recreate flag — the Apple container docker shim's subcommand set
# does not include the recreate flag, while stop/rm are universally supported.
# install.sh always runs stop+rm when a container exists (not only leftovers):
# compose up would keep a same-project container running and would NOT apply a new
# mount topology, so stop+rm-then-up IS the recreate semantic on both engines.
if docker ps -a --format '{{.Names}}' | grep -qx "${MAIN_CONTAINER_NAME}"; then
  say "Existing container ${MAIN_CONTAINER_NAME}: stop+rm first (recreate via stop/rm, no compose recreate flag needed)" "现存容器 ${MAIN_CONTAINER_NAME}: 先 stop+rm (重建由 stop/rm 兜底, 无需 compose 重建参数)"
  docker stop "${MAIN_CONTAINER_NAME}" >/dev/null 2>&1 || true
  docker rm "${MAIN_CONTAINER_NAME}" >/dev/null 2>&1 || true
fi

# 2026-10-01: docker compose v2 versions disagree on the base for resolving relative paths in compose files
# (some resolve against project-directory, some against the directory of the first -f file).
# Fix: install.sh generates a temp main.compose.abs.yaml (all relative paths made absolute),
#       runs docker compose with it so the bind mount is guaranteed to take effect,
#       leaving the original main.compose.yaml (repo source file) untouched.
# m1-impl (2026-10-18 macOS): the old §8 step 2 language-sentinel sed (which
#       rewrote the compose bind-source literals to the active language's
#       suffixed file) is REMOVED — the directory-mount topology no longer
#       carries language sentinels: install.sh §6 stages the active-LANG files
#       into deploy/.runtime-{skills,soul}/ so the compose bind sources are
#       language-neutral. One less in-place rewrite, one less macOS sed
#       portability surface.
ABS_COMPOSE="$(mktemp "${TMPDIR:-/tmp}/main.compose.abs.XXXXXX")"
trap 'rm -f "${ABS_COMPOSE}"' EXIT
# Rewrite relative ./xxx paths into absolute ${PWD}/xxx (bind sources must resolve
# regardless of which base dir the compose CLI picks).
sed -E "s|\\./([a-zA-Z0-9_./-]+)|${PWD}/\\1|g" deploy/main.compose.yaml > "${ABS_COMPOSE}"
say "Generated absolute-path compose: ${ABS_COMPOSE} (LANG=${SAS_LANG} applied via staging dirs)" "生成绝对路径 compose: ${ABS_COMPOSE} (LANG=${SAS_LANG} 已经由暂存目录应用)"
# 2026-10-02 critical fix: the project name must be bound to MAIN_CONTAINER_NAME.
# Running a second deploy (e.g. a scratch drill with a different container name) under the fixed
# project name 'hermes-agents' made the rebuilt containers owned by service 'hermes'
# in that project — measured: it replaced production hermes_main. With the container name in the project name, deploys stay invisible to each other.
docker compose --project-name "${MAIN_PROJECT}" \
  --project-directory "${PWD}" -f "${ABS_COMPOSE}" up -d \
  || fail "docker compose up failed — see 'docker logs ${MAIN_CONTAINER_NAME}' for the real error" \
          "docker compose up 失败 — 跑 'docker logs ${MAIN_CONTAINER_NAME}' 看真实错误"
say "✓ Compose up done; main agent deploy complete" "✓ Compose up 完成, 主代理部署完成"

# ── 8. Wrap-up ─────────────────────────────────────────────
IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo 127.0.0.1)
say "✅ main agent deploy complete" "✅ main agent 部署完成"
# I18N-R3 (2026-10-16, REQ §4.3): wrap-up 双语 say (en/zh) —— LANG=en 时零中文, LANG=zh-cn 时维持原文.
say '──────────────────────────────────────────────' '──────────────────────────────────────────────'
say '  Web console:    http://'"${IP}"':'"${CONSOLE_PORT}"'/' \
    '  Web 控制台:    http://'"${IP}"':'"${CONSOLE_PORT}"'/'
say '  gateway API:    http://'"${IP}"':'"${GW_PORT:-8642}"'/' \
    '  gateway API:    http://'"${IP}"':'"${GW_PORT:-8642}"'/'
say '  Next: open the console → "Worker management" to add a worker (generate compose → start container → register),' \
    '  下一步: 控制台『Worker 管理』添加 worker(生成 compose → 起容器 → 注册),'
say '         or add a worker manually per DEPLOY.md §3.' \
    '         或按 DEPLOY.md §3 手动加 worker。'
say '  Verify deploy:  ./deploy/verify.sh' \
    '  验证部署:       ./deploy/verify.sh'
