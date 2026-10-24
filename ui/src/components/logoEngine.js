/* Pixel-art SVG strings for the brand mark (raw path data; pure data).
   No i18n needed; sizes and positions are constants. */
// 动态 LOGO 渲染引擎 — 移植自 docs-source/17-logo.js (已验证渲染器, 几何/调度机制原样继承)
// 要点: 全局单一 rAF ticker 驱动多实例 / IntersectionObserver 离屏停绘 / document.hidden 停绘
//       / t0 只设一次(暂停恢复相位连续) / DPR 封顶 2 / 三层降级(静止帧·SVG兜底·不空白)
// React 侧经 LogoMark.jsx 挂载, 本模块与 React 解耦, 可独立复用。

// 几何唯一源: 系数 × S; 三点不对称 ~10:30 / 7:30 / 4:30, 禁均分对称摆放
const DOTS = [
  { r: 1.06, a0: 315, T: 16.0, dr: 0.062 },  // A 外圈小
  { r: 1.03, a0: 225, T: 15.3, dr: 0.055 },  // B 外圈更小
  { r: 0.55, a0: 135, T:  6.0, dr: 0.085 },  // C 环内中
]

// 引擎单例: list=实例注册表, raf=全局唯一循环句柄, t0=时间基准(只设一次)
const engine = { list: [], raf: null, t0: 0 }

// 画一帧: staticFrame=true 时三点钉在初始钟点位 (reduced-motion 静止帧)
function draw(ctx, S, ringColor, dotColor, tSec, staticFrame) {
  const cx = 0.5 * S, cy = 0.5 * S, R = 0.34 * S
  ctx.clearRect(0, 0, S, S)
  ctx.strokeStyle = ringColor
  ctx.lineWidth = 0.085 * S
  ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke()
  ctx.fillStyle = dotColor
  for (let i = 0; i < 3; i++) {
    const d = DOTS[i]
    const a = staticFrame ? d.a0 : d.a0 + (tSec / d.T) * 360
    const rad = a * Math.PI / 180
    ctx.beginPath()
    ctx.arc(cx + d.r * R * Math.sin(rad), cy - d.r * R * Math.cos(rad), d.dr * S, 0, Math.PI * 2)
    ctx.fill()
  }
}

// 降级层2: canvas 不可用 → 几何一致的静态 SVG data URI, 永不空白 (viewBox 16 格)
// 点色与 colors() 主题同源: dark=#4DABF7 / light=#1878D8, 降级切换瞬间不跳色
export function fallbackSvg(variant) {
  const ring = variant === 'dark' ? '%238A93A0' : '%23343A40'
  const dot = variant === 'dark' ? '%234DABF7' : '%231878D8'
  return "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ccircle cx='8' cy='8' r='5.4' fill='none' stroke='" + ring + "' stroke-width='1.7'/%3E%3Ccircle cx='3.9' cy='3.9' r='1.35' fill='" + dot + "'/%3E%3Ccircle cx='4.0' cy='12.0' r='1.2' fill='" + dot + "'/%3E%3Ccircle cx='10.0' cy='10.0' r='1.8' fill='" + dot + "'/%3E%3C/svg%3E"
}

// 取色一次: canvas 不解析 var(), init 时从 :root 读实际值, 缺省用品牌 fallback 色
function colors(variant) {
  const cs = getComputedStyle(document.documentElement)
  const get = (name, fb) => { const v = cs.getPropertyValue(name).trim(); return v || fb }
  return variant === 'dark'
    ? { ring: get('--brand-ring-dark', '#8A93A0'), dot: get('--brand-dot-dark', '#4DABF7') }
    : { ring: get('--brand-ring', '#343A40'), dot: get('--brand-dot', '#1878D8') }
}

const reduceMQ = window.matchMedia('(prefers-reduced-motion: reduce)')

function tick(m, tSec, force) {
  if (reduceMQ.matches && !force) return  // 降级层1: 静止帧已由首帧画出
  draw(m.ctx, m.S, m.colors.ring, m.colors.dot, tSec, reduceMQ.matches)
}

function start() {
  if (engine.raf) return                              // 已有循环则不重复启动(全局单 ticker)
  if (reduceMQ.matches) return                        // reduce 模式不启动 ticker
  if (!engine.t0) engine.t0 = performance.now()       // 基准只设一次, 暂停/恢复相位连续
  function loop(now) {
    const t = (now - engine.t0) / 1000
    let any = false
    for (const m of engine.list) {
      if (m.active && !document.hidden) { tick(m, t); any = true }
    }
    engine.raf = any ? requestAnimationFrame(loop) : null  // 全部离屏/隐藏则停循环, 不空转
  }
  engine.raf = requestAnimationFrame(loop)
}

// 挂载一个 canvas 实例, 返回卸载函数(从注册表摘除 + 断开 IO)
export function mount(canvas, { size = 32, variant = 'light' } = {}) {
  const S = size
  let ctx = null
  try { ctx = canvas.getContext('2d') } catch { /* canvas 不可用 */ }
  if (!ctx) return () => {}   // React 侧由 LogoMark 渲染 SVG 兜底, 不注册引擎
  const dpr = Math.min(window.devicePixelRatio || 1, 2)  // DPR 封顶 2
  canvas.width = S * dpr
  canvas.height = S * dpr
  canvas.style.width = S + 'px'
  canvas.style.height = S + 'px'
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  const m = { canvas, ctx, S, variant, active: true, colors: colors(variant) }
  engine.list.push(m)
  tick(m, 0, true)  // 首帧必画, 永不空白
  let io = null
  if ('IntersectionObserver' in window) {  // 离屏暂停: 滚出视口即停绘, 回屏恢复
    io = new IntersectionObserver((entries) => {
      m.active = entries[0].isIntersecting
      if (m.active) start()
    })
    io.observe(canvas)
  }
  start()
  return () => {
    if (io) io.disconnect()
    const i = engine.list.indexOf(m)
    if (i >= 0) engine.list.splice(i, 1)
  }
}

// reduced-motion 运行时切换: 开启→补画静止帧; 关闭→重启 ticker (双向响应)
function onReduceChange() {
  if (reduceMQ.matches) {
    for (const m of engine.list)
      draw(m.ctx, m.S, m.colors.ring, m.colors.dot, 0, true)
  } else {
    start()
  }
}
if (reduceMQ.addEventListener) reduceMQ.addEventListener('change', onReduceChange)
else if (reduceMQ.addListener) reduceMQ.addListener(onReduceChange)

// 回前台重启 (切后台时循环因 document.hidden 停绘, 不空转)
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) start()
})
