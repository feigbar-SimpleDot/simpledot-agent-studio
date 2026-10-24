// ui/src/i18n/index.js
//
// i18next bootstrap + language resolution. Per design.md §10, this module owns:
//   - initI18n(): static locale import (en + zh-cn baked into the singlefile
//     build so deployment does not need a runtime locale fetch);
//   - fallback chain target → en → key-with-dots-replaced-by-·;
//   - resolveLang(): boot-time fetch of GET /api/lang (1 s AbortController
//     timeout → 'en'); fresh browser without backend still paints English
//     (design.md §10 E1);
//   - applyHtmlLang(): maps internal 'en'/'zh-cn' to BCP-47 'en'/'zh-CN' on
//     <html lang> so screen readers get the right pronunciation;
//   - persistLang + listenStorageLang: localStorage 'sas-lang' roundtrip
//     for cross-tab sync (interaction-design §5.2);
//
// The recommended English-baseline spec is in architecture §5.4 (comment
// bilingualization) — every file in i18n/** keeps an English module header
// describing its purpose; Chinese version of the header note lives in the
// comment below.
// [en] Frontend i18n module: i18next init, language resolution, html/lang effect,
//      localStorage roundtrip. English-default; zh-cn baked into the same
//      singlefile build.

// [zh-cn] 前端 i18n 模块:i18next 初始化、语言解析、<html lang> 副作用、
//        localStorage 同步。默认 en;zh-cn 同步打包进 dist 单文件。
import i18next from 'i18next'
import { initReactI18next, useTranslation } from 'react-i18next'
import en from './locales/en.json'
import zhcn from './locales/zh-cn.json'

// Internal language identifiers used everywhere in the app.
// BCP-47 mapping happens only at applyHtmlLang; the rest of the code,
// localStorage, the GET /api/lang response, and fallbackLng all use these.
export const SUPPORTED = ['en', 'zh-cn']

// --- i18next init ------------------------------------------------------------------
// Architecture §7.2 / D4:
//   - fallbackLng: 'en' (target → en → key placeholder)
//   - returnEmptyString: false (empty translations treated as missing)
//   - interpolation.escapeValue: false (React already escapes)
//   - parseMissingKeyHandler: render 'nav.chat' as 'nav·chat' so a missing key
//     shows a calm, visible, neutral placeholder instead of either the raw
//     key string (machine-facing) or a blank cell.
export function initI18n() {
  return i18next
    .use(initReactI18next)
    .init({
      resources: {
        en: { translation: en },
        'zh-cn': { translation: zhcn },
      },
      lng: 'en',                       // boot default (E1); boot sequence may
                                       // upgrade to zh-cn after resolveLang()
      fallbackLng: 'en',
      // E3 fix (pair with I18nextProvider): i18next formats the language code
      // to BCP-47 during negotiation ('zh-cn' → 'zh-CN'), which then misses
      // the lowercase 'zh-cn' resource bundle and silently falls back to en.
      // lowerCaseLng keeps the negotiated code lowercase; load:'currentOnly'
      // stops the zh → zh-Hans → … variant chain. Without these two,
      // t() returns stale English strings even though i18next.language
      // reports 'zh-cn' (verified: node i18next@23 repro).
      // [zh-cn] E3 配套修复: i18next 语言协商会把 zh-cn 规范成 zh-CN,
      //        压不中小写 'zh-cn' 词条包而静默回落 en;lowerCaseLng 保小写,
      //        load:currentOnly 关掉 zh→zh-Hans 变体链。
      lowerCaseLng: true,
      load: 'currentOnly',
      returnEmptyString: false,
      // E2 fix (interpolation convention): the 539-key locale tables bake in
      // single-brace placeholders ('Load failed: {err}') — the established
      // convention of this table set. i18next defaults to '{{ }}', so every
      // {param} silently rendered literally. Align the interpolator with the
      // table convention (verified: all 66 placeholder values are plain
      // {word} tokens; no literal-brace values exist in either locale).
      // [zh-cn] E2 配套修复: 词条表 66 处占位符全部是单花括号 {param} 约定,
      //        i18next 默认 {{ }} 导致插值静默不生效; 对齐词条表约定。
      interpolation: { escapeValue: false, prefix: '{', suffix: '}' },
      parseMissingKeyHandler: (key) =>
        key.replace(/\./g, '·'),
    })
}

// Re-export the hook so call sites have one import. The i18next default
// export is the singleton instance — callers and main.jsx both read
// `i18next.language` and call `i18next.changeLanguage(...)`.
export { useTranslation, i18next }

// --- Language resolution (boot-time fetch of GET /api/lang) -----------------------
// Per design.md §10 (D3 / language resolution):
//   1. localStorage['sas-lang'] (user explicit choice, persisted by Settings)
//      → only if value is a supported language code (en|zh-cn); invalid → fall
//        through. Reading this BEFORE the network call is what makes a reload
//        or a fresh tab restore the user's explicit selection instead of
//        resetting to the deployment default.
//   2. GET /api/lang (1 s AbortController timeout) → deployment default
//   3. 'en' (architecture §5.4 fallback)
// The fetch in step 2 is best-effort: a fresh browser without backend still
// paints English.
export async function resolveLang() {
  // Step 1: read the persisted user choice synchronously (P1-a fix).
  // persistLang/listenStorageLang guarantee that whatever value sits in
  // localStorage was written by the user via the Settings segmented control
  // — but third-party scripts could stuff anything, hence the SUPPORTED gate.
  try {
    const stored = (localStorage.getItem('sas-lang') || '').toString()
    if (SUPPORTED.includes(stored)) return stored
  } catch {
    // localStorage may be unavailable (private mode, sandboxed iframe);
    // fall through to the network step.
  }
  // Step 2: ask the backend for the deployment default.
  try {
    const ctrl = new AbortController()
    const t = setTimeout(() => ctrl.abort(), 1000)
    const res = await fetch('/api/lang', { signal: ctrl.signal, cache: 'no-store' })
    clearTimeout(t)
    if (!res.ok) return 'en'
    const data = await res.json().catch(() => null)
    const v = data && typeof data.lang === 'string' ? data.lang : ''
    return SUPPORTED.includes(v) ? v : 'en'
  } catch {
    return 'en'
  }
}

// --- <html lang> effect ------------------------------------------------------------
// Internal code → BCP-47 (en, zh-CN). Setting document.documentElement.lang is
// mandatory: screen readers / font shaping depend on it. §8.1.
export function applyHtmlLang(lang) {
  const map = { en: 'en', 'zh-cn': 'zh-CN' }
  const bc = map[lang] || map.en
  document.documentElement.lang = bc
}

// --- localStorage roundtrip --------------------------------------------------------
// Only the user-driven switch writes 'sas-lang'; the boot-time fetch result
// is intentionally NOT persisted (interaction-design §5.2 — keep "no
// preference" distinguishable from "chose en").
export function persistLang(lang) {
  try { localStorage.setItem('sas-lang', lang) } catch { /* ignore quota */ }
}

// Cross-tab listener (interaction-design §5.2). Returns the unsubscribe fn so
// React effects can clean up.
export function listenStorageLang(handler) {
  const onStorage = (e) => {
    if (e && e.key === 'sas-lang') {
      const v = (e.newValue || '').toString()
      handler(SUPPORTED.includes(v) ? v : 'en')
    }
  }
  window.addEventListener('storage', onStorage)
  return () => window.removeEventListener('storage', onStorage)
}

// Switch language end-to-end: i18next re-renders, html lang updates,
// localStorage persists. Used by the Settings segmented control and the
// storage-event listener.
export async function setLang(lang) {
  if (!SUPPORTED.includes(lang)) return
  await i18next.changeLanguage(lang)
  applyHtmlLang(lang)
  persistLang(lang)
}
