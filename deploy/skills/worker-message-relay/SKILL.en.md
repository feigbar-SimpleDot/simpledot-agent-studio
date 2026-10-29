---
name: worker-message-relay
description: "Route worker questions and cross-worker information needs. Use when a worker task is NEED_CLARIFICATION / blocked, when one worker needs another worker's output, or when the user mentions `转发/转问/澄清/worker提问/worker之间通讯/relay`."
version: 2.1.0
platforms: [linux]
metadata:
  hermes:
    tags: [Agent, Matrix, Relay, Clarify, Kanban]
---

# Worker Message Routing (main = entry + project manager · 1A boundary)

Your role: the only coordinator who owns kanban. Workers are pure executors — their boundary is "what they
can and cannot do"; what they can't do, **they only look for you**, no need to know who else to ask. All
information passing between workers must go through you; workers never connect to each other.

**1A boundary**: Q&A cards are also created by you (`hermes kanban create` + `claim`), then `mw dispatch` to
spawn the worker. mw doesn't create cards.

## Three-fork Decision on Receiving a Worker Question (NEED_CLARIFICATION)

The worker writes `.clarify.json` and exits; after reaper settles, it wakes you via chat; the question shows
up on the board / console. **Judge first, then act**, in order:

### ① You can answer → answer directly

You hold the global context (user intent, project background, each task's deliverable). If the information is
in your hands, answer it yourself, don't pass it to the user.

```bash
export PATH=/opt/hermes/.venv/bin:$PATH
python3 -m mw.cli resume --task <task_id> --instruction "<your answer + continue instruction>"
```

### ② You can't answer, but a worker in the registry can → dispatch a Q&A card to relay

If the question is about some worker's domain knowledge / their deliverable content:

```bash
# 1. You yourself create a Q&A card and claim it
QTID=$(hermes kanban create "Q&A: <question summary>" --assignee <knowing-worker> \
       --body "Pure Q&A task" | grep -oE 't_[a-z0-9]+' | head -1)
hermes kanban claim "$QTID"

# 2. mw spawns the worker for this card (sync wait for the result)
python3 -m mw.cli dispatch --manifest - <<EOF
{
  "task_id": "$QTID",
  "title": "Q&A: <question summary>",
  "target": {"profile": "<knowing-worker>"},
  "input_spec": {
    "task_description": "This is a pure Q&A task, do not edit any files. Question: <full question>. Please answer directly from your work deliverable / domain knowledge; write the answer to answer.md (structured, complete, readable standalone).",
    "context_files": []
  },
  "harvest": {"required_files": ["answer.md"], "timeout_seconds": 180},
  "idempotency_key": "relay-<asking-task>-$(date +%s)"
}
EOF

# 3. Read answer.md content
# 4. Pass the answer back to the asking worker (resume in same session, zero context loss)
python3 -m mw.cli resume --task <asking-task-id> --instruction "The answer to your question is: <answer.md content>. Please continue the task."
```

### ③ Neither can answer → find the human

The question involves user intent, business decision, or info you don't have: don't guess; keep the
NEED_CLARIFICATION state, relay the question summary to the user, and after the user answers resume the
worker.

## Iron Rules

1. **Workers never directly connect to other workers.** If you see any "worker A says go ask worker B"
   phrasing, the correct action is always: you create a card for B, dispatch, then pass the answer back to A.
2. **Relaying must be faithful.** The question sent to B must be complete and self-contained (B has zero
   context); the answer sent back to A must be exactly as received, no embellishment or loss of key
   qualifiers.
3. **Q&A cards don't touch file deliverables.** Q&A tasks only require `answer.md` to exist and be non-empty,
   no code deliverable required.
4. **A question only relays once.** If B also can't answer, escalate to human immediately, don't relay A→B→C
   in a chain (prevent routing loops).
5. **No auto-retry on failure.** Q&A card blocked → escalate to human, don't re-dispatch.

## View Pending Queue

```bash
python3 -m mw.cli status          # ledger: who is PENDING / NEED_CLARIFICATION / BLOCKED
# Web console: http://127.0.0.1:8700  attention group has 💬 Answer button
```

## Explicit relay confirm (issue #119)

Background: `notify_confirmed_at` used to be set only when the relay actually received
HTTP 200 from the gateway. The gateway chat endpoint is fully synchronous (it waits for
the session turn lease and runs the whole turn before returning), so when the client
times out before the server responds, the confirmation flag stays NULL forever and the
card keeps getting re-delivered. This endpoint lets main confirm explicitly after it has
consumed a report, without waiting for a 200 that may never come.

```bash
curl -s -X POST http://127.0.0.1:8700/api/relay/confirm \
     -H 'Content-Type: application/json' \
     -d '{"task_id": "<task_id>"}'
```

- Method: POST; body `{"task_id": "<id>"}`.
- Expected response: 200 `{"ok": true, "task_id": "...", "confirmed_at": <ts>}`.
- Error shapes: 400 (missing `task_id`) / 404 (`task not found`) — distinguishable from
  internal errors (500).
- Semantics: sets `notify_confirmed_at` and clears the backoff/dead-letter fields
  (`notify_attempts` → 0, `notify_dead_until` → NULL, `notify_last_failed_at` /
  `notify_last_error` → NULL). A dead-letter card (`notify_attempts >= 8`,
  `notify_dead_until` = now + 1 year) recovers with one confirm call.
- Idempotent: confirming the same card repeatedly is safe — same result, no error, no
  duplicate events. It only affects the explicitly named `task_id`; it never batch-scans
  the table.
- Relationship to the old signal: the legacy path ("relay got HTTP 200 ⇒ confirmed") is
  unchanged; this endpoint is an explicit alternative for main, not a replacement of the
  relay path. When you don't call it, existing behavior is bit-for-bit identical.

Typical use: after main has finished processing a worker report (including historical
cards that were settled but never delivered), call this endpoint for that `task_id` to
close the delivery loop and, if the card was dead-lettered, recover it.
