#!/usr/bin/env node
// issue #228 守护套件 — /api/workers api_key 明文回显根治 (BB轮 s2 / coder)
//
// 依据: docs/issue-convergence-BB/ADJUDICATION.md §2 + docs/dispatch/
//       BB-DOWNSTREAM.md s2 (唯一技术依据)。三步修复:
//       1) 后端 api_workers() 剥离 api_key;
//       2) 后端 api_registry_save() 保存回环合并语义 (缺失/空 ⇒ 保留旧值);
//       3) 前端编辑弹窗 api_key「留空 = 不修改」, 不再回填明文。
//
// 覆盖:
//   T1  openEdit 不再从 GET 响应回填 api_key 明文。
//   T2  doRegister: 16 位必填仅注册态校验 (编辑态留空合法); entry 的
//       api_key 为条件写入 (留空 = 不提交该字段)。
//   T3  编辑弹窗存在「留空 = 不修改」提示 (edit 分支渲染 hint)。
//   T4  i18n: workers.field.api_key_edit_hint 在 zh-cn/en 双语齐备,
//       且 workers.field 子树 key 集合两语完全相等 (parity 守护)。
//   T5  后端静态面: api_workers 的 workers_view 剥离含 k != "api_key";
//       api_registry_save 合并语义块在位 (缺失/空 ⇒ 保留旧值)。
//
// 变异反证锚 (MV): MV-228-A openEdit 恢复 api_key: e.api_key 回填 → T1 红;
//   MV-228-B 去掉 !isEdit 门 (编辑态也强制 16 位) 或 entry 恢复无条件
//   api_key 写入 → T2 红; MV-228-C 删 edit 分支 hint → T3 红;
//   MV-228-D 只改单语 locale → T4 红; MV-228-E 删后端剥离或合并块 → T5 红。
//
// 风格: 沿用仓库既有 ui/tests/*.mjs — 源码抽取 + 断言, 不起浏览器。
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(path.join(here, f), 'utf8')
const wv = read('../src/views/WorkersView.jsx')
const consolePy = read('../../mw/console.py')
const zh = JSON.parse(read('../src/i18n/locales/zh-cn.json'))
const en = JSON.parse(read('../src/i18n/locales/en.json'))

const tests = []
const test = (name, fn) => tests.push({ name, fn })
const assert = (cond, msg) => { if (!cond) throw new Error(msg) }

// ── T1: openEdit 不回填 api_key 明文 ───────────────────────────────
test('T1 openEdit 不再从 GET 响应回填 api_key', () => {
  const at = wv.indexOf('const openEdit')
  assert(at >= 0, 'openEdit 未找到')
  const body = wv.slice(at, at + 700)
  assert(!/api_key:\s*e\.api_key/.test(body),
    'openEdit 仍从 entry 回填 api_key 明文 (#228 回归: GET 已剥离, 应留空)')
  assert(/api_key:\s*''/.test(body),
    "openEdit 应显式 api_key: '' (留空 = 不修改)")
})

// ── T2: doRegister 校验与 entry 构造 ───────────────────────────────
test('T2 16位必填仅注册态 + entry 条件写入 api_key', () => {
  const at = wv.indexOf('const doRegister')
  assert(at >= 0, 'doRegister 未找到')
  const body = wv.slice(at, at + 2200)
  assert(/const isEdit = dlg\.mode === 'edit'/.test(body),
    'isEdit 判定缺失')
  assert(/if \(!isEdit && \(f\.api_key \|\| ''\)\.trim\(\)\.length < 16\)/.test(body),
    '16 位必填必须仅在 !isEdit (注册态) 校验 — 编辑态留空 = 不修改是合法输入')
  assert(!/api_key:\s*f\.api_key\.trim\(\),/.test(body),
    'entry 不得无条件写入 api_key (编辑态留空会把空值 PUT 上去)')
  assert(/if \(\(f\.api_key \|\| ''\)\.trim\(\)\) entry\.api_key = f\.api_key\.trim\(\)/.test(body),
    'entry.api_key 必须条件写入 (非空才提交)')
})

// ── T3: 编辑弹窗「留空 = 不修改」提示 ──────────────────────────────
test('T3 编辑弹窗存在留空=不修改提示 (edit 分支)', () => {
  assert(/dlg\.mode === 'edit' \? '' : ' \*'/.test(wv),
    'api_key 标签的必填星号必须按 edit/register 分支区分')
  assert(/dlg\.mode === 'edit' && \(\s*<div className="hint">\{t\('workers\.field\.api_key_edit_hint'\)\}/.test(wv),
    "edit 分支必须渲染 workers.field.api_key_edit_hint 提示")
})

// ── T4: i18n 双语齐备 + workers.field parity ───────────────────────
test('T4 i18n: api_key_edit_hint 双语齐备且 workers.field 两语 key 相等', () => {
  for (const [tag, loc] of [['zh-cn', zh], ['en', en]]) {
    assert(typeof loc?.workers?.field?.api_key_edit_hint === 'string'
      && loc.workers.field.api_key_edit_hint.length > 0,
      `${tag} 缺 workers.field.api_key_edit_hint`)
  }
  const keys = (o) => JSON.stringify(Object.keys(o).sort())
  assert(keys(zh.workers.field) === keys(en.workers.field),
    'workers.field 子树两语 key 集合不一致 (parity 守护)')
})

// ── T5: 后端静态面 — 剥离 + 合并语义 ───────────────────────────────
test('T5 console.py: api_workers 剥离 api_key + api_registry_save 合并语义', () => {
  const at = consolePy.indexOf('def api_workers()')
  assert(at >= 0, 'api_workers 未找到')
  const fn = consolePy.slice(at, at + 2500)
  assert(/if k != "api_key"/.test(fn),
    'api_workers 的 workers_view 必须剥离 api_key (#228 根治点)')
  const sv = consolePy.indexOf('def api_registry_save')
  assert(sv >= 0, 'api_registry_save 未找到')
  const save = consolePy.slice(sv, sv + 3000)
  assert(/merged\[name\] = entry/.test(save) && /api_key.*old_key|old_key/.test(save)
    && /if not new_key and isinstance\(old_key, str\) and old_key\.strip\(\)/.test(save),
    'api_registry_save 必须含保存回环合并语义 (缺失/空 api_key ⇒ 保留旧值)')
})

// ── runner ─────────────────────────────────────────────────────────
let failed = 0
for (const t of tests) {
  try { await t.fn(); console.log(`  PASS ${t.name}`) }
  catch (e) { failed++; console.error(`  FAIL ${t.name}\n    ${e.message}`) }
}
console.log(failed ? `\n${failed}/${tests.length} FAILED` : `\nALL ${tests.length} PASSED`)
process.exit(failed ? 1 : 0)
