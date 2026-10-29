import React, { useEffect, useState, useCallback, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from './api.js'
import ChatView from './views/ChatView.jsx'
import TasksBoardView from './views/TasksBoardView.jsx'
import WorkersView from './views/WorkersView.jsx'
import WorkflowsView from './views/WorkflowsView.jsx'
import CronView from './views/CronView.jsx'
import PixelOfficeView from './views/PixelOfficeView.jsx'
import SettingsView from './views/SettingsView.jsx'
import LogoMark from './components/LogoMark.jsx'
import { BRAND_EN, BRAND_ZH } from './brand.js'

// [en] NAV icons + ids are protocol-shaped; the human-readable label is
//      sourced from t('nav.*') so each language picks the right short word.
//      Pixel Office stays conditional on pixelStatus.enabled (interaction-
//      design §3.1 — never show a disabled feature).
//      2026-10-15 re-ordered per R3.1 — tasks-board (multi-agent collaboration
//      outcomes) takes the lead slot; chat moves to 2nd; tail unchanged.
// [zh-cn] NAV 的 id 与 SVG 是协议层,label 走 t('nav.*'),Pixel Office 仍
//        按 pixelStatus.enabled 条件显示(交互设计 §3.1)。2026-10-15 按 R3.1
//        调整顺序 — 任务看板(多智能体协作成果主视图)置顶,对话次位,尾部不变。
const NAV = [
  // 2026-10-06 改版:合并原「任务看板」(扁平 task 列表) +「Team Works」
  // 为新版「任务看板」(run→task 树状结构)。Team Works 概念不再单独 tab 暴露。
  // [en] Tasks board (folded runs into tasks tree). i18n label picks short word.
  // [zh-cn] 任务看板 (run→task 树)。i18n label 取短词。
  { id: 'tasks-board', icon: 'M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4' },
  { id: 'chat',        icon: 'M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.86 9.86 0 01-4-.8L3 21l1.8-4.2A7.9 7.9 0 013 12c0-4.418 4.03-8 9-8s9 3.582 9 8z' },
  { id: 'workers',     icon: 'M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4' },
  { id: 'workflows',   icon: 'M4 6h16M4 12h16M4 18h7m-7 0l3-3m-3 3l3 3M17 15l2 2 4-4' },
  // 定时任务 (s4 / t_da1c3a64): 时钟图标 (heroicons outline clock),
  // 位置按交互设计 §1.1 —— workflows 之后、pixel-office 之前
  // (定时任务与 Workflow 同属「自动化编排」心智组)。
  { id: 'cron',        icon: 'M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z' },
  // 像素办公室 tab 仅在启用时显示(pixelStatus.enabled), 未启用不占导航位。
  { id: 'pixel-office', icon: 'M4 4h16v12H4zM4 20h16M9 8h.01M12 8h.01M15 8h.01M8 12h8', dynamic: true },
  { id: 'settings',    icon: 'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z M15 12a3 3 0 11-6 0 3 3 0 016 0z' },
]
// NAV label i18n key table — keyed by id, resolved by t() per render.
// Sorted by NAV order so adding a tab only touches this table + NAV list.
const NAV_LABEL_KEYS = {
  'tasks-board': 'nav.tasks_board',
  'chat':        'nav.chat',
  'workers':     'nav.workers',
  'workflows':   'nav.workflows',
  'cron':        'nav.cron',
  'pixel-office':'nav.pixel_office',
  'settings':    'nav.settings',
}

function Icon({ d, className = 'w-5 h-5' }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
         strokeLinecap="round" strokeLinejoin="round" className={className}>
      <path d={d} />
    </svg>
  )
}

// 手机屏检测: <768px 视为移动端, 布局切换为上下排版
function useIsMobile() {
  const [isMobile, setIsMobile] = useState(
    () => window.matchMedia('(max-width: 767px)').matches)
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 767px)')
    const onChange = (e) => setIsMobile(e.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])
  return isMobile
}

export default function App() {
  // R1.2: 取 i18n 对象以便用 .language.startsWith('zh') 判定是否渲染中文副标题。
  // 语言运行时切换(zh↔en)走 i18next.changeLanguage → React 组件重渲,无需额外 effect。
  const { t, i18n } = useTranslation()
  const isZh = (i18n.language || '').startsWith('zh')
  // 2026-10-15 R3.4: 首屏落地改为 tasks-board(多智能体协作成果主视图);协作发起
  // 由全局「核心功能」按钮在任意视图 1 击可达,不再依赖 chat 作首页兜底。
  const [view, setView] = useState('tasks-board')
  const [collapsed, setCollapsed] = useState(false)
  const [reaper, setReaper] = useState(null)
  const [counts, setCounts] = useState({ attention: 0, running: 0, finished: 0, total: 0 })
  const [theme, setTheme] = useState(() => localStorage.getItem('sas-theme') || 'dark')
  const [webuiUrl, setWebuiUrl] = useState('')
  // 像素办公室是否启用(控制 tab 显隐); 与设置页开关联动。
  const [pixelEnabled, setPixelEnabled] = useState(false)
  // ── 多对话 tab 并存(路径1): 数组里第一项恒为 'main', 后跟若干 worker 名。
  //    切换 = 改 view;关闭 worker tab = 从数组移除。
  //    所有 ChatView 实例始终 mount, 仅激活的 display 非 none,
  //    切走时 sessionId / 输入框 / 附件 / 滚动位置全部保留。
  const [chatTabs, setChatTabs] = useState(() => {
    try {
      const saved = JSON.parse(localStorage.getItem('sas-chat-tabs') || 'null')
      if (Array.isArray(saved) && saved.length > 0 && saved[0] === 'main') return saved
    } catch { /* ignore */ }
    return ['main']
  })
  const [activeChat, setActiveChat] = useState('main')

  // 2026-10-15 R2.3a: 全局「核心功能」按钮需要触发 main ChatView 的 openTeamWork。
  // chatTabs 任意一项都 mount 一个 ChatView 实例;只有 tab='main' 的实例负责
  // 弹窗(useImperativeHandle 在 ChatView 内部已暴露)。维护 chatTabs.length
  // 大小的 ref 数组,位置与 chatTabs 一一对应。
  const chatViewRefs = useRef([])

  // hermes-webui 跳转地址(设置页配置, 非空才显示入口)
  // issue #75 B2: 依赖 [view] → [] — 这两个是会话级静态配置, 每次切视图
  // 重复拉属重复请求 (§2 闸门表); 保存设置成功后由 onSaved 回调单独刷新。
  useEffect(() => {
    api.settings().then((d) => setWebuiUrl((d.settings?.hermes_webui_url || '').trim())).catch(() => {})
    api.pixelStatus().then((d) => setPixelEnabled(!!d.enabled)).catch(() => setPixelEnabled(false))
  }, [])

  const openWorkerChat = (name) => {
    setChatTabs((tabs) => tabs.includes(name) ? tabs : [...tabs, name])
    setActiveChat(name); setView('chat')
  }
  const closeWorkerChat = (name) => {
    setChatTabs((tabs) => {
      const next = tabs.filter((t) => t !== name)
      // 至少保留 main tab; 移除后若激活的就是被关闭的, 退回 main
      if (next.length === 0) next.push('main')
      return next
    })
    setActiveChat((cur) => cur === name ? 'main' : cur)
    // 如果当前 view 是 chat 且没有任何 worker tab 了, 退回默认的 chat 视图(main)
    if (name === activeChat) setView('chat')
  }
  const openWebui = () => webuiUrl && window.open(webuiUrl, '_blank', 'noopener')
  // 2026-10-15 R2.3a: 全局「核心功能」按钮点击 = (1) 切回 main 对话 +
  // (2) 触发 main ChatView 的 openTeamWork() 打开协作启动弹窗。
  // R2.6: 全局按钮恒可点击,不感知 busy。
  const openCoreFeatures = () => {
    setActiveChat('main')
    setView('chat')
    // chatTabs 第一项必为 'main'(持久化/初始化的契约)。当前 view 已是
    // chat 时可直接触发;从其它视图切回时, 因非激活 tab 祖先 div 处于
    // `hidden` (display:none), 挂在它上面的 fixed Modal 也跟着不可见,
    // 必须先等 React 完成 commit、切到 chat 并激活 main tab 之后, 再
    // 调用 openTeamWork —— setTimeout(0) 把回调推到下一个宏任务,
    // 此时 commit 后的 DOM 已生效, Modal 能正确锚到可见容器里。
    const mainRef = chatViewRefs.current[0]
    if (!mainRef || typeof mainRef.openTeamWork !== 'function') return
    if (view === 'chat' && activeChat === 'main') {
      mainRef.openTeamWork()
    } else {
      // 切视图 + 激活 main tab 之后再调用, 避免 Modal 锚到 hidden tab。
      setTimeout(() => mainRef.openTeamWork(), 0)
    }
  }
  const WebuiBtn = ({ compact = false }) => webuiUrl ? (
    <button onClick={openWebui} className="btn-ghost btn-sm shrink-0" title={webuiUrl}>
      <Icon d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6m4-3v6m-1 9l5-5m0 0l-5-5m5 5H10" className="w-4 h-4" />
      {!compact && 'hermes-webui'}
    </button>
  ) : null
  // 2026-10-15 R2.1/R2.2: 全局「核心功能」按钮 —— 桌面主 header 动作组最左
  // btn-primary 高亮,移动端顶栏紧凑态(图标-only),文案走 t(),tooltip 复用
  // chat.session.start_teamwork_btn 保证点击预期可发现。
  const CoreFeaturesBtn = ({ compact = false }) => (
    <button onClick={openCoreFeatures}
      className={compact ? 'btn-primary btn-sm shrink-0 !px-2' : 'btn-primary btn-sm shrink-0'}
      title={t('chat.session.start_teamwork_btn')}>
      <Icon d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2M9 11a4 4 0 100-8 4 4 0 000 8zm14 10v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75"
            className="w-3.5 h-3.5" />
      {!compact && t('chat.session.start_teamwork_btn')}
    </button>
  )

  useEffect(() => {
    document.documentElement.className = theme
    localStorage.setItem('sas-theme', theme)
  }, [theme])

  // chatTabs 持久化: 刷新后保留已打开的 worker 对话
  useEffect(() => {
    localStorage.setItem('sas-chat-tabs', JSON.stringify(chatTabs))
  }, [chatTabs])

  // 顶栏状态轮询 — issue #75 B2 (§2/§6.3): 拆为两路
  //   ① heartbeat (5s, 恒开): 只拉数百字节存活信号, 不再拉 workers 全量
  //   ② counts (tasks 投影 ~29KB): 挂载 + 切视图 + 子视图 onChanged 时拉,
  //      不再进 5s 定时器 (R1 裁决: workers 732KB 移出首屏; tasks 瘦身后
  //      投影值仅作顶栏计数/红点数据源)
  // 页面不可见(切后台/切标签)时跳过, 回到前台立即补一次, 避免无谓轮询
  const pollHeartbeat = useCallback(async () => {
    if (document.hidden) return
    try {
      setReaper(await api.heartbeat())
    } catch { /* 心跳失败保留上一态; 三态灯按 unknown 处理 */ }
  }, [])

  const pollCounts = useCallback(async () => {
    if (document.hidden) return
    try {
      const t = await api.tasks()
      const c = { attention: 0, running: 0, finished: 0, total: (t.tasks || []).length }
      for (const x of t.tasks || []) {
        if (x.status === 'done' || x.status === 'archived') c.finished++
        else if (x.harvest_status === 'NEED_CLARIFICATION' ||
                 (x.status === 'blocked' && x.block_kind === 'needs_input') ||
                 x.status === 'blocked') c.attention++
        else c.running++
      }
      setCounts(c)
    } catch { /* 后端不可达时静默, 视图内自有错误态 */ }
  }, [])

  // 子视图 onChanged 统一入口: 心跳 + 计数各补一次
  const pollStatus = useCallback(() => { pollHeartbeat(); pollCounts() },
    [pollHeartbeat, pollCounts])

  // issue #75 §2: 视图激活时补拉一次计数 (顶栏红点/副标题数据源),
  // 取代旧「5s 定时器无脑拉 tasks 全量」——切视图即刷新, 不常驻轮询。
  // issue #75 s6 缺陷 2 (b): 首屏 tasks 全量【只由本 effect 负责一次】
  // (挂载时 view 已定, 本 effect 天然覆盖首拉; 旧代码挂载 effect 里再调
  // 一次 pollCounts 属重复请求, 已移除)。TasksBoardView 侧的 5s 轮询走
  // 轻量端点 api.tasksActive(), 不再拖全量。
  useEffect(() => { pollCounts() }, [view, pollCounts])

  useEffect(() => {
    pollHeartbeat()
    const t = setInterval(pollHeartbeat, 5000)
    const onVis = () => { if (!document.hidden) { pollHeartbeat(); pollCounts() } }
    document.addEventListener('visibilitychange', onVis)
    return () => { clearInterval(t); document.removeEventListener('visibilitychange', onVis) }
  }, [pollHeartbeat, pollCounts])

  // issue #75 §3 顶栏三态: true→dot-ok / false→dot-bad / undefined→dot-dim
  // (改造前 undefined 走 falsy 分支 → 心跳回来之前闪红点, NFR-4 违例)
  const alive = reaper?.reaper_alive
  const aliveDot = alive === true ? 'dot-ok' : alive === false ? 'dot-bad' : 'dot-dim'
  const isMobile = useIsMobile()

  // ── 手机: 上下排版, 侧边栏变顶部 TAB ──
  if (isMobile) {
    return (
      <div className="flex flex-col h-[100dvh] overflow-hidden bg-canvas text-ink font-sans">
        {/* 顶栏: 品牌 + 状态 + 主题 */}
        <header className="shrink-0 flex items-center gap-2 px-3 h-12 border-b border-line bg-panel">
          <LogoMark size={24} />
          <span className="text-[13px] font-bold truncate">{BRAND_EN}</span>
          <span className={`ml-auto shrink-0 ${aliveDot}`}
                title={alive === undefined ? t('app.reaper_unknown') : alive ? t('app.reaper_online') : t('app.reaper_offline')} />
          {/* R2.2: 移动端顶栏同步新增紧凑态「核心功能」按钮(图标-only) */}
          <CoreFeaturesBtn compact />
          <WebuiBtn compact />
          <button onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
            className="btn-ghost btn-sm shrink-0 !px-2" title={t('app.theme_toggle')}>
            <Icon d={theme === 'dark'
              ? 'M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z'
              : 'M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z'}
              className="w-4 h-4" />
          </button>
        </header>

        {/* 顶部 TAB 栏 */}
        <nav className="shrink-0 flex border-b border-line bg-panel overflow-x-auto">
          {NAV.filter((n) => !n.dynamic || pixelEnabled).map((n) => {
            const active = view === n.id && activeChat === 'main'
            const navKey = NAV_LABEL_KEYS[n.id]
            const navLabel = navKey ? t(navKey) : ''
            return (
              <button key={n.id}
                onClick={() => { setActiveChat('main'); setView(n.id) }}
                className={`flex-1 flex flex-col items-center gap-0.5 py-2 relative
                           text-[11px] font-medium transition-colors shrink-0 min-w-[72px]
                           ${active ? 'text-accent' : 'text-dim'}`}
                title={navLabel}>
                <Icon d={n.icon} className="w-[18px] h-[18px]" />
                <span className="truncate max-w-[88px]">{navLabel}</span>
                {n.id === 'tasks-board' && counts.attention > 0 && (
                  <span className="absolute top-1 right-[calc(50% - 18px)] min-w-[16px] h-4 px-1
                                 rounded-full bg-bad text-white text-[9.5px] leading-4
                                 text-center shadow-[0_0_6px_rgb(var(--c-bad))]">
                    {counts.attention}
                  </span>
                )}
                {active && (
                  <span className="absolute bottom-0 left-3 right-3 h-0.5 rounded-full bg-accent" />
                )}
              </button>
            )
          })}
          {/* worker 对话 tab: 跳过 'main', 因为 main 已经由 "对话" NAV 项承担 */}
          {chatTabs.filter((t) => t !== 'main').map((name) => {
            const active = view === 'chat' && activeChat === name
            const chatTitle = t('app.chat_tab_title', { name })
            return (
              <button key={name}
                onClick={() => { setActiveChat(name); setView('chat') }}
                className={`flex-1 flex flex-col items-center gap-0.5 py-2 relative
                           text-[11px] font-medium transition-colors shrink-0 min-w-[80px] max-w-[120px]
                           ${active ? 'text-accent' : 'text-dim'}`}
                title={chatTitle}>
                <Icon d="M21 11.5a8.38 8.38 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.38 8.38 0 01-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.38 8.38 0 013.8-.9h.5a8.48 8.48 0 018 8v.5z"
                      className="w-[18px] h-[18px]" />
                <span className="truncate max-w-[100px]" title={name}>{name}</span>
                {/* 关闭按钮统一由 ChatView 内部 "✕ 关闭" 按钮触发, tab 这里不重复 */}
                {active && (
                  <span className="absolute bottom-0 left-3 right-3 h-0.5 rounded-full bg-accent" />
                )}
              </button>
            )
          })}
        </nav>

        {/* 主区 */}
        <main className="flex-1 min-h-0 flex flex-col">
          <div className={`flex-1 min-h-0 overflow-auto p-3 ${view === 'chat' ? 'flex flex-col' : ''}`}>
            {/* chatTabs 里每个 tab 都 mount 自己的 ChatView 实例(独立 sessionId/输入框/附件),
                切走时 React 保留 state, 切回去原封不动. 仅激活的显示, 其余 display:none */}
            {chatTabs.map((tab, idx) => {
              const active = view === 'chat' && activeChat === tab
              const base = tab === 'main' ? '' : `/api/w/${tab}`
              const label = tab === 'main' ? t('app.main_agent') : t('app.worker_chat_title', { name: tab })
              return (
                <div key={tab} className={`flex-1 min-h-0 flex flex-col ${active ? '' : 'hidden'}`}>
                  <ChatView ref={(el) => { chatViewRefs.current[idx] = el }} base={base} targetLabel={label}
                    active={active}
                    onClose={tab === 'main' ? null : () => closeWorkerChat(tab)} />
                </div>
              )
            })}
            {view !== 'chat' && (
              <>
                {view === 'tasks-board' && <TasksBoardView onChanged={pollStatus} />}
                {view === 'workers' && <WorkersView onChanged={pollStatus} onChat={openWorkerChat} />}
                {view === 'workflows' && <WorkflowsView onChanged={pollStatus} />}
                {view === 'cron' && <CronView />}
                {view === 'pixel-office' && pixelEnabled && <PixelOfficeView onNavigate={setView} />}
                {view === 'settings' && <SettingsView onSaved={(st) => { setWebuiUrl((st?.hermes_webui_url || '').trim()); api.pixelStatus().then((d) => setPixelEnabled(!!d.enabled)).catch(() => {}) }} />}
              </>
            )}
          </div>
        </main>
      </div>
    )
  }

  // ── 桌面: 左右排版(原布局) ──
  return (
    <div className="flex h-screen overflow-hidden bg-canvas text-ink font-sans">
      {/* ── 侧边栏 (LangUI sidebar: slate-900 底, 细右边框) ── */}
      <aside className={`${collapsed ? 'w-[68px]' : 'w-60'} shrink-0 flex flex-col border-r border-line
                       bg-panel transition-all duration-200`}>
        <div className="flex items-center gap-2.5 px-4 h-16 border-b border-line">
          {/* 品牌位: 动态 LOGO 直接绘制于透明容器 (去渐变底, 贴合品牌负空间) */}
          <LogoMark size={32} />
          {!collapsed && (
            <div className="min-w-0">
              <div className="text-[13.5px] font-bold leading-tight truncate">{BRAND_EN}</div>
              {/* R1.1/R1.3: 中文副标题仅当界面语言为中文时渲染。判定口径用
                  i18n.language.startsWith('zh') 涵盖 zh-cn / zh-Hans / zh-CN 等。
                  非渲染(React 条件),不是 CSS display:none —— en 下 DOM 0 中文。*/}
              {isZh && (
                <div className="text-[10.5px] text-dim leading-tight truncate">{BRAND_ZH}</div>
              )}
            </div>
          )}
        </div>

        <nav className="flex-1 px-2.5 py-4 space-y-1">
          {NAV.filter((n) => !n.dynamic || pixelEnabled).map((n) => {
            const active = view === n.id
            const navKey = NAV_LABEL_KEYS[n.id]
            const navLabel = navKey ? t(navKey) : ''
            return (
              <button key={n.id}
                onClick={() => { setView(n.id); if (n.id === 'chat') setActiveChat('main') }}
                className={`w-full flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium
                           transition-all duration-150 group relative
                           ${active
                             ? 'bg-accent/15 text-accent ring-1 ring-inset ring-accent/30'
                             : 'text-dim hover:bg-panel2 hover:text-ink'}`}>
                <Icon d={n.icon} className="w-[18px] h-[18px] shrink-0" />
                {!collapsed && <span className="truncate" title={navLabel}>{navLabel}</span>}
                {!collapsed && n.id === 'tasks-board' && counts.attention > 0 && (
                  <span className="ml-auto badge-red">{counts.attention}</span>
                )}
                {collapsed && counts.attention > 0 && n.id === 'tasks-board' && (
                  <span className="absolute top-1.5 right-1.5 w-2 h-2 rounded-full bg-bad shadow-[0_0_6px_rgb(var(--c-bad))]" />
                )}
              </button>
            )
          })}
          {/* worker 对话 tab: 桌面侧边栏也并列, 多个 worker 时用竖排列表 */}
          {chatTabs.filter((t) => t !== 'main').map((name) => {
            const active = view === 'chat' && activeChat === name
            return (
              <button key={name}
                onClick={() => { setActiveChat(name); setView('chat') }}
                className={`w-full flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium
                           transition-all duration-150 group relative
                           ${active
                             ? 'bg-accent/15 text-accent ring-1 ring-inset ring-accent/30'
                             : 'text-dim hover:bg-panel2 hover:text-ink'}`}>
                <Icon d="M21 11.5a8.38 8.38 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.38 8.38 0 01-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.38 8.38 0 013.8-.9h.5a8.48 8.48 0 018 8v.5z"
                      className="w-[18px] h-[18px] shrink-0" />
                {!collapsed && <span className="truncate" title={name}>💬 {name}</span>}
                {/* 关闭按钮统一由 ChatView 内部 onClose 触发("✕ 关闭" 按钮), 侧边栏不重复 */}
              </button>
            )
          })}
        </nav>

        <div className="px-3 py-3 border-t border-line">
          <div className={`flex items-center gap-2 ${collapsed ? 'justify-center' : ''}`}>
            <span className={aliveDot} />
            {!collapsed && (
              <span className="text-[11.5px] text-dim truncate">
                {alive === undefined
                  ? t('app.reaper_unknown')
                  : alive
                  ? t('app.reaper_online_age', { n: Math.floor(reaper.reaper_heartbeat_age_s | 0) })
                  : t('app.reaper_offline_short')}
              </span>
            )}
          </div>
          {!collapsed && (
            <div className="mt-2 flex items-center justify-between text-[10.5px] text-dim/70">
              <span className="truncate" title={t('app.sidebar_footer_tagline')}>{t('app.sidebar_footer_tagline')}</span>
            </div>
          )}
        </div>

        <button onClick={() => setCollapsed(!collapsed)}
          className="mx-2.5 mb-3 btn-quiet btn-sm justify-center"
          title={t('app.collapse_btn')}>
          <Icon d={collapsed ? 'M13 5l7 7-7 7M4 5l7 7-7 7' : 'M11 19l-7-7 7-7M20 19l-7-7 7-7'}
                className="w-4 h-4" />
          {!collapsed && <span>{t('app.collapse_btn')}</span>}
        </button>
      </aside>

      {/* ── 主区 ── */}
      <main className="flex-1 min-w-0 flex flex-col">
        <header className="h-16 shrink-0 border-b border-line bg-panel/60 backdrop-blur
                          flex items-center justify-between px-6">
          <div>
            <h1 className="text-[15px] font-bold leading-tight">
              {view === 'chat' && activeChat !== 'main'
                ? t('app.chat_h1_worker', { name: activeChat })
                : (
                    (() => {
                      const navKey = NAV_LABEL_KEYS[view]
                      return navKey ? t(navKey) : ''
                    })()
                  )
              }
            </h1>
            <p className="text-[11.5px] text-dim leading-tight mt-0.5">
              {view === 'chat' && activeChat === 'main' && t('app.subtitle_chat_main')}
              {view === 'chat' && activeChat !== 'main' && t('app.subtitle_chat_worker', { name: activeChat })}
              {view === 'tasks-board' && t('app.subtitle_tasks_board', { total: counts.total, attention: counts.attention, running: counts.running, finished: counts.finished })}
              {view === 'workers' && t('app.subtitle_workers')}
              {view === 'workflows' && t('app.subtitle_workflows')}
              {view === 'cron' && t('app.subtitle_cron')}
              {view === 'pixel-office' && t('app.subtitle_pixel_office')}
              {view === 'settings' && t('app.subtitle_settings')}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {/* R2.1: 桌面主 header 动作组最左新增「核心功能」按钮(btn-primary 高亮) */}
            <CoreFeaturesBtn />
            <WebuiBtn />
            <button onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
              className="btn-ghost btn-sm" title={t('app.theme_toggle')}>
              <Icon d={theme === 'dark'
                ? 'M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z'
                : 'M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z'}
                className="w-4 h-4" />
            </button>
            <button onClick={pollStatus} className="btn-ghost btn-sm"
                    title={t('app.refresh_btn')}>
              <Icon d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" className="w-4 h-4" />
              {t('app.refresh_btn')}
            </button>
          </div>
        </header>

        <div className={`flex-1 overflow-auto p-6 ${view === 'chat' ? 'flex flex-col' : ''}`}>
          {/* chatTabs 里每个 tab 都 mount 自己的 ChatView 实例(独立 sessionId/输入框/附件),
              切走时 React 保留 state, 切回去原封不动. 仅激活的显示, 其余 display:none */}
          {chatTabs.map((tab, idx) => {
                        const active = view === 'chat' && activeChat === tab
                        const base = tab === 'main' ? '' : `/api/w/${tab}`
                        const label = tab === 'main' ? t('app.main_agent') : t('app.worker_chat_title', { name: tab })
                        return (
                          <div key={tab} className={`flex-1 min-h-0 flex flex-col ${active ? '' : 'hidden'}`}>
                            <ChatView ref={(el) => { chatViewRefs.current[idx] = el }} base={base} targetLabel={label}
                              active={active}
                              onClose={tab === 'main' ? null : () => closeWorkerChat(tab)} />
                          </div>
                        )
                      })}
          {view !== 'chat' && (
            <>
              {view === 'tasks-board' && <TasksBoardView onChanged={pollStatus} />}
              {view === 'workers' && <WorkersView onChanged={pollStatus} onChat={openWorkerChat} />}
              {view === 'workflows' && <WorkflowsView onChanged={pollStatus} />}
              {view === 'cron' && <CronView />}
              {view === 'pixel-office' && pixelEnabled && <PixelOfficeView onNavigate={setView} />}
              {view === 'settings' && <SettingsView onSaved={(st) => { setWebuiUrl((st?.hermes_webui_url || '').trim()); api.pixelStatus().then((d) => setPixelEnabled(!!d.enabled)).catch(() => {}) }} />}
            </>
          )}
        </div>
      </main>
    </div>
  )
}
