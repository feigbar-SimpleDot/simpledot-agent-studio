#!/usr/bin/env node
// 单测: L 批 s8b 回流修复 D-1 — FileDrawer 上传/删除后文件树不自动刷新。
//
// 缺陷 (docs/TEST-REPORT-L-20261029.md §6 D-1, tester 真机复现):
//   文件浏览器抽屉内上传/删除成功后, 左侧文件树不自动刷新, 仍显示旧列表;
//   手动点 ⟳ 或重开抽屉才恢复。服务端状态始终正确 ⇒ 数据正确性不受影响,
//   属 UX 缺陷 (P2), 且为 issue#46 预存在缺陷 (非 L 批引入)。
//
// 根因 (tester 已代码级定位, 本套件复核确认):
//   FileDrawer.jsx 把 refreshKey prop 与内部 refreshTick 直接相加喂给
//   FileTree, 而 refreshKey prop 在唯一调用处 (ChatView.jsx 的 <FileDrawer>)
//   从未传入 ⇒ undefined + n = NaN ⇒ React 依赖比较用 Object.is(NaN, NaN)
//   === true ⇒ FileTree 的 useEffect([loadRoot, refreshKey]) 永不重跑。
//   ⇒ 上传 / 删除 / ⟳ 三条路径后文件树都不刷新。
//
// ★ 为何本套件必须是【运行时】用例 (BRIEF-s8b §2 的硬要求):
//   #158 的既有前端守护 (ui/tests/test_issue158_fs_write.mjs) A2/B3 只做
//   【源码 grep】(断言 setRefreshTick 出现在 doUpload/doDelete 体内) ——
//   而 setRefreshTick 确实在源码里, 但它 bump 的那个键喂到 FileTree 之前
//   已经变成 NaN ⇒ 源码 grep 恒绿, 运行时却是坏的。本缺陷正是"源码 grep
//   形态守卫生效但行为失效"的典型 ⇒ 必须真挂载组件、真发请求、真数请求数。
//
// 本套件五级护栏:
//   L1 运行时级 (jsdom + react-dom/client + esbuild bundle): 真挂载
//      FileDrawer, 驱动隐藏 file input 的 change 事件走完「上传 → 成功 →
//      树重新拉取」全链路, 断言 GET /api/fs/list 的请求数确实增加 (闭环
//      实证, 不是只断言"按钮存在")。同型再验删除路径与 ⟳ 手动刷新路径。
//   L2 语义级 (node:vm): 直接求值「喂给 FileTree 的刷新键」的那段计算,
//      断言 undefined / NaN / null / 非数 一律归 0 且结果恒为有限数
//      (NaN 会让 Object.is 恒真 ⇒ 依赖永不触发)。
//   L3 源码形态级: 断言 FileDrawer 不再出现裸 `refreshKey + refreshTick`
//      形态 (旧缺陷的直写形态), 且 FileTree 的 effect 依赖里 refreshKey
//      仍在位 (防止"修法"退化成"删掉依赖")。
//   L4 编译产物级 (esbuild 压缩): 修法进了压缩产物 (结构化形态匹配,
//      不按变量名 —— 压缩器会改名)。
//   L5 已入库构建产物级 (ui/dist/index.html): 改了源码但没重建 dist ⇒
//      转红 (dist 纪律: 仓库只 tracked 一份构建产物)。
//
// 依赖: L1 需要 jsdom (devDependency)。若环境未装 jsdom, L1 显式报"缺件"
//       而非静默跳过 (缺件 = 护栏没跑 = 必须被看见, 不能假绿)。
// 形态: 与 ui/tests/test_issue75_chat_asi.mjs 的 L1 同基建 (jsdom 渲染级),
//       脚手架要点见该文件头注释; 本套件另有两处不同的坑, 已在下文标注。

import { readFileSync, mkdtempSync, rmSync } from 'fs'
import { join } from 'path'
import vm from 'node:vm'

const UI_DIR = new URL('../', import.meta.url).pathname
const drawerSrc = readFileSync(
  new URL('../src/components/filebrowser/FileDrawer.jsx', import.meta.url), 'utf8')
const treeSrc = readFileSync(
  new URL('../src/components/filebrowser/FileTree.jsx', import.meta.url), 'utf8')

const tests = []
function test(name, fn) { tests.push({ name, fn }) }
function assert(cond, msg) { if (!cond) throw new Error(msg) }
function eq(a, b, msg) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b)
  if (sa !== sb) throw new Error(`${msg || 'eq'}: ${sa} !== ${sb}`)
}

// ═════════════════ L2: 语义级 (node:vm) ═════════════════════════════
// 把「喂给 FileTree 的刷新键」那段计算抽出来在 vm 里求值, 断言:
//   (1) 旧缺陷输入 (refreshKey=undefined) 得到有限数, 不是 NaN;
//   (2) 各类非有限输入一律归 0;
//   (3) refreshTick 递增 ⇒ 刷新键严格递增 (真变化, 不是恒等)。
//
// 抽取方式: 按 "treeRefreshKey" 标识定位赋值语句, 取其右侧表达式求值。
// 若源码里没有该标识 (修法被改写/删除) ⇒ 本套件显式报失败, 而不是
// 静默跳过 —— 否则"守护自己被改没了"会假绿。
//
// issue #169 第4条 (F-D1-1): 旧实现取"等号右侧到行尾"的**单行** ⇒ 一旦
// 该语句被合法重构为多行 (如 `(...) + refreshTick` 换行), 抽出半截表达式
// ⇒ vm 求值语义漂移 ⇒ 用例显式转红 (阻断开发, 非假绿)。改法 = 跨行平衡
// 括号扫描: 从等号右侧起维护 ()[]{} 合计深度, 跳过行/块注释与字符串字面量;
// 深度归零后遇行尾/分号且下一行不是运算符续行 ⇒ 表达式结束。单行形态
// 照样抽出 (既有 4 条 L2 用例零改动仍绿), 多行重构同样覆盖。
// 扫不到闭合 (括号不配平/字符串未闭合) ⇒ 显式抛错转红, 非假绿。
function extractTreeRefreshKeyExpr(source) {
  const idx = source.indexOf('treeRefreshKey')
  assert(idx >= 0,
    'FileDrawer 未找到 treeRefreshKey 标识 —— D-1 修法被改写/删除, 本套件判定口径失效')
  // 找到 "const treeRefreshKey = <expr>" 的等号 (必须与标识同行, 形态守护)
  const lineStart = source.lastIndexOf('\n', idx) + 1
  const nlAfter = source.indexOf('\n', idx)
  const declLineEnd = nlAfter < 0 ? source.length : nlAfter
  const eqIdx = source.indexOf('=', idx)
  assert(eqIdx >= 0 && eqIdx < declLineEnd,
    `treeRefreshKey 声明行形态异常: ${source.slice(lineStart, declLineEnd).trim()}`)
  // 跨行平衡扫描: depth = ()[]{} 三种括号深度的合计
  const n = source.length
  const isOpStart = (j) => {           // 下一非空白内容是否运算符续行
    while (j < n && /\s/.test(source[j])) j++
    return j < n && '+-*/%?:.&|'.includes(source[j])
  }
  let i = eqIdx + 1, depth = 0, end = -1
  while (i < n) {
    const ch = source[i]
    if (ch === '/' && source[i + 1] === '/') {          // 行注释
      if (depth === 0) { end = i; break }
      const eol = source.indexOf('\n', i)
      i = eol < 0 ? n : eol
      continue
    }
    if (ch === '/' && source[i + 1] === '*') {          // 块注释
      if (depth === 0) { end = i; break }
      const e = source.indexOf('*/', i + 2)
      assert(e >= 0, 'treeRefreshKey 表达式含未闭合块注释 (形态异常, 显式转红)')
      i = e + 2
      continue
    }
    if (ch === "'" || ch === '"' || ch === '`') {       // 字符串字面量
      const q = ch
      i++
      while (i < n && source[i] !== q) { if (source[i] === '\\') i++; i++ }
      assert(i < n, 'treeRefreshKey 表达式含未闭合字符串 (形态异常, 显式转红)')
      i++
      continue
    }
    if (ch === '(' || ch === '[' || ch === '{') { depth++; i++; continue }
    if (ch === ')' || ch === ']' || ch === '}') {
      depth--
      assert(depth >= 0, 'treeRefreshKey 表达式括号不配平 (形态异常, 显式转红)')
      i++
      continue
    }
    if ((ch === '\n' || ch === ';') && depth === 0) {   // 深度归零处的行尾/分号
      if (ch === '\n' && isOpStart(i + 1)) { i++; continue }  // 运算符续行 ⇒ 未结束
      end = i
      break
    }
    i++
  }
  if (end < 0) end = n
  const expr = source.slice(eqIdx + 1, end).trim()
  assert(expr.length > 0, 'treeRefreshKey 表达式为空')
  return expr
}

function evalTreeRefreshKey(expr, refreshKey, refreshTick) {
  const ctx = vm.createContext({ refreshKey, refreshTick, Number })
  vm.runInContext(`globalThis.__out = (${expr})`, ctx)
  return ctx.__out
}

test('L2: refreshKey 缺失 (旧缺陷输入) ⇒ 刷新键为有限数, 不是 NaN', () => {
  const expr = extractTreeRefreshKeyExpr(drawerSrc)
  const out = evalTreeRefreshKey(expr, undefined, 0)
  assert(typeof out === 'number' && Number.isFinite(out),
    `刷新键必须是有限数 (旧缺陷得 NaN), 实际: ${String(out)}`)
  eq(out, 0, 'refreshKey 缺失 + tick=0 ⇒ 刷新键应为 0')
})

test('L2: 非有限 refreshKey (undefined/NaN/null/非数) 一律归 0', () => {
  const expr = extractTreeRefreshKeyExpr(drawerSrc)
  for (const bad of [undefined, NaN, null, 'abc', {}, []]) {
    const out = evalTreeRefreshKey(expr, bad, 0)
    assert(Number.isFinite(out),
      `refreshKey=${String(bad)} ⇒ 刷新键必须是有限数, 实际: ${String(out)}`)
    eq(out, 0, `refreshKey=${String(bad)} ⇒ 应归 0`)
  }
})

test('L2: 有限 refreshKey 正常透传 (不吞掉调用方的真实意图)', () => {
  const expr = extractTreeRefreshKeyExpr(drawerSrc)
  eq(evalTreeRefreshKey(expr, 7, 0), 7, 'refreshKey=7, tick=0 ⇒ 应为 7')
  eq(evalTreeRefreshKey(expr, 7, 3), 10, 'refreshKey=7, tick=3 ⇒ 应为 10')
})

test('L2: refreshTick 递增 ⇒ 刷新键严格递增 (依赖必触发)', () => {
  const expr = extractTreeRefreshKeyExpr(drawerSrc)
  // ★ 关键语义: React 用 Object.is 比较依赖 ⇒ NaN 与 NaN 恒相等 (依赖不触发)。
  //   故断言"两次取值不相等" (Object.is 口径), 而非仅"不相等"。
  const a = evalTreeRefreshKey(expr, undefined, 0)
  const b = evalTreeRefreshKey(expr, undefined, 1)
  assert(!Object.is(a, b),
    `两次刷新键 Object.is 相等 ⇒ 依赖永不触发 (旧缺陷): ${String(a)} vs ${String(b)}`)
  const c = evalTreeRefreshKey(expr, undefined, 2)
  assert(!Object.is(b, c), '相邻两次刷新键 Object.is 相等 ⇒ 依赖永不触发')
})

// ═════════════════ L3: 源码形态级 ════════════════════════════════════
test('L3: FileDrawer 不再出现裸 `refreshKey + refreshTick` 形态', () => {
  assert(!/refreshKey\s*\+\s*refreshTick/.test(drawerSrc),
    'FileDrawer 仍存在裸 `refreshKey + refreshTick` (D-1 旧形态未除)')
})

test('L3: FileTree 的刷新 effect 依赖里 refreshKey 仍在位 (防"删依赖"式修法)', () => {
  // 修法退化成"把 refreshKey 从依赖数组里删掉"会让树永不刷新, 且源码 grep
  // (若只看 FileDrawer) 仍绿 ⇒ 这里钉住依赖项不得消失。
  const m = treeSrc.match(/useEffect\(\(\) => \{[\s\S]*?\}, \[loadRoot, refreshKey\]\)/)
  assert(m, 'FileTree 未找到 `useEffect(..., [loadRoot, refreshKey])` 形态')
})

test('L3: FileDrawer 把计算好的刷新键喂给 FileTree (不是裸 prop)', () => {
  assert(/refreshKey=\{treeRefreshKey\}/.test(drawerSrc),
    'FileTree 未收到 treeRefreshKey (接线断了)')
})

// ═════════════════ L4: 编译产物级 (esbuild 压缩) ══════════════════════
// 编译产物级守"源码修了但产物没重建"这一类漏做 (dist 纪律: 仓库只 tracked
// 一份构建产物 ui/dist/index.html, 前端改动后必须重建并同 commit 提交)。
//
// ★ 判定口径 (结构化, 不 grep 某个精确字面量): 压缩器会把局部变量改名
//   (treeRefreshKey → R 之类), 所以不能按名字找。要找的是【语义形态】:
//   "把某个值用 Number.isFinite 判过再与一个 state 相加"。
//   压缩后该形态形如 (Number.isFinite(X)?X:0)+Y ⇒ 用正则扫全部出现位置,
//   断言至少命中一处 (命中数 > 0 ⇒ 修法进了产物)。
//   ⚠ 若产物里一处都没有 ⇒ 说明"源码修了但没重建 dist" ⇒ 本护栏转红。
test('L4: esbuild 压缩产物含刷新键的有限数兜底形态 (防"源码修了没重建")', async () => {
  const { build } = await import('esbuild')
  const out = await build({
    stdin: {
      contents: drawerSrc,
      resolveDir: new URL('../src/components/filebrowser', import.meta.url).pathname,
      loader: 'jsx',
    },
    bundle: false, minify: true, write: false, target: 'es2020',
  })
  const code = out.outputFiles[0].text
  const guard = code.match(/Number\.isFinite\([A-Za-z_$][\w$]*\)\?([A-Za-z_$][\w$]*):0\)\+/g) || []
  assert(guard.length > 0,
    '压缩产物里找不到「有限数兜底 + 相加」的刷新键形态 —— 修法未进产物 (需重建 dist)')
  // 兜底的值与加数必须是同一个变量 (即 (X?X:0)+Y), 防止形态相似但语义错位
  for (const g of guard) {
    const m = g.match(/Number\.isFinite\(([A-Za-z_$][\w$]*)\)\?([A-Za-z_$][\w$]*):0\)\+/)
    assert(m && m[1] === m[2],
      `兜底形态语义错位 (判的值与兜的值不是同一个): ${g}`)
  }
})

// ═════════════════ L5: 已入库构建产物级 (ui/dist/index.html) ═══════════
// ★ 与 L4 互补: L4 只证明"源码能编译出正确形态"; 本级证明【仓库里那份
//   唯一入库的构建产物 ui/dist/index.html】里也带着修法。
//   漏做 "npm run build" 时: 源码对、产物旧 ⇒ 本级转红。
//   (dist 纪律: ui/dist/index.html 是唯一 tracked 构建产物, 必须与源码同 commit)
test('L5: 入库构建产物 ui/dist/index.html 含刷新键的有限数兜底形态', () => {
  const distPath = new URL('../dist/index.html', import.meta.url)
  let html = ''
  try {
    html = readFileSync(distPath, 'utf8')
  } catch {
    // 产物缺失 = C3 红 (dist 纪律) ⇒ 本护栏直接转红, 不静默跳过。
    throw new Error('ui/dist/index.html 不存在 (dist 纪律: 唯一入库构建产物丢失)')
  }
  const guard = html.match(/Number\.isFinite\([A-Za-z_$][\w$]*\)\?[A-Za-z_$][\w$]*:0\)\+/g) || []
  assert(guard.length > 0,
    '已入库构建产物 ui/dist/index.html 里没有修法形态 ⇒ 改了源码但没重建 dist')
})

// ═════════════════ L1: 运行时级 (jsdom + react-dom/client) ═══════════
// 真挂载 FileDrawer → 驱动 file input 的 change → 断言树真的重新拉了。
//
// 脚手架要点 (逐条踩过的坑, 别删):
//   (1) bundle 必须落在 ui/ 目录下 —— node 的 node_modules 解析要求 bundle
//       与 ui/node_modules 同级或子级, 否则 import 'react' 解析失败。
//   (2) react 系必须 external —— 否则 bundle 内嵌一份 React, 与测试侧
//       import 的 React 是两个实例 → dispatcher 为 null → useContext /
//       useTranslation 崩 (与被测缺陷无关, 是测试脚手架问题)。
//   (3) Node 21+ 的 globalThis.navigator 是只读 getter → 必须
//       Object.defineProperty 覆盖, 直接赋值会抛 TypeError。
//   (4) 组件内 import React 是 JSX 运行时必需 (bundle 里的 JSX 会编译成
//       React.createElement) ⇒ 入口文件须自带 `import React from 'react'`。
//   (5) fetch 桩要覆盖 /api/fs/list (树拉取)、/api/fs/upload (上传)、
//       /api/fs/file (预览拉取)。fsUpload 走 FileReader.readAsDataURL ⇒
//       jsdom 的 File/FileReader 可用, 实测可拿到 data URL。
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
    // ★ FileReader / File / Blob 必须挂到 globalThis: api.js 的 fileToDataUrl
    //   用的是裸全局 `new FileReader()`, 而 Node 侧 globalThis.FileReader 压根
    //   不存在 (undefined) ⇒ 上传 promise 直接 reject ⇒ 根本走不到
    //   setRefreshTick ⇒ 断言会因"上传失败"而红, 而不是因 D-1 而红
    //   (那样这条用例就成了假信号: 分不清"上传没成"与"刷新没发生")。
    globalThis.FileReader = window.FileReader
    globalThis.File = window.File
    globalThis.Blob = window.Blob

    // 记录请求; 树内容随上传成功而变化 (模拟服务端真实状态)。
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
            // ★ refreshKey 故意不传 —— 与真实调用处 (ChatView.jsx) 一致,
            //   正是旧缺陷的触发条件 (prop 缺失 ⇒ NaN)。
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
    }
  } finally {
    // 临时 bundle 目录清理 (不留副产物在仓库里)
    try { rmSync(dir, { recursive: true, force: true }) } catch { /* ignore */ }
  }
}

// 驱动隐藏 file input 的 change 事件 (等价于用户在系统文件框里选中文件)。
// jsdom 的 file input 无法真正"选中文件", 只能手动定义 files 属性后派发
// change 事件 —— React 的 onChange 监听的是 change 事件, 实测可触发。
async function pickFiles(r, names) {
  const input = r.rootEl.querySelector('input[type=file]')
  assert(input, '未找到隐藏的 file input (上传入口缺失)')
  const files = names.map((n) => new r.window.File(['x'], n, { type: 'text/plain' }))
  Object.defineProperty(input, 'files', { value: files, configurable: true })
  // 组件里 onChange 立刻回写 e.target.value = '' ⇒ React 的值追踪器会认为
  // 值没变而吞掉事件 ⇒ 必须先把 value 改成一个非空假值再派发。
  Object.defineProperty(input, 'value', {
    value: 'C:\\fakepath\\' + names[0], writable: true, configurable: true,
  })
  await r.act(async () => {
    input.dispatchEvent(new r.window.Event('change', { bubbles: true }))
  })
  await r.settle()
}

// 文件树条目按钮 (TreeEntry 用 title={entry.name} 标记)。
// ★ 必须按 title 定位树条目, 不能拿 innerHTML 全文判: 删除成功的轻提示
//   "Deleted readme.md" 与上传成功的 "Uploaded x.txt" 同样含文件名 ⇒
//   全文匹配会把"提示条还在"误判成"树没刷新" (反之亦然) ⇒ 假信号。
// 排除抽屉头部的 ⟳ / ✕ (title = Refresh / Close)。
function treeEntries(r) {
  return [...r.rootEl.querySelectorAll('button[title]')]
    .filter((b) => !['Refresh', 'Close'].includes(b.getAttribute('title')))
    .map((b) => b.getAttribute('title'))
}

test('L1: FileDrawer 挂载不抛错且渲染出抽屉 (基线)', async () => {
  const r = await renderDrawer()
  try {
    assert(r.renderErr === null,
      `FileDrawer 挂载抛错: ${r.renderErr && r.renderErr.message}`)
    assert(r.rootEl.childNodes.length > 0, 'React 树被卸载 (#root 无子节点)')
    assert(r.listCalls().length >= 1,
      `挂载后未拉取文件树 (calls=${JSON.stringify(r.calls)})`)
    assert(treeEntries(r).includes('readme.md'),
      `文件树未渲染出 readme.md: ${JSON.stringify(treeEntries(r))}`)
  } finally {
    await r.act(async () => { r.root.unmount() })
  }
})

test('L1: 上传成功 → 文件树重新拉取 (D-1 核心闭环)', async () => {
  const r = await renderDrawer()
  try {
    assert(r.renderErr === null, `挂载抛错: ${r.renderErr && r.renderErr.message}`)
    const before = r.listCalls().length
    await pickFiles(r, ['uploaded.txt'])
    const after = r.listCalls().length
    assert(after > before,
      `上传后文件树未重新拉取 (before=${before}, after=${after}) —— D-1 未修`)
    // 闭环实证: 树里确实出现了新上传的文件 (不只是多发了一次请求)
    assert(treeEntries(r).includes('uploaded.txt'),
      `上传后文件树未显示新文件: ${JSON.stringify(treeEntries(r))}`)
  } finally {
    await r.act(async () => { r.root.unmount() })
  }
})

test('L1: 连续两次上传 → 树各刷新一次 (刷新键随 tick 严格变化)', async () => {
  const r = await renderDrawer()
  try {
    const before = r.listCalls().length
    await pickFiles(r, ['one.txt'])
    const mid = r.listCalls().length
    await pickFiles(r, ['two.txt'])
    const after = r.listCalls().length
    assert(mid > before, `第一次上传后未刷新 (before=${before}, mid=${mid})`)
    assert(after > mid, `第二次上传后未刷新 (mid=${mid}, after=${after})`)
    const entries = treeEntries(r)
    assert(entries.includes('one.txt') && entries.includes('two.txt'),
      `第二次上传后文件树未显示两个新文件: ${JSON.stringify(entries)}`)
  } finally {
    await r.act(async () => { r.root.unmount() })
  }
})

test('L1: 删除成功 → 文件树重新拉取 (D-1 同源路径)', async () => {
  const r = await renderDrawer()
  try {
    // 选中根层文件 → 点删除 → 确认框点确认 → 服务端删除 → 树刷新
    const fileBtn = [...r.rootEl.querySelectorAll('button')]
      .find((b) => /readme\.md/.test(b.textContent || ''))
    assert(fileBtn, '文件树里未找到 readme.md 按钮')
    await r.act(async () => {
      fileBtn.dispatchEvent(new r.window.MouseEvent('click', { bubbles: true }))
    })
    await r.settle()

    // 动作条上的删除入口 (issue #202 R4: ✕ 文字符号已换 SVG close 图标,
    // 定位锚改文案 delete; 与确认框里的裸 "Delete" 由 dialog 作用域区分)
    const delBtn = [...r.rootEl.querySelectorAll('button')]
      .find((b) => /delete/i.test(b.textContent || ''))
    assert(delBtn, '未找到动作条删除按钮')
    await r.act(async () => {
      delBtn.dispatchEvent(new r.window.MouseEvent('click', { bubbles: true }))
    })
    await r.settle()

    // 确认框 (role=alertdialog) 里的确认按钮 —— 必须限定在对话框内选,
    // 否则会误选动作条上的删除入口 (两者文案都含 delete)。
    const dialog = r.rootEl.querySelector('[role=alertdialog]')
    assert(dialog, '未弹出删除二次确认框')
    const before = r.listCalls().length
    const confirmBtn = [...dialog.querySelectorAll('button')]
      .find((b) => /delete/i.test(b.textContent || ''))
    assert(confirmBtn, '确认框内未找到确认删除按钮')
    await r.act(async () => {
      confirmBtn.dispatchEvent(new r.window.MouseEvent('click', { bubbles: true }))
    })
    await r.settle()
    const after = r.listCalls().length
    assert(after > before,
      `删除后文件树未重新拉取 (before=${before}, after=${after}) —— D-1 未修`)
    assert(!treeEntries(r).includes('readme.md'),
      `删除后文件树仍显示 readme.md: ${JSON.stringify(treeEntries(r))}`)
  } finally {
    await r.act(async () => { r.root.unmount() })
  }
})

test('L1: ⟳ 手动刷新 → 文件树重新拉取 (旧缺陷下该路径同样失效)', async () => {
  const r = await renderDrawer()
  try {
    const before = r.listCalls().length
    // issue #202 R4: ⟳ 文字符号已换 SVG refresh 图标, 定位锚改 aria-label
    const refreshBtn = [...r.rootEl.querySelectorAll('button')]
      .find((b) => /refresh/i.test(b.getAttribute('aria-label') || ''))
    assert(refreshBtn, '未找到刷新按钮 (aria-label=refresh)')
    await r.act(async () => {
      refreshBtn.dispatchEvent(new r.window.MouseEvent('click', { bubbles: true }))
    })
    await r.settle()
    const after = r.listCalls().length
    assert(after > before,
      `点 ⟳ 后文件树未重新拉取 (before=${before}, after=${after}) —— D-1 未修`)
  } finally {
    await r.act(async () => { r.root.unmount() })
  }
})

// ── runner ──────────────────────────────────────────────────────────
let pass = 0, fail = 0
for (const { name, fn } of tests) {
  try { await fn(); pass++; console.log(`  ✓ ${name}`) }
  catch (e) { fail++; console.error(`  ✗ ${name}\n    ${e.message}`) }
}
console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail ? 1 : 0)
