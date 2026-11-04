#!/usr/bin/env node
// 单测: issue #58 token 消耗量显示 — 前端纯函数 + i18n 键集合机检。
//
// main 裁决 2 (a2 结算时核出): ui/tests/ 原 4 套件无任何 i18n/locale 覆盖,
// i18n 键集合机检须由 a3 自建。本套件补两件事:
//   A. en.json 与 zh-cn.json 的 tboard 子 dict 键集合差集 = 空 (AC-U5 机检),
//      且 4 个新键 (token_short_label / token_cost_label / token_missing /
//      token_detail_title) 双边都在;
//   B. formatTokens / formatCost / tokenLabel / tokenTitle / runTokenLabel /
//      runTokenTitle 六个纯函数按「源码抽取直测」路线 (与
//      test_todo_viewer_chips.mjs 一致) 从 TasksBoardView.jsx 抽出跑用例
//      (API-SPEC-58 §4.5 数值格式 + §4.2/§4.3 缺失口径)。
// 接入 package.json "test" 脚本, npm test 即跑 (a5 复测同源)。

import { readFileSync } from 'fs'
import vm from 'node:vm'

const SRC = new URL('../src/views/TasksBoardView.jsx', import.meta.url)
const src = readFileSync(SRC, 'utf8')

const tests = []
function test(name, fn) { tests.push({ name, fn }) }
function assert(cond, msg) { if (!cond) throw new Error(msg) }
function eq(a, b, msg) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b)
  if (sa !== sb) throw new Error(`${msg || 'eq'}: ${sa} !== ${sb}`)
}

// ── 源码抽取 (花括号配平, 同 test_todo_viewer_chips 口径) ──────────
function extractFn(source, startMarker) {
  const start = source.indexOf(startMarker)
  if (start < 0) throw new Error(`源码抽取失败, 未找到标记: ${startMarker}`)
  let braces = 0, parens = 0, seenBrace = false
  for (let i = start; i < source.length; i++) {
    const ch = source[i]
    if (ch === '{') { braces++; seenBrace = true }
    else if (ch === '}') braces--
    else if (ch === '(') parens++
    else if (ch === ')') parens--
    if (seenBrace && braces === 0 && parens === 0) return source.slice(start, i + 1)
  }
  throw new Error(`括号不配平: ${startMarker}`)
}

// issue #58 helper 块 = formatTokens 起, 到 runTokenTitle 止 (连续区段)
const helpersBlock = (() => {
  const start = src.indexOf('function formatTokens')
  if (start < 0) throw new Error('源码抽取失败: 未找到 formatTokens')
  const endMarker = 'function runTokenTitle'
  const endIdx = src.indexOf(endMarker, start)
  if (endIdx < 0) throw new Error('源码抽取失败: 未找到 runTokenTitle')
  let braces = 0, parens = 0, seenBrace = false
  for (let i = endIdx; i < src.length; i++) {
    const ch = src[i]
    if (ch === '{') { braces++; seenBrace = true }
    else if (ch === '}') braces--
    else if (ch === '(') parens++
    else if (ch === ')') parens--
    if (seenBrace && braces === 0 && parens === 0) return src.slice(start, i + 1)
  }
  throw new Error('runTokenTitle 括号不配平')
})()

const ctx = vm.createContext({ console, Intl })
vm.runInContext(
  helpersBlock +
    '\nglobalThis.__exports = { formatTokens, formatCost, tokenLabel, tokenTitle,' +
    ' runTokenLabel, runTokenTitle };',
  ctx,
)
const { formatTokens, formatCost, tokenLabel, tokenTitle,
        runTokenLabel, runTokenTitle } = ctx.__exports

// ═════════════════ A. i18n 键集合机检 (main 裁决 2, AC-U5) ═════════════════
const enPath = new URL('../src/i18n/locales/en.json', import.meta.url)
const zhPath = new URL('../src/i18n/locales/zh-cn.json', import.meta.url)
const en = JSON.parse(readFileSync(enPath, 'utf8'))
const zh = JSON.parse(readFileSync(zhPath, 'utf8'))

// 极简 t() stub: 无插值调用按 en 表解析真实值 (如 token_missing → "—");
// 带插值调用返回 "key:{插值对象}", 便于断言模板键与占位值。
const mkT = () => (key, opts) => {
  if (opts) return `${key}|${JSON.stringify(opts)}`
  const v = key.split('.').reduce((o, k) => (o == null ? o : o[k]), en)
  return typeof v === 'string' ? v : key
}

test('i18n: 顶层全量 flatten 键集合 en == zh (基线 639 → 643 → 644)', () => {
  const flat = (d, p = '') => Object.entries(d).flatMap(([k, v]) =>
    (v && typeof v === 'object' && !Array.isArray(v))
      ? flat(v, p ? `${p}.${k}` : k) : [p ? `${p}.${k}` : k])
  const fe = new Set(flat(en)), fz = new Set(flat(zh))
  const onlyEn = [...fe].filter((k) => !fz.has(k))
  const onlyZh = [...fz].filter((k) => !fe.has(k))
  eq(onlyEn, [], `only-in-en: ${onlyEn}`)
  eq(onlyZh, [], `only-in-zh: ${onlyZh}`)
  // 643 → 644: issue #63 新增 chat.errors.stop_failed (双语同 commit, 同
  // issue#58 加 4 键时的先例口径 —— 键数锚随合法新增同步上调)。
  // 644 → 650: issue #75 新增 6 键 (app.reaper_unknown / chat.session.load_more
  // / chat.session.no_more_history / chat.errors.more_history_failed /
  // err.network / err.server), 双语同 commit (INV-09 先例口径)。
  // 650 → 650: issue #81-1 删除死键 chat.session.no_more_history, 同时
  // issue #81-2 新增 err.client —— 一增一减, 键数锚净数不变仍 650。
  // 650 → 651: issue #79 新增 tboard.todo_viewer_title_toggle_show
  // (弹窗长标题折叠开关, 双语同 commit, INV-09 先例口径)。
  // 651 → 653: issue #116 新增 tboard.reply_conflict (后到答复冲突警示)
  // + tboard.answered_badge (卡片「已被 X 于 HH:MM 答复」角标), 双语同 commit。
  // 653 → 652: issue #109 删除 workers.compose_port_label (创建 Worker 不再
  // 填宿主端口), 双语同 commit —— 键数锚随合法删除同步下调。
  // 652 → 686: cron 定时任务视图 (s4 / 卡 t_da1c3a64) 新增 34 键
  // (nav.cron + app.subtitle_cron + cron.* 子树 32 键), 双语同 commit
  // (INV-09 先例口径 —— 键数锚随合法新增同步上调)。
  // 686 → 688: issue #132 新增 tboard.state_waiting_sub +
  // workers.status_waiting_sub (waiting_sub 派生态显示「子任务处理中」,
  // 双语同 commit, INV-09 先例口径)。
  // 688 → 693: issue #146 新增 tboard.todo_status_* 五键 (TODO 状态 chip
  // 文案 i18n 化, 双语同 commit, INV-09 先例口径 —— 键数锚随合法新增同步上调)。
  // 693 → 699: issue #159 新增 6 键 (chat.errors.image_compress_failed /
  // image_too_large / image_unsupported / pillow_unavailable +
  // chat.input.image_meta / image_meta_compressed —— 图片附件服务端压缩的
  // 错误文案与元信息模板; 元信息按直通/压缩拆两模板故比方案表多 1 键),
  // 双语同 commit (INV-09 先例口径)。
  // 699 → 715: issue #158 新增 16 键 (chat.files.upload/upload_hint/uploading/
  // upload_ok/upload_failed/upload_conflict/upload_too_large + delete/
  // delete_confirm_title/delete_confirm_body/delete_cancel/delete_ok/
  // delete_failed/delete_needs_file + err_conflict/err_confirm_required ——
  // 文件浏览器上传/删除的文案与错误映射; 双语同 commit, INV-09 先例口径,
  // 键数锚按工作树实测值 699 上调 —— X-3 纪律, 未假设 693/709 基线)。
  // 715 → 719: issue #169 第1条新增 4 键 (chat.errors.image_missing_data /
  // image_bad_base64 / image_payload_too_large / image_compress_failed_generic
  // —— 图片压缩错误映射补齐三码专属文案 + 兜底不再回显裸码; 双语同 commit,
  // INV-09 先例口径, 键数锚按工作树实测值 719 上调 —— X-3 纪律)。
  // 719 → 768: issue #164 任务排期新增 49 键 (nav.schedule +
  // app.subtitle_schedule + chat.team_work_modal.queued_toast +
  // schedule.* 子树 46 键 (现 44 键 —— btn 7 / form 5 / toast 8;
  // eb874fd 删 3 死键后, 历史链保留不删史 —— issue #188) —— 分组 3 /
  // 状态 6 / 优先级 4 / 按钮 8 /
  // 开关与提示 2 / 倒计时 1 / 列名 2 / cron 徽标 1 / 空态 2 / 表单 6 /
  // 二次确认 2 / toast 9; 双语同 commit, INV-09 先例口径)。
  // 768 → 765: issue #164 追加 (排期唯一入口) 删除 3 死键 (schedule.btn.new /
  // schedule.form.new_title / schedule.toast.created —— 排期页「新建任务」
  // 入口移除后无任何调用点; 双语同 commit, INV-09 先例口径, 键数锚随合法
  // 删除同步下调)。
  // 765 → 766: issue #178 新增 tboard.delivery_failed_badge (列表常驻轮询面
  // 的「投递失败」徽标文案, 双语同 commit, INV-09 先例口径)。
  // 766 → 767: issue #197 新增 schedule.group.paused (收件箱「已暂停」独立
  // 分组标题 —— 暂停任务排除出待执行列表但保留可见入口, OQ-1 裁决);
  // 双语同 commit, INV-09 先例口径, 键数锚按工作树实测值上调
  // —— 子树口径同步: schedule.* 子树 43 → 44 键 (现 44 键; #197 实际把子树从 43 推到 44,
  //    「46 → 47」是一条从未发生的迁移 —— DOC-AUDIT-R D4 修正)。
  // 767 → 770: issue #208 新增 3 键 (schedule.meta_workspace /
  // meta_workspace_default / meta_workflow —— inbox 卡片 meta 行 workspace/
  // workflow 轻量标识标签与「默认」占位, 双语同 commit, INV-09 先例口径;
  // 子树口径同步: schedule.* 子树 44 → 47 键)。
  // 770 → 778: issue #211 新增 8 键 (chat.team_work_modal.attach_label /
  // attach_btn / attach_hint / attach_uploading / attach_upload_failed /
  // attach_too_large / attach_too_many / attach_remove —— 发起协作弹窗附件
  // 字段的标签/按钮/提示与上传中/失败/超限/超数文案, 双语同 commit,
  // INV-09 先例口径 —— 键数锚随合法新增同步上调)。
  // 778 → 783: issue #212 新增 5 键 (chat.decode_speed.approx / exact /
  // label / reasoning_suffix / tooltip —— 对话页 decode 速度显示的近似/精确
  // 文案、标签、reasoning 分桶后缀与口径 tooltip, 双语同 commit,
  // INV-09 先例口径 —— 键数锚随合法新增同步上调)。
  assert(fe.size === 783 && fz.size === 783,
    `期望 778+5=783 键, 实际 en=${fe.size} zh=${fz.size}`)
})

test('i18n: tboard 子 dict 键集合差集为空 (124+5=129, +1=130)', () => {
  const te = en.tboard, tz = zh.tboard
  const ke = new Set(Object.keys(te)), kz = new Set(Object.keys(tz))
  eq([...ke].filter((k) => !kz.has(k)), [], 'tboard only-in-en')
  eq([...kz].filter((k) => !ke.has(k)), [], 'tboard only-in-zh')
  // 120 → 121: issue #79 新增 todo_viewer_title_toggle_show (双语同 commit)。
  // 121 → 123: issue #116 新增 reply_conflict + answered_badge (双语同 commit)。
  // 123 → 124: issue #132 新增 state_waiting_sub (双语同 commit)。
  // 124 → 129: issue #146 新增 todo_status_working / todo_status_blocked /
  // todo_status_completed / todo_status_failed / todo_status_waiting_sub
  // (双语同 commit)。
  // 129 → 130: issue #178 新增 delivery_failed_badge (投递失败徽标文案,
  // 双语同 commit)。
  assert(ke.size === 130 && kz.size === 130,
    `期望 tboard 130 键, 实际 en=${ke.size} zh=${kz.size}`)
})

test('i18n: 4 个 token 新键双边存在且在 tboard.* 下', () => {
  for (const k of ['token_short_label', 'token_cost_label',
                   'token_missing', 'token_detail_title']) {
    assert(typeof en.tboard[k] === 'string' && en.tboard[k].length > 0,
      `en.tboard.${k} 缺失`)
    assert(typeof zh.tboard[k] === 'string' && zh.tboard[k].length > 0,
      `zh.tboard.${k} 缺失`)
  }
})

// issue #74: 顶层扁平重复键已删, 嵌套形态保留 (i18next 解析取嵌套值)。
// 键数锚不变 (唯一键 644), 本用例只钉形态, 不重复钉键数。
test('i18n: 顶层无扁平 chat.errors.file_read_failed, 嵌套形态保留 (issue #74)', () => {
  assert(!Object.prototype.hasOwnProperty.call(en, 'chat.errors.file_read_failed'),
    'en.json 顶层仍存在扁平重复键')
  assert(!Object.prototype.hasOwnProperty.call(zh, 'chat.errors.file_read_failed'),
    'zh-cn.json 顶层仍存在扁平重复键')
  eq(en.chat.errors.file_read_failed, 'Read file failed')
  eq(zh.chat.errors.file_read_failed, '读取文件失败')
})

test('i18n: token_detail_title 占位符走本表单花括号 {var} 约定', () => {
  // i18n/index.js init 配 interpolation prefix '{' suffix '}';
  // 双花括号 {{var}} 会被字面渲染 (E2 fix 注释), 新键必须同约定。
  const tpl = en.tboard.token_detail_title
  assert(!tpl.includes('{{'), '新键不得用 {{var}} 双花括号')
  for (const v of ['total', 'in', 'out', 'cr', 'cw', 'reason',
                   'cost', 'captured_at', 'source'])
    assert(tpl.includes(`{${v}}`), `模板缺占位符 {${v}}`)
  eq(zh.tboard.token_detail_title, tpl,
    'zh 模板应与 en 同占位符结构')
})

// issue #81-1 / #81-2 定点护栏 (scoped, 非全局死键门禁):
// 静态扫描发现 650 键里另有 103 个静态零引用键, 其中一部分是运行时按
// 数据键枚举消费的假阳性 (如 settings.row.*_label 模板取键) ⇒ 全局
// "必须被引用" 门禁会把可用键判死, 属越权。本套件只钉本次改动的那
// 一键 + 新增的那一键, 与本卡改动一一对应。
// 红→绿自证: 改动前必红 (死键在、err.client 不在), 改后绿。
test('i18n: #81-1 死键已清 (chat.session.no_more_history), #81-2 新增 err.client', () => {
  const flatKeys = (d, p = '') => Object.entries(d).flatMap(([k, v]) =>
    (v && typeof v === 'object' && !Array.isArray(v))
      ? flatKeys(v, p ? `${p}.${k}` : k) : [p ? `${p}.${k}` : k])
  const fen = new Set(flatKeys(en)), fzh = new Set(flatKeys(zh))
  assert(!fen.has('chat.session.no_more_history'),
    '#81-1: 死键未删除')
  assert(!fzh.has('chat.session.no_more_history'),
    '#81-1: 死键 zh 侧未删除')
  assert(fen.has('err.client'), '#81-2: err.client 未新增')
  assert(fzh.has('err.client'), '#81-2: err.client zh 缺失 (双语对称)')
})

// ═════════════════ B. formatTokens / formatCost (AC-U1) ═════════════════
test('formatTokens: 0 → "0"', () => { eq(formatTokens(0), '0') })
test('formatTokens: 1-999 原样', () => {
  eq(formatTokens(7), '7'); eq(formatTokens(999), '999')
})
test('formatTokens: 1,000-999,999 千分位', () => {
  eq(formatTokens(1234), '1,234'); eq(formatTokens(999999), '999,999')
})
test('formatTokens: ≥1M 缩写 1 位小数', () => {
  eq(formatTokens(1_234_567), '1.2M'); eq(formatTokens(1_000_000), '1.0M')
})
test('formatTokens: ≥1B 缩写 1 位小数', () => {
  eq(formatTokens(1_234_567_890), '1.2B')
})
test('formatTokens: 非数值防御 → "0"', () => {
  eq(formatTokens(undefined), '0'); eq(formatTokens(null), '0')
  eq(formatTokens('abc'), '0')
})

test('formatCost: 0 → $0.00; 0<usd<0.01 → <$0.01; 其余 2 位小数', () => {
  eq(formatCost(0), '$0.00')
  eq(formatCost(0.0042), '<$0.01')
  eq(formatCost(0.01), '$0.01')
  eq(formatCost(4.2), '$4.20')
})

// ═════════════════ C. 卡片级 tokenLabel/tokenTitle (AC-U2) ═════════════════
const snap = (over = {}) => ({
  input_tokens: 1234, output_tokens: 567, cache_read_tokens: 0,
  cache_write_tokens: 0, reasoning_tokens: 0, estimated_cost_usd: 0.0042,
  total_tokens: 1801, captured_at: 1760874000.123, source: 'api', ...over,
})

test('tokenLabel: 缺失(undefined) → token_missing', () => {
  eq(tokenLabel(mkT(), undefined), '—')
})
test('tokenLabel: source=unavailable → token_missing', () => {
  eq(tokenLabel(mkT(), snap({ source: 'unavailable', total_tokens: 0 })),
    '—')
})
test('tokenLabel: source=api 且 0 → "0" (与缺失可区分)', () => {
  eq(tokenLabel(mkT(), snap({ total_tokens: 0 })), '0')
})
test('tokenLabel: source=api 正常值走 formatTokens', () => {
  eq(tokenLabel(mkT(), snap()), '1,801')
})
test('tokenTitle: 缺失 → token_missing', () => {
  eq(tokenTitle(mkT(), undefined), '—')
})
test('tokenTitle: 有快照 → detail_title 模板 + 五桶/cost/ISO/source 全值', () => {
  const t = mkT()
  const out = tokenTitle(t, snap())
  assert(out.startsWith('tboard.token_detail_title|'), out)
  const opts = JSON.parse(out.split('|').slice(1).join('|'))
  eq(opts.total, 1801); eq(opts.in, 1234); eq(opts.out, 567)
  eq(opts.cr, 0); eq(opts.cw, 0); eq(opts.reason, 0)
  eq(opts.cost, '<$0.01')
  eq(opts.captured_at, new Date(1760874000.123 * 1000).toISOString())
  eq(opts.source, 'api')
})
test('tokenTitle: captured_at null → "—" (unavailable 快照)', () => {
  const out = tokenTitle(mkT(), snap({ source: 'unavailable', captured_at: null }))
  const opts = JSON.parse(out.split('|').slice(1).join('|'))
  eq(opts.captured_at, '—')
})

// ═════════════════ C2. issue #68: 展示侧整数化 (脏浮点 token) ═════════════════
// 后端 mw/console.py::_aggregate_run_tokens 只信 int 桶值 (ARCHITECTURE-58 §5.1),
// 浮点按 0 计; 展示侧直读原值会出现「卡上 150.5 / run 合计算 0」两处口径不一。
// 裁决 = 展示侧整数化 (Math.trunc), 聚合侧不动。
test('issue #68: tokenLabel 脏浮点 150.5 → "150" (不是 "150.5")', () => {
  eq(tokenLabel(mkT(), snap({ total_tokens: 150.5 })), '150')
})
test('issue #68: tokenTitle 的 total 同样整数化', () => {
  const out = tokenTitle(mkT(), snap({ total_tokens: 150.5 }))
  const opts = JSON.parse(out.split('|').slice(1).join('|'))
  eq(opts.total, 150)
})
test('issue #68: 负浮点向零截断 (与 Math.trunc 一致)', () => {
  eq(tokenLabel(mkT(), snap({ total_tokens: -150.5 })), '0')
})
test('issue #68: 非数值 / NaN / Infinity → 0 (与 formatTokens 同防御)', () => {
  eq(tokenLabel(mkT(), snap({ total_tokens: undefined })), '0')
  eq(tokenLabel(mkT(), snap({ total_tokens: null })), '0')
  eq(tokenLabel(mkT(), snap({ total_tokens: 'abc' })), '0')
  eq(tokenLabel(mkT(), snap({ total_tokens: NaN })), '0')
  eq(tokenLabel(mkT(), snap({ total_tokens: Infinity })), '0')
})
test('issue #68: 整数原值不受影响 (150 → "150")', () => {
  eq(tokenLabel(mkT(), snap({ total_tokens: 150 })), '150')
  eq(tokenLabel(mkT(), snap()), '1,801')
})
test('issue #68: 0 / 缺失 / source=unavailable 三分支行为不变', () => {
  // 0 必须仍显示 "0" (与缺失 em dash 可区分)
  eq(tokenLabel(mkT(), snap({ total_tokens: 0 })), '0')
  eq(tokenLabel(mkT(), undefined), '—')
  eq(tokenLabel(mkT(), snap({ source: 'unavailable', total_tokens: 0 })), '—')
  eq(tokenTitle(mkT(), undefined), '—')
})

// ═════════════════ D. run 级 runTokenLabel/runTokenTitle (AC-U1/U4) ═════════════════
test('runTokenLabel: 缺失/unavailable → token_missing (空 run 同)', () => {
  eq(runTokenLabel(mkT(), undefined), '—')
  eq(runTokenLabel(mkT(), { total_tokens: 0, estimated_cost_usd: 0.0,
    captured: 0, missing: 3, source: 'unavailable' }), '—')
})
test('runTokenLabel: api 全采集 → 总量/成本', () => {
  eq(runTokenLabel(mkT(), { total_tokens: 5403, estimated_cost_usd: 0.0126,
    captured: 3, missing: 0, source: 'api' }), '5,403/$0.01')
})
test('runTokenLabel: partial → 带「已采集/应采集」缺失角标', () => {
  eq(runTokenLabel(mkT(), { total_tokens: 1801, estimated_cost_usd: 0.0042,
    captured: 1, missing: 2, source: 'partial' }), '1,801/<$0.01 tokens 1/3')
})
test('runTokenTitle: 缺失 → token_missing; 有值 → 模板 source 透传', () => {
  eq(runTokenTitle(mkT(), undefined), '—')
  const out = runTokenTitle(mkT(), { total_tokens: 1801,
    estimated_cost_usd: 0.5, captured: 1, missing: 2, source: 'partial' })
  const opts = JSON.parse(out.split('|').slice(1).join('|'))
  eq(opts.total, 1801); eq(opts.cost, '$0.50'); eq(opts.source, 'partial')
})

// ═════════ E. issue #178 / #188 守护 (s6) ═════════
test('issue #178: tboard.delivery_failed_badge 双边存在且非空', () => {
  for (const [loc, dict] of [['en', en], ['zh-cn', zh]]) {
    const v = dict.tboard && dict.tboard.delivery_failed_badge
    assert(typeof v === 'string' && v.length > 0,
      `${loc}.tboard.delivery_failed_badge 缺失或为空`)
  }
})
test('issue #178: TasksBoardView 消费 current_card_delivery_failed (可选链读法)', () => {
  // 徽标必须挂在列表常驻轮询面 (CurrentTaskCard), 数据源来自
  // team_work_active.current_card_delivery_failed; 空态安全要求可选链
  // 读法 (active?.current_card_delivery_failed), 不得直接解引用。
  assert(src.includes('active?.current_card_delivery_failed'),
    'TasksBoardView.jsx 未以可选链消费 active?.current_card_delivery_failed')
  assert(src.includes("t('tboard.delivery_failed_badge')"),
    'TasksBoardView.jsx 未渲染 delivery_failed_badge 徽标文案')
})
test('issue #188: 键数注释链含 schedule.* 现值口径「现 44 键」', () => {
  // MV-188-a: 若注释被改回「46」而删掉现值括注, 本守护转红。
  // 历史链 719→768→765→766→767 保留不删史, 只要求现值括注在位。
  const self = readFileSync(new URL('./test_issue58_tokens.mjs',
    import.meta.url), 'utf8')
  assert(/schedule\.\* 子树 46 键 \(现 44 键 —— btn 7 \/ form 5 \/ toast 8/.test(self),
    '注释链缺少「现 44 键 —— btn 7 / form 5 / toast 8」现值括注 (issue #188)')
})

// ── runner ──────────────────────────────────────────────────────────
let pass = 0, fail = 0
for (const { name, fn } of tests) {
  try { fn(); pass++; console.log(`  ✓ ${name}`) }
  catch (e) { fail++; console.error(`  ✗ ${name}\n    ${e.message}`) }
}
console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail ? 1 : 0)
