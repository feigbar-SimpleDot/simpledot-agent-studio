---
name: worker-message-relay
description: "Route worker questions and cross-worker information needs. Use when a worker task is NEED_CLARIFICATION / blocked, when one worker needs another worker's output, or when the user mentions 转发/转问/澄清/worker提问/worker之间通讯/relay."
version: 2.0.0
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
