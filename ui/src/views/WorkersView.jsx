import React, { useEffect, useRef, useState, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { api, fmtApiError } from '../api.js'
import { Modal, Confirm, Toast } from '../components/ui.jsx'

// ════════════════════════════════════════════════════════════════════
// WorkersView — i18n v1.0 conversion
//
// [en] Worker directory view. Protocol-derived status enum (WORKING/HANDOFF/
//      NEED_CLARIFICATION/WAITING) flows through workerStatus(); the display
//      label is translated via t('workers.status_*'). Form labels/hints/buttons/
//      errors all route through t(). The visual carries the same latin worker
//      names (no translation of those — they're match keys).
// [zh-cn] Worker 目录视图。协议派生态枚举(WORKING/HANDOFF/...)经 workerStatus;
//        显示标签走 t('workers.status_*');表单 label/hint/按钮/error 全走 t();
//        worker 名仍是拉丁键(匹配键保持不动)。
// ════════════════════════════════════════════════════════════════════

// D2 派生态 → 可视态映射 (issue #5 状态机统一): server 端
// ledger.derive_worker_state 的输出直接落到本面板四档可视态,
// 与 run 页 (TasksBoardView classify(t.state)) 同一派生输入源。
const DERIVED_TO_VISUAL = {
  WORKING: 'working', HANDOFF: 'working',
  // issue #132: waiting_sub 不再落空折到 idle/offline, 归入 subagent 可视态
  // (球在 worker 的子任务侧); 顺序与后端 _DERIVED_SALIENCE 同构便于对照。
  WAITING_SUB: 'subagent',
  NEED_CLARIFICATION: 'attention', BLOCKED: 'attention', ERROR: 'attention',
  WAITING: 'waiting',
}

// 单一权威优先 (VERIFY-20261013 D2): server 注入 derived_state (来自
// ledger.derive_worker_state) 时以其为准; 字段缺失 (worker 名下没卡)
// 才按 active_team_work_run_id 上下文降级 ——
//
//   1) entry.derived_state 存在 → 走派生态视觉映射 (WORKING/HANDOFF/
//      NEED_CLARIFICATION/BLOCKED/ERROR/WAITING 单源; issue #15)。
//   2) derived_state 缺失 + active_team_work_run_id 不存在 (无 ACTIVE
//      run) → worker 不在派生态, **绝不**靠 sse_bridge.live 决定 working
//      (issue #15: 跨 run SSE 残留导致的误激活根因)。
//   3) derived_state 缺失 + active_team_work_run_id 存在 → worker 不在
//      当前 run, 同样降级 idle (worker 在本 run 没活卡就不应 working)。
//
// FIX-15 (2026-10-17, issue #15): 把『工作中』的权威源限定为
// derived_state, sse_bridge.live 降为 SSE 辅助信号 (live 字段保留
// 在 entry 上以供 UI 装饰/调试, 但不再单独驱动徽章)。
function workerStatus(entry, activeRunId) {
  const dv = entry && entry.derived_state
  if (dv && DERIVED_TO_VISUAL[dv]) return DERIVED_TO_VISUAL[dv]
  // 派生为空 → 不在派生态 (FIX-15 worker 误激活):
  // 无 active run, 或本 worker 在 active run 名下没卡。
  // 直接走 online/offline, **不**看 entry.live.thinking (issue #15 根因)。
  if (entry && entry.online === true) return 'idle'
  if (entry && entry.online === false) return 'offline'
  return 'unknown'                            // online 字段缺失 → 中性兜底
}

// 可视映射: 颜色 + 图形 class + label 由 t() 翻译。
const STATUS_META = {
  working:   { i18nKey: 'workers.status_working',   cls: 'badge-blue',    gfx: 'wcard-gfx-working' },
  // issue #132: subagent = worker 在等自己的子任务, 复用 working 图形与忙碌族蓝,
  // 不再误显示 idle/offline/等介入。
  subagent:  { i18nKey: 'workers.status_waiting_sub', cls: 'badge-blue',  gfx: 'wcard-gfx-working' },
  attention: { i18nKey: 'workers.status_attention', cls: 'badge-yellow',  gfx: 'wcard-gfx-idle' },
  waiting:   { i18nKey: 'workers.status_waiting',   cls: 'badge-amber',   gfx: 'wcard-gfx-idle' },
  idle:      { i18nKey: 'workers.status_idle',      cls: 'badge-green',   gfx: 'wcard-gfx-idle' },
  offline:   { i18nKey: 'workers.status_offline',   cls: 'badge-slate',   gfx: 'wcard-gfx-offline' },
  unknown:   { i18nKey: 'workers.status_unknown',   cls: 'badge-unknown', gfx: 'wcard-gfx-unknown' },
}

// 状态徽章: 图形全部内联 span+CSS, 零图片 (DESIGN §3.4)
function WStatusBadge({ status }) {
  const m = STATUS_META[status] || STATUS_META.unknown
  const { t } = useTranslation()
  const label = t(m.i18nKey)
  return (
    <span className={`badge ${m.cls}`} role="status" aria-label={t('workers.status_aria', { status: label })}>
      <span className={`wcard-gfx ${m.gfx}`} />
      {label}
    </span>
  )
}

// ── 头像校验 (WCARD-REQ §6.2, 拒绝式不缩放, 文案逐字) ─────────────
// [en] All four validation strings (non-image, MIME/ext fail, oversized,
//      too many pixels) are i18n keys routed through t().
// [zh-cn] 头像校验的四种错误文案(非图片/MIME 失败/文件超限/像素超限)走 t()。
async function validateAvatar(file, t) {
  const AVATAR_EXT = ['png', 'jpg', 'jpeg', 'webp', 'gif']
  const AVATAR_MIME = ['image/png', 'image/jpeg', 'image/webp', 'image/gif']
  const ext = (file.name.split('.').pop() || '').toLowerCase()
  // 1) 非图片: 无 MIME 且扩展名不在白名单
  if (!file.type && !AVATAR_EXT.includes(ext)) return 'workers.err_avatar_not_image'
  // 2) 格式白名单: MIME 泛型(application/octet-stream 等)时按扩展名兜底
  if (!AVATAR_MIME.includes(file.type) && !AVATAR_EXT.includes(ext))
    return 'workers.err_avatar_format'
  // 3) 文件大小 ≤ 200KB
  if (file.size > 200 * 1024) return 'workers.err_avatar_too_large'
  // 4) 像素尺寸 ≤ 200×200 (Image 对象读 naturalWidth/Height)
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file)
    const img = new Image()
    img.onload = () => {
      URL.revokeObjectURL(url)
      if (img.naturalWidth > 200 || img.naturalHeight > 200)
        resolve('workers.err_avatar_pixels')
      else resolve(null)
    }
    img.onerror = () => { URL.revokeObjectURL(url); resolve('workers.err_avatar_not_image') }
    img.src = url
  })
}

// 占位符首字符: 无字母数字显示 ? (DESIGN §2.2)
function avatarInitial(name) {
  const c = (name || '').trim()[0]
  return c && /[A-Za-z0-9]/.test(c) ? c.toUpperCase() : '?'
}

// Issue #51 (2026-10-17): avatar 字段在 registry 里只存裸文件名
// (e.g. "coder-abcd1234.png"), 前端组装成 /api/workers/<name>/avatar URL。
// data: / 缺省/坏值形态一律退回 null, 触发 AvatarPicker broken 回退占位符。
// 缺 name 或编码异常也走 null —— `<img onError>` 兜底进一步接管坏图。
function avatarSrc(name, avatar) {
  if (!name || typeof avatar !== 'string') return null
  if (!avatar) return null
  if (avatar.startsWith('data:')) return null  // 旧 base64 残留, 后端已迁
  return `/api/workers/${encodeURIComponent(name)}/avatar`
}

// ── 头像选择器: 展示 + 上传入口 + 行内错误 (DESIGN §2.3 / §4) ─────
function AvatarPicker({ name, avatar, busy, error, onFile }) {
  const inputRef = useRef(null)
  const [broken, setBroken] = useState(false)
  const { t } = useTranslation()
  useEffect(() => { setBroken(false) }, [avatar])   // 头像换了就重试展示
  const errMessage = error ? t(error, error.startsWith('workers.err_avatar_pixels')
    ? { w: '200×200', ow: 'see browser' } : {}) : ''
  // Issue #51: entry.avatar 改为裸文件名 → 走 /api/workers/<name>/avatar URL。
  // 仍 null-safe: 缺值 / 坏值 / data: 残留 → 显示首字符占位 (DESIGN §2.2)。
  const src = avatarSrc(name, avatar)
  return (
    <div className="shrink-0 flex flex-col items-center gap-1 max-w-[180px]">
      <button type="button" className="wcard-avatar" aria-label={t('workers.avatar_change_aria', { name })}
        onClick={() => !busy && inputRef.current?.click()}>
        {src && !broken ? (
          <img src={src} alt="" onError={() => setBroken(true)} />
        ) : (
          <span className="wcard-avatar-ph">{avatarInitial(name)}</span>
        )}
        <span className="wcard-avatar-mask">📷 {t('workers.avatar_change_mask')}</span>
        {busy && <span className="wcard-spin" />}
      </button>
      <input ref={inputRef} type="file" className="hidden"
        accept="image/png,image/jpeg,image/webp,image/gif"
        onChange={(ev) => { const f = ev.target.files?.[0]; ev.target.value = ''; if (f) onFile(f) }} />
      {error && <div className="text-bad text-[11.5px] text-center leading-snug">{errMessage || error}</div>}
    </div>
  )
}

// Pixel-size errors carry dynamic pixel counts. Keep t('workers.err_avatar_pixels',
// {w: '200×200'}) available locally so the line above stays consistent.
function pixelErrText(t, w, h) {
  return t('workers.err_avatar_pixels', { w: '200×200', actual: `${w}×${h}` })
}

// ── 单张 Worker 卡片 (DESIGN §1.2) ───────────────────────────────
function WorkerCard({ name, entry, activeRunId, onAvatar, onView, onChat, onEdit, onSoul, onDelete }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const { t } = useTranslation()
  const clearErrSoon = () => setTimeout(() => setErr(''), 5000)

  // 校验 → FileReader 转 base64 → 乐观保存, 失败回滚(不写本地)
  const handleFile = async (file) => {
    setBusy(true); setErr('')
    const v = await validateAvatar(file, t)
    if (v) { setBusy(false); setErr(v); clearErrSoon(); return }
    const reader = new FileReader()
    reader.onload = async () => {
      try {
        await onAvatar(name, String(reader.result))
        setBusy(false)
      } catch (e) {
        setBusy(false)
        setErr(`workers.save_failed_inline`) // marker; rendered via t()
        clearErrSoon()
      }
    }
    reader.onerror = () => {
      setBusy(false)
      setErr('workers.read_file_failed')
      clearErrSoon()
    }
    reader.readAsDataURL(file)
  }

  // FIX-15: activeRunId 作为 workerStatus 第二参数, 让派生为空时
  // 显式知道『无 active run / worker 不在 run』, 杜绝 SSE 残留误激活。
  const st = workerStatus(entry, activeRunId)
  const desc = entry.description || ''
  const tags = entry.scope?.tags || []
  const excludes = entry.scope?.excludes || []
  return (
    <div className="card wcard p-4 flex flex-col gap-3 min-w-0">
      {/* 头部行: 头像 + 名称/简介 + 状态徽章(ml-auto 永不被挤) */}
      <div className="flex gap-3 min-w-0">
        <AvatarPicker name={name} avatar={entry.avatar} busy={busy} error={err} onFile={handleFile} />
        <div className="flex-1 min-w-0 flex flex-col gap-1">
          <div className="flex items-center gap-2 min-w-0">
            <span className="badge-blue shrink-0" title={name}>{name}</span>
            <span className="ml-auto shrink-0"><WStatusBadge status={st} /></span>
          </div>
          {desc && (
            <p className="text-[12px] text-dim line-clamp-2" title={desc}>
              {desc}
            </p>
          )}
        </div>
      </div>
      {/* 能力标签行 (同现状: tags 绿 / excludes 红 / 均无「未标注」) */}
      <div className="flex flex-wrap gap-1">
        {tags.map((tg) => (
          <span key={tg} className="badge-green text-[10px]"
                title={t('workers.tools_label')}>{tg}</span>
        ))}
        {excludes.map((ex) => (
          <span key={'x' + ex} className="badge-red text-[10px]"
                title={t('workers.tools_excludes_label')}>{ex}</span>
        ))}
        {!tags.length && !excludes.length && (
          <span className="text-dim text-[11px]">{t('workers.tools_unknown')}</span>
        )}
      </div>
      {/* 操作区: flex-wrap 保证 375px 换行不重叠 (AC-02) */}
      <div className="border-t border-line/60 pt-2.5 flex flex-wrap gap-2">
        <button className="btn-ghost btn-sm" onClick={() => onView(name)}>👁 {t('workers.view_btn')}</button>
        {entry.api_url && onChat && (
          <button className="btn-ghost btn-sm" onClick={() => onChat(name)}>💬 {t('workers.chat_btn')}</button>
        )}
        <button className="btn-ghost btn-sm" onClick={() => onEdit(name)}>✎ {t('workers.edit_btn')}</button>
        <button className="btn-ghost btn-sm" onClick={() => onSoul(name, entry.soul)}>📜 SOUL</button>
        <button className="btn-danger btn-sm" onClick={() => onDelete(name)}>{t('workers.delete_btn')}</button>
      </div>
    </div>
  )
}

// [en] WorkerDetail — read-only view modal body. Card-specific strings
//      (effective model, registry, alias, toolsets, gating note) all t().
// [zh-cn] WorkerDetail —— 只读视图。卡片专属文案(生效模型/注册表/别名/工具集/隔离说明)走 t()。
function WorkerDetail({ d, t }) {
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <span className={d.online ? 'dot-ok' : 'dot-bad'} />
        <span className="text-sm font-medium">{d.online ? t('workers.online_label') : t('workers.offline_label')}</span>
      </div>

      <div>
        <div className="label">{t('workers.effective_model_label')} <span className="text-dim font-normal">/api/model/options</span></div>
        {d.actual_model
          ? <div><span className="badge-green">{d.actual_model}</span> <span className="text-dim text-xs">via {d.actual_provider || '?'}</span></div>
          : <div className="text-dim text-xs">{t('workers.fetch_failed_label')}{d.actual_model_error ? `（${d.actual_model_error}）` : ''}</div>}
      </div>

      <div>
        <div className="label">{t('workers.registry_label')}（registry.json）</div>
        <div className="card overflow-hidden">
          <table className="tbl">
            <tbody>
              {Object.entries(d.registry || {}).map(([k, v]) => (
                <tr key={k}>
                  <td className="text-dim text-xs w-32" title={k}>{k}</td>
                  {/* Issue #51: avatar 改裸文件名形态, 不再含 base64。
                      服务端已迁, 此处不再需要 base64 截断展示逻辑;
                      filename 形态直接展示, 复制按钮自然可命中。 */}
                  <td className="mono text-[12px] break-all max-w-[420px]"
                      title={String(v)}>
                    {String(v)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div>
        <div className="label">{t('workers.field.model_alias_label')} <span className="text-dim font-normal">/v1/models</span></div>
        <div className="flex flex-wrap gap-1.5">
          {(d.models?.data || []).map((m) => <span key={m.id || m.name} className="badge-slate">{m.id || m.name}</span>)}
          {!d.models_ok && <span className="text-dim text-xs">{t('workers.fetch_failed_label')}</span>}
        </div>
        <div className="hint">{t('workers.field.model_alias_hint_alias')}</div>
      </div>

      <div>
        <div className="label">{t('workers.field.tools_label')} <span className="text-dim font-normal">/v1/toolsets</span></div>
        <div className="flex flex-wrap gap-1.5">
          {(d.toolsets?.data || []).filter((tg) => tg.enabled).map((tg) => (
            <span key={tg.name} className="badge-blue" title={(tg.tools || []).join(', ')}>{tg.name}</span>
          ))}
          {!d.toolsets_ok && <span className="text-dim text-xs">{t('workers.fetch_failed_label')}</span>}
        </div>
      </div>

      <div className="rounded-xl border-l-4 border-accent bg-accent/5 px-4 py-3 text-[12px] text-dim leading-relaxed">
        ℹ️ {t('workers.detail_soul_isolation_note')}
      </div>
    </div>
  )
}

export default function WorkersView({ onChanged, onChat }) {
  const { t } = useTranslation()
  const [workers, setWorkers] = useState(null)
  // FIX-15 (issue #15 worker 误激活): 跟踪当前 active_team_work_run_id
  // (顶层字段), 让 workerStatus 在 derived_state 缺失时按上下文降级,
  // 而不是靠 sse_bridge.live 决定 working。
  const [activeRunId, setActiveRunId] = useState(null)
  const [err, setErr] = useState('')
  const [dlg, setDlg] = useState(null) // {mode:'register'|'compose'|'view'|'edit', ...}
  const [soulDlg, setSoulDlg] = useState(null) // {name, content, sending, msg}
  const [del, setDel] = useState(null)
  const [toast, setToast] = useState('')
  const [paused, setPaused] = useState(false) // 页面隐藏: 停轮询 + 停动效

  const load = useCallback(async () => {
    try {
      const d = await api.workers()
      setWorkers(d.workers || {})
      setActiveRunId(d.active_team_work_run_id ?? null)
      setErr('')
    } catch (e) { setErr(fmtApiError(e)) }  // issue #75 §3: 三态归因 (network/server)
  }, [])

  // 5s 轮询 + visibilitychange: 隐藏停轮询停动效, 回前台立即刷新 (DESIGN §5, AC-09)
  useEffect(() => {
    load()
    let timer = setInterval(load, 5000)
    const onVis = () => {
      if (document.hidden) {
        clearInterval(timer); timer = null; setPaused(true)
      } else {
        setPaused(false); load()
        if (!timer) timer = setInterval(load, 5000)
      }
    }
    document.addEventListener('visibilitychange', onVis)
    return () => { clearInterval(timer); document.removeEventListener('visibilitychange', onVis) }
  }, [load])

  const saveWorkers = async (next) => {
    await api.saveWorkers(next)
    setWorkers(next)
    onChanged?.()
  }

  // 头像保存 (Issue #51): 仍走 PUT /api/workers, 客户端仍发 data URL
  // (FileReader.readAsDataURL 现行链路零改动), 服务端落盘 + 回写文件名。
  // 保存成功后从服务端再拉一次 registry (load()), 拿回最新文件名映射,
  // 本地 state 不再持有 base64。
  const saveAvatar = async (name, dataUrl) => {
    const prev = workers?.[name]
    if (!prev) throw new Error('worker not found')
    const next = { ...workers, [name]: { ...prev, avatar: dataUrl } }
    await api.saveWorkers(next)
    // 服务端已落盘 + 替换成文件名, 重新拉 registry 拿真实 avatar 字段。
    await load()
    onChanged?.()
    setToast(t('workers.avatar_updated_toast'))
  }

  const openSoul = async (name, current) => {
    // 优先显示 worker 当前 SOUL(registry 里存过的), 否则拉通用模板预填
    setSoulDlg({ name, content: current || '', sending: false,
                 msg: current ? '' : t('common.loading') })
    if (!current) {
      try {
        const r = await api.soulTemplate()
        setSoulDlg((d) => (d && d.name === name ? {
          ...d,
          content: r.ok ? r.content : '',
          msg: r.ok ? t('workers.avatar_preset_toast') : `✗ ${r.error}`,
        } : d))
      } catch (e) {
        setSoulDlg((d) => (d && d.name === name
          // issue #84: B-2 弹窗族错误纳入三态归因 (保留 ✗ toast 前缀)
          ? { ...d, msg: `✗ ${fmtApiError(e)}` } : d))
      }
    }
  }

  const doRegister = async () => {
    const f = dlg.f || {}
    const name = (f.name || '').trim()
    if (!name) { setDlg((d) => ({ ...d, error: t('workers.err_name_required') })); return }
    // 名称会被拼到 URL 路径 /api/w/<name>/... 里, 中文会被 URL 编码,
    // 后端 BaseHTTPRequestHandler 不解码百分号 → 必然查不到 worker。
    // 前端直接禁掉,避免后端误导性错误「未注册或缺少 api_url」。
    if (!/^[A-Za-z0-9_.\-]+$/.test(name)) {
      setDlg((d) => ({ ...d, error: t('workers.err_name_charset') })); return
    }
    if (!(f.api_url || '').trim()) { setDlg((d) => ({ ...d, error: t('workers.err_api_url_required') })); return }
    if ((f.api_key || '').trim().length < 16) { setDlg((d) => ({ ...d, error: t('workers.err_api_key_required_length') })); return }
    const isEdit = dlg.mode === 'edit'
    try {
      const next = { ...workers }
      if (isEdit && dlg.origName && dlg.origName !== name) delete next[dlg.origName]
      const tags = (f.tags || '').split(',').map((s) => s.trim()).filter(Boolean)
      const excludes = (f.excludes || '').split(',').map((s) => s.trim()).filter(Boolean)
      const entry = {
        runner: 'api',
        api_url: f.api_url.trim(),
        api_key: f.api_key.trim(),
      }
      // 简介: main 拆解时读它做语义匹配(比裸标签更有料), 有内容才写
      const desc = (f.description || '').trim()
      if (desc) entry.description = desc
      // 能力边界: 有任一标签才写 scope, 保持 registry 干净
      if (tags.length || excludes.length) {
        entry.scope = { tags, excludes }
      }
      // 头像: 编辑回填防冲掉; 移除(清空)则不写字段 → 回落占位符 (DESIGN §8.1)
      if (f.avatar) entry.avatar = f.avatar
      next[name] = entry
      await saveWorkers(next)
      setDlg(null)
      setToast(isEdit ? t('workers.edit_success', { name }) : t('workers.register_success', { name }))
    } catch (e) { setDlg((d) => ({ ...d, error: t('workers.save_failed', { err: fmtApiError(e) }) })) }
  }

  const doCompose = async () => {
    try {
      const f = dlg.f || {}
      const d = await api.genCompose({
        name: (f.name || '').trim(), api_key: (f.api_key || '').trim(),
      })
      setDlg((d2) => ({ ...d2, yaml: d.yaml, steps: d.next_steps }))
      navigator.clipboard?.writeText(d.yaml).catch(() => {})
      setToast(t('workers.avatar_copied_toast'))
    } catch (e) { setDlg((d2) => ({ ...d2, error: t('workers.save_failed', { err: fmtApiError(e) }) })) }
  }

  const openView = async (name) => {
    setDlg({ mode: 'view', name, detail: null, loading: true })
    try {
      const d = await api.workerDetail(name)
      setDlg((p) => (p && p.mode === 'view' && p.name === name ? { ...p, detail: d, loading: false } : p))
    } catch (e) {
      // issue #84 (主点): 弹窗错误路径纳入三态归因 (与 load() :329 同口径)。
      // fmtApiError 对非 ApiError 逐字等价旧写法, 对 ApiError 给出
      // 4xx/5xx/断网 归因文案 + 端点标签 (/api/workers/:name)。
      setDlg((p) => (p && p.mode === 'view' ? { ...p, loading: false, error: fmtApiError(e) } : p))
    }
  }

  const openEdit = (name) => {
    const e = workers[name] || {}
    setDlg({
      mode: 'edit', origName: name,
      f: { name, api_url: e.api_url || '', api_key: e.api_key || '',
           description: e.description || '',
           tags: (e.scope?.tags || []).join(', '),
           excludes: (e.scope?.excludes || []).join(', '),
           avatar: e.avatar || '' },
    })
  }

  if (err && !workers)
    // issue #75 §3: 错误态带重试 (骨架已存在, 见下 !workers 分支)
    return (
      <div className="empty flex flex-col items-center gap-2">
        <span className="field-err text-[12.5px]">{err}</span>
        <button className="btn-ghost btn-sm" onClick={load}>{t('common.retry')}</button>
      </div>
    )
  if (!workers)
    return (
      // 骨架屏即卡片轮廓, 加载完成无布局跳变 (DESIGN §6.1)
      <div className="grid gap-4 grid-cols-1 md:grid-cols-2 xl:grid-cols-3">
        {[...Array(6)].map((_, i) => (
          <div key={i} className="card p-4 space-y-3">
            <div className="flex gap-3">
              <div className="skeleton w-14 h-14 rounded-full shrink-0" />
              <div className="flex-1 space-y-2 py-1">
                <div className="skeleton h-5 w-1/2" />
                <div className="skeleton h-3 w-3/4" />
              </div>
            </div>
            <div className="skeleton h-4 w-2/3" />
            <div className="skeleton h-8 w-full" />
          </div>
        ))}
      </div>
    )

  const list = Object.entries(workers)

  return (
    <div className="space-y-5">
      {/* 工具栏 */}
      <div className="flex flex-wrap items-center gap-2.5">
        <button className="btn-primary" onClick={() => setDlg({ mode: 'register', f: {} })}>
          <svg viewBox="0 0 24 24" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>
          {t('workers.register_btn')}
        </button>
        <button className="btn-ghost" onClick={() => setDlg({ mode: 'compose', f: {} })}>
          🐳 {t('workers.dialog_new_title')}
        </button>
        <span className="text-[11.5px] text-dim ml-1">{t('workers.toolbar_hint')}</span>
      </div>

      {/* Worker 卡片墙: 1/2/3 列响应式 (DESIGN §1.1) */}
      {list.length ? (
        <div className={`grid gap-4 grid-cols-1 md:grid-cols-2 xl:grid-cols-3 wcard-grid wcard-touch${paused ? ' anim-stopped' : ''}`}>
          {list.map(([name, e]) => (
            // key 只用 worker 名: 同状态轮询刷新不重置动效周期 (REQ §5.4)
            // FIX-15: activeRunId 透传, workerStatus 据此判定派生态。
            <WorkerCard key={name} name={name} entry={e}
              activeRunId={activeRunId}
              onAvatar={saveAvatar}
              onView={openView}
              onChat={onChat ? () => onChat(name) : null}
              onEdit={openEdit}
              onSoul={openSoul}
              onDelete={setDel} />
          ))}
        </div>
      ) : (
        <div className="empty">
          <span className="text-3xl">🗂</span>
          {t('workers.empty')}
        </div>
      )}

      {/* 注册/编辑弹窗 (edit 模式回填现有值, 改名时自动摘除旧 key) */}
      <Modal open={dlg?.mode === 'register' || dlg?.mode === 'edit'} onClose={() => setDlg(null)}
        title={dlg?.mode === 'edit' ? t('workers.dialog_edit_title', { name: dlg?.origName || '' }) : t('workers.dialog_register_title')}
        footer={
          <>
            <button className="btn-ghost" onClick={() => setDlg(null)}>{t('common.cancel')}</button>
            <button className="btn-primary" onClick={doRegister}>
              {dlg?.mode === 'edit' ? t('workers.save_edit_btn') : t('workers.save_register_btn')}
            </button>
          </>
        }>
        {(dlg?.mode === 'register' || dlg?.mode === 'edit') && (
          <div className="px-5 py-3 space-y-3.5">
            <p className="hint !mt-0">
              {dlg?.mode === 'edit'
                ? t('workers.dialog_edit_subtitle')
                : t('workers.dialog_register_subtitle')}
            </p>
            {/* 头像管理: 仅编辑态, 移除入口在此防误触 (DESIGN §2.4) */}
            {dlg.mode === 'edit' && (
              <div className="flex items-center gap-3 rounded-xl border border-line bg-panel2/40 px-3 py-2.5">
                <div className="w-10 h-10 rounded-full overflow-hidden shrink-0 ring-1 ring-inset ring-line">
                  {/* Issue #51: 编辑弹窗预览走 avatarSrc (filename→URL)。
                      dlg.f.avatar 在编辑回填时是裸文件名, 新上传未提交时可能
                      是 data URL (handleFile 即时预览, 提交后服务端落盘)。
                      兼容两种形态。 */}
                  {(() => {
                    const editSrc = avatarSrc(dlg.f.name || dlg.origName, dlg.f.avatar)
                      || (typeof dlg.f.avatar === 'string' && dlg.f.avatar.startsWith('data:')
                          ? dlg.f.avatar : null)
                    return editSrc ? (
                      <img src={editSrc} alt="" className="w-full h-full object-cover" />
                    ) : (
                      <span className="wcard-avatar-ph w-10 h-10 text-[16px]">{avatarInitial(dlg.f.name || dlg.origName)}</span>
                    )
                  })()}
                </div>
                <span className="text-[12px] text-dim flex-1">
                  {dlg.f.avatar ? t('workers.avatar_current') : t('workers.avatar_none')}
                </span>
                {dlg.f.avatar && (
                  <button className="btn-quiet btn-sm"
                    onClick={() => setDlg((d) => ({ ...d, f: { ...d.f, avatar: '' } }))}>
                    {t('workers.avatar_remove')}
                  </button>
                )}
              </div>
            )}
            <div>
              <label className="label">{t('workers.field.name')} *</label>
              <input className="input" placeholder="e.g. reviewer"
                defaultValue={dlg.f.name || ''}
                onChange={(e) => setDlg((d) => ({ ...d, f: { ...d.f, name: e.target.value } }))} />
              <div className="hint">{t('workers.field.name_hint')}</div>
            </div>
            <div>
              <label className="label">{t('workers.field.api_url')} *</label>
              <input className="input" placeholder="http://hermes_<name>:8642"
                defaultValue={dlg.f.api_url || ''}
                onChange={(e) => setDlg((d) => ({ ...d, f: { ...d.f, api_url: e.target.value } }))} />
              <div className="hint">{t('workers.field.api_url_hint')}</div>
            </div>
            <div>
              <label className="label">{t('workers.field.api_key')} *</label>
              <input className="input" placeholder={t('workers.field.api_key_hint')}
                defaultValue={dlg.f.api_key || ''}
                onChange={(e) => setDlg((d) => ({ ...d, f: { ...d.f, api_key: e.target.value } }))} />
            </div>
            <div>
              <label className="label">{t('workers.field.tagline_label')}</label>
              <textarea className="input" rows={2}
                placeholder={t('workers.field.tagline_hint')}
                defaultValue={dlg.f.description || ''}
                onChange={(e) => setDlg((d) => ({ ...d, f: { ...d.f, description: e.target.value } }))} />
            </div>
            <div>
              <label className="label">{t('workers.tools_label')}</label>
              <input className="input" placeholder="e.g. code-write, test, analysis"
                defaultValue={dlg.f.tags || ''}
                onChange={(e) => setDlg((d) => ({ ...d, f: { ...d.f, tags: e.target.value } }))} />
              <div className="hint">{t('workers.tags_hint')}</div>
            </div>
            <div>
              <label className="label">{t('workers.tools_excludes_label')}</label>
              <input className="input" placeholder="e.g. prod-deploy, security-audit"
                defaultValue={dlg.f.excludes || ''}
                onChange={(e) => setDlg((d) => ({ ...d, f: { ...d.f, excludes: e.target.value } }))} />
              <div className="hint">{t('workers.excludes_hint')}</div>
            </div>
            {dlg.error && <div className="text-bad text-xs">{dlg.error}</div>}
          </div>
        )}
      </Modal>

      {/* compose 生成弹窗 */}
      <Modal open={dlg?.mode === 'compose'} onClose={() => setDlg(null)}
        title={`🐳 ${t('workers.dialog_new_title')}`} width="max-w-2xl"
        footer={
          <>
            <button className="btn-ghost" onClick={() => setDlg(null)}>{t('common.close')}</button>
            <button className="btn-primary" onClick={doCompose}>
              {dlg?.yaml ? t('workers.regenerate_avatar_btn') : t('workers.register_avatar_btn')}
            </button>
          </>
        }>
        {dlg?.mode === 'compose' && (
          <div className="px-5 py-3 space-y-3.5">
            <p className="hint !mt-0">{t('workers.compose_subtitle')}</p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label className="label">{t('workers.field.name')} *</label>
                <input className="input" placeholder="e.g. reviewer"
                  onChange={(e) => setDlg((d) => ({ ...d, f: { ...d.f, name: e.target.value } }))} />
              </div>
              <div>
                <label className="label">{t('workers.field.api_key')} *</label>
                <input className="input" placeholder={t('workers.compose_api_key_hint')}
                  onChange={(e) => setDlg((d) => ({ ...d, f: { ...d.f, api_key: e.target.value } }))} />
              </div>
            </div>
            {dlg.yaml && (
              <>
                <div className="code-block max-h-[38vh]">{dlg.yaml}</div>
                <div className="rounded-xl border-l-4 border-accent bg-accent/5 px-4 py-3 text-[12.5px] text-dim leading-relaxed">
                  {dlg.steps?.map((s, i) => <div key={i}>▸ {s}</div>)}
                </div>
              </>
            )}
            {dlg.error && <div className="text-bad text-xs">{dlg.error}</div>}
          </div>
        )}
      </Modal>

      {/* 查看弹窗 */}
      <Modal open={dlg?.mode === 'view'} onClose={() => setDlg(null)}
        title={`👁 ${t('workers.view_btn')}: ${dlg?.name || ''}`} width="max-w-xl">
        {dlg?.mode === 'view' && (
          <div className="px-5 py-3">
            {dlg.loading && <div className="space-y-2 py-4">{[...Array(4)].map((_, i) => <div key={i} className="skeleton h-8" />)}</div>}
            {dlg.error && !dlg.detail && <div className="text-bad text-sm py-4">{dlg.error}</div>}
            {dlg.detail && <WorkerDetail d={dlg.detail} t={t} />}
          </div>
        )}
      </Modal>

      <Confirm open={!!del} text={t('workers.confirm_delete_text', { name: del })}
        title={t('workers.confirm_delete_title')}
        onCancel={() => setDel(null)}
        onOk={async () => {
          try {
            await api.deleteWorker(del)   // 专用 DELETE: 只摘除目标, 不做全表校验
            setWorkers((w) => { const n = { ...w }; delete n[del]; return n })
            setDel(null); setToast(t('workers.delete_success', { name: del }))
            onChanged?.()
          } catch (e) { setToast(t('workers.delete_failed', { err: fmtApiError(e) })) }
        }} />

      <Modal open={!!soulDlg} onClose={() => !soulDlg?.sending && setSoulDlg(null)}
        title={soulDlg ? `📜 ${t('workers.soul_modal_title', { name: soulDlg.name })}` : ''}
        width="max-w-3xl"
        footer={
          <>
            <button className="btn-ghost" onClick={() => setSoulDlg(null)}
              disabled={soulDlg?.sending}>{t('common.cancel')}</button>
            <button className="btn-primary"
              onClick={async () => {
                if (!soulDlg?.content?.trim()) {
                  setSoulDlg((d) => ({ ...d, msg: t('workers.soul_empty') })); return
                }
                setSoulDlg((d) => ({ ...d, sending: true, msg: t('workers.deploying') }))
                try {
                  const r = await api.workerSoulInit(soulDlg.name, soulDlg.content)
                  setSoulDlg((d) => ({
                    ...d, sending: false,
                    msg: r.ok
                      ? t('workers.soul_init_ok', { name: soulDlg.name, reply: (r.worker_reply || '').slice(0, 200) })
                      : t('workers.soul_init_failed_msg', { err: r.error }),
                  }))
                } catch (e) {
                  setSoulDlg((d) => ({ ...d, sending: false, msg: t('workers.soul_init_failed_msg', { err: fmtApiError(e) }) }))
                }
              }}
              disabled={soulDlg?.sending}>
              {soulDlg?.sending ? t('workers.deploying') : `🚀 ${t('workers.deploy_to_worker_btn')}`}
            </button>
          </>
        }>
        {soulDlg && (
          <div className="px-5 py-3 space-y-3">
            <div className="hint">
              {t('workers.soul_body_hint')}
            </div>
            <textarea className="input font-mono text-[12px] h-[50vh]"
              placeholder={t('workers.soul_modal_placeholder')}
              value={soulDlg.content}
              onChange={(e) => setSoulDlg((d) => ({ ...d, content: e.target.value, msg: '' }))} />
            <div className={`text-[12px] ${soulDlg.msg?.startsWith('✓') ? 'text-ok' :
              soulDlg.msg?.startsWith('✗') ? 'text-bad' : 'text-dim'}`}>
              {soulDlg.msg || t('workers.soul_default_hint')}
            </div>
          </div>
        )}
      </Modal>

      <Toast msg={toast} onDone={() => setToast('')} />
    </div>
  )
}
