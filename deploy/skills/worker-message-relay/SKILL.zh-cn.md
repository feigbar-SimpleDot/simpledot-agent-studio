---
name: worker-message-relay
description: "Route worker questions and cross-worker information needs. Use when a worker task is NEED_CLARIFICATION / blocked, when one worker needs another worker's output, or when the user mentions 转发/转问/澄清/worker提问/worker之间通讯/relay."
version: 2.1.0
platforms: [linux]
metadata:
  hermes:
    tags: [Agent, Matrix, Relay, Clarify, Kanban]
---

# Worker 消息路由（main = 入口 + 项目经理 · 1A 边界）

你的角色：唯一持有 kanban 的协调者。worker 是纯执行者——它的边界就是
"能做和不能做"，不能做的部分**它只找你**，不需要知道找谁。
所有 worker 之间的信息传递都必须经你转发，worker 之间永不直连。

**1A 边界**：问答卡也是你自己建（`hermes kanban create` + `claim`），
再 `mw dispatch` 起 worker。mw 不建卡。

## 收到 worker 提问（NEED_CLARIFICATION）时的三岔决策

worker 写了 `.clarify.json` 退出，reaper 落账后通过 chat 唤醒你，
问题会出现在看板/控制台。**先判断，再行动**，按顺序：

### ① 你自己能答 → 直接答

你持有全局上下文（用户需求、项目背景、各任务产出）。
信息在你手里就自己答，不要转嫁给用户。

```bash
export PATH=/opt/hermes/.venv/bin:$PATH
python3 -m mw.cli resume --task <task_id> --instruction "<你的回答+继续指令>"
```

### ② 你答不了，但注册表里有 worker 能答 → 派问答卡转发

问的是某个 worker 的领域知识/它的产出内容：

```bash
# 1. 你自己建一张问答卡并 claim
QTID=$(hermes kanban create "问答: <问题摘要>" --assignee <知情worker> \
       --body "纯问答任务" | grep -oE 't_[a-z0-9]+' | head -1)
hermes kanban claim "$QTID"

# 2. mw 为这张卡起 worker（同步等结果）
python3 -m mw.cli dispatch --manifest - <<EOF
{
  "task_id": "$QTID",
  "title": "问答: <问题摘要>",
  "target": {"profile": "<知情worker>"},
  "input_spec": {
    "task_description": "这是一个纯问答任务, 不要修改任何文件。问题: <完整问题>。请基于你的工作产出/领域知识直接回答, 回答写入 answer.md(结构化、完整、可独立阅读)。",
    "context_files": []
  },
  "harvest": {"required_files": ["answer.md"], "timeout_seconds": 180},
  "idempotency_key": "relay-<提问task>-$(date +%s)"
}
EOF

# 3. 读 answer.md 内容
# 4. 把答案转给提问者 worker(同会话 resume, 上下文零丢失)
python3 -m mw.cli resume --task <提问task_id> --instruction "你问的问题答案是: <answer.md 内容>。请继续完成任务。"
```

### ③ 都答不了 → 找人类

问题涉及用户意图、业务决策、你没有的信息：不要瞎猜，
保持 NEED_CLARIFICATION 状态，把问题摘要转述给用户，
等用户回答后再 resume worker。

## 铁律

1. **worker 永不直连 worker。** 你看到任何"worker A 说去问 worker B"
   的表述，正确动作永远是你建卡派给 B 再把答案转回 A。
2. **转发要保真。** 转给 B 的问题要完整自包含（B 零上下文）；
   转回 A 的答案要原样，不要添油加醋或丢失关键限定。
3. **问答卡不碰文件产出。** 问答任务只要求 answer.md 存在且非空，
   不要求代码产出。
4. **一个提问只转一轮。** 若 B 也答不了，直接升级人类，
   不要 A→B→C 连环转（防路由循环）。
5. **失败不自动重试。** 问答卡 blocked → 升级人类，别重派。

## 查看待处理队列

```bash
python3 -m mw.cli status          # 账本: 谁 PENDING/NEED_CLARIFICATION/BLOCKED
# Web 控制台: http://127.0.0.1:8700  待处理分组带 💬 回答按钮
```

## 显式回执（issue #119）

背景：`notify_confirmed_at` 原先只在 relay 真收到 gateway HTTP 200 时置位。gateway chat
端点是全同步的（等 session turn lease + 跑完整回合才返回），客户端 timeout 早于服务端
返回时确认位永远 NULL，卡会被反复重投。本端点让 main 消费完报告后主动确认，不必等那个
可能永远不来的 200。

```bash
curl -s -X POST http://127.0.0.1:8700/api/relay/confirm \
     -H 'Content-Type: application/json' \
     -d '{"task_id": "<task_id>"}'
```

- 方法：POST；body `{"task_id": "<id>"}`。
- 期望响应：200 `{"ok": true, "task_id": "...", "confirmed_at": <ts>}`。
- 错误形态：400（缺 `task_id`）/ 404（`task not found`）——与内部错误（500）可区分。
- 语义：置 `notify_confirmed_at` 并清退避/死信字段（`notify_attempts` → 0、
  `notify_dead_until` → NULL、`notify_last_failed_at` / `notify_last_error` → NULL）。
  死信卡（`notify_attempts >= 8`、`notify_dead_until` = now + 1 年）回执一次即恢复。
- 幂等：同一张卡重复回执安全——结果一致、不报错、不重复发事件；只对显式点名的
  `task_id` 生效，绝不批量扫描全表。
- 与旧信号的关系：旧路径「relay 收到 HTTP 200 ⇒ 已确认」原样不动；本端点是给 main 的
  显式替代通道，不是替换 relay 路径。不调用它时，现有行为逐字不变。

典型用法：main 处理完某张 worker 报告后（含「已结算但从未送达」的历史卡），对该
`task_id` 调用本端点闭环投递；若该卡已死信，同时完成恢复。
