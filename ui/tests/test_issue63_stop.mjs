#!/usr/bin/env node
// 单测: issue #63 ChatView 显式停止改走原生 POST /v1/runs/{run_id}/stop。
//
// 覆盖两件事 (PLAN-63 §5 步骤4 前端项):
//   A. gateway.js 客户端工厂 makeGw(base) 暴露 stop(runId), 请求形态 =
//      POST `${base}/v1/runs/<runId>/stop` + JSON 头 + body '{}' (对齐 steer
//      写法); runId 走 encodeURIComponent; 非 2xx 经 j() 抛错 (停止失败可感知)。
//   B. ChatView.jsx 的 stop 处理已改为「先 g.stop(rid) 再 abort」且失败提示
//      词条 chat.errors.stop_failed 双语同存 (en/zh-cn 键集合差集 = 空)。
// 路线: 同 test_issue58_tokens.mjs — gateway.js 真实 import + JSX 源码静态断言
// (ChatView 是 React 组件, 不引 jsdom; 交互真机验收归 s4 / 170 旁路实例)。

import { readFileSync } from 'fs'
import { makeGw } from '../src/gateway.js'

const tests = []
function test(name, fn) { tests.push({ name, fn }) }
function assert(cond, msg) { if (!cond) throw new Error(msg) }

// ── A) gateway.stop 请求形态 (mock fetch 记录) ──────────────────────
const originalFetch = global.fetch

function captureFetch(status = 200, body = { run_id: 'r1', status: 'stopping' }) {
  const calls = []
  global.fetch = async (url, opts) => {
    calls.push({ url, opts })
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => body,
    }
  }
  return calls
}

test('stop: POST /v1/runs/{runId}/stop, JSON 头, body {}', async () => {
  const calls = captureFetch()
  const g = makeGw('')
  const r = await g.stop('run_abc123')
  global.fetch = originalFetch
  assert(calls.length === 1, `应恰好 1 次 fetch, 实际 ${calls.length}`)
  const { url, opts } = calls[0]
  assert(url === '/v1/runs/run_abc123/stop', `URL 错: ${url}`)
  assert(opts.method === 'POST', `method 错: ${opts.method}`)
  assert(opts.headers['Content-Type'] === 'application/json', '缺 JSON 头')
  assert(opts.body === '{}', `body 应为 '{}', 实际 ${opts.body}`)
  assert(r.status === 'stopping', '响应应透传')
})

test('stop: worker 前缀 base 生效 + runId 编码', async () => {
  const calls = captureFetch()
  const g = makeGw('/api/w/coder')
  await g.stop('r/x 1')
  global.fetch = originalFetch
  assert(calls[0].url === '/api/w/coder/v1/runs/r%2Fx%201/stop',
    `worker 前缀/编码错: ${calls[0].url}`)
})

test('stop: 非 2xx 抛错 (停止失败可感知)', async () => {
  captureFetch(409, { error: 'run not active' })
  const g = makeGw('')
  let threw = null
  try { await g.stop('r_dead') } catch (e) { threw = e }
  global.fetch = originalFetch
  assert(threw && /409|run not active/.test(String(threw.message)),
    `非 2xx 必须抛错, 实际: ${threw}`)
})

// ── B) ChatView.jsx 源码静态断言 + i18n 双语键 ─────────────────────
const chatview = readFileSync(new URL('../src/views/ChatView.jsx', import.meta.url), 'utf8')
const en = JSON.parse(readFileSync(new URL('../src/i18n/locales/en.json', import.meta.url), 'utf8'))
const zh = JSON.parse(readFileSync(new URL('../src/i18n/locales/zh-cn.json', import.meta.url), 'utf8'))

test('ChatView stop: 先 g.stop(rid) 再 abort (显式停止真中断)', () => {
  // 定位 stop 处理函数体
  const m = chatview.match(/const stop = async \(\) => \{([\s\S]*?)\n  \}\n/)
  assert(m, '未找到 async stop 处理函数 (issue #63 改造点)')
  const body = m[1]
  const iStop = body.indexOf('g.stop(rid)')
  const iAbort = body.indexOf('abortRef.current?.abort()')
  assert(iStop >= 0, 'stop 内必须调 g.stop(rid) (POST /v1/runs/{id}/stop)')
  assert(iAbort >= 0, 'stop 内必须保留本地 abort')
  assert(iStop < iAbort, '顺序必须: 先 run stop 后 abort (否则排空保活抢先接管)')
})

test('ChatView: 切会话/卸载路径只 abort 不 stop (保活语义)', () => {
  // newChat 路径不得调 g.stop —— 关页面/切会话正是要保活的场景
  const nc = chatview.match(/const newChat = \(\) => \{([\s\S]*?)\n  \}\n/)
  assert(nc && !nc[1].includes('g.stop('), 'newChat 不得调 run stop (由 1B 保活接管)')
})

test('i18n: chat.errors.stop_failed 双语同存且键集合对称', () => {
  assert(typeof en.chat?.errors?.stop_failed === 'string', 'en 缺 chat.errors.stop_failed')
  assert(typeof zh.chat?.errors?.stop_failed === 'string', 'zh-cn 缺 chat.errors.stop_failed')
  assert(en.chat.errors.stop_failed.includes('{err}'), 'en 值应含 {err} 插值')
  assert(zh.chat.errors.stop_failed.includes('{err}'), 'zh-cn 值应含 {err} 插值')
  // 键集合对称 (同 test_issue58_tokens 的 AC-U5 口径, 只查 chat.errors 子树)
  const ek = Object.keys(en.chat.errors).sort()
  const zk = Object.keys(zh.chat.errors).sort()
  assert(JSON.stringify(ek) === JSON.stringify(zk),
    `chat.errors 键集合不对称: en-zh=${ek.filter(k => !zk.includes(k))} zh-en=${zk.filter(k => !ek.includes(k))}`)
})

// ── runner ────────────────────────────────────────────────────────
;(async () => {
  let pass = 0, fail = 0
  for (const { name, fn } of tests) {
    try {
      await fn()
      pass++
      console.log(`  ok  ${name}`)
    } catch (e) {
      fail++
      console.error(`FAIL  ${name}: ${e.message}`)
    } finally {
      global.fetch = originalFetch
    }
  }
  console.log(`\n${pass} passed, ${fail} failed`)
  process.exit(fail ? 1 : 0)
})()
