#!/usr/bin/env python3
"""
i18n docs sync check (post-converge release policy, 2026-10-15).

Verifies the release doc final-state required by the converge plan
(docs/repo-converge-20261015/converge-plan.md §3.3, decisions D-B):

1. The three root docs (README.md / DEPLOY.md / design.md) have CJK=0
   in non-code-block prose (F1). CJK is allowed only inside fenced or
   inline code blocks (the same carve-out as B2 for the templates).
   Root docs are the English authority; platform bilingual runtime
   strings live in mw/i18n/*.json and mw/soul_template.<lang>.md.
2. F3': the tracked docs/ tree must contain exactly the two deploy
   souls (docs/main-soul.en.md, docs/main-soul.zh-cn.md) — nothing
   else. This freezes the release doc final-state as a machine check.

The former F2 (zh-cn mirror existence / set integrity / no-loss) and
F4 (i18n note pointing at the retired zh-cn mirror dir) checks were
retired together with the zh-cn doc mirror layer (decision D-A).

Exit code:
  0 = all checks pass
  1 = violations found (printed)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The three root docs that must be English-only (F1, decision D-B).
ROOT_DOCS = ("README.md", "DEPLOY.md", "design.md")

# F3': the only files allowed to be tracked under docs/ (release final-state).
DOCS_TRACKED_EXACT = {"main-soul.en.md", "main-soul.zh-cn.md"}

CJK_RE = re.compile(r"[一-鿿]")
CODE_FENCE_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def _strip_code_blocks(content: str) -> str:
    """Remove fenced + inline code blocks for F1 CJK-outside-code check."""
    stripped = CODE_FENCE_RE.sub("", content)
    stripped = INLINE_CODE_RE.sub("", stripped)
    return stripped


def check_root_doc_cjk(path: Path) -> list[str]:
    """F1: root three docs must have CJK=0 outside code blocks."""
    violations = []
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"MISSING: {path}"]
    stripped = _strip_code_blocks(content)
    matches = list(CJK_RE.finditer(stripped))
    if matches:
        # Show first few offending positions
        examples = []
        for m in matches[:5]:
            line_no = stripped[: m.start()].count("\n") + 1
            examples.append(f"line ~{line_no}: '...'")
        violations.append(
            f"F1: {path.name} has {len(matches)} CJK char(s) outside code "
            f"blocks ({', '.join(examples)})"
        )
    return violations


def check_docs_tracked() -> list[str]:
    """F3': git ls-files docs must equal exactly the two soul files."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "docs"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        return [f"F3': cannot run git ls-files docs: {e}"]
    tracked = {
        Path(line).name for line in out.splitlines() if line.strip()
    }
    violations = []
    for extra in sorted(tracked - DOCS_TRACKED_EXACT):
        violations.append(
            f"F3': docs/{extra} is tracked but not in the release final-state "
            f"(only the two souls may live under docs/)"
        )
    for missing in sorted(DOCS_TRACKED_EXACT - tracked):
        violations.append(
            f"F3': deploy asset docs/{missing} missing from tracked docs"
        )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as failures",
    )
    args = parser.parse_args()

    violations: list[str] = []

    # F1: root doc CJK=0 outside code
    for name in ROOT_DOCS:
        violations.extend(check_root_doc_cjk(REPO_ROOT / name))

    # F3': docs/ tracked set == exactly the two souls
    violations.extend(check_docs_tracked())

    # Report
    if violations:
        print("i18n_docs_sync FAILED:")
        for v in violations:
            print(f"  - {v}")
        return 1

    print(
        "i18n_docs_sync OK: "
        "F1 CJK=0 (root 3), F3' docs tracked == 2 souls."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
