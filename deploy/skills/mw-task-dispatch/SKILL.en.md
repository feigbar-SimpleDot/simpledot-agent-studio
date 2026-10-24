---
name: mw-task-dispatch
description: "Dispatch Team Work tasks via the mw launch endpoint. Use when asked to delegate, assign, distribute, or hand off work to a worker/coder/agent, run tasks in the agent matrix, orchestrate multi-agent jobs, or when the user mentions `派发/分发/委派/多智能体任务/Team Work`."
version: 3.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [Agent, Matrix, Dispatch, Middleware, Kanban, TeamWork]
---

# MW Task Dispatch (Agent Matrix · 1A Boundary · Team Work Top-level Object)

This environment is a multi-container Agent matrix. **1A boundary: card lifecycle belongs to you (main); the worker is
only spawned by mw.** mw never builds a kanban card — you do.

⚠️ **Dispatch MUST go through `POST /api/team-work/launch`, never use `mw.cli dispatch`.**

`launch` is the **only trigger** that creates a `team_work_runs` row in the ledger: the instant you create your cards,
launch synchronously writes the run into the ledger (`run_id` + each card's `team_work_run_id` + initiating session +
current-card slot), **the Team Works board sees it right away, reaper starts scanning it immediately**.

`mw.cli dispatch` bypasses launch; the resulting card has `team_work_run_id=NULL`, the Team Works board can't see it,
and reaper SKIPs it (orphan card, the chain is dead).

## When to Use

- The user asks "let worker / coder / some agent do X"
- The task needs isolated execution and auditable settlement
- The user mentions: dispatch, distribute, hand off, multi-agent, task matrix, worker, Team Work (`派发/分发/委派/多智能体/任务矩阵`)

## How to Dispatch (three steps, fixed order)

⚠️ Your terminal session PATH may not include the venv; **always use an absolute path**:

```bash
export PATH=/opt/hermes/.venv/bin:$PATH   # contains python3 (with pydantic) and hermes

# ① You yourself create the kanban card (mw does not create cards) — one step per card
TID=$(hermes kanban create "Task title" --assignee coder --body "Self-contained task description" \
      | grep -oE 't_[a-z0-9]+' | head -1)

# ② You yourself claim this card
hermes kanban claim "$TID"

# ③ One launch submits all steps (launch creates run + dispatches first card)
curl -s -X POST http://127.0.0.1:8700/api/team-work/launch \
  -H 'Content-Type: application/json' \
  -d '{
    "initial_task": "<original request>",
    "session_id": "<your Hermes session_id>",
    "project_workspace": "/share_workspace/projects/<name>",
    "steps": [
      {"step_id":"s1","title":"Step 1 title","task_id":"<card 1 id>","assignee":"coder",
       "required_files":["out.py"],"timeout_seconds":600},
      {"step_id":"s2","title":"Step 2 title","task_id":"<card 2 id>","assignee":"code-reviewer"}
    ]
  }'
```

launch response:

```json
{"ok": true, "run_id": "<16hex>", "card_count": 2, "dispatched": "<first card id>",
 "note": "已建 run + 登记全部卡 + 派发第一张。后续卡: main complete 当前卡后 POST /api/team-work/<run_id>/advance 推进。"}
```

**Save the returned `run_id`** — every downstream card advance uses it, the Team Works board keys on it.

### What `session_id` to fill in

Fill **your own** Hermes `session_id` (reaper uses it to deliver worker reports back to you,
`POST /api/sessions/<sid>/chat`).

- From the "Start Team Work" button in the console: the request text already has `session_id: "<sid>"` injected, **use
  it directly**.
- If unsure of your `session_id`: read it from your system prompt (format `api_<timestamp>_<8hex>`), or copy it from the
  console session list.
- **Never leave it empty** — empty = worker reports have nowhere to go, you "lose contact".

### Advancing the next card (the key to strict serialization)

After the current card settles (you `hermes kanban complete` and the ledger's `current_card_id` is cleared):

```bash
curl -s -X POST http://127.0.0.1:8700/api/team-work/<run_id>/advance \
  -H 'Content-Type: application/json' \
  -d '{"task_id":"<next card id>","target_profile":"coder","title":"Step 2 title",
       "required_files":["review.md"],"timeout_seconds":600}'
```

advance: verifies previous card is settled (`current_card_id` is NULL) → occupies the slot → spawns the worker. Calling
advance before the previous card settles → **409 reject** (serialization iron rule; prevents parallel construction).

### Adding cards mid-flow (backflow scenarios: review reject → fix card / re-review card)

If you need to add cards mid-chain (e.g. code-review verdict CHANGES_REQUESTED, you create a fix card + a re-review
card), **use this endpoint to register; never call ledger functions by hand inside containers**:

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

- Same prerequisite as before: create the card via `hermes kanban create` first; **this endpoint only registers, does
  not occupy slot or dispatch**.
- After registration, dispatch via `advance` one at a time (strict serialization unchanged).
- Added cards automatically inherit the run's `project_workspace` (deliverable root); `required_files` will not
  mis-place into the task directory.
- Idempotent: re-registering the same card does not double-count; one card cannot belong to two runs.
- Response: `{registered, already_registered, skipped, card_count}`, cross-check each field.

### Field rules (violating → 400/422 rejection)

| Field | Rule |
|---|---|
| `initial_task` | Required, original request (echoed back) |
| `session_id` | **Required**, your own Hermes `session_id` (see above) |
| `steps` | Non-empty array, each item = one step |
| `steps[].step_id` | Required, unique within this launch |
| `steps[].task_id` | **Required** = the kanban card id you have created and claimed. mw does not create cards |
| `steps[].assignee` | Must exist in the registry (currently: coder / code-reviewer / system_engineer / tester). Unknown = reject |
| `steps[].required_files` | Injected into the worker prompt, the worker will create them. **Don't hide the answer in the filename** |
| `steps[].timeout_seconds` | Timeout backstop (default 1800). When the worker hangs, reaper collects the body per this |
| `steps[].include` | Optional, default `true`. `false` = skip this step: mw does not register the card, does not count it in `card_count`, does not occupy the slot. Pair with `skip_reason` |
| `steps[].skip_reason` | Reason for `include:false` (lands in launch response `skipped_steps[]` for audit) |
| `project_workspace` | Optional. For project tasks, use `/share_workspace/projects/<name>` (must be under the whitelist prefix, else reject) |

### Skipping steps (you are the decision-maker, 2026-10-06)

Steps marked `optional: true` in workflow templates (check `GET /api/workflows`); you can decide to skip per task type.
**Decision is yours (product manager role); mw does not judge.**

**Path 1 — Skip the step entirely (zero overhead, preferred)**: in the launch payload, add
`"include": false, "skip_reason": "<why skip>"` to that step. The card never enters the ledger, no worker dispatched.
Note: if you've already created the card, remember to `hermes kanban archive` it — don't leave orphans.

```json
{"step_id":"design","task_id":"t_xxx","assignee":"system_engineer",
 "include":false,"skip_reason":"Pure backend hotfix, no architectural design needed"}
```

**Path 2 — Trace skip (when audit is needed)**: register the card normally, but add a `- 跳过: 是 [原因: <reason>]` line in
the TODO.md `当前任务` section you seeded. (The `- 跳过: 是` text is a protocol literal
shared by zh-cn and en deployments — the worker's skip-fast-path in `mw/pipeline.py`
matches the Chinese-key wording directly so that `流程(LANG=en/中文)` writes are
interoperable with one canonical skip line.) The worker on startup immediately writes
`结果: 产品经理决策跳过 — <reason>` + `状态: completed` and exits; reaper settles normally.
Cost = one very short worker round.

Typical scenarios (pure backend dev): skip interaction / UI design roles; hotfix skips design; typo / doc edits skip
testing. The `skip_when` list in `workflows.yaml` is a hint for you, not auto- executed — every card needs your explicit
decision.

Rules:
- All skipped (every step `include:false`) → launch returns 400, keep at least one.
- The two paths cannot stack on the same card (include:false wins; the card never enters the run).
- `optional: false` steps (requirements / implementation / review) are in principle not skipped; forcing `include:false`
  won't be blocked, but that's product overreach and you're responsible.
- Cross-check `skipped_steps` in the launch response against your decision.

### Guards (built into launch, you don't judge yourself)

- **An ACTIVE run already exists → 409**: only one Team Work at a time. Close the old one first (`POST /api/team-work/<run_id>/close`),
  then launch.
- **First card dispatch fails → auto rollback**: run auto-closes + cards archived, no half-baked ACTIVE, can re-launch.
- **DB partial unique index backstop**: even under a race only one ACTIVE run can land in the DB.

## Iron Rules

1. **Cards are yours to create, not mw's.** `hermes kanban create` + `claim` first, then launch.
2. **Dispatch goes through launch, not CLI.** At launch, the run enters the ledger, the board sees it, reaper scans it.
   `mw.cli dispatch` is the legacy single-task path, doesn't write `team_work_runs`, in the Team Work system it is an
   **orphan**: the board can't see it, reaper doesn't settle it, reports don't come back.
3. **Strict serialization.** launch only dispatches the first card; every subsequent card, after you complete the
   previous one, goes through advance. Never dispatch multiple in parallel (causes duplicate work, state conflicts).
4. **Don't ask the user for information on the worker's behalf.** When the worker is missing info, it writes
   `.clarify.json` itself; the question will wake you via reaper; you answer and resume in the same session.
5. **No auto-retry on failure.** blocked tasks wait for you, never re-dispatch (preventing duplicate work).
6. **Workers have zero kanban awareness.** Your task description is their entire world; be clear: what to do, what files
   to deliver, where to put them.

## When reaper settles (worker perspective)

After launch, reaper polls every 15s; the worker's `TODO.md` and its `## Current Task → status` field decide whether to settle:

| `status` | reaper action |
|---|---|
| `working` / `in-progress` / `进行中` | No action (worker is working) |
| `completed` | Settle immediately to ledger, relay wakes you to take over |
| `failed` | Settle immediately, verdict=FAILED (worker's own decision) |
| `blocked` + `.clarify.json` | Settle, verdict=NEED_CLARIFICATION |
| `blocked` (no clarify) | Still settle as NEED_CLARIFICATION (backstop placeholder, don't wait) |
| Field missing | Wait for worker startup; after 5 min the platform verdicts FAILED |

> **Boundary reduction (2026-10-08)**: legacy versions had `PHYSICAL_ERROR` (mw uses required_files existence to
> override the worker's completed decision) and `PASS_BUMPED` (mw auto-starts a loop re-running the same card); both
> were mw overreach and were removed. Physical-check results now appear only as a `worker_report.physical_check`
> **evidence** alongside COMPLETE; whether to loop is orchestrated by **you** (main), not by the middleware.

**The worker must write `working` immediately on startup, and write `completed` the moment it finishes (atomic rename:
write `.tmp` then `mv`, to avoid reaper reading a half-finished file)** — otherwise reaper never settles. `exit_code=0`
is not a settle signal (that's the HTTP return value of `POST /chat`, not a worker-exit event; the worker hermes session
is long-idle, no "exit" concept).

⚠️ **reaper only scans cards under the current ACTIVE run** (single- task serialization iron rule). Cards that didn't go
through launch (no `team_work_run_id`) are SKIPped by reaper, never settle — this is why you must go through launch.

## View Results + Advance

After the worker finishes, harvest only writes the ledger report (**does not change kanban**); reaper wakes your session
via chat. You read the `worker_report`'s verdict and decide yourself:

- `COMPLETE` → you call `hermes kanban complete <task_id>` to settle, then `POST /api/team-work/<run_id>/advance` to
  advance to the next card (strict serialization)
- `NEED_CLARIFICATION` → after answering, `mw resume --task <id> --instruction "..."`
- `FAILED` → change direction / swap worker / create follow-up card / give up (worker self-reports can't-do, or platform
  times out; check `worker_report.why` to differentiate)

Chain advancing (complete + advance to next card) is your duty, the middleware doesn't do it for you.

**Don't poll**: while waiting for settlement, don't repeatedly check worker-session message counts / personally run a
browser to recheck worker deliverables — wait for reaper to wake you; the backstop knock will wake you when truly
deadlocked.

```bash
# When needed (for want to see progress), check the ledger once, don't poll:
python3 -m mw.cli status                                            # ledger snapshot (includes worker_report)
curl -s "http://127.0.0.1:8700/api/team-works?include_closed=1"    # team work list
curl -s  http://127.0.0.1:8700/api/team-work/<run_id>               # single run detail
hermes kanban show <task_id>                                        # task detail
# Web console: http://127.0.0.1:8700 → Team Works board, view ACTIVE cards + close
```
