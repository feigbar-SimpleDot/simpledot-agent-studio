#!/usr/bin/env bash
# deploy/check_dist_sync.sh — issue #125 dist drift gate (route A)
#
# Usage:
#   bash deploy/check_dist_sync.sh      # from anywhere; the script cd's to the repo root
#   ./deploy/check_dist_sync.sh         # same (exec bit set)
#
# Exit code: 0 = all green, non-zero = at least one check is red.
#
# Contract (three checks; route-A background: DEPLOY.md console-frontend
# notes + deploy/install.sh §3 comment):
#   C1  mw/ui_dist/** must NOT be tracked by git.
#       Route A made mw/ui_dist a deploy-time copy of ui/dist; the repo keeps
#       exactly ONE tracked build artifact (ui/dist/index.html). A tracked
#       mw/ui_dist is the drift source of issue #125 coming back.
#   C2  Tri-state on the local runtime copy (issue #129):
#       - mw/ui_dist/index.html exists: its md5 must equal ui/dist/index.html's
#         md5. This catches BOTH drift directions: ui/dist moved on (runtime
#         copy lags) and runtime copy moved on (runtime copy is not what
#         ui/dist says it is).
#       - runtime copy missing but mw/ui_dist/.stamp.json present (install.sh
#         ran here = deployed): RED — the copy was deleted after deploy.
#       - neither exists: bare clone, not deployed yet — legitimately SKIPPED.
#   C3  ui/dist/index.html must exist — the one tracked build artifact must
#       never be lost.
#
# Why C2 keeps a SKIPPED state at all: under route A a bare clone (cloned but
# not deployed yet) legitimately has NO mw/ui_dist at all — a valid state the
# gate must not paint red. The .stamp.json install.sh writes at deploy time is
# what separates that valid state from "deployed but the runtime copy went
# missing", which is a violation (issue #129).
#
# Hashing goes through deploy/lib/hash.sh (hash_of md5), never a bare md5sum:
# macOS/BSD ships no md5sum (issue #113) — a bare call yields an empty string
# and a bogus green/red verdict.
#
# i18n: bilingual output via deploy/lib/i18n.sh (same loading style as
# install.sh / verify.sh); SAS_LANG=en|zh-cn picks the language.
set -uo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

LIB_DIR="$(cd "$(dirname "$0")" && pwd)/lib"
# shellcheck source=lib/i18n.sh
. "${LIB_DIR}/i18n.sh"
# shellcheck source=lib/hash.sh
. "${LIB_DIR}/hash.sh"

# say <en> [zh-cn] — informational line on stdout
say() {
  if [ $# -ge 2 ]; then
    printf '%s\n' "$(i18n_say "$1" "$2")"
  else
    printf '%s\n' "$1"
  fi
}

# bad <en> [zh-cn] — red line on stderr (detail + actionable fix instruction)
bad() {
  if [ $# -ge 2 ]; then
    printf '%s\n' "$(i18n_fail "$1" "$2")" >&2
  else
    printf '%s\n' "$1" >&2
  fi
}

UI_DIST="ui/dist/index.html"
RT_DIST="mw/ui_dist/index.html"
# install.sh 部署时写 stamp; stamp 在 = 本机跑过 install (已部署), 是 #129
# 区分「裸 clone 合法态」与「部署后副本被删」的判据。
RT_STAMP="mw/ui_dist/.stamp.json"
FAIL=0

say "dist sync gate (#125) — repo root: $(pwd)" "dist 同步门禁 (#125) — 仓库根: $(pwd)"

# ── C1: mw/ui_dist must not be tracked ─────────────────────────────────────
# `git ls-files -- mw/ui_dist` lists every tracked file under that path;
# a non-empty list means the runtime copy got committed back (route A
# violation). In a non-git directory there is no index to violate, so the
# check is skipped instead of falsely reported.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  tracked=$(git ls-files -- "mw/ui_dist")
  if [ -n "${tracked}" ]; then
    bad "C1 RED: mw/ui_dist is tracked again (route A violation): ${tracked}" \
        "C1 红: mw/ui_dist 又被纳入版本控制 (违反路线 A): ${tracked}"
    bad "     fix: git rm --cached -r mw/ui_dist  (and keep the .gitignore rule for mw/ui_dist/)" \
        "     修法: git rm --cached -r mw/ui_dist  (并保留 .gitignore 里 mw/ui_dist/ 的规则)"
    FAIL=1
  else
    say "C1 OK: mw/ui_dist is not tracked (repo keeps only ui/dist)" \
        "C1 通过: mw/ui_dist 未入库 (仓库只保留 ui/dist)"
  fi
else
  say "C1 SKIPPED: not a git work tree — nothing can be tracked here" \
      "C1 跳过: 非 git 工作树 — 此处不存在入库物"
fi

# ── C2: local runtime copy must not drift from ui/dist (tri-state, #129) ───
if [ -f "${RT_DIST}" ] && [ -f "${UI_DIST}" ]; then
  ui_md5=$(hash_of md5 "${UI_DIST}" 2>/dev/null || true)
  rt_md5=$(hash_of md5 "${RT_DIST}" 2>/dev/null || true)
  if [ -z "${ui_md5}" ] || [ -z "${rt_md5}" ]; then
    bad "C2 RED: could not hash the dist files (no md5sum/md5 on PATH?)" \
        "C2 红: 无法计算 dist 文件哈希 (PATH 上没有 md5sum/md5?)"
    FAIL=1
  elif [ "${ui_md5}" != "${rt_md5}" ]; then
    bad "C2 RED: dist drift — ui/dist=${ui_md5} vs runtime copy=${rt_md5}" \
        "C2 红: dist 漂移 — ui/dist=${ui_md5} 与运行时副本=${rt_md5} 不一致"
    bad "     fix: run ./deploy/install.sh to rebuild the local runtime copy from ui/dist" \
        "     修法: 跑 ./deploy/install.sh, 从 ui/dist 重建本地运行时副本"
    FAIL=1
  else
    say "C2 OK: runtime copy matches ui/dist (md5=${ui_md5})" \
        "C2 通过: 运行时副本与 ui/dist 一致 (md5=${ui_md5})"
  fi
elif [ -f "${RT_STAMP}" ] && [ -f "${UI_DIST}" ]; then
  # #129: stamp 由 install.sh 部署时写入 ⇒ 本机已部署; 此时运行时副本不在,
  # 是「部署后被误删」而非「裸 clone 未部署」, 必须报红 (旧版落 SKIPPED 漏报)。
  bad "C2 RED: deployed (stamp present) but runtime copy ${RT_DIST} is missing" \
      "C2 红: 已部署 (stamp 在) 但运行时副本 ${RT_DIST} 被删"
  bad "     fix: run ./deploy/install.sh to rebuild the runtime copy" \
      "     修法: 跑 ./deploy/install.sh 重建运行时副本"
  FAIL=1
else
  say "C2 SKIPPED: no local runtime copy yet (bare clone, not deployed) — nothing to drift" \
      "C2 跳过: 本地还没有运行时副本 (裸 clone, 未部署) — 无从漂移"
fi

# ── C3: the one tracked build artifact must exist ─────────────────────────
if [ -f "${UI_DIST}" ]; then
  say "C3 OK: ${UI_DIST} exists" "C3 通过: ${UI_DIST} 存在"
else
  bad "C3 RED: ${UI_DIST} is missing — the only tracked build artifact is gone" \
      "C3 红: ${UI_DIST} 不存在 — 唯一入库的构建产物丢了"
  bad "     fix: cd ui && npm run build, then commit the rebuilt ui/dist/index.html" \
      "     修法: cd ui && npm run build, 再提交重建后的 ui/dist/index.html"
  FAIL=1
fi

if [ "${FAIL}" -ne 0 ]; then
  say "dist sync gate: RED (see the C-lines above)" "dist 同步门禁: 红 (见上方各 C 项)"
  exit 1
fi
say "dist sync gate: GREEN" "dist 同步门禁: 绿"
exit 0
