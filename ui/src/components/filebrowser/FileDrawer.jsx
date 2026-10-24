// FileDrawer.jsx — 文件浏览器抽屉容器 (issue #46, s3-arch §4.1)
// 设计依据: docs/UX-FILEBROWSER.md §1 + docs/ARCH-FILEBROWSER.md §4.1
import React, { useState, useEffect, useCallback, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { FileTree } from './FileTree.jsx'
import { FilePreview } from './FilePreview.jsx'
import { api } from '../../api.js'

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
          <span className="text-dim/50">/</span>
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

// 底部动作条 (UX §1.2 + AC-D3: 常驻可用)
function ActionBar({ project, selection }) {
  const { t } = useTranslation()
  if (!selection) return null
  const downloadUrl = api.fsFileUrl(project, selection.path, 'download')
  const openUrl = api.fsFileUrl(project, selection.path, 'open')
  return (
    <div className="shrink-0 flex items-center gap-2 px-4 py-2 border-t
                    border-line bg-panel/40">
      <a href={downloadUrl} download
         className="btn-ghost btn-sm flex items-center gap-1">
        <span aria-hidden="true">⬇</span> {t('chat.files.download')}
      </a>
      <a href={openUrl} target="_blank" rel="noreferrer noopener"
         className="btn-ghost btn-sm flex items-center gap-1">
        <span aria-hidden="true">↗</span> {t('chat.files.open_in_browser')}
      </a>
      <span className="ml-auto text-[10.5px] text-dim truncate font-mono"
            title={selection.path}>
        {selection.path}
      </span>
    </div>
  )
}

export function FileDrawer({ open, project, onClose, revealTarget, refreshKey }) {
  const { t } = useTranslation()
  const [selection, setSelection] = useState(null)
  const [refreshTick, setRefreshTick] = useState(0)
  const drawerRef = useRef(null)

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
            ⟳
          </button>
          <button onClick={onClose}
                  className="btn-quiet btn-sm !px-2"
                  title={t('chat.files.close')}
                  aria-label={t('chat.files.close')}>
            ✕
          </button>
        </div>
        {selection && <Crumbs path={selection.path}
                              onCrumbsClick={() => { /* 折叠树到该层 — FileTree 内部状态 */ }} />}
      </div>

      {/* 主体: 树 + 预览 (桌面双栏, 移动下钻) */}
      <div className="flex-1 min-h-0 flex flex-col md:flex-row">
        <div className="md:w-[220px] md:border-r md:border-line
                        md:block flex-shrink-0">
          <FileTree project={project}
                    onPick={setSelection}
                    revealPath={revealTarget || null}
                    refreshKey={refreshKey + refreshTick} />
        </div>
        <div data-fb-preview
             className="flex-1 min-h-0 flex flex-col bg-canvas/40">
          <FilePreview project={project} selection={selection} />
        </div>
      </div>

      {/* 底部动作条 */}
      <ActionBar project={project} selection={selection} />
    </div>
  )
}