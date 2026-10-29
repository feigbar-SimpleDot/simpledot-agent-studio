# SimpleDot Agent Studio

**English** | [`中文`](README.zh-cn.md)

> A multi-agent long-running task management tool built on Hermes' native
> Kanban system.

## What this is

A multi-container agent matrix on a task bus: **Main** drives the
workflow via Kanban, **Worker** containers run in isolation, the
middleware (`mw`) provides **deterministic code** (not LLM) for
validation, dispatch, reaping, and relay, and the Web console makes the
whole thing observable and operable.

## Design philosophy (the 1A boundary, set 2026-10-03)

> Determinism comes from "let the LLM do only the right thing, and make
> it unable to do anything else" — not from "make the LLM do the right
> thing".

- **main drives Kanban; `mw` is only a pipe.** Creating / claiming /
  completing cards is entirely main's job; `mw` validates, ledgers,
  launches workers and relays — it runs no external command and writes
  no project file.
- **Worker self-reports are never state input.** Verdicts rely only on
  physical evidence: the TODO.md protocol status field + product files
  + real tool calls in the transcript.
- **Reaping reaps archives, not living things.** The session archive
  survives independently of the process; a crashed worker resumes from
  the session anchor with zero context loss.
- **Failures only BLOCK — never auto-retry.** Retrying is main's
  explicit decision, to prevent duplicate work.
- **Strictly serial.** One card in flight; main completes, then
  dispatches the next.
- **Workflow management lives here; agent management lives there.**
  Each worker's model / SOUL / skills are managed by the user with
  hermes-webui or the Hermes TUI — this platform deliberately does not
  proxy agent management, to keep the isolation boundary intact.

## Quick start

```bash
cp .env.example .env && vi .env    # fill API_SERVER_KEY / model endpoint
./deploy/install.sh                # one-shot: main (middleware + console)
./deploy/verify.sh                 # all green = ready
open http://<host>:8700            # console: chat / kanban / files / workers
```

## Where to read more

| Topic | Doc |
|---|---|
| Deploy: three-step deploy, add workers, special needs, troubleshooting | [DEPLOY.md](DEPLOY.md) |
| Full architecture design (1A boundary, reaping flow, deployment constraints) | [design.md](design.md) |
| Team Work flow: boundaries, workflow/registry schema, step skip, TODO.md contract | [design.md §11](design.md) |
| i18n v1.0: deploy-time `LANG`, `GET /api/lang`, live Settings switch | [design.md §10](design.md) |
| Main Agent platform-default SOUL | [docs/main-soul.en.md](docs/main-soul.en.md) / [docs/main-soul.zh-cn.md](docs/main-soul.zh-cn.md) |
| Worker SOUL runtime template | `mw/soul_template.en.md` / `mw/soul_template.zh-cn.md` |
| Changelog (merged changes, newest first) | [CHANGELOG.md](CHANGELOG.md) |
| Simplified-Chinese README | [`中文`](README.zh-cn.md) |

## Video tutorials

- [Getting started walkthrough](https://youtu.be/2eWsBmwHzT0?si=NDQCfmLtZHU50LLt)
- [Advanced walkthrough](https://youtu.be/ymXLxXRYZWk?si=K2SMFhe5qWcQah89)

## Acknowledgements

This platform stands on the shoulders of these open-source projects:

| Project | Role here |
|---|---|
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | The agent runtime this platform orchestrates — gateway, native Kanban, skills, session model. |
| [gufo](https://github.com/gufo-org/gufo) | Open-source Strix Halo AI engine — powers the local models behind this setup. |
| [halogen-flash-server](https://github.com/peonist-ai/halogen-flash-server) | Fast local Qwen3.8-Flash-Next inference for agent workloads — the other half of the local-model stack. |
| [Hermes WebUI](https://github.com/nesquena/hermes-webui) | Web interface for Hermes Agent; how users manage each worker's model / SOUL / skills. |
| [pixel-agents](https://github.com/pixel-agents-hq/pixel-agents) | Optional pixel-office watchdog, surfaced in the console through the same-origin `/pixel` proxy. |
| [React](https://github.com/facebook/react) + [i18next](https://github.com/i18next/i18next) | Console UI and bilingual (en / zh-cn) rendering. |
| [marked](https://github.com/markedjs/marked) + [DOMPurify](https://github.com/cure53/DOMPurify) | Markdown rendering with XSS sanitization in the console. |
| [Vite](https://github.com/vitejs/vite) + [Tailwind CSS](https://github.com/tailwindlabs/tailwindcss) | Console build tooling and styling. |

Thanks to all their maintainers.

## Support

Enjoying this project? Consider [Buy Me a Coffee](https://buymeacoffee.com/feigbar) to support its development.
