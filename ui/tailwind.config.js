/** @type {import('tailwindcss').Config} */
// LangUI 设计语言: slate(中性) + blue(强调) 双色体系, 暗色优先。
// 参考 https://github.com/CommandCodeAI/langui — 组件为 Tailwind copy-paste 片段,
// 本项目将其理念落地为 React 组件: 圆角 rounded-xl、细边框、slate-900 底、
// blue-600 主操作、状态色克制使用。
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // 语义色映射, 组件只用语义 token, 换肤改这里
        canvas: 'rgb(var(--c-canvas) / <alpha-value>)',
        panel: 'rgb(var(--c-panel) / <alpha-value>)',
        panel2: 'rgb(var(--c-panel2) / <alpha-value>)',
        line: 'rgb(var(--c-line) / <alpha-value>)',
        ink: 'rgb(var(--c-ink) / <alpha-value>)',
        dim: 'rgb(var(--c-dim) / <alpha-value>)',
        accent: 'rgb(var(--c-accent) / <alpha-value>)',
        ok: 'rgb(var(--c-ok) / <alpha-value>)',
        warn: 'rgb(var(--c-warn) / <alpha-value>)',
        bad: 'rgb(var(--c-bad) / <alpha-value>)',
      },
      fontFamily: {
        sans: ['-apple-system', 'BlinkMacSystemFont', 'PingFang SC', 'Microsoft YaHei', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
      borderRadius: { xl: '0.875rem', '2xl': '1.125rem' },
      boxShadow: {
        glow: '0 0 0 1px rgb(59 130 246 / 0.35), 0 8px 24px -8px rgb(59 130 246 / 0.25)',
      },
    },
  },
  plugins: [],
}
