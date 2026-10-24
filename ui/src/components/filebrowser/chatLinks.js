// chatLinks.js — 聊天消息中本地文件路径 → data-fb-path 链接增强
// 设计依据: docs/ARCH-FILEBROWSER.md §4.3 (s3-arch)
// 调用方式: enhanceFileLinks(containerEl, projectRoot, onFileClick)
//
// 规则 (OQ-1 定稿):
//   1. TreeWalker 遍历容器内文本节点 + a[href]
//   2. 祖先链含 pre/code → 跳过 (AC-E5)
//   3. 文本节点: 匹配绝对路径 = <projectRoot>/[\w./+@%一-鿿-]*? (前置非 [\w/], 尾剥悬挂标点)
//   4. a[href]: href 原始值满足 (a) 落在 projectRoot 内 (b) 相对路径命中 KNOWN_EXTS
//   5. http/https/mailto/# 不动
//   6. 幂等: 已有 data-fb-path 跳过
//   7. 容器级 click 委托 (一次性): closest('a[data-fb-path]') → preventDefault → onFileClick
//
// 注: 不依赖 React / i18n, 纯函数, 可在 ui/tests/*.mjs 直接单测。
import { KNOWN_EXTS, extOf } from './kinds.js'

// 路径前缀正则: 绝对路径必须落在 <projectRoot>/ 下
const TRAILING_PUNCT = '.,;:!?，。；、)）]】}」』'
const PROJECT_PATH_RE = (root) => {
  // 转义 root 中的正则元字符 (但 / 在 POSIX 路径里不用转义)
  const esc = root.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  // 路径字符集: word char / . / + / @ / % / 中日韩
  return new RegExp(
    `(${esc})(/[\\w./+@%\\u4e00-\\u9fff-]*)?`,
    'g'
  )
}

// 悬挂标点剥离: 从尾剥除 . , ; : ! ? 等
function trimTrailing(s) {
  return s.replace(new RegExp(`[${TRAILING_PUNCT}]+$`), '')
}

// 已知扩展名正则 (用于相对路径 / md 链接 href)
const KNOWN_EXT_RE = new RegExp(
  '\\.(' + Array.from(KNOWN_EXTS).map(e => e.slice(1)).join('|') + ')$',
  'i'
)

// a[href] 处理: 是否升级为 data-fb-path 链接?
// 严格按 §4.3 决策表:
function shouldUpgradeHref(href, projectRoot) {
  if (!href) return null
  // http(s)/mailto/# → 不动
  if (/^(https?:|mailto:|#|javascript:)/i.test(href)) return null
  // 绝对路径 (POSIX): 必须以 <projectRoot>/ 开头
  if (href.startsWith('/')) {
    if (!projectRoot) return null
    if (href === projectRoot || href.startsWith(projectRoot + '/')) {
      return href
    }
    return null
  }
  // 相对路径 (无前导 /): 命中已知扩展名才升级
  if (KNOWN_EXT_RE.test(href)) {
    return href
  }
  return null
}

// 文本节点中裸路径匹配: 返回所有命中的 abs 路径数组 (含尾剥悬挂标点)
// 返回空数组表示未命中 (issue #48: 同节点多路径需全部匹配)
function matchAbsPathsInText(text, projectRoot) {
  if (!projectRoot || !text) return []
  const re = PROJECT_PATH_RE(projectRoot)
  const hits = []
  let m
  // 使用 /g 标志循环获取所有匹配
  while ((m = re.exec(text)) !== null) {
    let hit = m[0]
    if (hit === projectRoot) continue  // 纯前缀不当成链接
    // 剥悬挂标点 (从路径尾)
    hit = trimTrailing(hit)
    if (hit && hit !== projectRoot) hits.push(hit)
  }
  return hits
}

// 主入口: 对 containerEl 内 DOM 做升级
// 返回 true 表示做了修改 (用于 effect 依赖判断 / 调试)。
export function enhanceFileLinks(containerEl, projectRoot, onFileClick) {
  if (!containerEl || typeof containerEl.nodeType !== 'number') return false
  if (!projectRoot) {
    // 无 projectRoot 时清掉所有已有的 data-fb-path (切换会话/工作目录时调用)
    containerEl.querySelectorAll('a[data-fb-path]').forEach((a) => {
      a.removeAttribute('data-fb-path')
    })
    return false
  }

  let changed = false

  // 1) 文本节点: 匹配裸路径
  const walker = document.createTreeWalker(
    containerEl, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const p = node.parentNode
        if (!p) return NodeFilter.FILTER_REJECT
        const tag = (p.tagName || '').toLowerCase()
        // pre/code/script/style 内的文本不处理
        if (tag === 'pre' || tag === 'code' ||
            tag === 'script' || tag === 'style') {
          return NodeFilter.FILTER_REJECT
        }
        // a[data-fb-path] 已是链接, 不再二次处理其内部文本
        if (tag === 'a' && p.hasAttribute('data-fb-path')) {
          return NodeFilter.FILTER_REJECT
        }
        return NodeFilter.FILTER_ACCEPT
      }
    }
  )
  const textNodes = []
  let tn
  while ((tn = walker.nextNode())) textNodes.push(tn)
  for (const node of textNodes) {
    const text = node.nodeValue || ''
    const absList = matchAbsPathsInText(text, projectRoot)
    if (!absList || absList.length === 0) continue

    // issue #48 fix (H-1 回流): 单次遍历, 按位置升序构造 fragment,
    // 一次性 replaceChild 替换原节点。避免同节点多次 replaceChild 导致
    // parentNode=null 崩溃，同时正确保留间隔文本。
    const positions = []
    for (const abs of absList) {
      let idx = text.indexOf(abs)
      while (idx >= 0) {
        // 去重: 避免同一位置重复添加
        if (!positions.some(p => p.idx === idx)) {
          positions.push({ abs, idx, end: idx + abs.length })
        }
        idx = text.indexOf(abs, idx + 1)
      }
    }
    if (positions.length === 0) continue
    // 按位置升序排序
    positions.sort((a, b) => a.idx - b.idx)

    // 构建 fragment: [text(前), a, text(间隔), a, ..., text(后)]
    const frag = document.createDocumentFragment()
    let lastEnd = 0
    for (const { abs, idx, end } of positions) {
      // 间隔文本: 上一个命中结束到本次命中开始之间
      if (idx > lastEnd) {
        frag.appendChild(document.createTextNode(text.slice(lastEnd, idx)))
      }
      // 创建链接
      const a = document.createElement('a')
      a.href = abs
      a.setAttribute('data-fb-path', abs)
      a.className = 'text-accent underline underline-offset-2 inline-flex items-baseline gap-1 break-all'
      const icon = document.createElement('span')
      icon.className = 'text-[10px] leading-none shrink-0'
      icon.textContent = '📄'
      icon.setAttribute('aria-hidden', 'true')
      a.appendChild(icon)
      a.appendChild(document.createTextNode(abs))
      frag.appendChild(a)
      lastEnd = end
    }
    // 最后剩余的后缀文本
    if (lastEnd < text.length) {
      frag.appendChild(document.createTextNode(text.slice(lastEnd)))
    }
    // 一次性替换原节点
    node.parentNode.replaceChild(frag, node)
    changed = true
  }

  // 2) a[href]: 升级符合条件的链接
  const anchors = containerEl.querySelectorAll('a[href]')
  anchors.forEach((a) => {
    if (a.hasAttribute('data-fb-path')) return  // 幂等
    const hrefRaw = a.getAttribute('href') || ''
    const upgraded = shouldUpgradeHref(hrefRaw, projectRoot)
    if (!upgraded) return
    // 相对路径补齐为绝对路径 (chat files 用绝对路径)
    const abs = upgraded.startsWith('/') ? upgraded
              : (projectRoot + '/' + upgraded).replace(/\/+/g, '/')
    a.setAttribute('data-fb-path', abs)
    a.classList.add('text-accent', 'underline', 'underline-offset-2')
    changed = true
  })

  // 3) 容器级 click 委托 (一次性挂载)
  if (!containerEl.__fbClickBound && typeof onFileClick === 'function') {
    containerEl.addEventListener('click', (e) => {
      const a = e.target && e.target.closest && e.target.closest('a[data-fb-path]')
      if (!a) return
      e.preventDefault()
      const abs = a.getAttribute('data-fb-path') || a.href || ''
      onFileClick(abs)
    })
    containerEl.__fbClickBound = true
  }

  return changed
}