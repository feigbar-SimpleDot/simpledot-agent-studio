#!/usr/bin/env node
// 单测: issue #169 第6条 (裁决 M-3) — A2/B3 从"源码 grep"升级为运行时用例。
//
// 背景 (docs/REVIEW-L-20261029.md / PLAN-M §3.6):
//   test_issue158_fs_write.mjs 的 A2/B3 只做源码 grep (断言 setRefreshTick
//   出现在 doUpload/doDelete 体内) —— D-1 缺陷正是"源码 grep 恒绿、运行时
//   失效"的典型 (NaN 刷新键)。裁决 M-3: 运行时用例为**增量**, 既有源码
//   模式断言一字不改 (本文件不触碰 test_issue158_fs_write.mjs)。
//
// 本套件 (真实例化, 与 test_issue_l_d1_filedrawer_refresh.mjs 同基建):
//   R-A2 上传闭环: 挂载 FileDrawer → 驱动隐藏 file input → 断言
//        GET /api/fs/list 请求数**增加** + 树里出现新文件 (闭环实证)。
//   R-B3 删除闭环: 选中文件 → 点删除 → 确认框确认 → 断言请求数增加 +
//        树里该文件消失 + 选择被清空 (动作条删除入口随 selection=null 消失,
//        DOM 可观测 ⇒ 不降级为已知边界)。
//   S1 自检 (MV-169-6-B 牙齿): 本文件源码必须含请求计数断言 —— 若有人把
//      运行时用例退化成源码 grep, 本自检转红 (防假绿)。
//
// 脚手架坑位说明见 test_issue_l_d1_filedrawer_refresh.mjs 头注释 (同源, 不重复)。
// 依赖: jsdom + esbuild (ui devDependency); 缺件时显式报"缺件"而非静默跳过。

import { readFileSync, mkdtempSync, rmSync } from 'fs'
import { join } from 'path'

const UI_DIR = new URL('../', import.meta.url).pathname
const SELF_SRC = readFileSync(new URL(import.meta.url), 'utf8')

const tests = []
function test(name, fn) { tests.push({ name, fn }) }
function assert(cond, msg) { if (!cond) throw new Error(msg) }

async function renderDrawer() {
  const { JSDOM } = await import('jsdom')
  const { build } = await import('esbuild')

  const dir = mkdtempSync(join(UI_DIR, '.d1-tmp-'))
  const outfile = join(dir, 'bundle.mjs')
  const entry = `
import React from 'react'
export { FileDrawer } from './src/components/filebrowser/FileDrawer.jsx'
export { initI18n, i18next } from './src/i18n/index.js'
`
  try {
    await build({
      stdin: { contents: entry, resolveDir: UI_DIR, loader: 'jsx' },
      bundle: true, format: 'esm', platform: 'browser', target: 'es2020',
      external: ['react', 'react-dom', 'react-dom/client', 'react-i18next', 'i18next'],
      outfile, logLevel: 'warning',
    })

    const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
      url: 'http://localhost/', pretendToBeVisual: true,
    })
    const { window } = dom
    globalThis.window = window
    globalThis.document = window.document
    Object.defineProperty(globalThis, 'navigator', {
      value: window.navigator, configurable: true, writable: true,
    })
    globalThis.HTMLElement = window.HTMLElement
    globalThis.Element = window.Element
    globalThis.Node = window.Node
    globalThis.Event = window.Event
    globalThis.MouseEvent = window.MouseEvent
    globalThis.getComputedStyle = window.getComputedStyle
    globalThis.requestAnimationFrame = window.requestAnimationFrame.bind(window)
    globalThis.cancelAnimationFrame = window.cancelAnimationFrame.bind(window)
    globalThis.IS_REACT_ACT_ENVIRONMENT = true
    globalThis.localStorage = window.localStorage
    globalThis.FileReader = window.FileReader
    globalThis.File = window.File
    globalThis.Blob = window.Blob

    // 请求记录 + 服务端状态模拟 (上传追加 / 删除移除 / list 回当前状态)
    const calls = []
    let serverFiles = ['readme.md']
    const fetchImpl = async (url, opts) => {
      const u = String(url)
      calls.push({ url: u, method: (opts && opts.method) || 'GET' })
      if (u.includes('/api/fs/upload')) {
        const body = JSON.parse(opts.body)
        serverFiles = [...serverFiles, body.name]
        return { ok: true, status: 200, json: async () => ({ name: body.name }) }
      }
      if (u.includes('/api/fs/delete')) {
        const body = JSON.parse(opts.body)
        serverFiles = serverFiles.filter((n) => n !== body.path)
        return { ok: true, status: 200, json: async () => ({ path: body.path }) }
      }
      if (u.includes('/api/fs/list')) {
        return {
          ok: true, status: 200,
          json: async () => ({
            items: serverFiles.map((n) => ({ name: n, dir: false, size: 5 })),
            truncated: false,
          }),
        }
      }
      return { ok: true, status: 200, json: async () => ({}) }
    }
    window.fetch = fetchImpl
    globalThis.fetch = fetchImpl

    const React = (await import('react')).default
    const { createRoot } = await import('react-dom/client')
    const { act } = React
    const { I18nextProvider } = await import('react-i18next')
    const mod = await import('file://' + outfile)
    const { FileDrawer, initI18n, i18next } = mod

    await act(async () => { initI18n() })

    const root = createRoot(window.document.getElementById('root'))
    let renderErr = null
    await act(async () => {
      try {
        root.render(React.createElement(I18nextProvider, { i18next },
          React.createElement(FileDrawer, {
            open: true, project: 'demo', onClose: () => {},
          })))
      } catch (e) { renderErr = e }
    })
    await act(async () => { await new Promise((r) => setTimeout(r, 100)) })

    return {
      renderErr, calls, act, root, window,
      rootEl: window.document.getElementById('root'),
      settle: async (ms = 120) => {
        await act(async () => { await new Promise((r) => setTimeout(r, ms)) })
      },
      listCalls: () => calls.filter((c) => c.url.includes('/api/fs/list')),
      deleteCalls: () => calls.filter((c) => c.url.includes('/api/fs/delete')),
    }
  } finally {
    try { rmSync(dir, { recursive: true, force: true }) } catch { /* ignore */ }
  }
}

async function pickFiles(r, names) {
  const input = r.rootEl.querySelector('input[type=file]')
  assert(input, '未找到隐藏的 file input (上传入口缺失)')
  const files = names.map((n) => new r.window.File(['x'], n, { type: 'text/plain' }))
  Object.defineProperty(input, 'files', { value: files, configurable: true })
  Object.defineProperty(input, 'value', {
    value: 'C:\\fakepath\\' + names[0], writable: true, configurable: true,
  })
  await r.act(async () => {
    input.dispatchEvent(new r.window.Event('change', { bubbles: true }))
  })
  await r.settle()
}

// 树条目按 title 定位 (排除抽屉头 ⟳/✕; 不能用 innerHTML 全文 —— 轻提示
// 同样含文件名, 全文匹配会假信号, 见 L 批 d1 套件同款注释)。
function treeEntries(r) {
  return [...r.rootEl.querySelectorAll('button[title]')]
    .filter((b) => !['Refresh', 'Close'].includes(b.getAttribute('title')))
    .map((b) => b.getAttribute('title'))
}

// 动作条删除入口 (带 ✕ 前缀, 与确认框里的裸 "Delete" 区分)。
// selection=null 时 canDelete 为假 ⇒ 该按钮消失 ⇒ "清选择"的 DOM 可观测面。
function deleteButton(r) {
  // issue #202 R4: 动作条删除入口的 ✕ 文字符号已换 SVG close 图标,
  // 定位锚改文案 delete 并排除确认框内按钮 (closest 守卫)。
  return [...r.rootEl.querySelectorAll('button')]
    .find((b) => /delete/i.test(b.textContent || '') && !b.closest('[role=alertdialog]'))
}

// ══ R-A2: 上传闭环 (A2 的运行时升级) ═══════════════════════════════
test('R-A2 上传成功 ⇒ /api/fs/list 请求数增加 + 树出现新文件 (闭环实证)', async () => {
  const r = await renderDrawer()
  try {
    assert(r.renderErr === null, `挂载抛错: ${r.renderErr && r.renderErr.message}`)
    const before = r.listCalls().length
    assert(before >= 1, `挂载后未拉取文件树: ${JSON.stringify(r.calls)}`)
    await pickFiles(r, ['uploaded.txt'])
    const after = r.listCalls().length
    assert(after > before,
      `上传后文件树未重新拉取 (before=${before}, after=${after}) —— A2 运行时失效`)
    assert(treeEntries(r).includes('uploaded.txt'),
      `上传后文件树未显示新文件: ${JSON.stringify(treeEntries(r))}`)
  } finally {
    await r.act(async () => { r.root.unmount() })
  }
})

// ══ R-B3: 删除闭环 (B3 的运行时升级) ═══════════════════════════════
test('R-B3 删除确认 ⇒ 请求数增加 + 条目消失 + 选择清空 (动作条删除入口消失)', async () => {
  const r = await renderDrawer()
  try {
    // 选中 readme.md
    const fileBtn = [...r.rootEl.querySelectorAll('button')]
      .find((b) => /readme\.md/.test(b.textContent || ''))
    assert(fileBtn, '文件树里未找到 readme.md 按钮')
    await r.act(async () => {
      fileBtn.dispatchEvent(new r.window.MouseEvent('click', { bubbles: true }))
    })
    await r.settle()
    assert(deleteButton(r), '选中文件后动作条未出现删除入口 (选择态未生效)')

    // 点删除 → 确认框 → 确认
    await r.act(async () => {
      deleteButton(r).dispatchEvent(new r.window.MouseEvent('click', { bubbles: true }))
    })
    await r.settle()
    const dialog = r.rootEl.querySelector('[role=alertdialog]')
    assert(dialog, '未弹出删除二次确认框')
    const beforeList = r.listCalls().length
    const confirmBtn = [...dialog.querySelectorAll('button')]
      .find((b) => /delete/i.test(b.textContent || ''))
    assert(confirmBtn, '确认框内未找到确认删除按钮')
    await r.act(async () => {
      confirmBtn.dispatchEvent(new r.window.MouseEvent('click', { bubbles: true }))
    })
    await r.settle()

    assert(r.deleteCalls().length >= 1, '确认删除后未发出 /api/fs/delete 请求')
    const afterList = r.listCalls().length
    assert(afterList > beforeList,
      `删除后文件树未重新拉取 (before=${beforeList}, after=${afterList}) —— B3 运行时失效`)
    assert(!treeEntries(r).includes('readme.md'),
      `删除后文件树仍显示 readme.md: ${JSON.stringify(treeEntries(r))}`)
    // 清选择的 DOM 可观测面: selection=null ⇒ canDelete 假 ⇒ 动作条删除入口消失
    assert(!deleteButton(r),
      '删除后选择未清空 (动作条仍显示删除入口) —— B3 的 setSelection(null) 运行时失效')
  } finally {
    await r.act(async () => { r.root.unmount() })
  }
})

// ══ S1: 自检 (MV-169-6-B 牙齿) ═════════════════════════════════════
// 运行时用例若退化成源码 grep (复刻 A2 老形态) 就是假绿。自检钉住:
// 本文件必须真的做请求计数断言 (listCalls().length 比较), 而不是只匹配源码。
test('S1 自检: 本套件含请求计数断言 (运行时用例不得退化为源码 grep)', () => {
  assert(/listCalls\(\)\.length/.test(SELF_SRC),
    '本文件缺少 listCalls().length 请求计数断言 —— 运行时用例失去牙齿')
  assert(/after > before|afterList > beforeList/.test(SELF_SRC),
    '本文件缺少请求数递增断言')
})

// ── runner ─────────────────────────────────────────────────────────
let pass = 0, fail = 0
for (const { name, fn } of tests) {
  try { await fn(); pass++; console.log(`  ✓ ${name}`) }
  catch (e) { fail++; console.error(`  ✗ ${name}\n    ${e.message}`) }
}
console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail ? 1 : 0)
