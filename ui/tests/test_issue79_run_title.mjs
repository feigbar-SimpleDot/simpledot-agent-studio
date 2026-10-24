// issue #79 — splitRunTitle 纯函数单测 (弹窗 run 标题折叠)。
// 写法沿用 test_todo_viewer_chips.mjs 的「源码抽取直测」路线: 从
// TasksBoardView.jsx 抽 RUN_TITLE_* 常量 + splitRunTitle 函数体喂 vm,
// 不引 JSX 编译链路。抽取锚: 起点 `const RUN_TITLE_PREVIEW_LINES`,
// 终点函数 `splitRunTitle` 花括号配平 (该块整体位于 splitGoalSegments
// 之后、parseDoneResult 之前, 不影响 chips 测试既有抽取区间)。
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import vm from 'node:vm'

const here = path.dirname(fileURLToPath(import.meta.url))
const src = readFileSync(path.join(here, '../src/views/TasksBoardView.jsx'), 'utf8')

// 抽取: 从 const 块起点到 splitRunTitle 函数体结束 (花括号配平)
const startIdx = src.indexOf('const RUN_TITLE_PREVIEW_LINES')
if (startIdx < 0) throw new Error('源码抽取失败: 未找到 const RUN_TITLE_PREVIEW_LINES')
const fnIdx = src.indexOf('function splitRunTitle', startIdx)
if (fnIdx < 0) throw new Error('源码抽取失败: 未找到 function splitRunTitle')
const blockEnd = (() => {
  let braces = 0, seenBrace = false
  for (let i = fnIdx; i < src.length; i++) {
    const ch = src[i]
    if (ch === '{') { braces++; seenBrace = true }
    else if (ch === '}') braces--
    if (seenBrace && braces === 0) return i + 1
  }
  throw new Error('splitRunTitle 括号不配平')
})()

const ctx = vm.createContext({ console })
vm.runInContext(
  src.slice(startIdx, blockEnd) +
    '\nglobalThis.__exports = { splitRunTitle,' +
    ' RUN_TITLE_PREVIEW_LINES, RUN_TITLE_PREVIEW_CHARS };',
  ctx,
)
const { splitRunTitle, RUN_TITLE_PREVIEW_LINES, RUN_TITLE_PREVIEW_CHARS } = ctx.__exports

const tests = []
function test(name, fn) { tests.push({ name, fn }) }
function assert(cond, msg) { if (!cond) throw new Error(msg) }
function eq(a, b, msg) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b)
  if (sa !== sb) throw new Error(`${msg || 'eq'}: ${sa} !== ${sb}`)
}

// 阈值锚: 与 PLAN-79-84 §3.3 A-1 一致
test('阈值: PREVIEW_LINES=2 / PREVIEW_CHARS=120', () => {
  assert(RUN_TITLE_PREVIEW_LINES === 2, `PREVIEW_LINES 应为 2, 实际 ${RUN_TITLE_PREVIEW_LINES}`)
  assert(RUN_TITLE_PREVIEW_CHARS === 120, `PREVIEW_CHARS 应为 120, 实际 ${RUN_TITLE_PREVIEW_CHARS}`)
})

// T79-1: 348 字单行 initial_task (本项目『目标』行量级) → 字符级截断
test('T79-1: 348 字单行 → 截断到 120+…, truncated, 隐藏 1 行', () => {
  const raw = '按'.repeat(348)
  const r = splitRunTitle(raw)
  assert(r.preview.length <= RUN_TITLE_PREVIEW_CHARS + 1,
    `preview 长度应 ≤ 121, 实际 ${r.preview.length}`)
  assert(r.preview.endsWith('…'), `preview 应以 … 结尾, 实际结尾=${JSON.stringify(r.preview.slice(-4))}`)
  assert(r.truncated === true, '348 字单行应 truncated')
  assert(r.hiddenLineCount === 1, `字符截断应记 1 隐藏行, 实际 ${r.hiddenLineCount}`)
  assert(r.full === raw, 'full 必须是全值原文 (全值可达的根据)')
})

// T79-2: 短标题 (实测真实 # Team Work: 短标题量级 42 字) → 不回归
test('T79-2: 短标题不折叠, preview === full', () => {
  const short = 'Team Work: issue #79 + #84 前端可读性与三态归因覆盖面修复 (B 批, 轻量批)'
  const r = splitRunTitle(short)
  assert(r.truncated === false, '短标题不应 truncated')
  assert(r.preview === r.full, '短标题 preview 应等于 full')
  assert(r.hiddenLineCount === 0, '短标题隐藏行数应为 0')
})

// T79-2b: 单行 200 字但 < 阈值? 不, 200 > 120 会截。补一条 120 字整边界: 不截
test('边界: 单行恰 120 字符 → 不截断', () => {
  const line = 'x'.repeat(120)
  const r = splitRunTitle(line)
  assert(r.truncated === false, '恰 120 字符不应截断 (判据是 > 120)')
  assert(r.preview === line, 'preview 应等于原文')
})

// T79-3: 空值防御, 不崩
test('T79-3: null/空串/空白 → 空形态不崩', () => {
  for (const v of [null, undefined, '', '   ', '\n \n']) {
    eq(splitRunTitle(v),
      { preview: '', full: '', truncated: false, hiddenLineCount: 0 },
      `空值形态错误 (${JSON.stringify(String(v))})`)
  }
})

// T79-4: 多行 (>2 行) → 预览只含前 2 行, hiddenLineCount = 总行数-2
test('T79-4: 5 行标题 → 预览前 2 行, 隐藏 3 行', () => {
  const text = ['第一行', '第二行', '第三行', '第四行', '第五行'].join('\n')
  const r = splitRunTitle(text)
  assert(r.preview === '第一行\n第二行', `preview 错误: ${JSON.stringify(r.preview)}`)
  assert(r.truncated === true, '5 行应 truncated')
  assert(r.hiddenLineCount === 5 - 2, `应隐藏 3 行, 实际 ${r.hiddenLineCount}`)
  assert(r.full === text, 'full 应保留全值')
})

test('多行 ≤2 行 → 不折叠 (行数未超上限)', () => {
  const text = '第一行\n第二行'
  const r = splitRunTitle(text)
  assert(r.truncated === false, '恰 2 行不应截断')
  assert(r.preview === text, 'preview 应等于全文')
})

// 设计点回归 (PLAN §3.3 关键设计点): truncated 不看全文长度
test('单行 130 字 (无换行) 会截, 但 2 行共 500 字不看不看长度也截于行数', () => {
  const twoLines = 'a'.repeat(119) + '\n' + 'b'.repeat(119) // 239 字 2 行
  const r = splitRunTitle(twoLines)
  assert(r.truncated === false, '2 行未超行数阈值不应折叠')
  const third = 'a'.repeat(119) + '\n' + 'b'.repeat(119) + '\n' + 'c'.repeat(119)
  const r3 = splitRunTitle(third)
  assert(r3.truncated === true, '3 行应折叠')
  assert(r3.hiddenLineCount === 1, `3 行预览 2 隐藏 1, 实际 ${r3.hiddenLineCount}`)
})

// 运行器
let failed = 0
for (const t of tests) {
  try { t.fn(); console.log(`ok - ${t.name}`) }
  catch (e) { failed++; console.error(`FAIL - ${t.name}\n  ${e.message}`) }
}
if (failed) { console.error(`\n${failed}/${tests.length} 失败`); process.exit(1) }
console.log(`\n全部通过 (${tests.length})`)
