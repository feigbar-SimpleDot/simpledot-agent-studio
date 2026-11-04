#!/usr/bin/env node
// issue #212 守护套件 — 对话页 LLM decode 速度显示 (U 批 s3 / coder)
//
// 依据: docs/PLAN-U-20261104.md §2 (唯一技术依据: 实时近似 = delta 滑动窗口
//       字符/秒 ÷4 粗估 tok/s, 标「近似」; 权威校准 = GET /api/sessions/{sid}
//       五桶快照 Δoutput_tokens ÷ 生成时长; §2.3 reasoning 独立分桶) ·
//       任务书: assistant.delta 滑动窗口统计, run.completed/assistant.completed 收口。
//
// 覆盖:
//   T1  滑动窗口近似算术: vm 直测 buildTurnStreamHandler, 受控时钟驱动
//        assistant.delta, 断言 setDecodeRate 收到的 tps 与窗口口径逐值相等
//        (span 0.25s 下限 / 3s 窗口裁剪)。
//   T2  窗口裁剪: 超 3s 的旧样本出窗, 速率只反映窗口内样本。
//   T3  收口: assistant.completed 与 run.completed 后 trackDelta 变 no-op,
//        setDecodeRate 不再被调用。
//   T4  权威校准: 首尾 token 快照差 ÷ 生成时长 = exactTps; reasoning_tokens
//        独立分桶 reasoningTps; 基线缺失 / run.failed / 无 assistant 消息
//        三种路径均不落 decodeStats (宁缺勿假)。
//   T5  i18n 对称: chat.decode_speed.* 两语种键集 diff = ∅ 且 5 新键双边在位;
//        全字典扁平键集 diff = ∅。
//   T6  显示层源码钉: 状态条与消息收口行均经 t() 引用 decode_speed 键,
//        无硬编码单语; 新回合/摘流重置 setDecodeRate(null) 三处在位;
//        校准取数仅走同源 /api/sessions/{sid} (零后端改动约束)。
//
// 变异反证锚 (MV): MV-212-A 删 assistant.delta 的 trackDelta 调用 → T1 红;
//   MV-212-B 删 sealDecode (assistant.completed/run.* 任一) → T3 红;
//   MV-212-C 校准去掉 reasoning 分桶 → T4 红; MV-212-D 任一语种删
//   decode_speed 键 → T5 红; MV-212-E 显示文案改硬编码 → T6 红;
//   MV-212-F 删任一 setDecodeRate(null) 重置 → T6 红。
//
// 风格: 沿用仓库既有 ui/tests/*.mjs — 源码抽取 + vm 直测 + JSON 断言, 不起浏览器。
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(path.join(here, f), 'utf8')
const chat = read('../src/views/ChatView.jsx')
const zh = JSON.parse(read('../src/i18n/locales/zh-cn.json'))
const en = JSON.parse(read('../src/i18n/locales/en.json'))

const tests = []
function test(name, fn) { tests.push({ name, fn }) }
function assert(c, m) { if (!c) throw new Error(m) }
function eq(a, b, msg) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b)
  if (sa !== sb) throw new Error(`${msg || 'eq'}:\n  实际 ${sa}\n  期望 ${sb}`)
}
function near(a, b, msg) {
  if (!(typeof a === 'number' && Math.abs(a - b) < 1e-6)) {
    throw new Error(`${msg || 'near'}: 实际 ${a} 期望 ${b}`)
  }
}
const flush = () => new Promise((r) => setImmediate(r))

// 源码抽取 (花括号/圆括号配平, 同 test_issue58_tokens.mjs 口径)
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

// ── vm 沙箱: 受控时钟 + React 风格 setMessages ─────────────────────
const handlerSrc = extractFn(chat, 'function buildTurnStreamHandler')

function makeHarness({ tokens = [], initialMessages = [] } = {}) {
  const clock = { now: 1000 }
  const rates = []            // setDecodeRate 收到的值序列
  const tokenQueue = [...tokens]  // fetchSessionTokens 依次回放的快照
  let msgs = [...initialMessages]
  const ctx = {
    Date: { now: () => clock.now },
    setInterval: () => 1,
    clearInterval: () => {},
    Set: Set, Map: Map, Promise: Promise, Math: Math, Array: Array, JSON: JSON,
    // 模块级辅助函数桩 (仅 run.completed 路径触达, 语义与本套件无关)
    pushToolCall: () => {}, closeTurnToolGroups: () => {},
    sealTurnThinking: () => {}, backfillToolResults: () => {},
    extractReasoningSegments: () => [], authoritativeReasoning: () => '',
  }
  vm.createContext(ctx)
  vm.runInContext(`__mk = (${handlerSrc.replace(/^function buildTurnStreamHandler/, 'function')})`, ctx)
  const handler = ctx.__mk({
    setMessages: (fn) => { msgs = typeof fn === 'function' ? fn(msgs) : fn },
    setRunInfo: () => {},
    setPhase: () => {},
    turnDedupRef: { current: { thinking: new Set(), msgIds: new Set() } },
    reasoningSeenRef: { current: new Map() },
    reasoningTimerRef: { current: null },
    runIdRef: { current: null },
    fetchMessages: async () => [],
    setDecodeRate: (v) => rates.push(v),
    fetchSessionTokens: async () => (tokenQueue.length ? tokenQueue.shift() : null),
  })
  return {
    clock, rates,
    send: (ev, d = {}) => handler(ev, d),
    get msgs() { return msgs },
    flush,
  }
}

// ── T1: 滑动窗口近似算术 (受控时钟逐值断言) ────────────────────────
test('T1 delta 滑动窗口 tok/s 近似 = 窗口字符/秒 ÷4 (含 0.25s span 下限)', async () => {
  const h = makeHarness()
  h.send('run.started', { run_id: 'r1' })
  // t=1000 首帧 40 字符: 单样本 span=0 → 0.25s 下限 → 40/0.25/4 = 40
  h.send('assistant.delta', { delta: 'x'.repeat(40) })
  near(h.rates.at(-1).tps, 40, '首帧速率')
  // t=2000 再 40 字符: 窗口 [{1000,40},{2000,40}] span=1s → 80/1/4 = 20
  h.clock.now = 2000
  h.send('assistant.delta', { delta: 'x'.repeat(40) })
  near(h.rates.at(-1).tps, 20, '两样本速率')
  assert(h.rates.length === 2, '每次 delta 应恰好一次 setDecodeRate')
})

// ── T2: 3s 窗口裁剪 ────────────────────────────────────────────────
test('T2 超 3s 旧样本出窗, 速率只反映窗口内样本', async () => {
  const h = makeHarness()
  h.send('run.started', { run_id: 'r1' })
  h.send('assistant.delta', { delta: 'x'.repeat(40) })   // t=1000
  h.clock.now = 2000
  h.send('assistant.delta', { delta: 'x'.repeat(40) })   // t=2000
  h.clock.now = 5000
  h.send('assistant.delta', { delta: 'x'.repeat(40) })   // t=5000, cutoff=2000
  // 窗口 = [{2000,40},{5000,40}] (t=1000 出窗) → 80/3/4 = 6.666...
  near(h.rates.at(-1).tps, 80 / 3 / 4, '裁剪后速率')
})

// ── T3: 收口 ───────────────────────────────────────────────────────
test('T3 assistant.completed 后近似停止更新', async () => {
  const h = makeHarness()
  h.send('run.started', {})
  h.send('assistant.delta', { delta: 'abc' })
  const n = h.rates.length
  h.send('assistant.completed', { message_id: 'm1', content: 'abc' })
  h.send('assistant.delta', { delta: 'more' })
  eq(h.rates.length, n, 'assistant.completed 收口后不得再更新速率')
})
test('T3 run.completed / run.failed 后近似停止更新', async () => {
  for (const ev of ['run.completed', 'run.failed']) {
    const h = makeHarness()
    h.send('run.started', {})
    h.send('assistant.delta', { delta: 'abc' })
    const n = h.rates.length
    h.send(ev, { messages: [] })
    h.send('assistant.delta', { delta: 'more' })
    eq(h.rates.length, n, `${ev} 收口后不得再更新速率`)
  }
})

// ── T4: 权威校准 ───────────────────────────────────────────────────
test('T4 run.completed 校准: Δoutput÷生成时长=exactTps, reasoning 独立分桶', async () => {
  const h = makeHarness({ tokens: [
    { output_tokens: 100, reasoning_tokens: 10 },   // run.started 基线
    { output_tokens: 600, reasoning_tokens: 110 },  // run.completed 收口
  ] })
  h.send('run.started', { run_id: 'r1' })
  await flush(); await flush()   // 基线快照异步落位
  h.clock.now = 2000
  h.send('assistant.delta', { delta: 'x'.repeat(40) })   // firstDeltaAt=2000
  h.clock.now = 12000
  h.send('run.completed', { messages: [] })              // genSec = 10
  await flush(); await flush(); await flush()
  const asst = h.msgs.filter((m) => m.role === 'assistant').at(-1)
  assert(asst && asst.decodeStats, 'run.completed 后最后一条 assistant 应带 decodeStats')
  near(asst.decodeStats.exactTps, 500 / 10, 'exactTps = Δoutput(500)/10s')
  near(asst.decodeStats.reasoningTps, 100 / 10, 'reasoningTps = Δreasoning(100)/10s')
  assert(asst.decodeStats.approxTps != null, '近似末值应随校准落消息')
})
test('T4 基线缺失 (session API 失败) → 不落 decodeStats (宁缺勿假)', async () => {
  const h = makeHarness({ tokens: [null, { output_tokens: 600, reasoning_tokens: 0 }] })
  h.send('run.started', {})
  await flush(); await flush()
  h.send('assistant.delta', { delta: 'abc' })
  h.send('run.completed', { messages: [] })
  await flush(); await flush(); await flush()
  assert(!h.msgs.some((m) => m.decodeStats), '基线缺失不得造假精确值')
})
test('T4 run.failed 不校准 (仅 run.completed 触发)', async () => {
  const h = makeHarness({ tokens: [
    { output_tokens: 0, reasoning_tokens: 0 },
    { output_tokens: 600, reasoning_tokens: 0 },
  ] })
  h.send('run.started', {})
  await flush(); await flush()
  h.send('assistant.delta', { delta: 'abc' })
  h.send('run.failed', { messages: [] })
  await flush(); await flush(); await flush()
  assert(!h.msgs.some((m) => m.decodeStats), 'run.failed 不得触发校准')
})
test('T4 无 assistant 消息时校准静默跳过 (不新增消息)', async () => {
  const h = makeHarness({ tokens: [
    { output_tokens: 0, reasoning_tokens: 0 },
    { output_tokens: 600, reasoning_tokens: 0 },
  ] })
  h.send('run.started', {})
  await flush(); await flush()
  h.send('run.completed', { messages: [] })   // 无 delta, 无气泡
  await flush(); await flush(); await flush()
  eq(h.msgs.length, 0, '无 assistant 气泡时不得凭空造消息')
})

// ── T5: i18n 对称 ──────────────────────────────────────────────────
function flat(d, p = '') {
  const out = {}
  for (const [k, v] of Object.entries(d)) {
    const kk = p ? `${p}.${k}` : k
    if (v && typeof v === 'object') Object.assign(out, flat(v, kk))
    else out[kk] = v
  }
  return out
}
test('T5 chat.decode_speed.* 双语键集 diff = ∅ 且 5 键双边在位', () => {
  const fEn = flat(en), fZh = flat(zh)
  const keys = Object.keys(fEn).filter((k) => k.startsWith('chat.decode_speed.'))
  eq(keys.sort(), [
    'chat.decode_speed.approx', 'chat.decode_speed.exact', 'chat.decode_speed.label',
    'chat.decode_speed.reasoning_suffix', 'chat.decode_speed.tooltip',
  ].sort(), 'decode_speed 键集')
  for (const k of keys) {
    assert(typeof fZh[k] === 'string' && fZh[k], `zh 缺键 ${k}`)
    assert(fEn[k] !== fZh[k] || k.endsWith('unit'), `键 ${k} 双语未区分`)
  }
  // 近似标注必须含「approx/近似」字样 (口径: 显示应标注近似)
  assert(/approx/i.test(fEn['chat.decode_speed.approx']), 'en approx 文案须标 approx')
  assert(/近似/.test(fZh['chat.decode_speed.approx']), 'zh approx 文案须标 近似')
})
test('T5 全字典扁平键集 diff = ∅ (双语对称总闸)', () => {
  const fEn = flat(en), fZh = flat(zh)
  const diff = [...new Set([...Object.keys(fEn), ...Object.keys(fZh)])]
    .filter((k) => !(k in fEn && k in fZh))
  eq(diff, [], 'en/zh 键集 diff 必须为空')
})

// ── T6: 显示层源码钉 ───────────────────────────────────────────────
test('T6 显示全部经 t(), 无硬编码单语', () => {
  assert(/t\('chat\.decode_speed\.approx', \{ n: Math\.round\(decodeRate\.tps\) \}\)/.test(chat),
    '状态条近似值应经 t(chat.decode_speed.approx)')
  assert(/t\('chat\.decode_speed\.exact', \{ n: m\.decodeStats\.exactTps\.toFixed\(1\) \}\)/.test(chat),
    '收口行精确值应经 t(chat.decode_speed.exact)')
  assert(/t\('chat\.decode_speed\.reasoning_suffix'/.test(chat),
    'reasoning 分桶文案应经 t()')
  assert(/t\('chat\.decode_speed\.tooltip'\)/.test(chat),
    'tooltip 应经 t()')
})
test('T6 新回合/摘流重置在位 (runStream/send/detachStream 三处)', () => {
  const resets = chat.match(/setDecodeRate\(null\)/g) || []
  assert(resets.length >= 3, `setDecodeRate(null) 重置应 ≥3 处 (实际 ${resets.length})`)
})
test('T6 校准取数仅走同源 /api/sessions/{sid} (零后端改动约束)', () => {
  assert(/fetch\(`\/api\/sessions\/\$\{encodeURIComponent\(sid\)\}`\)/.test(chat),
    'fetchSessionTokens 应走同源 GET /api/sessions/{sid}')
  const seg = chat.slice(chat.indexOf('fetchSessionTokens: async'), chat.indexOf('fetchSessionTokens: async') + 500)
  assert(!/method:|POST/.test(seg), '校准取数必须是 GET, 不得引入写操作')
})

// ── runner ─────────────────────────────────────────────────────────
let failed = 0
for (const t of tests) {
  try { await t.fn(); console.log(`  PASS ${t.name}`) }
  catch (e) { failed++; console.error(`  FAIL ${t.name}\n    ${e.message}`) }
}
console.log(failed ? `\n${failed}/${tests.length} FAILED` : `\nALL ${tests.length} PASSED`)
process.exit(failed ? 1 : 0)
