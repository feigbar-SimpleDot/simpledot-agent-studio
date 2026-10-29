# Changelog

Release-policy note: this file records merged changes against `main`, newest
first. Prose is English per the repo root-doc convention (see
`tools/i18n_docs_sync.py` F1); commit subjects are quoted verbatim in code
spans. Process/review/test notes live outside the repo under the task
workspace `docs/` and are referenced by card id, not duplicated here.

## Unreleased — Issue batch K (2026-10-29): the contract parser reads every damaged TODO.md shape through one core, the verify script speaks the operator's language, and the receipt channel gets assertions that can fail

Range: four commits `04e04b9` + `e5eb729` + `7921b77` + `ee390d6`, i.e. `dc71a31..ee390d6` (`git log --oneline dc71a31..ee390d6 | wc -l` = 4). One commit per issue, except that #139 carries a follow-up commit closing the review's only blocking finding (F-1, a parser cost regression introduced by the first #139 commit). This entry and the document correction listed under "Notes" were added by the delivery-closure commit on top of that range.

Commits, in order, with the files each one touched:

- `04e04b9` — `fix(issue139): TODO.md 契约解析层健壮性 — 单一解析核心 + 四形态容错 + diagnose (#139)` — `mw/todo.py` (+132/−43), `tests/test_issue139_todo_contract_robustness.py` (new, 306 lines), `tests/test_issue152_todo_prefix.py` (+11/−8).
- `e5eb729` — `fix(issue156): verify.sh ok()/bad() 双语输出 — 复用 lib/i18n.sh 的 _i18n_pick 按 SAS_LANG 选语言 (#156)` — `deploy/verify.sh` (+5/−2), `tests/test_issue156_verify_bilingual.py` (new, 167 lines).
- `7921b77` — `fix(issue157): #119 回执通道两条 P3 观察测试加固: T4 幂等补 mw_events 行数断言 + 观察 2 竞态登记 (#157)` — `mw/console.py` (+10/−0, comments only), `tests/test_issue119_relay_confirm.py` (+39/−1).
- `ee390d6` — `fix(issue139-F1): _FIELD_LINE 相邻量词致超线性回溯, 无冒号短路 + 键名组改贪婪压平整类成本 (#139)` — `mw/todo.py` (+31/−1), `tests/test_issue139_todo_contract_robustness.py` (+150/−1).

Aggregate over the range: `git diff --shortstat dc71a31..ee390d6` reports 7 files changed, 849 insertions(+), 54 deletions(-). Files touched (7): `mw/todo.py` (+162/−43 cumulative), `tests/test_issue139_todo_contract_robustness.py` (new, +455 cumulative), `tests/test_issue152_todo_prefix.py` (+11/−8), `deploy/verify.sh` (+5/−2), `tests/test_issue156_verify_bilingual.py` (new, 167 lines), `mw/console.py` (+10/−0), `tests/test_issue119_relay_confirm.py` (+39/−1).

### Fixed

- **A damaged TODO.md could still stall the reaper, and the four read entry points could disagree about the same file (#139)** — batch I closed the `N|` line-number shape, but four residual damage shapes stayed unreadable: the `cat -n` shape (`N` + tab), a `*` bullet instead of `- `, a status line missing the `- ` prefix, and an indented status line. The last one was worse than unreadable: `current_binding` accepted leading whitespace while `parse_status` did not, so launch/advance saw a healthy card while the reaper saw an unrecognised status — two subsystems of one parser reading two facts out of one file. The read side now converges on one core: `_match_field_line` is the single field-line recogniser (indent, `*` bullet and missing `- ` all tolerated in one regex), `_is_current_header` collapses the duplicated `## 当前任务` header checks, and `_strip_line_num_prefix` was widened to `^[ \t]*\d+[|\t]` so the tab shape is stripped too. The old fast path (`if "|" not in text: return text`) was removed — it is what made the batch-I boundary assertion blind (#153), and every tolerated shape is pipe-free, so keeping it would have made the tab tolerance silently ineffective. Tolerance is strictly read-side and section-scoped: it only applies inside the `## 当前任务` section (bilingual header aliases included), the key must still match `状态`/`status`, values still pass through `normalize_status` with no new aliases, and the whole-file single-line fallback deleted in the 2026-10-03 subtraction stays deleted (a status line outside the current section still reads `None` — pinned by T-D and by mutation MV-139-C). A new `diagnose(path)` returns why a status cannot be read (`file_missing` / `no_current_section` / `no_status_line` / `unrecognized_value` / `ok`); it is pure-read with zero production callers by design (plan option A: no new runtime noise). The write side (`write_status_atomic`, `write`, `rotate*`, `init_*`, `bind_kanban_tasks`) is AST-equal to the baseline. Verification (test report K, 170 machine): the six-shape probe reads `completed` through all four entry points on all four repaired shapes while the baseline control group reproduces the old blind spots; 28 new cases in `tests/test_issue139_todo_contract_robustness.py`; the five mutations MV-139-A..E each turn the expected cases red and go green after restore; the full-suite failure-set difference against the baseline is empty (head 899 passed / 8 skipped, baseline 864 / 8, the +35 all attributable to new cases).
- **A single malformed line could drag one settlement round out to minutes (#139 follow-up F-1)** — the new `_FIELD_LINE` put three adjacent quantifiers on the same leading-whitespace run, so a long whitespace-only line without a colon forced the engine to backtrack over every split of that run: measured 16.9 s for one 400-space line on the pre-fix head, against 0.0024 ms on the baseline (whose old regex required a literal `-` and failed in constant time). The recogniser is shared by `parse_status` (the reaper's 15 s settlement loop), `current_binding`, `_parse_kv` and `diagnose`, so one damaged line could stall the very silent-stall class #139 exists to eliminate. The fix is two-part and behaviour-identical: `_match_field_line` short-circuits lines containing neither colon form (the pattern cannot match them anyway), and the key group went greedy (`([^:：]+?)` → `([^:：]+)`), which flattens the colon-present whitespace-run family. Equivalence was cross-checked by two independent differential runs (2,568,282 samples, zero differences) and the recognition surface did not narrow (full-width-space and nbsp indents still read). Guards: T-H pins a 10 ms per-line budget (stdlib `perf_counter`, no new dependency) and T-I re-pins the four shapes through all four entry points; mutations MV-F1-A (full revert: 17 344 ms red) and MV-F1-E (greedy reverted only: T-H2 red) show each half of the fix has its own teeth. Post-fix worst case measured 0.0813 ms (margin ×123).
- **`verify.sh` printed English under `LANG=zh-cn` (#156)** — `ok()` and `bad()` printed only their first (English) argument, so every gate line came out English in a Chinese deployment even though 23 of the 24 call sites (23 two-argument + 1 one-argument) already carried Chinese second arguments. The two function definitions now route through `_i18n_pick` from `deploy/lib/i18n.sh` — the same language pick `section_banner` uses — with `"${2:-$1}"` so the single-argument `LANG=` report line falls back to its English text instead of printing an empty line under `set -u`. Only the two definitions changed: call-site wording, verdict logic, exit codes and the PASS/FAIL counters are byte-identical, and `deploy/lib/i18n.sh` itself was not touched. Verification: real-script runs of the archived `deploy/` tree under both languages (zh-cn prints `容器未运行` without the English line, en the reverse, the `LANG=` line non-empty on both sides, identical ✓/✗ line sets and matching summary counts), plus six static/real-script cases in `tests/test_issue156_verify_bilingual.py`; mutations MV-156-A/B/C each turn the expected assertions red.
- **The receipt channel's two batch-J observations now have behavioural assertions (#157)** — T4 idempotence did not pin "no event is emitted": a stray `emit_event` inside `confirm_delivered` would have passed. The test now counts `mw_events` rows before and after the two confirms and asserts the count is unchanged, which sits directly on the single INSERT point (`mw/ledger.py:1395`); MV-157-A (injecting an emit) turns it red (`assert 2 == 0`). The theoretical pre-check/receipt race (card deleted between the existence pre-check and the UPDATE) was registered rather than tightened — no `DELETE FROM harvest_contracts` path exists in production code, and tightening would change the existing `ok:true, confirmed_at:null` contract for zero benefit: a comment block above the pre-check records the window mechanism and the condition that would force a tightening, and a stateful fake `get_contract` pins the current semantics so any future tightening turns that case red (MV-157-B proves it distinguishes the two). `mw/console.py` carries comments only — whole-file AST equality against the baseline holds.

### Notes

- **Two earlier entries are superseded by this one** — the batch-I #152 entry stated "the tolerated shape is exactly one — `^\s*\d+\|`" and registered the `cat -n` shape (digits plus a tab) as a follow-up; that follow-up is now closed and the tolerated set is two shapes (`^[ \t]*\d+[|\t]`), so both sentences describe the pre-batch-K parser. The batch-J #153 entry described the fast path (`if "|" not in text: return text`) as a live mechanism and warned that pattern and fast path must be re-checked together; #139 removed the fast path, so that coupling no longer exists — the case #153 added (`test_widened_pattern_caught_when_pipe_elsewhere`) keeps its teeth without it (verified under mutation on the new head).
- **What was deliberately not touched** — the write side of `mw/todo.py` (all eight write functions AST-equal to the baseline), `mw/reaper.py`, `mw/ledger.py`, `deploy/lib/i18n.sh`, `mw/todo_status_contract.json`, `ui/**` (so no bundle rebuild was needed), `mw/i18n/*` and locale files (the batch adds no locale key), and `mw/ui_dist/**`. No new dependency, secret, mount or compose change.
- **One document correction rides with this entry** — the bilingual section of `DEPLOY.md` says every line is in the chosen language; one call site (the `LANG=` report line) passes only its English text and prints that under `zh-cn` by design (there is no Chinese counterpart to pick). The section now carries an explicit exemption note for that line; no behaviour changed, only prose.
- **Verification** — acceptance evidence lives in the batch test reports (test report K and the F1 re-test): targeted sets green (28 + 129 + 27 + 8 + 33), the twelve distinct mutation counter-proofs (MV-139-A..E, MV-156-A..C, MV-157-A..B, MV-F1-A/E; the five MV-139 ones were re-run a second time after the F-1 fix) all turned red as expected and green after restore, the full-suite failure-set difference is empty on both re-test rounds (final head 906 passed / 8 skipped, collected 914, the +7 all attributable to the F-1 guards), and `npm test`, `tools/i18n_docs_sync.py --strict` and the dist sync gate all exit 0. Five review follow-ups (F-2 `parse()` invents a current task from colon-bearing prose lines, F-3 the section-header check still has a second copy in `_split_sections`, F-4 the widened strip touches `2026<TAB>` content lines, F-5 first-hit semantics can read a stale no-dash line, F-6 two #157 assertions are maintenance-brittle) are registered for the next batch; none is a regression introduced here and none blocks this merge.
- **Deployment** — this batch changes running middleware (`mw/todo.py`, `mw/console.py`) and the verify script, so `git pull` alone is not enough: the installer has to run to refresh the staging copies and the container has to be restarted to load the new middleware. At re-test the deployed copies on the test machine were still at the pre-batch checkout (md5s recorded in the test report — the deployed `todo.py` predates even the batch-I tolerance), so until `git pull` + `./deploy/install.sh` + restart, none of the #139 tolerance, the F-1 fix or the #156 bilingual output is live.

## Unreleased — Issue batch J (2026-10-29): main gets an explicit receipt channel for delivered reports, and three test-and-operations debts from batch I are closed

Range: five commits `2f18649` + `bf35dbc` + `1cc47c2` + `ec0eb7c` + `b78f9cd`, i.e. `5af0429..b78f9cd` (`git log --oneline 5af0429..b78f9cd | wc -l` = 5). One commit per issue, except that #119 is split into a code commit and a consumer-documentation commit. This entry and the document corrections listed under "Notes" were added by the delivery-closure commit on top of that range.

Commits, in order, with the files each one touched:

- `2f18649` — `fix(issue119): main 侧显式回执通道 POST /api/relay/confirm + 死信顺带恢复 (#119)` — `mw/console.py` (+45/−0), `tests/test_issue119_relay_confirm.py` (new, 224 lines).
- `bf35dbc` — `docs(issue119): worker-message-relay skill 新增显式回执调用形态一节, 双语同 commit, 2.0.0→2.1.0 (#119)` — `deploy/skills/worker-message-relay/SKILL.en.md` (+35/−1), `deploy/skills/worker-message-relay/SKILL.zh-cn.md` (+28/−1).
- `1cc47c2` — `fix(issue153): 断言 6b 补快路径掩蔽补牙用例 + _strip_line_num_prefix 注释写明快路径与 pattern 耦合 (#153)` — `mw/todo.py` (+8/−0, docstring only), `tests/test_issue152_todo_prefix.py` (+28/−0).
- `ec0eb7c` — `fix(issue154): test_issue146 后端键集断言去 python3 子进程依赖, 直读契约 JSON + 运行时断言移交 pytest test_issue124 (#154)` — `ui/tests/test_issue146_todo_status_i18n.mjs` (+21/−22).
- `b78f9cd` — `fix(issue155): verify.sh 指纹报红补归因提示(中英双语) (#155)` — `deploy/verify.sh` (+4/−1).

Aggregate over the range: `git diff --shortstat 5af0429..b78f9cd` reports 8 files changed, 393 insertions(+), 25 deletions(-). Files touched (8): `mw/console.py` (+45/−0), `tests/test_issue119_relay_confirm.py` (new, 224 lines), `deploy/skills/worker-message-relay/SKILL.en.md` (+35/−1), `deploy/skills/worker-message-relay/SKILL.zh-cn.md` (+28/−1), `mw/todo.py` (+8/−0), `tests/test_issue152_todo_prefix.py` (+28/−0), `ui/tests/test_issue146_todo_status_i18n.mjs` (+21/−22), `deploy/verify.sh` (+4/−1).

### Fixed

- **A report main had already consumed could keep being re-relayed, and a dead-lettered one had no way back (#119)** — the relay marks a report delivered only on a real HTTP 200 from the main gateway, but the gateway accepts the POST synchronously and does the model turn afterwards: when the send times out (the 600 s window) or the gateway answers non-2xx after main already read the report, `notify_confirmed_at` stays NULL forever, the card is re-relayed on every scan, and after 8 attempts it is dead-lettered for a year with no recovery path. The console now exposes `POST /api/relay/confirm` with body `{"task_id": "<id>"}`: it validates the id (400 when missing), pre-checks the card exists (404, distinguishable from a 500), and calls the existing `Ledger.confirm_delivered` — the same function the relay path uses, so there is no second source of truth. One call sets `notify_confirmed_at` and clears `notify_attempts`, `notify_dead_until`, `notify_last_failed_at` and `notify_last_error` in a single UPDATE, which is what recovers a dead-lettered card; the write is `WHERE task_id=?`, so it only ever touches the explicitly named card, and repeating it is idempotent. The old path is untouched: `mw/ledger.py` and `mw/reaper.py` are byte-identical to the baseline, the relay scan SQL is unchanged, and a card nobody confirms behaves exactly as before. Seven new cases pin the endpoint (200/400/404, confirmation set, dead-letter fields actually `(0, NULL)` in sqlite, idempotence, isolation, and a regression lock that an unconfirmed card is still scanned with its fields intact); the assertions read the ledger fields, not just the HTTP status, so a stub endpoint that always answers 200 turns them red.
- **The batch-I boundary assertion could not see a widened strip pattern (#153)** — assertion 6 of the line-prefix suite only replaces the status line, and its fixture has no pipe anywhere, so the fast path (`if "|" not in text: return text`) short-circuits before the pattern runs: widening `_LINE_NUM_PREFIX` while keeping the fast path left all 22 cases green. The new case 6b injects a pipe into a non-status line so the pattern actually runs, and uses the `12:- 状态:` shape (no space after the colon) so that a widened pattern produces an observable difference — the shape suggested in the plan (`12: - 状态:`) was tried first and stayed green under the mutation, because the leftover leading space keeps the status line unmatched either way. The docstring of `_strip_line_num_prefix` now states the coupling: changing the pattern or the fast path requires re-checking both, and names the case that guards it. The pattern and the function body were not touched (AST-equal after stripping docstrings), and the existing assertion 6 is byte-identical.
- **A frontend test spawned a python3 subprocess to read the backend's key set (#154)** — `test_issue146_todo_status_i18n.mjs` ran `execFileSync('python3', …)` to import `mw.console` and read `_TODO_STATUS_SEM` at runtime, so `npm test` failed with `spawnSync python3 ENOENT` on any machine without python3 on PATH. The backend side now reads the contract JSON directly, which keeps the assertion cross-artifact rather than self-referential: the frontend side is still derived from `TasksBoardView.jsx` source in a vm sandbox and compared against the contract file — two different artifacts. The runtime half of the assertion moved to where a python already lives: `tests/test_issue124_status_alias.py` imports `_TODO_STATUS_SEM` and pins the five keys and the tuple shape, and the mjs T8 source-shape assertions still pin that both sides stay derived. Mutation counter-proofs on both sides (frontend derivation drops a key → three mjs cases red; backend comprehension drops a key → two pytest cases red) show the cross-layer teeth survived the move.
- **The fingerprint gate reported "stale" without saying what to do about it (#155)** — when the SOUL/SKILL fingerprint mismatched, `verify.sh` printed only the two hashes and "re-run install.sh", leaving the operator to guess whether a plain `git pull` was enough. The message now names the mechanism and splits the two causes: the runtime copy is a read-only bind mount generated by `install.sh`, so if only `deploy/skills/**` or the SOUL/SKILL sources changed, re-running `./deploy/install.sh` is enough, while a change under `mw/**` needs `git pull` plus the installer. Both language arguments of the one `bad` call carry the full attribution; the `bad()` definition and the fingerprint comparison were not touched. One honest boundary: `bad()` prints only its first argument, so under `LANG=zh-cn` the output is still English — that pre-existing i18n defect affects all ~18 two-argument call sites, is tracked as its own issue, and was deliberately not folded into this card; the Chinese side is verified at source level.

### Notes

- **What was deliberately not touched** — `mw/ledger.py` and `mw/reaper.py` (both byte-identical to the baseline, `git diff` empty), `mw/todo.py`'s pattern and function body, `mw/todo_status_contract.json`, `ui/src/**` (so no bundle rebuild was needed), `ui/dist/index.html`, `mw/i18n/*` and `ui/src/i18n/locales/*` (the batch adds no locale key, so the bilingual-commit discipline does not trigger), and `mw/ui_dist/**` (the deploy-time copy, not tracked). No new dependency, secret, mount or compose change.
- **Two document corrections ride with this entry** — the three-state model in `design.md` described a real HTTP 200 as the only setter of `notify_confirmed_at`, which #119 superseded: the "Confirmed delivered" line and the fix-history line now say the relay marks on true 200 while the explicit `POST /api/relay/confirm` is the second legal setter, matching the skill's new section verbatim. The §11.7 endpoint table gained the `/api/relay/confirm` row (it is an LLM-facing write endpoint, same class as `/api/resume`). No behaviour changed, only prose.
- **Verification** — re-test on the 170 machine: the four targeted sets are green (7 new #119 cases, 33 existing relay cases with zero regression, 23 for the prefix suite, 2 for the pytest status-contract half, mjs T1..T8 green), all eight mutation counter-proofs turned red as expected and back to green after restore — including the two that prove the #119 tests assert ledger field effects rather than HTTP 200 — and the full-suite failure-set difference between baseline and head is empty (head 864 passed / 8 skipped, baseline 856 / 8, the +8 all attributable to the new cases); `npm test` is green, and with python3 removed from PATH the mjs suite and `npm test` both stay green, which is the #154 acceptance itself. `tools/i18n_docs_sync.py --strict` and the dist sync gate exit 0. The production ledger on the test machine was byte-identical before and after the runs.
- **Deployment** — this batch changes running middleware (`mw/console.py`), the verify script and the skill text, so `git pull` alone is not enough: the installer has to run to refresh the staging copies and the container has to be restarted to load the new middleware. At re-test the deployed copies on the test machine were still at the pre-batch commit (md5s recorded in the test report), so until `git pull` + `./deploy/install.sh` + restart, the receipt endpoint and the new verify wording are not live. The 19 existing dead-lettered cards are cleared by calling the new endpoint per card after a human check, not by a batch tool — that is a coordinator action, not code.

## Unreleased — Issue batch I (2026-10-29): the reaper stops stalling on a line-numbered TODO.md, the settlement surface gets a test that can fail, the status contract has one source, and the orchestration skill stops contradicting the main soul

Range: five commits `01d756d` + `82cc05d` + `f61d38b` + `9354d42` + `939a4a2`, i.e. `3530929..939a4a2` (`git log --oneline 3530929..939a4a2 | wc -l` = 5). One commit per issue, except that #149 and #148 share one commit because they touch the same two assertion surfaces. This entry and the document corrections listed under "Notes" were added by the delivery-closure commit on top of that range.

Commits, in order, with the files each one touched:

- `01d756d` — `fix(issue152): todo.py 解析面行号前缀容错, 四入口同享 + 回归锁测试 (#152)` — `mw/todo.py` (+52/−0), `tests/test_issue152_todo_prefix.py` (new, 270 lines).
- `82cc05d` — `fix(issue140): current_card_contract 结算面红线补测试牙齿, 被测代码零改动 (#140)` — `tests/test_issue140_current_card_contract.py` (new, 181 lines).
- `f61d38b` — `fix(issue147): orchestration skill 冲突条款收紧为建卡前零研判 + 守护测试 (#147)` — `deploy/skills/team-work-chain-orchestration/SKILL.en.md` (+28/−21), `deploy/skills/team-work-chain-orchestration/SKILL.zh-cn.md` (+12/−10), `tests/test_issue147_skill_conflict_guard.py` (new, 200 lines).
- `9354d42` — `fix(issue149/148): 状态契约真相源收敛为派生 + 断言面补强 (#149 #148)` — `mw/console.py` (+13/−6), `mw/todo_status_contract.json` (new, 9 lines), `tests/test_cron_api.py` (+33/−0), `ui/dist/index.html` (+40/−40, rebuilt in the same commit), `ui/src/views/TasksBoardView.jsx` (+9/−9), `ui/tests/test_cron_view.mjs` (+40/−5), `ui/tests/test_issue146_todo_status_i18n.mjs` (+76/−16), `ui/tests/test_issue87_fmtapierror_coverage.mjs` (+7/−0).
- `939a4a2` — `docs(issue150): README 中英两份加入视频教程入口, 链接逐字照抄 issue 原文 (#150)` — `README.md` (+5/−0), `README.zh-cn.md` (+7/−0).

Aggregate over the range: `git diff --shortstat 3530929..939a4a2` reports 16 files changed, 982 insertions(+), 107 deletions(-). Files touched (16): `mw/todo.py` (+52/−0), `tests/test_issue152_todo_prefix.py` (new, 270 lines), `tests/test_issue140_current_card_contract.py` (new, 181 lines), `deploy/skills/team-work-chain-orchestration/SKILL.en.md` (+28/−21), `deploy/skills/team-work-chain-orchestration/SKILL.zh-cn.md` (+12/−10), `tests/test_issue147_skill_conflict_guard.py` (new, 200 lines), `mw/console.py` (+13/−6), `mw/todo_status_contract.json` (new, 9 lines), `tests/test_cron_api.py` (+33/−0), `ui/dist/index.html` (+40/−40, rebuilt), `ui/src/views/TasksBoardView.jsx` (+9/−9), `ui/tests/test_cron_view.mjs` (+40/−5), `ui/tests/test_issue146_todo_status_i18n.mjs` (+76/−16), `ui/tests/test_issue87_fmtapierror_coverage.mjs` (+7/−0), `README.md` (+5/−0), `README.zh-cn.md` (+7/−0).

### Fixed

- **A TODO.md written back with line numbers could never be settled, and the platform had no tolerance for it (#152)** — `mw/todo.py::parse_status` only recognises `- 状态: <value>` inside the `## 当前任务` section, and it matched the line prefix as part of the line: a worker that read the file with a line-numbering view (`read_file`, `nl -ba`) and wrote the whole thing back verbatim turned every line into `12|- 状态: completed`, so `parse_status` returned `None`, the reaper took its "unrecognised status → wait conservatively" branch, and the card was never harvested. The G-batch instance of this stalled for 3h19m, and the mitigations at the time (a driver-side repair function, a card constraint, a skill rule) were all outside the platform, so it came back on every new environment. The parser now strips a leading `N|` from each line before the existing rules run. The strip is **line-level on purpose**: section detection (`## `) and field detection are two independent steps, so removing the prefix only at the field regex would leave the section header `3|## 当前任务` unmatched, the section would never be opened, and the status line would still be unreachable. All four read entry points share it — `parse()`, `parse_status()`, `current_binding()`, and `verify_current_task_binding()` (which inherits it through `current_binding`) — so there is no state where one of them can read a file and another cannot. The tolerance is **read-side only**: `write_status_atomic()` is untouched, so the platform does not rewrite what a worker delivered, and the TODO.md writing surface is unchanged. The tolerated shape is exactly one — `^\s*\d+\|` — and the section split, the bilingual field names, the pending/done counts and the status literals are all left as they were. The `cat -n` shape (digits plus a tab) is not tolerated and is registered as a follow-up.
- **The settlement surface had no test that could fail (#140)** — putting `'WAITING'` back into the `IN` list of `current_card_contract` left the whole related suite green, so the rule "a WAITING card must not be settled" was held only by reviewer discipline. The new test uses a real `Ledger` instance rather than a stub, establishes the positive control first (`PENDING` returns a row) so it cannot pass by finding nothing, pins `WAITING` → `None` as the subject, pins the three settlement states and the five non-settlement states one by one, and asserts the two sets are identical so a drifting set reports an exact difference. It also covers cross-run isolation and the empty-argument guard, and it reaches every status through `update_worker_state` with the actor whitelist, so the states it asserts on are reachable in the real state machine. The code under test was not modified: this issue asks for test teeth, and editing the production query would have hidden the defect instead of closing it.
- **Two of the H-batch follow-ups are now closed by construction (#149 F-2, #149 F-3)** — the backend `_TODO_STATUS_SEM` table and the frontend `TODO_STATUS_TEXT_KEYS` map were two independent literal sets kept in step only by a test, so adding a status to one side and forgetting the other was a single keystroke away. Both are now derived from one machine-readable contract, `mw/todo_status_contract.json`: the backend reads it and builds its table, the frontend imports it and builds its key map, and "change one side only" is no longer expressible. The cross-layer comparison was kept real — the backend side of the key-set assertion runs the backend module and compares the runtime objects, so the test does not grade its own homework against the same JSON it reads. Three source-shape assertions pin the shape: the frontend must stay derived, the backend must stay a comprehension, and the contract file must not carry locale keys. The `#144` content anchor now also states in its comment what it is sensitive to: it locates by content shape, so renaming the identifier it follows leaves it matching, while renaming the locale key it quotes empties the anchor set and makes the set-identity assertion report the difference explicitly.
- **The source-shape scan could be bypassed by writing the same comparison differently (#149 F-7)** — the scan looked for one exact spelling of the accepted-status comparison, so a rewritten form (`==` instead of `===`, a different set of values) slipped past it while the defect it guards against stayed. The scan now covers the whole file, matches a comparison of a status against a string literal in either quote style and either equality operator, and reports when two or more distinct literal comparisons appear, so the alternative spelling is caught. A single literal comparison is still not treated as a whitelist, which is a known boundary and not a claim of full coverage.
- **Two behaviour-equivalent mutations left the guards unable to notice (#148 OBS-1, #148 OBS-2)** — for the cron status badge, swapping the derived set for a plain own-property check while keeping the `typeof` guard changes nothing observable, so the behavioural assertions stayed green; for the cron bridge's stderr tail, replacing the whitespace collapse with a newline replacement also produces a single-line tail and stayed green. Both now have explicit source-shape assertions: one pins the derived-set lookup and the `typeof` guard as two co-existing defences, the other pins the split, join and truncate pipeline. Under the two behaviour-equivalent mutations the behavioural assertions remain green while the new shape assertions report the change, which is the point of adding them.
- **The orchestration skill told main to do what the main soul forbids (#147)** — the deploy soul had already adopted "no research before the card exists", but the skill still granted main a bounded measured baseline, encouraged file and line locating hints, and put a pre-locating step in the dispatch checklist. The skill clauses were more specific and more actionable than the principle, and a run recorded 20.2 minutes and 127 calls for the requirement-to-first-card window against a 10-minute budget. Ten places across the two language versions were rewritten so the responsibility moves: main's pre-card work is a closed set of second-scale intake (the issue text and its comments, attachments, PROJECT-MEMORY), while the measured baselines and the file and line locating hints are produced by the first research or analysis card and relayed with its conclusion. Every original information point was kept — the budget discipline, the motivation for shortening the build round, the boundary between positioning and design — and no clause is left pointing at text that no longer exists. A permanent guard asserts that ten conflicting wordings stay at zero occurrences across the whole skills tree, that the new wording is still present so the clauses cannot be resolved by deletion alone, that each of the five bilingual pairs is present on both sides, that the three anchor lines another guard depends on are untouched, and that mutating an in-memory copy of the file turns the guard red.
- **Both READMEs pointed readers at no tutorial video (#150)** — the English README gained a "Video tutorials" section with two links and the Chinese README gained a "视频教程" section with the same two links plus the channel name to search for. The links are copied verbatim from the issue body, query parameters included.

### Changed

- **The status contract has one machine-readable source of truth (#149 F-2)** — `mw/todo_status_contract.json` holds the five statuses with their family and badge class; the backend table and the frontend key map are both derived from it. The values are byte-identical to the table they replace, so nothing observable changed for any of the five statuses. The contract file travels into the container with the existing `mw/` bind mount, so no compose or install change was needed.
- **The status chip's key map follows the backend automatically (#149 F-2)** — the frontend builds its map from the contract instead of listing the five keys, so the frontend cannot drift from the backend. The lookup function's own defences are unchanged.

### Notes

- **What was deliberately not touched** — `mw/ledger.py` and `mw/reaper.py` (both byte-identical to the baseline), `mw/todo.py`'s writing path and its other parsing rules, `mw/i18n/*`, `ui/src/i18n/locales/*` (the batch adds no locale key, so the bilingual-commit discipline does not trigger), `docs/main-soul.{en,zh-cn}.md`, `deploy/.runtime-skills/**` and `deploy/.runtime-soul/**` (install.sh staging output, gitignored), and `mw/ui_dist/**` (the deploy-time copy, not tracked). No new dependency, secret, mount or compose change. The frontend bundle is tracked in exactly one place (`ui/dist/index.html`) and was rebuilt in the same commit that changed `ui/src`.
- **Two earlier entries are superseded by this one** — the #147 entry above recorded a "known tension, deliberately left in place", stating that the skill text was kept unchanged on purpose and that tightening it was a separate decision. That decision was taken back and this batch is that follow-up card: commit `f61d38b` rewrote the conflicting clauses and added the permanent guard, so the tension is resolved in the text. The H-batch entry above listed as a non-blocking follow-up that the frontend token map and the backend table "are two lists kept in step by a test rather than by construction"; that description is no longer accurate, because both sides now derive from `mw/todo_status_contract.json` as of commit `9354d42`. Both older entries carry a back-reference to this one.
- **Three document corrections ride with this entry** — the upgrade section of `DEPLOY.md` listed only the compose file and the installer's provisioning as reasons to re-run the installer, and did not mention that a pull touching the SOUL or SKILL sources needs the installer too; the runtime copies are staging directories that `deploy/install.sh` regenerates on every run, so a pull and a restart alone leave the old text in place. Two passages described those runtime copies as read-only mounts of the repository source files, which is not what the installer does; they are mounts of the staging copies the installer generates. All three were corrected here; no behaviour changed, only prose.
- **Verification** — the full backend suite is 856 passed with 8 skipped and no failures against a 793-passed baseline, a 63-case increase all attributable to this batch (22 for #152, 14 for #140, 26 for #147, 1 for #148), and the failure-set difference between baseline and head is empty; the frontend suite is green across all 14 entry points; `tools/i18n_docs_sync.py --strict`, the dist sync gate and the i18n protocol guard all exit 0. Fourteen mutation counter-proofs were run at re-test and all turned red as expected; three more are recorded as exceptions and are reported as such rather than as passes — one variant cannot be constructed at all under the derived shape (adding a sixth key to the backend while the frontend misses it), one is a positive proof (the derived key set follows the contract), and one is a known boundary (a single literal comparison is not treated as a whitelist).
- **One test-effectiveness observation, not a product defect** — widening the strip pattern on its own does not turn the boundary cases red, because the fast path that skips files without a pipe hides it; widening the pattern *and* removing the fast path turns them red, so the boundary assertion does have teeth. The wording in the implementation notes that claimed the boundary was already closed was incomplete and was corrected during the document audit that closed this batch.
- **Follow-ups registered, none blocking** — the #152 tolerance covers one prefix shape only; the scan's remaining blind spots (backtick literals, comparison through a variable, `switch` statements) are known boundaries that would need an AST-level assertion to close; the contract file carries the frontend's badge class names alongside the status semantics; and the main soul's time box and the skill's budget rule measure different axes. These are "could be stricter" items, not defects.
- **Deployment** — this batch changes the running middleware (`mw/todo.py`, `mw/console.py`, the new contract file), the frontend bundle and the skill text, so `git pull` alone is not enough: the installer has to run to refresh the staging copies and the runtime bundle, and the container has to be restarted to load the new middleware.

## Unreleased — Issue batch H (2026-10-28): five P3 debts closed — prototype-chain keys in the cron frontend, a log file for the cron bridge, content anchors instead of line numbers, one source of truth for the subagent timeout, and the TODO status chip goes bilingual

Range: six commits `65defff` + `401696d` + `15add84` + `413a833` + `42b8cd6` + `70e11a7`, i.e. `af92695..70e11a7` (`git log --oneline af92695..70e11a7 | wc -l` = 6). One commit per issue, plus one follow-up commit for the review's only blocking finding. This entry and the document corrections listed under "Notes" were added by the delivery-closure commit on top of that range.

Commits, in order, with the files each one touched:

- `65defff` — `fix(issue142): CronView 原型链键防御 + 词条键白名单单一来源 (#142)` — `ui/src/views/CronView.jsx` (+25/−8), `ui/tests/test_cron_view.mjs` (+62/−1).
- `401696d` — `fix(issue144): S2-h 行号锚改内容锚, 锚集合与残留集合恒等 (#144)` — `ui/tests/test_issue87_fmtapierror_coverage.mjs` (+63/−7).
- `15add84` — `fix(issue143): console 接 logging + RotatingFileHandler, cron 桥 stderr 落固定文件 (#143)` — `mw/console.py` (+42/−8), `tests/test_cron_api.py` (+71), `DEPLOY.md` (+1).
- `413a833` — `fix(issue145): MW_REAPER_SUBAGENT_MIN 默认值真相源收敛为引用式 + 新增守护测试 (#145)` — `DEPLOY.md` (+1/−1), `design.md` (+2/−1), `docs/main-soul.en.md` (+2/−1), `docs/main-soul.zh-cn.md` (+2/−1), `tests/test_issue145_subagent_min_doc_sync.py` (new, 92 lines).
- `42b8cd6` — `fix(issue146): TODO 状态 chip 文案 i18n 化 — status_sem 第二位改规范 token, 前端按运行时语言渲染 (#146)` — `mw/console.py` (+15/−7), `ui/src/views/TasksBoardView.jsx` (+19/−1), `ui/src/i18n/locales/{en,zh-cn}.json` (+6/−1 each), `ui/tests/test_issue146_todo_status_i18n.mjs` (new, 158 lines), `ui/tests/test_issue58_tokens.mjs` (+10/−5), `tests/test_issue124_status_alias.py` (+18), `ui/package.json` (the new frontend suite is wired into `npm test`), `ui/dist/index.html` (rebuilt in the same commit).
- `70e11a7` — `fix(issue146): TODO 状态 chip 映射表补原型链键防御 (审查 F-1 回流) (#146)` — `ui/src/views/TasksBoardView.jsx` (+31/−3), `ui/tests/test_issue146_todo_status_i18n.mjs` (+110/−7), `ui/dist/index.html` (rebuilt in the same commit).

Aggregate over the range: `git diff --shortstat af92695..70e11a7` reports 18 files changed, 817 insertions(+), 134 deletions(-). Files touched (18): `mw/console.py` (+57/−15), `ui/src/views/TasksBoardView.jsx` (+47/−1), `ui/src/views/CronView.jsx` (+25/−8), `ui/src/i18n/locales/{en,zh-cn}.json` (+6/−1 each), `ui/dist/index.html` (+90/−90, rebuilt), `ui/package.json` (+1/−1), `tests/test_cron_api.py` (+71), `tests/test_issue124_status_alias.py` (+18), `tests/test_issue145_subagent_min_doc_sync.py` (new, 92 lines), `ui/tests/test_issue146_todo_status_i18n.mjs` (new, 261 lines), `ui/tests/test_cron_view.mjs` (+62/−1), `ui/tests/test_issue87_fmtapierror_coverage.mjs` (+63/−7), `ui/tests/test_issue58_tokens.mjs` (+10/−5), `DEPLOY.md` (+2/−1), `design.md` (+2/−1), `docs/main-soul.{en,zh-cn}.md` (+2/−1 each).

### Fixed

- **A prototype-chain key could reach the cron view's CSS class and its wording (#142)** — `cronRunBadge`, `cronStateBadge` and `cronStateKey` looked their argument up in a plain object literal, so a value like `constructor`, `toString` or `__proto__` resolved to something inherited from `Object.prototype` instead of missing: the badge function returned a function object where a CSS class name belonged, and the state-key function returned the key unchanged, which rendered a bare key in the UI. All three now guard with `typeof x === 'string'` first and then test own-key membership with `Object.prototype.hasOwnProperty.call`, so any non-string shape and any inherited key collapses to `badge-unknown` / `unknown`. The `typeof` guard is not decoration: `hasOwnProperty.call(RUN_BADGE, ['completed'])` is `true`, because an array is coerced to the string `'completed'` and hits a real key — so own-key testing alone would let an array through. Three new cases pin this (eight prototype-chain keys across the three functions, four bad array shapes, and a source scan that fails if a second hand-written whitelist reappears).
- **The cron run-status wording had a second, hand-maintained whitelist (#142)** — `cronRunStatusKey` compared against four hard-coded literals while `RUN_BADGE` was the table that actually decided the badge, so adding a status to one and not the other would silently pin the wording at "unknown" forever. The whitelist is now derived: `const RUN_STATUS_KEYS = new Set(Object.keys(RUN_BADGE))`. A guard case asserts the derived set equals the table's own keys and that the literal form cannot come back.
- **The cron bridge's stderr now has a file an operator can read (#143)** — `mw/console.py` had no logging framework at all, and the console is started by `deploy/main_entry.sh` in a watchdog loop with no stdout/stderr redirection, so the only place a bridge failure was visible was `docker logs`, which does not survive a container recreate. The console now installs a module-level `mw.console` logger with a `RotatingFileHandler` at `${MW_HOME}/logs/console.log` (default `/mw_data/logs/console.log`, which the compose file bind-mounts from the host at `./data/mw_data/logs/console.log`), rotating at 5 MiB with 3 backups — console.log plus three backups is a 20 MiB ceiling. Writing to the log can never become a new way for the console to fail: the handler construction is inside a `try`, and if the target is not writable the logger falls back to stderr. The `[cron-bridge] rc=… stderr_tail: …` line moved from `print(..., file=sys.stderr)` to that logger, and the #135 contract is unchanged — the tail is still collapsed to one line, still truncated to 500 characters, and still never enters the HTTP response body, which carries only the fixed message plus the exit code.
- **A test file could go red because someone added a line above it (#144)** — `test_issue87_fmtapierror_coverage.mjs` S2-h located the two surviving `x.message || x` residuals by line number, and every insertion above them shifted the anchors (the file's own comments record two such corrections already). The line numbers are gone and replaced by a content anchor that matches the residual shape itself — a back-reference capture group expressing "the same identifier", deliberately isomorphic with the residual regex it is checking against, so it is immune to identifier renames. The anchor set must be *identical* to the residual set, not merely non-empty: asserting only "the anchor hits something" would stay green after one of the two residuals was deleted. Inserting 1, 5 or 20 lines above the target now leaves the anchor count unchanged, which is the positive proof of drift immunity.
- **The English console no longer shows Chinese in the TODO status chip (#146)** — see "Changed" below; this also supersedes the note in the #132 entry.
- **The new chip lookup reproduced the very defect this batch fixed elsewhere (#146, review finding F-1)** — the first #146 commit looked `TODO_STATUS_TEXT_KEYS[semToken]` up as a truthy table, which is exactly the prototype-chain bug #142 was raised to remove, in the same frontend, in the same batch; and the tests written alongside it did not cover it. The lookup is now a named pure function `todoStatusTextKey()` with the same two-stage guard as #142 (`typeof` then own-key test), plus `todoStatusText()` for the chip wording itself, so the consumption site cannot quietly re-open a direct table lookup. Three more cases were added: prototype-chain keys, bad shapes, and a source scan that counts subscripts of the mapping table and fails if anything outside the guard function indexes it. Reverting the guard turns all three red; deleting only the `typeof` guard turns two of them red, which is why the guard is not optional.

### Changed

- **The status wording a card chip shows is now rendered by the frontend in the reader's language (#146)** — the backend's `_TODO_STATUS_SEM` table used to carry hard-coded Chinese display text in the position the chip rendered, so an English reader saw 执行中 / 等你回复 / 子任务处理中. The second element of that tuple is now the canonical token (`working` / `blocked` / `completed` / `failed` / `waiting_sub`) and the frontend maps token to a locale key, five new keys added bilingually in the same commit (`tboard.todo_status_*`, en/zh key sets identical, 693 leaves each side). The Chinese values are byte-identical to the pre-change wording, so a zh-cn deployment reads exactly as before. This is a cross-layer contract change: an old frontend against a new backend would display the bare token, and a new frontend against an old backend degrades cleanly by passing the Chinese text through — the batch ships both sides in one commit, so no deployment has that mixed shape.
- **`MW_REAPER_SUBAGENT_MIN`'s default has one home again (#145)** — the number 60 was written down in four places (`mw/reaper.py::_subagent_wait_min`, the DEPLOY.md environment table, and both deploy souls), so changing the default meant remembering to change three documents. The three document sites now say the default is defined by `mw/reaper.py::_subagent_wait_min` and point there, and a new guard test asserts both halves: each document contains that reference verbatim, and no line that mentions a subagent restates the number (patterns `60 min`, `= 60`, `default 60`). The guard reads the code's own fallback literal rather than hard-coding 60, so changing the default in code needs no test edit — a mutation that changes the code default leaves the documents green, while a mutation that writes the number back into a document turns the guard red.

### Notes

- **What was deliberately not touched** — `mw/reaper.py` (the semantic source of truth, its default unchanged), `mw/todo.py` (the settlement path and the TODO.md contract parser are byte-identical to the baseline), `mw/i18n/*`, `mw/console_ui.py`, and everything under `deploy/` and `tools/`. No new dependency, secret, mount or compose change. The frontend bundle is tracked in exactly one place (`ui/dist/index.html`) and was rebuilt in the same commits that changed `ui/src`.
- **Three document corrections ride with this entry** — the troubleshooting row added for #143 said the 503 message was an English string with a Chinese equivalent, but the backend's `error` field is the same fixed Chinese string on every locale; it said `docker logs` still works as a fallback, which is only true when the log file itself is not writable; and it said to look for lines *starting with* `[cron-bridge] rc=`, when the formatter puts a timestamp first so the marker is a substring. The rotation-ceiling comment in `mw/console.py` said 15 MiB where `backupCount=3` means four files and 20 MiB. All four were corrected here; no behaviour changed, only prose and a comment.
- **The #132 note about the Chinese chip is superseded** — that entry recorded "the English deployment still shows the Chinese chip for this status … was not widened here". #146 widened it; see the #132 entry's back-reference.
- **Verification** — the full backend suite is 801 passed with no failures against a 792-passed baseline, a nine-test increase all attributable to this batch, and the failure-set difference between baseline and head is empty; the frontend suite is green across all 14 entry points; `tools/i18n_docs_sync.py --strict`, the dist sync gate and the i18n protocol guard all exit 0. Twenty-one mutation counter-proofs were run at re-test, plus two more with the F-1 follow-up: fifteen turned red as expected, three were positive proofs (line-drift immunity, whitelist auto-follow, code-default change needing no document edit), two were behaviour-equivalent mutations that correctly stayed green — their strengthened forms do turn red, recorded rather than hidden — and one was a contradiction inside the plan document, adjudicated before the review rather than treated as a defect.
- **Follow-ups registered, none blocking** — the frontend token map and the backend `_TODO_STATUS_SEM` table are two lists kept in step by a test rather than by construction (resolved by batch-I commit `9354d42`: both sides now derive from `mw/todo_status_contract.json` — see the batch-I entry); the #144 content anchor is coupled to the locale key it quotes, so renaming that key requires updating the anchor; and the source-shape scan covers one spelling of the derived whitelist. These are "could be stricter" items, not defects, and were logged for the coordinator to decide on.

## Unreleased — Issue #147: the main agent converts a requirement into a card instead of researching it, and every ambiguity leaves with an owner (2026-10-28)

Range: one commit `96e34e6` — `docs(#147): main SOUL 需求转换纪律 — 建卡前零研判/承接卡骨架/三分法/时间盒` (one commit ahead of `b0b4bad`, so the range is `b0b4bad..96e34e6`). Files touched (2): `docs/main-soul.zh-cn.md` (+41/−2), `docs/main-soul.en.md` (+57/−4) — `git diff --shortstat b0b4bad..96e34e6` reports 2 files changed, 98 insertions(+), 6 deletions(-); plus this CHANGELOG entry.

### Changed

- **The main agent's pre-card window is now bounded, and its deliverable is a card** — the deploy souls already said "classify coarsely, create the first card immediately, do not reproduce the problem, do not analyse root causes, do not review code yourself", but they never said what main may still do before that card exists, nor what the card must contain. The new subsection "Requirement-Conversion Discipline: zero pre-card research" (issue #147) states both: the only output is a structured requirement-intake card, and the pre-card window allows only second-level evidence — fetch the issue body and its comments, download attachments, read PROJECT-MEMORY — while grepping the repo, reading git history, running tests and probing remote machines are explicitly the receiving role's deliverable, not main's. A timebox closes the window: at most 10 minutes and 20 tool calls, and blowing it is a process violation rather than a slow start.
- **Ambiguity is split into three kinds, because the three are resolved by different parties** — a fact gap (which version broke it, whether the data source exists) is findable in the repo or on the machine and is pushed down into the first research card's goal; an intent gap (scope boundary, acceptance criteria, priority trade-off) is known only to the user and is pushed down into the card's Open Questions, where the receiving role escalates it through `.clarify.json` one at a time if thorough research still cannot settle it; a shape gap (no deliverable definition, no completion criteria) needs nobody's knowledge at all — it is a format conversion main does itself. The old acceptance wording "no ambiguity" is replaced by "every ambiguity is listed and has an owner": a requirement the next LLM can take is precisely one that knows where it is still fuzzy.
- **The intake card has a fixed skeleton, and the first card is a research card** — the body of a requirement-intake card now has a fixed shape (Goal / Context / Deliverable / Constraints / Open Questions, one ambiguity per line, tagged fact or intent), and the first card defaults to a research/adjudication card whose body states only the research goal plus the open questions — main does not pre-do the receiver's localisation work. The assignee is still chosen the way it always was: the classify endpoint plus `/api/workers` scope.tags semantics.
- **Three existing clauses were widened so the discipline has one home** — the "Things you don't do" entry now lists the four forbidden pre-card evidence-gathering moves alongside the old reproduce / analyse / review trio, with a cross-reference to the new subsection; the workflow's step 2 gains a bullet saying the first card defaults to a research/adjudication card; and the `.clarify.json` section gains the sentence that open questions the receiver still cannot adjudicate escalate one at a time through that channel, and that main must not batch-resolve them before card creation. Both language versions were changed together, section for section.

### Notes

- **No product code moved** — the diff is exactly two files, `docs/main-soul.zh-cn.md` and `docs/main-soul.en.md`; `mw/`, `tests/` and `deploy/skills/` are untouched, so the settlement path, the status machine and the TODO.md contract parser are byte-identical to the previous baseline. The requirement-intake skeleton is indented prose inside the soul, so it never parses as a TODO.md protocol key.
- **The bilingual guards hold** — the `REQUIRED` marker count is still 9 in both souls (the new subsection sits inside an existing REQUIRED block and adds no marker line), the English soul has zero CJK outside code fences, the `/api/decision-paths/classify` pointer is still in both, no hand-computed scoring formula was introduced, and `tools/i18n_docs_sync.py --strict` exits 0.
- **Verified** — the full backend suite is 792 passed with no failures, identical to the baseline count; the SOUL guard cases (bilingual decision-path section, English CJK-outside-code, classify-endpoint pointer) and the bilingual-document gate are green.
- **Known tension, deliberately left in place** — the `team-work-chain-orchestration` skill still says that main may take bounded live measurements and hand the coder card precise file/line anchors, which reads wider than the new zero-pre-card-research rule. The product decision was to keep the skill text unchanged for now, so the tension is registered rather than resolved; a follow-up card to tighten the skill is a separate decision (superseded by batch-I commit `f61d38b`, which rewrote the conflicting clauses — see the batch-I entry).
- **The run-time half of the issue is not a text requirement** — the issue's acceptance numbers (median pre-card window, share of grep-style calls) are run-level measurements with no static source of truth, so they are tracked as an observation item for real runs rather than written into the souls.

## Unreleased — Issue #132: a worker waiting on its own subagent now says "Subagent", not "Waiting for you" (2026-10-28)

Range: seven commits `b14be4c` + `795bd38` + `03cc13f` + `6f8f2ee` + `0a11086` + `6d78eca` + `b292263` — `fix(issue132): worker 等待子任务显示「子任务处理中」不再误显示等介入 (#132)`, `fix(#134/#136): cron runs 徽标 unknown 归一 + 展开连点竞态封死 (#134/#136)`, `fix(issue137): 桥层测试缺 venv 时 pytest.skip (裸 return 改 skip) (#137)`, `fix(issue135): _cron_bridge rc≠0 的 stderr 不再透传进响应 detail (#135)`, `fix(issue130): test_team_work_reaper.py 裸 rmtree 改安全清理 (#130)`, `fix(#129/#128): dist 门禁 C2 三态判定 + share 副本 ignore + 头注释悬空引用 (#129/#128)`, `fix(issue131): DEPLOY.md 环境变量表补 HERMES_WEBUI_PORT 行 (#131)`. Files touched (19): `ui/src/views/TasksBoardView.jsx` (+9), `ui/src/views/WorkersView.jsx` (+6), `ui/src/views/CronView.jsx` (+19/−2), `mw/console.py` (+18/−2), `ui/src/i18n/locales/{en,zh-cn}.json` (+2 each), `ui/tests/test_issue132_waiting_sub.mjs` (new, 153 lines), `ui/tests/test_cron_view.mjs` (+203/−1), `ui/tests/test_issue58_tokens.mjs` (key-count anchor 686 -> 688, tboard 123 -> 124), `ui/tests/test_issue87_fmtapierror_coverage.mjs` (line anchors 1410/1421 -> 1419/1430), `ui/package.json` (the new frontend suite is wired into `npm test`), `tests/test_cron_api.py` (+59/−10), `tests/test_team_work_reaper.py` (+114/−57), `deploy/check_dist_sync.sh` (+27/−9), `.gitignore` (+3), `DEPLOY.md` (+1), `docs/main-soul.{en,zh-cn}.md` (+14 / +11), `ui/dist/index.html` (rebuilt in the same commits), plus this CHANGELOG entry.

### Fixed

- **A worker waiting on its own subagent was displayed as "Waiting for you"** — `waiting_sub` is the status a worker writes while it waits for a background subagent it dispatched itself, so the ball is with the worker, not with the user. The console had no display branch for it: `TasksBoardView`'s `classify()` fell through to the amber `WAITING` badge and `WorkersView`'s `DERIVED_TO_VISUAL` had no entry, so a card could fall all the way through to the idle/offline fold and read as "Waiting for you" (zh) / "Waiting" (en) — telling the user to intervene when nobody had to. The card badge now has its own branch (busy-family blue, the `tboard.state_waiting_sub` wording), the header's current-card chain got a matching branch placed before the `WORKING` fallback, and the worker board maps `WAITING_SUB` to a dedicated `subagent` visual state (blue, reusing the busy glyph). The backend's `_TODO_STATUS_SEM` entry for `waiting_sub` moved from the grey "pending" family to the same running family and wording. The existing "Waiting for you" keys are untouched — `WAITING` still reads as waiting for you.
- **The settlement path was not touched, and that is asserted, not assumed** — the ledger, the reaper and the status module are byte-identical to the previous baseline (`git diff` over `mw/ledger.py`, `mw/reaper.py`, `mw/todo.py` is empty), so the card's settlement query, the timeout-reap path and the status-alias table cannot have been affected by a display-only change.
- **Both deploy souls now state the rule** — `docs/main-soul.{en,zh-cn}.md` gained a short section: while a worker's status is `waiting_sub`, do not intervene, do not resume, do not declare it stuck, do not harvest early; the only moment to intervene is after the reaper's dedicated over-threshold knock (`MW_REAPER_SUBAGENT_MIN`, default 60 min), and "Waiting for you" (`WAITING`) and "Subagent" (`waiting_sub`) are two different states.

### Fixed (continued)

- **A cron run badge could render an untranslated key** — the run-status badge built its locale key by string concatenation (`cron.run_status.` plus the raw status), so a dirty value or a shape the library had not produced yet rendered a bare key to the user. The state side of the same view already had a normalising pure function; the run side did not. The new `cronRunStatusKey()` returns the four known statuses unchanged and normalises everything else to `unknown` — and because it compares literally instead of looking the value up in a table, prototype-chain keys (`constructor`, `toString`, `__proto__`) cannot slip through. Both call sites use it, and the old "fall back only when falsy" form is gone. No locale key was added: `cron.run_status.unknown` already existed on both sides.
- **Rapidly toggling the run list could overwrite the newest result with a stale one** — expanding, collapsing and expanding again fires several requests, and a slow earlier response could land after a faster later one and repaint the panel with the wrong rows. The component now keeps a per-job request generation counter (a ref, so it never enters the render path) that every toggle advances; both the success and the failure path compare the generation before writing back and drop a stale response. Guarding only the success path would still have let a stale failure repaint a successful panel as an error.
- **A failing cron bridge no longer echoes its stderr into the HTTP response** — when the bridge subprocess exits non-zero, the response `detail` carried the tail of the process's stderr. The bridge script already collapses its own business exceptions, so a non-zero exit only ever means an interpreter-level fault (a path, a version) — information that does not belong in a response body. The response now carries a fixed message plus the exit code, and the stderr tail moves to the console's log stream, collapsed to one line (so a multi-line payload cannot forge extra log lines) and truncated to 500 characters. The log write is itself guarded, because a closed stderr pipe must not become a new way for this function to throw.
- **A test file could report coverage it never had** — the cron bridge cases skipped themselves with a bare `return` when no cron virtualenv was present, which pytest records as *passed*. The report therefore showed coverage that did not exist. All eight sites now call `pytest.skip()` with a reason that names the missing interpreter, split into the two real cases (the interpreter is absent vs. the interpreter exists but cannot import the cron package). The product assertions are unchanged.
- **A cleanup race could turn a green suite red** — the reaper test file built its sandbox with `mkdtemp` and deleted it with a bare `rmtree`, while the reporting thread reopens the ledger in the background and recreates its write-ahead-log files; the delete scan and the recreate interleaved, and the final directory removal failed. The directory lifecycle moved to the pytest `tmp_path` fixture, with a matching fallback for the direct-run path. The product assertions are unchanged, and the file's direct-run entry point also stops referring to a test name that no longer exists.

### Changed

- **The dist gate can now tell "deployed but the copy was deleted" from "not deployed yet"** — the copy-consistency check skipped whenever the runtime bundle was absent, which conflated a bare clone with a deployment whose runtime bundle had gone missing. The gate now uses the stamp file that the install script writes at deploy time: stamp present but the bundle gone is a failure (with the fix — re-run the install script — in the message), stamp and bundle both absent is the legitimate skip. The header comment of the gate script now points at the in-repository documentation instead of a process note that does not exist in a clean clone.
- **The second deployment copy of the frontend bundle is now ignored** — the install script copies the bundle to a host-side share directory as well, and that copy could be pulled back into the index by a blanket `git add` in a deployed working tree. The ignore rules now cover it.
- **One more environment variable is registered** — `HERMES_WEBUI_PORT` was missing from the environment-variable table, although the compose file, the install script and the verify script all read it. The table now lists it next to the other Hermes-home entries, with the row label spelling out that it is the optional WebUI embed's port and not this platform's console port (which is `CONSOLE_PORT`).

### Notes

- **No new secret, package, mount or compose change** — this batch touches the console's status display, one error-message path, two test files, one shell gate and three documents. The cron bridge keeps using the existing virtualenv interpreter and the existing Hermes home; the install and verify scripts are unchanged.
- **Deployment impact** — `ui/src` changed, so `ui/dist/index.html` was rebuilt and committed in the same commits as the source changes (the tracked-artifact discipline in DEPLOY.md §5 is unchanged: only `ui/dist` is tracked, and the install script copies it at deploy time). **The runtime effect needs a user-side `git pull` followed by the install script** — a pull alone does not refresh the deployed bundle.
- **The English deployment still shows the Chinese chip for this status** — the backend's status wording table is hard-coded Chinese, so an English reader sees the Chinese wording in the card-status chip. This is the pre-existing shape of the other statuses in the same table and was not widened here. **Superseded by issue #146** (see the batch-H entry above, commits `42b8cd6` + `70e11a7`): the status wording is now a canonical token the frontend renders in the reader's language, so an English deployment no longer shows the Chinese chip.
- **Verification** — the full suite is 792 passed with no failures, the frontend suite is green, the dist gate is green and the bilingual-document gate exits 0. The mutation counter-proofs for each fix turned red when the fix was reverted and green again after it was restored.

## Unreleased — Scheduled tasks: the console gets a "Scheduled" menu entry that lists cron jobs and can pause / resume / queue them (2026-10-27)

Range: five commits `ffca75b` + `0a3c2e9` + `2f48ece` + `391fa7c` + `e66cc99` — `feat(cron): 定时任务端点 + venv 子进程 JSON 桥 (卡 t_da1c3a64 / s4)`, `feat(cron-ui): 定时任务视图 CronView + NAV 入口 + 双语词条 + dist 重建 (卡 t_da1c3a64 / s4)`, `test(cron): 定时任务两层测试 CT1-CT9 + MV1-MV3 (卡 t_da1c3a64 / s4)`, `test(cron): D-1 MV3 变异锚限定 _cron_bridge 作用域 + 补行为断言 (卡 t_59874c29 / s5b)`, `fix(cron-ui): D-2 窄屏操作区独立行 + 按钮 44px 触达规范 (卡 t_59874c29 / s5b)`. Files touched (11): `mw/console.py`, `tests/test_cron_api.py` (new, 540 lines), `ui/src/views/CronView.jsx` (new, 374 lines), `ui/src/App.jsx`, `ui/src/api.js`, `ui/src/i18n/locales/{en,zh-cn}.json` (+50 each), `ui/tests/test_cron_view.mjs` (new, 361 lines), `ui/tests/test_issue58_tokens.mjs` (key-count anchor 652 -> 686), `ui/package.json` (the new frontend suite is wired into `npm test`), `ui/dist/index.html` (rebuilt in the same commits), plus this CHANGELOG entry, plus `design.md` (§11.7 endpoint registry) and `DEPLOY.md` (§3 environment-variable table).

### Added

- **A "Scheduled" entry in the console menu** — `CronView` renders the cron job list with four states (loading skeleton / empty / error with retry / ready), polling every 20 s only while the tab is visible and never while a write operation is in flight. Each card shows name, schedule, last/next run, and a two-line prompt summary. The NAV entry sits between "Workflows" and "Pixel Office" with a clock icon, and the whole view is bilingual (34 new locale keys, both sides in the same commit).
- **Three job operations, each with a frozen visibility matrix** — `scheduled` jobs offer "Run" and "Pause", `paused` jobs offer "Resume", terminal (`completed` / `error`) jobs offer none and point the user at the main agent instead. The matrix is a pure function (`cronActions(state)`), so it is asserted cell by cell in tests. There is no second confirmation dialog (all three operations are reversible) and no optimistic rewriting: the flow is *disable -> request -> toast -> re-fetch -> re-enable*.
- **"Run" means "queue for the next tick", and the toast says so** — the success copy is `已排入下次调度` / `Queued for the next tick`, deliberately not promising that the job has already started. The scheduler-liveness heartbeat is read directly from the ticker files; when the ticker is down a persistent banner says so, and when the heartbeat file is missing no banner is shown at all.
- **Five backend endpoints** — `GET /api/cron/jobs` (list + ticker liveness), `GET /api/cron/jobs/<id>/runs?limit=` (recent executions, clamped to 1..10, default 3), and `POST /api/cron/jobs/<id>/pause|resume|trigger`. The error contract is fixed: 404 when the job is gone, 409 on a state conflict (a terminal job cannot be triggered), 422 for a malformed `job_id` or an unknown action, 503 when the cron data source is unavailable. The endpoint registry in `design.md` §11.7 now lists all five.
- **A JSON bridge to a venv subprocess, because the console's own interpreter cannot import `cron.*`** — the console runs on `/usr/bin/python3.13`, where `import cron` fails (`ModuleNotFoundError`, and `PYTHONPATH=/opt/hermes` still misses `yaml`). So the console starts a short-lived subprocess on the Hermes venv interpreter (`PYTHON_BIN`, default `/opt/hermes/.venv/bin/python3`) and speaks JSON over stdin/stdout. The bridge script is an embedded constant that uses only public `cron` API (`list_jobs` / `get_job` / `pause_job` / `resume_job` / `trigger_job` / `effective_job_state` / `list_executions`); no `shell=True`, no string concatenation into Python source, and the request fields are whitelisted (`action` / `job_id` / `limit`). The `job_id` is validated against `^[A-Za-z0-9_-]{1,64}$` before the subprocess is even started, and the action is checked against a whitelist at both the endpoint and the bridge.
- **Same-source guarantee for the job store** — the subprocess environment explicitly overrides `HERMES_HOME` (never inheriting a possibly-mutated value) with the value of `MW_HERMES_HOME` (default `/opt/data`), and the bridge echoes the home it actually used back in its response; the console asserts the two are equal and answers 500 if they are not. This is the same store the scheduler ticker reads, so the list the user sees is the list the scheduler runs.

### Notes

- **Deliberately not in this batch** — there is no delete and no edit. Removing a job is irreversible and was not on the feature list; changing a schedule or a prompt goes through the main agent's LLM instead. The bridge's action whitelist has exactly five entries (`list` / `runs` / `pause` / `resume` / `trigger`), so there is no code path that could delete a job even by accident.
- **Deployment impact** — this batch changed `ui/src`, so `ui/dist/index.html` was rebuilt and committed in the same commits as the source changes (the tracked-artifact discipline in DEPLOY.md §5 is unchanged: only `ui/dist` is tracked, and `install.sh` copies it at deploy time). No new secret, no new Python package, no new mount and no change to `deploy/install.sh` or `deploy/verify.sh`: the cron bridge reuses the existing venv interpreter and the existing Hermes home. `deploy/main.compose.yaml` was not touched.
- **The console has no independent authentication layer on port 8700** (no Origin/Referer/CSRF check), so the cron write endpoints follow the same convention as the existing write endpoints (`/api/task/cancel` and friends): the port is only exposed on a trusted internal network, and the write path is guarded by the action whitelist, the `job_id` regex, and the bridge's lack of an injection surface. This is a documented pre-existing convention, not a change introduced here.

### Tests

- **Two test layers** — the endpoint layer mocks the bridge boundary and asserts the response shapes, the error-code table and the red lines (no `DELETE` route, whitelisted `job_id`, no private `cron` symbols in the bridge source, `HERMES_HOME` explicitly passed); the bridge layer really runs the subprocess against an isolated `HERMES_HOME` with a fake job store, so pause/resume round-trips and trigger semantics are exercised against the real library.
- **Mutation counter-proofs have teeth** — removing the bridge's timeout branch turns two cases red (the scope anchor no longer finds the branch, and the behavioural assertion sees the generic fallback's `str(e)` instead of the timeout-only `detail`); deleting the "Run" button from a `scheduled` job turns the visibility case red. The timeout anchor is scoped to the bridge function body on purpose: an unscoped anchor used to be satisfied by an unrelated `except TimeoutExpired` elsewhere in the same file.
- **Regression counter-proofs for the two review findings** — the narrow-screen fix (the action row becomes its own column below 768 px and the buttons reach the 44 px touch target, with the desktop layout restored byte-for-byte above the breakpoint) was measured in a real 375 px browser: every button is 44 px tall, nothing overflows horizontally, and the desktop rendering is unchanged.

## Unreleased — Issue #125: the second tracked frontend bundle is gone, so dist drift is physically impossible, and a local gate keeps it that way (2026-10-27)

Range: two commits `ba28911` + `409f6ea` — `fix(#125): dist 双份漂移根治 — 路线A 单一 tracked 产物 + 本地门禁 + 口径同步` and `chore(#125): 门禁脚本补可执行位 (deploy/check_dist_sync.sh), 与 deploy/install.sh / deploy/verify.sh 一致`. Files touched: `.gitignore`, `DEPLOY.md`, `deploy/check_dist_sync.sh` (new, 126 lines, mode 100755), `deploy/install.sh` (comments only), `deploy/verify.sh`, `mw/ui_dist/index.html` (deleted from index, 347 lines), plus this CHANGELOG entry.

### Fixed

- **The dual-bundle drift trap is closed at the source (route A)** — the repo tracked two frontend build outputs (`ui/dist/index.html` and `mw/ui_dist/index.html`) and they had silently drifted (md5 `406cfa2a…` vs `6fd0c573…`); anyone validating the UI against the in-repo `mw/ui_dist` copy tested a stale UI and misread it as "features disappeared". `mw/ui_dist` is now untracked (`.gitignore` covers the whole directory) and is rebuilt by `install.sh` §3 at deploy time from `ui/dist` — the only tracked build output is `ui/dist/index.html`, so the "two copies can disagree" state no longer exists. **Lineage note: this supersedes the old INV-13 dual-copy byte-identical discipline** referenced in earlier entries of this changelog; the current contract is DEPLOY.md §5 ("only ui/dist is tracked; validate the UI via the live endpoint or ui/dist, never the working-tree mw/ui_dist").
- **A local gate keeps it that way** — new `deploy/check_dist_sync.sh` (no CI platform needed): C1 fails if `mw/ui_dist` ever returns to the index, C2 fails on md5 drift between `ui/dist` and the local runtime copy in either direction (skipped, not failed, on a bare clone before deploy), C3 fails if `ui/dist/index.html` is missing. Hashing goes through `deploy/lib/hash.sh::hash_of` (issue #113 discipline: no bare `md5sum`), messages are bilingual via `deploy/lib/i18n.sh`. `verify.sh` runs it report-style (exit code wrapped in `if`, FAIL counted without truncating the remaining sections), taking the verify checklist from 13 to 14 items.
- **Verified** — four-way mutation counter-proofs all red (drift either copy → C2 red; force-add back to index → C1 red; missing `ui/dist` → C3 red), bare clone stays green with C2 correctly SKIPPED; the 170 real-machine end-to-end chain closed for the first time: clean clone → `install.sh` → `curl :console | md5sum` == `ui/dist` md5 (`406cfa2a…`) → `verify.sh` 14/14. Red lines held: zero `ui/src` changes, zero `console.py` dual-channel changes.

## Unreleased — Issue #126: two test cases stop producing false reds under concurrent runs (tmp_path lifecycle + a behavioral short-circuit assertion) (2026-10-27)

Range: one commit `68d5862` — `fix(#126): 两个用例时序/清理竞态 — g9 改 tmp_path + C2 改行为断言`. Files touched: `tests/test_reaper_issue_9_guards.py`, `tests/test_issue58_tokens.py` (tests-only, +52/−7), plus this CHANGELOG entry.

### Fixed

- **g9 cleanup race** — the case built its sandbox with `mkdtemp` + a bare `rmtree` in `finally`, and the reporting-relay thread that `reap_once` spawns reopens the ledger after the test body, recreating WAL files under the directory being deleted (`OSError: [Errno 39]`). The directory lifecycle moved to the pytest `tmp_path` fixture (retrying teardown), with a `TemporaryDirectory(ignore_cleanup_errors=True)` fallback for the `__main__` direct-run path; both product assertions are byte-identical to before.
- **C2 wall-clock proxy** — `assert time.time() - t0 < 2.0` was a proxy for "empty registry → settlement short-circuits without waiting on HTTP", and heavy load broke it at 2.04s. The case now asserts the semantics themselves: monkeypatched `urlopen` probe records zero HTTP calls on the settlement path (`_http_calls == []`), which is strictly stronger than the wall clock. The wall-clock guard is kept (red line: never delete it) with the threshold moved 2.0 → 5.0, the exact single-call HTTP timeout in `harvest.py`, so a real hang still trips it.
- **Verified** — 170 real-machine concurrency hard gate: 3 rounds x dual-process full-suite = 6 full runs, 0 false reds; local targeted 8-process x 2 rounds clean; mutation counter-proof (forcing one HTTP call in the short-circuit) turns the new assertion red; full suite 764 passed unchanged; direct run 17/17.

## Unreleased — Issue #127: five E-batch review carry-over cleanups, with the timeout-settlement trap pinned by regression locks (2026-10-27)

Range: one commit `506a7e0` — `fix(#127): E 批审查建议级五项清理 — timeout 前置短路 + 单趟模板替换`. Files touched: `mw/harvest.py`, `mw/reaper.py`, `mw/console_files.py`, `mw/worker_contract_prompt.en.md`, `tests/test_team_work_reaper.py`, `tests/test_issue122_md_open.py`, `tests/test_i18n_protocol_guard.py`, plus this CHANGELOG entry.

### Fixed

- **S-1 harvest status normalization, without breaking timeout reaping** — the failed/blocked branches now route through `normalize_status` with ASCII-subset alias sets proven equal to the old literal sets (zero behavior change), while `"timeout"` — a platform-internal reaper reap value, not a worker-writable TODO status, and deliberately absent from `STATUS_ALIASES` — is short-circuited before normalization with an explanatory comment. Naively folding it into the normalizer would have dropped timeout reaping into the unknown-status fallback and silently reclassified it COMPLETE; two new regression locks (`test_s1_harvest_timeout_still_FAILED_with_timeout_reason`, `test_s1_timeout_not_in_alias_table`) pin behavior and table against future vocabulary changes.
- **S-4 template substitution is now single-pass** — the chained `replace(__TITLE__).replace(__BODY__)` allowed placeholder re-expansion in both directions (a filename containing the body placeholder injected content into `<title>`; a body containing the title placeholder got the filename injected). One `re.sub` with alternation and a lambda resolves each placeholder exactly once; injection counter-proof tests cover both directions. The issue's suggested "swap the replace order" was measured to only move the defect (4 reds on the other direction) and was rejected.
- **S-2 / S-3 / N-4** — `reaper.py` docstring now names the real constant `IN_PROGRESS_SET`; unused imports removed (`Path` in `console_files.py`, `os` in `test_i18n_protocol_guard.py`, both AST-verified as unused); `worker_contract_prompt.en.md` gained its trailing newline, matching the zh side (zh byte-equal baseline intact).
- **Verified** — four mutation counter-proofs all red then restored green (disable the timeout short-circuit → the P0 silent-reclassify reproduces; inject `timeout` into the alias table → table guard red; restore chained substitution → 3 reds; swapped order → 4 reds); targeted suites 18 passed + 61/61 script-style; full suite 766 passed on both sides (the +2 cases are the two new S-1 locks, set diff closed).

## Unreleased — I-1 follow-up: markdown open-mode rendering no longer 500s on adversarial NUL-placeholder input (2026-10-26)

Range: one commit `0cd3110` — `fix(I-1): md_render NUL 占位符越界致 open 模式 .md 渲染 500, 索引防护+渲染段兜底双保险`. Files touched: `mw/md_render.py`, `mw/console_files.py`, `tests/test_issue122_md_open.py`, plus this CHANGELOG entry. Source: review finding I-1 (`REVIEW-E-20261026` §6) and doc-audit carry C-1, fixed as a follow-up card on `main` after the E-batch merge.

### Fixed

- **A crafted file could turn "open in browser" into a 500** — the placeholder restore regex in `mw/md_render.py` matched literal `\x00<digits>\x00` sequences that survive `html.escape`, and an out-of-range index into the code-snippet list raised `IndexError`, which `handle_fs_get` did not catch. The restore step now bounds-checks the index against `[0, len(codes))`: legitimate placeholders still restore to `<code>`, forged ones stay as escaped text with no tag injection.
- **Any render-time exception now degrades instead of failing the request** — `_render_md_open_page` wraps the render call in `except Exception` and returns `None`, routing the file through the existing `text/plain` fallback path, so the documented degradation contract covers every render failure, not just oversize and read errors.
- **Verified** — 5 new regression cases in `tests/test_issue122_md_open.py` (adversarial input does not raise, legitimate code still restores, no forged `<code>42</code>`, multi-segment restore unaffected, pure-fake generates no tag); independent probe on the merged tree confirms the original crash input returns escaped text and normal code spans still render.

## Unreleased — Issue #124: TODO status values get one bilingual alias table behind a single normalization entry point, ending silent settlement stalls (2026-10-26)

Range: one commit `dc91f69` — `fix(#124): TODO 状态中英别名表 + 单一归一化入口, 结算卡死根治`. Files touched: `mw/todo.py` (+42), `mw/reaper.py`, `mw/console.py`, `mw/worker_contract_prompt.{en,zh-cn}.md`, `tests/fixtures/worker_prompt_baseline_zh.txt`, `tests/test_i18n_r3_todo.py`, `tests/test_issue124_status_alias.py` (new, 275 lines), plus this CHANGELOG entry.

### Fixed

- **Chinese and variant status writings were silently mis-settled** — the terminal-state recognition was six scattered hardcoded sets (reaper completed/failed/blocked gates, the in-progress set, the late-settlement gate, the console reassess gate), none of which knew `已完成` / `已失败` / `已阻塞` / `done`. A worker writing `状态: 已完成` got its card reaped as `timeout` (false failure) and `状态: 已阻塞` never reached NEED_CLARIFICATION (silent chain stall).
- **One entry point, one table** — `mw/todo.py` now owns `normalize_status(raw)` over `STATUS_ALIASES`: five canonical English values (completed / failed / blocked / working / waiting_sub), 21 aliases total, case- and whitespace-insensitive, unknown input returns `None` (no guessing). All six reaper call sites and both console gates (display mapping + reassess) converge on it; the local in-progress set construction and its direct constant references are gone, and a repo-wide scan guard forbids reintroducing bypass literals. `_TODO_STATUS_SEM` is keyed by canonical value, so a Chinese-variant card now shows the correct badge instead of the gray queued one.
- **Worker contract prompts teach the canonical form** — both bilingual templates state that terminal states are written with the English canonical values, with the Chinese writings kept only as compatibility aliases.
- **Verified** — 18 new guard cases (table completeness, three-path settlement parametrized, delete-one-alias mutation counter-proofs for 已完成/已失败/已阻塞, bypass-literal scan with `normalize_status` count >= 6); 170 real-machine `_reap_one_task` maps 已完成→COMPLETE / 已失败→FAILED / 已阻塞→NEED_CLARIFICATION (D-tip same input: SKIP); late settlement COMPLETE/HARVESTED; console badge green in a real browser; full-suite pytest 764 passed on both sides.

## Unreleased — Issue #122: file preview wraps overlong lines in all four shapes, and "open in browser" renders markdown server-side (2026-10-26)

Range: one commit `cb1e10f` — `fix(#122): 文件预览四形态超长行换行 + open 模式 .md 服务端渲染`. Files touched: `mw/console_files.py` (+80), `mw/md_render.py` (new, 197 lines), `tests/test_issue122_md_open.py` (new, 221 lines), `ui/src/index.css`, `ui/src/components/filebrowser/FilePreview.jsx`, `ui/dist/index.html` (rebuilt same commit), plus this CHANGELOG entry.

### Fixed

- **Four overlong-line shapes all wrap, none truncate** — F-a plain-text preview `pre` gets a `.wrap-anywhere` rule (custom because Tailwind 3.4 has no `overflow-wrap: anywhere`; `word-break: normal` explicitly excludes break-all), so space-free 5000-character tokens break at the viewport edge; F-b markdown fenced code blocks switch from horizontal-scroll `overflow-auto` to `pre-wrap` + anywhere; F-c inline `code` and links wrap without bursting the bubble; F-d tables get `table-layout: fixed` with per-cell anywhere wrapping. The red line held: zero `truncate`/ellipsis in the changed CSS — content stays fully visible.
- **"Open in browser" on `.md` now renders** — new stdlib-only `mw/md_render.py` (escape-first invariant: HTML-escape before any markup generation; link whitelist https/mailto/#; images degrade to alt; fence interiors never parsed) serves rendered markdown under `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox` — no script execution, no exfiltration surface. Over-1-MiB or unread files fall back to the plain-text stream; `.txt`/`.log`/`.json` open behavior is unchanged.
- **Verified** — real-browser measurements: 5000-char single-line `.txt` preview `scrollWidth == clientWidth` (0px overflow, 5001 chars intact); counterfactual CSS override overflows 36674px, proving the fix is what holds it; `.md` open returns 200 + sandbox CSP with rendered `<h1>/<table>/<pre>` and zero `<script>`; 45 rendered tags carry zero dangerous attributes. `ui/dist/index.html` rebuilt in the same commit and byte-identical to an independent `vite build`.
- **Known carry-over (registered, not blocking)** — review I-1: a literal NUL-digit-NUL sequence inside a `.md` file can make the code-placeholder restore index past the list and raise `IndexError` (single request 500, process safe, not an XSS). Fix shape options are recorded in REVIEW-E §6; a follow-up card is handed to the coordinator.

## Unreleased — Issues #120 + #121 + #123: the E3 case no longer trusts readdir order, the ph_url dual tables are value-aligned behind a new consistency guard, and `.env.example` finally matches the post-#109 reality (2026-10-26)

Range: one commit `a33c028` — `fix(#120/#121/#123): E3 用例去 readdir 序 + ph_url 双表对齐 + 文档口径同步`. Files touched: `tests/test_issue114_post_terminal_output.py`, `tests/test_i18n_protocol_guard.py`, `mw/i18n/{en,zh-cn}.json`, `mw/reaper.py`, `mw/md_render.py`, `.env.example`, `DEPLOY.md`, `design.md`, `deploy/skills/team-work-chain-advance/SKILL.en.md`, `deploy/skills/worker-message-relay/SKILL.en.md`, plus this CHANGELOG entry.

### Fixed

- **#120 (test hermeticity)** — `test_e3_scan_cap_bounds_work` wrapped `os.walk` in a `_sorted_walk` that sorts deterministically (grabbing the real walk reference to avoid self-recursion), removing the implicit "old files first" assumption that 170's ext4 `dir_index` violated. The cap semantics under test ("bounded stat budget, stop on hit") are unchanged and the product code was not touched — the diff is tests-only. 170 real-machine: the case that was permanently red at D-tip is green, 3 consecutive runs, and the full suite is 764 passed on both sides.
- **#121 F-1 (dual-table drift)** — `console.fallback.ph_url` in `mw/i18n/{en,zh-cn}.json` was still the pre-#109 `http://<host>:8643` while `_LANG_TABLES` had moved to `http://hermes_<name>:8642`; all four tables now carry the same value, and a new guard `test_json_vs_lang_tables_value_consistency` (G1b) closes the old "keys only, values unchecked" gate gap — mutating one side turns it red, and the guard self-checks that a zero-intersection key set would not silently void it.
- **#121 F-3/F-4 (dead code + verdict honesty)** — the unreferenced `active_run`/`base` parameters of `_notify_post_terminal_output` are deleted with their call sites, and the no-initiator path now records `NO_INITIATOR` instead of falsely claiming `NOTIFIED`, with a new assertion that the dedup event still lands.
- **#123 I-1/S-2/S-3 (doc caliber)** — `.env.example` no longer tells operators to register with `http://<host_ip>:GW_PORT` + an `X-API-Key` header (both wrong since #109: registration goes to the container-internal alias `http://hermes_<name>:8642` with a `Bearer` header); the GW_PORT section keeps only a bilingual deprecation note. DEPLOY.md's 8642 symptom line gained a `(pre-#109)` qualifier preserving the historical cause analysis, and design.md's routing example moved to the container-internal address.

## Unreleased — README: a bilingual sponsorship entry (Buy Me a Coffee) (2026-10-26)

Range: one commit `a780678` — `docs(readme): add buymeacoffee sponsorship link (#E-readme-donate)`. Files touched: `README.md` (+4), `README.zh-cn.md` (+4), plus this CHANGELOG entry.

### Added

- **One Support / 支持 section per README, mirrored position** — added right after Acknowledgements / 致谢 on both sides, one line of copy plus `https://buymeacoffee.com/feigbar`, no tracking parameters, no badge images, no third-party JS, zero deletions to existing sections. Coverage note: the three preceding `docs(readme)` commits on the base (`fa1c1bf` / `194dd76` / `02e5222`) are post-D-batch README housekeeping (English-first home, zh filename restore, gufo + halogen-flash-server credits) and are covered by this entry rather than separate ones.

## Unreleased — Issue #117: the #116 answered-badge guards S1b/S1d are upgraded from string-presence checks to render-condition structural assertions with mutation counter-proof (2026-10-26)

Range: one commit `308b6d8` — `test(issue117): 角标守护 S1b/S1d 升级为渲染条件结构断言 + 变异反证`. Files touched: `tests/test_issue116_ui_conflict_consumption.py` (+551/−12), plus this CHANGELOG entry.

### Changed (test quality)

- **Presence assertions could not fail on a broken render condition** — S1b/S1d used to assert only that `"step.answered_by"` / `"t.answered_by"` appear somewhere in the source, so `{step.answered_by && (` rewritten to `{false && (` (badge never renders, string still present) stayed green. The guards now mask comments/strings/template-literals/regex literals with a pure-stdlib JS/JSX masker (1:1 length-preserving, braces kept as structural anchors), locate the badge call-site anchor `t('tboard.answered_badge', …)`, walk brace-pairing outward to the render-guard frame that structurally wraps it, and assert the guard condition actually references `answered_by` — not a comment, not another field. The fallback surface is i18n-sentinel-expanded before analysis (raw template braces are unbalanced until sentinel values close them; 148/148 balanced after expansion).
- **Mutation counter-proof (issue-mandated, new S1m ×2)** — mutating the render condition to a `false` constant, or to read the wrong field, must turn the same judgment red. Mutation points are derived from the structural location (not hardcoded source literals), so they still aim at the real guard after source edits, and are applied only to in-memory copies — product files are never touched.
- **Verified** — targeted 10 passed (was 8); disk-level counter-proof: real edits `{step.answered_by && (` → `{false && (` and `${t.answered_by?` → `${false?` turned S1b/S1d red as required, restore returned 10 green, product md5 unchanged; full-suite pytest 723 passed (baseline 721 + 2 new, zero regression); 170 real-machine re-run 10/10 passed.

## Unreleased — Issue #114: the reaper alerts main when a settled terminal card's workspace shows new output (post-terminal output alert) (2026-10-26)

Range: one commit `8d8f3b9` — `feat(issue114): reaper 终态卡新产出告警 (post-terminal output alert)`. Files touched: `mw/reaper.py` (+163), `tests/test_issue114_post_terminal_output.py` (+320, new), plus this CHANGELOG entry.

### Added

- **Hook 3.7 `_notify_post_terminal_output` closes the second half of #104 expectation 3** — after a card reaches a terminal state (`HARVESTED` / `CANCELLED` / `ARCHIVED`), if its workspace still shows new output (file writes / git commits touching the work tree), the reaper notifies main: a detection layer behind the P1-1 execution-point guard, catching "orphan construction residue".
- **False-positive controls** — detection window `MW_REAPER_POST_TERMINAL_WINDOW_MIN` (default 30 min, only cards settled inside the window; illegal values fall back to default); grace period `MW_REAPER_POST_TERMINAL_GRACE_S` (default 120 s, so settlement's own transcript/report writes are not counted as new output); alert-once per task via an `mw_events` `worker.state` (`reason=post_terminal_output`) dedup stamp — no new column, event written before the knock so a failed delivery cannot spam every 15 s and a missed alert is still traceable.
- **Switch and bounded scan** — `MW_REAPER_POST_TERMINAL_ALERT` default on (explicit `0/false/off/no` off); scan skips `.git` / `node_modules` / `__pycache__` / `.venv` / `venv` and caps at `MW_REAPER_POST_TERMINAL_SCAN_MAX` (default 5000) stopping on hit, so the sweep never walks a large repo unbounded; per-card detection failure degrades to a SKIP result inside a try/except and never blocks the rest of `reap_once`.
- **Verified** — 13 new cases (trigger on all three terminal states, second-round dedup SKIP, no-output/grace/non-terminal/out-of-window/no-workspace false-positive controls, switch + env fallback, missing-directory robustness, `reap_once` integration, cap); 170 real-machine smoke 4/4 (alert fired + event landed + one knock / second round SKIP / switch-off zero alerts / grace no false positive); full-suite pytest 736 passed at `8d8f3b9` (baseline 723 + 13, zero regression).
- **Known carry-over (registered, not blocking)** — `test_e3_scan_cap_bounds_work` is red on 170: the case's "new output beyond cap" premise depends on `readdir` order (170 ext4 dir_index places the new file first; local overlayfs places it eleventh), and the product cap semantics ("stat budget + stop on hit") were proven correct by counterfactual probes (s6 §3.1). A test-only fix-up with dual-side re-run is handed to the coordinator.

## Unreleased — Issue #109: compose templates no longer publish the gateway's 8642 host port, and console worker creation no longer asks for a port (2026-10-26)

Range: two commits — `9d0e4c8` (`fix(issue109): compose 去 8642 宿主端口映射, 控制台创建 Worker 免填端口`) plus the s5 deploy-reflow fix `93c1dc5` (`fix(issue109): verify.sh gateway 探测 Authorization 前缀改回 Bearer (s5 部署回流)`). Files touched: `deploy/main.compose.yaml`, `deploy/worker.compose.yaml`, `mw/console.py`, `mw/console_ui.py`, `mw/i18n/{en,zh-cn}.json`, `ui/src/views/WorkersView.jsx`, `ui/src/i18n/locales/{en,zh-cn}.json`, `ui/dist/index.html`, `ui/tests/test_issue58_tokens.mjs`, `deploy/verify.sh`, `deploy/install.sh`, `DEPLOY.md`, `tests/test_issue109_verify_gateway_auth_header.py` (new), plus this CHANGELOG entry.

### Changed

- **Four layers drop the redundant host mapping in sync** — since #82 all main↔worker traffic goes over the container-internal alias `http://hermes_<name>:8642` on the `simple-dot` network, so the host port mapping was dead weight that was in fact *more* fragile through docker-proxy under memory pressure. `main.compose.yaml` drops `"${GW_PORT:-8642}:8642"` (console `:8700` / webui `:8787` browser entries untouched); `worker.compose.yaml` drops the `WORKER_PORT` mapping and docs, adds the `simple-dot` external network with a unique alias, and renames the service `hermes` → `hermes_${WORKER_NAME}` (joining the shared network auto-registers the service name as an alias — a shared `hermes` alias is exactly the #82 misrouting trap); `mw/console.py` `COMPOSE_TEMPLATE` loses its ports line and `api_compose_generate` relaxes `port` to optional (a passed port is ignored, so old frontends keep working); both UI surfaces drop the port input (React `WorkersView` 3 columns → 2, fallback `cPort` row removed) with placeholders switched to the container-internal form, dead i18n entries `lbl_port`/`ph_port` removed on both sides, and the React top-level key anchor lowered 653→652 per the INV-09 precedent for legitimate deletions.
- **verify.sh gateway check rewritten to the real communication path** — host-side `curl :8642` replaced by `docker exec` in-container `python3 urllib` POST `/api/sessions` (same shape as how workers actually reach the gateway), 5-retry cadence unchanged; `install.sh` wrap-up and `DEPLOY.md` §2/§2.5.3/§3/§4.5 port wording all synced to the container-internal caliber.

### Fixed (s5 deploy reflow)

- **The rewritten probe shipped a placeholder as the auth prefix** — `9d0e4c8` copied the Authorization prefix as a three-asterisk placeholder instead of `Bearer `, so the header went out as literal placeholder-plus-key and the gateway answered 401 `gateway_auth_failed` on every run: the deployment was actually fine but verify.sh's gateway item was permanently red — a false failure. This was the *second* regression of the same trap (`b732423`, 2026-10-03, whose commit message even recorded the lesson "verify.sh self-test must not use placeholders"). Real-machine three-variant comparison pinned the shape: `"Bearer " + key` → 201, raw key → 401, `"bearer "` lowercase → 401 — the prefix must be `Bearer` + one ASCII space, case-sensitive.
- **Seven static guards now pin it** (`tests/test_issue109_verify_gateway_auth_header.py`): G1 byte-exact prefix + sha256 anchor (`35e5a963…cdcf`, 7 bytes); G2 no stray asterisks in the prefix; G3 the key must still come from the container env (no hardcoded key); G4 case sensitivity (`bearer`/`BEARER` judged red); G5 mutation counter-proof (5 variants all red); G6 repo-wide tracked scan for asterisk-placeholder residue with a 4-class whitelist (registry key masking / DEPLOY.md sample password / echo-masking artifact note / test dummy fixtures), the guard file itself excluded precisely (170 → 169 files scanned); G7 no line containing `Authorization` may carry an asterisk placeholder. The s5fix also aligned the orchestration skill's English doc placeholder to `<token>`, matching its Chinese twin.
- **Verified** — 170 real-machine verify.sh **13/13 twice** (`VERIFY_RC=0`, s6b), gateway segment green with the 7-byte `Bearer ` prefix landing byte-identical; host `8642` absent from `docker port`/`ss` (no rollback); new guards 7 passed on the real machine.

## Unreleased — Issue #101: the chain-advance skill's commit/push wording is synced bilingually per the user's ruling (2026-10-26)

Range: one commit `45601d7` — `docs(issue101): advance skill commit/push 措辞按用户裁决双语同步 (#101)`. Files touched: `deploy/skills/team-work-chain-advance/SKILL.en.md`, `deploy/skills/team-work-chain-advance/SKILL.zh-cn.md`, plus this CHANGELOG entry.

### Changed

- **The user's 2026-10-25 ruling, written into the skill** — a local commit is part of the task chain and the backtrace chain, so it is squarely inside the skill's duty and must land; a push is outside the task chain and is the user's own choice (the user may specify it via their own soul/skill/prompt) — the skill does not do it on the user's behalf. The protocol-patch discipline line changed from "并 commit+push" to "commit 落地 (本地 commit 必须落地)" with the push-not-a-duty clause stated explicitly, on both language sides.
- **The push-reconciliation paragraph rewritten in the same direction** — push is not the skill's duty, but if anyone *reports* "pushed", the reconciliation recipe runs exactly as before (recipe and all three false-positive patterns preserved); if nobody reports a push, no reconciliation is needed, and push must never be written into acceptance criteria — a worker that only commits without pushing is behaving correctly, not defecting.
- **Cross-skill contradiction eliminated** — the new wording points the same way as orchestration L4b "push 归协调方" / "push belongs to the coordinator" (zh:319 / en:576). Verified on the real machine word-for-word: advance zh:96 / en:105–108, orchestration zh:319 / en:576 all match, zero residue of the old "并 commit+push" phrasing; `i18n_docs_sync --strict` rc=0; the commit itself practiced the ruling — committed, not pushed.

## Unreleased — Issue #108: README slimmed to intro + quick links, Chinese README added with a top-of-page switch (2026-10-25)

Range: one commit. Files touched: `README.md` (rewritten slim), `README.zh-cn.md` (new), plus this CHANGELOG entry.

### Changed

- **`README.md` slimmed from 313 to 61 lines** — the page now keeps only the one-paragraph project intro and the 1A design-philosophy bullets; the detailed content that used to live inline (architecture diagram, role model, directory-layout table, verified-capabilities list, the full i18n walkthrough, management-boundary rationale) is replaced by a "Where to read more" link table pointing at `DEPLOY.md`, `design.md` (§10 i18n, §11 Team Work), the SOUL pairs and `CHANGELOG.md`. No information was deleted from the repo — every removed section already has an authoritative home in `design.md` / `DEPLOY.md`.
- **`README.zh-cn.md` added** — a Simplified-Chinese counterpart mirroring the slim English README (intro, philosophy, quick start, link table), so Chinese readers get the same overview without wading through English prose.
- **Bidirectional language switch at the top of both pages** — line 3 of each README is a prominent `English | 中文` / `中文 | English` link pair, satisfying the issue's "显眼位置切换入口" requirement; every link in both link tables resolves to a tracked file.
- **i18n gate unchanged and green** — `README.zh-cn.md` sits at the repo root, outside the F1 root-doc set (`README.md` / `DEPLOY.md` / `design.md` must stay CJK=0) and outside `docs/` (F3' still exactly the two soul files), so `tools/i18n_docs_sync.py --strict` passes with no allowlist edits.

## Unreleased — Issue #116: both console UI surfaces now consume `conflict` / `answered_by`, so a late answer is visible to the operator (2026-10-25)

Range: one commit. Files touched: `mw/console.py` (`api_team_work_get` batch injection), `mw/console_ui.py` (fallback surface: `submitAnswer` conflict branch + dialog answered line + two new i18n sentinel keys), `mw/i18n/{en,zh-cn}.json`, `ui/src/views/TasksBoardView.jsx` (React surface: `submitAnswer` return-value consumption + step-row answered badge), `ui/src/i18n/locales/{en,zh-cn}.json`, `ui/tests/test_issue58_tokens.mjs` (key-count anchors 651→653 / 121→123 per the INV-09 precedent), `ui/dist/index.html` (rebuilt same commit), `tests/test_issue116_ui_conflict_consumption.py` (new), plus this CHANGELOG entry.

### Fixed

- **The React surface no longer discards the resume response** — `submitAnswer` used to `await api.resume(...)` and toast success unconditionally, so a late answer looked identical to a blind injection. It now captures the response and, when `conflict` is non-empty, shows the warning toast `tboard.reply_conflict` ("Reply accepted, but ⚠ 已被 X 于 HH:MM 答复 …") instead of the plain success copy.
- **The fallback surface no longer closes the dialog on a conflicting answer** — `submitAnswer` now checks `d.conflict` before `close()`: the notice goes into the existing `dlgErr` slot and the dialog stays open with the submit button restored, so the operator sees the conflict before deciding. The answer itself is still queued (backend semantics unchanged); only the UI silence is fixed.
- **"Answered by X at HH:MM" is now rendered on both surfaces** — the React step-row shows an amber badge (`tboard.answered_badge`) from `answered_by`/`answered_at_hhmm`, and the fallback dialog shows a yellow answered line. Because the run-detail endpoint previously carried no per-card answer facts, `api_team_work_get` now batch-injects `answered_by`/`answered_at`/`answered_at_hhmm` per card from `task_resume_requests` (one `IN` query, latest row per card, fail-open) — the same source `api_task_detail` already used per-card.
- **i18n parity holds** — two new `console.fallback.*` keys (answered line + conflict notice) added to `mw/i18n` en/zh-cn and `_LANG_TABLES` in the same commit (G1 key-set parity, no leftover `⟪⟫` sentinels in either language), and two new `tboard.*` keys added to both React locales.

## Unreleased — Issue #115: `/api/resume` no longer trusts a client-claimed `actor`, so answer attribution cannot be forged (2026-10-25)

Range: one commit. Files touched: `mw/console.py` (route layer + `api_resume`), `mw/ledger.py` (`record_resume_answer`), `tests/test_issue115_actor_hardening.py` (new), plus this CHANGELOG entry.

### Fixed

- **A client could sign an answer as `main`** — the `/api/resume` route passed `actor=payload.get("actor") or "operator"` straight into the ledger, so a body of `{"actor": "main"}` landed verbatim in all three audit surfaces: `task_resume_requests.actor`, the `[resume-answer] actor=…` line in `harvest_log`, and the `resume.answered` event payload. The whole point of the `actor` column (issue #104 expectation 2 — telling a coordinator's answer from an operator's) was defeated by one unvalidated input parameter. The route is the operator channel, so the effective actor is now fixed server-side to `"operator"`; `main` answers through its own CLI entry (`mw/cli.py::cmd_resume` → `actor="main"`) and is unaffected. The claimed value is kept only as an audit trace (`actor_claimed`), never as attribution.
- **A spoofed identity could also silence the conflict hint** — the conflict check is `prior["actor"] != actor`, so two answers that both claim the same actor see no conflict, re-creating the very blindness #104 was filed for. This is why a value-domain whitelist (`actor in ("main", "operator")`) was rejected as the fix: `"main"` is inside such a whitelist, so the bypass survives it. With the effective actor pinned server-side, a self-claimed `main` is always `operator` and therefore always differs from a prior `main` answer, so the conflict still reports. Guarded by case H4.
- **The normalization itself is auditable** — when a claimed value differs from the effective one, the trail reads `[resume-answer] actor=operator at=HH:MM (claimed=main normalized)` and the event carries `actor_claimed`. Without it, "who impersonated whom" would exist only in transient request logs. The claimed string is truncated to 32 chars so a long body cannot flood the 2000-char `harvest_log`. Honest answers (no `actor` field, or a claim equal to the effective value) leave no normalization trace.

## Unreleased — Issue #113: the eight pre-existing pytest failures are attributed and cleared; a missing transcript CLI no longer breaks settlement, and the host-side deploy fingerprint works on macOS (2026-10-25)

Range: one commit on `main`. Files touched: `mw/harvest.py`, `deploy/lib/hash.sh` (new), `deploy/install.sh`, `deploy/verify.sh`, `tests/test_harvest_b_rewrite_guard.py` (untouched — the fix is in the code under test), `tests/test_f8_reaper_harvest_fixes.py`, `tests/test_issue51_avatar_file.py`, `tests/test_issue113_transcript_degrade.py` (new), plus this CHANGELOG entry. Full-suite pytest failure count goes from 8 to 0.

### Fixed

- **A missing `hermes` CLI aborted the whole settlement** — `harvest.export_transcript()` shells out to `hermes sessions export` to collect the worker's transcript. When the binary is not on `PATH`, `subprocess.run` raises `FileNotFoundError` (an `OSError` subclass) and the exception propagated straight out of `harvest()`, so the card could never be settled: the reaper recorded one `verdict=ERROR` and the card stayed unsettled forever. This is production-reachable, not a test-environment artifact — the container installs `hermes` but does not necessarily put it on `PATH` (`main_entry.sh` only exports `PYTHONPATH`), and `MW_EXPORT_PREFIX` pointing at an unloaded image behaves the same way. The existing degradation path already tolerates a missing transcript (`doc=None` ⇒ "export failed/incomplete (degraded to physical verification only)"), so "the command does not exist" now takes that same path instead of exploding. Both the direct and the `docker exec` prefix forms are covered.
- **The host-side deploy fingerprint was broken on macOS** — `deploy/verify.sh` computed the SOUL/SKILL fingerprint with a bare `sha256sum`. macOS/BSD ships `shasum -a 256` and has no `sha256sum`, so on a documented deploy platform the host-side fingerprint silently came out as an *empty string* and verify.sh reported a bogus "SOUL/SKILL content stale" mismatch. The cross-platform `hash_of` helper (md5/sha256, `sha256sum`/`shasum` probing, `-` for stdin) moved to `deploy/lib/hash.sh` and is now sourced by `install.sh` and `verify.sh`; the host side uses it while the container side keeps its native `sha256sum` (the container is Linux). Measured: the host fingerprint is now byte-identical under a Linux `PATH` and under a macOS-shaped `PATH` (only `shasum` present), where the old form produced an empty string.
- **The `hash_of` helper was deleted while two tests still guarded it** — commit `f908906` (issue #53 C) trimmed `deploy/install.sh` and removed the inline `hash_of()` together with the §8.5 code-version check. The two tests that exercised it (`test_r6_hash_of_via_bash`, `test_r6_install_sh_section_85_uses_hash_of`) therefore failed forever, guarding code that no longer existed. They now target the live helper in `deploy/lib/hash.sh` and assert the contract that actually matters today: the helper exists with its `-` stdin branch, both deploy scripts source it, and no host-side bare `sha256sum` remains.

### Fixed (test quality)

- **Two reaper liveness tests were asserting a dead code path** — `test_f8_1_*` read `sse_bridge._worker_live` directly, but SSE-FIX-F3 (`0fa0f40`) replaced that read with the console endpoint `/api/workers/live` (`reaper._query_console_liveness`), because the reaper and the SSE bridge are separate processes and do not share memory. The old test's stub was missing `ledger.path`, so the "suppressed" case always ended in `verdict=ERROR` instead of `SUPPRESSED_ALIVE` (permanently red), while the "not suppressed" cases were vacuously green (the console is unreachable in CI, so `fail-open` never suppressed — the assertion no longer tested anything). The group now mocks the real seam and asserts the real outcome in both directions: suppressed ⇒ `SUPPRESSED_ALIVE` and *no* message sent; not suppressed ⇒ `NOTIFIED` and exactly one message sent. A new case pins `thinking==True` as an independent liveness gate (SSE-FIX-F3 changed the rule from "thinking AND window" to "thinking OR window"; the thinking gate is sticky across a long tool call, so downgrading it to AND would make the reaper mistake a running worker for a hung one).
- **The whole `test_issue51_avatar_file.py` file could never fail under pytest** — its `PASS()` helper only appended to a module-level `FAIL_LIST` and never raised, and nothing checks that list outside the file's `__main__` block. All 23 tests in it were therefore hollow greens: a wrong hash value, a wrong avatar, or a wrong MIME verdict was invisible to pytest. The R-6 pair (the two tests this issue is about) now use real `assert` statements.

### Added

- `tests/test_issue113_transcript_degrade.py` — 5 cases pinning the transcript degradation contract: missing CLI ⇒ `None` (no raise); settlement still completes and the contract row reaches `HARVESTED`; a working CLI is still parsed; a non-zero exit still yields `None`; the `docker` prefix form degrades the same way.

## Unreleased — Relay send-timeout raised above the gateway turn-lease wait (duplicate-report storm root cause) (2026-10-25)

Range: one commit on `main`, `mw/reaper.py` only. Validated end-to-end on the 170 test machine with an A/B harness (long-turn lease holder + injected HARVESTED contract + 20s ledger/transcript sampling); repro recipes and both run logs are archived in the task-workspace docs.

### Fixed

- **The relay POST timed out *before* the gateway had even started the turn** — `POST /api/sessions/{sid}/chat` is fully synchronous (handler waits for the session turn lease, runs the whole turn, only then responds), and the lease wait is hardcoded at 1800s (`agent/turn_facade_lease.py`; production `agent.log` measures these POSTs blocked at p50=1801s / p90=1859s / max=2040s). The relay's 600s send timeout therefore almost always fired first, recording a delivery as *failed* (`notify_attempts` +1, backoff, retry) while the server still held the queued request and delivered it normally once the lease freed — a client-side timeout does not cancel the delivery (170 probe, measured). When the retry landed after the first copy had already been processed, the same report woke `main` a second time: the residual channel behind the #83 forward storm. Default `MW_RELAY_SEND_TIMEOUT_S` 600 → 2400 (1800 lease + 600 turn margin) so the first send waits through the lease and gets a deterministic 200 → `confirm_delivered`; the misjudge→retry path loses its trigger. E2E A/B under an identical 750s lease hold: old code flipped `notify_attempts` 0→1 (`"timed out"`) while the message had in fact been delivered; new code keeps `notify_attempts=0`, confirms delivery, and the report appears exactly once in the transcript.
- **The single-flight inflight window was shorter than the send it was guarding** — `claim_for_relay`'s 660s default is well under the new 2400s send timeout, so a second thread could claim while the first was still blocked in its POST — exactly the concurrent-inflight window #83 item B closed. The call site now derives `inflight_s` from `_relay_send_timeout_s() + 60`, keeping "backoff cap ≥ inflight ≥ send timeout" true under any env override (the backoff cap already derives from the same anchor).

Delivery *latency* itself is gateway lease serialization and cannot be removed from mw; this fix eliminates the misjudged failures and the duplicate processing, not the wait.

## Unreleased — s6fix: terminal-state guards at every resume execution point, plus review follow-ups P2-1/3/4/5/7 (2026-10-25)

Range: one commit on the working branch (`fix(s6fix): resume 执行点终态守卫 + P2 措辞/健壮性收口`, message verbatim in the repo). Files touched: `mw/reaper.py`, `mw/pipeline.py`, `mw/cli.py`, `mw/ledger.py`, `tests/test_issue104_resume_unified.py` (+3 cases), `tests/test_issue105_106.py` (+1 case). Chain: `docs/REVIEW-BATCH-20261025.md` (verdict CHANGES_REQUESTED, P1-1 + P2-1/3/4/5/7) → fix (see `FIX-NOTES-S6FIX.md` in the task workspace). P2-2/P2-6 untouched per coordinator ruling (P2-6 → Gitea #114 as a separate card).

### Fixed

- **#104 P1-1 (the 409 guard only guarded the front door)** — `api_resume` rejected terminal cards, but the three execution points did not: the reaper's resume-consumption loop called `resume_task` on any queued request without re-checking the card, `pipeline.resume_task` itself never consulted `is_terminal` before the injection POST, and `mw resume` (the CLI, main's channel) enqueued without a check either. An answer queued while the card was alive would therefore still wake a worker session after the card was ARCHIVED/CANCELLED/HARVESTED — the exact ownerless-construction injection shape of incident `t_a8052713`, now reachable through both answer channels since the console path became an async queue. All three points now refuse on `Ledger.is_terminal`, and every refusal leaves an auditable trace (harvest_log trail and/or a `worker.state` event carrying `resume_rejected: "terminal"`): the reaper consumer closes the queue row through the regular `finish_resume(ok=False, error="terminal card")` — no orphan PROCESSING row; `resume_task` returns `ok=False, state_reset="refused:terminal"` before any POST; `cmd_resume` exits non-zero without enqueuing. Guarded by cases E1/E2/E3; removing any single guard turns its case red (mutation-verified), restoring it turns green.
- **P2-1 (hardcoded socket timeout silently broke the backoff-cap invariant)** — the relay POST used `urlopen(rq, timeout=600)` while the backoff cap derives from `_relay_send_timeout_s()` (env-overridable). Lowering `MW_RELAY_SEND_TIMEOUT_S` below 600 made the cap smaller than the actual socket timeout, silently re-opening the concurrent-send window that cap ≥ timeout exists to close. The POST now takes its timeout from the same `_relay_send_timeout_s()` call, so env and invariant move together.
- **P2-7 (one bad row aborted the whole archive-sync batch)** — `sync_archived_contract_states` looped without a per-row guard, so when two concurrent list reads raced on the same card, the loser's `update_worker_state` raised `ValueError` (ARCHIVED→ARCHIVED is illegal) and killed the remaining cards in the batch. The loop body is now wrapped in `try/except (ValueError, PermissionError)` with per-row skip (partially-successful semantics, unchanged return shape). Guarded by case B7; mutation-verified.

### Documentation

- **P2-3** — `relay_precheck`'s docstring claimed it also drops HARVESTED cards; the code (correctly) drops only ARCHIVED/CANCELLED, because a harvested-but-undelivered card is precisely what the #103 F4 knock must deliver. Doc corrected to match code.
- **P2-4** — two stale annotations: `_send_to_main`'s backoff note still said "max 300s = 5min" (the cap has been `RELAY_BACKOFF_CAP_S`, default 660s, since #83 item B), and `claim_for_relay`'s docstring still said a failed send immediately clears `last_notified_at=NULL` (#83 item B changed this to keep the flag set). Both corrected.
- **P2-5** — `update_worker_state`'s `event_extra` parameter had zero callers repo-wide (the answered_by trace actually flows through `_log_resume_state_reset`'s own `emit_event`); dead parameter and its fictional-usage docstring removed.

## Unreleased — Fix-up for the #83 / #107 regression gaps the targeted retest handed back (2026-10-25)

Range: one commit on the working branch (`fix(s5fix): A1 真并发窗口守护 + provenance 幂等判定放宽`, message verbatim in the repo). Files touched: `tools/evidence_provenance.py`, `tests/test_issue103_83_relay_dedup.py`, `tests/test_issue107_provenance.py`, plus `CHANGELOG.md`. No runtime middleware code touched (`mw/` untouched). Chain: `docs/TEST-REPORT-BATCH-20261025.md` §8 D-1 / D-2 → fix (see `FIX-NOTES-S5FIX.md` in the task workspace).

### Fixed

- **#83 (D-1, Gitea #111: the concurrency guard had no regression net)** — `test_a1_concurrent_sends_only_one_posts` mocked `urlopen` with an immediate return, so the first sender finished the whole claim → precheck → POST → confirm chain before the second thread even started; the second thread was therefore stopped by the *precheck*, not by the send-layer single-flight claim, and the case stayed green even with the claim removed (retest mutation M13 measured `mut_exit=0`). The case now opens a real race window: a `threading.Barrier` makes both senders enter the delivery window at the same instant, the mocked gateway blocks until the test releases it (so the first sender has not yet received its 200 and `notify_confirmed_at` is still unset), and the assertion is on the number of POSTs *inside the window* — one. Removing the single-flight claim now lets the second sender's precheck pass and the case turns red; restoring it turns green again.
- **#107 (D-2, Gitea #112: a hand-written comment on the first line produced a double header)** — the idempotence check only accepted a first line strictly matching `PROVENANCE_RE`, so a file starting with a hand-written comment (`# already a header`) was judged header-less and got a second provenance line stamped on top, leaving the file with two `#` header lines. The first line is now classified into four kinds: a strict provenance header (skip, idempotent), an approximate header — a `#` line carrying provenance field markers such as `host=`/`head=` (that line is **replaced**, so the stale host/sha/timestamp is refreshed and no double header appears), a hand-written comment (treated as "already has a header" and left byte-for-byte untouched, with a `NOTE` on stderr instead of a silent skip), and a non-comment first line (stamped as before). `--force` explicitly re-enables stamping on top of a hand-written comment.

## Unreleased — Issue #107: evidence files carry an auto-injected provenance header so independent re-verification no longer needs per-file md5 collisions against the source machine (2026-10-25)

Range: one commit on the working branch (`feat(issue107): 证据文件 provenance 头采集脚本 + 单测 + 用法文档`, message verbatim in the repo). Files touched: new `tools/evidence_provenance.py` + `tools/README-evidence-provenance.md` + the regression net `tests/test_issue107_provenance.py` (7 cases). No runtime code touched. Chain: `docs-source/issue-batch-20261025/ISSUE-107.md` → implement → targeted retest (see `FIX-NOTES-107.md` in the task workspace).

### Added

- **#107 (evidence provenance header)** — 25 of 58 acceptance evidence files carried no host/path marker and 42 of 43 no HEAD sha, so downstream re-verification had to md5-collide every file against the source machine to tell real-device output from local forgery. Team convention: every acceptance/test evidence file's first line is now `# host=<hostname> head=<commit sha> cmd=<full command line> ts=<UTC ISO>`, injected by `tools/evidence_provenance.py` rather than by hand. The script collects `socket.gethostname()`, `git rev-parse HEAD` (degrades to `-` outside a git checkout), the command line (`--cmd`, defaulting to `shlex.join(sys.argv)`), and a UTC second-precision timestamp; it stamps a batch of files in place, is idempotent (a first line already matching the header pattern is skipped, never duplicated), and `--print` emits a single header for pipeline composition. The `cmd` field is the greedily-matched middle group anchored on a trailing ` ts=`, so commands containing spaces or `=` survive a round-trip.

## Unreleased — Issues #105 + #106: volume seeding no longer trusts a host-side empty listing, and an archived kanban card no longer leaves its contract row stuck at PENDING (2026-10-25)

Range: one commit on the working branch (`fix(issue105+106): 播种判空走 root helper + archived 卡契约联动推终态`, message verbatim in the repo). Files touched: `deploy/install.sh`, `mw/ledger.py`, `mw/console.py`, plus the new regression net `tests/test_issue105_106.py` (12 cases). Chain: `docs-source/issue-batch-20261025/ISSUE-105.md` + `ISSUE-106.md` → implement → targeted retest (see `FIX-NOTES-105-106.md` in the task workspace).

### Fixed

- **#105 (a 0700 host directory made seeding silently skip, and the board "reset")** — `_seed_main_volume()` judged emptiness with a host-user `ls -A data/main`; when `data/main` is owned by container uid 10000 at mode 0700 (the 170 legacy shape) the listing comes back EACCES-empty, the deploy printed "data/main is empty — nothing to seed", and the recreated container mounted a volume without `kanban.db` (the data was never lost — the authority sat in the host copy the script had just declared empty). Emptiness is now judged inside a root helper container of the deploy image (`_seed_helper` → `docker run --rm --entrypoint sh --user 0:0`, the same pattern §7b already used), a failing listing is a hard error instead of a silent "empty", and both the seed and top-up paths end with `chown -R 10000:10000` plus `chmod -R u+rwX` so the worker uid can actually write what was seeded (the s4e2e side observation).
- **#105 (§7b: a volume with content but no `kanban.db` was kept as-is)** — "non-empty" was treated as "seeded". A volume that has content yet misses a key file (exactly the post-incident volume) is now topped up file-by-file from the source without overwriting anything that already exists, with the same ownership/permissions pass.
- **#106 (option (a): an archived card's contract row stayed PENDING forever)** — a card created outside a Team Work run and archived directly via `hermes kanban archive` never enters the settlement plane (reaper T3 only settles the current card of an ACTIVE run), so its `harvest_contracts` row sat at PENDING, blocked launches with a false `orphan_pending_cards` 409, and polluted the "no PENDING orphans" health probe (170: 2 rows from s4 test cards). At the two moments the orphan face is consumed — the launch guard and the task-list probe read (`api_tasks`) — `_reconcile_archived_orphans()` now intersects orphan contract rows with kanban `status='archived'` and pushes each hit through the state machine (`Ledger.sync_archived_contract_states` → `update_worker_state`, `actor=operator`, with a `worker.state` event and an audit trail — never a bare UPDATE). Every failure mode (unreadable kanban.db, missing row, already terminal, CANCELLED which the state machine forbids as a source) degrades to a no-op, and a real (unarchived) orphan still blocks the launch exactly as before.

## Unreleased — Issue #104: console resume joins the ledger queue with actor attribution, and archiving now actually stops the worker (2026-10-25)

Range: one commit on the working branch — `fix(issue104+103+83): 澄清回路统一 + relay 投递层单飞去重/失败留痕/F4 补档`. Files touched: `mw/console.py`, `mw/pipeline.py`, `mw/ledger.py`, `mw/cli.py`, plus the new regression net `tests/test_issue104_resume_unified.py` (12 cases). Chain: `docs-source/issue-batch-20261025/ISSUE-104.md` → implement → targeted retest (see `FIX-NOTES-104-103-83.md` in the task workspace).

### Fixed

- **Root cause 1 (console resume bypassed the ledger)** — `api_resume()` spawned a background thread straight into `pipeline.resume_task()` without writing `task_resume_requests`, so the reaper never saw an operator's answer. Console resume now enqueues through `Ledger.enqueue_resume()` onto the same queue the CLI uses; `task_resume_requests` gained an `actor` column (`main`/`operator`, added via the established `PRAGMA table_info` + `ALTER TABLE` migration pattern, idempotent on old databases).
- **Root cause 2 (silent state-reset)** — when `resume_task()`'s state rewind was rejected, `state_reset='skipped:…'` only lived in the return dict that the console thread discarded. `_log_resume_state_reset()` now lands a `harvest_log` line and a `worker.state` event (with `state_reset` and `answered_by`) on both success and skip paths, through the unified updater rather than a bare UPDATE.
- **Root cause 3 (the two answer channels were blind to each other)** — every answer now stamps a `resume.answered` event plus a `harvest_log` trail carrying the actor; `GET /api/tasks` detail exposes `answered_by`, and a later answer from the other channel receives an explicit conflict hint instead of a blind injection (HTTP 409 on terminal cards).
- **Root cause 4 (ARCHIVED did not stop the worker)** — `api_task_archive()` now calls `_kill_spawn_bg()` before `set_terminal(…, "ARCHIVED")`, matching the cancel path (field case t_a8052713: the worker kept committing 3+ minutes after archive). `api_resume` additionally gained an `is_terminal()` guard.

## Unreleased — Issues #83 + #103: one in-flight delivery per report, failures leave a trace, and a harvested-but-undelivered card can no longer sit silent (2026-10-25)

Range: same commit as issue #104 above — `fix(issue104+103+83): 澄清回路统一 + relay 投递层单飞去重/失败留痕/F4 补档`. Files touched: `mw/reaper.py`, `mw/ledger.py`, plus the new regression net `tests/test_issue103_83_relay_dedup.py` (16 cases). Chain: `ISSUE-83.md` plan A + `ISSUE-103.md` → implement → targeted retest (see `FIX-NOTES-104-103-83.md`).

### Fixed

- **#83 A (duplicate concurrent POSTs)** — the send-rights claim moved from the scan layer into `_send_to_main` itself: before spawning the sender thread the call atomically `claim_for_relay()`s the card (one UPDATE, `待发送→等待确认`), and the sender re-reads the card (`Ledger.relay_precheck()`) immediately before the real POST — `notify_confirmed_at` already set by a winning thread, terminal card, or a run that is no longer `ACTIVE` ⇒ abandon without POSTing. The scan-layer claim was removed because the double claim made the send-layer claim always lose.
- **#83 B (backoff cap below send timeout)** — the failure backoff cap was raised from 300 s to send-timeout + 60 s (default 660 s, `MW_RELAY_BACKOFF_CAP_S` may only move it more conservative), so a backoff window can never expire while the previous sender still blocks inside its 600 s timeout; and the failure path no longer clears `last_notified_at` — keeping it set pins the in-flight state across the whole window (clearing it was the direct cause of 6 duplicate forwards in 20 minutes).
- **#103 C (failures left no trace)** — `harvest_contracts` gained `notify_last_failed_at` / `notify_last_error` (same migration pattern); every failed delivery records them plus a `relay.notify_failed` event, and `confirm_delivered` clears the trace. The dead-letter message and the new F4 knock text carry the failure count and reason so main can tell "never sent" from "repeatedly failed".
- **#103 D (F4 blank window)** — a run that is `ACTIVE` with a NULL slot whose cards are `HARVESTED` yet `notify_confirmed_at IS NULL` (settled, undelivered) previously got no knock at all (field case: 23+ minutes of silence). `_notify_undelivered_harvest()` now knocks after the stuck window, sharing the `last_null_slot_knock_at` stamp so at most one knock fires per window.

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