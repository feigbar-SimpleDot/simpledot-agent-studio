/* Brand mark — React wrapper over the SVG strings produced by logoEngine.
   No user-facing strings; the brand itself is a constant (brand.js) per D9. */
import React, { useEffect, useRef, useState } from 'react'
import { mount, fallbackSvg } from './logoEngine.js'

// 品牌动态 LOGO 组件: canvas + 全局单例 ticker (logoEngine.js)
// props: size=边长px(默认32); variant 自动跟随 <html> 的 dark/light 主题
// 降级: canvas 不可用 → 几何一致静态 SVG; 装饰性 aria-hidden, 品牌名以文本呈现
export default function LogoMark({ size = 32 }) {
  const ref = useRef(null)
  const [failed, setFailed] = useState(false)
  // 主题跟随: <html> class 由 App 维护 (dark/light), MutationObserver 感知切换
  const [variant, setVariant] = useState(() =>
    document.documentElement.classList.contains('light') ? 'light' : 'dark')

  useEffect(() => {
    const mo = new MutationObserver(() => {
      setVariant(document.documentElement.classList.contains('light') ? 'light' : 'dark')
    })
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
    return () => mo.disconnect()
  }, [])

  // variant 变化 → 重新挂载取新色 (引擎内部 t0 不重置, 相位连续)
  useEffect(() => {
    const el = ref.current
    if (!el || failed) return
    let unmount = () => {}
    try {
      const ctx = el.getContext('2d')
      if (!ctx) { setFailed(true); return }
      unmount = mount(el, { size, variant })
    } catch {
      setFailed(true)
    }
    return unmount
  }, [size, variant, failed])

  if (failed) {
    // 降级层2: 静态 SVG 兜底, 永不空白
    return <img src={fallbackSvg(variant)} width={size} height={size} alt="" aria-hidden="true"
                className="shrink-0" style={{ width: size, height: size }} />
  }
  return <canvas ref={ref} aria-hidden="true" className="shrink-0"
                style={{ width: size, height: size }} />
}
