#!/usr/bin/env node
// 单测: issue #158 前端改造 — 文件浏览器上传/删除 (FileDrawer/FileTree/api.js)。
//
// 覆盖 (docs/PLAN-L-20261029.md §2.7/§2.8):
//   A. 上传入口在位: 隐藏 file input (multiple) + api.fsUpload 调用 + 完成后刷新。
//   B. 删除二次确认 (AC-158-2): 确认对话框组件存在且展示完整相对路径 + 大小 +
//      不可撤销文案; 取消 ⇒ 零请求 (doDelete 只在确认框 onConfirm 挂接)。
//   C. 删除按钮可见性: selection.dir === false 才渲染 (目录不显示删除)。
//   D. api.js: fsUpload/fsDelete 形态; fsDelete 必须带 confirm: true 严格布尔。
//   E. FileTree: onEnterDir 回调在 onToggle 分支 (cwd 更新链路)。
//   F. i18n: 16 新键双语双边在位 + 占位符双边一致。
//
// 形态: 源码抽取 + node:vm (与 test_issue159_image_meta.mjs 同口径), npm test 即跑。

import { readFileSync } from 'fs'
import vm from 'node:vm'

const drawer = readFileSync(new URL('../src/components/filebrowser/FileDrawer.jsx', import.meta.url), 'utf8')
const tree = readFileSync(new URL('../src/components/filebrowser/FileTree.jsx', import.meta.url), 'utf8')
const apiSrc = readFileSync(new URL('../src/api.js', import.meta.url), 'utf8')
const en = JSON.parse(readFileSync(new URL('../src/i18n/locales/en.json', import.meta.url), 'utf8'))
const zh = JSON.parse(readFileSync(new URL('../src/i18n/locales/zh-cn.json', import.meta.url), 'utf8'))

const tests = []
function test(name, fn) { tests.push({ name, fn }) }
function assert(cond, msg) { if (!cond) throw new Error(msg) }

// ── A. 上传入口 ──────────────────────────────────────────────────
test('A1 ActionBar 有隐藏 multiple file input (上传入口)', () => {
  assert(/type=["']file["']/.test(drawer), '缺 file input')
  assert(/multiple/.test(drawer), 'file input 缺 multiple')
  assert(/className=["']hidden["']/.test(drawer), 'file input 未隐藏')
  assert(/api\.fsUpload\(/.test(drawer), '未调用 api.fsUpload')
})
test('A2 上传成功后刷新树 (setRefreshTick)', () => {
  const m = drawer.match(/const doUpload[\s\S]*?\n  \}, \[/)
  assert(m, '未找到 doUpload')
  assert(/setRefreshTick/.test(m[0]), 'doUpload 未触发刷新')
})
test('A3 上传目标 = 选中目录或 cwd (PLAN §2.7)', () => {
  assert(/selection\.dir\) \? selection\.path : cwd/.test(drawer)
      || /selection\.dir\s*\?\s*selection\.path\s*:\s*cwd/.test(drawer),
    '上传目标未取 选中目录/cwd')
})

// ── B. 删除二次确认 (AC-158-2) ──────────────────────────────────
test('B1 确认对话框组件存在且展示完整相对路径+大小+不可撤销', () => {
  assert(/function DeleteConfirmDialog/.test(drawer), '缺确认对话框组件')
  assert(/delete_confirm_body/.test(drawer), '确认框未用 delete_confirm_body 文案')
  assert(/path:\s*entry\.path/.test(drawer), '确认框未展示完整相对路径')
  assert(/size:\s*fmtBytes\(entry\.size\)/.test(drawer), '确认框未展示大小')
  // 不可撤销提示在 i18n 文案里 (双语双边)
  assert(/cannot be undone/i.test(en['chat.files.delete_confirm_body']),
    'en 文案缺不可撤销提示')
  assert(en['chat.files.delete_confirm_body'].includes('{path}')
      && en['chat.files.delete_confirm_body'].includes('{size}'),
    'en 文案缺 path/size 占位符')
})
test('B2 取消 ⇒ 零请求: doDelete 只挂确认框 onConfirm, 取消只关框', () => {
  const m = drawer.match(/<DeleteConfirmDialog[\s\S]*?\/>/)
  assert(m, '确认框未渲染')
  assert(/onCancel=\{\(\) => !delBusy && setConfirmDel\(null\)/.test(m[0]),
    '取消按钮未只关框 (应零请求)')
  assert(/onConfirm=\{doDelete\}/.test(m[0]), '确认按钮未挂 doDelete')
  // onRequestDelete 只置 confirmDel (打开对话框), 不发请求
  const od = drawer.match(/onRequestDelete=\{[^}]*\}/)
  assert(od && !/api\.fsDelete/.test(od[0]), 'onRequestDelete 不应直接发请求')
})
test('B3 doDelete 成功后清选择 + 刷新', () => {
  const m = drawer.match(/const doDelete[\s\S]*?\n  \}, \[/)
  assert(m && /api\.fsDelete\(/.test(m[0]), 'doDelete 未调 api.fsDelete')
  assert(/setSelection\(null\)/.test(m[0]) && /setRefreshTick/.test(m[0]),
    'doDelete 未清选择/刷新')
})

// ── C. 删除按钮可见性 ───────────────────────────────────────────
test('C1 删除按钮仅 selection.dir === false 时渲染 (目录不可删)', () => {
  assert(/selection\.dir === false/.test(drawer), '缺 dir===false 判定')
  const m = drawer.match(/const canDelete = selection && selection\.dir === false/)
  assert(m, 'canDelete 判定形态不符')
  assert(/\{canDelete && \(/.test(drawer), '删除按钮未按 canDelete 条件渲染')
})

// ── D. api.js 写面方法 ─────────────────────────────────────────
test('D1 api.fsUpload / api.fsDelete 在位且走 POST JSON', () => {
  assert(/api\.fsUpload = /.test(apiSrc), '缺 api.fsUpload')
  assert(/api\.fsDelete = /.test(apiSrc), '缺 api.fsDelete')
  assert(/method: 'POST'/.test(apiSrc), 'fs 写面未用 POST')
  assert(/fsPost\('\/api\/fs\/upload'/.test(apiSrc), 'fsUpload 未打 /api/fs/upload')
  assert(/fsPost\('\/api\/fs\/delete'/.test(apiSrc), 'fsDelete 未打 /api/fs/delete')
})
test('D2 fsDelete 请求体带 confirm: true 严格布尔 (服务端闸门)', () => {
  assert(/confirm:\s*true\s*\}/.test(apiSrc),
    'fsDelete 未带 confirm: true 字面量 (严格布尔)')
})
test('D3 FsHttpError 形态复用: 非 2xx 抛 status, 不解析 body', () => {
  const m = apiSrc.match(/const fsPost = async[\s\S]*?\n\}/)
  assert(m, '未找到 fsPost')
  assert(/throw new FsHttpError\(r\.status\)/.test(m[0]),
    'fsPost 未按 FsHttpError(status) 形态抛错')
  assert(!/\.json\(\)[\s\S]*catch/.test(m[0]), 'fsPost 不应在错误路径解析 body')
})

// ── E. FileTree onEnterDir ─────────────────────────────────────
test('E1 FileTree 新增 onEnterDir prop 且在 onToggle 分支回调', () => {
  assert(/onEnterDir/.test(tree), 'FileTree 缺 onEnterDir')
  const m = tree.match(/const onToggle = useCallback[\s\S]*?\n  \}, \[[^\]]*\]/)
  assert(m && /onEnterDir\(entry\.path/.test(m[0]),
    'onToggle 分支未回调 onEnterDir (cwd 更新链路断)')
})
test('E2 FileDrawer 把 onEnterDir 接到 setCwd (上传目标随浏览更新)', () => {
  assert(/onEnterDir=\{\(p\) => setCwd\(p\)\}/.test(drawer),
    'FileDrawer 未接 onEnterDir → setCwd')
})

// ── F. i18n 16 新键双语对齐 ────────────────────────────────────
const NEW_KEYS = [
  'chat.files.upload', 'chat.files.upload_hint', 'chat.files.uploading',
  'chat.files.upload_ok', 'chat.files.upload_failed', 'chat.files.upload_conflict',
  'chat.files.upload_too_large', 'chat.files.delete', 'chat.files.delete_confirm_title',
  'chat.files.delete_confirm_body', 'chat.files.delete_cancel', 'chat.files.delete_ok',
  'chat.files.delete_failed', 'chat.files.delete_needs_file',
  'chat.files.err_conflict', 'chat.files.err_confirm_required',
]
test('F1 16 新键双语双边在位', () => {
  assert(NEW_KEYS.length === 16, '键表数量应为 16')
  for (const k of NEW_KEYS) {
    assert(typeof en[k] === 'string' && en[k].length > 0, `en 缺键 ${k}`)
    assert(typeof zh[k] === 'string' && zh[k].length > 0, `zh 缺键 ${k}`)
  }
})
test('F2 模板占位符双语一致 (i18next 单花括号约定)', () => {
  const ph = (s) => (String(s).match(/\{\w+\}/g) || []).sort().join(',')
  for (const k of NEW_KEYS) {
    assert(ph(en[k]) === ph(zh[k]), `占位符不一致: ${k} en=${ph(en[k])} zh=${ph(zh[k])}`)
  }
})
test('F3 确认框文案不含绝对路径线索 (零回显纪律)', () => {
  for (const k of NEW_KEYS) {
    assert(!/^\/[a-z]/.test(en[k]) && !/^\/[a-z]/.test(zh[k]),
      `键 ${k} 的文案疑似绝对路径`)
  }
})

// ── 运行 ───────────────────────────────────────────────────────
let pass = 0, fail = 0
for (const { name, fn } of tests) {
  try { fn(); pass++; console.log(`PASS  ${name}`) }
  catch (e) { fail++; console.log(`FAIL  ${name}\n      ${e.message}`) }
}
console.log(`\n== issue158 前端单测: ${pass} passed, ${fail} failed ==`)
process.exit(fail ? 1 : 0)
