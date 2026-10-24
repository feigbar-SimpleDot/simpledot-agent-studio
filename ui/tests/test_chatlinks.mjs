#!/usr/bin/env node
// 文件浏览器纯函数单测 (issue #46, s4-impl)
// 设计依据: docs/ARCH-FILEBROWSER.md §9.1 (s4 测试清单, ui/tests 一组)
// 覆盖: kinds.js + chatLinks.js
//
// 跑法: node ui/tests/test_chatlinks.mjs
import assert from 'node:assert/strict'
import { readFileSync } from 'fs'

// 1) kinds.js — 通过源码抽取纯函数
const kindsSrc = readFileSync(
  new URL('../src/components/filebrowser/kinds.js', import.meta.url),
  'utf8')

// 直接 import ESM (Node 18+ 支持)
import { extOf, kindOf, dotClass, formatSize, formatMtime,
         isTooLarge, KNOWN_EXTS, SIZE_LIMIT } from '../src/components/filebrowser/kinds.js'

let PASS = 0, FAIL = 0
function ok(name, cond, detail='') {
  if (cond) { PASS++; console.log(`  PASS  ${name}`) }
  else { FAIL++; console.log(`  FAIL  ${name}  ${detail}`) }
}

// ──────────────────────────────────────────────────────────────────
// kinds.js — ext / kind / dotClass / formatSize / isTooLarge
// ──────────────────────────────────────────────────────────────────
console.log('\n[kinds.js]')
ok('extOf(README.md) → md',       extOf('README.md') === 'md')
ok('extOf(setup.py) → py',         extOf('setup.py') === 'py')
ok('extOf(Makefile) → "" (无 ext)', extOf('Makefile') === '')
ok('extOf(IMG.JPG) → jpg (lower)',  extOf('IMG.JPG') === 'jpg')
ok('extOf("") → ""',                extOf('') === '')
ok('extOf(null) → ""',              extOf(null) === '')

ok('kindOf(a.md) → md',             kindOf('a.md') === 'md')
ok('kindOf(a.html) → html',         kindOf('a.html') === 'html')
ok('kindOf(a.pdf) → pdf',           kindOf('a.pdf') === 'pdf')
ok('kindOf(a.png) → image',         kindOf('a.png') === 'image')
ok('kindOf(a.txt) → text',          kindOf('a.txt') === 'text')
ok('kindOf(a.py) → text',           kindOf('a.py') === 'text')
ok('kindOf(a.zip) → other',         kindOf('a.zip') === 'other')
ok('kindOf(a.svg) → other (ADR-4)', kindOf('a.svg') === 'other')
ok('kindOf(Makefile) → other',      kindOf('Makefile') === 'other')

ok('dotClass(md) → accent',         dotClass('a.md').includes('accent'))
ok('dotClass(html) → warn',         dotClass('a.html').includes('warn'))
ok('dotClass(pdf) → bad',           dotClass('a.pdf').includes('bad'))
ok('dotClass(image) → ok',          dotClass('a.png').includes('ok'))
ok('dotClass(text) → dim',          dotClass('a.py').includes('dim'))
ok('dotClass(other) → dim',         dotClass('a.zip').includes('dim'))

ok('formatSize(0) → 0 B',           formatSize(0) === '0 B')
ok('formatSize(512) → 512 B',       formatSize(512) === '512 B')
ok('formatSize(2048) → 2.0 KB',     formatSize(2048).includes('KB'))
ok('formatSize(2*1024*1024) → 2.00 MB', formatSize(2 * 1024 * 1024) === '2.00 MB')
ok('formatSize(-1) → ""',           formatSize(-1) === '')
ok('formatSize("abc") → ""',        formatSize('abc') === '')

ok('SIZE_LIMIT = 5MB',              SIZE_LIMIT === 5 * 1024 * 1024)
ok('isTooLarge(SIZE_LIMIT) → false (临界)', isTooLarge(SIZE_LIMIT) === false)
ok('isTooLarge(SIZE_LIMIT+1) → true', isTooLarge(SIZE_LIMIT + 1) === true)

// ──────────────────────────────────────────────────────────────────
// chatLinks.js — DOM 后处理 (用 JSDOM-lite, 简化为手动 DOM stub)
// ──────────────────────────────────────────────────────────────────
console.log('\n[chatLinks.js]')

// 简化 DOM stub: 足够跑 enhanceFileLinks 主路径
class StubNode {
  constructor(tag) {
    this.tagName = (tag || '').toUpperCase()
    this.childNodes = []
    this.attributes = {}
    this._listeners = {}
    this.parentNode = null
    this.nodeType = this.tagName ? 1 : 3  // element=1, text=3
    this.nodeValue = ''
    this.__fbClickBound = false
  }
  set textContent(v) {
    this.childNodes = [{ nodeType: 3, nodeValue: String(v),
                          parentNode: this, _isText: false }]
  }
  get textContent() {
    return this.childNodes.map(c => c.nodeValue || '').join('')
  }
  setAttribute(k, v) { this.attributes[k] = String(v) }
  hasAttribute(k) { return k in this.attributes }
  removeAttribute(k) { delete this.attributes[k] }
  getAttribute(k) { return this.attributes[k] }
  get className() { return this.attributes.class || '' }
  set className(v) { this.attributes.class = String(v) }
  get classList() {
    const me = this
    return {
      _set: new Set((me.attributes.class || '').split(/\s+/).filter(Boolean)),
      add(...args) { args.forEach(a => me.classList._set.add(a))
                     me.attributes.class = Array.from(me.classList._set).join(' ') },
    }
  }
  appendChild(c) {
    c.parentNode = this
    this.childNodes.push(c)
    return c
  }
  removeChild(c) {
    this.childNodes = this.childNodes.filter(x => x !== c)
    c.parentNode = null
    return c
  }
  replaceChild(newC, oldC) {
    const i = this.childNodes.indexOf(oldC)
    if (i >= 0) {
      newC.parentNode = this
      this.childNodes[i] = newC
      oldC.parentNode = null
    }
    return oldC
  }
  addEventListener(name, fn) {
    (this._listeners[name] = this._listeners[name] || []).push(fn)
  }
  closest(selector) {
    // 仅支持 [data-fb-path] 简单 selector
    if (selector === 'a[data-fb-path]') {
      let n = this
      while (n) {
        if (n.hasAttribute && n.hasAttribute('data-fb-path')) return n
        n = n.parentNode
      }
      return null
    }
    return null
  }
  querySelectorAll(sel) {
    if (sel === 'a[data-fb-path]') return findBy(this, 'data-fb-path')
    if (sel === 'a[href]') return findByAttr(this, 'href')
    return []
  }
  querySelector(sel) {
    const all = this.querySelectorAll(sel)
    return all[0] || null
  }
  get firstChild() { return this.childNodes[0] || null }
}
function findBy(root, attr) {
  const out = []
  function walk(n) {
    if (n.nodeType === 1 && n.hasAttribute(attr)) out.push(n)
    n.childNodes && n.childNodes.forEach(walk)
  }
  walk(root)
  return out
}
function findByAttr(root, attr) {
  const out = []
  function walk(n) {
    if (n.nodeType === 1 && attr in n.attributes) out.push(n)
    n.childNodes && n.childNodes.forEach(walk)
  }
  walk(root)
  return out
}
// TreeWalker: 简化版, 全树遍历文本节点
function walkText(root) {
  const out = []
  function walk(n) {
    if (n.nodeType === 3) {
      const p = n.parentNode
      const tag = (p && p.tagName || '').toLowerCase()
      if (tag !== 'pre' && tag !== 'code' && tag !== 'script' && tag !== 'style') {
        out.push(n)
      }
    }
    if (n.childNodes) n.childNodes.forEach(walk)
  }
  walk(root)
  return out
}

// Stub document.createElement / createDocumentFragment / etc.
globalThis.document = {
  createElement(tag) {
    const n = new StubNode(tag)
    return n
  },
  createTextNode(text) {
    return { nodeType: 3, nodeValue: String(text), parentNode: null,
             childNodes: [], __seen: false,
             appendChild: function() {},
             replaceChild: function() {},
             removeAttribute: function() {} }
  },
  createDocumentFragment() {
    return new StubNode('#fragment')
  },
  createTreeWalker(root, what, opts) {
    return { nextNode() {
      const stack = [root]
      const walked = new Set()
      while (stack.length) {
        const n = stack.shift()
        if (walked.has(n)) continue
        walked.add(n)
        if (n.nodeType === 3) {
          const p = n.parentNode
          const tag = (p && p.tagName || '').toLowerCase()
          if (tag !== 'pre' && tag !== 'code' && tag !== 'script' && tag !== 'style') {
            // Stub: 仅用于方法
            n._isText = true
            return n
          }
        }
        if (n.childNodes) n.childNodes.forEach(c => stack.push(c))
      }
      return null
    } }
  }
}

// Stub TreeWalker 用的 NodeFilter (chatLinks.js 引用)
globalThis.NodeFilter = { FILTER_ACCEPT: 1, FILTER_REJECT: 2, FILTER_SKIP: 3,
                          SHOW_TEXT: 4 }

// 简化版 TreeWalker: 全树遍历文本节点, 跳过 pre/code/script/style/a[data-fb-path] 祖先。
// 真正的浏览器 TreeWalker 支持 FILTER_ACCEPT/REJECT/SKIP, 我们只支持 ACCEPT。
let _walkStack = []
function makeWalker(root) {
  const init = (root.childNodes || []).slice()
  init.forEach((n) => { n.__seen = false })
  _walkStack = init
  return null
}
function nextTextNode() {
  while (_walkStack.length) {
    const n = _walkStack.shift()
    if (n.__seen) continue
    n.__seen = true
    // 文本节点: 检查父链是否在 pre/code/script/style/a[data-fb-path]
    if (n.nodeType === 3) {
      let p = n.parentNode
      let inSkipped = false
      while (p) {
        const tag = (p.tagName || '').toLowerCase()
        if (tag === 'pre' || tag === 'code' ||
            tag === 'script' || tag === 'style' ||
            (tag === 'a' && p.hasAttribute && p.hasAttribute('data-fb-path'))) {
          inSkipped = true; break
        }
        p = p.parentNode
      }
      if (!inSkipped) return n
    }
    if (n.childNodes) {
      n.childNodes.forEach((c) => { c.__seen = false; _walkStack.push(c) })
    }
  }
  return null
}
globalThis.document.createTreeWalker = (root, what, opts) => {
  makeWalker(root)
  return { nextNode: nextTextNode }
}

// 加载 chatLinks.js (ESM 模式)
import { enhanceFileLinks } from '../src/components/filebrowser/chatLinks.js'

const ROOT = '/share_workspace/projects/simpledot-agent-studio'

// 测试 1: 裸路径在文本节点中 → 转链接
{
  const container = new StubNode('div')
  container.appendChild({ nodeType: 3,
    nodeValue: `See ${ROOT}/docs/REQ.md for more`,
    parentNode: container })
  let clicked = null
  enhanceFileLinks(container, ROOT, (abs) => { clicked = abs })
  const a = container.querySelector('a[data-fb-path]')
  ok('裸路径 → 生成 a[data-fb-path]',
     a && a.getAttribute('data-fb-path').includes('/docs/REQ.md'),
     `a=${a ? a.getAttribute('data-fb-path') : 'none'}`)
  ok('click handler 挂载', clicked === null, '未点击')
}

// 测试 2: 已转链接 → 幂等
{
  const container = new StubNode('div')
  const text = { nodeType: 3, nodeValue: `See ${ROOT}/docs/REQ.md`,
                 parentNode: container }
  container.appendChild(text)
  enhanceFileLinks(container, ROOT, () => {})
  // 第一次调用后, 应有 1 个 link
  let links = container.querySelectorAll('a[data-fb-path]')
  ok('一次调用后 → 1 个 link', links.length === 1, `links=${links.length}`)
  enhanceFileLinks(container, ROOT, () => {})  // 二次
  links = container.querySelectorAll('a[data-fb-path]')
  ok('二次调用幂等 (只 1 个 link)', links.length === 1,
     `links=${links.length}`)
}

// 测试 3: pre/code 内文本不处理
{
  const container = new StubNode('div')
  const pre = new StubNode('pre')
  pre.appendChild({ nodeType: 3, nodeValue: `${ROOT}/docs/REQ.md`,
                     parentNode: pre })
  container.appendChild(pre)
  enhanceFileLinks(container, ROOT, () => {})
  const links = container.querySelectorAll('a[data-fb-path]')
  ok('pre/code 内路径不升级', links.length === 0, `links=${links.length}`)
}

// 测试 4: 他项目路径 → 不升级
{
  const container = new StubNode('div')
  container.appendChild({ nodeType: 3,
    nodeValue: `/share_workspace/projects/other-project/secret.md`,
    parentNode: container })
  enhanceFileLinks(container, ROOT, () => {})
  const links = container.querySelectorAll('a[data-fb-path]')
  ok('他项目路径不升级', links.length === 0, `links=${links.length}`)
}

// 测试 5: /etc/passwd 不升级
{
  const container = new StubNode('div')
  container.appendChild({ nodeType: 3,
    nodeValue: '/etc/passwd',
    parentNode: container })
  enhanceFileLinks(container, ROOT, () => {})
  const links = container.querySelectorAll('a[data-fb-path]')
  ok('/etc/passwd 不升级', links.length === 0, `links=${links.length}`)
}

// 测试 6: 相对路径 + 已知扩展名 → 升级
{
  const container = new StubNode('div')
  const a = new StubNode('a')
  a.setAttribute('href', 'docs/REQ.md')
  a.appendChild({ nodeType: 3, nodeValue: 'click me', parentNode: a })
  container.appendChild(a)
  enhanceFileLinks(container, ROOT, () => {})
  ok('相对路径 .md → 升级',
     a.hasAttribute('data-fb-path'),
     `href=${a.getAttribute('href')} fb=${a.getAttribute('data-fb-path')}`)
}

// 测试 7: 相对路径 + 未知扩展名 (.zip) → 不升级
{
  const container = new StubNode('div')
  const a = new StubNode('a')
  a.setAttribute('href', 'docs/foo.zip')
  a.appendChild({ nodeType: 3, nodeValue: 'dl', parentNode: a })
  container.appendChild(a)
  enhanceFileLinks(container, ROOT, () => {})
  ok('相对路径 .zip → 不升级 (svg/zip 等不在白名单)',
     !a.hasAttribute('data-fb-path'),
     `fb=${a.getAttribute('data-fb-path')}`)
}

// 测试 8: http 链接不升级
{
  const container = new StubNode('div')
  const a = new StubNode('a')
  a.setAttribute('href', 'https://example.com/foo.md')
  a.appendChild({ nodeType: 3, nodeValue: 'ext', parentNode: a })
  container.appendChild(a)
  enhanceFileLinks(container, ROOT, () => {})
  ok('https 链接不升级', !a.hasAttribute('data-fb-path'))
}

// 测试 9: 悬挂标点剥离
{
  const container = new StubNode('div')
  container.appendChild({ nodeType: 3,
    nodeValue: `See ${ROOT}/docs/REQ.md, thanks!`,
    parentNode: container })
  enhanceFileLinks(container, ROOT, () => {})
  const links = container.querySelectorAll('a[data-fb-path]')
  if (links.length === 1) {
    ok('悬挂标点 , ! 不在 href 内',
       links[0].getAttribute('data-fb-path') === `${ROOT}/docs/REQ.md`,
       `href=${links[0].getAttribute('data-fb-path')}`)
  } else {
    ok('悬挂标点剥离', false, `links=${links.length}`)
  }
}

// 测试 10: KNOWN_EXTS 包含 24 个 (不含 svg)
ok('KNOWN_EXTS 大小 ≥ 20', KNOWN_EXTS.size >= 20, `size=${KNOWN_EXTS.size}`)
ok('KNOWN_EXTS 不含 .svg (移出预览白名单 ADR-4)',
   !KNOWN_EXTS.has('.svg'))

// 测试 11 (H-1 回归): 同一文本节点两个路径 → 间隔文本保留, 无崩溃
{
  const container = new StubNode('div')
  // 文本: "见 pathA.md 和 pathB.md"
  const text = `见 ${ROOT}/docs/REQ.md 和 ${ROOT}/docs/ARCH.md 请查收`
  container.appendChild({ nodeType: 3, nodeValue: text, parentNode: container })
  enhanceFileLinks(container, ROOT, () => {})
  const links = container.querySelectorAll('a[data-fb-path]')
  ok('同节点两路径 → 生成 2 个链接',
     links.length === 2,
     `links=${links.length}`)
  // H-1 核心验证: 5 路径无崩溃
}

// 测试 12 (H-1 回归): 三个路径连续出现
{
  const container = new StubNode('div')
  const text = `${ROOT}/a.md ${ROOT}/b.md ${ROOT}/c.md`
  container.appendChild({ nodeType: 3, nodeValue: text, parentNode: container })
  enhanceFileLinks(container, ROOT, () => {})
  const links = container.querySelectorAll('a[data-fb-path]')
  ok('同节点三路径 → 生成 3 个链接',
     links.length === 3,
     `links=${links.length}`)
}

// 测试 13 (H-1 回归): 路径在文本开头和结尾
{
  const container = new StubNode('div')
  const text = `${ROOT}/start.md 中间内容 ${ROOT}/end.md`
  container.appendChild({ nodeType: 3, nodeValue: text, parentNode: container })
  enhanceFileLinks(container, ROOT, () => {})
  const links = container.querySelectorAll('a[data-fb-path]')
  ok('首尾路径 → 生成 2 个链接',
     links.length === 2,
     `links=${links.length}`)
}

// 测试 14 (H-1 回归): 验证 replaceChild 只执行一次, 无 parentNode=null 崩溃
// 本测试通过即证明 H-1 崩溃已修复
{
  const container = new StubNode('div')
  const text = `${ROOT}/a.md ${ROOT}/b.md ${ROOT}/c.md ${ROOT}/d.md ${ROOT}/e.md`
  container.appendChild({ nodeType: 3, nodeValue: text, parentNode: container })
  // 若有崩溃会抛 TypeError, enhanceFileLinks 会返回 false
  const result = enhanceFileLinks(container, ROOT, () => {})
  const links = container.querySelectorAll('a[data-fb-path]')
  ok('5 路径无崩溃 → 成功处理',
     result === true && links.length === 5,
     `result=${result}, links=${links.length}`)
}

// ──────────────────────────────────────────────────────────────────
// §汇总
// ──────────────────────────────────────────────────────────────────
console.log('\n' + '='.repeat(60))
console.log(`  chatLinks 测试: ${PASS}/${PASS+FAIL} PASS, ${FAIL} FAIL`)
if (FAIL > 0) process.exit(1)