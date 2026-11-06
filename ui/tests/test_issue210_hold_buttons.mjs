// issue #210 Z批 守护套件 — hold/resume 操作入口 (当前卡挂起按钮 + 历史 HOLD 行恢复按钮)
// (依据 docs/PLAN-Z-20261105.md §2.6 T1-T6 + docs/acceptance-criteria.md AC-Z-*)
//
// 覆盖:
//   T1  api.js 含 teamWorkHold/teamWorkResume 封装, URL 模板含 /hold 与
//       /hold/resume 且 runId 过 encodeURIComponent; api.js 无 /hold 之外的
//       新端点字符串 (AC-Z-FE-1, 零后端契约红线源码面)。
//   T2  TasksBoardView 含 askHoldRun/askResumeRun, 且两者内部先 setConfirm(
//       后 api.teamWorkHold(/api.teamWorkResume( (confirm 包裹顺序, AC-Z-FE-4)。
//   T3  CurrentTaskCard 调用点含 onHold= 且按钮 disabled={holding};
//       TaskCard 含 run.status === 'HOLD' 门控的 resume 按钮 + disabled={holding}
//       (AC-Z-FE-3/9 源码面)。
//   T4  holdResultToast 含 was_already 与 schedule_linkage 消费点 +
//       startsWith('failed') (AC-Z-FE-7/10 源码面)。
//   T5  失败分支含 e?.message || 透传 (409 业务文案通道, AC-Z-FE-8 源码面)。
//   T6  i18n: 17 键双语在位 + zh/en 全量键路径集合 diff 为空 (沿 #213 T4 同法)。
//
// 变异反证锚 (MV): 删封装→T1 红; 删 setConfirm 包裹→T2 红; 删按钮/disabled→T3 红;
//                  删 holdResultToast 分类→T4 红; 改回 fmtApiError 吞文案→T5 红;
//                  任一语种删键→T6 红。
//
// 风格: 沿用仓库既有 ui/tests/*.mjs — 源码抽取 + JSON 断言, 不起浏览器。
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(path.join(here, f), 'utf8')
const api = read('../src/api.js')
const board = read('../src/views/TasksBoardView.jsx')
const zh = JSON.parse(read('../src/i18n/locales/zh-cn.json'))
const en = JSON.parse(read('../src/i18n/locales/en.json'))

let failed = 0, passed = 0
function test(n, f) { try { f(); passed++; console.log(`ok - ${n}`) } catch (e) { failed++; console.error(`FAIL - ${n}\n  ${e.message}`) } }
function assert(c, m) { if (!c) throw new Error(m) }

// 取 askHoldRun / askResumeRun / holdResultToast 函数体
const holdFn = board.match(/const askHoldRun = [\s\S]*?\n  \}\)/)?.[0] || ''
const resumeFn = board.match(/const askResumeRun = [\s\S]*?\n  \}\)/)?.[0] || ''
const toastFn = board.match(/function holdResultToast\([\s\S]*?\n\}/)?.[0] || ''

// ── T1: api.js 两封装 ────────────────────────────────────────────────
test('T1 api.js 含 teamWorkHold/teamWorkResume 且 URL 模板正确', () => {
  assert(/teamWorkHold: \(runId, body\) =>/.test(api), 'api.js 缺 teamWorkHold 封装')
  assert(/teamWorkResume: \(runId, body\) =>/.test(api), 'api.js 缺 teamWorkResume 封装')
  assert(/fetch\(`\/api\/team-work\/\$\{encodeURIComponent\(runId\)\}\/hold`/.test(api),
    'teamWorkHold URL 应为 /api/team-work/${encodeURIComponent(runId)}/hold')
  assert(/fetch\(`\/api\/team-work\/\$\{encodeURIComponent\(runId\)\}\/hold\/resume`/.test(api),
    'teamWorkResume URL 应为 /api/team-work/${encodeURIComponent(runId)}/hold/resume')
})
test('T1 api.js 无 /hold 之外的新端点字符串 (零契约红线)', () => {
  // /hold 出现次数恰为 2 (两条模板各一次) —— 夹带第三条 hold 端点即红。
  const n = (api.match(/\/hold/g) || []).length
  assert(n === 2, `/hold 端点字符串应恰为 2 条 (/hold 与 /hold/resume), 实际 ${n}`)
  // team-work 动作段集合钉死为基线 + hold/hold/resume (夹带新动作即红)。
  const actions = new Set(
    [...api.matchAll(/\/api\/team-work\/\$\{encodeURIComponent\(runId\)\}\/([a-z/]+)/g)]
      .map((m) => m[1]))
  const expected = new Set(['close', 'archive', 'advance', 'hold', 'hold/resume', 'todo'])
  const extra = [...actions].filter((a) => !expected.has(a))
  assert(extra.length === 0, `team-work 出现预期外动作段: ${extra.join(', ')}`)
})

// ── T2: confirm 包裹顺序 (AC-Z-FE-4) ────────────────────────────────
test('T2 askHoldRun 先 setConfirm 后 api.teamWorkHold', () => {
  assert(holdFn.length > 0, 'askHoldRun 未找到')
  assert(/setConfirm\(\{/.test(holdFn), 'askHoldRun 应经 setConfirm 二次确认')
  const iConfirm = holdFn.indexOf('setConfirm(')
  const iApi = holdFn.indexOf('api.teamWorkHold(')
  assert(iConfirm >= 0 && iApi > iConfirm, 'setConfirm 应先于 api.teamWorkHold (弹窗先行)')
})
test('T2 askResumeRun 先 setConfirm 后 api.teamWorkResume', () => {
  assert(resumeFn.length > 0, 'askResumeRun 未找到')
  assert(/setConfirm\(\{/.test(resumeFn), 'askResumeRun 应经 setConfirm 二次确认')
  const iConfirm = resumeFn.indexOf('setConfirm(')
  const iApi = resumeFn.indexOf('api.teamWorkResume(')
  assert(iConfirm >= 0 && iApi > iConfirm, 'setConfirm 应先于 api.teamWorkResume (弹窗先行)')
})

// ── T3: 按钮落点与防连点 (AC-Z-FE-2/3/9) ────────────────────────────
test('T3 CurrentTaskCard hold 按钮: onHold= 调用点 + disabled={holding}', () => {
  assert(/onHold=\{\(\) => askHoldRun\(/.test(board), 'CurrentTaskCard 调用点缺 onHold=')
  assert(/onClick=\{onHold\} disabled=\{holding\}/.test(board),
    'hold 按钮应 disabled={holding} (防连点)')
})
test('T3 TaskCard HOLD 分支 resume 按钮 + disabled={holding}', () => {
  assert(/run\.status === 'HOLD' \?/.test(board), "TaskCard 缺 run.status === 'HOLD' 门控分支")
  assert(/onClick=\{onResumeRun\} disabled=\{holding\}/.test(board),
    'resume 按钮应 disabled={holding} (防连点)')
  // HOLD 分支必须在 !== 'ARCHIVED' 兜底分支之前, 否则 HOLD 行仍只显示归档
  const iHold = board.indexOf("run.status === 'HOLD'")
  const iArch = board.indexOf("run.status !== 'ARCHIVED'")
  assert(iHold > 0 && iArch > 0 && iHold < iArch,
    `HOLD 分支应在 !== 'ARCHIVED' 兜底分支之前 (hold=${iHold}, archived=${iArch})`)
})

// ── T4: holdResultToast 三分类 (AC-Z-FE-7/10) ───────────────────────
test('T4 holdResultToast 消费 was_already + schedule_linkage + startsWith(failed)', () => {
  assert(toastFn.length > 0, 'holdResultToast 函数未找到')
  assert(/schedule_linkage/.test(toastFn), 'holdResultToast 应消费 schedule_linkage')
  assert(/startsWith\('failed'\)/.test(toastFn), "联动半失败应 startsWith('failed') 判定")
  assert(/was_already/.test(toastFn), 'holdResultToast 应消费 was_already (幂等如实申报)')
  assert(/type: 'warn'/.test(toastFn), '联动半失败应 warn 分类 (不伪装全成功)')
})

// ── T5: 409 业务文案透传 (AC-Z-FE-8) ────────────────────────────────
test('T5 失败分支 e?.message || 透传 (409 业务文案不被 fmtApiError 吞)', () => {
  assert(/t\('tboard\.hold_failed', \{ err: e\?\.message \|\| fmtApiError\(e\) \}\)/.test(holdFn),
    'askHoldRun catch 应 e?.message 优先透传服务端 error')
  assert(/t\('tboard\.resume_failed', \{ err: e\?\.message \|\| fmtApiError\(e\) \}\)/.test(resumeFn),
    'askResumeRun catch 应 e?.message 优先透传服务端 error')
})

// ── T6: i18n 17 键双语 + 全量键集 diff 为空 (AC-Z-I18N-1) ───────────
const NEW_KEYS = [
  'hold_btn', 'hold_btn_busy', 'resume_btn', 'resume_btn_busy',
  'confirm_hold_title', 'confirm_hold_text', 'confirm_hold_ok',
  'confirm_resume_title', 'confirm_resume_text', 'confirm_resume_ok',
  'hold_success', 'hold_already', 'hold_failed',
  'resume_success', 'resume_already', 'resume_failed',
  'hold_linkage_warn',
]
function flatKeys(obj, prefix = '') {
  const out = new Set()
  for (const [k, v] of Object.entries(obj)) {
    if (v && typeof v === 'object') {
      for (const kk of flatKeys(v, `${prefix}${k}.`)) out.add(kk)
    } else out.add(`${prefix}${k}`)
  }
  return out
}
test('T6 17 键双语在位 (tboard.* 下非空字符串)', () => {
  assert(NEW_KEYS.length === 17, '本套件键清单应为 17')
  for (const k of NEW_KEYS) {
    assert(typeof zh.tboard[k] === 'string' && zh.tboard[k].length > 0, `zh.tboard.${k} 缺失`)
    assert(typeof en.tboard[k] === 'string' && en.tboard[k].length > 0, `en.tboard.${k} 缺失`)
  }
})
test('T6 zh/en 全量键路径集合 diff 为空 (沿 #213 T4 同法)', () => {
  const kz = flatKeys(zh), ke = flatKeys(en)
  const onlyZh = [...kz].filter((k) => !ke.has(k))
  const onlyEn = [...ke].filter((k) => !kz.has(k))
  assert(onlyZh.length === 0, `only-in-zh: ${onlyZh.join(', ')}`)
  assert(onlyEn.length === 0, `only-in-en: ${onlyEn.join(', ')}`)
})

console.log(`\n${passed} passed, ${failed} failed`)
if (failed > 0) process.exit(1)
