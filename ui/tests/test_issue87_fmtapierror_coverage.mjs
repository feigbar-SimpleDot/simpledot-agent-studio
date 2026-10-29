// issue #87 S-2 — fmtApiError 覆盖面静态回归测试。
//
// 【计数口径 (PLAN-87-86 §3.2 定稿 + PLAN-88-89 §4.2 加固, 下游勿改)】
//  * 「调用点」= 源码中【非注释行】里出现 `fmtApiError(` 的**行数**。
//    **不含 import 行** —— import 写作 `import { api, fmtApiError } from '../api.js'`,
//    左括号不在该行, 故天然不被计入; 口径与 `grep -c 'fmtApiError('` 逐字一致。
//    **不含整行注释** (PLAN-88-89 §4.2 加固): 计数前把整行是注释的行替换成空串
//    (保持行号对齐, 便于报错定位), 再统计 —— 堵住「删一处真调用点 + 加一行含
//    fmtApiError( 的注释」凑数的逃逸。
//    本基线 (7ffbb9d) 实测: WorkersView 7 + TasksBoardView 9 = 16; 加固前后同值
//    (逐行命中集合 set 差空 ⇒ 精确锚 16 零漂移)。
//  * 「残留」= `e.message || e` / `e?.message || e` 形态的行数, 判前先做**边界保留
//    空白归一** (squash, issue #90 修复): 只把「标识符字符之间」的空白折叠成哨兵
//    \0, 其余空白直接删。旧版全删空白会让 `return e.message||e` 这类**关键字紧贴**
//    形态把关键字尾字符与标识符焊成连续 \w 串, 正则前置边界 [^\w$] 不成立 ⇒ 漏检
//    (issue #90, 负控矩阵 S2-f 钉桩); 折叠成 \0 既堵住 `e . message || e` 改写逃逸
//    (见 PLAN §3.3 R4), 又保住 `b c`→`b\0c` 不融合出假词 (负控 S2-h 钉桩)。
//    判前同样**排除整行注释** (issue #92 对称化): 与 countCalls 同一条注释剥离,
//    堵「示例注释含 x.message||x 使 S2-b 假红」的过检方向 (REVIEW-88-89 §八 建议2)。
//    变量名**不限** `e` (PLAN-88-89 §4.1): 捕获组 + 反向引用匹配「同一标识符的
//    `.message || 自身`」, 故 `err.message || err` 这类改写形态也检得出。
//  * 阈值同时钉「精确 16」与 issue #87 原文的「>= 13」: 精确锚保证覆盖度不被悄悄削掉,
//    >=13 保留 issue 原文语义 (13 = #84 两 commit 新纳入三态归因的 catch 点数 6+7)。
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(path.join(here, f), 'utf8')
const workers = read('../src/views/WorkersView.jsx')
const board   = read('../src/views/TasksBoardView.jsx')

const squash = (s) => s.replace(/([\w$])\s+(?=[\w$])/g, '$1\0').replace(/\s+/g, '')
// 整行注释剥离 (#92 对称化): countCalls 与 countResidual 共用同一条口径 ——
// 注释行替换为空串 (保持行号对齐, 便于报错定位)。历史: 旧版只有 countCalls 剥注释,
// 残留侧不剥 ⇒ 新增一行含 `x.message||x` 的示例注释会让 S2-b 假红 (REVIEW-88-89 §八 建议2)。
const stripLineComments = (s) => s.split('\n').map((l) => (/^\s*(\/\/|\/\*|\*)/.test(l) ? '' : l))
// 计数口径 (PLAN-88-89 §4.2 加固): 「调用点」= 源码中【非注释行】里出现
// `fmtApiError(` 的行数。注释行先替换为空串 (保持行号对齐, 便于报错定位),
// 再统计 —— 故 countCalls 的返回值与 grep -n 'fmtApiError(' 逐行可对照。
// 现状 (570b712) 实测: WorkersView 7 + TasksBoardView 9 = 16, 加固前后同值。
const countCalls = (s) => stripLineComments(s)
  .filter((l) => l.includes('fmtApiError(')).length
// 残留正则 (PLAN-88-89 §4.1): 捕获组 + 反向引用 ⇒ 匹配「同一标识符的 `.message || 自身`」,
// 变量名任意 (e / err / ex / _e / e1), 不再焊死字面量 e; 前缀 (?:^|[^\\w$]) 保证
// 捕获到的是标识符起点; \\?? 同时覆盖 x.message 与 x?.message; 结尾 \\b 避免
// `err.message || err2` 误报; 边界保留 squash() ⇒ `e . message || e` 仍被堵住,
// 且关键字紧贴形态 (`return e.message||e` 等, issue #90) 因哨兵 \0 保住边界而检出。
const RESIDUAL = /(?:^|[^\w$])([A-Za-z_$][\w$]*)\??\.message\|\|\1\b/
const countResidual = (s) => stripLineComments(s)
  .filter((l) => RESIDUAL.test(squash(l))).length

const W = countCalls(workers), B = countCalls(board), TOTAL = W + B
const RW = countResidual(workers), RB = countResidual(board)

let failed = 0
function test(n, f) { try { f(); console.log(`ok - ${n}`) } catch (e) { failed++; console.error(`FAIL - ${n}\n  ${e.message}`) } }
function assert(c, m) { if (!c) throw new Error(m) }
// #144: 集合恒等断言用的深比较 (JSON 逐字), 与 test_cron_view.mjs 同款口径。
function eq(a, b, m) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b)
  if (sa !== sb) throw new Error(`${m || ''} 期望 ${sb}, 实得 ${sa}`)
}

test('S2-a WorkersView 无裸 e.message||e 残留 (issue #84 全覆盖口径)', () => {
  assert(RW === 0, `期望 0, 实际 ${RW}`)
})
test('S2-b TasksBoardView 残留恰为 2 (B 批裁决保留的 fail-soft: [1410,1421])', () => {
  assert(RB === 2, `期望 2, 实际 ${RB}`)
})
test('S2-c 调用点合计 >= 13 (issue #87 原文阈值)', () => {
  assert(TOTAL >= 13, `期望 >=13, 实际 ${TOTAL} (WorkersView ${W} + TasksBoardView ${B})`)
})
test('S2-d 调用点精确锚 16 —— 覆盖度不得被悄悄削掉 (少一处即红)', () => {
  assert(TOTAL === 16, `期望 16, 实际 ${TOTAL} (WorkersView ${W} + TasksBoardView ${B})`)
})
test('S2-e 两文件都仍在 import fmtApiError (口径前提: 调用点存在)', () => {
  for (const [n, s] of [['WorkersView', workers], ['TasksBoardView', board]])
    assert(/^import\s*\{[^}]*\bfmtApiError\b[^}]*\}/m.test(s), `${n} 未 import fmtApiError`)
})

// ---- issue #90: 关键字紧贴形态负控矩阵 (钉住边界保留 squash, 回退即红) ----
// 旧版 squash 全删空白 ⇒ 关键字尾字符与标识符 e 焊成连续 \w 串 ⇒ 前置边界
// [^\w$] 不成立 ⇒ 新旧正则同漏 (issue #90)。S2-f/S2-g 是对该漏检的独有覆盖:
// 把 squash 回退成 s.replace(/\s+/g,'') 会让 S2-f 全矩阵转红。
test('S2-f 关键字紧贴 + 空白改写矩阵: 残留形态逐条检出 (issue #90)', () => {
  // 正控: 每行都是「同一标识符 .message || 自身」, 关键字紧贴或空白改写, 必须检出
  const POS = [
    '  return e.message || e',                      // return 紧贴 (原漏形态)
    '  return await err.message || err',            // return+await 双关键字紧贴
    '  const v = typeof ex.message || ex',          // typeof 紧贴
    '  const w = new _e.message || _e',             // new 紧贴 + $/_ 起头变量
    '  switch (k) { case e1.message || e1: break }', // case 紧贴
    '  const a = e . message || e',                 // 空白改写逃逸形态 (PLAN §3.3 R4)
    '  return err?.message||err',                   // 可选链 + 无空格
  ]
  const src = POS.map((l) => `${l}\n`).join('')
  const got = countResidual(src)
  assert(got === POS.length, `正控 ${POS.length} 行应全检出, 实际 ${got}`)
})
test('S2-g 正常代码不误报矩阵 + squash 边界语义钉桩 (issue #90 反向负控)', () => {
  // 负控: 合法/无关形态, 一行都不许算成残留
  const NEG = [
    '  fmtApiError(e)',                              // 修复后的标准调用
    '  return e.message;',                           // 无 || 兜底
    '  err.message || err2',                         // 结尾 \b: err vs err2 不同标识符
    '  return fmtApiError(err)',                     // import 后调用形态
    '  const errorMsg = err.message',                // 无关命名
    '  const ok = a || b',                           // 无关 ||
    '  const msg = sample.message || e',             // 前置边界: 捕获须起于标识符头, 不得从 sample 尾部抠出 e 误报
    '  // 说明: err.message || err 已按 fmtApiError 收敛', // 整行注释 (同时是 #92 口径)
  ]
  const got = countResidual(NEG.join('\n'))
  assert(got === 0, `负控 ${NEG.length} 行应 0 检出, 实际 ${got}`)
  // squash 语义: 词间空白折叠成哨兵 (不融合出假词), 非词间隙直接删 (保留 || 紧贴判定)
  assert(squash('a b') === 'a\0b', `squash 不得融合 'a b', 实际 ${JSON.stringify(squash('a b'))}`)
  assert(squash('a || b') === 'a||b', `squash 应删非词间隙, 实际 ${JSON.stringify(squash('a || b'))}`)
})

// ---- issue #92: countResidual 与 countCalls 整行注释排除对称化 ----
// issue #144: S2-h 的**行号锚**改**内容锚** —— 行号锚在上方增删行时漂移
// (该文件注释自证已漂移两轮: #92→#116 插 8 行、#132 插 9 行;
//  MV-132-A 首轮全量 npm test 即因此假红)。
// 锚的选取原则 (PLAN-H §4.2):
//   ① **内容锚**而非行号锚 —— 抗行号漂移;
//   ② 特征串必须**唯一命中同一个残留** —— 放宽成宽泛串 = 假绿;
//   ③ 锚的语义是「这行**就是**一处 `x.message || x` 残留」—— 用
//      **反向引用捕获组** (`(\w+)\??\.message\s*\|\|\s*\1`) 表达
//      「同一标识符」, 与 RESIDUAL 的语义**同构**, 不引入第二套语义;
//   ④ 锚集合必须与 RESIDUAL 集合**恒等** (不多不少) —— 只断「命中」
//      不断「全集」会漏检 (删掉一处残留后另一处仍命中 ⇒ 假绿)。
//   ★ 锚与词条键 `common.load_failed` **耦合** (MV-144-E 实测): 若该键改名,
//     锚会失配并显式报红 (集合恒等断言) —— 届时必须同步更新锚, 不得放宽。
test('S2-h 两计数器注释排除口径对称 + 残留内容锚定位 (issue #92 / #144)', () => {
  // 合成源: 1 处真残留 + 1 行含 x.message||x 的示例注释 + 1 处真调用 + 1 行含
  // fmtApiError( 的示例注释。对称口径 ⇒ 各计 1 (旧版残留侧不剥注释 ⇒ 计 2)。
  const synth = [
    '  const bad = err.message || err',
    '  // 反例: x.message || x 是待收敛形态',
    '  fmtApiError(err)',
    '  // 写法: fmtApiError( 是收敛后的调用',
  ].join('\n')
  assert(countResidual(synth) === 1, `残留应计 1, 实际 ${countResidual(synth)}`)
  assert(countCalls(synth) === 1, `调用点应计 1, 实际 ${countCalls(synth)}`)

  // ── issue #144: 内容锚 (替代旧行号锚 —— 旧锚的两个行号字面量已彻底移除,
  //    AC-144-1 要求 grep 该文件零命中行号字面量) ──────────────────────
  // 锚 = 「common.load_failed 的 err 参数处, 同一标识符的 .message || 自身」。
  // 特征串刻意**窄**: 必须同时命中 t('common.load_failed' 调用形态 +
  // 反向引用残留形态 —— 宽泛串 (如 '|| e' / 'e.message') 会命中无关行。
  //
  // 锚的改名敏感性 (issue #149 F-3): 本锚对**标识符**改名免疫
  // (锚以内容特征定位 —— 调用形态 + 反向引用「同一标识符」, 不依赖标识符
  //  名字; 后人把 err 改名为 error 时锚仍然命中), 但对**词条键**改名敏感
  // (正则硬含 t('common.load_failed', ⇒ 键改名后锚集合变空而残留集合非空,
  //  下方恒等断言**显式报红** —— 设计内的显式失败, 非静默假绿)。
  // ⇒ 词条键改名时**必须同步更新本锚**, 不得放宽。
  const RESIDUAL_ANCHOR =
    /t\('common\.load_failed',\s*\{\s*err:\s*([A-Za-z_$][\w$]*)\??\.message\s*\|\|\s*\1\s*\}/
  const boardLines = board.split('\n')
  const strippedBoard = stripLineComments(board)
  const anchored = []
  for (let i = 0; i < strippedBoard.length; i++) {
    if (RESIDUAL_ANCHOR.test(strippedBoard[i])) anchored.push(i + 1)
  }
  // ① 锚集合必须与残留集合**恒等** (不多不少) —— 这是「锚没有放宽」的机器判据
  const residualLines = []
  for (let i = 0; i < strippedBoard.length; i++) {
    if (RESIDUAL.test(squash(strippedBoard[i]))) residualLines.push(i + 1)
  }
  eq(anchored.join(','), residualLines.join(','),
    `内容锚定位的行集 (${anchored}) 必须与残留行集 (${residualLines}) 恒等`)
  // ② 锚到的每一行, 剥注释后**必须仍是残留** (非注释形态) —— 原行号锚语义保留
  assert(anchored.length > 0, '内容锚零命中 ⇒ 锚失配 (词条键或残留形态变了?)')
  for (const ln of anchored) {
    assert(RESIDUAL.test(squash(strippedBoard[ln - 1])),
      `锚 :${ln} 剥注释后不再是残留 ⇒ 内容锚失配 (现 ${JSON.stringify(boardLines[ln - 1].trim().slice(0, 60))})`)
  }
  // ③ 锚不得依赖行号: 源码上方插 N 行, 锚**命中数不变** (行号变, 集合随移)
  for (const shift of [1, 5, 20]) {
    const shifted = new Array(shift).fill('// inserted').concat(strippedBoard)
    const shiftedHits = []
    for (let i = 0; i < shifted.length; i++) {
      if (RESIDUAL_ANCHOR.test(shifted[i])) shiftedHits.push(i + 1)
    }
    eq(shiftedHits.length, anchored.length,
      `插入 ${shift} 行后锚命中数应不变 (实得 ${shiftedHits.length})`)
    eq(shiftedHits, anchored.map((ln) => ln + shift),
      `插入 ${shift} 行后锚应整体平移 ${shift} 行`)
  }
  // ④ **键名无关**的兜底: 残留总数仍由 S2-b 钉死 (RB === 2);
  //    锚的作用是「复核这 2 处是**非注释形态**且位置符合预期」。
  //    若 common.load_failed 被改名 (i18n 改造常见), 锚会失配 —— 此时
  //    必须**显式**报红 (而不是安静地少锚一处), 由 coder 更新锚并说明。
  eq(residualLines.length, 2,
    `TasksBoardView 残留应恰为 2 处, 实际 ${residualLines.length} (${residualLines})`)
})
if (failed) { console.error(`\n${failed}/8 失败`); process.exit(1) }
console.log('\n全部通过 (8)')
