#!/usr/bin/env node
// ASR-SET 守护套件 — ASR 三键进设置页 (前端面) (ASR-SET s1 / coder)
//
// 依据: docs/asr-config-settings/DESIGN.md §7 U1-U4 (唯一技术依据)。
// 用户拍板「进配置而非env」: 设置页新增「语音转写 (ASR)」分组三行
// (asr_base_url / asr_model / asr_key), asr_key 走 password 框遮蔽,
// asr_base_url 前端预检 http(s) 前缀 (与后端 _clean_asr_base 同口径)。
//
// 覆盖:
//   U1  SETTING_ROWS 含 3 新键; GROUP_KEYS 含 settings.group.asr 分组
//       (置于 external 组之后)。
//   U2  PASSWORD_KEYS 含 asr_key 且 renderRow 文本分支 input type 分派在位。
//   U3  validateField 的 asr_base_url 分支在位 (空串放行 + http(s) 前缀 +
//       settings.invalid_asr_url 错误键)。
//   U4  i18n parity: §4 表 8 键在 en.json/zh-cn.json 双语齐备且值非空,
//       settings 子树两语 key 集合完全相等 (沿用 test_issue228 T4 parity 断言法)。
//
// 变异反证锚 (MV): MV-ASR-A 删 SETTING_ROWS 任一新键或 asr 分组 → U1 红;
//   MV-ASR-B PASSWORD_KEYS 删 asr_key 或 input type 分派回退固定 text → U2 红;
//   MV-ASR-C 删 validateField asr_base_url 分支 → U3 红;
//   MV-ASR-D 只改单语 locale 或删任一键 → U4 红。
//
// 风格: 沿用仓库既有 ui/tests/*.mjs — 源码抽取 + 断言, 不起浏览器。
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(path.join(here, f), 'utf8')
const sv = read('../src/views/SettingsView.jsx')
const zh = JSON.parse(read('../src/i18n/locales/zh-cn.json'))
const en = JSON.parse(read('../src/i18n/locales/en.json'))

const tests = []
const test = (name, fn) => tests.push({ name, fn })
const assert = (cond, msg) => { if (!cond) throw new Error(msg) }

// ── U1: SETTING_ROWS 3 新键 + asr 分组 ─────────────────────────────
test('U1 SETTING_ROWS 含 asr 三键且 GROUP_KEYS 含 settings.group.asr 分组', () => {
  const at = sv.indexOf('const SETTING_ROWS')
  assert(at >= 0, 'SETTING_ROWS 未找到')
  const rows = sv.slice(at, sv.indexOf(']', at))
  for (const k of ['asr_base_url', 'asr_model', 'asr_key']) {
    assert(rows.includes(`'${k}'`), `SETTING_ROWS 缺 ${k}`)
  }
  const gat = sv.indexOf('const GROUP_KEYS')
  assert(gat >= 0, 'GROUP_KEYS 未找到')
  const groups = sv.slice(gat, sv.indexOf('\n]', gat))
  assert(/titleKey:\s*'settings\.group\.asr'/.test(groups),
    'GROUP_KEYS 缺 settings.group.asr 分组标题')
  const ext = groups.indexOf('settings.group.external')
  const asr = groups.indexOf('settings.group.asr')
  assert(ext >= 0 && asr > ext, 'asr 分组必须置于 external 组之后 (§4 裁决)')
  assert(/titleKey:\s*'settings\.group\.asr',\s*rowKeys:\s*\[\s*'asr_base_url',\s*'asr_model',\s*'asr_key'\s*\]/.test(groups),
    'asr 分组 rowKeys 必须恰为三新键')
})

// ── U2: PASSWORD_KEYS + input type 分派 ────────────────────────────
test('U2 PASSWORD_KEYS 含 asr_key 且 renderRow input type 分派在位', () => {
  assert(/const PASSWORD_KEYS = new Set\(\['asr_key'\]\)/.test(sv),
    "PASSWORD_KEYS 必须存在且含 'asr_key'")
  assert(/type=\{PASSWORD_KEYS\.has\(k\) \? 'password' : 'text'\}/.test(sv),
    'renderRow 文本分支 input 必须按 PASSWORD_KEYS 分派 type (遮蔽仅屏幕面, 值仍随表单提交)')
})

// ── U3: validateField asr_base_url 分支 ────────────────────────────
test('U3 validateField 含 asr_base_url 分支 (空串放行 + http(s) 前缀)', () => {
  const at = sv.indexOf('function validateField')
  assert(at >= 0, 'validateField 未找到')
  const fn = sv.slice(at, sv.indexOf('\n}', at))
  assert(/key === 'asr_base_url'/.test(fn), 'validateField 缺 asr_base_url 分支')
  const br = fn.slice(fn.indexOf("key === 'asr_base_url'"))
  assert(/if \(!raw\) return ''/.test(br),
    '空串必须放行 (留空 = 回退 env/内置默认, §2 空串哨兵语义)')
  assert(/startsWith\('http:\/\/'\) \|\| .*startsWith\('https:\/\/'\)/.test(br),
    '非空值必须校验 http(s):// 前缀 (与后端 _clean_asr_base 同口径)')
  assert(/settings\.invalid_asr_url/.test(br),
    '非法值必须返回 settings.invalid_asr_url 错误键')
})

// ── U4: i18n 8 键双语齐备 + settings 子树 parity ──────────────────
test('U4 i18n: ASR 8 键双语齐备非空且 settings 子树两语 key 集全等', () => {
  const KEYS = [
    ['group', 'asr'],
    ['row', 'asr_base_url_label'], ['row', 'asr_base_url_hint'],
    ['row', 'asr_model_label'], ['row', 'asr_model_hint'],
    ['row', 'asr_key_label'], ['row', 'asr_key_hint'],
    ['invalid_asr_url'],
  ]
  for (const [tag, loc] of [['zh-cn', zh], ['en', en]]) {
    for (const p of KEYS) {
      let v = loc.settings
      for (const seg of p) {
        v = v?.[seg]
      }
      assert(typeof v === 'string' && v.length > 0,
        `${tag} 缺 settings.${p.join('.')} 或值为空`)
    }
  }
  // hint 里的内置默认值展示文案必须与 audio_proxy 常量口径一致 (§8 同步面)
  assert(en.settings.row.asr_base_url_hint.includes('<your-lan-ip>:8731')
    && zh.settings.row.asr_base_url_hint.includes('<your-lan-ip>:8731'),
    'asr_base_url_hint 必须展示内置默认地址 (与 audio_proxy DEFAULT_ASR_BASE 同批改)')
  assert(en.settings.row.asr_model_hint.includes('qwen3-asr-1.7b')
    && zh.settings.row.asr_model_hint.includes('qwen3-asr-1.7b'),
    'asr_model_hint 必须展示内置默认模型 (与 audio_proxy DEFAULT_ASR_MODEL 同批改)')
  // settings 子树 key 集合两语完全相等 (深度收集)
  const deepKeys = (o, prefix = '') => Object.entries(o).flatMap(([k, v]) =>
    v && typeof v === 'object' ? deepKeys(v, `${prefix}${k}.`) : [`${prefix}${k}`])
  const enK = new Set(deepKeys(en.settings))
  const zhK = new Set(deepKeys(zh.settings))
  const onlyEn = [...enK].filter((k) => !zhK.has(k))
  const onlyZh = [...zhK].filter((k) => !enK.has(k))
  assert(onlyEn.length === 0 && onlyZh.length === 0,
    `settings 子树两语 key 集不等: only-en=${onlyEn} only-zh=${onlyZh}`)
})

// ── runner ─────────────────────────────────────────────────────────
let failed = 0
for (const t of tests) {
  try { await t.fn(); console.log(`  PASS ${t.name}`) }
  catch (e) { failed++; console.error(`  FAIL ${t.name}\n    ${e.message}`) }
}
console.log(failed ? `\n${failed}/${tests.length} FAILED` : `\nALL ${tests.length} PASSED`)
process.exit(failed ? 1 : 0)
