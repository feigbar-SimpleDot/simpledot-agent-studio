// issue #86 — net() 转包时保留原始 error 引用 (Error cause, PLAN-87-86 §2)。
// 抽取 ApiError 类 + net(); vm 不认 export 关键字, 需先剥掉; i18next 需 stub。
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import vm from 'node:vm'

const here = path.dirname(fileURLToPath(import.meta.url))
const src = readFileSync(path.join(here, '../src/api.js'), 'utf8')
const start = src.indexOf('export class ApiError')
const end = src.indexOf('export const fmtApiError')
if (start < 0 || end < 0) throw new Error('源码抽取失败: 未定位 ApiError / fmtApiError')
const block = src.slice(start, end).replace(/^export /gm, '')
const ctx = vm.createContext({ console, i18next: { t: (k, o) => `${k}:${JSON.stringify(o)}` } })
vm.runInContext(block + '\nglobalThis.__e={ApiError,net};', ctx)
const { ApiError, net } = ctx.__e

let failed = 0
function assert(c, m) { if (!c) throw new Error(m) }
function eq(a, b, m) { const sa = JSON.stringify(a), sb = JSON.stringify(b); if (sa !== sb) throw new Error(`${m}: ${sa} !== ${sb}`) }
const check = (n, f) => { try { f(); console.log(`ok - ${n}`) } catch (e) { failed++; console.error(`FAIL - ${n}\n  ${e.message}`) } }

// N-1 fetch reject(TypeError) → ApiError kind=network 且原始 error 引用可达
const orig = new TypeError('Failed to fetch')
net(Promise.reject(orig), '/api/workers').then(
  () => { failed++; console.error('FAIL - N-1: 期望 reject') },
  (err) => check('N-1 断网转包保留原始 error 引用, kind/message/endpoint 逐字不变', () => {
    assert(err instanceof ApiError, '应为 ApiError')
    eq(err.kind, 'network', 'kind 应为 network')
    eq(err.endpoint, '/api/workers', 'endpoint 应透传')
    eq(err.message, 'Failed to fetch', 'message 应逐字不变')
    assert(err.cause === orig, `cause 应 === 原始 error, 实际 ${err.cause}`)
    assert(err.cause instanceof TypeError, 'cause 应保留原始类型 TypeError')
    assert(err.cause.stack && err.cause.stack.includes('TypeError'), 'cause 应带原始 stack')
  })).then(() =>
  // N-2 ApiError 原样上抛, 不被二次转包 (net() 的早返回分支未受影响)
  net(Promise.reject(new ApiError('HTTP 500', { kind: 'server', status: 500 })), '/api/tasks').then(
    () => { failed++; console.error('FAIL - N-2: 期望 reject') },
    (err) => check('N-2 已是 ApiError 的错误原样上抛, kind/message 不被改写', () => {
      assert(err instanceof ApiError, '应仍是 ApiError')
      eq(err.kind, 'server', 'kind 应保持 server')
      eq(err.message, 'HTTP 500', 'message 不应被改写')
    })).then(() =>
  // N-3 非 Error 的 reject 值 → 逐字兜底不变, 且 cause 仍保留原值
  net(Promise.reject('boom'), '/api/workers').then(
    () => {},
    (err) => check('N-3 非 Error reject 值: String(e?.message||e) 兜底不变且 cause 保留', () => {
      eq(err.kind, 'network', 'kind 应为 network')
      eq(err.message, 'boom', '兜底应逐字不变')
      assert(err.cause === 'boom', 'cause 应保留原始 reject 值')
    })).then(() =>
  // N-4 三态归因不变 —— kind 决定文案键, 不因 cause 存在而改变
  check('N-4 三态归因不变: network 归因 + 端点标签不受 cause 影响', () => {
    const e2 = new ApiError('Failed to fetch', { kind: 'network', endpoint: '/api/x', cause: orig })
    eq(e2.kind, 'network', 'network 归因不变')
    eq(e2.endpoint, '/api/x', '端点标签不变')
  })).then(() => {
    console.log(failed ? `\n${failed} 失败` : '\n全部通过 (N-1..N-4)')
    if (failed) process.exit(1)
  })))
