import React, { useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'

// [en] ui.jsx — Modal / Toast / Confirm primitives shared across views.
//      Title/aria/button labels are routed through i18n so views can keep
//      passing static copy through while the modal still reads in the
//      active language. (See design.md §10.)
// [zh-cn] ui.jsx —— Modal/Toast/Confirm 基础控件,跨视图共用。
//        标题/aria/按钮文案走 i18n,调用方传静态文案,弹窗照当前语言渲染。

// 通用弹窗: 替代原生 <dialog>, 带遮罩模糊 + 弹入动画
export function Modal({ open, onClose, title, children, footer, width = 'max-w-lg' }) {
  const ref = useRef(null)
  const { t } = useTranslation()
  useEffect(() => {
    if (!open) return
    const onKey = (e) => e.key === 'Escape' && onClose?.()
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4"
         onMouseDown={(e) => e.target === e.currentTarget && onClose?.()}>
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm animate-[fadeIn_.15s_ease]" />
      <div ref={ref}
           className={`relative ${width} w-full card shadow-2xl animate-[popIn_.18s_cubic-bezier(.34,1.56,.64,1)]
                      max-h-[88vh] flex flex-col`}>
        <div className="flex items-center justify-between px-5 py-4 border-b border-line shrink-0">
          <h3 className="text-[14.5px] font-bold">{title}</h3>
          <button onClick={onClose} className="btn-quiet btn-sm !px-2"
                  aria-label={t('common.close')}>✕</button>
        </div>
        <div className="flex-1 overflow-y-auto py-1">{children}</div>
        {footer && (
          <div className="flex items-center justify-end gap-2.5 px-5 py-4 border-t border-line shrink-0">
            {footer}
          </div>
        )}
      </div>
      <style>{`
        @keyframes fadeIn { from { opacity: 0 } to { opacity: 1 } }
        @keyframes popIn { from { opacity: 0; transform: scale(.96) translateY(8px) } to { opacity: 1; transform: scale(1) translateY(0) } }
      `}</style>
    </div>
  )
}

// Toast 轻提示
export function Toast({ msg, type = 'ok', onDone }) {
  useEffect(() => {
    if (!msg) return
    const t = setTimeout(onDone, 3200)
    return () => clearTimeout(t)
  }, [msg, onDone])
  if (!msg) return null
  const cls = type === 'ok' ? 'border-ok/50 text-ok' : type === 'bad' ? 'border-bad/50 text-bad' : 'border-warn/50 text-warn'
  return (
    <div className={`fixed bottom-6 left-1/2 -translate-x-1/2 z-[60] card px-4 py-2.5 text-sm
                   border ${cls} shadow-2xl animate-[slideUp_.2s_ease]`}>
      {msg}
      <style>{`@keyframes slideUp { from { opacity:0; transform: translate(-50%, 12px) } to { opacity:1; transform: translate(-50%,0) } }`}</style>
    </div>
  )
}

// 确认框 (title/okText 可选, 供看板大白话文案定制; 默认值走 t() 保证 en/zh-cn 都翻)
// [en] Confirm — modal-style yes/no. Defaults to translated 'Confirm action' /
//      'Confirm' / 'Cancel'; views usually override title/okText with task
//      intent (Finish, Archive, Cancel, ...).
// [zh-cn] Confirm —— 弹窗式确认。默认文案走 t(),调用方一般传具体意图的 title。
export function Confirm({ open, title, okText, text, onOk, onCancel, danger = true }) {
  const { t } = useTranslation()
  return (
    <Modal open={open} onClose={onCancel}
      title={title ?? t('common.confirm_action')} width="max-w-sm"
      footer={
        <>
          <button className="btn-ghost" onClick={onCancel}>{t('common.cancel')}</button>
          <button className={danger ? 'btn-danger' : 'btn-primary'} onClick={onOk}>
            {okText ?? t('common.confirm')}
          </button>
        </>
      }>
      <p className="px-5 py-4 text-sm text-dim">{text}</p>
    </Modal>
  )
}
