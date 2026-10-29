// issue #146 前端回归测试 (s6)
//
// 依据: docs/PLAN-H-20261028.md §6.9 (过程文档, 位于任务 workspace docs/,
//       不入仓 —— 本文件注释自含覆盖点)。
//
// 覆盖:
//   T1  TasksBoardView: TODO_STATUS_TEXT_KEYS 映射表五态齐全, 值是
//       'tboard.todo_status_*' 词条键 (机器值, 非展示文案)。
//   T2  映射表键集 === 契约 JSON statuses 键集 (跨产物契约: 后端下发的
//       token 必须都有前端映射 ⇒ 漏一个 = 显示裸 token)。后端运行时
//       _TODO_STATUS_SEM 键集由 tests/test_issue124_status_alias.py 钉住,
//       后端派生形态由本文件 T8 断言 B 钉住 (issue #154 去 Python 子进程依赖)。
//   T3  locale 双语: 五个 tboard.todo_status_* 键在 en/zh 两侧都在,
//       zh 值与改造前逐字一致 (零回归锚), en 值按 s1 §5.2 定稿逐字。
//   T4  渲染分支: token 命中映射 → 走 t(); 未命中 (fallback 自由文本)
//       → 原样透传, **不被吞成「排队中」** (AC-146-6)。
//   T5  审查 F-1 原型链键防御: todoStatusTextKey/todoStatusText 对
//       'constructor'/'toString'/'__proto__' 等原型链键**不得**命中映射表
//       (旧 truthy 查表会命中 Object.prototype, 把函数源码灌进 chip 文案)。
//   T6  审查 F-1 坏形状防御: 数组/对象/数字等非字符串 token 不得因
//       String() 强制转换被当成合法状态 (hasOwnProperty.call 对数组返回 true)。
//   T7  审查 F-1 防回退源码扫描: 映射表的下标查表只允许出现在
//       todoStatusTextKey 内 (消费点必须走自有键判定函数)。
//   T8  issue #149 F-2 防回退源码形态断言: 前端映射表必须由
//       mw/todo_status_contract.json 派生 (断言 A), 后端 _TODO_STATUS_SEM
//       必须由该 JSON 的推导式派生 (断言 B), 真相源 JSON 不得混入前端
//       词条键 (断言 C) —— 钉死「单一真相源」不回退成两份字面量。
//
// 变异反证锚 (MV): MV-146-A 删映射表里 waiting_sub → T2 红;
//                  MV-146-B 把 zh 值改成「排队中」 → T3 红;
//                  MV-146-D 把渲染改成「查不到映射就显示排队中」 → T4 红;
//                  MV-146-G 把 todoStatusTextKey 改回 truthy 查表 → T5 红;
//                  MV-146-H 删掉 typeof 守卫 → T6 红;
//                  MV-149-F2-b 前端退回字面量字典 → T8 断言 A 红;
//                  MV-149-F2-c 后端退回字面量字典 → T8 断言 B 红;
//                  MV-154-a 前端派生式 filter 掉一键 → T1/T2 红
//                  (后端侧变异 MV-154-b 由 pytest test_issue124 拦, 见 FIX-NOTES-J-154)。
//
// 风格: 沿用仓库既有 ui/tests/*.mjs —— 源码抽取 + vm 沙箱, 不起浏览器。
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import vm from 'node:vm'

const here = path.dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(path.join(here, f), 'utf8')
const boardSrc = read('../src/views/TasksBoardView.jsx')
// issue #149 F-2: 状态契约的单一真相源 (前后端共同派生源), T1/T2/F2S 用
const contractPath = path.join(here, '../../mw/todo_status_contract.json')
const contract = JSON.parse(readFileSync(contractPath, 'utf8'))
// 跨层契约源: 后端 _TODO_STATUS_SEM 的键集 (T2 用)
const consoleSrc = read('../../mw/console.py')
const zh = JSON.parse(read('../src/i18n/locales/zh-cn.json'))
const en = JSON.parse(read('../src/i18n/locales/en.json'))

let failed = 0
function test(n, f) { try { f(); console.log(`ok - ${n}`) } catch (e) { failed++; console.error(`FAIL - ${n}\n  ${e.message}`) } }
function assert(c, m) { if (!c) throw new Error(m) }
function eq(a, b, m) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b)
  if (sa !== sb) throw new Error(`${m || ''} 期望 ${sb}, 实得 ${sa}`)
}

// ── 源码抽取工具 (与 test_issue132_waiting_sub.mjs 同款: 大括号配平) ──
function extractConst(src, name) {
  const idx = src.indexOf(`const ${name} = {`)
  if (idx < 0) throw new Error(`源码抽取失败: 找不到常量 ${name}`)
  let braces = 0, seen = false
  for (let i = src.indexOf('{', idx); i < src.length; i++) {
    const ch = src[i]
    if (ch === '{') { braces++; seen = true } else if (ch === '}') braces--
    if (seen && braces === 0) return src.slice(idx, i + 1)
  }
  throw new Error(`源码抽取失败: ${name} 大括号未配平`)
}

// 映射表 (issue #149 F-2 派生形态): 前端源码里 TODO_STATUS_TEXT_KEYS 由
// 真相源 JSON 派生 ⇒ 优先从源码抽出**派生表达式本身**在 vm 里求值 (源码即
// 真相, 不手抄), 真相源以沙箱变量注入。抽不到派生表达式时退回旧的字面量
// 抽取路径 —— 让文件仍能加载、T8 断言 A **显式报红** (而不是模块级崩溃,
// 保证「哪条断言拦住回退」可归因)。
const DERIVE_EXPR_RE =
  /const\s+TODO_STATUS_TEXT_KEYS\s*=\s*(Object\.fromEntries\([\s\S]*?\)\s*\))/
function deriveFrontMap() {
  const m = boardSrc.match(DERIVE_EXPR_RE)
  if (m) return vm.runInContext(`(${m[1]})`, vm.createContext({ TODO_STATUS_CONTRACT: contract }))
  // 回退路径: 字面量字典形态 (T8 断言 A 会把它钉死为违规)
  return vm.runInContext(
    `(${extractConst(boardSrc, 'TODO_STATUS_TEXT_KEYS').replace(/^const\s+\S+\s*=\s*/, '').replace(/;\s*$/, '')})`,
    vm.createContext({}))
}
const TODO_STATUS_TEXT_KEYS = deriveFrontMap()

// ── 纯函数抽取 (源码执行, 不是复制逻辑) ──────────────────────────
// 审查 F-1: token → 词条键的自有键判定函数 (todoStatusTextKey) 与
// chip 文案解析函数 (todoStatusText) 都必须是**具名导出函数** —— 单测
// 直接抽源码进沙箱跑, 行为级断言「原型链键不得命中映射表」。
function extractFn(src, name) {
  const idx = src.indexOf(`function ${name}(`)
  if (idx < 0) throw new Error(`源码抽取失败: 找不到函数 ${name}`)
  let braces = 0, seen = false
  for (let i = idx; i < src.length; i++) {
    const ch = src[i]
    if (ch === '{') { braces++; seen = true } else if (ch === '}') braces--
    if (seen && braces === 0) return src.slice(idx, i + 1)
  }
  throw new Error(`源码抽取失败: ${name} 大括号未配平`)
}

// CurrentBlock 的 sem 解析段: 原样抽出两行 const, 包成可执行函数 (T4/T5/T6)。
// 抽取失败 (锚丢失) 直接 throw ⇒ 测试红, 防「源码重构后测试静默失效」。
// 审查 F-1 后锚由「内联三元查表」改为「调用 todoStatusText()」—— 消费点
// 必须走自有键判定函数, 锚本身就是该纪律的机器判据。
const sandbox = (() => {
  const start = boardSrc.indexOf('const [, semToken, badgeCls]')
  if (start < 0) throw new Error('源码抽取失败: 找不到 semToken 解构行')
  const endMarker = 'const sem = todoStatusText(semToken, cur.status, t)'
  const end = boardSrc.indexOf(endMarker, start)
  if (end < 0) throw new Error('源码抽取失败: 找不到 sem 解析行')
  const body = boardSrc.slice(start, end + endMarker.length)
  const ctx = vm.createContext({ TODO_STATUS_TEXT_KEYS })
  vm.runInContext(
    extractFn(boardSrc, 'todoStatusTextKey') + '\n' +
    extractFn(boardSrc, 'todoStatusText') + '\n' +
    'globalThis.__resolve = (cur, t) => {\n' + body + '\nreturn { sem, badgeCls }\n}' +
    '\nglobalThis.__api = { todoStatusTextKey, todoStatusText }',
    ctx)
  return ctx
})()
const resolveSem = sandbox.__resolve
const { todoStatusTextKey, todoStatusText } = sandbox.__api

// 契约键集源 (issue #154 改造): 旧实现经 Python 子进程 import mw.console
// 拿运行时 _TODO_STATUS_SEM, 使 npm test 与 Python 环境耦合 ⇒ 改为直读契约
// JSON (跨产物断言: 前端源码派生表 vs 契约文件, 两个不同产物, 不是同源自证);
// 后端运行时 `_TODO_STATUS_SEM` 的运行时断言由 tests/test_issue124_status_alias.py
// 接管 (pytest 侧 import 断言), 后端派生形态由本文件 T8 静态断言接管 ——
// 「后端派生写错」仍被 pytest 面抓住, 「前端派生写错」仍被本文件 T1/T2 抓住。
function backendSemKeys() {
  // 键集 = 契约 JSON statuses 键集 (与后端 _load_todo_status_contract 同源)
  const sem = contract.statuses
  // badge 字段形状顺带钉住 (旧版对运行时三元组第三位的检查, 直读后落到
  // 契约 JSON 的 badge 字段上; 运行时三元组断言移交 pytest 侧, 见上方注释)
  for (const [k, d] of Object.entries(sem)) {
    assert(typeof d.badge === 'string' && d.badge.startsWith('badge-'),
      `契约 statuses.${k}.badge 形状破 (应以 badge- 开头): ${JSON.stringify(d)}`)
  }
  return Object.keys(sem)
}

const CANONICAL = ['blocked', 'completed', 'failed', 'waiting_sub', 'working']

// ═════════════════ T1: 映射表五态齐全, 值是词条键非展示文案 ═════════════════
test('T1: TODO_STATUS_TEXT_KEYS 五态齐全且值为 tboard.todo_status_* 词条键', () => {
  eq(Object.keys(TODO_STATUS_TEXT_KEYS).sort(), CANONICAL, '映射表键集')
  for (const [k, v] of Object.entries(TODO_STATUS_TEXT_KEYS)) {
    assert(/^tboard\.todo_status_[a-z_]+$/.test(v),
      `${k} 的值应是 tboard.todo_status_* 词条键 (机器值), 实得 ${JSON.stringify(v)}`)
    assert(!/[\u4e00-\u9fff]/.test(v),
      `${k} 的值是词条键不是展示文案, 不应含中文: ${v}`)
  }
})

// ═════════════════ T2: 跨层契约 —— 映射键集 === 后端 _TODO_STATUS_SEM 键集 ═════
test('T2: 映射表键集 === 契约 JSON statuses 键集 (跨产物契约; 后端运行时键集由 pytest test_issue124 钉住)', () => {
  const backend = backendSemKeys().sort()
  eq(backend, CANONICAL, '后端 _TODO_STATUS_SEM 键集')
  eq(Object.keys(TODO_STATUS_TEXT_KEYS).sort(), backend,
    '后端新增态而前端无映射 ⇒ chip 显示裸 token, 契约破')
})

// ═════════════════ T3: locale 双语五键 + 逐字零回归/定稿锚 ═════════════════
test('T3: tboard.todo_status_* 双语齐全, zh 逐字零回归 / en 逐字定稿', () => {
  const zhExpect = {
    working: '执行中', blocked: '等你回复', completed: '已完成',
    failed: '已失败', waiting_sub: '子任务处理中',
  }
  const enExpect = {
    working: 'Running', blocked: 'Needs your reply', completed: 'Completed',
    failed: 'Failed', waiting_sub: 'Subagent running',
  }
  for (const [k, expectKey] of Object.entries(TODO_STATUS_TEXT_KEYS)) {
    const leaf = expectKey.split('.')[1] // 'todo_status_working' 等
    assert(typeof zh.tboard[leaf] === 'string' && zh.tboard[leaf].length > 0,
      `zh 缺键 tboard.${leaf}`)
    assert(typeof en.tboard[leaf] === 'string' && en.tboard[leaf].length > 0,
      `en 缺键 tboard.${leaf}`)
    eq(zh.tboard[leaf], zhExpect[k], `zh tboard.${leaf} 与改造前逐字一致`)
    eq(en.tboard[leaf], enExpect[k], `en tboard.${leaf} 按 s1 §5.2 定稿逐字`)
  }
})

// ═════════════════ T4: 渲染分支 —— 命中走 t(), 未命中原样透传 ═════════════════
test('T4: token 命中映射走 t(); fallback 自由文本原样透传不被吞成排队中', () => {
  const t = (key) => `T:${key}` // 记录调用路径的 stub
  // 命中: 五态 token 全部走词条
  for (const k of CANONICAL) {
    const { sem } = resolveSem({ status: 'x', status_sem: ['running', k, 'badge-blue'] }, t)
    eq(sem, `T:tboard.todo_status_${k}`, `token ${k} 应查表渲染`)
  }
  // 未命中 (fallback 分支第二位 = 原始状态文本): 原样透传
  const raw = resolveSem({ status: 'x', status_sem: ['pending', '自定义状态', 'badge-slate'] }, t)
  eq(raw.sem, '自定义状态', 'fallback 必须透传原始文本')
  assert(!raw.sem.includes('排队中') && raw.sem !== 'T:tboard.state_pending',
    `未知状态被吞成「排队中」(AC-146-6 破): ${raw.sem}`)
  // 老后端降级: 第二位是中文文案 (改造前形态) ⇒ 透传中文原文
  const legacy = resolveSem({ status: 'x', status_sem: ['running', '执行中', 'badge-blue'] }, t)
  eq(legacy.sem, '执行中', '新前端 + 老后端 = 完美降级 (透传中文原文)')
  // status_sem 缺失: 回落 cur.status, 再回落 '?'
  eq(resolveSem({ status: 'weird' }, t).sem, 'weird', '无 status_sem 回落 cur.status')
  eq(resolveSem({}, t).sem, '?', '两者皆缺失落 ?')
  // 徽章类名不受影响 (第三位契约不变)
  eq(resolveSem({ status_sem: ['running', 'working', 'badge-blue'] }, t).badgeCls,
    'badge-blue', 'badgeCls 仍取第三位')
})

// ═════════════════ T5: 审查 F-1 —— 原型链键防御 ═════════════════
// F-1 症状: TODO_STATUS_TEXT_KEYS 是普通对象字面量 ⇒ 继承 Object.prototype;
// 旧的真值查表 `TODO_STATUS_TEXT_KEYS[semToken]` 对 'constructor' 等原型链键
// **truthy** ⇒ 走 t() 分支 ⇒ chip 上渲染出 `function Object() { [native code] }`
// 这种函数源码字符串 (后端 fallback 分支会把 TODO.md 的原始状态文本原样下发,
// 后端侧不构成防线 —— normalize_status 对未识别值返回 None)。
const PROTO_KEYS = ['constructor', 'toString', '__proto__', 'valueOf',
                    'hasOwnProperty', 'isPrototypeOf',
                    'propertyIsEnumerable', 'toLocaleString']
test('T5: 原型链键不得命中 TODO_STATUS_TEXT_KEYS (审查 F-1)', () => {
  for (const k of PROTO_KEYS) {
    // ① 自有键判定函数: 原型链键必须返回 null (不命中映射表)
    eq(todoStatusTextKey(k), null, `原型链键 ${k} 不得命中映射表`)
    // ② chip 文案解析: 必须原样透传该键 (fallback 分支语义),
    //    **不得**渲染成函数源码字符串
    const t = (key) => `T:${key}`
    const sem = todoStatusText(k, 'x', t)
    eq(sem, k, `原型链键 ${k} 应原样透传`)
    assert(typeof sem === 'string', `原型链键 ${k} 的 chip 文案必须是字符串`)
    assert(!/native code|function Object|\[object Object\]/.test(sem),
      `原型链键 ${k} 渲染出了函数/对象源码: ${sem}`)
    // ③ 渲染链路 (CurrentBlock) 同口径: 不得走 t() 分支
    const r = resolveSem({ status: 'x', status_sem: ['pending', k, 'badge-slate'] }, t)
    eq(r.sem, k, `渲染链路下原型链键 ${k} 应原样透传`)
    assert(!String(r.sem).includes('native code'),
      `渲染链路下原型链键 ${k} 渲染出了函数源码: ${r.sem}`)
  }
})

// ═════════════════ T6: 审查 F-1 —— 坏形状防御 ═════════════════
// hasOwnProperty.call(TODO_STATUS_TEXT_KEYS, ['working']) === true (数组被
// String() 强制转成 'working' 命中自有键) ⇒ 只判自有键不判类型会放行数组。
test('T6: 坏形状 (数组/对象/数字) 不得因字符串强制转换被当成合法状态 (审查 F-1)', () => {
  for (const bad of [['working'], ['completed'], ['waiting_sub']]) {
    eq(todoStatusTextKey(bad), null, `数组 ${JSON.stringify(bad)} 不得命中映射表`)
  }
  for (const bad of [{}, 123, true, null, undefined, Symbol('x')]) {
    eq(todoStatusTextKey(bad), null, `坏形状 ${String(bad)} 不得命中映射表`)
  }
  // chip 文案: 坏形状不得被渲染成映射词条 (不得出现 T: 前缀)
  const t = (key) => `T:${key}`
  for (const bad of [['working'], {}, 123]) {
    const sem = todoStatusText(bad, 'weird', t)
    assert(!String(sem).startsWith('T:'),
      `坏形状 ${JSON.stringify(bad)} 竟走了 t() 分支: ${sem}`)
    assert(typeof sem === 'string', `坏形状 chip 文案必须是字符串, 实得 ${typeof sem}`)
  }
})

// ═════════════════ T7: 审查 F-1 —— 防回退源码扫描 ═════════════════
// 行为级断言 (T5/T6) 证「现在对」; 源码扫描证「不会悄悄回退成 truthy 查表」
// —— 即映射表的下标查表只允许出现在 todoStatusTextKey 内部。
test('T7: 映射表的下标查表只允许在 todoStatusTextKey 内 (审查 F-1 防回退)', () => {
  // 自有键判定必须真的存在 (不许退化成真值查表)
  assert(/Object\.prototype\.hasOwnProperty\.call\(TODO_STATUS_TEXT_KEYS,\s*token\)/.test(boardSrc),
    'todoStatusTextKey 必须用 Object.prototype.hasOwnProperty.call 做自有键判定')
  // typeof 守卫必须真的存在 (挡住数组等被 String() 强制转换的坏形状)
  assert(/typeof token !== 'string'/.test(boardSrc),
    'todoStatusTextKey 必须有 typeof 守卫')
  // 源码扫描: TODO_STATUS_TEXT_KEYS[...] 的下标查表只许出现在判定函数体内
  const fnSrc = extractFn(boardSrc, 'todoStatusTextKey')
  const lookupRe = /TODO_STATUS_TEXT_KEYS\s*\[/g
  const hits = boardSrc.match(lookupRe) || []
  const inFn = (fnSrc.match(lookupRe) || []).length
  eq(hits.length, inFn,
    `TODO_STATUS_TEXT_KEYS 的下标查表只允许出现在 todoStatusTextKey 内 (实得 ${hits.length} 处, 函数内 ${inFn} 处)`)
  // 消费点必须走判定函数, 不得自己查表
  assert(/const sem = todoStatusText\(semToken, cur\.status, t\)/.test(boardSrc),
    'CurrentBlock 的消费点必须调用 todoStatusText(), 不得内联查表')
})

// ═════════════════ T8: issue #149 F-2 —— 单一真相源防回退源码形态断言 ═════════════════
// 行为级断言 (T1/T2) 证「现在对」; 本测试钉「不会悄悄回退成两份字面量」。
// 三条断言各自独立可归因 (MV-149-F2-b/c 分别打中 A/B):
//   断言 A: 前端映射表必须由真相源 JSON 派生 (import + Object.keys 派生式);
//   断言 B: 后端 _TODO_STATUS_SEM 必须由该 JSON 的推导式派生 (不得退回
//           字面量字典 —— 双份真相源正是 #149 F-2 要治的缺陷);
//   断言 C: 真相源 JSON 只管状态语义 (family/badge), 不得混入前端词条键。
test('T8: 前后端映射必须由 todo_status_contract.json 派生 (issue #149 F-2 防回退)', () => {
  // ── 断言 A: 前端派生形态在位 ──
  assert(/import\s+TODO_STATUS_CONTRACT\s+from\s+'[^']*todo_status_contract\.json'/.test(boardSrc),
    '断言 A: 前端必须 import 真相源 todo_status_contract.json (派生形态)')
  assert(/const\s+TODO_STATUS_TEXT_KEYS\s*=\s*Object\.fromEntries\(/.test(boardSrc) &&
    /Object\.keys\(TODO_STATUS_CONTRACT\.statuses\)/.test(boardSrc),
    '断言 A: TODO_STATUS_TEXT_KEYS 必须由 Object.keys(TODO_STATUS_CONTRACT.statuses) 派生')
  // ── 断言 B: 后端派生形态在位 (推导式, 不是字面量字典) ──
  assert(/_load_todo_status_contract/.test(consoleSrc) &&
    /for\s+_tok,\s*_d\s+in\s+_load_todo_status_contract\(\)\.items\(\)/.test(consoleSrc),
    '断言 B: 后端 _TODO_STATUS_SEM 必须由 _load_todo_status_contract() 推导式派生')
  assert(!/_TODO_STATUS_SEM\s*=\s*\{\s*\n\s*"[a-z_]+":\s*\(/.test(consoleSrc),
    '断言 B: 后端 _TODO_STATUS_SEM 不得退回字面量字典 (双份真相源回退)')
  // ── 断言 C: 真相源边界清晰 (只管状态语义, 不管前端文案) ──
  eq(Object.keys(contract), ['statuses'], '真相源顶层只允许 statuses')
  for (const [tok, d] of Object.entries(contract.statuses)) {
    eq(Object.keys(d).sort(), ['badge', 'family'],
      `真相源 statuses.${tok} 只允许 family/badge 两字段`)
    assert(!/^tboard\.|todo_status_/.test(tok) &&
      !Object.values(d).some((v) => /tboard\.|todo_status_/.test(String(v))),
      `真相源不得混入前端词条键: ${tok}`)
  }
})

if (failed) { console.error(`\n${failed} test(s) failed`); process.exit(1) }
console.log('\nall issue#146 frontend i18n tests passed')
