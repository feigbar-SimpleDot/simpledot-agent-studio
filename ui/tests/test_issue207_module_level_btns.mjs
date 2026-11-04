// issue #207 守护套件 — App.jsx 内联组件提升为模块级 (S批 s3 / 卡 t_363ab3b2)
//
// 依据: docs/TRIAGE-S-20261102.md §2.3 + docs-source/issue-batch-20261103-s/
//       ISSUES-S.md #207 (根因: WebuiBtn/CoreFeaturesBtn 定义在 App 函数体内,
//       5s 心跳重渲染 ⇒ 组件身份变化 ⇒ 子树重建 ⇒ 焦点掉 body)。
//
// 覆盖:
//   T1  两组件定义位于模块级 (源码位置断言: 定义出现在 `export default
//       function App` 之前), 且 App 函数体内不再出现任何同名定义。
//   T2  依赖 props 化: WebuiBtn 签名含 webuiTarget/onClick; CoreFeaturesBtn
//       签名含 onClick 且内部自调 useTranslation (不再闭包捕获 App 的 t)。
//   T3  四处调用点全部显式传依赖 (onClick / webuiTarget), 无裸用法回潮。
//
// 变异反证锚 (MV): MV-207-A 把任一组件定义搬回 App 函数体 → T1 红;
//                  MV-207-B 去掉 props 改回闭包捕获 → T2/T3 红。
//
// 风格: 沿用仓库既有 ui/tests/*.mjs — 源码抽取 + 正则断言, 不起浏览器。
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const app = readFileSync(path.join(here, '../src/App.jsx'), 'utf8')

let failed = 0, passed = 0
function test(n, f) { try { f(); passed++; console.log(`ok - ${n}`) } catch (e) { failed++; console.error(`FAIL - ${n}\n  ${e.message}`) } }
function assert(c, m) { if (!c) throw new Error(m) }

const appStart = app.indexOf('export default function App()')
assert(appStart > 0, 'App 入口存在')
const moduleLevel = app.slice(0, appStart)
const appBody = app.slice(appStart)

// ── T1: 定义位置 = 模块级 ────────────────────────────────────────────
for (const name of ['WebuiBtn', 'CoreFeaturesBtn']) {
  test(`T1 ${name} 定义在模块级 (App 之前)`, () => {
    const def = new RegExp(`function ${name}\\s*\\(`)
    assert(def.test(moduleLevel), `模块级区未找到 function ${name}`)
    assert(!new RegExp(`(const|function) ${name}\\s*[=(]`).test(appBody),
      `App 函数体内仍残留 ${name} 定义 (内联重建缺陷回潮)`)
  })
}

// ── T2: 依赖 props 化 ────────────────────────────────────────────────
test('T2 WebuiBtn 签名含 webuiTarget/onClick', () => {
  assert(/function WebuiBtn\(\{[^}]*webuiTarget[^}]*onClick[^}]*\}\)/.test(moduleLevel),
    'WebuiBtn props 应含 webuiTarget 与 onClick')
})
test('T2 CoreFeaturesBtn 签名含 onClick 且自调 useTranslation', () => {
  const m = moduleLevel.match(/function CoreFeaturesBtn\([\s\S]*?\n\}/)
  assert(m, 'CoreFeaturesBtn 定义缺失')
  assert(/onClick/.test(m[0].slice(0, m[0].indexOf(')'))), 'CoreFeaturesBtn props 应含 onClick')
  assert(/useTranslation\(\)/.test(m[0]), 'CoreFeaturesBtn 应内部自调 useTranslation (不闭包捕获 App 的 t)')
  assert(!/openCoreFeatures/.test(m[0]), 'CoreFeaturesBtn 不应再直接引用 openCoreFeatures (须 props 传入)')
})

// ── T3: 调用点显式传依赖 ─────────────────────────────────────────────
test('T3 调用点全部传 props (桌面+移动 4 处)', () => {
  const cfUses = [...appBody.matchAll(/<CoreFeaturesBtn\b[^/>]*\/>/g)].map((x) => x[0])
  const wbUses = [...appBody.matchAll(/<WebuiBtn\b[^/>]*\/>/g)].map((x) => x[0])
  assert(cfUses.length === 2, `CoreFeaturesBtn 调用点应为 2, 实得 ${cfUses.length}`)
  assert(wbUses.length === 2, `WebuiBtn 调用点应为 2, 实得 ${wbUses.length}`)
  assert(cfUses.every((u) => /onClick=\{openCoreFeatures\}/.test(u)),
    'CoreFeaturesBtn 调用点须传 onClick={openCoreFeatures}')
  assert(wbUses.every((u) => /webuiTarget=\{webuiTarget\}/.test(u) && /onClick=\{openWebui\}/.test(u)),
    'WebuiBtn 调用点须传 webuiTarget/onClick')
})

console.log(failed ? `\n${failed} FAILURES` : `\nALL PASS (${passed} tests)`)
process.exit(failed ? 1 : 0)
