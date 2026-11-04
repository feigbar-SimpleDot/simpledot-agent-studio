import React from 'react'

// [en] icons.jsx — shared SVG icon set (issue #202 R4).
//      Single stroke style (1.8w round caps, heroicons-outline family) so
//      every functional glyph renders identically across fonts/themes.
//      Semantic slots (status badges, close/plus, emoji slots) must use this
//      set instead of text characters (✕ ✓ ● ↻ ⌛ ⋯ ○ ＋) or emoji.
//      Decorative instances must stay aria-hidden (the Icon component sets
//      aria-hidden by default; callers add title/aria-label on the *button*,
//      never on the svg).
// [zh-cn] icons.jsx —— 全站共享 SVG 图标体系 (issue #202 R4)。统一 1.8w 圆头
//        描边 (heroicons-outline 系), 语义位 (状态徽章/关闭/加号/emoji 位)
//        一律用本体系, 不再用文字符号或 emoji。Icon 默认 aria-hidden;
//        动作含义由外层按钮的 title/aria-label/相邻文字承载 (AC-R4-3)。

export function Icon({ d, className = 'w-5 h-5', strokeWidth = 1.8 }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={strokeWidth}
         strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden="true">
      <path d={d} />
    </svg>
  )
}

// Path table — one entry per semantic slot. Multi-subpath strings are fine
// (single <path> with several M segments renders as one glyph).
export const ICONS = {
  // generic actions
  close: 'M6 18L18 6M6 6l12 12',
  plus: 'M12 5v14M5 12h14',
  check: 'M5 13l4 4L19 7',
  refresh: 'M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15',
  external: 'M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6m4-3v6m-1 9l5-5m0 0l-5-5m5 5H10',
  upload: 'M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M17 8l-5-5-5 5M12 3v12',
  download: 'M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3',
  send: 'M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z',
  // status semantics (TasksBoardView classify badges / timeline nodes)
  alert: 'M12 9v3.5m0 3.5h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z',
  dot: 'M12 12h.01',            // render with strokeWidth>=5 + round cap = filled dot
  ellipsis: 'M5 12h.01M12 12h.01M19 12h.01', // render with strokeWidth>=2.5 = "⋯"
  clock: 'M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z',
  circle: 'M21 12a9 9 0 11-18 0 9 9 0 1118 0z',
  // info / media slots (replacing emoji)
  info: 'M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 1118 0z',
  warn: 'M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0zM12 9v4m0 4h.01',
  file: 'M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8zM14 2v6h6',
  doc: 'M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8zM14 2v6h6M16 13H8M16 17H8',
  camera: 'M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2zM12 17a4 4 0 100-8 4 4 0 000 8z',
  eye: 'M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8zM12 15a3 3 0 100-6 3 3 0 000 6z',
  chat: 'M21 11.5a8.38 8.38 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.38 8.38 0 01-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.38 8.38 0 013.8-.9h.5a8.48 8.48 0 018 8v.5z',
  cube: 'M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4',
  pencil: 'M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z',
  inbox: 'M22 12h-6l-2 3h-4l-2-3H2M5.45 5.11L2 12v6a2 2 0 002 2h16a2 2 0 002-2v-6l-3.45-6.89A2 2 0 0016.76 4H7.24a2 2 0 00-1.79 1.11z',
}
