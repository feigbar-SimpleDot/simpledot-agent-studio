#!/usr/bin/env node
// 单测: issue #169 第1条 (F-L1) — 图片压缩错误三码不再裸英文进用户文案。
//
// 缺陷 (docs/REVIEW-L-20261029.md F-L1, PLAN-M §3.1):
//   后端 mw/console.py 的 _attach_img_error 会返回 missing_data / bad_base64 /
//   payload_too_large 三个错误码, 而 ChatView.jsx 的三元映射链只覆盖 5 个码,
//   兜底分支 `(d.error || …)` 把三码**裸码**拼进 t('chat.errors.image_compress_failed',
//   { reason }) ⇒ 中文界面出现裸英文码。
//
// 本套件形态 (与 test_issue159_image_meta.mjs 的 extractFn 同口径):
//   源码抽取 + node:vm 运行时求值 —— uploadFiles 是组件内闭包函数不能整体抽,
//   抽「错误映射表达式段」(const reason = … 到 throw new Error( 之间), 注入
//   d / res / t(stub) 求值 ⇒ 断言渲染出的 reason 不含裸码字面量。
//   这是 MV-169-1-A (只改注释) / MV-169-1-B (映射写进 mw/i18n 前端不读) /
//   MV-169-1-C (兜底仍回显 d.error) 三个变体的牙齿。
//
// 跑法: node ui/tests/test_issue_m169_error_codes.mjs (npm test 链内)

import { readFileSync } from 'fs'
import vm from 'node:vm'

const src = readFileSync(new URL('../src/views/ChatView.jsx', import.meta.url), 'utf8')
const en = JSON.parse(readFileSync(new URL('../src/i18n/locales/en.json', import.meta.url), 'utf8'))
const zh = JSON.parse(readFileSync(new URL('../src/i18n/locales/zh-cn.json', import.meta.url), 'utf8'))

const tests = []
function test(name, fn) { tests.push({ name, fn }) }
function assert(cond, msg) { if (!cond) throw new Error(msg) }

// ── 抽取「错误映射表达式段」: const reason = <expr> 直到 throw new Error( ──
function extractReasonExpr(source) {
  const anchor = "const reason = d.error === 'pillow_unavailable'"
  const start = source.indexOf(anchor)
  assert(start >= 0, '源码抽取失败: 未找到错误映射链锚点 (守护被改写/删除?)')
  const end = source.indexOf('throw new Error(', start)
  assert(end > start, '源码抽取失败: 映射链之后未找到 throw new Error(')
  const seg = source.slice(start + 'const reason ='.length, end)
  // 段尾是 "…)\n          " (三元链闭合) —— 去掉尾部空白后必须是可求值表达式
  return seg.trim().replace(/;$/, '')
}

const reasonExpr = extractReasonExpr(src)

// i18n stub: 按表解析模板并做 {param} 插值 (对齐 i18next 单花括号约定)
const mkT = (table) => (key, opts) => {
  const v = table[key]
  if (typeof v !== 'string') return key
  if (!opts) return v
  return v.replace(/\{(\w+)\}/g, (_, k) => (opts[k] ?? ''))
}

// vm 求值: 注入 d (后端响应形态: error=裸码, detail=固定英文短语)、res、t
function evalReason(d, res, t) {
  const ctx = vm.createContext({ d, res, t })
  vm.runInContext(`globalThis.__out = (${reasonExpr})`, ctx)
  return ctx.__out
}

// ══ R1. 三码运行时渲染: reason 不含裸码字面量 (REQ-169-AC1 主判据) ══
const BARE = {
  missing_data: { zh: '缺少图片附件数据', en: 'Missing image attachment data' },
  bad_base64: { zh: '图片附件数据不是合法 base64', en: 'Image attachment data is not valid base64' },
  payload_too_large: { zh: '图片附件请求体超过上限', en: 'Image attachment request body exceeds the limit' },
}
for (const [code, texts] of Object.entries(BARE)) {
  test(`R1 三码渲染无裸码: ${code} (zh/en 双语)`, () => {
    for (const [lang, table] of [['zh', zh], ['en', en]]) {
      const reason = evalReason(
        { error: code, detail: 'fixed english phrase' },
        { ok: false, status: 400 }, mkT(table))
      assert(!reason.includes(code),
        `${lang} 渲染仍含裸码 ${code}: ${reason}`)
      assert(reason === texts[lang],
        `${lang} ${code} 文案不符: ${reason}`)
      // 外层模板拼出来的完整用户文案也不得含裸码
      const full = mkT(table)('chat.errors.image_compress_failed', { reason })
      assert(!full.includes(code), `${lang} 完整文案含裸码: ${full}`)
    }
  })
}

// ══ R2. 兜底不再回显 d.error (MV-169-1-C 牙齿) ══
test('R2 兜底不回显裸码: 未知错误码走通用文案', () => {
  for (const [lang, table] of [['zh', zh], ['en', en]]) {
    const reason = evalReason(
      { error: 'some_future_error_code', detail: 'x' },
      { ok: false, status: 500 }, mkT(table))
    assert(!reason.includes('some_future_error_code'),
      `${lang} 兜底仍回显裸码: ${reason}`)
    assert(reason === table['chat.errors.image_compress_failed_generic'],
      `${lang} 兜底应为通用文案: ${reason}`)
  }
})
test('R2b res.ok 但响应缺 dataUrl ⇒ 兜底给带 status 的上传失败文案', () => {
  const reason = evalReason({}, { ok: true, status: 200 }, mkT(en))
  assert(reason === en['chat.errors.upload_failed'].replace('{status}', '200'),
    `res.ok 兜底: ${reason}`)
})

// ══ R3. 既有映射零改动 (回归: 已映射 5 码行为不变) ══
test('R3 既有 5 码映射零改动', () => {
  const cases = [
    ['pillow_unavailable', 'chat.errors.pillow_unavailable'],
    ['still_too_large', 'chat.errors.image_too_large'],
    ['image_too_large', 'chat.errors.image_too_large'],
    ['not_an_image', 'chat.errors.image_unsupported'],
    ['decode_failed', 'chat.errors.image_unsupported'],
  ]
  for (const [code, key] of cases) {
    for (const table of [zh, en]) {
      const reason = evalReason({ error: code }, { ok: false, status: 400 }, mkT(table))
      assert(reason === table[key], `${code} ⇒ ${key} 回归: ${reason}`)
    }
  }
})

// ══ R4. 键在位 + 源码形态 (MV-169-1-A/B 的静态面) ══
test('R4 4 新键双语双边在位', () => {
  for (const k of ['chat.errors.image_missing_data', 'chat.errors.image_bad_base64',
                   'chat.errors.image_payload_too_large',
                   'chat.errors.image_compress_failed_generic']) {
    assert(typeof en[k] === 'string' && en[k].length > 0, `en 缺键 ${k}`)
    assert(typeof zh[k] === 'string' && zh[k].length > 0, `zh 缺键 ${k}`)
  }
})
test('R5 源码含三码显式映射且兜底无裸回显形态 (MV-169-1-C)', () => {
  const i = src.indexOf("fetch('/api/attachments/image'")
  assert(i > 0, '未找到压缩端点调用')
  const win = src.slice(i, i + 1600)
  for (const code of ['missing_data', 'bad_base64', 'payload_too_large']) {
    assert(win.includes(`d.error === '${code}'`), `缺显式映射: ${code}`)
  }
  // 兜底不得再直接吐 d.error (MV-169-1-C: 只补映射不动兜底 ⇒ 转红)
  assert(!win.includes('d.error ||'), '兜底仍含 `d.error ||` 裸回显形态')
})

// ── runner ─────────────────────────────────────────────────────────
let pass = 0, fail = 0
for (const { name, fn } of tests) {
  try { fn(); pass++; console.log(`PASS  ${name}`) }
  catch (e) { fail++; console.log(`FAIL  ${name}\n      ${e.message}`) }
}
console.log(`\n== issue#169 错误码映射单测: ${pass} passed, ${fail} failed ==`)
process.exit(fail ? 1 : 0)
