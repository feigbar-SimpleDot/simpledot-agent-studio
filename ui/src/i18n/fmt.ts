/* Locale-aware formatters using i18n.language.
   fmtDateTime / fmtDay / fmtDuration / fmtNumber;
   setLanguageGetter(fn) lets us inject i18n without cyclic imports. */
// ui/src/i18n/fmt.ts
//
// Formatting helpers — date / time / number — bound to the active i18n.language.
// Per design.md §10, every on-screen
// formatted value MUST go through Intl keyed off the active language, so a
// browser switch from en to zh-cn flips both number and time presentation.
//
// All formatters read the current language via a getter the index module
// injects (setLanguageGetter). This breaks the circular dependency
// fmt.ts → i18n → locales and lets fmt.ts stay a pure, side-effect-free module.
//
// English is the default; the boot sequence in main.jsx calls setLanguageGetter
// after i18next has loaded.
// -----------------------------------------------------------------------------

// Internal code → BCP-47 mapping. Both i18n.language ("en" | "zh-cn") and
// Intl accept the internal codes directly; zh-cn resolves to zh-Hans-CN and en
// resolves to en-US defaults. We keep the mapping local to fmt.ts so the rest
// of the app uses the internal codes exclusively.
const LANG_MAP = { en: "en", "zh-cn": "zh-CN" } as const
type Internal = keyof typeof LANG_MAP

// Current-language getter, set by i18n/index.js after init. Returns 'en'
// before init to guarantee fmt.ts never throws at import time.
let _languageGetter: () => string = () => "en"

export function setLanguageGetter(fn: () => string): void {
  _languageGetter = fn
}

function locale(): string {
  const code = _languageGetter()
  return (LANG_MAP as Record<string, string>)[code] || LANG_MAP.en
}

// ---- Date / time ------------------------------------------------------------------

// fmtDateTime(ts, style) — short → MM-DD HH:mm 24h, medium → full locale string.
// ts is the epoch seconds supplied by the backend; pass undefined for "—".
// Spec: §8.2 (interaction-design). Short uses 24h so the fixed columns stay narrow
// in both languages.
export function fmtDateTime(
  ts: number | string | null | undefined,
  style: "short" | "medium" = "medium",
): string {
  if (ts === null || ts === undefined || ts === "") return "—"
  const n = typeof ts === "string" ? Number(ts) : ts
  const d = new Date(n * 1000)
  if (Number.isNaN(d.getTime())) return "—"
  const loc = locale()
  if (style === "short") {
    return new Intl.DateTimeFormat(loc, {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(d)
  }
  return new Intl.DateTimeFormat(loc, {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(d)
}

// fmtDay(ts) — alias for the short form, used by board timeline cells that
// always want MM-DD HH:mm (regardless of style arg). Provided as a named
// helper to keep call sites readable.
export function fmtDay(ts: number | string | null | undefined): string {
  return fmtDateTime(ts, "short")
}

// ---- Number / duration ------------------------------------------------------------

// fmtNumber — thin wrapper over Intl.NumberFormat so call sites import one symbol.
// Bare formatter with default options (no currency, no percent). Used for any
// user-visible number; digit grouping follows the active locale.
export function fmtNumber(n: number): string {
  if (typeof n !== "number" || Number.isNaN(n)) return "—"
  return new Intl.NumberFormat(locale()).format(n)
}

// fmtDuration(s) — seconds to "{n}s". Per interaction-design §8.2 durations
// stay language-agnostic ("5s", "12s") in both locales — universal dev-tool
// convention, simpler than dealing with pluralization here.
export function fmtDuration(s: number | null | undefined): string {
  if (typeof s !== "number" || Number.isNaN(s)) return "—"
  return `${Math.round(s)}s`
}
