// issue #213/#214 守护套件 — 收件箱详情折叠展开 + hold 分组/徽标/看板 HOLD
// (V批 v4 / 依据 docs/UX-V-20261104.md §5 T1-T5 + docs/PLAN-V-20261104.md §7)
//
// 覆盖:
//   T1  schedGroups 含 t.status === 'hold' 显式分支且初始 g 含 hold 键
//       (防 else 兜底回归 —— hold 不落 done)。
//   T2  STATE_BADGE 含 hold: 映射 (防 schedStateKey('hold') 落 unknown /
//       文案 Unknown 降级回归)。
//   T3  renderTaskRow 含详情展开渲染 (task.detail + openDetail 门控) 且
//       操作按钮容器含 stopPropagation (防误触锚, AC-V-1 判据 2)。
//   T4  UX-V §4 五键双语在位 + zh/en 全量键路径集合 diff 为空 (沿 #208 T4 同法)。
//   T5  TasksBoardView runStatus 含 'HOLD' 分支 + tboard.run_status_hold 双语键。
//
// 变异反证锚 (MV): 删 hold 分支→T1 红; 删 STATE_BADGE.hold→T2 红;
//                  删 stopPropagation→T3 红; 任一语种删键→T4 红;
//                  删 runStatus HOLD 分支→T5 红。
//
// 风格: 沿用仓库既有 ui/tests/*.mjs — 源码抽取 + JSON 断言, 不起浏览器。
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(path.join(here, f), 'utf8')
const sched = read('../src/views/ScheduleView.jsx')
const board = read('../src/views/TasksBoardView.jsx')
const zh = JSON.parse(read('../src/i18n/locales/zh-cn.json'))
const en = JSON.parse(read('../src/i18n/locales/en.json'))

let failed = 0, passed = 0
function test(n, f) { try { f(); passed++; console.log(`ok - ${n}`) } catch (e) { failed++; console.error(`FAIL - ${n}\n  ${e.message}`) } }
function assert(c, m) { if (!c) throw new Error(m) }

// 取 schedGroups 函数体
const groupsFn = sched.match(/export function schedGroups[\s\S]*?\n\}/)?.[0] || ''
// 取 renderTaskRow 函数体 (task 行专用)
const row = sched.match(/const renderTaskRow[\s\S]*?\n  \}/)?.[0] || ''
// 取 STATE_BADGE 对象
const badge = sched.match(/const STATE_BADGE = \{[\s\S]*?\n\}/)?.[0] || ''
// 取 runStatus 函数体
const runStatusFn = board.match(/function runStatus\([\s\S]*?\n\}/)?.[0] || ''

// ── T1: schedGroups hold 显式分支 ────────────────────────────────────
test('T1 schedGroups 含 hold 显式分支 (防 else 兜底进 done)', () => {
  assert(groupsFn.length > 0, 'schedGroups 函数未找到')
  assert(/t\.status === 'hold'/.test(groupsFn), "schedGroups 缺 t.status === 'hold' 显式分支")
  assert(/g\.hold\.push/.test(groupsFn), 'hold 分支应 push 进 g.hold')
})
test('T1 schedGroups 初始 g 含 hold 键', () => {
  assert(/const g = \{[^}]*hold: \[\]/.test(groupsFn), '初始 g 应含 hold: [] 键')
})
test('T1 hold 组渲染块在 pending 组之前 (置顶于未执行组正上方)', () => {
  const iHold = sched.indexOf("sectionTitle('schedule.group.hold'")
  const iPending = sched.indexOf("sectionTitle('schedule.group.pending'")
  assert(iHold > 0 && iPending > 0 && iHold < iPending,
    `hold 组应在 pending 组之前渲染 (hold=${iHold}, pending=${iPending})`)
})

// ── T2: STATE_BADGE hold 映射 ────────────────────────────────────────
test('T2 STATE_BADGE 含 hold 映射 (防 Unknown 降级回归)', () => {
  assert(badge.length > 0, 'STATE_BADGE 未找到')
  assert(/hold:\s*'badge-yellow'/.test(badge), "STATE_BADGE 应含 hold: 'badge-yellow' (main 裁决 R-5)")
})

// ── T3: 详情折叠展开 + 防误触锚 ──────────────────────────────────────
test('T3 renderTaskRow 含 openDetail 门控 + task.detail 渲染', () => {
  assert(row.length > 0, 'renderTaskRow 未找到')
  assert(/openDetail\.has\(task\.id\)/.test(row), '缺 openDetail.has(task.id) 展开门控')
  assert(/task\.detail/.test(row), '展开区未渲染 task.detail')
  assert(/schedule\.detail\.label/.test(row), '展开区缺 schedule.detail.label 键')
  assert(/schedule\.detail\.empty/.test(row), '空 detail 缺 schedule.detail.empty 空态键')
  assert(/whitespace-pre-wrap/.test(row), 'detail 全文应 whitespace-pre-wrap 保留换行')
})
test('T3 操作按钮容器含 stopPropagation (防误触锚)', () => {
  const btns = row.match(/<div className="flex items-center gap-1\.5 shrink-0"[^\n]*/)?.[0] || ''
  assert(btns.length > 0, '操作按钮容器未找到')
  assert(/stopPropagation/.test(btns), '操作按钮容器缺 onClick stopPropagation (点按钮不得 toggle 展开)')
})
test('T3 内容区可点击 + a11y (role/tabIndex/aria-expanded)', () => {
  assert(/role="button"/.test(row) && /tabIndex=\{0\}/.test(row) && /aria-expanded=\{open\}/.test(row),
    '内容区应有 role="button" + tabIndex={0} + aria-expanded')
})

// ── T4: i18n 双语键 + 键集 diff ──────────────────────────────────────
for (const [loc, dict] of [['zh-cn', zh], ['en', en]]) {
  test(`T4 ${loc} UX-V §4 五键在位`, () => {
    assert(typeof dict.schedule?.detail?.label === 'string' && dict.schedule.detail.label.length > 0,
      '缺键 schedule.detail.label')
    assert(typeof dict.schedule?.detail?.empty === 'string' && dict.schedule.detail.empty.length > 0,
      '缺键 schedule.detail.empty')
    assert(typeof dict.schedule?.state?.hold === 'string' && dict.schedule.state.hold.length > 0,
      '缺键 schedule.state.hold')
    assert(typeof dict.schedule?.group?.hold === 'string' && dict.schedule.group.hold.length > 0,
      '缺键 schedule.group.hold')
    assert(typeof dict.tboard?.run_status_hold === 'string' && dict.tboard.run_status_hold.length > 0,
      '缺键 tboard.run_status_hold')
  })
}
test('T4 zh 文案符合 UX-V §4 定稿', () => {
  assert(zh.schedule.detail.label === '任务详情', `zh detail.label 应「任务详情」, 实得 ${zh.schedule.detail.label}`)
  assert(zh.schedule.detail.empty === '该任务没有详情内容', `zh detail.empty 应「该任务没有详情内容」, 实得 ${zh.schedule.detail.empty}`)
  assert(zh.schedule.state.hold === '挂起', `zh state.hold 应「挂起」, 实得 ${zh.schedule.state.hold}`)
  assert(zh.schedule.group.hold === '已挂起', `zh group.hold 应「已挂起」, 实得 ${zh.schedule.group.hold}`)
  assert(zh.tboard.run_status_hold === '已挂起', `zh run_status_hold 应「已挂起」, 实得 ${zh.tboard.run_status_hold}`)
})
test('T4 en 文案符合 UX-V §4 定稿', () => {
  assert(en.schedule.detail.label === 'Task detail', `en detail.label 应 "Task detail", 实得 ${en.schedule.detail.label}`)
  assert(en.schedule.detail.empty === 'This task has no detail', `en detail.empty 应 "This task has no detail", 实得 ${en.schedule.detail.empty}`)
  assert(en.schedule.state.hold === 'On hold', `en state.hold 应 "On hold", 实得 ${en.schedule.state.hold}`)
  assert(en.schedule.group.hold === 'On hold', `en group.hold 应 "On hold", 实得 ${en.schedule.group.hold}`)
  assert(en.tboard.run_status_hold === 'On hold', `en run_status_hold 应 "On hold", 实得 ${en.tboard.run_status_hold}`)
})
test('T4 zh/en 全量键路径集合 diff 为空', () => {
  const keys = (o, pre = '') => {
    const s = new Set()
    for (const [k, v] of Object.entries(o)) {
      s.add(pre + k)
      if (v && typeof v === 'object') for (const kk of keys(v, pre + k + '.')) s.add(kk)
    }
    return s
  }
  const a = keys(zh), b = keys(en)
  const onlyZh = [...a].filter((k) => !b.has(k))
  const onlyEn = [...b].filter((k) => !a.has(k))
  assert(onlyZh.length === 0 && onlyEn.length === 0,
    `键集不对称 zh-only=${JSON.stringify(onlyZh)} en-only=${JSON.stringify(onlyEn)}`)
})

// ── T5: 看板 runStatus HOLD 分支 (OQ-V-3) ───────────────────────────
test('T5 runStatus 含 HOLD 分支 (badge-yellow, 无 pulse)', () => {
  assert(runStatusFn.length > 0, 'runStatus 函数未找到')
  const holdLine = runStatusFn.match(/if \(s === 'HOLD'\) return \{[^\n]*\}/)?.[0] || ''
  assert(holdLine.length > 0, "runStatus 缺 s === 'HOLD' 分支")
  assert(/tboard\.run_status_hold/.test(holdLine), 'HOLD 分支应使用 tboard.run_status_hold 键')
  assert(/badge-yellow/.test(holdLine), 'HOLD 分支应为 badge-yellow')
  assert(!/pulse/.test(holdLine), 'HOLD 冻结态不应有 pulse')
})

console.log(failed ? `\n${failed} FAILURES` : `\nALL PASS (${passed} tests)`)
process.exit(failed ? 1 : 0)
