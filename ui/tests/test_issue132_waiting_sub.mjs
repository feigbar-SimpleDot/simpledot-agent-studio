// issue #132 前端回归测试 (s3 / 卡 t_13296c02)
//
// 依据: docs/PLAN-G-20261028.md §1.2 (过程文档, 位于任务 workspace docs/,
//       不入仓 —— 本文件注释自含覆盖点)。
//
// 覆盖 (AC-132-8):
//   T1  TasksBoardView classify(): WAITING_SUB → sem='tboard.state_waiting_sub'
//       且 group='running' 且 badge='badge-blue' (忙碌族蓝, 非琥珀);
//       WAITING 仍 → 'tboard.state_waiting' (防串); WORKING 分支不受影响。
//   T2  WorkersView: workerStatus({derived_state:'WAITING_SUB', online:true})
//       === 'subagent' (不落 idle/offline); STATUS_META.subagent 的 i18nKey /
//       cls / gfx 逐项断言。
//   T3  locale JSON: zh/en 均含 tboard.state_waiting_sub 与
//       workers.status_waiting_sub; zh 值逐字「子任务处理中」, en 值 'Subagent'。
//   T4  头部当前卡三元链: WAITING_SUB 分支存在且位于 WORKING 兜底分支之前
//       (源码级锚, 防 tasks 摘要 running 兜底抢先渲染「执行中」)。
//
// 变异反证锚 (MV): MV-132-A 删 classify 的 WAITING_SUB 分支 → T1 红;
//                  MV-132-B 删 DERIVED_TO_VISUAL 的 WAITING_SUB 映射 → T2 红。
//
// 风格: 沿用仓库既有 ui/tests/*.mjs —— 源码抽取 + vm 沙箱, 不起浏览器。
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import vm from 'node:vm'

const here = path.dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(path.join(here, f), 'utf8')
const boardSrc = read('../src/views/TasksBoardView.jsx')
const workersSrc = read('../src/views/WorkersView.jsx')
const zh = JSON.parse(read('../src/i18n/locales/zh-cn.json'))
const en = JSON.parse(read('../src/i18n/locales/en.json'))

let failed = 0
function test(n, f) { try { f(); console.log(`ok - ${n}`) } catch (e) { failed++; console.error(`FAIL - ${n}\n  ${e.message}`) } }
function assert(c, m) { if (!c) throw new Error(m) }
function eq(a, b, m) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b)
  if (sa !== sb) throw new Error(`${m || ''} 期望 ${sb}, 实得 ${sa}`)
}

// ── 源码抽取工具 (与 test_cron_view.mjs 同款: 函数按名定位 + 大括号配平) ──
function extractFn(src, name) {
  const idx = src.indexOf(`function ${name}(`)
  if (idx < 0) throw new Error(`源码抽取失败: 找不到 ${name}`)
  let braces = 0, seen = false
  for (let i = idx; i < src.length; i++) {
    const ch = src[i]
    if (ch === '{') { braces++; seen = true } else if (ch === '}') braces--
    if (seen && braces === 0) return src.slice(idx, i + 1)
  }
  throw new Error(`源码抽取失败: ${name} 大括号未配平`)
}
function extractConst(src, name) {
  const idx = src.indexOf(`const ${name} = {`)
  if (idx < 0) throw new Error(`源码抽取失败: 找不到常量 ${name}`)
  let braces = 0, seen = false
  for (let i = src.indexOf('{', idx); i < src.length; i++) {
    const ch = src[i]
    if (ch === '{') { braces++; seen = true } else if (ch === '}') braces--
    if (seen && braces === 0) return src.slice(idx, i + 1)
  }
  throw new Error(`源码抽取失败: 常量 ${name} 大括号未配平`)
}

// TasksBoardView: classify 进沙箱 (t 由测试注入 key→key 假翻译)
const boardCtx = vm.createContext({ console })
vm.runInContext(extractFn(boardSrc, 'classify') + '\nglobalThis.__classify = classify', boardCtx)
const classify = boardCtx.__classify
const tIdent = (k) => k   // 假 t: 原样返回 key, 断言键名而非译文

// WorkersView: 两常量 + workerStatus 进沙箱
const workersCtx = vm.createContext({ console })
vm.runInContext(
  [extractConst(workersSrc, 'DERIVED_TO_VISUAL'),
   extractConst(workersSrc, 'STATUS_META'),
   extractFn(workersSrc, 'workerStatus'),
   'globalThis.__e = { workerStatus, DERIVED_TO_VISUAL, STATUS_META }'].join('\n'),
  workersCtx)
const W = workersCtx.__e

// ══════════════════════════════════════════════════════════════════
// T1: classify 的 WAITING_SUB 分支 (MV-132-A 锚)
// ══════════════════════════════════════════════════════════════════
test('T1 classify WAITING_SUB → 忙碌族蓝 + 专属文案键', () => {
  const r = classify({ state: 'WAITING_SUB' }, tIdent)
  eq(r.sem, 'tboard.state_waiting_sub', 'sem 文案键')
  eq(r.group, 'running', 'group 归忙碌族')
  eq(r.badge, 'badge-blue', 'badge 不得用琥珀')
  eq(r.dataState, 'WAITING_SUB', 'dataState 透传')
})
test('T1 classify WAITING 仍 → 等介入 (防串)', () => {
  const r = classify({ state: 'WAITING' }, tIdent)
  eq(r.sem, 'tboard.state_waiting', 'WAITING 语义不动')
  eq(r.group, 'waiting', 'WAITING 组不动')
  eq(r.badge, 'badge-amber', 'WAITING 琥珀不动')
})
test('T1 classify WORKING 不受影响', () => {
  const r = classify({ state: 'WORKING' }, tIdent)
  eq(r.sem, 'tboard.state_running', 'WORKING 语义不动')
  eq(r.icon, '●', 'WORKING 图标与 WAITING_SUB 的 ⋯ 区分')
  eq(classify({ state: 'WAITING_SUB' }, tIdent).icon, '⋯', 'WAITING_SUB 图标')
})

// ══════════════════════════════════════════════════════════════════
// T2: WorkersView 可视态映射 (MV-132-B 锚)
// ══════════════════════════════════════════════════════════════════
test('T2 workerStatus WAITING_SUB → subagent (不落 idle/offline)', () => {
  eq(W.workerStatus({ derived_state: 'WAITING_SUB', online: true }, 'r1'), 'subagent', '可视态')
})
test('T2 STATUS_META.subagent 形状', () => {
  const m = W.STATUS_META.subagent
  assert(m, 'STATUS_META 缺 subagent 项')
  eq(m.i18nKey, 'workers.status_waiting_sub', 'i18nKey')
  eq(m.cls, 'badge-blue', '忙碌族蓝')
  eq(m.gfx, 'wcard-gfx-working', '复用 working 图形')
})
test('T2 既有映射不回归 (WORKING/HANDOFF/WAITING)', () => {
  eq(W.DERIVED_TO_VISUAL.WORKING, 'working')
  eq(W.DERIVED_TO_VISUAL.HANDOFF, 'working')
  eq(W.DERIVED_TO_VISUAL.WAITING, 'waiting')
})

// ══════════════════════════════════════════════════════════════════
// T3: i18n 双语键 (MV-132-C 的文案面锚)
// ══════════════════════════════════════════════════════════════════
test('T3 zh-cn 两键逐字「子任务处理中」', () => {
  eq(zh.tboard.state_waiting_sub, '子任务处理中', 'tboard.state_waiting_sub')
  eq(zh.workers.status_waiting_sub, '子任务处理中', 'workers.status_waiting_sub')
})
test('T3 en 两键 = Subagent', () => {
  eq(en.tboard.state_waiting_sub, 'Subagent', 'tboard.state_waiting_sub')
  eq(en.workers.status_waiting_sub, 'Subagent', 'workers.status_waiting_sub')
})
test('T3 既有「等介入」键语义不动 (需求面 6)', () => {
  eq(zh.tboard.state_waiting, '等介入')
  eq(zh.workers.status_waiting, '等介入')
})

// ══════════════════════════════════════════════════════════════════
// T4: 头部当前卡三元链源码锚 (WAITING_SUB 分支在 WORKING 兜底之前)
// ══════════════════════════════════════════════════════════════════
test('T4 头部三元链含 WAITING_SUB 分支且先于 WORKING 兜底', () => {
  const subIdx = boardSrc.indexOf("cardState === 'WAITING_SUB'")
  assert(subIdx > 0, '头部三元链缺 WAITING_SUB 分支')
  const runIdx = boardSrc.indexOf("active.current_card_status === 'running'")
  assert(subIdx < runIdx, 'WAITING_SUB 分支必须在 WORKING 兜底之前')
  assert(boardSrc.slice(subIdx, subIdx + 400).includes("t('tboard.state_waiting_sub')"),
    'WAITING_SUB 分支必须渲染 state_waiting_sub 文案')
})

if (failed) { console.error(`\n${failed} 个断言失败`); process.exit(1) }
console.log('all tests passed')
