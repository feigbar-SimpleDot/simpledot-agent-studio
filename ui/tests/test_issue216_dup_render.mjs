#!/usr/bin/env node
// issue #216 守护套件 — 聊天框消息重复渲染修复 (W 批 s4 / coder)
//
// 依据: docs/PLAN-W.md §4 (唯一技术依据: 根因 = SSE 流路径与 4s 轮询路径
//       双写 messages, 去重登记互不相通 — 流路径渲染的用户/assistant 消息
//       的 gateway id 从不写入轮询路径的 seenIdsRef id 水位; 修复 = 统一
//       去重真源到 seenIdsRef: assistant.completed 同步登记 message_id
//       + 轮询 tick 对 user 回声做尾部同文兜底吞并, 纯前端单文件)。
//
// 覆盖:
//   T1  vm 直测: assistant.completed 把 d.message_id 同时登记进
//       turnDedupRef.msgIds (poll.recovered 仍用) 与 seenIdsRef.current
//       (轮询 id 水位) —— 去重单一真源在位; 未传 seenIdsRef 的旧调用方
//       零破坏 (缺省 null 不炸)。
//   T2  deps 接线: buildTurnStreamHandler 解构含 seenIdsRef; runStream 与
//       send 两处调用点均补传 seenIdsRef (缺一处即一条流漏登记)。
//   T3  轮询 tick user 回声兜底: fresh 过滤后、追加前存在「尾部 user 气泡
//       同文吞并」逻辑; 且 fresh 各条 id 先入 seen 再进 setMessages
//       (吞并一次性生效, 不拦后续同文新消息)。
//   T4  无第二处裸追加: 消息写入路径 `[...m, ...fresh]` 恰好 1 处, 且位于
//       回声兜底 updater 内部 (未过滤的裸追加即回归)。
//   T5  回归红线: poll.recovered 两级去重语义保持 (id 整条跳过 + 半截气泡
//       前缀替换); loadHistoryWindow 的 alreadySeen 快照幂等逻辑未动。
//   T6  真实 id 形态 (D-W-1 守护补强): vm 直测 tick 回声兜底 updater ——
//       复刻 170 实测场景: seen 水位里是 SSE assistant.completed 登记的
//       gateway 字符串 id (`msg_…`), 轮询 fresh 是窗口端点整数 id
//       (user=1244/assistant=1245), 两命名空间永不相交。此场景下 assistant
//       回声必须被内容级兜底吞并 (旧实现仅 user 兜底 → assistant 重复 →
//       本用例红, 正是 D-W-1「守护绿/真机红」的封堵点); 不同文本的
//       fresh assistant 必须照常追加 (兜底不得误拦)。
//   T7  T6 的静态面: tick 兜底必须同时覆盖 user 与 assistant 两个 role。
//
// 变异反证锚 (MV): MV-216-A 删 assistant.completed 的 seenIdsRef 登记 →
//   T1 红; MV-216-B 删 runStream/send 任一调用点的 seenIdsRef 补传 → T2 红;
//   MV-216-C 删轮询 tick 的回声兜底块 → T3 红; MV-216-D 把 tick 追加改回
//   裸 [...m, ...fresh] → T4 红; MV-216-E 删 poll.recovered 前缀替换或
//   loadHistoryWindow alreadySeen → T5 红; MV-216-F 删 tick 兜底的
//   assistant 分支 (回退到 f24b469 旧实现) → T6+T7 红。
//
// 风格: 沿用仓库既有 ui/tests/*.mjs — 源码抽取 + vm 直测 + 断言, 不起浏览器。
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(path.join(here, f), 'utf8')
const chat = read('../src/views/ChatView.jsx')

const tests = []
function test(name, fn) { tests.push({ name, fn }) }
function assert(c, m) { if (!c) throw new Error(m) }

// 源码抽取 (花括号/圆括号配平, 同 test_issue212_decode_speed.mjs 口径)
function extractFn(source, startMarker) {
  const start = source.indexOf(startMarker)
  if (start < 0) throw new Error(`源码抽取失败, 未找到标记: ${startMarker}`)
  let braces = 0, parens = 0, seenBrace = false
  for (let i = start; i < source.length; i++) {
    const ch = source[i]
    if (ch === '{') { braces++; seenBrace = true }
    else if (ch === '}') braces--
    else if (ch === '(') parens++
    else if (ch === ')') parens--
    if (seenBrace && braces === 0 && parens === 0) return source.slice(start, i + 1)
  }
  throw new Error(`源码抽取失败 (括号不配平): ${startMarker}`)
}

// ── T1: vm 直测 assistant.completed 双登记 ─────────────────────────
const handlerSrc = extractFn(chat, 'function buildTurnStreamHandler')

function makeHandler({ withSeen = true } = {}) {
  let msgs = []
  const seen = new Set()
  const ctx = {
    Date: { now: () => 1000 },
    setInterval: () => 1, clearInterval: () => {},
    Set: Set, Map: Map, Promise: Promise, Math: Math, Array: Array, JSON: JSON,
    pushToolCall: () => {}, closeTurnToolGroups: () => {},
    sealTurnThinking: () => {}, backfillToolResults: () => {},
    extractReasoningSegments: () => [], authoritativeReasoning: () => '',
  }
  vm.createContext(ctx)
  vm.runInContext(`__mk = (${handlerSrc.replace(/^function buildTurnStreamHandler/, 'function')})`, ctx)
  const deps = {
    setMessages: (fn) => { msgs = typeof fn === 'function' ? fn(msgs) : fn },
    setRunInfo: () => {}, setPhase: () => {},
    turnDedupRef: { current: { thinking: new Set(), msgIds: new Set() } },
    reasoningSeenRef: { current: new Map() },
    reasoningTimerRef: { current: null },
    runIdRef: { current: null },
    fetchMessages: async () => [],
  }
  if (withSeen) deps.seenIdsRef = { current: seen }
  const handler = ctx.__mk(deps)
  return { handler, seen, get msgs() { return msgs } }
}

test('T1 assistant.completed 双登记 msgIds + seenIdsRef (缺省不炸)', () => {
  const h = makeHandler()
  h.handler('assistant.completed', { message_id: 'gm-42', content: '正文' })
  assert(h.seen.has('gm-42'),
    'assistant.completed 的 message_id 必须同步登记进轮询 id 水位 seenIdsRef (重复渲染根因封堵点)')
  // 未传 seenIdsRef 的旧调用方 (既有守护测试) 零破坏: 缺省 null 不抛
  const h2 = makeHandler({ withSeen: false })
  h2.handler('assistant.completed', { message_id: 'gm-7', content: 'x' })
  assert(h2.msgs.length === 1, '未接 seenIdsRef 时正文气泡仍正常落位')
})

// ── T2: deps 接线 ──────────────────────────────────────────────────
test('T2 解构含 seenIdsRef 且 runStream/send 两调用点均补传', () => {
  assert(/seenIdsRef\s*=\s*null\s*\}\s*=\s*deps/.test(chat),
    'buildTurnStreamHandler 解构应含可选 seenIdsRef (缺省 null)')
  const wired = chat.match(/reasoningTimerRef, runIdRef, seenIdsRef,/g) || []
  assert(wired.length === 2,
    `runStream 与 send 两处调用点都应补传 seenIdsRef (实际 ${wired.length} 处)`)
})

// ── T3: 轮询 tick user 回声兜底 ────────────────────────────────────
const TICK_ANCHOR = "const freshRaw = raw.filter((e) => e && e.id != null && !seen.has(e.id))"
test('T3 轮询 tick fresh 过滤含 user 回声内容级兜底', () => {
  const tickAt = chat.indexOf(TICK_ANCHOR)
  assert(tickAt >= 0, '轮询 tick fresh 过滤段未找到')
  const tick = chat.slice(tickAt, tickAt + 2000)
  assert(/role === 'user'/.test(tick) && /lastUser/.test(tick),
    'tick 追加前应对 fresh 的 user 条目做尾部 user 气泡同文兜底 (issue #216)')
  assert(/fresh\.findIndex\(\(f\) => f\.role === 'user'\)/.test(tick) &&
         /\.text \|\| ''\)\s*===\s*\(lastUser\.text \|\| ''\)/.test(tick),
    '兜底应为「fresh 首个 user 条目 vs 尾部 user 气泡」的同文比对')
  const seenAdd = tick.indexOf('seen.add(e.id)')
  const setAt = tick.indexOf('setMessages')
  assert(seenAdd >= 0 && setAt >= 0 && seenAdd < setAt,
    'fresh 各条 id 必须先入 seen 再进 setMessages (吞并一次性生效)')
})

// ── T4: 无第二处裸 [...m, ...fresh] 追加 ───────────────────────────
test('T4 消息写入路径裸 [...m, ...fresh] 恰好 1 处且在兜底 updater 内', () => {
  const bare = chat.match(/\[\.\.\.m, \.\.\.fresh\]/g) || []
  assert(bare.length === 1,
    `[...m, ...fresh] 应恰好 1 处且受回声兜底保护 (实际 ${bare.length} 处 — 第二处即未过滤追加回归)`)
  const tickAt = chat.indexOf(TICK_ANCHOR)
  const tick = chat.slice(tickAt, tickAt + 2000)
  assert(/\[\.\.\.m, \.\.\.fresh\]/.test(tick), '唯一一处应位于轮询 tick 的回声兜底 updater 内')
  assert(/lastUser[\s\S]*\[\.\.\.m, \.\.\.fresh\]/.test(tick),
    '该处必须排在 lastUser 兜底判定之后 (裸追加 = 回归)')
})

// ── T5: 回归红线 (poll.recovered / loadHistoryWindow 语义未动) ─────
test('T5 poll.recovered 两级去重与历史加载 alreadySeen 快照保持', () => {
  assert(chat.includes('raw.content.trim().startsWith(bubble)'),
    'poll.recovered 半截气泡前缀替换逻辑必须保持 (PLAN-W §4.2-3)')
  assert(chat.includes('dd.msgIds.has(raw.id)'),
    'poll.recovered id 整条跳过逻辑必须保持')
  assert(chat.includes('const alreadySeen = new Set(seen)'),
    'loadHistoryWindow alreadySeen 快照幂等逻辑不得改动 (PLAN-W §4.3 不改清单)')
})

// ── T6: 真实 id 形态 — vm 直测 tick 回声兜底 updater (D-W-1 封堵点) ──
// 复刻 TEST-W-REPORT §2 AC-4 证据链 1-3 的实测场景: SSE assistant.completed
// 把 gateway 字符串 id (`msg_…`) 登记进 seenIdsRef, 轮询 tick 拿到的却是
// 窗口端点整数 id (user=1244/assistant=1245) —— 命名空间永不相交, id 水位
// 对 assistant 回声完全无效。旧实现 (f24b469) 只有 user 兜底, 此场景下
// assistant 正文整条重复追加 (D-W-1); 新实现必须靠内容级兜底吞并。
function extractTickUpdater(source) {
  const tickAt = source.indexOf(TICK_ANCHOR)
  if (tickAt < 0) throw new Error(`轮询 tick fresh 过滤段未找到: ${TICK_ANCHOR}`)
  const arrowAt = source.indexOf('(m) => {', tickAt)
  if (arrowAt < 0) throw new Error('tick setMessages 回声兜底 updater 未找到')
  let braces = 0
  for (let i = arrowAt + 7; i < source.length; i++) {
    if (source[i] === '{') braces++
    else if (source[i] === '}') { braces--; if (braces === 0) return source.slice(arrowAt, i + 1) }
  }
  throw new Error('tick updater 花括号不配平')
}

test('T6 真实 id 形态: msg_ 字符串水位 vs 轮询整数 id, assistant 回声必须被吞并', () => {
  const updaterSrc = extractTickUpdater(chat)
  const ctx = { Set, fresh: [] }
  vm.createContext(ctx)
  vm.runInContext(`__up = (${updaterSrc})`, ctx)
  // 场景前提: 命名空间错配真实存在 —— 整数 id 均不在 msg_ 字符串水位里
  const seen = new Set(['msg_eaf3e948bd2e43eca3c6abaff1cd8fcb'])
  assert(!seen.has(1244) && !seen.has(1245),
    '场景前提: 轮询整数 id 不在 SSE msg_ 字符串水位中 (两命名空间不相交, id 水位必然漏放行)')
  // 收口后的当前视图: send() 本地 user 气泡 + 流式已渲染 assistant 正文 (带解码速度)
  const m = [
    { role: 'user', text: '你好' },
    { role: 'assistant', text: '你好！有什么可以帮您？', decodeStats: { exact: 11.2 } },
  ]
  // 收口后首个 4s tick: 窗口端点回声 user=1244 / assistant=1245 (整数 id)
  ctx.fresh = [
    { role: 'user', text: '你好', id: 1244 },
    { role: 'assistant', text: '你好！有什么可以帮您？', id: 1245 },
  ]
  const out = ctx.__up(m)
  const users = out.filter((x) => x.role === 'user').length
  const assistants = out.filter((x) => x.role === 'assistant').length
  assert(users === 1, `user 回声应被吞并, 气泡恰 1 (实际 ${users})`)
  assert(assistants === 1,
    `assistant 回声在 id 命名空间错配下必须被内容级兜底吞并, 气泡恰 1 ` +
    `(实际 ${assistants} — 即 D-W-1 重复渲染, 旧实现 f24b469 在此为 2)`)
  assert(out.length === 2, `收口后 tick 不得追加任何重复 (实际 out.length=${out.length})`)
  // 反向: 不同文本的新 assistant 消息必须照常追加 (兜底不得误拦)
  ctx.fresh = [{ role: 'assistant', text: '一条完全不同的新回复', id: 1246 }]
  const out2 = ctx.__up(m)
  assert(out2.length === 3,
    `不同文本的新 assistant 消息必须正常追加 (实际 out2.length=${out2.length} — 兜底误拦)`)
})

// ── T7: tick 兜底静态面 — 必须同时覆盖 user 与 assistant ────────────
test('T7 tick 回声兜底覆盖 assistant role (命名空间错配的唯一前端封堵点)', () => {
  const tickAt = chat.indexOf(TICK_ANCHOR)
  assert(tickAt >= 0, '轮询 tick fresh 过滤段未找到')
  const tick = chat.slice(tickAt, tickAt + 2000)
  assert(/lastAssistant/.test(tick) &&
         /fresh\.findIndex\(\(f\) => f\.role === 'assistant'\)/.test(tick) &&
         /\.text \|\| ''\)\s*===\s*\(lastAssistant\.text \|\| ''\)/.test(tick),
    "tick 追加前应对 fresh 的 assistant 条目做尾部 assistant 气泡同文兜底 " +
    '(D-W-1: SSE msg_ 字符串 vs 轮询整数 id 命名空间错配, 内容级兜底是唯一前端封堵点)')
})

// ── runner ─────────────────────────────────────────────────────────
let failed = 0
for (const t of tests) {
  try { await t.fn(); console.log(`  PASS ${t.name}`) }
  catch (e) { failed++; console.error(`  FAIL ${t.name}\n    ${e.message}`) }
}
console.log(failed ? `\n${failed}/${tests.length} FAILED` : `\nALL ${tests.length} PASSED`)
process.exit(failed ? 1 : 0)
