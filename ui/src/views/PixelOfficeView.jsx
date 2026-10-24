import React, { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../api.js'

/* Pixel Office (pixel-agents) embedded view.
 * iframe points at the console's same-origin proxy /pixel/ (the browser only
 * sees the console origin; pixel-agents binds to container-local 127.0.0.1).
 * [en] A tokenless iframe can still watch the animation; tokens are needed
 *      only for hook installation.
 * [zh-cn] iframe 走控制台同源代理 /pixel/(浏览器只见控制台源);无 token 仍可观
 *        看动画, 仅 hook 安装路径需要 token。
 *
 * State machine: skeleton → not-enabled guide → not-ready guide → running.
 * 15 s polling + iframe src/sandbox/reloadKey logic preserved; only the
 * rendering layer is rewritten for i18n.
 */
export default function PixelOfficeView({ onNavigate }) {
  const { t } = useTranslation()
  const [status, setStatus] = useState(null)  // {enabled, running}
  const [reloadKey, setReloadKey] = useState(0)
  const [frameReady, setFrameReady] = useState(false)  // iframe paint ready — overlay skeleton until then (§1.5)
  const timer = useRef(null)

  const poll = () => {
    api.pixelStatus().then(setStatus).catch(() => setStatus({ enabled: false, running: false }))
  }

  useEffect(() => {
    poll()
    timer.current = setInterval(() => { if (!document.hidden) poll() }, 15000)
    return () => clearInterval(timer.current)
  }, [])

  // reloadKey change → iframe reloads, ready flag resets
  useEffect(() => { setFrameReady(false) }, [reloadKey])

  // ── Loading skeleton (§1.2): same shape as running state, zero reflow ──
  if (!status) {
    return (
      <div className="pof-touch h-full flex flex-col gap-2" aria-busy="true" aria-label={t('pixel.aria.loading')}>
        <div className="skeleton h-9 shrink-0" />
        <div className="skeleton flex-1 min-h-[420px] rounded-xl" />
      </div>
    )
  }

  // ── Not-enabled guide (§1.3) ──
  if (!status.enabled) {
    return (
      <div className="pof-touch h-full flex items-center justify-center p-6">
        <div className="empty !py-20 gap-4 max-w-[420px] mx-auto text-center">
          {/* 48 px line-art building icon, text-dim */}
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
               strokeLinecap="round" strokeLinejoin="round"
               className="w-12 h-12 text-dim" aria-hidden="true">
            <path d="M3 21h18M5 21V7l7-4 7 4v14M9 9h.01M9 13h.01M9 17h.01M15 9h.01M15 13h.01M15 17h.01" />
          </svg>
          <div className="text-ink text-[15px] font-bold">{t('pixel.disabled.title')}</div>
          <div className="text-dim text-[13px] leading-6">
            {t('pixel.disabled.body')}
          </div>
          {onNavigate && (
            <button className="btn-primary mt-2" onClick={() => onNavigate('settings')}>
              {t('pixel.disabled.goto_settings')}
            </button>
          )}
          <details className="tech-details mt-2 w-full text-left">
            <summary>{t('pixel.tech.enable_summary')}</summary>
            <div className="mt-1.5 space-y-1 leading-5">
              <div>{t('pixel.tech.setting_label')}: <code className="px-1 rounded bg-panel2">pixel_office_enabled</code></div>
              <div>{t('pixel.tech.deploy_var_label')}: <code className="px-1 rounded bg-panel2">PIXEL_OFFICE_ENABLED=1</code> (.env)</div>
              <div>{t('pixel.tech.mount_hint')}</div>
            </div>
          </details>
        </div>
      </div>
    )
  }

  // ── Enabled but not running (§1.4): operator info collapsed ──
  if (!status.running) {
    return (
      <div className="pof-touch h-full flex items-center justify-center p-6">
        <div className="empty !py-20 gap-4 max-w-[420px] mx-auto text-center">
          {/* 48 px warning icon, text-warn */}
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
               strokeLinecap="round" strokeLinejoin="round"
               className="w-12 h-12 text-warn" aria-hidden="true">
            <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0zM12 9v4M12 17h.01" />
          </svg>
          <div className="text-ink text-[15px] font-bold">{t('pixel.not_ready.title')}</div>
          <div className="text-dim text-[13px] leading-6">
            {t('pixel.not_ready.body')}
          </div>
          <button className="btn-ghost mt-2" onClick={poll}>{t('common.retry')}</button>
          <details className="tech-details mt-2 w-full text-left">
            <summary>{t('pixel.tech.troubleshoot_summary')}</summary>
            <div className="mt-1.5 space-y-1 leading-5">
              <div>{t('pixel.tech.no_response', { addr: '127.0.0.1:3199' })}</div>
              <div>{t('pixel.tech.log_hint')}: <code className="px-1 rounded bg-panel2">docker logs hermes_main | grep pixel</code></div>
              <div><code className="px-1 rounded bg-panel2">/pixel-office</code> {t('pixel.tech.mount_required')}</div>
            </div>
          </details>
        </div>
      </div>
    )
  }

  // ── Running (§1.5): natural language header + iframe skeleton overlay ──
  return (
    <div className="pof-touch h-full flex flex-col">
      <div className="shrink-0 flex items-center justify-between px-2 py-1.5 border-b border-line bg-panel">
        <span className="text-dim text-xs">
          {t('pixel.running.subtitle')}
        </span>
        <button className="btn-ghost btn-sm" onClick={() => setReloadKey((k) => k + 1)}>
          {t('pixel.running.reload')}
        </button>
      </div>
      <div className="relative flex-1 min-h-0">
        <iframe
          key={reloadKey}
          src="/pixel/"
          title={t('nav.pixel_office')}
          className="w-full h-full border-0 bg-canvas"
          sandbox="allow-scripts allow-same-origin allow-popups"
          onLoad={() => setFrameReady(true)}
        />
        {/* iframe paint not yet ready: overlay skeleton, remove on load (§1.5) */}
        {!frameReady && (
          <div className="absolute inset-0 skeleton rounded-none flex items-center justify-center"
               aria-hidden="true">
            <span className="text-dim text-xs">{t('pixel.running.iframe_loading')}</span>
          </div>
        )}
      </div>
    </div>
  )
}
