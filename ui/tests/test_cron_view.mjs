// cron 定时任务视图前端测试 (s4 / 卡 t_da1c3a64)
//
// 依据: docs/UX-CRON-20261027.md §9 交接要点 + docs/PLAN-CRON-20261027.md §6.1
//       (以上两份是**过程文档**, 位于任务 workspace docs/, 不入仓 —— 干净克隆
//        里无此路径; 本文件注释自含覆盖点, 不依赖它们才能读懂)
//
// 覆盖:
//   CT7  操作可见性纯函数 cronActions(state): 四 state × 三操作矩阵逐格断言
//        (与需求 §3.1 可见性矩阵一致; 未知/非字符串 → [])
//   CT8  渲染分支: loading/空/错误/正常四态分支存在; 错误态含重试;
//        坏形状防御 (MV1): jobs 非数组 → 错误态; 单条 state 非字符串 → 未知徽标 + 无按钮
//   CT8  #134 runs 词条键归一: cronRunStatusKey 白名单四值原样, 白名单外 (含坏形状)
//        一律 'unknown'; 两处调用点均走归一, 不再裸拼 `cron.run_status.${...}`
//   #142 原型链键防御: cronRunBadge/cronStateBadge/cronStateKey 对原型链键归一到
//        安全类名/安全词条键 (且返回类型必须是字符串); cronRunStatusKey 白名单由
//        RUN_BADGE 自有键派生 (单一真相源, 源码扫描钉死不得回退字面量白名单)
//   #148 OBS-1 显式源码形态断言: cronRunStatusKey 的 Set.has 与 typeof 守卫
//        二者并存 (删任一道防线即红; MV-148-B 行为等价变异下行为断言不红而
//        本形态断言红 —— 这是它有牙齿的判据)
//   CT9  i18n 键集两侧一致 (脚本比对 locale JSON)
//   #136 runs 展开连点竞态: onToggleRuns 代次比对 (源码锚) + 乱序响应行为级
//        (陈旧响应被丢弃, 终态恒等于最后一次操作) + 变异反证锚 (去掉比对即破)
//   MV2  可见性回归反证: scheduled 的 trigger 被删 → CT7 必须红 (锚的实现)
//   MV3  桥协议回归: 后端超时 → 503 映射存在 (后端锚, 见 tests/test_cron_api.py)
//
// 风格: 沿用仓库既有 ui/tests/*.mjs —— 纯源码/纯函数断言, 不起浏览器。
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import vm from 'node:vm'

const here = path.dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(path.join(here, f), 'utf8')
const cronSrc = read('../src/views/CronView.jsx')
const appSrc = read('../src/App.jsx')
const apiSrc = read('../src/api.js')

let failed = 0
function test(n, f) { try { f(); console.log(`ok - ${n}`) } catch (e) { failed++; console.error(`FAIL - ${n}\n  ${e.message}`) } }
function assert(c, m) { if (!c) throw new Error(m) }
function eq(a, b, m) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b)
  if (sa !== sb) throw new Error(`${m || ''} 期望 ${sb}, 实得 ${sa}`)
}
// 异步用例队列 (含 await 的行为级用例): 文件末尾统一 await 执行,
// 保证「ok/FAIL」打印顺序稳定, 且失败能计入 failed 退出码。
const asyncTests = []
function atest(n, f) { asyncTests.push([n, f]) }

// ── 从 CronView.jsx 抽取纯函数 (源码执行, 不是复制一份) ────────────
// 抽取方式与既有 test_issue87_s1_deadbranch.mjs 同款: 按函数名定位,
// 大括号配平截取函数体, 在 vm 沙箱里跑。
function extractFn(src, name) {
  const idx = src.indexOf(`function ${name}(`)
  if (idx < 0) throw new Error(`源码抽取失败: 找不到 ${name}`)
  let braces = 0, seen = false
  for (let i = idx; i < src.length; i++) {
    const ch = src[i]
    if (ch === '{') { braces++; seen = true } else if (ch === '}') braces--
    if (seen && braces === 0) return src.slice(idx, i + 1)
  }
  throw new Error(`源码抽取失败: ${name} 大括号未配平`)
}

const ctx = vm.createContext({ console })
const fns = ['cronActions', 'cronIsTerminal', 'cronStateBadge', 'cronStateKey',
             'cronRunBadge', 'cronRunStatusKey', 'cronJobsValid', 'cronJobStateSafe']
// 模块级常量 (徽标映射表) 必须一起进沙箱, 否则函数引用不到。
const consts = ['STATE_BADGE', 'RUN_BADGE'].map((n) => {
  const idx = cronSrc.indexOf(`const ${n} = {`)
  if (idx < 0) throw new Error(`源码抽取失败: 找不到常量 ${n}`)
  let braces = 0, seen = false
  for (let i = cronSrc.indexOf('{', idx); i < cronSrc.length; i++) {
    const ch = cronSrc[i]
    if (ch === '{') { braces++; seen = true } else if (ch === '}') braces--
    if (seen && braces === 0) return cronSrc.slice(idx, i + 1)
  }
  throw new Error(`源码抽取失败: 常量 ${n} 大括号未配平`)
}).join('\n')
// #142: cronRunStatusKey 的白名单是模块级派生常量 (非对象字面量, 上面的
// 大括号配平抽取不适用), 按整行原样抽进沙箱 —— 仍是执行源码, 不是复制逻辑。
const runStatusKeysDecl = (cronSrc.match(/^const RUN_STATUS_KEYS = .*$/m) || [null])[0]
if (!runStatusKeysDecl) throw new Error('源码抽取失败: 找不到常量 RUN_STATUS_KEYS')
const code = consts + '\n' + runStatusKeysDecl + '\n' + fns.map((n) => extractFn(cronSrc, n)).join('\n')
vm.runInContext(code + `\nglobalThis.__e = { ${fns.join(', ')} }`, ctx)
const F = ctx.__e

// ══════════════════════════════════════════════════════════════════
// CT7: 操作可见性矩阵逐格断言 (需求 §3.1 / UX §4.1)
// ══════════════════════════════════════════════════════════════════
test('CT7 scheduled → [立即运行, 暂停]', () => {
  eq(F.cronActions('scheduled'), ['trigger', 'pause'], 'scheduled 可见性')
})
test('CT7 paused → [恢复]', () => {
  eq(F.cronActions('paused'), ['resume'], 'paused 可见性')
})
test('CT7 completed → [] (终态引导文案, 无按钮)', () => {
  eq(F.cronActions('completed'), [], 'completed 可见性')
})
test('CT7 error → [] (终态引导文案, 无按钮)', () => {
  eq(F.cronActions('error'), [], 'error 可见性')
})
test('CT7 未知/非字符串 state → [] (坏形状不可操作)', () => {
  for (const bad of [undefined, null, 123, '', {}, [], true]) {
    eq(F.cronActions(bad), [], `坏形状 ${JSON.stringify(bad)} 可见性`)
  }
})
test('CT7 矩阵完整性: 恰好 4 个已知 state + 其余全空', () => {
  const known = ['scheduled', 'paused', 'completed', 'error']
  for (const s of known) {
    assert(Array.isArray(F.cronActions(s)), `${s} 可见性必须是数组`)
  }
  // 终态判定
  assert(F.cronIsTerminal('completed') === true, 'completed 应是终态')
  assert(F.cronIsTerminal('error') === true, 'error 应是终态')
  assert(F.cronIsTerminal('scheduled') === false, 'scheduled 非终态')
  assert(F.cronIsTerminal('paused') === false, 'paused 非终态')
})

// ══════════════════════════════════════════════════════════════════
// MV2: 可见性回归反证 —— 把 scheduled 的 trigger 删掉必须被 CT7 抓红
//      (防"悄悄削覆盖"; 本用例自身是实现锚的负控说明)
// ══════════════════════════════════════════════════════════════════
test('MV2 scheduled 必须保留 trigger (削覆盖立即变红)', () => {
  assert(F.cronActions('scheduled').includes('trigger'),
    'scheduled 的「立即运行」被删了 —— 可见性矩阵回退')
  assert(F.cronActions('scheduled').includes('pause'),
    'scheduled 的「暂停」被删了 —— 可见性矩阵回退')
  assert(!F.cronActions('paused').includes('trigger'),
    'paused 不该有「立即运行」(与矩阵冲突)')
})

// ══════════════════════════════════════════════════════════════════
// CT8: 渲染分支 + 坏形状防御 (MV1)
// ══════════════════════════════════════════════════════════════════
test('CT8 四态分支齐备: loading / error / ready+空 / ready+列表', () => {
  assert(cronSrc.includes("phase === 'loading'"), '缺 loading 分支')
  assert(cronSrc.includes("phase === 'error'"), '缺 error 分支')
  assert(cronSrc.includes('jobs.length === 0'), '缺空态分支')
  assert(cronSrc.includes('jobs.length > 0'), '缺列表分支')
})
test('CT8 错误态含重试按钮 (AC-V6)', () => {
  // 注: 'phase === error' 在源码里出现两次 (JSX 判定 + 错误分支的 setPhase),
  // 取**最后一个**索引定位真正的错误态渲染块。
  const errIdx = cronSrc.lastIndexOf("phase === 'error'")
  assert(errIdx > 0, '缺 error 分支')
  const seg = cronSrc.slice(errIdx, errIdx + 1200)
  assert(seg.includes('common.retry'), '错误态缺重试按钮')
  assert(seg.includes('load(true)'), '错误态重试未回到 loading 重拉')
})
test('CT8 加载态有骨架 + cron.loading 文案', () => {
  assert(cronSrc.includes('cron.loading'), '缺加载文案')
  assert(/\[0, 1, 2\]/.test(cronSrc), '缺骨架卡片')
})
test('CT8 空态含标题 + 引导语 (AC-V4)', () => {
  assert(cronSrc.includes('cron.empty.title'), '缺空态标题')
  assert(cronSrc.includes('cron.empty.hint'), '缺空态引导语')
})
test('MV1 jobs 非数组 → 错误态 (不白屏)', () => {
  eq(F.cronJobsValid({ jobs: null }), false, 'jobs=null 应非法')
  eq(F.cronJobsValid({ jobs: 'x' }), false, 'jobs=字符串应非法')
  eq(F.cronJobsValid({}), false, '缺 jobs 应非法')
  eq(F.cronJobsValid(null), false, 'null payload 应非法')
  eq(F.cronJobsValid({ jobs: [] }), true, '空数组应合法 (空态)')
  eq(F.cronJobsValid({ jobs: [{}] }), true, '数组应合法')
  // 视图里必须真的用这个校验落错误态
  assert(cronSrc.includes('cronJobsValid(data)'), '视图未使用坏形状校验')
})
test('MV1 单条 state 非字符串 → 未知态且无按钮', () => {
  eq(F.cronJobStateSafe({ state: 123 }), 'unknown', '数字 state 应降级')
  eq(F.cronJobStateSafe({}), 'unknown', '缺 state 应降级')
  eq(F.cronJobStateSafe(null), 'unknown', 'null job 应降级')
  eq(F.cronJobStateSafe('x'), 'unknown', '非对象 job 应降级')
  eq(F.cronJobStateSafe({ state: 'paused' }), 'paused', '合法 state 原样')
  // 未知态无按钮 (防误伤)
  eq(F.cronActions(F.cronJobStateSafe({ state: 123 })), [], '未知态应无按钮')
})
test('CT8 未知态徽标 = badge-unknown (坏形状不崩)', () => {
  eq(F.cronStateBadge('scheduled'), 'badge-green', 'scheduled 徽标')
  eq(F.cronStateBadge('paused'), 'badge-slate', 'paused 徽标')
  eq(F.cronStateBadge('completed'), 'badge-blue', 'completed 徽标')
  eq(F.cronStateBadge('error'), 'badge-red', 'error 徽标')
  eq(F.cronStateBadge('unknown'), 'badge-unknown', '未知徽标')
  eq(F.cronStateBadge(123), 'badge-unknown', '坏形状徽标')
  eq(F.cronStateKey(123), 'unknown', '坏形状词条键')
})
test('CT8 runs 状态徽标映射齐备', () => {
  eq(F.cronRunBadge('completed'), 'badge-green')
  eq(F.cronRunBadge('failed'), 'badge-red')
  eq(F.cronRunBadge('running'), 'badge-blue')
  eq(F.cronRunBadge('claimed'), 'badge-blue')
  eq(F.cronRunBadge('weird'), 'badge-unknown')
  eq(F.cronRunBadge(undefined), 'badge-unknown')
})
// ── #134: runs 词条键归一 (白名单外 → 'unknown', 不出裸键) ────────────
test('#134 cronRunStatusKey 白名单四值原样返回', () => {
  for (const s of ['claimed', 'running', 'completed', 'failed']) {
    eq(F.cronRunStatusKey(s), s, `${s} 应原样`)
  }
})
test('#134 cronRunStatusKey 白名单外 (含坏形状) → unknown', () => {
  // 脏值 (库演进/脏数据) 与坏形状 (null/空串/数字/对象) 一律归一。
  for (const bad of ['killed', 'aborted', 'SUCCESS', '', null, undefined,
                     0, 42, true, {}, [], ['completed']]) {
    eq(F.cronRunStatusKey(bad), 'unknown',
      `坏形状 ${JSON.stringify(bad)} 应归一 unknown`)
  }
  // 原型链上的属性名 (constructor/toString/...) 也不在白名单 ⇒ 一律 unknown。
  // (逐字比较而非对象查表, 故天然不受 Object.prototype 污染影响。)
  for (const bad of ['constructor', 'toString', '__proto__', 'valueOf', 'hasOwnProperty']) {
    eq(F.cronRunStatusKey(bad), 'unknown',
      `原型链键 ${bad} 应归一 unknown`)
  }
})
test('#134 两处调用点均走 cronRunStatusKey (不再裸拼 `|| unknown`)', () => {
  // 调用点 1: 最近执行状态 (L2 行); 调用点 2: runs 列表每行徽标。
  assert(cronSrc.includes('cron.run_status.${cronRunStatusKey(latest.status)}'),
    '调用点 1 (latest.status) 未走归一')
  assert(cronSrc.includes('cron.run_status.${cronRunStatusKey(r.status)}'),
    '调用点 2 (r.status) 未走归一')
  // 旧写法 (只兜 falsy, 脏值出裸键) 必须**完全**消失。
  assert(!cronSrc.includes("cron.run_status.${latest.status || 'unknown'}"),
    '调用点 1 仍用 `|| unknown` 旧写法 (脏值会渲染裸键)')
  assert(!cronSrc.includes("cron.run_status.${r.status || 'unknown'}"),
    '调用点 2 仍用 `|| unknown` 旧写法 (脏值会渲染裸键)')
})
test('#134 cron.run_status.unknown 词条两侧都在 (归一后必有译文)', () => {
  const zh = JSON.parse(read('../src/i18n/locales/zh-cn.json'))
  const en = JSON.parse(read('../src/i18n/locales/en.json'))
  assert(typeof zh.cron?.run_status?.unknown === 'string' &&
         zh.cron.run_status.unknown.length > 0, 'zh-cn 缺 cron.run_status.unknown')
  assert(typeof en.cron?.run_status?.unknown === 'string' &&
         en.cron.run_status.unknown.length > 0, 'en 缺 cron.run_status.unknown')
})

// ── #142: 原型链键防御 + 白名单单一来源 (AC-142-1/2/3/5) ────────────────
// N-1: 原型链键 × 三函数。issue 核心症状 = RUN_BADGE['constructor'] 返回
// **函数**灌进 className; 断言先钉返回**类型**是字符串, 再钉归一值。
test('#142 原型链键不得命中徽标表 (run/state 徽标 + state 词条键)', () => {
  const protoKeys = ['constructor', 'toString', '__proto__', 'valueOf',
                     'hasOwnProperty', 'isPrototypeOf',
                     'propertyIsEnumerable', 'toLocaleString']
  for (const k of protoKeys) {
    // 返回值必须是**字符串**, 不是函数/对象 (issue #142 的核心症状)
    const rb = F.cronRunBadge(k)
    assert(typeof rb === 'string', `cronRunBadge(${k}) 返回非字符串: ${typeof rb}`)
    eq(rb, 'badge-unknown', `cronRunBadge(${k}) 应为 badge-unknown`)
    const sb = F.cronStateBadge(k)
    assert(typeof sb === 'string', `cronStateBadge(${k}) 返回非字符串: ${typeof sb}`)
    eq(sb, 'badge-unknown', `cronStateBadge(${k}) 应为 badge-unknown`)
    const sk = F.cronStateKey(k)
    assert(typeof sk === 'string', `cronStateKey(${k}) 返回非字符串: ${typeof sk}`)
    eq(sk, 'unknown', `cronStateKey(${k}) 应为 unknown`)
  }
})
// N-2: hasOwnProperty.call 对数组做 String() 强制转换 (['completed'] → 'completed'
// 命中自有键) ⇒ 只判自有键不判类型会放行数组; typeof 守卫是必需的第二道闸。
test('#142 坏形状 (数组/对象) 不得因字符串强制转换被当成合法状态', () => {
  // hasOwnProperty.call(RUN_BADGE, ['completed']) === true (数组被 String()
  // 强制转成 'completed') ⇒ 只判自有键不判类型就会放行数组。
  for (const bad of [['completed'], ['running'], ['failed'], ['claimed']]) {
    eq(F.cronRunStatusKey(bad), 'unknown', `数组 ${JSON.stringify(bad)} 应归一 unknown`)
    eq(F.cronRunBadge(bad), 'badge-unknown', `数组 ${JSON.stringify(bad)} 应为 badge-unknown`)
  }
  eq(F.cronStateKey(['scheduled']), 'unknown', '数组应归一 unknown')
  eq(F.cronStateBadge(['scheduled']), 'badge-unknown', '数组应为 badge-unknown')
})
// N-3: AC-142-5 单一真相源。前两段证「行为对」, 第三段源码扫描证「没有第二份
// 真相源」—— 否则保留旧字面量白名单 + 派生集并存时行为照样全绿 (假绿)。
test('#142 cronRunStatusKey 的白名单 === RUN_BADGE 自有键集合 (单一真相源)', () => {
  // ★ 防「第二份真相源」回退: 代码里不得再出现「status 与字符串字面量的
  //    比较」构成的白名单。issue #149 F-7 放宽: 旧 regex 只认
  //    `status === 'completed' … 'failed'` 一种书写形态, 换等价写法即绕过;
  //    新判据扫**全文件**, `==`/`===` 与单/双引号都算, 命中**不同字面量**
  //    ≥2 个即判红 (阈值 2: 单点比较不构成白名单, 零误报优先)。
  //    已知边界 (登记 PLAN-I §5.3/§10 Q-B): 反引号模板字面量、变量中转、
  //    switch/case、单字面量白名单不在此 regex 覆盖面。
  //    本段置于行为断言**之前**: 变异时先在此归因 (形态扫描独立有牙齿,
  //    行为等价变异 MV-149-F7-x 也只有这段能拦)。
  const statusLiteralCmps = new Set(
    (cronSrc.match(/\bstatus\s*===?\s*(?:'[^']*'|"[^"]*")/g) || []))
  eq(statusLiteralCmps.size < 2, true,
    `cronRunStatusKey 不得退回硬编码字面量白名单 (双份真相源); 实得 ${statusLiteralCmps.size} 个不同字面量比较: ${[...statusLiteralCmps].join(' | ')}`)
  // RUN_BADGE 已随 consts 进沙箱 (与函数同源), 直接从沙箱取自有键, 不复制表。
  const runBadgeKeys = Object.keys(vm.runInContext('RUN_BADGE', ctx))
  const accepted = runBadgeKeys.filter((k) => F.cronRunStatusKey(k) === k)
  eq(accepted.length, runBadgeKeys.length,
    'RUN_BADGE 的自有键必须全部被 cronRunStatusKey 接受')
  // 反向: 白名单外一律 unknown (含原型链键)
  for (const k of ['killed', 'aborted', 'SUCCESS', 'constructor', '__proto__']) {
    eq(F.cronRunStatusKey(k), 'unknown', `${k} 应在白名单外`)
  }
  // 白名单必须是**派生**声明 (RUN_BADGE 自有键快照), 不是手写字面量集合
  assert(/new Set\(Object\.keys\(RUN_BADGE\)\)/.test(cronSrc),
    'cronRunStatusKey 白名单必须由 Object.keys(RUN_BADGE) 派生 (单一来源)')
})

// ── #148 OBS-1: 显式源码形态断言 —— Set.has + typeof 守卫**二者并存** ──────
// #142 的双保险: ① 白名单由 RUN_BADGE 自有键派生成 Set (Set.has 用
// SameValueZero, 不做字符串强制转换); ② typeof 守卫挡住非字符串坏形状。
// 行为级断言 (N-2/N-3) 在「删掉其中一道防线但行为仍等价」时可能不红
// (如 Set 派生 → hasOwnProperty 且保留 typeof 守卫 ⇒ 行为等价 ⇒ 行为断言
// 全绿) —— 本测试钉**源码形态**, 删任一道防线即红 (MV-148-A/B/B2/C)。
test('#148 OBS-1: cronRunStatusKey 的 Set.has 与 typeof 守卫必须并存 (源码形态钉死)', () => {
  const fn = extractFn(cronSrc, 'cronRunStatusKey')
  // A1: 白名单判定必须走 Set.has (不许退回 hasOwnProperty / in / 字面量)
  assert(/RUN_STATUS_KEYS\.has\(status\)/.test(fn),
    'cronRunStatusKey 函数体必须用 RUN_STATUS_KEYS.has(status) 判定白名单')
  // A2: typeof 守卫必须在场 (挡住数组等被 String() 强制转换的坏形状)
  assert(/typeof status !== 'string'/.test(fn),
    'cronRunStatusKey 函数体必须有 typeof status !== \'string\' 守卫')
  // A3: Set 必须是派生声明 (单一真相源, 不许手写字面量集合)
  assert(/const RUN_STATUS_KEYS = new Set\(Object\.keys\(RUN_BADGE\)\)/.test(cronSrc),
    'RUN_STATUS_KEYS 必须由 new Set(Object.keys(RUN_BADGE)) 派生')
  // 白名单判定不许被 hasOwnProperty 形态替换 (hasOwnProperty.call 对数组
  // String() 强制转换会放行 —— 正是 #142 治的缺陷)。判定范围限函数体:
  // cronRunBadge 用 hasOwnProperty 查 RUN_BADGE 是合法形态, 不在本锚范围。
  assert(!/hasOwnProperty/.test(fn),
    'cronRunStatusKey 函数体不得出现 hasOwnProperty (白名单判定不得替换 Set.has)')
})

// ── #136: runs 展开连点竞态 (源码锚 + 行为级乱序响应) ──────────────────
// 行为级做法: 把 onToggleRuns 的**真实实现**抽到 vm 沙箱里跑 (不是复制逻辑),
// 沙箱提供 openRuns (实时 getter) / setOpenRuns / api.cron.runs / runsGenRef ——
// 于是「展开→收起→再展开 + 乱序响应」能真的复现陈旧写竞态。
// 抽取方式: useCallback(...) 实参 = 箭头函数, 先对 useCallback 的括号配平,
// 再对箭头函数体的大括号配平。
function extractUseCallbackArrow(src, decl) {
  const idx = src.indexOf(decl)
  if (idx < 0) throw new Error(`源码抽取失败: 找不到 ${decl}`)
  const open = src.indexOf('(', idx)
  let depth = 0, seen = false, close = -1
  for (let i = open; i < src.length; i++) {
    const ch = src[i]
    if (ch === '(') { depth++; seen = true }
    else if (ch === ')') { depth--; if (seen && depth === 0) { close = i; break } }
  }
  if (close < 0) throw new Error(`源码抽取失败: ${decl} 括号未配平`)
  const args = src.slice(open + 1, close)
  const arrow = args.indexOf('=>')
  if (arrow < 0) throw new Error(`源码抽取失败: ${decl} 缺箭头函数`)
  const brace = args.indexOf('{', arrow)
  let braces = 0, seenB = false
  for (let j = brace; j < args.length; j++) {
    const ch = args[j]
    if (ch === '{') { braces++; seenB = true } else if (ch === '}') braces--
    if (seenB && braces === 0) return args.slice(0, j + 1)
  }
  throw new Error(`源码抽取失败: ${decl} 大括号未配平`)
}

// 沙箱 harness: state.openRuns 是唯一真源; api.cron.runs 挂起由用例决定
// 何时、以何顺序落地 —— 这是竞态复现的关键 (响应乱序)。
function makeRunsHarness(arrowSrc) {
  const state = { openRuns: {} }
  const pending = []
  const sandbox = {
    console,
    setOpenRuns: (u) => {
      state.openRuns = (typeof u === 'function') ? u(state.openRuns) : u
    },
    fmtApiError: (e) => String((e && e.message) || 'err'),
    t: (k) => k,
    api: {
      cron: {
        runs: (jobId, limit) => new Promise((resolve, reject) => {
          pending.push({ jobId, limit, resolve, reject })
        }),
      },
    },
    runsGenRef: { current: {} },
  }
  // openRuns 走 getter: 沙箱里的实现每次读到的都是**当前**状态
  // (等价于 React 每次 setState 后重渲染、回调拿到最新快照)。
  Object.defineProperty(sandbox, 'openRuns', {
    get: () => state.openRuns, enumerable: true, configurable: true,
  })
  const ctx = vm.createContext(sandbox)
  vm.runInContext(`globalThis.__toggle = ${arrowSrc}`, ctx)
  return {
    toggle: (jobId, force) => ctx.__toggle(jobId, force),
    get state() { return state.openRuns },
    get pending() { return pending },
    resolveAt: (i, data) => pending[i].resolve(data),
    rejectAt: (i, err) => pending[i].reject(err),
  }
}

const toggleArrow = extractUseCallbackArrow(cronSrc, 'const onToggleRuns = useCallback(')

test('#136 onToggleRuns 有 per-job 代次推进 + 两处回填前比对', () => {
  assert(cronSrc.includes('runsGenRef'), '缺 runsGenRef (per-job 代次容器)')
  assert(cronSrc.includes('const gen = (runsGenRef.current[jobId] || 0) + 1'),
    '缺代次推进语句')
  assert(cronSrc.includes('runsGenRef.current[jobId] = gen'), '缺代次写回')
  const guards = cronSrc.match(/runsGenRef\.current\[jobId\] !== gen/g) || []
  eq(guards.length, 2, '成功/失败两处回填都应比对代次')
})
test('#136 两处代次比对都先于各自的回填 (顺序锚)', () => {
  const seg = cronSrc.slice(cronSrc.indexOf('const onToggleRuns'))
  const g = 'runsGenRef.current[jobId] !== gen'
  const pos = []
  let i = -1
  while ((i = seg.indexOf(g, i + 1)) >= 0) pos.push(i)
  eq(pos.length, 2, '比对点应有两处')
  for (const p of pos) {
    const after = seg.slice(p, p + 400)
    assert(after.includes('return'), '比对后必须 return (丢弃陈旧响应)')
    assert(after.includes('setOpenRuns'), '比对点与回填不在同一分支内')
  }
})
atest('#136 展开→收起: 陈旧慢响应不得把已收起的面板重新撑开', async () => {
  const h = makeRunsHarness(toggleArrow)
  const p1 = h.toggle('j1')            // 展开: 请求 A 在飞
  await h.toggle('j1')                  // 收起: 不发请求, 但代次已推进
  eq(h.state.j1.open, false, '收起应立即生效')
  h.resolveAt(0, { runs: [{ id: 'rA' }] })   // 慢响应 A 此刻才落地
  await p1
  eq(h.state.j1.open, false, '陈旧响应把已收起的面板重新撑开了')
  eq((h.state.j1.runs || []).length, 0, '陈旧响应不得写入 runs')
})
atest('#136 展开→收起→再展开: 乱序响应下终态恒等于最后一次操作', async () => {
  const h = makeRunsHarness(toggleArrow)
  const pA = h.toggle('j1')             // 请求 A (gen 1)
  await h.toggle('j1')                  // 收起 (gen 2)
  const pB = h.toggle('j1')             // 再展开: 请求 B (gen 3)
  eq(h.state.j1.open, true, '再展开应生效')
  eq(h.state.j1.phase, 'loading', '再展开应先落 loading 态')
  // 乱序: 最新 (B) 先落地, 陈旧 (A) 后落地
  h.resolveAt(1, { runs: [{ id: 'new' }] })
  await pB
  h.resolveAt(0, { runs: [{ id: 'old' }] })
  await pA
  eq(h.state.j1.open, true, '终态应为展开')
  eq(h.state.j1.phase, 'ready', '终态应为 ready')
  eq((h.state.j1.runs || []).map((r) => r.id), ['new'], '陈旧响应覆盖了末次结果')
})
atest('#136 陈旧失败也不得覆盖末次操作的画面 (catch 分支同样比对代次)', async () => {
  const h = makeRunsHarness(toggleArrow)
  const pA = h.toggle('j1')             // 请求 A (gen 1) —— 稍后失败
  await h.toggle('j1')                  // 收起 (gen 2)
  const pB = h.toggle('j1')             // 再展开: 请求 B (gen 3)
  h.resolveAt(1, { runs: [{ id: 'new' }] })
  await pB
  h.rejectAt(0, new Error('boom'))      // 陈旧的失败晚到
  await pA
  eq(h.state.j1.phase, 'ready', '陈旧失败把末次成功的画面改成了错误态')
  eq((h.state.j1.runs || []).map((r) => r.id), ['new'], '陈旧失败污染了末次结果')
})
atest('#136 变异反证: 去掉代次比对的实现必被上述断言抓住 (锚有牙齿)', async () => {
  const mutant = toggleArrow.replace(
    /if \(runsGenRef\.current\[jobId\] !== gen\) return[^\n]*\n/g, '')
  assert(mutant !== toggleArrow, '变异体未生效 (源码形态与预期不符)')
  const h = makeRunsHarness(mutant)
  const pA = h.toggle('j1')
  await h.toggle('j1')
  const pB = h.toggle('j1')
  h.resolveAt(1, { runs: [{ id: 'new' }] })
  await pB
  h.resolveAt(0, { runs: [{ id: 'old' }] })
  await pA
  // 没有代次比对时, 陈旧响应会污染终态 —— 这正是断言要防的事故。
  eq((h.state.j1.runs || []).map((r) => r.id), ['old'],
    '变异体竟未被陈旧响应污染 —— 断言已失去牙齿')
})

test('D-2 窄屏操作区: flex-col + min-h-[44px], 桌面 md: 复原 (UX §6)', () => {
  // L4 操作区: <768px 纵向堆叠独立行, md: 起恢复同行 flex-wrap (桌面零变化)
  const seg = cronSrc.slice(cronSrc.indexOf('L4: 操作区'))
  assert(seg.includes('flex-col'), '窄屏操作区缺 flex-col 纵向堆叠')
  assert(seg.includes('md:flex-row') && seg.includes('md:flex-wrap'),
    '缺 md: 断点复原桌面布局')
  // 操作按钮 (含 runs 展开按钮) 窄屏触达高度 44px, 桌面复原原高
  const btns = seg.slice(0, seg.indexOf('runs 展开面板'))
  const hits = btns.match(/min-h-\[44px\] md:min-h-0/g) || []
  assert(hits.length >= 2, `操作按钮缺 min-h-[44px] md:min-h-0 (实得 ${hits.length} 处)`)
})

// ══════════════════════════════════════════════════════════════════
// 反馈流 / 诚实口径 (UX §4.2 / §4.3, PLAN §2.4)
// ══════════════════════════════════════════════════════════════════
test('反馈流: toast → 重拉列表 → 清 busy, 无乐观改写', () => {
  const seg = cronSrc.slice(cronSrc.indexOf('const onAction'))
  assert(seg.includes('setBusy'), '缺 busy 置位')
  assert(seg.includes('setToast'), '缺 toast 反馈')
  assert(seg.includes('await load(false)'), '操作后未重拉列表')
  assert(seg.includes('delete next[jobId]'), 'busy 未清理')
  // 禁止乐观改写 state: 视图里不得直接 setJobs 改 state
  assert(!/setJobs\([^)]*state\s*:/s.test(cronSrc), '疑似乐观改写 state')
})
test('404 走「任务已不存在」toast 并重拉列表', () => {
  const seg = cronSrc.slice(cronSrc.indexOf('const onAction'))
  assert(seg.includes('cron.toast.gone'), '缺 404 专用 toast')
  assert(seg.includes('e.status === 404'), '缺 404 分支判定')
})
test('操作失败 toast 走 fmtApiError 三态归因', () => {
  const seg = cronSrc.slice(cronSrc.indexOf('const onAction'))
  assert(seg.includes('cron.toast.failed'), '缺失败 toast')
  assert(seg.includes('fmtApiError(e, t)'), '失败文案未走 fmtApiError')
})
test('trigger 成功文案 = 「已排入下次调度」口径 (不承诺已开始执行)', () => {
  const zh = JSON.parse(read('../src/i18n/locales/zh-cn.json'))
  const en = JSON.parse(read('../src/i18n/locales/en.json'))
  eq(zh.cron.toast.triggered, '✓ 已排入下次调度', 'zh trigger toast 文案')
  eq(en.cron.toast.triggered, '✓ Queued for the next tick', 'en trigger toast 文案')
  assert(!/已开始执行|已运行|Started|Running now/.test(zh.cron.toast.triggered),
    'trigger toast 不得承诺已开始执行')
})
test('ticker 横幅仅 alive===false 时显示, 缺失/null 不显示', () => {
  assert(cronSrc.includes('ticker && ticker.alive === false'), '缺 ticker 判定')
  assert(cronSrc.includes('cron.ticker_down'), '缺 ticker 横幅文案')
})
test('轮询 20s + 文档可见 + busy 跳过本轮', () => {
  assert(cronSrc.includes('20000'), '缺 20s 轮询')
  assert(cronSrc.includes('document.hidden'), '缺可见性判定')
  assert(cronSrc.includes('busyRef'), '缺 busy 跳过轮询')
})
test('runs 展开状态跨轮询保持 (openRuns 按 job id 存)', () => {
  assert(cronSrc.includes('openRuns'), '缺 openRuns 状态')
  assert(cronSrc.includes('onToggleRuns'), '缺 runs 展开处理')
  assert(cronSrc.includes('cron.runs.empty'), '缺 runs 空态文案')
  assert(cronSrc.includes('cron.runs.retry'), '缺 runs 重试')
})

// ══════════════════════════════════════════════════════════════════
// 接线自检: NAV / api.js / 端点契约
// ══════════════════════════════════════════════════════════════════
test('NAV 含 cron 项, 位置在 workflows 之后、pixel-office 之前', () => {
  const navIdx = appSrc.indexOf("{ id: 'cron'")
  assert(navIdx > 0, 'NAV 缺 cron 项')
  const wfIdx = appSrc.indexOf("{ id: 'workflows'")
  const pxIdx = appSrc.indexOf("{ id: 'pixel-office'")
  assert(wfIdx < navIdx && navIdx < pxIdx, 'NAV cron 位置不对 (应在 workflows 后 / pixel-office 前)')
  assert(appSrc.includes("'cron':        'nav.cron'"), 'NAV_LABEL_KEYS 缺 cron')
})
test('NAV 图标为时钟 (heroicons outline clock path)', () => {
  const m = appSrc.match(/\{ id: 'cron',\s*icon: '([^']+)'/)
  assert(m, 'cron NAV 项缺 icon')
  assert(m[1].includes('M12 8v4l3 3'), `cron 图标非时钟 path: ${m[1]}`)
})
test('App.jsx 两处 view 分支都渲染 CronView (桌面 + 移动端)', () => {
  const count = (appSrc.match(/view === 'cron' && <CronView \/>/g) || []).length
  assert(count === 2, `CronView view 分支应出现 2 次 (桌面+移动端), 实得 ${count}`)
  assert(appSrc.includes("import CronView from './views/CronView.jsx'"), '缺 CronView import')
})
test('api.js cron 命名空间 5 个封装 + 端点标签用路径模板', () => {
  for (const fn of ['jobs:', 'runs:', 'pause:', 'resume:', 'trigger:']) {
    assert(apiSrc.includes(fn), `api.cron 缺 ${fn}`)
  }
  assert(apiSrc.includes("'/api/cron/jobs/:id/pause'"), 'pause 端点标签应为模板')
  assert(apiSrc.includes("'/api/cron/jobs/:id/resume'"), 'resume 端点标签应为模板')
  assert(apiSrc.includes("'/api/cron/jobs/:id/trigger'"), 'trigger 端点标签应为模板')
  assert(apiSrc.includes("'/api/cron/jobs/:id/runs'"), 'runs 端点标签应为模板')
  // 端点标签 (j() 的第二参) 必须是路径模板, 不得插值实际 id。
  // fetch 的 URL 插值 id 是正常的 (那是真实请求路径), 只查标签参数。
  const labels = [...apiSrc.matchAll(/j\(r, '([^']+)'\)/g)].map((m) => m[1])
  for (const l of labels.filter((x) => x.startsWith('/api/cron/'))) {
    assert(l.includes(':id') || !l.includes('/jobs/'),
      `端点标签不得插值实际 id: ${l}`)
  }
  const cronLabels = labels.filter((x) => x.startsWith('/api/cron/'))
  assert(cronLabels.length === 5, `cron 端点标签应 5 个, 实得 ${cronLabels.length}`)
})
test('api.js 不得改动既有 ApiError/net/j/fmtApiError (红线)', () => {
  // 这些符号必须仍在
  for (const sym of ['export class ApiError', 'const net =', 'const j = async',
                     'export const fmtApiError']) {
    assert(apiSrc.includes(sym), `api.js 丢了 ${sym}`)
  }
})

// ══════════════════════════════════════════════════════════════════
// CT9: i18n 键集两侧一致 (cron.* 子树 + nav.cron)
// ══════════════════════════════════════════════════════════════════
function flat(d, p = '') {
  return Object.entries(d).flatMap(([k, v]) =>
    (v && typeof v === 'object' && !Array.isArray(v))
      ? flat(v, p ? `${p}.${k}` : k) : [p ? `${p}.${k}` : p])
}
test('CT9 locale 键集完全一致 (cron 子树)', () => {
  const zh = JSON.parse(read('../src/i18n/locales/zh-cn.json'))
  const en = JSON.parse(read('../src/i18n/locales/en.json'))
  const fz = flat(zh), fe = flat(en)
  eq(fe.filter((k) => !fz.includes(k)), [], 'only-in-en')
  eq(fz.filter((k) => !fe.includes(k)), [], 'only-in-zh')
})
test('CT9 cron.* 必需词条两侧都在 (视图用到的每一个键)', () => {
  const zh = JSON.parse(read('../src/i18n/locales/zh-cn.json'))
  const en = JSON.parse(read('../src/i18n/locales/en.json'))
  const required = [
    'nav.cron', 'app.subtitle_cron',
    'cron.title', 'cron.loading', 'cron.never_run', 'cron.ticker_down',
    'cron.terminal_hint',
    'cron.state.scheduled', 'cron.state.paused', 'cron.state.completed',
    'cron.state.error', 'cron.state.unknown',
    'cron.action.trigger', 'cron.action.pause', 'cron.action.resume',
    'cron.toast.triggered', 'cron.toast.paused', 'cron.toast.resumed',
    'cron.toast.failed', 'cron.toast.gone',
    'cron.empty.title', 'cron.empty.hint', 'cron.err.title',
    'cron.runs.show', 'cron.runs.hide', 'cron.runs.loading', 'cron.runs.empty',
    'cron.runs.error', 'cron.runs.retry',
    'cron.run_status.completed', 'cron.run_status.failed',
    'cron.run_status.running', 'cron.run_status.claimed',
    'cron.run_status.unknown',
  ]
  const get = (o, k) => k.split('.').reduce((acc, part) => (acc ? acc[part] : undefined), o)
  for (const k of required) {
    assert(get(zh, k) !== undefined, `zh-cn 缺 ${k}`)
    assert(get(en, k) !== undefined, `en 缺 ${k}`)
  }
  // 视图里引用的 cron.* **字面量**键必须都在两侧存在 (源码级反查)。
  // 排除 (a) 动态拼接键 (含 $ 的模板串, 如 `cron.state.${...}`) ——
  //   那些由 cronStateKey 派生的字面量键已在 required 清单里逐个钉住;
  // (b) API 方法名 (api.cron.jobs / api.cron.runs / api.cron.pause ...) ——
  //   它们不是 i18n 键。正则要求 cron. 后跟至少两段 (子树层级)。
  const used = [...cronSrc.matchAll(/cron\.[a-zA-Z_]+\.[a-zA-Z_]+/g)].map((m) => m[0])
  const apiCalls = new Set(['cron.jobs', 'cron.runs', 'cron.pause',
                            'cron.resume', 'cron.trigger'])
  for (const k of new Set(used)) {
    if (apiCalls.has(k)) continue
    if (k.includes('$')) continue
    assert(get(zh, k) !== undefined, `zh-cn 缺源码引用的 ${k}`)
    assert(get(en, k) !== undefined, `en 缺源码引用的 ${k}`)
  }
})
test('CT9 toast.failed 带 {err} 占位符 (两侧一致)', () => {
  const zh = JSON.parse(read('../src/i18n/locales/zh-cn.json'))
  const en = JSON.parse(read('../src/i18n/locales/en.json'))
  assert(zh.cron.toast.failed.includes('{err}'), 'zh failed toast 缺 {err}')
  assert(en.cron.toast.failed.includes('{err}'), 'en failed toast 缺 {err}')
})
test('CT9 复用既有 common.refresh / common.retry (不重复建键)', () => {
  const zh = JSON.parse(read('../src/i18n/locales/zh-cn.json'))
  assert(zh.common.refresh, 'common.refresh 应存在 (视图复用)')
  assert(zh.common.retry, 'common.retry 应存在 (视图复用)')
  assert(!zh.cron.refresh, 'cron.refresh 不该重复建 (应复用 common.refresh)')
  assert(!zh.cron.retry, 'cron.retry 不该重复建 (应复用 common.retry)')
})

// ══════════════════════════════════════════════════════════════════
// MV3: 桥协议回归锚 (后端超时 → 503 映射存在)
// ══════════════════════════════════════════════════════════════════
test('MV3 后端超时 → 503 映射仍在 (跨仓锚)', () => {
  const consoleSrc = read('../../mw/console.py')
  assert(consoleSrc.includes('_sp.TimeoutExpired'), '后端缺 TimeoutExpired 捕获')
  assert(consoleSrc.includes('cron 数据源不可用'), '后端缺 503 固定文案')
})

// ══════════════════════════════════════════════════════════════════
// 异步用例收尾: 统一 await 执行 (#136 行为级用例), 失败计入退出码
// ══════════════════════════════════════════════════════════════════
for (const [n, f] of asyncTests) {
  try { await f(); console.log(`ok - ${n}`) }
  catch (e) { failed++; console.error(`FAIL - ${n}\n  ${e.message}`) }
}

if (failed) {
  console.error(`\n${failed} 个用例失败`)
  process.exit(1)
}
console.log('\n全部通过')
