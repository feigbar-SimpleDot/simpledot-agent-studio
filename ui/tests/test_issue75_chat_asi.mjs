#!/usr/bin/env node
// 单测: issue #75 s10 D-1 (P0 白屏崩溃) + C-1 (首拉失败永不重试) 回归护栏。
//
// 背景 (docs/TEST-REPORT-75.md §1 D-1 / §3 C-1):
//   D-1 (P0): ChatView.jsx 首拉历史 effect 里
//       didLoadHistRef.current = true
//       (async () => { ... })()
//     赋值后换行接 '(' → ASI 不生效 → 解析成 `true(async()=>{...})()`
//     → TypeError: true is not a function → React 树卸载 → 整页白屏。
//     esbuild 压缩后原样保留 `current=(!0)(async()=>{...})`。
//   C-1 (P2): 首拉失败时 didLoadHistRef 已置 true 且无回滚 → 切走再回永不
//     重拉, 错误条无 Retry → 用户必须刷新页面才能恢复。
//
// 本套件三级护栏 (卡内硬要求: 纯字符串/逻辑单测抓不住 ASI, 必须渲染级):
//   L1 渲染级 (jsdom + react-dom/client + esbuild bundle): 真挂载 ChatView
//      带 sessionId, 断言不抛错、#root 有子节点 (D-1 白屏 = 树被卸载 =
//      子节点 0), 且首拉成功后渲染出消息气泡 (证明 effect 真跑到
//      loadHistoryWindow, 且 AC-11 的 UI 面成立)。
//      另含 C-1 的失败路径: 首拉 502 → 错误条 + Retry 按钮出现在 DOM,
//      且 Retry 能真的重发请求 (闭环实证, 不是只渲染了个按钮)。
//   L2 编译产物级 (esbuild): 压缩产物里不得出现 ASI 同型 (`=(!0)(async`),
//      防止"源码修了但产物没重建"漏做 (dist 纪律, INV-12/13)。
//   L3 语义级 (node:vm): 直接 eval 该 effect 的 IIFE, 断言不抛
//      "true is not a function"; 并断言 C-1 语义 (失败回滚闸门 / 成功保持
//      闸门)。
//   L4 全仓同型扫描: 源码级扫 `= true/false/null` 后紧跟 `(` 的 ASI 隐患行。
//
// 依赖: L1 需要 jsdom (devDependency)。若环境未装 jsdom, L1 显式报"缺件"
// 而非静默跳过 (缺件 = 护栏没跑 = 必须被看见, 不能假绿)。
// 选型取舍与依赖归属记录在 docs/FIX-NOTES-75C.md。

import { readFileSync, mkdtempSync, rmSync } from 'fs'
import { join } from 'path'
import vm from 'node:vm'

const SRC = new URL('../src/views/ChatView.jsx', import.meta.url)
const src = readFileSync(SRC, 'utf8')
const UI_DIR = new URL('../', import.meta.url).pathname

const tests = []
function test(name, fn) { tests.push({ name, fn }) }
function assert(cond, msg) { if (!cond) throw new Error(msg) }
function eq(a, b, msg) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b)
  if (sa !== sb) throw new Error(`${msg || 'eq'}: ${sa} !== ${sb}`)
}

// ═════════════════ 源码定位工具 ═══════════════════════════════════
// 坑 (本套件实测踩到): 源码注释里含 `true(async()=>{...})()` 字样, 朴素
// 「找第一个 `})()`」会命中注释里的那段 → 抽出的 IIFE 片段被截断。故先做
// 「等长注释/字面量置空」: 内容替换成等长空格, 偏移量完全不变 → 所有下标
// 定位既不受注释干扰, 又能原样切回带注释的原文做 eval。
function blankComments(source) {
  const out = source.split('')
  let i = 0
  const n = source.length
  while (i < n) {
    const c = source[i], d = source[i + 1]
    if (c === '/' && d === '/') {
      while (i < n && source[i] !== '\n') { out[i] = ' '; i++ }
      continue
    }
    if (c === '/' && d === '*') {
      out[i] = ' '; out[i + 1] = ' '; i += 2
      while (i < n && !(source[i] === '*' && source[i + 1] === '/')) {
        if (source[i] !== '\n') out[i] = ' '
        i++
      }
      if (i < n) { out[i] = ' '; out[i + 1] = ' '; i += 2 }
      continue
    }
    i++
  }
  return out.join('')
}

function blankStrings(source) {
  const out = source.split('')
  let i = 0
  const n = source.length
  while (i < n) {
    const c = source[i]
    if (c === '"' || c === "'" || c === '`') {
      const q = c
      out[i] = ' '
      i++
      while (i < n) {
        if (source[i] === '\\') {
          out[i] = ' '
          if (i + 1 < n) out[i + 1] = ' '
          i += 2
          continue
        }
        if (source[i] === q) { out[i] = ' '; i++; break }
        if (source[i] !== '\n') out[i] = ' '
        i++
      }
      continue
    }
    i++
  }
  return out.join('')
}

const clean = blankStrings(blankComments(src))

// 取首拉 effect 回调体: 定位 `useEffect(() => {` 的函数体 `{`, 按花括号
// 配平取到闭合 (只数花括号 —— 体内圆括号自身配平, 不参与判定)。
function extractEffect(source, cleanSource, anchor) {
  const anchorIdx = cleanSource.indexOf(anchor)
  if (anchorIdx < 0) throw new Error(`源码抽取失败, 未找到锚点: ${anchor}`)
  const effectStart = cleanSource.lastIndexOf('useEffect(() => {', anchorIdx)
  if (effectStart < 0) throw new Error('源码抽取失败: 未找到 useEffect 起点')
  const bodyStart = cleanSource.indexOf('{', effectStart)
  let depth = 0
  for (let i = bodyStart; i < cleanSource.length; i++) {
    if (cleanSource[i] === '{') depth++
    else if (cleanSource[i] === '}') {
      depth--
      if (depth === 0) return source.slice(effectStart, i + 1)
    }
  }
  throw new Error('effect 体括号不配平')
}

const effectSrc = extractEffect(src, clean, 'if (didLoadHistRef.current) return')

// IIFE 片段定位: 在「注释/字面量已置空」的坐标里找, 再用同偏移切原文。
function sliceIife(source, cleanSource) {
  const iifeStart = cleanSource.indexOf('void (async () => {')
  assert(iifeStart >= 0, '源码中未找到 `void (async () => {` —— D-1 修复丢失')
  const iifeEnd = cleanSource.indexOf('})()', iifeStart)
  assert(iifeEnd > iifeStart, '源码中 IIFE 结尾未找到')
  return source.slice(iifeStart, iifeEnd + 4)
}

// IIFE 内的自由变量集合 (eval 时必须备齐, 否则 ReferenceError 会掩盖
// 「是否抛 true-is-not-a-function」这一判定)。
const IIFE_FREES = {
  console,
  sessionId: 'sid_1',
  loadHistoryWindow: async () => ({ data: [] }),
  setHistLoadFailed: () => {},
  setHistErr: () => {},
  setHistLoaded: () => {},
  setHistLoading: () => {},
  setHistRetryTick: () => {},
  didLoadHistRef: { current: false },
  histFetchInFlightRef: { current: false },
  histPendingSidRef: { current: '' },
  histCurSidRef: { current: 'sid_1' },
}

// ═════════════════ L3-1: ASI 语义级 (node:vm) ══════════════════════
// 反向对照: D-1 的原始写法确实会炸 —— 证明本护栏的判定口径有效。
test('L3 对照: D-1 原始写法 (= true 后换行接 "(") 确实抛 TypeError', () => {
  const broken = `
    let didLoadHistRef = { current: false }
    didLoadHistRef.current = true
    (async () => { return 1 })()
  `
  let threw = null
  try { vm.runInNewContext(broken, {}) } catch (e) { threw = e }
  assert(threw !== null, 'D-1 原始写法应当抛错 (若不抛, 说明 ASI 语义已变, 结论需重估)')
  assert(/true is not a function/.test(String(threw && threw.message)),
    `期望 TypeError: true is not a function, 实际: ${threw && threw.message}`)
})

test('L3: 首拉 effect 的 IIFE (修后) eval 不抛 (ASI 已消除)', () => {
  const iife = sliceIife(src, clean)
  const ctx = vm.createContext({ ...IIFE_FREES })
  let threw = null
  try { vm.runInContext(iife, ctx) } catch (e) { threw = e }
  assert(threw === null, `修后 IIFE 仍抛错: ${threw && threw.message}`)
  assert(!/true is not a function/.test(String(threw && threw.message)),
    'D-1 未修: 仍抛 true is not a function')
})

// ═════════════════ L3-2: C-1 语义级 (node:vm) ═════════════════════
// 首拉失败 → 幂等闸门回滚 (可重试) + 失败态置位 + 错误原因保留。
test('L3: C-1 首拉失败 → 闸门回滚 false (可重试) + 失败态置位', async () => {
  const iife = sliceIife(src, clean)
  const state = {
    histLoadFailed: false, histErr: '', histLoaded: false, histLoading: false,
  }
  const ctx = vm.createContext({
    ...IIFE_FREES,
    loadHistoryWindow: async () => { throw new Error('injected gateway failure') },
    setHistLoadFailed: (v) => { state.histLoadFailed = v },
    setHistErr: (v) => { state.histErr = v },
    setHistLoaded: (v) => { state.histLoaded = v },
    setHistLoading: (v) => { state.histLoading = v },
  })
  // 同上: 先模拟 effect 体把闸门置 true, 之后断言「IIFE 把它回滚成 false」
  // 才是真断言 (否则该断言恒真 = 空断言, 护栏等于没写)。
  ctx.didLoadHistRef.current = true
  vm.runInContext(iife, ctx)
  await new Promise((r) => setTimeout(r, 20))   // 等 await 链走完
  eq(state.histLoadFailed, true, '首拉失败必须置失败态 (错误条可见)')
  assert(/injected gateway failure/.test(state.histErr),
    `错误原因须保留, 实际: ${state.histErr}`)
  eq(ctx.didLoadHistRef.current, false,
    'C-1 核心: 首拉失败必须回滚幂等闸门 → 下次激活/Retry 可重拉')
  eq(ctx.histPendingSidRef.current, 'sid_1', '须记下待重试的会话 sid')
  eq(state.histLoaded, true, 'finally 必须完成 (骨架屏退场)')
  eq(state.histLoading, false, 'finally 必须解除加载屏蔽 (Retry 按钮可点)')
})

test('L3: C-1 首拉成功 → 闸门保持 true (幂等, 不重复拉) + 无失败态', async () => {
  const iife = sliceIife(src, clean)
  const state = { histLoadFailed: false, histErr: 'x', histLoaded: false, histLoading: false }
  const ctx = vm.createContext({
    ...IIFE_FREES,
    loadHistoryWindow: async () => ({ data: [] }),
    setHistLoadFailed: (v) => { state.histLoadFailed = v },
    setHistErr: (v) => { state.histErr = v },
    setHistLoaded: (v) => { state.histLoaded = v },
    setHistLoading: (v) => { state.histLoading = v },
    histPendingSidRef: { current: 'stale' },
  })
  // 幂等闸门 true 是由 effect 体 (IIFE 之外) 设置的 → eval IIFE 前先模拟
  // 那一步, 否则断言的是「IIFE 从未置 true」的空断言 (恒假/恒真皆无意义)。
  ctx.didLoadHistRef.current = true
  vm.runInContext(iife, ctx)
  await new Promise((r) => setTimeout(r, 20))
  eq(ctx.didLoadHistRef.current, true, '成功路径闸门保持 true (切回不重复拉, INV-01/02)')
  eq(state.histLoadFailed, false, '成功路径不得留失败态')
  eq(state.histErr, '', '成功路径须清空错误原因')
  eq(ctx.histPendingSidRef.current, '', '成功路径须清空待重试标记')
})

test('L3: 过期响应 (会话已切走) 被丢弃, 不写回错误态/不清闸门', async () => {
  // 判别力: 去掉 IIFE 里的 `if (histCurSidRef.current !== sid) return`
  // 过期判定 → 本用例必红 (旧会话的迟到失败会置 histLoadFailed=true,
  // 且把闸门回滚成 false → 污染当前会话)。
  const iife = sliceIife(src, clean)
  const state = { histLoadFailed: false, histErr: '' }
  const ctx = vm.createContext({
    ...IIFE_FREES,
    // 当前会话已是 sid_2, 而本次请求属于 sid_1 (迟到响应)
    histCurSidRef: { current: 'sid_2' },
    loadHistoryWindow: async () => { throw new Error('late failure of old session') },
    setHistLoadFailed: (v) => { state.histLoadFailed = v },
    setHistErr: (v) => { state.histErr = v },
  })
  ctx.didLoadHistRef.current = true
  vm.runInContext(iife, ctx)
  await new Promise((r) => setTimeout(r, 20))
  eq(state.histLoadFailed, false,
    '过期失败不得置失败态 (旧会话的错误条会挂在当前会话上)')
  eq(state.histErr, '', '过期失败不得写回错误原因')
  eq(ctx.didLoadHistRef.current, true,
    '过期失败不得回滚当前会话的幂等闸门 (会让当前会话被重复拉取)')
})

// ═════════════════ L2: 编译产物级 (esbuild) ════════════════════════
// 同型扫描: 编译产物里不得出现 ASI 同型 (`=(!0)(async` (ASI 合并后的
// 调用形态)。这是"源码修了但产物没重建"这一类漏做的护栏 (dist 纪律,
// INV-12/13)。
async function minifiedChatView() {
  const { build } = await import('esbuild')
  const out = await build({
    stdin: {
      contents: src,
      resolveDir: new URL('../src/views', import.meta.url).pathname,
      loader: 'jsx',
    },
    bundle: false,
    minify: true,
    write: false,
    target: 'es2020',
  })
  return out.outputFiles[0].text
}

test('L2: esbuild 压缩产物不含 ASI 同型 `=(!0)(async`', async () => {
  const code = await minifiedChatView()
  const bad = code.match(/=[(!0-9]+\(async/g) || []
  eq(bad, [], `压缩产物含 ASI 同型调用: ${JSON.stringify(bad)}`)
  assert(!/=\(!0\)\(async/.test(code), '压缩产物含 `=(!0)(async` 同型 (D-1 未修)')
})

test('L2: esbuild 压缩产物中无 IIFE 紧跟右括号的危险形态', async () => {
  // esbuild 压缩后 `void (async()=>{})()` 会被折叠成逗号表达式
  // `a=!0,(async()=>{})()` —— 该形态安全 (逗号运算符, 非 ASI 合并)。
  // 危险形态只有一种: `a=!0(async()=>{})()` —— 即 `(async()=>{` 的前一个
  // 字符是 `)` (右括号直接贴着 IIFE 调用)。
  //
  // 判定口径: 扫压缩产物里【每一个】 `(async()=>{` 出现位置, 断言其前一
  // 字符不是 `)`。不指定"第一个" —— 源码里另有多处 IIFE (gwBootstrap 等),
  // 钉第一个会误命中别处而假绿 (本套件实测踩到: 首个 IIFE 是 gwBootstrap
  // 的, 它前面是 `;` → 断言恒过, 对 D-1 真正所在的 IIFE 零覆盖)。
  const code = await minifiedChatView()
  const needle = '(async()=>{'
  const dangers = []
  let idx = code.indexOf(needle)
  let seen = 0
  while (idx >= 0) {
    seen++
    const prev = idx > 0 ? code[idx - 1] : ''
    if (prev === ')') {
      dangers.push(code.slice(Math.max(0, idx - 40), idx + 20))
    }
    idx = code.indexOf(needle, idx + 1)
  }
  assert(seen > 0, '压缩产物中未找到任何 IIFE 形态 (判定口径失效, 需重估)')
  eq(dangers, [], `压缩产物含 IIFE 紧跟右括号的危险 ASI 形态: ${JSON.stringify(dangers)}`)
})

// ═════════════════ L1: 渲染级 (jsdom + react-dom/client) ═══════════
// 真挂载 ChatView (带 sessionId) → 不抛错 + #root 有子节点。
// D-1 白屏的表征 = React 树被卸载 = 容器子节点 0。
//
// 工程要点 (本套件实测踩到, 逐条注释):
//   (1) bundle 必须落在 ui/ 目录下 —— node 的 node_modules 解析要求 bundle
//       与 ui/node_modules 同级或子级, 否则 import 'react' 解析失败。
//   (2) react 系必须 external —— 否则 bundle 内嵌一份 React, 与测试侧
//       import 的 React 是两个实例 → dispatcher 为 null → useContext /
//       useTranslation 崩 (与被测缺陷无关, 是测试脚手架问题)。
//   (3) Node 21+ 的 globalThis.navigator 是只读 getter → 必须
//       Object.defineProperty 覆盖, 直接赋值会抛 TypeError。
//   (4) fetch 桩必须覆盖 /messages (首拉) /api/gwconfig /api/workspaces。
async function renderChatView({ failMessages = false } = {}) {
  const { JSDOM } = await import('jsdom')
  const { build } = await import('esbuild')

  const dir = mkdtempSync(join(UI_DIR, '.l1-tmp-'))
  const outfile = join(dir, 'bundle.mjs')
  const entry = `
export { default as ChatView } from './src/views/ChatView.jsx'
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

    const calls = []
    const fetchImpl = async (url) => {
      calls.push(String(url))
      const u = String(url)
      if (u.includes('/messages')) {
        if (failMessages) {
          return {
            ok: false, status: 502,
            json: async () => ({ error: 'injected gateway failure' }),
          }
        }
        return {
          ok: true, status: 200,
          json: async () => ({
            data: [
              { id: 1, role: 'user', content: 'hello', timestamp: 1760874000 },
              { id: 2, role: 'assistant', content: 'hi there', timestamp: 1760874001 },
            ],
            history: { has_more: false, before_cursor: null },
          }),
        }
      }
      if (u.includes('/api/gwconfig')) {
        return { ok: true, status: 200, json: async () => ({ enabled: true }) }
      }
      if (u.includes('/api/workspaces')) {
        return { ok: true, status: 200, json: async () => ({ workspaces: [], root: '/x' }) }
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
    const { ChatView, initI18n, i18next } = mod

    await act(async () => { initI18n() })
    // localStorage 预置会话 id → ChatView 初始 sessionId 非空 (D-1 触发路径)
    window.localStorage.setItem('sas-chat-sid', 'sid_1')

    const root = createRoot(window.document.getElementById('root'))
    let renderErr = null
    await act(async () => {
      try {
        root.render(React.createElement(I18nextProvider, { i18next },
          React.createElement(ChatView, { active: true })))
      } catch (e) { renderErr = e }
    })
    await act(async () => { await new Promise((r) => setTimeout(r, 100)) })

    const rootEl = window.document.getElementById('root')
    return {
      renderErr,
      childNodes: rootEl ? rootEl.childNodes.length : 0,
      html: rootEl ? rootEl.innerHTML : '',
      calls,
      rootEl,
      window,
      act,
      root,
    }
  } finally {
    // 临时 bundle 目录清理 (不留副产物在仓库里)
    try { rmSync(dir, { recursive: true, force: true }) } catch { /* ignore */ }
  }
}

test('L1: ChatView 带 sessionId 挂载不抛错且渲染出内容 (D-1 白屏护栏)', async () => {
  const r = await renderChatView()
  try {
    assert(r.renderErr === null,
      `ChatView 挂载抛错 (D-1 复现): ${r.renderErr && r.renderErr.message}`)
    assert(r.childNodes > 0,
      'React 树被卸载 (#root 无子节点) —— D-1 白屏复现')
    assert(r.calls.some((u) => u.includes('/messages')),
      `首拉历史请求未发出 (calls=${JSON.stringify(r.calls)})`)
    // 渲染层实锤: 消息气泡出现 (D-1 修完后 AC-11 的 UI 面成立)
    assert(r.html.includes('hello') && r.html.includes('hi there'),
      `消息未渲染 (AC-11 UI 面), innerHTML 前 400 字: ${r.html.slice(0, 400)}`)
  } finally {
    await r.act(async () => { r.root.unmount() })
  }
})

test('L1: C-1 首拉失败 → 错误条 + Retry 按钮出现在 DOM (可重试闭环)', async () => {
  const r = await renderChatView({ failMessages: true })
  try {
    assert(r.renderErr === null,
      `ChatView 挂载抛错: ${r.renderErr && r.renderErr.message}`)
    assert(r.childNodes > 0, 'React 树被卸载 —— D-1 白屏复现')
    // 错误条可见 (含失败原因)
    assert(/injected gateway failure/.test(r.html),
      `错误条未出现失败原因, innerHTML 前 600 字: ${r.html.slice(0, 600)}`)
    // Retry 按钮存在 (AC-15 失败态 → 恢复闭环的入口)
    const buttons = [...r.rootEl.querySelectorAll('button')]
    const retryBtn = buttons.find((b) => /retry/i.test(b.textContent || ''))
    assert(retryBtn !== undefined,
      `未找到 Retry 按钮, 按钮文案: ${JSON.stringify(buttons.map((b) => (b.textContent || '').trim()))}`)
    // 点 Retry → 真发一次重拉请求 (闭环实证, 不是只渲染了个按钮)
    const before = r.calls.filter((u) => u.includes('/messages')).length
    await r.act(async () => {
      retryBtn.dispatchEvent(new r.window.MouseEvent('click', { bubbles: true }))
    })
    await r.act(async () => { await new Promise((res) => setTimeout(res, 100)) })
    const after = r.calls.filter((u) => u.includes('/messages')).length
    assert(after > before,
      `点 Retry 后未重新拉取历史 (before=${before}, after=${after}) —— C-1 闭环未生效`)
  } finally {
    await r.act(async () => { r.root.unmount() })
  }
})

// ═════════════════ L4: 全仓同型扫描 ════════════════════════════════
// 源码级全仓扫描: `= true/false/null` 后紧跟 `(` 的同型 (D-1 家族)。
test('L4: 全仓同型扫描: 无 `= true` 后紧跟 `(` 的 ASI 隐患行', async () => {
  const { readdirSync, statSync } = await import('fs')
  const root = new URL('../src/', import.meta.url).pathname
  const hits = []
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const p = join(dir, name)
      if (statSync(p).isDirectory()) { walk(p); continue }
      if (!/\.(js|jsx|mjs)$/.test(name)) continue
      const lines = readFileSync(p, 'utf8').split('\n')
      for (let i = 0; i < lines.length - 1; i++) {
        const cur = lines[i].replace(/\s+$/, '')
        const nxt = lines[i + 1].trim()
        if (/=\s*(true|false|null)\s*$/.test(cur) && nxt.startsWith('(')) {
          hits.push(`${p}:${i + 1} ${cur.trim()} | ${nxt.slice(0, 40)}`)
        }
      }
    }
  }
  walk(root)
  eq(hits, [], '存在 ASI 隐患行 (赋值后换行接 "(" → 解析为函数调用):')
})

// ── runner ──────────────────────────────────────────────────────────
let pass = 0, fail = 0
for (const { name, fn } of tests) {
  try { await fn(); pass++; console.log(`  ✓ ${name}`) }
  catch (e) { fail++; console.error(`  ✗ ${name}\n    ${e.message}`) }
}
console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail ? 1 : 0)
