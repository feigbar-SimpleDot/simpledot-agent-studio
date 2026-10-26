import React, { useEffect, useState, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { api, fmtApiError } from '../api.js'
import { Modal, Toast, Confirm } from '../components/ui.jsx'
import { fmtDateTime } from '../i18n/fmt.ts'

// ════════════════════════════════════════════════════════════════════
// TasksBoardView — i18n v1.0 conversion
//
// [en] Tasks board view (UIUX-DESIGN §3). All user-visible Chinese copy is
//      routed through t(); status enums stay protocol (WORKING/HANDOFF/...)
//      and only their badge text is translated. Time formatting goes through
//      fmtDateTime (interaction-design §8.2, MM-DD HH:mm 24h in both locales).
//      Status column bumped 84→112 and StatusBadge w-76→min-76/max-112 so
//      longer English labels ("Needs Reply") fit per §6.3. Every truncate
//      element gets a title attribute.
// [zh-cn] 任务看板(UIUX-DESIGN §3)。所有用户可见中文走 t(),状态 enum 仍是
//        协议(WORKING/HANDOFF/...),只有其徽章文本被翻译。时间格式化走
//        fmtDateTime(交互设计 §8.2),状态列 84→112,StatusBadge w-76
//        → min-76/max-112 适配英语长标签,所有 truncate 加 title。
// ════════════════════════════════════════════════════════════════════

// ── 小工具 ──────────────────────────────────────────────────────────
// [en] ts(x) — short-format timestamp, MM-DD HH:mm 24h in both locales
//      (interaction-design §8.2). Kills the hardcoded 'zh-CN' that used
//      to live here.
// [zh-cn] ts(x) — 短格式时间戳,MM-DD HH:mm 24h,清掉了原来硬编码的 'zh-CN'。
const ts = (x) => fmtDateTime(x, 'short')

// [en] trunc(s, n) — bounded truncation; full value goes on `title`.
// [zh-cn] trunc(s, n) — 截断;完整值挂 title。
const trunc = (s, n = 18) => (s && s.length > n ? s.slice(0, n) + '…' : s || '—')

// ════════════════════════════════════════════════════════════════════
// Issue #58 — token 消耗量显示 (API-SPEC-58 §4, 纯展示零新交互 AC-U7)
//
// [en] Read-only token summary helpers:
//        formatTokens(n)  — 0→'0'; <1M thousands-separated; ≥1M→'1.2M'; ≥1B→'1.2B'
//        formatCost(usd)  — '<$0.01' for tiny positives, else '$X.YZ'
//        tokenLabel / tokenTitle — step-row (card.worker_report.tokens) display
//        runTokenLabel / runTokenTitle — run-level (server token_total) display
//      Missing data renders em dash (distinct from a real 0 — AC-U2).
// [zh-cn] 只读 token 摘要 helper: 缺失数据显示 "—" (与真实 0 可区分, AC-U2);
//        run 级缺失角标以「已采集/应采集」呈现 (REQUIREMENTS-58 §5.3)。
// ════════════════════════════════════════════════════════════════════

// [en] formatTokens — spec API-SPEC-58 §4.5 (semantic per spec, impl local)
function formatTokens(n) {
  if (typeof n !== 'number' || !Number.isFinite(n) || n <= 0) return '0'
  if (n < 1000) return String(n)
  if (n < 1e6) return n.toLocaleString('en-US')
  if (n < 1e9) return (n / 1e6).toFixed(1) + 'M'
  return (n / 1e9).toFixed(1) + 'B'
}

// [en] formatCost — '$0.00' at zero, '<$0.01' for tiny positives
function formatCost(usd) {
  const v = (typeof usd === 'number' && Number.isFinite(usd)) ? usd : 0
  if (v > 0 && v < 0.01) return '<$0.01'
  return '$' + v.toFixed(2)
}

// [en] issue #68: 展示侧整数化 — 脏浮点桶值 (历史脏数据) 与 run 级聚合口径对齐。
// 后端 mw/console.py::_aggregate_run_tokens 只信 int 桶值 (ARCHITECTURE-58 §5.1
// 既定防御), 浮点按 0 计; 展示侧直读原值会出现「卡上显示 150.5 / run 合计算 0」
// 的两处口径不一致。此处截断为整数, 与聚合侧对齐 (聚合侧不动, 见 issue #68 裁决)。
// Math.trunc 只对数值生效; 非数值 / NaN / Infinity 走 0 分支 (与 formatTokens 同)。
function truncTokens(n) {
  if (typeof n !== 'number' || !Number.isFinite(n)) return 0
  return Math.trunc(n)
}

// [zh-cn] detail title 模板共享组装: captured_at 数字按 ISO 展示 (AC-U2)
const _capAtIso = (tsVal) =>
  (typeof tsVal === 'number' ? new Date(tsVal * 1000).toISOString() : '—')

// [en] step-row label/title from card-level snapshot (undefined → 缺失 "—")
function tokenLabel(t, tokens) {
  if (!tokens || tokens.source !== 'api') return t('tboard.token_missing')
  return formatTokens(truncTokens(tokens.total_tokens))
}
function tokenTitle(t, tokens) {
  if (!tokens) return t('tboard.token_missing')
  return t('tboard.token_detail_title', {
    total: truncTokens(tokens.total_tokens),
    in: tokens.input_tokens ?? 0, out: tokens.output_tokens ?? 0,
    cr: tokens.cache_read_tokens ?? 0, cw: tokens.cache_write_tokens ?? 0,
    reason: tokens.reasoning_tokens ?? 0,
    cost: formatCost(tokens.estimated_cost_usd),
    captured_at: _capAtIso(tokens.captured_at),
    source: tokens.source || 'unavailable',
  })
}

// [en] run-level (server token_total): unavailable → "—"; partial → 角标
function runTokenLabel(t, tt) {
  if (!tt || tt.source === 'unavailable') return t('tboard.token_missing')
  const base = `${formatTokens(tt.total_tokens || 0)}/${formatCost(tt.estimated_cost_usd)}`
  if (tt.source === 'partial') {
    return `${base} ${t('tboard.token_short_label')} ${tt.captured}/${(tt.captured || 0) + (tt.missing || 0)}`
  }
  return base
}
function runTokenTitle(t, tt) {
  if (!tt) return t('tboard.token_missing')
  return t('tboard.token_detail_title', {
    total: tt.total_tokens ?? 0,
    in: '—', out: '—', cr: '—', cw: '—', reason: '—',
    cost: formatCost(tt.estimated_cost_usd),
    captured_at: '—',
    source: tt.source || 'unavailable',
  })
}

// [en] TokenSummary — one read-only text span with a hover title.
//      Shared by TaskCard header / StepTimeline rows / CurrentTaskCard.
//      No button, no state, no timers (AC-U7).
// [zh-cn] TokenSummary — 只读文本段 + 悬浮 title, 三处落点共用; 零新交互。
function TokenSummary({ label, title, className }) {
  return (
    <span className={`text-[11.5px] text-dim tabular-nums truncate ${className || ''}`}
          title={title}>
      {label}
    </span>
  )
}

// ════════════════════════════════════════════════════════════════════
// Issue #14 (2026-10-17) — 目标格式化 + 折叠抽屉 + 完成结果 chip 化
//
// [en] Pure helpers for the todo-viewer:
//        splitGoalSegments(goal)  — detect multi-line / overlong goal,
//          return { preview, full, truncated, hiddenLineCount } so GoalCell
//          can render a default-collapsed details drawer once we exceed a
//          soft limit. Fixes the long-standing "目标看不到" bug where the
//          goal body was emitted by main with a trailing \n + a leading
//          blank on the next visual line and CSS was clipping the result.
//        parseDoneResult(text)    — split `coder | 卡 t_… | commits abc+def`
//          result strings of completed history cards into typed segments
//          { kind: 'assignee' | 'card' | 'commits' | 'note', value }
//          so HistoryList can render them as chip/badge; leftover free
//          text collapses into a default-collapsed details drawer.
// [zh-cn] todo viewer 用纯函数:
//        splitGoalSegments(goal)  — 目标多行/过长时返回 {preview, full,
//          truncated, hiddenLineCount}, 目标长时由 GoalCell 默认折叠抽屉。
//          修复"目标看不到"长期 bug: main 写目标尾部带 \n + 首行为空,
//          CSS 在第一空行处对齐即把内容判掉。
//        parseDoneResult(text)    — 把已完成区 `coder | 卡 t_… | commits`
//          类结果字符串切成 {kind, value} 段, HistoryList 据此渲染 chip/
//          badge; 剩余自由文本降级到默认折叠抽屉。
// ════════════════════════════════════════════════════════════════════

// [en] Goal visual caps. Preview shows the first non-empty paragraph
//      truncated by characters, with hidden section folded by default.
//      Tuned for 2/3-col layout in TodoViewerModal; long enough to keep
//      enough context, short enough not to fill the modal viewport.
// [zh-cn] 目标视觉上限。预览 = 首个非空段截 N 字符;超出时默认折叠抽屉。
const GOAL_PREVIEW_LINES = 2   // 预览最多保留的非空行
const GOAL_PREVIEW_CHARS = 140 // 单段超过此长度也按"过长"折叠
const GOAL_FULL_CHARS = 600    // 全文 ≥ 此值仍保留折叠按钮 (即便行数 ≤ 上限)

// [en] splitGoalSegments(goal) — see header. Defensive: trims trailing
//      blank-line noise, distinguishes "too long" by line count or by
//      any single non-empty line longer than GOAL_PREVIEW_CHARS.
//      Returns { preview, full, truncated, hiddenLineCount } where
//      `truncated` is true iff anything should fold away.
// [zh-cn] splitGoalSegments(goal): 详见头注释;剥离尾部空行, 按行数/单行
//      长度判断是否折叠。
function splitGoalSegments(goal) {
  const raw = (goal == null ? '' : String(goal))
  if (!raw.trim()) return { preview: '', full: '', truncated: false, hiddenLineCount: 0 }
  // 规范化: 末尾多余空行剥掉; 段间空行合并成单 \n
  const text = raw.replace(/\n{3,}/g, '\n\n').replace(/[\r\t]+/g, ' ').trimEnd()
  const allLines = text.split('\n')
  // 选择预览: 前 N 个非空连续行, 单行过长则截断字符
  const previewLines = []
  let usedLines = 0, reachedBlank = false
  let anyCharTruncated = false
  for (let i = 0; i < allLines.length; i++) {
    const ln = allLines[i]
    if (ln.trim() === '') { reachedBlank = true; continue }
    if (reachedBlank) break
    if (previewLines.length >= GOAL_PREVIEW_LINES) break
    let pushed = ln
    if (ln.length > GOAL_PREVIEW_CHARS) {
      pushed = ln.slice(0, GOAL_PREVIEW_CHARS).trimEnd() + '…'
      anyCharTruncated = true  // 标记有字符级截断
      reachedBlank = true
    }
    previewLines.push(pushed)
    usedLines = i + 1
    if (ln.length > GOAL_PREVIEW_CHARS) break
  }
  const preview = previewLines.join('\n').trimEnd()
  const moreLines = allLines.slice(usedLines).join('\n').trimStart()
  // issue #43 fix: 被截断行的剩余字符算隐藏内容
  // 若存在字符截断(anyCharTruncated)但moreLines为空,仍应显示1行(被截断的那行)
  // [#88 复核结论 —— 此三元【不是】死代码, 勿按 splitRunTitle 的形态删成 ': 0']
  // 可达充要条件: 被字符级截断的行是 allLines 的【最后一行】且 index <= 1
  //   (即输入只含 1-2 个非空行、超长行在末位)。例: 'x'.repeat(141) ⇒ hiddenLineCount=1。
  // 与 splitRunTitle 的差别: 本函数循环尾的 usedLines=i+1 (previewLines.push 之后) 无条件执行,
  //   被截行被计入 usedLines ⇒ slice(usedLines) 可为空 ⇒ moreLines 可为空;
  //   splitRunTitle 则是超长行 push 后立即 break, break 在其 usedLines=i+1 赋值【之前】, 故其不可达。
  // 此 else 侧同时是 GoalCell 的折叠开关守卫 (其 if (!seg.truncated || seg.hiddenLineCount <= 0) 直渲染分支):
  //   truncated=true 且 hiddenLineCount<=0 会让抽屉整个消失, 故不可简化。
  const hiddenCount = moreLines
    ? allLines.length - usedLines
    : (anyCharTruncated ? 1 : 0)
  // truncated 触发的三个条件 (任一满足 → 折叠):
  //   1) 预览非全文 (有 moreLines)
  //   2) 全文超全文阈值 (GOAL_FULL_CHARS)
  //   3) 任一段被字符级截断 (previewLines 里包含 "…")
  const truncated = moreLines.length > 0 || text.length > GOAL_FULL_CHARS || anyCharTruncated
  return { preview, full: text, truncated, hiddenLineCount: hiddenCount }
}

// [en] Run-title visual cap for the plan modal (#79). Same shape as the
//      issue #14 goal cell: bounded preview + default-collapsed <details>
//      holding the full value. Tuned for the 2xl modal: 2 lines ≈ 44px.
//      注意与 splitGoalSegments 的差别: truncated 只看行数/单行字符数,
//      不看全文长度 —— 弹窗容器宽度固定, 单行长标题视觉只占 1 行,
//      无谓折叠反而多出一个控件。
// [zh-cn] 弹窗 run 标题视觉上限 (#79), 与 issue #14 目标单元格同形。
const RUN_TITLE_PREVIEW_LINES = 2   // 预览保留行数 (≥2 行可读, 再多挤压弹窗首屏)
const RUN_TITLE_PREVIEW_CHARS = 120 // 单行超此字符数按字符级截断

function splitRunTitle(raw) {
  const text = (raw == null ? '' : String(raw)).trim()
  if (!text) return { preview: '', full: '', truncated: false, hiddenLineCount: 0 }
  const allLines = text.split('\n')
  const previewLines = []
  let usedLines = 0, charTruncated = false
  for (let i = 0; i < allLines.length; i++) {
    const ln = allLines[i]
    if (previewLines.length >= RUN_TITLE_PREVIEW_LINES) break
    if (ln.length > RUN_TITLE_PREVIEW_CHARS) {
      // 超长行: 字符级截断后仍要进预览 (先 push 再 break; 若照 PLAN 片段
      // 先 break 会得到空 preview, 与其 T79-1 断言矛盾), 与 splitGoalSegments 同义。
      previewLines.push(ln.slice(0, RUN_TITLE_PREVIEW_CHARS).trimEnd() + '…')
      charTruncated = true
      break
    }
    previewLines.push(ln)
    usedLines = i + 1
  }
  const preview = previewLines.join('\n').trimEnd()
  const more = allLines.slice(usedLines).join('\n').trimStart()
  const hiddenLineCount = more ? allLines.length - usedLines : 0  // #87 S-1: else 侧恒为 0 (字符截断 ⇒ more 恒真 ⇒ 原计数子表达式不可达, 见 PLAN-87-86 §1.2)
  const truncated = more.length > 0 || charTruncated
  return { preview, full: text, truncated, hiddenLineCount }
}

// [en] parseDoneResult(text) — see header. Returns array of segment objects
//      preserving original order. Pipeline:
//        1) split input on '|' (with optional surrounding spaces).
//        2) for each piece, run a *kind detection cascade* (case-insensitive):
//             a) explicit anchor (`承接方:`, `assignee:`, `@…`) → assignee
//             b) explicit anchor (`卡:`, `card:`, `kanban…`)       → card
//             c) explicit anchor (`commits:`, `commit:`, `提交:`)   → commits
//             d) bare worker name (^[a-z][a-z0-9_]*$ and not in
//                STOPWORDS like 'commits'/'card'/'kanban'/'fix'…)     → assignee
//             e) bare kanban id (`^t_[0-9a-f]{6,}$`)                    → card
//             f) bare commit sha prefix (hex 7-40 chars alone)          → commits
//             g) else                                                  → note
//        3) skip blank pieces; if everything ends up blank, fall back to
//           a single `note` segment with the trimmed raw so the
//           HistoryList renders at least the raw text instead of nothing.
// [zh-cn] parseDoneResult(text): 详见头注释; 识别优先级 (case-insensitive):
//        a) 显式前缀 → assignee/card/commits
//        b) 裸 worker 名 (^[a-z][a-z0-9_]*$) → assignee
//        c) 裸 kanban id (^t_…$) → card
//        d) 裸 sha 前缀 (7-40 hex) → commits
//        e) 其余 note
const _ASSIGNEE_HINT_RE = /^(?:承接方[::]?\s*|assignee[:\s]?\s*|@)/i
const _CARD_HINT_RE = /^(?:卡[::]?\s*|card[:\s]?\s*|kanban[_ ]?task[:\s]?\s*|kanban[:\s]?\s*)/i
const _COMMITS_HINT_RE = /^(?:commits?[:\s]?\s*|提交[::]?\s*)/i
// 裸 worker 名: 全小写字母/数字/下划线, 长度 ≤ 32
const _BARE_WORKER_RE = /^[a-z][a-z0-9_]{1,31}$/
// 裸 kanban id: t_<hex> 形态
const _BARE_KANBAN_RE = /^t_[0-9a-f]{6,}$/i
// 裸 commit 哈希: 7~40 hex 字符, 允许 +xxx 多 commit 形式 ('97949af+5xxxx44' 也接受)
const _BARE_SHA_RE = /^[0-9a-f]{7,40}(\+[0-9a-z]+)?$/i
// STOPWORDS: 形状像 worker 名但语义不属于 assignee (commands / common verbs)
const _WORKER_STOPWORDS = new Set([
  'card', 'cards', 'kanban', 'commit', 'commits', 'and', 'or', 'with',
  'notes', 'note',
])
function parseDoneResult(text) {
  const raw = (text == null ? '' : String(text))
  const trimmed = raw.trim()
  if (!trimmed) return []
  if (!trimmed.includes('|')) {
    // 单段输入仍要过识别器 — 裸 worker 名/t_xxx/sha 也能被识别成 chip。
    // 例: "kanban t_xxxxx" 用户期望 card chip, 而不是整段 note。
    return [_classifySegment(trimmed)]
  }
  const segments = []
  for (const part of trimmed.split('|')) {
    const v = part.trim()
    if (!v) continue
    segments.push(_classifySegment(v))
  }
  return segments.length ? segments : [_classifySegment(trimmed)]
}
function _classifySegment(v) {
  if (_ASSIGNEE_HINT_RE.test(v)) {
    return { kind: 'assignee', value: v.replace(_ASSIGNEE_HINT_RE, '').trim() || v }
  }
  if (_CARD_HINT_RE.test(v)) {
    return { kind: 'card', value: v.replace(_CARD_HINT_RE, '').trim() || v }
  }
  if (_COMMITS_HINT_RE.test(v)) {
    return { kind: 'commits', value: v.replace(_COMMITS_HINT_RE, '').trim() || v }
  }
  // 裸 kanban id 优先于裸 worker 名 — t_xxxxxxxx 形态比 worker 名更特异
  // (worker 名 ^t_xxx 含下划线可能误命中)
  if (_BARE_KANBAN_RE.test(v)) {
    return { kind: 'card', value: v }
  }
  // 裸 sha (含 +n_more 形态) 优先于 worker 名 (纯 hex 字符串)
  if (_BARE_SHA_RE.test(v)) {
    return { kind: 'commits', value: v }
  }
  // 裸 worker 名 → assignee (必须不在 STOPWORDS)
  if (_BARE_WORKER_RE.test(v) && !_WORKER_STOPWORDS.has(v)) {
    return { kind: 'assignee', value: v }
  }
  return { kind: 'note', value: v }
}

// 复制到剪贴板: clipboard API 优先, 失败降级 execCommand
async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(text); return true }
  } catch { /* 无用户激活/无权限, 走降级 */ }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch { return false }
}

// ── 状态语义层 (DESIGN §3.1) ─────────────────────────────────────────
// [en] runStatus(s) — maps protocol run status enum to a translated badge.
//      The returned object: { label: translated text, cls: tailwind color class, pulse: bool }.
// [zh-cn] runStatus(s) —— 把协议态 run status enum 映射成翻译后的徽章。
function runStatus(s, t) {
  if (s === 'ACTIVE') return { label: t('tboard.run_status_active'), cls: 'badge-blue', pulse: true }
  if (s === 'ABORTED') return { label: t('tboard.run_status_aborted'), cls: 'badge-red' }
  if (s === 'ARCHIVED') return { label: t('tboard.run_status_archived'), cls: 'badge-slate opacity-60' }
  if (s === 'CLOSED') return { label: t('tboard.run_status_closed'), cls: 'badge-green' }
  return { label: t('tboard.run_status_unknown'), cls: 'badge-slate' }
}

// [en] classify(t) — single-source derivation (issue #5) of step state.
//      Returns { group, badge, sem: translated text, icon, dataState }.
//      The `sem` text goes through t() so both locales render correctly
//      while server-derived state enums stay untouched (D10).
// [zh-cn] classify(t) —— 步骤态单源派生(issue #5)。`sem` 走 t(),后端派生的
//        state enum 仍是协议值(D10 禁区不动)。
function classify(card, t) {
  // 单源派生 (F5): server 给了 state 字段时优先用, 没有才走原 fallback
  // (向后兼容旧版 server 没回 state 字段时)。 data-state 字段也会被
  // 加上便于 CSS / 调试针对性选择。
  const sv = card && card.state
  if (sv) {
    if (sv === 'HARVESTED' || sv === 'ARCHIVED')
      return { group: 'finished', badge: 'badge-green', sem: t('tboard.state_finished'), icon: '✓', dataState: sv }
    if (sv === 'NEED_CLARIFICATION')
      return { group: 'attention', badge: 'badge-yellow', sem: t('tboard.state_attention'), icon: '!', dataState: sv }
    if (sv === 'BLOCKED' || sv === 'ERROR')
      return { group: 'blocked', badge: 'badge-red', sem: t('tboard.state_blocked'), icon: '!', dataState: sv }
    if (sv === 'WORKING')
      return { group: 'running', badge: 'badge-blue', sem: t('tboard.state_running'), icon: '●', dataState: sv }
    if (sv === 'HANDOFF')
      return { group: 'handoff', badge: 'badge-purple', sem: t('tboard.state_handoff'), icon: '↻', dataState: sv }
    if (sv === 'WAITING')
      return { group: 'waiting', badge: 'badge-amber', sem: t('tboard.state_waiting'), icon: '⌛', dataState: sv }
    if (sv === 'PENDING')
      return { group: 'pending', badge: 'badge-slate', sem: t('tboard.state_pending'), icon: '○', dataState: sv }
    if (sv === 'CANCELLED')
      return { group: 'finished', badge: 'badge-slate', sem: t('tboard.state_cancelled'), icon: '✕', dataState: sv }
  }
  // Fallback: 旧版派生 (保持向后兼容 — 老 server / 直接 consumer
  // kanban 数据时仍可工作)
  if (card.status === 'done' || card.status === 'archived')
    return { group: 'finished', badge: 'badge-green', sem: t('tboard.state_finished'), icon: '✓' }
  if (card.harvest_status === 'NEED_CLARIFICATION' ||
      (card.status === 'blocked' && card.block_kind === 'needs_input'))
    return { group: 'attention', badge: 'badge-yellow', sem: t('tboard.state_attention'), icon: '!' }
  if (card.status === 'blocked')
    return { group: 'blocked', badge: 'badge-red', sem: t('tboard.state_blocked'), icon: '!' }
  if (card.status === 'running')
    return { group: 'running', badge: 'badge-blue', sem: t('tboard.state_running'), icon: '●' }
  return { group: 'pending', badge: 'badge-slate', sem: t('tboard.state_pending'), icon: '○' }
}

// 统一状态徽章 (pulse=进行中呼吸圆点; 定宽居中, 对齐规范 §3.3)
// [en] StatusBadge — w-[76px] → min-w-[76px] max-w-[112px] (interaction-design
//      §6.3) so longer English labels like "Needs Reply" fit without truncation
//      while the 'finished' / 'pending' / 'running' short texts stay centered.
// [zh-cn] StatusBadge —— w-[76px] → min-w-[76px] max-w-[112px],英语长徽章
//        ("Needs Reply" 11 字符)不会撑爆,短徽章仍居中。
function StatusBadge({ cls, pulse = false, wide = false, className = '', children, title }) {
  return (
    <span title={title}
      className={`${cls} inline-flex items-center justify-center gap-1.5 px-2 py-0.5 rounded text-[10.5px] font-medium shrink-0
                      ${wide ? 'min-w-[76px] max-w-[112px]' : 'min-w-[64px]'} ${className}`}>
      {pulse && <span className="w-1.5 h-1.5 rounded-full bg-accent animate-pulse shrink-0" />}
      <span className="truncate">{children}</span>
    </span>
  )
}

// 执行者徽章 (定宽居中, 超长截断, 对齐规范 §3.3)
// [en] AssigneeBadge — kept `min-w-[56px] max-w-[84px]` (Latin worker names
//      already fit). Placeholder uses t('tboard.unassigned').
// [zh-cn] AssigneeBadge —— 保留 min-w-[56px] max-w-[84px](拉丁 worker 名字
//        本来就能装下)。placeholder 走 t('tboard.unassigned')。
function AssigneeBadge({ name, t }) {
  const nm = name || t('tboard.unassigned')
  return (
    <span className="badge-blue px-1.5 py-0.5 rounded text-[10.5px] shrink-0
                   justify-self-center min-w-[56px] max-w-[84px] truncate text-center"
          title={nm}>
      {nm}
    </span>
  )
}

// 折叠箭头 (16px SVG, 展开态旋转 90°, 对齐规范 §3.5)
function Chevron({ open }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
         className={`w-4 h-4 text-dim transition-transform shrink-0 ${open ? 'rotate-90' : ''}`}>
      <path d="M9 6l6 6-6 6" />
    </svg>
  )
}

// ── 步骤纵向时间线 (DESIGN §3.4) ────────────────────────────────────
function StepTimeline({ detail, currentId, busy, onReply, onCancel, onArchive, t }) {
  const all = detail.cards || []
  const cards = all.filter((c) => !c.orphan)
  const orphans = all.filter((c) => c.orphan)
  if (!all.length) return (
    <div className="text-dim text-[12.5px] py-3 text-center">{t('tboard.step_unregistered')}</div>
  )
  return (
    <div className="min-w-0">
      {cards.map((step, i) => {
        const c = classify(step, t)
        const isCur = !!currentId && step.id === currentId
        const time = step.completed_at || step.started_at || step.created_at
        // s5 fix #11: 序号统一消费 server 注入的 exec_index (执行序 + 计划序)。
        // 兜底用 i+1 兼容旧版 server 未注入该字段的情况。两条都对齐规范 §3.4。
        const stepNo = (typeof step.exec_index === 'number') ? step.exec_index : (i + 1)
        // 追加卡视觉角标 (s5 fix #11 / D3 修正): 权威判定移到 server ——
        // card.appended = 本卡登记时刻晚于 run 内最早执行时刻 (mid-run
        // add_cards)。旧前端判定 (i+1 !== stepNo) 恒假属死代码已删。
        const isAppended = step.appended === true
        const stepTitle = isAppended
          ? t('tboard.step_appended_title', { n: stepNo })
          : t('tboard.step_index_marker', { n: stepNo })
        return (
          <div key={step.id} className="relative flex gap-2 min-w-0"
               data-state={c.dataState || c.group}>
            {/* 时间线轴: 节点圆 20px + 连接线 */}
            <div className="w-[20px] flex flex-col items-center shrink-0 pt-3">
              <span className={`w-5 h-5 rounded-full border-[1.5px] flex items-center justify-center
                text-[11px] font-bold shrink-0
                ${c.group === 'finished' ? 'border-ok text-ok'
                  : c.group === 'attention' ? 'border-warn text-warn animate-pulse'
                  : c.group === 'blocked' ? 'border-bad text-bad'
                  : c.group === 'running' ? 'border-accent text-accent animate-pulse'
                  : c.group === 'handoff' ? 'border-warn text-warn animate-spin'
                  : c.group === 'waiting' ? 'border-warn text-warn'
                  : 'border-line text-dim'}`}>{c.icon}</span>
              {i < cards.length - 1 && <span className="w-[1.5px] flex-1 bg-line min-h-[20px]" />}
            </div>
            {/* 步骤内容: 固定列网格 (对齐规范 §3.1); 当前步高亮底, 等你回复整行黄底
                F5 (2026-10-13, issue #5): handoff / waiting 两态加上对应底色,
                桌面: 序号 | 标题 | 执行者 | 状态 | token(#58) | 时间 | 操作
                移动: 第一行 标题+状态, 第二行(从标题列起) 执行者+token+时间+操作, 换行位固定 */}
            <div className={`flex-1 min-w-0 py-2 px-2 mb-1 rounded-lg grid gap-x-2 gap-y-1 items-center
                            grid-cols-[minmax(0,1fr)_auto]
                            sm:grid-cols-[26px_minmax(0,1fr)_112px_76px_96px_104px_146px]
                            ${isCur ? 'bg-accent/5' : ''}
                            ${c.group === 'attention' ? 'bg-warn/5' : ''}
                            ${c.group === 'handoff' ? 'data-state-handoff' : ''}
                            ${c.group === 'waiting' ? 'data-state-waiting' : ''}`}>
              <span className="hidden sm:flex items-center gap-1 text-[11px] font-mono text-dim tabular-nums truncate"
                    title={stepTitle}>
                <span>#{stepNo}</span>
                {isAppended && (
                  <span className="badge-amber px-1 py-0 rounded text-[9px] leading-none">{t('tboard.step_appended_label')}</span>
                )}
              </span>
              <span className="text-[13px] font-medium truncate min-w-0"
                    title={step.title}>{step.title}</span>
              <StatusBadge cls={c.badge} wide title={c.sem}>{c.sem}</StatusBadge>
              <div className="col-start-1 row-start-2 flex items-center gap-2 min-w-0 sm:contents">
                <AssigneeBadge name={step.assignee} t={t} />
                {/* issue #116: 「已被 X 于 HH:MM 答复」角标 —— 双通道答复
                    互可见 (数据源: run 详情端点注入的 answered_by/hhmm) */}
                {step.answered_by && (
                  <span className="badge-amber px-1.5 py-0.5 rounded text-[10px] leading-none whitespace-nowrap"
                        title={t('tboard.answered_badge', { by: step.answered_by, time: step.answered_at_hhmm })}>
                    {t('tboard.answered_badge', { by: step.answered_by, time: step.answered_at_hhmm })}
                  </span>
                )}
                {/* issue #58 (AC-U2): 步骤行 token 段, 读 card.worker_report.tokens
                    (main 裁决 1 已由 server 注入); 历史卡缺失 → "—" */}
                <TokenSummary
                  label={tokenLabel(t, step.worker_report?.tokens)}
                  title={tokenTitle(t, step.worker_report?.tokens)}
                  className="whitespace-nowrap sm:w-[96px] sm:text-right" />
                <span className="text-[11px] text-dim whitespace-nowrap tabular-nums
                                sm:justify-self-end sm:w-[104px] sm:text-right">{ts(time)}</span>
                <span className="flex items-center gap-1.5 sm:justify-self-end">
                  {c.group === 'attention' && (step.session_id ? (
                    <button className="btn-primary btn-sm" onClick={() => onReply(step)}>{t('common.reply')}</button>
                  ) : (
                    <span className="text-dim text-[11px]">{t('tboard.current_step_unreplyable')}</span>
                  ))}
                  {c.group !== 'finished' && (
                    <button className="btn-ghost btn-sm !text-bad !border-bad/40 hover:!bg-bad/10"
                            disabled={busy[step.id] === 'cancel'} onClick={() => onCancel(step)}>
                      {busy[step.id] === 'cancel' ? t('tboard.cancel_busy') : t('tboard.cancel_btn')}
                    </button>
                  )}
                  <button className="btn-quiet btn-sm"
                          disabled={busy[step.id] === 'archive'} onClick={() => onArchive(step)}>
                    {busy[step.id] === 'archive' ? t('tboard.archive_busy') : t('tboard.archive_btn')}
                  </button>
                </span>
              </div>
            </div>
          </div>
        )
      })}
      {/* 异常记录: 普通用户不可操作, 折叠进技术详情留痕 */}
      {orphans.length > 0 && (
        <details className="mt-1 text-[11.5px] text-dim">
          <summary className="cursor-pointer select-none">{t('tboard.tech_details_orphans', { n: orphans.length })}</summary>
          <div className="mt-1.5 space-y-1 pl-2">
            {orphans.map((o) => (
              <div key={o.id} className="flex items-center gap-2">
                <code className="font-mono bg-panel2 px-1 rounded" title={o.id}>{trunc(o.id, 16)}</code>
                <span>{classify(o, t).sem}</span>
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  )
}

// ── 历史任务卡片 (DESIGN §3.4) ──────────────────────────────────────
function TaskCard({ run, detail, isOpen, loading, err, busy,
                   onToggle, onRetryDetail, onEndRun, onArchiveRun,
                   onReply, onCancel, onArchive, onShowTodo, t }) {
  const st = runStatus(run.status, t)
  const cards = (detail?.cards || []).filter((c) => !c.orphan)
  const done = cards.filter((c) => c.status === 'done' || c.status === 'archived').length
  const total = run.card_count ?? cards.length
  // 主列单值时间: 已结束显示结束时间, 未关闭显示创建时间 (对齐规范 §3.2)
  const mainTime = run.closed_at || run.created_at
  const timeLabel = run.closed_at
    ? t('tboard.ended_at', { time: ts(mainTime) })
    : t('tboard.created_at', { time: ts(mainTime) })
  const initialTitle = run.initial_task || t('tboard.empty_initial_task')
  return (
    <div className="card overflow-hidden">
      {/* 卡片头: 单行网格 箭头|标题|状态|token|进度|时间|操作 (§3.2, #58 加 token 列);
          <640px 降级两行 */}
      <div className={`grid gap-x-2 gap-y-1 items-center px-4 py-3 cursor-pointer select-none min-h-[44px]
                      grid-cols-[16px_minmax(0,1fr)_auto]
                      sm:grid-cols-[16px_minmax(0,1fr)_112px_96px_72px_104px_auto]
                      hover:bg-panel2/40 transition-colors
                      ${isOpen ? 'bg-panel2/30' : ''}`}
           onClick={onToggle}>
        <Chevron open={isOpen} />
        <span className="text-[13px] font-semibold truncate min-w-0" title={initialTitle}>
          {initialTitle}
        </span>
        <StatusBadge cls={st.cls} pulse={st.pulse} wide title={st.label}>{st.label}</StatusBadge>
        {/* issue #58 (AC-U1): run 级 token 总量 + 成本, 数据来自 /api/team-works
            的 run.token_total (server 聚合); title 保留全值 */}
        <TokenSummary
          label={runTokenLabel(t, run.token_total)}
          title={runTokenTitle(t, run.token_total)}
          className="whitespace-nowrap sm:text-right" />
        <div className="col-start-2 row-start-2 flex items-center gap-2 min-w-0 sm:contents">
          <span className="text-[11.5px] text-dim tabular-nums sm:w-[72px] sm:text-right">
            {detail ? t('tboard.progress_done_total', { done, total }) : t('tboard.progress_total', { total })}
          </span>
          <span className="text-[11.5px] text-dim tabular-nums whitespace-nowrap
                          sm:w-[104px] sm:text-right">
            {timeLabel}
          </span>
          <span className="shrink-0 flex items-center gap-1.5 sm:justify-self-end"
                onClick={(e) => e.stopPropagation()}>
            {/* 2026-10-11: 历史/进行中都能看 TODO.md (read-only)。
                始终展示, 即使 run 没有 project_workspace 后端也会给 ok+exists=false 空态。 */}
            <button onClick={() => onShowTodo(run)}
                    className="btn-quiet btn-sm"
                    title={t('tboard.plan_btn_title')}>
              {t('tboard.plan_btn_short')}
            </button>
            {run.status === 'ACTIVE' ? (
              <button onClick={onEndRun} className="btn-danger btn-sm">{t('tboard.finish_task')}</button>
            ) : run.status !== 'ARCHIVED' ? (
              <button onClick={onArchiveRun} className="btn-secondary btn-sm">{t('tboard.confirm_archive_ok')}</button>
            ) : (
              <button onClick={onArchiveRun} className="btn-secondary btn-sm opacity-60"
                      title={t('tboard.archived_idempotent')}>
                {t('tboard.already_archived')}
              </button>
            )}
          </span>
        </div>
      </div>
      {isOpen && (
        <div className="bg-canvas/60 border-t border-line/50 px-4 py-3">
          {loading && !detail && (
            <div className="space-y-2">
              {[...Array(2)].map((_, i) => <div key={i} className="skeleton h-10" />)}
            </div>
          )}
          {err && (
            <div className="flex items-center gap-3 text-bad text-[12.5px]">
              <span>{t('tboard.loading_error_detail', { err })}</span>
              <button className="btn-ghost btn-sm" onClick={onRetryDetail}>{t('tboard.retry')}</button>
            </div>
          )}
          {detail && (
            <StepTimeline detail={detail} currentId={run.current_card?.id || run.current_card_id}
              busy={busy} onReply={onReply} onCancel={onCancel} onArchive={onArchive} t={t} />
          )}
        </div>
      )}
    </div>
  )
}

// ── 当前任务卡 (DESIGN §3.3) ────────────────────────────────────────
function CurrentTaskCard({ active, detail, detailErr, closing,
                          onEnd, onReplyCard, onCopy, onRetryDetail,
                          busy, onCancel, onArchive, onShowTodo, t }) {
  // 全链路预览区折叠位: 默认展开 (需求原话是"看不到", 默认收起等于没修, §2.2)
  const [chainOpen, setChainOpen] = useState(true)
  const cards = (detail?.cards || []).filter((c) => !c.orphan)
  const done = cards.filter((c) => c.status === 'done' || c.status === 'archived').length
  const total = active.card_count ?? cards.length
  const pct = total > 0 ? Math.round((done / total) * 100) : 0
  // 当前步骤: 优先从详情取整卡(拿 question/session_id), 兜底用 tasks 摘要字段
  const cur = active.current_card_id
    ? (cards.find((c) => c.id === active.current_card_id) || null) : null
  const runState = active.state || null  // 由 api_team_work_get 注入
  const cardState = cur?.state || null
  const displayState = cardState || (cur && (
      cur.harvest_status === 'NEED_CLARIFICATION' ? 'NEED_CLARIFICATION' :
      cur.harvest_status === 'BLOCKED' ? 'BLOCKED' : null))
  const needsReply = displayState === 'NEED_CLARIFICATION'
  const isHandoff = runState === 'HANDOFF' || cardState === 'HANDOFF'
  const isWaiting = runState === 'WAITING_ACTIVE' || cardState === 'WAITING'
  const canReply = !!cur?.session_id
  const activeTitle = active.initial_task || t('tboard.empty_initial_task')
  const runLabel = runStatus(active.status, t).label
  const curTitle = active.current_card_title || '—'
  const assignee = active.current_card_assignee || t('tboard.unassigned')
  // run-level secondary badge: short translated labels for HANDOFF/WAITING_ACTIVE/
  // ACTIVE_BLOCKED/CLOSED. We use plain protocol text here only because these
  // four are *not* in classify(); each gets its own translated tag.
  const runExtraBadge = (() => {
    if (runState === 'HANDOFF') return { cls: 'badge-purple', text: t('tboard.state_handoff') }
    if (runState === 'WAITING_ACTIVE') return { cls: 'badge-amber', text: t('tboard.state_waiting') }
    if (runState === 'ACTIVE_BLOCKED') return { cls: 'badge-yellow', text: t('tboard.blocked_state_short') }
    if (runState === 'CLOSED') return { cls: 'badge-slate', text: t('tboard.closed_state_short') }
    return null
  })()
  return (
    <div className={`card p-5 border-accent/30 bg-accent/5
                     ${isHandoff ? 'data-state-handoff' : ''}
                     ${isWaiting ? 'data-state-waiting' : ''}`}>
      {/* 第一层级: 需求标题 + 状态 */}
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-[15px] font-bold truncate min-w-0" title={activeTitle}>
              {activeTitle}
            </span>
            <StatusBadge cls="badge-blue" pulse title={runLabel}>{runLabel}</StatusBadge>
            {/* F5: run 级派生一目了然, 让用户立刻看到「当前是交接中还是
              执行中」—— 不再依赖旧版「当前步骤高亮块」二次跳。 state
              字段由 server 给, fallback 只在 server 没给时显示 IDLE。 */}
            {runExtraBadge && (
              <StatusBadge cls={runExtraBadge.cls} title={runExtraBadge.text}>{runExtraBadge.text}</StatusBadge>
            )}
          </div>
          {/* 进度条 + 文案 (详情未就绪时降级为「共 y 步」) */}
          <div className="mt-3 flex flex-col sm:flex-row sm:items-center gap-2">
            <div className="flex-1 min-w-0 h-1.5 rounded-full bg-panel2 overflow-hidden">
              {detail
                ? <div className="h-full rounded-full bg-accent transition-all duration-300"
                       style={{ width: `${pct}%` }} />
                : <div className="h-full w-full skeleton !rounded-full" />}
            </div>
            <span className="text-[11.5px] text-dim tabular-nums shrink-0 sm:text-right">
              {detail
                ? t('tboard.progress_done_total', { done, total })
                : t('tboard.progress_total', { total })}
            </span>
            {/* issue #58 (AC-U4): ACTIVE run 累计 token。权威源 =
                api_team_work_get 注入的 detail.team_work.token_total
                (active 摘要对象若已带 token_total 则优先), 刷新即更新。 */}
            <TokenSummary
              label={runTokenLabel(t, active.token_total ?? detail?.team_work?.token_total)}
              title={runTokenTitle(t, active.token_total ?? detail?.team_work?.token_total)}
              className="shrink-0 sm:text-right" />
          </div>
        </div>
        <div className="flex flex-col gap-2 shrink-0">
          <button onClick={onShowTodo}
                  className="btn-secondary btn-sm whitespace-nowrap"
                  title={t('tboard.plan_btn_title')}>
            {t('tboard.view_plan_btn')}
          </button>
          <button onClick={onEnd} disabled={closing}
                  className="btn-danger btn-sm whitespace-nowrap">
            {closing ? t('tboard.finish_task_busy') : t('tboard.finish_task')}
          </button>
        </div>
      </div>

      {/* 当前步骤高亮块 */}
      <div className={`mt-4 rounded-xl bg-panel border border-line px-4 py-3
                      ${needsReply ? 'border-l-4 border-l-warn' : ''}`}>
        <div className="text-[11px] text-dim font-medium mb-1.5">{t('tboard.current_step_block_label')}</div>
        {!active.current_card_id ? (
          <div className="text-[12.5px] text-dim">{t('tboard.current_step_pending')}</div>
        ) : (
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-2 min-w-0">
            <span className="text-[13px] font-medium basis-full sm:flex-1 sm:basis-auto sm:min-w-0 truncate"
                  title={curTitle}>
              {curTitle}
            </span>
            <span className="badge-blue px-1.5 py-0.5 rounded text-[10.5px] shrink-0"
                  title={assignee}>
              {assignee}
            </span>
            {needsReply ? (
              <StatusBadge cls="badge-yellow" title={t('tboard.state_attention')}>{t('tboard.state_attention')}</StatusBadge>
            ) : isHandoff ? (
              <StatusBadge cls="badge-purple" title={t('tboard.state_handoff')}>{t('tboard.state_handoff')}</StatusBadge>
            ) : isWaiting ? (
              <StatusBadge cls="badge-amber" title={t('tboard.state_waiting')}>{t('tboard.state_waiting')}</StatusBadge>
            ) : displayState === 'BLOCKED' || displayState === 'ERROR' ? (
              <StatusBadge cls="badge-red" title={t('tboard.state_blocked')}>{t('tboard.state_blocked')}</StatusBadge>
            ) : (cur && cur.state === 'WORKING') || active.current_card_status === 'running' ? (
              <StatusBadge cls="badge-blue" pulse title={t('tboard.state_running')}>{t('tboard.state_running')}</StatusBadge>
            ) : (
              // 复用 classify 语义层兜底, 任何未识别状态码都渲染翻译文本, 不泄漏英文 (AC-07)
              <StatusBadge cls="badge-slate" title={classify(cur || { status: active.current_card_status }, t).sem}>
                {classify(cur || { status: active.current_card_status }, t).sem}
              </StatusBadge>
            )}
            {(isHandoff || displayState === 'HANDOFF') && (
              <span className="basis-full text-[12px] text-warn">
                {t('tboard.handoff_hint')}
              </span>
            )}
            {needsReply && (
              <span className="basis-full text-[12px] text-warn">
                {t('tboard.needs_reply_hint')}
              </span>
            )}
            {needsReply && (
              canReply
                ? <button className="btn-primary btn-sm" onClick={() => onReplyCard(cur)}>
                    {t('common.reply')}
                  </button>
                : <span className="text-[11px] text-dim">{t('tboard.current_step_unreplyable')}</span>
            )}
          </div>
        )}
      </div>

      {/* 全链路步骤预览区 (UIUX-KANBAN §2): 复用 activeDetail + StepTimeline, 零新增请求 */}
      <div className="mt-3">
        <button type="button" onClick={() => setChainOpen((v) => !v)}
          className="flex items-center gap-1.5 text-[12.5px] font-semibold text-dim
                     select-none hover:text-ink transition-colors min-h-[28px] tabular-nums">
          <Chevron open={chainOpen} />
          {t('tboard.chain_link_steps', { done, total })}
        </button>
        {chainOpen && (
          <div className="mt-2 min-w-0">
            {!detail && !detailErr ? (
              /* 首拉未回: 骨架屏, 不白屏 (§2.2) */
              <div className="space-y-2">
                {[...Array(3)].map((_, i) => <div key={i} className="skeleton h-10" />)}
              </div>
            ) : detailErr ? (
              /* 详情失败: 红字 + 重试, 不影响卡片其他区 (§2.2) */
              <div className="flex items-center gap-3 text-bad text-[12.5px]">
                <span>{t('tboard.loading_error_detail', { err: detailErr })}</span>
                <button className="btn-ghost btn-sm" onClick={onRetryDetail}>{t('tboard.retry')}</button>
              </div>
            ) : (
              <StepTimeline detail={detail} currentId={active.current_card_id}
                busy={busy} onReply={onReplyCard} onCancel={onCancel} onArchive={onArchive} t={t} />
            )}
          </div>
        )}
      </div>

      {/* 技术详情折叠区 (原始 ID 只在这里可见) */}
      <details className="mt-3 text-[11.5px] text-dim">
        <summary className="cursor-pointer select-none">{t('tboard.tech_details')}</summary>
        <div className="mt-2 space-y-1.5 pl-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span>Run ID:</span>
            <code className="font-mono bg-panel2 px-1.5 py-0.5 rounded break-all"
                  title={active.run_id}>{active.run_id}</code>
            <button className="btn-quiet btn-sm" onClick={() => onCopy(active.run_id)}>{t('common.copy')}</button>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <span>Workspace:</span>
            <code className="font-mono bg-panel2 px-1.5 py-0.5 rounded break-all"
                  title={active.project_workspace || t('tboard.empty_workspace')}>
              {active.project_workspace || t('tboard.empty_workspace')}
            </code>
          </div>
          <div><span>Created:</span> <span className="text-ink">{ts(active.created_at)}</span></div>
          <div className="flex items-center gap-2 flex-wrap">
            <span>Initiator session:</span>
            <code className="font-mono bg-panel2 px-1.5 py-0.5 rounded break-all"
                  title={active.initiator_session || ''}>
              {trunc(active.initiator_session, 24)}
            </code>
          </div>
          {detailErr && (
            <div className="flex items-center gap-2 text-bad">
              <span>{t('tboard.loading_error_detail', { err: detailErr })}</span>
              <button className="btn-ghost btn-sm" onClick={onRetryDetail}>{t('tboard.retry')}</button>
            </div>
          )}
        </div>
      </details>
    </div>
  )
}

// ════════════════════════════════════════════════════════════════════
// 任务计划 只读视图 (2026-10-11 新增; UI 看板『查看任务计划』入口)
// 数据来自 /api/team-work/<run_id>/todo, 完全只读, 不入 fs。
// 主次分层:
//   1) 当前任务  (主视图, 全宽, 完整字段)
//   2) 待执行    (次视图, 步骤流, 卡片网格)
//   3) 已完成    (弱化, 单行条目)
//   4) 已放弃    (最弱, 红字条目)
//   + 原始 markdown 折叠 (供运营/审计)
// ════════════════════════════════════════════════════════════════════
function FieldRow({ label, children, mono = false, accent = false }) {
  return (
    <div className={`grid grid-cols-[64px_minmax(0,1fr)] gap-x-3 items-start py-1.5
                    border-b border-line/40 last:border-b-0`}>
      <div className="text-[11px] text-dim uppercase tracking-wide pt-0.5">{label}</div>
      <div className={`min-w-0 ${mono ? 'font-mono' : ''}
                      ${accent ? 'text-accent font-medium' : 'text-ink'} text-[12.5px]
                      break-words`}>{children}</div>
    </div>
  )
}

// ════════════════════════════════════════════════════════════════════
// [en] GoalCell — 目标单元格 (issue #14, 2026-10-17).
//       Renders splitGoalSegments(goal): preview + default-collapsed
//       details drawer for the rest. The button label always shows the
//       hidden-line count when truncated. White-space: pre-line keeps
//       line breaks the user authored (TODO.md source-faithful).
// [zh-cn] 目标单元格 — 目标格式化修复 + 默认折叠抽屉。前后空行/换行被
//       splitGoalSegments 规整掉, 详情用 <details> 默认收起。
// ════════════════════════════════════════════════════════════════════
function GoalCell({ goal, t }) {
  const seg = splitGoalSegments(goal)
  if (!seg.full) {
    return <span className="text-dim italic">{t('tboard.todo_viewer_goal_placeholder')}</span>
  }
  // issue #43 fix: hiddenLineCount 为 0 时不显示徽章, 直接渲染完整文本
  if (!seg.truncated || seg.hiddenLineCount <= 0) {
    // 无需折叠或无隐藏行: 直接渲染
    return <span className="whitespace-pre-line break-words">{seg.full}</span>
  }
  return (
    <div className="space-y-1.5">
      <span className="whitespace-pre-line break-words">{seg.preview}</span>
      <details className="text-[11.5px] text-dim">
        <summary className="cursor-pointer select-none hover:text-ink transition-colors">
          {t('tboard.todo_viewer_goal_toggle_show', { n: seg.hiddenLineCount })}
        </summary>
        <div className="mt-1.5 max-h-48 overflow-y-auto rounded-md bg-canvas/60
                        border border-line/50 px-2.5 py-1.5
                        whitespace-pre-line break-words text-ink">
          {seg.full}
        </div>
      </details>
    </div>
  )
}

// ════════════════════════════════════════════════════════════════════
// [en] ResultChips — 把已完成区 result 字符串切段渲染 (issue #14).
//       `assignee` → 蓝色承接方 chip (复用 AssigneeBadge 同款蓝底),
//       `card`     → 紫色卡号 chip (单色 + monospace),
//       `commits`  → 琥珀色 commit chip (短 hash + 单色),
//       `note`     → slate 文本节点。长 note 跨过多行 → 默认折叠 drawer。
// [zh-cn] ResultChips — 完成结果字符串 chip 化; 长 note 降级到折叠抽屉。
// ════════════════════════════════════════════════════════════════════
const NOTE_DRAWER_CHARS = 80  // note 文本超过此长度走默认折叠抽屉
function ResultChips({ text, t }) {
  const segs = parseDoneResult(text)
  if (!segs.length) {
    return <span className="text-dim italic">{t('tboard.todo_viewer_result_placeholder')}</span>
  }
  return (
    <div className="flex flex-wrap items-center gap-1.5 min-w-0">
      {segs.map((s, i) => (
        <ResultSegment key={`${s.kind}-${i}`} seg={s} t={t} />
      ))}
    </div>
  )
}
function ResultSegment({ seg, t }) {
  const { kind, value } = seg
  if (!value) return null
  if (kind === 'assignee') {
    return (
      <span className="badge-blue px-1.5 py-0.5 rounded text-[10.5px] shrink-0"
            title={t('tboard.todo_viewer_result_chip_assignee') + ': ' + value}>
        <span className="text-dim mr-1">{t('tboard.todo_viewer_result_chip_assignee')}</span>
        <span className="font-medium">{value}</span>
      </span>
    )
  }
  if (kind === 'card') {
    return (
      <span className="badge-purple px-1.5 py-0.5 rounded text-[10.5px] font-mono shrink-0"
            title={t('tboard.todo_viewer_result_chip_card') + ': ' + value}>
        <span className="text-dim mr-1">{t('tboard.todo_viewer_result_chip_card')}</span>
        <span>{value}</span>
      </span>
    )
  }
  if (kind === 'commits') {
    // 短 hash 串 (≤40) 正常显示; 否则第一个 sha + "+N more" 形式截断
    const display = value.length > 40 ? value.slice(0, 14) + '…' : value
    return (
      <span className="badge-amber px-1.5 py-0.5 rounded text-[10.5px] font-mono shrink-0"
            title={t('tboard.todo_viewer_result_chip_commits') + ': ' + value}>
        <span className="text-dim mr-1">{t('tboard.todo_viewer_result_chip_commits')}</span>
        <span>{display}</span>
      </span>
    )
  }
  // note: 短文本直接行内, 长文本走默认折叠抽屉
  if (value.length <= NOTE_DRAWER_CHARS) {
    return (
      <span className="badge-slate px-1.5 py-0.5 rounded text-[10.5px] shrink-0"
            title={t('tboard.todo_viewer_result_chip_other') + ': ' + value}>
        <span className="text-dim mr-1">{t('tboard.todo_viewer_result_chip_other')}</span>
        <span className="break-words">{value}</span>
      </span>
    )
  }
  const preview = value.slice(0, NOTE_DRAWER_CHARS).trimEnd() + '…'
  return (
    <span className="inline-flex items-baseline gap-1 min-w-0">
      <span className="badge-slate px-1.5 py-0.5 rounded text-[10.5px] shrink-0"
            title={t('tboard.todo_viewer_result_chip_other')}>
        {t('tboard.todo_viewer_result_chip_other')}
      </span>
      <details className="text-[11px] text-dim min-w-0">
        <summary className="cursor-pointer select-none hover:text-ink transition-colors
                            inline-block max-w-full">
          <span className="break-words">{preview}</span>
        </summary>
        <div className="mt-1 max-h-32 overflow-y-auto rounded-md bg-canvas/60
                        border border-line/50 px-2 py-1
                        whitespace-pre-wrap break-words text-ink">
          {value}
        </div>
      </details>
    </span>
  )
}

// 『当前任务』主块 — 头部标题 + 状态徽章 + 元数据栅格。
function CurrentBlock({ cur, t }) {
  if (!cur) {
    return (
      <div className="rounded-xl border border-line bg-canvas/60 px-4 py-6 text-center text-dim text-[12.5px]">
        {/* "no current task (chain ended or TODO.md not seeded)" */}
        ——
      </div>
    )
  }
  const [, sem, badgeCls] = cur.status_sem || ['pending', cur.status || '?', 'badge-slate']
  return (
    <div className="rounded-xl border-2 border-accent/30 bg-accent/5 overflow-hidden">
      {/* 标题 + 状态 (对齐规范 §3.2: 主行信息密) */}
      <div className="px-4 py-3 border-b border-accent/20 bg-accent/[0.08] flex items-start gap-3 flex-wrap">
        <div className="min-w-0 flex-1">
          <div className="text-[11px] text-dim font-medium mb-0.5">{t('tboard.todo_viewer_section_current_label')}</div>
          <div className="text-[15px] font-bold leading-tight break-words">{cur.title || t('tboard.todo_viewer_no_title')}</div>
        </div>
        <span className={`${badgeCls} px-2 py-0.5 rounded text-[11px] font-medium shrink-0`}>{sem}</span>
      </div>
      {/* 元数据栅格 (单列, 信息密度高) */}
      <div className="px-4 py-2">
        <FieldRow label={t('tboard.todo_viewer_field_step_id')} mono>{cur.step_id || '—'}</FieldRow>
        <FieldRow label={t('tboard.todo_viewer_field_assignee')} accent={!!cur.assignee}>
          {cur.assignee || t('tboard.todo_viewer_pending_unassigned')}
        </FieldRow>
        {/* (issue #14, 2026-10-17) 目标用 GoalCell, 修"目标看不到":
             长目标/多空行 → 默认折叠抽屉, 短目标直接展示。 */}
        <FieldRow label={t('tboard.todo_viewer_field_goal')}>
          <GoalCell goal={cur.goal} t={t} />
        </FieldRow>
        <FieldRow label={t('tboard.todo_viewer_field_result')}>
          {cur.has_result
            ? <span className="text-ok"><ResultChips text={cur.result} t={t} /></span>
            : <span className="text-dim italic">{cur.result || t('tboard.todo_viewer_result_placeholder')}</span>}
        </FieldRow>
        {cur.product_workspace && (
          <FieldRow label={t('tboard.todo_viewer_field_workspace')} mono>{cur.product_workspace}</FieldRow>
        )}
        {cur.required_files?.length > 0 && (
          <FieldRow label={t('tboard.todo_viewer_field_files')}>
            <span className="font-mono text-[11.5px]">{cur.required_files.join(', ')}</span>
          </FieldRow>
        )}
        {cur.kanban_task_id && (
          <FieldRow label={t('tboard.todo_viewer_field_related')} mono>{cur.kanban_task_id}</FieldRow>
        )}
      </div>
    </div>
  )
}

// 『待执行』次块 — 步骤流, 后续步骤顺序明确但不强高亮。
function PendingList({ items, t }) {
  if (!items?.length) {
    return (
      <div className="rounded-xl border border-line bg-canvas/40 px-4 py-4
                      text-center text-dim text-[12px]">
        {t('tboard.todo_viewer_pending_empty')}
      </div>
    )
  }
  return (
    <ol className="space-y-2">
      {items.map((s, i) => (
        <li key={`${s.step_id || i}-${i}`}
            className="rounded-lg border border-line bg-panel2/40 px-3 py-2.5
                       flex items-start gap-3">
          <span className="shrink-0 w-6 h-6 rounded-full bg-panel border border-line
                           flex items-center justify-center text-[11px] font-mono font-medium text-dim">
            {i + 1}
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              {s.step_id && <code className="text-[10.5px] font-mono bg-canvas px-1.5 py-0.5 rounded
                                            border border-line text-dim"
                                  title={s.step_id}>{s.step_id}</code>}
              <span className="text-[13px] font-medium break-words"
                    title={s.title || t('tboard.todo_viewer_no_title')}>
                {s.title || t('tboard.todo_viewer_no_title')}
              </span>
            </div>
            {s.goal && (
              <div className="mt-1 text-[11.5px] text-dim leading-relaxed break-words">{s.goal}</div>
            )}
            {s.assignee && (
              <div className="mt-1.5">
                <span className="badge-blue px-1.5 py-0.5 rounded text-[10.5px]">→ {s.assignee}</span>
              </div>
            )}
          </div>
        </li>
      ))}
    </ol>
  )
}

// 『已完成』/『已放弃』卡片化 (s5 fix #10, 2026-10-13):
// items 是 server 序列化回来的结构化 dict, 不是裸 markdown 文本。
// 结构: { step_id, title, result, raw };解析失败的行退回 raw 文本展示。
// 视觉对齐 PendingList (边框/圆角/背景/间距/序号徽章), 但弱化色保留。
function HistoryList({ items, kind, t }) {
  if (!items?.length) {
    return (
      <div className="text-[11.5px] text-dim italic px-1">{t('tboard.todo_viewer_done_empty')}</div>
    )
  }
  const isDropped = kind === 'dropped'
  return (
    <ol className="space-y-2">
      {items.map((it, i) => {
        // 防御: 兼容老版本 / 直接传 markdown 字符串的形态, 不让任何行渲染崩溃
        const obj = (it && typeof it === 'object') ? it : { raw: String(it || '') }
        const title = obj.title || obj.raw || t('tboard.todo_viewer_no_title')
        const result = obj.result || ''
        const stepId = obj.step_id || ''
        const hasStructured = !!(obj.step_id || obj.title)
        return (
          <li key={`${stepId || i}-${i}`}
              className={`rounded-lg border px-3 py-2 flex items-start gap-3 break-words
                          ${isDropped
                            ? 'border-bad/30 bg-bad/5 opacity-75 line-through'
                            : 'border-line bg-panel2/40'}`}>
            {/* 序号位换 ✓ 徽章 (dropped 用 ✕ 红底) 保持视觉对齐 PendingList */}
            <span className={`shrink-0 w-6 h-6 rounded-full border flex items-center justify-center
                              text-[11px] font-bold
                              ${isDropped
                                ? 'border-bad text-bad'
                                : hasStructured
                                  ? 'border-ok text-ok'
                                  : 'border-line text-dim'}`}>
              {isDropped ? '✕' : hasStructured ? '✓' : '·'}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 flex-wrap">
                {stepId && (
                  <code className="text-[10.5px] font-mono bg-canvas px-1.5 py-0.5 rounded
                                    border border-line text-dim"
                        title={stepId}>{stepId}</code>
                )}
                <span className="text-[13px] font-medium break-words"
                      title={title}>{title}</span>
              </div>
              {result && (
                /* (issue #14, 2026-10-17) 完成区 result chip 化:
                   "coder | 卡 t_… | commits abc+def" 类串切成多 chip。 */
                <div className="mt-1 leading-relaxed" title={result}>
                  <ResultChips text={result} t={t} />
                </div>
              )}
              {/* 解析失败的兜底: 显示原文单行 (避免丢信息) */}
              {!hasStructured && obj.raw && (
                <div className="mt-1 text-[11.5px] text-dim font-mono break-words">
                  {obj.raw}
                </div>
              )}
            </div>
          </li>
        )
      })}
    </ol>
  )
}

// 总入口: 完整只读视图
function TodoViewerModal({ open, onClose, run, payload, loading, error, onCopyRaw,
                          onRefresh, t }) {
  const todo = payload?.todo
  const exists = payload?.exists
  const title = run?.initial_task || payload?.run_id || t('tboard.todo_viewer_title')
  const titleSeg = splitRunTitle(title)   // issue #79: 长标题预览段 (全值见 details 抽屉)
  const headerMeta = [
    payload?.project_workspace ? t('tboard.todo_viewer_meta_workspace', { path: payload.project_workspace }) : null,
    t('tboard.todo_viewer_meta_run', { rid: (run?.run_id || payload?.run_id || '—').slice(0, 12) }),
  ].filter(Boolean).join('  ·  ')
  return (
    <Modal open={open} onClose={onClose} title={t('tboard.todo_viewer_title')} width="max-w-2xl"
      footer={
        <>
          <button className="btn-quiet" onClick={onRefresh} disabled={loading}>
            {loading ? t('tboard.todo_viewer_refresh_loading') : t('tboard.todo_viewer_refresh')}
          </button>
          {todo?.raw && (
            <button className="btn-ghost" onClick={onCopyRaw}>
              {t('tboard.todo_viewer_copy_raw')}
            </button>
          )}
          <button className="btn-primary" onClick={onClose}>{t('common.close')}</button>
        </>
      }>
      <div className="px-5 py-4 space-y-4">
        {/* 头部标题 + 元数据 */}
        <div>
          <div className="text-[11px] text-dim font-medium mb-1">{t('tboard.todo_viewer_section_current_label')}</div>
          {/* issue #79: run 标题 = initial_task 全量原文, 单行渲染会占满弹窗首屏。
              走 issue #14 GoalCell 同款「预览 + 默认折叠抽屉」, 全值在 details 内可达。 */}
          <div className="space-y-1.5">
            <div className="text-[16px] font-bold leading-snug whitespace-pre-line break-words"
                 title={title}>{titleSeg.preview || title}</div>
            {titleSeg.truncated && titleSeg.hiddenLineCount > 0 && (
              <details className="text-[11.5px] text-dim">
                <summary className="cursor-pointer select-none hover:text-ink transition-colors">
                  {t('tboard.todo_viewer_title_toggle_show', { n: titleSeg.hiddenLineCount })}
                </summary>
                <div className="mt-1.5 max-h-48 overflow-y-auto rounded-md bg-canvas/60
                                border border-line/50 px-2.5 py-1.5
                                whitespace-pre-line break-words text-ink">
                  {titleSeg.full}
                </div>
              </details>
            )}
          </div>
          {todo?.title && todo.title !== title && (
            <div className="mt-1 text-[11.5px] text-dim">
              {t('tboard.todo_viewer_title_internal')}: <span className="text-ink">{todo.title}</span>
            </div>
          )}
          {headerMeta && (
            <div className="mt-1.5 text-[10.5px] text-dim font-mono break-all">{headerMeta}</div>
          )}
        </div>

        {/* 加载/错误/空态分情况 */}
        {loading && !payload && (
          <div className="space-y-2">
            <div className="skeleton h-24" />
            <div className="skeleton h-12" />
          </div>
        )}
        {error && (
          <div className="rounded-xl border border-bad/40 bg-bad/5 px-4 py-3 text-bad text-[12.5px]">
            {error}
          </div>
        )}
        {!loading && payload && !error && exists === false && (
          <div className="rounded-xl border border-line bg-canvas/60 px-4 py-6 text-center
                          space-y-1.5">
            <div className="text-[14px] font-semibold">{t('tboard.todo_viewer_no_todo_title')}</div>
            <p className="text-[12px] text-dim leading-relaxed">
              {payload.reason || t('tboard.todo_viewer_no_todo_subtitle')}
            </p>
            <p className="text-[11px] text-dim">{t('tboard.todo_viewer_no_todo_hint')}</p>
          </div>
        )}

        {todo && (
          <>
            {/* 当前任务 — 主要信息 */}
            <section>
              <SectionTitle index={1} accent="主要">{t('tboard.todo_viewer_section_current')}</SectionTitle>
              <CurrentBlock cur={todo.current} t={t} />
            </section>

            {/* 待执行 — 次要流 */}
            <section>
              <SectionTitle index={2}>{t('tboard.todo_viewer_section_pending', { n: todo.pending?.length || 0 })}</SectionTitle>
              <PendingList items={todo.pending || []} t={t} />
            </section>

            {/* 已完成 — 卡片化 (s5 fix #10) */}
            <section>
              <SectionTitle index={3} muted>{t('tboard.todo_viewer_section_done', { n: todo.done_lines?.length || 0 })}</SectionTitle>
              <HistoryList items={todo.done_lines} kind="done" t={t} />
            </section>

            {/* 已放弃 — 最弱 (s5 fix #10) */}
            {todo.dropped_lines?.length > 0 && (
              <section>
                <SectionTitle index={4} muted danger>{t('tboard.todo_viewer_section_dropped', { n: todo.dropped_lines.length })}</SectionTitle>
                <HistoryList items={todo.dropped_lines} kind="dropped" t={t} />
              </section>
            )}

            {/* 原始 markdown — 默认收起, 给运营/审计用 */}
            {todo.raw && (
              <details className="text-[11.5px] text-dim">
                <summary className="cursor-pointer select-none hover:text-ink transition-colors">
                  {t('tboard.todo_viewer_raw_label')}
                </summary>
                <pre className="mt-2 p-3 bg-canvas border border-line rounded-lg
                                text-[11.5px] font-mono whitespace-pre-wrap break-words
                                max-h-60 overflow-y-auto">{todo.raw}</pre>
              </details>
            )}
          </>
        )}
      </div>
    </Modal>
  )
}

// 分节标题 (主次分明, 用 index 数字 + 文字标 + 颜色表达权重)
function SectionTitle({ index, children, accent, muted, danger }) {
  let cls = 'text-[12.5px] font-semibold flex items-center gap-2 mb-2 '
  if (accent === '主要') cls += 'text-accent'
  else if (danger) cls += 'text-bad'
  else if (muted) cls += 'text-dim'
  else cls += 'text-ink'
  return (
    <div className={cls}>
      <span className={`inline-flex w-5 h-5 rounded-full items-center justify-center
                         text-[10.5px] font-mono font-bold
                         ${accent === '主要' ? 'bg-accent/15 text-accent'
                           : danger ? 'bg-bad/15 text-bad'
                           : muted ? 'bg-panel2 text-dim'
                           : 'bg-panel2 text-ink'}`}>{index}</span>
      {children}
    </div>
  )
}

// ── 主视图 ──────────────────────────────────────────────────────────
export default function TasksBoardView({ onChanged }) {
  const { t } = useTranslation()
  const [active, setActive] = useState(null)          // 顶层进行中任务 (/api/tasks)
  const [runs, setRuns] = useState(null)             // 全部任务列表 (null=未加载)
  const [expanded, setExpanded] = useState({})       // {run_id: bool}
  const [runDetail, setRunDetail] = useState({})     // {run_id: {team_work, cards}}
  const [loadingRun, setLoadingRun] = useState({})
  const [runErr, setRunErr] = useState({})
  const [activeDetail, setActiveDetail] = useState(null)   // 当前任务详情(算进度/取问题)
  const [activeDetailErr, setActiveDetailErr] = useState('')
  const [tasksErr, setTasksErr] = useState('')
  const [runsErr, setRunsErr] = useState('')
  const [showArchived, setShowArchived] = useState(false)
  const [closing, setClosing] = useState(false)
  const [archivingRun, setArchivingRun] = useState('')
  const [busy, setBusy] = useState({})               // {task_id: 'cancel'|'archive'}
  const [dlg, setDlg] = useState(null)               // 回复弹窗 {task, answer, picked, error}
  const [toast, setToast] = useState(null)           // {msg, type}
  const [confirm, setConfirm] = useState(null)        // {title, text, okText, action}
  // 2026-10-11: 任务计划只读弹窗。{open, run, payload, loading, error}
  // run 必带 project_workspace 字段 (后端权威源)。
  const [todoViewer, setTodoViewer] = useState(null)

  // ── 数据加载 (5s 轮询不变; 当前任务详情搭 active 轮询顺带刷新, 不新增定时器) ──
  // issue #75 s6 缺陷 2 (P1): loadActive 只消费顶层 d.team_work_active,
  // 却每次拖走整个 /api/tasks 列表 (实测 122KB/次 ≈ 1.4MB/min)。改为走
  // 轻量端点 GET /api/tasks?fields=active (只回 team_work_active 段, ~1KB)。
  // 首次挂载同样走轻量端点 —— 本视图压根不消费 d.tasks 列表。
  const loadActive = useCallback(async () => {
    try {
      const d = await api.tasksActive()
      const a = d.team_work_active || null
      setActive(a)
      setTasksErr('')
      if (a?.run_id) {
        try {
          setActiveDetail(await api.teamWorkGet(a.run_id))
          setActiveDetailErr('')
        } catch (e) { setActiveDetailErr(fmtApiError(e)) }  // issue #84 B-4: 三态归因
      } else {
        setActiveDetail(null)
      }
    } catch (e) { setTasksErr(fmtApiError(e)) }  // issue #75 §3: 三态归因
  }, [])

  const loadRuns = useCallback(async () => {
    try {
      const d = await api.teamWorks(true, showArchived)
      setRuns(d.team_works || d.runs || [])
      setRunsErr('')
    } catch (e) { setRunsErr(fmtApiError(e)) }
  }, [showArchived])

  const loadRunDetail = useCallback(async (runId, { silent = false } = {}) => {
    if (!silent) setLoadingRun((s) => ({ ...s, [runId]: true }))
    try {
      const d = await api.teamWorkGet(runId)
      setRunDetail((s) => ({ ...s, [runId]: d }))
      setRunErr((s) => { const n = { ...s }; delete n[runId]; return n })
    } catch (e) {
      setRunErr((s) => ({ ...s, [runId]: fmtApiError(e) }))  // issue #84 B-4
    } finally {
      if (!silent) setLoadingRun((s) => { const n = { ...s }; delete n[runId]; return n })
    }
  }, [])

  // 操作后的统一刷新: 顶层两路 + 已展开任务详情
  const refresh = () => {
    loadActive(); loadRuns()
    Object.keys(expanded).forEach((rid) => {
      if (expanded[rid]) loadRunDetail(rid, { silent: true })
    })
  }

  // ── 任务计划只读弹窗 (2026-10-11) ─────────────────────────────────
  // 入口: TaskCard / CurrentTaskCard 的 "查看任务计划" 按钮。
  // 数据: /api/team-work/<run_id>/todo, 后端 fail-soft。
  // 不抢 5s 轮询: 用户主动点才拉, 弹窗开期间不轮询, 关掉就丢缓存。
  const openTodo = (run) => {
    if (!run?.run_id) return
    setTodoViewer({ open: true, run, payload: null, loading: true, error: '' })
    api.teamWorkTodo(run.run_id)
      .then((p) => setTodoViewer((s) => (
        s?.run?.run_id === run.run_id
          ? { ...s, payload: p, loading: false,
              error: p?.ok === false ? (p.error || t('common.load_failed_short')) : '' }
          : s)))
      .catch((e) => setTodoViewer((s) => (
        s?.run?.run_id === run.run_id
          ? { ...s, loading: false, error: t('common.load_failed', { err: e.message || e }) }
          : s)))
  }
  const refreshTodo = () => {
    const rid = todoViewer?.run?.run_id
    if (!rid) return
    setTodoViewer((s) => ({ ...s, loading: true, error: '' }))
    api.teamWorkTodo(rid)
      .then((p) => setTodoViewer((s) => ({ ...s, payload: p, loading: false,
        error: p?.ok === false ? (p.error || t('common.load_failed_short')) : '' })))
      .catch((e) => setTodoViewer((s) => ({ ...s, loading: false,
        error: t('common.load_failed', { err: e.message || e }) })))
  }
  const copyTodoRaw = async () => {
    const raw = todoViewer?.payload?.todo?.raw || ''
    if (!raw) return
    const ok = await copyText(raw)
    setToast(ok ? { msg: t('common.copied'), type: 'ok' }
                : { msg: t('common.copy_failed'), type: 'bad' })
  }
  const closeTodo = () => setTodoViewer(null)

  useEffect(() => {
    loadActive()
    loadRuns()
    const t1 = setInterval(loadActive, 5000)
    const t2 = setInterval(loadRuns, 5000)
    return () => { clearInterval(t1); clearInterval(t2) }
  }, [loadActive, loadRuns])

  // runs 刷新后, 已展开的任务静默重拉详情, 避免步骤状态过期
  useEffect(() => {
    Object.keys(expanded).forEach((rid) => {
      if (expanded[rid]) loadRunDetail(rid, { silent: true })
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runs])

  // ── 操作: 结束任务 (大白话文案, DESIGN §3.6) ──────────────────────
  const askEndRun = (run) => setConfirm({
    title: t('tboard.confirm_finish_title'),
    text: t('tboard.confirm_finish_text'),
    okText: t('tboard.confirm_finish_ok'),
    action: async () => {
      setClosing(true)
      try {
        await api.teamWorkClose(run.run_id, { operator: 'console-ui' })
        setToast({ msg: t('tboard.finish_success'), type: 'ok' })
        await Promise.all([loadActive(), loadRuns()])
        onChanged?.()
      } catch (e) {
        setToast({ msg: t('tboard.finish_failed', { err: fmtApiError(e) }), type: 'bad' })
      } finally { setClosing(false) }
    },
  })

  // ── 操作: 归档任务 ────────────────────────────────────────────────
  const askArchiveRun = (run) => setConfirm({
    title: t('tboard.confirm_archive_title'),
    text: t('tboard.confirm_archive_text'),
    okText: t('tboard.confirm_archive_ok'),
    action: async () => {
      setArchivingRun(run.run_id)
      try {
        await api.teamWorkArchive(run.run_id, { operator: 'console-ui' })
        setToast({ msg: t('tboard.archive_success'), type: 'ok' })
        await Promise.all([loadActive(), loadRuns()])
        onChanged?.()
      } catch (e) {
        setToast({ msg: t('tboard.archive_failed', { err: fmtApiError(e) }), type: 'bad' })
      } finally { setArchivingRun('') }
    },
  })

  // ── 操作: 终止步骤 ────────────────────────────────────────────────
  const askCancel = (step) => setConfirm({
    title: t('tboard.confirm_cancel_title'),
    text: t('tboard.confirm_cancel_text'),
    okText: t('tboard.confirm_cancel_ok'),
    action: async () => {
      setBusy((b) => ({ ...b, [step.id]: 'cancel' }))
      try {
        await api.cancelTask(step.id)
        setToast({ msg: t('tboard.cancel_success'), type: 'ok' })
        onChanged?.()
        setTimeout(refresh, 500)
      } catch (e) {
        setToast({ msg: t('tboard.cancel_failed', { err: fmtApiError(e) }), type: 'bad' })
      } finally {
        setBusy((b) => { const n = { ...b }; delete n[step.id]; return n })
      }
    },
  })

  // ── 操作: 归档步骤 ────────────────────────────────────────────────
  const askArchive = (step) => setConfirm({
    title: t('tboard.confirm_archive_title'),
    text: t('tboard.confirm_archive_text'),
    okText: t('tboard.confirm_archive_ok'),
    action: async () => {
      setBusy((b) => ({ ...b, [step.id]: 'archive' }))
      try {
        await api.archiveTask(step.id)
        setToast({ msg: t('tboard.archive_success'), type: 'ok' })
        onChanged?.()
        setTimeout(refresh, 500)
      } catch (e) {
        setToast({ msg: t('tboard.archive_failed', { err: fmtApiError(e) }), type: 'bad' })
      } finally {
        setBusy((b) => { const n = { ...b }; delete n[step.id]; return n })
      }
    },
  })

  const runConfirm = async () => {
    const a = confirm?.action
    setConfirm(null)
    if (a) await a()
  }

  // ── 操作: 回复并继续 (仅调现有 resume, 行为不变) ──────────────────
  const openReply = (task) => setDlg({ task, answer: '', picked: null })
  const submitAnswer = async () => {
    if (!dlg?.answer?.trim() || dlg.sending) return
    setDlg((d) => ({ ...d, sending: true }))
    try {
      const r = await api.resume(dlg.task.id, dlg.answer.trim())
      setDlg(null)
      // issue #116: 后到答复不再一律成功文案 —— 后端 conflict 非空时
      // 用警示条呈现「已被 X 于 HH:MM 答复」, 消除操作员盲注体感。
      setToast(r?.conflict
        ? { msg: t('tboard.reply_conflict', { conflict: r.conflict }), type: 'warn' }
        : { msg: t('tboard.finish_success'), type: 'ok' })
      onChanged?.()
      await Promise.all([loadActive(), loadRuns()])
      Object.keys(expanded).forEach((rid) => {
        if (expanded[rid]) loadRunDetail(rid, { silent: true })
      })
    } catch (e) {
      setDlg((d) => ({ ...d, error: fmtApiError(e), sending: false }))  // issue #84 B-4
    }
  }

  const onCopy = async (text) => {
    const ok = await copyText(text)
    setToast(ok ? { msg: t('common.copied'), type: 'ok' } : { msg: t('common.copy_failed'), type: 'bad' })
  }

  const toggleExpand = (runId) => {
    setExpanded((s) => {
      const willExpand = !s[runId]
      if (willExpand && !runDetail[runId] && !loadingRun[runId]) loadRunDetail(runId)
      return { ...s, [runId]: willExpand }
    })
  }

  // 历史列表: ACTIVE 已由当前任务卡承载, 过滤避免双份
  const historyRuns = (runs || []).filter((r) => r.status !== 'ACTIVE')
  const anyErr = tasksErr || runsErr

  return (
    <div className="board-touch space-y-4">
      {/* 错误态: 顶部红横幅 + 重试 (DESIGN §3.7) */}
      {anyErr && (
        <div className="card p-4 border-bad/40 bg-bad/5 flex items-center justify-between gap-3 flex-wrap">
          <span className="text-bad text-[13px]">
            {tasksErr
              ? t('tboard.loading_error_tasks', { err: tasksErr })
              : t('tboard.loading_error_runs', { err: runsErr })}
          </span>
          <button className="btn-ghost btn-sm" onClick={refresh}>{t('tboard.retry')}</button>
        </div>
      )}

      {/* 首次加载: 骨架屏, 不得白屏 */}
      {!runs && !runsErr && (
        <div className="space-y-4">
          <div className="skeleton h-24" />
          <div className="skeleton h-24" />
          <div className="skeleton h-16" />
          <div className="skeleton h-16" />
          <div className="skeleton h-16" />
        </div>
      )}

      {/* 当前任务卡 */}
      {runs && active && (
        <CurrentTaskCard active={active} detail={activeDetail} detailErr={activeDetailErr}
          closing={closing}
          onEnd={() => askEndRun({ run_id: active.run_id })}
          onReplyCard={openReply} onCopy={onCopy}
          onRetryDetail={loadActive}
          busy={busy} onCancel={askCancel} onArchive={askArchive}
          onShowTodo={() => openTodo(active)} t={t} />
      )}

      {/* 历史任务列表 */}
      {runs && (
        <div className="space-y-3">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <h2 className="text-[13px] font-semibold text-dim tabular-nums">
              {t('tboard.history_title', { n: historyRuns.length })}
            </h2>
            <label className="flex items-center gap-1.5 text-[11.5px] text-dim cursor-pointer select-none">
              <input type="checkbox" className="accent-accent w-3.5 h-3.5"
                checked={showArchived}
                onChange={(e) => setShowArchived(e.target.checked)} />
              {t('tboard.show_archived')}
            </label>
          </div>
          {historyRuns.length === 0 ? (
            <div className="card p-8 text-center">
              <div className="text-[14px] font-semibold mb-1.5">{t('tboard.empty_title')}</div>
              <p className="text-[12.5px] text-dim leading-relaxed">
                {t('tboard.empty_subtitle')}
              </p>
            </div>
          ) : (
            historyRuns.map((r) => (
              <TaskCard key={r.run_id} run={r}
                detail={runDetail[r.run_id]}
                isOpen={!!expanded[r.run_id]}
                loading={!!loadingRun[r.run_id]}
                err={runErr[r.run_id]}
                busy={busy}
                onToggle={() => toggleExpand(r.run_id)}
                onRetryDetail={() => loadRunDetail(r.run_id)}
                onEndRun={() => askEndRun(r)}
                onArchiveRun={() => askArchiveRun(r)}
                onReply={openReply} onCancel={askCancel} onArchive={askArchive}
                onShowTodo={() => openTodo(r)} t={t} />
            ))
          )}
        </div>
      )}

      {/* 回复并继续 弹窗 (DESIGN §3.5) */}
      <Modal open={!!dlg} onClose={() => setDlg(null)}
        title={t('tboard.reply_modal_title')} width="max-w-xl"
        footer={
          <>
            <button className="btn-ghost" onClick={() => setDlg(null)}>{t('common.cancel')}</button>
            <button className="btn-primary" disabled={!dlg?.answer?.trim()} onClick={submitAnswer}>
              {dlg?.sending ? t('tboard.reply_submit_busy') : t('tboard.reply_submit')}
            </button>
          </>
        }>
        {dlg && (
          <div className="px-5 py-3 space-y-3">
            <p className="text-[12.5px] text-dim">{t('tboard.reply_modal_subtitle')}</p>
            <div>
              <div className="text-[14px] font-semibold">{dlg.task.title}</div>
              {dlg.task.assignee && (
                <div className="text-[11.5px] text-dim mt-1">
                  {t('tboard.reply_modal_actor')} <span className="badge-blue ml-1">{dlg.task.assignee}</span>
                </div>
              )}
            </div>
            <div className="rounded-xl border-l-4 border-warn bg-warn/5 px-4 py-3 text-[13px] leading-relaxed">
              {dlg.task.question || t('tboard.reply_modal_no_question')}
            </div>
            {dlg.task.options?.length > 0 && (
              <div>
                <div className="text-[11.5px] text-dim mb-2">{t('tboard.reply_modal_options_hint')}</div>
                <div className="flex flex-wrap gap-2">
                  {dlg.task.options.map((o, i) => (
                    <button key={i}
                      className={`rounded-lg border px-3 py-1.5 text-[12.5px] transition-all
                        ${dlg.picked === i
                          ? 'border-ok bg-ok/15 text-ok font-semibold'
                          : 'border-line bg-panel2 text-ink hover:border-accent hover:text-accent'}`}
                      onClick={() => {
                        setDlg((d) => ({ ...d, picked: i, answer: o }))
                        setTimeout(() => { document.getElementById('dlgAns')?.focus() }, 30)
                      }}>
                      {o}
                    </button>
                  ))}
                </div>
              </div>
            )}
            <textarea id="dlgAns" rows={4} className="input resize-y font-sans"
              placeholder={t('tboard.reply_modal_placeholder')}
              value={dlg.answer}
              onChange={(e) => setDlg((d) => ({ ...d, answer: e.target.value, picked: null }))}>
            </textarea>
            {dlg.error && <div className="text-bad text-xs">{dlg.error}</div>}
          </div>
        )}
      </Modal>

      {/* 页内确认组件 (替代 window.confirm, AC-13) */}
      <Confirm open={!!confirm} title={confirm?.title} okText={confirm?.okText}
        text={confirm?.text} danger onOk={runConfirm} onCancel={() => setConfirm(null)} />

      {/* 2026-10-11: 任务计划只读弹窗 (active + history 共用入口) */}
      <TodoViewerModal open={!!todoViewer}
        onClose={closeTodo}
        run={todoViewer?.run}
        payload={todoViewer?.payload}
        loading={!!todoViewer?.loading}
        error={todoViewer?.error}
        onCopyRaw={copyTodoRaw}
        onRefresh={refreshTodo} t={t} />

      {/* 全局轻提示 */}
      <Toast msg={toast?.msg} type={toast?.type} onDone={() => setToast(null)} />
    </div>
  )
}
