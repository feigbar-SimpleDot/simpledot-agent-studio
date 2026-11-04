import React, { useEffect, useId, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { Icon, ICONS } from './icons.jsx'

// [en] ui.jsx — Modal / Toast / Confirm primitives shared across views.
//      Title/aria/button labels are routed through i18n so views can keep
//      passing static copy through while the modal still reads in the
//      active language. (See design.md §10.)
// [zh-cn] ui.jsx —— Modal/Toast/Confirm 基础控件,跨视图共用。
//        标题/aria/按钮文案走 i18n,调用方传静态文案,弹窗照当前语言渲染。

// 通用弹窗: 替代原生 <dialog>, 带遮罩模糊 + 弹入动画
// issue #202 R3-4 (走查 P0-4): 补 role=dialog / aria-modal / aria-labelledby +
// 打开收焦、关闭回焦 (参照 FileDrawer 既有正确做法)。焦点收拢在 rAF 里做:
// children 若自带 autoFocus 已把焦点带进弹框, 则不抢; 否则收拢到首个可聚焦元素。
// onClose 走 ref: 调用方普遍传内联箭头, 若进依赖数组会让本 effect 每次父渲染
// 重跑, 反复重记 prevFocus (记成弹框内元素 ⇒ 关闭后焦点无处可还)。
// D-1 修复 (TEST-REPORT-R-20261102 §4): prevFocus 捕获必须在 render 阶段做。
// React 的 autoFocus 在 commit 阶段触发, 早于**任何** effect (useLayoutEffect
// 也晚于它) ⇒ 旧写法在 effect 里读 activeElement, 带 autoFocus 的弹框读到的是
// 刚被聚焦的弹框内元素 ⇒ 关闭时把焦点还给已卸载节点 ⇒ 焦点掉 body。render
// 阶段弹框 DOM 尚未提交, autoFocus 尚未发生, activeElement 仍是触发元素。
export function Modal({ open, onClose, title, children, footer, width = 'max-w-lg' }) {
  const ref = useRef(null)
  const onCloseRef = useRef(onClose)
  const prevFocusRef = useRef(null)
  const wasOpenRef = useRef(false)
  const titleId = useId()
  const { t } = useTranslation()
  useEffect(() => { onCloseRef.current = onClose })
  // open 跃迁 false→true 的那次 render 里捕获触发元素 (幂等: 弹框保持打开期间
  // 父组件重渲染不会重记; StrictMode 双渲染两次读到同一 activeElement)。
  if (open !== wasOpenRef.current) {
    if (open) prevFocusRef.current = document.activeElement
    wasOpenRef.current = open
  }
  useEffect(() => {
    if (!open) return
    const onKey = (e) => e.key === 'Escape' && onCloseRef.current?.()
    document.addEventListener('keydown', onKey)
    const raf = requestAnimationFrame(() => {
      const el = ref.current
      if (!el || el.contains(document.activeElement)) return
      const first = el.querySelector(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')
      ;(first || el).focus?.()
    })
    return () => {
      document.removeEventListener('keydown', onKey)
      cancelAnimationFrame(raf)
      const prev = prevFocusRef.current
      if (prev && prev.isConnected) prev.focus?.()
      prevFocusRef.current = null
    }
  }, [open])

  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4"
         onMouseDown={(e) => e.target === e.currentTarget && onClose?.()}>
      <div className="absolute inset-0 bg-black/70 backdrop-blur-sm animate-[fadeIn_.15s_ease]" />
      <div ref={ref} role="dialog" aria-modal="true" aria-labelledby={titleId}
           className={`relative ${width} w-full card shadow-2xl animate-[popIn_.18s_cubic-bezier(.34,1.56,.64,1)]
                      max-h-[88vh] flex flex-col`}>
        <div className="flex items-center justify-between px-5 py-4 border-b border-line shrink-0">
          <h3 id={titleId} className="text-[14.5px] font-bold">{title}</h3>
          <button onClick={onClose} className="btn-quiet btn-sm !px-2"
                  aria-label={t('common.close')}>
            <Icon d={ICONS.close} className="w-4 h-4" />
          </button>
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
  // issue #195 根因修复 (照抄 v2 已验证修法 commit 4bcb7c0 F-1):
  // 旧形态 deps=[msg, onDone] + 6 处调用方全传内联箭头 onDone ⇒ 父组件每次
  // 重渲染都产生新的 onDone 身份 ⇒ effect 清理并重启 3.2s 定时器 ⇒ 收件箱
  // 2s 轮询 (< 3.2s) 令 toast 永不自动消失。ref 方案修在根因处: 未来任何
  // 调用方再传内联箭头也不会复发。(不改调用方包 useCallback —— 那是治标。)
  const onDoneRef = useRef(onDone)
  // 每次渲染后同步最新 onDone (无依赖数组 = 每渲染都跑); 声明在定时器 effect
  // 之前 ⇒ 同一次提交内 ref 先于定时器就位。
  useEffect(() => { onDoneRef.current = onDone })

  useEffect(() => {
    if (!msg) return
    const t = setTimeout(() => onDoneRef.current?.(), 3200)
    return () => clearTimeout(t)
  }, [msg])
  if (!msg) return null
  const cls = type === 'ok' ? 'border-ok/50 text-ok' : type === 'bad' ? 'border-bad/50 text-bad' : 'border-warn/50 text-warn'
  // issue #202 R3-5 (走查 P0-5): Toast 补 role=status / aria-live=polite,
  // 屏幕阅读器可感知操作反馈 (WCAG 4.1.3)。
  return (
    <div role="status" aria-live="polite"
         className={`fixed bottom-6 left-1/2 -translate-x-1/2 z-[60] card px-4 py-2.5 text-sm
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
