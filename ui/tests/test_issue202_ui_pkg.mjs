// [en] test_issue202_ui_pkg.mjs — static guard suite for issue #202 UI change
//      package (R1 default inbox / R2 teamwork modal overlay / R3 audit P0+P1
//      whitelist / R4 icon unification). Pure fs+regex assertions (no DOM),
//      mutation-proof: reverting a fix to its old form turns the matching
//      assertion red. Browser-level checks (computed styles, focus, contrast)
//      belong to s5 real-browser retest per REQ-R-20261102 §4.
// [zh-cn] issue #202 UI 改动包静态守护套件 (R1~R4)。fs+正则断言, 变异反证:
//        把修复改回旧形态对应断言必须转红。浏览器级实测 (computed style/焦点/
//        对比度) 按 REQ §4 归 s5。
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const read = (p) => readFileSync(join(root, p), 'utf8')

const app = read('src/App.jsx')
const ui = read('src/components/ui.jsx')
const chat = read('src/views/ChatView.jsx')
const tboard = read('src/views/TasksBoardView.jsx')
const workers = read('src/views/WorkersView.jsx')
const wf = read('src/views/WorkflowsView.jsx')
const sched = read('src/views/ScheduleView.jsx')
const cron = read('src/views/CronView.jsx')
const drawer = read('src/components/filebrowser/FileDrawer.jsx')
const preview = read('src/components/filebrowser/FilePreview.jsx')
const css = read('src/index.css')
const html = read('index.html')
const icons = read('src/components/icons.jsx')

let pass = 0, fail = 0
function ok(cond, msg) {
  if (cond) { pass++; console.log('ok -', msg) }
  else { fail++; console.error('FAIL -', msg) }
}

// ── R1 · 默认首页 = inbox (AC-R1-1~3 静态面) ─────────────────────────
// dashboard-cockpit-20261105 OQ-2 裁决 (main, 留痕): 固定落地页 schedule →
// dashboard。AC 不变式 = 「恒固定落地、零视图记忆」(下方 sas-view 无记忆断言
// 原样保留), 落点值随 OQ 裁决更新 —— ACCEPT R-2 允许的断言合理变更。
ok(/useState\('dashboard'\)/.test(app) && !/useState\('tasks-board'\)/.test(app),
  'R1: App 初始 view = dashboard (固定落地), tasks-board 硬编码清零')
ok(!/localStorage\.getItem\(['"]sas-view['"]/.test(app),
  'R1: 未引入视图记忆 (OQ-A 固定 inbox)')
ok(/sas-chat-tabs/.test(app) && /sas-theme/.test(app),
  'R1 回归: chat tabs / 主题 localStorage 持久化未回退 (AC-R1-4)')

// ── R2 · 添加任务弹框不切 tab (AC-R2-1~6 静态面) ─────────────────────
const ocf = app.match(/const openCoreFeatures = \(\) => \{[\s\S]*?\n  \}/)?.[0] || ''
ok(ocf.length > 0, 'R2: openCoreFeatures 存在')
ok(!/setView\(['"]chat['"]\)/.test(ocf) && !/setActiveChat\(['"]main['"]\)/.test(ocf),
  'R2: openCoreFeatures 不再 setView(chat)/setActiveChat(main) (AC-R2-1~3)')
ok(/createPortal\(\s*\n?\s*<Modal open=\{twOpen\}/.test(chat),
  'R2: TeamWork 弹框 createPortal 挂 document.body (任意视图 overlay)')
ok(/queueToast && createPortal\(/.test(chat),
  'R2: 入队成功反馈条 portal 到 body, 任意视图可见 (AC-R2-4)')
// issue #211 起 detail 值为 buildTeamWorkDetail(task, twFiles) (附件清单拼装),
// 三字段结构不变 —— 正则放宽 detail 的取值字面, 保留「三字段入队」判据本意。
// 守点归属 (DOC-AUDIT-T D9): 本断言只守结构层 (detail/workflow/workspace 三键在位);
// detail 取值形态由 test_issue211_teamwork_attach.mjs T4 (buildTeamWorkDetail 逐字锁) 守。
ok(/api\.scheduleCreate\(\{[\s\S]*?detail:[\s\S]*?workflow:[\s\S]*?workspace:/.test(chat),
  'R2 回归: 提交仍走 api.scheduleCreate 三字段入队 (AC-R2-5)')
ok(/role="dialog"/.test(ui) && /aria-modal="true"/.test(ui),
  'R2/R3-4: Modal overlay 语义 role=dialog + aria-modal (AC-R2-6/R3-4)')

// ── R3 P0 ────────────────────────────────────────────────────────────
ok(/@media \(prefers-reduced-motion: reduce\)/.test(css),
  'R3-1: prefers-reduced-motion 全局降级块在位 (AC-R3-1)')
for (const cls of ['sas-status-bar', 'wcard-gfx-working', 'animate-pulse', 'animate-spin', '.spin']) {
  const rmBlock = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'))
  ok(rmBlock.includes(cls), `R3-1: reduce 块覆盖 ${cls}`)
}
ok(/:focus-visible\s*\{[^}]*outline:/.test(css),
  'R3-2: :focus-visible 焦点环全局兜底 (AC-R3-2)')
ok(!/text-dim\/(50|60|70)\b/.test([app, chat, drawer, sched, css].join('\n')),
  'R3-3: text-dim/50|60|70 低对比度用法清零 (AC-R3-3)')
ok(/aria-labelledby=\{titleId\}/.test(ui) && /prevFocusRef/.test(ui),
  'R3-4: Modal aria-labelledby + 打开收焦/关闭回焦 (AC-R3-4)')
ok(/role="status" aria-live="polite"/.test(ui),
  'R3-5: Toast role=status aria-live=polite (AC-R3-5)')

// ── R3 P1 白名单 ─────────────────────────────────────────────────────
ok(!/transition-all/.test(css.replace(/\/\*[\s\S]*?\*\//g, '')),
  'R3-6: index.css 无 transition-all (.btn 显式列属性) (AC-R3-6)')
for (const [name, src] of [['App.jsx', app], ['TasksBoardView.jsx', tboard]]) {
  ok(!/transition-all/.test(src), `R3-6: ${name} 无 transition-all`)
}
ok(/active:scale-\[0\.97\]/.test(css),
  'R3-7: .btn 体系 :active scale(0.97) 按压反馈 (AC-R3-7)')
ok(!/animate-pulse/.test(tboard.match(/w-5 h-5 rounded-full border-\[1\.5px\][\s\S]*?\n\s*<\/span>/)?.[0] || 'x')
    || /w-1\.5 h-1\.5[^"]*animate-pulse/.test(tboard),
  'R3-8: 看板节点 animate-pulse 收敛为 6px 状态点 (AC-R3-8)')
ok(!/uppercase tracking/.test(tboard) && !/uppercase tracking/.test(sched)
    && !/uppercase/.test(css.match(/\.tbl thead th \{[^}]*\}/)?.[0] || 'uppercase'),
  'R3-9: 表头/FieldRow/ScheduleView 三处 uppercase tracked 清零 (AC-R3-9)')
const wcardBlock = css.slice(css.indexOf('.wcard-avatar-ph'), css.indexOf('.wcard-spin'))
ok(!/#[0-9a-fA-F]{3,8}\b/.test(wcardBlock),
  'R3-10: wcard-avatar 块无裸 hex (token 化) (AC-R3-10)')
ok(/\.badge-purple \{[^}]*violet/.test(css) && /--c-violet/.test(html),
  'R3-10: badge-purple 用真紫 token 与 amber 区分 (AC-R3-10)')
ok(/<svg[^>]*aria-hidden="true"/.test(tboard) && /<svg[^>]*aria-hidden="true"/.test(cron)
    && /<svg[^>]*aria-hidden="true"/.test(workers) && (chat.match(/<svg[^>]*aria-hidden="true"/g) || []).length >= 4,
  'R3-11: 走查点名装饰 svg 均 aria-hidden ×7 (AC-R3-11)')
ok(/scaleX\(\$\{pct \/ 100\}\)/.test(tboard) && !/style=\{\{ width: `\$\{pct\}%` \}\}/.test(tboard),
  'R3-12: 看板进度条 width 动画 -> transform scaleX (AC-R3-12)')
ok(!/aside[^>]*transition-all/.test(app) && !/bg-panel transition-all/.test(app),
  'R3-12: 侧栏折叠不再动画 width (AC-R3-12)')

// ── R4 · icon 统一 ───────────────────────────────────────────────────
ok(/icon: '(check|alert|dot|refresh|clock|ellipsis|circle|close)'/.test(tboard)
    && !/icon: '[✓!●↻⌛⋯○✕]'/.test(tboard),
  'R4-1: classify() 状态徽章图标 = SVG 键, 文字符号清零 (AC-R4-1)')
ok(!/[✕✓＋]/.test(ui.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')),
  'R4-1: ui.jsx 关闭钮无 ✕ (SVG close) (AC-R4-1)')
ok(!/>✕</.test(chat) && !/[✕＋] \{t\(/.test(wf) && !/>✕</.test(drawer),
  'R4-1: ChatView/WorkflowsView/FileDrawer 关闭/加号语义位符号清零 (AC-R4-1)')
ok(!/[📷👁💬📜ℹ️🐳🗂🚀⬆⬇⚠📄✎]/.test(workers)
    && !/💬/.test(chat) && !/💬/.test(app)
    && !/[⬆⬇✕⟳↗]/.test(drawer.replace(/\/\/.*$/gm, '')) && !/[⚠📄]/.test(preview),
  'R4-2: 走查点名 emoji 位清零 (WorkersView/ChatView/App/FileDrawer/FilePreview) (AC-R4-2)')
ok(/export function Icon\(/.test(icons) && /aria-hidden="true"/.test(icons)
    && /export const ICONS/.test(icons),
  'R4-3: 共享 Icon 组件默认 aria-hidden + ICONS path 表在位 (AC-R4-3)')
ok(/<Icon d=\{ICONS\.close\}/.test(ui) && /<Icon d=\{ICONS\.chat\}/.test(chat),
  'R4-4: 关闭/emoji 位替换为统一 1.8w stroke SVG 体系 (AC-R4-4)')
ok(/isDropped \? <StateGlyph icon="close" \/>[\s\S]{0,80}<StateGlyph icon="check" \/>/.test(tboard),
  'R4-5: 已完成列表 ✓/✕ 语义映射原样迁移 (done=check/dropped=close) (AC-R4-5)')

console.log(`\n${fail === 0 ? 'ALL PASS' : 'FAILED'} (${pass} passed, ${fail} failed)`)
process.exit(fail === 0 ? 0 : 1)
