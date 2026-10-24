// FilePreview.jsx — 类型分档预览 + 状态机 (issue #46, s3-arch §4.2)
// 设计依据: docs/UX-FILEBROWSER.md §4 + docs/ARCH-FILEBROWSER.md §4.2
import React, { useEffect, useRef, useState, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { api } from '../../api.js'
import { kindOf, formatSize, formatMtime, isTooLarge } from './kinds.js'

// 与 ChatView.md() 同管线: marked + DOMPurify, 共享 DOMPurify 配置 (H2 红线零改动)
function md(text) {
  return { __html: DOMPurify.sanitize(marked.parse(text || '')) }
}

// E-4/5/6/8 错误卡: 统一卡片样式 + 重试
function ErrorCard({ kind, onRetry }) {
  const { t } = useTranslation()
  const msg = {
    404: t('chat.files.err_not_found'),
    403: t('chat.files.err_forbidden'),
    500: t('chat.files.err_server'),
    'network': t('chat.files.err_network'),
    'deleted': t('chat.files.err_deleted'),
  }[kind] || t('chat.files.err_server')
  return (
    <div className="card-pad flex flex-col items-center gap-2 text-center">
      <div className="text-bad text-[24px]" aria-hidden="true">⚠</div>
      <div className="text-[13px] text-ink">{msg}</div>
      {kind !== 403 && onRetry && (
        <button onClick={onRetry} className="btn-ghost btn-sm">
          {t('chat.files.retry')}
        </button>
      )}
    </div>
  )
}

// 兜底卡: 不支持预览 / 超限 (§4.2)
function FallbackCard({ selection }) {
  const { t } = useTranslation()
  const size = formatSize(selection.size)
  return (
    <div className="card-pad flex flex-col items-center gap-1.5 text-center">
      <div className="text-dim text-[28px]" aria-hidden="true">📄</div>
      <div className="text-[13px] font-medium text-ink">{selection.name}</div>
      <div className="text-[11px] text-dim font-mono">{size}</div>
    </div>
  )
}

// md / text 预览: 拉 embed 流 → 渲染
function TextPreview({ project, selection, onError, registerRetry }) {
  const { t } = useTranslation()
  const [loading, setLoading] = useState(true)
  const [content, setContent] = useState('')
  const abortRef = useRef(null)

  const load = useCallback(async () => {
    if (abortRef.current) abortRef.current.abort()
    const ac = new AbortController()
    abortRef.current = ac
    setLoading(true)
    try {
      const text = await api.fsText(project, selection.path, ac.signal)
      setContent(text || '')
      onError(null)
    } catch (e) {
      if (e?.name === 'AbortError') return
      if (e?.name === 'FsHttpError') {
        onError(e.status === 404 ? 404 : e.status === 403 ? 403 : 500)
      } else {
        onError('network')
      }
    } finally {
      setLoading(false)
    }
  }, [project, selection.path, onError])

  useEffect(() => {
    load()
    registerRetry(load)
    return () => abortRef.current?.abort()
  }, [load, registerRetry])

  if (loading) {
    return (
      <div className="flex items-center gap-2 px-4 py-3 text-[12px] text-dim">
        <span className="spin" />
        <span>{t('chat.files.loading', { name: selection.name })}</span>
      </div>
    )
  }
  const isMd = kindOf(selection.name) === 'md'
  return (
    <div className="px-5 py-4 overflow-y-auto h-full">
      {isMd ? (
        <div className="md-body text-[13px]" dangerouslySetInnerHTML={md(content)} />
      ) : (
        <pre className="font-mono text-[12px] leading-relaxed whitespace-pre-wrap
                        text-ink">{content}</pre>
      )}
    </div>
  )
}

// html 预览: iframe sandbox
function HtmlPreview({ project, selection, onError, registerRetry }) {
  const { t } = useTranslation()
  const url = api.fsFileUrl(project, selection.path, 'embed')
  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-1.5 text-[11px] text-warn border-b border-line/50
                      bg-warn/5">
        {t('chat.files.sandbox_note')}
      </div>
      <iframe key={url}
              src={url}
              sandbox="allow-scripts"
              className="flex-1 w-full bg-white border-0" />
    </div>
  )
}

// pdf 预览: iframe + 3s 超时 fallback
function PdfPreview({ project, selection, onError, registerRetry }) {
  const { t } = useTranslation()
  const [fallback, setFallback] = useState(false)
  const url = api.fsFileUrl(project, selection.path, 'embed')
  useEffect(() => {
    setFallback(false)
    const timer = setTimeout(() => setFallback(true), 3000)
    return () => clearTimeout(timer)
  }, [url])
  return (
    <div className="flex flex-col h-full">
      {fallback && (
        <div className="px-4 py-1.5 text-[11px] text-bad border-b border-line/50 bg-bad/5">
          {t('chat.files.pdf_fallback')}
        </div>
      )}
      <iframe key={url} src={url} className="flex-1 w-full border-0" />
    </div>
  )
}

// image 预览: <img>
function ImagePreview({ project, selection, onError, registerRetry }) {
  const { t } = useTranslation()
  const url = api.fsFileUrl(project, selection.path, 'embed')
  const [loaded, setLoaded] = useState(false)
  return (
    <div className="flex items-center justify-center h-full p-4 bg-canvas">
      {!loaded && (
        <div className="absolute flex items-center gap-2 text-[12px] text-dim">
          <span className="spin" />
          <span>{t('chat.files.loading', { name: selection.name })}</span>
        </div>
      )}
      <img key={url} src={url} alt={selection.name}
           className="max-w-full max-h-full"
           style={{ display: loaded ? 'block' : 'none' }}
           onLoad={() => setLoaded(true)}
           onError={() => onError('network')} />
    </div>
  )
}

// 主组件
export function FilePreview({ project, selection }) {
  const { t } = useTranslation()
  const [errKind, setErrKind] = useState(null)
  const retryRef = useRef(() => {})
  const registerRetry = useCallback((fn) => { retryRef.current = fn }, [])

  // 切换选择时清错
  useEffect(() => {
    setErrKind(null)
    retryRef.current = () => {}
  }, [selection?.path])

  // 空态 (E-3)
  if (!selection) {
    return (
      <div className="h-full flex items-center justify-center p-6">
        <div className="text-center text-dim text-[13px] max-w-[260px]">
          {t('chat.files.preview_placeholder')}
        </div>
      </div>
    )
  }

  // 超限 (OQ-4): 硬拒走兜底卡
  if (isTooLarge(selection.size)) {
    return (
      <div className="h-full flex items-center justify-center p-6">
        <FallbackCard selection={selection} />
      </div>
    )
  }

  // 错误态
  if (errKind) {
    return (
      <div className="h-full flex items-center justify-center p-6">
        <ErrorCard kind={errKind} onRetry={() => { setErrKind(null); retryRef.current() }} />
      </div>
    )
  }

  const kind = kindOf(selection.name)
  if (kind === 'md' || kind === 'text') {
    return <TextPreview project={project} selection={selection}
                        onError={setErrKind} registerRetry={registerRetry} />
  }
  if (kind === 'html') {
    return <HtmlPreview project={project} selection={selection}
                        onError={setErrKind} registerRetry={registerRetry} />
  }
  if (kind === 'pdf') {
    return <PdfPreview project={project} selection={selection}
                       onError={setErrKind} registerRetry={registerRetry} />
  }
  if (kind === 'image') {
    return <ImagePreview project={project} selection={selection}
                         onError={setErrKind} registerRetry={registerRetry} />
  }
  // other / svg / 未知
  return (
    <div className="h-full flex items-center justify-center p-6">
      <FallbackCard selection={selection} />
    </div>
  )
}