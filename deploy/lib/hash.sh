#!/bin/sh
# deploy/lib/hash.sh — cross-platform hash helper (issue #113, 2026-10-25)
#
# Usage:
#   . "$LIB_DIR/hash.sh"
#   hash_of md5 <file>          # → "<hash>"
#   hash_of sha256 <file>       # → "<hash>"
#   hash_of sha256 -            # → "<hash>"  (read from stdin)
#
# Design:
# - POSIX sh compatible (main_entry.sh runs under /bin/sh; no bash features).
# - Single-arg form (hash_of <file>) = md5, kept for backward compatibility.
# - macOS/BSD ships `shasum -a 256` but NOT `sha256sum`; Linux coreutils ships
#   `sha256sum` but not `shasum` on minimal images. Probe both, first hit wins.
# - `md5 -r` (BSD) and `md5sum` (GNU) both print "<hash> <file>", so `awk '{print $1}'`
#   normalizes the output shape across platforms.
#
# Why this file exists (issue #113):
#   The original `hash_of()` lived in deploy/install.sh and was deleted by
#   f908906 (issue #53 C, install.sh trim). The two tests that guarded it
#   (test_r6_hash_of_via_bash / test_r6_install_sh_section_85_uses_hash_of)
#   then failed forever, because the helper they exercised no longer existed.
#   Meanwhile deploy/verify.sh kept using a BARE `sha256sum` for the SOUL/SKILL
#   fingerprint — on a macOS host (the documented alternative deploy platform)
#   that command does not exist, so the host-side fingerprint silently came out
#   EMPTY and verify.sh reported a bogus "content stale" mismatch.
#   Moving the helper into deploy/lib/ (shared by install.sh / verify.sh,
#   same house style as deploy/lib/i18n.sh; main_entry.sh does not consume
#   it today) restores the cross-platform contract AND fixes that live
#   macOS bug.
#
# Note: the container side is Linux, so `docker exec ... sha256sum` inside the
# container stays as-is (see deploy/verify.sh); only the HOST side needs the
# portable helper.

# hash_of [<algo>] <file> — print "<hash>" for <file>; "-" reads stdin.
# file='-' + algo=sha256/md5 => standard stdin semantics.
hash_of() {
  if [ $# -eq 1 ]; then
    algo="md5"; f="${1}"
  elif [ $# -eq 2 ]; then
    algo="${1}"; f="${2}"
  else
    echo "hash_of: usage: hash_of [<algo>] <file>" >&2
    return 1
  fi
  [ -n "${f}" ] || { echo "hash_of: missing argument" >&2; return 1; }
  case "${algo}" in
    md5)
      if command -v md5sum >/dev/null 2>&1; then
        if [ "${f}" = "-" ]; then
          md5sum | awk '{print $1}'
        else
          md5sum "${f}" | awk '{print $1}'
        fi
      elif command -v md5 >/dev/null 2>&1; then
        # macOS md5 -r prints "<hash> <file>"; same two-field shape, awk picks the hash.
        if [ "${f}" = "-" ]; then
          md5 -r | awk '{print $1}'
        else
          md5 -r "${f}" | awk '{print $1}'
        fi
      else
        echo "hash_of: neither md5sum nor md5 found on PATH" >&2
        return 1
      fi
      ;;
    sha256)
      if command -v sha256sum >/dev/null 2>&1; then
        if [ "${f}" = "-" ]; then
          sha256sum | awk '{print $1}'
        else
          sha256sum "${f}" | awk '{print $1}'
        fi
      elif command -v shasum >/dev/null 2>&1; then
        # macOS/BSD: shasum -a 256 prints "<hash> <file>"; same shape, awk picks hash.
        if [ "${f}" = "-" ]; then
          shasum -a 256 | awk '{print $1}'
        else
          shasum -a 256 "${f}" | awk '{print $1}'
        fi
      else
        echo "hash_of: neither sha256sum nor shasum found on PATH" >&2
        return 1
      fi
      ;;
    *)
      echo "hash_of: unsupported algo '${algo}' (md5|sha256)" >&2
      return 1
      ;;
  esac
}
