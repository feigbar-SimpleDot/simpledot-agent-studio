#!/usr/bin/env node
// 守护测试: issue #195 — v1 Toast 永不自动消失 (Q 批 s5 / 卡 t_3cbe65b3)
//
// 依据: docs/PLAN-Q-20261102.md §2.2 (根因核实 + 修法裁决, 参照 commit
//       4bcb7c0 对 V2Toast 的 useRef 修法); 原始缺陷: ui.jsx Toast 的定时器
//       effect deps=[msg, onDone], 而全部调用点都传内联箭头 onDone ⇒ 父组件
//       每次重渲染产生新 onDone 身份 ⇒ effect 清理重启 3.2s 定时器 ⇒ 收件箱
//       2s 轮询 (< 3.2s) 令 toast 永不消失。
//
// 三级护栏 (模式参照 test_uxv2_s6a_p1_fixes.mjs 的 F-1 组):
//   L0 源码形态级: onDone 收进 useRef; 定时器 effect 依赖逐字 [msg];
//      Toast 内任何 effect 依赖数组都不得含 onDone; 调用方仍是内联箭头
//      (不许有人转去调用方包 useCallback 治标)。
//   L1 运行时级 (jsdom + react-dom/client + esbuild 真挂载): 父组件 60ms
//      高频重渲染 (比真实 2s 轮询更激进) ⇒ toast 仍在约 3.2s 后自动消失
//      (onDone 恰好触发 1 次, DOM 节点移除)。
//   L2 变异反证级: 把源码改回旧形态 (deps=[msg,onDone] + setTimeout(onDone))
//      跑同一场景 ⇒ 必须**不**消失 ⇒ 证明 L1 不是恒绿。
//
// 接入: ui/package.json "test" 脚本末尾 (npm test 即跑)。
//
// 依赖: L1/L2 需要 jsdom + esbuild (均为 devDependency)。缺件时显式报失败,
//       不静默跳过 (缺件 = 护栏没跑 = 必须被看见, 不能假绿)。

import { readFileSync, mkdtempSync, rmSync, writeFileSync } from 'fs'
import { join } from 'path'

const UI_DIR = new URL('../', import.meta.url).pathname
const read = (p) => readFileSync(join(UI_DIR, p), 'utf8')

const uiSrc = read('src/components/ui.jsx')
const TOAST_MS = 3200   // Toast 自动消失时长

let failed = 0
const tests = []
function test(name, fn) { tests.push({ name, fn }) }
function assert(cond, msg) { if (!cond) throw new Error(msg) }

// ── Toast 函数体抽取 (从 export function Toast 到下一个顶层 export / EOF) ──
function toastBody(src) {
  const start = src.indexOf('export function Toast')
  assert(start >= 0, 'ui.jsx 未找到 Toast')
  const rest = src.slice(start)
  const next = rest.indexOf('\nexport ')
  return next > 0 ? rest.slice(0, next) : rest
}
const body = toastBody(uiSrc)

// ═══════════════════════════ L0: 源码形态级 ═══════════════════════════

test('L0: Toast 把 onDone 收进 useRef (根因修在组件内)', () => {
  assert(/const\s+onDoneRef\s*=\s*useRef\(onDone\)/.test(body),
    'Toast 未把 onDone 收进 useRef (修法不在根因处)')
  assert(/onDoneRef\.current\s*=\s*onDone/.test(body),
    'Toast 未同步最新 onDone 到 ref')
  assert(/onDoneRef\.current\?\.\(\)/.test(body),
    'Toast 定时器未走 onDoneRef (仍在直接引用 onDone 闭包)')
})

test('L0: Toast 定时器 effect 的依赖数组只含 msg', () => {
  const deps = body.match(/\},\s*\[([^\]]*)\]\)/g) || []
  assert(deps.length >= 1, 'Toast 内未找到 effect 依赖数组 (源码形态异常)')
  const timerDeps = deps.filter((d) => /msg/.test(d))
  assert(timerDeps.length === 1,
    `Toast 内含 msg 的 effect 依赖数组应恰好 1 个 (定时器 effect), 实际 ${timerDeps.length}: ${timerDeps}`)
  assert(/^\},\s*\[msg\]\)$/.test(timerDeps[0].trim()),
    `定时器 effect 依赖数组应逐字为 [msg], 实际 ${timerDeps[0].trim()}`)
})

test('L0: Toast 内没有任何 effect 依赖 onDone (防回退)', () => {
  const bad = body.match(/\},\s*\[[^\]]*onDone[^\]]*\]\)/g) || []
  assert(bad.length === 0,
    `Toast 内出现依赖 onDone 的 effect: ${bad.join(' | ')} ⇒ 父组件重渲染会重启定时器`)
})

test('L0: 6 处调用方仍是内联箭头 onDone (不许转去调用方 useCallback 治标)', () => {
  // PLAN §2.2 点名 6 视图; 逐文件核 onDone={<stateSetter>(...)} 内联形态仍在。
  const callSites = [
    ['src/views/ScheduleView.jsx', /onDone=\{\(\)\s*=>\s*setToast\(null\)\}/],
    ['src/views/CronView.jsx', /onDone=\{\(\)\s*=>/],
    ['src/views/TasksBoardView.jsx', /onDone=\{\(\)\s*=>/],
    ['src/views/SettingsView.jsx', /onDone=\{\(\)\s*=>/],
    ['src/views/WorkersView.jsx', /onDone=\{\(\)\s*=>/],
    ['src/views/WorkflowsView.jsx', /onDone=\{\(\)\s*=>/],
  ]
  for (const [f, re] of callSites) {
    assert(re.test(read(f)), `${f} 的 Toast onDone prop 不再是内联箭头 ⇒ 疑似被改成稳定引用 (治标)`)
  }
})

// ═══════════ L1/L2: 运行时级 (jsdom + esbuild 真挂载 + 变异反证) ═══════════
// 工程要点 (与 test_issue75_chat_asi.mjs L1 同源):
//   (1) bundle 必须落在 ui/ 目录下 (node_modules 解析);
//   (2) react 系必须 external, 否则双 React 实例 → dispatcher null 崩;
//   (3) Toast 不依赖 i18n (文案由调用方传入), 无需 i18n 脚手架。

// 场景 (与参照套件 test_uxv2_s6a_p1_fixes.mjs L1/L2 同构): 父组件每 60ms
// 重渲染一次 (真实调用方是 2s 轮询, 这里更激进 ⇒ 旧形态下定时器被重启得更
// 频繁 ⇒ 若此场景能消失, 2s 轮询场景必然也能消失)。
// ★ 关键坑 (本套件实测): 必须用 50ms 小步 act() 轮询等待, 不能一次 act()
//   睡满 3.6s —— act 作用域内定时器触发的 setState 会被批处理到作用域结束
//   才提交 ⇒ 变异体的 effect 在睡眠期间根本不重跑 ⇒ 假绿。小步 act 每步都
//   flush 提交, 变异体才会真的被压测定时器重置。
const RERENDER_MS = 60

async function runToastScenario({ source }) {
  const { JSDOM } = await import('jsdom')
  const { build } = await import('esbuild')

  const dir = mkdtempSync(join(UI_DIR, '.q195-tmp-'))
  const outfile = join(dir, 'bundle.mjs')
  // 入口: 一个父组件, 60ms 高频重渲染, onDone 传内联箭头 (调用方实况形态)
  const entry = `
import React, { useState } from 'react'
import { Toast } from './toast-under-test.jsx'
export function Harness() {
  const [msg, setMsg] = useState('已加入收件箱队列')
  const [tick, setTick] = useState(0)
  React.useEffect(() => {
    const id = setInterval(() => setTick((x) => x + 1), ${RERENDER_MS})
    return () => clearInterval(id)
  }, [])
  return React.createElement('div', null,
    React.createElement('span', { 'data-testid': 'tick' }, String(tick)),
    React.createElement(Toast, {
      msg, type: 'ok',
      onDone: () => { globalThis.__toastDone = true; setMsg('') },
    }))
}
`
  try {
    writeFileSync(join(dir, 'toast-under-test.jsx'), source)
    // issue #202 R4: ui.jsx 现引用共享图标库 ./icons.jsx — 沙箱目录同步拷入,
    // 否则 esbuild 无法解析 (Toast 本体不用 Icon, 但模块图必须完整)。
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
    globalThis.IS_REACT_ACT_ENVIRONMENT = true
    globalThis.__toastDone = false

    const React = (await import('react')).default
    const { createRoot } = await import('react-dom/client')
    const { act } = React
    const { Harness } = await import('file://' + outfile)

    const root = createRoot(window.document.getElementById('root'))
    // flake 根因修复 (R 批 s5b 实测: 基线 2785fcb 上本套件 3 跑 2 红):
    // 计时必须用单调钟 performance.now, 不能用 Date.now — 本容器墙钟存在
    // NTP 步进 (实测 3.2s 区间 Date.now 可少报 ~110~220ms), 旧写法 elapsed
    // 恒可低于 3200 ⇒ 假红。t0 同时提前到 render act 前 (定时器在 act 内
    // 挂载时起表, act 后取 t0 还会叠加冲刷耗时)。断言语义不变:
    // 仍要求 ≥3200 且 ≤3200+1200。
    const t0 = performance.now()
    await act(async () => { root.render(React.createElement(Harness)) })
    const shown = window.document.getElementById('root').textContent.includes('已加入收件箱队列')
    // 轮询等待 toast 自动消失 (上限: 3.2s 定时 + 余量)
    const deadline = t0 + TOAST_MS + 2000
    while (!globalThis.__toastDone && performance.now() < deadline) {
      await act(async () => { await new Promise((r) => setTimeout(r, 50)) })
    }
    const elapsed = performance.now() - t0
    const ticks = Number(window.document.querySelector('[data-testid="tick"]')?.textContent || 0)
    const stillInDom = window.document.getElementById('root').textContent.includes('已加入收件箱队列')
    await act(async () => { root.unmount() })
    dom.window.close()
    return { shown, done: globalThis.__toastDone, elapsed, ticks, stillInDom }
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

test('L1: 修复后 — 父组件 60ms 高频重渲染下 toast 仍在约 3.2s 自动消失', async () => {
  const r = await runToastScenario({ source: uiSrc })
  assert(r.shown, 'toast 未渲染出来 ⇒ 场景本身失效')
  assert(r.ticks >= 5, `父组件重渲染次数 ${r.ticks} 过少 ⇒ 未真正压测定时器重置 (场景失效)`)
  assert(r.done, `每 ${RERENDER_MS}ms 重渲染 (${r.ticks} 次) 下 toast 未自动消失 (耗时 ${r.elapsed}ms, 仍在 DOM=${r.stillInDom}) ⇒ #195 未修好`)
  assert(r.elapsed >= TOAST_MS, `toast 在 ${r.elapsed}ms 就消失 ⇒ 早于 ${TOAST_MS}ms 定时器, 场景口径异常`)
  assert(r.elapsed <= TOAST_MS + 1200, `toast 耗时 ${r.elapsed}ms 远超 ${TOAST_MS}ms ⇒ 不是被定时器正常触发的`)
})

test('L2 变异反证: 旧形态 (deps=[msg,onDone]) 同场景必须不消失 (L1 非恒绿)', async () => {
  // 把源码改回 s5 修复前的缺陷形态 (与 commit 前 ui.jsx 逐字同构)。
  const mutated = uiSrc
    .replace(/const\s+onDoneRef\s*=\s*useRef\(onDone\)[\s\S]*?useEffect\(\(\)\s*=>\s*\{\s*onDoneRef\.current\s*=\s*onDone\s*\}\)/, '')
    .replace('const t = setTimeout(() => onDoneRef.current?.(), 3200)', 'const t = setTimeout(onDone, 3200)')
    .replace('  }, [msg])', '  }, [msg, onDone])')
  assert(mutated !== uiSrc, '变异未生效 (源码形态与预期不符)')
  assert(/\[msg, onDone\]/.test(mutated), '变异后应含 [msg, onDone]')
  const r = await runToastScenario({ source: mutated })
  assert(r.shown, '变异体未渲染出 toast (场景失效)')
  assert(r.ticks >= 5, `变异体场景重渲染次数 ${r.ticks} 过少 (场景失效)`)
  assert(!r.done, '把源码改回 deps=[msg,onDone] 后 toast 仍然自动消失 ⇒ L1 断言恒绿, 守护无效 (变异存活)')
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
