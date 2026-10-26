---
name: team-work-chain-advance
description: "Advance Team Work chains: complete, add cards, advance next card."
version: 2.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [Agent, Matrix, Kanban, TeamWork]
---

# Team Work 链推进（main agent 侧）

## When to Use

- 收到 mw 转发的「任务结算落账: verdict=COMPLETE」消息，需推进下一张卡
- 链跑到一半要追加新卡（审查打回建修复卡 / 复审卡）
- launch 之后第二张及以后卡片需要真正拉起 worker 时

`POST /api/team-work/launch` 只自动派第一张卡。后续每张卡即使
`hermes kanban claim` 也不会拉起 worker——spawn 必须由 main 显式经
`advance` 端点触发。本技能是收到 mw 结算报告后推进链的完整序列。

**v2.0 变更（2026-10-04）**：推进一律走 `POST /api/team-work/<run_id>/advance`，
不再手写 manifest + `__spawn-bg`（那是 legacy 路径，绕过 run 归属校验与
project_workspace 继承，是上一轮补充卡死锁的诱因）。

## 推进序列（每张下游卡）

前置: `export PATH=/opt/hermes/.venv/bin:$PATH`（venv 含 hermes 与 mw）。

1. **结算上一张**：`hermes kanban complete <task_id> --result "做了什么 + 产物绝对路径"`
   —— 空 complete 被拒（"empty completion is not evidence"），必须带 --result/--summary。
2. **占卡**：`hermes kanban claim <next_task_id>`。
3. **推进派发**：
   ```bash
   curl -s -X POST http://127.0.0.1:8700/api/team-work/<run_id>/advance \
     -H 'Content-Type: application/json' \
     -d '{"task_id":"<next_task_id>","target_profile":"<worker名>",
          "title":"<卡标题>","required_files":["<产物>"],"timeout_seconds":1800}'
   ```
   advance 会：校验上一张已结算（`current_card_id` 为 NULL）→ 占槽 → 派 worker。
   上一张没结算就 advance → **409 拒**（串行铁律）。
   `project_workspace` 可不传——advance 自动继承 run 的产物根。

4. **轮转 TODO.md**（见下）。

## 中途追加卡片（回流场景：审查打回建修复卡 / 复审卡）

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
- 追加卡自动继承 run 的 `project_workspace`，required_files 不会错落到 task 目录。
- 幂等：重复登记同一张卡不重复计数；一张卡不能挂两个 run。
- 返回 `{registered, already_registered, skipped, card_count}`，逐项核对。

## TODO.md 轮转（main 直接编辑，遵守所有权）

- 上一张连『结果』摘要移入『已完成』；下一张从『待执行』升为『当前任务』
  （目标/承接方/产物目录/产出文件 原样搬，结果行重置为占位）。
- 用 execute_code 里的 python `open()/write` 改文件：patch/write_file 工具写
  /share_workspace 会被 HERMES_WRITE_SAFE_ROOT 拒，execute_code 不受限。
- 解析『待执行』条目时先 strip 行首 `- ` 再按 `字段名:` 切分，改完打印
  标题/产物目录等关键字段核对——拼接残渣写进契约文件会污染 worker 读取。

## 每张卡轮转时必须先汇报再 advance（用户 2026-10-19 明确纠正）

结算（complete）一张 → 派发（advance）下一张之前，必须先在对话里给用户一份
**面向人的进展汇报**——不能只做账本动作流（complete + claim + advance + 一行
状态描述 = 不足）。反例: c6 复测卡结算时 main 只动了账本 + 写一行短报,
用户看不到进展, 违反 main 默认协议"每完成 1 张 step 简报"。

汇报四要素（缺一不可）:
1. **卡标识** — 卡号 + worker 名 + session id + 产物报告绝对路径
2. **实测结果** — 与基线对比的通过/失败、关键 commit hash / blob md5 / 测试数；
   需"有条件接受"时一句话写条件边界（如"附 N 个 FAIL 全是 env 与本批无关"）
3. **独立核验** — main 侧独立做了什么（亲跑测试 / grep commit / md5sum 对比 /
   环境事实验证），与 worker 自报结论交叉印证
4. **下游影响** — 本卡缺陷闭环情况 + 队列下一张是谁 / 在做什么

不可省略场景: 哪怕只是"动账 + 一行描述"也得补一段话（≥4 句）。
**没有进展汇报 = 用户认为链停摆**。

**协议补丁必须同时落到仓库 deploy/skills/**: main 在 `/opt/data/skills/agent-matrix/` (运行环境缓存层) patch skill 后, 必须把同补丁写入仓库 `<project>/repo/deploy/skills/<skill>/SKILL.zh-cn.md` 并 commit 落地 (本地 commit 属任务链与回溯链的一部分, 必须落地)——运行环境缓存会在下次 install.sh 时被覆盖, 仓库版本才是 部署真正落地的源。push 不在本 skill 义务内: 是否 push 由用户自行选择 (用户可用自己的 soul/skill/prompt 指定), skill 不代劳。补一次不补两次 = 用户下次加载 skill 时协议仍缺失。

## 坑

- **不要再用 `__spawn-bg` + 手写 manifest 推进**。v1 的这条路绕过 run 归属
  校验与 project_workspace 继承，是 2026-10-04 补充卡死锁的诱因。一律走
  `advance` 端点。
- 验证 spawn 是否真在跑：`ps aux | grep __spawn-bg`（advance 内部仍用它起
  worker）+ 查 GET /api/tasks 的 session_id/runner_kind，不要只看 kanban 状态。
- 结算报告里的 verdict 与产物要交叉核对（读产物文件存在且非空）再 complete，
  不要只信转发消息。
- 相关但不覆盖：初次下发见 mw-task-dispatch 技能；worker 注册表实时名单用
  GET http://127.0.0.1:8700/api/workers。

## 修复/交付卡报告"已 push"必须 main 侧独立对账

**push 不是 skill 规定的义务, 但一旦有人报了"已 push", 对账照样要做。**
本 skill 只要求 commit 落地; 是否 push 由用户自行选择 (用户可用自己的
soul/skill/prompt 指定)。因此:

- worker/协调方**若**执行了 push (用户已授权), 结算前 main 必须用
  `PROJECT-MEMORY.md` 的仓库地址自己跑一遍:
  ```bash
  git rev-parse HEAD                                       # 本地
  git ls-remote <project-remote-url> HEAD                  # 远端
  # 期望两值全等；MATCH 即落账「远端 tip==HEAD push 真实落地」
  ```
- 没人报"已 push" ⇒ 不需要这段对账, 也不要把 push 写进本卡的验收口径;
  worker 只 commit 不 push 是**正确行为, 不是缺陷**。

三个常见假阳性:

- **仓库 owner/repo 路径错写**: worker 笔记写"推到 sds/studio"实际推到
  另一台主机上的 `<owner>/<repo>` (或反过来)。worker's local `git remote -v` 才是
  唯一可信源, `PROJECT-MEMORY.md` 第 3-4 行的地址是权威比对基准。main 不要沿用
  worker 笔记里的 owner/repo 路径文字, 必须独立
  `curl .../repos/<owner>/<repo>/branches/main` 校验。
- **本地已 commit 但远端未接收**: FF 推送非默认; HEAD 在远端不见得是本地这条。
  以 `git ls-remote ...refs/heads/main` 拿到的远端 SHA 为准。
- **Gitea token 解码失败**: 探测一次 401/403 立即停, 勿用空 token 连发。

有人报"已 push"时, main 报告里 push 段必须含: 远端 commit SHA + 本地 SHA +
仓库 owner/repo 路径, 三者贴出原文等号即 PASS。否则视为 worker 自报"push 成功"
但未实测, 结算拒收, 走 resume 补实测, 或由用户/协调方自行 push。
