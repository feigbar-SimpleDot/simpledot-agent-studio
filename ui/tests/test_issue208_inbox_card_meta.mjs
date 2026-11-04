// issue #208 守护套件 — inbox 卡片 meta 行显示 workspace/workflow (S批 s3 / 卡 t_363ab3b2)
//
// 依据: docs/TRIAGE-S-20261102.md §2.2 + docs-source/issue-batch-20261103-s/
//       ISSUES-S.md #208。数据侧零缺口 (GET /api/schedule/tasks SELECT * 已含
//       workflow/workspace), 本卡纯前端展示增量: 卡片 meta 行追加两个轻量标识,
//       11.5px text-dim 同级小字, workspace 空显示「默认」占位, workflow=auto
//       弱化显示, 不与 prio/state badge 抢视觉层级。
//
// 覆盖:
//   T1  meta 行渲染断言: renderTaskRow 的 meta div 内出现 task.workspace 与
//       task.workflow 渲染表达式, 且位于 col_executed 之后、last_error 之前。
//   T2  视觉层级: 标识落在既有 meta div (text-[11.5px] text-dim) 内, 未新增
//       badge 类样式类; workflow=auto 有弱化分支 (opacity)。
//   T3  i18n: schedule.meta_workspace / meta_workspace_default / meta_workflow
//       双语键在位且值符合设计 (zh 占位「默认」)。
//   T4  i18n 两语种键集 diff 必须为空 (全量键路径集合逐字相等)。
//
// 变异反证锚 (MV): MV-208-A 删 meta 行 workspace/workflow 渲染 → T1 红;
//                  MV-208-B 删 auto 弱化分支 → T2 红; MV-208-C 任一语种删键 → T3/T4 红。
//
// 风格: 沿用仓库既有 ui/tests/*.mjs — 源码抽取 + JSON 断言, 不起浏览器。
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(path.join(here, f), 'utf8')
const sched = read('../src/views/ScheduleView.jsx')
const zh = JSON.parse(read('../src/i18n/locales/zh-cn.json'))
const en = JSON.parse(read('../src/i18n/locales/en.json'))

let failed = 0, passed = 0
function test(n, f) { try { f(); passed++; console.log(`ok - ${n}`) } catch (e) { failed++; console.error(`FAIL - ${n}\n  ${e.message}`) } }
function assert(c, m) { if (!c) throw new Error(m) }

// 取 renderTaskRow 的 meta div 段 (task 行专用, 不含 cron 行)
const row = sched.match(/const renderTaskRow[\s\S]*?\n  \}/)?.[0] || ''
const meta = row.match(/<div className="mt-1 text-\[11\.5px\] text-dim">[\s\S]*?<\/div>/)?.[0] || ''

// ── T1: meta 行渲染 workspace + workflow ─────────────────────────────
test('T1 meta 行渲染 task.workspace', () => {
  assert(meta.length > 0, 'renderTaskRow meta div 未找到')
  assert(/task\.workspace/.test(meta), 'meta 行未渲染 task.workspace')
  assert(/meta_workspace_default/.test(meta), 'workspace 空值未回退占位键 meta_workspace_default')
})
test('T1 meta 行渲染 task.workflow', () => {
  assert(/task\.workflow/.test(meta), 'meta 行未渲染 task.workflow')
  assert(/meta_workflow/.test(meta), 'meta 行未使用 meta_workflow 标签键')
})
test('T1 顺序: workspace/workflow 在 col_executed 之后、last_error 之前', () => {
  const iExec = meta.indexOf('col_executed')
  const iWs = meta.indexOf('task.workspace')
  const iWf = meta.indexOf('task.workflow')
  const iErr = meta.indexOf('last_error')
  assert(iExec >= 0 && iWs > iExec && iWf > iWs, `顺序应为 exec→workspace→workflow (exec=${iExec}, ws=${iWs}, wf=${iWf})`)
  assert(iErr === -1 || iErr > iWf, 'workspace/workflow 应排在 last_error 之前')
})

// ── T2: 视觉层级 ─────────────────────────────────────────────────────
test('T2 标识复用既有 meta div (11.5px text-dim), 未新起 badge 层级', () => {
  assert(/text-\[11\.5px\] text-dim/.test(meta), 'meta div 类名应保持 11.5px text-dim')
  assert(!/badge-/.test(meta), 'meta 行不应引入 badge-* 抢视觉层级')
})
test('T2 workflow=auto 弱化分支在位', () => {
  assert(/!== 'auto'/.test(meta) && /opacity-/.test(meta),
    '应有 workflow===auto 时弱化 (opacity) 的分支')
})

// ── T3: i18n 双语键 ──────────────────────────────────────────────────
for (const [loc, dict] of [['zh-cn', zh], ['en', en]]) {
  test(`T3 ${loc} schedule.meta_* 三键在位`, () => {
    for (const k of ['meta_workspace', 'meta_workspace_default', 'meta_workflow']) {
      assert(typeof dict.schedule?.[k] === 'string' && dict.schedule[k].length > 0,
        `缺键 schedule.${k}`)
    }
  })
}
test('T3 zh 占位文案 = 「默认」', () => {
  assert(zh.schedule.meta_workspace_default === '默认', `zh 占位应「默认」, 实得 ${zh.schedule.meta_workspace_default}`)
})

// ── T4: 两语种键集 diff 为空 ─────────────────────────────────────────
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

console.log(failed ? `\n${failed} FAILURES` : `\nALL PASS (${passed} tests)`)
process.exit(failed ? 1 : 0)
