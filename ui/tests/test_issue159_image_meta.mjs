#!/usr/bin/env node
// 单测: issue #159 前端改造 — 图片压缩元信息 part + 压缩失败明确报错。
//
// 覆盖 (docs/PLAN-L-20261029.md §7.3 X-4/X-5 义务, MV-159-H 的牙齿):
//   A. imageMetaText 纯函数 (vm 直测): 压缩形态含前后分辨率/体积/格式/
//      节省比例; 直通形态含"未重压缩"文案 (AC-159-1 直通路径同样满足)。
//   B. send() parts 组装语义: 图片 part 后必须紧跟元信息 text part
//      (MV-159-H 删掉该行 ⇒ 本用例转红)。
//   C. uploadFiles 图片分支: 必须 POST /api/attachments/image; 失败必须
//      throw (走 setError), 不得静默 setPendingFiles (AC-159-3)。
//   D. i18n: 6 新键双语双边在位 + 模板占位符与 imageMetaText 传参一致。
//
// 形态: 源码抽取 + node:vm (与 test_issue58_tokens.mjs / test_issue75 同
// 口径), 无需 jsdom; 纯 node 跑, npm test 即跑。

import { readFileSync } from 'fs'
import vm from 'node:vm'

const src = readFileSync(new URL('../src/views/ChatView.jsx', import.meta.url), 'utf8')
const en = JSON.parse(readFileSync(new URL('../src/i18n/locales/en.json', import.meta.url), 'utf8'))
const zh = JSON.parse(readFileSync(new URL('../src/i18n/locales/zh-cn.json', import.meta.url), 'utf8'))

const tests = []
function test(name, fn) { tests.push({ name, fn }) }
function assert(cond, msg) { if (!cond) throw new Error(msg) }
function eq(a, b, msg) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b)
  if (sa !== sb) throw new Error(`${msg || 'eq'}: ${sa} !== ${sb}`)
}

// ── 源码抽取: imageMetaText 函数体 (花括号配平, 同既有口径) ──────────
function extractFn(source, marker) {
  const start = source.indexOf(marker)
  if (start < 0) throw new Error(`源码抽取失败, 未找到: ${marker}`)
  let braces = 0, seen = false
  for (let i = start; i < source.length; i++) {
    const ch = source[i]
    if (ch === '{') { braces++; seen = true }
    else if (ch === '}') braces--
    if (seen && braces === 0) return source.slice(start, i + 1)
  }
  throw new Error(`括号不配平: ${marker}`)
}

const ctx = vm.createContext({ console })
vm.runInContext(
  extractFn(src, 'function imageMetaText') +
    '\nglobalThis.__f = imageMetaText',
  ctx,
)
const imageMetaText = ctx.__f

// i18n stub: 按 en 表解析模板并做 {param} 插值 (对齐 i18next 单花括号约定)
const mkT = (table) => (key, opts) => {
  const v = table[key]
  if (typeof v !== 'string') return key
  if (!opts) return v
  return v.replace(/\{(\w+)\}/g, (_, k) => (opts[k] ?? ''))
}

const compressedFile = {
  name: 'photo.jpg', isImage: true, bytes: 962560, originalFormat: 'PNG',
  compressed: true,
  meta: {
    originalResolution: '4000x3000', compressedResolution: '2048x1536',
    originalBytes: 8123456, compressedBytes: 962560, savedRatio: 0.882,
  },
}
const passthroughFile = {
  name: 'logo.png', isImage: true, bytes: 245760, compressed: false,
  meta: {
    originalResolution: '2048x1536', compressedResolution: '2048x1536',
    originalBytes: 245760, compressedBytes: 245760, savedRatio: 0,
  },
}

// ══ A. imageMetaText 形态 ═════════════════════════════════════════
test('A1 压缩形态: 含前后分辨率/体积/格式/节省比例 (AC-159-1)', () => {
  const s = imageMetaText(compressedFile, mkT(en))
  assert(s.startsWith('[Image attachment: photo.jpg'), `前缀: ${s}`)
  for (const frag of ['4000x3000', '8123456', '2048x1536', '962560',
                      'PNG', '88.2']) {
    assert(s.includes(frag), `缺片段 ${frag}: ${s}`)
  }
  assert(s.endsWith(']'), `方括号闭合: ${s}`)
})

test('A2 直通形态: 未重压缩文案 + 分辨率/体积 (不省略)', () => {
  const s = imageMetaText(passthroughFile, mkT(en))
  assert(s.includes('logo.png') && s.includes('2048x1536')
    && s.includes('245760') && s.includes('not recompressed'), s)
  assert(!s.includes('saved'), `直通不写节省比例: ${s}`)
})

test('A3 中文模板双边解析 (zh-cn)', () => {
  const s = imageMetaText(compressedFile, mkT(zh))
  assert(s.startsWith('[图片附件: photo.jpg') && s.includes('压缩节省 88.2%'), s)
})

// issue #169 第3条 (P3-1 / REQ-169-AC4): 直通路径也显示真实 format。
test('A4 直通形态含真实 format (顶层 originalFormat 优先, 双语)', () => {
  const f = { ...passthroughFile, originalFormat: 'PNG' }
  const se = imageMetaText(f, mkT(en))
  assert(se.includes('original format PNG'), `en 直通缺真实 format: ${se}`)
  const sz = imageMetaText(f, mkT(zh))
  assert(sz.includes('原始格式 PNG'), `zh 直通缺真实 format: ${sz}`)
})

test('A5 format 双保险: 顶层缺失时 meta 兜底 (MV-169-3-A 牙齿)', () => {
  // 顶层 originalFormat 被裁剪 ⇒ 消费点退化读 meta.originalFormat 而非 unknown
  const f = {
    name: 'x.png', isImage: true, bytes: 100, compressed: false,
    meta: { originalResolution: '2x2', originalBytes: 100, originalFormat: 'JPEG' },
  }
  const s = imageMetaText(f, mkT(en))
  assert(s.includes('original format JPEG') && !s.includes('unknown'),
    `meta 兜底失效: ${s}`)
  const fc = {
    name: 'y.jpg', isImage: true, bytes: 90, compressed: true,
    meta: { originalResolution: '2x2', compressedResolution: '2x2',
      originalBytes: 100, compressedBytes: 90, savedRatio: 0.1,
      originalFormat: 'WEBP' },
  }
  const sc = imageMetaText(fc, mkT(en))
  assert(sc.includes('original format WEBP') && !sc.includes('unknown'),
    `压缩路径 meta 兜底失效: ${sc}`)
})

// ══ B. send() parts 组装: 元信息 part 紧跟图片 part (MV-159-H 牙齿) ══
test('B1 send() 图片分支: image_url part 后紧跟 imageMetaText text part', () => {
  const i = src.indexOf("{ type: 'image_url', image_url: { url: f.dataUrl } }")
  assert(i > 0, '未找到 image_url part')
  const win = src.slice(i, i + 400)
  assert(win.includes("parts.push({ type: 'text', text: imageMetaText(f, t) })"),
    'image_url part 之后未紧跟元信息 text part (MV-159-H 变异点)')
})

test('B2 元信息不并入用户文本 (独立 part, 图文配对相邻)', () => {
  // 用户文本 part 仍是独立 push (text 在前), 图片元信息只在图片分支出现
  const sendBlock = src.slice(src.indexOf('const send = useCallback'),
    src.indexOf('const uploadFiles'))
  const metaCount = (sendBlock.match(/imageMetaText\(f, t\)/g) || []).length
  eq(metaCount, 1, 'imageMetaText 在 send() 里恰好调用一次 (图片分支内)')
})

// ══ C. uploadFiles 图片分支: 走压缩端点 + 失败抛错 (AC-159-3) ══════
test('C1 图片分支 POST /api/attachments/image (fetch 直呼, 不动 api.js)', () => {
  assert(src.includes("fetch('/api/attachments/image'"),
    '图片分支未走压缩端点')
})

test('C2 压缩失败必须 throw (setError 呈现), 不得静默丢图', () => {
  const i = src.indexOf("fetch('/api/attachments/image'")
  assert(i > 0, '未找到压缩端点调用')
  // issue #169 第1条: 三元映射链补 3 码 + 兜底改造后长度增加 ⇒ 窗口 1200→2000
  // (断言字面形态逐字不变, 仅适配被测源码合法增长)
  const win = src.slice(i, i + 2000)
  assert(win.includes('if (!res.ok || !d.ok || !d.dataUrl)'),
    '失败判定缺失')
  assert(win.includes('throw new Error(t(\'chat.errors.image_compress_failed\''
    + ', { reason }))'), '失败未抛错 (静默丢图回归)')
  // 四类错误码各有专属文案映射
  for (const code of ['pillow_unavailable', 'still_too_large',
                      'not_an_image', 'decode_failed']) {
    assert(win.includes(code), `错误码映射缺失: ${code}`)
  }
})

test('C3 非图片分支零改动 (/api/upload 落盘 + 文本占位保持)', () => {
  assert(src.includes("fetch('/api/upload'"), '/api/upload 调用被误动')
  assert(src.includes("t('chat.input.attachment_placeholder_msg'"),
    '非图片占位文案被误动')
})

// ══ D. i18n 键双边在位 + 占位符一致 ═══════════════════════════════
test('D1 6 新键双语双边在位', () => {
  const keys = ['chat.errors.image_compress_failed', 'chat.errors.image_too_large',
    'chat.errors.image_unsupported', 'chat.errors.pillow_unavailable',
    'chat.input.image_meta', 'chat.input.image_meta_compressed']
  for (const k of keys) {
    assert(typeof en[k] === 'string', `en 缺键 ${k}`)
    assert(typeof zh[k] === 'string', `zh 缺键 ${k}`)
  }
})

test('D2 模板占位符与 imageMetaText 传参一致 (双语)', () => {
  const ph = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort()
  // issue #169 第3条: image_meta 直通模板补 {originalFormat} 占位符
  // (直通路径同样显示真实 format, REQ-169-AC4 的必要条件)。
  eq(ph(en['chat.input.image_meta']),
    ['bytes', 'mb', 'name', 'originalFormat', 'resolution'],
    'en image_meta 占位符')
  eq(ph(zh['chat.input.image_meta']), ph(en['chat.input.image_meta']),
    'zh/en image_meta 占位符一致')
  eq(ph(en['chat.input.image_meta_compressed']),
    ['compressedBytes', 'compressedMb', 'compressedResolution', 'name',
      'originalBytes', 'originalFormat', 'originalMb', 'originalResolution',
      'savedPct'].sort(), 'en image_meta_compressed 占位符')
  eq(ph(zh['chat.input.image_meta_compressed']),
    ph(en['chat.input.image_meta_compressed']), 'zh/en compressed 占位符一致')
})

// ── runner ─────────────────────────────────────────────────────────
let failed = 0
for (const t of tests) {
  try { t.fn(); console.log(`PASS  ${t.name}`) }
  catch (e) { failed++; console.log(`FAIL  ${t.name}\n      ${e.message}`) }
}
console.log(`\n== issue159 前端单测: ${tests.length - failed} passed, ${failed} failed ==`)
if (failed) process.exit(1)
