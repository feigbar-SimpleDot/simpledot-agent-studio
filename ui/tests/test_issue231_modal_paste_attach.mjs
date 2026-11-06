#!/usr/bin/env node
// issue #231 守护套件 — 新建任务弹窗支持直接粘贴附件 (FF轮 s2 / coder)
//
// 依据: docs/issue-convergence-FF/ADJUDICATION.md §3 (唯一技术依据)。
// 改法逐条锁死 (§3.3):
//   1) TeamWork 弹窗 textarea (value={twTask}) 挂 onPaste={onTwPasteFiles};
//   2) 剪贴板判定纪律与 #230 onPasteFiles 完全一致: clipboardData.files 主源,
//      兜底 items kind==='file' getAsFile; 无文件 ⇒ 早退不拦截走原生;
//   3) 有文件 ⇒ preventDefault + addTwFiles(files) 汇入弹窗既有 twFiles 管线
//      (addTwFiles 自 onTwFilesPicked 抽出, slots/超限/uploading 预检原样);
//   4) 主输入 onPaste (#230) 不回退。
// 范围边界: 仅 TeamWork 弹窗 textarea。主聊天输入、steer 输入、ScheduleView
//   修改弹窗零接触。零后端改动, 零 i18n 新键。
//
// 变异反证锚 (MV): MV-231-A 摘掉弹窗 textarea 的 onPaste → T1 红;
//   MV-231-B 删 onTwPasteFiles 的 clipboardData.files 读取或 items 兜底 → T2 红;
//   MV-231-C 删 preventDefault 或 addTwFiles 汇入 → T3 红;
//   MV-231-D 把无文件早退挪到 preventDefault 之后 (纯文本被吞) → T3 红;
//   MV-231-E 删 addTwFiles 内 slots/超限预检 → T4 红;
//   MV-231-F 摘掉主输入 onPaste={onPasteFiles} (#230 回退) → T5 红;
//   MV-231-G dist 不随 src 重建 → T6 红。
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

// 弹窗任务 textarea 块: 以 value={twTask} 为身份锚 (主输入用 value={input},
// 不会误命中)。
function twTextarea() {
  const at = cv.indexOf('value={twTask}')
  assert(at >= 0, 'TeamWork 弹窗 textarea 未找到 (value={twTask} 锚缺失)')
  const start = cv.lastIndexOf('<textarea', at)
  assert(start >= 0, '弹窗 textarea 起始标签未找到')
  const end = cv.indexOf('/>', at)
  assert(end >= 0, '弹窗 textarea 结束未找到')
  return cv.slice(start, end)
}

// onTwPasteFiles 处理器体
function twPasteHandler() {
  const at = cv.indexOf('const onTwPasteFiles')
  assert(at >= 0, 'onTwPasteFiles 处理器未找到')
  const end = cv.indexOf('\n  }', at)
  assert(end >= 0, 'onTwPasteFiles 处理器结尾未找到')
  return cv.slice(at, end)
}

// addTwFiles 处理器体
function addTwFilesBody() {
  const at = cv.indexOf('const addTwFiles = (files) => {')
  assert(at >= 0, 'addTwFiles 统一入口未找到')
  const end = cv.indexOf('\n  }', at)
  assert(end >= 0, 'addTwFiles 结尾未找到')
  return cv.slice(at, end)
}

// ── T1: 弹窗 textarea 挂 onPaste ───────────────────────────────────
test('T1 弹窗 textarea 挂 onPaste={onTwPasteFiles}', () => {
  const ta = twTextarea()
  assert(/onPaste=\{onTwPasteFiles\}/.test(ta),
    '弹窗 textarea 必须挂 onPaste={onTwPasteFiles} (#231 粘贴入口)')
  // 全文件 onPaste 恰 2 处: 主输入 onPasteFiles (#230) + 弹窗 onTwPasteFiles (#231)
  const hits = cv.match(/onPaste=\{[A-Za-z]+\}/g) || []
  assert(hits.length === 2,
    `onPaste 绑定全文件应恰 2 处 (主输入+弹窗), 实为 ${hits.length}: ${hits.join(', ')}`)
})

// ── T2: 剪贴板判定纪律与 #230 同 ───────────────────────────────────
test('T2 onTwPasteFiles 读 clipboardData.files + items kind===file 兜底 (与 #230 同纪律)', () => {
  const h = twPasteHandler()
  assert(/e\.clipboardData/.test(h), '处理器必须读 e.clipboardData')
  assert(/dt\.files/.test(h), '处理器必须读 clipboardData.files (主源)')
  assert(/kind === 'file'/.test(h),
    "处理器必须有 items 中 kind === 'file' 的兜底路径")
  assert(/getAsFile/.test(h), 'items 兜底必须经 getAsFile() 取 File')
  // 与 #230 onPasteFiles 判定纪律逐条比对 (两处同一判定)
  const at = cv.indexOf('const onPasteFiles')
  assert(at >= 0, '#230 onPasteFiles 不得删除')
  const h230 = cv.slice(at, cv.indexOf('\n  }', at))
  for (const anchor of [/dt\.files/, /kind === 'file'/, /getAsFile/]) {
    assert(anchor.test(h230) && anchor.test(h),
      `判定纪律锚 ${anchor} 必须在 onPasteFiles 与 onTwPasteFiles 两处同现`)
  }
})

// ── T3: 有文件 ⇒ preventDefault + addTwFiles; 无文件 ⇒ 先早退 ──────
test('T3 有文件 preventDefault+addTwFiles; 无文件早退在 preventDefault 之前', () => {
  const h = twPasteHandler()
  assert(/addTwFiles\(files\)/.test(h),
    '有文件必须汇入 addTwFiles(files) (弹窗既有 twFiles 管线, 不另起上传路径)')
  const pd = h.indexOf('e.preventDefault()')
  assert(pd >= 0, '有文件分支必须 e.preventDefault() 拦截默认粘贴')
  assert((h.match(/preventDefault/g) || []).length === 1,
    '处理器内 preventDefault 只允许 1 处 (仅文件分支), 纯文本路径不得拦截')
  const early = h.indexOf('files.length === 0) return')
  assert(early >= 0, "缺 '无文件 ⇒ 直接 return' 早退 (纯文本粘贴会被吞)")
  assert(early < pd,
    '无文件早退必须位于 preventDefault 之前 — 纯文本粘贴走原生行为')
})

// ── T4: addTwFiles 汇入既有 twFiles 管线 (预检原样, 双入口共用) ────
test('T4 addTwFiles 含 slots/超限/uploading 预检; 回形针与粘贴双入口共用', () => {
  const b = addTwFilesBody()
  assert(/TW_ATTACH_MAX_FILES - twFiles\.length/.test(b),
    'addTwFiles 必须保留 slots 超数预检 (TW_ATTACH_MAX_FILES - twFiles.length)')
  assert(/attach_too_many/.test(b), '超数必须复用 chat.team_work_modal.attach_too_many 文案')
  assert(/f\.size > TW_ATTACH_MAX_BYTES/.test(b),
    'addTwFiles 必须保留 20MB 超限预检 (超限条目直接标红, 不发请求)')
  assert(/entry\.uploading = true/.test(b) && /uploadTwFile\(entry\.id, accepted\[i\]\)/.test(b),
    '通过预检的条目须挂 uploading 并异步 uploadTwFile (既有管线原样)')
  assert(/setTwFiles\(\(p\) => \[\.\.\.p, \.\.\.entries\]\)/.test(b),
    '条目必须追加进 twFiles state')
  // 回形针入口改为调用 addTwFiles (单一管线, 不复制粘贴)
  const at = cv.indexOf('const onTwFilesPicked')
  assert(at >= 0, 'onTwFilesPicked 未找到')
  const picked = cv.slice(at, cv.indexOf('\n  }', at))
  assert(/addTwFiles\(picked\)/.test(picked),
    'onTwFilesPicked 必须改为调用 addTwFiles(picked) (统一入口)')
})

// ── T5: 主输入 onPaste (#230) 不回退 + steer 闸保留 ────────────────
test('T5 主输入 onPaste={onPasteFiles} 不回退; uploadFiles 管线不变; steer 闸保留', () => {
  const at = cv.indexOf("placeholder={t('chat.input.placeholder')}")
  assert(at >= 0, '主输入 textarea 未找到')
  const main = cv.slice(cv.lastIndexOf('<textarea', at), cv.indexOf('/>', at))
  assert(/onPaste=\{onPasteFiles\}/.test(main),
    '主输入 textarea 的 onPaste={onPasteFiles} (#230) 不得回退')
  assert(/const uploadFiles = async \(fileList\) => \{/.test(cv),
    'uploadFiles 签名不得改动')
  assert(/steer_no_attachment/.test(cv),
    'steer 带附件拒绝闸 (chat.errors.steer_no_attachment) 不得移除')
})

// ── T6: dist 重建同步 (构建产物含弹窗粘贴逻辑) ────────────────────
test('T6 dist/index.html 已随 src 重建 (弹窗粘贴逻辑入包)', () => {
  assert(dist.includes('clipboardData'),
    'dist 构建产物不含 clipboardData — dist 未随 #231 改动重建')
  // 弹窗粘贴与主输入粘贴两处处理器都在包内 ⇒ clipboardData 引用 >= 2
  const n = (dist.match(/clipboardData/g) || []).length
  assert(n >= 2,
    `dist 中 clipboardData 引用应 >= 2 (主输入+弹窗两处理器), 实为 ${n} — 弹窗粘贴未入包`)
  assert(/onpaste/i.test(dist),
    'dist 构建产物不含 onPaste 绑定 — dist 未随改动重建')
})

// ── runner ─────────────────────────────────────────────────────────
let failed = 0
for (const t of tests) {
  try { await t.fn(); console.log(`  PASS ${t.name}`) }
  catch (e) { failed++; console.error(`  FAIL ${t.name}\n    ${e.message}`) }
}
console.log(failed ? `\n${failed}/${tests.length} FAILED` : `\nALL ${tests.length} PASSED`)
process.exit(failed ? 1 : 0)
