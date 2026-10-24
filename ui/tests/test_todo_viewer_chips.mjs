#!/usr/bin/env node
// 单测: TasksBoardView.jsx 的 todo viewer 辅助纯函数 (issue #14, 2026-10-17)。
//
// 被测函数住在 .jsx 组件文件里无法直接 import, 沿用「源码抽取直测」路线
// (与 test_toolgroup/test_sse_parse 一致): 从 TasksBoardView.jsx 抽出
//   splitGoalSegments(goal)  — 目标格式化 + 折叠阈值
//   parseDoneResult(text)    — 完成区结果分段 chip 化
// 两个函数体, 注入 node:vm 沙箱执行。抽取即钉住源码现状 —— 函数被改动而
// 本测试未同步时, 抽取失败/用例失败即报警。

import { readFileSync } from 'fs'
import vm from 'node:vm'

const SRC = new URL('../src/views/TasksBoardView.jsx', import.meta.url)
const src = readFileSync(SRC, 'utf8')

// 从 startMarker 起做花括号+圆括号联合配平, 返回完整函数/箭头函数文本。
function extractFn(source, startMarker) {
  const start = source.indexOf(startMarker)
  if (start < 0) throw new Error(`源码抽取失败, 未找到标记: ${startMarker}`)
  let braces = 0, parens = 0, seenBrace = false
  for (let i = start; i < source.length; i++) {
    const ch = source[i]
    if (ch === '{') { braces++; seenBrace = true }
    else if (ch === '}') braces--
    else if (ch === '(') parens++
    else if (ch === ')') parens--
    if (seenBrace && braces === 0 && parens === 0) return source.slice(start, i + 1)
  }
  throw new Error(`括号不配平: ${startMarker}`)
}

// splitGoalSegments / parseDoneResult 内部都用 const, 但抽到 vm 时同
// 文件的顶层 const 会被 vm.runInContext 当成全局可见, 这里需要把模块
// 顶部那段也一并喂入, 让 const 进入沙箱上下文对象可见。先抽到顶层:
//  - 函数体: splitGoalSegments
//  - 函数体: parseDoneResult
//  - 函数体: GOAL_PREVIEW_LINES / GOAL_PREVIEW_CHARS / GOAL_FULL_CHARS (顶层 const)
//  - _ASSIGNEE_HINT_RE / _CARD_HINT_RE / _COMMITS_HINT_RE (顶层 const)
//
// 简便做法: 直接从头截到 parseDoneResult 末尾 (因为这些 const 都集中
// 在 issue #14 新增的纯函数块里), 把这段模块顶层代码送入 vm。
const startIdx = src.indexOf("const GOAL_PREVIEW_LINES")
if (startIdx < 0) throw new Error("源码抽取失败: 未找到 const GOAL_PREVIEW_LINES 块")
// 找到最后一个 `function _classifySegment` 的结束位置 (它定义在 parseDoneResult 之后)
// 包含整块: const + 函数定义 + 函数 _classifySegment
const endMarker = "function _classifySegment"
const endIdx = src.indexOf(endMarker, startIdx)
if (endIdx < 0) throw new Error("源码抽取失败: 未找到 _classifySegment 块")
// 配平到 _classifySegment 函数体结束
const blockEnd = (() => {
  let braces = 0, parens = 0, seenBrace = false
  for (let i = endIdx; i < src.length; i++) {
    const ch = src[i]
    if (ch === '{') { braces++; seenBrace = true }
    else if (ch === '}') braces--
    else if (ch === '(') parens++
    else if (ch === ')') parens--
    if (seenBrace && braces === 0 && parens === 0) return i + 1
  }
  throw new Error("_classifySegment 括号不配平")
})()

const blockSrc = src.slice(startIdx, blockEnd)
const ctx = vm.createContext({ console })
vm.runInContext(
  blockSrc +
    '\nglobalThis.__exports = { splitGoalSegments, parseDoneResult,' +
    ' GOAL_PREVIEW_LINES, GOAL_PREVIEW_CHARS, GOAL_FULL_CHARS };',
  ctx,
)
const { splitGoalSegments, parseDoneResult,
        GOAL_PREVIEW_LINES, GOAL_PREVIEW_CHARS, GOAL_FULL_CHARS } = ctx.__exports

const tests = []
function test(name, fn) { tests.push({ name, fn }) }
function assert(cond, msg) { if (!cond) throw new Error(msg) }
function eq(a, b, msg) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b)
  if (sa !== sb) throw new Error(`${msg || 'eq'}: ${sa} !== ${sb}`)
}

// ───────────────────────── splitGoalSegments ─────────────────────────

test('splitGoalSegments: 空/undefined 走 placeholder 形态', () => {
  eq(splitGoalSegments(''), { preview: '', full: '', truncated: false, hiddenLineCount: 0 })
  eq(splitGoalSegments(null), { preview: '', full: '', truncated: false, hiddenLineCount: 0 })
  eq(splitGoalSegments(undefined), { preview: '', full: '', truncated: false, hiddenLineCount: 0 })
  eq(splitGoalSegments('   \n  \n'), { preview: '', full: '', truncated: false, hiddenLineCount: 0 })
})

test('splitGoalSegments: 单行短目标 → 直接展示, 不折叠', () => {
  const r = splitGoalSegments('修复 issue #11 排序问题, 改 add_cards 与 api_team_work_get')
  assert(r.full === '修复 issue #11 排序问题, 改 add_cards 与 api_team_work_get', 'full 错误')
  assert(r.preview === r.full, '短目标 preview 应等于 full')
  assert(r.truncated === false, '短目标不应 truncated')
  assert(r.hiddenLineCount === 0, '短目标隐藏行数应为 0')
})

test('splitGoalSegments: 多段(每段短)折叠 (issue #14 主诉求)', () => {
  // 1 段 = 第 1 行 (10字), 第 2 行 = 第 2 段 (20字) — GOAL_PREVIEW_LINES=2 全部纳入预览,
  // 第 3 行 = 空行后开始第 3 段 → 触发 reachedBlank = true, 跳出循环
  const text = '设计 plan_seq 字段\n让 add_cards 可传 position\n\n后续段隐藏在 details 里'
  const r = splitGoalSegments(text)
  assert(r.preview === '设计 plan_seq 字段\n让 add_cards 可传 position', `preview 错误: ${JSON.stringify(r.preview)}`)
  assert(r.full === text, 'full 应保留原文')
  assert(r.truncated === true, '多段应 truncated')
  assert(r.hiddenLineCount >= 2, `应至少隐藏 2 行 (后续段+空行), 实际 ${r.hiddenLineCount}`)
})

test('splitGoalSegments: 单段超长字符级截断 + truncated=true', () => {
  const longLine = 'x'.repeat(200)  // > GOAL_PREVIEW_CHARS=140
  const r = splitGoalSegments(longLine)
  assert(r.preview.endsWith('…'), '超长首段末尾应 …')
  assert(r.preview.length <= GOAL_PREVIEW_CHARS + 5, // 截断长度加 ellipsis
    `截断长度超阈: ${r.preview.length}`)
  assert(r.truncated === true, '单段超长应 truncated=true')
})

test('splitGoalSegments: [#88] 单段超长字符级截断 ⇒ hiddenLineCount===1 (三元 else 侧可达, issue 正文"同形死三元"表述有误)', () => {
  // 可达充要条件 (PLAN-88-89 §1.3): 被字符级截断的行是 allLines 的最后一行
  // 且 index <= 1。'x'.repeat(N) 单行与 'a\n'+'x'.repeat(N) 两行都满足。
  for (const len of [141, 142, 200, 400]) {
    const r = splitGoalSegments('x'.repeat(len))
    assert(r.truncated === true, `长度 ${len} 应 truncated`)
    assert(r.hiddenLineCount === 1, `长度 ${len} 隐藏行数应为 1 (实际 ${r.hiddenLineCount})`)
  }
  const r2 = splitGoalSegments('a\n' + 'x'.repeat(200))
  assert(r2.hiddenLineCount === 1, `第二行超长隐藏行数应为 1 (实际 ${r2.hiddenLineCount})`)
})

test('splitGoalSegments: [#88] 字符级截断边界对: len<=140 隐藏 0 行, len>=141 隐藏 1 行', () => {
  assert(splitGoalSegments('x'.repeat(139)).hiddenLineCount === 0, '139 应隐藏 0 行')
  assert(splitGoalSegments('x'.repeat(140)).hiddenLineCount === 0, '140 应隐藏 0 行')
  assert(splitGoalSegments('x'.repeat(141)).hiddenLineCount === 1, '141 应隐藏 1 行')
})

test('splitGoalSegments: [#88] GoalCell 折叠不变量: truncated=true ⇒ hiddenLineCount>=1', () => {
  // GoalCell (折叠判据 `!seg.truncated || seg.hiddenLineCount <= 0`, 本批 A-1 加注释后位于 :893)
  // 判据: !seg.truncated || seg.hiddenLineCount <= 0 ⇒ 渲染全文、无抽屉。
  // 故 hiddenLineCount 从 1 降到 0 不是"徽章少 1", 而是 <details> 整个消失。
  const seg = splitGoalSegments('x'.repeat(200))
  assert(seg.truncated === true, '超长单行应 truncated')
  assert(seg.hiddenLineCount >= 1, `truncated 时隐藏行数必须 >=1 (实际 ${seg.hiddenLineCount})`)
})

test('splitGoalSegments: [#88 结构锚] 源码仍保留 anyCharTruncated ? 1 : 0', () => {
  const s = readFileSync(new URL('../src/views/TasksBoardView.jsx', import.meta.url), 'utf8')
  const i = s.indexOf('const GOAL_PREVIEW_LINES')
  const j = s.indexOf('function parseDoneResult', i)
  const block = s.slice(i, j)
  const m = block.match(/anyCharTruncated\s*\?\s*1\s*:\s*0/g)
  assert(m && m.length === 1,
    `期望恰 1 处 anyCharTruncated?1:0, 实际 ${m ? m.length : 0} —— 按 issue #88 删成 ': 0' 会使本断言转红`)
})

test('splitGoalSegments: 尾部连续空行/制表清洗 (UI 渲染友好)', () => {
  // main 历史曾因"目标后紧跟换行首行为空"导致 UI 看不到; splitGoalSegments
  // 应把多余空行/制表/回车洗掉, 但保留单 \n 段间空行。
  const text = '修复排队预览顺序\n\n\n\n\t  '
  const r = splitGoalSegments(text)
  assert(!r.full.endsWith(' '), 'full 不应以空白结尾')
  assert(!r.full.includes('\n\n\n'), 'full 不应含连续 3+ 换行')
  assert(r.truncated === false, '短文本 (原文 << GOAL_FULL_CHARS=600) 不应 truncated')
  assert(r.hiddenLineCount === 0, '短文本无隐藏行')
})

test('splitGoalSegments: 全文超过 GOAL_FULL_CHARS 即便行数少也折叠', () => {
  // 制造一个 3 行但每行都很长的目标, 篇幅总和 > GOAL_FULL_CHARS
  const longLine = 'y'.repeat(220)
  const r = splitGoalSegments(`${longLine}\n${longLine}\n${longLine}`)
  assert(r.truncated === true, '总长超阈值应 truncated')
})

test('splitGoalSegments: 三行短文本保留换行 + 折叠 (预览=前2行)', () => {
  // GOAL_PREVIEW_LINES=2, 第 3 行走 truncated, hiddenLineCount=1
  const r = splitGoalSegments('一行\n二行\n三行')
  assert(r.full === '一行\n二行\n三行', 'full 应保留换行')
  assert(r.preview === '一行\n二行', `预览应为前 2 行, 实际 ${JSON.stringify(r.preview)}`)
  assert(r.truncated === true, '三行 (短) 第二行后还有第 3 行 → truncated')
  assert(r.hiddenLineCount === 1, `隐藏 1 行, 实际 ${r.hiddenLineCount}`)
})

test('splitGoalSegments: 隐藏行数计算 (issue #14 折叠徽章文案)', () => {
  // 5 行内容 (每行 5 字), 预览 2 行 → 应至少隐藏 3 行
  const text = 'aaaaa\nbbbbb\nccccc\nddddd\neeeee'
  const r = splitGoalSegments(text)
  assert(r.truncated === true, '5 行 > 预览 2 行 → truncated')
  assert(r.hiddenLineCount >= 3, `应至少隐藏 3 行, 实际 ${r.hiddenLineCount}`)
})

// ───────────────────────── parseDoneResult ─────────────────────────

test('parseDoneResult: 空/全空白 → 空数组', () => {
  eq(parseDoneResult(''), [])
  eq(parseDoneResult(null), [])
  eq(parseDoneResult(undefined), [])
  eq(parseDoneResult('   \t'), [])
})

test('parseDoneResult: 整段无 | → 整体作为单 note (不强制切)', () => {
  eq(parseDoneResult('done by coder'), [{ kind: 'note', value: 'done by coder' }])
  eq(parseDoneResult('一行结果, 没有分隔符'), [{ kind: 'note', value: '一行结果, 没有分隔符' }])
})

test('parseDoneResult: 标准 3 段全识别 (issue #14 样例串)', () => {
  // 用户原话: `coder | 卡 t_9103c49f | commits 97949af+5xxxx44`
  const r = parseDoneResult('coder | 卡 t_9103c49f | commits 97949af+5xxxx44')
  eq(r, [
    { kind: 'assignee', value: 'coder' },
    { kind: 'card',     value: 't_9103c49f' },
    { kind: 'commits',  value: '97949af+5xxxx44' },
  ])
})

test('parseDoneResult: 英文 anchor (en side 部署)', () => {
  const r = parseDoneResult('coder | card t_9103c49f | commits 97949af')
  eq(r, [
    { kind: 'assignee', value: 'coder' },
    { kind: 'card',     value: 't_9103c49f' },
    { kind: 'commits',  value: '97949af' },
  ])
})

test('parseDoneResult: 带 @ 前缀的承接方', () => {
  const r = parseDoneResult('@system_engineer | 卡 t_abc123')
  eq(r, [
    { kind: 'assignee', value: 'system_engineer' },
    { kind: 'card',     value: 't_abc123' },
  ])
})

test('parseDoneResult: 不可识别段全部降级为 note', () => {
  const r = parseDoneResult('唱歌 | 跳舞 | 打鼓')
  eq(r, [
    { kind: 'note', value: '唱歌' },
    { kind: 'note', value: '跳舞' },
    { kind: 'note', value: '打鼓' },
  ])
})

test('parseDoneResult: 混合 — 已识别 + 不可识别', () => {
  const r = parseDoneResult('coder | 已上线 | commits abc')
  eq(r, [
    { kind: 'assignee', value: 'coder' },
    { kind: 'note',     value: '已上线' },
    { kind: 'commits',  value: 'abc' },
  ])
})

test('parseDoneResult: 空段(全 | | 紧贴)跳过', () => {
  const r = parseDoneResult('coder || 卡 t_x | | commits y')
  eq(r, [
    { kind: 'assignee', value: 'coder' },
    { kind: 'card',     value: 't_x' },
    { kind: 'commits',  value: 'y' },
  ])
})

test('parseDoneResult: 全空段 → 兜底单 note (极端)', () => {
  // 切后全空, fallback 经过 _classifySegment; trimmed='|  |' 不匹配任何 anchor/bare 模式 → note
  eq(parseDoneResult('  |  |  '), [{ kind: 'note', value: '|  |' }])
})

test('parseDoneResult: 单段全部 | → 1 个 note (兜底)', () => {
  // trimmed = '|' 不匹配任何模式 → note chip
  eq(parseDoneResult('|'), [{ kind: 'note', value: '|' }])
})

test('parseDoneResult: 单段无 | 仍走 chip 识别器 (kanban 别名)', () => {
  // 真实形态: 12 位 hex kanban id 单段 → card chip
  eq(parseDoneResult('t_9103c49f'), [{ kind: 'card', value: 't_9103c49f' }])
  // 纯 7+ 位 hex 串 → commits chip (例外: 全数字/全字母不会误撞, worker 名规则)
  eq(parseDoneResult('abc1234'), [{ kind: 'commits', value: 'abc1234' }])
  // 无前缀的 worker 名 → assignee chip
  eq(parseDoneResult('coder'), [{ kind: 'assignee', value: 'coder' }])
  // 卡 id 即便有小写 x 也属 hex 字符, _BARE_KANBAN_RE 允许 a-f
})

test('parseDoneResult: 单段无 | 自由文本 → 单 note chip', () => {
  // 不匹配任何模式 → 单 note 段, 仍走 chip 渲染 (singleton)
  eq(parseDoneResult('唱歌跳舞'), [{ kind: 'note', value: '唱歌跳舞' }])
})

test('parseDoneResult: 顺序保留', () => {
  const r = parseDoneResult('commits abc | coder | 卡 t_z')
  eq(r, [
    { kind: 'commits',  value: 'abc' },
    { kind: 'assignee', value: 'coder' },
    { kind: 'card',     value: 't_z' },
  ])
})

test('parseDoneResult: kanban 兼容 (REOPEN-DESIGN D1 别名)', () => {
  const r = parseDoneResult('kanban t_xxxxx')
  eq(r, [{ kind: 'card', value: 't_xxxxx' }])
})

test('parseDoneResult: 大小写不敏感 (Commits, COMMITS)', () => {
  const r1 = parseDoneResult('Commits abc')
  eq(r1, [{ kind: 'commits', value: 'abc' }])
  const r2 = parseDoneResult('COMMITS abc')
  eq(r2, [{ kind: 'commits', value: 'abc' }])
})

test('parseDoneResult: 段值原样保真 (不二次加工)', () => {
  // 不应截断 hash、不应改大小写、不应去前缀 (除 anchor 标识)
  const r = parseDoneResult('commits 97949af5e1ce4e9a98bfdb43c8e0f5a47b0623f0')
  eq(r, [{ kind: 'commits', value: '97949af5e1ce4e9a98bfdb43c8e0f5a47b0623f0' }])
})

// ───────────────────────── runner ─────────────────────────
let pass = 0, fail = 0
for (const t of tests) {
  try {
    await t.fn()
    console.log(`  ✓ ${t.name}`)
    pass++
  } catch (e) {
    console.log(`  ✗ ${t.name}\n    ${e.message}`)
    fail++
  }
}
console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail === 0 ? 0 : 1)
