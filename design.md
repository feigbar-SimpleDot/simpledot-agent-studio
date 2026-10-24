# SimpleDot Agent Studio — Architecture Design Document

> **Positioning**: a multi-agent long-running task management tool built
> on Hermes' native Kanban system.
>
> **Solves two core problems**:
> 1. Incomplete isolation between multiple profiles in a single-process
>    Hermes (shared filesystem / environment / runtime state contaminate
>    each other) -> one container per Worker, Main owns the bus exclusively,
>    physical isolation.
> 2. Hard cross-process multi-agent collaboration, hard long-task tracing
>    -> Kanban ledger + session anchors + deterministic reaper, hour-long
>    tasks are fully traceable, recoverable, auditable.
>
> **Version: the 1A boundary (set 2026-10-03)**. This document reflects the
> current implementation.
> **Architecture in one line: main drives Kanban which moves the workflow;
> `mw` is only a communication pipe, passively notifying main when to step
> in.**
>
> History: v1 "hijack the kanban binary" -> v2 "mw creates cards + active
> verdict" -> **1A "main creates the card, mw only launches workers"**.
> v2 overloaded `mw` (create card + claim + seed + run validation scripts),
> and every extra responsibility added a new class of incident — the root
> cause of repeated rework. 1A takes create / claim / complete all back to
> main; `mw` shrinks back to a pure communication middleware. Old audit
> records (DESIGN_AUDIT, etc.) have been merged into this document, and
> the originals removed.

---

## 1. Goals and principles

**Goal**: multiple isolated container Subagents share one Kanban task
bus, bus ownership is one-way:

1. **Main Agent exclusively owns Kanban**: `kanban.db` lives only on the
   Main side. Create / claim / complete are all explicitly initiated by
   main's LLM; Subagents have **zero permission, zero awareness** of
   Kanban (they don't even need to read it).
2. **`mw` is a communication pipe, not a coordinator**: `mw` does not
   create cards, claim, complete, run external commands (the old
   `check_script` is deleted), or write project files. It only: validates
   (registry / whitelist / guards) + ledgers + launches workers + relays
   messages.
3. **Deterministic reaping (Harvest)**: task reaping does not depend on
   any single LLM self-report. The verdict relies only on physical
   evidence — TODO.md protocol status field (see §11.4 for
   the exact literal) + product files + real tool calls in the transcript.
4. **Session ID anchor**: the worker's session ID is pre-created by the
   middleware and recorded as a `task_id <-> session_id` mapping. On
   abnormal reaping, return to the worker session via the ID to resume,
   maximising the avoidance of duplicate work / error overwrite / task
   chain collapse.
5. **TODO.md as the single source of truth**: task content lives in the
   shared volume's TODO.md, the worker reads it directly, not through the
   launch->kanban->spawn->prompt code chain (if any link in that chain
   breaks, the worker gets a `continue task` placeholder).

**Iron rules**:

- Worker self-reports are never used as state input.
- Harvest failure != auto re-execute. Re-dispatch must be Main Agent's
  explicit decision.
- `mw` never touches Kanban (create / claim / complete are entirely
  main's business).
- Strictly serial: only claim + dispatch the next card after the previous
  one is completed.

---

## 2. Topology

```
+--------------------------------------------------------------------------+
| Main Agent container (sole owner of kanban.db + middleware ledger)       |
|                                                                          |
|   LLM (Main Agent)                                                       |
|     | 1. hermes kanban create/claim/complete (card lifecycle, main        |
|     |    direct)                                                         |
|     | 2. Write <project>/TODO.md to seed the current task (task truth,    |
|     |    main writes directly)                                           |
|     | 3. POST /api/dispatch {task_id,...} (launch worker, via mw)        |
|     v                                                                    |
|   Middleware (mw, deterministic Python)                                  |
|     1. Pydantic validates the manifest (fail -> 422, main fixes and      |
|        resubmits)                                                        |
|     2. Validate target_profile against registry / workspace whitelist /  |
|        duplicate-dispatch guard                                          |
|     3. Ledger registers the harvest contract + pre-create session anchor |
|     4. Write spawn marker -> POST worker gateway /chat to launch worker  |
|     5. Grab session_id, ledger task_id <-> session_id                    |
+--------------------------------------------------------------------------+
                    | one-way: mw -> worker (inject); worker -> file (report)
                    v
+--------------------------------------------------------------------------+
| Worker container (stateless compute unit)                                |
|  - no kanban.db, no kanban toolset, no HERMES_KANBAN_TASK                |
|  - reads TODO.md protocol status field, works; only edits two protocol   |
|    fields (status / result - see §11.4)                                  |
|  - HERMES_HOME mounted as a persistent volume (session transcript         |
|    archive; container dies but the site survives)                        |
|  - missing info: write .clarify.json + status blocked; on completion     |
|    write completed + result                                              |
+--------------------------------------------------------------------------+
                    | worker writes TODO.md status
                    v
+--------------------------------------------------------------------------+
| Reaper (middleware, deterministic, re-entrant)                          |
|  Every 15s scans the PENDING task's TODO.md protocol status field:       |
|    completed  -> harvest ledgers (physical check + transcript cross-      |
|                  check)                                                  |
|    blocked + .clarify.json -> harvest, verdict=NEED_CLARIFICATION       |
|    working times out -> physical reaping PHYSICAL_ERROR                  |
|  After ledgers, relay: chat POST the worker_report to the main session,  |
|  waking main (reaper does NOT modify kanban, only notifies; decision     |
|  power lives with main)                                                  |
+--------------------------------------------------------------------------+
                    | chat wake
                    v
        Main LLM starts a turn, reads the report -> itself completes the
        current card + dispatches the next
```

**Shared volume**: `/share_workspace` is mounted at the same path on
main and every worker. Task directories, project directories, TODO.md,
and product files all live here, visible to both ends.

---

## 3. Task-start determinism (1A)

### 3.1 The problem

Kanban cards are created by the LLM. If flow correctness depends on the
LLM "remembering" to dispatch to the right agent, "remembering" to let
the middleware record sessionId, "remembering" to configure the recovery
mechanism — i.e. depending on keyword / prompt compliance — then the
starting point is non-deterministic, and the whole chain inherits that
uncertainty downstream.

### 3.2 Empirical facts (where the holes are)

- `hermes kanban create --assignee ghost_agent_999` **succeeds**, the
  card enters the ready state. Hermes does NOT validate whether the
  assignee is a real profile. If the LLM writes / invents a wrong
  assignee, the task silently stalls.
- v2 let `mw` create cards on the LLM's behalf, which overloaded `mw`
  (create card + claim + seed + run scripts), and every extra
  responsibility added a new class of incident — the root cause of
  repeated death loops. 1A takes card creation back to main; `mw` only
  launches workers.

### 3.3 Solution: main creates cards, `mw` launches workers — a deterministic split

**main owns "what cards to create, for whom" (business decision); `mw`
owns "launch the worker and guarantee reaping" (deterministic
mechanism).**

1. **main creates + claims the card**: main's LLM itself does
   `hermes kanban create` + `claim`. main is the holder, trustworthy;
   card creation is its day job, not via `mw`.
2. **`mw` dispatch is the only entry to launch a worker**: main calls
   `POST /api/dispatch` (or the `mw dispatch` CLI); after validating
   the manifest, `mw` only launches a worker for an **already-existing**
   card.
   - Pydantic hard contract (`extra="forbid"`): extra fields are
     rejected outright, prevents smuggling.
   - registry validation: profile does not exist -> 422.
   - workspace whitelist: `project_workspace` out of bounds (e.g. /etc)
     -> 422.
   - duplicate-dispatch guard: same task already has an active session
     -> rejected already_running, returns the existing session for main
     to resume, does not start a second worker duplicating work.
3. **Bind at creation**: after dispatch succeeds, the harvest contract,
   session-recording rules, and recovery policy are all registered in
   the middleware ledger and welded to the `task_id`. Subsequent
   reaping **does not need to ask any LLM** "how to verify this task" —
   the answer is fixed at dispatch time and written to the ledger.
4. **Idempotent**: `idempotency_key` prevents main from going haywire
   and dispatching the same thing twice.

**Manifest is a Pydantic hard contract** (replacing "keyword
compliance"):

```python
class WorkerTarget(BaseModel):
    profile: str                      # must exist in registry; middleware validates

class HarvestContract(BaseModel):
    required_files: List[str] = []
    timeout_seconds: int = Field(default=1800, ge=60, le=86400)
    # 1A: no check_script -- mw runs no external command
    # 1A: no auto_retry -- re-dispatch is outside what LLM can express

class TaskManifest(BaseModel):
    task_id: str                      # 1A required: the kanban card main already created and claimed
    title: str
    target: WorkerTarget
    input_spec: InputSpec             # task content injected into worker directly, not via Kanban
    harvest: HarvestContract
    idempotency_key: str              # prevent duplicate worker launches
    project_workspace: Optional[str]  # for project tasks
    model_config = ConfigDict(extra="forbid")   # reject extra fields outright, prevents smuggling
```

Schema validation failure -> middleware exit code 422 + error details
back to main -> main fixes and resubmits. This is the **only** feedback
loop between main and the flow, and it is a deterministic "fail-reject"
loop.

### 3.4 Responsibility split summary

| Decision | Owner | Determinism guarantee |
|---|---|---|
| What to do, who does it, which card to create | main LLM (business intent) | main is the holder, trustworthy |
| Launch worker / validate / bind / record session | `mw` code | dispatch is the only entry, no bypass |
| Do the work | worker LLM | fully decoupled from the bus, recoverable on failure |
| Verdict | `mw` (physical evidence: TODO status + product + transcript) | does not trust LLM self-report |
| Advance / re-dispatch / clarify | main LLM explicit decision | harvest only reports, does not edit kanban; main decides |

### 3.5 Trigger mechanism

**`mw` is NOT an "externally triggered" service — it is the tool in
main's hand.**

Dispatch side (main active):

```
User gives main a goal -> main LLM decomposes -> creates + claims the card
  -> POST /api/dispatch
  -> mw validates / ledgers / launches worker -> returns "task t_xxx accepted, session anchored"
```

Reap side (main's conversation may have long ended; cannot depend on
main being online):

| Mode | Mechanism |
|---|---|
| **Background reaper loop (production shape)** | Resident process every 15s scans TODO.md status -> on change harvests to ledger -> relay chat wakes main. The reaping trigger itself is deterministic code, does not depend on main remembering to reap |

**The nature of LLM-trigger dependence (irreducible but harmless)**:

"Whether to dispatch" is decided by the LLM — that is a function, not a
defect. All its failure modes are clean:

| LLM behaviour | Consequence |
|---|---|
| Calls and fills correctly | Content locked by schema, flow locked by pipe |
| Calls and fills wrong | Content revealed; 422 rejects, zero side-effects, retryable |
| Does not call | Zero tasks produced, system stays at a clean visible state |

Escape hatch: non-LLM trigger sources (cron / webhook) can feed dispatch
directly, same pipe. The pipe does not care whether the doorbell was
rung by a human, an LLM, or a timer.

### 3.6 Binding: Kanban = ledger, registry = sole route

**Kanban cards do NOT "bind" executors.** The `assignee` field stores
the string `"coder"`, semantically inert in Kanban — the native
dispatcher is disabled (`dispatch_in_gateway=false`); Kanban does
nothing with that name. It is just a record for humans and main to look
at ("who owns this work").

What actually turns `"coder"` into a living worker container is the
middleware registry's lookup:

```
Kanban card:        "this work is coder's"           <- record (ledger, no execution power)
Middleware registry:"coder = http://host:8643"      <- route (the only mapping with execution power)
Inside image:       preset Hermes profile "coder"     <- worker's persona config
```

**Profile location clarification**: you don't need to create a coder
profile locally; locally there is only the registry. The binding chain
= registry (local) -> worker gateway -> profile inside the image ->
worker LLM behaviour.

### 3.7 Clarification protocol (worker requests clarification, full chain validated)

When a worker lacks critical info it **does NOT guess, does NOT ask in
the output text** (the output-text-not-trustworthy principle); it uses
the pause-resume protocol, reusing the BLOCK->resume chain, all
communication goes through main:

```
worker lacks info
  -> write .clarify.json {"question": "..."} to the task directory + change TODO status to blocked, exit normally
  -> reaper detects -> harvest verdict=NEED_CLARIFICATION
  -> relay chat wakes main
  -> main answers: ask the user, or launch a "consulting task" at another worker
     (answer also comes back via harvest)
  -> mw resume --task <id> --instruction "<answer>"
  -> worker wakes in the same session via the session anchor, continues with the answer, zero context loss
```

Three-stage latency: **detect** = reaper period (default 15s);
**wait for answer** = unlimited (persistent suspension); **resume** =
instant (API direct, no poll).

### 3.8 `project_workspace`: project-directory sync model

Answer "main created a new project workspace, how does the worker
know": **the worker doesn't need to know; the middleware tells it**.
Single fact chain:
  main creates project: /share_workspace/projects/<name>/ (clone/init + write TODO.md)
  manifest carries:    "project_workspace": "/share_workspace/projects/<name>"
  middleware validates: 1. under MW_ALLOWED_WS_PREFIX (blocks out-of-bounds, e.g. /etc -> 422)
  inject:              worker prompt uses the absolute project path; task workspace = <project>/.tasks/<id>

The worker never does "discovery" actions — discovery is main and
middleware's job. Whether the worker switches image or machine, as long
as the mount path is consistent, the manifest is the entire world info
it needs.

> **1A path convention**: the TODO.md for project tasks lives at the
> **project root** `<project>/TODO.md`; the worker's scratch directory
> is `<project>/.tasks/<task_id>`. The reaper slices the project root
> from `/.tasks/` to read TODO.md (a pitfall from the drill, fixed).

### 3.9 Main Agent dispatch SKILL

Main agent "knows to use `mw`" via **skill, not memory**:
`deploy/skills/mw-task-dispatch/SKILL.md`, deployed to the main
container at `/opt/data/skills/agent-matrix/` (Hermes native skill
mechanism, enabled means active).

- **Trigger**: description covers delegate / assign / dispatch /
  decompose / delegate / multi-agent etc.
- **Content**: create + claim + dispatch three-step template + field
  rules + strict serial iron rule.
- **Enforcement is still architecture**: the skill is just a "manual";
  `mw`'s validation / guardrails are the hard constraints.
- Pitfall: main's chat terminal session PATH does not include the venv,
  the skill explicitly states you MUST
  `export PATH=/opt/hermes/.venv/bin:$PATH`.

---

## 4. Data structures

### 4.1 Native reuse (don't reinvent)

- `tasks` / `task_runs` / `task_events`: Hermes Kanban native tables.
  `task_runs` auto-ledgers every claim (profile / outcome /
  started_at / error), i.e. attempt history.
- Native `VALID_STATUSES = triage/todo/scheduled/ready/running/blocked/review/done/archived`.

### 4.2 Middleware ledger (self-built, `/mw_data/ledger.db`)

```sql
-- task <-> session anchor map (the only index for reaping / resuming)
CREATE TABLE task_sessions (
    task_id        TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    profile        TEXT NOT NULL,
    runner_kind    TEXT,
    container_id   TEXT,
    workspace      TEXT,
    worker_hermes_home TEXT,
    updated_at     TIMESTAMP
);

-- harvest contract (bound at creation, zero questions at reaping)
CREATE TABLE harvest_contracts (
    task_id           TEXT PRIMARY KEY,
    contract_json     TEXT NOT NULL,
    harvest_status    TEXT NOT NULL DEFAULT 'PENDING',  -- see ledger.py:_STATE_TRANSITIONS for the complete state machine
    harvest_log       TEXT,
    worker_report     TEXT,        -- structured report for main (verdict + evidence)
    exit_code         INTEGER,
    last_notified_at  TIMESTAMP,   -- relay dedup
    notify_attempts   INTEGER,     -- failure backoff
    notify_dead_until TIMESTAMP,   -- dead-letter
    last_progress_at  TIMESTAMP,   -- stuck-knock baseline
    created_at        TIMESTAMP
);

-- initiator session routing table (which main session relay sends the report to)
CREATE TABLE task_initiators (
    task_id     TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL
);

-- dispatch idempotency log
CREATE TABLE submit_log (
    idempotency_key TEXT PRIMARY KEY,
    task_id         TEXT,
    manifest_json   TEXT,
    created_at      TIMESTAMP
);

-- main->worker resume-request queue (consumed by reaper)
CREATE TABLE task_resume_requests (
    id          INTEGER PRIMARY KEY,
    task_id     TEXT,
    instruction TEXT,
    status      TEXT,   -- PENDING/CLAIMED/DONE/FAILED
    created_at  TIMESTAMP
);
```

> 1A subtraction: the old `contract_hash` / `ContractTampered` tamper-
> proof fields are removed — the worker-prompt hard constraint ("only
> edit status / result two lines") replaces cryptographic verification,
> to avoid the ContractTampered deadlock.

---

## 5. Reaping flow (session anchor + TODO.md status driven)

> **Revised 2026-10-04: reaper upgraded from "polling" to "fsnotify
> primary path + 15s polling fallback"**. The original "every 15s scan
> TODO.md status field" had up to 30-second latency in long chains,
> significantly lagging behind main LLM's self-started watchdog — this
> is a project-level collapse surface (main stalls waiting for the
> notification -> the whole chain halts). This section records the
> real-time channel architecture.

```
worker writes TODO.md protocol status = completed / blocked / failed (atomic rename)
  |
  +-- fsnotify primary path (ms-level) - <project_workspace>/TODO.md mtime subscription
  |     +-- on_modified -> _reap_one_task() -> harvest()
  |        +-- completed -> harvest -> write worker_report -> only true 200 mark_notified
  |        +-- failed    -> same
  |        +-- blocked + .clarify.json -> harvest, NEED_CLARIFICATION
  |        +-- still working -> skip (worker hasn't actually finished)
  |
  +-- polling fallback (15s) - fsnotify miss / file delete-rebuild repair path
  |     +-- reap_once() full logic retained (unchanged)
  |
  +-- stuck detection (5min) - replaces the original 30min threshold
  |     +-- PENDING hanging > 5min -> actively chat-wake main
  |
  +-- relay: chat POST worker_report to task_initiators.session_id
       -> wake main LLM to start a turn
       -> main reads the verdict and decides:
           COMPLETE            -> self kanban complete + claim/dispatch next
           NEED_CLARIFICATION  -> answer then mw resume
           PHYSICAL_ERROR      -> change direction / swap worker / give up
```

**Key: reaper only notifies, does not decide, does not edit Kanban.**
Verdict is `mw`'s (physical evidence), advance is main's (LLM). The two
are separated; `mw` never decides for main.

### 5.0 Real-time design: fsnotify primary path + polling fallback

**Motivation**: the original design treated the reaper as a "real-time
channel" but it was actually 15s polling + 30min stuck fallback — in
long chains main stalled waiting for the notification, the whole
project collapsed. The fact that LLM self-started watchdogs were even
faster exposed the fundamental flaw of the original design:
**real-time notification cannot be implemented by polling**.

**Architecture**:

| Path | Latency | Responsibility | NOT |
|---|---|---|---|
| **fsnotify primary path** | 100ms-level | mtime change triggers reap+relay | not stuck fallback |
| **Polling fallback** | 15s | fsnotify miss-event fix / file rebuild | not real-time notification |
| **stuck detection** | 5min fallback | PENDING hang wakes main | not completion notification |

**Dependency**: `watchdog` Python package (Linux `inotify` / macOS
`FSEvents`). Add to the main container image's dependency list;
`deploy/install.sh` §8.5 hash-check covers `mw/realtime.py`.

**Event source**: `<project_workspace>/TODO.md`. The
`team_work_runs.project_workspace` is the fsnotify subscription root
(only ACTIVE runs have a watcher). Release the watcher when the run
closes.

**Threading model**: the fsnotify Observer + three daemon threads
(harvest / relay / stuck) share one `Ledger` connection
(`busy_timeout=30s` + WAL), lock-free — SQLite's single-writer
serialisation is naturally sufficient.

### 5.0.1 Relay dedup (revised: only mark_notified on real 200)

**Fixed 2026-10-04**: the original design "optimistically mark
`last_notified_at` before sending" did **not** retry on send failure
(timeout / 401 / session end) — the ledger said "sent" but the message
was actually lost. This is **one of the real killers of project
collapse**.

**Fix** (the three-state model finalised on 2026-10-04):

The dedup gate upgraded from "`should_notify` + `mark_notified`" to an
**explicit three-state model**:
- **Pending send**: `notify_confirmed_at IS NULL` — relay scans WHERE
  confirmed_at IS NULL
- **Awaiting confirmation**: `claim_for_relay` atomic claim +
  `last_notified_at=now` (thread-crash fallback 660s)
- **Confirmed delivered**: `notify_confirmed_at=now` (set on real HTTP
  200) + `claim` release

Three-state field meaning: "optimistic mark vs real delivery" fully
decoupled — `last_notified_at` is an **in-transit signal**, NOT a dedup
basis; `notify_confirmed_at` is the **delivery proof**. Relay scan only
looks at the delivery proof; without proof it is treated as pending.
`should_notify`'s default `min_interval_s=0` — the time gate only takes
effect when callers like `stuck_notify` explicitly pass an interval
(the 120s gate capability is retained, but is no longer the default).

**Fix history (superseded by three-state)**:
- Initial (2026-10-03): only `mark_notified` after receiving main
  gateway HTTP 200. Send exception -> not mark -> next fsnotify trigger
  / next reap round retries. `should_notify`'s `min_interval_s` dropped
  from 120s **to 0** (fsnotify event uniqueness replaces time dedup).
- Follow-up (2026-10-04): discovered 429 avalanche — halogen reasoning
  is slow, relay retries the same report every 15s, the run piles up 10
  concurrent -> 14-minute backoff. Three-state solves it:
  `claim_for_relay` atomic grab + only true 200 `confirm_delivered` +
  on failure rollback `last_notified_at=NULL` so backoff can re-dispatch.
- `notify_dead_until` dead-letter backoff retained (dead-letter after
  at most 8 retries, `notify_dead_until=1` year); this is a
  **retry-count gate**, not a real-time gate.

**Second edge check**: the sending thread, after receiving the
response, **re-queries `should_notify`** — if in the meantime it was
silenced by `kill reaper` or `archive`, it proactively rolls back
`last_notified_at` so the next fsnotify event re-dispatches. This edge
check fixed the race window of "marked then silenced".

### 5.0.2 Verdict scope: `current_card_id` single card (option 2)

**Invariant (user-set)**: at any time there can only be 1 ACTIVE team
work run, and within it only 1 ACTIVE task. The ledger's
`current_card_id` field is the sole authority for "who to look at now".

**TODO.md's role**: the sole answer to "is it finished". No separate
`completed` file. The ledger does NOT rely on a "completed" flag to
decide who to verdict — it is decided by `current_card_id`.

**Implementation iron rules (user-set 2026-10-04)**:
- reaper `reap_once` scan scope = `current_card_contract(run_id,
  current_card_id)` single card, **no longer** scans all
  `pending_in_team_work(run_id)` pending cards.
- `current_card_id` is NULL (main hasn't advanced yet) -> this round
  does not verdict any card.
- Queued cards' TODO.md **is not read at all** — stale state cannot
  trigger false verdicts.
- stuck-knock also narrowed to the current card: non-current cards are
  "queued waiting for dispatch", not "stuck"; knocking on them would
  mislead main away from the current task.

**Validation (170 reproduces the original failure scene)**: the second
card's workspace retains stale TODO.md status=completed -> old version
immediately false-verdicted; new version keeps PENDING, first card
advances normally.

### 5.1 Completion detection: TODO.md status field, not exit_code

**`exit_code` is not trustworthy**: it is the HTTP return value of
`POST /chat`, not a worker-exit event. The worker Hermes session sits
idle forever; there is no "exit" concept. The verdict signal is
**only** the TODO.md protocol status field (see §11.4 for
the exact literal):

| TODO status | reaper action |
|---|---|
| `completed` | harvest immediately (trigger: todo_completed) |
| `failed` | harvest immediately (trigger: todo_failed) |
| `blocked` + `.clarify.json` | harvest, NEED_CLARIFICATION |
| `blocked` without clarify | wait (worker may be writing) |
| `working` (and other in-progress forms) | no action; on timeout physical reap |
| missing | wait for start; physical reap after 5min |

Worker MUST write `working` at start, and `completed` immediately on
completion (atomic rename: write `.tmp` then `mv`, avoid reaper seeing
half-written). Not writing = reaper never verdicts.

**Verdict leaves the queue, no repeated requests**: regardless of
result, harvest writes `harvest_status`, while `pending()` only
queries `='PENDING'`. A task's transcript read count = verdict count
(usually 1), independent of reaper polling frequency. ERROR does not
auto-retry — consistent with the "failure only BLOCKS" principle.

### 5.2 Transcript reading: worker API preferred

| Priority | Path | Note |
|---|---|---|
| 1 | **worker Hermes API** `GET /api/sessions/{id}/messages` | gateway owns state.db as authority; reading via it avoids concurrency / WAL problems; same channel and key as dispatch; **no need to mount worker hermes_home** |
| 2 | state.db read-only direct query | optional optimisation |
| 3 | `docker exec sessions export` | last fallback |

Read count = verdict count (O(task count)), not O(polls), same-host
HTTP overhead negligible. Transcript parsing of real tool calls (which
files changed, which commands ran, each exit_code) is zero-LLM and is
the primary verdict basis.

### 5.3 Don't lose the anchor on timeout: pre-create session

Before dispatch, `POST /api/sessions` pre-creates the session and writes
it to the ledger. The anchor exists before the dispatch HTTP call,
so no matter the timeout / disconnect / process death, it cannot be
lost. Work goes through `POST /api/sessions/{sid}/chat`. **Do NOT**
"after timeout reverse-query the most recent session by time" — workers
may run concurrent tasks; the most-recent-by-time is not necessarily
ours — that is guessing, not determinism.

### 5.4 Relay dedup (the critical pitfall from the drill)

Relay chat-POSTs the report to main, dedup relies on `should_notify` +
`mark_notified` (optimistic mark before send).

> **1A fix (drilled 2026-10-03)**: the old `_should_deliver`'s "contract
> already HARVESTED -> drop" gate was **mutually exclusive** with
> report relay — relay's trigger condition is exactly
> harvest_status=HARVESTED, that gate drops even the **first**
> delivery, so the report never reaches main. Remove that gate;
> dedup is entirely `should_notify` + `mark_notified`'s job. DISPATCHED
> log changed to: only record on true delivery; blocked = SUPPRESSED.

### 5.5 Worker message routing: main = entry + project manager

**Role model**: do NOT add a "PM worker". main takes the **entry**
(receiving requirements) and the **PM** (sole kanban owner: decompose,
dispatch, supervise, verdict). workers are pure executors; for
everything they cannot do it **only goes to main**. workers never
connect to each other directly.

```
worker A lacks info -> .clarify.json -> NEED_CLARIFICATION -> relay wakes main
  1. main can answer itself (full global context in hand) -> directly resume A
  2. registry has a worker who knows -> main creates a Q&A card for B ->
     reads the answer -> passes back verbatim to resume A
  3. Neither can answer -> escalate to human (keep NEED_CLARIFICATION,
     relay to the user)
```

A and B have zero awareness throughout: A just thinks "the coordinator
answered me", B just thinks "got a Q&A task". Protection iron rule: one
question forwarded at most once (prevent A->B->C routing loop);
forwarding preserves fidelity; Q&A cards do not require code product;
failure escalates don't redispatch.

**Why no plan-file / DAG / PM-worker**: that would re-invent an
orchestration system, violating the original intent of "maximally reuse
Hermes native Kanban, we only solve information flow between agents".
Decomposition is naturally main's (the holder) LLM duty; supervision is
naturally reaper + Kanban; the only missing piece is "how do worker
questions flow to whoever can answer" — one skill fills that gap.

---

## 6. Deployment constraints (hard requirements in container scenarios)

1. **`kanban.db` only lives on the Main container's local disk**. Worker
   containers do NOT mount it at all. SQLite WAL does not work on
   network filesystems (NFS); sharing a DB volume across containers is
   an incident source and violates the isolation principle.
2. **Worker's `HERMES_HOME` MUST be on a persistent volume** (fixed
   per profile). The session transcript lives in the worker's
   `state.db`; container destroyed = site destroyed = anchor lost. This
   volume is physically separate from `kanban.db`; isolation is not
   broken.
3. **Middleware runs inside the main container** (mounts code + ledger
   volume), NOT on the host. Host-side middleware causes cross-UID
   permission drift on the shared volume.
4. **`share_workspace` mounted at the same absolute path**: both main
   and workers mount `/share_workspace`; the prompt gives both ends a
   valid absolute path.
5. **Disable the native dispatcher**:
   `hermes config set kanban.dispatch_in_gateway false`, otherwise the
   native dispatcher and main's LLM fight for driving rights.
6. **One kanban.db**: `mw`'s `MW_HERMES_HOME` MUST point at main's
   `HERMES_HOME`; otherwise a second `kanban.db` silently forks (dual
   ledger, tasks disappear from one side).
7. **main's three-process entry**: gateway (foreground) + reaper loop +
   console, each under a watchdog; deploy new code by `pkill`ing the
   child process (watchdog restarts), zero container restart.
   (Since image v2026.9.24 the gateway is hosted by the image's s6
   `gateway-default` slot — the image's own
   `_maybe_redirect_run_to_s6_supervision` rewrites the entry's
   `exec hermes gateway run` into `s6-svc -u gateway-default` +
   `sleep infinity`, so the gateway is not the container's foreground
   process; see the CHANGELOG issue#82 entry.)
8. **Bind-mount inode can become invalid**: after the host-side
   `share_workspace` is rebuilt, the worker container still mounts the
   old inode (mountinfo shows `//deleted`) -> the worker sees an empty
   shared volume. Diagnose with
   `docker exec <c> cat /proc/self/mountinfo | grep share_workspace`,
   and fix with `docker restart` of the worker container to rebind.
9. **Project directory ownership**: drill directories are created by
   the host user (1000); the worker is uid 10000 and cannot write ->
   the worker reports BLOCKED. Fix: `chown -R 10000:10000 <project>`.
10. **Worker is unattended**: `approvals.mode: off` +
    `HERMES_WRITE_SAFE_ROOT=/share_workspace` (see DEPLOY.md §2.5).

---

## 7. Empirical evidence (1A full-chain drill, 2026-10-03, real containers on 170)

main creates + claims the card -> `POST /api/dispatch` -> coder reads
TODO, works, writes `completed` -> reaper scans TODO, harvests ledger
`HARVESTED` -> relay chat wakes main -> **main itself does
`kanban complete` + rotates TODO.md**. All four signals arrive:

| Signal | Verification |
|---|---|
| (a) TODO.md end state | `completed` + result summary |
| (b) ledger worker_report | non-empty, verdict=COMPLETE, physical OK |
| (c) kanban card | `done` (main itself verdicts) |
| (d) main session received relay | chat message delivered, main starts a turn and reads the report |

The 4 silent killers fixed in the drill (bind mount / dir ownership /
reaper TODO path / relay dedup gate mutual exclusion) are detailed in
the `agent-matrix-middleware` skill — a dev-machine asset relocated out
of the repo to the project workspace at
`/share_workspace/projects/simpledot-agent-studio/devops-skills/agent-matrix-middleware/`.

Historical empirical evidence (v2 era, mechanisms still valid):
session anchor same-session recovery, transcript programmatic parsing,
kill-interruption half-written without false-positive, clarification
full chain, chat-page native multimodal / steer — these underlying
mechanisms are unchanged in 1A, only "who creates the card" moves from
`mw` to main.

---

## 8. Open issues / next steps

- 7 pre-existing failures in `test_repeat_notify_dedup.py` (backoff /
  dead-letter assertions don't match current ledger behaviour), unrelated
  to the 1A refactor, pending alignment.
- Multi-worker parallel orchestration (currently strictly serial): if
  DAG parallelism is needed in the future, the dispatch concurrency
  guards must be redesigned; not in the current 1A scope.
- Transcript compression lineage (structure after long-session
  compression) not covered.

---

## 9. Computer Use deployment strategy (deploy on demand, not a code problem)

**Background**: all Hermes agents run in headless Docker containers; the
image does not include a browser / desktop environment. When a task
needs computer use (especially "observe the rendered page"), wire it
on demand at deploy time per the strategy below, no code changes —
Hermes natively has all channels ready.

**Empirically verified (170 environment)**:
- The worker image (nousresearch/hermes-agent) has no
  chromium / chrome / playwright / selenium inside.
- Hermes natively supports remote CDP attach: `config.yaml`'s
  `browser.cdp_url`, discovered via
  `HTTP /json/version -> webSocketDebuggerUrl` standard, not
  limited to localhost.
- Cross-container connectivity PoC passed: a browserless/chrome
  container maps :3000, the worker container connects directly and
  receives `HeadlessChrome` + WS endpoint ✓.
- Hermes natively supports remote MCP (`mcp_servers.<name>.url`,
  streamable_http).

### 9.1 Only need a browser -> dedicated Chromium CDP container (recommended)

```
worker container (hermes)  --browser.cdp_url--> Chromium/browserless container (:3000)
                                              real Chromium, real render, CDP over TCP
```

- Worker side adds only one config line:
  `browser.cdp_url: http://<host>:3000`; browser_exec's js() /
  page_info() / screenshots all work out of the box, zero code.
- **Real Chromium render fidelity** — satisfies "observe the rendered
  page" type acceptance.
- Start one instance and share among multiple workers; isolate one
  container per worker when needed.

**Security red line**: the CDP port is **unauthenticated** by default;
connecting to it grants full control of the browser. MUST bind to a
Docker internal network or firewall whitelist; forbidden to expose to
the LAN raw.

### 9.2 Other computer use (desktop GUI) -> bare-metal / desktop-enabled VM + forward

| Channel | Form | Use |
|---|---|---|
| **Hermes peer forward** | Bare-metal machine runs Hermes, bot-to-bot DM / SSH trigger | Real desktop GUI, human-takeover demo |
| **peekaboo MCP** | On Mac, peekaboo is exposed to the container via `mcp_servers.url` | Operate macOS native apps |

Note: peekaboo is macOS-only (Accessibility API); its essence is **desktop
automation** — getting DOM / network / console relies indirectly on
screenshots, cruder than CDP; do NOT use it for browser page observation.

### 9.3 moli's positioning (lightweight alternative, render-fidelity risk)

moli (Rust self-developed kernel, Servo family) is **NOT Chromium**:
can accept CDP, light, fast, but the real-page (especially
Vue2+eladmin-style enterprise frontends) render result is **not
guaranteed to match the user's Chrome**.
- ✅ Pure structured scraping, lightweight alternative when you care
  about resource usage
- ❌ **NOT usable for pixel-level render acceptance** — "normal in moli
  ≠ normal in the user's Chrome"

### 9.4 Selection cheat-sheet

| Need | Solution |
|---|---|
| Browser page observation / interaction / render acceptance | §9.1 Chromium CDP container (main force) |
| macOS desktop / native-app automation | §9.2 peekaboo MCP or peer forward |
| Light structured scraping (no render check) | moli (optional) |

For Hermes, all three are one config line (`cdp_url` /
`mcp_servers`); they don't conflict, stack on demand.

---

*Deployment operations are in `DEPLOY.md`. Team Work flow design,
launch / dispatch endpoint boundaries, workflow schema, step skip and
worker registry schema are in §11 below. Main SOUL is in
`docs/main-soul.en.md` (Chinese at `docs/main-soul.zh-cn.md`); the
Worker SOUL runtime template is `mw/soul_template.en.md` /
`mw/soul_template.zh-cn.md`.*

---

## 10. i18n v1.0 — template family & language selection

The 1A boundary above is language-agnostic; v1.0 layers bilingual
support on top without changing any of the boundary semantics.

### Deployment-time language (`LANG` → `MW_LANG`)

A single deployment-wide language is selected at install time. The
selection lives in `.env` as `LANG=en|zh-cn` (default `en`); the value is
hard-validated by `install.sh` (`i18n_validate` in `deploy/lib/i18n.sh`)
and written into `data/mw_data/deploy_lang`. The main container's entry
script (`deploy/main_entry.sh`) reads that file once at start and exports
`MW_LANG` for the running process — this is the server-side locale used
by:

- console `api_lang` lookups
- `mw/console.py api_soul_template()` (returns the language-matched
  `mw/soul_template.<lang>.md` content; an explicit `MW_SOUL_TEMPLATE`
  env override still wins)
- `mw/console.py api_worker_soul_init()` (SOUL-init instruction body and
  all error strings go through `mw/i18n.t()`; the handshake marker
  `【平台 SOUL 初始化指令】` stays Chinese verbatim in both languages)
- `mw/todo.py` TODO.md rendering (field/section keys are emitted in the
  deployment language; the parser accepts both languages — I18N-R3)
- the Workflows "Copy LLM template" output (English example data and
  field semantics under `en`; the `zh-cn` output is byte-identical to
  the pre-R3 baseline)
- `mw/i18n/__init__.t()` resolution (Chinese-keyed `zh-cn.json` vs
  English-keyed `en.json` for server-rendered note strings)
- `verify.sh` / `install.sh` terminal output (via `deploy/lib/i18n.sh`
  `say` / `fail` double-param form)

The POSIX `LANG` environment variable inside the container is **not**
changed — gateway, hermes, and pixel-agents process locale are not
affected. This is deliberate: the hermes image runs with a UTF-8
(`C.UTF-8`-class) locale, and forcing POSIX `LANG=en` inside the main
container would risk ASCII-locale side effects on the gateway, terminal
encoding, and third-party processes; those processes also have their own
i18n concerns unrelated to the platform's bilingual layer. The i18n
feature therefore never touches the process locale — the config name
`LANG` (operator-facing, in `.env`) crosses into runtime only as
`MW_LANG`, matching the existing `MW_*` namespace.

### The `GET /api/lang` endpoint (deploy-language discovery)

A read-only, additive endpoint lets a browser with no stored preference
pick up the deployment language at boot:

| Aspect | Contract |
|---|---|
| Method / path | `GET /api/lang` (sibling of `/api/gwconfig` in `console.py`) |
| Auth | None — same as `/api/gwconfig` / `/health` (console is a LAN ops tool; the value is non-secret) |
| Success | `200`, `Content-Type: application/json`, body exactly `{"lang": "en"}` or `{"lang": "zh-cn"}` |
| Semantics | Returns `mw.i18n.resolve_lang()` — the validated deployment value (`MW_LANG`; missing / invalid -> `en`). Never returns any other string; never 404s. |
| Caching | `Cache-Control: no-store` — changing LANG requires re-install + console restart, so instant freshness beats a cached default |
| Failure handling | The frontend treats any non-2xx / unparseable / unexpected-value body as absent and falls back to `en` |

Frontend boot chain: `localStorage['sas-lang']` -> (if absent) `GET
/api/lang` with a 1 s bounded timeout -> `en`. The fetch must not block
first paint. `sas-lang` is written **only** on explicit user switch; the
endpoint value is never persisted. Hard-defaulting the frontend to `en`
(the rejected alternative) would show English on first boot to every user
of a `zh-cn` deployment until each one manually switches — a worse default
for the platform's primary audience.

The whole i18n API surface is additive: no route removed, renamed or
re-verbed; no response JSON key renamed or removed; only user-visible
string **values** (`message`, `note`, `error`, workflow display names)
follow `MW_LANG`, and `/api/workflows` gained extra keys
(`name_en` / `description_en`) while the originals always stay present.
Protocol payloads (status enums, DB schema, workflow match keys) are
never i18n'd. TODO.md contract parsing is machine-side but bilingual
since I18N-R3: the parser accepts Chinese and English keys
interchangeably; only rendering follows `MW_LANG`.

### Template family layout

The platform SOUL and four dispatched skills ship as language-paired
files. Each pair carries `<basename>.en.md` + `<basename>.zh-cn.md`; at
deploy time, `install.sh` selects the right pair by the validated
`LANG` and bind-mounts the chosen template to the language-neutral name
(`SOUL.md`, `SKILL.md`) that the runtime expects.

| Asset | Source-of-truth files | Layout target |
|---|---|---|
| Main SOUL | `docs/main-soul.en.md` / `docs/main-soul.zh-cn.md` | container `/opt/data/SOUL.md` (or `data/main/SOUL.md` before image build) |
| Worker SOUL template (runtime) | `mw/soul_template.en.md` / `mw/soul_template.zh-cn.md` | runtime-only, read in-process by `console.py api_soul_template()` (no deploy copy) |
| Dispatched skills ×4 | `deploy/skills/<skill>/SKILL.en.md` / `SKILL.zh-cn.md` | container `/opt/data/skills/agent-matrix/<skill>/SKILL.md` |

Each `verify.sh` run recomputes a SHA-256 fingerprint over the host
template files and compares it with the deployed container's copy; a
mismatch surfaces as a red item with a clear remediation
("re-run `install.sh` to refresh the template pair"). This is what
keeps i18n drift between host and container from silently breaking
the deployed worker prompt.

### End-user language switch (`Settings` view, persistence model)

Inside the deployed console, end users may switch languages without
re-deploying. The interaction design decisions in this section carry the
design-layer conclusions for the user-facing switch: `D1/D2/D3/D7/D9`
summarised below; `D4/D6/D8` (default key fallback chain, `<html lang>`
bootstrap ownership, English text overflow rule) are codified in the
`Hard guarantees` subsection of §10 together with the machine-side
contract, since they are runtime invariants rather than per-screen UX
decisions.

- D1: switch entry is `Settings` -> `外观与语言` (no top-bar toggle).
- D2: instant re-render, no reload, no chat-tab loss.
- D3: resolution chain `localStorage['sas-lang']` -> `GET /api/lang`
  (the deploy-time value) -> `en`. The `GET /api/lang` endpoint
  returns only the resolved language value, with no-store cache so it
  does not interact with the i18next in-memory state.
- D7: locale-sensitive dates and numbers all go through `Intl` in a
  single shared module (`ui/src/i18n/fmt.ts`); no component hardcodes
  a locale string.
- D9: server-side strings (note values, ledger messages, console
  fallback HTML) follow the deployment-time `MW_LANG` and ignore
  Accept-Language. User preference only steers the client i18next.

A user who picks `中文` in `Settings` on a `LANG=en` deployment sees the
client side switch to Chinese immediately; the server-rendered
fallback (when the SPA bundle is missing or stale) keeps English. This
asymmetry is deliberate — the server locale is a deployment invariant,
and the client locale is a user invariant.

### Hard guarantees

- i18n v1.0 is **not** a refactor of the 1A boundary; the sections
  above (§1 through §9) describe an unchanged design. Bilingualization
  is additive.
- Business-logic invariants — status machine, dispatcher, reaper,
  ledger — have `git diff main b72ee6b..feat/i18n-v1.0 -- mw/ deploy/ tests/`
  showing **zero** functional-line changes (verified by the protocol
  guard, G1–G13, and the AST diff tool `tools/ast_diff_i18n.py`).
- The TODO.md contract fields are protocol, not copy (G13). I18N-R3
  (2026-10-16) keeps the protocol machine-safe across languages: parsers
  (`mw/todo.py` → reaper) accept **both** key languages via
  `BILINGUAL_HEADER_ALIASES` / `BILINGUAL_FIELD_ALIASES` (e.g.
  `当前任务|Current Task`, `状态|status`, `产物目录|product_workspace`),
  while the renderer emits keys in the deployment language (`MW_LANG`).
  Status **values** are never translated (`working` / `进行中` /
  `in-progress` all parse as in-progress). Legacy Chinese TODO.md files
  (in-flight tasks included) parse unchanged.
- Cross-language protocol literals stay Chinese **verbatim in both
  deployments**, because consumers match them literally: dispatch marker
  `【Team Work 多智能体任务】` (`parseDispatchCard`), SOUL-init marker
  `【平台 SOUL 初始化指令】` (console↔worker SOUL §7 handshake), skip
  fast-path line `- 跳过: 是` (`mw/pipeline.py`). English-language docs
  and templates quote these literals and label them as protocol tokens.
- Protocol literals inside code blocks (e.g. LLM dispatch prompts)
  retain these shared literals; everything else in the `en` template
  family is English (user-visible copy, field keys, example data).

---

## 11. Team Work flow — boundaries, schemas, step skip

> Absorbed from the former living flow documents at the release
> convergence (2026-10-15); this section is now the authoritative
> description. Core principle (1A boundary, 2026-10-03): **main drives
> Kanban to push the workflow forward; `mw` is only a communication pipe
> that passively notifies main when to step in.**

### 11.1 The 1A boundary matrix

| Action | Owner | Channel |
|---|---|---|
| Create / claim / link / complete cards | **main** | `hermes kanban ...` (inside main's container) |
| Seed / edit TODO.md | **main** | writes the shared-volume file directly |
| Spawn worker | mw | `POST /api/dispatch` -> worker gateway |
| Scan TODO.md status | mw (reaper) | read-only polling, no decisions |
| Settlement judgement + wake up main | mw (harvest) | chat POST into main's session |
| Decisions (advance / clarify / swap owner) | **main** | acts itself after receiving the notification |
| Relaying clarification back to worker | mw | `POST /api/resume`, same-session continuation |

mw **never**: creates/claims/completes cards, executes external commands
(the old `check_script` gate was removed), writes project files, seeds
TODO.md, or performs contract-validation sums.

### 11.2 The full chain (strictly serial)

```
User -> [Enable Team Work] -> createSession -> main session
  |
  v
Main LLM:
  1. GET /api/workflows        (pick a reference workflow)
  2. clarify with the user
  3. decompose into N steps, semantically match assignees
  4. create its own cards: hermes kanban create x N -> record task_id
     hermes kanban claim <first card>
     seed <project>/TODO.md (current task + pending queue)
  5. POST /api/team-work/launch {steps: [...]}
     -> mw validates + registers initiator + dispatches ONLY the first card
  |
  v
mw dispatch: validate (registry / whitelist / guardrails) -> ledger -> marker -> spawn worker
  |
  v
Worker: read TODO.md current task -> status=working -> work -> write artifacts
        -> status=completed + result   (or blocked + .clarify.json)
  |
  v
mw reaper (15s poll): scan TODO.md status -> on change, harvest
  -> write ledger worker_report -> chat wakes the main session
  |
  v
Main LLM reads the notification and decides:
  - COMPLETE -> kanban complete current card -> claim next -> POST /api/dispatch
  - NEED_CLARIFICATION -> judge / ask user -> POST /api/resume back to worker
  - FAILED -> change direction / swap worker / create a follow-up card
  - chain end -> summarize "done" to the user + POST /api/team-work/<run>/close
```

Only the shared protocol literals (`- 跳过: 是`, the two `【…】` markers)
stay Chinese verbatim in every deployment — they are protocol, not copy
(see §10 hard guarantees). TODO.md field keys follow the deployment
language and parse bilingually (I18N-R3).

### 11.3 `POST /api/team-work/launch` boundary (1A)

Accepts: `initial_task`, `session_id`, optional `project_workspace`, and
`steps[]` where each step carries `step_id`, `title`, `task_id` (the
kanban card id **main already created** — required),
`task_description`, optional `required_files`, and `assignee` (the
worker profile main picked by semantic match).

What mw does: validates required fields / assignee exists in the
registry / `excludes` red lines / `must_split`; registers the initiator
for all cards (report-relay routing table); dispatches **only the first
card**. What mw does NOT do: create/claim/link cards, seed TODO.md,
decompose or make routing decisions, dispatch more than one card at a
time.

Subsequent cards are dispatched one at a time via `POST /api/dispatch`
(`task_id`, `title`, `target.profile`, `input_spec.task_description`,
`harvest.required_files` / `timeout_seconds`, `idempotency_key`,
`project_workspace`).

### 11.4 TODO.md ownership (unified format, no checksum)

| Field | Who may change it |
|---|---|
| File structure / goal / assignee / artifact directory | main |
| **status** (`working` / `blocked` / `completed` / `failed`) | the current owning worker |
| **result** | the current owning worker |
| pending / done / abandoned queues | main |

The reaper recognises only the status line inside the current-task
section. A worker that does not write its status is never settled.

### 11.5 Workflow schema (v2) and registry schema

`mw/workflows.yaml` is written for two readers — the LLM
(`GET /api/workflows` assists decomposition) and humans (visualisation).
The backend does **not** read it for business validation: the workflow is
a reference layer, not a hard constraint. The repo file is the source of
truth (bidirectional sync with the deployed copy).

```yaml
workflows:
  <workflow_id>:
    name: str
    description: str
    steps:
      - id: str                    # required, ^[a-z0-9][a-z0-9-]{0,62}$
        name: str                  # required
        description: str           # optional (recommended), <=500 chars;
                                   # fallback source for task_description
        input: []                  # optional, input docs (usually upstream output)
        output: []                 # optional, suggested output docs (not a hard gate)
        required_role_tags: []     # required, matched against registry scope.tags
        split_policy: str          # optional: allow_same | prefer_split | must_split
        optional: bool             # optional, default false; when true main may
                                   # skip via include:false or TODO.md 跳过: 是
                                   # (see §11.6).
        skip_when: str             # optional, runtime condition expression
                                   # (resolved by main when seeding TODO.md);
                                   # a non-empty value flips the same step to
                                   # optional=true at launch time.
        depends_on: []             # optional, upstream step_ids (no self-reference)
        loops: []                  # optional, failure rework step_ids (must exist)
        # pass_threshold is deprecated (2026-10-08 subtraction): mw no longer
        # counts passes, no longer gates settlement, no longer reports pass_count.
        # "Run the tests 3 times" is realized by main creating 3 cards.
```

Worker registry (`mw/registry.json`) per profile: `runner: "api"`,
`api_url`, `api_key`, and `scope.tags` (semantic reference for the LLM
when picking an assignee; mw does not do literal-intersection rejection)
plus `scope.excludes` (a **hard** red line enforced by mw: a step's
`required_role_tags` hitting excludes -> 422).

Optional `avatar` field (Issue #51, 2026-10-17): `PUT /api/workers`
accepts either a `data:image/...;base64,` URL or a bare filename. On a
data URL the server decodes it, sniffs the magic bytes against a
whitelist (png/jpg/webp/gif), enforces a 200KB byte cap, writes the file
to `<REGISTRY_DIR>/avatars/<name>-<8hex>.<ext>` and stores only that
bare filename in the registry — the registry JSON never holds base64
payloads. A bare-filename value must match a strict whitelist regex
(no path separators, <=128 chars) or the save is rejected with 422
before anything is written. Bytes are served back via
`GET /api/workers/<name>/avatar` (see §11.7).

### 11.6 Workflow step skip (2026-10-06)

Main / the product manager may skip a step marked `optional: true`;
decision authority is entirely main's side — mw, reaper and workers only
recognise the signal, never judge. Path 1 (zero overhead, recommended):
add `"include": false` + `"skip_reason"` to the step in the launch
payload — no kanban card is created, the step does not enter
`team_work_runs.card_count`, and the launch response's `skipped_steps`
field records it for audit. Path 2 (audit-friendly): when seeding
TODO.md, add a `- 跳过: 是 [原因: <reason>]` line to the current-task
section — the literal is protocol (matched verbatim by
`mw/pipeline.py` in every deployment, en included). The worker recognises
it and exits immediately with `completed` (result line
`产品经理决策跳过 — <reason>`),
leaving the card and a one-turn audit trail. The two paths cannot be
stacked on the same card; steps with `optional: false` may still be
force-skipped by main as an explicit product decision.

### 11.7 Backend endpoint surface

| Endpoint | Method | Purpose | Called by |
|------|------|------|--------|
| `/api/workflows` | GET | list workflow templates | LLM / frontend |
| `/api/workflows/<id>` | PUT / DELETE | create/update/delete workflow | frontend |
| `/api/workers` | GET / PUT | list / register worker registry entries | LLM / frontend |
| `/api/workers/<name>` | GET / DELETE | worker detail (api_key redacted) / delete | frontend |
| `/api/workers/<name>/avatar` | GET | serve worker avatar bytes (file on disk, routed before the worker-detail fallback; see §11.5) | frontend |
| `/api/team-work/launch` | POST | register all cards + dispatch the first | LLM |
| `/api/dispatch` | POST | spawn a worker for one claimed card | LLM |
| `/api/resume` | POST | relay a clarification answer to the worker's same session | LLM |
| `/api/workspaces` | GET / POST | list / create project workspaces (allowlist) | frontend |
| `/api/fs/list` | GET | list one directory level under a project workspace (read-only, allowlist-anchored, 1000-row cap) | frontend |
| `/api/fs/file` | GET | stream a project file (mode=embed\|open\|download, read-only, symlinks blocked) | frontend |
| `/api/tasks` | GET | task status (kanban + ledger); the list is a whitelist projection (`result` reduced to a `has_result` boolean, `harvest_log` / `workspace` / `exit_code` / `question` / `options` stripped, `block_kind` kept); `?fields=active` returns the active-run summary only | frontend |
| `/api/tasks/<id>` | GET | single task detail with the full record (`result` / `harvest_log` / `workspace` / `exit_code`); 404 when the task does not exist | frontend |
| `/api/heartbeat` | GET | reaper liveness heartbeat (`reaper_alive` + `reaper_heartbeat_age_s`); the liveness rule is identical to the one in `/api/workers` | frontend |
| `/api/settings` | GET / PUT | public settings | frontend |
| `/api/compose/generate` | POST | generate worker compose | frontend |
| `/api/upload` | POST | upload files | frontend |
| `/api/gwconfig` | GET | gateway config | frontend |
| `/api/lang` | GET | deployment language (see §10) | frontend |
| `/api/w/<path>` | GET/PUT/POST | passthrough to worker gateway | frontend |
| `/api/sessions/<sid>/messages` | GET | proxy to the gateway session transcript; with `want`/`before` the console serves a windowed page itself (tail-anchored, cursor `o:<n>`), without those parameters it stays a plain passthrough (the rollback switch) | frontend |
| `/health` | GET | health check | monitoring |
| `/` | GET | serve React UI | user's browser |

---

> **i18n note (v1.0)**: Platform runtime bilingual support is selected at
> install time via `LANG` -> `MW_LANG` (§10). The SOUL templates live at
> `docs/main-soul.en.md` / `docs/main-soul.zh-cn.md`.
