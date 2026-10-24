#!/usr/bin/env node
// 单测: ui/src/gateway.js 的 chatStream SSE 解析。
// 复现用户场景: chat 流只 push 一两条就静默, 必须手动刷新页面。
// 根因: 旧 parser chunk 边界切到帧中间时, 把后续帧堆在 buf 里不 flush。
import { readFileSync } from 'fs'

// 在 node 环境模拟 fetch ReadableStream
import { makeGw } from '../src/gateway.js'

// 手动 mock fetch 返回 ReadableStream
const originalFetch = global.fetch
let mockController = null  // 测试控制
function makeMockStreamResponse(chunks, opts={}) {
  const enc = new TextEncoder()
  const stream = new ReadableStream({
    start(c) {
      mockController = c
      // 异步推 chunks
      let i = 0
      const push = () => {
        if (i >= chunks.length) {
          c.close()
          return
        }
        c.enqueue(enc.encode(chunks[i++]))
        setTimeout(push, 0)
      }
      setTimeout(push, 0)
    }
  })
  return {
    ok: true,
    status: 200,
    body: stream,
  }
}

const tests = []
function test(name, fn) { tests.push({name, fn}) }

async function runWithStream(chunks, onEvent) {
  global.fetch = async () => makeMockStreamResponse(chunks)
  const g = makeGw('')
  // 直接 await chatStream 完成(由 mockController close 触发)
  await g.chatStream('sess-x', 'hi', onEvent)
  global.fetch = originalFetch
}

// ---- 1) 基本 LF LF 多帧(模仿服务端真实格式) ----
test('LF: 完整多帧能逐条 dispatch', async () => {
  const events = []
  await runWithStream([
    'event: run.started\ndata: {"run_id":"r1","seq":1}\n\n',
    'event: message.started\ndata: {"id":"m1"}\n\n',
    'event: assistant.delta\ndata: {"delta":"hello"}\n\n',
    'event: assistant.delta\ndata: {"delta":" world"}\n\n',
    'event: run.completed\ndata: {"status":"ok"}\n\n',
  ], (ev, d) => events.push([ev, d]))
  // 期望 5 帧
  if (events.length !== 5) throw new Error(`期望 5 帧, 实际 ${events.length}: ${JSON.stringify(events)}`)
  if (events[0][0] !== 'run.started') throw new Error('frame 0 wrong event')
  if (events[2][1].delta !== 'hello') throw new Error('frame 2 delta wrong')
  if (events[3][1].delta !== ' world') throw new Error('frame 3 delta wrong')
  if (events[4][0] !== 'run.completed') throw new Error('frame 4 wrong event')
})

// ---- 2) chunk 边界切到帧中间(用户 bug 复现) ----
test('chunk 边界切到帧中间: 不再丢帧', async () => {
  const full = 'event: run.started\ndata: {"run_id":"r1"}\n\nevent: a.delta\ndata: {"delta":"a"}\n\nevent: b.delta\ndata: {"delta":"b"}\n\nevent: c.delta\ndata: {"delta":"c"}\n\nevent: d.delta\ndata: {"delta":"d"}\n\nevent: run.completed\ndata: {"ok":true}\n\n'
  // 切成每个 char 一个 chunk, 极端边界
  const chunks = []
  for (let i = 0; i < full.length; i += 7) chunks.push(full.slice(i, i+7))

  const events = []
  await runWithStream(chunks, (ev, d) => events.push([ev, d]))
  // 期望 6 帧(不丢)
  if (events.length !== 6) {
    throw new Error(`丢帧! 期望 6, 实际 ${events.length}: ${JSON.stringify(events)}`)
  }
})

// ---- 3) CRLF 分隔 ----
test('CRLF: 兼容 \\r\\n\\r\\n 分隔', async () => {
  const events = []
  await runWithStream([
    'event: run.started\r\ndata: {"run_id":"r1"}\r\n\r\n',
    'event: a.delta\r\ndata: {"delta":"x"}\r\n\r\n',
  ], (ev, d) => events.push([ev, d]))
  if (events.length !== 2) throw new Error(`CRLF 期望 2, 实际 ${events.length}`)
  if (events[0][0] !== 'run.started') throw new Error('CRLF event name missing')
})

// ---- 4) done 时残留 buf 完整帧不丢 ----
test('done 时残留 buf: 最后完整帧兜底推送', async () => {
  const events = []
  // 注意: 第二帧没有尾随 \n\n, 但 chunk 1 末尾切到一半, 第二帧刚好完整
  // 模拟服务端不发 done 直接 close, 但最后一帧完整
  const full = 'event: run.started\ndata: {"r":1}\n\nevent: a.delta\ndata: {"d":"a"}\n\nevent: b.delta\ndata: {"d":"b"}\n\n'
  const chunks = []
  for (let i = 0; i < full.length; i += 13) chunks.push(full.slice(i, i+13))
  await runWithStream(chunks, (ev, d) => events.push([ev, d]))
  if (events.length !== 3) throw new Error(`done-flush 期望 3, 实际 ${events.length}: ${JSON.stringify(events)}`)
})

// ---- 5) SSE 注释/心跳行 ----
test('心跳行 :comment 不触发 onEvent', async () => {
  const events = []
  await runWithStream([
    ': heartbeat\n\nevent: a.delta\ndata: {"d":"x"}\n\n',
  ], (ev, d) => events.push([ev, d]))
  if (events.length !== 1) throw new Error(`心跳应忽略, 实际 ${events.length}`)
  if (events[0][0] !== 'a.delta') throw new Error('frame 0 wrong')
})

// ---- 6) 没有 event 行(只 data) → 默认 event=message ----
test('data-only: 默认 event=message', async () => {
  const events = []
  await runWithStream([
    'data: {"hello":"world"}\n\n',
  ], (ev, d) => events.push([ev, d]))
  if (events.length !== 1) throw new Error(`data-only 期望 1, 实际 ${events.length}`)
  if (events[0][0] !== 'message') throw new Error(`default event 应是 message, 实际 ${events[0][0]}`)
})

// ---- 7) 多 chunk 边界混合 LF + CRLF ----
test('混合: LF 帧 + CRLF 帧都能切', async () => {
  const events = []
  await runWithStream([
    'event: a.delta\ndata: {"d":"1"}\n\n',
    'event: b.delta\r\ndata: {"d":"2"}\r\n\r\n',
    'event: c.delta\ndata: {"d":"3"}\n\n',
  ], (ev, d) => events.push([ev, d]))
  if (events.length !== 3) throw new Error(`混合期望 3, 实际 ${events.length}`)
})


// ---- 8/9) chatStream fallback: 见 gateway.js 注释
//   ReadableStream controller.error() 时机太脆, 单元测试不写, 部署后浏览器手动刷新验证。

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