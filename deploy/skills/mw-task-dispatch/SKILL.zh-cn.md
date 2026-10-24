---
name: mw-task-dispatch
description: "Dispatch Team Work tasks via the mw launch endpoint. Use when asked to delegate, assign, distribute, or hand off work to a worker/coder/agent, run tasks in the agent matrix, orchestrate multi-agent jobs, or when the user mentions 派发/分发/委派/多智能体任务/Team Work."
version: 3.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [Agent, Matrix, Dispatch, Middleware, Kanban, TeamWork]
---

# MW 任务分发（Agent 矩阵 · 1A 边界 · Team Work 顶层对象）

本环境是多容器 Agent 矩阵。**1A 边界：卡的生命周期归你（main），mw 只起 worker。**

⚠️ **下发必须走 `POST /api/team-work/launch`，不要用 `mw.cli dispatch`。**

launch 是 ledger 里 `team_work_runs` 的**唯一触发点**：你建好卡的那一刻，
launch 同步把 run 写进账本（`run_id` + 每张卡的 `team_work_run_id` + 发起
session + 当前卡槽），**Team Works 面板立刻可见、reaper 立刻纳入扫描**。

`mw.cli dispatch` 绕过 launch，写出的卡 `team_work_run_id=NULL`，
Team Works 面板看不到、reaper 直接 SKIP 不结算（孤儿卡，链是死的）。

## 何时使用

- 用户要求"让 worker / coder / 某个 agent 做 X"
- 任务需要隔离执行、可审计的结算
- 用户提到：派发、分发、委派、多智能体、任务矩阵、worker、Team Work

## 如何下发（三步，顺序固定）

⚠️ 你的终端会话 PATH 可能不含 venv，**必须用绝对路径**：

```bash
export PATH=/opt/hermes/.venv/bin:$PATH   # 含 python3(带pydantic) 与 hermes

# ① 你自己建 kanban 卡（mw 不建卡）—— 一个 step 一张卡
TID=$(hermes kanban create "任务标题" --assignee coder --body "自包含任务描述" \
      | grep -oE 't_[a-z0-9]+' | head -1)

# ② 你自己 claim 这张卡
hermes kanban claim "$TID"

# ③ 一次 launch 提交全部 step（launch 建 run + 派第一张）
curl -s -X POST http://127.0.0.1:8700/api/team-work/launch \
  -H 'Content-Type: application/json' \
  -d '{
    "initial_task": "<需求原文>",
    "session_id": "<你的 Hermes session_id>",
    "project_workspace": "/share_workspace/projects/<name>",
    "steps": [
      {"step_id":"s1","title":"步骤1标题","task_id":"<卡1 id>","assignee":"coder",
       "required_files":["out.py"],"timeout_seconds":600},
      {"step_id":"s2","title":"步骤2标题","task_id":"<卡2 id>","assignee":"code-reviewer"}
    ]
  }'
```

launch 响应：

```json
{"ok": true, "run_id": "<16hex>", "card_count": 2, "dispatched": "<第一张卡 id>",
 "note": "已建 run + 登记全部卡 + 派发第一张。后续卡: main complete 当前卡后 POST /api/team-work/<run_id>/advance 推进。"}
```

**记住返回的 `run_id`**——后续每张卡推进都要用，Team Works 面板按它显示。

### `session_id` 填什么

填**你自己的 Hermes session_id**（reaper 靠它把 worker 报告送回你，
`POST /api/sessions/<sid>/chat`）。

- 从控制台「开启 Team Work 任务」按钮进来的：请求文本里已注入
  `session_id: "<sid>"`，**直接用那个值**。
- 不确定自己的 session_id：读你系统提示里的 session id（形如
  `api_<时间戳>_<8hex>`），或从控制台会话列表复制。
- **绝不能留空**——留空 worker 报告无处投递，你会"失联"。

### 推进下一张卡（严格串行的关键）

当前卡结算（你 `hermes kanban complete` 且 ledger 里 `current_card_id` 已清）后：

```bash
curl -s -X POST http://127.0.0.1:8700/api/team-work/<run_id>/advance \
  -H 'Content-Type: application/json' \
  -d '{"task_id":"<下一张卡 id>","target_profile":"coder","title":"步骤2标题",
       "required_files":["review.md"],"timeout_seconds":600}'
```

advance 会：校验上一张已结算（`current_card_id` 为 NULL）→ 占槽 → 派 worker。
上一张没结算就 advance → **409 拒**（串行铁律，防并行施工）。

### 中途追加卡片（回流场景：审查打回建修复卡 / 复审卡）

链跑到一半要加新卡（如 code-review 判 CHANGES_REQUESTED，要建修复卡 +
复审卡），**走这个端点登记，绝不在容器里徒手调 ledger 函数**：

```bash
curl -s -X POST http://127.0.0.1:8700/api/team-work/<run_id>/cards \
  -H 'Content-Type: application/json' \
  -d '{"cards":[
    {"task_id":"<你建好的卡id>","title":"安全修复","assignee":"coder",
     "required_files":["FIX_NOTES.md"],"timeout_seconds":1800},
    {"task_id":"<复审卡id>","title":"安全复审","assignee":"code-review",
     "required_files":["REVIEW2.md"]}
  ]}'
```

- 前置照旧：卡先 `hermes kanban create`，本端点**只登记，不占槽，不派发**。
- 登记后照常走 `advance` 逐张派（严格串行不变）。
- 追加卡自动继承 run 的 `project_workspace`（产物根），required_files 不会
  错落到 task 目录。
- 幂等：重复登记同一张卡不重复计数；一张卡不能挂两个 run。
- 返回 `{registered, already_registered, skipped, card_count}`，逐项核对。

### 字段规则（违反会被 400/422 拒收）

| 字段 | 规则 |
|---|---|
| `initial_task` | 必填，原始需求（回显用） |
| `session_id` | **必填**，你自己的 Hermes session_id（见上） |
| `steps` | 非空数组，每项 = 一个 step |
| `steps[].step_id` | 必填，本次 launch 内唯一 |
| `steps[].task_id` | **必填**，= 你已建好并 claim 的 kanban 卡 id。mw 不建卡 |
| `steps[].assignee` | 必须在注册表中（当前: coder / code-reviewer / system_engineer / tester）。未知 = 拒收 |
| `steps[].required_files` | 会注入 worker 提示词，worker 会创建它们。**答案不要藏在文件名里** |
| `steps[].timeout_seconds` | 超时兜底（默认 1800）。worker 挂死时 reaper 按此收尸 |
| `steps[].include` | 可选，默认 `true`。`false` = 本步跳过：mw 不登记该卡、不计入 `card_count`、不占槽。需配 `skip_reason` |
| `steps[].skip_reason` | `include:false` 时的跳过原因（进 launch 响应 `skipped_steps[]` 供审计） |
| `project_workspace` | 可选。项目任务用 `/share_workspace/projects/<name>`（须在白名单前缀下，否则拒收） |

### 跳过步骤（你是决策者，2026-10-06）

workflow 模板里标 `optional: true` 的步骤（查 `GET /api/workflows`），
你可以按任务类型决策跳过。**决策权在你（产品经理角色），mw 不评价。**

**路径 1 — 整步不要（零开销，首选）**：launch payload 里给该 step 加
`"include": false, "skip_reason": "<为什么跳>"`。该卡不进 ledger、不派
worker。注意：卡你已建的话记得 `hermes kanban archive` 掉，别留孤儿。

```json
{"step_id":"design","task_id":"t_xxx","assignee":"system_engineer",
 "include":false,"skip_reason":"纯后端 hotfix，无需架构设计"}
```

**路径 2 — 留痕跳过（审计需要时）**：卡照常登记，但你在播种的
TODO.md『当前任务』里加一行 `- 跳过: 是 [原因: <理由>]`。worker
起手读到立即写 `结果: 产品经理决策跳过 — <原因>` + `状态: completed`
退出，reaper 照常结算。开销 = 一次极短 worker 回合。

典型场景（纯后台开发）：跳过交互/UI 设计类角色；hotfix 跳过 design；
typo/文档改动跳过 testing。`workflows.yaml` 的 `skip_when` 列表是给你的
提示，不是自动执行——每张卡你都要显式决策。

规则：
- 全跳光（所有 step 都 include:false）→ launch 400 拒，至少留一张。
- 两条路径不可叠加在同一张卡（include:false 优先，卡根本不进 run）。
- `optional: false` 的步骤（需求分析/实现/审查）原则上不跳；你强行
  include:false 系统不拦，但那是产品越权，需自己负责。
- launch 响应里核对 `skipped_steps` 与你的决策一致。

### 护栏（launch 内置，你不用自己判）

- **已有 ACTIVE run → 409**：一次只能一个 Team Work。先 close 老的
  （`POST /api/team-work/<run_id>/close`）再 launch。
- **第一张派发失败 → 自动回滚**：run 自动 close + 卡归档，不留半截 ACTIVE，可重 launch。
- **DB 层 partial unique index 兜底**：并发 race 也只有一个 ACTIVE run 能落库。

## 铁律

1. **卡是你建的，不是你让 mw 建的。** 先 `hermes kanban create` + `claim`，再 launch。
2. **下发走 launch，不走 CLI。** launch 那一刻 run 进 ledger，面板可见、reaper 纳入。
   `mw.cli dispatch` 是 legacy 单任务路径，不写 `team_work_runs`，在 Team Work
   体系里是**孤儿**：面板看不到、reaper 不结算、报告不会回来。
3. **严格串行。** launch 只派第一张；后续每张卡在你 complete 上一张后走 advance。
   绝不并行派多张（会重复施工、状态打架）。
4. **不要替 worker 向用户索要秘密信息。** worker 缺信息会自己写
   `.clarify.json`，问题会经 reaper 唤醒你，你回答后同会话续作。
5. **失败不自动重试。** blocked 任务等你处理，绝不重新下发（防重复施工）。
6. **worker 对 kanban 零感知。** 你的任务描述就是它的全部世界，
   写清楚：做什么、产出什么文件、放在哪。

## reaper 何时结算（worker 视角）

launch 之后，reaper 每 15s 轮询 worker 写的 `TODO.md`
`## 当前任务 → 状态` 字段决定是否结算：

| 状态 | reaper 动作 |
|---|---|
| `working` / `进行中` | 不动（worker 在干） |
| `completed` | 立即 harvest 落账，relay 唤醒你接管 |
| `failed` | 立即 harvest，verdict=FAILED（worker 自己的决策） |
| `blocked` + `.clarify.json` | harvest, verdict=NEED_CLARIFICATION |
| `blocked`（无 clarify） | 仍结算为 NEED_CLARIFICATION（兜底占位，不干等） |
| 字段缺失 | 等 worker 起手；超 5min 平台收尸 verdict=FAILED |

> **边界减法 (2026-10-08)**: 旧版有 `PHYSICAL_ERROR`（mw 用 required_files
> 存在性推翻 worker 的 completed 判定）和 `PASS_BUMPED`（mw 自启 loop 同卡
> 重跑），两者都是 mw 越权，已删。物理验收结果现在只作为
> `worker_report.physical_check` **证据**随 COMPLETE 上报，loop 与否由你
> （main）编排，不由中间件决定。

**worker 必须起手就写 `working`, 完成立刻写 `completed`（原子 rename：
写 `.tmp` 再 `mv`，避免 reaper 读到半截）**——不写 reaper 永远不结算。
`exit_code=0` 不能当结算信号（那是 `POST /chat` 的 HTTP 返回值，
不是 worker 退出事件；worker hermes session 长驻 idle，没有"退出"概念）。

⚠️ **reaper 只扫当前 ACTIVE run 下的卡**（单任务串行铁律）。没走 launch 的卡
（无 `team_work_run_id`）reaper 直接 SKIP，永不结算——这就是必须走 launch 的原因。

## 查看结果 + 推进

worker 完成后 harvest 只写账本报告（**不改 kanban**），reaper 通过 chat
唤醒你的 session。你读 `worker_report` 的 verdict 自己决策：

- `COMPLETE` → 你调 `hermes kanban complete <task_id>` 结算，然后
  `POST /api/team-work/<run_id>/advance` 推进下一张（严格串行）
- `NEED_CLARIFICATION` → 回答后 `mw resume --task <id> --instruction "..."`
- `FAILED` → 改方向 / 换 worker / 建后续 card / 放弃
  （worker 自报做不到，或平台超时收尸；看 `worker_report.why` 区分）

链推进（complete + advance 下一张）是你的职责，中间件不代劳。

**别轮询**: 不要在你等结算时反复查 worker session 消息数 / 亲自跑浏览器复检 worker 产物 —— 等 reaper 叫你即可; 兜底敲门会在真挂死时叫醒你。

```bash
# 必要时 (想看进度) 一次性查账本, 不要轮询:
python3 -m mw.cli status                                            # 账本快照(含 worker_report)
curl -s "http://127.0.0.1:8700/api/team-works?include_closed=1"    # team work 列表
curl -s  http://127.0.0.1:8700/api/team-work/<run_id>               # 单个 run 详情
hermes kanban show <task_id>                                        # 任务详情
# Web 控制台: http://127.0.0.1:8700 → Team Works 面板看 ACTIVE 卡 + 关闭
```
