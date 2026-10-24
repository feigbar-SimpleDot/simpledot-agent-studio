import React, { useEffect, useState, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { i18next } from '../i18n'   // 全局 i18next 实例 (buildLLMTemplate 在组件外调用, 需要直接读 language)
import * as yaml from 'js-yaml'
import { api } from '../api.js'
import { Modal, Confirm, Toast } from '../components/ui.jsx'

// ════════════════════════════════════════════════════════════════════
// WorkflowsView — i18n v1.0 conversion
//
// [en] Workflow template directory view. Labels for form fields / buttons
//      / dialog copy route through t(); built-in step name/description
//      values inside the LLM template stay in their **current** language
//      (zh-cn) by architecture §5.3 in-scope rule — the LLM-facing prompt
//      is plain data and not part of the UI copy table. Workflow yaml
//      structure hint text is split: English explanation outside the
//      schema block, Chinese example inside (since it is data, not UI
//      copy, but the prompt block is bilingual-fixed per interaction
//      design §9.3 — keep raw yaml literal as-is; only the surrounding
//      prose goes through t()).
// [zh-cn] Workflow 模板目录视图。表单字段/按钮/对话框文案走 t();LLM
//        模板「内置样例」的 name/description 仍是当前 zh-cn 字面值
//        (架构 §5.3 入表范围排除);只有外层说明文案走 i18next。
// ════════════════════════════════════════════════════════════════════

// [en] split_policy options are the protocol enum names (allow_same /
//      prefer_split / must_split). Labels around them are translated,
//      the enum values themselves stay.
// [zh-cn] split_policy 选项是协议 enum 名 (allow_same/prefer_split/
//        must_split)。选项 label 走翻译,enum 值不变。
function splitPolicies(t) {
  return [
    { v: '', label: t('workflows.split_policy_default') },
    { v: 'allow_same',   label: t('workflows.split_policy_allow_same') },
    { v: 'prefer_split', label: t('workflows.split_policy_prefer_split') },
    { v: 'must_split',   label: t('workflows.split_policy_must_split') },
  ]
}

// 步骤可跳过性: main/产品经理按任务类型决策是否跳过该步 (2026-10-06)
function optionalPolicies(t) {
  return [
    { v: 'false', label: t('workflows.skippable_must_go') },
    { v: 'true',  label: t('workflows.skippable_can_skip') },
  ]
}

// ── 给 LLM 看的模板(JSON Schema + 完整样例), 复制用 ──
// [en] Language-aware template: en deployment → fully English (titles,
//      step names/descriptions, example workflow, field semantics);
//      zh-cn deployment → pre-i18n literal (byte-identical to af25139
//      baseline, REQ §4.1 / AC-2). Both modes still call t() for schema
//      descriptions (yaml_*_hint) and wrap into the same schema block.
// [zh-cn] 语言感知模板: en 部署走全英文版 (标题/样例/字段语义), zh-cn
//        部署走原 zh-cn 字面值 (基线 af25139 逐字节保留)。schema hint
//        描述字段 (yaml_*_hint) 继续走 t()。
function buildLLMTemplate(t) {
  const schema = {
    type: 'object',
    required: ['name', 'description', 'steps'],
    properties: {
      name: { type: 'string' },
      description: { type: 'string' },
      steps: {
        type: 'array',
        items: {
          required: ['id', 'name', 'required_role_tags'],
          properties: {
            id: { type: 'string', pattern: '^[a-z0-9][a-z0-9-]{0,62}$' },
            name: { type: 'string' },
            description: { type: 'string', maxLength: 500,
              description: t('workflows.yaml_step_description_hint') },
            input: { type: 'array', items: { type: 'string' },
              description: t('workflows.yaml_input_hint') },
            output: { type: 'array', items: { type: 'string' },
              description: t('workflows.yaml_output_hint') },
            required_role_tags: { type: 'array', items: { type: 'string' } },
            split_policy: { enum: ['allow_same', 'prefer_split', 'must_split'] },
            depends_on: { type: 'array', items: { type: 'string' },
              description: t('workflows.yaml_depends_hint') },
            loops: { type: 'array', items: { type: 'string' },
              description: t('workflows.yaml_loops_hint') },
            pass_threshold: { type: 'integer', minimum: 1,
              description: t('workflows.yaml_pass_threshold_hint') },
            optional: { type: 'boolean', default: false,
              description: t('workflows.yaml_optional_hint') },
            skip_when: { type: 'array', items: { type: 'string' },
              description: t('workflows.yaml_skip_when_hint') },
          },
        },
      },
    },
  }
  // I18N-R3: zh-cn 字面值保留逐字节不变 (REQ §4.1 / AC-2), 仅在
  // 检测到 en 部署时改走英文样例 + 英文字段语义 + 英文你的任务段。
  // 安全兜底: 没拿到 i18next.language 时默认 zh-cn (零回退, 与 zh 部署一致)。
  const lang = (typeof i18next !== 'undefined' && i18next.language) || 'zh-cn'
  const useEn = lang === 'en'
  if (!useEn) {
    // ── zh-cn branch (byte-identical pre-i18n literal) ──
    const example = {
      name: '标准开发流程 (含测试 loop)',
      description: '新功能开发, 测试失败时回流到设计/实现, 至少通过 3 次才算完成',
      steps: [
        { id: 'req-analysis', name: '需求分析', description: '梳理需求, 明确范围与验收标准',
          required_role_tags: ['analysis'],
          input: ['用户原始需求'], output: ['docs/requirements.md'] },
        { id: 'arch-design', name: '架构设计', description: '基于需求出技术方案, 定模块边界与接口',
          required_role_tags: ['arch', 'design'],
          input: ['docs/requirements.md'], output: ['docs/design.md'],
          optional: true, skip_when: ['hotfix', 'typo', 'config_change'] },
        { id: 'implementation', name: '代码实现', description: '按设计编码实现, 自测通过',
          required_role_tags: ['code-write'],
          input: ['docs/design.md'], output: ['源代码变更'] },
        { id: 'testing', name: '测试验证', description: '编写并执行测试, 覆盖验收标准',
          required_role_tags: ['test'],
          split_policy: 'prefer_split',
          input: ['docs/requirements.md', 'docs/design.md'],
          output: ['docs/test-report.md'],
          loops: ['arch-design', 'implementation'],
          pass_threshold: 3 },
        { id: 'code-review', name: '代码审查', description: '审查代码质量、安全与可维护性',
          required_role_tags: ['review'],
          split_policy: 'must_split',
          input: ['源代码变更', 'docs/design.md'], output: ['docs/review-notes.md'] },
      ],
    }
    return [
      '# mw platform workflow 定义模板 (v2, 含 description/input/output 字段)',
      '',
      '## JSON Schema (workflow body)',
      '```json',
      JSON.stringify(schema, null, 2),
      '```',
      '',
      '## 字段语义',
      '- steps 按数组顺序从左到右执行',
      '- description: 本步骤主要任务简述, 500 字以内 (每个 step 都建议写, 让流程规范、不遗漏关键信息)',
      '- input: 输入文档 (可选), 本步骤参考的输入文档/路径, 如上游步骤的产出',
      '- output: 输出文档 (可选), 本步骤建议产出的文档/路径, 供下游步骤作为 input',
      '- required_role_tags: 承接方需具备的能力标签 (与 worker registry 的 scope.tags 匹配)',
      '- split_policy: 控制承接方换人规则',
      '  - allow_same (默认): 可同 worker',
      '  - prefer_split: 默认换人, 可接受同 worker',
      '  - must_split: 强制换 worker',
      '- depends_on: 依赖的上游 step_id (可选, 默认取数组前一步)',
      '- loops: 列表元素是 step_id, 表示失败回流路径 (可指向上游多个 step)',
      '- pass_threshold: 通过多少次才算本 step 完成 (空 = 一次性通过即过)',
      '- optional: true = 本步可被 main/产品经理按任务类型跳过 (如纯后端跳设计); 默认 false 必走',
      '- skip_when: 建议跳过的任务类型提示 (hotfix/typo/doc_only 等), 只提示不自动执行',
      '',
      '## 完整样例',
      '```yaml',
      yaml.dump({ workflows: { 'your-id': example } }, { lineWidth: 100, noRefs: true }),
      '```',
      '',
      '## 你的任务',
      '请按以上 schema 生成一个 workflow, 满足: [在这里写你的需求]',
      '回复格式: 纯 YAML, 外层用 workflows: { "your-id": {...} }, 不要包在 ``` 里。',
    ].join('\n')
  }
  // ── en branch (full English template, REQ §4.1) ──
  const example = {
    name: t('workflows.tmpl_example_name'),
    description: t('workflows.tmpl_example_description'),
    steps: [
      { id: 'req-analysis',
        name: t('workflows.tmpl_example_step_req_name'),
        description: t('workflows.tmpl_example_step_req_desc'),
        required_role_tags: ['analysis'],
        input: [t('workflows.tmpl_example_step_req_io_in')],
        output: [t('workflows.tmpl_example_step_req_io_out')] },
      { id: 'arch-design',
        name: t('workflows.tmpl_example_step_arch_name'),
        description: t('workflows.tmpl_example_step_arch_desc'),
        required_role_tags: ['arch', 'design'],
        input: [t('workflows.tmpl_example_step_arch_io_in')],
        output: [t('workflows.tmpl_example_step_arch_io_out')],
        optional: true, skip_when: ['hotfix', 'typo', 'config_change'] },
      { id: 'implementation',
        name: t('workflows.tmpl_example_step_impl_name'),
        description: t('workflows.tmpl_example_step_impl_desc'),
        required_role_tags: ['code-write'],
        input: [t('workflows.tmpl_example_step_impl_io_in')],
        output: [t('workflows.tmpl_example_step_impl_io_out')] },
      { id: 'testing',
        name: t('workflows.tmpl_example_step_test_name'),
        description: t('workflows.tmpl_example_step_test_desc'),
        required_role_tags: ['test'],
        split_policy: 'prefer_split',
        input: [t('workflows.tmpl_example_step_test_io_in')],
        output: [t('workflows.tmpl_example_step_test_io_out')],
        loops: ['arch-design', 'implementation'],
        pass_threshold: 3 },
      { id: 'code-review',
        name: t('workflows.tmpl_example_step_review_name'),
        description: t('workflows.tmpl_example_step_review_desc'),
        required_role_tags: ['review'],
        split_policy: 'must_split',
        input: [t('workflows.tmpl_example_step_review_io_in')],
        output: [t('workflows.tmpl_example_step_review_io_out')] },
    ],
  }
  return [
    t('workflows.tmpl_header'),
    '',
    t('workflows.tmpl_schema_header'),
    '```json',
    JSON.stringify(schema, null, 2),
    '```',
    '',
    t('workflows.tmpl_field_semantics_header'),
    t('workflows.tmpl_field_steps_order'),
    t('workflows.tmpl_field_description'),
    t('workflows.tmpl_field_input'),
    t('workflows.tmpl_field_output'),
    t('workflows.tmpl_field_required_role_tags'),
    t('workflows.tmpl_field_split_policy'),
    t('workflows.tmpl_field_split_policy_allow_same'),
    t('workflows.tmpl_field_split_policy_prefer_split'),
    t('workflows.tmpl_field_split_policy_must_split'),
    t('workflows.tmpl_field_depends_on'),
    t('workflows.tmpl_field_loops'),
    t('workflows.tmpl_field_pass_threshold'),
    t('workflows.tmpl_field_optional'),
    t('workflows.tmpl_field_skip_when'),
    '',
    t('workflows.tmpl_example_header'),
    '```yaml',
    yaml.dump({ workflows: { 'your-id': example } }, { lineWidth: 100, noRefs: true }),
    '```',
    '',
    t('workflows.tmpl_your_task_header'),
    t('workflows.tmpl_your_task_body'),
  ].join('\n')
}

// ── ComfyUI 风格节点画布 ──
// 设计要点:
//  - 深色画布 + 网格点状背景
//  - 节点 = 圆角矩形, 顶部彩色标题栏 (按 split_policy 着色, allow_same 默认紫蓝)
//  - 节点底部黑区放 id + role_tags chips + 阈值/loop 提示
//  - 顺序连线: 贝塞尔曲线 (控制点横向偏移), 灰色实线 + 圆点箭头
//  - loop 回流: 贝塞尔曲线 (下绕), 虚线 + 琥珀色 + 箭头
//  - 节点 hover 边框提亮 + 微缩放
//  - 仅静态展示, 无交互 (不拖拽、不平移、不缩放)
//
// 布局: 等宽横排, NODE_W×NODE_H 节点 + 固定 gap, 画布内 padding 40。
const NODE_W = 200
const NODE_H = 110
const GAP_X = 60       // 节点水平间距 (容纳曲线)
const PAD = 40
const ARC_RISE = 80    // loop 弧线下凸距离
const indexOf = (steps, id) => steps.findIndex((s) => s.id === id)

// split_policy 主题: CONSOLE2 §3.5 决策为「变量双主题化」——
// 颜色全部走 --wf-pol-* CSS 变量(index.css §0.1-⑤), 亮/暗自动联动, 不再硬编码。
const SP_THEME = {
  allow_same:   { bar: 'var(--wf-pol-allow-bar)', text: 'var(--wf-pol-allow-ink)',
                  chip: 'var(--wf-pol-allow-chip)', chipInk: 'var(--wf-pol-allow-chip-ink)',
                  dot: 'bg-slate-400', label: 'allow_same' },
  prefer_split: { bar: 'var(--wf-pol-prefer-bar)', text: 'var(--wf-pol-prefer-ink)',
                  chip: 'var(--wf-pol-prefer-chip)', chipInk: 'var(--wf-pol-prefer-chip-ink)',
                  dot: 'bg-blue-400', label: 'prefer_split' },
  must_split:   { bar: 'var(--wf-pol-must-bar)', text: 'var(--wf-pol-must-ink)',
                  chip: 'var(--wf-pol-must-chip)', chipInk: 'var(--wf-pol-must-chip-ink)',
                  dot: 'bg-orange-400', label: 'must_split' },
}

// ── 网格点背景 (ComfyUI 风, CONSOLE2 §3.5: 颜色走 --wf-* 变量双主题) ──
function DotGrid({ w, h }) {
  const step = 24
  const cols = Math.ceil(w / step) + 2
  const rows = Math.ceil(h / step) + 2
  const dots = []
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      dots.push(<circle key={`${c}-${r}`} cx={c * step} cy={r * step} r="1" fill="var(--wf-dot)" />)
    }
  }
  return (
    <svg width={w} height={h} className="absolute inset-0 pointer-events-none">
      <defs>
        <radialGradient id="vignette" cx="50%" cy="50%" r="60%">
          <stop offset="0%" stopColor="var(--wf-vignette)" stopOpacity="0" />
          <stop offset="100%" stopColor="var(--wf-vignette)" stopOpacity="0.9" />
        </radialGradient>
      </defs>
      <g>{dots}</g>
      <rect width={w} height={h} fill="url(#vignette)" />
    </svg>
  )
}

// ── 单个节点 (圆角矩形 + 顶部标题栏 + 底部信息区; CONSOLE2 §3.5 变量双主题 + 字号提升 B4) ──
// [en] lang is injected by the parent WorkflowGraph (which owns useTranslation),
//      since GraphNode itself does not call the hook. Avoids ReferenceError on
//      a freshly-deployed build where lang was previously referenced out of scope.
// ──
function GraphNode({ step, index, total, theme, lang }) {
  const { bar, text, chip, chipInk, dot: dotCls } = theme
  return (
    <g transform={`translate(${index * (NODE_W + GAP_X)}, 0)`}>
      {/* 主卡片 (foreignObject 装 HTML, 便于沿用样式; 底色走 --wf-node) */}
      <foreignObject x="0" y="0" width={NODE_W} height={NODE_H}>
        <div className="relative h-full rounded-xl overflow-hidden border border-line
                       shadow-[0_4px_14px_-4px_rgba(0,0,0,0.6)]
                       transition-colors duration-150"
             style={{ background: 'var(--wf-node)' }}>
          {/* 顶部彩色标题栏 (split_policy 决定, 走 --wf-pol-* 变量) */}
          <div className="px-2.5 py-1.5 flex items-center gap-1.5"
               style={{ background: bar, color: text }}>
            <span className={`w-1.5 h-1.5 rounded-full ${dotCls} shadow-[0_0_6px_currentColor] shrink-0`} />
            <div className="flex-1 min-w-0">
              <div className="text-[12.5px] font-bold truncate leading-tight" title={lang === 'zh-cn' ? (step.name || step.name_en) : (step.name_en || step.name)}>{lang === 'zh-cn' ? (step.name || step.name_en) : (step.name_en || step.name)}</div>
              <div className="text-[10.5px] opacity-70 truncate font-mono" title={step.id}>{step.id}</div>
            </div>
            <div className="text-[10px] font-mono opacity-50 shrink-0">#{index + 1}/{total}</div>
          </div>
          {/* 底部信息区 */}
          <div className="px-2.5 py-1.5 space-y-1" style={{ background: 'var(--wf-node)' }}>
            {/* role tag chips */}
            <div className="flex flex-wrap gap-1">
              {(step.required_role_tags || []).slice(0, 4).map((tg) => (
                <span key={tg}
                      className="px-1.5 py-0.5 rounded text-[10px] font-medium leading-none"
                      style={{ background: chip, color: chipInk }}>
                  {tg}
                </span>
              ))}
              {(step.required_role_tags || []).length > 4 && (
                <span className="px-1.5 py-0.5 rounded text-[10px] font-medium leading-none"
                      style={{ background: 'var(--wf-pol-allow-chip)', color: 'var(--wf-pol-allow-chip-ink)' }}>
                  +{step.required_role_tags.length - 4}
                </span>
              )}
            </div>
            {/* 底部状态栏: split_policy / pass_threshold / loop 数 */}
            <div className="flex items-center gap-2 text-[10px] pt-1 border-t border-line"
                 style={{ color: 'var(--wf-node-dim)' }}>
              <span className="font-mono">{theme.label}</span>
              {step.pass_threshold && (
                <span className="px-1 rounded leading-tight"
                      style={{ background: 'var(--wf-badge-loop-bg)', color: 'var(--wf-badge-loop-ink)' }}>
                  ×{step.pass_threshold}
                </span>
              )}
              {(step.loops || []).length > 0 && (
                <span className="ml-auto font-mono inline-flex items-center gap-0.5"
                      style={{ color: 'var(--wf-badge-loop-ink)' }}>
                  <span aria-hidden="true" className="text-[10px]">↩</span>
                  <span>{step.loops.length}</span>
                </span>
              )}
            </div>
          </div>
        </div>
      </foreignObject>
      {/* 左右端口 (socket) 视觉提示 */}
      <circle cx="0" cy={NODE_H / 2} r="5" fill="var(--wf-dot)" stroke="var(--wf-edge)" strokeWidth="2" />
      <circle cx={NODE_W} cy={NODE_H / 2} r="5" fill="var(--wf-dot)" stroke="var(--wf-edge)" strokeWidth="2" />
    </g>
  )
}

// ── 顺序连线 (贝塞尔, 源右 socket → 目标左 socket) ──
function FlowEdge({ from, to }) {
  const x1 = from.x + NODE_W
  const y1 = from.y + NODE_H / 2
  const x2 = to.x
  const y2 = to.y + NODE_H / 2
  const dx = (x2 - x1) * 0.5
  // S 形贝塞尔: 控制点横向偏移 50%
  const d = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`
  return (
    <path d={d} fill="none" stroke="var(--wf-edge)" strokeWidth="2.5"
          markerEnd="url(#arrow-flow)" />
  )
}

// ── loop 回流连线 (从源节点底部 → 下绕 → 目标节点底部) ──
function LoopEdge({ from, to }) {
  const x1 = from.x + NODE_W / 2
  const y1 = from.y + NODE_H
  const x2 = to.x + NODE_W / 2
  const y2 = to.y + NODE_H
  const peakY = Math.max(y1, y2) + ARC_RISE
  // 用同高度控制点画平滑下凸曲线
  const d = `M ${x1} ${y1} C ${x1} ${peakY}, ${x2} ${peakY}, ${x2} ${y2}`
  return (
    <path d={d} fill="none" stroke="var(--wf-loop)" strokeWidth="2"
          strokeDasharray="6 4" markerEnd="url(#arrow-loop)" />
  )
}

// ── 顶部画布 ──
function WorkflowGraph({ wf, t }) {
  const { i18n } = useTranslation()
  const lang = i18n.language || 'en'
  const steps = wf.steps || []
  if (!steps.length) return <div className="empty">{t('workflows.empty_steps')}</div>

  const totalW = PAD * 2 + steps.length * NODE_W + (steps.length - 1) * GAP_X
  const totalH = PAD * 2 + NODE_H + ARC_RISE + 20

  const nodePos = (i) => ({ x: i * (NODE_W + GAP_X) + PAD, y: PAD })
  const indexLookup = (id) => indexOf(steps, id)

  // 收集顺序连线 + loop 连线 (loop 只画回指上游, 避免重复)
  const flowEdges = []
  const loopEdges = []
  const seenLoops = new Set()
  steps.forEach((s, i) => {
    if (i < steps.length - 1) {
      flowEdges.push({ from: nodePos(i), to: nodePos(i + 1) })
    }
    ;(s.loops || []).forEach((ref) => {
      const j = indexLookup(ref)
      if (j < 0 || j >= i) return
      const key = `${ref}->${s.id}`
      if (seenLoops.has(key)) return
      seenLoops.add(key)
      loopEdges.push({ from: nodePos(i), to: nodePos(j) })
    })
  })

  // 步骤级标签: zh 显示 zh(原值),en 显示英文(描述内置样例的部分不翻译,纯英文 label 通用)
  // [en] Legend labels around the graph itself stay in their UI language.
  // [zh-cn] 图例文案走 UI 语言;内置示例 step 名/description 不翻译(数据)。
  return (
    <div className="space-y-3">
      <div className="relative rounded-xl overflow-hidden border border-line"
           style={{ minHeight: totalH, background: 'var(--wf-canvas)' }}>
        <DotGrid w={totalW} h={totalH} />
        <div className="relative overflow-x-auto">
          <svg width={totalW} height={totalH} className="block">
            <defs>
              {/* 顺序箭头 (走 --wf-edge 变量) */}
              <marker id="arrow-flow" viewBox="0 0 10 10" refX="9" refY="5"
                      markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--wf-edge)" />
              </marker>
              {/* loop 箭头 (走 --wf-loop 变量) */}
              <marker id="arrow-loop" viewBox="0 0 10 10" refX="9" refY="5"
                      markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--wf-loop)" />
              </marker>
            </defs>
            {/* 先画连线, 后画节点 (节点覆盖连线端点) */}
            {flowEdges.map((e, i) => <FlowEdge key={`f-${i}`} {...e} />)}
            {loopEdges.map((e, i) => <LoopEdge key={`l-${i}`} {...e} />)}
            {steps.map((s, i) => {
              const sp = s.split_policy || 'allow_same'
              const theme = SP_THEME[sp] || SP_THEME.allow_same
              return <GraphNode key={s.id} step={s} index={i}
                                total={steps.length} theme={theme} lang={lang} />
            })}
          </svg>
        </div>
      </div>
      {/* 图例 (CONSOLE2 §3.5 E2: 色块走 --wf-* 变量, 随画布主题联动) */}
      <div className="flex flex-wrap gap-x-4 gap-y-1.5 text-[11px] text-dim">
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block w-5 h-0.5" style={{ background: 'var(--wf-edge)' }} /> {t('workflows.legend_flow')}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <svg width="20" height="6"><line x1="0" y1="3" x2="20" y2="3"
            stroke="var(--wf-loop)" strokeWidth="2" strokeDasharray="3 2" /></svg>
          {t('workflows.legend_loop')}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="w-3 h-3 rounded border border-line" style={{ background: 'var(--wf-pol-allow-bar)' }} /> allow_same
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="w-3 h-3 rounded border border-line" style={{ background: 'var(--wf-pol-prefer-bar)' }} /> prefer_split
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="w-3 h-3 rounded border border-line" style={{ background: 'var(--wf-pol-must-bar)' }} /> must_split
        </span>
      </div>
    </div>
  )
}

export default function WorkflowsView({ onChanged }) {
  const { t, i18n } = useTranslation()
  const [workflows, setWorkflows] = useState(null)
  const [err, setErr] = useState('')
  const [dlg, setDlg] = useState(null) // {mode:'create'|'edit'|'template', id, f:{...}, importText, importError}
  const [view, setView] = useState(null) // {id, wf} 当前查看流程图的 workflow
  const [del, setDel] = useState(null)
  const [toast, setToast] = useState('')

  const load = useCallback(async () => {
    try {
      const d = await api.workflows()
      // 过滤掉 auto 占位项
      const wfs = { ...(d.workflows || {}) }
      delete wfs.auto
      setWorkflows(wfs)
      setErr('')
    } catch (e) { setErr(String(e.message || e)) }
  }, [])

  useEffect(() => { load() }, [load])

  const openCreate = () => {
    setDlg({ mode: 'create', id: '', importText: '', importError: '', importCollapsed: false,
      f: { name: '', description: '', steps: [emptyStep()] } })
  }
  const openEdit = (id) => {
    const w = workflows[id] || {}
    setDlg({
      mode: 'edit', id, importText: '', importError: '', importCollapsed: false,
      f: wfToForm(w),
    })
  }

  const doImport = () => {
    const text = (dlg.importText || '').trim()
    if (!text) { setDlg((d) => ({ ...d, importError: t('workflows.import_empty') })); return }
    try {
      const parsed = yaml.load(text)
      let wf = null, wfId = null
      if (parsed?.workflows && typeof parsed.workflows === 'object') {
        const ids = Object.keys(parsed.workflows)
        wfId = ids[0]
        wf = parsed.workflows[wfId]
      } else if (parsed?.steps) {
        wf = parsed
        wfId = dlg.mode === 'edit' ? dlg.id : ''
      }
      if (!wf || !Array.isArray(wf.steps)) {
        setDlg((d) => ({ ...d, importError: t('workflows.import_no_structure') })); return
      }
      setDlg((d) => ({
        ...d,
        id: dlg.mode === 'edit' ? dlg.id : (wfId || ''),
        f: wfToForm(wf),
        importError: '✓ ' + t('workflows.import_parsed_toast'),
        importCollapsed: true,  // §3.4: 解析成功后导入区折叠为一行摘要
      }))
    } catch (e) {
      setDlg((d) => ({ ...d, importError: `YAML ${t('common.load_failed', { err: e.message || e })}` }))
    }
  }

  const copyTemplate = async () => {
    try {
      await navigator.clipboard.writeText(buildLLMTemplate(t))
      setToast(t('workflows.llm_template_copied'))
    } catch {
      setDlg({ mode: 'template' })
    }
  }

  const doSave = async () => {
    const f = dlg.f || {}
    const wfId = (dlg.mode === 'edit' ? dlg.id : (f.id || '')).trim()
    if (!wfId) { setDlg((d) => ({ ...d, importError: t('workflows.err_id_required') })); return }
    if (!/^[a-z0-9][a-z0-9-]{0,62}$/.test(wfId)) {
      setDlg((d) => ({ ...d, importError: t('workflows.err_id_charset') })); return
    }
    if (!(f.name || '').trim()) { setDlg((d) => ({ ...d, importError: t('workflows.err_name_required') })); return }
    const steps = (f.steps || []).map((s) => {
      const o = {
        id: (s.id || '').trim(),
        name: (s.name || '').trim(),
        required_role_tags: (s.required_role_tags || '').split(',').map((x) => x.trim()).filter(Boolean),
      }
      if ((s.description || '').trim()) o.description = s.description.trim()
      const inp = (s.input || '').split(',').map((x) => x.trim()).filter(Boolean)
      if (inp.length) o.input = inp
      const outp = (s.output || '').split(',').map((x) => x.trim()).filter(Boolean)
      if (outp.length) o.output = outp
      if ((s.split_policy || '').trim()) o.split_policy = s.split_policy.trim()
      if (s.optional === true || s.optional === 'true') o.optional = true
      const sw = (s.skip_when || '').split(',').map((x) => x.trim()).filter(Boolean)
      if (sw.length) o.skip_when = sw
      const deps = (s.depends_on || '').split(',').map((x) => x.trim()).filter(Boolean)
      if (deps.length) o.depends_on = deps
      if ((s.loops || '').trim()) o.loops = s.loops.split(',').map((x) => x.trim()).filter(Boolean)
      if (s.pass_threshold !== '' && s.pass_threshold !== null && s.pass_threshold !== undefined) {
        const n = parseInt(s.pass_threshold, 10)
        if (!isNaN(n) && n > 0) o.pass_threshold = n
      }
      return o
    })
    if (!steps.length) { setDlg((d) => ({ ...d, importError: t('workflows.err_at_least_one_step') })); return }
    const seen = new Set()
    for (let i = 0; i < steps.length; i++) {
      if (!steps[i].id) { setDlg((d) => ({ ...d, importError: `step ${i + 1} id ${t('workflows.err_id_required')}` })); return }
      if (!steps[i].name) { setDlg((d) => ({ ...d, importError: `step ${i + 1} name ${t('workflows.err_name_required')}` })); return }
      if (seen.has(steps[i].id)) { setDlg((d) => ({ ...d, importError: `step id duplicate: ${steps[i].id}` })); return }
      seen.add(steps[i].id)
    }
    try {
      const r = await api.workflowSave(wfId, {
        name: f.name.trim(), description: (f.description || '').trim(), steps,
      })
      if (r && r.error) { setDlg((d) => ({ ...d, importError: t('workflows.err_backend_unreachable', { err: r.error }) })); return }
      setDlg(null)
      setToast(t(r.created ? 'workflows.create_workflow_btn' : 'workflows.save_changes', { }))  // toast_below reused
        + (r.created ? ` ✓「${wfId}」` : ` ✓「${wfId}」`)
      load(); onChanged?.()
    } catch (e) { setDlg((d) => ({ ...d, importError: t('workflows.err_backend_unreachable', { err: e.message || e }) })) }
  }

  const setStep = (i, patch) => setDlg((d) => ({
    ...d, f: { ...d.f, steps: d.f.steps.map((s, j) => (j === i ? { ...s, ...patch } : s)) },
  }))
  const addStep = () => setDlg((d) => ({ ...d, f: { ...d.f, steps: [...d.f.steps, emptyStep()] } }))
  // 在位置 i 插入新步骤 (中间插入, 2026-10-06)
  const insertStepAt = (i) => setDlg((d) => {
    const steps = [...d.f.steps]
    steps.splice(i, 0, emptyStep())
    return { ...d, f: { ...d.f, steps } }
  })
  // 上下移动步骤 (2026-10-06)
  const moveStep = (i, dir) => setDlg((d) => {
    const j = i + dir
    if (j < 0 || j >= d.f.steps.length) return d
    const steps = [...d.f.steps]
    ;[steps[i], steps[j]] = [steps[j], steps[i]]
    return { ...d, f: { ...d.f, steps } }
  })
  const delStep = (i) => setDlg((d) => ({
    ...d, f: { ...d.f, steps: d.f.steps.filter((_, j) => j !== i) },
  }))

  if (err && !workflows)
    return <div className="empty"><span className="dot-bad" />{t('workflows.err_backend_unreachable', { err })}</div>
  if (!workflows)
    return <div className="space-y-3">{[...Array(3)].map((_, i) => <div key={i} className="skeleton h-16" />)}</div>

  const list = Object.entries(workflows)

  return (
    <div className="wf-touch space-y-5">
      <div className="flex flex-wrap items-center gap-2.5">
        <button className="btn-primary" onClick={openCreate}>
          <svg viewBox="0 0 24 24" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>
          {t('workflows.create_workflow_btn')}
        </button>
        <button className="btn-ghost" onClick={copyTemplate}>📋 {t('workflows.llm_template_copy_btn')}</button>
        <span className="text-[11.5px] text-dim ml-1">
          {t('workflows.toolbar_loop_hint')}
        </span>
      </div>

      {list.length ? (
        <section className="card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Workflow</th>
                  <th>{t('workflows.header_name')}</th>
                  <th>{t('workflows.header_steps')}</th>
                  <th className="text-right">{t('workflows.header_actions')}</th>
                </tr>
              </thead>
              <tbody>
                {list.map(([id, w]) => (
                  <tr key={id}>
                    <td><span className="badge-blue mono" title={id}>{id}</span></td>
                    <td className="text-[12.5px]">
                      <div title={i18n.language === 'zh-cn' ? (w.name || w.name_en) : (w.name_en || w.name)}>{i18n.language === 'zh-cn' ? (w.name || w.name_en) : (w.name_en || w.name)}</div>
                      {w.description && <div className="text-dim text-[11px] line-clamp-2" title={i18n.language === 'zh-cn' ? (w.description || w.description_en) : (w.description_en || w.description)}>{i18n.language === 'zh-cn' ? (w.description || w.description_en) : (w.description_en || w.description)}</div>}
                    </td>
                    <td>
                      <div className="flex flex-wrap gap-1 max-w-[360px]">
                        {(w.steps || []).map((s) => {
                          const tip = [
                            `tags: ${(s.required_role_tags || []).join(', ')}`,
                            s.split_policy ? `· ${s.split_policy}` : '',
                            (s.loops || []).length ? `· loops: ${s.loops.join(',')}` : '',
                            s.pass_threshold ? `· ×${s.pass_threshold}` : '',
                          ].filter(Boolean).join(' ')
                          return (
                            <span key={s.id} className="badge-slate text-[10px]" title={tip}>
                              {(s.loops || []).length > 0 && <span aria-hidden="true">↩</span>}
                              {i18n.language === 'zh-cn' ? (s.name || s.name_en) : (s.name_en || s.name)}
                              {s.pass_threshold ? ` ×${s.pass_threshold}` : ''}
                            </span>
                          )
                        })}
                      </div>
                    </td>
                    <td className="text-right whitespace-nowrap space-x-2">
                      <button className="btn-ghost btn-sm" onClick={() => setView({ id, wf: w })}>{t('workflows.view_visualize_btn')}</button>
                      <button className="btn-ghost btn-sm" onClick={() => openEdit(id)}>{t('workflows.edit_btn')}</button>
                      <button className="btn-danger btn-sm" onClick={() => setDel(id)}>{t('workflows.delete_btn')}</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : (
        <div className="empty">
          <span className="text-3xl">🧭</span>
          {t('workflows.no_workflows')}
        </div>
      )}

      {/* 可视化弹窗 (纯静态 SVG) */}
      <Modal open={!!view} onClose={() => setView(null)}
        title={view ? `🧭 ${t('workflows.view_visualize_btn')}: ${view.id}` : ''} width="max-w-[90rem]">
        {view && (
          <div className="px-3 py-3 space-y-3">
            <WorkflowGraph wf={view.wf} t={t} />
            {view.wf.description && <div className="text-dim text-[12px]">{i18n.language === 'zh-cn' ? (view.wf.description || view.wf.description_en) : (view.wf.description_en || view.wf.description)}</div>}
            <div className="text-dim text-[11px] border-t border-line pt-2">
              {t('workflows.view_modal_hint')}
            </div>
          </div>
        )}
      </Modal>

      {/* LLM 模板 fallback 弹窗 (剪贴板不可用时手动复制) */}
      <Modal open={dlg?.mode === 'template'} onClose={() => setDlg(null)}
        title={`📋 ${t('workflows.llm_template_modal_title')}`} width="max-w-3xl">
        {dlg?.mode === 'template' && (
          <div className="px-5 py-3">
            <div className="hint mb-2">{t('workflows.llm_template_modal_hint')}</div>
            <textarea className="input font-mono text-[11px] h-[60vh]" readOnly value={buildLLMTemplate(t)} />
          </div>
        )}
      </Modal>

      {/* 新建/编辑弹窗 (顶部有 YAML 导入区) */}
      <Modal open={!!dlg && dlg.mode !== 'template'} onClose={() => setDlg(null)}
        title={dlg?.mode === 'edit' ? `✎ ${t('workflows.edit_workflow_btn')}: ${dlg?.id || ''}` : `➕ ${t('workflows.create_form_title')}`}
        width="max-w-3xl"
        footer={
          <>
            <button className="btn-ghost" onClick={() => setDlg(null)}>{t('common.cancel')}</button>
            <button className="btn-primary" onClick={doSave}>
              {dlg?.mode === 'edit' ? t('workflows.save_changes') : t('workflows.create_form_title')}
            </button>
          </>
        }>
        {dlg && dlg.mode !== 'template' && (
          <div className="px-5 py-3 space-y-3.5">
            {/* 顶部: YAML 导入区 (CONSOLE2 §3.4: 加高可拖, 解析成功后折叠为一行摘要) */}
            {dlg.importCollapsed ? (
              <div className="rounded-lg border border-ok/30 bg-ok/5 p-3 flex items-center gap-2 text-[12.5px] text-ok">
                <span>✓ {t('workflows.import_parsed_toast')}</span>
                <button className="btn-ghost btn-sm ml-auto"
                  onClick={() => setDlg((d) => ({ ...d, importCollapsed: false }))}>
                  {t('workflows.import_expand_again')}
                </button>
              </div>
            ) : (
            <div className="rounded-lg border border-accent/30 bg-accent/5 p-3 space-y-2">
              <div className="flex items-center justify-between">
                <div className="text-[12px] font-medium">📥 {t('workflows.import_modal_title')}</div>
                <button className="btn-ghost btn-sm" onClick={copyTemplate}>📋 {t('workflows.llm_template_copy_btn')}</button>
              </div>
              <textarea className="input font-mono text-[11px] h-40 min-h-[160px] resize-y"
                placeholder={t('workflows.import_modal_hint')}
                value={dlg.importText || ''}
                onChange={(e) => setDlg((d) => ({ ...d, importText: e.target.value, importError: '' }))} />
              <div className="flex items-center justify-between">
                <div className={`hint !mt-0 ${dlg.importError?.startsWith('✓') ? 'text-ok' : dlg.importError ? 'text-bad' : ''}`}>
                  {dlg.importError || `${t('workflows.supported_structures')} ${t('workflows.structure_workflow_top')} ${t('workflows.structure_workflow_steps_under')}`}
                </div>
                <button className="btn-primary btn-sm" onClick={doImport}>{t('workflows.import_fill_btn')}</button>
              </div>
            </div>
            )}

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label className="label">{t('workflows.field_id')}{dlg.mode === 'edit' ? ` (${t('common.edit')})` : ' *'}</label>
                <input className="input mono" placeholder="e.g. hotfix-cycle"
                  disabled={dlg.mode === 'edit'}
                  value={dlg.mode === 'edit' ? dlg.id : (dlg.f.id || '')}
                  onChange={(e) => setDlg((d) => ({ ...d, f: { ...d.f, id: e.target.value } }))} />
                <div className="hint">{t('workflows.yaml_charset_hint')}</div>
              </div>
              <div>
                <label className="label">{t('workflows.field_name')} *</label>
                <input className="input" placeholder={t('workflows.field_name_hint')}
                  value={dlg.f.name || ''}
                  onChange={(e) => setDlg((d) => ({ ...d, f: { ...d.f, name: e.target.value } }))} />
              </div>
            </div>
            <div>
              <label className="label">{t('workflows.field_description')}</label>
              <input className="input" placeholder={t('workflows.field_name_hint')}
                value={dlg.f.description || ''}
                onChange={(e) => setDlg((d) => ({ ...d, f: { ...d.f, description: e.target.value } }))} />
            </div>

            <div className="pt-1">
              <div className="flex items-center justify-between mb-2">
                <label className="label !mb-0">{t('common.create')}{t('workflows.header_steps')}（{dlg.f.steps.length}）</label>
                <button className="btn-ghost btn-sm" onClick={addStep}>{t('workflows.add_step_btn')}</button>
              </div>
              <div className="space-y-2.5">
                {dlg.f.steps.map((s, i) => (
                  <React.Fragment key={i}>
                    {/* 在此处插入 (中间插入); 触屏无 hover, 由下方 CSS 常显 (§3.3) */}
                    <div className="group/ins -my-1 py-0.5 text-center opacity-0 hover:opacity-100 transition-opacity wf-insert">
                      <button className="btn-ghost btn-sm !py-0.5 text-[10px]"
                        title={t('workflows.insert_here_btn')}
                        onClick={() => insertStepAt(i)}>＋ {t('workflows.insert_here_btn')}</button>
                    </div>
                    {/* 步骤块: 头部(序号+实时步骤名) + 双列字段网格, <768px 单列 (§3.2) */}
                    <div className="rounded-lg border border-line bg-panel2/40 p-2.5 space-y-2">
                    <div className="flex items-center gap-2">
                      <span className="text-[11px] text-dim shrink-0" title={(i18n.language === 'zh-cn' ? (s.name || s.name_en) : (s.name_en || s.name)) || ''}>
                        {i18n.language === 'zh-cn' ? '步骤' : 'Step'} {i + 1}{(i18n.language === 'zh-cn' ? (s.name || s.name_en) : (s.name_en || s.name)) ? ` · ${i18n.language === 'zh-cn' ? (s.name || s.name_en) : (s.name_en || s.name)}` : ''}
                      </span>
                      <div className="ml-auto flex items-center gap-1 shrink-0">
                        <button className="btn-ghost btn-sm !px-2 !py-0.5 leading-none text-[11px]"
                          title={t('workflows.move_up_btn')} disabled={i === 0}
                          onClick={() => moveStep(i, -1)}>▲</button>
                        <button className="btn-ghost btn-sm !px-2 !py-0.5 leading-none text-[11px]"
                          title={t('workflows.move_down_btn')} disabled={i === dlg.f.steps.length - 1}
                          onClick={() => moveStep(i, 1)}>▼</button>
                        <button className="btn-ghost btn-sm !px-2 !py-0.5 leading-none text-[11px] text-bad"
                          onClick={() => delStep(i)}
                          disabled={dlg.f.steps.length <= 1} title={t('workflows.delete_step_btn')}>✕</button>
                      </div>
                    </div>
                    {/* 紧凑 label: 原始 key 进 title 悬浮, placeholder 只留示例值 (§3.2 映射表) */}
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-3 gap-y-2">
                      <div>
                        <div className="text-[11px] font-semibold text-dim mb-1">{t('workflows.field_step_id')} <span className="text-bad">*</span></div>
                        <input className="input mono" placeholder="e.g. fix" title="id"
                          value={s.id}
                          onChange={(e) => setStep(i, { id: e.target.value })} />
                      </div>
                      <div>
                        <div className="text-[11px] font-semibold text-dim mb-1">{t('workflows.field_step_name')} <span className="text-bad">*</span></div>
                        <input className="input" placeholder={t('workflows.field_step_name')} title="name"
                          value={s.name}
                          onChange={(e) => setStep(i, { name: e.target.value })} />
                      </div>
                      <div className="sm:col-span-2">
                        <div className="text-[11px] font-semibold text-dim mb-1">{t('workflows.field_summary')}</div>
                        <input className="input" placeholder={t('workflows.field_summary')} title="description"
                          maxLength={500}
                          value={s.description || ''}
                          onChange={(e) => setStep(i, { description: e.target.value })} />
                      </div>
                      <div>
                        <div className="text-[11px] font-semibold text-dim mb-1">{t('workflows.field_input_doc')}</div>
                        <input className="input" placeholder="e.g. docs/req.md, comma separated" title="input"
                          value={s.input || ''}
                          onChange={(e) => setStep(i, { input: e.target.value })} />
                      </div>
                      <div>
                        <div className="text-[11px] font-semibold text-dim mb-1">{t('workflows.field_output_doc')}</div>
                        <input className="input" placeholder="e.g. docs/design.md, comma separated" title="output"
                          value={s.output || ''}
                          onChange={(e) => setStep(i, { output: e.target.value })} />
                      </div>
                      <div>
                        <div className="text-[11px] font-semibold text-dim mb-1">{t('workflows.field_capability_tags')}</div>
                        <input className="input" placeholder="e.g. code-write, test" title="required_role_tags"
                          value={s.required_role_tags}
                          onChange={(e) => setStep(i, { required_role_tags: e.target.value })} />
                      </div>
                      <div>
                        <div className="text-[11px] font-semibold text-dim mb-1">{t('workflows.field_division_rules')}</div>
                        <select className="input cursor-pointer" value={s.split_policy || ''} title="split_policy"
                          onChange={(e) => setStep(i, { split_policy: e.target.value })}>
                          {splitPolicies(t).map((p) => <option key={p.v} value={p.v}>{p.label}</option>)}
                        </select>
                      </div>
                      <div>
                        <div className="text-[11px] font-semibold text-dim mb-1">{t('workflows.field_depends_on')}</div>
                        <input className="input" placeholder="upstream step IDs, comma separated" title="depends_on"
                          value={s.depends_on || ''}
                          onChange={(e) => setStep(i, { depends_on: e.target.value })} />
                      </div>
                      <div>
                        <div className="text-[11px] font-semibold text-dim mb-1">{t('workflows.field_failure_redirect')}</div>
                        <input className="input" placeholder="loop back targets, comma separated" title="loops"
                          value={s.loops || ''}
                          onChange={(e) => setStep(i, { loops: e.target.value })} />
                      </div>
                      <div>
                        <div className="text-[11px] font-semibold text-dim mb-1">{t('workflows.field_pass_count')}</div>
                        <input className="input" type="number" min="1" placeholder="e.g. 3, leave blank for single-pass" title="pass_threshold"
                          value={s.pass_threshold || ''}
                          onChange={(e) => setStep(i, { pass_threshold: e.target.value })} />
                      </div>
                      <div>
                        <div className="text-[11px] font-semibold text-dim mb-1">{t('workflows.field_skippable')}</div>
                        <select className="input cursor-pointer" value={s.optional || 'false'} title="optional"
                          onChange={(e) => setStep(i, { optional: e.target.value })}>
                          {optionalPolicies(t).map((p) => <option key={p.v} value={p.v}>{p.label}</option>)}
                        </select>
                      </div>
                      <div className="sm:col-span-2">
                        <div className="text-[11px] font-semibold text-dim mb-1">{t('workflows.field_skippable_scenes')}</div>
                        <input className="input" placeholder="e.g. hotfix, typo, comma separated" title="skip_when"
                          value={s.skip_when || ''}
                          onChange={(e) => setStep(i, { skip_when: e.target.value })} />
                      </div>
                    </div>
                    </div>
                  </React.Fragment>
                ))}
              </div>
            </div>
          </div>
        )}
      </Modal>

      <Confirm open={!!del} text={`${t('workflows.delete_confirm_text', { id: del })}`}
        title={t('workflows.confirm_delete_title')}
        onCancel={() => setDel(null)}
        onOk={async () => {
          try {
            const r = await api.workflowDelete(del)
            if (r && r.error) { setToast(`✗ ${t('workflows.delete_failed', { err: r.error })}`); setDel(null); return }
            setWorkflows((w) => { const n = { ...w }; delete n[del]; return n })
            setDel(null); setToast(`✓ ${t('workflows.delete_success', { id: del })}`)
            onChanged?.()
          } catch (e) { setToast(`✗ ${t('workflows.delete_failed', { err: e.message || e })}`) }
        }} />

      <Toast msg={toast} onDone={() => setToast('')} />
    </div>
  )
}

function emptyStep() {
  return { id: '', name: '', description: '', input: '', output: '',
    required_role_tags: '', split_policy: '', optional: 'false', skip_when: '',
    depends_on: '', loops: '', pass_threshold: '' }
}
function wfToForm(w) {
  return {
    id: w.id || '',
    name: w.name || '',
    description: w.description || '',
    steps: (w.steps || []).map((s) => ({
      id: s.id || '',
      name: s.name || '',
      description: s.description || '',
      input: (s.input || []).join(', '),
      output: (s.output || []).join(', '),
      required_role_tags: (s.required_role_tags || []).join(', '),
      split_policy: s.split_policy || '',
      optional: s.optional === true || s.optional === 'true' ? 'true' : 'false',
      skip_when: (s.skip_when || []).join(', '),
      depends_on: (s.depends_on || []).join(', '),
      loops: (s.loops || []).join(', '),
      pass_threshold: s.pass_threshold || '',
    })),
  }
}
