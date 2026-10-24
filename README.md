# SimpleDot Agent Studio

> **A multi-agent long-running task management tool built on Hermes' native Kanban system.**

## What problem does this solve

1. **Hermes multi-profile isolation in a single process is incomplete**: profiles
   in the same process share filesystem, environment variables, and runtime
   state — they see each other and contaminate each other. SimpleDot Agent
   Studio puts every Worker Agent in its own container, Main owns the task
   bus exclusively, Workers have zero awareness of the Kanban, and isolation
   moves from "convention" to "physical".
2. **Cross-process multi-agent collaboration is hard, long tasks are hard to
   track**: cross-process agents have no shared state; when a task crashes,
   drifts, or stops reporting mid-way, a chat transcript alone cannot recover.
   This tool uses Kanban as the ledger, the session archive as the anchor,
   and a deterministic reaper as the safety net — so hour-long tasks are
   fully traceable, recoverable, and auditable.

A multi-container agent matrix on a task bus: Main drives the workflow via
Kanban, Worker containers run in isolation, the middleware (`mw`) provides
**deterministic code** (not LLM) for validation, dispatch, reaping, and
relay, and the Web console makes the whole thing observable and operable.

## Core principles (the 1A boundary, set 2026-10-03)

> Determinism comes from "let the LLM do only the right thing, and make it
> unable to do anything else" — not from "make the LLM do the right thing".

**In one line: main drives Kanban which moves the workflow; `mw` is only a
communication pipe, passively notifying main when to step in.**

| Action | Owner | Channel |
|---|---|---|
| Create / claim / link / complete a card | **main** | `hermes kanban ...` |
| Seed / edit `TODO.md` | **main** | Write the shared volume directly |
| Launch a worker | `mw` | `POST /api/dispatch` |
| Poll TODO.md status | `mw` reaper | Read-only poll, no decisions |
| Verdict + wake main | `mw` harvest + relay | `POST /chat` to main session |
| Decide advance / clarify / reassign | **main** | Self-act after receiving the notification |

- **Worker self-reports are NEVER used as state input**: verdicts rely only on
  physical evidence — TODO.md protocol status field (see design.md §11.4
  for the exact literal) + product files + real tool calls in the
  transcript. (The old `check_script` external-command gate has been
  removed.)
- **`mw` never touches Kanban**: create / claim / complete are entirely
  main's job; `mw` only validates, ledgers, launches workers, and relays —
  it runs no external command and writes no project file.
- **Reaping reaps archives, not living things**: the session archive
  (state.db auto-persisted by the Hermes framework) survives independently of
  the process; after a worker crash, the same session is resumed from the
  session anchor.
- **Failures only BLOCK — never auto-retry**: retrying is main's explicit
  decision, to prevent duplicate work.
- **Strictly serial**: launch only dispatches the first card; subsequent
  cards are dispatched by main `complete` then `dispatch`.

## Architecture

```
+- main container ---------------------------------------------+
|  gateway(:8642) + kanban.db                                  |
|  mw middleware: dispatch / reaper (resident) / console(:8700)|
|  ledger /mw_data: task<->session anchors + harvest contracts  |
+--------------+-----------------------------------------------+
               | HTTP API (session anchor)
+--------------v-------------+   shared volume /share_workspace
|  worker container (coder ...)   |       (task dir / project dir, same mount path)
|  gateway + API server           |
|  zero Kanban permission,        |
|  zero Kanban awareness          |
+-----------------------------+
```

**Task flow**: main creates the card + claims + seeds TODO.md ->
`POST /api/dispatch` (Pydantic hard-contract validation) -> launch worker ->
worker reads TODO.md and writes the protocol status field (see
design.md §11.4 for the exact on-the-wire literals) -> reaper polls
TODO.md status -> harvest ledgers -> relay chat wakes main -> main reads the
report and decides (complete current + dispatch next / resume after
clarification / reassign / give up).

**TODO.md as the single source of truth**: task content lives in the shared
volume's TODO.md; the worker reads it directly, not via a launch->kanban->
spawn->prompt code chain (any link in that chain breaking hands the worker
a `continue task` placeholder).

**Clarification protocol**: when the worker lacks critical information, it
writes `.clarify.json` (optional `options`) + status `blocked` then exits
-> harvest decides NEED_CLARIFICATION -> relay wakes main -> main answers
and `POST /api/resume` -> same session resumed from the session anchor,
zero context loss.

## Role model: main = entry point + project manager

- **main**: the sole Kanban owner. Receives requirements (entry point) +
  decomposes / creates cards / dispatches / supervises / verdicts (PM).
- **worker**: a pure executor. Its boundary is "what it can and cannot do",
  and for everything it cannot do **it only goes to main** — no need to
  know who.
- **Workers never connect to each other directly**; all information goes
  through main (the `worker-message-relay` skill: self-answer / dispatch
  a Q&A card / escalate to human — three-way decision).

There is no "PM worker" — decomposition and supervision are the holder
(main)'s job; adding another would break the single-owner principle. See
[design.md](design.md) for details.

## Directory layout

| Path | Description |
|---|---|
| `mw/` | Middleware package: `schemas` (contracts), `pipeline` (dispatch), `harvest` (reaping), `reaper` (resident), `console` (Web) + `console_files` (read-only filesystem API), `ledger` (ledger), `todo` (TODO.md contract), `cli` |
| `deploy/` | `install.sh` (one-shot deploy), `verify.sh` (self-check), `main.compose.yaml` (parameterised), `main_entry.sh` (three-process entry) + `skills/` (4 dispatched Main SKILLs — `mw-task-dispatch` mandatory, plus `team-work-chain-advance` / `team-work-chain-orchestration` / `worker-message-relay`) |
| `tests/` | Integration tests: fake worker / three rounds of complexity / clarification + project workspace / kill interruption / full chain in container |
| `docs/` | Auxiliary docs: SOUL pair (`main-soul.en.md` / `main-soul.zh-cn.md`), main `SOUL.md` (installed to container), worker SOUL pair (`mw/soul_template.<lang>.md`) |
| `ui/` | React/Vite console: `src/App.jsx` entry, `src/views/*` panels, `src/components/filebrowser/*` (sidebar file browser), `src/i18n/locales/{en,zh-cn}.json`, `dist/index.html` (built artifact installed by `install.sh`) |
| `harvest/` | Reaper cache (counters, last seen timestamps, idempotency). Recreated on startup; not for hand editing. |
| `tools/` | Developer-only scripts (AST diff, i18n doc sync, codegraph smoke). Not used by runtime; safe to drop when not iterating. |
| `topology.png` | One-page topology diagram referenced by `design.md §2`. |
| `DEPLOY.md` | Deployment manual (three-step deploy / add worker / special needs / troubleshooting) |
| `design.md` | Full design doc (architecture rationale, deployment constraints, real test records, Computer Use strategy) |

## Quick start

Three-step deploy (clone -> fill `.env` -> one-shot script), see **[DEPLOY.md](DEPLOY.md)**:

```bash
cp .env.example .env && vi .env    # fill API_SERVER_KEY / model endpoint
./deploy/install.sh                # one-shot to bring up main (middleware + console)
./deploy/verify.sh                 # all green = ready
open http://<host>:8700            # console: chat / kanban / files / create worker
```

## Boundaries of management: what this platform owns, what it doesn't

**This platform only owns the "unified entry point for multi-agent
workflows"; it does NOT own the agent itself, and does not aim to ship a
full-featured WebUI.** Each Worker Agent is an independent Hermes instance
that the user manages themselves — fully decoupled from this console:

| Concern | Owner | How |
|---|---|---|
| Task dispatch / tracking / verdict / clarification | **This platform** (console + middleware) | Web console :8700 |
| Worker's model, SOUL.md, memory, skills, toolsets | **The user** | [hermes-webui](https://github.com/nesquena/hermes-webui) / Hermes TUI / `hermes` CLI |
| Worker container start/stop, image upgrade, compose maintenance | **The user** | Console only generates the compose template; starting the container is an explicit host operation |

**Need a fuller WebUI?** Use **[nesquena/hermes-webui](https://github.com/nesquena/hermes-webui)**.
This platform's console is a lightweight interface built for "task
orchestration" (kanban + chat entry + worker registration), NOT a generic
Web frontend for Hermes. Likewise, **single-worker configuration**
(change model, edit SOUL.md, install skills, inspect its own session) goes
to hermes-webui or Hermes's built-in TUI against that worker — there's no
need to force it into this platform, because this platform deliberately
does NOT proxy agent management, to avoid breaking the "worker has no
shared volume mount, no Kanban awareness" isolation boundary.

Rationale:

- **Model configuration does not live here**. The registry only carries the
  request alias `hermes-agent`; the actual model the worker runs is decided
  by its own `config.yaml` (`model.default`) — switching models only edits
  the worker's own config, the registry stays untouched, and the litellm
  fallback stays transparent to dispatch.
- **The console's "view" tab shows run-time metadata only** (online status /
  effective model / toolsets), for accounting, not for management.
- **Single-agent management has dedicated tools**: [hermes-webui](https://github.com/nesquena/hermes-webui),
  Hermes TUI, the worker gateway's own API. This platform does not reinvent
  the wheel — it only covers what they don't, the cross-agent task bus.

In one line: **workflow management lives here; agent management lives there.**

## Verified capabilities (all on real containers)

- **1A full chain drill** (2026-10-03): main creates + claims the card ->
  `POST /api/dispatch` -> coder reads TODO, works, writes `completed` ->
  reaper polls TODO, harvest ledgers -> relay chat wakes main -> **main
  itself does `kanban complete` + rotates TODO.md**. All four signals
  (TODO end state / ledger report / kanban done / worker artifact) arrive.
- Three rounds of different-complexity tasks passed consecutively
  (single file / module + pytest / small project + async + deliberate gap).
- Clarification full chain: worker asks (with options) -> console answers ->
  same session resumes -> done.
- Kill interruption: worker container restarted mid-run -> half-written
  state BLOCKed without false-positive -> resume completes.
- Privilege blocking: project_workspace path whitelist, ghost assignee,
  schema missing-field — all 422 rejected.
- Resident reaper: after dispatch zero manual touch, reaper scans TODO.md
  for automatic verdict; console directly answers BLOCKed tasks.
- **Chat page (user's main entry)**: direct SSE stream chat with main
  gateway, session switching and context continuity, thought-process
  display, mid-turn `/steer`, multimodal image attachments, non-image
  files dropped to the shared volume (handoverable on main->worker path);
  phone top/bottom adaptive layout.
- **Team Work loop**: UI "Enable Team Work" button -> real Hermes session
  created -> LLM reads `/api/workflows` to decompose -> creates its own
  cards -> `/api/team-work/launch` dispatches the first -> back to main.
- **Worker capability tags**: registry adds `scope.tags` / `scope.excludes`,
  Worker management page shows badges, main picks by semantic match.
- **Workflow platform hosting**: web console workflow CRUD, pure-static
  SVG visualisation (incl. loop arcs), "Copy LLM template" + "Import from
  YAML" one-click to let LLM generate a new workflow. The copied template
  (example data, field semantics, task section) follows the console
  language: English under `en`, Chinese under `zh-cn` (unchanged
  baseline copy).
- **Workspace selector**: top-bar dropdown to switch
  `/share_workspace/projects/*`, bound to the session and forwarded into
  the worker prompt.

## Language & i18n

Starting with v1.0, the platform is bilingual (English default + Simplified
Chinese). A single deployment-wide language is selected at install time;
end users may switch between languages live from the Settings view.

### Deployment-time language (`.env` → `LANG`)

The deploy script reads the `LANG` line of `.env` and validates it as one
of `en` (default) or `zh-cn`. Anything else fails fast at install time.
Empty / unset is treated as `en`. The validated value is written into
`data/mw_data/deploy_lang`; the main container's entry script
(`deploy/main_entry.sh`) reads it back and exports it as `MW_LANG` once at
container start.

Two design choices are deliberate:

- `LANG` does **not** become the POSIX `LANG` environment variable — it
  does not affect gateway, hermes, or pixel-agents process locale.
- The value is stored in a file rather than passed through docker-compose
  env because the s6 supervisor chain inside the container does not
  inherit compose env vars into the entry script under all bind-mount
  layouts (design.md §10).

The full Chinese / English checklist is in `.env.example` (see the
`LANG=` block at the top). Once installed, running `LANG=zh-cn ./deploy/verify.sh`
prints every line in Chinese; `LANG=en` prints English. Both languages
expose the same checks — the script is fully bilingual in its terminal
output.

### Runtime language switching (Settings → `外观与语言`)

Inside the deployed console, end users may switch languages without
re-deploying:

1. Open **Settings** in the top bar.
2. Scroll to the **`外观与语言` / Appearance & language** section.
3. Click **English** or **`中文`**. The change is instant — the page re-renders
   without a reload, chat tabs and unsaved input are preserved.

The choice is persisted in the browser (`localStorage['sas-lang']`) and
survives reloads and new tabs of the same origin. Deploying a new
machine and logging back in keeps the language tied to the **browser**
preference, not to the server — so users on shared hosts can each have
their own language. The deploy-time `LANG` remains the **server-side**
language used by background workers and the fallback / console base
template when no user preference is stored.

For the unresolved hard-constraint cases — server-rendered note strings,
locale-sensitive numbers and dates — the server side picks the
deployment-time `LANG` (i18n v1.0 does not chase Accept-Language). See
design.md §10 for the full in/out table and the `GET /api/lang`
endpoint contract.

### Template family (SOUL / SKILL)

Both the main agent's platform-default SOUL and the four dispatched
skills ship as language-paired files:

- `docs/main-soul.en.md` / `docs/main-soul.zh-cn.md`
- `deploy/skills/<skill>/SKILL.en.md` / `SKILL.zh-cn.md`

`install.sh` selects the right pair by the validated `LANG` value and
lays them down as the language-neutral names (`SOUL.md`, `SKILL.md`) the
runtime expects, so changing the language requires only a re-install
followed by `./deploy/verify.sh`.

### Where to read more

- [`DEPLOY.md`](DEPLOY.md) — how to set the language at deploy time
  (including the LANG entry and the install-time hard-validation error
  message).
- [`design.md` §10](design.md) — the full i18n design: template family
  layout, the `GET /api/lang` endpoint contract, the Settings switch and
  the localStorage-persistence model, and the fallback language chain.

## Related documentation

- [`design.md` §11](design.md) — Team Work flow design, launch/dispatch
  endpoint boundaries, workflow schema, step skip, worker registry
  schema.
- [`docs/main-soul.en.md`](docs/main-soul.en.md) — Main Agent platform default
  SOUL (auto-installed, user-overridable). The Chinese version lives at
  [`docs/main-soul.zh-cn.md`](docs/main-soul.zh-cn.md).
- `mw/soul_template.en.md` / `mw/soul_template.zh-cn.md` — Worker SOUL
  runtime template (incl. non-removable hard-constraint sections),
  selected in-process by `MW_LANG`.
- [`DEPLOY.md`](DEPLOY.md) — Three-step deploy, adding workers, new
  worker container setup, troubleshooting.
- [`design.md`](design.md) — Full architecture design (1A boundary,
  updated 2026-10-03).

> **i18n note (v1.0)**: Platform runtime bilingual support (server-side
> and console UI) is selected at deploy time via `LANG` -> `MW_LANG`,
> with a per-browser live switch in Settings. The SOUL templates ship as
> [`docs/main-soul.en.md`](docs/main-soul.en.md) /
> [`docs/main-soul.zh-cn.md`](docs/main-soul.zh-cn.md); see
> [`design.md` §10](design.md) for the full mechanism.

---

Detailed deployment steps, environment variables, and the
quick-reference troubleshooting table are in **[DEPLOY.md](DEPLOY.md)**.
