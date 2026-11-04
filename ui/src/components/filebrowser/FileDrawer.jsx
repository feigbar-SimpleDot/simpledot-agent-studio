// FileDrawer.jsx — 文件浏览器抽屉容器 (issue #46, s3-arch §4.1)
// 设计依据: docs/UX-FILEBROWSER.md §1 + docs/ARCH-FILEBROWSER.md §4.1
import React, { useState, useEffect, useCallback, useRef } from 'react'
import { Icon, ICONS } from '../icons.jsx'
import { useTranslation } from 'react-i18next'
import { FileTree } from './FileTree.jsx'
import { FilePreview } from './FilePreview.jsx'
import { api, FsHttpError } from '../../api.js'

// 面包屑: 把选中路径拆段展示
function Crumbs({ path, onCrumbsClick }) {
  const { t } = useTranslation()
  if (!path) return null
  const parts = path.split('/').filter(Boolean)
  return (
    <div className="flex items-center gap-1 text-[11.5px] text-dim font-mono
                    truncate min-w-0">
      <button onClick={() => onCrumbsClick('')}
              className="hover:text-accent truncate">
        {t('chat.files.crumb_root')}
      </button>
      {parts.map((p, i) => (
        <React.Fragment key={i}>
          <span className="text-dim">/</span>
          <button onClick={() => onCrumbsClick(parts.slice(0, i + 1).join('/'))}
                  className={`hover:text-accent truncate ${i === parts.length - 1
                              ? 'text-ink font-medium' : ''}`}>
            {p}
          </button>
        </React.Fragment>
      ))}
    </div>
  )
}

// 体积人性化显示 (确认框展示用; 与 FilePreview 的展示口径独立, 只做删除确认)
function fmtBytes(n) {
  if (!Number.isFinite(n)) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

// issue #158: 删除二次确认对话框 (PLAN §2.4 第一层)。
// ★ 必须展示**完整相对路径** (同名文件跨目录常见, 只显示名会删错) + 大小 +
//   不可撤销提示; 不含绝对路径 (零回显纪律)。取消 ⇒ 零网络请求零副作用。
function DeleteConfirmDialog({ entry, busy, onCancel, onConfirm }) {
  const { t } = useTranslation()
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center
                    bg-black/50 p-4"
         role="alertdialog" aria-modal="true"
         aria-label={t('chat.files.delete_confirm_title')}>
      <div className="w-full max-w-sm rounded-xl border border-line bg-panel
                      shadow-2xl p-4 flex flex-col gap-3">
        <div className="text-[13px] font-medium text-ink">
          {t('chat.files.delete_confirm_title')}
        </div>
        <div className="text-[12px] text-dim leading-relaxed">
          {t('chat.files.delete_confirm_body', {
            path: entry.path, size: fmtBytes(entry.size),
          })}
        </div>
        <div className="flex items-center justify-end gap-2 pt-1">
          <button type="button" onClick={onCancel} disabled={busy}
                  className="btn-ghost btn-sm">
            {t('chat.files.delete_cancel')}
          </button>
          <button type="button" onClick={onConfirm} disabled={busy}
                  className="btn-ghost btn-sm !text-bad border border-bad/40">
            {busy ? '…' : t('chat.files.delete')}
          </button>
        </div>
      </div>
    </div>
  )
}

// 底部动作条 (UX §1.2 + AC-D3: 常驻可用; issue #158 加上传/删除)
function ActionBar({ project, selection, cwd, uploading, onPickFiles,
                     onRequestDelete }) {
  const { t } = useTranslation()
  // 上传目标: 选中目录 → 该目录; 否则当前浏览目录 cwd ("" = 项目根)
  const targetDir = (selection && selection.dir) ? selection.path : cwd
  const canDelete = selection && selection.dir === false
  return (
    <div className="shrink-0 flex items-center gap-2 px-4 py-2 border-t
                    border-line bg-panel/40">
      {/* issue #158: 上传入口 (隐藏 input, 选中即逐个 POST /api/fs/upload) */}
      <label className="btn-ghost btn-sm flex items-center gap-1 cursor-pointer"
             title={t('chat.files.upload_hint', { path: targetDir || '.' })}>
        <Icon d={ICONS.upload} className="w-3.5 h-3.5" />
        {uploading ? t('chat.files.uploading') : t('chat.files.upload')}
        <input type="file" multiple className="hidden" disabled={uploading}
               onChange={(e) => {
                 const files = Array.from(e.target.files || [])
                 e.target.value = ''  // 同名文件可连续选
                 if (files.length) onPickFiles(files)
               }} />
      </label>
      {canDelete && (
        <button type="button"
                className="btn-ghost btn-sm flex items-center gap-1 !text-bad"
                onClick={onRequestDelete}>
          <Icon d={ICONS.close} className="w-3.5 h-3.5" /> {t('chat.files.delete')}
        </button>
      )}
      {selection && (<>
      <a href={api.fsFileUrl(project, selection.path, 'download')} download
         className="btn-ghost btn-sm flex items-center gap-1">
        <Icon d={ICONS.download} className="w-3.5 h-3.5" /> {t('chat.files.download')}
      </a>
      <a href={api.fsFileUrl(project, selection.path, 'open')} target="_blank" rel="noreferrer noopener"
         className="btn-ghost btn-sm flex items-center gap-1">
        <Icon d={ICONS.external} className="w-3.5 h-3.5" /> {t('chat.files.open_in_browser')}
      </a>
      </>)}
      <span className="ml-auto text-[10.5px] text-dim truncate font-mono"
            title={selection ? selection.path : (cwd || '')}>
        {selection ? selection.path : (cwd || t('chat.files.crumb_root'))}
      </span>
    </div>
  )
}

export function FileDrawer({ open, project, onClose, revealTarget, refreshKey }) {
  const { t } = useTranslation()
  const [selection, setSelection] = useState(null)
  const [refreshTick, setRefreshTick] = useState(0)
  // issue #158: 当前浏览目录 ("" = 项目根), 上传目标; 由目录点击/面包屑更新
  const [cwd, setCwd] = useState('')
  const [uploading, setUploading] = useState(false)
  const [confirmDel, setConfirmDel] = useState(null)  // 非空 = 确认框打开
  const [delBusy, setDelBusy] = useState(false)
  const [fbMsg, setFbMsg] = useState(null)  // {kind:'ok'|'err', text}
  const drawerRef = useRef(null)
  const msgTimer = useRef(null)

  // D-1 修复 (L 批 s8b 回流, 依据 docs/TEST-REPORT-L-20261029.md §6 D-1):
  //   旧写法直接把 refreshKey 与 refreshTick 相加喂给 FileTree, 而 refreshKey
  //   prop 在当前唯一调用处 (ChatView.jsx 的 <FileDrawer>) 从未传入 ⇒
  //   undefined + n = NaN ⇒ React 的依赖比较用 Object.is(NaN, NaN) === true ⇒
  //   FileTree 的 useEffect([loadRoot, refreshKey]) 永不重跑 ⇒ 上传/删除/⟳
  //   三条路径后文件树都不自动刷新 (服务端状态正确, 仅 UX 失效, P2)。
  //   修法: 非有限数 (undefined / NaN / null / 非数) 一律归 0, 保证喂给
  //   FileTree 的刷新键恒为有限数 ⇒ 每次 refreshTick 递增都是真变化。
  //   ★ 兜底放在本组件内 (而非只在 ChatView 补传参): 调用方漏传 prop 是
  //     本缺陷的根因, 兜底必须在"做加法的地方"才对该缺陷类免疫。
  const treeRefreshKey = (Number.isFinite(refreshKey) ? refreshKey : 0) + refreshTick

  const flash = useCallback((kind, text) => {
    setFbMsg({ kind, text })
    if (msgTimer.current) clearTimeout(msgTimer.current)
    msgTimer.current = setTimeout(() => setFbMsg(null), 5000)
  }, [])
  useEffect(() => () => { if (msgTimer.current) clearTimeout(msgTimer.current) }, [])

  // FsHttpError.status → i18n 文案 (零回显: 不解析服务端 body)
  const errText = useCallback((status, kind) => {
    if (status === 409) return t('chat.files.upload_conflict')
    if (status === 413) return t('chat.files.upload_too_large')
    if (status === 400) return t('chat.files.err_confirm_required')
    if (status === 403) return t('chat.files.err_forbidden')
    if (status === 404) return t('chat.files.err_not_found')
    return kind === 'upload' ? t('chat.files.upload_failed',
                                  { reason: t('chat.files.err_server') })
                             : t('chat.files.delete_failed',
                                 { reason: t('chat.files.err_server') })
  }, [t])

  // 上传: 逐个 POST, 全部完成后刷新树 (AC-158-1)
  const doUpload = useCallback(async (files) => {
    setUploading(true)
    try {
      for (const f of files) {
        const r = await api.fsUpload(project, cwd, f)
        flash('ok', t('chat.files.upload_ok', { name: r.name }))
      }
      setRefreshTick((n) => n + 1)
    } catch (e) {
      flash('err', e instanceof FsHttpError
        ? errText(e.status, 'upload') : t('chat.files.err_network'))
    } finally {
      setUploading(false)
    }
  }, [project, cwd, flash, errText, t])

  // 删除 (确认框点"确认"才走到这里; 取消 = 零请求): POST + 刷新 + 清选择
  const doDelete = useCallback(async () => {
    if (!confirmDel) return
    setDelBusy(true)
    try {
      const r = await api.fsDelete(project, confirmDel.path)
      flash('ok', t('chat.files.delete_ok', { path: r.path }))
      setConfirmDel(null)
      setSelection(null)
      setRefreshTick((n) => n + 1)
    } catch (e) {
      flash('err', e instanceof FsHttpError
        ? errText(e.status, 'delete') : t('chat.files.err_network'))
    } finally {
      setDelBusy(false)
    }
  }, [project, confirmDel, flash, errText, t])

  // Esc 关闭 (焦点在抽屉外输入时不拦截 — UX §2.2, 由父容器决策)
  useEffect(() => {
    if (!open) return
    const onKey = (e) => {
      if (e.key !== 'Escape') return
      const a = document.activeElement
      const tag = a && (a.tagName || '').toLowerCase()
      if (tag === 'input' || tag === 'textarea') return
      if (a && a.isContentEditable) return
      onClose && onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  // 切换选择: 滚动到顶
  useEffect(() => {
    if (!drawerRef.current) return
    const main = drawerRef.current.querySelector('[data-fb-preview]')
    if (main) main.scrollTop = 0
  }, [selection?.path])

  if (!open || !project) return null
  const projectName = project.split('/').filter(Boolean).pop() || project

  return (
    <div ref={drawerRef}
         role="dialog"
         aria-label={t('chat.files.title')}
         className="fixed top-0 right-0 h-full w-screen md:w-auto md:w-[clamp(420px,42vw,560px)]
                    bg-panel border-l border-line shadow-2xl rounded-none md:rounded-l-2xl
                    flex flex-col z-40 animate-[slideIn_.24s_ease-out]">
      <style>{`
        @keyframes slideIn { from { transform: translateX(100%) } to { transform: translateX(0) } }
        @media (prefers-reduced-motion: reduce) {
          [role="dialog"] { animation: none !important }
        }
      `}</style>
      {/* 头部 */}
      <div className="shrink-0 border-b border-line bg-panel/60">
        <div className="flex items-center gap-2 px-4 py-2.5">
          <span className="text-[13px] font-mono font-medium text-ink truncate flex-1">
            {projectName}
          </span>
          <button onClick={() => setRefreshTick((n) => n + 1)}
                  className="btn-quiet btn-sm !px-2"
                  title={t('chat.files.refresh')}
                  aria-label={t('chat.files.refresh')}>
            <Icon d={ICONS.refresh} className="w-3.5 h-3.5" />
          </button>
          <button onClick={onClose}
                  className="btn-quiet btn-sm !px-2"
                  title={t('chat.files.close')}
                  aria-label={t('chat.files.close')}>
            <Icon d={ICONS.close} className="w-3.5 h-3.5" />
          </button>
        </div>
        {selection && <Crumbs path={selection.path}
                              onCrumbsClick={(p) => setCwd(p)} />}
      </div>

      {/* issue #158: 上传/删除结果轻提示 (5s 自动消失) */}
      {fbMsg && (
        <div role="status"
             className={`shrink-0 px-4 py-1.5 text-[11.5px] border-b border-line
                         ${fbMsg.kind === 'ok' ? 'text-ok' : 'text-bad'}`}>
          {fbMsg.text}
        </div>
      )}

      {/* 主体: 树 + 预览 (桌面双栏, 移动下钻) */}
      <div className="flex-1 min-h-0 flex flex-col md:flex-row">
        <div className="md:w-[220px] md:border-r md:border-line
                        md:block flex-shrink-0">
          <FileTree project={project}
                    onPick={setSelection}
                    onEnterDir={(p) => setCwd(p)}
                    revealPath={revealTarget || null}
                    refreshKey={treeRefreshKey} />
        </div>
        <div data-fb-preview
             className="flex-1 min-h-0 flex flex-col bg-canvas/40">
          <FilePreview project={project} selection={selection} />
        </div>
      </div>

      {/* 底部动作条 */}
      <ActionBar project={project} selection={selection} cwd={cwd}
                 uploading={uploading} onPickFiles={doUpload}
                 onRequestDelete={() => selection && setConfirmDel(selection)} />

      {/* issue #158: 删除二次确认 (取消 = 零请求零副作用, AC-158-2) */}
      {confirmDel && (
        <DeleteConfirmDialog entry={confirmDel} busy={delBusy}
                             onCancel={() => !delBusy && setConfirmDel(null)}
                             onConfirm={doDelete} />
      )}
    </div>
  )
}