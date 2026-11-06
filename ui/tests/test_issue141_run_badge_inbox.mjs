// issue #141 裁② + TAB 改名「收件箱/Inbox」前端回归测试 (O批 s3c / 卡 t_4f2aa8fc)
//
// 依据: docs/FIX-PLAN-O.md §TAB改名 (13 项用户可见清单) +
//       docs/ADJUDICATION-O.md §s3 实施口径 (#141 选项②, runExtraBadge 1 分支)。
//
// 覆盖:
//   T1  TAB 改名 13 项双语逐字锚 (nav.schedule / app.subtitle_schedule /
//       chat.queued_toast / schedule.busy_hint / schedule.empty.title+hint /
//       schedule.form.edit_title, zh 与 en 各自逐字)。
//   T2  mw/i18n schedule.err_task_not_found 双语逐字 (后端错误文案面)。
//   T3  runExtraBadge WAITING_SUB 分支源码级锚: 分支存在、判据逐字
//       (runState==='ACTIVE_BLOCKED' && active.holder_state==='WAITING_SUB')、
//       复用既有键 tboard.state_waiting_sub、badge-blue, 且必须插在
//       ACTIVE_BLOCKED 兜底分支之前 (否则被兜底抢先, 「承接者受阻」照旧)。
//   T4  改名只动值不动键: en/zh 键集仍逐字相等 (G1 同口径的 UI 侧轻量复检)。
//
// 变异反证锚 (MV): MV-141-A 删 runExtraBadge 的 WAITING_SUB 分支 → T3 红;
//                  MV-141-B 把该分支挪到 ACTIVE_BLOCKED 兜底之后 → T3 红;
//                  MV-141-C 任一改名值回退旧文案 → T1/T2 红。
//
// 风格: 沿用仓库既有 ui/tests/*.mjs —— 源码抽取 + JSON 断言, 不起浏览器。
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(path.join(here, f), 'utf8')
const boardSrc = read('../src/views/TasksBoardView.jsx')
const zh = JSON.parse(read('../src/i18n/locales/zh-cn.json'))
const en = JSON.parse(read('../src/i18n/locales/en.json'))
const mwZh = JSON.parse(read('../../mw/i18n/zh-cn.json'))
const mwEn = JSON.parse(read('../../mw/i18n/en.json'))

let failed = 0
function test(n, f) { try { f(); console.log(`ok - ${n}`) } catch (e) { failed++; console.error(`FAIL - ${n}\n  ${e.message}`) } }
function assert(c, m) { if (!c) throw new Error(m) }
function eq(a, b, m) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b)
  if (sa !== sb) throw new Error(`${m || ''} 期望 ${sb}, 实得 ${sa}`)
}

// ── 键路径取值工具 (UI locale 是嵌套结构) ──
function dig(obj, dotted) {
  return dotted.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj)
}

// ══════════════════════════════════════════════════════════════════
// T1: TAB 改名 13 项双语逐字 (FIX-PLAN-O §TAB改名 清单)
// ══════════════════════════════════════════════════════════════════
const RENAMED_ZH = {
  'nav.schedule': '收件箱',
  'app.subtitle_schedule': '收件箱 · 任务队列自动衔接执行',
  'chat.team_work_modal.queued_toast': '已加入收件箱队列',
  'schedule.busy_hint': '当前有任务执行中, 收件箱排队中',
  'schedule.empty.title': '收件箱还是空的',
  'schedule.empty.hint': '点击顶栏「添加任务」, 任务会加入收件箱队列',
  'schedule.form.edit_title': '修改收件箱任务',
}
const RENAMED_EN = {
  'nav.schedule': 'Inbox',
  'app.subtitle_schedule': 'Inbox · task queue, auto-chained',
  'chat.team_work_modal.queued_toast': 'Added to the inbox queue',
  'schedule.busy_hint': 'A task is running; inbox is queued',
  'schedule.empty.title': 'Inbox is empty',
  'schedule.empty.hint': 'Click "Add Task" in the top bar to join the inbox queue',
  'schedule.form.edit_title': 'Edit Inbox Task',
}
test('T1 zh 7 键改名值逐字 (MV-141-C)', () => {
  for (const [k, want] of Object.entries(RENAMED_ZH)) eq(dig(zh, k), want, `zh ${k}`)
})
test('T1 en 7 键改名值逐字 (MV-141-C)', () => {
  for (const [k, want] of Object.entries(RENAMED_EN)) eq(dig(en, k), want, `en ${k}`)
})
test('T1 旧文案在双语 locale 中零残留 (排期 TAB 措辞清干净)', () => {
  const zhRaw = read('../src/i18n/locales/zh-cn.json')
  const enRaw = read('../src/i18n/locales/en.json')
  for (const dead of ['"任务排期"', '已加入任务排期队列', '还没有排期任务', '修改排期任务', '排期等待中'])
    assert(!zhRaw.includes(dead), `zh 残留旧文案: ${dead}`)
  // 注: en 的 'No scheduled tasks yet' 在 cron.empty.title (CronView 定时任务面)
  // 是清单外既有文案 (zh 侧本就写「定时任务」), 不属 TAB 改名 13 项, 不查。
  for (const dead of ['"Task Schedule"', 'task schedule queue', 'Edit Scheduled Task', 'schedule is waiting'])
    assert(!enRaw.includes(dead), `en 残留旧文案: ${dead}`)
})

// ══════════════════════════════════════════════════════════════════
// T2: mw/i18n 后端错误文案双语逐字
// ══════════════════════════════════════════════════════════════════
test('T2 mw/i18n err_task_not_found 双语', () => {
  eq(mwZh['schedule.err_task_not_found'], '收件箱任务不存在', 'zh')
  eq(mwEn['schedule.err_task_not_found'], 'Inbox task not found', 'en')
})

// ══════════════════════════════════════════════════════════════════
// T3: runExtraBadge WAITING_SUB 分支 (MV-141-A/B 锚, 源码级)
// ══════════════════════════════════════════════════════════════════
test('T3 runExtraBadge 含 WAITING_SUB 分支且判据/样式/键逐字 (MV-141-A)', () => {
  const i = boardSrc.indexOf('const runExtraBadge')
  assert(i >= 0, '找不到 runExtraBadge')
  const body = boardSrc.slice(i, boardSrc.indexOf('})()', i))
  assert(
    body.includes("runState === 'ACTIVE_BLOCKED' && active.holder_state === 'WAITING_SUB'"),
    '缺 #141 WAITING_SUB 判据分支')
  const subLine = body.split('\n').find((l) => l.includes("holder_state === 'WAITING_SUB'"))
  const nextLine = body.split('\n')[body.split('\n').indexOf(subLine) + 1]
  assert(nextLine.includes("t('tboard.state_waiting_sub')"), '须复用既有键 state_waiting_sub (零新键)')
  assert(nextLine.includes('badge-blue'), '须 badge-blue (与卡级 chip 同色)')
})
test('T3 WAITING_SUB 分支在 ACTIVE_BLOCKED 兜底之前 (MV-141-B)', () => {
  const i = boardSrc.indexOf('const runExtraBadge')
  const body = boardSrc.slice(i, boardSrc.indexOf('})()', i))
  const subAt = body.indexOf("holder_state === 'WAITING_SUB'")
  const blockedAt = body.indexOf("if (runState === 'ACTIVE_BLOCKED') return")
  assert(subAt >= 0 && blockedAt >= 0, '分支缺失')
  assert(subAt < blockedAt, 'WAITING_SUB 分支必须插在 ACTIVE_BLOCKED 兜底之前')
})
test('T3 既有 run 态分支不回归 (HANDOFF/WAITING_ACTIVE/ACTIVE_BLOCKED/CLOSED)', () => {
  const i = boardSrc.indexOf('const runExtraBadge')
  const body = boardSrc.slice(i, boardSrc.indexOf('})()', i))
  for (const [state, key] of [['HANDOFF', 'state_handoff'], ['WAITING_ACTIVE', 'state_waiting'],
                              ['ACTIVE_BLOCKED', 'blocked_state_short'], ['CLOSED', 'closed_state_short']]) {
    assert(body.includes(`runState === '${state}'`), `缺 ${state} 分支`)
    assert(body.includes(`t('tboard.${key}')`), `${state} 键被换: ${key}`)
  }
})

// ══════════════════════════════════════════════════════════════════
// T4: 改名只动值不动键 (键集是协议面, G1 同口径 UI 侧复检)
// ══════════════════════════════════════════════════════════════════
function flattenKeys(obj, prefix = '', out = []) {
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === 'object') flattenKeys(v, key, out)
    else out.push(key)
  }
  return out
}
test('T4 UI locale en/zh 键集逐字相等 (改名未增删键)', () => {
  const a = flattenKeys(en).sort(), b = flattenKeys(zh).sort()
  eq(a.length, b.length, '键数')
  eq(a, b, '键集')
})
test('T4 mw/i18n en/zh 键集逐字相等', () => {
  const a = Object.keys(mwEn).sort(), b = Object.keys(mwZh).sort()
  eq(a, b, '键集')
})

if (failed) { console.error(`\n${failed} test(s) failed`); process.exit(1) }
console.log('\nall tests passed')
