import React, { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Chart, BarController, BarElement, CategoryScale, LinearScale,
  DoughnutController, ArcElement, Tooltip } from 'chart.js'
import { api } from '../api.js'
import { Icon } from '../components/icons.jsx'
import { Toast, Confirm } from '../components/ui.jsx'

// Chart.js v4 tree-shake (ARCH-BRIEF §3.2 A 选型 / main 裁决): 只注册用到的
// Controller/Element/Scale/Tooltip, 不引 chart.js/auto 全量。
Chart.register(BarController, BarElement, CategoryScale, LinearScale,
  DoughnutController, ArcElement, Tooltip)

// ════════════════════════════════════════════════════════════════════
// DashboardView — 驾驶舱首页 (dashboard-cockpit-20261105, DESIGN-UX Z0-Z3)
//
// 四层纵向叙事 (DESIGN-UX §0/§1): Z0 指挥条 → Z1 活跃 worker 放大区 →
// Z2 图表区 (token 柱图 + 收件箱环图) → Z3 明细卡区 (C2/C5 原样搬移)。
// 旧五卡信息零丢失 (R-1): C1→Z0 / C2→Z3 左 / C3→Z2 右 (环图+徽章行双呈现) /
// C4→Z1 吸收 (计数上移 Z0 右) / C5→Z3 右。CardFrame 全留。
//
// 数据源 (全部现存端点, 零后端改动):
//   Z0/C1 系统健康   api.heartbeat()     → /api/heartbeat
//                    api.scheduleState() → /api/schedule/state
//   Z3/C2 进行中的链 api.teamWorks()     → /api/team-works (默认 ACTIVE/HOLD)
//   Z2/C3 收件箱     api.scheduleList()  → /api/schedule/tasks (前端聚合计数)
//                    + 环形图 (aggregateInbox 五计数, 零新端点)
//   Z1/C4 worker     fetch('/api/workers/live') 组件内直连 —— api.js 无封装且
//                    api.js 不在白名单 (PLAN-X §3)
//                    fetch('/api/tasks?fields=active') 活跃口径 team_work_active 段
//                    fetch /api/workers 全量仅首屏一次 (名单+头像+在线计数;
//                    含 api_key 明文回显, 禁入 5s 轮询 — ARCH §5.5 载荷红线)
//   Z2/C5 最近收口   api.teamWorks(true) → /api/team-works?include_closed=1
//                    (同一响应喂 C5 卡与 token 柱图, 不重复请求)
//
// 轮询纪律 (PLAN-X §2.3 / AC-D8): 全组件**单一定时器**; 请求节奏恒为 5s 一轮
// (POLL_MS = 5000), document.hidden 时跳过, visibilitychange 恢复可见立即刷新,
// 卸载 clearInterval + removeEventListener。定时器本体以 1s 心跳驱动 Z0 倒计时
// 的本地每秒递减 (AC-D3 误差 ≤2s 的既定手段, PLAN-X §2.2 C1), 每满 5000ms 才
// 发一轮请求 —— 不新增第二处定时器 (§2.3 硬约束); 图表刷新搭现有 5s 节奏
// (chart.data 赋值 + chart.update(), canvas 不重挂, DESIGN-UX §3.2)。
//
// 每卡独立 {data, error, loading} (REQ §4.1): 某端点失败只塌该卡, 其余照常,
// 禁止整页白屏。
// ════════════════════════════════════════════════════════════════════

// 口径源 TasksBoardView.jsx:49-58 formatTokens — 勿改语义。
// [en] Local copy by explicit adjudication (PLAN-X §2.5): extracting a shared
//      module would violate the §3.2 whitelist and break the test_issue58_tokens
//      source anchor — do NOT "deduplicate" this.
function fmtTokens(n) {
  if (typeof n !== 'number' || !Number.isFinite(n) || n <= 0) return '0'
  if (n < 1000) return String(n)
  if (n < 1e6) return n.toLocaleString('en-US')
  if (n < 1e9) return (n / 1e6).toFixed(1) + 'M'
  return (n / 1e9).toFixed(1) + 'B'
}

// 口径源 TasksBoardView.jsx:58-62 formatCost — 勿改语义 (PLAN-X §2.5 同款局部实现裁决)。
function fmtCost(usd) {
  const v = (typeof usd === 'number' && Number.isFinite(usd)) ? usd : 0
  if (v > 0 && v < 0.01) return '<$0.01'
  return '$' + v.toFixed(2)
}

// D-2 (TEST-X-REPORT §4.2): token_total 是服务端 dict (mw/console.py `_aggregate_run_tokens`
// 五键结构), 不是 number。
// dashboard-ui-20261105 R2: C2 卡改版后不再展示 run 级 token 行 (UX-SPEC §2.2
// 卡结构 = 卡头/点条/当前行), 本文件 runTokenLabel 局部副本随之移除;
// TasksBoardView 口径源与 test_issue58 守护不受影响。

// 口径源 TasksBoardView.jsx:355-363 runStatus 类名口径 — 勿改语义 (同上裁决)。
// 只取 dashboard 用到的四态 + 兜底; 文案走 dashboard.* 词条。
function runBadge(s) {
  if (s === 'ACTIVE') return { cls: 'badge-blue', pulse: true }
  if (s === 'HOLD') return { cls: 'badge-yellow' }
  if (s === 'CLOSED') return { cls: 'badge-green' }
  if (s === 'ABORTED') return { cls: 'badge-red' }
  return { cls: 'badge-slate' }
}

// epoch 秒 → MM-DD HH:mm (同 ScheduleView formatSchedTime 口径, 本地化不建词条)
function fmtTime(epoch) {
  if (!epoch) return '—'
  const d = new Date(epoch * 1000)
  if (Number.isNaN(d.getTime())) return String(epoch)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

// ── dashboard-ui-20261105 R5: MM:SS 液晶格式 (UX-SPEC §3.2, 恒 5 字符) ──
function fmtMMSS(s) {
  const v = Math.max(0, Math.round(s))
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(Math.floor(v / 60))}:${pad(v % 60)}`
}

// dashboard-fix-20261105 R-C (main 裁决 OQ-2): worker 卡/行 token 数值行整体
// 移除, token 展示统一由 24h 柱状图承担 —— 上一轮的 fmtTokenShort 缩写口径
// 随 token 行一并移除 (柱图刻度沿用既有 fmtTokens)。

// ── dashboard-ui-20261105 R2: 链步骤序列 (UX-SPEC §2.2 四段语义) ──
// 顺序 = 当前→待执行→已完成→已放弃 (TODO.md 原序即链真实推进序)。
// sem = 后端 status_sem [family, token|自由文本, badgeCls] (issue #146 契约)。
export function todoSteps(todo) {
  const steps = []
  if (!todo) return steps
  if (todo.current) {
    steps.push({ kind: 'current', title: todo.current.title || '', sem: todo.current.status_sem || null })
  }
  for (const p of (Array.isArray(todo.pending) ? todo.pending : [])) {
    steps.push({ kind: 'pending', title: p.title || '', sem: p.status_sem || null })
  }
  for (const d of (Array.isArray(todo.done_lines) ? todo.done_lines : [])) {
    steps.push({ kind: 'done', title: d.title || d.raw || '' })
  }
  for (const d of (Array.isArray(todo.dropped_lines) ? todo.dropped_lines : [])) {
    steps.push({ kind: 'dropped', title: d.title || d.raw || '' })
  }
  return steps
}

// >12 步窗口折叠 (UX-SPEC §2.4): 当前点必在窗内 = 前3+当前+后4 (≤8 点);
// 头部已完成段折左 +N, 尾部待执行段折右 +N; 无当前 → 前 8 + 右 +N。
export function stepWindow(steps) {
  const n = steps.length
  if (n <= 12) return { left: 0, items: steps.map((s, i) => ({ s, i })), right: 0 }
  const ci = steps.findIndex((s) => s.kind === 'current')
  if (ci < 0) {
    return { left: 0, items: steps.slice(0, 8).map((s, i) => ({ s, i })), right: n - 8 }
  }
  const start = Math.max(0, ci - 3)
  const end = Math.min(n, ci + 5)
  return {
    left: start,
    items: steps.slice(start, end).map((s, i) => ({ s, i: i + start })),
    right: n - end,
  }
}

// 点样式四段语义 (UX-SPEC §2.2); 状态色只走 accent/ok/warn/bad token。
function stepDotCls(s) {
  if (s.kind === 'current') {
    const tok = s.sem && s.sem[1]
    if (tok === 'blocked') return 'bg-warn'
    if (tok === 'failed') return 'bg-bad'
    if (tok === 'completed') return 'bg-ok/70'
    return 'bg-accent ring-2 ring-accent/30 animate-pulse' // working/waiting_sub/未知
  }
  if (s.kind === 'pending') return 'bg-transparent border border-line'
  if (s.kind === 'done') return 'bg-ok/70'
  return 'bg-transparent border border-dashed border-dim/60' // dropped
}

// 口径源 WorkersView.jsx:124-128 avatarSrc — 勿 import 跨视图, 同款局部实现
// (F7 复用已有头像: 裸文件名 → /api/workers/<n>/avatar; 无/坏值 → null 走
// initials 占位, A7-2/A7-3)。
function avatarSrc(name, avatar) {
  if (!name || typeof avatar !== 'string') return null
  if (!avatar) return null
  if (avatar.startsWith('data:')) return null
  return `/api/workers/${encodeURIComponent(name)}/avatar`
}

// 占位符首字符 (口径源 WorkersView.jsx avatarInitial, 同款局部实现)
function avatarInitial(name) {
  const c = (name || '').trim()[0]
  return c && /[A-Za-z0-9]/.test(c) ? c.toUpperCase() : '?'
}

// C3 收件箱聚合 (纯函数, 供守护测试): pending/running/paused/hold 全量计数;
// done 仅计 COALESCE(finished_at, updated_at) >= now-24h 的行
// (字段实证 mw/schedule.py:89 finished_at 列, mark_done 写 finished_at)。
export function aggregateInbox(tasks, nowSec) {
  const c = { pending: 0, running: 0, paused: 0, hold: 0, done24h: 0 }
  const cutoff = nowSec - 24 * 3600
  for (const t of (Array.isArray(tasks) ? tasks : [])) {
    if (t.status === 'pending') c.pending++
    else if (t.status === 'running') c.running++
    else if (t.status === 'paused') c.paused++
    else if (t.status === 'hold') c.hold++
    else if (t.status === 'done') {
      const ts = (t.finished_at != null ? t.finished_at : t.updated_at) || 0
      if (ts >= cutoff) c.done24h++
    }
  }
  return c
}

// C5 最近收口 (纯函数): **先过滤** CLOSED/ABORTED (include_closed 响应含
// ACTIVE/HOLD 行, 其 closed_at 为 NULL — PLAN-X §2.8 AC-D7 修正), 再按
// closed_at 降序取 top5 (不足按实际)。
export function recentClosed(runs, top = 5) {
  return (Array.isArray(runs) ? runs : [])
    .filter((r) => r && (r.status === 'CLOSED' || r.status === 'ABORTED') && r.closed_at != null)
    .sort((a, b) => b.closed_at - a.closed_at)
    .slice(0, top)
}

// C4 活体去重 (纯函数): live[] 按 profile 去重 ⇒ 名单。
// CC轮 §4.1 (worker 永不回空闲修复): 仅计入 thinking===true 的条目 ——
// sse_bridge 终态事件把 thinking 置 False (sse_bridge.py:291), 完成即自动
// 回空闲区; 长工具调用空窗 thinking 保持 True ⇒ 不误伤。thinking!==true
// 一律不入活跃集 (验收口径: 终态后 ≤5s 回空闲, 故不加 ts 宽限窗 ——
// 终态事件同样刷新 last_event_ts, OR 窗会把回空闲推迟 120s)。
export function liveProfiles(live) {
  const seen = new Set()
  const out = []
  for (const w of (Array.isArray(live) ? live : [])) {
    const p = w && w.profile
    if (!p || seen.has(p)) continue
    if (w.thinking !== true) continue
    seen.add(p)
    out.push(p)
  }
  return out
}

// Z2 token 柱图数据 (纯函数, DESIGN-UX §3.2 / A4-1): 过滤 CLOSED/ABORTED →
// created_at DESC 前 10 (≥10 恰好 10 项, <10 按实际, 不补空柱)。
// source==='unavailable' ⇒ missing 语义 (A4-3: 不进数值轴, 渲染灰 stub+'—')。
export function recentTokenRuns(runs, top = 10) {
  return (Array.isArray(runs) ? runs : [])
    .filter((r) => r && (r.status === 'CLOSED' || r.status === 'ABORTED'))
    .sort((a, b) => (b.created_at || 0) - (a.created_at || 0))
    .slice(0, top)
    .map((r) => {
      const tt = r.token_total || {}
      const missing = tt.source === 'unavailable' || (!r.token_total && tt.source !== 'api' && tt.source !== 'partial')
      return {
        run_id: r.run_id,
        // R3 display_title 口径 (main 裁决: 各展示位换 display_title 兜底 initial_task)
        name: r.display_title || r.initial_task || '—',
        missing,
        partial: tt.source === 'partial',
        tokens: missing ? null : (tt.total_tokens || 0),
        cost: missing ? null : (tt.estimated_cost_usd || 0),
        captured: tt.captured || 0,
        missingCards: tt.missing || 0,
      }
    })
}

// dashboard-fix-20261105 R-C: worker 24h token 柱图数据 (纯函数, AC-C1/C2)。
// 输入 = /api/workers/token-usage 的 profile→usage map; source==='none' 或
// total_tokens 非数值 ⇒ missing 语义 (灰虚线空心 stub + '—', 绝不画 0,
// 复用 missingStubPlugin 先例)。排序 = profile 字典序 (端点本已稳定, 双保险)。
export function workerTokenItems(usageMap) {
  if (!usageMap) return []
  return Object.values(usageMap)
    .filter((w) => w && w.profile)
    .sort((a, b) => a.profile.localeCompare(b.profile))
    .map((w) => {
      const missing = w.source === 'none' || typeof w.total_tokens !== 'number'
      return {
        name: w.profile,
        missing,
        partial: false,
        tokens: missing ? null : w.total_tokens,
        cost: null, // worker 口径无成本换算 (REQ R-C Out: 不做金额), tooltip 只显 tokens
      }
    })
}

// Z1 活跃口径 (纯函数, OQ-5 落地 / DESIGN-UX §2.2): 活跃 = team_work_active
// 段当前卡承接 worker ∪ /api/workers/live live[] profile。返回 [{name, viaRun}]
// (viaRun=true 表示有进行中 run 的卡 ⇒ 状态文案「执行中」+ 卡数; 否则 SSE 活体)。
export function activeWorkers(twa, live) {
  const out = []
  const seen = new Set()
  const assignee = twa && twa.current_card_assignee
  if (assignee && !seen.has(assignee)) {
    seen.add(assignee)
    out.push({ name: assignee, viaRun: true, cardCount: twa.card_count || 0 })
  }
  for (const p of liveProfiles(live)) {
    if (seen.has(p)) continue
    seen.add(p)
    out.push({ name: p, viaRun: false, cardCount: 0 })
  }
  return out
}

// ── 图表取色 (DESIGN-UX §3.1): Chart.js 不认 CSS 变量, 构建/主题切换时读一次
// getComputedStyle 的 --c-* 三元组拼 rgb(r g b / a)。主题切换由 MutationObserver
// 监听 html class 触发重取色 + chart.update('none') —— 不重挂 canvas, 不新增定时器。
function chartTheme() {
  const cs = getComputedStyle(document.documentElement)
  const v = (name) => (cs.getPropertyValue(name) || '0 0 0').trim()
  const rgb = (name, a) => (a == null ? `rgb(${v(name)})` : `rgb(${v(name)} / ${a})`)
  return {
    accent: rgb('--c-accent', 0.85), accentSolid: rgb('--c-accent'),
    line: rgb('--c-line', 0.5), lineSoft: rgb('--c-line', 0.8),
    dim: rgb('--c-dim'), dimSoft: rgb('--c-dim', 0.6),
    ok: rgb('--c-ok'), warn: rgb('--c-warn'), violet: rgb('--c-violet'),
    panel: rgb('--c-panel'), panel2: rgb('--c-panel2'), ink: rgb('--c-ink'),
  }
}

// 主题切换监听 (单实例引用计数: 两图共用一个 observer)
let themeSubs = new Set()
let themeMo = null
function subscribeChartTheme(fn) {
  themeSubs.add(fn)
  if (!themeMo) {
    themeMo = new MutationObserver(() => {
      const th = chartTheme()
      for (const f of themeSubs) f(th)
    })
    themeMo.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
  }
  return () => {
    themeSubs.delete(fn)
    if (themeSubs.size === 0 && themeMo) { themeMo.disconnect(); themeMo = null }
  }
}

// ── 卡壳: 标题 + 点击跳转 + 每卡独立 loading/error 占位 (REQ §4.1 / R-1 全留) ──
function CardFrame({ title, onOpen, loading, error, onRetry, children }) {
  const { t } = useTranslation()
  return (
    <section className="bg-panel border border-line rounded-lg p-4 flex flex-col min-h-[180px]">
      <div className="flex items-center justify-between gap-2 mb-3">
        <h2 className="text-sm font-semibold text-ink">{title}</h2>
        {onOpen && (
          <button onClick={onOpen} className="btn-ghost btn-sm shrink-0" title={title}>→</button>
        )}
      </div>
      {loading && !error ? (
        <div className="flex-1 space-y-2" aria-busy="true">
          <div className="h-4 rounded bg-line animate-pulse" />
          <div className="h-4 rounded bg-line animate-pulse w-3/4" />
          <div className="h-4 rounded bg-line animate-pulse w-1/2" />
          <p className="text-[11px] text-dim">{t('dashboard.loading')}</p>
        </div>
      ) : error ? (
        <div className="flex-1 flex flex-col items-start justify-center gap-2">
          <p className="text-xs text-dim">{t('dashboard.error')}</p>
          <button onClick={onRetry} className="btn-ghost btn-sm">{t('dashboard.retry')}</button>
        </div>
      ) : children}
    </section>
  )
}

// 分区铭牌 (DESIGN-UX §1.4): 区标题左侧 3px×14px accent 竖条 —— 驾驶舱
// "分区铭牌"感, 全站唯一新增装饰元素。
function ZoneTitle({ children, right }) {
  return (
    <div className="flex items-center justify-between gap-3 mb-3">
      <h2 className="text-sm font-semibold text-ink border-l-[3px] border-accent pl-2">{children}</h2>
      {right && <div className="text-xs text-dim font-mono tabular-nums truncate">{right}</div>}
    </div>
  )
}

// 骨架占位 (loading 态复用)
function SkeletonLines() {
  const { t } = useTranslation()
  return (
    <div className="flex-1 space-y-2" aria-busy="true">
      <div className="h-4 rounded bg-line animate-pulse" />
      <div className="h-4 rounded bg-line animate-pulse w-3/4" />
      <p className="text-[11px] text-dim">{t('dashboard.loading')}</p>
    </div>
  )
}

// worker 头像 (F7 / A7-1~3): 复用 .wcard-avatar-ph 占位样式 (WorkersView 同款
// 口径的局部实现), 404/无 avatar → initials 占位, 无破图无 console error。
function WorkerAvatar({ name, avatar, size = 64, ring = false }) {
  const [broken, setBroken] = useState(false)
  useEffect(() => { setBroken(false) }, [avatar])
  const src = avatarSrc(name, avatar)
  return (
    <span
      className={`inline-block rounded-full overflow-hidden shrink-0 ${ring ? 'ring-2 ring-ok/60' : ''}`}
      style={{ width: size, height: size }}
    >
      {src && !broken ? (
        <img src={src} alt="" onError={() => setBroken(true)}
          className="w-full h-full object-cover block" />
      ) : (
        <span className="wcard-avatar-ph" style={{ fontSize: Math.round(size * 0.42) }}>
          {avatarInitial(name)}
        </span>
      )}
    </span>
  )
}

// ── dashboard-fix-20261105 R-B: 工作中 worker 大卡 (REQ R-B 区1, 红线 L3) ──
// 三要素: 较大卡片 (沿用 260px 量级) + 动效边框 (.wcard-live 呼吸, index.css,
// prefers-reduced-motion 全局降级) + 当前 run 标签 (OQ-1 裁决: 经
// team_work_active 命中显示当前 run display_title, 仅 SSE 活体无 run 不显示)。
// token 数值行已移除 (OQ-2 裁决 / AC-C3, token 展示统一由 24h 柱状图承担)。
// 模块级定义 (issue #207 同款纪律: 5s 轮询重渲染不重挂组件身份)。
function WorkerRunCard({ name, avatar, cardCount, runTitle, onChat, t }) {
  return (
    <div className="wcard-live w-[260px] bg-panel border border-line rounded-2xl p-4
                    flex flex-col gap-3">
      <div className="flex items-center gap-3 min-w-0">
        <WorkerAvatar name={name} avatar={avatar} size={64} ring />
        <div className="min-w-0 flex-1">
          <div className="text-[15px] font-semibold text-ink truncate">{name}</div>
          <div className="flex items-center gap-1.5 mt-1 text-xs text-dim min-w-0">
            <span className="wcard-gfx wcard-gfx-working shrink-0" aria-hidden="true" />
            <span className="badge-blue shrink-0">{t('dashboard.c3_running')}</span>
            {cardCount > 0 && (
              <span className="truncate">· {t('dashboard.c2_cards', { n: cardCount })}</span>
            )}
          </div>
        </div>
      </div>
      {runTitle && (
        <div className="flex items-center gap-1.5 min-w-0 text-[11px]">
          <span className="badge-blue shrink-0">{t('dashboard.r4_run_label')}</span>
          <span className="text-dim truncate" title={runTitle}>{runTitle}</span>
        </div>
      )}
      <button onClick={() => onChat && onChat(name)}
        className="btn btn-accent btn-sm w-full mt-auto">
        {t('dashboard.enter_chat')}
      </button>
    </div>
  )
}

// ── dashboard-fix-20261105 R-B: 空闲 worker 单行 (REQ R-B 区2, 红线 L4) ──
// 每 worker 严格一行: 小头像 (24px, 明显小于工作卡 64px) + 名字 + **仅一个
// 对话按钮** (AC-B3 DOM 逐行按钮数=1); 无 token 行、无状态徽章、无卡片边框
// (AC-B4)。模块级定义, 同上纪律。
function IdleWorkerRow({ name, avatar, onChat, t }) {
  return (
    <div className="flex items-center gap-2.5 h-9 min-w-0">
      <WorkerAvatar name={name} avatar={avatar} size={24} />
      <span className="text-[13px] text-ink truncate flex-1 min-w-0" title={name}>{name}</span>
      <button onClick={() => onChat && onChat(name)}
        className="btn-ghost btn-sm shrink-0">
        {t('dashboard.enter_chat')}
      </button>
    </div>
  )
}

// ── Z2 左: token 柱图 (F4 / A4-1~4, Chart.js BarController) ──────────
// 缺失项 (A4-3 硬条款): data 置 null 不进数值轴, afterDatasetsDraw 插件在
// 该 x 位画灰虚线空心 stub (高 16px, dash [4,3]) + 上方 '—'; 悬停 stub 由
// onHover 最近桶位判定显示「无 token 数据」浮层 (tooltip 等价交互)。
const missingStubPlugin = {
  id: 'missingStub',
  afterDatasetsDraw(chart) {
    const missing = chart.$missing
    if (!Array.isArray(missing) || missing.length === 0) return
    const th = chart.$theme || chartTheme()
    const { ctx, chartArea, scales } = chart
    ctx.save()
    ctx.setLineDash([4, 3])
    ctx.lineWidth = 1.5
    ctx.strokeStyle = th.dimSoft
    ctx.fillStyle = th.dim
    ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, monospace'
    ctx.textAlign = 'center'
    for (const i of missing) {
      const x = scales.x.getPixelForValue(i)
      const y2 = chartArea.bottom
      ctx.strokeRect(x - 12, y2 - 16, 24, 16)
      ctx.fillText('—', x, y2 - 22)
    }
    ctx.restore()
  },
}

function applyBarTheme(chart, th) {
  chart.$theme = th
  const o = chart.options
  o.scales.x.grid.color = th.line
  o.scales.x.ticks.color = th.dim
  o.scales.y.grid.color = th.line
  o.scales.y.ticks.color = th.dim
  o.plugins.tooltip.backgroundColor = th.panel2
  o.plugins.tooltip.titleColor = th.ink
  o.plugins.tooltip.bodyColor = th.ink
  o.plugins.tooltip.borderColor = th.lineSoft
  chart.data.datasets[0].backgroundColor = th.accent
  chart.data.datasets[0].hoverBackgroundColor = th.accentSolid
}

// NIT-1 (REVIEW dashboard-cockpit-20261105): 柱图数据三要素抽纯函数, 创建 effect 与
// 数据 effect 共用 —— 语言切换销毁重建时创建即用 itemsRef.current 初始化 labels/data/
// $missing, 杜绝「重建后 items 引用未变、数据 effect 不重跑」的空窗。
function barDataOf(items) {
  return {
    labels: items.map((it) => (it.name.length > 8 ? `${it.name.slice(0, 8)}…` : it.name)),
    data: items.map((it) => (it.missing ? null : it.tokens)),
    missing: items.map((it, i) => (it.missing ? i : -1)).filter((i) => i >= 0),
  }
}

function TokenBarChart({ items, ariaLabel }) {
  const { t } = useTranslation()
  const canvasRef = useRef(null)
  const chartRef = useRef(null)
  const itemsRef = useRef(items)
  const [stubTip, setStubTip] = useState(null) // {x,y} 缺失 stub 悬停浮层
  itemsRef.current = items

  // 创建一次 (canvas 不随数据重挂, 单定时器纪律下 5s 只 update)
  useEffect(() => {
    if (!canvasRef.current) return undefined
    const th = chartTheme()
    const init = barDataOf(itemsRef.current || [])
    const chart = new Chart(canvasRef.current, {
      type: 'bar',
      data: {
        labels: init.labels,
        datasets: [{ data: init.data, borderRadius: 4, maxBarThickness: 48, borderWidth: 0 }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 300 },
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: false } },
          y: {
            beginAtZero: true,
            grid: {},
            ticks: { count: 5, callback: (v) => fmtTokens(v) },
          },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            borderWidth: 1, padding: 8, displayColors: false,
            callbacks: {
              title: (els) => (itemsRef.current[els[0] && els[0].dataIndex] || {}).name || '',
              label: (el) => {
                const it = itemsRef.current[el.dataIndex]
                if (!it || it.missing) return t('dashboard.chart_no_data')
                // dashboard-fix-20261105 R-C: worker 柱图 items 无 cost
                // (cost==null) ⇒ 只显 tokens, 不显 '$0.00' 误导金额。
                const base = it.cost == null
                  ? `${fmtTokens(it.tokens)} tokens`
                  : `${fmtTokens(it.tokens)} tokens · ${fmtCost(it.cost)}`
                return it.partial
                  ? `${base} (${it.captured}/${it.captured + it.missingCards})`
                  : base
              },
            },
          },
        },
        // 缺失 stub 悬停: 取最近桶位, 命中 missing ⇒ 浮层「无 token 数据」
        onHover: (evt, _els, chart) => {
          const native = evt && evt.native
          if (!native || !chart.scales.x) { setStubTip(null); return }
          const idx = Math.round(chart.scales.x.getValueForPixel(native.offsetX))
          const miss = chart.$missing || []
          if (idx >= 0 && idx < itemsRef.current.length && miss.includes(idx)) {
            setStubTip({ x: native.offsetX, y: native.offsetY })
          } else {
            setStubTip(null)
          }
        },
      },
      plugins: [missingStubPlugin],
    })
    chart.$missing = init.missing
    applyBarTheme(chart, th)
    chartRef.current = chart
    const unsub = subscribeChartTheme((nth) => { applyBarTheme(chart, nth); chart.update('none') })
    return () => { unsub(); chart.destroy(); chartRef.current = null }
  }, [t])

  // 数据刷新搭 5s 轮询 (chart.update, 不重挂)
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const d = barDataOf(items)
    chart.data.labels = d.labels
    chart.data.datasets[0].data = d.data
    chart.$missing = d.missing
    chart.update()
  }, [items])

  return (
    <div className="relative flex-1 min-h-[200px]" onMouseLeave={() => setStubTip(null)}>
      <canvas ref={canvasRef} role="img" aria-label={ariaLabel || t('dashboard.z2_token_title')} />
      {stubTip && (
        <div className="absolute z-10 pointer-events-none rounded-md border border-line bg-panel2
                      px-2 py-1 text-[11px] text-dim whitespace-nowrap"
          style={{ left: Math.min(stubTip.x + 8, 220), top: Math.max(stubTip.y - 28, 0) }}>
          {t('dashboard.chart_no_data')}
        </div>
      )}
    </div>
  )
}

// ── Z2 右: 收件箱环形图 (OQ-6 首批纳入, DESIGN-UX §3.3) ──────────────
// 数据 = C3 aggregateInbox 五计数 (零新端点); 扇区色序与页面 badge 语义对齐;
// 全 0 画单圈灰环 + 中心 '0' 不隐藏图 (布局稳定); 图例 = 原 C3 徽章行 (R-1)。
function InboxDonutChart({ inbox }) {
  const { t } = useTranslation()
  const canvasRef = useRef(null)
  const chartRef = useRef(null)
  const inboxRef = useRef(inbox)
  inboxRef.current = inbox
  const total = inbox.pending + inbox.running + inbox.paused + inbox.hold + inbox.done24h

  useEffect(() => {
    if (!canvasRef.current) return undefined
    const th = chartTheme()
    const colorsOf = (nth) => {
      const cur = inboxRef.current
      const sum = cur.pending + cur.running + cur.paused + cur.hold + cur.done24h
      return sum > 0
        ? [nth.accentSolid, nth.ok, nth.dim, nth.warn, nth.violet]
        : [nth.lineSoft]
    }
    const dataOf = () => {
      const cur = inboxRef.current
      const sum = cur.pending + cur.running + cur.paused + cur.hold + cur.done24h
      return sum > 0 ? [cur.pending, cur.running, cur.paused, cur.hold, cur.done24h] : [1]
    }
    const chart = new Chart(canvasRef.current, {
      type: 'doughnut',
      data: {
        labels: [t('dashboard.c3_pending'), t('dashboard.c3_running'), t('dashboard.c3_paused'),
          t('dashboard.c3_hold'), t('dashboard.c3_done_24h')],
        datasets: [{ data: dataOf(), borderWidth: 2, borderColor: th.panel, hoverOffset: 4 }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '68%',
        animation: { duration: 300 },
        plugins: {
          legend: { display: false },
          tooltip: {
            borderWidth: 1, padding: 8,
            filter: (item) => {
              const cur = inboxRef.current
              return (cur.pending + cur.running + cur.paused + cur.hold + cur.done24h) > 0
            },
            callbacks: { label: (el) => ` ${el.label} ${el.parsed}` },
          },
        },
      },
    })
    const paint = (nth) => {
      chart.$theme = nth
      chart.data.datasets[0].backgroundColor = colorsOf(nth)
      chart.data.datasets[0].borderColor = nth.panel
      chart.options.plugins.tooltip.backgroundColor = nth.panel2
      chart.options.plugins.tooltip.titleColor = nth.ink
      chart.options.plugins.tooltip.bodyColor = nth.ink
      chart.options.plugins.tooltip.borderColor = nth.lineSoft
    }
    paint(th)
    chartRef.current = chart
    const unsub = subscribeChartTheme((nth) => { paint(nth); chart.update('none') })
    return () => { unsub(); chart.destroy(); chartRef.current = null }
  }, [t])

  // 计数变化搭 5s 轮询刷新
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const sum = total
    chart.data.datasets[0].data = sum > 0
      ? [inbox.pending, inbox.running, inbox.paused, inbox.hold, inbox.done24h]
      : [1]
    const th = chart.$theme || chartTheme()
    chart.data.datasets[0].backgroundColor = sum > 0
      ? [th.accentSolid, th.ok, th.dim, th.warn, th.violet]
      : [th.lineSoft]
    chart.update()
  }, [inbox, total])

  return (
    <div className="relative flex-1 min-h-[200px] flex items-center justify-center">
      <canvas ref={canvasRef} role="img" aria-label={t('dashboard.z2_inbox_title')} />
      <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
        <span className="font-mono text-2xl tabular-nums text-ink leading-none">{total}</span>
        <span className="text-[11px] text-dim mt-1">{t('dashboard.chart_total')}</span>
      </div>
    </div>
  )
}

export default function DashboardView({ onNavigate, onWorkerChat }) {
  const { t, i18n } = useTranslation()
  const [c1, setC1] = useState({ hb: null, st: null, loading: true, error: false })
  const [c2, setC2] = useState({ data: null, loading: true, error: false })
  const [c3, setC3] = useState({ data: null, loading: true, error: false })
  const [c4, setC4] = useState({ data: null, loading: true, error: false })
  const [c5, setC5] = useState({ data: null, loading: true, error: false })
  // Z1 活跃口径第二路: /api/tasks?fields=active 的 team_work_active 段 (轻量 ~1KB)
  const [c6, setC6] = useState({ data: null, loading: true, error: false })
  // dashboard-ui-20261105 R2: 各链 TODO 步骤数据 run_id → { todo, stale }
  // (GET /api/team-work/<run>/todo, 与任务计划弹窗同源, fail-soft: 单链失败
  // 只降级该卡, 有旧数据打 stale 标记继续展示, 无缓存走占位, AC-R2-5)。
  const [c2todos, setC2todos] = useState({})
  // dashboard-fix-20261105 R-C: worker 近 24h token (GET /api/workers/token-usage,
  // impl-be 625df18 交付) 升级为独立柱状图数据源 (REQ R-C): data = profile →
  // {total_tokens, source} map; error 态供图表卡错误占位 + 重试 (AC-C5),
  // 失败只塌图表卡不阻塞其他区块 (REQ §4.1 每卡独立口径)。
  const [tokenUsage, setTokenUsage] = useState({ data: null, loading: true, error: false })
  // dashboard-ui-20261105 R5: 立即开始二次确认 + 请求态 + Toast; 归零 2s
  // 本地表现 (UX-SPEC §3.3, 前端本地驱动不等轮询)。
  const [confirmRun, setConfirmRun] = useState(false)
  const [runNowPending, setRunNowPending] = useState(false)
  const [toast, setToast] = useState(null)
  const [zeroFired, setZeroFired] = useState(false)
  // worker 名单 + 头像 + 在线计数: /api/workers 全量**仅首屏一次** (载荷红线:
  // 含 api_key 明文回显, 禁入 5s 轮询 — ARCH §5.5); null = 未回, 静默降级
  // (头像走 initials 占位, 计数 '—'), 失败不塌 Z1。
  const [registry, setRegistry] = useState(null)
  // 倒计时本地每秒递减的心跳 (AC-D3): 显示值 = ends_at - nowMs, 每轮 5s 轮询
  // 用服务端 snapshot 的 ends_at 校准 (PLAN-X §2.2 C1)。
  const [nowMs, setNowMs] = useState(() => Date.now())

  // ── 每路独立 catch, 互不阻塞 (PLAN-X §2.3) ──
  const refreshC1 = () => {
    api.heartbeat()
      .then((d) => setC1((s) => ({ ...s, hb: d, loading: false, error: false })))
      .catch(() => setC1((s) => ({ ...s, hb: null }))) // 灯降级 unknown 灰, 不算卡级错
    api.scheduleState()
      .then((d) => setC1((s) => ({ ...s, st: d, loading: false, error: false })))
      .catch(() => setC1((s) => ({ ...s, st: null })))
  }
  const refreshC2 = () => {
    api.teamWorks()
      .then((d) => {
        const runs = d?.team_works || []
        setC2({ data: runs, loading: false, error: false })
        // R2 (UX-SPEC §2.1): 每 ACTIVE 链追加拉 todo 步骤 (与弹窗同源端点,
        // 搭 refreshAll 单定时器节奏, AC-R2-8 不另起定时器)。teamWorkTodo
        // 自身 fail-soft 不抛; 单链失败只降级该卡 (保留旧数据打 stale)。
        for (const r of runs) {
          api.teamWorkTodo(r.run_id).then((td) => {
            setC2todos((prev) => {
              if (td && td.ok && td.exists && td.todo) {
                return { ...prev, [r.run_id]: { todo: td.todo, stale: false } }
              }
              const old = prev[r.run_id]
              return old ? { ...prev, [r.run_id]: { ...old, stale: true } } : prev
            })
          })
        }
      })
      .catch(() => setC2((s) => ({ ...s, loading: false, error: true })))
  }
  const refreshC3 = () => {
    api.scheduleList()
      .then((d) => setC3({ data: d?.tasks || [], loading: false, error: false }))
      .catch(() => setC3((s) => ({ ...s, loading: false, error: true })))
  }
  const refreshC4 = () => {
    fetch('/api/workers/live')
      // REVIEW-X M-2: 同款 j(r) 错误处理 (api.js:25-37) — 非 2xx 必须 throw 落
      // catch 进错误态, 否则 4xx/5xx+合法 JSON 会静默降级为空态
      .then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json() })
      .then((d) => setC4({ data: d || { live: [] }, loading: false, error: false }))
      .catch(() => setC4((s) => ({ ...s, loading: false, error: true })))
  }
  const refreshC5 = () => {
    // 存原始 runs 数组: 同一响应喂 C5 卡 (recentClosed) 与 Z2 token 柱图
    // (recentTokenRuns), 不重复请求。
    api.teamWorks(true)
      .then((d) => setC5({ data: d?.team_works || [], loading: false, error: false }))
      .catch(() => setC5((s) => ({ ...s, loading: false, error: true })))
  }
  const refreshC6 = () => {
    // 活跃口径 run 侧 (OQ-5): team_work_active 段; 失败静默降级 (Z1 仍以
    // live[] 判活跃), 不塌 Z1 卡。
    fetch('/api/tasks?fields=active')
      .then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json() })
      .then((d) => setC6({ data: d || null, loading: false, error: false }))
      .catch(() => setC6((s) => ({ ...s, loading: false, error: true })))
  }
  const refreshC7 = () => {
    // R-C (dashboard-fix-20261105): 24h worker token 柱图源; 端点失败 →
    // error 态, 图表卡显示错误占位 + 重试 (AC-C5), 不阻塞其他区块。
    fetch('/api/workers/token-usage')
      .then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json() })
      .then((d) => {
        const m = {}
        for (const w of (d && Array.isArray(d.workers) ? d.workers : [])) {
          if (w && w.profile) m[w.profile] = w
        }
        setTokenUsage({ data: m, loading: false, error: false })
      })
      .catch(() => setTokenUsage((s) => ({ ...s, loading: false, error: true })))
  }
  const refreshAll = () => {
    refreshC1(); refreshC2(); refreshC3(); refreshC4(); refreshC5(); refreshC6(); refreshC7()
  }

  useEffect(() => {
    const POLL_MS = 5000
    let lastPoll = Date.now()
    const tick = () => {
      setNowMs(Date.now())
      if (document.hidden) return // 隐藏即跳过 (AC-D8)
      if (Date.now() - lastPoll >= POLL_MS) {
        lastPoll = Date.now()
        refreshAll()
      }
    }
    refreshAll() // 挂载立即一次
    // 名单/头像首屏一次 (非定时器, 不进 refreshAll — 载荷红线)
    fetch('/api/workers')
      .then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json() })
      .then((d) => setRegistry(d?.workers || {}))
      .catch(() => setRegistry({}))
    const timer = setInterval(tick, 1000)
    // REVIEW-X P-2: 恢复刷新同步 lastPoll, 防恢复后 ≤1s 内 tick 双发一轮
    const onVis = () => { if (!document.hidden) { lastPoll = Date.now(); refreshAll() } }
    document.addEventListener('visibilitychange', onVis)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [])

  // ── Z0/C1 派生 ──
  const reaper = c1.hb == null ? 'unknown' : (c1.hb.reaper_alive === true ? 'alive' : 'dead')
  // REVIEW-X M-1: 与顶栏 App.jsx:245 完全同款 dot-ok/dot-bad/dot-dim
  // (bg-green/bg-red/bg-slate 系不存在的类, dist 无规则 ⇒ 透明点)
  const reaperDot = reaper === 'alive' ? 'dot-ok' : reaper === 'dead' ? 'dot-bad' : 'dot-dim'
  const reaperText = reaper === 'alive' ? t('dashboard.c1_reaper_alive')
    : reaper === 'dead' ? t('dashboard.c1_reaper_dead') : t('dashboard.c1_reaper_unknown')
  const cd = c1.st?.countdown || null
  const cdRemaining = cd && cd.active && cd.ends_at != null
    ? Math.max(0, Math.round(cd.ends_at - nowMs / 1000))
    : null

  // ── R5 倒计时模块派生 (UX-SPEC §3.2/§3.3) ──
  const cdActive = cdRemaining != null
  // 进度条分母: snapshot total_s (impl-be 625df18 只增键), 常量 300 兜底 (§3.2)
  const cdTotalS = (cd && typeof cd.total_s === 'number' && cd.total_s > 0) ? cd.total_s : 300
  // 归零瞬间 2s 本地表现 (§3.3): 前端本地状态驱动不等轮询, 2s 后以服务端 snapshot 为准。
  // REVIEW S-2: zeroFired 复位独立于 cdActive —— 2s 窗口内若轮询先回 idle
  // (cdActive 转 false), 定时器不清除不复位, 00:00 pulse 恒满 2s 后才复位。
  // zeroTimerRef 兼作去重闩: 窗口内 effect 重跑 (deps 变化) 不重复起定时器。
  const zeroTimerRef = useRef(null)
  useEffect(() => {
    if (cdActive && cdRemaining === 0 && zeroTimerRef.current == null) {
      setZeroFired(true)
      zeroTimerRef.current = setTimeout(() => {
        zeroTimerRef.current = null
        setZeroFired(false)
      }, 2000)
    }
    return undefined
  }, [cdActive, cdRemaining])
  // 卸载兜底: 清掉仍在跑的 2s 定时器 (防 unmount 后 setState)
  useEffect(() => () => {
    if (zeroTimerRef.current) clearTimeout(zeroTimerRef.current)
  }, [])
  const cdDisplay = cdActive ? fmtMMSS(cdRemaining) : (zeroFired ? '00:00' : '--:--')
  // 递减语义 (剩余占比); 归零跳满 100% bg-ok (「任务已发射」反差语义)
  const cdBarPct = zeroFired ? 100
    : cdActive ? Math.min(100, Math.max(0, (cdRemaining / cdTotalS) * 100))
    : 0
  // 立即开始可点条件 = 后端 _condition_ok 同源口径 (ANALYSIS 裁决 2):
  // countdown.active && busy_reason==null && auto_execute
  const runNowEnabled = !!(cdActive && c1.st && c1.st.busy_reason == null && c1.st.auto_execute)
  const runNowHint = !cdActive ? t('dashboard.r5_run_now_off_idle')
    : (c1.st && c1.st.busy_reason != null) ? t('schedule.busy_hint')
    : !(c1.st && c1.st.auto_execute) ? t('dashboard.r5_run_now_off_auto')
    : t('dashboard.r5_run_now')
  const doRunNow = async () => {
    setConfirmRun(false)
    setRunNowPending(true)
    try {
      await api.scheduleRunNow()
      setToast({ msg: t('dashboard.r5_run_now_ok'), type: 'ok' })
    } catch (e) {
      // 409: 后端 error 字段文案直接透出 (api.js j() 已提取 body.error, 已按
      // _RUN_NOW_ERRORS 本地化), 无假成功 (AC-R5-11)
      setToast({ msg: String(e?.message || e), type: 'bad' })
    } finally {
      setRunNowPending(false)
      refreshC1() // 成功/失败都回拉 snapshot, ≤5s 内以服务端态渲染 (AC-R5-10/12)
    }
  }

  // R2 状态词 (AC-R2-3 硬约束): 只复用 tboard.todo_status_* 词表; status_sem[1]
  // 未命中词表 (fallback 自由文本, issue #146) 原样透传, 不新造同义词。
  const statusWord = (sem) => {
    const tok = sem && sem[1]
    if (typeof tok !== 'string' || !tok) return '—'
    const key = `tboard.todo_status_${tok}`
    return i18n.exists(key) ? t(key) : tok
  }
  const statusBadge = (sem) => (sem && typeof sem[2] === 'string' && sem[2]) || 'badge-slate'

  // ── Z2/C3 派生 ──
  const inbox = c3.data ? aggregateInbox(c3.data, Date.now() / 1000) : null

  // ── Z2 token 柱图派生 (C5 同源) ──
  const tokenItems = useMemo(() => recentTokenRuns(c5.data), [c5.data])
  const closedList = useMemo(() => recentClosed(c5.data), [c5.data])

  // ── Z1 派生 (OQ-5: team_work_active ∪ live[]) ──
  const twa = c6.data ? c6.data.team_work_active : null
  const actives = useMemo(() => activeWorkers(twa, c4.data && c4.data.live), [twa, c4.data])
  const activeNames = new Set(actives.map((w) => w.name))
  const registryList = registry
    ? Object.entries(registry).map(([name, e]) => ({
        name, avatar: (e && e.avatar) || '', online: !!(e && e.online),
      }))
    : []
  // R4 两态映射 (main 裁决 4, 硬口径): live[]/team_work_active 命中 = 工作中;
  // registry online 且不在上集合 = 空闲; offline/缺席 = 不渲染 (AC-R4-2)。
  // 排序 = 工作中在前、空闲在后, 各自 name 字典序 (稳定, AC-R4-5)。
  const workingWorkers = [...actives].sort((a, b) => a.name.localeCompare(b.name))
  const idleWorkers = registryList
    .filter((w) => w.online && !activeNames.has(w.name))
    .sort((a, b) => a.name.localeCompare(b.name))
  // dashboard-fix-20261105 R-B (main 裁决 OQ-1): 当前 run 标签 = team_work_active
  // 命中 run 的 display_title (兜底 initial_task); 仅 SSE 活体无 run 的 worker
  // (viaRun=false) 不显示 run 标签。
  const runTitleOf = twa ? (twa.display_title || twa.initial_task || null) : null
  // dashboard-fix-20261105 R-C: worker 24h token 柱图 items (AC-C1/C2,
  // source=none → missing stub, 绝不画 0)。
  const workerTokenChartItems = useMemo(() => workerTokenItems(tokenUsage.data), [tokenUsage.data])
  const onlineCount = registry ? registryList.filter((w) => w.online).length : null
  const runCount = (c4.data && Array.isArray(c4.data.active_run_ids)) ? c4.data.active_run_ids.length : 0
  const avatarOf = (name) => (registry && registry[name] && registry[name].avatar) || ''

  return (
    <div className="flex flex-col gap-4 max-w-[1600px] mx-auto">
      {/* ══ Z0 指挥条 (C1 吸收; CC轮 §4.2 已移除快速添加任务入口, 全局入口
          保留在 App header 核心功能按钮; R5 升级 h-[92px]
          双行指挥区: LCD 倒计时为视觉主体 + 下一任务 + 立即开始 + 进度条,
          UX-SPEC §3。模块高度恒 92px, 任何态 (loading/idle/归零) 不塌陷跳动) ══ */}
      <section className="bg-panel border border-line rounded-xl px-4 h-[92px] flex items-center gap-4">
        <div className="flex items-center gap-4 min-w-0 flex-1">
          {/* 左列: reaper 灯 + auto 徽章 (原样保留, 上下两层) */}
          <div className="flex flex-col gap-1.5 shrink-0">
            {c1.loading && !c1.error ? (
              <>
                <span className="h-3.5 w-28 rounded bg-line animate-pulse shrink-0" />
                <span className="h-3.5 w-20 rounded bg-line animate-pulse shrink-0" />
              </>
            ) : c1.error ? (
              <>
                <p className="text-xs text-dim">{t('dashboard.error')}</p>
                <button onClick={refreshC1} className="btn-ghost btn-sm">{t('dashboard.retry')}</button>
              </>
            ) : (
              <>
                <div className="flex items-center gap-2">
                  <span className={reaperDot} />
                  <span className="text-xs text-ink whitespace-nowrap">{reaperText}</span>
                </div>
                {c1.st == null ? <span className="text-dim text-xs">—</span> : (
                  <span className={c1.st.auto_execute ? 'badge-green' : 'badge-slate'}>
                    {c1.st.auto_execute ? t('dashboard.c1_auto_on') : t('dashboard.c1_auto_off')}
                  </span>
                )}
              </>
            )}
          </div>
          {/* 倒计时模块: [LCD 数字] [右列: 下一任务行 + 进度条] (§3.1) */}
          <div className="flex items-center gap-4 min-w-0">
            <div className="inline-flex items-center justify-center rounded-lg border border-line
                            bg-canvas px-3 py-1.5 min-w-[7.5ch] shrink-0"
                 style={{ boxShadow: 'inset 0 1px 4px rgb(var(--c-scrim) / 0.25)' }}>
              <span className={`font-mono tabular-nums font-bold text-[34px] leading-none tracking-[0.08em]
                                ${cdActive || zeroFired ? 'text-accent' : 'text-dim'}
                                ${zeroFired ? 'animate-pulse' : ''}`}
                    style={cdActive || zeroFired
                      ? { textShadow: '0 0 10px rgb(var(--c-accent) / 0.45)' } : undefined}>
                {cdDisplay}
              </span>
            </div>
            <div className="flex flex-col gap-2 min-w-0">
              <div className="flex items-center gap-2 min-w-0">
                {cdActive ? (
                  <>
                    <span className="text-[11px] text-dim shrink-0">{t('dashboard.r5_next_task')}</span>
                    <span className="text-xs text-ink truncate max-w-[360px]"
                          title={cd.next_task_title || ''}>
                      {cd.next_task_title || '—'}
                    </span>
                  </>
                ) : (
                  <span className="text-xs text-dim truncate">{t('dashboard.c1_countdown_idle')}</span>
                )}
                {/* 立即开始 (§3.5): 置灰 disabled 原生无副作用 + title 原因短句;
                    点击二次确认 (复用 schedule.confirm 词条) → pending 防重复提交 */}
                <button onClick={() => setConfirmRun(true)}
                  disabled={!runNowEnabled || runNowPending}
                  title={runNowEnabled && !runNowPending ? t('dashboard.r5_run_now') : runNowHint}
                  className={`btn btn-primary btn-sm shrink-0 ${(!runNowEnabled || runNowPending) ? 'opacity-50 cursor-not-allowed' : ''}`}>
                  {runNowPending && (
                    <Icon d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
                          className="w-3.5 h-3.5 animate-spin" aria-hidden="true" />
                  )}
                  {t('dashboard.r5_run_now')}
                </button>
              </div>
              <div className="h-1.5 w-full max-w-[280px] rounded-full bg-panel2 overflow-hidden">
                <div className={`h-full rounded-full transition-[width] duration-1000 ease-linear ${zeroFired ? 'bg-ok' : 'bg-accent'}`}
                     style={{ width: `${cdBarPct}%` }} />
              </div>
            </div>
          </div>
        </div>
        <div className="ml-auto flex items-center gap-3 shrink-0">
          {/* C4 计数上移 (DESIGN §1.1): {n} 个在线 · {m} run */}
          <span className="text-xs text-dim font-mono tabular-nums whitespace-nowrap hidden sm:inline">
            {onlineCount == null ? '—' : t('dashboard.c4_count', { n: onlineCount })} · {runCount} {t('dashboard.z0_runs_unit')}
          </span>
        </div>
        {/* R5 立即开始二次确认 (与 ScheduleView 收件箱 TAB 同款组件与词条) */}
        <Confirm open={confirmRun}
          title={t('schedule.confirm.title')}
          okText={t('schedule.btn.run_now')}
          danger={false}
          text={t('schedule.confirm.body', { title: (cd && cd.next_task_title) || '—' })}
          onOk={doRunNow}
          onCancel={() => setConfirmRun(false)} />
        <Toast msg={toast?.msg} type={toast?.type || 'ok'} onDone={() => setToast(null)} />
      </section>

      {/* ══ Z1 worker 两区 (dashboard-fix-20261105 R-B, 红线 L3/L4):
          工作中 = 大卡片 + 动效边框 + 当前 run 标签; 空闲 = 单行列表
          (小头像 + 名字 + 仅对话按钮)。两态判定口径不回退 (main 裁决 4):
          live[]/team_work_active 命中 = 工作中; registry online 且不在上
          集合 = 空闲; 离线不显示 (AC-B5)。各区独立空态占位, 区不塌陷。 ══ */}
      <section className="flex flex-col min-h-[180px]">
        <ZoneTitle>{t('dashboard.z1_title')}</ZoneTitle>
        {c4.loading && !c4.error ? (
          <SkeletonLines />
        ) : c4.error ? (
          <div className="flex flex-col items-start justify-center gap-2 py-6">
            <p className="text-xs text-dim">{t('dashboard.error')}</p>
            <button onClick={refreshC4} className="btn-ghost btn-sm">{t('dashboard.retry')}</button>
          </div>
        ) : (
          <div className="flex flex-col gap-4">
            {/* ── 区1: 工作中 (大卡片网格, 动效边框) ── */}
            <div className="flex flex-col gap-3">
              <h3 className="text-xs font-semibold text-dim border-l-[3px] border-accent pl-2">
                {t('dashboard.z1_working_title')}
              </h3>
              {workingWorkers.length === 0 ? (
                <div className="flex items-center justify-center min-h-[72px] text-xs text-dim
                                border border-dashed border-line rounded-xl">
                  {t('dashboard.z1_empty')}
                </div>
              ) : (
                <div className="flex flex-wrap gap-4">
                  {workingWorkers.map((w) => (
                    <WorkerRunCard key={w.name} t={t}
                      name={w.name} avatar={avatarOf(w.name)}
                      cardCount={w.viaRun ? w.cardCount : 0}
                      runTitle={w.viaRun ? runTitleOf : null} onChat={onWorkerChat} />
                  ))}
                </div>
              )}
            </div>
            {/* ── 区2: 空闲 (单行列表; ≥8 worker 纵向滚动不溢出, AC-B6) ── */}
            <div className="flex flex-col gap-1.5">
              <h3 className="text-xs font-semibold text-dim border-l-[3px] border-line pl-2">
                {t('dashboard.r4_idle')}
              </h3>
              {idleWorkers.length === 0 ? (
                <div className="flex items-center justify-center min-h-[48px] text-xs text-dim
                                border border-dashed border-line rounded-xl">
                  {t('dashboard.z1_idle_empty')}
                </div>
              ) : (
                <div className="flex flex-col gap-1 max-h-[320px] overflow-auto pr-1">
                  {idleWorkers.map((w) => (
                    <IdleWorkerRow key={w.name} t={t}
                      name={w.name} avatar={w.avatar} onChat={onWorkerChat} />
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </section>

      {/* ══ Z2 图表区 (F1/F4 + dashboard-fix-20261105 R-C): run 级 token 柱图
          (既有 Z2 保留, main 裁决 OQ-3) + worker 24h token 独立柱状图 (红线 L5,
          独立新增成图) + 收件箱环图 ══ */}
      <div className="grid grid-cols-1 xl:grid-cols-[1fr_1fr_420px] gap-4 items-stretch">
        <CardFrame title={t('dashboard.z2_token_title')} loading={c5.loading} error={c5.error} onRetry={refreshC5}>
          {tokenItems.length === 0 ? (
            /* A4-4: 空数据空态 (复用 c5_empty 文案), 不渲染 canvas */
            <div className="flex-1 flex items-center justify-center min-h-[160px] text-xs text-dim">
              {t('dashboard.c5_empty')}
            </div>
          ) : (
            <TokenBarChart items={tokenItems} ariaLabel={t('dashboard.z2_token_title')} />
          )}
        </CardFrame>
        {/* dashboard-fix-20261105 R-C: worker 24h token 独立柱状图 (红线 L5)。
            x=worker(profile), y=近24h token 总量; source=none → 灰虚线 stub+'—'
            不画 0 (AC-C2); 端点失败 → 错误占位 + 重试, 不阻塞其他区块 (AC-C5);
            数据搭 refreshAll 单定时器 5s 节奏, 不另起定时器 (REQ §4 轮询纪律)。 */}
        <CardFrame title={t('dashboard.z2_worker_token_title')}
          loading={tokenUsage.loading} error={tokenUsage.error} onRetry={refreshC7}>
          {workerTokenChartItems.length === 0 ? (
            <div className="flex-1 flex items-center justify-center min-h-[160px] text-xs text-dim">
              {t('dashboard.chart_no_data')}
            </div>
          ) : (
            <TokenBarChart items={workerTokenChartItems} ariaLabel={t('dashboard.z2_worker_token_title')} />
          )}
        </CardFrame>
        <CardFrame title={t('dashboard.z2_inbox_title')} loading={c3.loading} error={c3.error}
          onRetry={refreshC3} onOpen={() => onNavigate && onNavigate('schedule')}>
          {inbox && (
            <>
              <InboxDonutChart inbox={inbox} />
              {/* 原 C3 徽章行保留 (R-1 双呈现): 环图例即徽章色 */}
              <div className="flex flex-wrap gap-2 justify-center text-xs">
                <span className="badge-blue">{t('dashboard.c3_pending')} {inbox.pending}</span>
                <span className="badge-green">{t('dashboard.c3_running')} {inbox.running}</span>
                <span className="badge-slate">{t('dashboard.c3_paused')} {inbox.paused}</span>
                <span className={inbox.hold > 0 ? 'badge-yellow' : 'badge-slate'}>
                  {t('dashboard.c3_hold')} {inbox.hold}
                </span>
                <span className="badge-slate">{t('dashboard.c3_done_24h')} {inbox.done24h}</span>
              </div>
              {inbox.hold > 0 && <p className="text-dim text-xs mt-2 text-center">{t('dashboard.c3_hold_warn')}</p>}
            </>
          )}
        </CardFrame>
      </div>

      {/* ══ Z3 明细卡区 (C2 / C5 整卡原样搬移, R-1) ══ */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
        {/* ── C2 进行中的链 (R2 卡片化, UX-SPEC §2.2: 卡头 + 步骤点条 + 当前任务行) ── */}
        <CardFrame title={t('dashboard.c2_title')} loading={c2.loading} error={c2.error}
          onRetry={refreshC2} onOpen={() => onNavigate && onNavigate('tasks-board')}>
          <div className="flex flex-col gap-2 text-xs overflow-auto max-h-[420px]">
            {c2.data && c2.data.length === 0 && <p className="text-dim">{t('dashboard.c2_empty')}</p>}
            {(c2.data || []).map((r) => {
              const b = runBadge(r.status)
              // R3 口径: display_title 兜底 initial_task (impl-be 625df18 只增键)
              const runTitle = r.display_title || r.initial_task || '—'
              const entry = c2todos[r.run_id]
              const todo = entry && entry.todo
              const steps = todo ? todoSteps(todo) : null
              const win = steps ? stepWindow(steps) : null
              const doneN = steps ? steps.filter((s) => s.kind === 'done').length : 0
              const stepWord = (s) => s.kind === 'current' ? statusWord(s.sem)
                : s.kind === 'pending' ? t('dashboard.c2_section_pending')
                : s.kind === 'done' ? t('dashboard.c2_section_done')
                : t('dashboard.c2_section_dropped')
              return (
                <button key={r.run_id} onClick={() => onNavigate && onNavigate('tasks-board')}
                  className="text-left border border-line rounded-xl p-3 flex flex-col gap-2 w-full
                             hover:bg-canvas hover:border-accent/45 transition-[border-color] duration-150">
                  {/* 卡头: 状态徽章 + 标题 (单行截断, 全文在 title) + 卡数 */}
                  <div className="flex items-center gap-2 min-w-0">
                    <span className={`${b.cls} shrink-0`}>{r.status}</span>
                    <span className="truncate text-ink" title={r.initial_task || ''}>{runTitle}</span>
                    <span className="ml-auto shrink-0 text-[11px] text-dim font-mono tabular-nums">
                      {t('dashboard.c2_cards', { n: r.card_count ?? 0 })}
                    </span>
                  </div>
                  {steps && win ? (
                    /* 步骤点条 (§2.2/§2.4): 每步一点, >12 步窗口折叠 +N chip */
                    <div className="flex items-center gap-1.5 flex-nowrap overflow-hidden">
                      {win.left > 0 && (
                        <span className="text-[10px] text-dim font-mono px-1 border border-line rounded shrink-0">+{win.left}</span>
                      )}
                      {win.items.map(({ s, i }) => (
                        <span key={i} title={`${i + 1}. ${s.title} · ${stepWord(s)}`}
                          className={`w-2 h-2 rounded-full shrink-0 ${stepDotCls(s)}`} />
                      ))}
                      {win.right > 0 && (
                        <span className="text-[10px] text-dim font-mono px-1 border border-line rounded shrink-0">+{win.right}</span>
                      )}
                      <span className="ml-auto shrink-0 text-[11px] text-dim font-mono tabular-nums">
                        {doneN}/{steps.length}
                      </span>
                    </div>
                  ) : (
                    /* 降级卡 (§2.2): todo 失败/exists=false 只降级该卡, 无错误图标不阻塞他卡 */
                    <div className="text-[11px] text-dim">{t('dashboard.c2_todo_unavailable')}</div>
                  )}
                  {todo ? (
                    /* 当前任务行: 「当前:」+ 标题 + 状态徽章 (词表 tboard.todo_status_*) */
                    <div className="flex items-center gap-2 text-xs min-w-0">
                      <span className="text-dim shrink-0">{t('dashboard.c2_current_prefix')}</span>
                      {todo.current ? (
                        <>
                          <span className="text-ink truncate" title={todo.current.title || ''}>
                            {todo.current.title || '—'}
                          </span>
                          <span className={`${statusBadge(todo.current.status_sem)} shrink-0`}>
                            {statusWord(todo.current.status_sem)}
                          </span>
                        </>
                      ) : (
                        <>
                          <span className="text-ink">—</span>
                          <span className="badge-slate shrink-0">—</span>
                        </>
                      )}
                    </div>
                  ) : (
                    /* 降级卡保留原行级信息 (current_card 摘要) */
                    <div className="text-dim">
                      {r.current_card
                        ? t('dashboard.c2_current_card', {
                            assignee: r.current_card.assignee || '—',
                            status: r.current_card.harvest_status || '—',
                          })
                        : '—'}
                    </div>
                  )}
                </button>
              )
            })}
          </div>
        </CardFrame>

        {/* ── C5 最近收口 ── */}
        <CardFrame title={t('dashboard.c5_title')} loading={c5.loading} error={c5.error}
          onRetry={refreshC5} onOpen={() => onNavigate && onNavigate('tasks-board')}>
          <div className="flex flex-col gap-2 text-xs overflow-auto max-h-64">
            {closedList && closedList.length === 0 && <p className="text-dim">{t('dashboard.c5_empty')}</p>}
            {(closedList || []).map((r) => {
              const b = runBadge(r.status)
              return (
                <button key={r.run_id} onClick={() => onNavigate && onNavigate('tasks-board')}
                  className="text-left border border-line rounded-md p-2 hover:bg-canvas flex items-center gap-2">
                  <span className={`${b.cls} shrink-0`}>
                    {r.status === 'ABORTED' ? t('dashboard.c5_aborted') : t('dashboard.c5_closed')}
                  </span>
                  <span className="truncate flex-1 text-ink" title={r.initial_task || ''}>
                    {r.display_title || r.initial_task || '—'}
                  </span>
                  <span className="text-dim shrink-0">{fmtTime(r.closed_at)}</span>
                </button>
              )
            })}
          </div>
        </CardFrame>
      </div>
    </div>
  )
}
