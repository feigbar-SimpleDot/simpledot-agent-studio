#!/usr/bin/env node
// 单测: ChatView.jsx 的 toolgroup / reasoning 纯函数
// (issue #7-1 基线 + issue #1 B面 REOPEN-DESIGN §2.8 反转与新增)。
// 被测函数住在 .jsx 组件文件里无法直接 import, 沿用「源码抽取直测」路线:
// 按标记 + 花括号/圆括号配平从 ChatView.jsx 抽出函数体, 注入 node:vm 沙箱执行。
// 抽取即钉住源码现状 —— 函数被改动而本测试未同步时, 抽取失败或用例失败即报警。
import { readFileSync } from 'fs'
import vm from 'node:vm'

const SRC = new URL('../src/views/ChatView.jsx', import.meta.url)
const src = readFileSync(SRC, 'utf8')

// 从 startMarker 起做花括号+圆括号联合配平, 返回完整函数/箭头函数文本。
// 说明: 被测函数体内 ${...} 与注释均花括号自配平; 圆括号须一并跟踪,
// 否则 `=> setMessages((m) => { ... })` 会在花括号闭合处截断少一个 ')'。
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
  throw new Error(`括号不配平: ${startMarker}`)
}

// 组装沙箱: 被测函数 + 其依赖。
// buildTurnStreamHandler 内部闭包含 findStreaming/appendField/pollReasoning,
// 工厂整体抽出后, findStreaming/appendField 从工厂返回值不可见 —— 但旧测试
// 直接测它们, 故从工厂源码文本里再单独抽出这两个箭头函数定义供直测。
const factoryText = extractFn(src, 'function buildTurnStreamHandler')
const parts = [
  extractFn(src, 'function fmtToolArgs'),
  extractFn(src, 'function pushToolCall'),
  extractFn(src, 'function closeTurnToolGroups'),
  extractFn(src, 'function findRunningToolItem'),
  extractFn(src, 'function attachToolResult'),
  extractFn(src, 'function backfillToolResults'),
  extractFn(src, 'function extractReasoningSegments'),
  extractFn(src, 'function authoritativeReasoning'),
  extractFn(src, 'function appendTurnThinking'),
  extractFn(src, 'function sealTurnThinking'),
  factoryText,
  extractFn(src, 'function finalizeTurn'),
  // 从源码二次抽出工厂内闭包 (extractFn 花括号+圆括号配平, 位置无关)
  extractFn(src, 'const findStreaming = (m) =>'),
  extractFn(src, 'const appendField = (field, chunk) =>'),
]
const ctx = vm.createContext({ Date, JSON, Math, console, Map, Set,
  setInterval: () => 0, clearInterval: () => {} })
// const 声明不进 vm 全局对象, 沙箱内显式导出到 globalThis 再取
vm.runInContext(parts.join('\n;\n') +
  '\nglobalThis.__exports = { pushToolCall, closeTurnToolGroups,' +
  ' findRunningToolItem, attachToolResult, backfillToolResults,' +
  ' extractReasoningSegments, authoritativeReasoning, appendTurnThinking,' +
  ' sealTurnThinking, buildTurnStreamHandler, finalizeTurn,' +
  ' findStreaming, appendField };', ctx)
const { pushToolCall, closeTurnToolGroups,
        findRunningToolItem, attachToolResult, backfillToolResults,
        extractReasoningSegments, authoritativeReasoning, appendTurnThinking,
        sealTurnThinking, buildTurnStreamHandler, finalizeTurn,
        findStreaming, appendField } = ctx.__exports

// setMessages 替身: 函数式更新语义, 状态收进 box.m
function makeStore(initial = []) {
  const box = { m: initial }
  box.set = (fn) => { box.m = typeof fn === 'function' ? fn(box.m) : fn }
  return box
}
const grp = (items, closed = false) =>
  ({ role: 'toolgroup', items, ts: 1, ...(closed ? { closed: true } : {}) })
const item = (name, status = 'running', result = null) =>
  ({ name, args: '', result, status, ts: 1 })

const tests = []
function test(name, fn) { tests.push({ name, fn }) }
function assert(cond, msg) { if (!cond) throw new Error(msg) }

// ---- T-C4: 连续 3 个 tool.started → 1 个 toolgroup 3 items ----
test('T-C4 连续 pushToolCall 合并进同一未关闭组', () => {
  const s = makeStore()
  pushToolCall(s.set, { tool_name: 'read_file', args: { p: 'a' } })
  pushToolCall(s.set, { tool_name: 'terminal', args: 'ls' })
  pushToolCall(s.set, { tool_name: 'web_search', args: { q: 'x' } })
  assert(s.m.length === 1, `期望 1 组, 实际 ${s.m.length}`)
  assert(s.m[0].role === 'toolgroup', 'role 应为 toolgroup')
  assert(s.m[0].items.length === 3, `期望 3 items, 实际 ${s.m[0].items.length}`)
  assert(s.m[0].items.every(i => i.status === 'running'), '新条目应全 running')
  assert(s.m[0].items[0].name === 'read_file' && s.m[0].items[1].name === 'terminal',
    '条目顺序/name 错误')
})

test('pushToolCall: 缺 tool_name 兜底为 "tool"', () => {
  const s = makeStore()
  pushToolCall(s.set, {})
  assert(s.m[0].items[0].name === 'tool', '缺 name 应兜底 tool')
})

test('pushToolCall: 末组已 closed → 开新组', () => {
  const s = makeStore([grp([item('a')], true)])
  pushToolCall(s.set, { tool_name: 'b' })
  assert(s.m.length === 2, `closed 组后应开新组, 实际 ${s.m.length}`)
  assert(s.m[1].items[0].name === 'b', '新组条目错误')
})

// appendField 是工厂内闭包, setMessages 是自由变量 → 每次调用前注入沙箱全局。
function callAppendField(store, field, chunk) {
  ctx.setMessages = store.set
  appendField(field, chunk)
}

// ---- 【反转 T-C5】穿插不断组 (issue #1 B-AC1, 旧「穿插关组」断言作废) ----
test('T-C5(反转) 穿插不断组: push → appendField → push 仍 1 组 2 items', () => {
  const s = makeStore()
  pushToolCall(s.set, { tool_name: 'read_file' })
  callAppendField(s, 'commentary', '想一下')  // 穿插 → 不再关组
  pushToolCall(s.set, { tool_name: 'terminal' })
  assert(s.m.length === 2, `期望 组/气泡 共 2, 实际 ${s.m.length}`)
  assert(s.m[0].role === 'toolgroup' && s.m[0].closed === undefined,
    '穿插后首组不应 closed')
  assert(s.m[0].items.length === 2, `穿插后应同组 2 items, 实际 ${s.m[0].items.length}`)
  assert(s.m[0].items[1].name === 'terminal', '第二个工具应挂回同一组')
  assert(s.m[1].role === 'assistant' && s.m[1].commentary === '想一下', '气泡字段错误')
})

test('appendField 全路径不再产生 closed 字段 (B-AC1)', () => {
  const s = makeStore()
  pushToolCall(s.set, { tool_name: 'a' })
  callAppendField(s, 'text', '正文穿插')
  callAppendField(s, 'commentary', '评论穿插')
  pushToolCall(s.set, { tool_name: 'b' })
  assert(s.m.every(x => x.closed === undefined), 'appendField 不应产生任何 closed')
  const groups = s.m.filter(x => x.role === 'toolgroup')
  assert(groups.length === 1 && groups[0].items.length === 2, '全程恒 1 组 2 items')
})

test('穿插 turnthinking 气泡也不断组 (回合级单卡)', () => {
  const s = makeStore()
  pushToolCall(s.set, { tool_name: 'a' })
  appendTurnThinking(s.set, '思考片段')
  pushToolCall(s.set, { tool_name: 'b' })
  const groups = s.m.filter(x => x.role === 'toolgroup')
  assert(groups.length === 1 && groups[0].items.length === 2,
    `思考穿插后应仍 1 组 2 items, 实际 ${groups.length} 组`)
})

// ---- closeTurnToolGroups: 回合结束封组 ----
test('closeTurnToolGroups: 多组全置 closed', () => {
  const s = makeStore([grp([item('a')]), { role: 'assistant' }, grp([item('b')])])
  closeTurnToolGroups(s.set)
  const groups = s.m.filter(x => x.role === 'toolgroup')
  assert(groups.length === 2 && groups.every(g => g.closed === true), '全部组应 closed')
})

test('closeTurnToolGroups: 幂等, 二次调用返回同一引用零更新', () => {
  const s = makeStore([grp([item('a')])])
  closeTurnToolGroups(s.set)
  const after1 = s.m
  closeTurnToolGroups(s.set)
  assert(s.m === after1, '已全 closed 时二次调用不应产生新数组')
})

test('closeTurnToolGroups: 不可变性, 旧组对象未被就地改', () => {
  const g = grp([item('a')])
  const orig = [g]
  const s = makeStore(orig)
  closeTurnToolGroups(s.set)
  assert(s.m !== orig, '应返回新数组')
  assert(s.m[0] !== g, '组对象应是新引用')
  assert(g.closed === undefined, '旧组被就地改 closed')
})

test('封组后 pushToolCall 开新组 (回合边界)', () => {
  const s = makeStore([grp([item('a')])])
  closeTurnToolGroups(s.set)
  pushToolCall(s.set, { tool_name: 'b' })
  assert(s.m.length === 2 && s.m[1].items[0].name === 'b' && !s.m[1].closed,
    '封组后新工具应开新组')
})

// ---- B-AC1 压力用例: 穿插 12 push + 11 appendField → 恒 1 组 12 items ----
test('压力: 12 工具穿插 11 次正文追加 → 恒 1 组 12 items', () => {
  const s = makeStore()
  for (let k = 0; k < 12; k++) {
    pushToolCall(s.set, { tool_name: 'tool' + k })
    if (k < 11) callAppendField(s, 'text', 'x' + k)
  }
  const groups = s.m.filter(x => x.role === 'toolgroup')
  assert(groups.length === 1, `期望恒 1 组, 实际 ${groups.length}`)
  assert(groups[0].items.length === 12, `期望 12 items, 实际 ${groups[0].items.length}`)
  assert(groups[0].closed === undefined, '未收口不应 closed')
})

// ---- findRunningToolItem: 同名优先 / 退化最后 running / 无命中 ----
test('findRunningToolItem 优先同名最后 running (跨组扫)', () => {
  const m = [grp([item('a'), item('b')]), grp([item('a')])]
  const hit = findRunningToolItem(m, 'a')
  assert(hit && hit.gi === 1 && hit.ij === 0, `同名应命中第2组, got ${JSON.stringify(hit)}`)
})

test('findRunningToolItem 同名无 running → 退化整体最后 running', () => {
  const m = [grp([item('x'), item('a', 'done')]), grp([item('y')])]
  const hit = findRunningToolItem(m, 'a')
  assert(hit && hit.gi === 1 && hit.ij === 0, `应退化到最后 running(y), got ${JSON.stringify(hit)}`)
})

test('findRunningToolItem 无 running → null', () => {
  const m = [grp([item('a', 'done')]), { role: 'assistant' }]
  assert(findRunningToolItem(m, 'a') === null, '应返回 null')
})

// ---- T-C6: A/B/A 交错完成, 结果按 name 挂回各自条目不串卡 ----
test('T-C6 A/B/A 交错 attachToolResult 按 name 精确挂回', () => {
  const s = makeStore([grp([item('read_file'), item('terminal'), item('read_file')])])
  attachToolResult(s.set, { tool_name: 'read_file', preview: 'R2' }, false)  // 后一个 read_file
  attachToolResult(s.set, { tool_name: 'terminal', preview: 'T1' }, false)
  attachToolResult(s.set, { tool_name: 'read_file', preview: 'R1' }, false)  // 前一个 read_file
  const its = s.m[0].items
  assert(its[0].result === 'R1' && its[1].result === 'T1' && its[2].result === 'R2',
    `挂错条目: ${its.map(i => i.result).join(',')}`)
  assert(its.every(i => i.status === 'done'), '成功结果应置 done')
})

test('attachToolResult failed=true → status=error, error 文本进 result', () => {
  const s = makeStore([grp([item('a')])])
  attachToolResult(s.set, { tool_name: 'a', error: 'boom' }, true)
  assert(s.m[0].items[0].status === 'error', 'failed 应置 error')
  assert(s.m[0].items[0].result === 'boom', 'error 文本应进 result')
})

test('attachToolResult: 无命中 → 原样返回不改', () => {
  const m0 = [grp([item('a', 'done')])]
  const s = makeStore(m0)
  attachToolResult(s.set, { tool_name: 'zzz', preview: 'x' }, false)
  assert(s.m === m0, '无命中应返回同一引用')
})

test('attachToolResult: 结果截断 4000 字符', () => {
  const s = makeStore([grp([item('a')])])
  attachToolResult(s.set, { tool_name: 'a', preview: 'x'.repeat(5000) }, false)
  assert(s.m[0].items[0].result.length === 4000, '应截断到 4000')
})

test('attachToolResult 不可变性: 旧数组/旧组/旧条目均未被就地改', () => {
  const it = item('a')
  const g = grp([it])
  const orig = [g]
  const s = makeStore(orig)
  attachToolResult(s.set, { tool_name: 'a', preview: 'ok' }, false)
  assert(s.m !== orig, '应返回新数组')
  assert(s.m[0] !== g, '组对象应是新引用')
  assert(s.m[0].items !== g.items, 'items 数组应是新引用')
  assert(it.result === null && it.status === 'running', '旧条目被就地改 — React 漏渲染隐患')
  assert(g.closed === undefined && orig.length === 1, '旧组/旧数组被就地改')
})

// ---- backfillToolResults: 从后往前配对 / 只填空 / 守卫 / 不可变 ----
test('backfill 从后往前配对: 最后未填条目 ← 最后一条 tool 消息', () => {
  const s = makeStore([grp([item('a'), item('b')])])
  backfillToolResults(s.set, [
    { role: 'user', content: 'q' },
    { role: 'tool', content: 'OUT_A' },
    { role: 'tool', content: 'OUT_B' },
  ])
  assert(s.m[0].items[0].result === 'OUT_A', `items[0] got ${s.m[0].items[0].result}`)
  assert(s.m[0].items[1].result === 'OUT_B', `items[1] got ${s.m[0].items[1].result}`)
})

test('backfill 只填 result==null 的条目, 已填不动', () => {
  const s = makeStore([grp([item('a', 'done', '已有'), item('b')])])
  backfillToolResults(s.set, [{ role: 'tool', content: 'NEW' }])
  assert(s.m[0].items[0].result === '已有', '已填条目被覆盖')
  assert(s.m[0].items[1].result === 'NEW', '未填条目应被填')
})

test('backfill 卡片多于 tool 消息: 只配 min(n) 对, 靠前的留空', () => {
  const s = makeStore([grp([item('a'), item('b'), item('c')])])
  backfillToolResults(s.set, [{ role: 'tool', content: 'X' }])
  assert(s.m[0].items[2].result === 'X', '最后条目应配对')
  assert(s.m[0].items[0].result === null && s.m[0].items[1].result === null, '多余条目应留空')
})

test('backfill 守卫: 非数组/空数组/无 tool 消息 → 不触发 setMessages', () => {
  let called = false
  backfillToolResults((fn) => { called = true }, null)
  backfillToolResults((fn) => { called = true }, [])
  backfillToolResults((fn) => { called = true }, [{ role: 'user', content: 'x' }])
  assert(!called, '守卫路径不应触发更新')
})

test('backfill 不可变性: 旧组旧条目未被就地改', () => {
  const it = item('a')
  const g = grp([it])
  const s = makeStore([g])
  backfillToolResults(s.set, [{ role: 'tool', content: 'OUT' }])
  assert(s.m[0] !== g && s.m[0].items !== g.items, '应逐层复制')
  assert(it.result === null, '旧条目被就地改')
})

test('backfill 对象型 content 序列化为缩进 JSON', () => {
  const s = makeStore([grp([item('a')])])
  backfillToolResults(s.set, [{ role: 'tool', content: { k: 1 } }])
  assert(s.m[0].items[0].result === '{\n  "k": 1\n}', '对象 content 应 JSON.stringify(,,2)')
})

// ---- extractReasoningSegments (DESIGN §2.3③) ----
test('extract: 新消息全量返回, 游标推进', () => {
  const seen = new Map()
  const segs = extractReasoningSegments(
    [{ id: 'm1', role: 'assistant', reasoning: 'AAA' }], seen)
  assert(segs.length === 1 && segs[0].text === 'AAA' && segs[0].key === 'm1',
    `全量返回错误: ${JSON.stringify(segs)}`)
  assert(seen.get('m1') === 3, '游标应推进到 3')
})

test('extract: 同消息增长只回尾巴', () => {
  const seen = new Map()
  extractReasoningSegments([{ id: 'm1', role: 'assistant', reasoning: 'AB' }], seen)
  const segs = extractReasoningSegments([{ id: 'm1', role: 'assistant', reasoning: 'ABCD' }], seen)
  assert(segs.length === 1 && segs[0].text === 'CD', `应只回尾巴 CD, got ${JSON.stringify(segs)}`)
  assert(seen.get('m1') === 4, '游标应推进到 4')
})

test('extract: reasoning_content 字段兼容 + 非字符串 String() 化', () => {
  const seen = new Map()
  const segs = extractReasoningSegments(
    [{ id: 'a', role: 'assistant', reasoning_content: 'via_content' },
     { id: 'b', role: 'assistant', reasoning: 123 }], seen)
  assert(segs.length === 2, '两字段都应识别')
  assert(segs[0].text === 'via_content', 'reasoning_content 应被读到')
  assert(segs[1].text === '123', '非字符串应 String() 化')
})

test('extract: 非 assistant / 空 / null 字段跳过', () => {
  const seen = new Map()
  const segs = extractReasoningSegments(
    [{ role: 'user', reasoning: 'x' },
     { id: 'a', role: 'assistant' },
     { id: 'b', role: 'assistant', reasoning: '' },
     { id: 'c', role: 'assistant', reasoning: null }], seen)
  assert(segs.length === 0, `应全跳过, got ${JSON.stringify(segs)}`)
})

test('extract: 无 id 消息用 #idx 兜底键, 重复调用不重复返回', () => {
  const seen = new Map()
  const msgs = [{ role: 'assistant', reasoning: 'tail' }]
  const s1 = extractReasoningSegments(msgs, seen)
  const s2 = extractReasoningSegments(msgs, seen)
  assert(s1.length === 1 && s1[0].key === '#0', '无 id 应兜底 #0')
  assert(s2.length === 0, '游标已消费, 二次调用应空')
})

test('extract: 非数组入参 → 空数组', () => {
  assert(extractReasoningSegments(null, new Map()).length === 0, 'null 应返回空')
})

// ---- authoritativeReasoning (DESIGN §2.3④) ----
test('authoritative: 多 assistant reasoning 按序拼接', () => {
  const out = authoritativeReasoning([
    { role: 'user', content: 'q' },
    { role: 'assistant', reasoning: 'P1' },
    { role: 'tool', content: 'r' },
    { role: 'assistant', reasoning_content: 'P2' },
  ])
  assert(out === 'P1P2', `应拼接 P1P2, got ${out}`)
})

test('authoritative: 无 reasoning / 非数组 → 空串', () => {
  assert(authoritativeReasoning([{ role: 'assistant', content: 'x' }]) === '', '无字段应空串')
  assert(authoritativeReasoning(null) === '', 'null 应空串')
})

// ---- appendTurnThinking / sealTurnThinking (回合思考卡生长与收口) ----
test('appendTurnThinking: 首段建卡, 后续段生长进同一张卡', () => {
  const s = makeStore()
  appendTurnThinking(s.set, '第一段')
  appendTurnThinking(s.set, '第二段')
  const cards = s.m.filter(x => x.role === 'turnthinking')
  assert(cards.length === 1, `思考卡应恒 1 张, 实际 ${cards.length}`)
  assert(cards[0].text === '第一段第二段' && cards[0].streaming === true,
    '生长/流式态错误')
})

test('appendTurnThinking 不可变性: 旧卡未被就地改', () => {
  const s = makeStore()
  appendTurnThinking(s.set, 'A')
  const first = s.m[0]
  appendTurnThinking(s.set, 'B')
  assert(first.text === 'A', '旧卡被就地改')
  assert(s.m[0] !== first, '应新引用')
})

test('seal: auth 非空 → 权威覆盖 + streaming=false', () => {
  const s = makeStore([{ role: 'turnthinking', text: '半截', streaming: true, ts: 1 }])
  sealTurnThinking(s.set, '权威全文')
  assert(s.m[0].text === '权威全文' && s.m[0].streaming === false, '权威覆盖错误')
})

test('seal: auth 空 + 卡存在 → 保留轮询内容只停流式 (不造假)', () => {
  const s = makeStore([{ role: 'turnthinking', text: '轮询所得', streaming: true, ts: 1 }])
  sealTurnThinking(s.set, '')
  assert(s.m[0].text === '轮询所得' && s.m[0].streaming === false, '应保留内容只停流式')
})

test('seal: auth 空 + 无卡 → 不创建 (B-AC5 无 reasoning 不渲染)', () => {
  const s = makeStore([{ role: 'assistant', text: 'hi' }])
  sealTurnThinking(s.set, '')
  assert(s.m.length === 1 && !s.m.some(x => x.role === 'turnthinking'), '不应凭空造思考卡')
})

test('seal: auth 非空 + 无卡 → 建收口卡 (轮询全漏时兜底)', () => {
  const s = makeStore()
  sealTurnThinking(s.set, '迟到权威')
  assert(s.m.length === 1 && s.m[0].role === 'turnthinking' && s.m[0].text === '迟到权威'
    && s.m[0].streaming === false, '应建收口卡')
})

// ---- finalizeTurn: 流退出兜底 ----
test('finalizeTurn: 封组 + 思考卡停流式 + 气泡收尾', () => {
  const s = makeStore([
    grp([item('a')]),
    { role: 'turnthinking', text: '思考', streaming: true, ts: 1 },
    { role: 'assistant', text: '正文', streaming: true, ts: 1 },
  ])
  finalizeTurn(s.set, { reasoning: null, elapsed: null })
  assert(s.m[0].closed === true, '组应封')
  assert(s.m[1].streaming === false && s.m[1].text === '思考', '思考卡应保留内容停流式')
  assert(s.m[2].streaming === false, '气泡应停流式')
})

test('finalizeTurn: 空消息列表零副作用 (幂等)', () => {
  const s = makeStore([])
  finalizeTurn(s.set, { reasoning: null, elapsed: null })
  assert(s.m.length === 0, '空态不应新增任何消息')
})

// ---- buildTurnStreamHandler: 工厂事件语义 (mock 依赖直测) ----
function makeHandler(fetchImpl) {
  const s = makeStore()
  const phases = [], runs = []
  const deps = {
    setMessages: s.set,
    setRunInfo: (x) => runs.push(x),
    setPhase: (p) => phases.push(p),
    turnDedupRef: { current: null },
    reasoningSeenRef: { current: new Map() },
    reasoningTimerRef: { current: null },
    runIdRef: { current: null },
    fetchMessages: fetchImpl,
  }
  return { handler: buildTurnStreamHandler(deps), store: s, deps, phases }
}

test('handler: run.started 重建去重集并启动轮询 (timer 置位)', () => {
  const { handler, deps } = makeHandler(async () => [])
  handler('run.started', { run_id: 'r1' })
  assert(deps.runIdRef.current === 'r1', 'runId 未记录')
  assert(deps.turnDedupRef.current && deps.turnDedupRef.current.thinking instanceof Set,
    '去重集未重建')
  assert(deps.reasoningTimerRef.current === 0, '轮询 timer 应置位 (沙箱 setInterval 恒 0)')
})

test('handler: tool.progress(_thinking) 不再渲染任何 thinking 字段 (根因切断)', () => {
  const { handler, store } = makeHandler(async () => [])
  handler('run.started', {})
  handler('tool.progress', { tool_name: '_thinking', delta: '这是可见正文预览不是reasoning' })
  assert(store.m.length === 0, 'relay 预览不应产生任何消息')
})

test('handler: run.completed 收口链 — 封组 + 权威覆盖 + 回填 + 停 timer', async () => {
  const { handler, store, deps } = makeHandler(async () => [])
  handler('run.started', {})
  pushToolCall(store.set, { tool_name: 'a' })
  appendTurnThinking(store.set, '轮询半截')
  await Promise.resolve()
  handler('run.completed', {
    messages: [
      { role: 'assistant', reasoning: '权威思考全文' },
      { role: 'tool', content: 'OUT' },
    ],
  })
  assert(deps.reasoningTimerRef.current === null, 'timer 应清')
  assert(store.m.find(x => x.role === 'toolgroup').closed === true, '组应封')
  const card = store.m.find(x => x.role === 'turnthinking')
  assert(card.text === '权威思考全文' && card.streaming === false, '权威覆盖错误')
  assert(store.m.find(x => x.role === 'toolgroup').items[0].result === 'OUT', '回填失败')
})

test('handler: poll.recovered 与轮询共用去重不重复渲染思考', async () => {
  let raw = [{ id: 'm1', role: 'assistant', content: '正文', reasoning: '思考X' }]
  const { handler, store } = makeHandler(async () => raw)
  handler('run.started', {})
  // 模拟一次轮询 tick 直接调 handler 不可达(pollReasoning 是闭包), 这里验证
  // poll.recovered 路径: 消息进 msgIds 后同 id 不再重复渲染
  handler('poll.recovered', { raw: raw[0] })
  const before = store.m.length
  handler('poll.recovered', { raw: raw[0] })  // 同 id 重放
  assert(store.m.length === before, '同 id 重放不应重复渲染')
})

// ---- REOPEN-FIX P1-1: 跨回合 reasoning 污染 (全历史 GET messages, 复审必测 §5.1) ----
test('P1-1 全历史mock: 历史含旧reasoning本轮无 → 轮询不建卡', async () => {
  // 真实 gateway 语义: GET messages 返回全会话历史, 含上一轮的 reasoning。
  const history = [
    { id: 'u1', role: 'user', content: '上一轮提问' },
    { id: 'm1', role: 'assistant', content: '上一轮回答', reasoning: 'TURN1_REASONING' },
  ]
  const { handler, store } = makeHandler(async () => history)
  handler('run.started', { run_id: 'r2' })
  await handler.captureTurnLowerBound()   // 锚定下界 = 2 (历史已 2 条)
  await handler.pollReasoning()          // 第一个 tick
  await handler.pollReasoning()          // 第二个 tick
  assert(store.m.length === 0,
    `本轮无 reasoning 不应建卡, 实际 ${JSON.stringify(store.m)}`)
})

test('P1-1 全历史mock: 本轮有reasoning → 卡内只含本轮片段无上一轮文本', async () => {
  const history = [
    { id: 'u1', role: 'user', content: '上一轮提问' },
    { id: 'm1', role: 'assistant', content: '上一轮回答', reasoning: 'TURN1_REASONING' },
  ]
  const { handler, store } = makeHandler(async () => history)
  handler('run.started', { run_id: 'r2' })
  await handler.captureTurnLowerBound()
  // 本回合新 assistant 消息落库 (带本轮 reasoning)
  history.push({ id: 'm2', role: 'assistant', content: '本轮回答', reasoning: 'TURN2_REASONING' })
  await handler.pollReasoning()
  const cards = store.m.filter(x => x.role === 'turnthinking')
  assert(cards.length === 1, `应恒 1 张思考卡, 实际 ${cards.length}`)
  assert(cards[0].text === 'TURN2_REASONING',
    `卡内只应含本轮片段, got ${JSON.stringify(cards[0].text)}`)
  assert(!cards[0].text.includes('TURN1'), '卡内混入上一轮 reasoning')
})

test('P1-1 下界未就绪(null)时轮询跳过, 收口由 authoritative 兜底', async () => {
  const history = [{ id: 'm1', role: 'assistant', reasoning: 'OLD' }]
  const { handler, store } = makeHandler(async () => history)
  handler('run.started', {})
  // 不调 captureTurnLowerBound (模拟下界捕获失败) → 轮询必须整体跳过
  await handler.pollReasoning()
  assert(store.m.length === 0, '下界未就绪时轮询不得渲染任何思考')
  // 回合末: run.completed 只带本回合 transcript 且无 reasoning → 不建卡
  handler('run.completed', { messages: [{ role: 'assistant', content: '本轮回答' }] })
  assert(!store.m.some(x => x.role === 'turnthinking'), '收口不得凭空造思考卡')
})

// ---- runner ----
let pass = 0, fail = 0
for (const t of tests) {
  try {
    await t.fn()
    console.log(`  ✓ ${t.name}`)
    pass++
  } catch (e) {
    console.log(`  ✗ ${t.name}\n    ${e.message}`)
    fail++
  }
}
console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail === 0 ? 0 : 1)
