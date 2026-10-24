---
name: team-work-chain-advance
description: "Advance Team Work chains: complete, add cards, advance next card."
version: 2.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [Agent, Matrix, Kanban, TeamWork]
---

# Team Work Chain Advance (main agent side)

## When to Use

- You receive an mw-forwarded "task settled: verdict=COMPLETE" message and need to advance to the next card
- The chain needs new cards added mid-flow (review rejected → fix card / re-review card)
- The second and later cards need to actually spawn a worker after launch

`POST /api/team-work/launch` only auto-dispatches the first card. Every subsequent card, even with
`hermes kanban claim`, won't spawn a worker — spawn must be triggered explicitly by main via the `advance`
endpoint. It is the complete sequence for advancing the chain after mw's settlement report. **v2.0 change
(2026-10-04)**: advance all through `POST /api/team-work/<run_id>/advance`; no more handwritten manifest +
`__spawn-bg` (that's the legacy path, it bypasses run-attribution validation and project_workspace
inheritance, and was the cause of the last round of supplemental-card deadlocks).

## Advance Sequence (each downstream card)

Prereq: `export PATH=/opt/hermes/.venv/bin:$PATH` (venv contains hermes and mw).

1. **Settle the previous card**: `hermes kanban complete <task_id> --result "what was done + absolute path
   to deliverable"` — an empty complete is rejected ("empty completion is not evidence"); you must pass
   `--result` / `--summary`.
2. **Claim the card**: `hermes kanban claim <next_task_id>`.
3. **Advance dispatch**:
   ```bash
   curl -s -X POST http://127.0.0.1:8700/api/team-work/<run_id>/advance \
     -H 'Content-Type: application/json' \
     -d '{"task_id":"<next_task_id>","target_profile":"<worker-name>",
          "title":"<card title>","required_files":["<deliverable>"],"timeout_seconds":1800}'
   ```
   advance: verify previous card is settled (`current_card_id` is NULL) → occupy slot → dispatch worker.
   Calling advance before the previous card settles → **409 reject** (serialization iron rule).
   `project_workspace` is optional — advance auto-inherits the run's deliverable root.
4. **Rotate TODO.md** (see below).

## Adding Cards Mid-Flow (backflow scenarios: review rejected → fix card / re-review card)

If you need to add cards mid-chain (e.g. code-review verdict CHANGES_REQUESTED, you create a fix card + a
re-review card), **use this endpoint to register; never call ledger functions by hand inside containers**:

```bash
curl -s -X POST http://127.0.0.1:8700/api/team-work/<run_id>/cards \
  -H 'Content-Type: application/json' \
  -d '{"cards":[
    {"task_id":"<your pre-created card id>","title":"Security fix","assignee":"coder",
     "required_files":["FIX_NOTES.md"],"timeout_seconds":1800},
    {"task_id":"<re-review card id>","title":"Security re-review","assignee":"code-review",
     "required_files":["REVIEW2.md"]}
  ]}'
```

- Same prereq as before: create the card via `hermes kanban create` first; **this endpoint only registers,
  does not occupy slot or dispatch**.
- After registration, dispatch via `advance` one at a time (strict serialization unchanged).
- Added cards automatically inherit the run's `project_workspace`, `required_files` won't mis-place into
  the task directory.
- Idempotent: re-registering the same card does not double-count; one card cannot belong to two runs.
- Response: `{registered, already_registered, skipped, card_count}`, cross-check each field.

## TODO.md Rotation (main edits directly, respect ownership)

- Move the previous card along with its `result` summary under `Done`; promote the next item
  from `Pending` to `Current Task` (carry over `goal` / `assignee` / `product_workspace` / `required_files` fields
  verbatim, reset the result line to placeholder).
- Use execute_code's python `open()/write` to modify the file: patch / write_file tools writing
  /share_workspace will be rejected by HERMES_WRITE_SAFE_ROOT; execute_code is unrestricted.
- When parsing `待执行` entries, first strip the leading `- `, then split by `field name:`. After
  modification, print key fields like title / deliverable directory for cross-check — spliced residue in
  the contract file will pollute what the worker reads.

## Report Before Every Card Rotation Then Advance (user explicit correction 2026-10-19)

After settling one card (complete) but BEFORE dispatching (advance) the next,
main must FIRST post a user-facing progress report to the conversation.
A bare ledger-action flow (complete + claim + advance + a one-line status blurb)
is INSUFFICIENT. Counter-example: when settling the c6 tester card, main only
touched the ledger and wrote a one-line blurb — the user couldn't see progress,
violating the default protocol "brief on every completed step".

The report must include four elements (all four required):

1. **Card identification** — card id + worker name + session id + absolute path of the report artifact
2. **Actual measured result** — pass/fail vs baseline, key commit hashes / blob md5s / test counts;
   when a "conditional pass" verdict applies, state the boundary in one sentence
   (e.g. "the 6 pytest FAILs are all env issues unrelated to this batch")
3. **Independent verification** — what main independently did (ran tests / grepped the commit /
   md5sum-compared / verified env facts) to cross-check the worker's self-reported conclusion
4. **Downstream impact** — whether this card closes any defect, and who/what is queued next

Non-omittable scenarios: even when there is "only ledger action + one-line description",
you must add a short paragraph (≥4 sentences). **No progress report = user perceives the chain as stalled**.

**Protocol patches must also land in the repo's `deploy/skills/`**: when main patches a skill
under `/opt/data/skills/agent-matrix/` (the runtime cache layer), the same patch must be written
into the repo's `<project>/repo/deploy/skills/<skill>/SKILL.<lang>.md` and committed + pushed —
the runtime cache is overwritten by the next `install.sh`, so the repo copy is the source that
actually reaches the deployment. Patching once but not twice = the protocol is still missing
the next time the user loads the skill.

## Pitfalls

- **Don't use `__spawn-bg` + handwritten manifest to advance.** The v1 path bypasses run-attribution
  validation and project_workspace inheritance, and was the cause of the 2026-10-04 supplemental-card
  deadlock. Always go through `advance`.
- Verify spawn is truly running: `ps aux | grep __spawn-bg` (advance still uses it internally to start the
  worker) + check GET `/api/tasks` for session_id / runner_kind, don't only look at kanban status.
- Cross-check the verdict in the settlement report against the deliverable (read the deliverable file
  exists and is non-empty) before complete, don't only trust the forwarded message.
- Related but not covered: initial dispatch — see the `mw-task-dispatch` skill; for the live
  worker-registry roster use GET `http://127.0.0.1:8700/api/workers`.

## Fix/delivery cards claiming "pushed" must be main-side verified

When a coder/reviewer claims "commit + push to origin/main succeeded", main must run the verification
itself using the repo URL from `PROJECT-MEMORY.md` before settling:

```bash
git rev-parse HEAD                                       # local
git ls-remote <project-remote-url> HEAD                  # remote
# Expected: equal. MATCH → record "remote tip == HEAD, push truly landed"
```

Three common false positives:

- **Wrong owner/repo path in notes**: the worker writes "pushed to sds/studio" but actually pushed
  to `<owner>/<repo>` on another host (or the reverse). The worker's local `git remote -v` is the only
  trustworthy source; the URL in `PROJECT-MEMORY.md` lines 3-4 is the authoritative reference.
  Never copy the owner/repo path text from the worker's note — independently
  `curl .../repos/<owner>/<repo>/branches/main` to verify.
- **Local committed but remote not received**: FF push is not the default; remote HEAD may not be
  the local one. The remote SHA from `git ls-remote ...refs/heads/main` is the source of truth.
- **Gitea token decode failure**: stop at the first 401/403, never retry with empty token.

The push paragraph in main's progress report must contain: remote commit SHA + local SHA + repo
owner/repo path, all three displayed verbatim with equality signs = PASS. Otherwise treat it as
worker self-claimed but unverified; refuse settlement, resume to ask the worker to actually verify,
or main pushes themselves.