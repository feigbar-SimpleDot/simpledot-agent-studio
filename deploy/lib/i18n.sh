#!/bin/sh
# deploy/lib/i18n.sh — bilingual output helpers for deploy scripts (i18n v1.0, AR6, arch §4.4)
#
# Usage:
#   . "$LIB_DIR/i18n.sh"   # load
#   say "English text" "Chinese text"
#   fail "English" "Chinese"        # writes to stderr and exits 1
#
# Design:
# - POSIX sh compatible (main_entry.sh runs under /bin/sh; no bash features)
# - LANG uses the deploy-side SAS_LANG variable (avoids polluting POSIX LANG, arch §3.1)
# - Default en; after the container is up, main_entry.sh loads /mw_data/deploy_lang to override
# - Chinese strings stay in the script source (arch §4.4 note: C3 checks terminal output, not source)
#
# Note: each calling script (say/fail) has its own color prefix (e.g. [install]/[verify]);
# this file binds no prefix, it only picks the 1st/2nd argument by language. Callers wrap it:

# SAS_LANG: deploy language (default en, overridable by install.sh / main_entry.sh)
SAS_LANG="${SAS_LANG:-en}"

# _i18n_pick <en> <zh-cn> -> pick which argument (public helper; scripts wrap say/fail themselves)
# uses printf %s instead of echo to avoid \n / -e portability differences
_i18n_pick() {
  if [ "${SAS_LANG}" = "zh-cn" ]; then
    printf '%s' "$2"
  else
    printf '%s' "$1"
  fi
}

# i18n_say <en> <zh-cn> — emit to stdout (caller applies its own prefix/color)
i18n_say() {
  _i18n_pick "$1" "$2"
}

# i18n_fail <en> <zh-cn> — picks the language then writes to stdout; caller wraps prefix + >&2
# Design: no built-in >&2, because when `i18n_fail` is wrapped in `$(...)`, the inner
# `>&2` would bypass command-substitution capture and hit stderr directly, so
# (a) the error leaks early without the [install]/[verify] prefix; (b) `$(...)`
# captures an empty string and the later printf emits a blank line. Fix (2026-10-15 fix-review-p1p2
# P2-c): the function only returns the value; stderr routing is left to the caller's `printf ... >&2`.
# Callers need no extra context; layout/coloring/exit-code semantics stay the same.
i18n_fail() {
  _i18n_pick "$1" "$2"
}

# i18n_validate <value> <varname>
# validate the value against the allowed set {en|zh-cn}; on failure print to stderr and return 1
# Default en (arch §3.2 AC C1). Hard gate on .env LANG (AC C2 fail-fast).
i18n_validate() {
  v="${1:-}"
  name="${2:-LANG}"
  case "$v" in
    en|zh-cn)
      return 0
      ;;
    "")
      return 0   # empty = default en (AC C1)
      ;;
    *)
      i18n_fail \
        "Invalid ${name}='${v}' in .env — allowed: en (default) | zh-cn" \
        ".env 里的 ${name}='${v}' 非法 — 允许值: en(默认) | zh-cn" >&2
      return 1
      ;;
  esac
}