#!/usr/bin/env node
// issue #230 守护套件 — 聊天窗口支持直接粘贴附件 (EE轮 s2 / coder)
//
// 依据: docs/issue-convergence-EE/ADJUDICATION.md §2.1 (唯一技术依据)。
// 改法三条, 逐条锁死:
//   1) 主输入 textarea 挂 onPaste={onPasteFiles};
//   2) onPasteFiles 读 clipboardData.files (兜底 items kind==='file'),
//      有文件 ⇒ preventDefault + 汇入既有 uploadFiles (:1473 零改动复用);
//   3) 无文件 ⇒ 不拦截不 preventDefault, 文本粘贴走原生行为。
// 范围边界: 仅主聊天输入。(TeamWork 弹窗 textarea 已由 FF轮 #231 补挂
// onPaste, 归 test_issue231 守护; steer 输入仍不动。)
//
// 变异反证锚 (MV): MV-230-A 摘掉 textarea 的 onPaste → T1 红;
//   MV-230-B 删 clipboardData.files 读取或 items 兜底 → T2 红;
//   MV-230-C 删 preventDefault 或 uploadFiles 汇入 → T3 红;
//   MV-230-D 把无文件早退挪到 preventDefault 之后 (纯文本被吞) → T3 红;
//   MV-230-E 改 uploadFiles 签名/删 steer_no_attachment 闸 → T4/T5 红;
//   MV-230-G dist 不随 src 重建 → T6 红。
//
// 风格: 沿用仓库既有 ui/tests/*.mjs — 源码抽取 + 断言, 不起浏览器。
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(path.join(here, f), 'utf8')
const cv = read('../src/views/ChatView.jsx')
const dist = read('../dist/index.html')

const tests = []
const test = (name, fn) => tests.push({ name, fn })
const assert = (cond, msg) => { if (!cond) throw new Error(msg) }

// 主输入 textarea 块: 以 chat.input.placeholder 为身份锚 (TeamWork 弹窗
// textarea 用 chat.team_work_modal.task_placeholder, 不会误命中)。
function mainTextarea() {
  const at = cv.indexOf("placeholder={t('chat.input.placeholder')}")
  assert(at >= 0, "主输入 textarea 未找到 (chat.input.placeholder 锚缺失)")
  const start = cv.lastIndexOf('<textarea', at)
  assert(start >= 0, '主输入 textarea 起始标签未找到')
  const end = cv.indexOf('/>', at)
  assert(end >= 0, '主输入 textarea 结束未找到')
  return cv.slice(start, end)
}

// onPasteFiles 处理器体
function pasteHandler() {
  const at = cv.indexOf('const onPasteFiles')
  assert(at >= 0, 'onPasteFiles 处理器未找到')
  const end = cv.indexOf('\n  }', at)
  assert(end >= 0, 'onPasteFiles 处理器结尾未找到')
  return cv.slice(at, end)
}

// ── T1: 主输入 textarea 挂 onPaste ─────────────────────────────────
test('T1 主输入 textarea 挂 onPaste={onPasteFiles}', () => {
  const ta = mainTextarea()
  assert(/onPaste=\{onPasteFiles\}/.test(ta),
    '主输入 textarea 必须挂 onPaste={onPasteFiles} (#230 粘贴入口)')
  // 全文件 onPaste 只允许出现一次 (仅主输入; TeamWork/steer 不动由 T5 锁)
  const hits = cv.match(/onPaste=\{onPasteFiles\}/g) || []
  assert(hits.length === 1,
    `onPaste={onPasteFiles} 全文件应恰 1 处, 实为 ${hits.length} (范围越界)`)
})

// ── T2: 处理器读 clipboardData (files 主源 + items 兜底) ───────────
test('T2 onPasteFiles 读 clipboardData.files + items kind===file 兜底', () => {
  const h = pasteHandler()
  assert(/e\.clipboardData/.test(h), '处理器必须读 e.clipboardData')
  assert(/dt\.files/.test(h), '处理器必须读 clipboardData.files (主源)')
  assert(/kind === 'file'/.test(h),
    "处理器必须有 items 中 kind === 'file' 的兜底路径")
  assert(/getAsFile/.test(h), 'items 兜底必须经 getAsFile() 取 File')
})

// ── T3: 有文件 ⇒ preventDefault + 汇入 uploadFiles; 无文件 ⇒ 先早退 ──
test('T3 有文件 preventDefault+uploadFiles; 无文件早退在 preventDefault 之前', () => {
  const h = pasteHandler()
  assert(/uploadFiles\(files\)/.test(h),
    '有文件必须汇入既有 uploadFiles(files) (零改动复用, 不另起上传路径)')
  const pd = h.indexOf('e.preventDefault()')
  assert(pd >= 0, '有文件分支必须 e.preventDefault() 拦截默认粘贴')
  assert((h.match(/preventDefault/g) || []).length === 1,
    '处理器内 preventDefault 只允许 1 处 (仅文件分支), 纯文本路径不得拦截')
  const early = h.indexOf('files.length === 0) return')
  assert(early >= 0, "缺 '无文件 ⇒ 直接 return' 早退 (纯文本粘贴会被吞)")
  assert(early < pd,
    '无文件早退必须位于 preventDefault 之前 — 纯文本粘贴走原生行为')
})

// ── T4: uploadFiles 既有函数零改动复用 ─────────────────────────────
test('T4 uploadFiles 既有管线不变 (签名 + 回形针入口)', () => {
  assert(/const uploadFiles = async \(fileList\) => \{/.test(cv),
    'uploadFiles 签名不得改动 (#230 只加粘贴入口, 不改上传管线)')
  assert(/onChange=\{\(e\) => uploadFiles\(e\.target\.files\)\}/.test(cv),
    '回形针 file input 的 uploadFiles 入口不得改动 (回归锚)')
})

// ── T5: 范围边界 — steer 不动 (TeamWork 弹窗边界已由 FF轮 #231 移交) ───
// 原断言「TeamWork 弹窗 textarea 不得挂 onPaste」依据 EE轮 §2.1 范围边界;
// FF轮 ADJUDICATION §3.3 明确「本单正是补上这块」⇒ 该边界已移交
// test_issue231_modal_paste_attach.mjs 管辖, 此处不再锁弹窗。
test('T5 steer 附件闸保留 (弹窗 onPaste 归 #231 守护)', () => {
  assert(/steer_no_attachment/.test(cv),
    'steer 带附件拒绝闸 (chat.errors.steer_no_attachment) 不得移除')
})

// ── T6: dist 重建同步 (构建产物含粘贴逻辑) ────────────────────────
test('T6 dist/index.html 已随 src 重建 (含粘贴处理串)', () => {
  assert(dist.includes('clipboardData'),
    'dist 构建产物不含 clipboardData — dist 未随 #230 改动重建')
  assert(/onpaste/i.test(dist),
    'dist 构建产物不含 onPaste 绑定 — dist 未随 #230 改动重建')
})

// ── runner ─────────────────────────────────────────────────────────
let failed = 0
for (const t of tests) {
  try { await t.fn(); console.log(`  PASS ${t.name}`) }
  catch (e) { failed++; console.error(`  FAIL ${t.name}\n    ${e.message}`) }
}
console.log(failed ? `\n${failed}/${tests.length} FAILED` : `\nALL ${tests.length} PASSED`)
process.exit(failed ? 1 : 0)
