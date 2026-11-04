#!/usr/bin/env node
// 守护: issue #209 流式中切换 session + 视图级摘流。
//
// 两条路线 (沿用 test_issue63_stop.mjs 套路):
//   A) gateway.js 真实 import + mock fetch —— 摘流行为实测:
//      chatStream(sid, ..., opts={activeSidRef}) 在 activeSidRef.current
//      ≠ 流所属 sid 时, 所有事件 (assistant.delta / run.* / poll.broken /
//      poll.recovered fallback 路径) 一律丢弃; 一致时全量透传; 不传 opts
//      保持旧行为 (向后兼容 test_sse_parse.mjs 既有调用)。
//   B) ChatView.jsx 源码静态断言 —— busy 闸门拆除、switchSession/newChat
//      摘流接线、世代守卫、三处 disabled 解除 (ChatView 是 React 组件,
//      不引 jsdom; 交互真机验收归 s6 tester)。
import { readFileSync } from 'fs'
import { makeGw } from '../src/gateway.js'

const tests = []
function test(name, fn) { tests.push({ name, fn }) }
function assert(cond, msg) { if (!cond) throw new Error(msg) }

const originalFetch = global.fetch
const enc = new TextEncoder()

// mock: SSE 流按 chunks 推送
function streamResponse(chunks) {
  let i = 0
  const stream = new ReadableStream({
    start(c) {
      const push = () => {
        if (i >= chunks.length) { c.close(); return }
        c.enqueue(enc.encode(chunks[i++]))
        setTimeout(push, 0)
      }
      setTimeout(push, 0)
    },
  })
  return { ok: true, status: 200, body: stream }
}

// mock: 流中途断 (transportBroken → fallback 轮询)
function brokenStreamResponse(chunks) {
  const stream = new ReadableStream({
    start(c) {
      c.enqueue(enc.encode(chunks[0]))
      setTimeout(() => c.error(new Error('connection reset')), 0)
    },
  })
  return { ok: true, status: 200, body: stream }
}

const SSE_FRAMES = [
  'event: run.started\ndata: {"run_id":"r1"}\n\n',
  'event: assistant.delta\ndata: {"delta":"tok-A"}\n\n',
  'event: assistant.delta\ndata: {"delta":"tok-B"}\n\n',
  'event: run.completed\ndata: {"status":"ok"}\n\n',
]

// ──────────────────────────────────────────────────────────────────
// A) gateway.js 摘流行为实测
// ──────────────────────────────────────────────────────────────────

test('A1 同会话: activeSidRef 一致 → 事件全量透传', async () => {
  const ref = { current: 'sess-A' }
  const events = []
  global.fetch = async () => streamResponse(SSE_FRAMES)
  await makeGw('').chatStream('sess-A', 'hi', (ev, d) => events.push([ev, d]),
    undefined, { activeSidRef: ref })
  global.fetch = originalFetch
  assert(events.length === 4, `应 4 帧全透传, 实际 ${events.length}`)
  assert(events[1][0] === 'assistant.delta' && events[1][1].delta === 'tok-A', 'delta 内容错')
})

test('A2 不传 opts: 旧行为全量透传 (向后兼容)', async () => {
  const events = []
  global.fetch = async () => streamResponse(SSE_FRAMES)
  await makeGw('').chatStream('sess-A', 'hi', (ev, d) => events.push(ev))
  global.fetch = originalFetch
  assert(events.length === 4, `不传 opts 应 4 帧, 实际 ${events.length}`)
})

test('A3 起流前已切走: activeSidRef≠sid → 全部丢弃 (摘流干净)', async () => {
  const ref = { current: 'sess-B' }   // 流属 A, 画面已在 B
  const events = []
  global.fetch = async () => streamResponse(SSE_FRAMES)
  await makeGw('').chatStream('sess-A', 'hi', (ev) => events.push(ev),
    undefined, { activeSidRef: ref })
  global.fetch = originalFetch
  assert(events.length === 0, `残余 token 不得落新会话, 实际收到 ${events.length} 帧`)
})

test('A4 流式中切换: 切换前帧透传, 切换后残余帧全丢', async () => {
  const ref = { current: 'sess-A' }
  const events = []
  // 首帧透传后把画面切到 B (等价用户点击切换的瞬间) —— 此后 A 流残余
  // token 必须全丢。在事件回调内翻转 ref: dispatch→emit→onEvent 同步链,
  // 无生产者/消费者竞态, 判定确定。
  global.fetch = async () => streamResponse(SSE_FRAMES)
  await makeGw('').chatStream('sess-A', 'hi', (ev, d) => {
    events.push([ev, d])
    if (events.length === 1) ref.current = 'sess-B'
  }, undefined, { activeSidRef: ref })
  global.fetch = originalFetch
  assert(events.length === 1 && events[0][0] === 'run.started',
    `只应收到切换前 1 帧, 实际 ${JSON.stringify(events.map(e => e[0]))}`)
  const leaked = events.filter(e => e[0] === 'assistant.delta')
  assert(leaked.length === 0, `旧流残余 token 泄漏进新会话: ${JSON.stringify(leaked)}`)
})

test('A5 fallback 轮询路径同样摘流 (poll.broken 后切走 → poll.recovered 丢)', async () => {
  const ref = { current: 'sess-A' }
  const events = []
  const recoveredMsg = { id: 9, role: 'assistant', content: '断连后落地全文',
    finish_reason: 'stop' }
  global.fetch = async (url) => {
    if (String(url).includes('/chat/stream')) {
      return brokenStreamResponse([SSE_FRAMES[0]])
    }
    // fallback 轮询的 messages 端点
    return { ok: true, status: 200, json: async () => ({ data: [recoveredMsg] }) }
  }
  await makeGw('').chatStream('sess-A', 'hi', (ev, d) => {
    events.push(ev)
    if (ev === 'poll.broken') ref.current = 'sess-B'   // 断连瞬间用户切走
  }, undefined, { activeSidRef: ref })
  global.fetch = originalFetch
  assert(events.includes('poll.broken'), '切换前 poll.broken 应透传')
  assert(!events.includes('poll.recovered'),
    `切走后 fallback 轮询消息不得落新会话: ${JSON.stringify(events)}`)
})

// ──────────────────────────────────────────────────────────────────
// B) ChatView.jsx 源码静态断言
// ──────────────────────────────────────────────────────────────────

const src = readFileSync(new URL('../src/views/ChatView.jsx', import.meta.url), 'utf8')

test('B1 switchSession 去 busy 短路, 保留 sid===sessionId 短路', () => {
  assert(!src.includes('if (busy || sid === sessionId)'),
    'switchSession 首行 busy 短路必须拆除 (#209 根因)')
  assert(/const switchSession = async \(sid\) => \{[\s\S]{0,600}?if \(sid === sessionId\) \{ setListOpen\(false\); return \}/.test(src),
    'switchSession 必须保留同会话短路')
})

test('B2 switchSession 切换前同步摘流接线', () => {
  const m = src.match(/const switchSession = async \(sid\) => \{[\s\S]{0,900}?setLoadingHist\(true\)/)
  assert(m, 'switchSession 结构变化, 守护失焦 — 请同步本测试')
  assert(m[0].includes('viewSidRef.current = sid'),
    'switchSession 必须同步把 viewSidRef 指向目标 sid (摘流零窗口)')
  assert(m[0].includes('detachStream()'), 'switchSession 必须调 detachStream()')
})

test('B3 switchSession 失败路径回指旧 sid', () => {
  const m = src.match(/const switchSession = async \(sid\) => \{[\s\S]*?\} catch \(e\) \{\s*\n\s*\/\/ #209[\s\S]*?viewSidRef\.current = sessionId/)
  assert(m, '切换失败必须 viewSidRef.current = sessionId 回指旧会话, 否则旧会话后续发送被自摘流')
})

test('B4 newChat 去 busy 短路 + 摘流接线', () => {
  const m = src.match(/const newChat = \(\) => \{[\s\S]{0,500}?setSessionId\(''\)/)
  assert(m, 'newChat 结构变化, 守护失焦 — 请同步本测试')
  assert(!m[0].includes('if (busy) return'), 'newChat 的 busy 短路必须拆除 (新建钮流式可用)')
  assert(m[0].includes("viewSidRef.current = ''"), 'newChat 必须把 viewSidRef 置 draft (空串)')
  assert(m[0].includes('detachStream()'), 'newChat 必须调 detachStream()')
})

test('B5 detachStream: 世代+1 / 只 abort 不 stop / 视图 state 复位', () => {
  const m = src.match(/const detachStream = \(\) => \{[\s\S]*?\n  \}/)
  assert(m, 'detachStream 必须存在')
  const body = m[0]
  assert(body.includes('streamGenRef.current += 1'), 'detachStream 必须世代 +1')
  assert(body.includes('abortRef.current?.abort()'), 'detachStream 必须 abort 本地流')
  assert(!body.includes('g.stop'), 'detachStream 不得调 g.stop (run 服务端继续, #63 保活)')
  assert(body.includes('setBusy(false)') && body.includes('runIdRef.current = null'),
    'detachStream 必须复位视图 busy/run 状态')
})

test('B6 三处 busy 闸门解除', () => {
  assert(!src.includes('disabled={busy || loadingHist}'),
    '会话列表钮 busy 闸必须摘 (loadingHist 闸可留)')
  assert(!src.includes('disabled={busy}'),
    '工作目录选择器/新建钮的 disabled={busy} 必须摘干净')
  assert(src.includes('disabled={loadingHist}'), '会话列表钮应保留 loadingHist 闸')
})

test('B7 世代守卫: runStream/send 双处快照 + finally 比对', () => {
  const snaps = src.match(/const gen = streamGenRef\.current/g) || []
  assert(snaps.length === 2, `runStream/send 应各快照世代一次, 实际 ${snaps.length}`)
  const guards = src.match(/if \(streamGenRef\.current === gen\)/g) || []
  assert(guards.length >= 4, `catch+finally 双处×双流应有 ≥4 处世代比对, 实际 ${guards.length}`)
})

test('B8 chatStream 双调用点都挂摘流 ref + 起流前锚定', () => {
  const calls = src.match(/g\.chatStream\(sid, content, handler, ac\.signal, \{ activeSidRef: viewSidRef \}\)/g) || []
  assert(calls.length === 2, `runStream/send 应各传 activeSidRef 一次, 实际 ${calls.length}`)
  const anchors = src.match(/viewSidRef\.current = sid\s*\/\/ #209/g) || []
  assert(anchors.length === 2, 'runStream/send 起流前必须把 viewSidRef 锚定到本流 sid')
})

test('B9 ensureSession 新 sid 同步进 viewSidRef', () => {
  const m = src.match(/const sid = await g\.createSession\(\)[\s\S]{0,200}?setSessionId\(sid\)/)
  assert(m && m[0].includes('viewSidRef.current = sid'),
    'ensureSession 必须同步 viewSidRef, 否则新会话首条消息被自摘流')
})

// ──────────────────────────────────────────────────────────────────
let PASS = 0, FAIL = 0
for (const t of tests) {
  try { await t.fn(); PASS++; console.log(`  PASS  ${t.name}`) }
  catch (e) { FAIL++; console.log(`  FAIL  ${t.name}\n        ${e.message}`) }
}
console.log(`\nissue#209 stream-detach: ${PASS} passed, ${FAIL} failed`)
if (FAIL) process.exit(1)
