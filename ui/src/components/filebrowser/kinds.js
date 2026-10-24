// kinds.js — 文件类型判定与格式化 (issue #46, 前后端共持)
// 设计依据: docs/ARCH-FILEBROWSER.md §3.6 (s3-arch 定稿)
// 单一职责: 纯函数, 无 React 依赖, 可在 ui/tests/*.mjs 直接单测。
//
// 口径约定 (与 mw/console_files.py 逐行一致):
//   md   = .md .markdown            渲染: marked+DOMPurify → .md-body
//   html = .html .htm               渲染: iframe sandbox embed
//   pdf  = .pdf                     渲染: iframe (3s 超时 → pdf_fallback)
//   image= .png .jpg .jpeg .gif .webp .bmp
//                                   渲染: <img> (svg 已移除, ADR-4)
//   text = .txt .log .json .yaml .yml .csv .py .js .jsx .sh .ini .toml
//                                   渲染: <pre> 等宽
//   other= 其余一切 (含 .svg .zip .db .docx …) → 兜底卡

const TEXT_EXT = new Set([
  '.md', '.markdown', '.txt', '.log', '.json', '.yaml', '.yml',
  '.csv', '.py', '.js', '.jsx', '.sh', '.ini', '.toml',
])
const HTML_EXT = new Set(['.html', '.htm'])
const PDF_EXT = new Set(['.pdf'])
const IMAGE_EXT = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'])

// 聊天链接识别白名单 = md + html + pdf + image + text 全集 (不含 svg, OQ-1/ADR-4)
export const KNOWN_EXTS = new Set([
  ...TEXT_EXT, ...HTML_EXT, ...PDF_EXT, ...IMAGE_EXT,
])

// 取扩展名 (lowercase, 不含 .)
export function extOf(name) {
  if (typeof name !== 'string') return ''
  const i = name.lastIndexOf('.')
  if (i <= 0 || i === name.length - 1) return ''
  return name.slice(i + 1).toLowerCase()
}

// kind 判定
export function kindOf(name) {
  const e = '.' + extOf(name)
  // md 优先于 text (md 是 text 的子集)
  if (e === '.md' || e === '.markdown') return 'md'
  if (HTML_EXT.has(e)) return 'html'
  if (PDF_EXT.has(e)) return 'pdf'
  if (IMAGE_EXT.has(e)) return 'image'
  if (TEXT_EXT.has(e)) return 'text'
  return 'other'
}

// 色点 (UX §5.1): md=accent, html=warn, pdf=bad, image=ok, text=dim, other=dim
export function dotClass(name) {
  const k = kindOf(name)
  if (k === 'md') return 'bg-accent'
  if (k === 'html') return 'bg-warn'
  if (k === 'pdf') return 'bg-bad'
  if (k === 'image') return 'bg-ok'
  return 'bg-dim/60'
}

// 文件大小格式化 (UX §5.2)
export function formatSize(bytes) {
  if (typeof bytes !== 'number' || bytes < 0) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

// 修改时间格式化 YYYY-MM-DD HH:mm (本地时区)
export function formatMtime(unixSec) {
  if (!unixSec) return ''
  const d = new Date(unixSec * 1000)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}`
}

// 5MB 上限 (OQ-4 硬拒, 前端预挡)
export const SIZE_LIMIT = 5 * 1024 * 1024
export function isTooLarge(bytes) {
  return typeof bytes === 'number' && bytes > SIZE_LIMIT
}