#!/usr/bin/env bash
# Simple-Agents-Studio — deployment self-check
# Usage: run  ./deploy/verify.sh  from the repo root
# Read-only checks, changes nothing. All green = deployment usable.
# i18n (2026-10-14): loads deploy/lib/i18n.sh for bilingual messages and LANG validation (report-style,
# no exit, consistent with verify.sh's long-standing read-only semantics).
set -uo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

# ── i18n v1.0: load bilingual helpers + LANG validation (arch §3.2/§4.4) ──
LIB_DIR="$(cd "$(dirname "$0")" && pwd)/lib"
# shellcheck source=lib/i18n.sh
. "${LIB_DIR}/i18n.sh"
# issue #113 (2026-10-25): 跨平台 hash helper (hash_of md5/sha256)。
# 宿主侧指纹必须走 hash_of 而不是裸 sha256sum —— macOS/BSD 没有
# sha256sum (只有 shasum -a 256), 裸调用会让宿主侧指纹算成空串,
# verify.sh 误报「SOUL/SKILL 内容陈旧」(issue #113 归因到的真 bug)。
# shellcheck source=lib/hash.sh
. "${LIB_DIR}/hash.sh"

[ -f .env ] && { set -a; . ./.env; set +a; }
# i18n (arch §3.2 / AC C1): validate LANG=en|zh-cn; empty = en; invalid -> bad line (no exit,
# same report-style as the rest of verify.sh). Unlike install.sh's fail-fast.
SAS_LANG="${LANG:-en}"
if ! i18n_validate "${SAS_LANG}" "LANG"; then
  SAS_LANG_VALID=0
else
  SAS_LANG_VALID=1
fi
export SAS_LANG
MAIN_CONTAINER_NAME="${MAIN_CONTAINER_NAME:-hermes_main}"
CONSOLE_PORT="${CONSOLE_PORT:-8700}"
# issue #109 (2026-10-26): GW_PORT 宿主映射已删, gateway 只在容器内 :8642;
# 检查改走 `docker exec` 容器内探测 (见下方 gateway 段)。

PASS=0; FAIL=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }

# Section banner with bilingual fallback (SAS_LANG picks en|zh-cn).
# Definition lives close to ok/bad so future reads naturally.
section_banner() {
  if [ "${SAS_LANG}" = "zh-cn" ]; then
    printf '== %s ==\n' "$2"
  else
    printf '== %s ==\n' "$1"
  fi
}


# i18n LANG report item
if [ "${SAS_LANG_VALID}" = "1" ]; then
  ok "LANG=${SAS_LANG} (allowed: en|zh-cn)"
else
  bad "Invalid LANG='${SAS_LANG}' (architecture §3.2): allowed values en (default) | zh-cn" "LANG='${SAS_LANG}' 非法(架构 §3.2): 允许值 en (default) | zh-cn"
fi

section_banner "Containers" "容器"
docker inspect "${MAIN_CONTAINER_NAME}" --format '{{.State.Status}}' 2>/dev/null | grep -q running \
  && ok "Container ${MAIN_CONTAINER_NAME} running" "容器 ${MAIN_CONTAINER_NAME} 运行中" || bad "Container not running (docker logs ${MAIN_CONTAINER_NAME})" "容器未运行 (docker logs ${MAIN_CONTAINER_NAME})"

section_banner "Console (:${CONSOLE_PORT})" "控制台 (:${CONSOLE_PORT})"
curl -sf --max-time 5 "http://127.0.0.1:${CONSOLE_PORT}/health" | grep -q ok \
  && ok "Console healthy" "控制台健康" || bad "Console not responding" "控制台无响应"
curl -sf --max-time 5 "http://127.0.0.1:${CONSOLE_PORT}/api/gwconfig" | grep -q '"proxy": *true' \
  && ok "Same-origin proxy ready" "同源代理就绪" || bad "gwconfig anomaly (check console version)" "gwconfig 异常(检查 console 版本)"
curl -sf --max-time 5 "http://127.0.0.1:${CONSOLE_PORT}/api/tasks" >/dev/null \
  && ok "Task API readable" "任务 API 可读" || bad "Task API failed" "任务 API 失败"

section_banner "reaper" "reaper"
curl -sf --max-time 5 "http://127.0.0.1:${CONSOLE_PORT}/api/workers" | grep -q reaper_alive \
  && ok "reaper heartbeat visible" "reaper 心跳可见" || bad "reaper not reporting" "reaper 未上报"

section_banner "gateway (container-internal :8642)" "gateway (容器内网 :8642)"
# issue #109 (2026-10-26): gateway 不再发布宿主端口 (原 GW_PORT:8642 已删),
# 宿主侧 curl 127.0.0.1:8642 必然 connection refused。检查改为容器内探测:
# gateway 在容器内仍监听 0.0.0.0:8642, 与 main<->worker 实际通信路径同构。
# 容器内无 curl 依赖保证, 用镜像自带的 python3 urllib。
GW_OK=0
for i in 1 2 3 4 5; do
  docker exec -e GWKEY="${API_SERVER_KEY:-}" "${MAIN_CONTAINER_NAME}" python3 -c '
import os, urllib.request
req = urllib.request.Request(
    "http://127.0.0.1:8642/api/sessions", data=b"{}", method="POST",
    headers={"Authorization": "Bearer " + os.environ.get("GWKEY", ""),
             "Content-Type": "application/json"})
print(urllib.request.urlopen(req, timeout=5).read().decode())
' 2>/dev/null | grep -q '"id"' \
    && { GW_OK=1; break; }
  sleep 2   # gateway may become ready a few seconds after the console
done
[ "$GW_OK" = 1 ] && ok "gateway session creation OK (probed inside ${MAIN_CONTAINER_NAME})" "gateway 会话创建 OK (在 ${MAIN_CONTAINER_NAME} 容器内探测)" || bad "gateway unreachable inside container or key mismatch (check .env API_SERVER_KEY matches container env)" "容器内 gateway 不可达或密钥不符 (检查 .env API_SERVER_KEY 是否跟容器 env 一致)"

section_banner "kanban" "kanban"
docker exec "${MAIN_CONTAINER_NAME}" sh -c \
  'test -f /opt/data/kanban.db && echo K' 2>/dev/null | grep -q K \
  && ok "kanban.db present" "kanban.db 存在" || bad "kanban not initialized (run 'hermes kanban init' or re-create container)" "kanban 未初始化 (运行 hermes kanban init 或重建容器)"

section_banner "Distributed SKILL" "分发 SKILL"
# Note: do not use `docker exec ... | grep -q` — under pipefail, grep exiting early makes
# the upstream docker exec die with SIGPIPE(141) and the whole pipeline falsely fails. Capture first, then match.
# Note: do not substring-match on `hermes skills list` rendered text — the rich table's Name column
# is 25 chars wide, so long skill names (team-work-chain-orchestration, 29 chars) get truncated
# to "team-work-chain-orches…" and produce a false "missing". Check SKILL.md file existence instead.
for sk in mw-task-dispatch team-work-chain-advance team-work-chain-orchestration worker-message-relay; do
  if docker exec -u hermes "${MAIN_CONTAINER_NAME}" \
      test -f "/opt/data/skills/agent-matrix/${sk}/SKILL.md" 2>/dev/null; then
    ok "${sk} installed" "${sk} 已装"
  else
    bad "${sk} missing" "${sk} 缺失"
  fi
done

# Content fingerprint check (2026-10-03): the host repo's SOUL/SKILL must match what's deployed
# inside the container, otherwise the main agent runs a stale version (after the reaper change,
# an old SOUL still says "worker edits the result field" and the worker never writes the status field -> the reaper never
# settles the task).
# i18n (2026-10-14): now reads docs/main-soul.${SAS_LANG}.md + deploy/skills/<sk>/SKILL.${SAS_LANG}.md
# With no .env configured, SAS_LANG already defaulted to en earlier.
# s4d-fix2 (2026-10-17): the container side now reads the in-container paths where the
# bind mount overlays the repo source directly (no host-side copy). The fingerprint
# logic is unchanged: compare host source sha256 to container in-place sha256 — when
# they match, the bind mount picked up the right file. A mismatch indicates either
# a wrong LANG in .env or a stale container (didn't pick up the latest repo source).
SKILL_FINGERPRINT_HOST=$( \
  { hash_of sha256 "deploy/skills/mw-task-dispatch/SKILL.${SAS_LANG}.md"; \
    hash_of sha256 "deploy/skills/team-work-chain-advance/SKILL.${SAS_LANG}.md"; \
    hash_of sha256 "deploy/skills/team-work-chain-orchestration/SKILL.${SAS_LANG}.md"; \
    hash_of sha256 "deploy/skills/worker-message-relay/SKILL.${SAS_LANG}.md"; \
    hash_of sha256 "docs/main-soul.${SAS_LANG}.md"; } 2>/dev/null \
  | tr -d '\n' \
  | hash_of sha256 - \
  )
# deployed fingerprint: sha256 each file inside the container, concatenated host-side in the same order.
# s4d-fix2 + m1-impl: the in-container /opt/data/skills/agent-matrix/<sk>/SKILL.md and
# /opt/data/SOUL.md come from directory-level read-only bind mounts of the host staging
# dirs deploy/.runtime-skills/<sk>/ and deploy/.runtime-soul/ (which install.sh regenerates
# each run from deploy/skills/<sk>/SKILL.<LANG>.md and docs/main-soul.<LANG>.md; the entry
# script symlinks /opt/data/SOUL.md to /opt/soul-src/SOUL.md). So the sha here should
# always equal the host sha, unless the
# container is on an older image that didn't pick up the new bind mounts (in which case
# a re-run of ./deploy/install.sh — which stop+rm's then composes up — will refresh).
# Note: the old `docker exec ... || true | awk ...` form — in bash, | binds tighter
# than ||, so it parses as docker exec || (true | awk ...); when docker exec succeeds
# the awk pipeline never runs and the variable gets the raw output. Fixed by wrapping the whole pipeline
# in a set +e subshell: on failure the output is empty and the variable is set to an empty string.
SKILL_FINGERPRINT_DEPLOYED=$(set +e; \
  docker exec "${MAIN_CONTAINER_NAME}" sh -c \
    'sha256sum /opt/data/skills/agent-matrix/mw-task-dispatch/SKILL.md /opt/data/skills/agent-matrix/team-work-chain-advance/SKILL.md /opt/data/skills/agent-matrix/team-work-chain-orchestration/SKILL.md /opt/data/skills/agent-matrix/worker-message-relay/SKILL.md /opt/data/SOUL.md 2>/dev/null' \
    | awk '{print $1}' \
    | tr -d '\n' \
    | hash_of sha256 - \
  )
# sha256 of empty input (fallback: don't falsely report a match when the container is down / files absent)
EMPTY_SHA="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
if [ -z "${SKILL_FINGERPRINT_DEPLOYED}" ]; then
  SKILL_FINGERPRINT_DEPLOYED="${EMPTY_SHA}"
fi
if [ "${SKILL_FINGERPRINT_HOST}" = "${SKILL_FINGERPRINT_DEPLOYED}" ]; then
  ok "SOUL/SKILL content fingerprint matches (${SKILL_FINGERPRINT_HOST:0:12}...)" "SOUL/SKILL 内容指纹一致 (${SKILL_FINGERPRINT_HOST:0:12}...)"
else
  bad "SOUL/SKILL content stale: host=${SKILL_FINGERPRINT_HOST:0:12} deployed=${SKILL_FINGERPRINT_DEPLOYED:0:12} — re-run install.sh" "SOUL/SKILL 内容陈旧: host=${SKILL_FINGERPRINT_HOST:0:12} deployed=${SKILL_FINGERPRINT_DEPLOYED:0:12} — 重跑 install.sh"
fi

# hermes-webui (optional): only checked when .env sets HERMES_WEBUI_DIR
if [ -n "${HERMES_WEBUI_DIR:-}" ]; then
  WPORT="${HERMES_WEBUI_PORT:-8787}"
  if curl -sf --max-time 5 "http://127.0.0.1:${WPORT}/health" >/dev/null 2>&1; then
    ok "hermes-webui online (:${WPORT})" "hermes-webui 在线 (:${WPORT})"
  else
    bad "hermes-webui not responding (:${WPORT}) — check docker logs ${MAIN_CONTAINER_NAME} for webui output" "hermes-webui 未响应 (:${WPORT}) — 查 docker logs ${MAIN_CONTAINER_NAME} 里的 webui 输出"
  fi
fi


echo
if [ "$FAIL" = 0 ]; then
  IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo 127.0.0.1)
  if [ "${SAS_LANG}" = "zh-cn" ]; then
    printf '\033[1;32m全部通过 (%s) — 打开 http://%s:%s/ 开始使用\033[0m\n' "$PASS" "$IP" "$CONSOLE_PORT"
  else
    printf '\033[1;32mall passed (%s) — open http://%s:%s/ to start\033[0m\n' "$PASS" "$IP" "$CONSOLE_PORT"
  fi
else
  if [ "${SAS_LANG}" = "zh-cn" ]; then
    printf '\033[1;31m%s 项失败 / %s 项通过 — 见 DEPLOY.md 故障排查\033[0m\n' "$FAIL" "$PASS"
  else
    printf '\033[1;31m%s failed / %s passed — see DEPLOY.md troubleshooting\033[0m\n' "$FAIL" "$PASS"
  fi
  exit 1
fi
