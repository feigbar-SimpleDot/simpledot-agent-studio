import React from 'react'
import { createRoot } from 'react-dom/client'
import { I18nextProvider } from 'react-i18next'
import './index.css'
import App from './App.jsx'
import {
  i18next,
  initI18n,
  applyHtmlLang,
  resolveLang,
  listenStorageLang,
} from './i18n'
import { setLanguageGetter } from './i18n/fmt.ts'

// Bootstrap i18n before React mounts. Architecture §7.1 / interaction
// design E1: paint must not block on the boot fetch.
//
//   1. initI18n() — synchronous, defaults to 'en'.
//   2. applyHtmlLang(initial) — runtime <html lang> follows the active code.
//   3. listenStorageLang — cross-tab sync (interaction-design §5.2).
//   4. resolveLang() /1 s — best-effort upgrade to the deployment default
//      if no localStorage preference exists.
//   5. setLanguageGetter — fmt.ts needs the active i18next.language.
initI18n()
// [en] Bind fmt.ts to the live i18next.language so changing language at
//      runtime also retargets Intl call sites.
// [zh-cn] 把 fmt.ts 绑到 i18next.language,运行时切语言也能带 Intl 走。
setLanguageGetter(() => i18next.language || 'en')
applyHtmlLang(i18next.language || 'en')
listenStorageLang((lang) => {
  i18next.changeLanguage(lang)
  applyHtmlLang(lang)
})
// Fire-and-forget: best-effort upgrade to the deployment default.
resolveLang().then((lang) => {
  // Only switch if the user hasn't already picked one in this session.
  if (i18next.language !== lang) {
    i18next.changeLanguage(lang)
    applyHtmlLang(lang)
  }
}).catch(() => { /* keep en */ })

// E3 fix (test-report-i18n P0-1): wrap App in <I18nextProvider> so every
// useTranslation() consumer subscribes to the i18next instance and re-renders
// on `languageChanged`. Without the Provider, changeLanguage() updates
// html lang + localStorage but React keeps painting stale en strings.
// [zh-cn] E3 修复: 用 I18nextProvider 包 App, languageChanged 事件才能
//        触发全部 useTranslation 组件 re-render。
createRoot(document.getElementById('root')).render(
  <I18nextProvider i18n={i18next}>
    <App />
  </I18nextProvider>
)
