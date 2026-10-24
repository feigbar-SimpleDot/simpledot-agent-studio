// issue #87 S-1 — splitRunTitle 隐藏行数死分支钉桩测试。
// 背景: S-1 裁决 (A) 删掉隐藏行数三元里的字符截断计数死分支 (PLAN-87-86 §1)。
// 抽取锚与 test_issue79_run_title.mjs 完全一致, 不引入第二套口径。
// ⚠️ 实现坑: 结构性断言必须包在 runner 里, 不能在模块顶层 throw ——
//    顶层 throw 会让文件第一条就退出, 后面两条根本没跑, 报告里看不到逐条结果。
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import vm from 'node:vm'

const here = path.dirname(fileURLToPath(import.meta.url))
const src = readFileSync(path.join(here, '../src/views/TasksBoardView.jsx'), 'utf8')

const startIdx = src.indexOf('const RUN_TITLE_PREVIEW_LINES')
const fnIdx = src.indexOf('function splitRunTitle', startIdx)
let braces = 0, seen = false, blockEnd = -1
for (let i = fnIdx; i < src.length; i++) {
  const ch = src[i]
  if (ch === '{') { braces++; seen = true } else if (ch === '}') braces--
  if (seen && braces === 0) { blockEnd = i + 1; break }
}
if (startIdx < 0 || fnIdx < 0 || blockEnd < 0) throw new Error('源码抽取失败')
const fnSrc = src.slice(startIdx, blockEnd)
const fnBody = fnSrc.slice(fnSrc.indexOf('function splitRunTitle'))

// S1-1 结构性: 死分支表达式不得再出现 (有人把它加回来 ⇒ 变红)
function assertNoDeadBranch() {
  const m = fnBody.match(/charTruncated\s*\?\s*1\s*:\s*0/g)
  if (m) throw new Error(`splitRunTitle 内仍存在死分支 charTruncated?1:0 (${m.length} 处) —— 方案 (A) 要求删除`)
}

const ctx = vm.createContext({ console })
vm.runInContext(fnSrc + '\nglobalThis.__e={splitRunTitle,RUN_TITLE_PREVIEW_LINES,RUN_TITLE_PREVIEW_CHARS}', ctx)
const { splitRunTitle } = ctx.__e

// 参考实现: 与源码同逻辑, 但隐藏行数写成方案 (A) 形态。
// 刻意不复用源码常量 —— 改源码不会"顺手"改到这份参考实现。
const RUN_TITLE_PREVIEW_LINES_REF = 2, RUN_TITLE_PREVIEW_CHARS_REF = 120
function ref(raw) {
  const text = (raw == null ? '' : String(raw)).trim()
  if (!text) return { preview: '', full: '', truncated: false, hiddenLineCount: 0 }
  const allLines = text.split('\n')
  const previewLines = []
  let usedLines = 0, charTruncated = false
  for (let i = 0; i < allLines.length; i++) {
    const ln = allLines[i]
    if (previewLines.length >= RUN_TITLE_PREVIEW_LINES_REF) break
    if (ln.length > RUN_TITLE_PREVIEW_CHARS_REF) {
      previewLines.push(ln.slice(0, RUN_TITLE_PREVIEW_CHARS_REF).trimEnd() + '\u2026')
      charTruncated = true
      break
    }
    previewLines.push(ln); usedLines = i + 1
  }
  const preview = previewLines.join('\n').trimEnd()
  const more = allLines.slice(usedLines).join('\n').trimStart()
  const hiddenLineCount = more ? allLines.length - usedLines : 0   // 方案 (A) 语义
  const truncated = more.length > 0 || charTruncated
  return { preview, full: text, truncated, hiddenLineCount }
}

// 覆盖输入集: 空值/空白/单行 119·120·121·348 字/2·3·5 行/超长行在首·中·末行/
// 含空白行/tab/尾部空格 —— 逐值比对源码实现与参考实现。
const INPUTS = [null, undefined, '', '   ', '\t', '\n\n', 'a', 'ab',
  'x'.repeat(119), 'x'.repeat(120), 'x'.repeat(121), 'x'.repeat(348), '按'.repeat(348),
  'a\nb', 'a\nb\nc', 'a\nb\nc\nd', 'a\nb\nc\nd\ne',
  'a\n\nb', 'a\n  \nb', 'a \nb ', '  a  ', 'a\tb', ' a', 'a ',
  'ok\n' + 'y'.repeat(200), 'y'.repeat(200) + '\nok',
  'ok1\nok2\n' + 'y'.repeat(400), 'y'.repeat(400) + '\nok1\nok2',
  'y'.repeat(400) + '\n' + 'y'.repeat(400),
  '\n\n' + 'z'.repeat(200), '  \n  \n' + 'z'.repeat(200)]

let failed = 0
function test(n, f) { try { f(); console.log(`ok - ${n}`) } catch (e) { failed++; console.error(`FAIL - ${n}\n  ${e.message}`) } }
function assert(c, m) { if (!c) throw new Error(m) }
function eq(a, b, m) { const sa = JSON.stringify(a), sb = JSON.stringify(b); if (sa !== sb) throw new Error(`${m}: ${sa} !== ${sb}`) }

test('S1-1 源码无 charTruncated?1:0 死分支', () => { assertNoDeadBranch() })
test(`S1-2 hiddenLineCount 与参考实现在 ${INPUTS.length} 组输入上逐值一致`, () => {
  assert(INPUTS.length >= 25, `输入集过小: ${INPUTS.length}`)
  for (const raw of INPUTS) eq(splitRunTitle(raw), ref(raw), `输入 ${JSON.stringify(String(raw).slice(0, 30))}`)
})
test('S1-3 隐藏行数语义锚: 多行隐藏数 = 总行数-预览行数', () => {
  eq(splitRunTitle('第一行\n第二行\n第三行\n第四行\n第五行').hiddenLineCount, 3, '5 行应隐藏 3')
  eq(splitRunTitle('a\nb').hiddenLineCount, 0, '2 行应隐藏 0')
})
if (failed) { console.error(`\n${failed}/3 失败`); process.exit(1) }
console.log('\n全部通过 (3)')
