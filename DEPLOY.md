# Deployment Manual — SimpleDot Agent Studio

> **Goal**: clone the code -> fill one config file -> run one script, and
> main agent is ready.
>
> All worker additions from then on happen in the Web console — no more
> command line needed.
>
> All commands are run in the **repo root directory**.

## 0. Prerequisites

| Item | Requirement | Self-check command |
|---|---|---|
| Docker | v20+, current user can run it | `docker ps` shows no permission error |
| compose plugin | docker compose v2 | `docker compose version` |
| (macOS alternative) | `docker` CLI via docker-for-apple-container shim — same `docker` command surface; install.sh calls `docker` directly | `docker ps` (after the shim is on PATH) |
| Model endpoint | OpenAI-compatible API (litellm / vLLM / official, etc.) already configured in Hermes | `curl <base_url>/models` returns content |

No need to install Python or any dependency on the host — middleware runs
in containers, the image ships them.

`install.sh` calls `docker` directly. On macOS, docker-for-apple-container
ships a docker-compatible CLI shim on `PATH`, so the very same `docker ...`
commands work without any installer-side branch in the script (2026-10-18,
s1-req #53 C). If `docker` is not on PATH at all, install aborts with a
one-line hint pointing at the compose v2 install path.

## 1. Three-step deploy

```bash
# ① Get the code
git clone <this repo> && cd <repo dir>

# ② Fill the config (only the two ★ items are needed to run)
cp .env.example .env
vi .env        # only fill API_SERVER_KEY (generate with: $(openssl rand -hex 32))
# That's it. Models/endpoints are Hermes's own configuration; this platform
# does not interfere (see below).

# ③ One-shot deploy
./deploy/install.sh
```

`install.sh` automatically does: directories and permissions, SKILL
bind-mount setup, bring up containers, wait for healthy. About
1~3 minutes total (depending on image pull speed).

**Step ③ is mandatory, not optional.** The repo ships exactly **one**
frontend build artifact (`ui/dist/index.html`). `mw/ui_dist/` and
`data/share_workspace/ui_dist/` are **deploy-time copies** that
`install.sh` rebuilds from `ui/dist` on every run — they are not in git
(issue #125, route A). So a clone that skips `install.sh` has no console
frontend at all: the console falls back to the built-in `console_ui.py`
page (very old HTML constants). That fallback is expected behaviour, not
a bug, but it means **always run `./deploy/install.sh` after cloning**.

The installer does **not** initialise Kanban — `kanban.db` is created by
Hermes itself: the gateway's embedded dispatcher calls the idempotent
`init_db()` on start-up, and so does every `hermes kanban` command. If
`verify.sh` reports the kanban item red, run `hermes kanban init` (or
re-create the container); `verify.sh` prints the same instruction.

After seeing `✅ main agent deploy complete`:

```bash
./deploy/verify.sh     # all green = deployment ready
```

Open `http://<host-ip>:8700/` in a browser — the Chat page is the user's
main entry; the **Worker management** page is where you start adding
workers.

## 1.5 Language selection (i18n v1.0)

The platform supports a single deployment-wide language (`en` default or
`zh-cn`). The decision lives in `.env` and is hard-validated at install
time.

### Decision: edit `.env` before running `install.sh`

```bash
# In .env (top section, after the i18n comment block)
LANG=en        # or zh-cn — anything else fails install with a clear error
```

That's the only place to set the deployment language. The value is read
once during `install.sh`:

- empty / unset -> treated as `en`
- `en` / `zh-cn` -> accepted, written to `data/mw_data/deploy_lang`
- anything else -> `install.sh` exits with a clear error message
  (`Invalid LANG='...' in .env — allowed: en (default) | zh-cn`)

The file `data/mw_data/deploy_lang` is then read at every main-container
start by `deploy/main_entry.sh`, which exports `MW_LANG` so the server-
side console locale and the deployed SOUL/SKILL template pair match.

### What `LANG=en` vs `LANG=zh-cn` actually changes

| Surface | `LANG=en` | `LANG=zh-cn` |
|---|---|---|
| Console default language on first visit | English | `简体中文` |
| SOUL template loaded into the container | `docs/main-soul.en.md` | `docs/main-soul.zh-cn.md` |
| Worker SOUL template served by `GET /api/soul-template` | `mw/soul_template.en.md` (English TODO.md field keys + protocol-literal notes) | `mw/soul_template.zh-cn.md` |
| TODO.md contract keys rendered by `mw/todo.py` (main seeds) | English (`## Current Task`, `- status:` …; the parser accepts both key languages either way) | Chinese (`## 当前任务`, `- 状态:` …) |
| Dispatched SKILL pair | `deploy/skills/.../SKILL.en.md` | `deploy/skills/.../SKILL.zh-cn.md` |
| `verify.sh` terminal output | English sections + English pass/fail summary | Chinese sections + Chinese pass/fail summary |
| `install.sh` terminal output | English | `简体中文` |

Shared protocol literals are **identical in both deployments** (matched
verbatim by the code): dispatch marker `【Team Work 多智能体任务】`,
SOUL-init marker `【平台 SOUL 初始化指令】`, skip line `- 跳过: 是`.
Status values (`working` / `进行中` / `in-progress`) are never translated.

Things that **do not** change with `LANG`:

- The POSIX `LANG` environment variable inside the container — gateway,
  hermes and pixel-agents process locale are unaffected (design.md §10).
- The end-user language switch in **Settings → `外观与语言`** — that is a
  browser-side preference that overrides the deployment default for one
  user at a time, persistent in `localStorage['sas-lang']`.
- Any other `.env` values (API keys, ports, image, container name).

### Bilingual output in `verify.sh`

After install, `./deploy/verify.sh` is fully bilingual in its own
report-style output:

- `LANG=en ./deploy/verify.sh` — every line English, every section banner
  in English, the final summary reads "all passed (N)".
- `LANG=zh-cn ./deploy/verify.sh` — same checks run, same green/red
  semantics, but every line and section banner is in Chinese, summary
  in Chinese.

One exemption: the `LANG=` report line carries only its English text at
the call site, so it prints in English under either language (the
language pick falls back to the first argument when no second argument
is given).

It is safe to run with either value; pick whichever you read faster
when triaging a deployment issue.

### Re-running install to change language

```bash
# 1. edit .env
vi .env            # change the LANG= line

# 2. re-run install. install.sh regenerates the staging copies
#    deploy/.runtime-soul/SOUL.md and deploy/.runtime-skills/<sk>/SKILL.md
#    from the repo sources (docs/main-soul.<LANG>.md, deploy/skills/...),
#    selected by the new LANG, and mounts those staging dirs read-only.
#    There is no host-side copy to preserve.
#    --preserve-soul is still accepted for old automation but is a no-op
#    (since s4d-fix2); to keep a customized SOUL, edit the repo source
#    docs/main-soul.<LANG>.md yourself and re-run install.
./deploy/install.sh --preserve-soul   # accepted, ignored (back-compat only); or
./deploy/install.sh                  # full re-seed (default)

# 3. verify the lang gate
./deploy/verify.sh
```

A mismatch between the host's `LANG=` and the deployed SOUL/SKILL is
detected by `verify.sh`'s "SOUL/SKILL content fingerprint" check and
will surface as a red item with a clear remediation.

### Troubleshooting entries

The next paragraph in this manual already includes the LANG-related
rows. Refer to the **Troubleshooting** table for:

- `LANG=fr` (or other invalid value) → `install.sh` fail-fast error
- `verify.sh` shows `Invalid LANG=...` line → `.env` LANG is not in
  `en|zh-cn`
- Console appears in the wrong language → check `data/mw_data/deploy_lang`
  matches the running container's `MW_LANG`

## 2. Add your first worker (in the console, no command line)

1. Open the console -> **Worker management** -> Add worker
2. Fill in the profile name (e.g. `coder`) and the gateway address —
   since issue #109 the worker publishes **no host port**; register the
   container-internal address `http://hermes_<name>:8642` (the
   `simple-dot` network alias). The model field is
   only a **request alias** (default `hermes-agent` is fine); the real
   model is decided by the worker's own Hermes config, no need to fill it
   here
3. The console generates the worker compose snippet -> follow the prompts
   to bring up the container on the target host (for same-host deployment
   the console directly gives a copyable `docker compose` command)
4. After saving, main can dispatch tasks to this profile

Worker container essentials (the console-generated template already
includes these; remember them when adding manually):

- Mount the **same** `./data/share_workspace` shared volume as main
- Environment variable `HERMES_WRITE_SAFE_ROOT=/share_workspace` (★ without
  it the worker cannot write the shared volume)
- The worker's model / endpoint are decided by **its own Hermes config**
  (`config.yaml` inside the container), use `hermes config` or
  hermes-webui to set it up — this platform does not proxy or inject it

## 2.5 New worker container: three mandatory configuration items

> Absorbed from the former per-container setup document at the
> release convergence (2026-10-15). After creating a new worker container
> (`coder` / `code-reviewer` / …) these three items MUST be applied;
> missing them makes the worker unable to run / persona-less /
> unroutable. **Model configuration is NOT in this checklist** — it is
> handled per environment and not standardised.

### 2.5.1 `approvals.mode: off` (workers run unattended)

**Why mandatory**: workers run headless; nobody answers command-approval
prompts. Under the default approval mode, a worker running a heredoc
verification command (e.g. `python3 - <<'EOF' …`) triggers an approval
request -> nobody responds -> **83–93 second approval timeout** -> the
worker gets stuck in a retry loop and never lands its output files.
(Real-world lesson: the first code-reviewer review run repeatedly hit
approval timeouts and needed 875 seconds to barely settle.)

**Fix**: append to `/opt/data/config.yaml`:

```yaml
approvals:
  mode: off
```

`mode: off` is hermes' pure-bypass source (`tools/approval.py`), safe
inside an isolated worker container — its only write root is pinned by
`HERMES_WRITE_SAFE_ROOT=/share_workspace`.

**Verify**: `grep -A1 '^approvals:' /opt/data/config.yaml` should show
`mode: off`, then restart the container.

### 2.5.2 `SOUL.md` role persona (hard constraint)

Every worker gets a role persona at `/opt/data/SOUL.md`. **A worker
without SOUL produces generic answers from the default Hermes persona
and cannot be used for Team Work.**

**Template (runtime source of truth)**:
`mw/soul_template.en.md` / `mw/soul_template.zh-cn.md` (selected by
`MW_LANG`, served by `GET /api/soul-template`). The template contains
two kinds of content:

- **REQUIRED (not removable)** — platform hard-constraint sections every
  worker must obey: platform worker identity (no kanban calls, no
  main-gateway calls); working-directory discipline (write only to the
  task directory); output file list (`required_files` must be produced);
  TODO.md status contract (`working` at start, `completed` when done —
  the only completion signal); the `.clarify.json` protocol (the
  platform's only worker->main communication channel); boundaries (no
  hermes commands / no modifying SOUL.md / no writing elsewhere).
- **Adjustable** — filled in or removed by the worker's creator: role
  definition, capability description, output habits, tone, etc.

Recommended flow when creating a worker: copy the soul template -> fill
the adjustable sections -> delete unused default capabilities -> deploy
to `/opt/data/SOUL.md` -> register in `mw/registry.json`.

**Deployment commands**:

```bash
# 1) persona
docker cp /tmp/soul.md <container>:/opt/data/SOUL.md
docker exec -u root <container> chown 10000:10000 /opt/data/SOUL.md

# 2) approvals off
docker exec <container> sh -c 'printf "\napprovals:\n  mode: off\n" >> /opt/data/config.yaml'
docker exec -u root <container> chown 10000:10000 /opt/data/config.yaml

# 3) restart to apply
docker restart <container>
```

**A worker's `/opt/data` is a docker named volume** (§4.6), so there is
no host directory to drop files into — `docker cp` and `docker exec`
still work, and they are the intended way to provision a worker. Compose
resolves the volume name against the project (the compose directory), so
the volume on disk is `<project>_hermes-<worker>`; use
`docker inspect <container> -f '{{range .Mounts}}{{if eq .Destination "/opt/data"}}{{.Name}}{{end}}{{end}}'`
to get the exact name instead of guessing it.

### 2.5.3 `mw/registry.json` registration (capability tags + routing)

After a worker goes online, mw's console must be able to route to it.
Add to `mw/registry.json`:

```json
{
  "<profile_name>": {
    "runner":  "api",
    "api_url": "http://hermes_<name>:8642",
    "api_key": "<hermes gateway api_key>",
    "scope": {
      "tags":     ["review", "code-quality", "code-read"],
      "excludes": ["code-write", "prod-deploy"]
    }
  }
}
```

| Field | Required | Description |
|------|------|------|
| `runner` | yes | fixed `"api"` (this platform only supports API mode) |
| `api_url` | yes | full address of the worker's hermes gateway |
| `api_key` | yes | the worker gateway's API key; shown redacted in the frontend list |
| `scope.tags` | no | capability tag array; reference for the LLM when picking an assignee; shown as badges on the Worker management page |
| `scope.excludes` | no | task types explicitly not accepted; a hard red line enforced by mw at launch (design.md §11.5) |

Tag naming advice: use the `domain-action` form (`code-write`,
`code-read`, `debug`, `review`, `test`, …); use the same names as the
workflow step's `required_role_tags` or matching will fail; the more
precise the better.

**Deploying registry.json** (hot-reloaded on every API call; restarting
`mw.console` also clears process-level caches):

> Note: the tracked `mw/registry.json` ships with placeholder `api_key` values
> (`CHANGE_ME_WORKER_API_KEY_…`). Replace them with each worker's real
> `API_SERVER_KEY` after deploy; the file is never committed with real secrets.

```bash
# edit mw/registry.json in the repo, then deploy to the main container
scp mw/registry.json <host>:/tmp/registry.json
ssh <host> 'docker cp /tmp/registry.json hermes_main:/opt/mwroot/mw/registry.json && \
  docker exec -u root hermes_main chown 10000:10000 /opt/mwroot/mw/registry.json && \
  docker exec hermes_main sh -c "pkill -f mw.console || true"'
```

**Verification checklist (before a new worker goes live)**:

- [ ] `/health` returns `{"status":"ok"}`
- [ ] `grep '^approvals:' /opt/data/config.yaml` -> `mode: off`
- [ ] `head -1 /opt/data/SOUL.md` is **not** the default Hermes persona (if it is, no persona was set)
- [ ] SOUL.md contains the 6 mandatory sections (`grep -c '^## ' /opt/data/SOUL.md` >= 6)
- [ ] Run one minimal card: the worker lands its output files (not killed by approval timeouts)
- [ ] Worker registered in `mw/registry.json` (`api_url` + `api_key` + `scope`)
- [ ] `GET /api/workers` shows the new worker with correct capability badges
- [ ] Pick a workflow step referencing the worker's tags; after launching Team Work it actually takes the card

## 3. Where to change things for special needs

| Need | Where to change |
|---|---|
| Change ports | `.env` -> `CONSOLE_PORT` (issue #109: the gateway no longer publishes a host port — it is container-internal `:8642` only) |
| Change Hermes image | `.env` -> `HERMES_IMAGE` (must be >= v2026.9.24, includes Kanban) |
| Change container name | `.env` -> `MAIN_CONTAINER_NAME` |
| Force container CLI (skip auto-detect) | n/a — install.sh calls `docker` directly. On macOS the docker-for-apple-container shim exposes the same `docker` surface; pick a different binary by editing `deploy/install.sh` only if your host does not have `docker` on PATH |
| Change model / endpoint / Hermes behaviour | **Hermes's own config** (`hermes config` or `config.yaml` inside the container); this platform does not proxy it. After change, `docker restart <container>` to apply |
| reaper period | add `MW_REAPER_INTERVAL` in compose (default 15s) |
| subagent waiting timeout (issue#13) | add `MW_REAPER_SUBAGENT_MIN` in `.env` (the default is defined by `mw/reaper.py::_subagent_wait_min` — read that function for the current value; `0` disables new behavior — reaper falls back to the old stuck threshold; note: even when `0`, reaper still classifies a `waiting_sub` card as in-progress per the contract timeout, so the 5-min TODO-missing cleanup does NOT apply to it) |
| SSE drain budget after a downstream disconnect (issue#63) | add `MW_PROXY_DRAIN_MAX_S` in compose (default 3600 s — upper bound on how long the console keeps draining the upstream gateway stream after the browser drops; `0` disables draining altogether — the rollback switch for issue#63) |
| Python interpreter for the console's subprocess bridges | add `PYTHON_BIN` in compose (default `/opt/hermes/.venv/bin/python3` — the interpreter the console uses for the cron bridge and the reaper probes; it must be the Hermes venv, because the console's own interpreter cannot import `cron.*`) |
| Hermes home directory (job store + ticker heartbeat) | add `MW_HERMES_HOME` in compose (default `/opt/data` — the per-profile Hermes home that holds `cron/jobs.json` and `cron/executions.db`; the cron bridge overrides `HERMES_HOME` for its subprocess so it reads the same store as the scheduler ticker) |
| hermes-webui port (optional WebUI embed; NOT this platform's console port — that one is `CONSOLE_PORT`, default 8700) | add `HERMES_WEBUI_PORT` in compose (default 8787 — one instance per host must pick a distinct port when running isolated consoles side by side; see §3.1 / the port-mapping notes below) |
| Worker needs a browser | see design.md §9: bring up a Chromium CDP container, worker sets `browser.cdp_url` |
| Worker needs a desktop GUI | see design.md §9: bare metal + peekaboo MCP / Hermes peer |
| Need a fuller WebUI | embed hermes-webui, see §3.1 below |
| Render worker live status as pixel animations | embed pixel-agents, see §3.2 below |

### 3.1 Embed hermes-webui (optional)

This platform's built-in console focuses on the "unified multi-agent
workflow entry"; if you need Hermes's **full Web frontend** (session
management, skill editing, model config, etc.), you can one-shot embed
[hermes-webui](https://github.com/nesquena/hermes-webui) running in the
same container as main.

```bash
# 1. Clone wherever you like
git clone https://github.com/nesquena/hermes-webui /opt/hermes-webui

# 2. Enable in .env (uncomment lines and edit values)
#    HERMES_WEBUI_DIR=/opt/hermes-webui
#    HERMES_WEBUI_PORT=8787
#    HERMES_WEBUI_PASSWORD=***

# 3. Re-deploy (idempotent, ready steps pass through in seconds)
./deploy/install.sh
```

What happens automatically after enabling:
- The project directory is mounted into the main container at `/webui`
- `entry.sh` detects `/webui/start.sh` exists -> auto-starts it as a
  watchdog (`HERMES_WEBUI_FOREGROUND=1` foreground mode, auto-restart on
  crash)
- Port mapping `HERMES_WEBUI_PORT` (default 8787), open
  `http://<host>:8787/` in browser
- `verify.sh` adds a "hermes-webui online" check

When not enabled (`HERMES_WEBUI_DIR` empty) nothing is touched: a
placeholder empty dir is mounted, entry detects no `start.sh` and skips,
zero overhead.

> Note: hermes-webui's first start builds its own Python venv (start.sh
> does this automatically); the first start may take an extra 1-2
> minutes — this is normal.

### 3.2 Embed pixel-agents (optional)

Render each worker's live status as pixel-character animations — coder
typing at a desk, raising a hand bubble when stuck on clarification,
idle characters walking around the office. This platform plugs into
the same optional sidebar as hermes-webui: when the switch is off,
**zero process, zero port, zero resource usage**.

```bash
# 1. Prepare the pixel-agents project root that already has npm install
#    (not the source tree, not the bare package directory)
mkdir -p ~/pixel_office && cd ~/pixel_office && npm install pixel-agents

# 2. Enable in .env
PIXEL_OFFICE_ENABLED=1
PIXEL_OFFICE_DIR=/home/<user>/pixel_office      # the npm-install dir above

# 3. Re-deploy (idempotent)
./deploy/install.sh
```

What happens automatically after enabling:
- The project directory is mounted **read-only** into the main container
  at `/pixel-office`
- `entry.sh` detects `/pixel-office/node_modules/pixel-agents/dist/cli.js`
  -> auto-starts as a watchdog, bound to container-internal
  `127.0.0.1:3199` (**not exposed externally**)
- Console same-origin proxies `/pixel/` to embed pixel-agents (browser
  only sees the console's one port)
- The "Settings" page -> "pixel-agents" checkbox is a second-level switch;
  the "pixel-agents" sidebar tab toggles when saved
- `verify.sh` check items: sidecar process online + console same-origin
  proxy `/pixel/` returns 200

When not enabled (`PIXEL_OFFICE_ENABLED=0` or empty) nothing is touched:
compose mounts a placeholder empty dir, entry detects no `cli.js` and
skips, **zero overhead**.

**Mount-dir validation**: `PIXEL_OFFICE_DIR` MUST be the **project root**
where `npm install pixel-agents` was run (containing
`node_modules/pixel-agents/dist/cli.js`). Mounting bare
`node_modules/pixel-agents` or mounting an un-installed source repo
makes cli.js fail at startup with `Cannot find module '@fastify/cors'`
(the dependency is installed in the parent `node_modules`, the whole
dir must be mounted together). `install.sh` validates this at start,
errors fail-fast with the correct prep instructions.

**Architecture boundary** (consistent with `mw`'s deterministic pipe):
- Browser only sees the console's `:8700` one port; pixel-agents is only
  bound to container-internal `127.0.0.1`
- Same-origin proxy preserves the Host header -> pixel-agents WS same-origin
  gate passes
- Authority: reaper reads the unique working worker from
  `ledger.team_work_runs.current_card_id` (not kanban status=running); the
  blocked signal comes from `harvest_status`, not kanban block
- Character session `mw-<worker>` = `session_id`; tokens are read live by
  bridge from `~/.pixel-agents/servers/<pid>-<port>.json`, never hardcoded

**Known limitation (does not affect function, just be aware)**:
synthetic sessions have no JSONL transcript, pixel-agents' replayable
tool state is persisted by transcript. So **late-join viewers who
reconnect will see the character start from Idle**, the next reaper
tick (<= 15s) will receive a live `PreToolUse` frame and refresh to
working. Viewers who were online before opening the page don't have
this issue.

After changing config, re-deploy with `./deploy/install.sh` — the new
macOS-compatible topology (`6f59c63`) generates the `.runtime-skills/` and
`.runtime-soul/` staging dirs and applies the new mount set via stop+rm +
`compose up`; a bare `docker compose ... up -d` does neither (it fails on a
first run with no `.runtime-*` staging, and silently keeps the old mounts on
an upgrade).

## 4. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `install.sh` says "missing .env" | .env not copied | `cp .env.example .env` and fill ★ items |
| Container fails to start with Permission denied | Volume owner != container UID (10000) | `chmod -R 777 data` then re-run `install.sh` (the kanban.db write inside the container still needs the parent dirs to be world-writable; `chmod -R 777` is the first thing install.sh does, but it itself can fail on a 10000-owned tree) |
| `install.sh` aborts on `cp ... Permission denied` (skills/SOUL bind-mount targets) | upgrade over a running container: `01-hermes-setup` chowned bind dirs to uid 10000, host cp cannot overwrite (issue #53 BUG-53-A) | **s4d-fix2 (2026-10-17)**: skills/SOUL are now read-only bind mounts of the staging copies install.sh generates from the repo source (`deploy/skills/<sk>/SKILL.<LANG>.md`, `docs/main-soul.<LANG>.md` → `deploy/.runtime-skills/<sk>/SKILL.md`, `deploy/.runtime-soul/SOUL.md`) — there is no host-side cp for these paths anymore, so Permission denied cannot happen on the bind mount itself. Editing the repo source alone does not change the running copy: re-run the installer to regenerate the staging. If you still see this symptom on an **older** s4b-fix install, upgrade to a commit ≥ s4d-fix2 (`git pull && ./deploy/install.sh`). For everything **else** under data/ (e.g. settings.json, registry.json, share_workspace), the existing `chmod -R 777 data` in §3 is the first fix; on machines without host write access to 10000-owned dirs, the historical `sudo chown -R $USER data` one-time command is the fallback. |
| Console not ready after 60s | Image not pulled / port occupied | `docker logs hermes_main` to see the error |
| Cron data source returns 503 (the `error` field is the fixed string `cron 数据源不可用` on every locale — the backend does not localize this message) | cron bridge subprocess exited non-zero | read the tail of the console log: `./data/mw_data/logs/console.log` (host path) — look for lines containing `[cron-bridge] rc=`; the same file is `/mw_data/logs/console.log` inside the container. The line only falls back to stderr (`docker logs hermes_main`) when the log file itself is not writable. |
| Chat sent, no reply | Model endpoint unreachable or wrong key | This is a Hermes config issue: check `model.base_url` in container's `config.yaml`, `curl <base_url>/models` for self-test |
| Worker write to shared volume rejected | missing `HERMES_WRITE_SAFE_ROOT` | add that env to the worker compose |
| Task dispatched but always pending | profile not registered / worker unreachable | check worker in console; `curl <worker_api>/health` |
| pixel-agents sidecar fails with `Cannot find module '@fastify/cors'` | mounted dir is bare package or source | `PIXEL_OFFICE_DIR` must be the `npm install pixel-agents` **project root** (contains `node_modules/`) |
| pixel-agents tab visible but iframe stuck on Idle | page just opened, no first reaper tick yet | wait <= 15s; or confirm ledger has an ACTIVE run with non-empty `current_card_id` (`docker exec hermes_main sqlite3 /mw_data/ledger.db "SELECT * FROM team_work_runs WHERE status='ACTIVE'"`) |
| `install.sh` aborts with "Invalid LANG='...'" | `LANG` in `.env` is not `en` or `zh-cn` (design.md §10 hard validation) | edit `.env` -> set `LANG=en` (default) or `LANG=zh-cn`, then re-run `./deploy/install.sh` |
| Console appears in the wrong language after deploy | `data/mw_data/deploy_lang` does not match the container's `MW_LANG` (e.g. install ran with `LANG=en` but entry script picked up an old value) | edit `.env` -> set desired `LANG=`, run `./deploy/install.sh` to refresh `deploy_lang` (do not edit the file directly), then restart the main container: `docker restart hermes_main` |
| `verify.sh` still prints Chinese after setting `LANG=en` | an older deploy left a stale `.env` or a stale deploy_lang file | verify the source-of-truth order: `.env` -> `data/mw_data/deploy_lang` (written by install) -> `MW_LANG` (read by entry.sh). Re-run `LANG=en ./deploy/install.sh` then `./deploy/verify.sh` |
| End-user sees the deployment language instead of their chosen language | user preference missing from `localStorage` (private mode, different machine) | browser-side switch persists per-origin; verify by opening the same machine in a fresh tab. Server-side messages follow `MW_LANG` until user clicks the toggle (design.md §10). |
| `verify.sh` shows a red item | the corresponding service didn't start | follow the prompt and check `docker logs`, fix and re-run `verify.sh` |
| Console reports `gateway unreachable` on macOS/colima | a socket inode on the virtiofs-shared data dir became unreachable in the guest (`Errno 95`); image `>= 0.22.x` (measured: v0.22.4) turns it into a startup abort | see §4.5 — the deploy path now keeps `/opt/data` on a named volume; for an old topology, clear the stale socket on the host and re-run `./deploy/install.sh` |
| Most workers show as *offline* in the console but their containers are up | the registered `api_url` points at a published host port, so calls go through `docker-proxy` inside the VM, which drops them under load; also check the VM memory (§4.7) | see §4.6 — register `http://hermes_<name>:8642` (container-internal address) |

## 4.5 macOS/colima: the control socket on a virtiofs-shared data dir (issue #82)

> **Status 2026-10-22: fixed in the deploy path.** `main`'s `/opt/data` is now
> a docker **named volume** (ext4 on the VM disk) instead of a host bind
> mount, and `main` + every worker share the `simple-dot` network so they
> address each other over container-internal addresses. The E95 failure and
> the "workers show as offline" symptom are both gone. The analysis below is
> kept because it explains *why* the topology is what it is — do not undo it.

**Scope.** Hosts running Docker via **colima** (or any VM setup whose data
directory is shared into the VM over **virtiofs/FUSE**), which is the
colima default (`mountType=virtiofs`). The failure is specific to a
**socket inode on a virtiofs-shared directory**: the host can see the
socket perfectly well while the guest can no longer `stat`/`unlink` it.

**Symptom set** (all three usually appear together):

- The console reports `gateway unreachable` when sending a chat message.
- (pre-#109) The host-side port `8642` answers with an empty reply / `503`
  (colima's ssh port forward has no live backend to forward to), while
  `curl` to the port is *not* refused — the listener you see on the host
  is the forwarder itself. (Since issue #109 the gateway publishes no
  host port; this bullet is kept as pre-#109 historical cause analysis.)
- Inside the container, `gateway-starts.log` grows in an equal-interval
  loop (one restart every ~1–9 minutes), and most of the time **no
  gateway process is alive** (`docker exec hermes_main ps` shows none).

**Root cause, one line.** The gateway's control socket
(`/opt/data/gateway.sock`) is bound on every start, and on a
virtiofs-shared directory a socket inode that the guest has already
looked up can become **permanently unremovable inside the guest**:
`os.lstat` and `os.unlink` on that inode both fail with
`[Errno 95] Operation not supported` (root included, and from a fresh
mount session too), so the bind-over-existing also fails with the same
E95 — while the **same virtiofs mount binds a brand-new socket name
without trouble** (measured as `root` and as the gateway's own user).
virtiofs is therefore **not** "unable to host AF_UNIX"; the poison is
the **socket inode that the guest's dcache no longer resolves**, it is
**persistent** (it survives container restarts, image swaps and
remounts) and it is **version-independent** (image `v2026.9.21`,
`v2026.9.24` and `v0.22.4` all fail identically on the same poisoned
directory). What differs between images is only what happens *after*
the bind failed: `v2026.9.x` logs `Gateway control socket failed to
start (non-fatal)` and keeps serving degraded, whereas upstream
`v0.22.4` made the missing control listener a **startup prerequisite**
(`run_bootstrap.py` raises `RuntimeError("gateway session bootstrap
control listener unavailable")`, upstream #92091), so every s6 restart of
the slot dies at startup.

**Why it "only lasts a few minutes".** The gap is a *lifecycle* window,
not a timer. A healthy gateway eventually exits or is replaced
(`gateway run --replace`); that exit leaves the socket inode behind, and
the *next* instance then walks into it:

```python
if bind_path.exists():        # Python's Path.exists() swallows OSError -> False
    bind_path.lstat()          # so this is never reached
    bind_path.unlink()         # ... and the poison is never removed
start_unix_server(path=...)    # bind on the stale inode -> E95 -> gateway dies
```

`cleanup_files()` compounds it: it wraps the exit-time unlink in
`contextlib.suppress(OSError)`, so a failed cleanup is silently ignored
and the poison survives into the next start. A pre-clean ("remove the
socket before starting") only helps for the *current* cycle — it cannot
prevent the next exit/replace from producing a new poisoned inode.

**Why a named volume fixes it.** A docker named volume lives on the VM's
ext4 disk, where AF_UNIX inodes behave normally. The worker/coder
containers already used a named volume and **never** reproduced E95 on
any image — the storage backend, not the image version, is the deciding
factor. `main` was the only service still on a virtiofs bind mount.

**The fix, as implemented.** `deploy/main.compose.yaml` mounts
`main_hermes_main:/opt/data` (a named volume) instead of
`./data/main:/opt/data`; `deploy/install.sh` creates that volume and
seeds it from `data/main` on first deploy (idempotent — a non-empty
volume is never overwritten). The host-side `data/main` directory stays
in the repo layout, but it is now only the *seed source*, not a live
bind mount.

**Seeding and ownership.** Two details decide whether the seed actually
happens, and both are easy to get wrong:

- The deploy image ships an **s6-overlay `ENTRYPOINT`**. A helper run
  must override it — `docker run --entrypoint sh <image> -c '...'` —
  otherwise docker starts the whole service stack, never runs the
  command, and the seeding silently does nothing while still exiting 0.
- The gateway runs as uid **10000**. A volume seeded from the host copy
  carries the host's ownership, and directories the image bootstrap
  creates as root (e.g. `pending_messages/`, `runtime/bot_live_delivery/`)
  stay root-owned — the gateway then logs `Operation not permitted` for
  them and silently drops bot/mailbox deliveries. `deploy/install.sh`
  therefore runs `chown -R 10000:10000` inside the volume after seeding.

Check both after a deploy:

```bash
docker run --rm --entrypoint sh -v mw-hermes_main_main_hermes_main:/vol \
  <deploy-image> -c 'ls -ldn /vol /vol/pending_messages /vol/runtime/bot_live_delivery'
# expect: every uid/gid to read 10000 10000

docker logs hermes_main 2>&1 | grep -c 'Operation not permitted'   # expect 0
```

**Forensics criterion** (confirm or rule out this cause on any suspect
machine in two greps). The two log files must **exist** for the counts to
mean anything — `grep -c` on a missing file also prints `0`:

```bash
ls -l "$HERMES_HOME/logs/gateway.log" "$HERMES_HOME/logs/gateway-exit-diag.log"  # both must exist
grep -c "Errno 95" "$HERMES_HOME/logs/gateway.log"                # > 0
grep -c "bootstrap control listener unavailable" \
    "$HERMES_HOME/logs/gateway-exit-diag.log"                      # > 0
```

Both files present **and** both counts non-zero on a virtiofs-backed host
means you hit exactly this cause. If either file is **missing**, the
criterion does not apply (the gateway never wrote those logs) — do not
conclude "not this cause" from a `0` count on a file that is not there.

**Recovery for a host that is still on the old topology.** The stale
socket file must be removed **from the macOS host** — inside the guest it
cannot be unlinked (E95), for any user including root. Removing it is
enough to make the gateway bind again on the same image; rolling the
image back is *not* required for recovery, only a way to keep serving
degraded while the socket is still poisoned.

```bash
# 1) remove the orphaned socket from the HOST (guest-side rm always fails with E95).
#    Run from the repo root: ./data/main is the host side of the old bind mount.
rm -f ./data/main/gateway.sock

# 2) re-run the installer — with the fixed compose this migrates /opt/data
#    onto the named volume and seeds it from data/main
./deploy/install.sh
```

**Last resort** (if the host-side `rm` is refused too): `colima stop &&
colima start` restarts `virtiofsd` and drops the stale inode view. It
stops every container on the host, so restart them one by one with
`docker start` afterwards, and get the operator's approval before
running it.

**Success criteria (all four).** The success token is
`Gateway control socket listening at` — note the **capital G**, matching
the image output byte for byte (`control_socket.py:167`); the lowercase
spelling never matches anything:

```bash
# 1) exactly one gateway process
docker exec "$MAIN_CONTAINER_NAME" sh -c 'pgrep -fc "gateway run"'    # expect 1

# 2) control socket bound — capitalised token, and the file must exist
docker exec "$MAIN_CONTAINER_NAME" sh -c '
  if [ -f /opt/data/logs/gateway.log ]; then
    echo "listening_count=$(grep -c "Gateway control socket listening at" /opt/data/logs/gateway.log)"
  else
    echo "FAIL: gateway.log does not exist - the gateway never started"
  fi'                                                    # expect >= 1

# 3) no new E95 (absence of the log must not be read as "clean")
docker exec "$MAIN_CONTAINER_NAME" sh -c '
  [ -f /opt/data/logs/gateway.log ] && grep -c "Errno 95" /opt/data/logs/gateway.log || echo "log-absent"'

# 4) functional: live socket file + api_server answering
docker exec "$MAIN_CONTAINER_NAME" sh -c 'ls -l /opt/data/gateway.sock'   # expect srw-------
# issue #109: gateway 8642 无宿主映射, 探测在容器内进行 (verify.sh 同口径)
docker exec "$MAIN_CONTAINER_NAME" python3 -c 'import urllib.request,urllib.error;
try: print(urllib.request.urlopen("http://127.0.0.1:8642/api/sessions",timeout=8).status)
except urllib.error.HTTPError as e: print(e.code)'   # expect 401 (no key) or 200 with key
```

**D-2 upstream dependency.** The upstream image should give
control-socket bind failures an exit: an E95 / EOPNOTSUPP failure should
join the existing `sun_path`-overflow fallback chain (XDG_RUNTIME_DIR /
/tmp), or offer an explicit `HERMES_CONTROL_SOCKET_DIR`-style override.
This is still worth filing upstream, but it is no longer a deployment
blocker: the named-volume topology removes the E95 condition entirely.

**Do not "fix" this in the compose.** Adding `HERMES_CONTROL_SOCKET_*` or
`XDG_RUNTIME_DIR` env vars does nothing here: reading the image code
shows the server-side socket path is resolved unconditionally and
`XDG_RUNTIME_DIR` is consulted **only** in the `sun_path` overflow
branch (paths longer than 100 bytes) — `/opt/data/gateway.sock` is 24
bytes and never takes that branch. Such env entries are dead
configuration and are deliberately kept out of `main.compose.yaml`.

### 4.6 Shared worker network `simple-dot` (2026-10-22, issue #82)

`main` and every worker join one docker network, `simple-dot`, created by
`deploy/install.sh` (`docker network create simple-dot`, idempotent) and
declared `external: true` in the compose files. Each service gets its
**own unique alias** — the container name, e.g. `hermes_coder`,
`hermes_tester` — so services address each other over container-internal
addresses:

```bash
# from inside any container on the network
curl -sS -o /dev/null -w '%{http_code}\n' http://hermes_coder:8642/health   # 200
```

**Why this matters.** The alternative — published host ports
(`http://<your-lan-ip>:8643`) — routes every call through `docker-proxy`
*inside* the VM. Under memory pressure that proxy drops connections, and
the web console then reports most workers as **offline** even though
their containers are up and healthy. Container-internal addressing has no
proxy in the path.

**Rules.**

- **Never share one alias across services.** A shared alias (e.g. `hermes`)
  resolves inside each container's own namespace, so a cross-container call
  to `hermes` silently reaches the *wrong* service. One unique alias per
  service, always the container name.
- **Register the worker with its internal URL.** In the console's
  "add worker" form, set `api_url=http://hermes_<name>:8642`. The
  console's online/offline badge is derived from probing that URL
  (`mw/console.py` `_health_probe`), so a host-port URL there is what made
  the badge lie.
- **The `simple-dot` network must exist before `compose up`.** It is
  declared external, so compose will not create it and will fail with
  "network simple-dot not found" if it is missing. `deploy/install.sh`
  creates it; a hand-run `docker compose up` needs
  `docker network create simple-dot` first.
- **The compose SERVICE name must equal the container name.** Compose
  registers the service name as a network alias *automatically*, on top of
  any alias you declare explicitly. A service named `hermes` therefore
  keeps answering to `hermes` no matter what the `aliases:` list says —
  which is how the shared alias survived a first migration that only
  edited the `aliases:` block. `deploy/main.compose.yaml` names its
  service `hermes_main` and the worker template names its service
  `hermes_{name}` for exactly this reason.

**Verify the alias layout after every deploy.** The shared alias must not
resolve from any container, and each service must answer to its own name:

```bash
# 1) no container may list a bare "hermes" alias
for c in $(docker ps --format '{{.Names}}'); do
  echo "$c -> $(docker inspect "$c" -f '{{range .NetworkSettings.Networks}}{{.Aliases}} {{end}}')"
done
# expect: only <container_name> entries, no bare "hermes"

# 2) from inside a container, the shared alias must NOT resolve
docker exec hermes_se sh -c 'getent hosts hermes || echo "OK: no shared alias"'

# 3) each service must answer on its own internal address
docker exec hermes_main python3 - <<'PY'
import socket, urllib.request
for n in ["hermes_coder", "hermes_se", "hermes_pm", "hermes_tester",
          "hermes_doc_audit", "hermes_code_reviewer", "hermes_ux_designer"]:
    ip = socket.gethostbyname(n)
    code = urllib.request.urlopen(f"http://{n}:8642/health", timeout=8).status
    print(f"{n:<24} {ip:<15} {code}")
PY
```

### 4.7 Sizing the VM (2026-10-22, issue #82)

Eight gateway processes (main + 7 workers) need roughly 220–260 MiB RSS
each. On colima the **default VM memory is 2 GB**, which is far too small
for that fleet: the whole set OOM-kills, workers restart in a loop
(measured: 856–870 `Previous gateway life` entries per worker) and drop
out of the web console. Give the VM room before deploying the fleet:

```bash
colima stop
colima start --cpu 4 --memory 8 --disk 100
```

Check what the VM actually got — `colima status` reports the *config*,
the VM's own view is authoritative:

```bash
docker run --rm -v /sys/fs/cgroup:/cgroup:ro alpine   sh -c 'grep -E "MemTotal|MemAvailable" /cgroup/memory.max /cgroup/memory.current 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null'
```

**Symptom of an undersized VM:** `suspected_oom=True` in the gateway
logs, hundreds of `Previous gateway life` entries, and workers that
answer `curl` on the first try and then time out. Raising the VM memory
is a prerequisite for the whole fleet being online — it is a separate
fault from the socket problem above, and fixing one does not fix the
other.

## 5. Upgrade / rollback

```bash
git pull                              # pull updates (middleware mounts code directly, restart takes effect)
docker restart hermes_main
./deploy/verify.sh
```

Restart is only enough for pure code changes. If the pull touched
`deploy/main.compose.yaml` (e.g. s4d-fix2 added the read-only SOUL/SKILL
mounts, m1-impl re-homed them to directory binds) or `deploy/install.sh`
provisioning, **restart does NOT apply the new mount topology** — re-run
the installer instead, which recreates the container (stop+rm then
compose up; m1-impl dropped `--force-recreate` because the Apple
container docker shim does not support that flag):

The same applies when the pull touches the **SOUL or SKILL sources**
(`docs/main-soul.*.md` or `deploy/skills/**`): the container does not mount
those repo files, it mounts the staging copies that `deploy/install.sh`
regenerates from them on every run. A pull plus a restart leaves the
previous text in place, so the installer has to run for the new text to
reach the container.

```bash
git pull
./deploy/install.sh    # stop+rm existing container, then docker compose up -d
./deploy/verify.sh
```

**Console frontend/backend upgrade is atomic.** `mw/` is a single
directory bind mount, so `git pull` + restart always applies the Python
proxy and `ui/dist` together — there is no "backend first" intermediate
state to schedule around. The only mixed-version combination users can
hit is **stale browser cache**: a browser holding an older `ui/dist`
bundle against the current backend. Clear the cache (hard reload) when
the chat page still polls `/messages` with a full-transcript payload.

**Only `ui/dist` is tracked; `mw/ui_dist` is a deploy-time copy**
(issue #125, route A). The repo contains exactly one frontend build
artifact: `ui/dist/index.html`. `install.sh` copies it to `mw/ui_dist/`
and `data/share_workspace/ui_dist/` on every run and rewrites their
`.stamp.json`; neither copy is in git (`.gitignore` covers
`mw/ui_dist/`). Consequences:

- Editing `ui/src` still requires `cd ui && npm run build` and
  committing the rebuilt `ui/dist/index.html` — the tracked-artifact
  discipline is unchanged.
- Never use a repo/worktree `mw/ui_dist/index.html` as UI evidence. It
  is a local runtime copy whose freshness depends on when you last ran
  `install.sh`, and before this change it silently drifted for months
  (that was issue #125). To check what the console actually serves,
  compare md5 against the live endpoint:

  ```bash
  md5sum ui/dist/index.html
  curl -s http://127.0.0.1:${CONSOLE_PORT:-8700}/ | md5sum   # must match
  ```

- `deploy/check_dist_sync.sh` is the local gate for this (wired into
  `deploy/verify.sh`): it fails if `mw/ui_dist` is tracked again, if the
  local runtime copy drifts from `ui/dist` in either direction, or if
  `ui/dist/index.html` went missing.

When in doubt, `./deploy/install.sh` is always safe (idempotent).

All data lives under `./data/`, separate from code; migrating to a new
machine is as easy as copying that directory across.

---

*Architecture principles and test evidence are in `design.md`. Deployment
operations are authoritative for this manual.*
*Team Work flow details, workflow schema, step skip, and worker registry
schema are in `design.md` §11.*
*New worker container essentials are in §2.5 above.*

## 5.1 vite cache rebuild (important)

If the built `dist/index.html` doesn't reflect recent source changes
(same size but stale code), clear the cache and rebuild:

```bash
cd ui
rm -rf dist node_modules/.vite
npm run build
```

**Verdict method**: rely on browser actual SVG / React component render
result (runtime attribute values), don't grep the `dist` static file
(vite minifies variable names).

**Don't use a worktree `mw/ui_dist/index.html` as the verdict input**
(issue #125): it is a deploy-time copy of `ui/dist`, rebuilt by
`install.sh`, and it can lag behind `ui/dist` between installs. Judge the
build against `ui/dist/index.html` (the single tracked artifact) or
against the live console endpoint, and run `bash deploy/check_dist_sync.sh`
if the two copies disagree.

## 5.2 workflow changes — bidirectional sync

The repo's `mw/workflows.yaml` is the source of truth. After editing,
bidirectional sync is mandatory:

- Local -> 170: scp to /tmp -> docker cp -> chown
- 170 -> local (debugging from container side):
  `docker cp hermes_main:/opt/mwroot/mw/workflows.yaml /tmp/`

## 5.3 Temporary debug item cleanup

Temporary workflows created with PUT during debugging (e.g.
`hotfix-cycle`, etc.) must be DELETEd when done, otherwise they pollute
the Workflow management page list.

---

> **i18n note (v1.0)**: Platform runtime bilingual support is selected at
> install time via `LANG` -> `MW_LANG` (§1.5). The SOUL templates ship as
> `docs/main-soul.en.md` / `docs/main-soul.zh-cn.md`; see `design.md` §10
> for the full mechanism.
