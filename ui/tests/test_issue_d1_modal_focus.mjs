#!/usr/bin/env node
// 守护测试: D-1 回流修复 — Modal 关闭回焦在带 autoFocus 的弹框失效
//
// 依据: docs/TEST-REPORT-R-20261102.md §4 D-1 (P2, AC-R3-4 半失败)。
//   根因: React 的 autoFocus 在 commit 阶段触发, 早于任何 effect (含
//   useLayoutEffect) ⇒ 旧写法在 useEffect 里 prevFocusRef.current =
//   document.activeElement, 读到的是刚被 autoFocus 聚焦的弹框内 TEXTAREA
//   ⇒ 关闭 cleanup 把焦点 focus 回已卸载节点 ⇒ activeElement 掉 body。
//   修法: prevFocus 捕获提前到 render 阶段 (open 跃迁 false→true 的那次
//   render, 弹框 DOM 尚未提交, activeElement 仍是触发元素)。
//
// 三级护栏 (模式参照 test_q195_toast_autohide.mjs):
//   L0 源码形态级: 捕获在 render 阶段 (wasOpenRef 跃迁守卫在函数体内、
//      effect 外); effect 体内不得再出现 prevFocusRef.current =
//      document.activeElement; ChatView 的 autoFocus 保留 (不改 UX 语义)。
//   L1 运行时级 (jsdom + react-dom/client + esbuild 真挂载): 触发按钮聚焦
//      → 打开带 autoFocus TEXTAREA 的 Modal → 焦点进 TEXTAREA (autoFocus
//      UX 不回退) → 点关闭 → activeElement 必须回到触发按钮。
//   L2 变异反证级: 把源码改回旧形态 (effect 内捕获) 跑同一场景 ⇒ 回焦必须
//      失败 ⇒ 证明 L1 不是恒绿。
//
// 接入: ui/package.json "test" 脚本末尾 (npm test 即跑)。
// 依赖: L1/L2 需要 jsdom + esbuild (均为 devDependency)。缺件显式报失败。

import { readFileSync, mkdtempSync, rmSync, writeFileSync } from 'fs'
import { join } from 'path'

const UI_DIR = new URL('../', import.meta.url).pathname
const read = (p) => readFileSync(join(UI_DIR, p), 'utf8')

const uiSrc = read('src/components/ui.jsx')
const chatSrc = read('src/views/ChatView.jsx')

let failed = 0
const tests = []
function test(name, fn) { tests.push({ name, fn }) }
function assert(cond, msg) { if (!cond) throw new Error(msg) }

// ── Modal 函数体抽取 (从 export function Modal 到下一个顶层 export) ──
function modalBody(src) {
  const start = src.indexOf('export function Modal')
  assert(start >= 0, 'ui.jsx 未找到 Modal')
  const rest = src.slice(start)
  const next = rest.indexOf('\nexport ')
  return next > 0 ? rest.slice(0, next) : rest
}
const body = modalBody(uiSrc)

// ═══════════════════════════ L0: 源码形态级 ═══════════════════════════

test('L0: prevFocus 捕获在 render 阶段 (open 跃迁守卫在函数体内)', () => {
  assert(/const\s+wasOpenRef\s*=\s*useRef\(false\)/.test(body),
    'Modal 未引入 wasOpenRef 跃迁守卫')
  // 跃迁守卫必须出现在第一个 useEffect( 之前 (render 阶段代码)
  const firstEffect = body.indexOf('useEffect(() => {\n    if (!open) return')
  assert(firstEffect > 0, 'Modal 主 effect 形态异常 (未找到 if (!open) return 头)')
  const renderPart = body.slice(0, firstEffect)
  assert(/if\s*\(open\s*!==\s*wasOpenRef\.current\)/.test(renderPart),
    'open 跃迁守卫不在 render 阶段 (必须在主 effect 之前)')
  assert(/prevFocusRef\.current\s*=\s*document\.activeElement/.test(renderPart),
    'render 阶段未捕获 document.activeElement 到 prevFocusRef')
})

test('L0: effect 体内不得再捕获 activeElement (防回退到 D-1 旧形态)', () => {
  const effectPart = body.slice(body.indexOf('useEffect(() => {\n    if (!open) return'))
  assert(!/prevFocusRef\.current\s*=\s*document\.activeElement/.test(effectPart),
    'effect 体内出现 prevFocusRef.current = document.activeElement ⇒ D-1 旧形态回潮 (autoFocus 先于 effect, 会记成弹框内元素)')
})

test('L0: 关闭回焦 cleanup 保留 (isConnected 守卫 + 置空)', () => {
  assert(/if\s*\(prev\s*&&\s*prev\.isConnected\)\s*prev\.focus\?\.\(\)/.test(body),
    '关闭回焦 cleanup 缺失或形态改变')
  assert(/prevFocusRef\.current\s*=\s*null/.test(body),
    'cleanup 未置空 prevFocusRef')
})

test('L0: ChatView TeamWork 弹框 TEXTAREA 的 autoFocus 保留 (UX 不回退)', () => {
  assert(/autoFocus/.test(chatSrc),
    'ChatView autoFocus 被删除 ⇒ 修复方式漂移 (裁决: 提前捕获时机, 不撤 autoFocus)')
})

// ═══════════ L1/L2: 运行时级 (jsdom + esbuild 真挂载 + 变异反证) ═══════════
// 工程要点与 test_q195_toast_autohide.mjs 同源: bundle 落 ui/ 下 (node_modules
// 解析); react 系 external (防双 React 实例); ui.jsx 模块图需拷 icons.jsx。
// Modal 的 t() 在无 i18n init 时回退为键名, 不影响焦点断言。

async function runFocusScenario({ source }) {
  const { JSDOM } = await import('jsdom')
  const { build } = await import('esbuild')

  const dir = mkdtempSync(join(UI_DIR, '.d1-tmp-'))
  const outfile = join(dir, 'bundle.mjs')
  const entry = `
import React, { useState } from 'react'
import { Modal } from './ui-under-test.jsx'
export function Harness() {
  const [open, setOpen] = useState(false)
  return React.createElement('div', null,
    React.createElement('button', { id: 'trigger', onClick: () => setOpen(true) }, 'open'),
    React.createElement(Modal, {
      open, onClose: () => setOpen(false), title: 'T',
    }, React.createElement('textarea', { autoFocus: true, id: 'ta' })))
}
`
  try {
    writeFileSync(join(dir, 'ui-under-test.jsx'), source)
    writeFileSync(join(dir, 'icons.jsx'), readFileSync(join(UI_DIR, 'src/components/icons.jsx'), 'utf8'))
    await build({
      stdin: { contents: entry, resolveDir: dir, loader: 'jsx' },
      bundle: true, format: 'esm', platform: 'browser', target: 'es2020',
      external: ['react', 'react-dom', 'react-dom/client'],
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
    // jsdom pretendToBeVisual 提供 rAF, 但只挂在 window 上; Modal 的收焦 rAF
    // 走全局, 必须显式桥接 (否则 effect 抛 ReferenceError, 弹框整体崩掉)。
    globalThis.requestAnimationFrame = (cb) => window.requestAnimationFrame(cb)
    globalThis.cancelAnimationFrame = (id) => window.cancelAnimationFrame(id)
    globalThis.IS_REACT_ACT_ENVIRONMENT = true

    const React = (await import('react')).default
    const { createRoot } = await import('react-dom/client')
    const { act } = React
    const { Harness } = await import('file://' + outfile)

    const root = createRoot(window.document.getElementById('root'))
    await act(async () => { root.render(React.createElement(Harness)) })

    const doc = window.document
    const trigger = doc.getElementById('trigger')
    // 模拟真实点击: 按钮先获得焦点, 再触发打开
    await act(async () => { trigger.focus() })
    const focusedBefore = doc.activeElement === trigger
    await act(async () => { trigger.click() })
    const ta = doc.getElementById('ta')
    const focusInModal = doc.activeElement === ta
    // 点弹框右上角关闭钮 (aria-label 在无 i18n 时回退为键名 common.close)
    const closeBtn = doc.querySelector('[role="dialog"] button[aria-label]')
    await act(async () => { closeBtn.click() })
    await act(async () => { await new Promise((r) => setTimeout(r, 50)) })
    const focusBackToTrigger = doc.activeElement === trigger
    const activeTag = doc.activeElement ? doc.activeElement.tagName : 'NULL'

    await act(async () => { root.unmount() })
    dom.window.close()
    return { focusedBefore, focusInModal, focusBackToTrigger, activeTag }
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

test('L1: 带 autoFocus 的 Modal 关闭后焦点回到触发按钮 (AC-R3-4)', async () => {
  const r = await runFocusScenario({ source: uiSrc })
  assert(r.focusedBefore, '场景失效: 触发按钮未获得焦点')
  assert(r.focusInModal, '场景失效: autoFocus 未把焦点带进 TEXTAREA (autoFocus 语义回退)')
  assert(r.focusBackToTrigger,
    `关闭后焦点未回到触发按钮 (activeElement=${r.activeTag}) ⇒ D-1 未修好`)
})

test('L2 变异反证: 旧形态 (effect 内捕获) 同场景必须回焦失败 (L1 非恒绿)', async () => {
  // 把源码改回 D-1 缺陷形态: 删 render 阶段跃迁守卫, 在 effect 内重新捕获。
  let mutated = uiSrc.replace(
    /  if \(open !== wasOpenRef\.current\) \{\n    if \(open\) prevFocusRef\.current = document\.activeElement\n    wasOpenRef\.current = open\n  \}\n/, '')
  mutated = mutated.replace(
    "    document.addEventListener('keydown', onKey)\n",
    "    document.addEventListener('keydown', onKey)\n    prevFocusRef.current = document.activeElement\n")
  assert(mutated !== uiSrc, '变异未生效 (源码形态与预期不符)')
  assert(!/wasOpenRef\.current = open/.test(mutated), '变异后仍残留 render 阶段守卫')
  const r = await runFocusScenario({ source: mutated })
  assert(r.focusInModal, '变异体场景失效: autoFocus 未聚焦 TEXTAREA')
  assert(!r.focusBackToTrigger,
    '旧形态下回焦仍成功 ⇒ L1 断言恒绿, 守护无效 (变异存活)')
})

// ═══════════════════════════ 运行器 ═══════════════════════════
;(async () => {
  for (const { name, fn } of tests) {
    try { await fn(); console.log(`ok - ${name}`) }
    catch (e) { failed++; console.error(`FAIL - ${name}\n  ${e.message}`) }
  }
  console.log(failed ? `\n${failed} FAILED / ${tests.length} tests` : `\nALL PASS (${tests.length} tests)`)
  process.exit(failed ? 1 : 0)
})()
