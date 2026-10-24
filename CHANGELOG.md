# Changelog

Release-policy note: this file records merged changes against `main`, newest
first. Prose is English per the repo root-doc convention (see
`tools/i18n_docs_sync.py` F1); commit subjects are quoted verbatim in code
spans. Process/review/test notes live outside the repo under the task
workspace `docs/` and are referenced by card id, not duplicated here.

## Unreleased — Issues #90 / #92: the `fmtApiError` residual net no longer misses keyword-adjacent shapes, and both counters share one comment-stripping rule (2026-10-24)

Range: two commits on `main` — `dbaa7fc` (`test(issue90+92): fmtApiError 覆盖网正则边界修复 + 残留/调用点计数对称化`) and `af8bc0d` (`docs(issue90+92): 补 FIX-NOTES 交付说明 — squash 边界保留归一方案取舍/改动面/变异 5/5 证据/遗留风险`; the `docs/FIX-NOTES-90-92.md` it added was later moved out of the git index by `20edc64` per the process-notes policy, so it is listed here for provenance only). Test-net only: the one file touched is `ui/tests/test_issue87_fmtapierror_coverage.mjs` (+80/-13); `ui/src`, `mw/` and `deploy/` are untouched. Chain: `REVIEW-88-89.md` §8 suggestions 2/3 (issues #92 / #90) → fix → targeted retest → review.

### Fixed

- **#90 (residual detection missed keyword-adjacent shapes)** — the residual
  regex requires a non-identifier character in front of the captured
  identifier (`(?:^|[^\w$])`), but the old `squash()` deleted *all*
  whitespace before matching, so a residual written flush against a keyword
  — `return e.message||e`, `await err.message||err`, `typeof ex.message||ex`,
  `new _e.message||_e`, `case e1.message||e1` — welded the keyword's last
  character onto the identifier into one continuous `\w` run, the boundary
  no longer held, and the old and the new regex missed the shape *together*
  (measured 0/5 hits). `squash()` now normalises with boundary preservation:
  only whitespace **between identifier characters** collapses to a `\0`
  sentinel, every other gap is still deleted. Two pre-existing guarantees
  are preserved, each pinned by a new case: the `e . message || e`
  whitespace-obfuscation escape stays blocked (PLAN §3.3 R4), and `a b`
  folds to `a\0b` instead of fusing into a fake word. The rejected
  alternative — normalising reserved keywords to boundary characters —
  would need a maintained keyword list where one missing keyword reopens
  the hole; the sentinel is keyword-agnostic by construction.
- **#92 (the two counters disagreed about comment lines)** — `countCalls`
  excluded whole-line comments but `countResidual` did not, so an example
  comment containing `x.message || x` would have made the residual anchor
  S2-b go falsely red (over-detection, fail-safe direction, but a trap for
  whoever writes docs next). Both counters now share one extracted
  `stripLineComments()` rule (comment lines replaced by empty strings so
  line numbers stay aligned for error messages). The two live residual
  anchors in `TasksBoardView` (`:1402`, `:1413`) were re-checked under the
  new rule — both still hit, i.e. neither is a comment — and a new case
  pins that premise so an anchor silently turning into a comment fails
  loudly instead of drifting.

### Changed

- **Regression coverage (5 → 8 cases)** — a positive matrix asserting all
  seven keyword-adjacent / whitespace-obfuscated / optional-chaining shapes
  are detected (reverting `squash` to delete-all-whitespace turns the whole
  matrix red); a false-positive matrix of eight legitimate shapes with zero
  hits plus the two `squash` semantics anchors; and a symmetry case for the
  shared comment-stripping rule (synthetic source with one real residual,
  one example residual comment, one real call, one example call comment —
  both counters must return 1, where the old residual side returned 2).
  Five deliberate mutations (squash reverted, residual side stops stripping,
  back-reference relaxed, detector hardcoded to 0, leading boundary removed)
  each turn the corresponding case red, so the assertions discriminate.
  Current-source anchors are unchanged: residuals 0 in `WorkersView` / 2 in
  `TasksBoardView`, call sites exactly 16; `npm test` full suite exit 0.

## Unreleased — Issue #91: the four line-number anchors above `splitGoalSegments` become semantic anchors (comment-only, dist byte-identical) (2026-10-24)

Range: one commit on `main` — `7841e53` (`docs(issue91): TasksBoardView 注释行号锚改语义锚 — :191/:241/:244/:893 四锚函数名+语句形态化, 零逻辑变化, dist 重建 md5 逐字节一致 (#91)`). Surface: three comment lines in `ui/src/views/TasksBoardView.jsx` (+3/-3, the file stays at 1707 lines); no behaviour change.

### Changed

- **#91 (line-number anchors rot on any insertion above them)** — the comment
  block above `splitGoalSegments` referenced `:191`, `:241`, `:244` and
  `:893`; inserting a line anywhere earlier would silently invalidate all
  four. Each anchor now identifies its target by function name and statement
  shape: the `usedLines = i + 1` assignment at the loop tail of
  `splitGoalSegments` itself (after `previewLines.push`), the before/after
  relationship between `splitRunTitle`'s truncate-`break` and its
  `usedLines` assignment (reproducing the original `:241` vs `:244`
  contrast with the reachability argument intact), and the collapse-switch
  guard of `GoalCell` (quoted `if (!seg.truncated || seg.hiddenLineCount <=
  0)`). After the change `grep -nE ':(191|241|244|893)\b'` hits nothing in
  the file, so the block is immune to future insertions.

### Notes

- The edit is comment-only with zero logic change, and the vite rebuild is
  byte-identical: `ui/dist/index.html` md5 `6fd0c5735c62c9001e022f39041cdc31`
  before, on a determinism re-run, and after the edit (the same digest the
  #88 / #89 entry records), with a clean `git status` afterwards — expected,
  because esbuild strips comments; the value of the step is that the
  expectation was proven rather than assumed. `npm test` full suite stays
  green.

## Unreleased — Issue #77: remote workers keep their kanban claim, and a TTL reclaim no longer counts as a failure when the worker is provably alive

Range: one commit on `main` following `0953a6c`; no push (push belongs to the
coordinator). Surface: `mw/pipeline.py`, `mw/reaper.py`,
`tests/test_issue77_claim_renewal.py`, `CHANGELOG.md`.

### Fixed

- **A remote worker's kanban card was reclaimed while the worker was still
  running.** The gateway's kanban dispatcher reclaims every `running` card
  whose `claim_expires` has passed (15-minute TTL by default), and its
  liveness escape hatch requires `host_local AND worker_pid` — neither of
  which holds for a remote `api_async` worker (`host_local=False`,
  `worker_pid=NULL`). There was therefore no way out: any card that ran
  past the TTL was flipped back to `ready` while its worker kept working,
  and each reclaim also burned one `consecutive_failures` slot (breaker
  limit 2), so a second reclaim auto-blocked the card and stalled the
  chain. `mw` now renews the claim itself, with a compare-and-set on the
  card's own `claim_lock`:

  ```sql
  UPDATE tasks SET claim_expires=?, last_heartbeat_at=?
   WHERE id=? AND status='running' AND claim_lock=?
  ```

  The renewal mirrors `task_runs.claim_expires` (same shape as the
  gateway's `_extend_run_claim`) and takes its TTL from the same
  `HERMES_KANBAN_CLAIM_TTL_SECONDS` environment variable as the reclaim
  threshold. The `hermes kanban heartbeat` CLI is deliberately **not**
  used: measured on a live card, it updates `last_heartbeat_at` only and
  leaves `claim_expires` untouched, so the reclaim would still happen.
  The renewal runs from the reaper's existing 15-second round — the only
  resident `mw` process that holds both the ledger context and the
  registry — and the whole implementation lives in `mw/pipeline.py`, so
  `mw/reaper.py` gains a single call.

- **A reclaim of a live remote worker no longer counts as a failure.** The
  reclaim event still lands in `task_runs` (`outcome='reclaimed'`) and in
  `task_events`, exactly as before — the audit trail is not hidden. But
  when the remote worker is provably alive (its session's `message_count`
  is still growing inside the 5-minute observation window), that reclaim
  is **not** added to `consecutive_failures`, and `last_failure_error` is
  written as `reclaimed_remote_alive` so it stays distinguishable from a
  real failure. The exemption is applied only when the recorded error is
  the reclaim's own `stale_lock=` marker (which makes the repair
  idempotent), the reclaim event is still inside the repair window, and
  the liveness evidence is present. The card's status is left alone:
  unblocking stays with the coordinator, per the middleware boundary.

### Tests

- Twenty-nine new cases in `tests/test_issue77_claim_renewal.py` cover
  the three acceptance points (liveness evidence, CAS renewal, reclaim
  counting) plus an end-to-end pass over a real SQLite database and a
  guard that the reaper actually calls the renewal (an implementation
  that is never wired up would not fix anything). The liveness judgement
  distinguishes the two signals by purpose: renewal accepts either a
  growing `message_count` or a live SSE entry from the console — a
  worker inside a long tool call has a flat `message_count` and would
  otherwise be mistaken for a dead one — while the reclaim exemption
  accepts only the growing `message_count`, the evidence named in the
  issue ruling.

## Unreleased — Issue #78+#70 P2 item P2-1: the decision-path label is now actually localized (a zh-cn deployment no longer falls back to English)

Range: one commit on `main` following `ddd46ac`; no push (push belongs to the
coordinator). Surface: `mw/decision_paths.py`,
`tests/test_decision_paths.py`, `docs/FIX-NOTES-78-70-P2.md` (count
correction), `CHANGELOG.md`.

### Fixed

- **`classify` label localization (P2-1)** — `_label()` built the
  yaml key as `f"label_{lang}"`, but `resolve_lang()` returns a **locale
  identifier** (`zh-cn`) while the yaml keys use a **language suffix**
  (`label_zh` / `label_en`). The lookup for `label_zh-cn` therefore always
  missed and the `or` chain silently fell back to `label_en`, so a `zh-cn`
  deployment returned the **English** label — both languages yielded the same
  string, with no warning. The label is now resolved through an explicit
  locale ⇒ key-suffix map (`_LANG_TO_LABEL_KEY`), so `zh-cn` yields
  `技术类输入` / `需求类输入` and `en` yields the English labels. The yaml key
  names are **unchanged**: they already follow the file's own `_zh` / `_en`
  suffix convention (`signals_zh` / `signals_en`), and renaming them would
  silently invalidate every route-table entry authored against
  `label_zh` / `label_en`. The fallback order is target language → English →
  Chinese → the path id, so the label is never empty and the function never
  raises. Introduced in `3164bb2` (issue #70) and surfaced by the issue #95
  endpoint, which passes the label straight through to clients.

### Tests

- Seven new cases in `tests/test_decision_paths.py` pin the behaviour in both
  directions: a `zh-cn` deployment returns the Chinese label, an `en`
  deployment returns the English label, the two languages differ (a
  same-value result means a constant assertion), an unsupported locale falls
  back without raising, the labels have exactly one source (the yaml — no
  literals and no locale-derived key concatenation in the module), the
  assertions read the yaml source rather than a self-referential constant, and
  the `GET /api/decision-paths/classify` response label follows the
  deployment language over real HTTP. The four defect-detecting cases turn
  red against the pre-fix implementation (the other three are no-regression
  guard rails, green by design against the buggy code), and all five targeted
  mutations turn the suite red.

### Changed

- **`docs/FIX-NOTES-78-70-P2.md` count correction (P3-2)** — the issue #95
  report claimed `tests/test_decision_paths.py` grew "21→28" and reported
  "28 passed"; the measured figures are **21→25** and **25 passed** (30 for
  the two files together). The "+9 new cases pass" conclusion is unaffected
  (4 + 5 = 9). The report's two count sites were corrected in place and a
  correction section records the measurement.

## Unreleased — Issue #93 + #94 + #96 + #98 (#78+#70 P2 doc items): zh step numbering, one missing EN pitfall, the asymmetric-veto rule, and the references count footnote

Range: one commit on `main` following `ae6f6bb`; no push (push belongs to the
coordinator). Surface:
`deploy/skills/team-work-chain-orchestration/SKILL.{zh-cn,en}.md` (docs only,
zero code change).

### Fixed

- **zh flow step numbering (#93)** — the backfill left the Chinese `## 流程`
  list as `1,2,3,5..11` (a hole at 4 left over from the snapshot's three
  duplicated `4`s; only EN was renumbered). Renumbered to a continuous
  `1..10` mirroring EN, and the two in-text cross-references moved with it
  (`见步骤 9` → `8`, `走步骤 7 结算` → `6`), keeping each reference pointing at
  the same content (spawn / settle).
- **EN missing pitfall (#94)** — the source-snapshot pitfall `skills 目录同名副本`
  (same-name backup dirs make `skill_view` report ambiguous; qualified-path and
  rollback-snapshot consequences) was backfilled into zh but never translated;
  EN `## Pitfalls` 45 → 46, now covering the same matrix as zh.

### Changed

- **Asymmetric-veto rule documented in both skill languages (#96)** — the
  coarse-classification paragraphs now state that requirement-shaped inputs are
  vetoed by technical words while technical-shaped inputs are *not* vetoed by
  requirement words (mixed input ⇒ technical). The rule previously lived only
  in `mw/decision_paths.yaml` comments; an agent re-deriving scores from prose
  would reach the opposite conclusion.
- **references count footnote in `docs/FIX-NOTES-78.md` (#98)** — the note now
  spells out both counters: section-scoped `references/` = 1 per file (the PLAN
  criterion) vs whole-file = 2 in zh / 1 in en, the extra zh hit being an
  existing in-prose recipe reference; both point at files that exist, so
  "no new dead links" stands under either counter.

## Unreleased — Issue #95 + #97 (#78+#70 P2 code items): the classifier gets a production entry point, and pyyaml joins the deploy dependency chain

Range: one commit on `main` following `9d1cbff`; no push (push belongs to the
coordinator). Surface: `mw/console.py`, `mw/decision_paths.py`,
`deploy/main_entry.sh`, `docs/main-soul.{zh-cn,en}.md`,
`deploy/skills/team-work-chain-orchestration/SKILL.{zh-cn,en}.md`,
`tests/test_decision_paths.py`, `tests/test_entry_pyyaml_dep.py` (new),
`CHANGELOG.md`.

### Added

- **`GET /api/decision-paths/classify?text=…` (`mw/console.py`, issue #95)** —
  the s5 review §2.2 finding stood: `classify()` had zero production callers
  while both SOUL and the skill spelled out the scoring formula in prose, so
  the classification contract existed twice (one testable, one free-form). The
  response **is the verdict itself** (`path_id` / `label` / `matched_signals` /
  `vetoed_signals` / `score` / `fallback`); a missing `text` parameter is an
  explicit 400, an empty `text` is a valid undecidable input (200 +
  `fallback: "ask_user"`), and a broken/missing yaml degrades to a null
  verdict, never a 500. SOUL (both languages) and the orchestration skill
  (both languages) now tell main to **call the endpoint** instead of scoring
  the word lists by hand — one contract, implemented once.
- **pyyaml in the deploy dependency chain (`deploy/main_entry.sh`, issue #97)** —
  the dependency block now probes the missing-set (`watchdog`, `pyyaml`),
  installs exactly the missing packages into `${MW_DEPS_DIR}` via the same
  uv → pip → pip-wheel-bootstrap chain, and reports per-dep success/degradation.
  Before this, a fresh deploy without PyYAML made `/api/decision-paths` (and
  `/api/workflows`) silently return empty tables while everything still looked
  healthy. Red lines kept: no image rebuild, `install.sh` untouched, failures
  are warn-and-degrade, container start is never blocked.

### Fixed

- **`classify()` honours "never raises" for non-list `paths` (`mw/decision_paths.py`)** —
  the s8-review S-1 nit: passing e.g. `paths=123` raised `TypeError` before the
  loop guard could run. A non-list `paths` argument is now treated as an empty
  table. The s7 N-2 id-defense also gained its hit-shaped pin (malformed
  `id: 123` entry whose `any_of` actually matches the text must still yield
  `path_id: null`).

### Notes

- `tests/test_entry_pyyaml_dep.py` pins the deploy-side semantics statically
  (probe / install-target / warn line / short-circuit line); container-level
  measurement belongs to the tester card, same discipline as before.
- The s5 §2.6 "image probably ships PyYAML" hedge is falsified by measurement —
  see `docs/FIX-NOTES-78-70-P2.md` §4 for the evidence chain.

## Unreleased — Issue #70 follow-up: the four P1 findings from the #78+#70 code review are fixed (case-insensitive matching, EN push wording, a real assertion, structural validation)

Range: one commit on `main` following `3164bb2`; this entry is the s6 reflow
card's own commit (the #70 and #78 entries below are untouched, no amend).
No push (push belongs to the coordinator). Surface: `mw/decision_paths.py`,
`tests/test_decision_paths.py`,
`deploy/skills/team-work-chain-orchestration/SKILL.en.md`, `CHANGELOG.md`.

The `docs/REVIEW-78-70.md` code review returned **CHANGES_REQUESTED** with
four P1 findings, all of them confined to the #70 commit `3164bb2`. This
entry records the four fixes; the six P2 suggestions were left for a
separate card.

### Fixed

- **Case-insensitive keyword matching (`mw/decision_paths.py`)** — `any_of` /
  `none_of` are lower-case in the yaml but `in` was case-sensitive, so the
  canonical Python traceback header `Traceback (most recent call last):`
  matched nothing: a bare stack trace classified as `path_id: null`
  (⇒ `fallback: "ask_user"`, so main had to ask the user on every real
  technical issue), and an all-English mixed input was mis-routed to
  `requirement`. Both sides are now normalized before comparison;
  `matched_signals` / `vetoed_signals` still report the **word-list
  spelling** so callers can trace a hit back to the table.
- **Structural validation on load (`mw/decision_paths.py`)** — `load_paths()`
  only checked `isinstance(p, dict) and p.get("id")`, so a `match:` written
  as a string or a list reached `classify()` and raised `AttributeError`,
  and the read-only endpoint returned the malformed path verbatim (missing
  `role_tags` / `forbidden_for_main`, so a client parsing the T-70-6
  contract would `KeyError`). Entries are now validated individually:
  `id` must be a non-empty string, `match` must be a dict whose `any_of` /
  `none_of` are lists, and the endpoint-contract fields (`route_to`,
  `role_tags`, `forbidden_for_main`) must be present and non-empty. A
  malformed entry is **dropped on its own** — the rest of the table stays
  usable, because the yaml is a repo asset main may edit by hand — and it
  never leaks to the endpoint. `classify()` keeps a second defensive layer
  so a caller passing a hand-built `paths` list cannot make it raise.
- **EN skill push wording (`deploy/skills/team-work-chain-orchestration/SKILL.en.md`)** —
  the zh mirror of the L4b protocol-patch rule had already been rewritten
  to "commit (push belongs to the coordinator)", but the en side still said
  "committed + pushed"; since `install.sh` ships the en file when
  `SAS_LANG=en`, the deployed en skill taught main to push, contradicting
  this repo's no-push discipline. The en wording now mirrors the zh side.
- **A vacuous assertion (`tests/test_decision_paths.py`)** —
  `test_route_to_semantic_roles_not_profiles` put its assertions inside
  `if p["route_to"] in seed:`; the seed registry holds only `coder` /
  `code-reviewer` while `route_to` holds semantic role names
  (`product-manager` / `system-engineer`), so the branch was never taken
  and the assertion body never executed — the test was green while checking
  nothing. It now asserts the structural contract unconditionally against
  the **raw yaml** (the loader drops malformed entries, so asserting on the
  loader's output would repeat the same mistake): `route_to` is a non-empty
  string that is not a seed profile name, `role_tags` is a non-empty list
  of non-empty strings, and the loader still keeps both default paths.

### Tests

`tests/test_decision_paths.py` grows from 13 to 21 cases: the bare-traceback
and all-English-mixed classification cases (F-1), the zh/en push-wording
mirror check (F-2), twelve malformed-entry shapes plus the endpoint
never-returns-a-malformed-path check and the never-raises check (F-4), and
the rewritten F-3 assertion. Each new case was verified non-vacuous by
mutation: reverting the module to `3164bb2` turns 7 of them red, and
targeted mutations (dropping the lowercase normalization, dropping the
`match` defense, relaxing the loader filter, renaming `route_to` to a seed
profile, emptying `role_tags`) each turn the relevant case red.

## Unreleased — Issue #70: decision-path module lands in the repo (yaml + pure-function classifier + read-only endpoint) with both main souls updated

Range: `2510d8d..HEAD` chain on `main`; this entry is s3's own commit (s2's #78 entries are untouched, no amend). No push (push belongs to the coordinator). Surface: `mw/decision_paths.yaml` (new), `mw/decision_paths.py` (new), `mw/console.py` (new `GET /api/decision-paths` only — zero behavior change to existing routes), `tests/test_decision_paths.py` (new, 13 cases), `docs/main-soul.{zh-cn,en}.md` (bilingual, same commit).

Per the user's ruling (2026-10-23 07:14, F batch): the decision-path module lives **in the repo**, stays **un-abstracted for now** (testable first), and the repo's two main souls must be updated in the same batch. Deliberately minimal (`暂不抽象`, PLAN-78-70 §3.2 candidate (b)): one yaml + one pure-function loader + one read-only endpoint. No framework, no plugin registry, no rules engine, no LLM in classification, no workflow-field reuse, no UI, no registry coupling.

- `mw/decision_paths.yaml` — two paths (`requirement` → product-manager, `technical` → system-engineer) carrying `match.any_of` / `match.none_of`, `route_to` / `role_tags` (semantic role names, not runtime profile names — main re-reads `/api/workers` at dispatch time), `then_cards` and a non-empty `forbidden_for_main` per path. Deviation registered in-file: `technical.none_of` is `[]` instead of the PLAN §3.3 example's requirement words, because under the PLAN's own score rule a mixed input would score ≤ 0 on both paths and could never yield the `technical` verdict PLAN §4.3 T-70-3 demands; technical-priority is implemented as an asymmetric veto (requirement path is vetoed by technical signals) and pinned by a test.
- `mw/decision_paths.py` — `load_paths()` (missing/corrupt yaml ⇒ `[]`, never raises) + `classify()` (score = hits − 2 × vetoes, ties resolved by yaml order, all ≤ 0 ⇒ `path_id=None` + `fallback="ask_user"`). Zero registry/network/LLM imports (static-purity test).
- `GET /api/decision-paths` — returns `{"paths": [...]}`, degrades to `{"paths": []}` on bad yaml (same discipline as `/api/workflows`). Labels are `label_zh`/`label_en` fields shipped with the yaml ⇒ zero new i18n keys (206 = 206 anchor untouched).
- Both main souls gained a `决策路径` / `Decision Paths` section inside the existing REQUIRED block after the core-boundary table, one line in workflow step 2 (read the endpoint before splitting; `path_id=null` ⇒ ask the user, never pick a role), three `do NOT do` items (reproduce / root-cause / code-review during role-finding), and an adjustable-defaults entry. EN CJK-outside-code stays 0 (G13); REQUIRED marker count stays 9 in both files.

## Unreleased — Issue #78: `team-work-chain-orchestration` skill backfilled from the runtime truth source (bilingual, adjudicated section merge)

Range: one commit on `main` — `2510d8d` (`docs(issue78): 编排技能回灌运行时真源 — 段级带裁决合并, 双语同 commit (#78)`); the chain count is updated when #70's card lands. Docs only (`deploy/skills/team-work-chain-orchestration/SKILL.{zh-cn,en}.md`); no code, no deploy-surface, no push (push belongs to the coordinator).

The repo copies of the orchestration skill had silently drifted behind the runtime truth source (19 sections / 48 pitfalls measured on 2026-10-21 vs 8 / 29 in repo). Per the user's ruling (issue #78 comment 2026-10-21 15:54: adopt suggestion 1 — backfill the repo mirror; manual intervention, not automatic sync), this is an **adjudicated section-level merge, not a wholesale overwrite**:

### Added

- Eight truth-source sections merged into both language files (sections 8 → 16): build-the-first-card-on-arrival, contract seeding in TODO.md, role division matrix, upstream-spec fact verification, non-card governance via the worker gateway, reference files, read-back-before-linking, scope and conventions.
- The role matrix gained a `决策路径` / `Decision path` column whose values (`requirement` / `technical`) match the `mw/decision_paths.yaml` path ids of issue #70 by design, plus the coarse-classification rule (classification by input text form only; undecidable ⇒ ask the user) — the user's "refine missing chapters 1 and 3 against #70" requirement.
- EN side: the seven ZH-only pitfalls (mw.cli status keys / ledger table names / duplicate notifications / __spawn-bg locked crash / false stuck knock / tail-card knock / TODO ownership) and the new truth-source lessons were translated in; EN `Pitfalls` coverage now matches the ZH set (the metric is the §2.6 coverage matrix, not raw counts).

### Preserved (repo-only content the truth source lacks — a blind `cp` would have silently dropped it)

- The runtime `SKILL.md` ownership trap (`root:root 755` dirs vs `hermes:hermes 664` files) in the flow section.
- "Protocol patches must also land in the repo's `deploy/skills/`" in the report-before-advance section (its commit+push wording was amended to "commit; push belongs to the coordinator" for this no-push batch).
- "Worker-claimed push success must be main-side measured" as a pitfall entry (zh-cn `## 坑` count 29 → 46 = 29 + 16 backfilled + 1 this entry).

### Removed / fixed

- Five reductions per the user's ruling, verified by grep = 0: codegraph section (dev-machine custom), bilingual-mirror section (dev-env custom), wake-gate section (declares itself non-repo), three dead reference links (`upstream-spec-fact-check` / `requirement-handoff-baseline` / `verify_todo_contract` — none exist anywhere), and the TTL mis-reclaim pitfall (fix lives in third-party image, not this repo).
- Truth-source pitfalls `dead-letter echo` and `current-task-not-bound guard` were folded into the liveness section (they already have synonyms there) instead of being duplicated in the pitfalls list.
- EN file CJK-outside-code 18 → 0 (the dead-letter pitfall line was the polluted one); inherited flow-step numbering corruption (1,2,3,4,4,4,5…) renumbered and cross-references fixed; no new dead links (the only remaining `references/` pointer resolves to a tracked-in-tree file).

## Unreleased — Issues #88 / #89: the goal-cell hidden-line ternary is reachable, not dead; `fmtApiError` residual detection and call-site counting hardened (2026-10-23)

Range: two commits on `main` — `854527f` (`docs(issue88): 标注 splitGoalSegments 三元非死代码, 勿删成 ': 0' (#88)`) and `94b90db` (`test(issue88/89): 补 #88 hiddenLineCount 钉桩 + 放宽 #89 残留正则与计数口径 (#88 #89)`).
UI only. The first commit touches `ui/src` and therefore carries the rebuilt
`ui/dist/index.html` / `mw/ui_dist/index.html` pair per INV-13; `mw/` and
`deploy/` are otherwise untouched. Chain: plan `PLAN-88-89.md` → fix → targeted
retest → review.

### Fixed

- **#88 (`splitGoalSegments` hidden-line ternary is reachable)** — the issue
  text described the ternary
  `moreLines ? allLines.length - usedLines : (anyCharTruncated ? 1 : 0)` as
  "shaped like" the `splitRunTitle` one and therefore dead in both. That is
  wrong for `splitGoalSegments`: the two functions only look alike, their loop
  structures differ. In `splitGoalSegments` the `usedLines = i + 1` assignment
  runs unconditionally after the push (the `break` comes after it), so a
  character-truncated line *is* counted into `usedLines` and
  `allLines.slice(usedLines)` can be empty, which makes the `else` side
  reachable. In `splitRunTitle` the truncation branch `break`s *before* the
  `usedLines` assignment, so its `else` side really is unreachable and the
  earlier batch's ruling there stands. The reachable input is narrow: the
  character-truncated line must be the *last* line of the input and sit at
  index `<= 1`, i.e. the input holds one or two non-blank lines with the
  over-long one last — `'x'.repeat(141)` yields `hiddenLineCount === 1`.
  The `else` side is also the guard for the goal cell's collapse switch: the
  predicate `!seg.truncated || seg.hiddenLineCount <= 0` decides between
  rendering the full text (no drawer) and rendering a `<details>` drawer.
  Rewriting it to `': 0'` would drop `hiddenLineCount` from 1 to 0 and make
  the `<details>` drawer of every over-long single-line goal (141 characters or
  more) disappear outright — not merely shift a badge by one — and would break
  the invariant `truncated === true ⇒ hiddenLineCount >= 1` that holds on
  every baseline input. The commit therefore only adds a comment above the
  ternary; the code is unchanged character for character, and the comment
  records both the reachability argument and the "do not simplify this to
  `': 0'`" warning for whoever revisits the issue next.
- **#88 (regression coverage)** — the existing "single over-long segment"
  case in `ui/tests/test_todo_viewer_chips.mjs` already used a reachable
  input (`'x'.repeat(200)`) but only asserted `preview` and `truncated`, never
  `hiddenLineCount`, which is why deleting the ternary turned no test red.
  Four cases are appended (24 → 28): a behaviour anchor pinning
  `hiddenLineCount === 1` on the reachable inputs, a boundary anchor pinning
  the 140/141 character threshold, a UI-invariant anchor pinning
  `truncated === true ⇒ hiddenLineCount >= 1`, and a structural anchor
  requiring the `anyCharTruncated ? 1 : 0` form to stay in the source.

### Changed

- **#89 suggestion 1 (residual detection could be escaped by renaming)** —
  the residual regex in `ui/tests/test_issue87_fmtapierror_coverage.mjs` was
  anchored to the literal identifier `e` at both ends, so rewriting a
  `catch (e)` site as `catch (err)` with `err.message || err` slipped past it
  undetected. It now uses a capture group with a back-reference, matching
  "the same identifier's `.message || itself`" for any identifier name
  (`e` / `err` / `ex` / `_e` / `e1`), while still covering the optional-chaining
  form and the whitespace-obfuscated form. Measured against the real source
  the hit lines are byte-identical to before — exactly two in
  `TasksBoardView`, none in `WorkersView` — so the existing anchors
  (`RW === 0`, `RB === 2`) are unchanged and no false negative is introduced;
  a newly added `err.message || err` site is now detected (the previous regex
  reported the same count and stayed green).
- **#89 suggestion 2 (call-site counting counted comment lines)** — the
  counting rule for `fmtApiError(` call sites now excludes whole-line
  comments, replacing such lines with empty strings so line numbers stay
  aligned for error messages. Trailing `//` is deliberately *not* stripped:
  this repository contains URL strings such as
  `placeholder="http://<host>:8643"`, and trimming after a quote-parsing
  step could damage them; excluding whole-line comments already closes the
  reported escape ("delete one real call site, add a comment line mentioning
  `fmtApiError(`") with no false-positive risk. Both rules count the same
  16 sites today (7 in `WorkersView`, 9 in `TasksBoardView`) with an empty
  set difference on the matched line numbers, so the exact anchor of 16 and
  the `>= 13` threshold are unchanged — no silent drift.
- **#89 suggestion 3 (empty `cause` property) — not adopted.** The `cause`
  property created when `cause` is `undefined` has no observable effect: it
  is non-enumerable, never reaches `JSON.stringify`, reads back as
  `undefined` either way, and nothing in the repository consumes it.
  Conditional passing would only introduce an observable change
  (`'cause' in err` flips from `true` to `false`) on an unrelated axis, so
  `ui/src/api.js` is unchanged in this batch and the `cause` assertions of
  `ui/tests/test_issue86_net_cause.mjs` are untouched.

### Notes

- The commit that touches `ui/src` rebuilds both dist copies byte-identical
  (INV-13); the md5 `6fd0c5735c62c9001e022f39041cdc31` reproduces on a
  rebuild (vite 5.4.21, node 26.5.1) and is *identical to the baseline*
  `570b712`, because `vite build` strips comments — so git records no dist
  change. A changed md5 would have meant the comment-only edit had touched
  executable code.
- Behaviour equivalence for #88: across 40,070 inputs (including the
  targeted reachable forms, the 140/141 threshold, over-long lines in first /
  middle / last position, trailing whitespace and blank lines, `null` /
  `undefined` / empty / whitespace-only, and random mixes) the four returned
  fields `preview` / `full` / `truncated` / `hiddenLineCount` are value-for-value
  identical before and after the change: **0 differences**. This batch has
  **zero user-visible behaviour change** (the only source edit is a comment).
- Regression: `npm test` stays at 11 suites, all green
  (`test_todo_viewer_chips.mjs` goes 24 → 28); full pytest failure set
  set-diff empty vs the pre-change baseline (8 failed / 569 passed, all
  pre-existing); `tools/i18n_docs_sync.py --strict` exit 0 with the key-count
  anchors untouched (flatten 651=651, `tboard` 121=121 — this batch adds and
  removes no i18n key).
- Each new assertion was checked against a deliberately introduced regression
  in a scratch copy (deleting the ternary, corrupting the count logic,
  rewriting the ternary without the `anyCharTruncated` literal, adding an
  `err.message || err` residual, renaming a residual's variable, deleting a
  call site and padding the count with a comment line): every case turns the
  corresponding assertion red, the control runs with the old implementation
  stay green (proving the old rules really did miss those shapes), and the
  restored tree goes green again.
- Counting-provenance note (carried over from the #87 batch): the #87
  CHANGELOG entry records "exhaustive check over 200,369 inputs" (the
  implementation round's generator) while `docs/PLAN-87-86.md` records
  200,383 (the plan round's generator); the two numbers come from **two
  independent input generators** and are not the same set. The registered
  200,383 also differs by 7 from its own decomposition
  (`341 + 35 + 200000 = 200,376`) — that figure belongs to the plan round and
  is not amended retroactively here.

## Unreleased — Issues #87 / #86: dead branch in the run-title hidden-line count removed; `net()` keeps the original error; three regression suites added (2026-10-22)

Range: one commit on `main` — `38d794e` (`polish: 清理隐藏行数死分支 + net() 保留原始错误, 补三组回归测试 (#87 #86)`).
UI only, with the rebuilt `ui/dist/index.html` / `mw/ui_dist/index.html` pair in
that commit per INV-13; `mw/` and `deploy/` untouched. Chain: plan `PLAN-87-86.md`
→ fix → targeted retest → review.

### Fixed

- **#87 S-1 (dead branch)** — `splitRunTitle` computed the hidden-line count as
  `more ? allLines.length - usedLines : (charTruncated ? 1 : 0)`. The
  character-truncation sub-expression was unreachable: `charTruncated` is only
  set on the branch that immediately `break`s, so `usedLines` never counts the
  truncated line, `allLines.slice(usedLines)` therefore still contains it, and
  `more` is always truthy. The dead sub-expression is dropped; the `else` side
  is a constant `0`, so `hiddenLineCount` is value-for-value unchanged on every
  input (exhaustive check over 200,369 inputs, zero differences). New unit test
  `ui/tests/test_issue87_s1_deadbranch.mjs` pins it: the dead expression must
  not come back, the implementation must agree value-for-value with a reference
  implementation, and the multi-line hidden-count anchor stays.
- **#87 S-2 (static coverage regression)** — new unit test
  `ui/tests/test_issue87_fmtapierror_coverage.mjs` turns the `fmtApiError`
  coverage into a static regression. Counting rule: a call site is a *line*
  containing `fmtApiError(`; import lines are excluded naturally because the
  import is written `fmtApiError }` without a left parenthesis, so the rule is
  literally `grep -c 'fmtApiError('` — 7 in `WorkersView`, 9 in
  `TasksBoardView`, 16 in total. Residual `e.message || e` detection squashes
  whitespace first, so a rewrite such as `e . message || e` cannot slip
  through. Five assertions: no residual in `WorkersView`, exactly two residuals
  in `TasksBoardView` (the two fail-soft sites the earlier batch deliberately
  kept), total call sites `>= 13` (the issue's own threshold), an exact anchor
  of 16, and both files still importing `fmtApiError`.
- **#86 (`net()` keeps the original error)** — a rejected `fetch` used to be
  repackaged into an `ApiError` carrying only a string, discarding the original
  `TypeError` with its stack. The repackaged error now passes the original
  through the standard `Error` `cause` option, so the failure site stays
  reachable for debugging. `kind` remains `network`, the
  `String(e?.message || e)` fallback and the `endpoint` are byte-identical, so
  the three-way attribution and the `err.network` wording are unaffected; `cause`
  is non-enumerable and never reaches `JSON.stringify`. New unit test
  `ui/tests/test_issue86_net_cause.mjs` asserts the original error is reachable
  and that the already-`ApiError` path is still rethrown untouched.

### Notes

- The commit rebuilds both dist copies byte-identical (INV-13); the md5
  `6fd0c5735c62c9001e022f39041cdc31` reproduces on a rebuild (vite 5.4.21,
  node 26.5.1). It supersedes the previous md5
  `aa550db9a2a46d87208711605d3afd07`.
- Regression: `npm test` goes from 8 to 11 suites, all green; full pytest
  failure set set-diff empty vs the pre-fix baseline (8 failed / 569 passed, all
  pre-existing); `tools/i18n_docs_sync.py --strict` exit 0 with the key-count
  anchors untouched (flatten 651=651, `tboard` 121=121 — this batch adds and
  removes no i18n key).
- Each new assertion was checked against a deliberately introduced regression in
  a scratch copy (restoring the dead branch, making the `else` side reachable,
  dropping `cause`, reverting one `fmtApiError(e)` call to `e.message || e`,
  deleting a call site, rewriting a residual to escape the regex, touching a
  reserved fail-soft site): every case turns the corresponding assertion red,
  and the restored tree goes green again.

## Unreleased — Issues #79 / #84: task-plan modal title collapsed behind a preview + drawer; worker/board error paths routed through three-way attribution (2026-10-22)

Range: three commits on `main` — `d8ffe66` (`polish: 弹窗 run 标题走预览+折叠抽屉, 全值可达 (#79)`),
`d695153` (`polish: WorkersView 弹窗与操作错误路径纳入三态归因 (#84)`) and
`c9955b8` (`polish: fmtApiError 覆盖任务看板操作错误路径 (#84)`), plus this
CHANGELOG. UI only, with the rebuilt `ui/dist/index.html` /
`mw/ui_dist/index.html` pair in every commit per INV-13; `mw/` and `deploy/`
untouched. Chain: plan `PLAN-79-84.md` → fix → targeted retest → review.

### Fixed

- **#79 (task-plan modal title)** — the modal header used to dump the whole
  `initial_task` text into the title line. `splitRunTitle` now keeps a
  preview (multi-line: 2 lines; single line: 120 chars) while the full value
  stays reachable through two paths: the `title` attribute and a
  default-collapsed `<details>` drawer (keyboard-operable, a11y name kept).
  Short titles render as before — no preview, no drawer, no regression. New
  bilingual i18n key `tboard.todo_viewer_title_toggle_show` (key-count
  anchors 650→651, `tboard` 120→121). New unit test
  `ui/tests/test_issue79_run_title.mjs` wired into `npm test`.
- **#84 (three-way error attribution, coverage)** — 13 `catch` sites (6 in
  `WorkersView`, 7 in `TasksBoardView`) now route the error through
  `fmtApiError` instead of raw `e.message`, so 4xx / 5xx / network failures
  render with the attribution wording *and endpoint label* introduced in #81.
  The outer `t()` prefixes are preserved verbatim, and the synchronous
  non-`ApiError` fallback stays byte-equivalent to the old text. Two
  remaining `TasksBoardView` sites are fail-soft paths deliberately kept
  as-is (the inner `api.js` call already attributes); the 16 sites in
  ChatView/SettingsView/WorkflowsView are filed-not-fixed per the plan.

### Notes

- Each of the three commits rebuilds both dist copies byte-identical
  (INV-13); the final md5 `aa550db9a2a46d87208711605d3afd07` reproduces on a
  fresh `npm ci && vite build` on the test machine (node 24), no downgrade
  path.
- Regression: full pytest failure set set-diff empty vs the pre-fix baseline
  (8 failed / 569 passed, all pre-existing), on this host and on the
  dedicated 170 test machine; `npm test` 8 suites green;
  `tools/i18n_docs_sync.py --strict` exit 0.
- Review verdict PASS (blocking 0 / important 0 / suggestion 2). The two
  suggestions are tracked as Gitea #87; the `net()` observation (rejects of
  non-`ApiError` repackaged as `network` — pre-existing, not introduced by
  this batch) is tracked as Gitea #86.

## Unreleased — Issue #82: main's control socket moved off the virtiofs bind mount; workers moved onto the shared `simple-dot` network (2026-10-22)

Range: this entry and the follow-up commits touching `deploy/main.compose.yaml`,
`deploy/install.sh`, `mw/console.py`, `DEPLOY.md` and this CHANGELOG. The
earlier docs-only diagnosis (root cause located, "no repo-side fix surface")
is superseded — the fix is now implemented in the deploy path.

### Root cause (issue #82, field forensics on the affected macOS host)

- The `gateway unreachable` symptom is caused by a **socket inode on a
  virtiofs-shared directory** (colima's default `mountType=virtiofs`) that
  the guest can no longer `stat`/`unlink` — both fail with
  `[Errno 95] Operation not supported`, root included — while the host
  still sees a perfectly normal socket. The gateway's startup path is

  ```python
  if bind_path.exists():        # Path.exists() swallows the E95 OSError -> False
      bind_path.unlink()         # never reached, so the poison survives
  start_unix_server(path=...)    # bind on the stale inode -> E95 -> gateway dies
  ```

  and `cleanup_files()` wraps its exit-time unlink in
  `contextlib.suppress(OSError)`, so the poison is silently carried into
  the next start. That is why the outage "only lasts a few minutes": the
  window is a *lifecycle* gap (a healthy gateway exits / is replaced,
  leaves the inode behind, the next instance walks into it) — not a timer.
- The poison is **version-independent**: `v2026.9.21`, `v2026.9.24` and
  `v0.22.4` all fail identically on the same poisoned directory. Only the
  *consequence* differs (`v2026.9.x` degrades, `v0.22.4` crash-loops
  because upstream #92091 made the control listener a startup
  prerequisite).
- The **deciding factor is the storage backend, not the image**: the
  worker/coder containers already kept `/opt/data` on a docker named
  volume (ext4) and never reproduced E95 on any image. `main` was the
  only service still on a virtiofs bind mount.
- A **second, independent** fault made the workers read as *offline*:
  the registered `api_url` pointed at published **host** ports, so every
  call went through `docker-proxy` inside the VM, which drops connections
  under load. On top of that, the colima VM had only 2 GB of RAM for
  ~8 gateway processes at ~220–260 MiB RSS each — the fleet OOM-killed
  (856–870 `Previous gateway life` entries per worker).

### Changes

- `deploy/main.compose.yaml` — `main`'s `/opt/data` moved from the host
  bind mount `./data/main:/opt/data` to the **named volume**
  `main_hermes_main:/opt/data` (ext4 on the VM disk). `main` also joins
  the shared `simple-dot` network. The compose **service name** was
  renamed `hermes` → `hermes_main` to match `container_name`: compose
  registers the service name as a network alias automatically, so a
  service named `hermes` kept answering to the shared alias `hermes`
  even after the `aliases:` block had been made unique. `hostname` is
  now pinned to the same value.
- `deploy/install.sh` — creates the `simple-dot` network (idempotent;
  it is declared `external: true` in the compose files, so compose never
  owns it) and seeds the main named volume from `data/main` on first
  deploy (idempotent: a non-empty volume is never overwritten). The
  volume name is resolved from the compose project name
  (`mw-<container>_main_hermes_main`) so the two can never drift apart.
  Two details are load-bearing and were fixed after the first field run:
  the seeding helper **must** override the image's s6-overlay
  `ENTRYPOINT` (`docker run --entrypoint sh <image> -c ...`), otherwise
  docker starts the whole service stack, never runs the command, and the
  seeding silently does nothing while still exiting 0; and the volume
  needs `chown -R 10000:10000` after seeding, because the gateway runs
  as uid 10000 while the image bootstrap creates
  `pending_messages/` and `runtime/bot_live_delivery/` as root — the
  gateway then logs `Operation not permitted` for them and silently drops
  bot/mailbox deliveries.
- `mw/console.py` — the worker compose template now emits a **named
  volume** for `/opt/data` and joins `simple-dot`, instead of a
  `./hermes` bind mount; the "add worker" hint now tells the operator to
  register `api_url=http://hermes_<name>:8642` (the container-internal
  address) rather than a host port. The template's service name is
  `hermes_{name}` for the same reason as `main`'s — the service name
  doubles as the network alias and must be unique per worker.
- Worker named volumes are mounted at the **service** level
  (`- hermes-<name>:/opt/data`) and the volume key is declared under the
  top-level `volumes:` block. Regression found on a live host: the
  compose carried a top-level `volumes:` block but the service had no
  `/opt/data` mount at all, so Docker auto-created an **anonymous** volume
  (the image declares `VOLUME /opt/data`) and the declared volume sat
  unused — the anonymous-storage topology was still in place. Guarded by
  `tests/test_issue82_deploy_topology.py`.

- `DEPLOY.md` — §4.5 rewritten from "known limitation, operator must
  clear the socket by hand" to "fixed in the deploy path", keeping the
  analysis and the host-side recovery steps for hosts still on the old
  topology; new §4.6 (shared network `simple-dot` + the unique-alias
  rule) and §4.7 (sizing the VM).

### Deployment standard now in force

- **Every** service — `main` and all workers — joins `simple-dot` and
  gets a **unique** alias (its container name). A shared alias is
  forbidden: inside each container's own netns `hermes` resolves to
  *itself*, so a cross-container call to `hermes` silently reaches the
  wrong service.
- Services address each other over **container-internal** URLs
  (`http://hermes_<name>:8642`); no `docker-proxy` hop.
- `main`'s and every worker's `/opt/data` is a **named volume** (ext4),
  never a host bind mount.
- The colima VM needs `--cpu 4 --memory 8 --disk 100` for the full fleet.

### Verification on the affected host (macOS mini, colima, virtiofs)

- 6 × `docker restart hermes_main` → `http=200 e95=0 sock=ok` every round.
- 3 × full rebuild (`stop`+`rm`+`deploy/install.sh`) → `http=200 e95=0`.
- colima VM restart + reinstall → `http=200 e95=0`, `/opt/data` on
  `/dev/vdb1` (ext4).
- Full internal matrix: 7/7 workers answer `200` on
  `http://hermes_<name>:8642/health` from inside `main`.
- Worker `RestartCount` = 0 (was 856–870 `Previous gateway life` cycles
  before the VM was resized).
- `main` control socket: `srw-------` on ext4, E95 count 0 across the
  whole lifecycle log.
- Volume ownership: `pending_messages/` and `runtime/bot_live_delivery/`
  read `10000:10000` and the `Operation not permitted` count in
  `main`'s log is 0 (was 3 before the ownership fix).
- Alias layout: no container resolves the shared name `hermes` any more;
  every container lists only its own name.
- `tests/test_issue82_deploy_topology.py` — new guard file pinning the
  three deployment invariants: `/opt/data` is a named volume, the
  service name equals the container name with a unique alias, and the
  worker template is structurally identical. The two pre-existing
  `install.sh` assertions that pinned the *old* bind topology were
  rewritten to pin the new one (they were failing against the fix).

### Notes

- The "pre-clean the socket before start" idea was **not** adopted: it
  only helps for the current cycle and cannot stop the next
  exit/replace from producing a new poisoned inode. Changing the storage
  backend removes the condition instead of racing it.
- `HERMES_CONTROL_SOCKET_*` / `XDG_RUNTIME_DIR` env vars are still
  deliberately **not** added: the image resolves the socket path
  unconditionally and consults `XDG_RUNTIME_DIR` only in the `sun_path`
  overflow branch (paths > 100 bytes); `/opt/data/gateway.sock` is 24
  bytes and never takes that branch.


## Unreleased — Issue #81/#80: console error attribution + windowed-history edge cases (2026-10-22)

Range: four commits on `main` touching `mw/console.py`,
`tests/test_issue75_messages_window.py`, `tests/test_issue75_heartbeat.py`,
`ui/src/api.js`, `ui/src/i18n/locales/en.json`,
`ui/src/i18n/locales/zh-cn.json`, `ui/tests/test_issue58_tokens.mjs`,
`ui/dist/index.html`, `mw/ui_dist/index.html`, `DEPLOY.md` and this
CHANGELOG. The plan document `docs/PLAN-81-80.md` lives in the task
workspace (`repo/docs/`) and is **not** under version control — the
release-doc final state (F3') requires `docs/` to track exactly the two
deploy souls, so the plan file stays workspace-only.

This batch closes the five code items of the issue #75 review follow-ups
(Gitea #81 items 1–5) plus the deployment-order item of Gitea #80, and
refines the 502 attribution of #80. The #75 entry below listed these as
"remain unimplemented" — that line is superseded by this entry.

### Fixed

- **`/messages` with a non-numeric `want` returned 500 instead of 400**
  (issue #81-3). `int()` ran as an argument of `build_messages_window`,
  so a `ValueError` escaped the route and fell through to the `do_GET`
  catch-all, answering `500` with `{"error": "invalid literal for
  int()..."}`. The parameter is now parsed before the call and a bad
  value is rejected with `400`, using the same wording family as the
  existing invalid-`before` response. `build_messages_window`'s
  signature and its clamp semantics are unchanged.
- **Gateway errors lost both their status code and their message**
  (issue #81-4). The `(code, body)` tuple was flattened into a string
  and re-parsed with `partition` + `json.loads`; a body longer than the
  `[:200]` truncation limit raised `JSONDecodeError`, which escaped to
  the `do_GET` catch-all and turned the whole request into a `500` with
  a `JSONDecodeError` message. A longer upstream diagnostic therefore
  produced a *less* useful attribution. The tuple now travels in a
  dedicated `_GwError` exception and is passed through verbatim; the
  truncation survives only in the log-facing `__str__` summary.
  `_gw_fetch_json`'s return contract is unchanged.
- **The first history batch always blamed a missing `message_count`**
  (issue #80-3). `get_message_count` swallowed gateway transport errors
  (unreachable / auth failure / 5xx) and returned `None`, so the first
  batch (no `before` cursor) always answered `502 {"error": "session
  message_count unavailable"}` and hid the real cause. Transport
  failures now propagate the upstream status code and body as-is; the
  `502` "message_count unavailable" semantics are kept for the case
  where the gateway is reachable but genuinely lacks the field.
- **`fmtApiError` labelled 4xx responses as server errors**
  (issue #81-2). Every kind except `network` rendered `err.server`, so a
  `404`/`400`/`403` was reported as `Server error 404`, pointing
  debugging at the wrong layer. A `client` branch now renders the new
  `err.client` key (bilingual, added in the same commit): `Request
  rejected {code} — {ep}` / `请求被拒绝 {code} — {ep}`. The `5xx`
  wording is unchanged.
- **Dead i18n key `chat.session.no_more_history` removed**
  (issue #81-1). The key had no static reference anywhere in
  `ui/src` — the "load earlier messages" button disappears once the
  history is exhausted, so the wording had no carrier. It is deleted
  from both locales. The leaf-key anchor stays at **650**: one key
  removed, one added (`err.client`), both locales in the same commit.
- **`api_workers` now reuses the `_reaper_hb_age()` helper**
  (issue #81-5). The function still inlined its own read of the reaper
  heartbeat file although the shared helper existed, so the name did
  not match the implementation. The inline read is replaced by the
  helper call; the `hb_age < 90` liveness rule stays byte-identical in
  both `api_heartbeat` and `api_workers`, and the missing-file and
  corrupt-file cases behave the same as before.

### Changed

- **`DEPLOY.md` §5 documents that the console upgrade is atomic**
  (issue #80-2). The suggested "upgrade the backend first" sequencing is
  not applicable here: `mw/` is a single directory bind mount, so
  `git pull` + restart always applies the Python proxy and `ui/dist`
  together and there is no intermediate state to schedule around. The
  only mixed-version combination a user can hit is a **stale browser
  cache** holding an older `ui/dist` bundle, which the section now
  names with the hard-reload remedy.

### Not done (registered, with reasons)

- **#80-1 (first-load failure wording)** — the Retry button and the
  bilingual error copy already exist from the #75 s10 work; the
  remainder is pure phrasing taste, and changing it would cost another
  `dist` rebuild for no defect.
- **#80-4 (4 s poll keeps retrying while the first load failed)** —
  the silent retry *is* the recovery mechanism that #75 s7 measured
  (recovery without user action). Throttling it would turn automatic
  recovery into a manual one, i.e. a behaviour change, not a bug fix.
- **#80-5 (server-side incremental window protocol)** — an `ETag` /
  `after_cursor` protocol changes the window API contract, the
  frontend dedup semantics and the seen-id set lifecycle. It is
  registered as a separate follow-up card, not folded into this
  batch.
- **#81-6 (CHANGELOG entry for the #81 items)** — already closed by
  commit `345d220`.

### Verification

- `pytest tests/test_issue75_messages_window.py
  tests/test_issue75_heartbeat.py` green (22 cases). New cases:
  invalid `want` → 400, oversized gateway error body preserved with
  the upstream dict shape (no double encoding), first-batch
  attribution, and the counter-proof that a genuinely missing
  `message_count` still answers 502. All six new/updated cases turn
  red when the fix is reverted (measured: 6 failed / 16 passed).
- `cd ui && npm test` — 7 suites green. The i18n key-count anchor is
  unchanged at 650 per locale, and the locale key sets stay symmetric.
- `ui/dist/index.html` and `mw/ui_dist/index.html` byte-identical
  (md5 `2fc4f326…`, deterministic double build) and different from the
  previous `4b150a90…`; the bundle no longer contains
  `no_more_history` and does contain the new `err.client` copy in both
  languages.
- `tools/i18n_docs_sync.py --strict` exit 0 (F1 CJK=0 on the root
  three docs, F3' `docs/` tracked == the two deploy souls).

## Unreleased — Issue #76: t10 TMPDIR env-coupling false-red fixed (test-side only, 2026-10-22)

Range: two commits on `main` touching exactly two files —
`tests/test_install_bind_mount_skill_soul.py` and this CHANGELOG (the
commits are this entry's own commits; no `A^..B` span is quoted to avoid
excluding the range's first commit). Zero production code was modified:
`deploy/install.sh` is byte-identical to `510c0e3` (md5
`ea3de274e17ae96b91dfff97146d00f2`), and T6
(`test_t6_bsd_red_lines_zero`) keeps pinning its POSIX mktemp
`TMPDIR`+`XXXXXX` shape as before.

### Fixed (test suite)

- **`test_t10_abs_compose_pipeline_directory_mounts` false-red under an
  unwritable `TMPDIR`** (card s2, task `t_d39f33bd`). The §8 abs-compose
  snippet embedded in the test took the *external* `TMPDIR` verbatim at
  shell runtime, so any runner with a non-writable `TMPDIR` (e.g. worker
  containers where uid≠`TMPDIR` owner) got `mktemp: Permission denied` —
  unrelated to the rewrite logic under test. The snippet now sets
  `TMPDIR='<the test's own tmp dir>'` as its own step inside the snippet; the
  `mktemp` line itself is rendered byte-identical to `deploy/install.sh:274`
  (anti-drift anchor AC-04) and is extracted to
  `_ABS_COMPOSE_MKTEMP_LINE` so the literal is stated exactly once.
- **Counter-proof test added**: `test_t10_unwritable_tmpdir_counter_proof`
  builds a `chmod 555` directory, points `TMPDIR` at it, first asserts the
  *pre-fix* bare idiom really fails there with `Permission denied` (red
  side, anti false-green), then asserts the hermetic snippet succeeds in
  the same environment with correct mount output (green side). The two
  subprocesses pin `LC_ALL=C` so the `Permission denied` stderr text stays
  locale-independent (`strerror()` output follows `LC_MESSAGES`; without
  the pin the fix would itself introduce an environment-coupled false
  red). Skips explicitly under root where `chmod 555` cannot bite.
  Self-cleans on the exception path; added to the `__main__` standalone list.
- Ground truth and acceptance criteria: `docs/REQ-76.md` /
  `docs/ACCEPT-76.md` (card s1); implementation notes `docs/IMPL-NOTES-76.md`
  (card s2), under the task workspace `docs/`.

## Unreleased — Issue #82: gateway-start uniqueness guard test, zero production-code change (2026-10-21)

Range: one commit on `main` touching exactly two files — the new
`tests/test_issue82_gateway_start_uniqueness.py` and this CHANGELOG entry.
No production file was modified: `deploy/main_entry.sh` (md5
`8872561f…`, 221 lines, byte-identical to the `d0a56b3` baseline),
`deploy/main.compose.yaml` (md5 `a9de2511…`) and `deploy/install.sh` are
untouched, as are `mw/`, `ui/` and every pre-existing `tests/` file. The
investigation ground truth is `docs/ANALYSIS-82.md` (card s1, verdict
accepted by main); this entry does not restate its evidence chain.

### Added

- **`tests/test_issue82_gateway_start_uniqueness.py`** (card s2, task
  `t_5f6acc24`). Static guard nailing the invariant "the entry's last line
  can bring the gateway up" — today maintained by luck, previously covered
  by zero tests. It pins G1 the last executable line of
  `deploy/main_entry.sh` is exactly `exec hermes gateway run` (on image
  v2026.9.24 this line is the ONLY bootstrap that starts the s6
  `gateway-default` slot on a fresh deploy), G2 no second gateway-starting
  statement anywhere else in the entry's executable code, G3 the compose
  `command:` still points at `/opt/mwroot/deploy/main_entry.sh`, G4 no
  `HERMES_GATEWAY_NO_SUPERVISE` / `--no-supervise` / `gateway-default` /
  `s6-svc` in the compose (we never took over the image's s6 orchestration),
  G5 no `s6-svc -d` / `s6-svc -t` in the entry. Self-check cases follow the
  `test_install_bind_mount_skill_soul.py` precedent: every mutation happens
  on an in-memory copy only, each first asserts the break-point actually hit
  (anti false-green) before asserting the same assertions turn red —
  covering `exec sleep infinity`, `exec tail -f /dev/null`, deleting the
  last line, commenting it out, inserting a second `hermes gateway run`,
  injecting `s6-svc -d`, rewriting the compose command, and injecting each
  banned s6 keyword.

### Investigation outcome (issue #82 — NOT fixed, recorded as-is)

- On image v2026.9.24 the entry's `exec hermes gateway run` is rewritten by
  the image's own `_maybe_redirect_run_to_s6_supervision`
  (`hermes_cli/gateway.py:4428`) into `s6-svc -u gateway-default` +
  `execvp("sleep","infinity")`: it does **not** spawn a second gateway. The
  `exec sleep infinity` fix proposed in issue #82 is therefore a runtime
  no-op AND removes the only slot bootstrap — on a fresh deploy (no
  `gateway_state.json`) the reconciler registers `gateway-default` with a
  `down` marker (`action='registered'`, not `started`) and s6 never starts
  it. Vetoed; see ANALYSIS-82 §2.
- The reported symptom (console `gateway unreachable`) was **not reproduced
  on 170** at this baseline (single gateway, `127.0.0.1:8642` → HTTP 401,
  `gateway-starts.log` 16 lines over 55 h). The real cause is **not yet
  located** — field forensics on `.190` are **still pending** (not started:
  the tester card did not touch `.190`, see `docs/TEST-REPORT-82.md`) and
  need a **dedicated read-only forensics card**; this entry deliberately
  does not claim a fix.

### Verification

- `bash -n deploy/main_entry.sh` clean; the new suite is 16/16 green
  (`uvx --from pytest pytest tests/test_issue82_gateway_start_uniqueness.py -v`).
- Reverse verification on a scratch copy of `deploy/`: eight deliberate
  break variants (R1–R8) each turned the corresponding guard red — raw
  output in the task workspace `docs/IMPL-NOTES-82.md`.
- Full-suite baseline-vs-after FAILED/ERROR set diff empty (no new reds);
  logs in the task workspace `.scratch/pytest-{baseline,after}-82.log`.
  No UI copy was touched, so the i18n docs gate (`--strict`) does not apply
  to this change (noted in IMPL-NOTES-82).

## Unreleased — Issue #75: web console loading split + windowed history (2026-10-21)

Range: commits `5dda6fd^..4a699d5` on `main` (6 commits — `5dda6fd` B1 backend,
`aaf7eb6` B2 frontend layering, `d888c7e` B3 pagination + dist, then the
in-chain fixes `4d83a43` / `502595c` / `4a699d5`; 20 files, +3056/-428). The
workspace process docs `docs/REQ-75.md`, `docs/ARCH-75.md`,
`docs/IMPL-NOTES-75.md`, `docs/REVIEW-75.md`, `docs/TEST-REPORT-75.md`,
`docs/FIX-NOTES-75.md`, `docs/FIX-NOTES-75B.md`, `docs/FIX-NOTES-75C.md` and
`docs/RETEST-75.md` are the ground truth for this entry; open items are
registered in Verification below, not smoothed over.

### Changed

- **Windowed `/messages` in the console proxy** (`5dda6fd`). `GET /messages`
  with `want`/`before` is now served by the console instead of being passed
  through: the gateway fetch window is `W = clamp(ceil(want / d * safety),
  20, 200)` (tail-anchored `offset = max(0, boundary - W)`, adaptive doubling
  at most 3 times, cursor format `o:<n>`), conversation items group their
  tool calls into a `tool_group` field and `thinking`/`reasoning` never ship.
  Without the parameters the endpoint keeps the exact legacy full passthrough
  (the documented rollback switch). New endpoints: `GET /api/heartbeat`
  (single-source reaper liveness for the 5 s poll; the `reaper_alive` rule
  stays line-for-line identical to `api_workers`) and
  `GET /api/tasks?fields=active` (active-run summary with field semantics
  identical to `team_work_active`).
- **`/api/tasks` list is a whitelist projection** (`5dda6fd`, tightened by
  `4d83a43`): `harvest_log` / `workspace` / `exit_code` / `question` /
  `options` are stripped and `result` is reduced to a `has_result` boolean;
  full records go through the new `GET /api/tasks/<id>` detail endpoint.
  `block_kind` stays whitelisted so the attention count keeps its precise
  `needs_input` semantics.
- **Loading layering on first paint** (`aaf7eb6`, `d888c7e`, `4d83a43`). The
  5 s timer only pulls `/api/heartbeat` (~55 B); tasks counts refresh on
  mount/view change exactly once; the active-board poll uses
  `fields=active`. `/api/workers` no longer carries base64 avatars (the
  732 KB dead weight; bytes are served from `/api/workers/<name>/avatar`,
  plain filenames kept). The reaper top-bar dot is tri-state (`ok` / `bad` /
  `dim` + unknown label). `ChatView` takes an `active` prop gating bootstrap,
  history restore, the 4 s poll and workspace loads. Measured first-screen
  API set: 7 requests, 66.3 KB (acceptance budget 100 KB).
- **ChatView history is windowed instead of full-dump** (`d888c7e`). First
  load is `want=10`; "Load earlier messages" prepends strict
  `before_cursor` batches with scroll anchoring, batches dedupe by an id
  watermark, the button hides when `has_more=false`, and auto-scroll only
  fires within 40 px of the bottom. The 1.27 MB×2 full history fetch on
  session entry is gone.
- **Error attribution + i18n** (`d888c7e`, `502595c`). All HTTP GET endpoints
  route through `net()` into `ApiError{kind,status,endpoint}` with stable
  path templates (`/api/workers/:name`), fail-soft semantics unchanged;
  locale keys 644 → 650 (bilingual, same commits) with the
  `ui/tests/test_issue58_tokens.mjs` anchor raised to match.
- **Test suites gained for the new surfaces**:
  `tests/test_issue75_messages_window.py`, `tests/test_issue75_heartbeat.py`,
  `ui/tests/test_issue75_chat_asi.mjs`.

### Fixed

- **Issue #75 white screen on session pick (P0, `4a699d5`).** An ASI hazard
  introduced by `d888c7e`: `didLoadHistRef.current = true` followed by a line
  starting with `(` parses as `true(async () => …)()` →
  `TypeError: true is not a function` → the whole React tree unmounted. Fixed
  with a `void` prefix (the repo keeps a no-semicolon style, so `void` is the
  ASI-immune form); the shipped bundle now greps `=(!0)(async` = 0 and the
  repo-wide same-pattern scan is 0. Guardrail `ui/tests/test_issue75_chat_asi.mjs`
  (10 cases: render-level, bundle scan, gate semantics, repo-wide scan) turns
  9/10 red if the baseline code is restored.
- **Issue #75 failed first history load never retried (P2, `4a699d5`).** The
  idempotence gate stayed `true` after a failed pull, so switching tabs back
  never re-fetched and the error bar had no way out. Now the gate rolls back
  on failure (switch-away-and-back auto-retries) and the error bar carries a
  Retry button (zero new i18n keys); race discipline via an in-flight ref,
  pending/current sid refs and a retry tick.
- **Windowed `/messages` always 502 (P0, `4d83a43`).** The console read
  `message_count` at the top level while the gateway detail response nests it
  under `session`; nested read first with a top-level fallback, both missing
  still returns an explicit 502 (never a guessed offset).
- **AC-03 first-screen budget overrun (P1, `4d83a43`).** The list projection
  kept a truncated `result` (~240 KB first paint with the 5 s poll re-pulling
  the full list); `result` reduced to `has_result`, the duplicate first-pull
  removed, active polling moved to `fields=active`.
- **Deep pagination duplicated whole batches (P0 blocker B-1, `502595c`).**
  With `boundary < W` a cursor page's fetch `limit` could re-read already
  shown records; the limit is now clamped to the boundary and window doubling
  is disabled for cursor pages, plus a seen-set filter on the frontend
  prepend path (double insurance). Regression cases fail if the pre-fix code
  is restored.
- **GET failure attribution coverage (M-1, `502595c`).** See Changed.
- **Fallback page DIR/exit_code columns were always '-' (M-2, `502595c`).**
  The no-JS fallback page now lazily backfills the two fields per task from
  `GET /api/tasks/<id>` (per-task once, 30 per round, pending guard,
  per-request fail-soft).
- **New pytest suites were vacuously green (M-3, `502595c`).** `check()`
  results never reached an assert; a decorator now converts each case's
  failures into real pytest assertions (direct-run summary semantics kept).
  This surfaced 7 direct-run failures, 6 of which were expectation bugs in
  the tests themselves (fixed with reasoning pinned in comments) and 1 was
  the real B-1 product defect (fixed, expectations untouched).

### Verification

- Browser retest `docs/RETEST-75.md` verdict PASS 7/7 on tip `4a699d5` (D-1
  both paths with the white screen reproduced on the `502595c` control, C-1
  fail-visible → Retry → recovery, AC-10/11/12/13/14 UI); the earlier runtime
  pass `docs/TEST-REPORT-75.md` covers AC-01~09/15, first screen 66.3 KB, and
  HTTP-side pagination 12 batches with 0 overlap 0 gap.
- `cd ui && npm test` 7 suites green (incl. the 10 new ASI guardrail cases
  and the 650-key anchor). `pytest` tip vs `502595c` same-venv: 9 failed /
  534 passed both sides, failure-set diff empty — zero new reds (the 9 are
  pre-existing environment reds, same set as the s5 archive).
- `ui/dist/index.html` and `mw/ui_dist/index.html` byte-identical
  (md5 `4b150a90…`, deterministic double build);
  `tools/i18n_docs_sync.py --strict` exit 0 (F1 CJK=0, F3' docs tracked == 2
  souls).
- Not closed, registered as-is (follow-ups tracked as Gitea #80/#81, split
  owned by the chain coordinator, issue text not readable from this worker
  container): five non-blocking review suggestions from `docs/REVIEW-75.md`
  §3 — items 2–5 in code (client-4xx wording, non-numeric `want` returning
  500 instead of 400, truncated-body error re-parse, `api_workers` not
  reusing `_reaper_hb_age()`) and the dead `chat.session.no_more_history`
  locale key (item 1). **These five were implemented in the later
  "Issue #81/#80" entry above** (this paragraph is the state as of the
  #75 merge; the #81/#80 entry supersedes it). The `WorkersView`
  edit-dialog `avatar:''` write-back is a pre-existing issue #51 surface
  (review accepted it as out of scope). RETEST-75 §5 observations stand as
  optimisation items, not defects: the 4 s poll re-reads the whole window and
  relies on client-side id dedup (a server-side incremental protocol is the
  follow-up), and real machine 170 SSH was unreachable for three consecutive
  sessions, so the browser tests ran on the isolated in-container stack with
  same-git-content code; a 170 re-run plus bilingual smoke is recommended.

## Unreleased — 遗留 issue 清理批 (#74/#73/#72/#68/#59) (2026-10-20)

Range: commits `3e2f11e` (batch A — #72/#74/#68/#73, parent `b2b87b2`) and
`fc52b3e` (batch B — #59 guardrail rework) on `main`. The static review
`docs/REVIEW-CLEANUP.md` and the runtime report
`docs/TEST-REPORT-CLEANUP.md` (workspace) are the ground truth for this
entry; open items are registered in Verification below, not smoothed over.

### Fixed

- **Issue #74 locale duplicate key** (`3e2f11e`). Deleted the top-level
  flat key `chat.errors.file_read_failed` from both
  `ui/src/i18n/locales/en.json` and `zh-cn.json` (-1 line each, at the
  pre-change line 616 of both files), keeping the nested
  `chat` → `errors` → `file_read_failed` form that i18next actually
  resolves. Unique key set stays at 644 (leaf count 645 → 644); the
  `ui/tests/test_issue58_tokens.mjs` 644 anchor was not touched. i18next
  under the app's full config renders byte-identical values before vs
  after the deletion (en `"Read file failed"` / zh-cn `"读取文件失败"`).
- **Issue #68 dirty-float token display** (`3e2f11e`). New `truncTokens()`
  in `ui/src/views/TasksBoardView.jsx` truncates totals toward zero
  (`Math.trunc`; non-numeric / NaN / Infinity → 0, same defence as
  `formatTokens`), applied to the `tokenLabel` and `tokenTitle` totals —
  `150.5` now renders `"150"`. The aggregation side `_aggregate_run_tokens`
  int gate in `mw/console.py` is deliberately unchanged (main ruling:
  display-side only); the `0` / missing / `source=unavailable` branches
  keep their exact prior behaviour. `ui/dist/index.html` and
  `mw/ui_dist/index.html` rebuilt in the same commit, byte-identical
  (md5 `5c1d9f15…`).
- **Issue #72 drain disconnect log typing** (`3e2f11e`). The upstream
  drain loop in `mw/console.py` now catches upstream read failures inside
  the existing `suppress(Exception)` shell and prints a distinct
  fingerprint line `[proxy] upstream gone during drain` (method + console
  path + gateway path + exception class name only — never headers,
  credentials or query strings), so B-type exits (upstream gone mid-drain)
  and A-type exits (read to EOF) are tellable at a glance. A-type
  semantics are untouched: log wording, drain loop, `DRAIN_MAX_S` deadline
  and `close_connection` behaviour are line-for-line unchanged, and the
  swallowed-exception set is identical to before the change. New cases
  D8–D10 in `tests/test_issue63_proxy_drain.py` pin fingerprint presence,
  EOF silence, and no-credential leakage.
- **Issue #73 DEPLOY.md env record** (`3e2f11e`). Exactly one row added to
  the §3 ops table: `MW_PROXY_DRAIN_MAX_S` (default 3600 s, upper bound on
  how long the console keeps draining after the browser drops; `0`
  disables draining altogether — the rollback switch), worded against the
  implementation at `mw/console.py:3128`; the line carries zero CJK.
- **Issue #59 install/entry guardrail rework** (`fc52b3e`, test-side +
  entry-script only — `deploy/install.sh` has a zero-line diff). T8 in
  `tests/test_install_bind_mount_skill_soul.py` no longer hand-copies
  install.sh §6: it slices the live section between the stable markers
  `# ── 6. Skills + SOUL staging` … `# ── 6.5 --preserve-soul` and runs
  it, with a self-proving case (break a §6 copy → the same assertions
  turn red) and a hard `assert` when a marker is missing — no silent
  fallback to a copy. T7's whole-line
  `docker exec "${MAIN_CONTAINER_NAME}"` exemption is removed: the
  allowlist is narrowed to six enumerated read-only subcommands
  (cat/ls/test/stat/sha256sum/md5sum), write-operation detection runs
  before the allowlist, shell control characters are blocked, backslash
  continuations are merged into logical lines before scanning, and 20
  reverse write-fragment cases plus 4 read-only no-false-positive cases
  pin the judgement. `deploy/main_entry.sh`'s SOUL guard now clears a
  pre-existing directory at the canonical path (`[ -e ] && [ ! -L ]` then
  `rm -rf` on the literal path) so `ln -sfn` can no longer land the link
  inside a directory; the pinned `ln -sfn` line itself is byte-unchanged
  and two new T11 cases cover the directory scenario.

### Verification

- Full pytest baseline diff (`docs/TEST-REPORT-CLEANUP.md` §1, same venv,
  pre-change run on a `b2b87b2` worktree): 9 failed / 488 passed before vs
  9 failed / 518 passed after, `FAILED` set name-identical — zero new
  reds. The 9th failure `test_t10_abs_compose_pipeline_directory_mounts`
  is environmental (mktemp Permission denied when TMPDIR is not writable;
  that one case passes on both sides with a writable TMPDIR) and is
  tracked as Gitea #76, not a code regression. Earlier in-chain runs on a
  writable-TMPDIR machine measured 8 failed / 492 (batch A) and 8 failed /
  519 (batch B) with the same empty failure-set diff.
- `cd ui && npm test` exit 0, all suites green, including the six #68
  integerisation cases (150.5 → "150", negative truncation, non-numeric →
  0, three-branch behaviour unchanged) and the #74 locale check
  (top-level flat key deleted, nested form kept) — 29 passed in
  `test_issue58_tokens`.
- `tools/i18n_docs_sync.py --strict` exit 0 (F1 CJK=0 root 3, F3' docs
  tracked == 2 souls); `ui/dist/index.html` and `mw/ui_dist/index.html`
  byte-identical to each other and to their committed blobs
  (`cmp` ×2 empty, md5 `5c1d9f155ad517534404feeb532de63e`).
- Targeted runtime proofs: i18next rendered byte-identical values
  before/after for #74 with the top-level flat key confirmed deleted;
  D1–D10 of `tests/test_issue63_proxy_drain.py` pass, plus an
  out-of-pytest real-stdout probe showing B-type prints the fingerprint
  and A-type does not; #68 verified in a real Chromium against the
  committed dist (step row renders "150"); #59 target file 37 passed
  including the N-a self-proof and the N-b reverse cases, `bash -n` clean
  on both deploy scripts.
- Not closed, registered as-is: the 170 real-machine B-type
  upstream-disconnect reproduction is NOT TESTED — killing the upstream
  gateway mid-drain would interrupt a live production chain and needs a
  user-scheduled window. The browser smoke covers the render layer of the
  committed dist over a mock API; the backend `worker_report.tokens`
  injection chain is issue #58's existing coverage, outside this batch.
  Two non-blocking review suggestions in `docs/REVIEW-CLEANUP.md` were
  not implemented in this batch: pinning an assertion that install.sh
  currently contains no `docker exec` lines behind the T7 allowlist, and
  truncated printing of the exception message in the #72 fingerprint.

## Unreleased — Issue #63: SSE disconnect no longer interrupts the running turn (2026-10-20)

Range: commit `e331eb0` on `main` (parent `9b41214`). This entry supersedes
the "Upstream behavior note (#63)" of the 2026-10-19 batch entry below. The
interrupt trigger itself still lives upstream in the Hermes agent (an SSE
client disconnect interrupts the live turn); what changed is that the repo
now owns a fix surface for the dominant case: in the shipped topology the
disconnect the gateway sees is produced by our own same-origin proxy, whose
forward loop serially couples browser→console→gateway, so a browser-side
break used to propagate upstream and kill the turn.

### Fixed

- **Issue #63 proxy keep-alive via upstream drain** (`e331eb0`,
  `fix(issue63): mw 同源代理 SSE 断连转排空保活 + ChatView 显式停止改原生 run stop`).
  `Handler._proxy_stream` in `mw/console.py` now wraps the line-forwarding
  loop in `try/except (BrokenPipeError, ConnectionResetError, OSError)`:
  when a downstream write fails the exception is swallowed inside the
  function (it no longer escapes to `do_GET`/`do_POST`, which previously
  closed the upstream `with resp:` and made the gateway fire its
  SSE-disconnect interrupt) and the handler switches to drain mode —
  reading the gateway stream to natural EOF so the turn runs to completion
  and is persisted normally. The drain is bounded by the new class constant
  `DRAIN_MAX_S` (`MW_PROXY_DRAIN_MAX_S`, default 3600 s, same env-override
  style as `PACE_*`; `0` = no drain, the rollback switch that restores the
  old behaviour). `close_connection` is set to `True` only on the
  disconnected path (the normal path keeps its keep-alive semantics). Two
  log lines give the console-side fingerprint: `downstream gone; draining
  upstream to completion` on entry, `drain timeout, abandoning` on
  deadline (suspected upstream-side break or gateway anomaly); both print
  method + console path + gateway path only, never headers. Because
  `_proxy_to_gateway` and `_proxy_to_worker` share `_proxy_stream`, the fix
  covers the whole proxied SSE surface in one place.
- **Explicit Stop now uses the native run-stop endpoint** (`e331eb0`).
  With the proxy keep-alive in place, aborting the local fetch is no longer
  the same as stopping a turn, so the UI's explicit Stop button must ask
  the gateway first: `ui/src/gateway.js` gains `stop(runId)` →
  `POST /v1/runs/{run_id}/stop` (runId `encodeURIComponent`-ed, written
  per the `steer` precedent, routed through the existing `/v1/runs` proxy
  whitelist with the server-side injected key), and `ChatView.jsx` Stop now
  awaits `g.stop(runId)` before `abort()`. Session-switch / unmount /
  page-close paths deliberately keep abort-only (no stop) — that is exactly
  the scenario the keep-alive protects. New locale key
  `chat.errors.stop_failed` (en `"Stop failed: {err}"` / zh-cn
  `"停止失败：{err}"`) in both `en.json` / `zh-cn.json` in the same commit
  (unique key set 643 → 644; the `test_issue58_tokens.mjs` key-count anchor
  was raised accordingly, same procedure as its own 639→643 precedent).
- Regression assets: `tests/test_issue63_proxy_drain.py` (8 cases: drain to
  exhaustion, normal-path byte-identical passthrough, deadline timeout
  exit, `DRAIN_MAX_S=0` rollback semantics, `ConnectionResetError` in the
  same catch set, non-streaming HTTPError/502 branches unaffected,
  `close_connection` semantics both ways; local stubs, zero real network)
  and `ui/tests/test_issue63_stop.mjs` (6 cases: stop request shape, worker
  prefix + runId encoding, non-2xx error propagation, stop-before-abort
  order, newChat never calls stop, bilingual key symmetry), wired into
  `npm test` (now 6 suites). `ui/dist/index.html` and
  `mw/ui_dist/index.html` rebuilt in the same commit and byte-identical
  (sha256 `2a12e696…`).

### Known limitations

- Race window: pressing Stop before the `run.started` event arrives leaves
  `runIdRef` null, so the click degrades to abort-only (the keep-alive then
  takes the turn over instead of stopping it); the window is normally
  milliseconds. Registered as review concern C1.
- The catch block wraps the whole forward loop, so an *upstream* read break
  also logs `downstream gone` first — a fingerprint noise source when
  triaging B-type disconnects (concern C2). The drain deadline is checked
  per frame; a long zero-byte stall falls back on the `urlopen` 1800 s
  timeout rather than `DRAIN_MAX_S` (concern C3). All three are non-blocking
  per `docs/REVIEW-63.md` (workspace) and handed to runtime verification.

### Verification

- Static review `docs/REVIEW-63.md` (workspace) verdict PASS: 11/11 files,
  six deviation registrations all accepted, bilingual mirror and dist
  discipline re-measured independently.
- Gates measured at `e331eb0`: `pytest` full run 489 passed / 8 failed —
  baseline 481/8 plus this commit's 8 new cases, FAILED set name-identical
  to baseline (zero new reds); `cd ui && npm test` 6 suites green;
  `tools/i18n_docs_sync.py --strict` exit 0; dist pair sha256-identical.
- Real-machine runtime retest (typed disconnect against the 170 test
  machine, explicit-stop round trip, concerns C1–C3) — executed
  2026-10-20: verdict PASS in `docs/TEST-REPORT-63.md` (workspace), hard
  acceptance 3/3 on the 170 machine with an old-code control group,
  zero new regressions.

## Unreleased — Issue #58: run/task token consumption display (2026-10-19)

Range: commit `775dd3c` on `main` (parent `0f7ecff`). This entry covers the
`feat(tokens)` commit only. The commits between the previous entry's range
tip (`b6c2432`) and `0f7ecff` — the #67 test-harness rework (`a2de5e4`) and
the skill/prompt polish commits (`5a43dae`, `6b14414`, `ea6f76e`,
`0f7ecff`) — are not covered here; they are listed as an open housekeeping
item in `docs/DOC-AUDIT-58.md` (workspace) pending a separate disposition.

### Added

- **Issue #58 run/task total token display** (`775dd3c`,
  `feat(tokens): issue #58 run/task 级 token 消耗量显示`). Settlement-time
  snapshot in `mw/harvest.py`: a new `_capture_tokens()` fetches the worker
  session object once per card (one HTTP request, 5 s timeout,
  `Authorization: Bearer` header, fail-open to an all-zero
  `source="unavailable"` snapshot) and merges a 9-key `tokens` sub-dict into
  every `worker_report` (COMPLETE / FAILED / CLARIFY paths). `total_tokens`
  is the five-bucket sum computed once at write time (single-source
  invariant; the console layer never recomputes it).
- Read-time aggregation in `mw/console.py`: `_aggregate_run_tokens()` feeds
  both `GET /api/team-works` and `GET /api/team-work/<id>` with an injected
  `token_total` (same source, same semantics on both outlets); the run
  detail additionally injects the parsed `worker_report` into `cards[]`
  (main ruling 1, additive only). Orphan-card snapshots count toward the
  run sum.
- Read-only token segments at three board places
  (`ui/src/views/TasksBoardView.jsx`): TaskCard run header, StepTimeline
  step row, and CurrentTaskCard, with a hover `title` carrying the five
  buckets, cost, capture time and source (`formatTokens` renders thousands
  separators and the `1.2M` abbreviation). Zero new interactions: no
  buttons, no dialogs, no new polling timers.
- Four `tboard.*` i18n keys (`token_short_label`, `token_cost_label`,
  `token_missing`, `token_detail_title`) in `en.json` / `zh-cn.json` with
  fully equal key sets (643/643). Note: `token_cost_label` is a
  spec-reserved key with no current consumer in `ui/src` (the run-level
  line renders `/cost` inline); the reservation intent is declared in
  `docs/DOC-AUDIT-58.md` (workspace).
- Regression assets: `tests/test_issue58_tokens.py` (12 cases, local HTTP
  stub, zero real network) and `ui/tests/test_issue58_tokens.mjs` (22
  cases, including the i18n key-set parity machine check per main ruling
  2), wired into `npm test` (now 5 suites). `ui/dist/index.html` and
  `mw/ui_dist/index.html` rebuilt in the same commit and byte-identical
  (md5 `57ecc1dcafa0f8e8db4dc76a7df5fa91`).

### Verification

- Audit-time reruns at `775dd3c` (`docs/DOC-AUDIT-58.md`):
  `pytest tests/test_issue58_tokens.py` 12 passed; `cd ui && npm test`
  5 suites green (last suite 22/22); `tools/i18n_docs_sync.py --strict`
  exit 0; `ui/dist` vs `mw/ui_dist` `cmp` empty.
- Full-chain runtime evidence: `docs/TEST-REPORT-58.md` + `evidence-58/`
  (24/24 AC PASS on the 170 test machine worktree at `775dd3c`, baseline
  key-set diff `added={token_total,worker_report}`, `removed=∅`).

## Unreleased — Batch A/B/C + H-1: paced proxy writes, chatLinks multi-path, file-browser API, docs wording (2026-10-19)

Range: `68910d1..b6c2432` on `main` (lineage `68910d1 → 665c8ea → 463a612 →
4c35b24 → 8c64a33 → 38df212 → e4335e5 → 6bfcb60 → 335fe9a → f64d117 →
b6c2432`; 10 commits, no external commits).

This range carries the three fix batches (A = middleware code, B = UI,
C = docs wording) plus the H-1 rework that batch B's chatLinks fix
triggered. The previous entry's own tail (`665c8ea`) closed the
#62/#63/#64/#65 changelog gap; every commit after it was undocumented
until now.

### Fixed

- **Apple container proxy truncates large responses** (`463a612`). On the
  Apple container port proxy a burst write above ~128 KB was cut short by
  an early FIN (measured on the 190 test machine: 642 KB truncated 92/100,
  176 KB 10/10; responses at or below 90 KB always passed and SSE streaming
  was unaffected). `Handler._write_paced` now splits bodies over 64 KB into
  chunks with a 25 ms pause between them so the proxy userland can drain;
  bodies at or below 64 KB (SSE frames, small JSON) still take the direct
  path, with no latency regression. Chunk size and delay are tunable via
  `MW_PACE_CHUNK` / `MW_PACE_DELAY`, with `MW_PACE_DISABLED` as the kill
  switch. Applied to `GET /` (642 KB), the `_send` helper (team-work
  detail/state), `_proxy_stream` (gateway chunked JSON, single lines up to
  MB size) and `console_files._stream_file` (discipline only, no behaviour
  change). Test anchors SW5~SW8 cover the chunking logic, the sleep count,
  the pass-through branch and disconnect semantics; the SW1~SW4 short-write
  regression anchors still pass. Data basis: `docs/ANALYSIS-PROXY-TRUNC.md`.
  References issue #62 (supplement) and #64.
- **#66 pixel proxy wrote the response in one shot** (`8c64a33`).
  `mw/pixel_proxy.py` called `handler.wfile.write(data)` directly and was
  therefore exposed to the same truncation; it now routes through
  `Handler._write_paced` like the other write paths.
- **#47 file-browser listing sorted case-insensitively** (`38df212`).
  `api_fs_list` sorted by `name.lower()`, which made the order depend on
  the locale's notion of case folding; the sort key is now the plain
  code-point order `(not dir, name)`. #49 in the same commit dropped the
  unused `project` / `rel` parameters from `_stream_file` and folded the
  `Content-Disposition` branch into a single expression.
- **#48 chatLinks matched several project paths in one text node**
  (`e4335e5`, reworked by `b6c2432`). `enhanceFileLinks` now resolves
  multiple paths inside the same text node. The first implementation
  replaced nodes back-to-front, so the first `replaceChild` detached the
  node and the second iteration dereferenced a null `parentNode` — the
  review round (H-1) caught it, and `b6c2432` rebuilt the replacement as a
  single ascending pass that constructs one fragment and swaps it in once;
  the text between two hits (e.g. " and ") is now preserved. Four
  regression cases were added (2 / 3 / 5 paths in one node, first and last
  path); `ui/tests/test_chatlinks.mjs` passes 47/47. The UI bundle was
  rebuilt in the same commit (`ui/dist` + `mw/ui_dist`, md5 `7cc24b3f`).
- **#43 the collapse badge showed "0 lines"** (`6bfcb60`).
  `splitGoalSegments` counted hidden content from the remaining lines only,
  so a goal truncated mid-line (a character-level cut, not a whole hidden
  line) reported zero hidden lines and the badge showed `0`. The function
  now tracks character-level truncation and reports the cut line as hidden
  content; the trailing-whitespace nit (L-1) in `TasksBoardView.jsx` was
  cleaned up in the same commit.
- **#44 stale comment in the prompt-template resolver** (`4c35b24`).
  `mw/pipeline.py` behaved as "parse failure degrades to the en template"
  but the code had degraded to `en` without saying so; the comment now
  states the actual behaviour. Comment-only change.

### Changed

- **#55-D7 dead i18n key removed** (`335fe9a`). The key that described an
  avatar registration flow which no longer exists was deleted from both
  `ui/src/i18n/locales/en.json` and `zh-cn.json`. A repo-wide grep for
  that key name now returns zero hits (code, docs and build artifacts).
- **#55 D-5 / D-6 / D-8 + #57 N-b / N-c docs wording** (`f64d117`).
  `design.md` now describes the template family as bind-mounted rather
  than "laid down" by the installer; `DEPLOY.md` §1 describes the SKILL
  step as a bind-mount setup instead of distributing a SKILL install; the
  `README.md` `docs/` row no longer advertises audit notes that the repo
  does not ship; `deploy/verify.sh` tells the operator to run
  `hermes kanban init` (or recreate the container) when `kanban.db` is
  missing, matching the fact that the installer no longer initialises
  Kanban; and the `DOCKER_CMD=` line was dropped from the T7 whitelist in
  `tests/test_install_bind_mount_skill_soul.py`.

### Changelog housekeeping

- **Previous entry's tail closed its own gap** (`665c8ea`): the #62
  (supplement) / #63 / #64 / #65 entries plus the upstream-behaviour note,
  and two comment corrections (`design.md` points the `harvest_status`
  comment at `mw/ledger.py::_STATE_TRANSITIONS`; `mw/pipeline.py` notes
  that the reaper does not yet book the resume state reset).
- **#55 carry-over line qualified** (this entry): the tail of the previous
  entry still described #55 as "carry-over, audit D-5..D-8"; D-5, D-6, D-7
  and D-8 have since landed in `335fe9a` and `f64d117` (see above), so the
  line now says so instead of implying the whole item is still open.

### Verification

`docs/REVIEW-BATCH-1910.md` (static review of the batch, 7 commits) and
`docs/REVIEW-H1-RECHECK.md` (re-review of the H-1 rework) both PASS.
`docs/TEST-BATCH-1910.md` (runtime retest, 8 commits) PASS with a
conditional acceptance — the failures it lists are pre-existing environment
issues, not introduced by this range. `docs/TEST-INVENTORY-1910.md`
inventories the 54-case test baseline. `ui/tests/test_chatlinks.mjs`
47/47, `node --test ui/tests/*.mjs` 4 suites green, and
`tools/i18n_docs_sync.py --strict` F1/F3' OK.

## Unreleased — Issues #62(supplement) / #63 / #64 / #65 (2026-10-19)

Range: `1dc17fd..68910d1` on `main` (lineage `1dc17fd → e28b032 → 68910d1`).
The `adc4de4` #62 fix in the previous changelog entry covered TOCTOU and
BrokenPipe, but left the macOS-specific short-write vulnerability (no internal
buffer when `wbufsize=0`); #64 is the same root cause hitting the
`_proxy_stream` path. #63 is an upstream Hermes behavior: SSE disconnections
trigger interrupts that halt the agent turn; no repo fix required. #65 was a
full blind-spot for WAITING cards — they never got harvested at all.

### Fixed

- **#62 (supplement) macOS short-write remnant & #64 same root cause in `_proxy_stream`**
  (`e28b032`). The `adc4de4` fix for `ERR_CONTENT_LENGTH_MISMATCH` guarded
  against TOCTOU and BrokenPipe, but on macOS (and any system where the
  underlying `SocketIO` reports `wbufsize=0`) a bare `wfile.write(bytes)` can
  silently drop bytes when the write call returns a count smaller than the
  buffer length. Seven call sites in `console.py` and two in `console_files.py`
  now use `_write_all()` which loops around `wfile.write()` until the whole
  buffer has been accepted (8 replacements in total, four `A-2` sites in
  `_stream_events`, two `A-3` sites in `_proxy_stream`, two `A-4` sites in
  `_pass_through`). The same fix automatically covers the `_proxy_stream` path
  that triggered #64 (`console.py:3100`), so #64 has no independent commit.
- **#65 WAITING cards were never harvested** (`68910d1`). The reaper only
  scanned PENDING/ERROR/BLOCKED, leaving WAITING cards (created by main
  `pause` or handoff) permanently invisible. Three simultaneous fixes:
  1. **C-1 resume actor white‑list** (`actor` in workflow steps can now be
     `resume`; `resume_task` had already been calling `update_worker_state`
     with `actor="resume"` but that actor was missing from the white‑list,
     raising PermissionError in step 1 of the resume flow);
  2. **C-2 resume_task idempotent rollback** + anti‑deadlock: if a card is
     already PENDING it skips the migration (PENDING→PENDING is illegal
     in `_STATE_TRANSITIONS`) and still continues to step 2 (slot re‑claim),
     so that `pause→resume` cannot deadlock on a state ValueError;
  3. **C-3 M3 waiting‑timeout sweep** (`mw/reaper.py:431–441`) queries
     `harvest_status='WAITING'` when `MW_REAPER_LATE_SETTLE` is true (default)
     and the run has no current card; the same `finish_as` logic as PENDING
     applies, delivering a verdict of `late_settled` with outcome based on
     TODO.md state. This closes the full blind‑spot.

### Upstream behavior note (#63)

> Superseded 2026-10-20 — see the Issue #63 entry at the top of this file
> (commit `e331eb0`): the repo now owns a fix surface for the dominant case.
> The text below is kept unmodified as a point-in-time record of what was
> believed when it was written.

- **SSE disconnections cause Hermes agent interrupts** — this is an upstream
  Hermes‑agent‑specific behavior: when the event‑stream transport is broken,
  the agent receives an interrupt signal and stops its turn (the server side
  of the SSE connection can't send more events in that session). The repo
  contains no fix surface; see the analysis at `docs/ANALYSIS-61-65.md` L70/L162.

### Verification

`docs/REVIEW-61-65.md` verdict PASS (0 P0/P1). `docs/TEST-61-65.md` (170 real
machine): 13 new automated checks pass, including the 12 tests introduced by
`e28b032` and `68910d1`; GET / 200×zero‑trunc + 304 20/20 + concurrency 50/50,
sha256 sum‑check passes; proxy‑direct‑write byte equality passes; pytest failure
set identical to the `1dc17fd` baseline (zero new).

---

## Unreleased — Issues #60/#61/#62 + stamp deployment-chain fix (2026-10-18)

Range: `9fc330f..679108f` on `main` (lineage `9fc330f → 9e0e651 → ca9323a →
adc4de4 → 679108f`, no external commits).

### Fixed

- **#60 Settings page validated `hermes_webui_url` as an integer**
  (`9e0e651`). Regression introduced by `ec87b09` (2026-10-14 i18n rework),
  which dropped the `key.endsWith('_seconds')` guard from `validateSeconds`,
  making every non-bool field integer-validated — URLs/paths became `NaN`
  → invalid → save disabled. Restored as `validateField(key, raw)` with
  per-type dispatch: `*_seconds` keeps integer ≥ 1; `hermes_webui_url`
  allows empty (= hides the top-bar entry) and otherwise requires an
  `http(s)://` prefix, matching backend `mw/console.py::_clean_webui_url`;
  other text fields skip numeric validation. New locale key
  `settings.invalid_webui_url` (zh-cn + en); UI bundle rebuilt in the same
  commit per the b9bfb66 convention.
- **#61 reaper sqlite handle leak** (`ca9323a`, plus review sweep in
  `adc4de4`): the `_send_to_main` success path was the only one of seven
  `Ledger(db_path)` sites in `reaper.py` without `try/finally` — an inline
  `Ledger(db_path).confirm_delivered(t)` leaked one sqlite fd per delivery
  until EMFILE killed the long-running reaper. `adc4de4` closes the same
  class of leaks in `console.py` (8 sites incl. the hottest one: the SSE
  `_stream_events` loop opening a fresh Ledger every 2 s, plus
  `api_dispatch`/`api_resume` inline construction).
- **#62 Console occasionally unopenable (`ERR_CONTENT_LENGTH_MISMATCH`)**
  (`adc4de4`): `do_GET \"/\"` read each candidate `index.html` twice — once
  in `_passes` for the md5 check, once again for the body — so a concurrent
  rebuild could swap the file between reads and desynchronise
  `Content-Length` from the bytes on the wire; and a bare `wfile.write`
  bubbling `BrokenPipeError` on early client disconnect stacked a 500 onto
  the half-sent 200 response. `_passes` is now `_read_if_pass`, reading the
  file once and serving the same bytes it hashed, and both write paths
  catch `BrokenPipe`/`ConnectionReset`/`OSError` → `close_connection`.
- **Deployment chain: stale `ui_dist/.stamp.json` silently served the
  fallback page** (`679108f`): `install.sh` §3 copied the new
  `index.html` into `mw/ui_dist` and `data/share_workspace/ui_dist` but
  never rewrote `.stamp.json`; since `_ui_stamp()` prefers the
  share_workspace stamp and `_read_if_pass` validates every candidate
  against it, a stale stamp rejected both fresh bundles and the console
  fell back to the 27 KB inline page while `install.sh` exited 0. §3 now
  calls `_write_ui_stamp()` after each `cp` (both sides, four fields:
  `commit_short`/`build_md5`/`build_size`/`installed_at`), BSD/GNU
  compatible (`md5 -r`/`md5sum`, `stat -f %z`/`stat -c %s`/`wc -c`) and
  non-fatal on failure.

### Verification

`docs/REVIEW60-62.md` verdict PASS (0 P0/P1). `docs/TEST60-62.md` (170
test machine): #60 four browser cases pass; #61 reaper fd-count flat over
5.5 min sampling; #62 220 iterations (110×200 + 110×304) + 80 concurrent —
body always equals Content-Length; pytest failure set identical to the
clean-tree baseline (zero new). `docs/TEST-STAMP.md` PASS: both stamps
byte-identical to `index.html` (`3c7f3e4e`/642441), corrupted-stamp
idempotent repair, ETag/body md5 reconciliation 6/6.

## Unreleased — Issue #53 macOS compatibility: directory-only bind mounts + recreate via stop/rm (2026-10-18)

Range: `9050dda..6f59c63` on `main`. Two root causes made the s4d-fix2
mount set unbootable on macOS:

- Apple `container` requires every bind-mount **source to be a directory**
  (`Sources/Services/ContainerAPIService/Client/Parser.swift`,
  `guard isDirectory` → `path '...' is not a directory`); the
  `docker-for-apple-container` shim passes this through verbatim, so the
  five file-level `:ro` mounts (SKILL×4 + SOUL) all failed there.
- `compose --force-recreate` is not in the shim's supported subcommand set.

### Changed

- **`deploy/main.compose.yaml`** (`6f59c63`): mounts are directory-only
  now — `./deploy` mounts whole at `/opt/mwroot/deploy:ro`; the four SKILL
  files stage into host-generated `deploy/.runtime-skills/<sk>/` (per-skill
  directory `:ro`, container paths unchanged); SOUL stages into
  `deploy/.runtime-soul/` mounted at `/opt/soul-src:ro`.
- **`deploy/install.sh` §6**: full restaging of `.runtime-{skills,soul}` on
  every run (LANG carried by content); the §8 language-sentinel `sed` block
  is removed entirely (skills land under their final names directly);
  recreate is now stop+rm-if-exists before `compose up` (shim-compatible).
- **`deploy/main_entry.sh`**: publishes the SOUL via idempotent
  `ln -sfn /opt/soul-src/SOUL.md /opt/data/SOUL.md` (with a guard that
  unlinks a stale regular file from pre-m1-impl installs).
- **`DEPLOY.md`**: §3 re-deploy blurb now points at `./deploy/install.sh`
  instead of a bare `docker compose up -d` (REVIEWMAC N-d — bare `up`
  neither generates `.runtime-*` staging nor applies the new mount set).

### Verification

`docs/REVIEWMAC.md` verdict PASS; `docs/TESTMAC.md`: Linux 6/6 + macOS
static 4/4, pytest 453 with the failure set identical to the clean-tree
baseline (zero new). Review observations N-a/N-b/N-c tracked as Gitea #59.

## Unreleased — Issue #53 rework: install.sh slimmed + watchdog bootstrap (2026-10-18)

Range: `fff2443..ce34026` on `main` (lineage `fff2443 → f908906 → ba05c21 →
e69d5b4 → ce34026`). This is the user-requested rework of #53 on top of the
batch above.

### Changed

- **#53 rework: `deploy/install.sh` slimmed 532 → 238 lines** (`f908906`).
  Ops-script segments removed with per-segment accounting in the commit
  message: `hash_of` generalisation, the container-CLI three-tier probe,
  daemon-reachability probe, UI md5/size/stamp/provenance comparisons, the
  §8.5 in-container code hash check, the 60s readiness loop, `kanban init`
  exec, the embedded watchdog pip step, and the toolgroup bundle-count
  gate. What remains is the minimal run set: image check/pull, LANG
  sentinel, bind-source pre-staging, `compose up`, and one-line failure
  reporting pointing at DEPLOY.md. The CLI abstraction was retired in
  favour of the docker-for-apple `container` shim providing `docker`
  directly (DEPLOY.md §0 rewritten to match); `grep -c` for the old
  abstraction variable in `install.sh` is 0, comments included.
- **BSD/macOS portability** (`ba05c21`): GNU-only in-place `sed -i` replaced
  by sed-to-temp + `mv`, `mktemp -t` templates replaced by `$TMPDIR`
  forms, cleanup consolidated into a single EXIT trap. Closes the boundary
  carried over as Gitea #56.

### Fixed

- **Watchdog dependency bootstrap moved into `deploy/main_entry.sh`**
  (`e69d5b4`, then `ce34026`): install no longer pip-installs into the
  image at all. At container start the entry probes for `watchdog`; if
  missing it installs with a uv → pip → stdlib-urllib (pip-wheel
  fetch/unpack) three-tier bootstrap into `/opt/data/mw-deps` — a
  bind-mounted, uid-writable, recreate-persistent volume — and prepends it
  to `PYTHONPATH`. This fixes the 170-test P1 where the root-owned venv
  made every fresh deploy fall back permanently. Any install failure
  warns once and the service still starts on the 15s polling fallback.
  Verified 5/5 in-container scenarios including recreate short-circuit
  and offline degradation.

### Deployment note

Unchanged from the batch above: upgrades MUST run `./deploy/install.sh`
(see DEPLOY.md §5).

## Unreleased — Issues #51/#52/#53 batch (2026-10-17)

Range: `31a96bc..HEAD` on `main` (fast-forward from the `s3b-fix` branch;
lineage `31a96bc → b062736 → 4f929ac → 024153f → 3e41ff9 → 43bff42 →
b2c7579 → 46deab9`).

### Added

- **#51 Worker avatars are now files, not base64** (`b062736`,
  `024153f`, `3e41ff9`). Uploaded avatars are decoded server-side and written
  to `mw_data/avatars/` as `<worker>-<8hex>.<ext>`; `registry.json` and every
  JSON API response carry only the bare filename, decoupling `GET
  /api/workers` payload size from avatar count. New endpoint
  `GET /api/workers/<name>/avatar` (routed ahead of the worker-detail
  fallback) serves the image bytes. Writes are guarded twice: a strict
  filename whitelist regex at write time (rejects `../`, absolute paths,
  subpaths) plus `realpath` prefix containment at read time. Magic-number
  sniffing validates image type (WebP requires the `WEBP` tag at offset
  8:12, so polyglot WAV/AVI payloads are rejected). A startup migration
  converts legacy base64 entries idempotently.
- `tests/test_issue51_avatar_file.py` — 14 pytest cases + standalone direct
  run covering AC-51-01..16, including 6 path-escape attack forms with zero
  escapes observed.

### Changed

- **#52 Workflow step `description` limit raised 100 → 500** (`b062736`).
  Backend schema, WorkflowsView.jsx (3 sites), design.md and both locale
  files updated in sync; UI bundle rebuilt in the same commit.
- **#53 `deploy/install.sh` restructured** (`4f929ac`, `43bff42`, `b2c7579`).
  A `DOCKER_CMD` abstraction with three-tier probe (env > docker > container,
  applied at 28 call sites) makes the script portable to macOS `container`.
  SOUL/SKILL/stamp/UI-dist deployment moved from post-start `docker cp` /
  `docker exec chown` to host-side writes into the bind-mount source before
  start; the compose file gained five file-level `:ro` bind mounts so the
  host no longer writes into in-container chowned directories. Failure paths
  compressed to a hash comparison plus a one-line pointer to DEPLOY.md.
  Cross-platform helpers (`hash_of`, `stat -f` fallback, POSIX `sed`) keep
  BSD/macOS compatibility.
- **#53 upgrade-path regression fix (BUG-53-A)** (`43bff42`, then `b2c7579`):
  container chown of bind-mounted parent dirs broke host-side `cp` during
  upgrades. Final fix is the bind-mount design above (root cause removed);
  `cp_into()` (unlink-then-write via parent-directory `w+x`) remains as
  defense. `tests/test_install_bind_mount_skill_soul.py` adds 9 assertions.
- **Docs sync D-1..D-4** (`46deab9`): design.md §11.7/§11.5 now document the
  avatar endpoint and schema semantics; DEPLOY.md corrected on the
  `--preserve-soul` no-op, macOS `container` + `DOCKER_CMD` manual, and a
  single rebuild-based upgrade story in §5.

### Fixed (review/fix rounds folded into this range)

- R-1 (High): avatar filename write-side whitelist + realpath double
  containment (`024153f`), hardened against `.`/`..` (`3e41ff9`).
- R-2/R-3/R-6 (Medium): strict WebP magic, 128-char filename cap enforced,
  `hash_of` generalised with BSD `shasum` fallback (`024153f`).
- N-1 (High): `hash_of` stdin (`-`) branch so the §8.5 hash pipeline tail no
  longer treats a sha256 digest as a filename under `set -e` (`3e41ff9`).

### Deployment note

This batch changes `deploy/main.compose.yaml`. Upgrades MUST run
`./deploy/install.sh` — a container restart alone leaves the old mount set
in place and will fail. See `DEPLOY.md` §5.

Carry-over items tracked as Gitea issues: #55 (docs wording, audit D-5..D-8
— D-5/D-6/D-7/D-8 have since landed; see the batch A/B/C + H-1 entry above)
and #56 (macOS BSD `sed -i` smoke test).