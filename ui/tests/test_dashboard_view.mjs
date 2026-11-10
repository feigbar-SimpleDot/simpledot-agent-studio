#!/usr/bin/env node
// X批 dashboard 守护套件 (REQ-X-DASHBOARD AC-D9 / PLAN-X §2.6)
//
// 依据: docs/PLAN-X.md §2.6 (唯一技术依据: 源码抽取 + 断言, 不起浏览器,
//       风格沿 test_issue216_dup_render.mjs)。
//
// 覆盖:
//   T1 结构: DashboardView.jsx 存在; App.jsx NAV 含 id: 'dashboard' 且位于
//      第一位 (CC轮 §4.3 用户点名 dashboard 置顶, 序: dashboard < schedule <
//      tasks-board); 默认落点
//      useState('dashboard') 红线仍在 (dashboard-cockpit-20261105 OQ-2)。
//   T2 端点红线: DashboardView 源码含 /api/heartbeat、/api/schedule/state、
//      /api/schedule/tasks、/api/team-works、/api/workers/live、
//      /api/tasks?fields=active; /api/workers 全量裸调用恰 1 次 (仅首屏,
//      dashboard-cockpit-20261105 载荷红线裁决) 且不在 refreshAll (5s 轮询)。
//   T3 轮询纪律: document.hidden 门控 + visibilitychange 监听 +
//      clearInterval 清理 + 5000 间隔字面 (AC-D8)。
//   T4 i18n 双语对等: dashboard 顶层键存在且双语键集逐字相等;
//      nav.dashboard 双语均在 (AC-D10 静态面)。
//   T5 无硬编码色值: DashboardView 源码 grep #[0-9a-fA-F]{6} = 0 (AC-D10)。
//   T6 错误态: 每卡 error 占位 + dashboard.retry 文案 key 引用在位;
//      refresh 各路径 .catch 独立 (catch 计数 ≥ 卡数 5)。
//   T10 M-2 反证锚 (FIX-X2): 裸 fetch('/api/workers/live') 路径必须保留
//      `if (!r.ok) throw` 检查 (REVIEW-X M-2; MV-5 回退即红)。
//   T11 M-1 反证锚 (FIX-X2): 状态灯必须用真实类 dot-ok/dot-bad/dot-dim,
//      代码区不得出现不存在的 bg-green/bg-red/bg-slate (REVIEW-X M-1;
//      MV-6 回退即红)。
//   T12 插值占位符风格锚 (FIX-X2 / D-1 反证): dashboard 双语词条不得含
//      双花括号 `{{` —— 本仓 i18next 约定 prefix:'{' suffix:'}'
//      (ui/src/i18n/index.js:75), 双花括号会逐字渲染致插值失效。
//
// 变异反证锚 (MV, PLAN-X §2.6):
//   MV-1 删 NAV dashboard 项 → T1 红
//   MV-2 把 /api/workers/live 全局换成 /api/workers → T2 红
//   MV-3 删 document.hidden 门控 → T3 红
//   MV-4 只改一侧 locale 的 dashboard 键 → T4 红
//   MV-5 回退 M-2 (删 r.ok 检查) → T10 红        (FIX-X2 补锚)
//   MV-6 回退 M-1 (dot-* → bg-green/bg-red/bg-slate) → T11 红 (FIX-X2 补锚)
//   MV-7 dashboard 词条回退双花括号 {{n}} → T12 红 (FIX-X2 补锚)
//
// 纯函数直测 (vm 抽取): aggregateInbox done 仅计 24h 窗口 (COALESCE
// finished_at/updated_at); recentClosed 先过滤 CLOSED/ABORTED 再排序取 top5
// (PLAN-X §2.8 AC-D7 修正口径); liveProfiles 按 profile 去重且仅计
// thinking===true 条目 (CC轮 §4.1)。
import { readFileSync, existsSync } from 'node:fs'
import vm from 'node:vm'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(path.join(here, f), 'utf8')

const dashPath = '../src/views/DashboardView.jsx'
const dashExists = existsSync(path.join(here, dashPath))
const dash = dashExists ? read(dashPath) : ''
const app = read('../src/App.jsx')
const zh = JSON.parse(read('../src/i18n/locales/zh-cn.json'))
const en = JSON.parse(read('../src/i18n/locales/en.json'))

const tests = []
function test(name, fn) { tests.push({ name, fn }) }
function assert(c, m) { if (!c) throw new Error(m) }

// 源码抽取 (花括号配平, 同 test_issue216_dup_render.mjs 口径)
function extractFn(source, startMarker) {
  const start = source.indexOf(startMarker)
  if (start < 0) throw new Error(`源码抽取失败, 未找到标记: ${startMarker}`)
  let braces = 0, seenBrace = false
  for (let i = start; i < source.length; i++) {
    const ch = source[i]
    if (ch === '{') { braces++; seenBrace = true }
    else if (ch === '}') { braces--; if (seenBrace && braces === 0) return source.slice(start, i + 1) }
  }
  throw new Error(`源码抽取失败 (花括号不配平): ${startMarker}`)
}

// ── T1 结构 ──
test('T1 结构: 视图文件存在 + NAV 位次 + 默认落点红线', () => {
  assert(dashExists, 'ui/src/views/DashboardView.jsx 不存在')
  const iSched = app.indexOf("id: 'schedule'")
  const iDash = app.indexOf("id: 'dashboard'")
  const iTasks = app.indexOf("id: 'tasks-board'")
  assert(iSched >= 0 && iDash >= 0 && iTasks >= 0, 'App.jsx NAV 缺 schedule/dashboard/tasks-board 项')
  // CC轮 §4.3: dashboard 置顶 (原 issue #164「收件箱置顶」口径更新为
  // 「CC轮用户点名 dashboard 置顶」)。
  assert(iDash < iSched && iSched < iTasks, `NAV dashboard 必须位于第一位 (dashboard < schedule < tasks-board; 实际序: ${iDash},${iSched},${iTasks})`)
  assert(app.includes("useState('dashboard')"), "默认落点 useState('dashboard') 红线被改动 (dashboard-cockpit-20261105 OQ-2 裁决: 固定落地页 schedule→dashboard, 恒固定零记忆语义不变)")
  assert(app.includes("import DashboardView from './views/DashboardView.jsx'"), 'App.jsx 缺 DashboardView import')
})

// ── T2 端点红线 ──
test('T2 端点红线: 端点字面在位 + workers 全量仅首屏一次 (禁入 5s 轮询)', () => {
  for (const ep of ['/api/heartbeat', '/api/schedule/state', '/api/schedule/tasks', '/api/team-works', '/api/workers/live']) {
    assert(dash.includes(ep), `DashboardView 源码缺端点字面 ${ep}`)
  }
  assert(dash.includes('/api/tasks?fields=active'), 'DashboardView 缺 /api/tasks?fields=active (Z1 活跃口径轮询源)')
  // /api/workers 全量 (含 api_key 明文回显): 仅首屏一次取名单+头像 (ARCH §5.5
  // 载荷红线), 禁入 refreshAll 5s 轮询。MV-2 反证: 把 /api/workers/live 全局
  // 换成 /api/workers ⇒ 裸调用计数变 2 ⇒ 红。
  const bareCalls = dash.match(/fetch\('\/api\/workers'\)/g) || []
  assert(bareCalls.length === 1, `/api/workers 全量裸调用必须恰为 1 次 (首屏一次), 实测 ${bareCalls.length}`)
  const ra = extractFn(dash, 'const refreshAll = () =>')
  assert(!ra.includes("fetch('/api/workers')"), 'refreshAll (5s 轮询) 出现 /api/workers 全量调用 (载荷红线违例)')
})

// ── T3 轮询纪律 ──
test('T3 轮询纪律: hidden 门控 + visibilitychange + clearInterval + 5000', () => {
  // REVIEW-X N-1: 代码形态锚, 防头注注释里的 document.hidden 字样误命中 (MV-3 真锚)
  assert(dash.includes('if (document.hidden) return'), '缺 document.hidden 门控 (AC-D8: 隐藏即跳过)')
  assert(dash.includes('visibilitychange'), '缺 visibilitychange 监听 (AC-D8: 恢复可见立即刷新)')
  assert(dash.includes('clearInterval'), '缺 clearInterval 卸载清理 (AC-D8)')
  assert(dash.includes('5000'), '缺 5000 轮询间隔字面 (AC-D8)')
})

// ── T4 i18n 双语对等 ──
test('T4 i18n: dashboard 顶层键双语逐字相等 + nav.dashboard 在位', () => {
  assert(zh.dashboard && en.dashboard, 'locales 缺顶层 dashboard 键')
  const zk = Object.keys(zh.dashboard).sort().join(',')
  const ek = Object.keys(en.dashboard).sort().join(',')
  assert(zk === ek, `dashboard 双语键集不等: zh-only/en-only 差异存在`)
  assert(zh.nav.dashboard && en.nav.dashboard, 'nav.dashboard 双语缺项')
  assert(zh.app.subtitle_dashboard && en.app.subtitle_dashboard, 'app.subtitle_dashboard 双语缺项')
})

// ── T5 无硬编码色值 ──
test('T5 无硬编码色值: DashboardView 无 #rrggbb 字面 (AC-D10)', () => {
  const hits = dash.match(/#[0-9a-fA-F]{6}/g) || []
  assert(hits.length === 0, `出现硬编码色值: ${hits.join(', ')}`)
})

// ── T6 错误态 ──
test('T6 错误态: retry 文案 key + 每路独立 catch (≥5)', () => {
  assert(dash.includes('dashboard.retry'), '缺 dashboard.retry 重试文案 key 引用')
  assert(dash.includes('dashboard.error'), '缺 dashboard.error 错误占位文案 key 引用')
  const catches = (dash.match(/\.catch\(/g) || []).length
  assert(catches >= 5, `refresh 各路径独立 .catch 不足 (卡数 5, 实测 ${catches})`)
})

// ── T10 M-2 反证锚 (FIX-X2 / MV-5) ──
test('T10 M-2 锚: 裸 fetch 路径保留 r.ok 检查 (MV-5 回退即红)', () => {
  // REVIEW-X M-2: 非 2xx 必须 throw 落 catch 进错误态, 否则 4xx/5xx+合法 JSON
  // 会静默降级为空态。回退该检查 ⇒ 本锚红。
  assert(dash.includes('if (!r.ok) throw'), 'DashboardView 裸 fetch 缺 r.ok 检查 (REVIEW-X M-2 修复被回退)')
})

// ── T11 M-1 反证锚 (FIX-X2 / MV-6) ──
test('T11 M-1 锚: 状态灯用 dot-* 真实类, 代码区无 bg-green/bg-red/bg-slate (MV-6 回退即红)', () => {
  for (const cls of ['dot-ok', 'dot-bad', 'dot-dim']) {
    assert(dash.includes(`'${cls}'`), `DashboardView 缺 '${cls}' 类 (REVIEW-X M-1: 须与顶栏 App.jsx dot-* 同款)`)
  }
  // 代码区 (剔除整行注释) 不得出现不存在的 bg-* 类; 注释里的说明性提及不算违例
  const code = dash.split('\n').filter((l) => !l.trim().startsWith('//')).join('\n')
  for (const bad of ['bg-green', 'bg-red', 'bg-slate']) {
    assert(!code.includes(bad), `DashboardView 代码区出现不存在的类 ${bad} (REVIEW-X M-1 修复被回退)`)
  }
})

// ── T12 插值占位符风格锚 (FIX-X2 / D-1 反证 / MV-7) ──
test('T12 dashboard 双语词条无 {{ 双花括号 (D-1 反证: 仓约定单花括号)', () => {
  // 本仓 i18next interpolation prefix:'{' suffix:'}' (ui/src/i18n/index.js:75),
  // dashboard 段任何 `{{` 会逐字渲染 ⇒ 插值失效 (TEST-X-REPORT D-1)。
  for (const [name, dict] of [['zh-cn', zh], ['en', en]]) {
    assert(dict && dict.dashboard, `locales 缺顶层 dashboard 段 (${name})`)
    const hits = JSON.stringify(dict.dashboard).match(/\{\{/g) || []
    assert(hits.length === 0, `${name} dashboard 段出现双花括号 ${hits.length} 处 (应为单花括号 {n} 约定, D-1 回退)`)
  }
})

// ── 纯函数直测 (vm) ──
test('T7 aggregateInbox: done 仅计 24h 窗口 (COALESCE finished_at/updated_at)', () => {
  const src = extractFn(dash, 'export function aggregateInbox')
  const fn = vm.runInNewContext(`(() => { ${src.replace('export function', 'function')} ; return aggregateInbox })()`)
  const now = 1_000_000
  const rows = [
    { status: 'pending' }, { status: 'pending' }, { status: 'running' },
    { status: 'paused' }, { status: 'hold' }, { status: 'hold' },
    { status: 'done', finished_at: now - 60 },                    // 窗口内
    { status: 'done', finished_at: now - 25 * 3600 },             // 窗口外
    { status: 'done', finished_at: null, updated_at: now - 3600 },// COALESCE → updated_at 窗口内
    { status: 'cancelled' },
  ]
  const c = fn(rows, now)
  assert(c.pending === 2 && c.running === 1 && c.paused === 1 && c.hold === 2, `全量计数错: ${JSON.stringify(c)}`)
  assert(c.done24h === 2, `done 24h 窗口计数错 (应 2): ${JSON.stringify(c)}`)
})

test('T8 recentClosed: 先过滤 CLOSED/ABORTED 再 closed_at 降序 top5 (AC-D7 修正)', () => {
  const src = extractFn(dash, 'export function recentClosed')
  const fn = vm.runInNewContext(`(() => { ${src.replace('export function', 'function')} ; return recentClosed })()`)
  const runs = [
    { run_id: 'a', status: 'ACTIVE', closed_at: null },           // 必须被过滤 (AC-D7)
    { run_id: 'b', status: 'HOLD', closed_at: null },             // 必须被过滤
    { run_id: 'c', status: 'CLOSED', closed_at: 300 },
    { run_id: 'd', status: 'ABORTED', closed_at: 500 },
    { run_id: 'e', status: 'CLOSED', closed_at: 400 },
    { run_id: 'f', status: 'CLOSED', closed_at: 200 },
    { run_id: 'g', status: 'CLOSED', closed_at: 100 },
    { run_id: 'h', status: 'CLOSED', closed_at: 50 },
  ]
  const top = fn(runs)
  assert(top.map((r) => r.run_id).join(',') === 'd,e,c,f,g', `过滤/排序/top5 错: ${top.map((r) => r.run_id).join(',')}`)
})

test('T9 liveProfiles: 按 profile 去重 + 仅 thinking===true 入列 (CC轮 §4.1)', () => {
  const src = extractFn(dash, 'export function liveProfiles')
  const fn = vm.runInNewContext(`(() => { ${src.replace('export function', 'function')} ; return liveProfiles })()`)
  const out = fn([{ profile: 'coder', thinking: true }, { profile: 'coder', thinking: true }, { profile: 'tester', thinking: true }, null, {}])
  assert(out.join(',') === 'coder,tester', `去重错: ${out.join(',')}`)
  // CC轮 §4.1 新语义: thinking:false (终态) 条目不入活跃集; thinking:true 入。
  assert(fn([{ profile: 'x', thinking: false }]).join(',') === '', 'thinking:false 条目不得入活跃集 (worker 永不回空闲回归)')
  assert(fn([{ profile: 'x', thinking: true }]).join(',') === 'x', 'thinking:true 条目必须入活跃集')
  // thinking 缺失/非 true 一律不入 (undefined/null/false 同口径)
  assert(fn([{ profile: 'y' }, { profile: 'z', thinking: null }, { profile: 'w', thinking: 'yes' }]).length === 0, 'thinking 非 true 不得入活跃集')
})

// ── T13/T14 驾驶舱改版纯函数 (dashboard-cockpit-20261105) ──
test('T13 recentTokenRuns: CLOSED/ABORTED 过滤 + created_at DESC top10 + 缺失语义 (A4-1/A4-3)', () => {
  const src = extractFn(dash, 'export function recentTokenRuns')
  const fn = vm.runInNewContext(`(() => { ${src.replace('export function', 'function')} ; return recentTokenRuns })()`)
  const mk = (id, status, created_at, tt) => ({ run_id: id, status, created_at, initial_task: 't' + id, token_total: tt })
  const runs = [
    mk('a', 'ACTIVE', 500, { source: 'api', total_tokens: 1 }),            // 必须被过滤
    mk('b', 'CLOSED', 300, { source: 'api', total_tokens: 160854, estimated_cost_usd: 1.2 }),
    mk('c', 'ABORTED', 500, { source: 'unavailable', total_tokens: 0, missing: 7 }),
    mk('d', 'CLOSED', 400, { source: 'partial', total_tokens: 90, captured: 3, missing: 1 }),
  ]
  const out = fn(runs)
  assert(out.map((r) => r.run_id).join(',') === 'c,d,b', `过滤/排序错: ${out.map((r) => r.run_id).join(',')}`)
  assert(out[0].missing === true && out[0].tokens === null, 'unavailable 必须 missing 语义且值 null (A4-3: 不进数值轴)')
  assert(out[1].partial === true && out[1].tokens === 90, 'partial 正常画柱')
  assert(out[2].missing === false && out[2].tokens === 160854, 'api 源数值错')
  // top10 截断
  const many = Array.from({ length: 12 }, (_, i) => mk('r' + i, 'CLOSED', i, { source: 'api', total_tokens: i }))
  assert(fn(many).length === 10 && fn(many)[0].run_id === 'r11', 'top10/DESC 截断错')
})

test('T14 activeWorkers: team_work_active ∪ live[] (OQ-5 口径 + CC轮 §4.1 thinking 过滤)', () => {
  const src = extractFn(dash, 'export function liveProfiles') + '\n'
    + extractFn(dash, 'export function activeWorkers')
  const fn = vm.runInNewContext(`(() => { ${src.replace(/export function/g, 'function')} ; return activeWorkers })()`)
  const out = fn({ current_card_assignee: 'coder', card_count: 3 }, [{ profile: 'tester', thinking: true }, { profile: 'coder', thinking: true }])
  assert(out.map((w) => w.name).join(',') === 'coder,tester', `并集/去重错: ${JSON.stringify(out)}`)
  assert(out[0].viaRun === true && out[0].cardCount === 3, 'run 侧 worker 应 viaRun + 卡数')
  assert(out[1].viaRun === false, 'live-only worker 应 SSE 活体态')
  assert(fn(null, null).length === 0 && fn({}, []).length === 0, '空输入应空数组')
  // CC轮 §4.1: thinking:false 的 live 条目不入活跃集 (终态后回空闲);
  // thinking:true 入。run 侧 (viaRun) 不受该过滤影响 (Z1 viaRun 路径红线)。
  const out2 = fn(null, [{ profile: 'done', thinking: false }, { profile: 'busy', thinking: true }])
  assert(out2.map((w) => w.name).join(',') === 'busy', `thinking 过滤错: ${JSON.stringify(out2)}`)
  const out3 = fn({ current_card_assignee: 'coder', card_count: 1 }, [{ profile: 'coder', thinking: false }])
  assert(out3.length === 1 && out3[0].name === 'coder' && out3[0].viaRun === true, 'run 侧 worker 不应被 thinking 过滤误伤')
})

// ── T15 NIT-1 防回归锚 (REVIEW dashboard-cockpit-20261105) ──
test('T15 TokenBarChart 创建 effect 用 itemsRef 初始化数据 (语言切换无空窗)', () => {
  // NIT-1: 创建 effect deps=[t], 语言切换销毁重建。若初始 data 为空而 items
  // 引用未变, 数据 effect 不重跑 ⇒ 空窗 ≤5s。回退成 labels:[] 空初始化 ⇒ 本锚红。
  // (全文件锚: 本文件两处 new Chart 均无空数组初始化, 环图本就带初始数据)
  assert(!/labels:\s*\[\s*\]/.test(dash), 'DashboardView 出现空 labels 初始化 (NIT-1 空窗回归)')
  assert(!/data:\s*\[\s*\]/.test(dash), 'DashboardView 出现空 data 初始化 (NIT-1 空窗回归)')
  assert(/barDataOf\(itemsRef\.current/.test(dash), 'TokenBarChart 创建 effect 未用 itemsRef.current 初始化 (NIT-1 修复被回退)')
})

// ── T16 CC轮 §4.2 静态守护: dashboard 新任务按钮已移除 ──
test('T16 dashboard 无快速添加任务入口 (CC轮 §4.2: quickAddBtn/onQuickAdd 全清)', () => {
  assert(!dash.includes('quickAddBtn'), 'DashboardView 仍含 quickAddBtn 字面 (§4.2 要求删除)')
  assert(!dash.includes('onQuickAdd'), 'DashboardView 仍含 onQuickAdd 字面 (§4.2 要求删除 prop)')
  assert(!app.includes('onQuickAdd'), 'App.jsx 仍向 DashboardView 传 onQuickAdd (§4.2 要求清理传参)')
  // 全局入口红线: App header 核心功能按钮 (openCoreFeatures) 必须保留。
  assert(app.includes('openCoreFeatures'), 'App.jsx openCoreFeatures 被误删 (header 核心功能按钮红线)')
})

// ── T17 issue #229 GG轮 静态守护: worker 行双栏两张并列大卡 (用户评论 2287) ──
// 依据 ADJUDICATION-GG §3: PC 宽屏 worker 行 = 左右两张等宽高并列大卡撑满一行
// (grid xl:grid-cols-2 + items-stretch); 左卡「工作中」(z1_working_title) 头像
// 带动效边框 (.wcard-avatar-live); 右卡「空闲」(r4_idle) 列表卡内滚动
// (max-h + overflow-auto)。旧 e03d36b 单一 flex-wrap 混排流必须移除。
// 硬约束: 两态判定口径 (workingWorkers/idleWorkers 派生) 一字不动 —— 本锚
// 同时是口径不回退的反证锚 (T14 activeWorkers 锚独立在位, 不得改)。
test('T17 #229 双栏两张并列大卡布局 (静态守护, 2287 新口径)', () => {
  const css = read('../src/index.css')
  // 1) IdleWorkerCard 紧凑条目行 (头像24 + truncate + 仅对话按钮, 零新键)
  assert(!dash.includes('IdleWorkerRow'), 'IdleWorkerRow 仍存在 (#229 要求紧凑条目)')
  const cStart = dash.indexOf('function IdleWorkerCard')
  assert(cStart >= 0, '缺 IdleWorkerCard 组件')
  const card = dash.slice(cStart, dash.indexOf('\n}', cStart) + 2)
  for (const cls of ['h-10', 'rounded-xl', 'border border-line', 'bg-panel', 'px-3']) {
    assert(card.includes(cls), `IdleWorkerCard 缺紧凑条目类 '${cls}'`)
  }
  assert(/size=\{24\}/.test(card), 'IdleWorkerCard 头像必须 24px')
  assert(card.includes('truncate'), 'IdleWorkerCard 名字必须 truncate')
  assert(card.includes('dashboard.enter_chat'), 'IdleWorkerCard 对话按钮必须复用 dashboard.enter_chat (零新键)')
  assert((card.match(/<button/g) || []).length === 1, 'IdleWorkerCard 应仅一个对话按钮')
  // 2) Z1 = 双栏 grid (等宽 xl:grid-cols-2 + 等高 items-stretch, 窄屏堆叠);
  //    旧 flex-wrap 混排流 (被否决的 e03d36b) 必须移除
  const sStart = dash.indexOf('<section className="flex flex-col min-h-[180px]">')
  assert(sStart >= 0, 'Z1 section 容器字面被改动 (min-h-[180px])')
  const zone = dash.slice(sStart, dash.indexOf('</section>', sStart))
  assert(!zone.includes('flex flex-wrap gap-4'),
    '旧单一 flex-wrap 混排容器未移除 (2287 已否决混排同流口径)')
  assert(zone.includes('grid grid-cols-1 xl:grid-cols-2') && zone.includes('items-stretch'),
    'Z1 必须为双栏 grid (grid-cols-1 xl:grid-cols-2 + items-stretch 等高撑满一行)')
  // 3) 左卡「工作中」在前、右卡「空闲」在后; 标题复用孤儿键零新键
  const iWorkTitle = zone.indexOf('dashboard.z1_working_title')
  const iIdleTitle = zone.indexOf('dashboard.r4_idle')
  assert(iWorkTitle >= 0 && iIdleTitle >= 0, '双栏标题缺 z1_working_title / r4_idle (复用现成键)')
  assert(iWorkTitle < iIdleTitle, '左卡必须为工作中、右卡为空闲')
  const iWork = zone.indexOf('workingWorkers.map')
  const iIdle = zone.indexOf('idleWorkers.map')
  assert(iWork >= 0 && iIdle >= 0, '双栏缺 workingWorkers/idleWorkers map')
  assert(iWork < iIdle, 'working 列表必须在左卡 (源码序在前)')
  // 4) 卡内滚动: 两栏内容区各自 max-h + overflow-auto (高度不够卡内上下滚动)
  const scrolls = zone.match(/max-h-\[[^\]]+\][^"]*overflow-auto|overflow-auto[^"]*max-h-\[[^\]]+\]/g) || []
  assert(scrolls.length >= 2, `双栏内容区必须各自 max-h + overflow-auto (实测 ${scrolls.length})`)
  // 5) working 头像动效边框: WorkerAvatar live 变体 → .wcard-avatar-live
  //    (复用 wcard-live-border 同款 keyframes); WorkerRunCard 挂 live;
  //    双通道降级 (.anim-stopped + prefers-reduced-motion 停死清单)
  assert(/function WorkerAvatar\(\{[^}]*live = false[^}]*\}\)/.test(dash),
    'WorkerAvatar 缺 live 变体 prop')
  assert(dash.includes("'wcard-avatar-live'"), 'WorkerAvatar live 分支缺 .wcard-avatar-live 类')
  assert(/<WorkerAvatar name=\{name\} avatar=\{avatar\} size=\{64\} live\s*\/>/.test(dash),
    'WorkerRunCard working 头像未挂 live (动效边框须落在头像)')
  assert(!dash.includes('wcard-live w-[260px]'), '旧卡级动效 + 固定 260px 宽未移除 (双栏改造后条目应 w-full)')
  assert(css.includes('.wcard-avatar-live') && css.includes('animation: wcard-live-border'),
    'index.css 缺 .wcard-avatar-live 动效 (复用 wcard-live-border keyframes)')
  assert(css.includes('.anim-stopped .wcard-avatar-live'), '.wcard-avatar-live 缺 .anim-stopped 降级')
  assert(/prefers-reduced-motion[\s\S]*\.wcard-avatar-live/.test(css),
    '.wcard-avatar-live 缺 prefers-reduced-motion 停死降级')
  // 6) 空态: 双区皆空保留 z1_empty 占位 (零新键)
  assert(zone.includes('workingWorkers.length === 0 && idleWorkers.length === 0')
    && zone.includes('dashboard.z1_empty'), '双区皆空必须保留空态占位 (复用 z1_empty)')
  // 7) 两态判定口径硬约束一字不动 (main 裁决 4 红线; 布局单不得触碰分类)
  assert(dash.includes('const workingWorkers = [...actives].sort((a, b) => a.name.localeCompare(b.name))'),
    'workingWorkers 派生口径被改动 (硬约束: 一字不动)')
  assert(dash.includes('.filter((w) => w.online && !activeNames.has(w.name))'),
    'idleWorkers 派生口径被改动 (硬约束: registry online 且不在活跃集)')
})

let failed = 0
for (const { name, fn } of tests) {
  try { fn(); console.log(`PASS ${name}`) }
  catch (e) { failed++; console.log(`FAIL ${name}: ${e.message}`) }
}
console.log(failed === 0 ? `\nALL ${tests.length} PASS` : `\n${failed}/${tests.length} FAIL`)
process.exit(failed === 0 ? 0 : 1)
