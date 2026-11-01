// issue #164 追加 (排期唯一入口改造) 前端测试 — 卡 t_91cab28d
//
// 用户裁决: 「New task」按钮不应存在; 「发起多智能体协作」按钮的作用 =
// 往排期任务添加新任务 (排期 = 任务唯一入口, 定时任务除外); 缺失的
// workflow/workspace 信息须在入队路径补齐。
//
// 覆盖:
//   E1  排期页新建入口移除: ScheduleView 无 openCreate / 无 api.scheduleCreate
//       调用 / 无 schedule.btn.new 引用 (三处锚, 删入口不删锚即红)
//   E2  paused 修改能力保留: openEdit 存在, submitForm 走
//       scheduleAction(id,'update'), 弹窗标题固定 edit_title (无 new_title)
//   E3  schedActions 矩阵回归: paused 仍含 edit (入口改造不得动状态机可见性)
//   E4  协作弹窗入队字段完整: ChatView submitTeamWork 调 api.scheduleCreate
//       且 detail/workflow/workspace 三字段齐 (缺失信息在入队路径补齐);
//       函数体不再直接 launch (createSession/runStream/teamWorkLaunch 零出现)
//   E5  i18n 双语同步: schedule 子树 en/zh 键集合相等; 3 个死键
//       (btn.new / form.new_title / toast.created) 双边已删; empty.hint
//       双边指向「发起多智能体协作」入口
//   E6  api.js scheduleCreate 封装保留 (入队端点唯一前端通道)
//
// 风格: 沿用仓库既有 ui/tests/*.mjs —— 纯源码/纯函数断言, 不起浏览器
// (源码抽取执行方式与 test_cron_view.mjs 同款)。
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const here = path.dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(path.join(here, f), 'utf8')
const schedSrc = read('../src/views/ScheduleView.jsx')
const chatSrc = read('../src/views/ChatView.jsx')
const apiSrc = read('../src/api.js')
const en = JSON.parse(read('../src/i18n/locales/en.json'))
const zh = JSON.parse(read('../src/i18n/locales/zh-cn.json'))

let failed = 0
function test(n, f) { try { f(); console.log(`ok - ${n}`) } catch (e) { failed++; console.error(`FAIL - ${n}\n  ${e.message}`) } }
function assert(c, m) { if (!c) throw new Error(m) }
function eq(a, b, m) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b)
  if (sa !== sb) throw new Error(`${m || ''} 期望 ${sb}, 实得 ${sa}`)
}
// 去注释后的代码体 (注释里出现 createSession 等历史说明不算调用)
function stripComments(src) {
  return src.split('\n')
    .filter((ln) => !ln.trim().startsWith('//'))
    .join('\n')
    .replace(/\/\*[\s\S]*?\*\//g, '')
}

// ── 从 ScheduleView.jsx 抽取 schedActions 纯函数 (vm 沙箱执行源码) ──
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
const ctx = vm.createContext({ console })
vm.runInContext(extractFn(schedSrc, 'schedActions') +
  '\nglobalThis.__e = { schedActions }', ctx)
const F = ctx.__e

// issue #189: 括号配平截取完整调用表达式 (替代 400 字符魔数窗口)。
// 从 marker 的 `(` 起深度计数到配平的 `)`; 必须跳过 ' " ` 字符串字面量
// (含转义), 否则实参字符串里的括号会让朴素计数提前截断 ⇒ 假红。
function sliceCall(src, marker) {
  const i = src.indexOf(marker)
  if (i < 0) throw new Error(`找不到调用: ${marker}`)
  let depth = 0
  for (let j = i; j < src.length; j++) {
    const ch = src[j]
    if (ch === "'" || ch === '"' || ch === '`') {
      let k = j + 1
      while (k < src.length) {
        if (src[k] === '\\') k += 2
        else if (src[k] === ch) break
        else k++
      }
      j = k
      continue
    }
    if (ch === '(') depth++
    else if (ch === ')') { depth--; if (depth === 0) return src.slice(i, j + 1) }
  }
  throw new Error(`括号未配平: ${marker}`)
}

// 取函数体: 从声明起点到下一个顶层 `const ` 声明 (与 H8 锚同法)
function fnBody(src, declMarker) {
  const m = src.indexOf(declMarker)
  assert(m >= 0, `找不到声明: ${declMarker}`)
  const start = src.indexOf('{', m)
  let braces = 0
  for (let i = start; i < src.length; i++) {
    const ch = src[i]
    if (ch === '{') braces++
    else if (ch === '}') { braces--; if (braces === 0) return src.slice(m, i + 1) }
  }
  throw new Error(`大括号未配平: ${declMarker}`)
}

// ══════════════════════════════════════════════════════════════════
// E1: 排期页「新建任务」入口移除 (三锚)
// ══════════════════════════════════════════════════════════════════
test('E1a ScheduleView 无 openCreate (新建入口函数已删)', () => {
  assert(!schedSrc.includes('openCreate'), 'ScheduleView 仍引用 openCreate')
})
test('E1b ScheduleView 无 api.scheduleCreate 调用 (排期页不再自建任务)', () => {
  assert(!schedSrc.includes('scheduleCreate'),
    'ScheduleView 仍调用 scheduleCreate —— 排期页不应保留新建通道')
})
test('E1c ScheduleView 无 schedule.btn.new 引用', () => {
  assert(!schedSrc.includes('schedule.btn.new'),
    'ScheduleView 仍引用 schedule.btn.new 词条')
})

// ══════════════════════════════════════════════════════════════════
// E2: paused 修改能力保留
// ══════════════════════════════════════════════════════════════════
test('E2a openEdit 保留且走表单弹窗', () => {
  assert(schedSrc.includes('const openEdit = (task) =>'), 'openEdit 被误删')
  assert(schedSrc.includes('onClick={() => openEdit(task)}'),
    '修改按钮的 openEdit 绑定丢失')
})
test('E2b submitForm 走 scheduleAction(id, update) 且含 paused 防御', () => {
  const body = stripComments(fnBody(schedSrc, 'const submitForm = async () =>'))
  assert(body.includes("scheduleAction(formTask.id, 'update'"),
    'submitForm 未走 update 动作')
  assert(body.includes('if (!formTask) return'),
    'submitForm 缺 formTask 空值防御 (弹窗只服务修改)')
  assert(!body.includes('scheduleCreate'),
    'submitForm 残留新建分支')
})
test('E2c 弹窗标题固定 edit_title (无 new_title 分支)', () => {
  assert(schedSrc.includes("title={t('schedule.form.edit_title')}"),
    '修改弹窗标题不再是固定 edit_title')
  assert(!schedSrc.includes('schedule.form.new_title'),
    'ScheduleView 仍引用 schedule.form.new_title')
})

// ══════════════════════════════════════════════════════════════════
// E3: schedActions 状态机可见性矩阵回归 (入口改造不动状态机)
// ══════════════════════════════════════════════════════════════════
test('E3 schedActions 矩阵逐格不变 (paused 仍含 edit)', () => {
  eq(F.schedActions('pending'), ['pause', 'cancel'], 'pending')
  eq(F.schedActions('paused'), ['resume', 'cancel', 'edit'], 'paused')
  eq(F.schedActions('running'), [], 'running')
  eq(F.schedActions('done'), [], 'done')
  eq(F.schedActions('cancelled'), ['delete'], 'cancelled')
  eq(F.schedActions('nonsense'), [], '未知态')
})

// ══════════════════════════════════════════════════════════════════
// E4: 协作弹窗 = 排期唯一新建入口, 入队字段完整
// ══════════════════════════════════════════════════════════════════
test('E4a submitTeamWork 入队且 detail/workflow/workspace 三字段齐', () => {
  const body = stripComments(fnBody(chatSrc, 'const submitTeamWork = async () =>'))
  assert(body.includes('api.scheduleCreate('), 'submitTeamWork 未走排期入队')
  // issue #189: 括号配平截取完整调用, 不再用 400 字符魔数窗口
  // (旧窗口在入队体前部插入长字段时会把三字段挤出窗口 ⇒ 误报缺陷)
  const call = sliceCall(body, 'api.scheduleCreate(')
  for (const f of ['detail:', 'workflow:', 'workspace:']) {
    assert(call.includes(f), `入队字段缺 ${f} (workflow/workspace 必须随任务补齐)`)
  }
})
test('E4a2 sliceCall 自测: 长字段不误报 + 字符串内括号不截断 (issue #189)', () => {
  // MV-189-a 形态: workflow 前插入 >400 字符假字段 ⇒ 配平截取仍含三字段
  const padded = 'api.scheduleCreate({ filler: "' + 'x'.repeat(500) +
    '", detail: d, workflow: w, workspace: ws })'
  const call = sliceCall(padded, 'api.scheduleCreate(')
  for (const f of ['detail:', 'workflow:', 'workspace:']) {
    assert(call.includes(f), `长字段插入后误报: 缺 ${f}`)
  }
  // 字符串字面量内的括号不得导致提前截断
  const tricky = "api.scheduleCreate({ detail: 'a(b)c', workflow: w, workspace: ws })"
  const call2 = sliceCall(tricky, 'api.scheduleCreate(')
  assert(call2.endsWith('})'), '字符串内括号导致截断')
  assert(call2.includes('workspace:'), '截断丢失后部字段')
})
test('E4b submitTeamWork 不再直接 launch (createSession/runStream/teamWorkLaunch 零出现)', () => {
  const body = stripComments(fnBody(chatSrc, 'const submitTeamWork = async () =>'))
  for (const banned of ['g.createSession()', 'runStream(', 'teamWorkLaunch(']) {
    assert(!body.includes(banned), `submitTeamWork 残留直接发起: ${banned}`)
  }
})
test('E4c ChatView 全文件无 teamWorkLaunch 调用 (launch 通道只余 api.js 封装)', () => {
  assert(!stripComments(chatSrc).includes('api.teamWorkLaunch'),
    'ChatView 仍调用 teamWorkLaunch')
})

// ══════════════════════════════════════════════════════════════════
// E5: i18n 双语同步
// ══════════════════════════════════════════════════════════════════
function flatKeys(obj, pre = '') {
  const out = new Set()
  for (const [k, v] of Object.entries(obj)) {
    if (v && typeof v === 'object') {
      for (const kk of flatKeys(v, `${pre}${k}.`)) out.add(kk)
    } else out.add(`${pre}${k}`)
  }
  return out
}
test('E5a schedule 子树 en/zh 键集合相等', () => {
  const se = [...flatKeys(en.schedule)].sort()
  const sz = [...flatKeys(zh.schedule)].sort()
  eq(se.filter((k) => !sz.includes(k)), [], 'only-in-en')
  eq(sz.filter((k) => !se.includes(k)), [], 'only-in-zh')
})
test('E5b 3 个死键双边已删 (btn.new / form.new_title / toast.created)', () => {
  const se = flatKeys(en.schedule), sz = flatKeys(zh.schedule)
  for (const k of ['btn.new', 'form.new_title', 'toast.created']) {
    assert(!se.has(k), `en schedule.${k} 未删`)
    assert(!sz.has(k), `zh schedule.${k} 未删`)
  }
})
test('E5c empty.hint 双边指向协作入口 (不再指向已删的新建按钮)', () => {
  assert(!en.schedule.empty.hint.includes('New Task'),
    'en empty.hint 仍指向已删的 New Task 按钮')
  assert(en.schedule.empty.hint.includes('multi-agent collaboration'),
    'en empty.hint 未指向协作入口')
  assert(!zh.schedule.empty.hint.includes('新建任务'),
    'zh empty.hint 仍指向已删的新建任务按钮')
  assert(zh.schedule.empty.hint.includes('发起多智能体协作'),
    'zh empty.hint 未指向协作入口')
})
test('E5d 协作入口按钮词条与 toast 键双边在位 (入口文案链完整)', () => {
  assert(en.chat.session.start_teamwork_btn && zh.chat.session.start_teamwork_btn,
    'start_teamwork_btn 缺失')
  assert(en.chat.team_work_modal.queued_toast && zh.chat.team_work_modal.queued_toast,
    'queued_toast 缺失 (入队反馈文案)')
})

// ══════════════════════════════════════════════════════════════════
// E6: api.js 入队封装保留
// ══════════════════════════════════════════════════════════════════
test('E6 api.js scheduleCreate 封装在位 (POST /api/schedule/tasks)', () => {
  assert(apiSrc.includes('scheduleCreate:'), 'api.js scheduleCreate 封装被删')
  assert(apiSrc.includes("fetch('/api/schedule/tasks'"),
    'api.js 排期入队端点封装丢失')
})

// ══════════════════════════════════════════════════════════════════
// E7: issue #185 migrateDraftWorkspace 死代码标注 (存在性 + 注释双守护)
// ══════════════════════════════════════════════════════════════════
test('E7a migrateDraftWorkspace 函数仍存在 (存在性守护, MV-185-a 的靶)', () => {
  assert(chatSrc.includes('const migrateDraftWorkspace = (newSid) =>'),
    'migrateDraftWorkspace 被删除 —— 删除前须先撤本锚并说明理由')
})
test('E7b 死代码标注四关键词齐备 (MV-185-b 的靶)', () => {
  const i = chatSrc.indexOf('const migrateDraftWorkspace = (newSid) =>')
  assert(i >= 0, '找不到 migrateDraftWorkspace')
  // 标注取函数定义之前的注释区 (向上 600 字符足够覆盖注释块)
  const above = chatSrc.slice(Math.max(0, i - 600), i)
  for (const kw of ['DEAD CODE', '死代码', '无调用点', '勿在此维护']) {
    assert(above.includes(kw), `死代码标注缺关键词: ${kw}`)
  }
})

// ══════════════════════════════════════════════════════════════════
// E8: issue #186 空详情显式错误文案 (编辑弹窗; 防呆保留是牙齿)
// ══════════════════════════════════════════════════════════════════
test('E8a 详情 onChange 清空即显式文案 / 非空即清除 (MV-186-a 的靶)', () => {
  const m = schedSrc.match(/onChange=\{\(e\) => \{[\s\S]*?\}\}/)
  assert(m, '详情 textarea 的 onChange 不是带分支的块 (清空分支被删?)')
  const oc = m[0]
  assert(oc.includes("t('schedule.form.detail_required')"),
    'onChange 清空分支未使用 detail_required 词条')
  assert(/if\s*\(!v\b/.test(oc) || /if\s*\(!e\.target\.value/.test(oc),
    'onChange 缺「变空」分支')
  assert(oc.includes('setFormErr(\'\')'), 'onChange 缺「非空清除错误」分支')
})
test('E8b 保存按钮防呆保留: disabled 仍含 !formDetail.trim() (MV-186-b 的靶)', () => {
  assert(schedSrc.includes('disabled={formSaving || !formDetail.trim()}'),
    '保存按钮置灰条件被放开 —— #164 防呆不得为 #186 让路')
})
test('E8c inline 错误渲染位仍在 (formErr 可见通道)', () => {
  assert(schedSrc.includes('{formErr &&'), 'formErr inline 渲染位丢失')
})

console.log(failed === 0 ? '\nALL PASS' : `\n${failed} FAILED`)
process.exit(failed === 0 ? 0 : 1)
