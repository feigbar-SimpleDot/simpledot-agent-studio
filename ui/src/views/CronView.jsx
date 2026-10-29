import React, { useEffect, useState, useCallback, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { api, fmtApiError } from '../api.js'
import { Toast } from '../components/ui.jsx'

// ════════════════════════════════════════════════════════════════════
// CronView — 定时任务视图 (s4 / 卡 t_da1c3a64)
//
// 依据: docs/UX-CRON-20261027.md (交互设计, 逐条落地)
//       + docs/PLAN-CRON-20261027.md §2 (端点契约) / §3.3 (状态机)
//       (以上两份是**过程文档**, 位于任务 workspace docs/, 不入仓 —— 干净克隆
//        里无此路径; 本文件注释自含设计约束, 不依赖它们才能读懂)
//
// 设计硬约束 (UX §9 交接要点):
//   1. 操作可见性实现为**纯函数** cronActions(state) → 按钮集 (CT7 逐格断言)
//   2. 反馈流顺序不可变: busy → 请求 → toast → 重拉列表 → finally 清 busy;
//      **禁止乐观改写 state**(服务端为准)
//   3. toast 文案逐字取词条; trigger 成功必须是「已排入下次调度」口径
//      (trigger_job 语义是 stamp 下个 tick, 不承诺已开始执行)
//   4. 坏形状防御: jobs 非数组 → 错误态; 单条 state 非字符串 → 未知徽标 + 无按钮
//   5. ticker 横幅仅 ticker && ticker.alive === false 时显示
//   6. 轮询 20s, 仅视图挂载且 document 可见时; 有 busy 操作时跳过本轮覆盖
//   7. 三操作**零二次确认**(均可逆或幂等), 靠 loading 禁用 + toast 闭环
// ════════════════════════════════════════════════════════════════════

// ── 操作可见性纯函数 (UX §4.1 可见性矩阵, 逐格照抄 s1 §3.1) ──────
// scheduled → [立即运行, 暂停]; paused → [恢复];
// completed/error → [] (终态卡显示引导文案); 未知/非字符串 → []
export function cronActions(state) {
  if (typeof state !== 'string') return []
  if (state === 'scheduled') return ['trigger', 'pause']
  if (state === 'paused') return ['resume']
  return []
}

// 终态 (completed/error) → 操作区显示引导文案而非按钮 (UX §4.1)。
export function cronIsTerminal(state) {
  return state === 'completed' || state === 'error'
}

// ── 状态徽标映射 (UX §3.2: state 唯一判定源, 坏形状降级为未知) ────
const STATE_BADGE = {
  scheduled: 'badge-green',
  paused: 'badge-slate',
  completed: 'badge-blue',
  error: 'badge-red',
}
export function cronStateBadge(state) {
  // #142: 原型链键防御 (与 run 侧同款缺陷, state 侧更弱)。truthy 查表会让
  // STATE_BADGE['constructor'] 命中 Object.prototype ⇒ 返回函数而非类名。
  if (typeof state !== 'string') return 'badge-unknown'
  return Object.prototype.hasOwnProperty.call(STATE_BADGE, state)
    ? STATE_BADGE[state]
    : 'badge-unknown'
}
// 未知态的 i18n 键: 非四态之一(含坏形状)一律走 unknown。
export function cronStateKey(state) {
  // #142: 旧 truthy 查表对原型链键**原样返回** ⇒ 渲染 t('cron.state.constructor')
  // 裸键。自有键判定 + typeof 守卫 (挡住 ['scheduled'] 等被 String() 强制转换的坏形状)。
  if (typeof state !== 'string') return 'unknown'
  return Object.prototype.hasOwnProperty.call(STATE_BADGE, state) ? state : 'unknown'
}

// ── 时间格式化: MM-DD HH:mm (UX §3.1 L2) ──────────────────────────
// 本地化函数, 不建词条 (UX §7 词条规则说明)。
export function formatCronTime(raw) {
  if (!raw) return ''
  const d = new Date(raw)
  if (Number.isNaN(d.getTime())) return String(raw)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

// runs 面板的状态徽标 (UX §3.3): completed→绿, failed→红,
// running/claimed→蓝, unknown→灰。
const RUN_BADGE = {
  completed: 'badge-green',
  failed: 'badge-red',
  running: 'badge-blue',
  claimed: 'badge-blue',
}
export function cronRunBadge(status) {
  // #142: 原型链键防御。RUN_BADGE 是普通对象字面量, `RUN_BADGE['constructor']`
  // 命中 Object.prototype ⇒ 返回函数/对象而非 CSS 类名。hasOwnProperty.call
  // 只认**自有**键; typeof 守卫挡住数组等会被 String() 强制转换的坏形状
  // (hasOwnProperty.call(RUN_BADGE, ['completed']) === true)。
  if (typeof status !== 'string') return 'badge-unknown'
  return Object.prototype.hasOwnProperty.call(RUN_BADGE, status)
    ? RUN_BADGE[status]
    : 'badge-unknown'
}
// runs 状态的**词条键**归一 (#134 + #142): 合法键集合由 RUN_BADGE 的**自有键**
// 派生 (单一真相源) —— 代码里不再存在第二处四值字面量白名单, 未来给
// RUN_BADGE 加键忘加白名单的漂移从根上消失。Set.has 用 SameValueZero,
// 不做字符串强制转换 ⇒ 天然免疫数组坏形状。
const RUN_STATUS_KEYS = new Set(Object.keys(RUN_BADGE))
export function cronRunStatusKey(status) {
  if (typeof status !== 'string') return 'unknown'
  return RUN_STATUS_KEYS.has(status) ? status : 'unknown'
}

// 响应形状防御: jobs 必须是数组, 否则整页落错误态 (UX §2.1 / MV1)。
export function cronJobsValid(payload) {
  return !!payload && Array.isArray(payload.jobs)
}

// 单条作业形状防御: state 必须是字符串, 否则未知态且**不可操作**。
export function cronJobStateSafe(job) {
  if (!job || typeof job !== 'object') return 'unknown'
  return typeof job.state === 'string' ? job.state : 'unknown'
}

// ── JobCard ─────────────────────────────────────────────────────────
function JobCard({ job, t, busy, onAction, runs, onToggleRuns }) {
  const state = cronJobStateSafe(job)
  const actions = cronActions(state)
  const terminal = cronIsTerminal(state)
  const name = typeof job.name === 'string' && job.name ? job.name : (job.id || '')
  const schedule = job.schedule_display || job.schedule_expr || ''
  const latest = (job.latest_execution && typeof job.latest_execution === 'object')
    ? job.latest_execution : null
  const latestAt = latest ? (latest.started_at || latest.finished_at) : null
  const prompt = typeof job.prompt === 'string' ? job.prompt : ''

  return (
    <div className="card p-4 flex flex-col gap-2">
      {/* L1: 名称 + 状态徽标 */}
      <div className="flex items-start gap-2 justify-between">
        <div className="text-[14.5px] font-bold truncate min-w-0" title={name}>{name}</div>
        <span className={`${cronStateBadge(state)} shrink-0`}
              role="status" aria-label={t(`cron.state.${cronStateKey(state)}`)}
              title={job.paused_reason || undefined}>
          {t(`cron.state.${cronStateKey(state)}`)}
        </span>
      </div>

      {/* L2: 周期 + 最近执行 */}
      <div className="text-[12px] text-dim flex flex-col sm:flex-row sm:gap-2">
        <span className="truncate" title={schedule}>{schedule}</span>
        <span className="truncate">
          {latestAt
            ? `${formatCronTime(latestAt)} · ${t(`cron.run_status.${cronRunStatusKey(latest.status)}`)}`
            : t('cron.never_run')}
        </span>
      </div>

      {/* L3: prompt 摘要 (后端已截 200 字符, 前端 2 行截断) */}
      {prompt && (
        <div className="text-[12.5px] text-dim leading-snug"
             style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical',
                      overflow: 'hidden' }}
             title={prompt}>
          {prompt}
        </div>
      )}

      {/* L4: 操作区 + runs 展开
          D-2 (UX §6 窄屏规则): <768px 按钮独立行纵向堆叠 + min-h-[44px] 触达;
          md: 前缀恢复桌面原布局 (同行 flex-wrap, 零变化)。 */}
      <div className="flex flex-col items-start md:flex-row md:flex-wrap md:items-center gap-2">
        {actions.map((act) => {
          const isBusy = busy === act
          const primary = act === 'trigger'
          return (
            <button key={act}
              className={`${primary ? 'btn-primary btn-sm' : 'btn-quiet btn-sm'} min-h-[44px] md:min-h-0`}
              disabled={!!busy}
              aria-busy={isBusy ? 'true' : 'false'}
              aria-label={`${t(`cron.action.${act}`)} ${name}`}
              onClick={() => onAction(job.id, act)}>
              {isBusy ? t('common.loading') : t(`cron.action.${act}`)}
            </button>
          )
        })}
        {terminal && (
          <span className="text-[12px] text-dim">{t('cron.terminal_hint')}</span>
        )}
        <button className="btn-ghost btn-sm min-h-[44px] md:min-h-0 md:ml-auto"
                aria-expanded={runs.open ? 'true' : 'false'}
                aria-controls={`cron-runs-${job.id}`}
                onClick={() => onToggleRuns(job.id)}>
          {runs.open ? t('cron.runs.hide') : t('cron.runs.show')}
        </button>
      </div>

      {/* runs 展开面板: 局部 loading/空/错误态, 不影响卡片其余部分 */}
      {runs.open && (
        <div id={`cron-runs-${job.id}`} className="border-t border-line pt-2 flex flex-col gap-1.5">
          {runs.phase === 'loading' && (
            <div className="text-[12px] text-dim">{t('cron.runs.loading')}</div>
          )}
          {runs.phase === 'error' && (
            <div className="text-[12px] text-dim flex items-center gap-2">
              <span className="truncate" title={runs.error}>{t('cron.runs.error')}</span>
              <button className="btn-ghost btn-sm"
                      onClick={() => onToggleRuns(job.id, true)}>
                {t('cron.runs.retry')}
              </button>
            </div>
          )}
          {runs.phase === 'ready' && (runs.runs || []).length === 0 && (
            <div className="text-[12px] text-dim">{t('cron.runs.empty')}</div>
          )}
          {runs.phase === 'ready' && (runs.runs || []).length > 0 && (
            (runs.runs || []).map((r, i) => (
              <div key={r.id || i} className="text-[12px] text-dim flex flex-col sm:flex-row sm:gap-2">
                <span className="shrink-0">{formatCronTime(r.started_at || r.finished_at)}</span>
                <span className={`${cronRunBadge(r.status)} shrink-0`}>
                  {t(`cron.run_status.${cronRunStatusKey(r.status)}`)}
                </span>
                {r.error && (
                  <span className="truncate" title={r.error}>{r.error}</span>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  )
}

// ── CronView ───────────────────────────────────────────────────────
export default function CronView() {
  const { t } = useTranslation()
  // phase: loading | error | ready —— 四态页面状态机 (UX §2.1)
  const [phase, setPhase] = useState('loading')
  const [error, setError] = useState('')
  const [jobs, setJobs] = useState([])
  const [ticker, setTicker] = useState(null)
  const [busy, setBusy] = useState({})          // { jobId: action }
  const [openRuns, setOpenRuns] = useState({})  // { jobId: {open, phase, runs, error} }
  const [toast, setToast] = useState(null)      // { msg, type }
  const [refreshing, setRefreshing] = useState(false)
  // busy 判定给轮询用: 任一操作进行中 → 本轮跳过整表刷新 (UX §4.2)。
  const busyRef = useRef({})
  busyRef.current = busy
  // runs 展开的 per-job 请求代次 (#136): 每次 onToggleRuns 推进该 job 的代次,
  // 异步回填前比对 —— 陈旧响应 (先发后到的慢请求) 直接丢弃, 不覆盖末次操作。
  // 用 ref 而非 state: 代次不进渲染, 且回填判定须读到**最新**值而非闭包快照。
  const runsGenRef = useRef({})

  // 拉列表: 坏形状 → 错误态 (MV1 反证); 成功 → ready。
  const load = useCallback(async (initial = false) => {
    if (!initial) setRefreshing(true)
    try {
      const data = await api.cron.jobs()
      if (!cronJobsValid(data)) {
        // 坏形状: jobs 非数组 ⇒ 落错误态, 不白屏
        setPhase('error')
        setError(t('cron.err.title'))
        return
      }
      setJobs(data.jobs)
      setTicker(data.ticker && typeof data.ticker === 'object' ? data.ticker : null)
      setPhase('ready')
      setError('')
    } catch (e) {
      setPhase('error')
      setError(fmtApiError(e, t))
    } finally {
      setRefreshing(false)
    }
  }, [t])

  // 首屏加载
  useEffect(() => { load(true) }, [load])

  // 轮询: 20s, 仅视图挂载且 document 可见; 有 busy 操作时跳过本轮 (UX §2)
  useEffect(() => {
    const tick = () => {
      if (document.hidden) return
      if (Object.keys(busyRef.current).length > 0) return
      load(false)
    }
    const timer = setInterval(tick, 20000)
    const onVis = () => { if (!document.hidden) tick() }
    document.addEventListener('visibilitychange', onVis)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [load])

  // 操作反馈流 (UX §4.2): busy → 请求 → toast → 重拉列表 → finally 清 busy
  const onAction = useCallback(async (jobId, action) => {
    setBusy((cur) => ({ ...cur, [jobId]: action }))
    try {
      await api.cron[action](jobId)
      const key = action === 'trigger' ? 'cron.toast.triggered'
                : action === 'pause' ? 'cron.toast.paused'
                : 'cron.toast.resumed'
      setToast({ msg: t(key), type: 'ok' })
      await load(false)   // 服务端为准, 无乐观改写
    } catch (e) {
      // 404 → 「任务已不存在」+ 重拉; 其余走 fmtApiError 三态归因。
      if (e && e.status === 404) {
        setToast({ msg: t('cron.toast.gone'), type: 'warn' })
        await load(false)
      } else {
        setToast({ msg: t('cron.toast.failed', { err: fmtApiError(e, t) }), type: 'bad' })
      }
    } finally {
      setBusy((cur) => {
        const next = { ...cur }
        delete next[jobId]
        return next
      })
    }
  }, [load, t])

  // runs 展开/收起: 展开时拉 GET runs?limit=3; 展开状态跨轮询保持。
  // 代次纪律 (#136): 每次调用 (含收起) 推进该 job 的代次; 回填前比对,
  // 不一致 = 陈旧响应 → 丢弃, 保证面板终态恒等于**最后一次**用户操作。
  const onToggleRuns = useCallback(async (jobId, force = false) => {
    const cur = openRuns[jobId] || { open: false, phase: 'idle', runs: [], error: '' }
    const gen = (runsGenRef.current[jobId] || 0) + 1
    runsGenRef.current[jobId] = gen
    if (cur.open && !force) {
      setOpenRuns((s) => ({ ...s, [jobId]: { ...cur, open: false } }))
      return
    }
    setOpenRuns((s) => ({ ...s, [jobId]: { ...cur, open: true, phase: 'loading' } }))
    try {
      const data = await api.cron.runs(jobId, 3)
      if (runsGenRef.current[jobId] !== gen) return   // 陈旧响应: 已被更新的操作取代
      const rows = Array.isArray(data?.runs) ? data.runs : []
      setOpenRuns((s) => ({ ...s, [jobId]: { open: true, phase: 'ready', runs: rows, error: '' } }))
    } catch (e) {
      if (runsGenRef.current[jobId] !== gen) return   // 陈旧失败: 不覆盖末次操作的画面
      setOpenRuns((s) => ({
        ...s,
        [jobId]: { open: true, phase: 'error', runs: [],
                   error: fmtApiError(e, t) },
      }))
    }
  }, [openRuns, t])

  // 视图头: 标题 + 手动刷新 (轮询已覆盖自动刷新, 此按钮给"立刻确认"出口)
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-3">
        <h2 className="text-[14.5px] font-bold">{t('cron.title')}</h2>
        <button className="btn-ghost btn-sm ml-auto"
                disabled={refreshing}
                onClick={() => load(false)}>
          {refreshing ? t('common.loading') : t('common.refresh')}
        </button>
      </div>

      {/* ticker 停摆横幅: 仅 ticker.alive === false 时显示 (UX §4.3) */}
      {ticker && ticker.alive === false && (
        <div className="badge-yellow !text-[12px] px-3 py-1.5" role="status">
          {t('cron.ticker_down')}
        </div>
      )}

      {phase === 'loading' && (
        <div className="flex flex-col gap-3">
          <div className="text-[12px] text-dim">{t('cron.loading')}</div>
          {[0, 1, 2].map((i) => (
            <div key={i} className="card p-4 h-[96px] opacity-60" />
          ))}
        </div>
      )}

      {phase === 'error' && (
        <div className="empty flex flex-col items-center gap-2">
          <span className="dot-bad" />
          <div className="text-[13px] font-medium">{t('cron.err.title')}</div>
          {error && (
            <div className="text-[12px] text-dim truncate max-w-full" title={error}>{error}</div>
          )}
          <button className="btn-ghost btn-sm" onClick={() => load(true)}>
            {t('common.retry')}
          </button>
        </div>
      )}

      {phase === 'ready' && jobs.length === 0 && (
        <div className="empty flex flex-col items-center gap-2">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
               strokeLinecap="round" strokeLinejoin="round" className="w-10 h-10 opacity-40">
            <path d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <div className="text-[13px] font-medium">{t('cron.empty.title')}</div>
          <div className="text-[12px] text-dim text-center max-w-[420px]">
            {t('cron.empty.hint')}
          </div>
        </div>
      )}

      {phase === 'ready' && jobs.length > 0 && (
        <div className="flex flex-col gap-3">
          {jobs.map((job, idx) => (
            <JobCard key={(job && job.id) || idx}
                     job={job} t={t}
                     busy={busy[(job && job.id) || '']}
                     onAction={onAction}
                     runs={openRuns[(job && job.id) || ''] || { open: false, phase: 'idle', runs: [], error: '' }}
                     onToggleRuns={onToggleRuns} />
          ))}
        </div>
      )}

      <Toast msg={toast?.msg} type={toast?.type || 'ok'}
             onDone={() => setToast(null)} />
    </div>
  )
}
