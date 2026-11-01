import React, { useEffect, useState, useCallback, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { api, fmtApiError } from '../api.js'
import { Toast, Modal, Confirm } from '../components/ui.jsx'

// ════════════════════════════════════════════════════════════════════
// ScheduleView — 收件箱 TAB (原任务排期) 视图 (issue #164, ARCH-164 §3.2)
//
// 结构: 顶栏自动执行开关 + busy 提示 / 倒计时条(进度条 + 立即执行) /
//       修改弹窗(仅 paused) / 三分组列表 (执行中 / 未执行+定时预览 / 已完成取消)。
//
// issue #164 追加 (ENTRY-164B): 排期页不再有「新建任务」入口 —— 排期是任务
// 唯一入口, 新建统一走「发起多智能体协作」弹窗入队 (ChatView.submitTeamWork);
// 本页只保留 paused 任务的修改能力。
//
// 设计约束 (ARCH-164 §3.2):
//   1. 轮询: state 每 2s (进度条平滑) + tasks 每 5s (TasksBoardView 同档);
//      组件卸载清 timer; 任何本地写操作后立即手动刷新两接口 (AC-2.4)。
//   2. 立即执行二次确认 (AC-6.2/6.3/6.4): 复用 ui.jsx Modal, 文案含队首
//      任务标题; 按钮仅在 countdown.active 时渲染 (A11)。
//   3. 定时任务只读插入 (F9/A13): 并行调既有 api.cron.jobs(), 取启用态
//      (state==='scheduled') 且 next_run_at 非空的条目按 next_run_at 混入
//      未执行组排序; 背景色区分、无操作按钮、不写排期库。
//   4. 已完成/取消组前端仅渲染最近 50 条 (R-5 缓解, 服务端全量返回)。
//   5. 操作可见性纯函数 schedActions(status) → 按钮集 (CT7 同族纪律)。
// ════════════════════════════════════════════════════════════════════

// 操作可见性纯函数: pending→[暂停,取消]; paused→[恢复,取消,修改];
// running→[](停止通道是看板 close); done→[]; cancelled→[删除]。
export function schedActions(status) {
  if (status === 'pending') return ['pause', 'cancel']
  if (status === 'paused') return ['resume', 'cancel', 'edit']
  if (status === 'cancelled') return ['delete']
  return []
}

// 状态徽标 (5 态机器值 → 颜色类; 未知值降级 slate, #142 原型链防御同法)
const STATE_BADGE = {
  pending: 'badge-blue',
  paused: 'badge-slate',
  running: 'badge-green',
  done: 'badge-purple',
  cancelled: 'badge-red',
}
export function schedStateBadge(status) {
  if (typeof status !== 'string') return 'badge-unknown'
  return Object.prototype.hasOwnProperty.call(STATE_BADGE, status)
    ? STATE_BADGE[status] : 'badge-slate'
}
export function schedStateKey(status) {
  if (typeof status !== 'string') return 'unknown'
  return Object.prototype.hasOwnProperty.call(STATE_BADGE, status) ? status : 'unknown'
}

// 优先级 tag 四色 (紧急红/高黄/普通蓝/低灰)
const PRIO_BADGE = { urgent: 'badge-red', high: 'badge-yellow', normal: 'badge-blue', low: 'badge-slate' }
export function schedPrioBadge(prio) {
  if (typeof prio !== 'string') return 'badge-slate'
  return Object.prototype.hasOwnProperty.call(PRIO_BADGE, prio) ? PRIO_BADGE[prio] : 'badge-slate'
}

// 时间格式化 MM-DD HH:mm (本地化函数, 不建词条 — cron 同纪律)
export function formatSchedTime(epoch) {
  if (!epoch) return ''
  const d = new Date(epoch * 1000)
  if (Number.isNaN(d.getTime())) return String(epoch)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

// 剩余秒 → mm:ss
export function fmtCountdown(sec) {
  const s = Math.max(0, Math.floor(sec))
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(Math.floor(s / 60))}:${pad(s % 60)}`
}

// 三分组切分 (纯函数, 供单测): 组0 running / 组1 pending+paused /
// 组2 done+cancelled。服务端已按 (组序, added_at DESC) 预排, 这里只切。
export function schedGroups(tasks) {
  const g = { running: [], pending: [], done: [] }
  for (const t of (Array.isArray(tasks) ? tasks : [])) {
    if (t.status === 'running') g.running.push(t)
    else if (t.status === 'pending' || t.status === 'paused') g.pending.push(t)
    else g.done.push(t)
  }
  g.done = g.done.slice(0, 50)   // R-5: 底部仅渲染最近 50 条
  return g
}

// cron 预览条目混入未执行组 (A13): 按 next_run_at 与任务 added_at 同轴排序。
// cron.next_run_at 是 RFC3339 字符串, added_at 是 epoch 秒 → 统一成 ms 比较。
export function mergePendingWithCron(pendingTasks, cronJobs) {
  const items = pendingTasks.map((t) => ({ kind: 'task', at: t.added_at * 1000, task: t }))
  for (const job of (Array.isArray(cronJobs) ? cronJobs : [])) {
    if (!job || job.state !== 'scheduled' || !job.next_run_at) continue
    const at = Date.parse(job.next_run_at)
    if (Number.isNaN(at)) continue
    items.push({ kind: 'cron', at, job })
  }
  items.sort((a, b) => a.at - b.at)   // 时间倒序 = 新在前 (显示序=执行序 A8)
  return items.reverse()
}

const COUNTDOWN_TOTAL = 300   // 5 分钟 (与后端 COUNTDOWN_S 同值)

export default function ScheduleView({ onNavigate }) {
  const { t } = useTranslation()
  const [tasks, setTasks] = useState([])
  const [state, setState] = useState(null)          // /api/schedule/state
  const [cronJobs, setCronJobs] = useState([])
  const [err, setErr] = useState('')
  const [toast, setToast] = useState(null)         // { msg, type }
  const [busy, setBusy] = useState('')             // 进行中的操作 id
  // 修改弹窗 (issue #164 追加: 仅 paused 修改; 新建入口已移除, 排期唯一入口
  // 是「发起多智能体协作」弹窗入队)
  const [formOpen, setFormOpen] = useState(false)
  const [formTask, setFormTask] = useState(null)   // 被修改的任务 (仅 paused)
  const [formDetail, setFormDetail] = useState('')
  const [formPrio, setFormPrio] = useState('normal')
  const [formErr, setFormErr] = useState('')
  const [formSaving, setFormSaving] = useState(false)
  // 立即执行二次确认
  const [confirmRun, setConfirmRun] = useState(false)
  const tickRef = useRef(null)

  const refreshState = useCallback(() => {
    api.scheduleState().then(setState).catch(() => {})
  }, [])
  const refreshTasks = useCallback(() => {
    api.scheduleList()
      .then((d) => { setTasks(Array.isArray(d.tasks) ? d.tasks : []); setErr('') })
      .catch((e) => setErr(fmtApiError(e, t)))
  }, [t])

  // 轮询: state 2s + tasks 5s + cron 15s (cron 仅展示用, 低频); 卸载清 timer
  useEffect(() => {
    refreshState(); refreshTasks()
    api.cron.jobs().then((d) => setCronJobs(Array.isArray(d.jobs) ? d.jobs : [])).catch(() => {})
    const tState = setInterval(refreshState, 2000)
    const tTasks = setInterval(refreshTasks, 5000)
    const tCron = setInterval(() => {
      api.cron.jobs().then((d) => setCronJobs(Array.isArray(d.jobs) ? d.jobs : [])).catch(() => {})
    }, 15000)
    return () => { clearInterval(tState); clearInterval(tTasks); clearInterval(tCron) }
  }, [refreshState, refreshTasks])

  // 本地写操作后: toast → 立即手动刷新两接口 (AC-2.4 无需手动刷新)
  const afterWrite = useCallback((msgKey, type = 'ok') => {
    if (msgKey) setToast({ msg: t(msgKey), type })
    refreshState(); refreshTasks()
  }, [t, refreshState, refreshTasks])

  const act = async (id, action, okKey) => {
    if (busy) return
    setBusy(`${id}:${action}`)
    try {
      await api.scheduleAction(id, action)
      afterWrite(okKey)
    } catch (e) {
      setToast({ msg: fmtApiError(e, t), type: 'bad' })
      refreshTasks()   // 409 等: 服务端为准重拉
    } finally { setBusy('') }
  }

  const toggleAuto = async (v) => {
    if (busy) return
    setBusy('auto')
    try {
      await api.scheduleSettings(v)
      afterWrite(v ? 'schedule.toast.auto_on' : 'schedule.toast.auto_off')
    } catch (e) {
      setToast({ msg: fmtApiError(e, t), type: 'bad' })
      refreshState()
    } finally { setBusy('') }
  }

  const submitForm = async () => {
    const detail = formDetail.trim()
    if (!detail) { setFormErr(t('schedule.form.detail_required')); return }
    if (!formTask) return   // 防御: 弹窗只服务于 paused 修改
    setFormSaving(true); setFormErr('')
    try {
      await api.scheduleAction(formTask.id, 'update', { detail, priority: formPrio })
      setFormOpen(false)
      afterWrite('schedule.toast.saved_paused')   // AC-3.7 保持暂停提示
    } catch (e) {
      setFormErr(fmtApiError(e, t))
    } finally { setFormSaving(false) }
  }

  const doRunNow = async () => {
    setConfirmRun(false)
    try {
      await api.scheduleRunNow()
      afterWrite('schedule.toast.run_started')
    } catch (e) {
      setToast({ msg: fmtApiError(e, t), type: 'bad' })
      refreshState()
    }
  }

  const openEdit = (task) => {
    setFormTask(task); setFormDetail(task.detail || ''); setFormPrio(task.priority || 'normal')
    setFormErr(''); setFormOpen(true)
  }

  const groups = schedGroups(tasks)
  const merged = mergePendingWithCron(groups.pending, cronJobs)
  const cd = state?.countdown
  const autoOn = !!state?.auto_execute
  const busyReason = state?.busy_reason

  // 行渲染 (task 与 cron 预览共用行壳, cron 背景色区分 + 无操作按钮)
  const renderTaskRow = (task) => {
    const acts = schedActions(task.status)
    return (
      <div key={task.id} className="card px-4 py-3 flex flex-col sm:flex-row sm:items-center gap-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm text-ink truncate max-w-[360px]" title={task.title}>{task.title}</span>
            <span className={schedPrioBadge(task.priority)}>{t(`schedule.prio.${task.priority}`, { defaultValue: task.priority })}</span>
            <span className={schedStateBadge(task.status)}>{t(`schedule.state.${schedStateKey(task.status)}`)}</span>
          </div>
          <div className="mt-1 text-[11.5px] text-dim">
            {t('schedule.col_added')}: {formatSchedTime(task.added_at) || '—'}
            {' · '}{t('schedule.col_executed')}: {task.executed_at ? formatSchedTime(task.executed_at) : '—'}
            {task.last_error ? <span className="text-bad"> · {task.last_error}</span> : null}
          </div>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {task.status === 'running' && onNavigate && (
            <button className="btn-ghost btn-sm" onClick={() => onNavigate('tasks-board')}>
              {t('schedule.btn.view_board')}
            </button>
          )}
          {acts.includes('pause') && (
            <button className="btn-ghost btn-sm" disabled={busy !== ''} onClick={() => act(task.id, 'pause', 'schedule.toast.paused')}>
              {t('schedule.btn.pause')}
            </button>)}
          {acts.includes('resume') && (
            <button className="btn-ghost btn-sm" disabled={busy !== ''} onClick={() => act(task.id, 'resume', 'schedule.toast.resumed')}>
              {t('schedule.btn.resume')}
            </button>)}
          {acts.includes('edit') && (
            <button className="btn-ghost btn-sm" disabled={busy !== ''} onClick={() => openEdit(task)}>
              {t('schedule.btn.edit')}
            </button>)}
          {acts.includes('cancel') && (
            <button className="btn-ghost btn-sm !text-bad" disabled={busy !== ''} onClick={() => act(task.id, 'cancel', 'schedule.toast.cancelled')}>
              {t('schedule.btn.cancel')}
            </button>)}
          {acts.includes('delete') && (
            <button className="btn-ghost btn-sm !text-bad" disabled={busy !== ''} onClick={() => act(task.id, 'delete', 'schedule.toast.deleted')}>
              {t('schedule.btn.delete')}
            </button>)}
        </div>
      </div>
    )
  }

  const renderCronRow = (job, at) => (
    <div key={`cron:${job.id}`} className="card px-4 py-3 flex items-center gap-2 bg-panel2/60 border-dashed">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm text-ink truncate max-w-[360px]" title={job.name}>{job.name}</span>
          <span className="badge-slate">{t('schedule.cron_badge')}</span>
        </div>
        <div className="mt-1 text-[11.5px] text-dim">
          {t('schedule.col_added')}: {formatSchedTime(at / 1000)}
        </div>
      </div>
      {/* F9: 仅显示, 无操作按钮 */}
    </div>
  )

  const sectionTitle = (key, n) => (
    <div className="text-xs font-semibold text-dim uppercase tracking-wide mt-4 mb-2">
      {t(key)}{n > 0 ? ` (${n})` : ''}
    </div>
  )

  const empty = groups.running.length === 0 && merged.length === 0 && groups.done.length === 0

  return (
    <div className="max-w-4xl mx-auto space-y-3">
      {/* 顶栏: 自动执行开关 + busy 提示 (AC-1.5/A15) */}
      <div className="card px-4 py-3 flex items-center gap-3 flex-wrap">
        <label className="flex items-center gap-2 cursor-pointer select-none">
          <input type="checkbox" checked={autoOn} disabled={busy === 'auto'}
                 onChange={(e) => toggleAuto(e.target.checked)} />
          <span className="text-sm text-ink">{t('schedule.auto_execute')}</span>
        </label>
        {busyReason && (
          <span className="text-[11.5px] text-warn ml-auto">{t('schedule.busy_hint')}</span>
        )}
        {/* issue #164 追加: 「新建任务」入口移除 —— 排期是任务唯一入口,
            新建统一走「发起多智能体协作」弹窗 (submitTeamWork 入队本排期) */}
      </div>

      {/* 倒计时条: 仅 countdown.active 时渲染 (AC-5.1/A11) */}
      {cd && cd.active && (
        <div className="card px-4 py-3">
          <div className="flex items-center gap-3">
            <div className="flex-1">
              <div className="text-[11.5px] text-dim mb-1">
                {t('schedule.countdown.next')}
                {cd.next_task_title ? `: ${cd.next_task_title}` : ''}
              </div>
              <div className="h-2 rounded bg-panel2 overflow-hidden">
                <div className="h-full bg-accent transition-[width] duration-1000 ease-linear"
                     style={{ width: `${Math.max(0, Math.min(100, (cd.remaining_s / COUNTDOWN_TOTAL) * 100))}%` }} />
              </div>
            </div>
            <span className="text-sm tabular-nums text-ink w-14 text-right">{fmtCountdown(cd.remaining_s ?? 0)}</span>
            <button className="btn-primary btn-sm shrink-0" disabled={busy !== ''}
                    onClick={() => setConfirmRun(true)}>
              {t('schedule.btn.run_now')}
            </button>
          </div>
        </div>
      )}

      {err && <div className="card px-4 py-3 text-sm text-bad">{err}</div>}

      {empty && (
        <div className="card px-6 py-10 text-center text-sm text-dim">
          <div className="text-base mb-1">{t('schedule.empty.title')}</div>
          <div>{t('schedule.empty.hint')}</div>
        </div>
      )}

      {/* 分组1 执行中 */}
      {groups.running.length > 0 && <>
        {sectionTitle('schedule.group.running', groups.running.length)}
        <div className="space-y-2">{groups.running.map(renderTaskRow)}</div>
      </>}

      {/* 分组2 未执行 (pending+paused 混排 + cron 预览按时间参与排序) */}
      {merged.length > 0 && <>
        {sectionTitle('schedule.group.pending', groups.pending.length)}
        <div className="space-y-2">
          {merged.map((it) => it.kind === 'task'
            ? renderTaskRow(it.task)
            : renderCronRow(it.job, it.at))}
        </div>
      </>}

      {/* 分组3 已完成/取消 (底部, 前端截 50) */}
      {groups.done.length > 0 && <>
        {sectionTitle('schedule.group.done', groups.done.length)}
        <div className="space-y-2 opacity-80">{groups.done.map(renderTaskRow)}</div>
      </>}

      {/* 修改弹窗 (仅 paused): 详情 textarea(必填) + 优先级 select */}
      <Modal open={formOpen} onClose={() => !formSaving && setFormOpen(false)}
        title={t('schedule.form.edit_title')}
        width="max-w-[560px]"
        footer={
          <>
            <button className="btn-ghost btn-sm" disabled={formSaving} onClick={() => setFormOpen(false)}>
              {t('common.cancel')}
            </button>
            <button className="btn-primary btn-sm" disabled={formSaving || !formDetail.trim()} onClick={submitForm}>
              {formSaving ? (<><span className="spin" />{t('common.saving')}</>) : t('common.save')}
            </button>
          </>
        }>
        <div className="px-5 py-4 space-y-4">
          <div>
            <label className="label">{t('schedule.form.detail')} <span className="text-bad">*</span></label>
            <textarea value={formDetail} rows={6} disabled={formSaving}
              onChange={(e) => {
                const v = e.target.value
                setFormDetail(v)
                // issue #186: 详情变空 ⇒ 立即显式错误文案 (按钮仍置灰, #164 防呆不动);
                // 重新输入非空 ⇒ 清除错误。openEdit 已 setFormErr('') ⇒ 不会一进弹窗就红。
                if (!v.trim()) setFormErr(t('schedule.form.detail_required'))
                else setFormErr('')
              }}
              placeholder={t('schedule.form.detail_placeholder')}
              className="w-full resize-none rounded-xl bg-panel2 border border-line px-3.5 py-2.5 text-[13.5px] text-ink placeholder:text-dim/60 outline-none focus:ring-1 focus:ring-accent/50" />
          </div>
          <div>
            <label className="label">{t('schedule.form.priority')}</label>
            <select value={formPrio} disabled={formSaving}
              onChange={(e) => setFormPrio(e.target.value)}
              className="rounded-xl bg-panel2 border border-line px-3 py-2 text-[13.5px] text-ink outline-none focus:ring-1 focus:ring-accent/50">
              <option value="urgent">{t('schedule.prio.urgent')}</option>
              <option value="high">{t('schedule.prio.high')}</option>
              <option value="normal">{t('schedule.prio.normal')}</option>
              <option value="low">{t('schedule.prio.low')}</option>
            </select>
          </div>
          {formErr && <div className="text-[12.5px] text-bad">{formErr}</div>}
        </div>
      </Modal>

      {/* 立即执行二次确认 (AC-6.2: 文案含队首任务标题) */}
      <Confirm open={confirmRun}
        title={t('schedule.confirm.title')}
        okText={t('schedule.btn.run_now')}
        danger={false}
        text={t('schedule.confirm.body', { title: cd?.next_task_title || '—' })}
        onOk={doRunNow}
        onCancel={() => setConfirmRun(false)} />

      <Toast msg={toast?.msg} type={toast?.type || 'ok'} onDone={() => setToast(null)} />
    </div>
  )
}
