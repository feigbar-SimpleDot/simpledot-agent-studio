#!/usr/bin/env node
// issue #211 守护套件 — 「发起多智能体协作」弹窗附件上传前端 (T 批 s5 / coder)
//
// 依据: docs/PLAN-T-20261103.md §1.3 s5 前端卡 + §1.4 协议级 detail 格式 +
//       §1.5 双语键 · docs/AC-T-20261103.md §C (AC-211-FE-1~7) ·
//       docs/FIX-NOTES-T-BE.md §3 (s4 契约: /api/upload 调用面 + ATTACH_DETAIL 格式)。
//
// 覆盖 (AC 对照):
//   T1  FE-2 弹窗含附件字段: twFiles state + type="file" multiple input + 隐藏 input ref。
//   T2  FE-3 上传走 /api/upload: uploadTwFile 内 fetch 仅命中既有端点, 无新端点字符串。
//   T3  FE-4 前端 20MB 预检: TW_ATTACH_MAX_BYTES = 20 * 1024 * 1024 且在选择路径参与比较;
//          5 个/任务上限常量在位。
//   T4  FE-5 detail 拼装格式: buildTeamWorkDetail 抽取直测 (vm), 与后端契约
//          tests/test_issue211_upload_contract.py 的 ATTACH_DETAIL 逐字对齐
//          (s4 D-1 口径: 前端产物无尾随换行)。
//   T5  FE-6 i18n 对称: chat.team_work_modal.* 两语种键集 diff = ∅ 且 8 新键双边在位。
//   T6  FE-7 取消弹窗清态: openTeamWork / closeTeamWork 均重置 twFiles。
//   T7  三态提示走 t(): 上传中/失败/超限/超数四文案键均经 t() 引用, 无硬编码单语。
//   T8  提交守卫: submitTeamWork 拒 uploading 中提交; 确定钮 disabled 含 uploading 分支。
//
// 变异反证锚 (MV): MV-211-A 删 multiple → T1 红; MV-211-B 改上传端点 → T2 红;
//   MV-211-C 改 20MB 常量 → T3 红; MV-211-D 改【附件】格式(去空行/改前缀) → T4 红;
//   MV-211-E 任一语种删 attach_* 键 → T5 红; MV-211-F 删 closeTeamWork 的
//   setTwFiles([]) → T6 红; MV-211-G 文案键改硬编码 → T7 红。
//
// 风格: 沿用仓库既有 ui/tests/*.mjs — 源码抽取 + vm 直测 + JSON 断言, 不起浏览器。
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(path.join(here, f), 'utf8')
const chat = read('../src/views/ChatView.jsx')
const zh = JSON.parse(read('../src/i18n/locales/zh-cn.json'))
const en = JSON.parse(read('../src/i18n/locales/en.json'))
// 后端契约文件 (s4 落定): detail 附件清单协议格式的权威定义
const contractPy = read('../../tests/test_issue211_upload_contract.py')

const tests = []
function test(name, fn) { tests.push({ name, fn }) }
function assert(c, m) { if (!c) throw new Error(m) }
function eq(a, b, msg) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b)
  if (sa !== sb) throw new Error(`${msg || 'eq'}:\n  实际 ${sa}\n  期望 ${sb}`)
}

// 源码抽取 (花括号/圆括号配平, 同 test_issue58_tokens.mjs 口径)
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
  throw new Error(`源码抽取失败 (括号不配平): ${startMarker}`)
}

// ── T1 (AC-211-FE-2): 弹窗含附件字段 ─────────────────────────────────
test('T1 twFiles state 在位 (数组条目含 uploading/error 形态)', () => {
  assert(/const \[twFiles, setTwFiles\] = useState\(\[\]\)/.test(chat),
    '缺 twFiles state: const [twFiles, setTwFiles] = useState([])')
  assert(/uploading/.test(chat) && /error/.test(chat), '条目应含 uploading/error 字段')
})
test('T1 弹窗含 multiple 文件 input (隐藏 + ref 触发)', () => {
  assert(/<input type="file" multiple ref=\{twFileInputRef\}/.test(chat),
    '弹窗应含 <input type="file" multiple ref={twFileInputRef} ...> 隐藏输入')
  assert(/twFileInputRef\.current && twFileInputRef\.current\.click\(\)/.test(chat),
    '「添加附件」按钮应经 ref.click() 触发隐藏 input')
})
test('T1 已传清单渲染名称+大小+移除钮', () => {
  assert(/twFiles\.map\(/.test(chat), '清单应 twFiles.map 渲染')
  assert(/f\.name/.test(chat) && /f\.bytes/.test(chat), '清单条目应显示名称与大小')
  assert(/removeTwFile\(f\.id\)/.test(chat), '清单条目应有移除按钮 removeTwFile(f.id)')
})

// ── T2 (AC-211-FE-3): 上传走既有 /api/upload, 无新端点 ───────────────
const uploadFn = (() => { try { return extractFn(chat, 'const uploadTwFile = async (entryId, file) => {') } catch { return '' } })()
test('T2 uploadTwFile 调用 POST /api/upload (既有端点)', () => {
  assert(uploadFn.length > 0, 'uploadTwFile 函数抽取失败')
  assert(/fetch\('\/api\/upload'/.test(uploadFn), '上传必须走 fetch(\'/api/upload\'...) 既有端点')
  assert(/method: 'POST'/.test(uploadFn), '/api/upload 调用须为 POST')
  assert(/name: file\.name, data: dataUrl/.test(uploadFn),
    'payload 须为 {name, data: dataURL} (s4 契约调用面)')
})
test('T2 无新端点: uploadTwFile 内 fetch 仅命中 /api/upload', () => {
  const fetches = uploadFn.match(/fetch\(['"]([^'"]+)['"]/g) || []
  assert(fetches.length === 1 && fetches[0] === "fetch('/api/upload'",
    `uploadTwFile 内 fetch 端点应唯一 /api/upload, 实得 ${JSON.stringify(fetches)}`)
})
test('T2 FileReader → dataURL 模式与聊天附件一致', () => {
  assert(/readAsDataURL\(file\)/.test(uploadFn), '应 FileReader.readAsDataURL 读文件')
})

// ── T3 (AC-211-FE-4): 前端 20MB 预检 + 5 个上限 ──────────────────────
test('T3 20MB 常量 = 20 * 1024 * 1024 且在选择路径参与比较', () => {
  assert(/const TW_ATTACH_MAX_BYTES = 20 \* 1024 \* 1024/.test(chat),
    '缺常量 const TW_ATTACH_MAX_BYTES = 20 * 1024 * 1024')
  assert(/f\.size > TW_ATTACH_MAX_BYTES/.test(chat),
    '选择路径须存在 f.size > TW_ATTACH_MAX_BYTES 预检 (超限不发请求)')
})
test('T3 5 个/任务上限常量在位并参与预检', () => {
  assert(/const TW_ATTACH_MAX_FILES = 5/.test(chat), '缺常量 const TW_ATTACH_MAX_FILES = 5')
  assert(/TW_ATTACH_MAX_FILES - twFiles\.length/.test(chat), '超数预检应基于 TW_ATTACH_MAX_FILES')
})
test('T3 服务端 20MB 闸文案映射本地化 (双保险)', () => {
  assert(/includes\('20MB'\)/.test(uploadFn),
    '服务端 ValueError 文案「附件超过 20MB 上限」须映射 attach_too_large 本地化')
})

// ── T4 (AC-211-FE-5): detail 拼装格式, 抽取直测 + 后端契约逐字对齐 ───
const buildDetail = (() => {
  const src = extractFn(chat, 'function buildTeamWorkDetail(task, files) {')
  const ctx = { module: {} }
  vm.createContext(ctx)
  vm.runInContext(`${src}; this.__f = buildTeamWorkDetail`, ctx)
  return ctx.__f
})()
// 从后端契约测试提取 ATTACH_DETAIL 常量 (字符串字面量拼接)
const contractDetail = (() => {
  const m = contractPy.match(/ATTACH_DETAIL = \(([\s\S]*?)\n\)/)
  assert(m, '后端契约 ATTACH_DETAIL 提取失败')
  const parts = m[1].match(/"((?:[^"\\]|\\.)*)"/g) || []
  return parts.map((p) => JSON.parse(p)).join('')
})()
test('T4 无附件 = 任务原文不变', () => {
  eq(buildDetail('任务', []), '任务', '无附件 detail')
  eq(buildDetail('任务', null), '任务', 'files=null 容错')
})
test('T4 附件段格式逐字 = 任务原文\\n\\n【附件】\\n- <绝对路径>', () => {
  const files = [
    { name: 'a.csv', path: '/share_workspace/uploads/20261103_120000_ab12cd_a.csv', bytes: 1 },
    { name: 'b.md', path: '/share_workspace/uploads/20261103_120001_ef34gh_b.md', bytes: 2 },
  ]
  const got = buildDetail('帮我分析这份数据', files)
  const want = '帮我分析这份数据\n\n【附件】\n'
    + '- /share_workspace/uploads/20261103_120000_ab12cd_a.csv\n'
    + '- /share_workspace/uploads/20261103_120001_ef34gh_b.md'
  eq(got, want, '附件清单格式')
})
test('T4 与后端契约 ATTACH_DETAIL 逐字对齐 (s4 D-1: 前端无尾随换行)', () => {
  const files = [
    { path: '/share_workspace/uploads/20261103_120000_ab12cd_data.csv' },
    { path: '/share_workspace/uploads/20261103_120001_ef34gh_notes.md' },
  ]
  eq(buildDetail('帮我分析这份数据', files), contractDetail.replace(/\n$/, ''),
    '前端产物必须与 tests/test_issue211_upload_contract.py ATTACH_DETAIL 逐字一致(去尾换行)')
})
test('T4 只收上传成功条目: uploading/error/无 path 一律不入 detail', () => {
  const files = [
    { name: 'ok', path: '/share_workspace/uploads/ok.txt' },
    { name: 'up', path: '', uploading: true },
    { name: 'fail', path: '', error: 'upload_failed' },
    { name: 'big', path: '', error: 'too_large' },
    { name: 'nopath', path: '' },
  ]
  eq(buildDetail('T', files), 'T\n\n【附件】\n- /share_workspace/uploads/ok.txt',
    'detail 只应含上传成功条目')
})

// ── T5 (AC-211-FE-6): i18n 双语对称 + 8 新键 ─────────────────────────
const NEW_KEYS = ['attach_label', 'attach_btn', 'attach_hint', 'attach_uploading',
  'attach_upload_failed', 'attach_too_large', 'attach_too_many', 'attach_remove']
test('T5 chat.team_work_modal 两语种键集 diff = ∅', () => {
  const te = en.chat.team_work_modal, tz = zh.chat.team_work_modal
  const ke = new Set(Object.keys(te)), kz = new Set(Object.keys(tz))
  eq([...ke].filter((k) => !kz.has(k)), [], 'only-in-en')
  eq([...kz].filter((k) => !ke.has(k)), [], 'only-in-zh')
})
test('T5 8 新键双边在位且非空', () => {
  for (const k of NEW_KEYS) {
    assert(typeof en.chat.team_work_modal[k] === 'string' && en.chat.team_work_modal[k].length > 0,
      `缺 en 键 chat.team_work_modal.${k}`)
    assert(typeof zh.chat.team_work_modal[k] === 'string' && zh.chat.team_work_modal[k].length > 0,
      `缺 zh 键 chat.team_work_modal.${k}`)
  }
})
test('T5 既有键零改动零删除 (基线 16 键全在)', () => {
  for (const k of ['auto_select', 'cancel_btn', 'inherit_option', 'start_btn',
    'start_busy_btn', 'subtitle', 'task_placeholder', 'task_requirement',
    'task_requirement_hint', 'title', 'workflows_count', 'workflows_unreachable',
    'workspace_hint', 'workspace_label', 'workspace_placeholder', 'queued_toast']) {
    assert(k in en.chat.team_work_modal && k in zh.chat.team_work_modal, `既有键丢失: ${k}`)
  }
})

// ── T6 (AC-211-FE-7): 打开/关闭弹窗清附件清单 ────────────────────────
test('T6 openTeamWork 重置 twFiles', () => {
  const fn = extractFn(chat, 'const openTeamWork = async () => {')
  assert(/setTwFiles\(\[\]\)/.test(fn), 'openTeamWork 须 setTwFiles([]) 重置')
})
test('T6 closeTeamWork 重置 twFiles', () => {
  const fn = extractFn(chat, 'const closeTeamWork = () => {')
  assert(/setTwFiles\(\[\]\)/.test(fn), 'closeTeamWork 须 setTwFiles([]) 清态')
})

// ── T7: 三态提示全部走 t(), 无硬编码单语 ─────────────────────────────
test('T7 上传中/失败/超限/超数四文案均经 t() 引用', () => {
  for (const k of ['attach_uploading', 'attach_upload_failed', 'attach_too_large', 'attach_too_many']) {
    assert(chat.includes(`t('chat.team_work_modal.${k}')`),
      `文案 ${k} 必须走 t('chat.team_work_modal.${k}') (不许硬编码单语)`)
  }
})
test('T7 弹窗附件区块无硬编码中文提示 (全部 i18n)', () => {
  const cstart = chat.indexOf('issue #211: 附件字段')
  assert(cstart > 0, '附件字段区块注释锚未找到')
  const block = chat.slice(chat.indexOf('*/}', cstart) + 3, chat.indexOf('{twError &&', cstart))
  // 剥离 JSX 注释后, 渲染表达式里不得出现中文字符
  const code = block.replace(/\{\/\*[\s\S]*?\*\/\}/g, '')
  assert(!/[\u4e00-\u9fff]/.test(code), '附件区块渲染代码含硬编码中文, 须走 t()')
})

// ── T8: 提交守卫 ─────────────────────────────────────────────────────
test('T8 submitTeamWork 拒绝上传中提交', () => {
  const fn = extractFn(chat, 'const submitTeamWork = async () => {')
  assert(/twFiles\.some\(\(f\) => f\.uploading\)/.test(fn),
    'submitTeamWork 须有 uploading 中拒绝提交守卫')
  assert(/detail: buildTeamWorkDetail\(task, twFiles\)/.test(fn),
    'scheduleCreate 的 detail 须经 buildTeamWorkDetail 拼装')
})
test('T8 确定钮 disabled 含 uploading 分支', () => {
  assert(/disabled=\{twLaunching \|\| !twTask\.trim\(\) \|\| twFiles\.some\(\(f\) => f\.uploading\)\}/.test(chat),
    '「确定并启用」disabled 须含 twFiles.some((f) => f.uploading)')
})

// ── runner ───────────────────────────────────────────────────────────
let failed = 0
for (const { name, fn } of tests) {
  try { fn(); console.log(`ok - ${name}`) }
  catch (e) { failed++; console.error(`FAIL - ${name}\n  ${e.message}`) }
}
console.log(`\nissue#211 teamwork-attach: ${tests.length - failed} passed, ${failed} failed`)
if (failed) process.exit(1)
