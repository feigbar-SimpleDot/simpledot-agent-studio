import React, { useEffect, useState, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../api.js'
import { Toast } from '../components/ui.jsx'
import { SUPPORTED, setLang } from '../i18n'

// ────────────────────────────────────────────────────────────────────────────
// Settings meta — language-bilingual form
//
// [en] Settings view — six backend knobs (paths/timeout/external) plus the
//      browser-side language switcher (interaction-design §3, see first
//      group "Appearance & Language"). Labels/hints live in i18next so
//      the same row keys render in en or zh-cn; values stayed byte-identical
//      to the pre-i18n copy in zh-cn.json (D3 zero-regression anchor).
// [zh-cn] 设置页:六项后端 knob(路径/超时/外部)+ 浏览器侧语言切换(交互设计 §3,
//        见首个分组「外观与语言」)。label/hint 走 i18next,行键不变;
//        zh-cn.json 里中文值与改造前逐字相同(D3 零回归锚)。
// ────────────────────────────────────────────────────────────────────────────

// Each row's label + hint keyed by i18n locale. The "key" itself stays a
// machine identifier (shared_workspace_host, default_timeout_seconds, ...).
const SETTING_ROWS = [
  'shared_workspace_host',
  'docker_compose_dir',
  'default_timeout_seconds',
  'reaper_interval_seconds',
  'default_image',
  'hermes_webui_url',
  'pixel_office_enabled',
]

// boolean 型设置项: 渲染成开关 checkbox, 保存时转 bool。
const BOOL_KEYS = new Set(['pixel_office_enabled'])

// CONSOLE2 §2.1: 三组卡片的信息架构(键不改, 只改呈现顺序与分组标题)。
// i18n keys under settings.group.* carry the title text.
const GROUP_KEYS = [
  { titleKey: 'settings.group.paths', rowKeys: ['shared_workspace_host', 'docker_compose_dir', 'default_image'] },
  { titleKey: 'settings.group.timeout', rowKeys: ['default_timeout_seconds', 'reaper_interval_seconds'] },
  { titleKey: 'settings.group.external', rowKeys: ['hermes_webui_url', 'pixel_office_enabled'] },
]

// CONSOLE2 §2.2 + issue #60: 按字段类型分派校验, 不再无差别套整数校验。
// - *_seconds: 整数且 ≥1
// - hermes_webui_url: 只做 http(s) 绝对地址格式校验(允许空 = 关闭入口),
//   与后端 mw/console.py _clean_webui_url 口径一致
// - 其余文本字段(path/image 等): 不做数值校验
function validateField(key, raw) {
  if (key.endsWith('_seconds')) {
    const v = Number(raw)
    return Number.isInteger(v) && v >= 1 ? '' : 'settings.invalid_seconds'
  }
  if (key === 'hermes_webui_url') {
    if (!raw) return ''  // 留空合法: 隐藏顶栏跳转入口
    const low = raw.toLowerCase()
    return (low.startsWith('http://') || low.startsWith('https://'))
      ? '' : 'settings.invalid_webui_url'
  }
  return ''
}

export default function SettingsView({ onSaved }) {
  const { t, i18n } = useTranslation()
  const [settings, setSettings] = useState(null)
  const [note, setNote] = useState('')
  const [err, setErr] = useState('')
  const [dirty, setDirty] = useState(false)
  const [toast, setToast] = useState('')
  const [saving, setSaving] = useState(false)          // §2.3 保存中态, 防重复提交
  const [fieldErrs, setFieldErrs] = useState({})      // {key: 错误文案 key} §2.2

  const load = useCallback(async () => {
    try {
      const d = await api.settings()
      setSettings(d.settings)
      setNote(d.note || '')
      setDirty(false)
    } catch (e) { setErr(String(e.message || e)) }
  }, [])

  useEffect(() => { load() }, [load])

  // 失焦即校验单个字段 (§2.2, issue #60: 按字段类型分派)
  const onBlurNum = (k) => {
    const el = document.getElementById('set_' + k)
    if (!el) return
    const msg = validateField(k, el.value.trim())
    setFieldErrs((m) => { const n = { ...m }; if (msg) n[k] = msg; else delete n[k]; return n })
  }

  // 全量校验(保存前跑一遍, §2.2)
  const validateAll = () => {
    const errs = {}
    for (const k of SETTING_ROWS) {
      const el = document.getElementById('set_' + k)
      if (!el || BOOL_KEYS.has(k)) continue
      const msg = validateField(k, el.value.trim())
      if (msg) errs[k] = msg
    }
    setFieldErrs(errs)
    return Object.keys(errs).length === 0
  }

  const hasErr = Object.keys(fieldErrs).length > 0

  const save = async () => {
    if (saving) return                       // §2.3 防重复提交入口守卫
    if (!validateAll()) return             // §2.2 保存前全量校验
    const body = {}
    for (const k of SETTING_ROWS) {
      const el = document.getElementById('set_' + k)
      if (!el) continue
      if (BOOL_KEYS.has(k)) { body[k] = !!el.checked; continue }
      let v = el.value.trim()
      if (k.endsWith('_seconds')) v = parseInt(v, 10)
      body[k] = v
    }
    setSaving(true)
    setErr('')
    try {
      const d = await api.saveSettings(body)
      setSettings(d.settings)
      setDirty(false)
      setToast(t('settings.saved_toast'))
      onSaved && onSaved(d.settings)
    } catch (e) { setErr(String(e.message || e)) }
    finally { setSaving(false) }
  }

  if (err && !settings)
    return <div className="empty"><span className="dot-bad" />{t('settings.backend_unreachable', { err })}</div>
  if (!settings)
    return <div className="space-y-3">{[...Array(5)].map((_, i) => <div key={i} className="skeleton h-16" />)}</div>

  // 单个设置项渲染: label 区(中文 label + 11px key + title 悬浮) + 控件区 (§2.1/§2.5)
  const renderRow = (k) => {
    const fe = fieldErrs[k]
    const label = t(`settings.row.${k}_label`)
    const hint = t(`settings.row.${k}_hint`)
    return (
      <div key={k} className="grid grid-cols-1 sm:grid-cols-[1fr_1.4fr] gap-1 sm:gap-6 items-start
                             py-3 border-b border-line/40 last:border-0">
        <div>
          <div className="text-[13px] font-semibold truncate" title={hint}>{label}</div>
          <div className="mono text-[11px] text-dim mt-0.5" title={hint}>{k}</div>
        </div>
        <div>
          {BOOL_KEYS.has(k) ? (
            // §2.5 开关行: 整行可点, 行高 ≥44px
            <label className="switch-row">
              <span className="text-[12.5px] text-ink" title={hint}>{t('settings.enable_pixel_office')}</span>
              <input id={'set_' + k} type="checkbox"
                defaultChecked={!!settings[k]}
                onChange={() => setDirty(true)} />
            </label>
          ) : (
            <>
              <input id={'set_' + k} className={`input ${fe ? 'input-err' : ''}`}
                defaultValue={settings[k] ?? ''}
                onChange={() => setDirty(true)}
                onBlur={() => onBlurNum(k)} />
              {fe
                ? <div className="field-err">{t(fe)}</div>
                : <div className="hint">{hint}</div>}
            </>
          )}
        </div>
      </div>
    )
  }

  const currentLang = (i18n.language || 'en')

  return (
    <div className="set-touch max-w-3xl space-y-4">
      <div>
        <h2 className="text-[14px] font-bold">{t('settings.view_title')}</h2>
        {note && <p className="hint !mt-1">{note}</p>}
      </div>

      {/* 外观与语言(交互设计 §3.2): 在 GROUPS.map 之前固定渲染,
          双语固定标签 English|中文,D2 即时生效不刷新。 */}
      <section className="card-pad !py-1">
        <div className="flex items-center gap-2 pt-3 min-h-[44px]">
          <div className="flex-1 min-w-0">
            <div className="text-[13px] font-semibold truncate"
                 title={t('settings.language.hint')}>
              {t('settings.language.label')}
            </div>
            <div className="text-[11px] text-dim mt-0.5 leading-snug">
              {t('settings.language.hint')}
            </div>
          </div>
          {/* 分段控件 English|中文(双语固定,任何语言下都显示原文) */}
          <div role="radiogroup"
               aria-label={currentLang === 'zh-cn' ? t('settings.language.aria_label_zh') : t('settings.language.aria_label_en')}
               className="inline-flex shrink-0 rounded-lg bg-panel2 ring-1 ring-line p-0.5">
            {SUPPORTED.map((lng) => (
              <button key={lng}
                role="radio"
                aria-checked={currentLang === lng}
                onClick={() => setLang(lng)}
                className={`min-w-[88px] px-3 py-1.5 text-[12.5px] font-medium rounded-md
                           transition-colors
                           ${currentLang === lng
                             ? 'bg-accent/15 text-accent ring-1 ring-inset ring-accent/30'
                             : 'text-dim hover:text-ink'}`}>
                {lng === 'en'
                  ? t('common.language_label_en')
                  : t('common.language_label_zh')}
              </button>
            ))}
          </div>
        </div>
      </section>

      {/* 三组卡片 (§2.1) — 标题走 t(settings.group.*) */}
      {GROUP_KEYS.map((g) => (
        <section key={g.titleKey} className="card-pad !py-1">
          <div className="text-[13px] font-bold pt-3">{t(g.titleKey)}</div>
          {g.rowKeys.map(renderRow)}
        </section>
      ))}

      <div className="rounded-xl border-l-4 border-warn bg-warn/5 px-4 py-3 text-[12.5px] text-dim leading-relaxed">
        {t('settings.banner_warning')}
      </div>

      {/* 吸底保存条 (§2.4): 状态文案 + 保存按钮, 错误紧邻操作点 (§2.3) */}
      <div className="sticky bottom-0 -mx-1 px-1 py-2.5 bg-panel/90 backdrop-blur border-t border-line
                     flex items-center justify-between gap-3 z-30">
        <div className="min-w-0 text-[12.5px]">
          {saving
            ? <span className="text-dim inline-flex items-center gap-1.5"><span className="spin" />{t('settings.saving')}</span>
            : hasErr
            ? <span className="text-bad" title={t('settings.invalid_input_other', { n: Object.keys(fieldErrs).length })}>
                {t('settings.invalid_input_other', { n: Object.keys(fieldErrs).length })}
              </span>
            : dirty
            ? <span className="text-warn">{t('settings.dirty_warning')}</span>
            : <span className="text-dim">{t('settings.all_saved')}</span>}
          {/* 保存失败就近显示 (§2.3) */}
          {err && dirty && <div className="field-err truncate">{t('settings.save_error_inline', { err })}</div>}
        </div>
        <button className="btn-primary shrink-0"
          disabled={!dirty || saving || hasErr}
          onClick={save}>
          {saving ? (
            <span className="inline-flex items-center gap-1.5"><span className="spin" />{t('settings.saving')}</span>
          ) : t('settings.save_btn')}
        </button>
      </div>

      <Toast msg={toast} onDone={() => setToast('')} />
    </div>
  )
}
